"""Auto Recovery -- the OPTIONAL, off-by-default background loop that
automatically calls RecoveryEngine.reconcile_node() when a node
reconnects (task item 9: "node-agent/controller khởi động lại phải tự
reconcile theo policy; configurable AUTO_RECOVERY_ENABLED, default an
toàn").

WAKE MECHANISM (same two-layer pattern as integration_loop.py's own
event-driven design -- see that module for the full reasoning this
mirrors):
  1. The REAL trigger: node_registry.py's own sync_status_transitions
    (already real, already a side effect of controller.list_nodes(),
    itself already driven by the dashboard's own periodic node poll) --
    an OFFLINE/DEGRADED -> ONLINE transition means "this node just
    reconnected", exactly what this feature exists to react to. This
    loop polls controller.list_nodes() itself (bounded interval,
    config.auto_recovery.reconcile_poll_seconds) and reconciles any
    node whose transition it observes -- no new node-status detection
    of its own, reuses the existing, real mechanism entirely.
  2. Every node currently ONLINE also gets reconciled on the very FIRST
    cycle after this loop starts (covers a node that was ALREADY online
    when the loop started, e.g. this control-plane process itself
    restarting -- there is no "transition" to observe for a node that
    never went away from this process's own point of view, but its
    MISSING/OFFLINE session rows are still real and still worth a
    reconcile pass).

BOUNDED, NEVER TIGHT: one reconcile_node() call per newly-online node
per cycle -- RecoveryEngine's own lock/max_attempts bound (see recovery_
engine.py) makes repeated cycles always safe even if a node flaps
online/offline/online, never a duplicate spawn, never an unbounded
retry storm.

SAFETY (same two-gate posture as queue_loop.py/integration_loop.py):
  1. config.auto_recovery.enabled (default False) -- a GLOBAL kill
     switch nothing starts without.
  2. Per-session: session_registry.py's own auto_recovery_enabled
     tri-state override (checked inside RecoveryEngine._recovery_allowed,
     not duplicated here) -- an operator can opt a SPECIFIC session out
     even while this loop is globally on."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from .recovery_engine import RecoveryEngine

_LOGGER = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 10.0


class RecoveryLoop:
    def __init__(self, engine: RecoveryEngine, controller: Any, *,
                poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
        self.engine = engine
        self.controller = controller
        self.poll_interval_seconds = max(1.0, poll_interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cycle_at: str | None = None
        self._last_error: dict[str, str] | None = None
        self._seen_nodes_once: set[str] = set()
        self._lock = threading.Lock()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, name="terminal-mcp-recovery-loop", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self.is_alive(), "poll_interval_seconds": self.poll_interval_seconds,
                    "last_cycle_at": self._last_cycle_at, "last_error": self._last_error}

    def run_one_cycle(self) -> list[dict[str, Any]]:
        """One pass: reconciles every node that either (a) just
        transitioned TO ONLINE this cycle (real transitions, straight
        from node_registry.py's own sync_status_transitions -- not
        hand-rolled here), or (b) is ONLINE and has never been
        reconciled by this loop INSTANCE before (covers this process's
        own startup/first cycle, item 9's "startup recovery" -- a node
        already online when this loop starts has no "transition" for
        sync_status_transitions to report, but its MISSING/OFFLINE
        session rows are still real and still worth a pass). Exposed as
        its own method -- same "a test/manual tool call can drive
        exactly one cycle deterministically" precedent as queue_loop.py
        /integration_loop.py's own run_one_cycle."""
        from .node_models import NODE_ONLINE
        results: list[dict[str, Any]] = []
        try:
            transitions = self.controller.registry.sync_status_transitions()
            nodes = self.controller.list_nodes()
        except Exception:  # noqa: BLE001 -- a node-listing glitch must never crash this loop
            _LOGGER.exception("recovery-loop: list_nodes failed, will retry next cycle")
            with self._lock:
                self._last_cycle_at = datetime.now(timezone.utc).isoformat()
            return results
        reconnected_now = {t["node_id"] for t in transitions if t["to_status"] == NODE_ONLINE}
        to_reconcile = set()
        for node in nodes:
            if node.status != NODE_ONLINE:
                continue
            if node.id in reconnected_now or node.id not in self._seen_nodes_once:
                to_reconcile.add(node.id)
            self._seen_nodes_once.add(node.id)
        for node_id in sorted(to_reconcile):
            try:
                node_results = self.engine.reconcile_node(node_id, requested_by="recovery-loop")
                results.extend(node_results)
            except Exception as exc:  # noqa: BLE001 -- one node's failure must never stop the others
                _LOGGER.exception("recovery-loop: reconcile failed for node %r, continuing", node_id)
                results.append({"node_id": node_id, "error": "ENGINE_ERROR", "detail": f"{type(exc).__name__}: {exc}"})
        with self._lock:
            self._last_cycle_at = datetime.now(timezone.utc).isoformat()
        return results

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_one_cycle()
                with self._lock:
                    self._last_error = None
            except Exception as exc:  # noqa: BLE001 -- cycle-level catch-all
                _LOGGER.exception("recovery-loop: cycle failed, will retry next interval")
                with self._lock:
                    self._last_error = {"at": datetime.now(timezone.utc).isoformat(),
                                        "error": f"{type(exc).__name__}: {exc}"}
            self._stop_event.wait(timeout=self.poll_interval_seconds)
