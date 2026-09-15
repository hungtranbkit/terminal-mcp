"""The event bus gets consumed, by the loop that already exists.

Before this, `event_wiring.py` published every queue transition onto the bus and
nothing ever claimed one. The bus was write-only: events accumulated forever, a
task whose dependency had just been satisfied waited for whatever cycle happened
to notice, and the bus's own dead-letter machinery could never fire because
nothing had ever claimed an event to fail.

These tests use the real EventBus (real SQLite, real claim/ack/lease semantics)
with a recording engine stand-in. The engine is the part whose behaviour is
already covered by test_queue_engine.py; what is under test here is the
claim/act/ack contract, the three gates, and what happens when things go wrong.
"""
from __future__ import annotations

import threading

import pytest

from terminal_mcp.event_bus import ACKED, CLAIMED, FAILED, PENDING, EventBus
from terminal_mcp.queue_event_drain import (
    ACTION_ALREADY_TICKED, ACTION_FAILED, ACTION_LANE_NOT_ENABLED, ACTION_NO_LANE,
    ACTION_NOT_ACTIONABLE, ACTION_TICKED, ACTIONABLE_EVENT_TYPES, QueueEventDrain,
)


class FakeStore:
    def __init__(self, enabled_lanes=(), unreadable=()):
        self.enabled_lanes = set(enabled_lanes)
        self.unreadable = set(unreadable)

    def lane_status(self, session):
        if session in self.unreadable:
            raise RuntimeError("lane table is unreadable")
        return {"session": session, "auto_dispatch_enabled": session in self.enabled_lanes}


class FakeResult:
    def __init__(self, session, action="DISPATCHED"):
        self.session, self.action = session, action

    def to_dict(self):
        return {"session": self.session, "action": self.action}


class FakeEngine:
    def __init__(self, store, *, fail_for=()):
        self.store = store
        self.fail_for = set(fail_for)
        self.ticks: list[str] = []

    def tick(self, session):
        self.ticks.append(session)
        if session in self.fail_for:
            raise RuntimeError("engine exploded")
        return FakeResult(session)


@pytest.fixture
def bus(tmp_path):
    return EventBus(tmp_path / "events.db")


def _publish(bus, type_, session, **kw):
    return bus.publish(type_, entity_type="task", entity_id=kw.pop("task_id", "t1"),
                       payload={"session": session, **kw.pop("payload", {})}, **kw)


def _drain(bus, engine, **kw):
    return QueueEventDrain(bus, engine, **kw)


def _status_of(bus, event_id):
    return bus.get(event_id)["status"]


# == the core contract ===================================================

