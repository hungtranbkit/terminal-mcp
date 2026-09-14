"""Phase 1 <-> Phase 2 integration invariants.

WHY THESE ARE CHARACTERIZATION TESTS AGAINST REAL MODULES, NOT FAKES.

Phase 2's watch handling rests on three assumptions about code it does not own:
that `SupervisorStore.upsert_watch` re-enables and re-pins an existing row, that
`SupervisorService.watch` accepts a `source`, and that
`PMService.route_task(mode="AUTO")` is the assignment path. Every one of those is
a thing Phase 1 could legitimately change.

The Phase-1 branch was not reachable from this host when these were written (see
the lane report: not on origin, not in any local ref; the controller at
100.117.214.87 runs commit e853ab39b8912a7c71ad0b3343f9ed8918adff4b, which is
advertised by neither). So rather than guess at Phase 1's shape, these tests pin
the assumptions themselves against the REAL modules. If Phase 1 changes any of
them, these fail loudly and name what broke -- which is the most useful thing a
test can do about an integration you cannot yet see.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import pm_service as pm_service_module
from terminal_mcp import supervisor as supervisor_module
from terminal_mcp.pm_store import SOURCE_AUTO, PMStore
from terminal_mcp.supervisor import SupervisorStore
from terminal_mcp.worker_discovery import (
    WATCH_CREATED,
    WATCH_EXISTS,
    WorkerDiscoveryPolicy,
    WorkerDiscoveryService,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
ON = WorkerDiscoveryPolicy(enabled=True)


def session_row(**overrides):
    row = {
        "node_id": "local", "session_name": "worker-a", "agent_type": "claude",
        "status": "ACTIVE", "input_granted": 1, "read_granted": 1,
        "last_seen_at": (NOW - timedelta(seconds=30)).isoformat(),
        "repo_root": "/repo/widget", "backend_type": "tmux", "recovery_state": None,
    }
    row.update(overrides)
    return row


class FakeRegistry:
    def __init__(self, records):
        self.records = list(records)

    def list(self, *, statuses=None, **_kwargs):
        if statuses is None:
            return list(self.records)
        return [r for r in self.records if r.get("status") in statuses]


class RealBackedSupervisor:
    """A thin service shim over the REAL SupervisorStore, so watch behaviour is
    the production one. Only `_read_authorized`-style access control is stubbed
    (it needs a live tmux/whitelist); everything about watch rows is real."""

    def __init__(self, store: SupervisorStore, *, denied=()):
        self.store = store
        self.denied = set(denied)
        self.set_enabled_calls = []

    def list_watches(self):
        return {"watches": self.store.list_watches()}

    def watch(self, session=None, binding=None, required_verifiers=None, source="manual"):
        if session in self.denied:
            return {"error": "ACCESS_DENIED", "session": session}
        row, created = self.store.upsert_watch("session", session, source=source)
        return {"target": session, "created": created}

    def set_enabled(self, key, enabled, *, disabled_reason=None):
        self.set_enabled_calls.append((key, enabled, disabled_reason))
        return self.store.set_enabled(key, enabled, disabled_reason=disabled_reason)


@pytest.fixture
def pm_store(tmp_path):
    return PMStore(tmp_path / "pm.db")


@pytest.fixture
def sup_store(tmp_path):
    return SupervisorStore(tmp_path / "supervisor.db")


# -- the assumptions Phase 2 makes about code it does not own -----------------

def test_upsert_watch_really_does_re_enable_an_existing_row(sup_store):
    """The characterization that justifies the create-only rule. If this ever
    stops being true, the rule can be relaxed -- but not before."""
    sup_store.upsert_watch("session", "w1", source="manual")
    key = supervisor_module.watch_key("session", "w1")
    sup_store.set_enabled(key, False, disabled_reason="same_failure_limit_exceeded")
    assert sup_store.get_watch(key)["enabled"] == 0

    sup_store.upsert_watch("session", "w1", source="manual")  # a blind re-watch

    row = sup_store.get_watch(key)
    assert row["enabled"] == 1, "upsert_watch re-enables -- create-only is required"
    assert row["disabled_reason"] is None


def test_upsert_watch_mints_a_fresh_completion_nonce_on_re_watch(sup_store):
    """The second half of why blind re-watching is unsafe: it invalidates the
    outstanding completion nonce and bumps the attempt counter."""
    row, _ = sup_store.upsert_watch("session", "w1", source="manual")
    first_nonce, first_attempt = row["completion_nonce"], row["completion_attempt"]

    again, created = sup_store.upsert_watch("session", "w1", source="manual")

    assert created is False
    assert again["completion_nonce"] != first_nonce
    assert again["completion_attempt"] > first_attempt


def test_supervisor_watch_accepts_the_source_kwarg():
    """Phase 2 passes source="auto-discovery"; a Phase-1 signature change that
    drops it must fail here rather than at runtime on the controller."""
    signature = inspect.signature(supervisor_module.SupervisorService.watch)
    assert "source" in signature.parameters
    assert signature.parameters["source"].default == "manual"


def test_route_task_auto_mode_is_still_the_assignment_contract():
    """Phase 2's dispatch() calls route_task(task_id, mode="AUTO"). Pin both the
    constant and the signature."""
    assert pm_service_module.MODE_AUTO == "AUTO"
    assert "AUTO" in pm_service_module.VALID_MODES
    signature = inspect.signature(pm_service_module.PMService.route_task)
    assert "mode" in signature.parameters
    source = inspect.getsource(pm_service_module.PMService.route_task)
    assert "assign_task" in source, "AUTO mode must still assign through QueueService"


# -- the invariant that matters: neither phase re-enables the other's disable --

def test_discovery_never_re_enables_a_watch_disabled_by_reconciliation(pm_store, sup_store):
    """Whatever Phase 1 disables -- for any reason -- Phase 2 leaves disabled.
    Asserted against the real store, over repeated passes."""
    supervisor = RealBackedSupervisor(sup_store)
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), pm_store,
                                     supervisor=supervisor, policy=ON)
    service.reconcile(now=NOW)  # creates the watch
    key = supervisor_module.watch_key("session", "worker-a")

    # Phase 1 (or the supervisor's own poll) disables it for each real reason.
    for reason in ("same_failure_limit_exceeded", "max_iterations_exceeded",
                   "target_missing", "access_denied_or_error", "manual_unwatch"):
        sup_store.set_enabled(key, False, disabled_reason=reason)
        for _ in range(3):
            report = service.reconcile(now=NOW)
        row = sup_store.get_watch(key)
        assert row["enabled"] == 0, f"discovery re-enabled a watch disabled for {reason}"
        assert row["disabled_reason"] == reason
        assert report["watches"] == {WATCH_EXISTS: ["worker-a"]}

    assert supervisor.set_enabled_calls == [], "discovery must never touch enablement at all"


def test_discovery_creates_exactly_one_watch_row_however_many_passes(pm_store, sup_store):
    supervisor = RealBackedSupervisor(sup_store)
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), pm_store,
                                     supervisor=supervisor, policy=ON)
    for _ in range(10):
        service.reconcile(now=NOW)
    assert len(sup_store.list_watches()) == 1


def test_restart_replay_creates_no_second_watch_and_no_second_profile(pm_store, sup_store):
    """Restart = fresh service objects over the same persisted stores."""
    registry = FakeRegistry([session_row(), session_row(session_name="worker-b")])
    supervisor = RealBackedSupervisor(sup_store)

    WorkerDiscoveryService(registry, pm_store, supervisor=supervisor, policy=ON).reconcile(now=NOW)
    nonces = {w["watch_key"]: w["completion_nonce"] for w in sup_store.list_watches()}

    replayed = WorkerDiscoveryService(registry, pm_store, supervisor=supervisor,
                                      policy=ON).reconcile(now=NOW)

    assert len(sup_store.list_watches()) == 2
    assert len(pm_store.list_capabilities()) == 2
    assert WATCH_CREATED not in replayed["watches"]
    # The nonce survives a restart -- an in-flight completion token stays valid.
    assert {w["watch_key"]: w["completion_nonce"] for w in sup_store.list_watches()} == nonces


# -- node offline/online transitions ------------------------------------------

class FlippableController:
    def __init__(self, online=True):
        self.online = online

    def list_nodes(self):
        class _Node:
            pass
        node = _Node()
        node.id = "local"
        node.status = "online" if self.online else "offline"
        node.platform = "linux"
        node.runtime_tools = ()
        return [node]


def test_offline_node_declares_nothing_then_online_declares_once(pm_store, sup_store):
    controller = FlippableController(online=False)
    supervisor = RealBackedSupervisor(sup_store)
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), pm_store,
                                     controller=controller, supervisor=supervisor, policy=ON)

    offline = service.reconcile(now=NOW)
    assert offline["created"] == []
    assert offline["skipped_counts"] == {"NODE_OFFLINE": 1}
    assert sup_store.list_watches() == []

    controller.online = True
    online = service.reconcile(now=NOW)
    assert online["created"] == ["local/worker-a"]
    assert online["watches"] == {WATCH_CREATED: ["worker-a"]}
    assert len(sup_store.list_watches()) == 1


def test_a_node_going_offline_leaves_the_existing_watch_and_profile_untouched(pm_store, sup_store):
    """Going offline is not a reason to delete or disable -- that belongs to the
    supervisor's own poll (target_missing) and to Phase 1, not to discovery."""
    controller = FlippableController(online=True)
    supervisor = RealBackedSupervisor(sup_store)
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), pm_store,
                                     controller=controller, supervisor=supervisor, policy=ON)
    service.reconcile(now=NOW)

    controller.online = False
    report = service.reconcile(now=NOW)

    assert report["skipped_counts"] == {"NODE_OFFLINE": 1}
    assert len(sup_store.list_watches()) == 1
    assert sup_store.list_watches()[0]["enabled"] == 1   # not disabled by discovery
    assert len(pm_store.list_capabilities()) == 1        # profile not deleted
    assert supervisor.set_enabled_calls == []


