"""Watches on other nodes' sessions must be watchable, and recoverable.

The production state this exists for, read straight out of the live
supervisor.db: ten watches, zero enabled.

    wtest        target_missing   iteration_count=1
    hp-work      target_missing   iteration_count=1
    hp1          target_missing   iteration_count=1
    hp2          target_missing   iteration_count=1
    hp3-work     target_missing   iteration_count=1
    win2         target_missing   iteration_count=1
    terminal-mcp-main  max_iterations_exceeded  223
    mcp-work           max_iterations_exceeded  121
    gatefix2-work      max_iterations_exceeded  20
    mesflow-dell       manual_unwatch   (a person's decision)

The six `target_missing` rows are sessions that were alive the whole time
on the hp and Windows nodes. Every status call in supervisor.py went to the
LOCAL TerminalService, so each was declared missing on its FIRST poll and
disabled permanently -- and `stalled_count` counted none of them, so the
status surface said 3 while 9 watches were actually not running.

These tests use a fake fleet resolver rather than real remote nodes: the
defect is entirely in which resolver gets asked, and a simulated node can be
made unreachable on demand, which a real one cannot.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import scheduler_health as sh
from terminal_mcp.config import AppConfig, PermissionsConfig, SessionAccessConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore, watch_key


def _service(tmp_path, **overrides):
    config = AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*", "hp*", "win*", "wtest"), 50, 20,
                       supervisor=SupervisorConfig(**overrides),
                       session_access=SessionAccessConfig(default_read=True, default_input=False))
    return SupervisorService(TerminalService(config), SupervisorStore(tmp_path / "supervisor.db"))


def _age(store, key, seconds):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store._connection() as connection:
        connection.execute("UPDATE watches SET updated_at = ? WHERE watch_key = ?", (stamp, key))


class FakeFleet:
    """A controller stand-in. `live` is what the fleet can see right now;
    `down` makes a node answer NODE_UNREACHABLE the way the real one does."""

    def __init__(self, live=(), down=()):
        self.live = {name: {"state": "RUNNING", "exists": True, "last_output": f"out:{name}"}
                     for name in live}
        self.down = set(down)
        self.calls: list[str] = []

    def terminal_status(self, target):
        self.calls.append(target)
        node = target.partition("/")[0] if "/" in target else ""
        if node and node in self.down:
            return {"error": "NODE_UNREACHABLE", "node_id": node,
                    "detail": "no client configured for this node"}
        if target in self.live:
            return dict(self.live[target])
        return {"error": "SESSION_NOT_FOUND", "session": target}

    def sessions(self):
        return sorted(self.live)


# == the routing defect itself ===========================================

def test_a_remote_session_is_polled_on_its_own_node_not_local_tmux(tmp_path):
    """The root cause. Without a fleet resolver the supervisor asks local tmux
    about `hp/hp1`, which has never heard of it."""
    service = _service(tmp_path)
    fleet = FakeFleet(live=["hp/hp1"])
    service.fleet_status = fleet.terminal_status

    service.watch(session="hp/hp1")
    service.run_once()

    assert fleet.calls, "the fleet resolver was never consulted"
    row = service.store.get_watch(watch_key("session", "hp/hp1"))
    assert row["enabled"] == 1, f"a live remote session was disabled: {row['disabled_reason']}"
    assert row["state"] == "RUNNING"


def test_without_a_fleet_resolver_behaviour_is_exactly_as_before(tmp_path):
    """Requirement: local-only deployments and every existing test are
    unaffected. fleet_status=None must mean the old local path."""
    service = _service(tmp_path)
    assert service.fleet_status is None
    service.watch(session="not-a-real-local-session")
    service.run_once()
    row = service.store.get_watch(watch_key("session", "not-a-real-local-session"))
    assert row["enabled"] == 0
    assert row["disabled_reason"] == "target_missing"


def test_a_binding_watch_is_never_routed_to_a_remote_node(tmp_path):
    """Bindings are local-node-scoped in this phase. Routing one to a remote
    node would resolve a binding name that node has never heard of."""
    service = _service(tmp_path)
    fleet = FakeFleet(live=["hp/hp1"])
    service.fleet_status = fleet.terminal_status
    service.store.upsert_watch("binding", "some-binding", source="manual")
    service.run_once()
    assert fleet.calls == [], "a binding watch was sent to the fleet resolver"


# == the six production rows, restored ===================================

REMOTE_ROWS = ["hp/wtest", "hp/hp-work", "hp/hp1", "hp/hp2", "hp/hp3-work", "dell-5530/win2"]


def test_the_six_target_missing_watches_come_back_when_their_nodes_do(tmp_path):
    """The whole point, at production scale: six watches disabled while their
    nodes were unreachable, all six restored once the fleet can see them --
    and NOT restored while it still cannot."""
    service = _service(tmp_path)
    fleet = FakeFleet(live=[], down=["hp", "dell-5530"])
    service.fleet_status = fleet.terminal_status

    for target in REMOTE_ROWS:
        service.watch(session=target)
    service.run_once()

    status = service.status()
    assert status["enabled_watch_count"] == 0, "precondition: the nodes are down"
    assert status["recoverable_disabled_count"] == 6
    assert set(status["disabled_reasons"].values()) == {"node_unreachable"}

    # Still down: reconciliation must not re-enable into a void.
    for target in REMOTE_ROWS:
        _age(service.store, watch_key("session", target), 600)
    assert service.reconcile_watches()["restored"] == []

    # The nodes come back.
    fleet.down.clear()
    for target in REMOTE_ROWS:
        fleet.live[target] = {"state": "RUNNING", "exists": True, "last_output": f"out:{target}"}
        _age(service.store, watch_key("session", target), 600)

    restored = service.reconcile_watches()["restored"]
    assert sorted(item["target"] for item in restored) == sorted(REMOTE_ROWS)
    assert service.status()["enabled_watch_count"] == 6


def test_a_manual_unwatch_is_never_resurrected_by_any_of_this(tmp_path):
    """`mesflow-dell` in production. A person turned it off; nothing here may
    turn it back on, however alive the target is."""
    service = _service(tmp_path)
    fleet = FakeFleet(live=["hp/mesflow-dell"])
    service.fleet_status = fleet.terminal_status
    service.watch(session="hp/mesflow-dell")
    key = watch_key("session", "hp/mesflow-dell")
    service.unwatch(session="hp/mesflow-dell")
    assert service.store.get_watch(key)["disabled_reason"] == "manual_unwatch"

    _age(service.store, key, 10_000)
    for _ in range(3):
        assert service.reconcile_watches()["restored"] == []
    row = service.store.get_watch(key)
    assert row["enabled"] == 0 and row["disabled_reason"] == "manual_unwatch"
    assert service.status()["intentionally_excluded_count"] == 1


def test_the_full_production_mix_restores_by_policy_not_all_or_nothing(tmp_path):
    """All ten rows as they actually are: nine recoverable, one deliberate.
    The requirement is that policy decides each one, not that they share a
    fate -- 'all ten enabled' would be as wrong as 'all ten disabled'."""
    service = _service(tmp_path)
    live = REMOTE_ROWS + ["terminal-mcp-main", "mcp-work", "gatefix2-work", "hp/mesflow-dell"]
    fleet = FakeFleet(live=live)
    service.fleet_status = fleet.terminal_status

    for target in REMOTE_ROWS:
        service.watch(session=target)
        service.store.set_enabled(watch_key("session", target), False,
                                  disabled_reason="node_unreachable")
    for target, count in (("terminal-mcp-main", 223), ("mcp-work", 121), ("gatefix2-work", 20)):
        service.watch(session=target)
        key = watch_key("session", target)
        service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
        with service.store._connection() as connection:
            connection.execute("UPDATE watches SET iteration_count = ? WHERE watch_key = ?", (count, key))
    service.watch(session="hp/mesflow-dell")
    service.unwatch(session="hp/mesflow-dell")

    before = service.status()
    assert before["watch_count"] == 10
    assert before["enabled_watch_count"] == 0
    assert before["recoverable_disabled_count"] == 9
    assert before["intentionally_excluded_count"] == 1

    for row in service.store.list_watches():
        _age(service.store, row["watch_key"], 600)
    service.reconcile_watches()

    after = service.status()
    assert after["enabled_watch_count"] == 9, "nine recoverable watches should be back"
    assert after["intentionally_excluded_count"] == 1, "the manual one must stay off"
    assert service.store.get_watch(watch_key("session", "hp/mesflow-dell"))["enabled"] == 0

    # A re-enabled watch must not instantly re-disable on its poll ceiling.
    for target in ("terminal-mcp-main", "mcp-work", "gatefix2-work"):
        assert service.store.get_watch(watch_key("session", target))["iteration_count"] == 0


# == error classification is what makes recovery possible ================

@pytest.mark.parametrize("error,expected", [
    ("NODE_UNREACHABLE", "node_unreachable"),
    ("SESSION_NOT_FOUND", "target_missing"),
    ("NODE_NOT_FOUND", "node_not_found"),
    ("AMBIGUOUS_SESSION", "ambiguous_target"),
])
def test_each_fleet_error_records_its_own_disable_reason(tmp_path, error, expected):
    """These all used to collapse into access_denied_or_error, so an operator
    could not tell a node outage from a revoked grant."""
    service = _service(tmp_path)
    service.fleet_status = lambda target: {"error": error}
    service.watch(session="hp/thing")
    service.run_once()
    row = service.store.get_watch(watch_key("session", "hp/thing"))
    assert row["disabled_reason"] == expected
    assert expected in sh.RECOVERABLE_DISABLE_REASONS


def test_an_unknown_status_error_is_still_recoverable(tmp_path):
    """A fleet that grows a new error code must not permanently blind the
    supervisor to a session."""
    service = _service(tmp_path)
    service.fleet_status = lambda target: {"error": "SOME_FUTURE_ERROR_CODE"}
    service.watch(session="hp/thing")
    service.run_once()
    row = service.store.get_watch(watch_key("session", "hp/thing"))
    assert row["disabled_reason"] in sh.RECOVERABLE_DISABLE_REASONS


def test_a_remote_timeout_mid_poll_does_not_abort_the_other_watches(tmp_path):
    """One node hanging must not starve every other watch in the pass."""
    service = _service(tmp_path)
    calls = []

    def _status(target):
        calls.append(target)
        if target == "hp/hangs":
            raise TimeoutError("node did not answer")
        return {"state": "RUNNING", "exists": True, "last_output": "fine"}

    service.fleet_status = _status
    for target in ("hp/hangs", "hp/ok1", "hp/ok2"):
        service.watch(session=target)
    service.run_once()

    assert {"hp/ok1", "hp/ok2"}.issubset(set(calls)), "healthy watches were skipped"
    for target in ("hp/ok1", "hp/ok2"):
        assert service.store.get_watch(watch_key("session", target))["enabled"] == 1


# == status truthfulness =================================================

def test_status_counts_every_disabled_watch_not_only_two_reasons(tmp_path):
    """The old `stalled_count` only recognised same_failure_limit_exceeded and
    max_iterations_exceeded, so six node_unreachable/target_missing watches were
    invisible: the surface reported 3 while 9 were not running."""
    service = _service(tmp_path)
    service.fleet_status = FakeFleet().terminal_status
    for target, reason in (("hp/a", "node_unreachable"), ("hp/b", "target_missing"),
                           ("hp/c", "max_iterations_exceeded"), ("hp/d", "manual_unwatch")):
        service.watch(session=target)
        service.store.set_enabled(watch_key("session", target), False, disabled_reason=reason)

    status = service.status()
    assert status["watch_count"] == 4
    assert status["enabled_watch_count"] == 0
    assert status["disabled_watch_count"] == 4
    assert status["recoverable_disabled_count"] == 3
    assert status["intentionally_excluded_count"] == 1
    assert (status["recoverable_disabled_count"] + status["intentionally_excluded_count"]
            == status["disabled_watch_count"]), "a disabled watch fell into neither bucket"


# == config-pattern seeding across the fleet =============================

def test_config_patterns_seed_watches_for_remote_sessions(tmp_path):
    """A config asking to watch "hp*" used to do nothing on a controller whose
    hp sessions all live on the hp node -- with no error to explain it."""
    service = _service(tmp_path, watched_session_patterns=("hp/*",))
    fleet = FakeFleet(live=["hp/hp1", "hp/hp2"])
    service.fleet_status = fleet.terminal_status
    service.fleet_sessions = fleet.sessions

    service._sync_config_watches()

    targets = {row["target"] for row in service.store.list_watches()}
    assert targets == {"hp/hp1", "hp/hp2"}
    assert all(row["source"] == "config_pattern" for row in service.store.list_watches())


def test_a_failing_fleet_listing_does_not_lose_local_seeding(tmp_path, tmux_session_factory):
    """One unreachable node must not cost the local sessions that were listed
    successfully in the same pass."""
    session = tmux_session_factory("test-seed-local", "bash -lc 'sleep 60'")
    service = _service(tmp_path, watched_session_patterns=("test-seed-*",))

    def _boom():
        raise RuntimeError("every node is unreachable")

    service.fleet_sessions = _boom
    service._sync_config_watches()

    assert {row["target"] for row in service.store.list_watches()} == {session}


def test_seeding_the_same_session_twice_does_not_duplicate_it(tmp_path):
    """A fleet listing that includes the local node returns names the local
    list already produced."""
    service = _service(tmp_path, watched_session_patterns=("hp/*",))
    service.fleet_sessions = lambda: ["hp/hp1", "hp/hp1", "hp/hp1"]
    service.fleet_status = FakeFleet(live=["hp/hp1"]).terminal_status
    service._sync_config_watches()
    assert len(service.store.list_watches()) == 1


# == rename across the fleet =============================================

def test_renaming_a_remote_session_re_keys_its_qualified_watch(tmp_path):
    """A fleet watch is stored as "node/session" while the rename arrives bare,
    from the node that performed it. An exact-key lookup missed it, so the watch
    kept pointing at a name that no longer existed and was disabled
    target_missing for a session that was alive under a new name."""
    service = _service(tmp_path)
    service.fleet_status = FakeFleet(live=["hp/old-name"]).terminal_status
    service.watch(session="hp/old-name")

    assert service.rename_session("old-name", "new-name") == 1

    assert service.store.get_watch(watch_key("session", "hp/old-name")) is None
    moved = service.store.get_watch(watch_key("session", "hp/new-name"))
    assert moved is not None and moved["target"] == "hp/new-name"


def test_a_qualified_rename_still_works_exactly_as_before(tmp_path):
    service = _service(tmp_path)
    service.watch(session="hp/old")
    assert service.rename_session("hp/old", "hp/new") == 1
    assert service.store.get_watch(watch_key("session", "hp/new")) is not None


def test_an_ambiguous_bare_rename_re_keys_nothing(tmp_path):
    """Two nodes holding the same bare name is the ambiguity the controller
    refuses to guess about. Re-keying the wrong node's watch would point it at a
    session on a machine that renamed nothing."""
    service = _service(tmp_path)
    service.watch(session="hp/dup")
    service.watch(session="dell/dup")
    assert service.rename_session("dup", "renamed") == 0
    assert service.store.get_watch(watch_key("session", "hp/dup")) is not None
    assert service.store.get_watch(watch_key("session", "dell/dup")) is not None


def test_renaming_leaves_a_local_watch_alone_when_no_qualified_row_matches(tmp_path):
    service = _service(tmp_path)
    service.watch(session="plain-name")
    assert service.rename_session("plain-name", "other-name") == 1
    assert service.store.get_watch(watch_key("session", "other-name")) is not None


# == the regression that fixing the routing nearly caused ================

def test_a_local_watch_survives_a_controller_that_cannot_resolve_it(tmp_path, tmux_session_factory):
    """Wiring fleet routing in unconditionally broke every LOCAL watch, and this
    is the test that caught it.

    Routing a bare name through the controller makes it depend on the local node
    being registered and ONLINE. A stale local heartbeat answers
    SESSION_NOT_FOUND for a session running right here -- so the fix for six
    remote watches would have disabled the three local ones that still worked.
    Local is asked first for exactly this reason."""
    session = tmux_session_factory("test-local-first", "bash -lc 'sleep 60'")
    service = _service(tmp_path)
    fleet_calls = []

    def _fleet_says_no(target):
        fleet_calls.append(target)
        return {"error": "SESSION_NOT_FOUND", "session": target}

    service.fleet_status = _fleet_says_no
    service.watch(session=session)
    service.run_once()

    row = service.store.get_watch(watch_key("session", session))
    assert row["enabled"] == 1, f"a live LOCAL session was disabled: {row['disabled_reason']}"
    assert fleet_calls == [], "local was answerable; the fleet should not have been asked at all"


def test_the_fleet_is_asked_only_when_local_cannot_answer(tmp_path):
    """The other half: a bare name that is NOT local must still reach the fleet,
    or the six production rows (all bare) stay broken."""
    service = _service(tmp_path)
    fleet = FakeFleet(live=["hp1"])
    service.fleet_status = fleet.terminal_status

    service.watch(session="hp1")
    service.run_once()

    assert fleet.calls == ["hp1"], "a non-local bare name never reached the fleet"
    row = service.store.get_watch(watch_key("session", "hp1"))
    assert row["enabled"] == 1 and row["state"] == "RUNNING"


def test_the_existing_production_rows_need_no_re_keying(tmp_path):
    """The six rows in production are BARE names (hp1, hp2, hp3-work, wtest,
    win2, hp-work) because a qualified watch was refused outright. They must
    start working through fleet resolution as they are -- a fix that required
    re-keying ten live rows would be a migration, not a fix."""
    service = _service(tmp_path)
    bare = ["hp1", "hp2", "hp3-work", "wtest", "win2", "hp-work"]
    fleet = FakeFleet(live=bare)
    service.fleet_status = fleet.terminal_status

    for name in bare:
        service.watch(session=name)
    service.run_once()

    for name in bare:
        row = service.store.get_watch(watch_key("session", name))
        assert row["target"] == name, "the target was rewritten"
        assert row["enabled"] == 1, f"{name} still disabled: {row['disabled_reason']}"


def test_a_qualified_watch_can_now_be_created_at_all(tmp_path):
    """`watch(session="hp/hp1")` used to return ACCESS_DENIED: the read gate was
    asked about the qualified name, and grants are keyed by session name. So the
    fleet-correct way to name a watch was the one way that could not be used."""
    service = _service(tmp_path)
    service.fleet_status = FakeFleet(live=["hp/hp1"]).terminal_status
    result = service.watch(session="hp/hp1")
    assert "error" not in result, result
    assert result["target"] == "hp/hp1"
