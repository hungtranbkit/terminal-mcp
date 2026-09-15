"""blg_8d65afc1b38b -- watch coverage must come back.

The production symptom this reproduces: `watch_count=4,
enabled_watch_count=0`. Every disable path in supervisor.py was permanent
because nothing ever called set_enabled(..., True), so a worker whose
watch hit max_iterations kept working and stayed invisible to scheduling
forever.

These tests drive the real SupervisorService and SupervisorStore against
real tmux sessions -- the point is the wiring, which the pure tests in
test_scheduler_health.py cannot cover.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import scheduler_health as sh
from terminal_mcp.config import AppConfig, PermissionsConfig, SessionAccessConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore, watch_key


def _service(tmp_path, **overrides):
    config = AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20,
                       supervisor=SupervisorConfig(**overrides),
                       session_access=SessionAccessConfig(default_read=True, default_input=False))
    return SupervisorService(TerminalService(config), SupervisorStore(tmp_path / "supervisor.db"))


def _age(store, key, seconds):
    """Backdate a watch so backoff has elapsed, instead of sleeping."""
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store._connection() as connection:
        connection.execute("UPDATE watches SET updated_at = ? WHERE watch_key = ?", (stamp, key))


@pytest.fixture
def worker(tmux_session_factory):
    """A live session standing in for a worker. Long-running so the
    reconcile liveness probe finds it alive, as a real worker would be."""
    return tmux_session_factory("test-sched-worker", "bash -lc 'sleep 120'")


def test_max_iterations_disable_does_not_permanently_lose_the_worker(tmp_path, worker):
    """The headline bug. A live worker whose watch hit the poll ceiling is
    brought back on the next reconcile pass."""
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)

    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
    assert service.status()["enabled_watch_count"] == 0
    assert service.status()["recoverable_disabled_count"] == 1

    _age(service.store, key, 120)
    result = service.reconcile_watches()

    assert [item["watch_key"] for item in result["restored"]] == [key]
    assert service.status()["enabled_watch_count"] == 1
    row = service.store.get_watch(key)
    assert row["disabled_reason"] is None
    # The ceiling is reset too -- otherwise it re-disables on the next
    # quiet poll and the fix looks like it did nothing.
    assert row["iteration_count"] == 0
    assert row["reconcile_attempts"] == 1


def test_manual_unwatch_is_never_resurrected(tmp_path, worker):
    """A person said no. Reconciliation must not argue."""
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.unwatch(session=worker, delete=False)
    assert service.store.get_watch(key)["disabled_reason"] == "manual_unwatch"

    _age(service.store, key, 10 ** 6)
    result = service.reconcile_watches()

    assert result["restored"] == []
    assert service.store.get_watch(key)["enabled"] == 0
    assert service.status()["intentionally_excluded_count"] == 1
    skipped = {item["watch_key"]: item["reason"] for item in result["skipped"]}
    assert "deliberate" in skipped[key]


def test_target_missing_then_returning_is_reconciled(tmp_path, worker):
    """A session that disappears and comes back must regain coverage
    rather than being abandoned."""
    service = _service(tmp_path)
    service.watch(session="test-gone-for-now")
    key = watch_key("session", "test-gone-for-now")
    service.store.set_enabled(key, False, disabled_reason="target_missing")
    _age(service.store, key, 300)

    # Still missing -> not re-enabled, but explicitly still tracked.
    result = service.reconcile_watches()
    assert result["restored"] == []
    assert any("still missing" in item["reason"] for item in result["skipped"])

    # It comes back under a name that DOES exist -> reconciled.
    service.store.rename_target("test-gone-for-now", worker)
    new_key = watch_key("session", worker)
    _age(service.store, new_key, 300)
    result = service.reconcile_watches()
    assert [item["watch_key"] for item in result["restored"]] == [new_key]


def test_reconcile_backs_off_rather_than_spinning(tmp_path, worker):
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")

    # Just disabled -> inside the backoff window, left alone.
    assert service.reconcile_watches()["restored"] == []
    _age(service.store, key, 120)
    assert len(service.reconcile_watches()["restored"]) == 1


def test_reconcile_is_idempotent_and_safe_to_repeat(tmp_path, worker):
    """Restart/recovery runs this on every pass; a second call must be a
    no-op rather than churning state."""
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="same_failure_limit_exceeded")
    _age(service.store, key, 120)

    first = service.reconcile_watches()
    second = service.reconcile_watches()
    assert len(first["restored"]) == 1
    assert second["restored"] == [], "an already-enabled watch must not be restored again"
    assert service.store.get_watch(key)["reconcile_attempts"] == 1


def test_run_once_reconciles_before_polling(tmp_path, worker):
    """Recovery must cost one cycle, not two -- a watch restored at the
    top of a pass is polled in that same pass."""
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
    _age(service.store, key, 120)

    result = service.run_once()
    assert [item["watch_key"] for item in result["reconciled"]["restored"]] == [key]
    assert service.store.get_watch(key)["enabled"] == 1
    # Polled in the same pass: the poll bumps iteration_count off zero.
    assert service.store.get_watch(key)["iteration_count"] >= 1


def test_reconcile_emits_an_auditable_event(tmp_path, worker):
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
    _age(service.store, key, 120)
    service.reconcile_watches()

    events = [e for e in service.store.list_events(limit=50) if e["event_type"] == "watch_reconciled"]
    assert len(events) == 1
    assert "max_iterations_exceeded" in events[0]["reason"]
    assert events[0]["metadata"]["was_disabled_reason"] == "max_iterations_exceeded"


def test_status_explains_why_watches_are_off(tmp_path, worker):
    """The old status could say 0 enabled but not why, nor whether they
    were coming back."""
    service = _service(tmp_path)
    service.watch(session=worker)
    service.watch(session="test-manual-off")
    service.store.set_enabled(watch_key("session", worker), False,
                              disabled_reason="max_iterations_exceeded")
    service.store.set_enabled(watch_key("session", "test-manual-off"), False,
                              disabled_reason="manual_unwatch")

    status = service.status()
    assert status["enabled_watch_count"] == 0
    assert status["disabled_watch_count"] == 2
    assert status["recoverable_disabled_count"] == 1
    assert status["intentionally_excluded_count"] == 1
    assert set(status["disabled_reasons"].values()) == {"max_iterations_exceeded", "manual_unwatch"}


def test_recovery_attempts_are_capped(tmp_path, worker):
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    with service.store._connection() as connection:
        connection.execute("UPDATE watches SET reconcile_attempts = 6 WHERE watch_key = ?", (key,))
    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
    _age(service.store, key, 10 ** 6)

    result = service.reconcile_watches()
    assert result["restored"] == []
    assert any("retried" in item["reason"] for item in result["skipped"])
