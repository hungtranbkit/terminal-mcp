"""Consume the event bus, and let the queue react to what it says.

THE GAP THIS CLOSES. `event_wiring.py` publishes every meaningful queue
transition onto the EventBus, and the bus implements a full consumer protocol
(claim_next / ack / fail / release, with leases, bounded attempts and a
dead-letter table). Nothing ever called any of it. So the bus was
write-only: events accumulated forever, no dependent task was ever woken by
the completion that unblocked it, and the dead-letter machinery could not
trigger because nothing had ever claimed an event to begin with.

WHY THIS IS NOT A NEW LOOP. `QueueLoop` already exists and already runs on a
timer. A second thread draining the bus would mean two schedulers acting on the
same lanes with no ordering between them -- two ticks for one lane racing each
other, which the lane's own claim lease would then have to arbitrate on every
cycle. So this is a plain step, `drain_once()`, that QueueLoop calls inside its
existing cycle. There is exactly one scheduler.

WHAT IT DOES. For each claimed event whose type can change what the queue should
do next (a task finished, failed, was blocked, a lease expired, a verification
landed), it ticks that event's lane -- which is how a task whose `depends_on`
edge was just satisfied gets picked up promptly rather than on whatever cycle
happens to notice. Events that carry no queue action are acknowledged and
dropped, because leaving them claimed would just re-deliver them forever.

THREE GATES, all of which must be open before any lane is touched -- the same
stacked posture as queue_loop.py, with one added:
  1. `config.queue.enabled`      -- the global background-loop switch
  2. `config.queue.drain_enabled` -- this drain specifically, default False, so
     enabling auto-dispatch does not silently also enable bus-driven reaction
  3. `queue_lanes.auto_dispatch_enabled` -- the per-lane opt-in, checked here
     as well as in QueueLoop. Not redundant: an event names a lane directly, so
     without this check the drain would be a way to dispatch into a lane that
     never opted in, which is exactly the protection the per-lane gate exists to
     give (`window`/`window2` must stay untouched however busy the bus is).

IDEMPOTENCY. Delivery is at-least-once: a drain that dies after acting and
before acking will see the event again. Two things make that safe. `tick()`
performs at most one state transition per call and reconciles stale claims
first, so a repeat is a no-op or the next legitimate step. And a lane is ticked
at most once per pass no matter how many of its events are in the batch -- ten
completions in one lane are one tick, not ten.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_CONSUMER = "queue-loop"
DEFAULT_BATCH_SIZE = 25

ACTIONABLE_EVENT_TYPES = frozenset({
    # A task reached a terminal state: dependents may now be dispatchable.
    "TASK_COMPLETED", "TASK_FAILED", "TASK_BLOCKED",
    # The worker says it is done; the lane's next step is verification.
    "WORKER_DONE",
    # Verification landed either way: the lane can move again.
    "VERIFY_PASS", "VERIFY_FAIL", "VERIFY_BLOCKED",
    # A pane came free, or work moved. The lane may now be able to dispatch.
    "LEASE_RELEASED", "LEASE_EXPIRED", "TASK_HANDOFF",
})
"""Types whose arrival can change what a lane should do next.

