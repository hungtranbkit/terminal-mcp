"""RecoveryEngine -- the Auto Recovery reconciliation core (task: "Auto
Recovery cho session sau reboot/crash/node-agent restart"). Real
SessionRegistryStore/PaneLeaseStore (tmp_path-scoped SQLite, fast, real
locking semantics) + a fake controller (the ONE external boundary this
module talks to -- terminal_registry_reopen/registry_list -- exercised
for real, end-to-end, against a real disposable tmux session in test_
recovery_engine_live.py; this file is the fast, deterministic policy/
lock/state-machine unit layer)."""
from __future__ import annotations

import pytest

from terminal_mcp.config import AutoRecoveryConfig
from terminal_mcp.core import (
    RECOVERY_STATE_BLOCKED, RECOVERY_STATE_DEGRADED, RECOVERY_STATE_RESUMED_OK,
)
from terminal_mcp.lease import PaneLeaseStore
from terminal_mcp.recovery_engine import RecoveryEngine
from terminal_mcp.session_registry import SessionRegistryStore


class FakeController:
    """Records every terminal_registry_reopen call it receives and
    returns a caller-scripted result -- never touches a real process."""

    def __init__(self) -> None:
        self.reopen_calls: list[str] = []
        self.reopen_result: dict = {"session": "s", "recreated_from_registry": True}
        self.registry_rows: dict[str, list[dict]] = {}

    def terminal_registry_reopen(self, qualified_name: str, *, requested_by=None) -> dict:
        self.reopen_calls.append(qualified_name)
        return dict(self.reopen_result)

    def registry_list(self, node_id: str, *, recoverable_only: bool = False) -> dict:
        return {"records": self.registry_rows.get(node_id, [])}


@pytest.fixture
def registry(tmp_path):
    return SessionRegistryStore(tmp_path / "registry.db")


@pytest.fixture
def lease_store(tmp_path):
    return PaneLeaseStore(tmp_path / "leases.db")


@pytest.fixture
def controller():
    return FakeController()


def _make_missing_record(registry, node_id="dell-5530", name="wtest", *, conversation_id=None):
    registry.upsert_seen(node_id, name, agent_type="claude", cwd="C:\\Dev\\proj", conversation_id=conversation_id)
    registry.mark_missing(node_id, set())  # nothing "seen" this pass -> the just-created row goes MISSING
    return registry.get(node_id, name)


def _engine(registry, controller, lease_store, config=None):
    return RecoveryEngine(registry, controller, lease_store, config or AutoRecoveryConfig(enabled=True))


# -- policy gate --------------------------------------------------------

def test_recover_session_blocked_when_globally_disabled_and_no_override(registry, controller, lease_store):
    _make_missing_record(registry)
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=False))
    result = engine.recover_session("dell-5530", "wtest")
    assert result["error"] == "RECOVERY_BLOCKED"
    assert controller.reopen_calls == []
    record = registry.get("dell-5530", "wtest")
    assert record.recovery_state == RECOVERY_STATE_BLOCKED


def test_per_session_override_enables_even_when_global_disabled(registry, controller, lease_store):
    _make_missing_record(registry)
    registry.set_auto_recovery_enabled("dell-5530", "wtest", True)
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=False))
    result = engine.recover_session("dell-5530", "wtest")
    assert "error" not in result
    assert controller.reopen_calls == ["dell-5530/wtest"]


def test_per_session_override_disables_even_when_global_enabled(registry, controller, lease_store):
    _make_missing_record(registry)
    registry.set_auto_recovery_enabled("dell-5530", "wtest", False)
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=True))
    result = engine.recover_session("dell-5530", "wtest")
    assert result["error"] == "RECOVERY_BLOCKED"
    assert controller.reopen_calls == []


def test_force_bypasses_the_policy_gate(registry, controller, lease_store):
    _make_missing_record(registry)
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=False))
    result = engine.recover_session("dell-5530", "wtest", force=True)
    assert "error" not in result
    assert controller.reopen_calls == ["dell-5530/wtest"]


# -- not-recoverable / missing record ------------------------------------

