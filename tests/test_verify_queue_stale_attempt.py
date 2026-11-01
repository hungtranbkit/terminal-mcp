"""A verify job belongs to ONE attempt of its task (#106 P0 hardening).

A task can leave VERIFYING (watchdog BLOCKED, operator FAIL), be retried and
come back to VERIFYING at attempt N+1 before any reconcile ran. The attempt-N
job is then still open, and reconcile used to skip it forever because the
task is VERIFYING again. Because claim_next hands out the OLDEST pending job,
the stale job was claimed first and its PASS completed attempt N+1 with a
verdict for different code -- unblocking dependants on unverified work.
"""
from __future__ import annotations

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.verify_queue import (
    VERIFIED_FAIL, VERIFIED_PASS, VERIFY_BLOCKED, VERIFY_CANCELLED, VERIFY_PENDING, VerifyQueue,
)

from .test_verify_queue import make_running_task

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def store(tmp_path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def verify(store) -> VerifyQueue:
    return VerifyQueue(store)


def _evidence(sha):
    return {"command": "pytest -q", "exit_code": 0, "tests_passed": 3, "tests_failed": 0,
            "commit_sha": sha}


def _retry_back_to_verifying(store, verify, task_id):
    """Attempt 1 leaves VERIFYING by an ordinary route, is retried and
    reaches VERIFYING again at attempt 2 -- no reconcile in between."""
    store.transition_task(task_id, qs.BLOCKED, event_type="BLOCKED", reason="watchdog")
    store.retry_task(task_id)
    store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHING")
    retried = store.mark_running_with_evidence(
        task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    assert retried.attempt_count == 2
    return verify.ensure_verify_job(retried, commit_sha=NEW, require_commit_evidence=True)


def _stale_setup(store, verify, *, claim_first: bool):
    task = make_running_task(store)
    job1 = verify.ensure_verify_job(task, commit_sha=OLD, require_commit_evidence=True)
    held = verify.claim_next(verifier="qa", capabilities=[]) if claim_first else None
    job2 = _retry_back_to_verifying(store, verify, task.id)
    assert job2.id != job1.id and job2.attempt == 2
    return task, job1, held, job2


def test_stale_holder_pass_cannot_complete_the_newer_attempt(store, verify):
    task, job1, held, job2 = _stale_setup(store, verify, claim_first=True)
    result = verify.complete(job1.id, held.claim_token, evidence=_evidence(OLD))
    assert result["ok"] is False and result["error"] == "STALE_ATTEMPT"
    assert store.get_task(task.id).status == qs.VERIFYING
    assert store.get_task(task.id).verification_evidence in (None, {}, "")
    assert verify.get(job1.id).status != VERIFIED_PASS
    assert verify.get(job2.id).status == VERIFY_PENDING


def test_stale_holder_fail_cannot_fail_the_newer_attempt(store, verify):
    task, job1, held, _ = _stale_setup(store, verify, claim_first=True)
    result = verify.fail(job1.id, held.claim_token, result=VERIFIED_FAIL,
                         failure_summary={"headline": "old code broke"})
    assert result["ok"] is False and result["error"] == "STALE_ATTEMPT"
    assert store.get_task(task.id).status == qs.VERIFYING


def test_claim_next_never_hands_out_a_superseded_job(store, verify):
    _, job1, _, job2 = _stale_setup(store, verify, claim_first=False)
    claimed = verify.claim_next(verifier="qa", capabilities=[])
    assert claimed is not None and claimed.id == job2.id
    assert verify.claim_next(verifier="qa2", capabilities=[], job_id=job1.id) is None


def test_reconcile_cancels_a_superseded_job_and_leaves_the_current_one(store, verify):
    _, job1, _, job2 = _stale_setup(store, verify, claim_first=False)
    report = verify.reconcile()
    assert verify.get(job1.id).status == VERIFY_CANCELLED
    assert "superseded" in verify.get(job1.id).history[-1]["reason"]
    assert verify.get(job2.id).status == VERIFY_PENDING
    assert {c["job_id"] for c in report["jobs_closed"]} == {job1.id}
    assert verify.reconcile()["closed_count"] == 0   # idempotent


def test_reconcile_never_passes_a_superseded_job_from_the_newer_attempts_evidence(store, verify):
    task, job1, _, job2 = _stale_setup(store, verify, claim_first=False)
    claimed = verify.claim_next(verifier="qa", capabilities=[])
    assert verify.complete(job2.id, claimed.claim_token, evidence=_evidence(NEW))["ok"] is True
    verify.reconcile()
    assert verify.get(job2.id).status == VERIFIED_PASS
    assert verify.get(job1.id).status == VERIFY_CANCELLED
    assert verify.get(job1.id).evidence in (None, {})


def test_requeued_blocked_job_from_an_old_attempt_is_not_claimable(store, verify):
    task = make_running_task(store)
    job1 = verify.ensure_verify_job(task, commit_sha=OLD, require_commit_evidence=True)
    held = verify.claim_next(verifier="qa", capabilities=[])
    verify.fail(job1.id, held.claim_token, result=VERIFY_BLOCKED,
                failure_summary={"headline": "device offline"})
    store.retry_task(task.id)
    store.transition_task(task.id, qs.DISPATCHING, event_type="DISPATCHING")
    retried = store.mark_running_with_evidence(
        task.id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    job2 = verify.ensure_verify_job(retried, commit_sha=NEW, require_commit_evidence=True)
    verify.requeue(job1.id, actor="op", reason="device is back")
    claimed = verify.claim_next(verifier="qa", capabilities=[])
    assert claimed.id == job2.id
