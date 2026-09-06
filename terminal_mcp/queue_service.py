"""Supervisor Queue v2 -- the MCP-facing service layer over queue_store.py
(task: "Supervisor Queue v2 cho Terminal MCP").

This is CRUD + read-only status only in this phase: every method here
either mutates QueueStore's own durable state (set/append/pause/resume/
retry/skip/cancel/reorder/clear) or reads it back (status/list_all/
events) -- none of it sends anything to any session, watches anything,
or runs on a timer. The autonomous dispatch loop that actually
transitions QUEUED -> DISPATCHING -> RUNNING by calling through
TerminalService.terminal_send_text (reusing its idempotency_key/
delivery_state machinery per the user's own explicit "reuse existing
reliable submission... instead of writing a second system" instruction)
is queue_engine.py, layered on top of this -- see its own module
docstring once added.

session IS the lane, per the task's own explicit semantics -- every
method below takes a bare session name, never a separate queue/lane id
ChatGPT would have to create first.

SAFETY (explicit, repeated user constraint for the whole feature): this
service has no allow/deny-list of session names -- same posture as
queue_store.py (see its own docstring's SAFETY note). The constraint
that `window`/`window2` must never be queued until the acceptance demo
passes and the user/ChatGPT explicitly confirms is enforced by NEVER
CALLING set_tasks/append_tasks against those names during this
feature's own development, not by a technical guard in this file."""
from __future__ import annotations

from typing import Any

from .permissions import valid_session_name
from .queue_store import VERIFYING, InvalidTransitionError, QueueStore


class QueueService:
    def __init__(self, store: QueueStore | None = None) -> None:
        self.store = store or QueueStore()

    def _validate_session(self, session: str) -> dict[str, Any] | None:
        if not session or not valid_session_name(session):
            return {"error": "INVALID_SESSION_NAME", "session": session}
        return None

    def _validate_tasks(self, tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not isinstance(tasks, list) or not tasks:
            return {"error": "TASKS_REQUIRED"}
        for task in tasks:
            if not isinstance(task, dict) or not task.get("prompt"):
                return {"error": "TASK_PROMPT_REQUIRED", "task": task}
        return None

    def set_tasks(self, session: str, tasks: list[dict[str, Any]], *, replace_pending: bool = True) -> dict[str, Any]:
        """queue_set: pushes `tasks` (an array) into `session`'s lane in
        ONE call. Each task's own prompt is stored VERBATIM (item 7:
        "MCP chỉ được thêm một wrapper rất ngắn... không tự viết lại yêu
        cầu nghiệp vụ") -- nothing here rewrites, trims, or wraps it;
        any completion-marker wrapper is queue_engine.py's job, applied
        only at dispatch time, never persisted over the original prompt."""
        if error := self._validate_session(session):
            return error
        if error := self._validate_tasks(tasks):
            return error
        ids = self.store.set_tasks(session, tasks, replace_pending=replace_pending)
        return {"session": session, "task_ids": ids, "replace_pending": replace_pending}

    def append_tasks(self, session: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        if error := self._validate_tasks(tasks):
            return error
        ids = self.store.append_tasks(session, tasks)
        return {"session": session, "task_ids": ids}

    def status(self, session: str) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        return self.store.lane_status(session)

    def list_all(self) -> dict[str, Any]:
        return {"lanes": self.store.list_all_lanes()}

    def pause(self, session: str, *, reason: str | None = None) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        self.store.pause_lane(session, reason=reason)
        return self.store.lane_status(session)

    def resume(self, session: str) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        self.store.resume_lane(session)
        return self.store.lane_status(session)

    def retry(self, session: str, task_id: str) -> dict[str, Any]:
        return self._task_action(session, task_id, self.store.retry_task)

    def skip(self, session: str, task_id: str) -> dict[str, Any]:
        return self._task_action(session, task_id, self.store.skip_task)

    def cancel(self, session: str, task_id: str) -> dict[str, Any]:
        return self._task_action(session, task_id, self.store.cancel_task)

    def _task_action(self, session: str, task_id: str, action) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        task = self.store.get_task(task_id)
        if task is None or task.session != session:
            return {"error": "TASK_NOT_FOUND", "session": session, "task_id": task_id}
        try:
            updated = action(task_id)
        except InvalidTransitionError as exc:
            return {"error": "INVALID_TRANSITION", "session": session, "task_id": task_id, "reason": str(exc)}
        return {"session": session, "task": updated.to_dict()}

    def reorder(self, session: str, ordered_task_ids: list[str]) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        self.store.reorder_tasks(session, ordered_task_ids)
        return self.store.lane_status(session)

    def clear(self, session: str, *, only_pending: bool = True) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        cleared = self.store.clear_tasks(session, only_pending=only_pending)
        return {"session": session, "cleared": cleared, **self.store.lane_status(session)}

    def events(self, session: str, limit: int = 50) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        return {"session": session, "events": self.store.list_events(session, limit)}

    def verify(self, session: str, task_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """Phase 2 (item 11's own explicit fallback): a human or ChatGPT
        supplies verification evidence directly for a task stuck in
        VERIFYING with no completion marker ever appearing (e.g. an
        agent that finished but didn't echo the marker correctly, or a
        task whose own completion_policy never asked for one) --
        moves it to COMPLETED, evidence attached, exactly like a real
        marker-verified completion would. This is NEVER automatic and
        NEVER a bare heuristic: it requires an explicit caller and
        non-empty evidence (queue_store.mark_completed_with_evidence's
        own requirement)."""
        if error := self._validate_session(session):
            return error
        task = self.store.get_task(task_id)
        if task is None or task.session != session:
            return {"error": "TASK_NOT_FOUND", "session": session, "task_id": task_id}
        if task.status != VERIFYING:
            return {"error": "INVALID_TRANSITION", "session": session, "task_id": task_id,
                    "reason": f"task is {task.status}, not VERIFYING -- nothing to verify"}
        if not evidence:
            return {"error": "EVIDENCE_REQUIRED", "session": session, "task_id": task_id}
        updated = self.store.mark_completed_with_evidence(task_id, evidence=evidence)
        return {"session": session, "task": updated.to_dict()}

    def set_auto_dispatch(self, session: str, enabled: bool) -> dict[str, Any]:
        """Phase 2's explicit per-session opt-in for an AUTOMATIC
        background dispatch loop (task's own constraint: "Không bật
        auto-dispatch cho session production hiện hữu mặc định...
        opt-in per session"). Manually calling terminal_queue_run_once
        is never gated by this -- it only controls whether an
        unattended poll loop may touch this lane on its own (see
        queue_engine.py's own module docstring; no such automatic loop
        is wired to run in this phase regardless of this flag -- see the
        Phase 2 report's own limitations section)."""
        if error := self._validate_session(session):
            return error
        self.store.set_auto_dispatch(session, enabled)
        return self.store.lane_status(session)