def test_no_registry_record_at_all(registry, controller, lease_store):
    engine = _engine(registry, controller, lease_store)
    result = engine.recover_session("dell-5530", "no-such-session")
    assert result["error"] == "REGISTRY_RECORD_NOT_FOUND"


def test_a_currently_active_session_is_not_recoverable(registry, controller, lease_store):
    registry.upsert_seen("dell-5530", "wtest", agent_type="claude", cwd="C:\\Dev\\proj")  # stays ACTIVE
    engine = _engine(registry, controller, lease_store)
    result = engine.recover_session("dell-5530", "wtest")
    assert result["error"] == "NOT_RECOVERABLE"
    assert controller.reopen_calls == []


# -- max_attempts ---------------------------------------------------------

def test_max_attempts_blocks_further_automatic_recovery(registry, controller, lease_store):
    _make_missing_record(registry)
    for _ in range(3):
        registry.begin_recovery_attempt("dell-5530", "wtest")
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=True, max_attempts=3))
    result = engine.recover_session("dell-5530", "wtest")
    assert result["error"] == "RECOVERY_BLOCKED"
    assert "max_attempts" in result["reason"]
    assert controller.reopen_calls == []


def test_force_bypasses_max_attempts_too(registry, controller, lease_store):
    _make_missing_record(registry)
    for _ in range(5):
        registry.begin_recovery_attempt("dell-5530", "wtest")
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=True, max_attempts=3))
    result = engine.recover_session("dell-5530", "wtest", force=True)
    assert "error" not in result
    assert controller.reopen_calls == ["dell-5530/wtest"]


# -- success paths: RESUMED_OK vs DEGRADED --------------------------------

def test_a_resumable_session_success_reports_resumed_ok(registry, controller, lease_store):
    _make_missing_record(registry, conversation_id="11111111-1111-1111-1111-111111111111")
    controller.reopen_result = {"session": "wtest", "recreated_from_registry": True,
                                "resume_verified": True, "resume_detail": "ok"}
    engine = _engine(registry, controller, lease_store)
    result = engine.recover_session("dell-5530", "wtest")
    assert result["recovery_state"] == RECOVERY_STATE_RESUMED_OK
    record = registry.get("dell-5530", "wtest")
    assert record.recovery_attempts == 0  # reset on success


def test_a_non_resumable_session_success_reports_degraded_not_resumed_ok(registry, controller, lease_store):
    _make_missing_record(registry, conversation_id=None)  # no conversation_id -> never resumable
    controller.reopen_result = {"session": "wtest", "recreated_from_registry": True}
    engine = _engine(registry, controller, lease_store)
    result = engine.recover_session("dell-5530", "wtest")
    assert result["recovery_state"] == RECOVERY_STATE_DEGRADED
    record = registry.get("dell-5530", "wtest")
    assert record.recovery_state == RECOVERY_STATE_DEGRADED
    assert "no conversation_id" in record.recovery_detail


def test_reopen_failure_marks_blocked_with_the_real_reason(registry, controller, lease_store):
    _make_missing_record(registry, conversation_id="11111111-1111-1111-1111-111111111111")
    controller.reopen_result = {"error": "RECOVERY_FAILED", "recovery_detail": "No conversation found"}
    engine = _engine(registry, controller, lease_store)
    result = engine.recover_session("dell-5530", "wtest")
    assert result["error"] == "RECOVERY_FAILED"
    record = registry.get("dell-5530", "wtest")
    assert record.recovery_state == RECOVERY_STATE_BLOCKED
    assert "No conversation found" in record.recovery_detail


# -- exactly-once (item 5) -------------------------------------------------

def test_a_held_lock_refuses_a_second_concurrent_attempt(registry, controller, lease_store):
    _make_missing_record(registry)
    lease_store.acquire("recovery:dell-5530/wtest", "someone-else", ttl_seconds=60)
    engine = _engine(registry, controller, lease_store)
    result = engine.recover_session("dell-5530", "wtest")
    assert result["error"] == "RECOVERY_IN_PROGRESS"
    assert controller.reopen_calls == []


