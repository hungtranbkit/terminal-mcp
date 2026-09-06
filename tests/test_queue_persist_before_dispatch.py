"""P0 persist-before-dispatch (task: "vấn đề thực tế là prompt từ
ChatGPT đang được gửi trực tiếp như message nên rất dễ miss khi Claude/
session đang bận"). Covers the store-level additions (DISPATCH_
UNCERTAIN/WAITING_SESSION state machine, queue_position, metrics) and
the engine-level wiring. Pure in-process, no real session -- see
test_queue_engine_smoke.py for the real-tmux equivalent.

SAFETY: every session name here is a disposable fixture."""
from __future__ import annotations

import pytest

from terminal_mcp.coordinator import CoordinatorGate, RepoEvidence
from terminal_mcp.queue_engine import QueueEngine, SESSION_UNREACHABLE_ERRORS
from terminal_mcp.queue_store import (
    DISPATCH_UNCERTAIN, PAUSED, QUEUED, RUNNING, WAITING_SESSION, QueueStore, is_valid_transition,
)


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _make_task(store, session="lane-a", **overrides):
    task = {"prompt": "do the real work carefully"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    return task_id


# ---------------------------------------------------------------------------
# Item 1/8: persist-before-dispatch itself -- the durable row exists the
# instant enqueue returns, and TASK_ACCEPTED carries a real queue_position.
# ---------------------------------------------------------------------------

def test_enqueue_creates_a_durable_row_before_anything_is_dispatched(store):
    task_id = _make_task(store)
    # The row is already there, fully durable -- no dispatch has
    # happened, nothing has been sent, yet get_task already finds it.
    task = store.get_task(task_id)
    assert task is not None
    assert task.status == QUEUED


def test_queue_position_reflects_real_fifo_order(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])
    assert store.queue_position(ids[0]) == 1
    assert store.queue_position(ids[1]) == 2
    assert store.queue_position(ids[2]) == 3


def test_queue_position_accounts_for_priority(store):
    ids = store.set_tasks("lane-a", [{"prompt": "normal", "priority": 0}, {"prompt": "urgent", "priority": 10}])
    assert store.queue_position(ids[1]) == 1  # urgent jumps ahead
    assert store.queue_position(ids[0]) == 2


def test_queue_position_is_none_once_no_longer_queued(store):
    task_id = _make_task(store)
    store.claim_next_task("lane-a", claimed_by="engine-1")
    assert store.queue_position(task_id) is None


# ---------------------------------------------------------------------------
# Item 2: DELIVERY_UNKNOWN -> DISPATCH_UNCERTAIN, never a silent re-QUEUED
# task, never a blind resend.
# ---------------------------------------------------------------------------

def test_dispatch_uncertain_is_a_valid_state_and_never_silently_looks_queued(store):
    assert is_valid_transition("DISPATCHING", DISPATCH_UNCERTAIN) is True
    task_id = _make_task(store)
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    updated = store.mark_dispatch_uncertain(task_id, reason="delivery_state=DELIVERY_UNKNOWN")
    assert updated.status == DISPATCH_UNCERTAIN
    assert updated.uncertain_or_waiting_since is not None


def test_dispatch_uncertain_does_not_occupy_the_lane_forever_and_blocks_new_claims(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    store.transition_task(ids[0], "DISPATCHING", event_type="TEST")
    store.mark_dispatch_uncertain(ids[0], reason="test")
    assert store.claim_next_task("lane-a", claimed_by="e") is None  # b must wait


def test_reconcile_uncertain_and_waiting_falls_back_to_queued_after_grace_period(store):
    task_id = _make_task(store)
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store.mark_dispatch_uncertain(task_id, reason="test")
    with store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET uncertain_or_waiting_since = '2000-01-01T00:00:00Z' "
                          "WHERE id = ?", (task_id,))
    reconciled = store.reconcile_uncertain_and_waiting("lane-a")
    assert task_id in reconciled
    task = store.get_task(task_id)
    assert task.status == QUEUED
    assert task.uncertain_or_waiting_since is None


def test_reconcile_uncertain_and_waiting_never_touches_a_task_still_within_grace(store):
    task_id = _make_task(store)
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store.mark_dispatch_uncertain(task_id, reason="test")  # timestamped "now"
    reconciled = store.reconcile_uncertain_and_waiting("lane-a", grace_seconds=3600)
    assert reconciled == []
    assert store.get_task(task_id).status == DISPATCH_UNCERTAIN


# ---------------------------------------------------------------------------
# Item 9: session/node unreachable -> WAITING_SESSION, never dropped,
# never a coordinator/execution failure.
# ---------------------------------------------------------------------------

def test_waiting_session_is_a_valid_state_from_precheck_running_and_verifying(store):
    for status in ("PRECHECK", RUNNING, "VERIFYING"):
        assert is_valid_transition(status, WAITING_SESSION) is True


