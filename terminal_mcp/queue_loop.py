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
import time
from datetime import datetime, timezone
from typing import Callable

from .queue_engine import QueueEngine
from .queue_event_drain import QueueEventDrain

_LOGGER = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 3.0
DEFAULT_RESCUE_INTERVAL_SECONDS = 10.0


class QueueLoop:
    def __init__(self, engine: QueueEngine, *, poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
                heartbeat_refresher: Callable[[], None] | None = None,
                event_drain: "QueueEventDrain | None" = None,
                project_feeder: object | None = None,
                task_router: object | None = None,
                rescue_interval_seconds: float = DEFAULT_RESCUE_INTERVAL_SECONDS) -> None:
        self.engine = engine
        # QUEUE RESCUE (TMCP-TASK-ROUTER-001). Injected and optional, and run
        # as a STEP of this cycle rather than as a second thread -- same
        # reasoning as the event drain above: one loop drives the queue.
        #
        # It has its OWN interval because the two jobs have different costs. A
        # tick reads durable state for one lane; a rescue sweep lists the whole
        # fleet. Running the sweep at the tick cadence would mean a fleet
        # listing every three seconds forever, which is how a safety feature
        # becomes the thing an operator turns off.
        self.task_router = task_router
        self.rescue_interval_seconds = max(1.0, rescue_interval_seconds)
        self._last_rescue_at: float | None = None
        self._last_rescue: dict | None = None
        # Set when a lane reports IDLE. A session that just became free is the
        # single best moment to re-ask "is anything waiting for a runtime?",
        # so that one cycle skips the interval instead of leaving a ready task
        # queued for the rest of the window.
        self._rescue_now = False
        # The event-bus drain, run as a STEP of this cycle. Injected and
        # optional: None means exactly today's behaviour, and there is still
        # only ever one background thread driving the queue (see
        # queue_event_drain.py's own docstring on why a second loop was the
        # wrong shape).
        self.event_drain = event_drain
        # Optional project-level feeder. QueueEngine already advances rows that
        # exist in the durable queue; this bridge only supplies the next
        # canonical project task when an opted-in lane is genuinely IDLE.
        self.project_feeder = project_feeder
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
        self._last_drain: dict | None = None
        self._last_feed: dict | None = None
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
            feeder_status = None
            if self.project_feeder is not None and hasattr(self.project_feeder, "status"):
                try:
                    feeder_status = self.project_feeder.status()
                except Exception as exc:  # status is observability only
                    feeder_status = {"error": type(exc).__name__}
            return {"running": self.is_alive(), "poll_interval_seconds": self.poll_interval_seconds,
                    "last_cycle_at": self._last_cycle_at, "last_error": self._last_error,
                    "drain_enabled": self.event_drain is not None,
                    "last_drain": self._last_drain,
                    "project_feed_enabled": self.project_feeder is not None,
                    "project_fleet": feeder_status,
                    "last_feed": self._last_feed,
                    "rescue_enabled": self.task_router is not None,
                    "rescue_interval_seconds": self.rescue_interval_seconds,
                    "last_rescue": self._last_rescue}

    def run_one_cycle(self) -> list[dict]:
        """One full pass over every auto-dispatch-enabled lane -- the
        SAME step the background thread repeats forever; exposed as its
        own method so a test (or a manual `terminal_queue_loop_run_once`
        tool call) can drive exactly one cycle deterministically without
        starting a real thread."""
        results = []
        # Drain BEFORE the lane sweep. An event that makes a task dispatchable
        # should be acted on in this cycle rather than waiting for the next one,
        # and the sweep below is what picks up anything the drain's single tick
        # per lane did not finish.
        # NON-DISPATCH RECOVERY SWEEP, fleet-wide and BEFORE the lane walk
        # below.
        #
        # The walk only visits lanes with auto_dispatch_enabled -- 7 of 56 on
        # this deployment -- and until now every reconciler lived inside
        # engine.tick(), which the walk is the only caller of. So the clears
        # that are documented as automatic (a stale PRECHECK/DISPATCHING
        # claim, a WAITING_SESSION whose session came back, a coordinator
        # pause with nothing left to guard) never ran for an opted-out lane.
        # A lane could sit paused for hours with tasks stranded behind it, as
        # one really did (see reconcile_stale_lane_pause's docstring).
        #
        # These three are pure store operations: they move tasks back to
        # QUEUED and clear a stale lane flag, and send NOTHING to any session.
        # Opting a lane out of auto-dispatch still means no work is ever
        # submitted to it -- it no longer means the lane stops being
        # maintained. Each is idempotent, so a quiet cycle writes nothing.
        # Resolved with getattr, not attribute access: a store that does not
        # implement one of these (a narrower test double, an older store) must
        # skip that sweep, never break the dispatch cycle -- the same fail-soft
        # posture as the except below.
        for name in ("reconcile_stale_lane_pause", "reconcile_stale_claims",
                     "reconcile_uncertain_and_waiting"):
            sweep = getattr(self.engine.store, name, None)
            if sweep is None:
                continue
            try:
                changed = sweep()
            except Exception:  # noqa: BLE001 -- a sweep glitch must never stop dispatch
                _LOGGER.exception("queue-loop: %s failed, continuing", name)
            else:
                if changed:
                    _LOGGER.info("queue-loop: %s reconciled %s", name, changed)
        self._drain_events()
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
                row = result.to_dict()
                results.append(row)
                if result.action == "IDLE":
                    # A free lane is the trigger the rescue sweep wants; the
                    # sweep itself runs once, after the loop, never per lane.
                    self._rescue_now = True
                if result.action == "IDLE" and self.project_feeder is not None:
                    feed = self._feed_project_task(session)
                    if feed and feed.get("action") in ("ENQUEUED", "EXISTING_TASK"):
                        # Persist-first: feeder wrote or found the queue row.
                        # Tick once more so the same cycle claims it immediately.
                        next_result = self.engine.tick(session)
                        results.append({**next_result.to_dict(), "project_feed": feed})
            except Exception as exc:  # noqa: BLE001 -- one lane's failure must never stop the others
                _LOGGER.exception("queue-loop: tick failed for session %r, continuing with other lanes", session)
                results.append({"session": session, "action": "ENGINE_ERROR", "task_id": None,
                                "detail": f"{type(exc).__name__}: {exc}"})
        self._rescue_queued_tasks()
        with self._lock:
            self._last_cycle_at = datetime.now(timezone.utc).isoformat()
        return results

    def _rescue_queued_tasks(self) -> dict | None:
        """Re-match every task that has no runtime bound to it.

        THE STUCK-QUEUED FIX. The lane sweep above can only advance tasks in
        lanes that opted into auto-dispatch; a task whose lane never opted in,
        or which has no real lane at all (the unassigned backlog), was
        previously invisible to every autonomous path in the system. This step
        is what makes "queued while a compatible session is idle" a state that
        repairs itself.

        Never raises: a rescue failure must not stop the lane dispatch that is
        this loop's primary job."""
        if self.task_router is None:
            return None
        now = time.monotonic()
        due = (self._rescue_now or self._last_rescue_at is None
               or now - self._last_rescue_at >= self.rescue_interval_seconds)
        if not due:
            return None
        self._rescue_now = False
        self._last_rescue_at = now
        try:
            result = self.task_router.rescue_once()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("queue-loop: task rescue failed, continuing with lane dispatch")
            result = {"error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._last_rescue = result
        return result

    def _feed_project_task(self, session: str) -> dict | None:
        """Ask the configured canonical-project feeder for one task.

        Never raises into the scheduler. A bad/unreadable registry must leave
        the lane idle rather than inventing or dispatching work.
        """
        if self.project_feeder is None:
            return None
        try:
            result = self.project_feeder.feed_if_idle(session)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("queue-loop: project feeder failed for %r", session)
            result = {"session": session, "action": "FEED_ERROR",
                      "detail": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            self._last_feed = result
        return result

    def _drain_events(self) -> dict | None:
        """One bounded drain pass. Never raises: an unreadable bus must not stop
        the lane dispatch that is this loop's primary job."""
        if self.event_drain is None:
            return None
        try:
            result = self.event_drain.drain_once()
        except Exception:  # noqa: BLE001 -- drain_once already catches; belt and braces
            _LOGGER.exception("queue-loop: event drain failed, continuing with lane dispatch")
            return None
        with self._lock:
            self._last_drain = result
        return result

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
