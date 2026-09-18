"""RecoveryLoop -- the optional, off-by-default background loop that
automatically reconciles a node the moment it reconnects (task item 9:
"node-agent/controller khởi động lại phải tự reconcile theo policy").
Fast, deterministic tests with fake controller/registry/engine objects
-- no real network, no real thread for most of them; ONE test at the
bottom starts a real background thread to prove the full autonomous
lifecycle."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from terminal_mcp.node_models import NODE_OFFLINE, NODE_ONLINE
from terminal_mcp.recovery_loop import RecoveryLoop


class FakeRegistry:
    def __init__(self, transitions=None):
        self._transitions = transitions or []

    def sync_status_transitions(self):
        result = self._transitions
        self._transitions = []  # consumed -- a real sync_status_transitions call only reports NEW transitions
        return result


class FakeController:
    def __init__(self, nodes, transitions=None):
        self._nodes = nodes
        self.registry = FakeRegistry(transitions)

    def list_nodes(self):
        return self._nodes


class FakeEngine:
    def __init__(self):
        self.reconciled: list[str] = []

    def reconcile_node(self, node_id, *, requested_by=None):
        self.reconciled.append(node_id)
        return [{"node_id": node_id, "session": "x"}]


def _node(node_id, status):
    return SimpleNamespace(id=node_id, status=status)


def test_first_cycle_reconciles_every_currently_online_node(monkeypatch):
    controller = FakeController([_node("local", NODE_ONLINE), _node("m910", NODE_ONLINE)])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller)
    loop.run_one_cycle()
    assert sorted(engine.reconciled) == ["local", "m910"]


def test_offline_nodes_are_never_reconciled(monkeypatch):
    controller = FakeController([_node("local", NODE_ONLINE), _node("m910", NODE_OFFLINE)])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller)
    loop.run_one_cycle()
    assert engine.reconciled == ["local"]


def test_a_node_already_reconciled_once_is_not_reconciled_again_on_the_next_cycle_with_no_new_transition():
    controller = FakeController([_node("local", NODE_ONLINE)])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller)
    loop.run_one_cycle()
    loop.run_one_cycle()
    assert engine.reconciled == ["local"]  # only once, not twice


def test_a_real_reconnect_transition_triggers_a_fresh_reconcile_even_if_seen_before():
    controller = FakeController([_node("m910", NODE_ONLINE)])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller)
    loop.run_one_cycle()
    assert engine.reconciled == ["m910"]
    # Simulate a real OFFLINE -> ONLINE transition being reported on a
    # LATER cycle (a genuine reconnect after this loop already saw it
    # once before).
    controller.registry._transitions = [{"node_id": "m910", "from_status": NODE_OFFLINE, "to_status": NODE_ONLINE}]
    loop.run_one_cycle()
    assert engine.reconciled == ["m910", "m910"]


def test_one_node_engine_failure_never_stops_the_others():
    controller = FakeController([_node("local", NODE_ONLINE), _node("broken", NODE_ONLINE)])

    class FlakyEngine(FakeEngine):
        def reconcile_node(self, node_id, *, requested_by=None):
            if node_id == "broken":
                raise RuntimeError("simulated engine failure")
            return super().reconcile_node(node_id, requested_by=requested_by)

    engine = FlakyEngine()
    loop = RecoveryLoop(engine, controller)
    results = loop.run_one_cycle()
    broken = next(r for r in results if r.get("node_id") == "broken")
    assert broken["error"] == "ENGINE_ERROR"
    assert engine.reconciled == ["local"]  # local still got reconciled despite broken's failure


def test_a_list_nodes_failure_never_crashes_the_cycle():
    class BrokenController(FakeController):
        def list_nodes(self):
            raise RuntimeError("simulated controller failure")

    controller = BrokenController([])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller)
    results = loop.run_one_cycle()  # must not raise
    assert results == []
    assert loop.status()["last_cycle_at"] is not None


def test_status_reports_running_and_last_cycle():
    controller = FakeController([])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller)
    assert loop.status()["running"] is False
    loop.run_one_cycle()
    assert loop.status()["last_cycle_at"] is not None


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_real_background_thread_reconciles_automatically_with_zero_manual_ticks():
    controller = FakeController([_node("local", NODE_ONLINE)])
    engine = FakeEngine()
    loop = RecoveryLoop(engine, controller, poll_interval_seconds=0.05)
    loop.start()
    try:
        assert loop.is_alive() is True
        assert _wait_until(lambda: "local" in engine.reconciled, timeout=3.0)
    finally:
        loop.stop()
    assert loop.is_alive() is False
