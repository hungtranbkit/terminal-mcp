"""blg_8d65afc1b38b -- scheduler work-conservation and watch recovery.

Production, 2026-09-14: `watch_count=4, enabled_watch_count=0` with the
supervisor loop polling every 20s, two stalled workers, `hp1` untouched
for ~35h, and `linux1`/`hp3-work` idle at an empty prompt while READY work
existed.

Root cause for the coverage half: supervisor.py disables watches for seven
distinct reasons and, before this change, called `set_enabled(..., True)`
from nowhere at all. Every disable was permanent.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp import scheduler_health as sh
from terminal_mcp.scheduler_health import (
    SchedulerThresholds,
    WorkerSignals,
    classify_worker,
    evaluate_refill,
    may_reclaim,
    utilization_snapshot,
    watch_recovery_action,
)

T = SchedulerThresholds()


# ---------------------------------------------------------------------------
# worker classification
# ---------------------------------------------------------------------------

def test_completed_agent_at_empty_prompt_becomes_idle_ready_after_debounce():
    """linux1 / hp3-work: finished, sitting at an empty prompt. Before the
    debounce it is still RUNNING (it may be between turns); after, it is
    dispatchable."""
    early = classify_worker(WorkerSignals("linux1", agent_prompt_empty=True,
                                          seconds_since_output_change=12))
    assert early.state == sh.RUNNING
    assert "debounce" in early.reason

    settled = classify_worker(WorkerSignals("linux1", agent_prompt_empty=True,
                                            seconds_since_output_change=90))
    assert settled.state == sh.IDLE_READY
    assert settled.confidence == "high"


def test_debounce_is_configurable():
    signals = WorkerSignals("w", agent_prompt_empty=True, seconds_since_output_change=35)
    assert classify_worker(signals, SchedulerThresholds(idle_debounce_seconds=30)).state == sh.IDLE_READY
    assert classify_worker(signals, SchedulerThresholds(idle_debounce_seconds=60)).state == sh.RUNNING


def test_a_running_child_process_is_never_called_stalled():
    """The compile that prints nothing for twenty minutes. Reclaiming this
    destroys real work, so a live child outranks every quietness signal."""
    verdict = classify_worker(WorkerSignals("builder", child_process_running=True,
                                            seconds_since_output_change=1500,
                                            seconds_since_tmux_activity=1500))
    assert verdict.state == sh.RUNNING
    assert may_reclaim(verdict)[0] is False


def test_a_child_process_stuck_past_the_hard_ceiling_is_stalled():
    """...but a child process alone cannot protect a session forever."""
    verdict = classify_worker(WorkerSignals("builder", child_process_running=True,
                                            seconds_since_output_change=40000))
    assert verdict.state == sh.STALLED
    assert may_reclaim(verdict)[0] is True


def test_hp1_style_long_dead_session_is_stalled():
    """hp1: ~35h with no activity and nothing running."""
    verdict = classify_worker(WorkerSignals("hp1", seconds_since_output_change=35 * 3600,
                                            seconds_since_tmux_activity=35 * 3600,
                                            child_process_running=False))
    assert verdict.state == sh.STALLED
    assert verdict.confidence == "high"
    assert may_reclaim(verdict)[0] is True


def test_moderately_quiet_without_prompt_state_is_unknown_not_stalled():
    """linux2/hp2: ~1h old activity while the UI still looked busy. Quiet
    is suspicious, not conclusive -- this must ask for a probe, never
    reclaim."""
    verdict = classify_worker(WorkerSignals("hp2", seconds_since_output_change=3600,
                                            seconds_since_tmux_activity=3600))
    assert verdict.state == sh.UNKNOWN
    assert verdict.confidence == "low"
    # The actionable guidance lives on the verdict -- may_reclaim only
    # reports the refusal, which is accurate but says less.
    assert "probe before reclaiming" in verdict.reason
    assert may_reclaim(verdict)[0] is False


def test_pending_shell_command_counts_as_running():
    assert classify_worker(WorkerSignals("w", pending_shell_command=True,
                                         seconds_since_output_change=900)).state == sh.RUNNING


@pytest.mark.parametrize("signals,expected", [
    (WorkerSignals("w", node_online=False), sh.OFFLINE),
    (WorkerSignals("w", session_exists=False), sh.OFFLINE),
    (WorkerSignals("w", reserved=True), sh.RESERVED),
    (WorkerSignals("w", blocked_reason="worktree locked"), sh.BLOCKED),
    (WorkerSignals("w", awaiting_human_input=True), sh.WAITING_INPUT),
])
def test_hard_states_win_over_activity_heuristics(signals, expected):
    assert classify_worker(signals).state == expected


def test_unknown_when_there_is_nothing_to_judge_on():
    verdict = classify_worker(WorkerSignals("w"))
    assert verdict.state == sh.UNKNOWN and verdict.confidence == "low"


def test_every_verdict_names_the_signals_it_used():
    verdict = classify_worker(WorkerSignals("w", agent_prompt_empty=True,
                                            seconds_since_output_change=90))
    assert "agent_prompt_empty" in verdict.signals_considered
    assert verdict.reason


# ---------------------------------------------------------------------------
# watch recovery -- the actual production bug
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", sorted(sh.RECOVERABLE_DISABLE_REASONS))
def test_recoverable_disables_come_back_when_the_target_is_alive(reason):
    action = watch_recovery_action(disabled_reason=reason, target_alive=True,
                                   attempts=0, seconds_since_disabled=3600)
    assert action.should_reenable is True


@pytest.mark.parametrize("reason", sorted(sh.INTENTIONAL_DISABLE_REASONS))
def test_a_persons_decision_is_never_overridden(reason):
    action = watch_recovery_action(disabled_reason=reason, target_alive=True,
                                   attempts=0, seconds_since_disabled=99999)
    assert action.should_reenable is False
    assert "deliberate" in action.reason


def test_a_missing_target_is_not_re_enabled_but_is_not_abandoned():
    action = watch_recovery_action(disabled_reason="target_missing", target_alive=False,
                                   seconds_since_disabled=9999)
    assert action.should_reenable is False
    assert "still missing" in action.reason
    # ...and the moment it comes back, it is taken again.
    assert watch_recovery_action(disabled_reason="target_missing", target_alive=True,
                                 seconds_since_disabled=9999).should_reenable is True


def test_recovery_backs_off_exponentially_and_is_capped():
    for attempts, waited, expected in [(0, 10, False), (0, 120, True),
                                       (1, 90, False), (1, 200, True),
                                       (2, 200, False), (2, 500, True)]:
        action = watch_recovery_action(disabled_reason="max_iterations_exceeded", target_alive=True,
                                       attempts=attempts, seconds_since_disabled=waited)
        assert action.should_reenable is expected, (attempts, waited)
    capped = watch_recovery_action(disabled_reason="max_iterations_exceeded", target_alive=True,
                                   attempts=6, seconds_since_disabled=10 ** 9)
    assert capped.should_reenable is False and "retried" in capped.reason


def test_an_enabled_watch_and_an_unknown_reason_are_left_alone():
    assert watch_recovery_action(disabled_reason=None, target_alive=True).should_reenable is False
    assert watch_recovery_action(disabled_reason="something_new", target_alive=True).should_reenable is False


# ---------------------------------------------------------------------------
# the refill invariant
# ---------------------------------------------------------------------------

def _idle(*names):
    return {name: classify_worker(WorkerSignals(name, agent_prompt_empty=True,
                                                seconds_since_output_change=120)) for name in names}


def test_ready_work_plus_idle_workers_dispatches():
    """Acceptance criterion 1: three idle workers, three independent READY
    tasks, all filled in ONE pass with no manual sends."""
    decision = evaluate_refill(workers=_idle("linux1", "hp3-work", "linux2"),
                               ready_tasks=[{"id": "t1"}, {"id": "t2"}, {"id": "t3"}])
    assert len(decision.dispatchable) == 3
    assert {pair[0] for pair in decision.dispatchable} == {"linux1", "hp3-work", "linux2"}
    assert {pair[1] for pair in decision.dispatchable} == {"t1", "t2", "t3"}
    assert decision.invariant_violated is False


def test_no_ready_work_leaves_workers_idle_with_a_reason():
    decision = evaluate_refill(workers=_idle("a", "b"), ready_tasks=[])
    assert decision.dispatchable == ()
    assert decision.reason == sh.NO_READY_WORK
    assert decision.invariant_violated is False, "no work is not a violation"


def test_no_idle_worker_is_not_a_violation():
    busy = {"a": classify_worker(WorkerSignals("a", child_process_running=True))}
    decision = evaluate_refill(workers=busy, ready_tasks=[{"id": "t1"}])
    assert decision.reason == sh.NO_IDLE_WORKER
    assert decision.invariant_violated is False


def test_dependency_blocked_gives_an_explicit_reason_not_silence():
    decision = evaluate_refill(
        workers=_idle("a"), ready_tasks=[{"id": "t1"}],
        compatible=lambda session, task: (False, sh.DEPENDENCY_BLOCKED))
    assert decision.dispatchable == ()
    assert decision.reason == sh.DEPENDENCY_BLOCKED
    assert decision.blocked_reasons["a"] == sh.DEPENDENCY_BLOCKED
    # The invariant IS violated -- and that is fine, because there is a
    # recorded reason. Silence is what must never happen.
    assert decision.invariant_violated is True


def test_capability_mismatch_never_dispatches_to_the_wrong_worker():
    def compatible(session, task):
        return (session == "windows1" and task.get("os") == "windows"), sh.CAPABILITY_MISMATCH

    workers = _idle("linux1", "windows1")
    decision = evaluate_refill(workers=workers, ready_tasks=[{"id": "w1", "os": "windows"}],
                               compatible=compatible)
    assert decision.dispatchable == (("windows1", "w1"),)
    assert decision.blocked_reasons.get("linux1") == sh.CAPABILITY_MISMATCH


def test_one_task_per_worker_and_one_worker_per_task_in_a_pass():
    decision = evaluate_refill(workers=_idle("a", "b", "c"), ready_tasks=[{"id": "t1"}])
    assert len(decision.dispatchable) == 1, "a task must not be handed to two workers"
    sessions = [pair[0] for pair in decision.dispatchable]
    assert len(sessions) == len(set(sessions))


def test_concurrency_limit_is_honoured_and_named():
    decision = evaluate_refill(workers=_idle("a", "b", "c"),
                               ready_tasks=[{"id": "t1"}, {"id": "t2"}, {"id": "t3"}],
                               max_dispatch=2)
    assert len(decision.dispatchable) == 2
    assert sh.CONCURRENCY_LIMIT in decision.blocked_reasons.values()


def test_dispatch_disabled_is_its_own_reason():
    decision = evaluate_refill(workers=_idle("a"), ready_tasks=[{"id": "t1"}], dispatch_enabled=False)
    assert decision.reason == sh.DISPATCH_DISABLED


def test_every_non_dispatch_reason_is_from_the_declared_set():
    decision = evaluate_refill(workers=_idle("a"), ready_tasks=[{"id": "t1"}],
                               compatible=lambda s, t: (False, sh.WORKTREE_CONFLICT))
    assert decision.reason in sh.NON_DISPATCH_REASONS
    for reason in decision.blocked_reasons.values():
        assert reason in sh.NON_DISPATCH_REASONS


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------

def test_snapshot_explains_why_the_fleet_is_idle():
    workers = {
        **_idle("linux1", "hp3-work"),
        "hp1": classify_worker(WorkerSignals("hp1", seconds_since_output_change=35 * 3600,
                                             seconds_since_tmux_activity=35 * 3600)),
        "builder": classify_worker(WorkerSignals("builder", child_process_running=True)),
        "gone": classify_worker(WorkerSignals("gone", node_online=False)),
    }
    snapshot = utilization_snapshot(workers, ready_count=4, reclaimed_count=1)
    assert snapshot["total_workers"] == 5
    assert snapshot["usable_workers"] == 4, "an offline worker is not usable capacity"
    assert snapshot["counts"][sh.IDLE_READY] == 2
    assert snapshot["counts"][sh.STALLED] == 1
    assert snapshot["counts"][sh.RUNNING] == 1
    assert snapshot["idle_with_ready_work"] is True
    assert snapshot["utilization_percent"] == 25.0
    assert snapshot["reclaimed_stalled_count"] == 1
    # Per-worker detail is what turns "the fleet is idle" into "hp1 has not
    # moved in 35 hours".
    assert "35 hours" in snapshot["per_worker"]["hp1"]["reason"] or \
           "126000" in snapshot["per_worker"]["hp1"]["reason"]


def test_snapshot_counts_every_worker_exactly_once():
    workers = {**_idle("a"), "b": classify_worker(WorkerSignals("b", node_online=False))}
    snapshot = utilization_snapshot(workers, ready_count=0)
    assert sum(snapshot["counts"].values()) == snapshot["total_workers"]
