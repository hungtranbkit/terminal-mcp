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

from typing import Any, Callable

from .permissions import valid_session_name
from .queue_store import UNASSIGNED_LANE, VERIFYING, InvalidTransitionError, TaskAlreadyClaimedError, QueueStore


class QueueService:
    def __init__(self, store: QueueStore | None = None, *,
                on_completed: Callable[[Any], None] | None = None, planner: Any = None) -> None:
        self.store = store or QueueStore()
        # Same optional 3-role-model hook as QueueEngine's own
        # on_completed -- the explicit-fallback completion path
        # (verify(), below) needs to fire it too, not just the
        # automatic marker-verified path.
        self.on_completed = on_completed
        # Task Migration/Load Balancing (task: "bổ sung Task Migration /
        # Load Balancing"): a TaskMigrationPlanner, wired in by mcp_app.py
        # once a real SessionOps (the controller) exists -- same
        # deferred-assignment pattern as IntegrationService.engine.
        self.planner = planner
        # AUTO-DISPATCH background loop (task: "hệ thống tự gửi task kế...
        # không cần ChatGPT đứng chờ"): the SAME QueueEngine instance
        # build_mcp constructs (over this SAME store/controller/
        # coordinator), wired in here so server_http.py's QueueLoop drives
        # the identical engine every terminal_queue_run_once tool call
        # already uses -- never a second, independently-constructed
        # engine that could see stale/duplicate state.
        self.engine: Any = None
        # Same deferred-assignment pattern, for the QueueLoop instance
        # itself (queue_loop.py) -- built once by mcp_app.py's build_mcp
        # around the SAME self.engine, so server_http.py's config.queue.
        # enabled gate starts/stops the real, wired-up loop rather than a
        # second, disconnected one.
        self.loop: Any = None

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
        only at dispatch time, never persisted over the original prompt.
        P0 (task: "persist-before-dispatch", item 1/8): by the time this
        method RETURNS, every task's own durable row already exists --
        the `tasks` list in the response is the TASK_ACCEPTED
        acknowledgment itself, each with its own queue_position."""
        if error := self._validate_session(session):
            return error
        if error := self._validate_tasks(tasks):
            return error
        ids = self.store.set_tasks(session, tasks, replace_pending=replace_pending)
        return {"session": session, "task_ids": ids, "replace_pending": replace_pending,
               "tasks": [self._accepted(session, task_id) for task_id in ids]}

    def append_tasks(self, session: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        if error := self._validate_tasks(tasks):
            return error
        ids = self.store.append_tasks(session, tasks)
        return {"session": session, "task_ids": ids,
               "tasks": [self._accepted(session, task_id) for task_id in ids]}

    def enqueue(self, session: str, prompt: str, *, title: str | None = None, priority: int = 0,
               metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """terminal_enqueue_task's own service method (P0 item 1/12: the
        new high-level, single-task, append-only convenience path --
        the RECOMMENDED default for a normal ChatGPT/UI/API-originated
        prompt, per the task's own explicit "MCP nên expose high-level
        enqueue tool làm mặc định"). Always appends (never replace_
        pending -- an "enqueue" call has no business cancelling
        anything already queued). Returns the exact TASK_ACCEPTED shape
        (item 8): status/task_id/queue_position, so the caller
        immediately knows this was ACCEPTED into the durable queue --
        distinct from, and not implying, that it has been DELIVERED to
        the session yet."""
        if error := self._validate_session(session):
            return error
        if not prompt:
            return {"error": "TASK_PROMPT_REQUIRED"}
        task = {"prompt": prompt, "title": title or "", "priority": priority, "metadata": metadata or {}}
        (task_id,) = self.store.append_tasks(session, [task])
        accepted = self._accepted(session, task_id)
        accepted["status"] = "TASK_ACCEPTED"
        return accepted

    def _accepted(self, session: str, task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "session": session, "queue_position": self.store.queue_position(task_id)}

    def create_task(self, title: str, prompt: str, *, session: str | None = None, priority: int = 0,
                    project: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Unified Task System checkpoint (2026-09-07, docs/REQUIREMENTS.
        md §20): the canonical `task_create` entry point (§20.7) --
        `session=None` creates a real, durable, UNASSIGNED (Global/
        Backlog) task, persisted immediately, exactly like any other
        queue task (§7's own persist-before-dispatch guarantee applies
        identically here -- an unassigned task is not a lesser, second-
        class kind of task, just one with no lane assignment yet).
        `session` given creates it directly assigned, same as `enqueue`
        -- this is the ONE canonical creation path either way, never two
        separate code paths for "assigned" vs "unassigned" beyond which
        lane the row lands in. `project` is stored in `metadata` (no
        schema change needed for this field alone -- reuses the existing
        free-form JSON column, same posture as `docs_exempt` elsewhere
        in this project)."""
        if not prompt:
            return {"error": "TASK_PROMPT_REQUIRED"}
        target = session
        if target is not None:
            if error := self._validate_session(target):
                return error
        else:
            target = UNASSIGNED_LANE
        full_metadata = dict(metadata or {})
        if project:
            full_metadata["project"] = project
        task = {"prompt": prompt, "title": title or "", "priority": priority, "metadata": full_metadata}
        (task_id,) = self.store.append_tasks(target, [task])
        accepted = self._accepted(target, task_id)
        accepted["status"] = "TASK_ACCEPTED"
        accepted["assigned"] = session is not None
        return accepted

    def assign_task(self, task_id: str, session: str) -> dict[str, Any]:
        """Unified Task System checkpoint: moves an existing task (from
        Global/Unassigned, or from another session's own queue) into
        `session`'s lane -- the SAME row/task_id/history, never a
        duplicate (task's own explicit "không duplicate record khi
        assign/move"). Refuses (TASK_NOT_MOVABLE) a task currently mid-
        review/mid-dispatch/running/verifying or already in a terminal
        state -- see `queue_store.py`'s own `MOVABLE_STATUSES` docstring
        for exactly why. `session` is validated the same way any other
        session name is everywhere else in this project."""
        if error := self._validate_session(session):
            return error
        result = self.store.move_task_to_session(task_id, session)
        if "error" in result:
            return result
        return {"task": result}

    def board(self) -> dict[str, Any]:
        """Unified Task System checkpoint: the Global Tasks Kanban's own
        real data source -- one bulk read (`list_all_lanes`, already
        real, already used by `list_all`/`global_inbox`), grouped into
        the 5 real lifecycle columns the Kanban UI shows (task's own
        explicit column set): Backlog (UNASSIGNED_LANE, not yet
        terminal), Queued, Running, Blocked/Review (BLOCKED/WAITING_
        SESSION/PAUSED/VERIFYING -- "in review" reads naturally for a
        task under verification too), Done (every terminal status,
        including FAILED/CANCELLED/SKIPPED -- the real per-card status
        is always shown, this column is a lifecycle grouping, never a
        claim that everything in it succeeded). Never a second grouping
        rule independent of `_group_tasks`'s own established buckets
        where they already apply -- this is a NEW grouping specifically
        for the 5-column Kanban shape, not a duplicate of the per-
        session Task Manager's own Running/Queued/Waiting-Dependency/
        Blocked-Rework/Recent buckets (a genuinely different view, same
        underlying rows)."""
        backlog: list[dict[str, Any]] = []
        queued: list[dict[str, Any]] = []
        running: list[dict[str, Any]] = []
        blocked_review: list[dict[str, Any]] = []
        done: list[dict[str, Any]] = []
        for lane in self.store.list_all_lanes():
            session = lane["session"]
            for task in lane["tasks"]:
                status = task["status"]
                row = dict(task)
                row["session"] = session if session != UNASSIGNED_LANE else None
                if status in ("COMPLETED", "FAILED", "CANCELLED", "SKIPPED"):
                    done.append(row)
                elif status == "RUNNING":
                    running.append(row)
                elif status in ("BLOCKED", "WAITING_SESSION", "PAUSED", "VERIFYING"):
                    blocked_review.append(row)
                elif session == UNASSIGNED_LANE:
                    backlog.append(row)
                else:
                    queued.append(row)  # QUEUED/PRECHECK/READY/DISPATCHING/DISPATCH_UNCERTAIN, assigned
        done.sort(key=lambda t: t.get("completed_at") or t.get("updated_at") or "", reverse=True)
        return {
            "backlog": backlog, "queued": queued, "running": running,
            "blocked_review": blocked_review, "done": done,
            "counts": {"backlog": len(backlog), "queued": len(queued), "running": len(running),
                      "blocked_review": len(blocked_review), "done": len(done)},
        }

    def task_status(self, task_id: str) -> dict[str, Any]:
        """Direct by-id lookup (item 11's own `task_status` tool) -- lets
        a caller track a specific task without already knowing (or
        re-deriving) which session/lane it lives in."""
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "TASK_NOT_FOUND", "task_id": task_id}
        return {"task": task.to_dict(), "queue_position": self.store.queue_position(task_id)}

    def metrics(self, session: str) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        return self.store.metrics(session)

    def status(self, session: str) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        lane = self.store.lane_status(session)
        lane["pending_count"] = self.count_pending(lane["tasks"])
        return lane

    def list_all(self) -> dict[str, Any]:
        return {"lanes": self.store.list_all_lanes()}

    def pending_counts(self) -> dict[str, int]:
        """Dashboard Task button badge's own bulk data source (the
        `/dashboard/api/sessions` route enriches every row with this in
        ONE call, never one queue read per session row) -- `{session:
        pending_count}` for every lane that has ever had a task. A
        session with no lane at all (never queued anything) simply has
        no key here; callers use `.get(name, 0)`, never a bare index."""
        return {lane["session"]: self.count_pending(lane["tasks"]) for lane in self.store.list_all_lanes()}

    _TERMINAL_RECENT_STATUSES = ("COMPLETED", "FAILED", "CANCELLED", "SKIPPED")
    _ATTENTION_STATUSES = ("BLOCKED", "FAILED", "DISPATCH_UNCERTAIN", "WAITING_SESSION", "PAUSED")
    _RUNNING_STATUSES = ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING")

    # Dashboard Task button badge (2026-09-07 checkpoint): the ONE
    # canonical definition of "pending" -- every status that is not
    # actively executing (RUNNING/VERIFYING -- the task is genuinely in
    # progress right now, not waiting on anything) and not terminal
    # (_TERMINAL_RECENT_STATUSES -- already resolved, one way or
    # another). Deliberately INCLUDES PRECHECK/READY/DISPATCHING (still
    # waiting for the dispatch pipeline to actually start executing the
    # task, from a user's-eye-view still "not started yet") and BLOCKED/
    # WAITING_SESSION/PAUSED/DISPATCH_UNCERTAIN (needs attention or is
    # waiting on something external -- still "pending", not done). A
    # frontend must NEVER re-derive this list itself (task's own
    # explicit "dùng một helper/backend field thống nhất để tránh
    # frontend/backend lệch nhau") -- `count_pending`/the `pending_count`
    # field this produces (session_task_board, status, and the dashboard
    # sessions list route all reuse it) is the only source of truth.
    PENDING_STATUSES = ("QUEUED", "PRECHECK", "READY", "DISPATCHING", "DISPATCH_UNCERTAIN",
                        "BLOCKED", "WAITING_SESSION", "PAUSED")

    @classmethod
    def count_pending(cls, tasks: list[dict[str, Any]]) -> int:
        return sum(1 for task in tasks if task.get("status") in cls.PENDING_STATUSES)

    @classmethod
    def _group_tasks(cls, tasks: list[dict[str, Any]], recent_limit: int) -> tuple[
            list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]],
            list[dict[str, Any]], list[dict[str, Any]]]:
        """The one grouping rule session_task_board (per-session) and
        global_inbox (fleet-wide) both share -- refactored out so the two
        views can never silently drift apart on what counts as "queued"
        vs "waiting on a dependency" vs "needs attention"."""
        by_id = {t["id"]: t for t in tasks}

        def _dependency_satisfied(task: dict[str, Any]) -> bool:
            return all(by_id.get(dep_id, {}).get("status") == "COMPLETED" for dep_id in task.get("depends_on") or ())

        running: list[dict[str, Any]] = []
        queued: list[dict[str, Any]] = []
        waiting_dependency: list[dict[str, Any]] = []
        blocked_rework: list[dict[str, Any]] = []
        recent: list[dict[str, Any]] = []
        for task in tasks:
            status = task["status"]
            if status in cls._RUNNING_STATUSES:
                running.append(task)
            elif status == "QUEUED":
                (queued if _dependency_satisfied(task) else waiting_dependency).append(task)
            elif status in cls._ATTENTION_STATUSES:
                blocked_rework.append(task)
            elif status in cls._TERMINAL_RECENT_STATUSES:
                recent.append(task)
        recent.sort(key=lambda t: t.get("completed_at") or t.get("updated_at") or "", reverse=True)
        recent = recent[:recent_limit]
        return running, queued, waiting_dependency, blocked_rework, recent

    def session_task_board(self, session: str, *, recent_limit: int = 10) -> dict[str, Any]:
        """Dashboard Task Manager UI's own single read (task: "khi chọn
        session/card/tab phải thấy Running/Queued/Waiting Dependency/
        Blocked-Rework/Recent Done-Failed") -- every field here comes
        straight from lane_status()'s own QueueTask.to_dict() rows
        (which themselves come straight off the persistent state
        machine -- status/priority/coordinator_decision/
        coordinator_reason/attempt_count/original_owner/
        migration_history/verification_evidence/last_error are all
        real, stored columns), never guessed or re-derived from a
        session's own terminal text. This method only GROUPS those same
        rows into the buckets the UI wants; it never creates a second,
        parallel task store (task's own explicit "không tạo một task
        store song song")."""
        if error := self._validate_session(session):
            return error
        lane = self.store.lane_status(session)
        tasks = lane["tasks"]
        running, queued, waiting_dependency, blocked_rework, recent = self._group_tasks(tasks, recent_limit)

        # Coordinator gate for the head-of-line task (task's own "Gate
        # được hiển thị ngay trên task tiếp theo") -- the first QUEUED/
        # PRECHECK task in position order that hasn't run yet; a
        # session with something already RUNNING has no "next" gate to
        # show (it's correctly busy, not waiting on a gate decision).
        next_gate = None
        if not running:
            head = next((t for t in tasks if t["status"] in ("QUEUED", "PRECHECK")), None)
            if head is not None:
                next_gate = {
                    "task_id": head["id"], "title": head["title"],
                    "decision": head.get("coordinator_decision") or None,
                    "reason": head.get("coordinator_reason"),
                    "checked_at": head.get("coordinator_checked_at"),
                }

        return {
            "session": session,
            "paused": lane["paused"],
            "paused_reason": lane["paused_reason"],
            "project": lane["project"],
            "summary": {
                "running": len(running), "queued": len(queued), "waiting_dependency": len(waiting_dependency),
                "blocked_rework": len(blocked_rework), "total": lane["total_count"],
            },
            "next_gate": next_gate,
            "running": running,
            "queued": queued,
            "waiting_dependency": waiting_dependency,
            "blocked_rework": blocked_rework,
            "recent": recent,
        }

    def fleet_task_summary(self) -> dict[str, Any]:
        """The dashboard's own small global overview line (task: "Running
        1 · Waiting 3 · Blocked 1") -- one aggregate across every lane
        that has ever had a task, deliberately not per-row (avoids a
        lane_status() query per visible session tab on every poll);
        entirely absent/zero lanes (never used queue features at all)
        collapses to all-zero counts, which the UI hides rather than
        showing a clutter line of zeros."""
        running = queued = blocked = 0
        for lane in self.store.list_all_lanes():
            for task in lane["tasks"]:
                status = task["status"]
                if status in self._RUNNING_STATUSES:
                    running += 1
                elif status == "QUEUED":
                    queued += 1
                elif status in self._ATTENTION_STATUSES:
                    blocked += 1
        return {"running": running, "queued": queued, "blocked": blocked}

    def global_inbox(self, *, recent_limit: int = 20) -> dict[str, Any]:
        """Dashboard Global Task Inbox (task: "Global Task Inbox") -- the
        SAME grouping session_task_board gives one session, applied
        fleet-wide: every task from every lane that has ever had one,
        each still tagged with its own `session` (unlike session_task_
        board, where that's implicit). Still the SAME persistent queue
        rows, still no second task store."""
        all_tasks: list[dict[str, Any]] = []
        lanes_by_session: dict[str, dict[str, Any]] = {}
        for lane in self.store.list_all_lanes():
            lanes_by_session[lane["session"]] = lane
            all_tasks.extend(lane["tasks"])
        running, queued, waiting_dependency, blocked_rework, recent = self._group_tasks(all_tasks, recent_limit)
        return {
            "summary": {"running": len(running), "queued": len(queued),
                       "waiting_dependency": len(waiting_dependency), "blocked_rework": len(blocked_rework),
                       "total": len(all_tasks), "sessions": len(lanes_by_session),
                       "paused_sessions": sum(1 for lane in lanes_by_session.values() if lane["paused"])},
            "running": running, "queued": queued, "waiting_dependency": waiting_dependency,
            "blocked_rework": blocked_rework, "recent": recent,
        }

    def recent_events(self, *, limit: int = 30) -> dict[str, Any]:
        """Fleet-wide recent queue events (task: Supervisor/Coordinator
        panel's own "recent event timeline") -- merges each lane's own
        list_events (already real, persistent, append-only rows) and
        sorts by timestamp, newest first. Bounded by however many lanes
        exist (never unbounded), same posture as queue_engine.py's own
        cross-lane conflict enrichment."""
        merged: list[dict[str, Any]] = []
        for lane in self.store.list_all_lanes():
            merged.extend(self.store.list_events(lane["session"], limit=limit))
        merged.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
        return {"events": merged[:limit]}

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
        if self.on_completed is not None:
            try:
                self.on_completed(updated)
            except Exception:  # noqa: BLE001 -- a handoff-publishing glitch must never un-complete a real task
                pass
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

    # -- Task Migration / Load Balancing -----------------------------------

    def set_project(self, session: str, project: str | None) -> dict[str, Any]:
        if error := self._validate_session(session):
            return error
        self.store.set_lane_project(session, project)
        return self.store.lane_status(session)

    def reassign(self, task_id: str, to_session: str, *, reason: str, actor: str) -> dict[str, Any]:
        """task_reassign(task_id, to_session, reason) -- item 10's own
        minimum tool. Fails clean (never partially applies) if the task
        is no longer eligible (already claimed, RUNNING, terminal, ...)."""
        if error := self._validate_session(to_session):
            return error
        task = self.store.get_task(task_id)
        if task is None:
            return {"error": "TASK_NOT_FOUND", "task_id": task_id}
        try:
            updated = self.store.reassign_task(task_id, to_session, reason=reason, actor=actor)
        except TaskAlreadyClaimedError as exc:
            return {"error": "TASK_ALREADY_CLAIMED", "task_id": task_id, "reason": str(exc)}
        except KeyError:
            return {"error": "TASK_NOT_FOUND", "task_id": task_id}
        return {"task": updated.to_dict()}

    def assignment_history(self, task_id: str) -> dict[str, Any]:
        try:
            return self.store.assignment_history(task_id)
        except KeyError:
            return {"error": "TASK_NOT_FOUND", "task_id": task_id}

    def rebalance_plan(self, project: str | None, sessions: list[str], *,
                       imbalance_threshold: int | None = None) -> dict[str, Any]:
        """task_rebalance_plan(project) -- item 10's own dry-run-first
        requirement: ALWAYS just a preview, never applies anything."""
        if self.planner is None:
            return {"error": "PLANNER_NOT_CONFIGURED"}
        kwargs = {} if imbalance_threshold is None else {"imbalance_threshold": imbalance_threshold}
        plan = self.planner.plan_rebalance(project, sessions, **kwargs)
        return {"project": project, "plan": plan, "move_count": len(plan)}

    def rebalance(self, project: str | None, sessions: list[str], *, dry_run: bool = True, actor: str = "auto-balancer",
                  imbalance_threshold: int | None = None) -> dict[str, Any]:
        """task_rebalance(project, dry_run=true/false) -- dry_run=True
        (the default, and the ONLY mode a caller gets without explicitly
        opting out) returns the exact same plan rebalance_plan would,
        applying nothing. dry_run=False actually calls apply_plan."""
        if self.planner is None:
            return {"error": "PLANNER_NOT_CONFIGURED"}
        kwargs = {} if imbalance_threshold is None else {"imbalance_threshold": imbalance_threshold}
        plan = self.planner.plan_rebalance(project, sessions, **kwargs)
        if dry_run:
            return {"project": project, "plan": plan, "move_count": len(plan), "applied": False}
        results = self.planner.apply_plan(plan, actor=actor)
        return {"project": project, "plan": plan, "results": results, "applied": True}
