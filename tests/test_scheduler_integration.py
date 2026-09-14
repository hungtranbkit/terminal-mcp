"""Integration contract for the scheduler fix (blg_8d65afc1b38b).

INTEGRATION LANE. This file deliberately contains **no scheduler
implementation**. Phase 1 (watch reconciliation + the pure state/refill
model) is on fix/scheduler-refill; phase 2 (worker discovery and actual
dispatch) is hp3-work's. This is the seam between them: the behaviour
integration will check the moment phase 2 lands.

How it behaves before phase 2 exists
------------------------------------
Checks that need a phase-2 symbol `pytest.skip` with the MISSING SYMBOL
NAMED, so a skip is a to-do list rather than a silent pass. Everything
phase 1 already guarantees is asserted unconditionally -- this file is
never fully inert, which is what stops "all green" from meaning "nothing
ran".

Run it as-is today to confirm the phase-1 half still holds; run it again
after the phase-2 merge and the skips should become passes without anyone
editing this file. If a skip is still skipping after phase 2 lands, the
name it prints is the integration gap.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import scheduler_health as sh
from terminal_mcp.config import AppConfig, PermissionsConfig, SessionAccessConfig, SupervisorConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import SupervisorService, SupervisorStore, watch_key


# --------------------------------------------------------------------------
# phase-2 capability probes -- one place, so the whole file agrees on what
# "phase 2 has landed" means.
# --------------------------------------------------------------------------

def _phase2(name: str):
    """Return a phase-2 callable, or skip naming exactly what is missing."""
    module = importlib.reload(importlib.import_module("terminal_mcp.scheduler_health"))
    target = getattr(module, name, None)
    if target is None:
        pytest.skip(f"phase 2 not landed: terminal_mcp.scheduler_health.{name} does not exist yet")
    return target


def _phase2_attr(module_name: str, name: str):
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        pytest.skip(f"phase 2 not landed: module {module_name} does not exist yet")
    target = getattr(module, name, None)
    if target is None:
        pytest.skip(f"phase 2 not landed: {module_name}.{name} does not exist yet")
    return target


def _service(tmp_path, **overrides):
    config = AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20,
                       supervisor=SupervisorConfig(**overrides),
                       session_access=SessionAccessConfig(default_read=True, default_input=False))
    return SupervisorService(TerminalService(config), SupervisorStore(tmp_path / "supervisor.db"))


def _age(store, key, seconds):
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with store._connection() as connection:
        connection.execute("UPDATE watches SET updated_at = ? WHERE watch_key = ?", (stamp, key))


@pytest.fixture
def worker(tmux_session_factory):
    return tmux_session_factory("test-integ-worker", "bash -lc 'sleep 120'")


# ==========================================================================
# 1. disabled-watch recovery  -- PHASE 1, asserted unconditionally
# ==========================================================================

def test_check1_disabled_watch_recovery(tmp_path, worker):
    """The original production symptom: watch_count=4, enabled=0."""
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
    _age(service.store, key, 120)

    assert service.status()["enabled_watch_count"] == 0
    restored = service.reconcile_watches()["restored"]
    assert [item["watch_key"] for item in restored] == [key]
    assert service.status()["enabled_watch_count"] == 1


def test_check1b_intentional_exclusion_survives_recovery(tmp_path, worker):
    """Integration must not "fix" a deliberate manual_unwatch."""
    service = _service(tmp_path)
    service.watch(session=worker)
    service.unwatch(session=worker, delete=False)
    _age(service.store, watch_key("session", worker), 10 ** 6)
    assert service.reconcile_watches()["restored"] == []
    assert service.status()["intentionally_excluded_count"] == 1


# ==========================================================================
# 2. auto-created watches  -- PHASE 2 (hp3-work worker discovery)
# ==========================================================================

def test_check2_worker_discovery_creates_a_watch(tmp_path, worker):
    """A live worker with no watch at all must gain coverage on its own.

    Phase 1 can only revive a watch that already exists; a worker that
    never had one stays invisible. That gap is phase 2's discovery pass.
    """
    discover = _phase2_attr("terminal_mcp.scheduler_health", "discover_workers")
    service = _service(tmp_path)
    assert service.status()["watch_count"] == 0
    discover(service)  # phase 2 decides the signature; adjust here if it differs
    keys = [row["watch_key"] for row in service.store.list_watches()]
    assert watch_key("session", worker) in keys
    assert service.status()["enabled_watch_count"] >= 1


# ==========================================================================
# 3. READY + IDLE auto-dispatch  -- PHASE 2
# ==========================================================================

def test_check3_ready_plus_idle_dispatches_within_one_pass():
    """Acceptance criterion 1: >=3 idle workers, >=3 independent READY
    tasks, all filled in one pass with no manual sends."""
    dispatch = _phase2("dispatch_refill")
    workers = {name: sh.classify_worker(sh.WorkerSignals(name, agent_prompt_empty=True,
                                                         seconds_since_output_change=120))
               for name in ("w1", "w2", "w3")}
    result = dispatch(workers=workers, ready_tasks=[{"id": "t1"}, {"id": "t2"}, {"id": "t3"}])
    assert len(result.get("dispatched", [])) == 3


def test_check3b_refill_decision_is_already_correct_today():
    """The DECISION half is phase 1 and must keep holding regardless of
    how phase 2 implements the acting half."""
    workers = {name: sh.classify_worker(sh.WorkerSignals(name, agent_prompt_empty=True,
                                                         seconds_since_output_change=120))
               for name in ("w1", "w2", "w3")}
    decision = sh.evaluate_refill(workers=workers,
                                  ready_tasks=[{"id": "t1"}, {"id": "t2"}, {"id": "t3"}])
    assert len(decision.dispatchable) == 3
    assert decision.invariant_violated is False


# ==========================================================================
# 4. stale / offline exclusion  -- PHASE 1 decision, PHASE 2 enforcement
# ==========================================================================

def test_check4_stale_and_offline_workers_are_never_dispatch_targets():
    workers = {
        "hp1": sh.classify_worker(sh.WorkerSignals("hp1", seconds_since_output_change=35 * 3600,
                                                   seconds_since_tmux_activity=35 * 3600)),
        "gone": sh.classify_worker(sh.WorkerSignals("gone", node_online=False)),
        "busy": sh.classify_worker(sh.WorkerSignals("busy", child_process_running=True)),
    }
    assert workers["hp1"].state == sh.STALLED
    assert workers["gone"].state == sh.OFFLINE
    decision = sh.evaluate_refill(workers=workers, ready_tasks=[{"id": "t1"}])
    assert decision.dispatchable == ()
    assert decision.reason == sh.NO_IDLE_WORKER


def test_check4b_probe_before_reclaim_is_enforced():
    """A merely-quiet worker must not be reclaimed by phase 2."""
    verdict = sh.classify_worker(sh.WorkerSignals("hp2", seconds_since_output_change=3600,
                                                  seconds_since_tmux_activity=3600))
    assert sh.may_reclaim(verdict)[0] is False


# ==========================================================================
# 5. duplicate claim prevention  -- PHASE 2
# ==========================================================================

def test_check5_concurrent_passes_cannot_double_claim():
    """Two scheduler passes racing the same READY task must produce one
    winner. Phase 1 guarantees it WITHIN a pass; across passes it is a
    lease/compare-and-swap job, which is phase 2's."""
    claim = _phase2("claim_task")
    first = claim(session="w1", task_id="t1")
    second = claim(session="w2", task_id="t1")
    assert bool(first) != bool(second), "exactly one claim must win"


