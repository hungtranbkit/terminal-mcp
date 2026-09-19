"""Server-side follow-through for ONE task a client explicitly started
(task: TMCP-CALLED-TOOL-SPAM-002 -- "normal ChatGPT orchestration must
need one terminal_turn call per user request").

WHY THIS EXISTS. `QueueEngine.tick()` deliberately makes at most one
state transition per call, so starting a task and then carrying it to
completion takes several ticks (claim -> coordinator gate -> dispatch ->
running -> verifying -> completed). Before this module the only things
that ever called tick() were (a) the operator/ChatGPT, one explicit MCP
call per transition, or (b) `QueueLoop`, which is correct but sits behind
two deliberately OFF-by-default gates (`config.queue.enabled` globally
and `queue_lanes.auto_dispatch_enabled` per lane -- see queue_loop.py).
With those off, the only way a task moved was the client calling again
and again, which is exactly the "Called tool" wall this task is about.

WHAT IT IS NOT. This is not a second auto-dispatcher and it does not
weaken either of QueueLoop's gates. It follows exactly the ONE task it
was handed, and it stops the moment that task leaves the active set --
so it never claims the next QUEUED task off a lane, which is precisely
the autonomous behaviour those gates exist to withhold. A lane that has
not opted into QueueLoop still will not have its backlog dispatched; it
will only carry the single task whose start a caller explicitly asked
for, in the same call that persisted it.

BOUNDED. Every follower has a deadline (`ttl_seconds`) and a poll
interval, one daemon thread per started task, and a hard cap on how many
may run at once (`max_concurrent`). Over the cap, `follow()` refuses and
says so rather than growing threads without limit -- the caller's task is
already durably persisted either way, so a refusal costs progress speed,
never the task itself.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_TTL_SECONDS = 900.0
DEFAULT_MAX_CONCURRENT = 8

#: A task in one of these is finished as far as this follower is concerned:
#: either genuinely done, or stopped on something only a human can move.
#: Kept as a literal tuple rather than imported from queue_store so this
#: module stays usable against any store exposing the same vocabulary (the
#: tests drive it with a fake).
FOLLOW_STOP_STATUSES = (
    "COMPLETED", "SKIPPED", "CANCELLED", "FAILED", "BLOCKED", "NEEDS_HUMAN",
    # PAUSED belongs here for the same reason: the coordinator gate parks a
    # task on a paused lane (observed live on hp-linux -- "session X is
    # already actively working in the same repo/worktree"), and tick() answers
    # PAUSED to every call after that. Following it would burn a thread for
    # the full TTL to learn nothing a human has not already been asked for.
    "PAUSED",
)


class StartedTaskFollower:
    """Carries explicitly-started tasks forward, one thread per task."""

    def __init__(self, tick: Callable[[str], Any], task_status: Callable[[str], dict[str, Any]], *,
                 poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
                 ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.tick = tick
        self.task_status = task_status
        self.poll_interval_seconds = poll_interval_seconds
        self.ttl_seconds = ttl_seconds
        self.max_concurrent = max_concurrent
        self.monotonic = monotonic
        self.sleep = sleep
        self._lock = threading.Lock()
        self._active: dict[str, threading.Thread] = {}

    # -- state ---------------------------------------------------------

    def active_task_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active)

    def _release(self, task_id: str) -> None:
        with self._lock:
            self._active.pop(task_id, None)

    # -- the follow loop ------------------------------------------------

    def status_of(self, task_id: str) -> str | None:
        """This task's durable status, or None when it cannot be read.

        Never raises: a follower that cannot read the store must stop
        quietly, not take the thread (or the server) down with it."""
        try:
            result = self.task_status(task_id)
        except Exception:  # noqa: BLE001 -- best effort; the record is durable regardless
            _LOGGER.warning("follower could not read task %s", task_id, exc_info=True)
            return None
        if not isinstance(result, dict):
            return None
        task = result.get("task")
        if isinstance(task, dict):
            return task.get("status")
        return result.get("status")

    def run_until_settled(self, session: str, task_id: str) -> str:
        """Tick `session` until `task_id` settles, the TTL expires, or the
        task becomes unreadable. Returns the reason it stopped -- used
        directly by the tests, and by the thread body below."""
        deadline = self.monotonic() + self.ttl_seconds
        while True:
            status = self.status_of(task_id)
            if status is None:
                return "UNREADABLE"
            if status in FOLLOW_STOP_STATUSES:
                return "SETTLED"
            if self.monotonic() >= deadline:
                return "TTL_EXPIRED"
            try:
                self.tick(session)
            except Exception:  # noqa: BLE001 -- one bad tick must not end the follow
                _LOGGER.warning("follower tick failed for session %s", session, exc_info=True)
            self.sleep(self.poll_interval_seconds)

    def follow(self, session: str, task_id: str) -> dict[str, Any]:
        """Start following `task_id`. Idempotent per task id: a second
        call while one is already in flight reports the existing follower
        rather than starting a second thread against the same lane."""
        if not session or not task_id:
            return {"following": False, "reason": "TASK_REQUIRED"}
        with self._lock:
            if task_id in self._active:
                return {"following": True, "reason": "ALREADY_FOLLOWING", "task_id": task_id}
            if len(self._active) >= self.max_concurrent:
                return {"following": False, "reason": "FOLLOWER_CAPACITY",
                        "task_id": task_id, "max_concurrent": self.max_concurrent}
            thread = threading.Thread(target=self._body, args=(session, task_id),
                                      name=f"task-follower-{task_id[:8]}", daemon=True)
            self._active[task_id] = thread
        thread.start()
        return {"following": True, "reason": "FOLLOWING", "task_id": task_id,
                "ttl_seconds": self.ttl_seconds}

    def _body(self, session: str, task_id: str) -> None:
        try:
            self.run_until_settled(session, task_id)
        finally:
            self._release(task_id)
