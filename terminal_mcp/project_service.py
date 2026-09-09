"""P0.7 Project APIs -- the PROJECT-level view ChatGPT asks questions of.

Every coordination primitive P0.1-P0.6 built is addressable today, but
only one layer at a time: lanes are per session, events are on the bus,
verify jobs are their own queue, locks are their own store, plans are in
the backlog. Answering "what is project X doing" meant six calls and
knowing which six.

THIS MODULE ADDS NO STATE. No table, no migration, no background loop.
It is a composition over stores that already exist, and that is the whole
design constraint: a project view that kept its own copy of anything
would immediately be a second source of truth to drift. Every number here
is read live from the store that owns it:

    lanes / tasks      queue_store   (project_id, P0.1)
    events             event_bus     (P0.2) + queue_events (derived)
    capabilities       node_registry (P0.3)
    task leases        queue_store   (P0.4)
    verification       verify_queue  (P0.5)
    resource locks     lease         (P0.6)
    plan / goals       backlog_service

NOTHING HERE STARTS ANYTHING. submit_goal writes a BACKLOG item -- an
intent -- and deliberately does not create or dispatch a queue task: the
standing production constraint is that autonomous dispatch stays behind
its existing two-gate opt-in, and a "submit a goal" API that quietly
queued work would be exactly the bypass. Turning a goal into work remains
the existing, explicit terminal_backlog_dispatch.

PAUSE/RESUME ARE NOT SYMMETRIC, ON PURPOSE. Pausing a project pauses
every lane it owns. Resuming only un-pauses the lanes THIS project pause
paused -- a lane a human paused for an unrelated reason keeps its pause
and is reported as skipped. A project-level resume that silently undid an
operator's own deliberate pause would be the single most dangerous thing
in this file; the marker in `paused_reason` is what prevents it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .queue_store import (
    BLOCKED, COMPLETED, DISPATCHING, FAILED, PAUSED, PRECHECK, QUEUED, READY,
    RUNNING, VERIFYING, WAITING_SESSION,
)

PROJECT_PAUSE_MARKER = "project-pause"
"""Written into `queue_lanes.paused_reason` as
`project-pause[<project_id>]: <reason>`. resume() matches on it so a
project resume can never un-pause a lane that something else paused --
see this module's own docstring."""

ACTIVE_TASK_STATUSES = (PRECHECK, READY, DISPATCHING, RUNNING, VERIFYING)
"""Statuses that mean a worker is actually engaged with the task right
now -- what "workers" in a project status means, as opposed to merely
having tasks."""