def test_check5b_one_pass_never_double_books_either_side():
    decision = sh.evaluate_refill(
        workers={name: sh.classify_worker(sh.WorkerSignals(name, agent_prompt_empty=True,
                                                           seconds_since_output_change=120))
                 for name in ("w1", "w2", "w3")},
        ready_tasks=[{"id": "t1"}])
    assert len(decision.dispatchable) == 1
    sessions = [pair[0] for pair in decision.dispatchable]
    tasks = [pair[1] for pair in decision.dispatchable]
    assert len(sessions) == len(set(sessions)) and len(tasks) == len(set(tasks))


# ==========================================================================
# 6. restart / idempotency  -- PHASE 1, asserted unconditionally
# ==========================================================================

def test_check6_reconcile_is_idempotent_across_restarts(tmp_path, worker):
    """A supervisor restart re-runs reconciliation on every pass; the
    second run must be a no-op, not churn."""
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="same_failure_limit_exceeded")
    _age(service.store, key, 120)
    assert len(service.reconcile_watches()["restored"]) == 1

    # Simulate a restart: a fresh service over the SAME database.
    restarted = _service(tmp_path)
    assert restarted.reconcile_watches()["restored"] == []
    assert restarted.store.get_watch(key)["reconcile_attempts"] == 1


# ==========================================================================
# 7. WAITING_INPUT recovery  -- PHASE 1 decision, PHASE 2 enforcement
# ==========================================================================

