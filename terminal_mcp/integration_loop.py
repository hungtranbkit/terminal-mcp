"""3-role pipeline -- the event-driven WAIT/wake background loop for the
Integration Agent (task follow-up: "chế độ WAIT/EVENT-DRIVEN cho session
Integration/Test"). See integration_engine.py's own IntegrationEngine
docstring for why tick() itself needs no changes -- claim_next_handoff's
own query is what "detects" a new handoff (a durable DB row, restart-
safe by construction); this loop is the previously-missing piece: an
actual driver that calls tick() automatically instead of requiring an
explicit terminal_integration_run_once every single time.

WAKE MECHANISM (two layers, deliberately not blind fixed-interval
polling alone):
  1. An in-process threading.Event, SET by IntegrationStore.publish_
     handoff itself (via its own injected on_handoff_published callback
     -- same "injected callback, not a new pub/sub system" convention
     as QueueEngine's own on_completed). This loop's wait() call returns
     almost immediately once a handoff is published in THIS process --
     real event-driven wake, not a poll tick.
  2. A bounded FALLBACK poll interval (config.integration_loop.
     fallback_poll_seconds, the event.wait(timeout=...) call's own
     timeout) -- so a handoff published by a DIFFERENT process (e.g. a
     coding session on a different node writing directly to the shared
     integration.db), or one that already existed before this loop
     started, is still picked up, bounded, without needing any
     cross-process signaling mechanism at all. This is the SAME "durable
     state you'd find anyway on the next tick, the event just shortens
     the wait" posture QueueLoop's own poll interval already has --
     never a correctness dependency, purely a latency optimization.
  3. Once woken (by either 1 or 2), a cycle that made real progress on
     any project is immediately followed by ANOTHER cycle with no wait
     at all -- publish_handoff's own wake fires only ONCE per handoff,
     not once per pipeline step, so a multi-step pipeline (CLAIMED ->
     MERGED -> INTEGRATED, each its own tick()) still advances back-to-
     back rather than paying the fallback interval between every single
     step. Only a cycle where every project reported WAITING_FOR_
     HANDOFF (nothing to do) or ENGINE_ERROR (broken -- never spin
     tight on a persistently failing project) goes back to waiting.

RESTART SAFETY: the Event is purely in-memory/best-effort -- a process
restart loses whatever was pending in it, but loses NOTHING real: every
handoff's own READY_FOR_INTEGRATION row is still sitting in the database
exactly where claim_next_handoff will find it on this loop's very first
cycle after restart (bounded by fallback_poll_seconds at worst, same
restart story as QueueLoop's own).

IDEMPOTENCY: tick() itself already claims via the existing, real, atomic
BEGIN IMMEDIATE claim_next_handoff (integration_store.py) -- calling
tick() an extra, redundant time (e.g. once from the event wake and once
from the very next fallback poll landing close together, or two loop
instances racing against the same shared database) is always safe: a
second call simply finds nothing new to claim and returns action=
WAITING_FOR_HANDOFF, never a duplicate claim, never double-processing
the same handoff.

SAFETY (same two-gate posture as QueueLoop -- belt and suspenders):
  1. A GLOBAL enable flag (config.integration_loop.enabled, default
     False) this loop's own caller (mcp_app.py/server_http.py) checks
     before ever constructing/starting it -- nothing runs unless an
     operator explicitly opts in.
  2. Per-project: a project must already be explicitly configured via
     terminal_integration_configure (real setup, not a bare flag)
     before any Handoff can even exist for it, and the already-real
     per-project `paused` flag (integration_service.py's pause/resume)
     is the reuse-not-rebuild equivalent of queue_lanes.auto_dispatch_
     enabled's per-lane gate -- this loop skips a paused project's tick
     entirely rather than relying on tick() to no-op it, so a paused
     project consumes zero cycles here, not just zero real actions.

One project's exception during a cycle is caught/logged and never stops
the loop from continuing to service every OTHER project in the same
cycle, matching QueueLoop's own isolation guarantee."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .integration_engine import IntegrationEngine
from .integration_store import IntegrationStore

_LOGGER = logging.getLogger(__name__)

DEFAULT_FALLBACK_POLL_SECONDS = 5.0


class IntegrationLoop:
    def __init__(self, engine: IntegrationEngine, store: IntegrationStore, *,
                fallback_poll_seconds: float = DEFAULT_FALLBACK_POLL_SECONDS) -> None:
        self.engine = engine
        self.store = store
        self.fallback_poll_seconds = max(0.5, fallback_poll_seconds)
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cycle_at: str | None = None
        self._last_error: dict[str, str] | None = None
        self._lock = threading.Lock()

    def wake(self, project: str | None = None) -> None:
        """The real event-driven wake signal -- wired as IntegrationStore's
        own on_handoff_published hook (see mcp_app.py), so a fresh
        publish_handoff call returns this loop's current wait() almost
        immediately. `project` is accepted (for a future per-project-
        targeted wake) but not otherwise used yet -- every cycle already
        sweeps every configured, non-paused project (a no-op tick() for
        an idle one is cheap), the same "sweep everything that's opted
        in" shape QueueLoop's own cycle already uses; a disclosed, not a
        real, gap. Also safe to call with no loop running at all (a bare
        Event.set() with nothing waiting on it yet is a no-op, picked up
        the moment start() is later called)."""
        self._wake_event.set()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_alive():
                return  # already running -- never spawn a second loop for this instance
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="terminal-mcp-integration-loop", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._wake_event.set()  # unblock a current wait() immediately, don't wait out the fallback poll
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.is_alive(), "fallback_poll_seconds": self.fallback_poll_seconds,
                    "last_cycle_at": self._last_cycle_at, "last_error": self._last_error}

    def run_one_cycle(self) -> list[dict[str, Any]]:
        """One full pass over every configured, non-paused project's own
        IntegrationEngine.tick() -- the SAME step the background thread
        repeats forever; exposed as its own method so a test (or a
        manual terminal_integration_loop_run_once tool call) can drive
        exactly one cycle deterministically without starting a real
        thread, same precedent as QueueLoop.run_one_cycle."""
        results: list[dict[str, Any]] = []
        for pipeline in self.store.list_pipelines():
            project = pipeline["project"]
            if pipeline.get("paused"):
                continue
            try:
                result = self.engine.tick(project)
                results.append(result.to_dict())
            except Exception as exc:  # noqa: BLE001 -- one project's failure must never stop the others
                _LOGGER.exception("integration-loop: tick failed for project %r, continuing with other projects",
                                 project)
                results.append({"project": project, "action": "ENGINE_ERROR",
                                "detail": f"{type(exc).__name__}: {exc}"})
        with self._lock:
            self._last_cycle_at = datetime.now(timezone.utc).isoformat()
        return results

    # Action values a tick() can report that mean "nothing actually
    # advanced" -- WAITING_FOR_HANDOFF is the real, documented no-op
    # (integration_engine.py's own docstring); ENGINE_ERROR is this
    # loop's OWN synthetic marker for a tick() that raised. Neither
    # should make the loop spin immediately again without its normal
    # wait -- WAITING_FOR_HANDOFF because there is genuinely nothing new
    # to do, ENGINE_ERROR because a persistently broken project must
    # never turn into a tight busy-loop with no backoff at all.
    _NO_PROGRESS_ACTIONS = frozenset({"WAITING_FOR_HANDOFF", "ENGINE_ERROR"})

    def _run(self) -> None:
        made_progress = False
        while not self._stop_event.is_set():
            if not made_progress:
                # A real handoff was just published (wake()) OR the
                # fallback interval elapsed -- either way, go check.
                # Skipped entirely right after a cycle that itself made
                # real progress, so a multi-step pipeline (CLAIMED ->
                # MERGED -> INTEGRATED, each its own tick()) advances
                # back-to-back instead of paying the fallback interval
                # between every single step -- see this loop's own
                # module docstring for why publish_handoff's wake fires
                # only ONCE per handoff, not once per step.
                self._wake_event.wait(timeout=self.fallback_poll_seconds)
                self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                results = self.run_one_cycle()
                made_progress = any(r.get("action") not in self._NO_PROGRESS_ACTIONS for r in results)
                with self._lock:
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001 -- cycle-level catch-all; run_one_cycle already isolates
                # per-project failures, so reaching here means something
                # broader (e.g. the store itself unreadable) -- never let
                # it kill the background thread.
                _LOGGER.exception("integration-loop: cycle failed, will retry next wake/poll")
                made_progress = False
                with self._lock:
                    self._last_error = {"at": datetime.now(timezone.utc).isoformat(),
                                        "error": f"{type(exc).__name__}: {exc}"}