BLOCKED_TASK_STATUSES = (BLOCKED, FAILED, WAITING_SESSION, PAUSED)
"""Statuses a human or coordinator should look at. WAITING_SESSION is
included even though it is auto-recoverable: a project whose lanes are
all waiting on unreachable sessions is stuck, and saying so is the point
of a status API."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pause_reason(project_id: str, reason: str | None) -> str:
    return f"{PROJECT_PAUSE_MARKER}[{project_id}]: {reason or 'paused at project level'}"


def _is_our_pause(project_id: str, paused_reason: str | None) -> bool:
    return bool(paused_reason) and paused_reason.startswith(f"{PROJECT_PAUSE_MARKER}[{project_id}]")


class ProjectService:
    """Read/act on a project as one thing. Every dependency is OPTIONAL:
    a server built without a verify queue or a lock store still answers,
    with those sections reporting `null` rather than zero -- "not wired"
    and "nothing there" are different answers and a status API that
    conflated them would be lying quietly."""

    def __init__(self, *, queue: Any = None, backlog: Any = None, events: Any = None,
                 verify: Any = None, locks: Any = None, registry: Any = None) -> None:
        self.queue = queue
        self.backlog = backlog
        self.events = events
        self.verify = verify
        self.locks = locks
        self.registry = registry

    # -- helpers ---------------------------------------------------------

    def _require_queue(self) -> dict[str, Any] | None:
        if self.queue is None:
            return {"error": "QUEUE_UNAVAILABLE",
                    "detail": "this server was built without a queue service"}
        return None

    @staticmethod
    def _validate(project_id: str) -> dict[str, Any] | None:
        if not project_id or not str(project_id).strip():
            return {"error": "INVALID_REQUEST", "detail": "project_id is required"}
        return None

    # -- status ----------------------------------------------------------

    def status(self, project_id: str) -> dict[str, Any]:
        """One read answering "what is project X doing right now".

        Deliberately a SNAPSHOT of live state, never a cached rollup --
        see the module docstring. `report()` is the one that looks
        backwards over a window."""
        if error := self._validate(project_id):
            return error
        if error := self._require_queue():
            return error
        store = self.queue.store
        lanes = store.lanes_for_project(project_id)
        counts = store.project_task_counts(project_id)
        tasks = store.list_tasks_for_project(project_id, limit=500)

        workers, blockers = [], []
        for task in tasks:
            if task.status in ACTIVE_TASK_STATUSES and task.claimed_by:
                workers.append({"worker": task.claimed_by, "task_id": task.id,
                                "session": task.session, "status": task.status,
                                "lease_expires_at": task.lease_expires_at,
                                "title": task.title})
            if task.status in BLOCKED_TASK_STATUSES:
                blockers.append({"task_id": task.id, "session": task.session,
                                 "status": task.status, "title": task.title,
                                 "reason": task.last_error or task.coordinator_reason})

        lane_rows = []
        for session in lanes:
            lane = store.lane_status(session)
            lane_rows.append({
                "session": session,
                "paused": bool(lane.get("paused")),
                "paused_reason": lane.get("paused_reason"),
                "paused_by_project": _is_our_pause(project_id, lane.get("paused_reason")),
                "auto_dispatch_enabled": bool(lane.get("auto_dispatch_enabled")),
                "pending_count": lane.get("queued_count", lane.get("pending_count")),
            })

        return {
            "project_id": project_id,
            "lanes": lane_rows,
            "lane_count": len(lane_rows),
            "paused": bool(lane_rows) and all(row["paused"] for row in lane_rows),
            "task_counts": counts,
            "open_tasks": sum(n for status, n in counts.items()
                              if status not in (COMPLETED, "SKIPPED", "CANCELLED")),
            "workers": workers,
            "blockers": blockers,
            "verification": self._verify_section(project_id),
            "resource_locks": self._locks_section(project_id),
            "events": self._events_section(project_id),
            "backlog": self._backlog_section(project_id),
        }

    def _verify_section(self, project_id: str) -> dict[str, Any] | None:
        if self.verify is None:
            return None
        stats = self.verify.stats(project_id=project_id)
        pending = []
        for job in self.verify.list_jobs(project_id=project_id, status="VERIFY_PENDING", limit=25):
            entry = {"job_id": job.id, "task_id": job.task_id,
                     "required_capabilities": list(job.required_capabilities)}
            # Pass OUR registry: a VerifyQueue built without one would
            # otherwise report "routability unknown" for every pending job,
            # even though this service is holding the registry that could
            # answer it.
            entry["routability"] = self.verify.routability(job, registry=self.registry)
            pending.append(entry)
        return {"stats": stats, "pending": pending}

    def _locks_section(self, project_id: str) -> list[dict[str, Any]] | None:
        if self.locks is None:
            return None
        return self.locks.list_locks(project_id=project_id)

    def _events_section(self, project_id: str) -> dict[str, Any] | None:
        if self.events is None:
            return None
        return {"stats": self.events.stats(project_id=project_id)}

    def _backlog_section(self, project_id: str) -> dict[str, Any] | None:
        if self.backlog is None:
            return None
        result = self.backlog.get(project_id=project_id)
        if "error" in result:
            return {"error": result["error"], "detail": result.get("detail")}
        return {"counts": result.get("counts", {}), "open_total": result.get("open_total", 0),
                "total": result.get("total", 0), "revision": result.get("revision")}

    # -- goals -----------------------------------------------------------

    def submit_goal(self, project_id: str, goal: str, *, priority: str = "P2",
                    description: str | None = None, acceptance_criteria: list[str] | None = None,
                    type: str = "feature", actor: str = "mcp") -> dict[str, Any]:
        """Record an INTENT for a project.

        Creates a backlog item, NOT a queue task, and never dispatches --
        see the module docstring. The returned item id is what
        terminal_backlog_dispatch takes when a human or coordinator
        decides it should become real work."""
        if error := self._validate(project_id):
            return error
        if not goal or not str(goal).strip():
            return {"error": "INVALID_REQUEST", "detail": "goal is required"}
        if self.backlog is None:
            return {"error": "BACKLOG_UNAVAILABLE",
                    "detail": "this server was built without a backlog service, so a goal has "
                              "nowhere durable to live"}
        result = self.backlog.add(
            project_id=project_id, source=actor,
            tasks=[{"title": str(goal).strip(), "description": description or "",
                    "priority": priority, "type": type,
                    "acceptance_criteria": list(acceptance_criteria or [])}])
        if "error" in result:
            return result
        created = (result.get("created") or [])
        return {"project_id": project_id, "submitted": True,
                "item": created[0] if created else None,
                "revision": result.get("revision"),
                "next_step": "terminal_backlog_dispatch turns this into a queue task when you "
                             "decide it should run -- submitting a goal never dispatches by itself"}

    # -- events / report --------------------------------------------------

    def project_events(self, project_id: str, *, since_seq: int | None = None,
                       types: list[str] | None = None, limit: int = 100) -> dict[str, Any]:
        """Both event streams a project has, kept clearly apart.

        `bus` is the P0.2 event bus (claimable, leased work signals).
        `queue` is queue_events (the task state-machine's own audit trail,
        derived for this project -- it has no project column of its own).
        They are NOT merged into one list: they have different id spaces
        and different meanings, and interleaving them by timestamp would
        invent an ordering neither guarantees."""
        if error := self._validate(project_id):
            return error
        out: dict[str, Any] = {"project_id": project_id}
        out["bus"] = (self.events.list_events(project_id=project_id, types=types,
                                              since_seq=since_seq, limit=limit)
                      if self.events is not None else None)
        if self.queue is not None:
            events = self.queue.store.project_events(project_id, limit=limit)
            if types:
                wanted = set(types)
                events = [e for e in events if e.get("event_type") in wanted]
            out["queue"] = events
        else:
            out["queue"] = None
        return out

    def report(self, project_id: str, *, window_hours: float = 24.0) -> dict[str, Any]:
        """What actually HAPPENED in a window, as opposed to what the
        queue looks like now (`status`).

        Throughput is counted from state TRANSITIONS, not from current
        statuses: a task that completed and was later retried is still a
        completion that happened, and a snapshot would have lost it."""
        if error := self._validate(project_id):
            return error
        if error := self._require_queue():
            return error
        since = _iso(_now() - timedelta(hours=float(window_hours)))
        transitions = self.queue.store.project_transition_counts(project_id, since=since)
        verify_stats = self.verify.stats(project_id=project_id) if self.verify is not None else None
        return {
            "project_id": project_id,
            "window_hours": window_hours,
            "since": since,
            "transitions": transitions,
            "throughput": {
                "completed": transitions.get(COMPLETED, 0),
                "failed": transitions.get(FAILED, 0),
                "blocked": transitions.get(BLOCKED, 0),
                "dispatched": transitions.get(DISPATCHING, 0),
                "waiting_session": transitions.get(WAITING_SESSION, 0),
            },
            "verification": verify_stats,
            "current": self.queue.store.project_task_counts(project_id),
            "note": "throughput counts TRANSITIONS in the window; `current` is a snapshot of now",
        }

    # -- pause / resume ---------------------------------------------------

    def pause(self, project_id: str, *, reason: str | None = None,
              actor: str = "mcp") -> dict[str, Any]:
        """Pause every lane this project owns.

        A lane already paused is left exactly as it is and reported --
        overwriting its reason would destroy why someone else paused it."""
        if error := self._validate(project_id):
            return error
        if error := self._require_queue():
            return error
        store = self.queue.store
        paused, already = [], []
        for session in store.lanes_for_project(project_id):
            lane = store.lane_status(session)
            if lane.get("paused"):
                already.append({"session": session, "paused_reason": lane.get("paused_reason")})
                continue
            store.pause_lane(session, reason=_pause_reason(project_id, reason))
            paused.append(session)
        return {"project_id": project_id, "paused": paused, "paused_count": len(paused),
                "already_paused": already, "actor": actor,
                "detail": "lanes already paused were left untouched, with their own reason intact"}

    def resume(self, project_id: str, *, actor: str = "mcp", force: bool = False) -> dict[str, Any]:
        """Resume the lanes THIS project's pause paused.

        A lane paused by something else -- an operator, a coordinator
        NEEDS_HUMAN decision -- is SKIPPED and reported with its reason,
        because silently undoing a deliberate pause is the worst thing
        this API could do. `force=True` overrides that, and says so in the
        result: it is an explicit decision to override someone else, not a
        convenience default."""
        if error := self._validate(project_id):
            return error
        if error := self._require_queue():
            return error
        store = self.queue.store
        resumed, skipped, not_paused = [], [], []
        for session in store.lanes_for_project(project_id):
            lane = store.lane_status(session)
            if not lane.get("paused"):
                not_paused.append(session)
                continue
            if _is_our_pause(project_id, lane.get("paused_reason")) or force:
                store.resume_lane(session)
                resumed.append(session)
            else:
                skipped.append({"session": session, "paused_reason": lane.get("paused_reason"),
                                "detail": "paused by something other than this project -- not resumed"})
        return {"project_id": project_id, "resumed": resumed, "resumed_count": len(resumed),
                "skipped": skipped, "already_running": not_paused,
                "forced": bool(force), "actor": actor}

    # -- assignment -------------------------------------------------------

    def assign(self, project_id: str, task_id: str, *, session: str | None = None,
               node_id: str | None = None, capabilities: list[str] | None = None,
               actor: str = "mcp") -> dict[str, Any]:
        """Route one of a project's tasks to a place that can run it.

        Three ways to say where, in precedence order: an explicit
        `session` (assign there), a `node_id` (assign to one of that
        node's lanes), or `capabilities` (pick a node that reports all of
        them). The capability path uses the SAME matcher P0.5 verifier
        routing uses -- one definition of "which node can do this".

        With capabilities and no session this RESOLVES ONLY: it reports
        the candidate nodes and does not move the task, because choosing
        a lane on a remote node is a decision this facade should not make
        silently on a caller's behalf."""
        if error := self._validate(project_id):
            return error
        if error := self._require_queue():
            return error
        task = self.queue.store.get_task(task_id)
        if task is None:
            return {"error": "TASK_NOT_FOUND", "task_id": task_id}
        if task.project_id and task.project_id != project_id:
            return {"error": "TASK_NOT_IN_PROJECT", "task_id": task_id,
                    "task_project_id": task.project_id, "project_id": project_id,
                    "detail": "refusing to assign another project's task"}

        if session:
            result = self.queue.assign_task(task_id, session)
            if "error" not in result and not task.project_id:
                # An unscoped task joining a project's lane gains the
                # project, so the next status() call sees it.
                self.queue.store.set_task_project(task_id, project_id)
            return {"project_id": project_id, "assigned_to": session, "actor": actor,
                    "result": result}

        candidates = self._candidate_nodes(node_id=node_id, capabilities=capabilities)
        if isinstance(candidates, dict):
            return candidates
        return {"project_id": project_id, "task_id": task_id, "assigned": False,
                "candidates": candidates, "actor": actor,
                "detail": "resolved candidate nodes only -- pass `session` to actually move the "
                          "task, so the choice of lane stays explicit"}

    def _candidate_nodes(self, *, node_id: str | None,
                         capabilities: list[str] | None) -> list[dict[str, Any]] | dict[str, Any]:
        if self.registry is None:
            return {"error": "REGISTRY_UNAVAILABLE",
                    "detail": "no node registry wired, so capability routing cannot be resolved"}
        from .verify_queue import match_nodes_by_capability, node_capability_set
        nodes = self.registry.list()
        if node_id:
            nodes = [n for n in nodes if n.id == node_id]
        matched = match_nodes_by_capability(nodes, tuple(capabilities or ()))
        return [{"node_id": n.id, "platform": n.platform, "status": n.status,
                 "capabilities": sorted(node_capability_set(n))} for n in matched]
