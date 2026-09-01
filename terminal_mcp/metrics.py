"""Internal metrics/readiness surface -- final audit pass item #12.

No external metrics backend (Prometheus, StatsD, ...) is configured for
this deployment, and none is invented here -- this is a small, in-process,
thread-safe counter registry plus a couple of point-in-time gauge
computations, exposed read-only via /health/metrics (health.py). It exists
so the specific events this project's own safety model cares about
(a send coming back unconfirmed, an identity mismatch, a policy block, a
lease/pane contention, how stale the supervisor loop's last poll is) are
at least observable and countable *now*, in a shape a real metrics backend
could scrape or forward later (the counter names are already flat,
dotted, Prometheus-metric-name-shaped) -- without pulling in a client
library or a network dependency this single-operator, loopback-first
deployment doesn't otherwise need.

Deliberately NOT persisted to disk: this is in-memory, per-process,
reset on restart -- exactly the same lifetime as the process health/
readiness itself already has (see health.py), and consistent with "read-
only, no side effects, cheap" for anything on this surface.
"""
from __future__ import annotations

import threading

# Fixed, known counter names -- every increment call site names one of
# these explicitly rather than an ad-hoc string, so /health/metrics always
# reports a complete, predictable set (zero-valued ones included) instead
# of only whichever have fired at least once since start.
COUNTER_NAMES = (
    "delivery.text_sent",
    "delivery.submit_confirmed",
    "delivery.delivery_unknown",
    "delivery.blocked",
    "delivery.error",
    "delivery.identity_mismatch",
    "delivery.pane_in_copy_mode",
    "delivery.pane_busy",
    "delivery.recovery_attempted",
    "supervisor.policy_blocked",
    "supervisor.action_failed",
)


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = dict.fromkeys(COUNTER_NAMES, 0)

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counters)


# One process-wide registry -- every send/guard/policy call site in this
# process (HTTP or STDIO -- each is its own process, so each reports only
# its own counters; there is no cross-process aggregation here, matching
# this surface's stated "later exportable" rather than "already a full
# metrics system" scope) increments the same instance.
REGISTRY = MetricsRegistry()


def increment(name: str, amount: int = 1) -> None:
    REGISTRY.increment(name, amount)


def snapshot() -> dict[str, int]:
    return REGISTRY.snapshot()


def record_delivery_outcome(response: dict) -> None:
    """Single shared classifier for a terminal_send_text/_keys/_bound/
    _granted response dict -- called from core.py's _audit_result (the one
    choke point every one of those paths already passes through) so every
    input attempt is counted exactly once, consistently, regardless of
    which specific tool/path produced it."""
    error = response.get("error")
    delivery_state = response.get("delivery_state")
    if error == "IDENTITY_CHANGED_MID_SEND" or error == "IDENTITY_MISMATCH":
        increment("delivery.identity_mismatch")
    if error == "PANE_IN_COPY_MODE":
        increment("delivery.pane_in_copy_mode")
    if error == "PANE_BUSY":
        increment("delivery.pane_busy")
    if delivery_state == "TEXT_SENT":
        increment("delivery.text_sent")
    elif delivery_state == "SUBMIT_CONFIRMED":
        increment("delivery.submit_confirmed")
    elif delivery_state == "DELIVERY_UNKNOWN":
        increment("delivery.delivery_unknown")
    elif delivery_state == "BLOCKED":
        increment("delivery.blocked")
    elif delivery_state == "ERROR":
        increment("delivery.error")
    if response.get("recovery_attempted"):
        increment("delivery.recovery_attempted")