def test_an_actionable_event_ticks_its_lane_and_is_acked(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    event = _publish(bus, "TASK_COMPLETED", "lane-a")

    result = _drain(bus, engine).drain_once()

    assert engine.ticks == ["lane-a"]
    assert [r["action"] for r in result["results"]] == [ACTION_TICKED]
    assert result["ticked_lanes"] == ["lane-a"]
    assert _status_of(bus, event["id"]) == ACKED


def test_a_non_actionable_event_is_consumed_without_touching_the_lane(bus):
    """TASK_STARTED describes progress the queue itself just made. Ticking on it
    is wasted work at best, and a second actor poking a lane mid-transition at
    worst -- but it must still be consumed, or it is re-delivered forever."""
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    event = _publish(bus, "TASK_STARTED", "lane-a")

    result = _drain(bus, engine).drain_once()

    assert engine.ticks == []
    assert [r["action"] for r in result["results"]] == [ACTION_NOT_ACTIONABLE]
    assert _status_of(bus, event["id"]) == ACKED


def test_the_bus_is_emptied_rather_than_growing_forever(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    for _ in range(8):
        _publish(bus, "TASK_STARTED", "lane-a")
    _drain(bus, engine).drain_once()
    assert bus.claim_next(consumer="someone-else") is None


@pytest.mark.parametrize("event_type", sorted(ACTIONABLE_EVENT_TYPES))
def test_every_actionable_type_actually_ticks(bus, event_type):
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    _publish(bus, event_type, "lane-a")
    _drain(bus, engine).drain_once()
    assert engine.ticks == ["lane-a"], f"{event_type} is listed actionable but did not tick"


# == the gates ===========================================================

def test_a_lane_that_never_opted_in_is_never_dispatched_into(bus):
    """The protection that keeps a production session safe: an event names its
    lane directly, so without this check the drain would be a way into a lane
    that never enabled auto-dispatch."""
    engine = FakeEngine(FakeStore(enabled_lanes=["opted-in"]))
    event = _publish(bus, "TASK_COMPLETED", "window2")

    result = _drain(bus, engine).drain_once()

    assert engine.ticks == [], "a lane that never opted in was ticked"
    assert [r["action"] for r in result["results"]] == [ACTION_LANE_NOT_ENABLED]
    assert _status_of(bus, event["id"]) == ACKED


def test_an_unreadable_lane_is_treated_as_not_opted_in(bus):
    """Fail closed. Guessing wrong here means dispatching into a production
    session that never asked for it."""
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"], unreadable=["lane-a"]))
    _publish(bus, "TASK_COMPLETED", "lane-a")
    result = _drain(bus, engine).drain_once()
    assert engine.ticks == []
    assert [r["action"] for r in result["results"]] == [ACTION_LANE_NOT_ENABLED]


def test_an_event_naming_no_lane_is_consumed_not_stranded(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    event = bus.publish("TASK_COMPLETED", entity_type="task", entity_id="t9", payload={})
    result = _drain(bus, engine).drain_once()
    assert [r["action"] for r in result["results"]] == [ACTION_NO_LANE]
    assert _status_of(bus, event["id"]) == ACKED


# == idempotency and dependency wake-ups =================================

def test_many_events_for_one_lane_produce_one_tick(bus):
    """Ten completions in a lane are one tick, not ten. tick() performs one
    transition per call, so ten calls would be nine no-ops -- and each no-op is
    a lane read and a claim attempt on a loop that has other lanes to service."""
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    for _ in range(10):
        _publish(bus, "TASK_COMPLETED", "lane-a")

    result = _drain(bus, engine).drain_once()

    assert engine.ticks == ["lane-a"]
    actions = [r["action"] for r in result["results"]]
    assert actions[0] == ACTION_TICKED
    assert set(actions[1:]) == {ACTION_ALREADY_TICKED}
    assert result["drained"] == 10, "all ten must still be consumed"


def test_distinct_lanes_each_get_their_own_tick(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["a", "b", "c"]))
    for lane in ("a", "b", "c", "a"):
        _publish(bus, "TASK_COMPLETED", lane)
    result = _drain(bus, engine).drain_once()
    assert engine.ticks == ["a", "b", "c"]
    assert result["ticked_lanes"] == ["a", "b", "c"]


def test_redelivery_after_a_crash_before_ack_is_harmless(bus):
    """At-least-once delivery. A drain that dies after ticking and before acking
    sees the event again once its lease expires; the second tick must be safe."""
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    event = _publish(bus, "TASK_COMPLETED", "lane-a")

    claimed = bus.claim_next(consumer="queue-loop", lease_seconds=0.0)  # lease already expired
    assert claimed["id"] == event["id"]

    # A fresh drain pass finds it eligible again and handles it properly.
    result = _drain(bus, engine).drain_once()
    assert [r["action"] for r in result["results"]] == [ACTION_TICKED]
    assert _status_of(bus, event["id"]) == ACKED


def test_a_second_pass_over_an_empty_bus_does_nothing(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    _publish(bus, "TASK_COMPLETED", "lane-a")
    drain = _drain(bus, engine)
    drain.drain_once()
    second = drain.drain_once()
    assert second["drained"] == 0
    assert engine.ticks == ["lane-a"], "an empty pass re-ticked a lane"


# == failure handling ====================================================

def test_a_failing_tick_is_reported_as_a_failure_not_acked(bus):
    """fail(), not ack(): the bus's attempt counter and dead-letter table are
    what stop a permanently broken event being retried forever, and they only
    work if failures are reported as failures."""
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]), fail_for=["lane-a"])
    event = _publish(bus, "TASK_COMPLETED", "lane-a")

    result = _drain(bus, engine).drain_once()

    assert [r["action"] for r in result["results"]] == [ACTION_FAILED]
    assert _status_of(bus, event["id"]) != ACKED