def test_mark_waiting_session_never_drops_the_task(store):
    task_id = _make_task(store)
    store.transition_task(task_id, "PRECHECK", event_type="TEST")
    updated = store.mark_waiting_session(task_id, reason="SESSION_NOT_FOUND")
    assert updated.status == WAITING_SESSION
    assert store.get_task(task_id) is not None  # still there, durably


def test_waiting_session_only_outgoing_edge_is_queued(store):
    assert is_valid_transition(WAITING_SESSION, QUEUED) is True
    assert is_valid_transition(WAITING_SESSION, RUNNING) is False
    assert is_valid_transition(WAITING_SESSION, "PRECHECK") is False


def test_reconcile_recovers_a_waiting_session_task_after_grace_period(store):
    task_id = _make_task(store)
    store.transition_task(task_id, "PRECHECK", event_type="TEST")
    store.mark_waiting_session(task_id, reason="NODE_UNREACHABLE")
    with store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET uncertain_or_waiting_since = '2000-01-01T00:00:00Z' "
                          "WHERE id = ?", (task_id,))
    reconciled = store.reconcile_uncertain_and_waiting("lane-a")
    assert task_id in reconciled
    assert store.get_task(task_id).status == QUEUED


def test_pausing_a_lane_pauses_a_dispatch_uncertain_or_waiting_session_task_too(store):
    task_id = _make_task(store)
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store.mark_dispatch_uncertain(task_id, reason="test")
    store.pause_lane("lane-a", reason="manual")
    task = store.get_task(task_id)
    assert task.status == PAUSED
    assert task.paused_from_status == DISPATCH_UNCERTAIN
    store.resume_lane("lane-a")
    assert store.get_task(task_id).status == DISPATCH_UNCERTAIN


# ---------------------------------------------------------------------------
# Item 14: metrics -- missed/dropped always 0, structural.
# ---------------------------------------------------------------------------

def test_metrics_reports_zero_missed_and_dropped_by_construction(store):
    store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    metrics = store.metrics("lane-a")
    assert metrics["missed_count"] == 0
    assert metrics["dropped_count"] == 0
    assert metrics["queued_depth"] == 2


def test_oldest_queued_age_of_a_just_enqueued_task_is_near_zero_not_a_timezone_offset(store):
    """Regression test for a real bug found in this same task: age
    computations that mix time.mktime (assumes LOCAL time) with a UTC-
    stamped ('...Z') timestamp are off by the host's own UTC offset --
    on this dev host, exactly +25200s (UTC+7). A task enqueued THIS
    instant must report an age of a few seconds at most, never
    thousands."""
    store.set_tasks("lane-a", [{"prompt": "a"}])
    metrics = store.metrics("lane-a")
    assert metrics["oldest_queued_age_seconds"] < 5


def test_metrics_counts_uncertain_and_waiting_session(store):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    store.transition_task(ids[0], "DISPATCHING", event_type="TEST")
    store.mark_dispatch_uncertain(ids[0], reason="test")
    store.transition_task(ids[1], "PRECHECK", event_type="TEST")
    store.mark_waiting_session(ids[1], reason="SESSION_NOT_FOUND")
    metrics = store.metrics("lane-a")
    assert metrics["dispatch_uncertain_count"] == 1
    assert metrics["waiting_session_count"] == 1
    assert metrics["queued_depth"] == 0


def test_ten_sequential_enqueues_into_one_busy_lane_never_miss_or_reorder(store):
    """Item 13's own '10 prompts liên tiếp vào 1 busy session không miss/
    reorder' -- pure persistence-layer proof (the real-session dispatch
    ordering is already covered by test_queue_engine_smoke.py)."""
    prompts = [f"task {i}" for i in range(10)]
    ids = []
    for prompt in prompts:
        (task_id,) = store.append_tasks("lane-a", [{"prompt": prompt}])
        ids.append(task_id)
    tasks = store.lane_status("lane-a")["tasks"]
    assert [t["prompt"] for t in tasks] == prompts  # exact order preserved
    assert len(set(ids)) == 10  # all distinct, none dropped/merged


# ---------------------------------------------------------------------------
# Engine-level: WAITING_SESSION / DISPATCH_UNCERTAIN wiring.
# ---------------------------------------------------------------------------

class FakeOps:
    def __init__(self):
        self.status_by_session = {}
        self.sent = []
        self._sent_by_key = {}

    def set_status(self, session, response):
        self.status_by_session[session] = response

    def terminal_status(self, session):
        return self.status_by_session.get(session, {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})

    def terminal_tail(self, session, lines=None):
        return {"output": ""}

    def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
        idempotency_key = kwargs.get("idempotency_key")
        if idempotency_key and idempotency_key in self._sent_by_key:
            return self._sent_by_key[idempotency_key]
        self.sent.append({"session": session, "text": text, "idempotency_key": idempotency_key})
        response = {"sent": True, "delivery_state": "SUBMIT_CONFIRMED"}
        if idempotency_key:
            self._sent_by_key[idempotency_key] = response
        return response


