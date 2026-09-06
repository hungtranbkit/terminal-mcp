"""Task Migration / Load Balancing -- QueueStore additions (task:
"bổ sung Task Migration / Load Balancing vào Queue/Coordinator").
Pure in-process, no real session -- see test_task_migration_engine.py
for the disposable-session/live E2E equivalent.

SAFETY: every session name here is disposable."""
from __future__ import annotations

import threading

import pytest

from terminal_mcp.queue_store import (
    BLOCKED, DISPATCHING, PRECHECK, QUEUED, READY, RUNNING, TaskAlreadyClaimedError, QueueStore,
)


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _make_task(store, session="lane-a", **overrides):
    task = {"prompt": "do the feature work"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    return task_id


# ---------------------------------------------------------------------------
# Item 1: stable task_id, no new task created, full history preserved.
# ---------------------------------------------------------------------------

def test_reassign_moves_ownership_without_creating_a_new_task(store):
    task_id = _make_task(store, session="lane-a", priority=7, metadata={"foo": "bar"})
    updated = store.reassign_task(task_id, "lane-b", reason="lane-a overloaded", actor="test-operator")
    assert updated.id == task_id  # SAME task_id
    assert updated.session == "lane-b"
    assert updated.priority == 7  # preserved
    assert updated.metadata == {"foo": "bar"}  # preserved
    assert store.get_task(task_id) is updated or store.get_task(task_id).id == task_id


def test_reassign_records_full_migration_history(store):
    task_id = _make_task(store, session="lane-a")
    store.reassign_task(task_id, "lane-b", reason="rebalance", actor="operator-1")
    store.reassign_task(task_id, "lane-c", reason="lane-b also overloaded", actor="operator-2")
    task = store.get_task(task_id)
    assert task.session == "lane-c"
    assert task.original_owner == "lane-a"  # never changes
    assert len(task.migration_history) == 2
    assert task.migration_history[0] == {"from": "lane-a", "to": "lane-b", "reason": "rebalance",
                                         "actor": "operator-1", "time": task.migration_history[0]["time"]}
    assert task.migration_history[1]["from"] == "lane-b"
    assert task.migration_history[1]["to"] == "lane-c"


def test_original_owner_is_set_at_creation(store):
    task_id = _make_task(store, session="lane-a")
    assert store.get_task(task_id).original_owner == "lane-a"


def test_assignment_history_tool_shape(store):
    task_id = _make_task(store, session="lane-a")
    store.reassign_task(task_id, "lane-b", reason="test", actor="op")
    history = store.assignment_history(task_id)
    assert history["original_owner"] == "lane-a"
    assert history["current_session"] == "lane-b"
    assert len(history["migration_history"]) == 1


# ---------------------------------------------------------------------------
# Item 2: only QUEUED/WAITING_SESSION/BLOCKED/FAILED are migratable --
# never RUNNING (and, disclosed, never mid-review PRECHECK/READY either).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [PRECHECK, READY, DISPATCHING, RUNNING, "VERIFYING"])
def test_reassign_refuses_a_task_that_is_currently_in_flight(store, status):
    task_id = _make_task(store)
    store.transition_task(task_id, PRECHECK, event_type="TEST")
    if status != PRECHECK:
        store.transition_task(task_id, READY, event_type="TEST")
    if status not in (PRECHECK, READY):
        store.transition_task(task_id, DISPATCHING, event_type="TEST")
    if status not in (PRECHECK, READY, DISPATCHING):
        store.transition_task(task_id, RUNNING, event_type="TEST")
    if status == "VERIFYING":
        store.transition_task(task_id, "VERIFYING", event_type="TEST")
    with pytest.raises(TaskAlreadyClaimedError):
        store.reassign_task(task_id, "lane-b", reason="test", actor="op")
    assert store.get_task(task_id).session == "lane-a"  # untouched


def test_reassign_allows_blocked_and_failed_tasks(store):
    for target_status in (BLOCKED, "FAILED"):
        task_id = _make_task(store)
        store.transition_task(task_id, DISPATCHING, event_type="TEST")
        store.transition_task(task_id, target_status, event_type="TEST", reason="simulated")
        updated = store.reassign_task(task_id, "lane-b", reason="stuck, moving to a healthier session",
                                      actor="operator")
        assert updated.session == "lane-b"
        assert updated.status == QUEUED  # fresh start at the destination


def test_reassign_allows_waiting_session_tasks(store):
    task_id = _make_task(store)
    store.transition_task(task_id, PRECHECK, event_type="TEST")
    store.mark_waiting_session(task_id, reason="SESSION_NOT_FOUND")
    updated = store.reassign_task(task_id, "lane-b", reason="source session permanently offline", actor="operator")
    assert updated.session == "lane-b"
    assert updated.status == QUEUED


# ---------------------------------------------------------------------------
# Item 12: race safety -- a concurrent claim wins, reassign fails clean.
# ---------------------------------------------------------------------------

def test_reassign_fails_clean_if_dispatcher_already_claimed_the_task(store):
    task_id = _make_task(store)
    store.claim_next_task("lane-a", claimed_by="engine-1")  # QUEUED -> PRECHECK
    with pytest.raises(TaskAlreadyClaimedError):
        store.reassign_task(task_id, "lane-b", reason="test", actor="op")
    assert store.get_task(task_id).session == "lane-a"
    assert store.get_task(task_id).status == PRECHECK  # claim wins, untouched by the failed reassign


def test_reassign_is_race_free_under_concurrent_claim_attempts(store):
    """The exact item 12 scenario: a claim and a reassign racing for the
    same task -- exactly one of them may succeed, never both, never a
    corrupted intermediate state."""
    task_id = _make_task(store)
    results = {"claim": None, "reassign": None, "reassign_error": None}

    def do_claim():
        results["claim"] = store.claim_next_task("lane-a", claimed_by="engine-1")

    def do_reassign():
        try:
            results["reassign"] = store.reassign_task(task_id, "lane-b", reason="race test", actor="op")
        except TaskAlreadyClaimedError as exc:
            results["reassign_error"] = str(exc)

    t1 = threading.Thread(target=do_claim)
    t2 = threading.Thread(target=do_reassign)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    task = store.get_task(task_id)
    if results["claim"] is not None:
        # Claim won -- reassign must have failed clean.
        assert results["reassign"] is None
        assert results["reassign_error"] is not None
        assert task.status == PRECHECK
        assert task.session == "lane-a"
    else:
        # Reassign won -- claim must have found nothing (task moved to lane-b).
        assert results["reassign"] is not None
        assert task.session == "lane-b"
        assert task.status == QUEUED


# ---------------------------------------------------------------------------
# Item 13: restart-safe -- reassign is a plain durable SQLite write, no
# claim/lease of its own to lose; a crash mid-reassign either committed
# (fully applied, atomically) or never happened (BEGIN IMMEDIATE).
# ---------------------------------------------------------------------------

def test_reassign_survives_a_simulated_restart(tmp_path):
    db_path = tmp_path / "queue.db"
    store1 = QueueStore(db_path)
    task_id = (store1.set_tasks("lane-a", [{"prompt": "a"}]))[0]
    store1.reassign_task(task_id, "lane-b", reason="test", actor="op")

    store2 = QueueStore(db_path)  # fresh instance, same file -- "process restarted"
    task = store2.get_task(task_id)
    assert task.session == "lane-b"
    assert task.original_owner == "lane-a"
    assert len(task.migration_history) == 1


# ---------------------------------------------------------------------------
# Item 8: AT_RISK marking -- informational only, never a state transition.
# ---------------------------------------------------------------------------

def test_mark_at_risk_never_changes_task_status(store):
    task_id = _make_task(store)
    store.transition_task(task_id, DISPATCHING, event_type="TEST")
    store.transition_task(task_id, RUNNING, event_type="TEST")
    updated = store.mark_at_risk(task_id)
    assert updated.at_risk is True
    assert updated.status == RUNNING  # untouched
    cleared = store.mark_at_risk(task_id, at_risk=False)
    assert cleared.at_risk is False


# ---------------------------------------------------------------------------
# Item 11: per-project boundary + cooldown clock.
# ---------------------------------------------------------------------------

def test_set_lane_project_and_default_none_never_matches_another_none_by_mistake(store):
    store.set_lane_project("lane-a", "proj-x")
    store.set_lane_project("lane-b", "proj-y")
    assert store.lane_status("lane-a")["project"] == "proj-x"
    assert store.lane_status("lane-b")["project"] == "proj-y"
    assert store.lane_status("lane-c")["project"] is None  # never configured


def test_mark_rebalanced_stamps_the_cooldown_clock(store):
    assert store.lane_status("lane-a")["last_rebalance_at"] is None
    store.mark_rebalanced("lane-a")
    assert store.lane_status("lane-a")["last_rebalance_at"] is not None
