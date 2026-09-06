"""Supervisor Queue v2 -- the AUTO-DISPATCH background loop (task:
"Khi task VERIFIED_DONE và coordinator READY cho task kế, hệ thống tự
gửi task kế vào đúng session. Không cần ChatGPT đứng chờ để push từng
task.").

Before this module, `QueueEngine.tick(session)` was real and correct but
nothing ever called it automatically -- an operator/ChatGPT had to
explicitly call `terminal_queue_run_once` for every single state
transition, which is not "auto-dispatch" in any real sense (see
queue_engine.py's own module docstring, which explicitly disclosed this
as a known gap in the phase that shipped it). QueueLoop is the real fix:
a plain daemon thread (same proven shape as SupervisorLoop/
MaintenanceLoop -- server_http.py has no asyncio/lifespan hook to attach
a coroutine-based task to instead) that repeatedly calls tick() for
every lane that has opted in, on a short interval, with NO operator
action required per task/transition.

SAFETY (two independent, stacked gates -- belt and suspenders, exactly
the project's own established convention for anything that can act on a
real session autonomously):
  1. `config.queue.enabled` -- a GLOBAL kill switch (default False,
     same posture as config.supervisor.enabled) for the ENTIRE
     background loop. Nothing in this module ever starts unless an
     operator explicitly turns this on in config.yaml.
  2. `queue_lanes.auto_dispatch_enabled` -- a PER-LANE, OFF-by-default
     opt-in column (queue_store.py's migration v3, already existed
     before this module) that this loop itself checks every single
     cycle before ever calling tick() for a given session. A lane with
     the column left off is completely untouched by this loop, even
     while the loop itself is globally running -- this is the exact
     mechanism that keeps `window`/`window2` (or any other real
     production session) safe from ever being auto-dispatched until
     their own operator explicitly opts that ONE lane in, regardless of
     whether the global loop is on for other, already-proven lanes.

One lane's exception during a cycle is caught and logged -- it never
stops the loop from continuing to service every OTHER lane in the same
cycle, and never crashes the background thread itself (same isolation
guarantee MaintenanceLoop/SupervisorLoop already give their own
sub-steps).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from .queue_engine import QueueEngine

_LOGGER = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 3.0


class QueueLoop:
    def __init__(self, engine: QueueEngine, *, poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
                heartbeat_refresher: Callable[[], None] | None = None) -> None:
        self.engine = engine
        self.poll_interval_seconds = max(0.5, poll_interval_seconds)
        # Injected, not imported -- this module has no business knowing
        # HOW to compute a fresh local heartbeat (that's mcp_app.py's/
        # dashboard.py's own closure, already duplicated between those
        # two per this project's own established precedent); it just
        # needs to be CALLED once per cycle so the local node never goes
        # OFFLINE (heartbeat-staleness-derived) purely because nothing
        # else happened to poll it during a quiet period -- see
        # controller.py's own refresh_local_heartbeat docstring for why
        # that would otherwise make every routed tick() call fail
        # SESSION_NOT_FOUND for a real, existing local session.
        self.heartbeat_refresher = heartbeat_refresher
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cycle_at: str | None = None
        self._last_error: dict[str, str] | None = None
        self._lock = threading.Lock()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return  # already started; never spawn a second loop for this instance
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-queue-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def status(self) -> dict:
        with self._lock:
            return {"running": self.is_alive(), "poll_interval_seconds": self.poll_interval_seconds,
                    "last_cycle_at": self._last_cycle_at, "last_error": self._last_error}

    def run_one_cycle(self) -> list[dict]:
        """One full pass over every auto-dispatch-enabled lane -- the
        SAME step the background thread repeats forever; exposed as its
        own method so a test (or a manual `terminal_queue_loop_run_once`
        tool call) can drive exactly one cycle deterministically without
        starting a real thread."""
        results = []
        if self.heartbeat_refresher is not None:
            try:
                self.heartbeat_refresher()
            except Exception:  # noqa: BLE001 -- a heartbeat refresh glitch must never stop dispatch entirely
                _LOGGER.exception("queue-loop: heartbeat refresh failed, continuing anyway")
        for lane in self.engine.store.list_all_lanes():
            if not lane.get("auto_dispatch_enabled"):
                continue
            session = lane["session"]
            try:
                result = self.engine.tick(session)
                results.append(result.to_dict())
            except Exception as exc:  # noqa: BLE001 -- one lane's failure must never stop the others
                _LOGGER.exception("queue-loop: tick failed for session %r, continuing with other lanes", session)
                results.append({"session": session, "action": "ENGINE_ERROR", "task_id": None,
                                "detail": f"{type(exc).__name__}: {exc}"})
        with self._lock:
            self._last_cycle_at = datetime.now(timezone.utc).isoformat()
        return results

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_one_cycle()
                with self._lock:
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001 -- the cycle-level catch-all; run_one_cycle already isolates
                # per-lane failures, so reaching here means something
                # broader (e.g. the store itself unreadable) -- never let
                # it kill the background thread.
                _LOGGER.exception("queue-loop: cycle failed, will retry next interval")
                with self._lock:
                    self._last_error = {"at": datetime.now(timezone.utc).isoformat(),
                                       "error": f"{type(exc).__name__}: {exc}"}
            self._stop_event.wait(self.poll_interval_seconds)
