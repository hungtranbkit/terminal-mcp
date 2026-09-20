"""The synchronous route has ONE budget, and looking at the fleet gets a slice.

THE LIVE FAILURE THIS PINS (hp-linux, 2026-09-20). `project_start` returned
after 12.5s with `dispatch_ticks: 0` and `budget_exhausted: true`: the router
had spent its entire 12s dispatch budget inside `terminal_list_sessions`,
which walked five nodes in series and took between 3.6s and 25.4s depending on
which one was slow that second. The task was bound and never started, so the
caller got TASK_ACCEPTED/QUEUED for work that looked assigned and was not
running.

Three separate defects, three separate tests here:

  * the fleet fan-out was SERIAL  -> controller asks every node at once
  * it was UNBOUNDED             -> one wall clock, a slow node is reported
  * it could eat the WHOLE budget -> the snapshot gets a slice, not all of it
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from terminal_mcp.config import RouterConfig
from terminal_mcp.controller import _fanout
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.task_router import TaskRouter

from tests.test_task_router import (FakeController, FakeNode, FakeRecord,
                                    RecordingEngine)


# ---------------------------------------------------------------------------
# The fan-out primitive itself.
# ---------------------------------------------------------------------------

def test_the_fanout_costs_the_slowest_node_not_the_sum_of_them():
    def slow(seconds):
        def call():
            time.sleep(seconds)
            return seconds
        return call

    work = [(f"node{i}", slow(0.25)) for i in range(5)]
    started = time.monotonic()
    answers = _fanout(work, budget_seconds=5.0, label="test")
    elapsed = time.monotonic() - started

    assert len(answers) == 5 and all(ok for ok, _ in answers.values())
    # Serial would be 1.25s. Generous ceiling so a loaded CI box still proves
    # the point rather than proving its own scheduler.
    assert elapsed < 0.9, f"fan-out took {elapsed:.2f}s -- that is serial, not concurrent"


def test_a_node_that_never_answers_is_reported_not_waited_on():
    forever = threading.Event()

    def hangs():
        forever.wait(30)
        return "too late"

    started = time.monotonic()
    answers = _fanout([("fast", lambda: "ok"), ("stuck", hangs)],
                      budget_seconds=0.4, label="test")
    elapsed = time.monotonic() - started
    forever.set()

    assert answers["fast"] == (True, "ok")
    ok, value = answers["stuck"]
    assert ok is False and isinstance(value, TimeoutError)
    assert elapsed < 3.0, "the budget did not bound the wait"


def test_one_node_raising_never_loses_the_others():
    def boom():
        raise RuntimeError("node exploded")

    answers = _fanout([("good", lambda: "ok"), ("bad", boom), ("also", lambda: "fine")],
                      budget_seconds=2.0, label="test")
    assert answers["good"] == (True, "ok")
    assert answers["also"] == (True, "fine")
    assert answers["bad"][0] is False and isinstance(answers["bad"][1], RuntimeError)


def test_a_single_node_is_run_inline_rather_than_in_a_pool():
    """The overwhelmingly common single-host deployment must not pay for a
    thread pool, and must not have its local TerminalService touched from a
    different thread than it always was."""
    seen: list[int] = []

    def call():
        seen.append(threading.get_ident())
        return "ok"

    answers = _fanout([("only", call)], budget_seconds=1.0, label="test")
    assert answers == {"only": (True, "ok")}
    assert seen == [threading.get_ident()]


# ---------------------------------------------------------------------------
# The controller's fleet listing.
# ---------------------------------------------------------------------------

class _SlowClient:
    def __init__(self, delay, sessions):
        self.delay = delay
        self.sessions = sessions
        self.timeouts: list[float | None] = []

    def list_sessions(self, *, timeout_seconds=None):
        self.timeouts.append(timeout_seconds)
        time.sleep(self.delay)
        return {"sessions": list(self.sessions)}


@dataclass
class _Node:
    id: str
    display_name: str = "n"
    status: str = "online"
    endpoint: str = "http://x"


class _StubController:
    """The real terminal_list_sessions, over stub nodes/clients."""

    from terminal_mcp.controller import ControllerService as _Real

    terminal_list_sessions = _Real.terminal_list_sessions
    # `_Real._session_lister` unwraps the staticmethod to a plain function, so
    # it has to be re-wrapped or `self` is passed as the client.
    _session_lister = staticmethod(_Real._session_lister)

    def __init__(self, clients, nodes):
        self._clients = clients
        self._nodes = nodes

    def list_nodes(self, *, budget_seconds=None):
        return list(self._nodes)


def test_the_fleet_listing_asks_every_node_at_once():
    clients = {name: _SlowClient(0.2, [{"name": f"{name}-a"}]) for name in ("a", "b", "c", "d")}
    controller = _StubController(clients, [_Node(id=name) for name in clients])

    started = time.monotonic()
    listing = controller.terminal_list_sessions(budget_seconds=5.0)
    elapsed = time.monotonic() - started

    assert {row["name"] for row in listing["sessions"]} == {"a-a", "b-a", "c-a", "d-a"}
    assert elapsed < 0.65, f"{elapsed:.2f}s for four 0.2s nodes is serial"
    assert listing["listed_in_seconds"] <= elapsed + 0.05


def test_a_slow_node_is_named_unreachable_instead_of_extending_the_call():
    clients = {"quick": _SlowClient(0.0, [{"name": "up"}]),
               "molasses": _SlowClient(5.0, [{"name": "never"}])}
    controller = _StubController(clients, [_Node(id="quick"), _Node(id="molasses")])

    started = time.monotonic()
    listing = controller.terminal_list_sessions(budget_seconds=0.5)
    elapsed = time.monotonic() - started

    assert [row["name"] for row in listing["sessions"]] == ["up"]
    stuck = [row for row in listing["unreachable_nodes"] if row["node_id"] == "molasses"]
    assert stuck and stuck[0]["status"] == "timeout"
    assert elapsed < 3.0
    # The node also got a socket-level ceiling, so an abandoned future does
    # not hold a connection open indefinitely.
    assert clients["molasses"].timeouts and clients["molasses"].timeouts[0] is not None


# ---------------------------------------------------------------------------
# The router's budget split.
# ---------------------------------------------------------------------------

class _SlowFleetController(FakeController):
    """A controller whose session listing takes a fixed, measurable time."""

    def __init__(self, *, delay, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay
        self.budgets: list[float | None] = []

    def terminal_list_sessions(self, *, budget_seconds=None):
        self.budgets.append(budget_seconds)
        time.sleep(min(self.delay, budget_seconds if budget_seconds else self.delay))
        return {"sessions": list(self._sessions), "unreachable_nodes": []}


def _router(tmp_path, controller, *, config=None, engine=None):
    store = QueueStore(tmp_path / "queue.db")
    queue = QueueService(store)
    router = TaskRouter(store, controller=controller, queue=queue,
                        engine=engine if engine is not None else RecordingEngine(store),
                        session_registry=controller.session_registry,
                        config=config, clock=time.monotonic)
    queue.router = router
    return store, queue, router


def test_the_fleet_snapshot_gets_a_slice_of_the_budget_not_all_of_it(tmp_path):
    controller = _SlowFleetController(
        delay=0.0,
        sessions=[{"name": "worker", "node_id": "local", "effective_input": True}],
        records=[FakeRecord(node_id="local", session_name="worker")])

    class _Config:
        router = RouterConfig(dispatch_budget_seconds=12.0, snapshot_budget_seconds=4.0)

    _store, _queue, router = _router(tmp_path, controller, config=_Config())
    receipt = router.route_start("do the thing", title="t")

    assert controller.budgets, "the router never passed a budget to the fleet listing"
    assert controller.budgets[0] == pytest.approx(4.0)
    # ... and the WHOLE budget was not handed over: half is the hard ceiling.
    assert controller.budgets[0] <= 12.0 * 0.5

    evidence_budget = router.store.get_task(receipt["task_id"]).routing_evidence
    assert receipt["dispatched"] is True, (
        "the task was bound but never started -- exactly the live failure")
    assert receipt["dispatch_ticks"] >= 1


def test_a_tiny_dispatch_budget_still_leaves_room_to_dispatch(tmp_path):
    """snapshot_budget_seconds is a CEILING, not an allocation. With a 2s total
    budget the snapshot may not take 4s of it."""
    controller = _SlowFleetController(
        delay=0.0,
        sessions=[{"name": "worker", "node_id": "local", "effective_input": True}],
        records=[FakeRecord(node_id="local", session_name="worker")])

    class _Config:
        router = RouterConfig(dispatch_budget_seconds=2.0, snapshot_budget_seconds=4.0)

    _store, _queue, router = _router(tmp_path, controller, config=_Config())
    router.route_start("do the thing", title="t")
    assert controller.budgets[0] == pytest.approx(1.0)


def test_the_routing_evidence_records_what_the_snapshot_cost(tmp_path):
    """"Why was this slow" has to be answerable from the durable row."""
    controller = _SlowFleetController(
        delay=0.0, sessions=[], records=[])

    class _Config:
        router = RouterConfig(dispatch_budget_seconds=6.0, snapshot_budget_seconds=2.0)

    store, _queue, router = _router(tmp_path, controller, config=_Config())
    receipt = router.route_start("nothing can take this", title="t")

    evidence = store.get_task(receipt["task_id"]).routing_evidence
    assert "fleet_snapshot_seconds" in evidence
    assert evidence["fleet_snapshot_budget_seconds"] == pytest.approx(2.0)


def test_an_empty_listing_falls_back_to_the_last_good_snapshot(tmp_path):
    """One slow listing must not defer the entire queue.

    An empty fleet and an unreadable fleet are indistinguishable in the
    listing's shape, and treating the second as the first defers every task
    there is -- with nothing downstream to correct it."""
    controller = _SlowFleetController(
        delay=0.0,
        sessions=[{"name": "worker", "node_id": "local", "effective_input": True}],
        records=[FakeRecord(node_id="local", session_name="worker")])

    class _Config:
        router = RouterConfig(dispatch_budget_seconds=6.0, snapshot_budget_seconds=2.0,
                              stale_snapshot_max_age_seconds=60.0)

    _store, _queue, router = _router(tmp_path, controller, config=_Config())
    assert [c.session for c in router.candidates(refresh=True)] == ["worker"]

    controller._sessions = []  # every node timed out this pass
    recovered = router.candidates(refresh=True)
    assert [c.session for c in recovered] == ["worker"], (
        "a fleet read that told us nothing was treated as 'the fleet is empty'")


def test_a_snapshot_older_than_policy_is_not_evidence(tmp_path):
    controller = _SlowFleetController(
        delay=0.0,
        sessions=[{"name": "worker", "node_id": "local", "effective_input": True}],
        records=[FakeRecord(node_id="local", session_name="worker")])

    class _Config:
        router = RouterConfig(stale_snapshot_max_age_seconds=0.0)

    ticks = iter([0.0, 0.0, 100.0, 100.0, 100.0, 100.0])
    store = QueueStore(tmp_path / "queue.db")
    router = TaskRouter(store, controller=controller, queue=QueueService(store),
                        session_registry=controller.session_registry,
                        config=_Config(), clock=lambda: next(ticks))
    assert router.candidates(refresh=True)
    controller._sessions = []
    assert router.candidates(refresh=True) == []


def test_one_routing_decision_reads_the_lanes_once(tmp_path):
    """Load and claims are two views of the same full scan. Two reads of it
    was the most expensive purely-local thing a routing decision did."""
    controller = FakeController(
        sessions=[{"name": "worker", "node_id": "local", "effective_input": True}],
        records=[FakeRecord(node_id="local", session_name="worker")])
    store = QueueStore(tmp_path / "queue.db")
    calls: list[int] = []
    real = store.list_all_lanes

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    store.list_all_lanes = counted  # type: ignore[method-assign]
    router = TaskRouter(store, controller=controller, queue=QueueService(store),
                        session_registry=controller.session_registry)
    router.candidates(refresh=True)
    assert len(calls) == 1, f"the lane scan ran {len(calls)} times for one snapshot"