def test_flapping_node_never_duplicates_or_re_pins(pm_store, sup_store):
    controller = FlippableController(online=True)
    supervisor = RealBackedSupervisor(sup_store)
    service = WorkerDiscoveryService(FakeRegistry([session_row()]), pm_store,
                                     controller=controller, supervisor=supervisor, policy=ON)
    service.reconcile(now=NOW)
    pinned = sup_store.list_watches()[0]["completion_nonce"]

    for _ in range(5):
        controller.online = False
        service.reconcile(now=NOW)
        controller.online = True
        service.reconcile(now=NOW)

    watches = sup_store.list_watches()
    assert len(watches) == 1
    assert watches[0]["completion_nonce"] == pinned
    assert len(pm_store.list_capabilities()) == 1


# -- the latent cross-phase hazard, pinned so a change is visible -------------

def test_config_pattern_sync_re_enables_disabled_watches(sup_store, monkeypatch):
    """CHARACTERIZATION, not an endorsement.

    `SupervisorService._sync_config_watches` upserts a watch for every live
    session matching `config.watched_session_patterns`, on every poll -- and
    upsert_watch re-enables. So if those patterns are ever made non-empty, the
    supervisor re-enables watches that reconciliation just disabled, every
    cycle, forever.

    It is dormant today (`watched_session_patterns: []` in config.yaml and
    config.example.yaml) and it is NOT Phase 2's to change -- Phase 2 creates
    watches directly and needs no patterns. This test exists so that turning the
    patterns on, or changing this behaviour in Phase 1, is a visible event
    rather than a silent fight between two loops.
    """
    key = supervisor_module.watch_key("session", "w1")
    sup_store.upsert_watch("session", "w1", source="config_pattern")
    sup_store.set_enabled(key, False, disabled_reason="same_failure_limit_exceeded")

    # exactly what _sync_config_watches does for a matching live session
    sup_store.upsert_watch("session", "w1", source="config_pattern")

    row = sup_store.get_watch(key)
    assert row["enabled"] == 1
    assert row["disabled_reason"] is None
    # If this assertion ever fails, _sync_config_watches became safe and the
    # note in worker_discovery.py's WATCH_* block should be revisited.
