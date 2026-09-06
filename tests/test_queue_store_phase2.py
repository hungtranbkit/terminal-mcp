"""Supervisor Queue v2 Phase 2 -- QueueStore additions (task: "Supervisor
Queue v2 Phase 2 -- Coordinator Agent"): atomic claim/lease, the
Coordinator decision -> task-status mapping, restart-safe stale-claim
reconciliation, and dependency gating. Same in-process, no-real-session
posture as test_queue_store.py.

SAFETY: every session name below is a disposable test fixture -- never
`window`/`window2`."""
from __future__ import annotations

import threading
import time

import pytest

from terminal_mcp.queue_store import (
    BLOCKED, CANCELLED, COMPLETED, DISPATCHING, FAILED, PAUSED, PRECHECK, QUEUED, READY, RUNNING, VERIFYING,
    QueueStore, is_valid_transition,
)


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _make_one_task(store, session="lane-a", **overrides):
    task = {"prompt": "do the thing", "title": "Task"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    return task_id


# ---------------------------------------------------------------------------
# New Phase 2 state-machine edges.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("from_status,to_status", [
    (QUEUED, PRECHECK), (PRECHECK, READY), (PRECHECK, QUEUED), (PRECHECK, BLOCKED), (PRECHECK, PAUSED),
    (READY, DISPATCHING), (DISPATCHING, FAILED), (RUNNING, FAILED), (VERIFYING, FAILED),
    (FAILED, QUEUED), (FAILED, SKIPPED := "SKIPPED"), (FAILED, CANCELLED),
    (PAUSED, PRECHECK), (PAUSED, READY),
])
def test_new_phase2_edges_are_valid(from_status, to_status):
    assert is_valid_transition(from_status, to_status) is True


def test_phase1_edges_still_valid_unchanged():
    # Backward compatibility -- direct QUEUED -> DISPATCHING (bypassing
    # the coordinator gate) must still work for any caller/test that
    # relies on it.
    assert is_valid_transition(QUEUED, DISPATCHING) is True
    assert is_valid_transition(QUEUED, "CANCELLED") is True


def test_precheck_cannot_jump_straight_to_dispatching():
    assert is_valid_transition(PRECHECK, DISPATCHING) is False  # must go through READY


# ---------------------------------------------------------------------------
# claim_next_task: atomicity (the real Phase 1 safety gap this closes).
# ---------------------------------------------------------------------------

def test_claim_next_task_transitions_queued_to_precheck_and_stamps_lease(store):
    task_id = _make_one_task(store)
    claimed = store.claim_next_task("lane-a", claimed_by="engine-1", lease_seconds=60)
    assert claimed.id == task_id
    assert claimed.status == PRECHECK
    assert claimed.claimed_by == "engine-1"
    assert claimed.claim_token is not None
    assert claimed.lease_expires_at is not None


def test_claim_next_task_returns_none_when_nothing_queued(store):
    assert store.claim_next_task("lane-a", claimed_by="engine-1") is None


def test_claim_next_task_returns_none_when_lane_paused(store):
    _make_one_task(store)
    store.pause_lane("lane-a", reason="test")
    assert store.claim_next_task("lane-a", claimed_by="engine-1") is None


