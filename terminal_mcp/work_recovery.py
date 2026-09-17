"""Read-only recovery views for durable Work Runtime runs.

The service deliberately owns no state.  WorkStore remains the source for
the run/event history and the queue remains the source for task state.  This
makes constructing a new service after a process restart sufficient to
rediscover work that was already in flight.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from . import work_store as ws


_MAX_RUNS = 500
_MAX_TASKS = 200
_MAX_APPROVALS = 100
_MAX_ARTIFACTS = 100
_MAX_EVENTS = 200
_MAX_TEXT = 500
_MAX_GOAL = 2_000

_DONE = frozenset({"COMPLETED"})
_BLOCKED = frozenset({"BLOCKED", "FAILED"})
_RUNNING = frozenset({"RUNNING", "VERIFYING", "DISPATCHING"})
_WAITING = frozenset({
    "QUEUED", "READY", "PRECHECK", "WAITING_SESSION",
    "DISPATCH_UNCERTAIN", "PAUSED",
})


def _limit(value: int, maximum: int) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return 1


def _text(value: Any, maximum: int = _MAX_TEXT) -> str | None:
    if value is None:
        return None
    return str(value)[:maximum]


class WorkRecoveryService:
    """Bounded, non-consuming recovery reads over an existing WorkStore."""

    def __init__(self, store: ws.WorkStore, *, queue: Any = None) -> None:
        self.store = store
        self.queue = queue

    def _run(self, work_id: str) -> ws.WorkRun | None:
        return self.store.get_run(work_id)

    def _event(self, event: dict[str, Any]) -> dict[str, Any]:
        # Event details can contain arbitrary diagnostic text.  Recovery only
        # needs the durable transition summary, so detail is intentionally not
        # returned (and neither prompts nor terminal transcripts can leak).
        return {
            "id": int(event["id"]),
            "work_id": event.get("work_id"),
            "work_task_id": event.get("work_task_id"),
            "kind": _text(event.get("kind"), 100),
            "summary": _text(event.get("summary")),
            "actor": _text(event.get("actor"), 200),
            "created_at": event.get("created_at"),
        }

    def _last_event(self, work_id: str) -> dict[str, Any] | None:
        rows = self.store.events_for(work_id, limit=1)
        return self._event(rows[0]) if rows else None

    def _queue_states(
        self, run: ws.WorkRun, tasks: list[dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        """Return compact task state plus lane-level read failures.

        A failed or incomplete queue read is represented as UNKNOWN.  It is
        never translated into FAILED, because this method is observational.
        """
        states: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        lanes: dict[str | None, list[dict[str, Any]]] = defaultdict(list)
        for task in tasks:
            lanes[task.get("lane") or run.lane].append(task)

        for lane, lane_tasks in lanes.items():
            bound = [task for task in lane_tasks if task.get("queue_task_id")]
            for task in lane_tasks:
                if not task.get("queue_task_id"):
                    states[task["work_task_id"]] = {
                        "status": "UNBOUND", "reason": "queue task is not bound"
                    }
            if not bound:
                continue

            reason: str | None = None
            listing: dict[str, Any] = {}
            if self.queue is None:
                reason = "QUEUE_UNAVAILABLE: no queue status reader is attached"
            elif not lane:
                reason = "QUEUE_UNAVAILABLE: task has no queue lane"
            else:
                try:
                    result = self.queue.status(lane)
                    if not isinstance(result, dict):
                        reason = "QUEUE_UNAVAILABLE: invalid queue status response"
                    elif result.get("error"):
                        reason = f"QUEUE_UNAVAILABLE: {_text(result.get('error'), 200)}"
                    else:
                        listing = result
                except Exception as exc:  # noqa: BLE001 - read failure is data
                    reason = f"QUEUE_UNAVAILABLE: {type(exc).__name__}: {_text(exc, 200)}"

            if reason is not None:
                failures.append({"lane": lane, "status": "UNKNOWN", "reason": reason})
                for task in bound:
                    states[task["work_task_id"]] = {
                        "status": "UNKNOWN", "reason": reason
                    }
                continue

            by_id = {
                str(row.get("id") or row.get("task_id")): row
                for row in (listing.get("tasks") or [])
                if isinstance(row, dict) and (row.get("id") or row.get("task_id"))
            }
            for task in bound:
                queue_id = str(task["queue_task_id"])
                row = by_id.get(queue_id)
                if row is None:
                    states[task["work_task_id"]] = {
                        "status": "UNKNOWN",
                        "reason": "QUEUE_TASK_NOT_FOUND: task absent from queue status",
                    }
                    continue
                # Strict allow-list: notably excludes prompt, output, result,
                # metadata, and any terminal transcript/capture fields.
                states[task["work_task_id"]] = {
                    "status": _text(row.get("status"), 50) or "UNKNOWN",
                    "position": row.get("position"),
                    "session": _text(row.get("session"), 200),
                    "claimed_by": _text(row.get("claimed_by"), 200),
                    "updated_at": row.get("updated_at"),
                }
        return states, failures

    @staticmethod
    def _progress(
        tasks: list[dict[str, Any]], states: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        total_weight = done_weight = 0.0
        counts = {"total": len(tasks), "done": 0, "blocked": 0, "unknown": 0}
        for task in tasks:
            weight = float(task.get("weight") or 1.0)
            total_weight += weight
            status = str(states.get(task["work_task_id"], {}).get("status") or "UNKNOWN")
            if status in _DONE:
                done_weight += weight
                counts["done"] += 1
            elif status in _BLOCKED:
                counts["blocked"] += 1
            elif status in {"UNKNOWN", "UNBOUND"}:
                counts["unknown"] += 1
        return {
            "percent": int(round(done_weight * 100 / total_weight)) if total_weight else 0,
            "total": counts["total"],
            "done": counts["done"],
            "blocked": counts["blocked"],
            "unknown": counts["unknown"],
            # Match WorkService's established names as well as retaining the
            # terse recovery counters above.
            "total_tasks": counts["total"],
            "done_tasks": counts["done"],
            "blocked_tasks": counts["blocked"],
        }

    def _task_rows(
        self, tasks: list[dict[str, Any]], states: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for task in tasks[:_MAX_TASKS]:
            state = states.get(task["work_task_id"], {"status": "UNKNOWN"})
            row = {
                "work_task_id": task["work_task_id"],
                "queue_task_id": task.get("queue_task_id"),
                "title": _text(task.get("title")),
                "required": bool(task.get("required")),
                "weight": float(task.get("weight") or 1.0),
                "position": task.get("position"),
                "status": state.get("status"),
            }
            for key in ("reason", "session", "claimed_by", "updated_at"):
                if state.get(key) is not None:
                    row[key] = state[key]
            rows.append(row)
        return rows

    def recover_active(self, project_id: str | None = None, limit: int = 100) -> dict[str, Any]:
        requested = _limit(limit, _MAX_RUNS)
        # WorkStore's public listing is the discovery boundary.  Project
        # filtering is applied here so recovery also works with older stores
        # whose list_runs(project_id=...) implementation was malformed.
        runs = self.store.list_runs(include_terminal=False, limit=_MAX_RUNS)
        if project_id is not None:
            runs = [run for run in runs if run.project_id == project_id]
        recovered: list[dict[str, Any]] = []
        for run in runs[:requested]:
            tasks = self.store.tasks_for(run.work_id)
            states, failures = self._queue_states(run, tasks)
            task_rows = self._task_rows(tasks, states)
            pending = self.store.approvals_for(run.work_id, pending_only=True)
            last = self._last_event(run.work_id)
            blockers = [
                {"type": "approval", "approval_id": item["approval_id"],
                 "summary": _text(item.get("summary"))}
                for item in pending[:_MAX_APPROVALS]
            ]
            if run.state in {ws.BLOCKED, ws.FAILED, ws.PAUSED, ws.WAITING_APPROVAL}:
                blockers.append({
                    "type": "work",
                    "status": run.state,
                    "reason": _text(run.failure_reason or run.paused_reason)
                              or f"work run is {run.state}",
                })
            blockers.extend(
                {"type": "task", "work_task_id": row["work_task_id"],
                 "status": row["status"], "reason": row.get("reason")}
                for row in task_rows if row["status"] in _BLOCKED | {"UNKNOWN"}
            )
            blockers.extend({"type": "queue", **failure} for failure in failures)
            recovered.append({
                "work_id": run.work_id,
                "project_id": run.project_id,
                "title": _text(run.title),
                "state": run.state,
                "updated_at": run.updated_at,
                "progress": self._progress(tasks, states),
                "last_event_id": last["id"] if last else 0,
                "last_event_at": last["created_at"] if last else None,
                "running_tasks": [row for row in task_rows if row["status"] in _RUNNING],
                "waiting_tasks": [row for row in task_rows if row["status"] in _WAITING],
                "blockers": blockers[:_MAX_TASKS],
            })
        return {"works": recovered, "count": len(recovered)}

    def snapshot(self, work_id: str) -> dict[str, Any]:
        run = self._run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        tasks = self.store.tasks_for(work_id)
        states, failures = self._queue_states(run, tasks)
        approvals = self.store.approvals_for(work_id, pending_only=True)[:_MAX_APPROVALS]
        artifacts = self.store.artifacts_for(work_id)[-_MAX_ARTIFACTS:]
        last = self._last_event(work_id)
        return {
            "work_id": work_id,
            "title": _text(run.title),
            "state": run.state,
            "project_id": run.project_id,
            "goal": _text(run.goal, _MAX_GOAL),
            "done_criteria": [_text(item) for item in run.done_criteria[:50]],
            "progress": self._progress(tasks, states),
            "tasks": self._task_rows(tasks, states),
            "tasks_truncated": len(tasks) > _MAX_TASKS,
            "pending_approvals": [
                {"approval_id": item["approval_id"],
                 "work_task_id": item.get("work_task_id"),
                 "kind": _text(item.get("kind"), 100),
                 "summary": _text(item.get("summary")),
                 "requested_by": _text(item.get("requested_by"), 200),
                 "requested_at": item.get("requested_at")}
                for item in approvals
            ],
            "artifacts": [
                {"artifact_id": item["artifact_id"],
                 "work_task_id": item.get("work_task_id"),
                 "kind": _text(item.get("kind"), 100),
                 "reference": _text(item.get("reference")),
                 "summary": _text(item.get("summary")),
                 "created_at": item.get("created_at")}
                for item in artifacts
            ],
            "queue_failures": failures,
            "last_event_cursor": last["id"] if last else 0,
            "last_event_at": last["created_at"] if last else None,
        }

    def events_since(
        self, work_id: str, after_event_id: int = 0, limit: int = 50
    ) -> dict[str, Any]:
        if self._run(work_id) is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        try:
            cursor = max(0, int(after_event_id))
        except (TypeError, ValueError):
            cursor = 0
        size = _limit(limit, _MAX_EVENTS)
        # WorkStore returns newest-first.  Reversing the durable rows and then
        # applying the cursor makes reads ordered, repeatable and consuming
        # nothing.  Its API intentionally bounds this historical read.
        rows = reversed(self.store.events_for(work_id, limit=1000))
        selected = [self._event(row) for row in rows if int(row["id"]) > cursor][:size]
        next_cursor = selected[-1]["id"] if selected else cursor
        return {"work_id": work_id, "after_event_id": cursor,
                "events": selected, "next_cursor": next_cursor}

    def attach(self, work_id: str, after_event_id: int | None = None) -> dict[str, Any]:
        snapshot = self.snapshot(work_id)
        if snapshot.get("error"):
            return snapshot
        cursor = snapshot["last_event_cursor"] if after_event_id is None else after_event_id
        return {"snapshot": snapshot, "delta": self.events_since(work_id, cursor)}

    def recommend_resume(self, work_id: str) -> dict[str, Any]:
        run = self._run(work_id)
        if run is None:
            return {"work_id": work_id, "recommendation": "UNKNOWN",
                    "reason": "work run was not found"}
        if run.state == ws.COMPLETE:
            return {"work_id": work_id, "recommendation": "DONE",
                    "reason": "work run is COMPLETE"}

        pending = self.store.approvals_for(work_id, pending_only=True)
        if pending or run.state in {ws.WAITING_APPROVAL, ws.BLOCKED, ws.FAILED}:
            reason = (f"pending approval: {_text(pending[0].get('summary'))}" if pending
                      else f"work run is {run.state}")
            return {"work_id": work_id, "recommendation": "NEEDS_HUMAN", "reason": reason}

        tasks = self.store.tasks_for(work_id)
        states, failures = self._queue_states(run, tasks)
        if failures:
            return {"work_id": work_id, "recommendation": "UNKNOWN",
                    "reason": failures[0]["reason"]}
        statuses = {str(item.get("status") or "UNKNOWN") for item in states.values()}
        if statuses & _BLOCKED:
            status = sorted(statuses & _BLOCKED)[0]
            return {"work_id": work_id, "recommendation": "NEEDS_HUMAN",
                    "reason": f"queue task is {status}"}
        if statuses & {"RUNNING", "VERIFYING"}:
            return {"work_id": work_id, "recommendation": "WAIT",
                    "reason": "work is currently running or verifying"}
        if statuses & {"READY", "QUEUED"}:
            return {"work_id": work_id, "recommendation": "CONTINUE",
                    "reason": "ready or queued work has no blocker"}
        if "UNKNOWN" in statuses:
            reason = next((item.get("reason") for item in states.values()
                           if item.get("status") == "UNKNOWN"), None)
            return {"work_id": work_id, "recommendation": "UNKNOWN",
                    "reason": reason or "queue task state is unknown"}
        if run.state in {ws.RUNNING, ws.VERIFYING}:
            return {"work_id": work_id, "recommendation": "WAIT",
                    "reason": f"work run is {run.state}"}
        if run.state == ws.READY:
            return {"work_id": work_id, "recommendation": "CONTINUE",
                    "reason": "work run is READY and has no blocker"}
        return {"work_id": work_id, "recommendation": "UNKNOWN",
                "reason": f"no resume rule for work state {run.state}"}