def test_one_failing_lane_does_not_stop_the_others_in_the_pass(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["bad", "good"]), fail_for=["bad"])
    _publish(bus, "TASK_COMPLETED", "bad")
    _publish(bus, "TASK_COMPLETED", "good")

    result = _drain(bus, engine).drain_once()

    assert "good" in engine.ticks
    assert {r["action"] for r in result["results"]} == {ACTION_FAILED, ACTION_TICKED}


def test_a_poison_event_eventually_dead_letters_instead_of_retrying_forever(bus):
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]), fail_for=["lane-a"])
    _publish(bus, "TASK_COMPLETED", "lane-a")
    drain = _drain(bus, engine)
    for _ in range(12):
        drain.drain_once()
    assert bus.dead_letters(), "a permanently failing event never dead-lettered"


def test_an_unreadable_bus_does_not_raise(bus):
    class BrokenBus:
        def claim_next(self, **kw):
            raise RuntimeError("database is locked")

    result = QueueEventDrain(BrokenBus(), FakeEngine(FakeStore())).drain_once()
    assert result["drained"] == 0


def test_an_ack_failure_does_not_lose_the_pass(bus):
    """A failed ack means re-delivery later, which is safe -- but it must not
    take down the rest of the pass."""
    engine = FakeEngine(FakeStore(enabled_lanes=["a", "b"]))

    class FlakyAck:
        def __init__(self, inner):
            self.inner = inner
            self.acks = 0

        def claim_next(self, **kw):
            return self.inner.claim_next(**kw)

        def ack(self, event_id, token):
            self.acks += 1
            raise RuntimeError("ack failed")

        def fail(self, *a, **kw):
            return self.inner.fail(*a, **kw)

    _publish(bus, "TASK_COMPLETED", "a")
    result = QueueEventDrain(FlakyAck(bus), engine, batch_size=2).drain_once()
    assert result["drained"] >= 1


# == bounds and concurrency ==============================================

def test_the_pass_is_bounded_by_batch_size(bus):
    """A loop cycle must stay bounded: an unbounded drain on a large backlog
    would stall lane dispatch for as long as the backlog took."""
    engine = FakeEngine(FakeStore(enabled_lanes=[f"lane-{n}" for n in range(20)]))
    for n in range(20):
        _publish(bus, "TASK_COMPLETED", f"lane-{n}")

    result = _drain(bus, engine, batch_size=5).drain_once()

    assert result["drained"] == 5
    assert len(engine.ticks) == 5
    assert bus.claim_next(consumer="other") is not None, "the rest must remain for the next pass"


def test_a_re_entrant_drain_returns_instead_of_queueing(bus):
    """The loop thread and a manual drain call can arrive together. The second
    must not block the first's cycle."""
    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    drain = _drain(bus, engine)
    observed = {}

    class BlockingEngine(FakeEngine):
        def tick(self, session):
            observed["reentrant"] = drain.drain_once()
            return super().tick(session)

    drain.engine = BlockingEngine(FakeStore(enabled_lanes=["lane-a"]))
    _publish(bus, "TASK_COMPLETED", "lane-a")
    drain.drain_once()

    assert observed["reentrant"]["skipped_busy"] is True
    assert observed["reentrant"]["drained"] == 0