Deliberately NOT every known type. TASK_CREATED/TASK_READY/TASK_CLAIMED/
TASK_STARTED describe the queue's own forward progress, and ticking a lane
because it just started something is at best wasted work and at worst a second
actor poking a lane mid-transition. They are still consumed -- just not acted
on."""

ACTION_TICKED = "TICKED"
ACTION_NO_LANE = "NO_LANE"
ACTION_LANE_NOT_ENABLED = "LANE_NOT_ENABLED"
ACTION_NOT_ACTIONABLE = "NOT_ACTIONABLE"
ACTION_ALREADY_TICKED = "ALREADY_TICKED"
ACTION_FAILED = "FAILED"


class QueueEventDrain:
    """One bounded pass over the bus. Owns no thread of its own."""

    def __init__(self, bus: Any, engine: Any, *, consumer: str = DEFAULT_CONSUMER,
                 batch_size: int = DEFAULT_BATCH_SIZE,
                 project_id: str | None = None) -> None:
        self.bus = bus
        self.engine = engine
        self.consumer = consumer
        self.batch_size = max(1, int(batch_size))
        self.project_id = project_id
        # Guards one INSTANCE against re-entry (the loop thread plus a manual
        # `terminal_queue_drain_once` call, say). Cross-process/cross-instance
        # exclusion is the bus's own claim lease, not this lock -- two drains
        # with the same consumer name cannot claim the same event.
        self._lock = threading.Lock()

    def drain_once(self) -> dict[str, Any]:
        """Claim up to `batch_size` events, act, then ack or fail each.

        Never raises. A drain that could throw would take the queue loop's
        whole cycle down with it, which would stop lane dispatch -- a strictly
        worse outcome than an event going unhandled for one interval."""
        if not self._lock.acquire(blocking=False):
            # Another drain on this instance is mid-pass. Returning rather than
            # queueing behind it keeps the loop's cycle time bounded.
            return {"drained": 0, "skipped_busy": True, "results": []}
        try:
            return self._drain_locked()
        finally:
            self._lock.release()

    def _drain_locked(self) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        ticked_lanes: set[str] = set()
        for _ in range(self.batch_size):
            try:
                claimed = self.bus.claim_next(consumer=self.consumer, project_id=self.project_id)
            except Exception:  # noqa: BLE001 -- an unreadable bus must not stop lane dispatch
                _LOGGER.exception("queue-drain: claim_next failed; ending this pass")
                break
            if claimed is None:
                break
            results.append(self._handle(claimed, ticked_lanes))
        return {"drained": len(results),
                "ticked_lanes": sorted(ticked_lanes),
                "skipped_busy": False,
                "results": results}

    def _handle(self, claimed: dict[str, Any], ticked_lanes: set[str]) -> dict[str, Any]:
        event_id = claimed.get("id")
        token = claimed.get("claim_token")
        event_type = str(claimed.get("type") or "")
        session = self._session_of(claimed)
        outcome = {"event_id": event_id, "type": event_type, "session": session}

        if event_type not in ACTIONABLE_EVENT_TYPES:
            self._ack(event_id, token)
            return {**outcome, "action": ACTION_NOT_ACTIONABLE}
        if not session:
            # An event about a task that names no lane. Nothing to tick, and
            # holding it claimed would re-deliver it until it dead-lettered.
            self._ack(event_id, token)
            return {**outcome, "action": ACTION_NO_LANE}
        if session in ticked_lanes:
            self._ack(event_id, token)
            return {**outcome, "action": ACTION_ALREADY_TICKED}
        if not self._lane_opted_in(session):
            self._ack(event_id, token)
            return {**outcome, "action": ACTION_LANE_NOT_ENABLED}

        try:
            result = self.engine.tick(session)
        except Exception as exc:  # noqa: BLE001 -- one lane's failure is not the pass's
            # fail(), not ack(): the bus's own attempt counter and dead-letter
            # table are what stop a permanently broken event from being retried
            # forever, and they only work if failures are reported as failures.
            _LOGGER.exception("queue-drain: tick failed for lane %r", session)
            self._fail(event_id, token, f"{type(exc).__name__}: {exc}")
            return {**outcome, "action": ACTION_FAILED, "detail": f"{type(exc).__name__}: {exc}"}

        ticked_lanes.add(session)
        self._ack(event_id, token)
        detail = result.to_dict() if hasattr(result, "to_dict") else {"result": str(result)}
        return {**outcome, "action": ACTION_TICKED, "tick": detail}

    @staticmethod
    def _session_of(claimed: dict[str, Any]) -> str | None:
        payload = claimed.get("payload")
        if isinstance(payload, dict):
            session = payload.get("session")
            if session:
                return str(session)
        return None

    def _lane_opted_in(self, session: str) -> bool:
        """Per-lane gate. An unreadable lane is treated as NOT opted in: the
        fail-closed direction, because the cost of guessing wrong is dispatching
        into a production session that never asked for it."""
        try:
            lane = self.engine.store.lane_status(session)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("queue-drain: could not read lane %r; treating as not opted in", session)
            return False
        return bool(lane.get("auto_dispatch_enabled"))

    def _ack(self, event_id: Any, token: Any) -> None:
        try:
            self.bus.ack(event_id, token)
        except Exception:  # noqa: BLE001 -- a failed ack re-delivers later, which is safe
            _LOGGER.exception("queue-drain: ack failed for event %r", event_id)

    def _fail(self, event_id: Any, token: Any, error: str) -> None:
        try:
            self.bus.fail(event_id, token, error=error)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("queue-drain: fail() failed for event %r", event_id)
