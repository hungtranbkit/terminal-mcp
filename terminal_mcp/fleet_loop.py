"""Keeping the fleet registry fresh, so "stale" stops being its normal state.

WHY THIS EXISTS
---------------
The registry shipped with no scheduler. Nothing re-projected local truth
unless a human hit refresh, so within fifteen minutes of every controller
start every node's metadata was stale -- and the auth screen, which reads
that cache, could only answer UNKNOWN_STALE. Observed 2026-09-13: the whole
fleet 6.8 hours old, `metadata_stale=True` on all five nodes, an hour after
the feature went live.

A cache with no refresh is not a cache. It is a snapshot that decays into a
lie, and the honest UNKNOWN_STALE label was the symptom rather than the fix.

SHAPE
-----
Same daemon-thread-with-a-stop-Event as MaintenanceLoop, for the same reason
it uses one: server_http.py has no asyncio lifespan hook to attach a
coroutine to. And like MaintenanceLoop this is ON by default, because it is
not an optional feature -- it is what makes an already-shipped feature tell
the truth. Turning it off is a supported choice; needing to turn it ON to get
correct data would not be.

TWO PHASES, DELIBERATELY SPLIT
------------------------------
  refresh_local  reads this machine's own stores and re-projects them. Pure
                 local I/O, always worth doing, cannot fail because of a
                 peer.
  peer exchange  talks to other nodes. Optional, and separately backed off,
                 because most of this fleet cannot do it yet: four of five
                 node agents run a build with no /v1/fleet/* route and answer
                 404 forever. Retrying those every cycle would be pure noise
                 in the log and pure load on the nodes, so an unsupported
                 peer backs off exponentially instead of being hammered.

The split matters: without it, one unreachable node would make the local
refresh look like a failure, and the metadata everything reads would go stale
for a reason that has nothing to do with it.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

# Must stay comfortably under fleet_service.STALE_AFTER_SECONDS (900) so
# ordinary operation never crosses the stale line. Three cycles of headroom
# means two consecutive failures still leave the data fresh.
DEFAULT_INTERVAL_SECONDS = 300

# A peer that answered "no such route" is not going to start supporting it
# between two cycles. Back off hard, but never permanently -- a node agent
# gets upgraded eventually, and a loop that gave up forever would need a
# controller restart to notice.
UNSUPPORTED_BACKOFF_CYCLES = (1, 2, 4, 8, 16, 32)
MAX_BACKOFF_CYCLES = 32


@dataclass
class _PeerState:
    """How a single peer has been behaving, for backoff purposes only."""

    skip_cycles: int = 0
    failures: int = 0
    unsupported: bool = False
    proven: bool = False       # has a real exchange with this peer ever worked?
    last_error: str | None = None


@dataclass
class FleetSyncLoopConfig:
    enabled: bool = True
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    # Peer exchange is separable so a deployment whose nodes all predate the
    # fleet endpoint can keep the local refresh (which is the half that makes
    # its own dashboard correct) without generating 404s every cycle.
    peer_exchange_enabled: bool = True


class FleetSyncLoop:
    """Re-projects local fleet truth on an interval, and exchanges with peers.

    `sources` is injected rather than imported so this module never reaches
    into TerminalService/ConnectionStore itself -- the caller that already
    owns those hands them over, and a test can hand over anything.
    """

    def __init__(self, *, sync: Any, config: FleetSyncLoopConfig,
                 sources: Callable[[], dict[str, Any]] | None = None) -> None:
        self._sync = sync
        self._config = config
        self._sources = sources or (lambda: {"sessions": [], "connections": []})
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._peers: dict[str, _PeerState] = {}
        self._cycles = 0
        self._last_result: dict[str, Any] = {}
        self._last_error: str | None = None
        self._last_run_at: float | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not self._config.enabled:
            _LOGGER.info("fleet sync loop disabled by config")
            return
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-fleet-sync",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        # Refresh IMMEDIATELY rather than after the first interval. A
        # controller that just started has the emptiest, least useful cache
        # it will ever have; making an operator wait five minutes for the
        # dashboard to be right is exactly the gap this closes.
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 -- a loop that dies is worse than a bad cycle
                _LOGGER.exception("fleet sync: cycle failed")
            self._stop_event.wait(self._config.interval_seconds)

    # -- one cycle -----------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        """One refresh, plus whatever peer exchange is due this cycle.

        Never raises. The local refresh and the peer sweep are reported
        separately so a fleet of unreachable peers cannot make a perfectly
        good local refresh look like a failure.
        """
        self._cycles += 1
        result: dict[str, Any] = {"cycle": self._cycles, "refreshed": None, "peers": [],
                                  "skipped_peers": [], "errors": []}
        try:
            sources = self._sources()
        except Exception as exc:  # noqa: BLE001
            sources = {"sessions": [], "connections": []}
            result["errors"].append(f"sources:{type(exc).__name__}")
            _LOGGER.exception("fleet sync: could not read local sources")

        try:
            result["refreshed"] = self._sync.service.refresh_local(
                nodes=list(self._sync.controller.list_nodes()),
                sessions=sources.get("sessions") or [],
                connections=sources.get("connections") or [])
        except Exception as exc:  # noqa: BLE001 -- local refresh is best-effort
            result["errors"].append(f"refresh:{type(exc).__name__}")
            _LOGGER.exception("fleet sync: local refresh failed")

        if self._config.peer_exchange_enabled:
            result.update(self._exchange_due_peers())

        self._last_result = result
        self._last_run_at = time.time()
        self._last_error = result["errors"][0] if result["errors"] else None
        return result

    def _exchange_due_peers(self) -> dict[str, Any]:
        done: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        try:
            nodes = list(self._sync.controller.list_nodes())
        except Exception:  # noqa: BLE001
            return {"peers": done, "skipped_peers": skipped}

        transport = self._sync._peer_transport()
        local = self._sync.service.local_node_id
        for node in nodes:
            node_id = getattr(node, "id", None)
            if not node_id or node_id == local:
                continue
            state = self._peers.setdefault(node_id, _PeerState())
            if state.skip_cycles > 0:
                state.skip_cycles -= 1
                skipped.append({"peer_node": node_id, "cycles_left": state.skip_cycles,
                                "unsupported": state.unsupported,
                                "reason": state.last_error})
                continue
            # A peer we have never successfully exchanged with gets a small
            # probe instead of the full export. See FleetSyncService.probe:
            # posting 363 objects at a route that may not exist is what made
            # the failure look like a network fault.
            endpoint = getattr(node, "endpoint", None)
            never_worked = state.failures > 0 or not state.proven
            call = (self._sync.service.sync.probe if never_worked
                    else self._sync.service.sync.exchange)
            outcome = call(node_id, transport=transport, endpoint=endpoint)
            if outcome.ok:
                state.failures = 0
                state.unsupported = False
                state.last_error = None
                state.skip_cycles = 0
                # Proven: from here on this peer gets the full exchange.
                state.proven = True
            else:
                state.failures += 1
                state.last_error = outcome.error
                # "No such route" is a different thing from "unreachable":
                # one is a build that will never answer until it is upgraded,
                # the other is a node that may be back next cycle.
                state.unsupported = _looks_unsupported(outcome.error)
                index = min(state.failures - 1, len(UNSUPPORTED_BACKOFF_CYCLES) - 1)
                state.skip_cycles = (UNSUPPORTED_BACKOFF_CYCLES[index]
                                     if state.unsupported else min(state.failures, 3))
            done.append(outcome.as_dict())
        return {"peers": done, "skipped_peers": skipped}

    # -- introspection --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What the loop is doing, for the doctor and the dashboard.

        Includes `age_seconds` because "the loop is alive" and "the data is
        fresh" are different claims, and only the second one matters to
        whoever is reading the fleet view.
        """
        age = (time.time() - self._last_run_at) if self._last_run_at else None
        return {
            "enabled": self._config.enabled,
            "running": self.is_alive(),
            "interval_seconds": self._config.interval_seconds,
            "peer_exchange_enabled": self._config.peer_exchange_enabled,
            "cycles": self._cycles,
            "last_run_at": self._last_run_at,
            "age_seconds": round(age, 1) if age is not None else None,
            "last_error": self._last_error,
            "last_result": self._last_result,
            "peers": {node_id: {"failures": state.failures,
                                "unsupported": state.unsupported,
                                "skip_cycles": state.skip_cycles,
                                "last_error": state.last_error}
                      for node_id, state in sorted(self._peers.items())},
        }


def _looks_unsupported(error: str | None) -> bool:
    """Does this error mean "that build has no fleet endpoint"?

    Matched on the shape of the message rather than an exception type because
    the node client collapses every transport failure into one error class --
    and telling "upgrade this node" apart from "this node is down" is the
    whole point of backing off differently.
    """
    text = (error or "").casefold()
    if "404" in text or "not found" in text:
        return True
    # Measured on this fleet 2026-09-13: an agent without /v1/fleet/* does
    # not answer a tidy 404. It resets the connection while the request body
    # is still going out, so the client sees ECONNRESET or EPIPE. Those are
    # ambiguous in general -- but on a node whose heartbeat and status calls
    # are working, a reset on THIS route specifically means the route is not
    # there.
    return "connection reset" in text or "broken pipe" in text