def test_two_concurrent_drains_never_handle_the_same_event(bus):
    """Cross-instance exclusion is the bus's claim lease, not a local lock. Two
    drains with the same consumer name must partition the events, never
    duplicate them -- a duplicated TASK_COMPLETED means two ticks racing on one
    lane."""
    store = FakeStore(enabled_lanes=[f"lane-{n}" for n in range(30)])
    engines = [FakeEngine(store), FakeEngine(store)]
    for n in range(30):
        _publish(bus, "TASK_COMPLETED", f"lane-{n}")

    drains = [QueueEventDrain(bus, engine, batch_size=30) for engine in engines]
    threads = [threading.Thread(target=d.drain_once) for d in drains]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    all_ticks = engines[0].ticks + engines[1].ticks
    assert len(all_ticks) == len(set(all_ticks)), f"a lane was ticked twice: {all_ticks}"
    assert sorted(all_ticks) == sorted(f"lane-{n}" for n in range(30))


# == lifecycle inside the ONE existing loop ==============================

def test_the_loop_drains_as_part_of_its_own_cycle(tmp_path, bus):
    """No second thread. The drain is a step of the cycle that already runs."""
    from terminal_mcp.queue_loop import QueueLoop

    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    engine.store.list_all_lanes = lambda: []
    drain = _drain(bus, engine)
    loop = QueueLoop(engine, event_drain=drain)
    _publish(bus, "TASK_COMPLETED", "lane-a")

    loop.run_one_cycle()

    assert engine.ticks == ["lane-a"]
    assert loop.status()["drain_enabled"] is True
    assert loop.status()["last_drain"]["drained"] == 1


def test_a_loop_with_no_drain_behaves_exactly_as_before(bus):
    from terminal_mcp.queue_loop import QueueLoop

    engine = FakeEngine(FakeStore())
    engine.store.list_all_lanes = lambda: []
    loop = QueueLoop(engine)
    loop.run_one_cycle()
    assert loop.status()["drain_enabled"] is False
    assert loop.status()["last_drain"] is None


def test_a_broken_drain_does_not_stop_lane_dispatch(bus):
    """Dispatch is the loop's primary job. A drain that throws must not take the
    lane sweep down with it."""
    from terminal_mcp.queue_loop import QueueLoop

    class ExplodingDrain:
        def drain_once(self):
            raise RuntimeError("drain exploded")

    swept = []

    class Store:
        def list_all_lanes(self):
            return [{"session": "lane-a", "auto_dispatch_enabled": True}]

        def lane_status(self, session):
            return {"session": session, "auto_dispatch_enabled": True}

    engine = FakeEngine(Store())
    engine.tick = lambda session: swept.append(session) or FakeResult(session)
    loop = QueueLoop(engine, event_drain=ExplodingDrain())

    loop.run_one_cycle()

    assert swept == ["lane-a"], "a broken drain stopped the lane sweep"


def test_stopping_the_loop_stops_draining(bus):
    from terminal_mcp.queue_loop import QueueLoop

    engine = FakeEngine(FakeStore(enabled_lanes=["lane-a"]))
    engine.store.list_all_lanes = lambda: []
    drain = _drain(bus, engine)
    loop = QueueLoop(engine, poll_interval_seconds=0.5, event_drain=drain)
    loop.start()
    try:
        deadline = __import__("time").monotonic() + 10
        while not loop.status()["last_drain"] and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.05)
        assert loop.status()["last_drain"] is not None, "the running loop never drained"
    finally:
        loop.stop()
    assert loop.is_alive() is False

    before = len(engine.ticks)
    _publish(bus, "TASK_COMPLETED", "lane-a")
    __import__("time").sleep(1.0)
    assert len(engine.ticks) == before, "the loop kept draining after stop()"


def test_the_drain_is_off_by_default_when_the_app_is_built():
    """Two independent switches. Enabling auto-dispatch must not silently also
    start consuming the event bus."""
    from terminal_mcp.config import QueueConfig

    assert QueueConfig().drain_enabled is False
    assert QueueConfig(enabled=True).drain_enabled is False, \
        "turning on the loop must not turn on the drain"
