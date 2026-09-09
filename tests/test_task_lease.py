"""P0.4 Task lease API -- renew / release / handoff.

claim_next_task already stamped claim_token + lease_expires_at
atomically. What was missing is everything AFTER the claim. Note
reconcile_stale_claims' own docstring already assumed "a healthy engine
keeps renewing ... well within the lease" -- renew_task_lease is the
method that assumption was written against and which did not exist.

The invariant every verb shares: only the CURRENT claim_token may act. A
holder whose lease expired and was reclaimed must never be able to renew,
release or hand off the NEW holder's work.
"""
from __future__ import annotations

import time

import pytest

from terminal_mcp.queue_store import (DISPATCHING, PRECHECK, QUEUED, QueueStore,
                                      TaskAlreadyClaimedError)


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def claimed(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "work"}])
    task = store.claim_next_task("s1", claimed_by="worker-A")
    return task_id, task


# ------------------------------------------------------------------ renew
def test_renew_extends_the_lease(store, claimed):
    task_id, task = claimed
    renewed = store.renew_task_lease(task_id, task.claim_token, lease_seconds=600)
    assert renewed is not None
    assert renewed.lease_expires_at > task.lease_expires_at
    assert renewed.claim_token == task.claim_token      # same claim, longer lease


def test_renew_requires_the_current_token(store, claimed):
    task_id, _ = claimed
    assert store.renew_task_lease(task_id, "not-the-token") is None


def test_renew_on_an_unclaimed_task_returns_none(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "x"}])
    assert store.renew_task_lease(task_id, "anything") is None


def test_renew_keeps_a_worker_safe_from_reconciliation(store, claimed):
    """The whole point: an alive worker on a long task must not be
    reconciled out from under itself."""
    task_id, task = claimed
    store.renew_task_lease(task_id, task.claim_token, lease_seconds=600)
    assert store.reconcile_stale_claims("s1") == []
    assert store.get_task(task_id).status == PRECHECK


def test_an_unrenewed_expired_claim_IS_reconciled(store):
    """Contrast case -- proves the renew test above is meaningful."""
    [task_id] = store.append_tasks("s1", [{"prompt": "x"}])
    store.claim_next_task("s1", claimed_by="crashed", lease_seconds=-1)
    assert store.reconcile_stale_claims("s1") == [task_id]
    assert store.get_task(task_id).status == QUEUED


# ---------------------------------------------------------------- release
def test_release_returns_the_task_to_queued_and_clears_the_claim(store, claimed):
    task_id, task = claimed
    released = store.release_task_claim(task_id, task.claim_token, reason="stepping back")
    assert released.status == QUEUED
    assert released.claim_token is None and released.claimed_by is None
    assert released.lease_expires_at is None


def test_a_released_task_is_immediately_reclaimable(store, claimed):
    task_id, task = claimed
    store.release_task_claim(task_id, task.claim_token)
    again = store.claim_next_task("s1", claimed_by="worker-B")
    assert again is not None and again.id == task_id and again.claimed_by == "worker-B"


def test_release_requires_the_current_token(store, claimed):
    task_id, _ = claimed
    assert store.release_task_claim(task_id, "wrong") is None
    assert store.get_task(task_id).status == PRECHECK      # untouched


def test_release_looks_like_reconciliation_downstream(store):
    """A released task and a crash-reconciled one must be
    indistinguishable, so nothing downstream needs to care which
    happened."""
    [a] = store.append_tasks("s1", [{"prompt": "a"}])
    task = store.claim_next_task("s1", claimed_by="w")
    store.release_task_claim(a, task.claim_token)
    released = store.get_task(a)

    [b] = store.append_tasks("s2", [{"prompt": "b"}])
    store.claim_next_task("s2", claimed_by="crashed", lease_seconds=-1)
    store.reconcile_stale_claims("s2")
    reconciled = store.get_task(b)

    shape = lambda t: (t.status, t.claimed_by, t.claim_token, t.lease_expires_at)
    assert shape(released) == shape(reconciled)


# ---------------------------------------------------------------- handoff
def test_handoff_transfers_the_claim_without_requeueing(store, claimed):
    task_id, task = claimed
    out = store.handoff_task(task_id, task.claim_token, to_worker="verifier-B",
                             reason="ready to verify")
    assert out.claimed_by == "verifier-B"
    assert out.status == PRECHECK              # never went back to QUEUED
    assert out.id == task_id                   # same task, not a new one


def test_handoff_issues_a_fresh_token_and_invalidates_the_old(store, claimed):
    task_id, task = claimed
    out = store.handoff_task(task_id, task.claim_token, to_worker="B", reason="r")
    assert out.claim_token != task.claim_token
    assert store.renew_task_lease(task_id, task.claim_token) is None     # old holder locked out
    assert store.renew_task_lease(task_id, out.claim_token) is not None  # new holder works


def test_handoff_preserves_identity_and_appends_history(store, claimed):
    task_id, task = claimed
    before = store.get_task(task_id)
    out = store.handoff_task(task_id, task.claim_token, to_worker="B", reason="ready to verify")
    assert (out.prompt, out.attempt_count, out.priority) == (before.prompt, before.attempt_count,
                                                             before.priority)
    entry = out.migration_history[-1]
    assert entry["event"] == "handoff"
    assert entry["from_worker"] == "worker-A" and entry["to_worker"] == "B"
    assert entry["reason"] == "ready to verify"


def test_handoff_keeps_the_lane_unless_asked_to_move_it(store, claimed):
    task_id, task = claimed
    same = store.handoff_task(task_id, task.claim_token, to_worker="B", reason="r")
    assert same.session == "s1"
    moved = store.handoff_task(task_id, same.claim_token, to_worker="C",
                               to_session="s2", reason="move lane too")
    assert moved.session == "s2"


def test_handoff_requires_the_current_token(store, claimed):
    task_id, _ = claimed
    assert store.handoff_task(task_id, "wrong", to_worker="B", reason="r") is None


def test_handoff_differs_from_reassign_which_refuses_a_claimed_task(store, claimed):
    """reassign_task deliberately refuses an actively-claimed task; that
    is exactly the case handoff exists for."""
    task_id, task = claimed
    with pytest.raises(TaskAlreadyClaimedError):
        store.reassign_task(task_id, "s2", reason="r", actor="test")
    assert store.handoff_task(task_id, task.claim_token, to_worker="B", reason="r") is not None


# ------------------------------------------------------------- observability
def test_lease_holder_reports_without_leaking_the_token(store, claimed):
    task_id, task = claimed
    holder = store.lease_holder(task_id)
    assert holder["claimed_by"] == "worker-A" and holder["status"] == PRECHECK
    assert "claim_token" not in holder, "the token is a capability, not an observability field"


def test_lease_holder_is_none_when_unclaimed(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "x"}])
    assert store.lease_holder(task_id) is None


# ------------------------------------------------------------- concurrency
def test_two_workers_cannot_both_hold_the_same_task(store):
    [task_id] = store.append_tasks("s1", [{"prompt": "x"}])
    first = store.claim_next_task("s1", claimed_by="A")
    assert store.claim_next_task("s1", claimed_by="B") is None
    # ...and only the real holder can act on it
    assert store.release_task_claim(task_id, "guessed-token") is None
    assert store.release_task_claim(task_id, first.claim_token) is not None