def test_check7_waiting_input_is_not_idle_and_not_stalled():
    """A worker blocked on a human is neither dispatchable nor
    reclaimable -- answering it is a person's job."""
    verdict = sh.classify_worker(sh.WorkerSignals("w", awaiting_human_input=True,
                                                  seconds_since_output_change=9000))
    assert verdict.state == sh.WAITING_INPUT
    assert verdict.state not in sh.DISPATCHABLE_STATES
    assert sh.may_reclaim(verdict)[0] is False


def test_check7b_answered_worker_returns_to_the_pool():
    """Once the human answers and the prompt settles, the same worker is
    dispatchable again -- no manual re-enable needed."""
    after = sh.classify_worker(sh.WorkerSignals("w", awaiting_human_input=False,
                                                agent_prompt_empty=True,
                                                seconds_since_output_change=120))
    assert after.state == sh.IDLE_READY


# ==========================================================================
# 8. event / metric evidence  -- PHASE 1 events, PHASE 2 dispatch metrics
# ==========================================================================

def test_check8_reconciliation_leaves_an_auditable_event(tmp_path, worker):
    service = _service(tmp_path)
    service.watch(session=worker)
    key = watch_key("session", worker)
    service.store.set_enabled(key, False, disabled_reason="max_iterations_exceeded")
    _age(service.store, key, 120)
    service.reconcile_watches()
    events = [e for e in service.store.list_events(limit=50) if e["event_type"] == "watch_reconciled"]
    assert len(events) == 1
    assert events[0]["metadata"]["was_disabled_reason"] == "max_iterations_exceeded"


def test_check8b_status_explains_every_disabled_watch(tmp_path, worker):
    service = _service(tmp_path)
    service.watch(session=worker)
    service.watch(session="test-integ-manual")
    service.store.set_enabled(watch_key("session", worker), False,
                              disabled_reason="max_iterations_exceeded")
    service.store.set_enabled(watch_key("session", "test-integ-manual"), False,
                              disabled_reason="manual_unwatch")
    status = service.status()
    assert status["recoverable_disabled_count"] == 1
    assert status["intentionally_excluded_count"] == 1
    assert len(status["disabled_reasons"]) == 2


def test_check8c_utilization_snapshot_answers_why_idle():
    workers = {
        "idle": sh.classify_worker(sh.WorkerSignals("idle", agent_prompt_empty=True,
                                                    seconds_since_output_change=120)),
        "hp1": sh.classify_worker(sh.WorkerSignals("hp1", seconds_since_output_change=35 * 3600,
                                                   seconds_since_tmux_activity=35 * 3600)),
    }
    snapshot = sh.utilization_snapshot(workers, ready_count=3)
    for field in ("total_workers", "usable_workers", "counts", "ready_tasks",
                  "utilization_percent", "idle_with_ready_work", "per_worker"):
        assert field in snapshot, f"{field} is required to explain utilization"
    assert snapshot["idle_with_ready_work"] is True


def test_check8d_dispatch_metrics_exist(tmp_path):
    """Phase 2 must surface last_dispatch_at and a non-dispatch reason on
    the live status surface, not only in the pure model."""
    status_fn = _phase2_attr("terminal_mcp.scheduler_health", "scheduler_status")
    status = status_fn()
    for field in ("last_dispatch_at", "non_dispatch_reason", "reclaimed_stalled_count"):
        assert field in status, f"{field} missing from scheduler_status"
