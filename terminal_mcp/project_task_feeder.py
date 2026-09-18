"""Feed canonical project TASKS.json records into an opted-in Work lane.

This is the missing bridge between project-level planning and Terminal MCP's
already-existing 3-second QueueLoop. QueueLoop can already drive task N -> N+1
with no ChatGPT involvement *once both tasks are durable queue rows*. What it
could not do was discover the next project task after the lane queue became
empty.

Safety rules:
- OFF unless an explicit ProjectFeedConfig names project, lane and registry.
- Only '-work' lanes are eligible.
- Registry dependencies are rechecked fail-closed; READY alone is not trusted.
- IN_PROGRESS is resumable only when its owner exactly matches the configured
  lane/owner.
- QueueService.enqueue() is the only persistence path, so request-key
  idempotency and normal queue invariants stay authoritative.
- This module never edits TASKS.json and never marks anything DONE.
"""
from __future__ import annotations

import json
import hashlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .work_eligibility import is_work_session

_TERMINAL_QUEUE_STATUSES = frozenset({"COMPLETED", "CANCELLED", "SKIPPED"})
_STOP_QUEUE_STATUSES = frozenset({
    "BLOCKED", "FAILED", "PRECHECK", "READY", "DISPATCHING", "RUNNING",
    "VERIFYING", "DISPATCH_UNCERTAIN", "WAITING_SESSION", "QUEUED",
})
_PRIORITY = {"P0": 100, "P1": 90, "P2": 80, "P3": 70, "P4": 60,
             "P5": 50, "P6": 40, "P7": 30, "P8": 20}


@dataclass(frozen=True)
class ProjectFeedConfig:
    project_id: str
    lane: str
    registry_path: str
    owner: str | None = None
    preferred_task_ids: tuple[str, ...] = ()
    max_registry_bytes: int = 5_000_000
    packet_min_minutes: int = 45
    packet_max_minutes: int = 90
    packet_lease_seconds: int = 7_200

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError("project feed project_id is required")
        if not is_work_session(self.lane):
            raise ValueError("project feed lane must be an explicit -work session")
        if not self.registry_path.strip():
            raise ValueError("project feed registry_path is required")
        if self.max_registry_bytes < 1_024:
            raise ValueError("project feed max_registry_bytes is too small")
        if not 1 <= self.packet_min_minutes <= self.packet_max_minutes:
            raise ValueError("invalid project packet duration bounds")