def test_the_lock_is_released_after_a_successful_attempt_so_a_later_one_can_proceed(registry, controller, lease_store):
    _make_missing_record(registry)
    engine = _engine(registry, controller, lease_store)
    engine.recover_session("dell-5530", "wtest")
    # Session is ACTIVE again now (a real upsert_seen would have run) --
    # simulate that, then confirm a LATER genuine MISSING cycle can
    # still acquire the lock (not stuck held forever).
    assert lease_store.acquire("recovery:dell-5530/wtest", "someone-else", ttl_seconds=1)


def test_the_lock_is_released_even_when_the_attempt_fails(registry, controller, lease_store):
    _make_missing_record(registry, conversation_id="11111111-1111-1111-1111-111111111111")
    controller.reopen_result = {"error": "RECOVERY_FAILED", "recovery_detail": "x"}
    engine = _engine(registry, controller, lease_store)
    engine.recover_session("dell-5530", "wtest")
    assert lease_store.acquire("recovery:dell-5530/wtest", "someone-else", ttl_seconds=1)


# -- generation counter ----------------------------------------------------

def test_each_attempt_gets_a_new_never_reused_generation(registry, controller, lease_store):
    _make_missing_record(registry)
    engine = _engine(registry, controller, lease_store, AutoRecoveryConfig(enabled=True, max_attempts=10))
    r1 = engine.recover_session("dell-5530", "wtest")
    # Force it back to MISSING for a second real attempt.
    registry.mark_missing("dell-5530", set())
    r2 = engine.recover_session("dell-5530", "wtest")
    assert r2["generation"] == r1["generation"] + 1


# -- reconcile_node --------------------------------------------------------

def test_reconcile_node_attempts_every_recoverable_row(registry, controller, lease_store):
    _make_missing_record(registry, name="a")
    _make_missing_record(registry, name="b")
    controller.registry_rows["dell-5530"] = [{"session_name": "a"}, {"session_name": "b"}]
    engine = _engine(registry, controller, lease_store)
    results = engine.reconcile_node("dell-5530")
    assert len(results) == 2
    assert sorted(controller.reopen_calls) == ["dell-5530/a", "dell-5530/b"]


def test_reconcile_node_propagates_a_node_unreachable_error(registry, controller, lease_store):
    class BrokenController(FakeController):
        def registry_list(self, node_id, *, recoverable_only=False):
            return {"error": "NODE_UNREACHABLE", "detail": "no client"}
    engine = _engine(registry, BrokenController(), lease_store)
    results = engine.reconcile_node("dell-5530")
    assert results == [{"error": "NODE_UNREACHABLE", "node_id": "dell-5530", "detail": "no client"}]


def test_reconcile_node_one_session_failure_never_stops_the_rest(registry, controller, lease_store):
    _make_missing_record(registry, name="a", conversation_id="11111111-1111-1111-1111-111111111111")
    _make_missing_record(registry, name="b")
    controller.registry_rows["dell-5530"] = [{"session_name": "a"}, {"session_name": "b"}]
    controller.reopen_result = {"error": "RECOVERY_FAILED", "recovery_detail": "boom"}
    engine = _engine(registry, controller, lease_store)
    results = engine.reconcile_node("dell-5530")
    assert len(results) == 2
    assert all(r.get("error") == "RECOVERY_FAILED" for r in results)


# -- checkpoint (item 6) ---------------------------------------------------

def test_checkpoint_records_a_real_confirmation_point(registry, controller, lease_store):
    registry.upsert_seen("dell-5530", "wtest", agent_type="claude", cwd="C:\\Dev\\proj")
    engine = _engine(registry, controller, lease_store)
    result = engine.checkpoint("dell-5530", "wtest", detail="task abc123 reached COMPLETED")
    assert result["last_checkpoint_detail"] == "task abc123 reached COMPLETED"
    assert result["last_checkpoint_at"] is not None


def test_checkpoint_on_a_session_with_no_registry_row(registry, controller, lease_store):
    engine = _engine(registry, controller, lease_store)
    result = engine.checkpoint("dell-5530", "no-such-session", detail="x")
    assert result["error"] == "REGISTRY_RECORD_NOT_FOUND"
