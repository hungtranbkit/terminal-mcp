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
import time
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

    def __post_init__(self) -> None:
        if not self.project_id.strip():
            raise ValueError("project feed project_id is required")
        if not is_work_session(self.lane):
            raise ValueError("project feed lane must be an explicit -work session")
        if not self.registry_path.strip():
            raise ValueError("project feed registry_path is required")
        if self.max_registry_bytes < 1_024:
            raise ValueError("project feed max_registry_bytes is too small")


class ProjectTaskFeeder:
    """Bounded, idempotent source of the next canonical project task."""

    def __init__(self, queue: Any, feeds: Iterable[ProjectFeedConfig]) -> None:
        self.queue = queue
        self._feeds = {feed.lane: feed for feed in feeds}
        self._last: dict[str, dict[str, Any]] = {}

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

        by_id = {str(task.get("id") or ""): task for task in tasks if task.get("id")}
        candidates = [task for task in tasks if self._eligible(task, feed, by_id)]
        candidates.sort(key=lambda task: self._rank(task, feed))

        skipped_terminal: list[str] = []
        for task in candidates:
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
                return self._record(session, "ENQUEUE_ERROR", task_id=task_id,
                                    detail=str(result.get("error")))
            state = str(result.get("task_status") or "")
            if result.get("deduplicated") and state in _TERMINAL_QUEUE_STATUSES:
                skipped_terminal.append(task_id)
                continue
            if result.get("deduplicated") and state in _STOP_QUEUE_STATUSES:
                return self._record(session, "EXISTING_TASK", task_id=task_id,
                                    queue_task_id=result.get("task_id"),
                                    queue_status=state)
            return self._record(session, "ENQUEUED", task_id=task_id,
                                queue_task_id=result.get("task_id"),
                                deduplicated=bool(result.get("deduplicated")))

        if skipped_terminal:
            return self._record(session, "REGISTRY_STALE",
                                detail="canonical READY/IN_PROGRESS tasks already terminal in queue",
                                skipped_terminal=skipped_terminal)
        return self._record(session, "NO_EXECUTABLE_TASK")

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
        return (preferred, in_progress, priority, task_id)

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