def _always_ready_gate():
    return CoordinatorGate(evidence_collector=lambda cwd: RepoEvidence(branch="main", head="x", clean=True,
                                                                       status_lines=()))


def test_engine_review_moves_to_waiting_session_on_session_not_found(store):
    _make_task(store)
    ops = FakeOps()
    ops.set_status("lane-a", {"error": "SESSION_NOT_FOUND"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")  # CLAIMED
    result = engine.tick("lane-a")  # review -> session unreachable
    assert result.action == "WAITING_SESSION"


def test_engine_recovers_waiting_session_once_session_resolves_again(store):
    task_id = _make_task(store)
    ops = FakeOps()
    ops.set_status("lane-a", {"error": "SESSION_NOT_FOUND"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")
    engine.tick("lane-a")
    assert store.get_task(task_id).status == WAITING_SESSION

    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    result = engine.tick("lane-a")
    assert result.action == "QUEUED"
    assert store.get_task(task_id).status == QUEUED


def test_engine_dispatch_uncertain_recovers_to_running_on_confirmed_activity(store):
    task_id = _make_task(store)
    ops = FakeOps()
    ops.set_status("lane-a", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})

    class UnknownOps(FakeOps):
        def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
            return {"sent": True, "delivery_state": "DELIVERY_UNKNOWN"}

    uncertain_ops = UnknownOps()
    uncertain_ops.status_by_session = ops.status_by_session
    engine = QueueEngine(store, uncertain_ops, coordinator=_always_ready_gate())
    engine.tick("lane-a")  # CLAIMED
    engine.tick("lane-a")  # READY
    dispatch_result = engine.tick("lane-a")  # DISPATCH_UNCERTAIN
    assert dispatch_result.action == "DISPATCH_UNCERTAIN"

    uncertain_ops.set_status("lane-a", {"state": "RUNNING", "node_id": "local", "cwd": "/repo/a"})
    recovered = engine.tick("lane-a")
    assert recovered.action == "RUNNING"
    assert store.get_task(task_id).status == RUNNING


def test_session_unreachable_errors_is_a_narrow_deliberate_set():
    assert "SESSION_NOT_FOUND" in SESSION_UNREACHABLE_ERRORS
    assert "NODE_UNREACHABLE" in SESSION_UNREACHABLE_ERRORS
    assert "AMBIGUOUS_SESSION" in SESSION_UNREACHABLE_ERRORS
    assert "ACCESS_DENIED" not in SESSION_UNREACHABLE_ERRORS  # a different kind of error, left to NEEDS_HUMAN/FAILED


# ---------------------------------------------------------------------------
# Item 6/13: restart recovery for DISPATCH_UNCERTAIN/WAITING_SESSION,
# same simulated-restart technique as test_queue_store_phase2.py's own
# reconcile_stale_claims test.
# ---------------------------------------------------------------------------

def test_restart_recovers_a_dispatch_uncertain_task_after_grace_period(tmp_path):
    db_path = tmp_path / "queue.db"
    store1 = QueueStore(db_path)
    task_id = (store1.set_tasks("lane-a", [{"prompt": "a"}]))[0]
    store1.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store1.mark_dispatch_uncertain(task_id, reason="test")
    with store1._connection() as connection:
        connection.execute("UPDATE queue_tasks SET uncertain_or_waiting_since = '2000-01-01T00:00:00Z' "
                          "WHERE id = ?", (task_id,))

    store2 = QueueStore(db_path)  # fresh instance, same file -- "process restarted"
    reconciled = store2.reconcile_uncertain_and_waiting()
    assert task_id in reconciled
    assert store2.get_task(task_id).status == QUEUED
    # And it's cleanly re-claimable from here on -- no double-dispatch,
    # no drop.
    reclaimed = store2.claim_next_task("lane-a", claimed_by="engine-after-restart")
    assert reclaimed.id == task_id


def test_restart_recovers_a_waiting_session_task_after_grace_period(tmp_path):
    db_path = tmp_path / "queue.db"
    store1 = QueueStore(db_path)
    task_id = (store1.set_tasks("lane-a", [{"prompt": "a"}]))[0]
    store1.transition_task(task_id, "PRECHECK", event_type="TEST")
    store1.mark_waiting_session(task_id, reason="NODE_UNREACHABLE")
    with store1._connection() as connection:
        connection.execute("UPDATE queue_tasks SET uncertain_or_waiting_since = '2000-01-01T00:00:00Z' "
                          "WHERE id = ?", (task_id,))

    store2 = QueueStore(db_path)
    reconciled = store2.reconcile_uncertain_and_waiting()
    assert task_id in reconciled
    assert store2.get_task(task_id).status == QUEUED
    reclaimed = store2.claim_next_task("lane-a", claimed_by="engine-after-restart")
    assert reclaimed.id == task_id