class ProjectTaskFeeder:
    """Bounded, idempotent source of the next canonical project task."""

    def __init__(self, queue: Any, feeds: Iterable[ProjectFeedConfig]) -> None:
        self.queue = queue
        self._feeds = {feed.lane: feed for feed in feeds}
        self._last: dict[str, dict[str, Any]] = {}
        self._idle_since: dict[str, float] = {}

    def configured_lanes(self) -> tuple[str, ...]:
        return tuple(sorted(self._feeds))

    def status(self) -> dict[str, Any]:
        return {"configured_lanes": list(self.configured_lanes()),
                "last": dict(self._last)}

    def feed_if_idle(self, session: str) -> dict[str, Any]:
        feed = self._feeds.get(session)
        if feed is None:
            return self._record(session, "NOT_CONFIGURED")
        if not is_work_session(session):
            return self._record(session, "REFUSED_NOT_WORK_SESSION")

        try:
            tasks = self._load(feed)
        except Exception as exc:  # fail closed; never invent a task
            return self._record(session, "REGISTRY_ERROR",
                                detail=f"{type(exc).__name__}: {exc}")

        idle_seconds = self._idle_seconds(session)
        worker = feed.owner or feed.lane
        active = self._active_packet(worker)
        stale_recovered = False
        if active and self._lease_expired(active.get("lease_expires_at")):
            self.queue.store.update_project_packet(active["packet_id"], state="EXPIRED",
                                                   checkpoint={"recovered": True, "reason": "stale_worker_lease"})
            active = None
            stale_recovered = True
        by_id = {str(task.get("id") or ""): task for task in tasks if task.get("id")}
        candidates = [task for task in tasks if self._eligible(task, feed, by_id)]
        # A canonical READY record can lag the durable queue. Never reserve a
        # second packet for a queue row that already reached a terminal state.
        terminal_existing = [str(task["id"]) for task in candidates
                             if self._queue_task_terminal(feed.project_id, str(task["id"]))]
        queued_existing = [] if stale_recovered else [str(task["id"]) for task in candidates
                           if self._queue_task_active(feed.project_id, str(task["id"]))]
        candidates = [task for task in candidates
                      if str(task["id"]) not in set(terminal_existing + queued_existing)]
        candidates.sort(key=lambda task: self._rank(task, feed))

        if active:
            progress = self._packet_progress(active)
            if progress == "COMPLETE":
                self.queue.store.update_project_packet(active["packet_id"], state="COMPLETED",
                                                       checkpoint={"completed": True})
            else:
                # A feeder cycle is also the worker heartbeat for a packet;
                # extending the lease is bounded to the configured packet
                # window and never creates a second packet.
                feed_lease = (datetime.now(timezone.utc) + timedelta(seconds=feed.packet_lease_seconds)).isoformat()
                self.queue.store.update_project_packet(active["packet_id"],
                                                       lease_expires_at=feed_lease)
                return self._record(session, "PACKET_ACTIVE", packet_id=active["packet_id"],
                                    task_ids=active["task_ids"], idle_seconds=idle_seconds,
                                    why_not_dispatched="worker_packet_active",
                                    next_candidate=(str(candidates[0]["id"]) if candidates else None))

        if not candidates:
            blocked = self._ownership_conflicts(tasks, feed)
            if blocked:
                return self._record(session, "REASSIGN_REQUIRED", task_ids=blocked,
                                    idle_seconds=idle_seconds, why_not_dispatched="owned_by_other_active_worker",
                                    next_candidate=blocked[0])
            if terminal_existing:
                return self._record(session, "REGISTRY_STALE", skipped_terminal=terminal_existing,
                                    idle_seconds=idle_seconds, why_not_dispatched="queue_row_terminal", next_candidate=None)
            return self._record(session, "NO_EXECUTABLE_TASK", idle_seconds=idle_seconds,
                                why_not_dispatched="dependencies_or_status_not_ready", next_candidate=None)

        selected = self._select_packet(candidates, by_id, feed)
        task_ids = [str(task["id"]) for task in selected]
        task_shas = {task_id: self._task_sha(task) for task_id, task in
                     ((str(task["id"]), task) for task in selected)}
        packet_key = "project-packet:" + feed.project_id + ":" + worker + ":" + \
            hashlib.sha256(json.dumps(task_shas, sort_keys=True).encode()).hexdigest()[:24]
        if stale_recovered:
            packet_key += ":recovery"
        packet_id = uuid.uuid4().hex
        lease = (datetime.now(timezone.utc) + timedelta(seconds=feed.packet_lease_seconds)).isoformat()
        telemetry = {"idle_seconds": 0, "task_ids": task_ids,
                     "next_candidate": task_ids[0] if task_ids else None,
                     "why_not_dispatched": None}
        reserved = self._reserve_packet(packet_id, feed, worker, task_ids, task_shas, packet_key, lease, telemetry)
        if reserved.get("blocked"):
            return self._record(session, "PACKET_ACTIVE", **reserved)
        if reserved.get("deduplicated"):
            return self._record(session, "EXISTING_PACKET", **reserved)

        skipped_terminal: list[str] = []
        queue_ids: list[str] = []
        for task in selected:
            task_id = str(task["id"])
            result = self.queue.enqueue(
                session,
                self._prompt(task),
                title=str(task.get("title") or task_id),
                priority=self._queue_priority(task),
                metadata={
                    "project": feed.project_id,
                    "canonical_task_id": task_id,
                    "canonical_registry": str(Path(feed.registry_path).expanduser()),
                    "continuous_dispatch": True,
                },
                request_key=f"project-feed:{feed.project_id}:{task_id}",
            )
            if result.get("error"):
                self.queue.store.update_project_packet(packet_id, state="BLOCKED",
                                                       checkpoint={"error": str(result.get("error"))})
                return self._record(session, "ENQUEUE_ERROR", task_id=task_id, packet_id=packet_id,
                                    detail=str(result.get("error")))
            queue_ids.append(str(result.get("task_id")))
            state = str(result.get("task_status") or "")
            if result.get("deduplicated") and state in _TERMINAL_QUEUE_STATUSES:
                skipped_terminal.append(task_id)
                continue
            if result.get("deduplicated") and state in _STOP_QUEUE_STATUSES:
                self.queue.store.update_project_packet(packet_id, state="RUNNING",
                                                       checkpoint={"queue_task_ids": queue_ids},
                                                       telemetry=telemetry, lease_expires_at=lease)
                return self._record(session, "EXISTING_TASK", task_id=task_id,
                                    packet_id=packet_id, task_ids=task_ids,
                                    queue_task_id=result.get("task_id"),
                                    queue_status=state)
        self.queue.store.update_project_packet(packet_id, state="RUNNING",
                                               checkpoint={"queue_task_ids": queue_ids},
                                               telemetry=telemetry, lease_expires_at=lease)
        self._idle_since.pop(session, None)
        return self._record(session, "ENQUEUED", task_id=task_ids[0], task_ids=task_ids,
                            packet_id=packet_id, queue_task_id=queue_ids[0],
                            queue_task_ids=queue_ids, deduplicated=False,
                            packet_minutes=sum(self._estimate_minutes(t) for t in selected))

    def _active_packet(self, worker: str) -> dict[str, Any] | None:
        store = getattr(self.queue, "store", None)
        return store.active_project_packet(worker) if store is not None and hasattr(store, "active_project_packet") else None

    @staticmethod
    def _lease_expired(value: str | None) -> bool:
        if not value:
            return True
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.now(timezone.utc)
        except ValueError:
            return True

    def _packet_progress(self, packet: dict[str, Any]) -> str:
        store = getattr(self.queue, "store", None)
        if store is None:
            return "ACTIVE"
        terminal = {"COMPLETED", "CANCELLED", "SKIPPED"}
        for task_id in packet.get("task_ids", []):
            key = f"project-feed:{packet['project_id']}:{task_id}"
            row = store.task_by_request_key(key)
            if row is None or row.get("status") not in terminal:
                return "ACTIVE"
        return "COMPLETE"

    def _queue_task_terminal(self, project_id: str, task_id: str) -> bool:
        store = getattr(self.queue, "store", None)
        if store is None:
            return False
        row = store.task_by_request_key(f"project-feed:{project_id}:{task_id}")
        return bool(row and row.get("status") in _TERMINAL_QUEUE_STATUSES)

    def _queue_task_active(self, project_id: str, task_id: str) -> bool:
        store = getattr(self.queue, "store", None)
        if store is None:
            return False
        row = store.task_by_request_key(f"project-feed:{project_id}:{task_id}")
        return bool(row and row.get("status") not in _TERMINAL_QUEUE_STATUSES)

    def _reserve_packet(self, packet_id: str, feed: ProjectFeedConfig, worker: str,
                        task_ids: list[str], task_shas: dict[str, str], request_key: str,
                        lease: str, telemetry: dict[str, Any]) -> dict[str, Any]:
        store = getattr(self.queue, "store", None)
        if store is None or not hasattr(store, "reserve_project_packet"):
            return {"packet_id": packet_id, "task_ids": task_ids}
        return store.reserve_project_packet(packet_id=packet_id, project_id=feed.project_id,
                                            lane=feed.lane, worker=worker, task_ids=task_ids,
                                            task_shas=task_shas, request_key=request_key,
                                            lease_expires_at=lease, telemetry=telemetry)

    @staticmethod
    def _task_sha(task: dict[str, Any]) -> str:
        canonical = json.dumps(task, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _estimate_minutes(task: dict[str, Any]) -> int:
        for key in ("estimated_minutes", "duration_minutes", "timebox_minutes"):
            try:
                value = int(task.get(key))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        return 15

    def _select_packet(self, candidates: list[dict[str, Any]], by_id: dict[str, dict[str, Any]],
                       feed: ProjectFeedConfig) -> list[dict[str, Any]]:
        selected = [candidates[0]]
        # Only explicit estimates are bundleable. This preserves the legacy
        # one-task feeder for registries that provide no sizing metadata.
        if not any(any(key in task for key in ("estimated_minutes", "duration_minutes", "timebox_minutes"))
                   for task in candidates):
            return selected
        total = self._estimate_minutes(selected[0])
        scopes = self._scopes(selected[0])
        ordered_candidates = list(candidates)
        # A directly dependent READY task may be included after its parent in
        # the same packet. It is intentionally not eligible on its own; the
        # ordered packet is the only way it can cross this boundary safely.
        selected_ids = {str(task["id"]) for task in selected}
        for task in by_id.values():
            if str(task.get("status") or "") == "READY" and str(task.get("id")) not in selected_ids:
                deps = [str(dep) for dep in (task.get("dependencies") or [])]
                if deps and all(dep in selected_ids for dep in deps):
                    ordered_candidates.append(task)
        for task in ordered_candidates[1:]:
            if len(selected) >= 5:
                break
            if task.get("dependencies") and not all(str(dep) in {str(t["id"]) for t in selected} or
                                                     str(by_id.get(str(dep), {}).get("status")) == "DONE"
                                                     for dep in task.get("dependencies", [])):
                continue
            if self._scopes_overlap(self._scopes(task), scopes):
                continue
            estimate = self._estimate_minutes(task)
            if total + estimate > feed.packet_max_minutes:
                continue
            selected.append(task); total += estimate; scopes |= self._scopes(task)
            if total >= feed.packet_min_minutes:
                break
        return selected

    @staticmethod
    def _scopes(task: dict[str, Any]) -> set[str]:
        values = task.get("scope") or task.get("files") or task.get("owned_files") or task.get("file_scope") or []
        return {str(value).strip().rstrip("/") for value in values if str(value).strip()} if isinstance(values, list) else set()

    @staticmethod
    def _scopes_overlap(left: set[str], right: set[str]) -> bool:
        return any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                   for a in left for b in right)

    def _ownership_conflicts(self, tasks: list[dict[str, Any]], feed: ProjectFeedConfig) -> list[str]:
        owner = feed.owner or feed.lane
        return [str(task["id"]) for task in tasks
                if str(task.get("status") or "") == "READY" and task.get("owner")
                and str(task.get("owner")) != owner]

    def _idle_seconds(self, session: str) -> int:
        started = self._idle_since.setdefault(session, time.monotonic())
        return max(0, int(time.monotonic() - started))

    def _load(self, feed: ProjectFeedConfig) -> list[dict[str, Any]]:
        path = Path(feed.registry_path).expanduser().resolve()
        stat = path.stat()
        if not path.is_file():
            raise ValueError(f"registry is not a file: {path}")
        if stat.st_size > feed.max_registry_bytes:
            raise ValueError(f"registry exceeds {feed.max_registry_bytes} bytes")
        raw = json.loads(path.read_text(encoding="utf-8"))
        tasks = raw.get("tasks") if isinstance(raw, dict) else None
        if not isinstance(tasks, list):
            raise ValueError("registry must contain a tasks array")
        return [task for task in tasks if isinstance(task, dict)]

    @staticmethod
    def _deps_done(task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
        deps = task.get("dependencies") or []
        if not isinstance(deps, list):
            return False
        for dep in deps:
            row = by_id.get(str(dep))
            if row is None or str(row.get("status") or "") != "DONE":
                return False
        return True

    def _eligible(self, task: dict[str, Any], feed: ProjectFeedConfig,
                  by_id: dict[str, dict[str, Any]]) -> bool:
        status = str(task.get("status") or "")
        if status == "READY":
            expected_owner = feed.owner or feed.lane
            if task.get("owner") and str(task.get("owner")) != expected_owner:
                return False
            return self._deps_done(task, by_id)
        if status != "IN_PROGRESS":
            return False
        expected_owner = feed.owner or feed.lane
        return str(task.get("owner") or "") == expected_owner and self._deps_done(task, by_id)

    @staticmethod
    def _queue_priority(task: dict[str, Any]) -> int:
        return _PRIORITY.get(str(task.get("priority") or ""), 0)

    @staticmethod
    def _rank(task: dict[str, Any], feed: ProjectFeedConfig) -> tuple[int, int, int, str]:
        task_id = str(task.get("id") or "")
        try:
            preferred = feed.preferred_task_ids.index(task_id)
        except ValueError:
            preferred = len(feed.preferred_task_ids) + 1000
        in_progress = 0 if str(task.get("status") or "") == "IN_PROGRESS" else 1
        priority = -ProjectTaskFeeder._queue_priority(task)
        # Resume matching-owner in-flight work before starting any preferred
        # READY task. Preferred ids only order otherwise-equivalent work.
        return (in_progress, preferred, priority, task_id)

    @staticmethod
    def _prompt(task: dict[str, Any]) -> str:
        task_id = str(task.get("id") or "")
        title = str(task.get("title") or "")
        scope = task.get("scope") if isinstance(task.get("scope"), list) else []
        acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), list) else []
        refs = task.get("contractReferences") if isinstance(task.get("contractReferences"), list) else []
        lines = [
            f"Execute canonical project task {task_id}: {title}",
            "Use the repository's canonical task record as authority. Execute; do not create a duplicate plan.",
        ]
        if scope:
            lines.append("\nOwned scope:")
            lines.extend(f"- {item}" for item in scope[:100])
        if acceptance:
            lines.append("\nAcceptance:")
            lines.extend(f"- {item}" for item in acceptance[:100])
        if refs:
            lines.append("\nRead before implementation:")
            lines.extend(f"- {item}" for item in refs[:50])
        lines.append(
            "\nUpdate canonical task/checkpoint evidence before stopping. "
            "Do not bypass credentials, approvals, destructive-action gates or production safety."
        )
        return "\n".join(lines)

    def _record(self, session: str, action: str, **extra: Any) -> dict[str, Any]:
        result = {"session": session, "action": action,
                  "at_monotonic": round(time.monotonic(), 3), **extra}
        self._last[session] = result
        return result