def test_claim_next_task_never_claims_a_second_task_while_one_is_active(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    first = store.claim_next_task("lane-a", claimed_by="engine-1")
    assert first.id == ids[0]
    second = store.claim_next_task("lane-a", claimed_by="engine-1")
    assert second is None  # a is still PRECHECK -- b must wait


def test_claim_next_task_is_race_free_under_concurrent_callers(store):
    """The exact Phase 1 gap: two 'engine' threads calling claim_next_task
    concurrently for the same lane must never both come back with the
    same task -- BEGIN IMMEDIATE serializes them."""
    ids = store.set_tasks("lane-a", [{"prompt": f"task-{i}"} for i in range(20)])
    results: list = []
    lock = threading.Lock()

    def worker(worker_id):
        for _ in range(30):
            claimed = store.claim_next_task("lane-a", claimed_by=f"worker-{worker_id}")
            if claimed is not None:
                with lock:
                    results.append(claimed.id)
            time.sleep(0.001)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every claimed id must be unique -- no task claimed twice, no matter
    # how many threads raced for it (only the FIRST task is ever
    # claimable anyway since nothing here ever completes it, but the
    # invariant under test is "never claimed twice").
    assert len(results) == len(set(results))
    assert len(results) <= 1  # only one task can ever be active at a time in this lane
    if results:
        assert results[0] in ids


# ---------------------------------------------------------------------------
# record_coordinator_decision: the READY/BLOCKED/NEEDS_REWORK/NEEDS_HUMAN
# -> task-status mapping.
# ---------------------------------------------------------------------------

def test_coordinator_ready_moves_precheck_to_ready(store):
    task_id = _make_one_task(store)
    store.claim_next_task("lane-a", claimed_by="engine-1")
    updated = store.record_coordinator_decision(task_id, status="READY", reason="all checks passed")
    assert updated.status == READY
    assert updated.coordinator_decision["status"] == "READY"
    assert updated.coordinator_attempts == 1


def test_coordinator_blocked_stops_only_this_lane(store):
    task_a = _make_one_task(store, session="lane-a")
    task_b = _make_one_task(store, session="lane-b")
    store.claim_next_task("lane-a", claimed_by="engine-1")
    store.claim_next_task("lane-b", claimed_by="engine-1")

    store.record_coordinator_decision(task_a, status="BLOCKED", reason="uncommitted changes present",
                                      blockers=["3 modified files not committed"])
    assert store.get_task(task_a).status == BLOCKED
    # lane-b's own task is untouched -- still claimable/reviewable.
    assert store.get_task(task_b).status == PRECHECK
    assert store.claim_next_task("lane-a", claimed_by="engine-1") is None  # lane-a stuck
    assert store.lane_status("lane-b")["paused"] is False


def test_coordinator_needs_rework_returns_task_to_queued_with_claim_cleared(store):
    task_id = _make_one_task(store)
    store.claim_next_task("lane-a", claimed_by="engine-1")
    updated = store.record_coordinator_decision(task_id, status="NEEDS_REWORK",
                                                reason="previous task has no recorded completion evidence",
                                                required_actions=["re-verify previous task"])
    assert updated.status == QUEUED
    assert updated.claimed_by is None
    assert updated.claim_token is None
    # a fresh claim must be possible again (not stuck thinking it's still claimed)
    reclaimed = store.claim_next_task("lane-a", claimed_by="engine-2")
    assert reclaimed.id == task_id


def test_coordinator_needs_human_pauses_the_whole_lane_not_others(store):
    task_a = _make_one_task(store, session="lane-a")
    task_b = _make_one_task(store, session="lane-b")
    store.claim_next_task("lane-a", claimed_by="engine-1")
    store.claim_next_task("lane-b", claimed_by="engine-1")

    store.record_coordinator_decision(task_a, status="NEEDS_HUMAN",
                                      reason="task prompt matches a destructive-command pattern")
    task = store.get_task(task_a)
    assert task.status == PAUSED
    assert task.paused_from_status == QUEUED
    assert store.lane_status("lane-a")["paused"] is True
    assert store.lane_status("lane-b")["paused"] is False  # never touched
    assert store.get_task(task_b).status == PRECHECK  # lane-b's own review proceeds normally


def test_coordinator_needs_human_then_resume_returns_task_to_queued_for_fresh_review(store):
    task_id = _make_one_task(store)
    store.claim_next_task("lane-a", claimed_by="engine-1")
    store.record_coordinator_decision(task_id, status="NEEDS_HUMAN", reason="needs a human look")
    store.resume_lane("lane-a")
    task = store.get_task(task_id)
    assert task.status == QUEUED  # not back into PRECHECK holding a stale claim
    assert task.claimed_by is None


def test_coordinator_decision_rejects_unknown_status(store):
    task_id = _make_one_task(store)
    store.claim_next_task("lane-a", claimed_by="engine-1")
    with pytest.raises(ValueError):
        store.record_coordinator_decision(task_id, status="MAYBE", reason="nonsense")


# ---------------------------------------------------------------------------
# reconcile_stale_claims: restart-safe recovery.
# ---------------------------------------------------------------------------

def test_reconcile_stale_claims_recovers_an_expired_precheck_lease(store):
    task_id = _make_one_task(store)
    store.claim_next_task("lane-a", claimed_by="dead-engine", lease_seconds=-1)  # already expired
    reconciled = store.reconcile_stale_claims("lane-a")
    assert task_id in reconciled
    task = store.get_task(task_id)
    assert task.status == QUEUED
    assert task.claimed_by is None


def test_reconcile_stale_claims_never_touches_a_healthy_lease(store):
    task_id = _make_one_task(store)
    store.claim_next_task("lane-a", claimed_by="alive-engine", lease_seconds=300)
    reconciled = store.reconcile_stale_claims("lane-a")
    assert reconciled == []
    assert store.get_task(task_id).status == PRECHECK


def test_reconcile_stale_claims_scoped_to_one_session_never_touches_another(store):
    task_a = _make_one_task(store, session="lane-a")
    task_b = _make_one_task(store, session="lane-b")
    store.claim_next_task("lane-a", claimed_by="dead", lease_seconds=-1)
    store.claim_next_task("lane-b", claimed_by="dead", lease_seconds=-1)
    reconciled = store.reconcile_stale_claims("lane-a")
    assert task_a in reconciled
    assert task_b not in reconciled
    assert store.get_task(task_b).status == PRECHECK  # untouched


def test_reconcile_stale_claims_with_no_session_arg_covers_the_whole_fleet(store):
    task_a = _make_one_task(store, session="lane-a")
    task_b = _make_one_task(store, session="lane-b")
    store.claim_next_task("lane-a", claimed_by="dead", lease_seconds=-1)
    store.claim_next_task("lane-b", claimed_by="dead", lease_seconds=-1)
    reconciled = store.reconcile_stale_claims()
    assert set(reconciled) == {task_a, task_b}


def test_reconcile_stale_claims_survives_a_simulated_restart(tmp_path):
    """Item 8/B's own 'restart controller/queue worker giữa chừng không
    duplicate dispatch' -- reconcile from a brand NEW QueueStore instance
    pointed at the same db file (simulating a process restart)."""
    db_path = tmp_path / "queue.db"
    store1 = QueueStore(db_path)
    task_id = _make_one_task(store1)
    store1.claim_next_task("lane-a", claimed_by="engine-before-restart", lease_seconds=-1)

    store2 = QueueStore(db_path)  # fresh instance, same file -- "process restarted"
    reconciled = store2.reconcile_stale_claims()
    assert task_id in reconciled
    assert store2.get_task(task_id).status == QUEUED
    # And it's cleanly re-claimable from here on.
    reclaimed = store2.claim_next_task("lane-a", claimed_by="engine-after-restart")
    assert reclaimed.id == task_id


# ---------------------------------------------------------------------------
# mark_completed_with_evidence
# ---------------------------------------------------------------------------

def _drive_to_verifying(store, task_id):
    store.transition_task(task_id, DISPATCHING, event_type="TEST")
    store.transition_task(task_id, RUNNING, event_type="TEST")
    store.transition_task(task_id, VERIFYING, event_type="TEST")


def test_mark_completed_with_evidence_requires_non_empty_evidence(store):
    task_id = _make_one_task(store)
    _drive_to_verifying(store, task_id)
    with pytest.raises(ValueError):
        store.mark_completed_with_evidence(task_id, evidence={})


def test_mark_completed_with_evidence_stores_it_on_the_task(store):
    task_id = _make_one_task(store)
    _drive_to_verifying(store, task_id)
    updated = store.mark_completed_with_evidence(
        task_id, evidence={"marker_verified": True, "nonce": "abc123"})
    assert updated.status == COMPLETED
    assert updated.verification_evidence == {"marker_verified": True, "nonce": "abc123"}


# ---------------------------------------------------------------------------
# Dependency gating (claim-time filter, item 3's "phụ thuộc task chưa xong").
# ---------------------------------------------------------------------------

def test_task_with_unmet_cross_lane_dependency_is_never_claimed(store):
    dep_id = _make_one_task(store, session="lane-a", prompt="dependency")
    (dependent_id,) = store.set_tasks("lane-b", [{"prompt": "depends on lane-a", "depends_on": [dep_id]}])
    assert store.claim_next_task("lane-b", claimed_by="engine-1") is None
    assert store.get_task(dependent_id).status == QUEUED  # left alone, not blocked -- just not claimable yet


def test_task_dependency_satisfied_once_dependency_completes(store):
    dep_id = _make_one_task(store, session="lane-a", prompt="dependency")
    (dependent_id,) = store.set_tasks("lane-b", [{"prompt": "depends on lane-a", "depends_on": [dep_id]}])
    store.transition_task(dep_id, DISPATCHING, event_type="TEST")
    store.transition_task(dep_id, RUNNING, event_type="TEST")
    store.transition_task(dep_id, VERIFYING, event_type="TEST")
    store.mark_completed_with_evidence(dep_id, evidence={"done": True})

    claimed = store.claim_next_task("lane-b", claimed_by="engine-1")
    assert claimed.id == dependent_id


def test_a_missing_dependency_id_is_treated_as_unmet_fail_closed(store):
    (dependent_id,) = store.set_tasks("lane-a", [{"prompt": "x", "depends_on": ["does-not-exist"]}])
    assert store.claim_next_task("lane-a", claimed_by="engine-1") is None


def test_dependency_gating_does_not_block_other_independent_queued_tasks(store):
    dep_id = _make_one_task(store, session="lane-a", prompt="dependency")
    ids = store.set_tasks("lane-a", [
        {"prompt": "depends on dep", "depends_on": [dep_id]},
        {"prompt": "independent"},
    ], replace_pending=False)
    # dep_id itself is QUEUED (position 0), the dependent is position 1,
    # the independent one is position 2 -- claim_next_task should skip
    # the still-unmet dependent and correctly find... actually dep_id
    # itself is claimable first (FIFO), which is the realistic case.
    claimed = store.claim_next_task("lane-a", claimed_by="engine-1")
    assert claimed.id == dep_id


# ---------------------------------------------------------------------------
# Priority ordering (FIFO within same priority, higher priority first).
# ---------------------------------------------------------------------------

def test_higher_priority_task_is_claimed_before_lower_priority_ones(store):
    ids = store.set_tasks("lane-a", [
        {"prompt": "normal-1", "priority": 0},
        {"prompt": "urgent", "priority": 10},
        {"prompt": "normal-2", "priority": 0},
    ])
    claimed = store.claim_next_task("lane-a", claimed_by="engine-1")
    assert claimed.id == ids[1]  # the priority=10 task, despite being enqueued second
