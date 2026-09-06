"""IntegrationStore -- persistence + state machine for the Integration
Agent's per-project merge/test pipeline (task: "3-role model: Coding
A/B + Integration Agent"). Pure in-process tests, no real git/subprocess
involved -- see test_integration_engine.py for the real-git-repo tests.

SAFETY: every project/session name here is a disposable fixture."""
from __future__ import annotations

import threading
import time

import pytest

from terminal_mcp.integration_store import (
    BLOCKED, CLAIMED, INTEGRATED, MERGE_READY, MERGING, READY_FOR_INTEGRATION, REGRESSION_FAILED,
    REGRESSION_PENDING, REGRESSION_RUNNING, REWORK_REQUIRED, TARGETED_TEST, InvalidHandoffTransitionError,
    IntegrationStore, is_valid_batch_transition, is_valid_handoff_transition,
)


@pytest.fixture
def store(tmp_path):
    return IntegrationStore(tmp_path / "integration.db")


def _configure(store, project="proj-a", **overrides):
    kwargs = {"repo_path": "/tmp/does-not-matter"}
    kwargs.update(overrides)
    return store.configure_pipeline(project, **kwargs)


def _publish(store, project="proj-a", **overrides):
    kwargs = {"task_id": "task-1", "origin_session": "lane-a", "branch": "feature/x",
             "commit_sha": "abc123", "base_sha": "base000", "changed_paths": ["src/a.py"]}
    kwargs.update(overrides)
    return store.publish_handoff(project=project, **kwargs)


# ---------------------------------------------------------------------------
# State machine.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("from_status,to_status", [
    (READY_FOR_INTEGRATION, CLAIMED), (CLAIMED, MERGING), (CLAIMED, READY_FOR_INTEGRATION),
    (MERGING, TARGETED_TEST), (MERGING, REWORK_REQUIRED), (MERGING, BLOCKED), (MERGING, READY_FOR_INTEGRATION),
    (TARGETED_TEST, INTEGRATED), (TARGETED_TEST, REWORK_REQUIRED), (TARGETED_TEST, BLOCKED),
    (REWORK_REQUIRED, READY_FOR_INTEGRATION), (BLOCKED, READY_FOR_INTEGRATION),
])
def test_valid_handoff_transitions(from_status, to_status):
    assert is_valid_handoff_transition(from_status, to_status) is True


@pytest.mark.parametrize("from_status,to_status", [
    (READY_FOR_INTEGRATION, MERGING), (READY_FOR_INTEGRATION, INTEGRATED),
    (INTEGRATED, READY_FOR_INTEGRATION), (INTEGRATED, CLAIMED),
    (REWORK_REQUIRED, INTEGRATED), (BLOCKED, INTEGRATED),
])
def test_invalid_handoff_transitions(from_status, to_status):
    assert is_valid_handoff_transition(from_status, to_status) is False


def test_integrated_is_terminal():
    for status in (READY_FOR_INTEGRATION, CLAIMED, MERGING, TARGETED_TEST, REWORK_REQUIRED, BLOCKED):
        assert is_valid_handoff_transition(INTEGRATED, status) is False


@pytest.mark.parametrize("from_status,to_status", [
    (REGRESSION_PENDING, REGRESSION_RUNNING), (REGRESSION_RUNNING, MERGE_READY),
    (REGRESSION_RUNNING, REGRESSION_FAILED), (REGRESSION_FAILED, REGRESSION_PENDING),
])
def test_valid_batch_transitions(from_status, to_status):
    assert is_valid_batch_transition(from_status, to_status) is True


def test_merge_ready_is_terminal_for_the_state_machine_promotion_is_separate():
    assert is_valid_batch_transition(MERGE_READY, REGRESSION_PENDING) is False


# ---------------------------------------------------------------------------
# publish_handoff: immutable provenance.
# ---------------------------------------------------------------------------

def test_publish_handoff_creates_a_ready_for_integration_row(store):
    _configure(store)
    handoff = _publish(store)
    assert handoff.status == READY_FOR_INTEGRATION
    assert handoff.branch == "feature/x"
    assert handoff.commit_sha == "abc123"
    assert handoff.changed_paths == ("src/a.py",)


def test_transition_never_touches_provenance_fields(store):
    _configure(store)
    handoff = store.publish_handoff(project="proj-a", task_id="t1", origin_session="lane-a", branch="feature/x",
                                    commit_sha="abc123", base_sha="base000", changed_paths=["src/a.py"])
    store.claim_next_handoff("proj-a", claimed_by="engine-1")
    updated = store.get_handoff(handoff.id)
    assert updated.branch == "feature/x"
    assert updated.commit_sha == "abc123"
    assert updated.task_id == "t1"
    assert updated.origin_session == "lane-a"


# ---------------------------------------------------------------------------
# claim_next_handoff: one-at-a-time, atomic under real concurrency.
# ---------------------------------------------------------------------------

def test_claim_next_handoff_moves_to_claimed_and_stamps_lease(store):
    _configure(store)
    handoff = _publish(store)
    claimed = store.claim_next_handoff("proj-a", claimed_by="engine-1", lease_seconds=60)
    assert claimed.id == handoff.id
    assert claimed.status == CLAIMED
    assert claimed.claimed_by == "engine-1"
    assert claimed.lease_expires_at is not None


def test_claim_next_handoff_none_when_nothing_ready(store):
    _configure(store)
    assert store.claim_next_handoff("proj-a", claimed_by="engine-1") is None


def test_claim_next_handoff_none_when_paused(store):
    _configure(store)
    _publish(store)
    store.pause_pipeline("proj-a", reason="test")
    assert store.claim_next_handoff("proj-a", claimed_by="engine-1") is None


def test_claim_next_handoff_one_at_a_time_per_project(store):
    _configure(store)
    h1 = _publish(store, task_id="t1", commit_sha="sha1")
    h2 = _publish(store, task_id="t2", commit_sha="sha2")
    first = store.claim_next_handoff("proj-a", claimed_by="engine-1")
    assert first.id == h1.id
    assert store.claim_next_handoff("proj-a", claimed_by="engine-1") is None


def test_projects_are_completely_independent(store):
    _configure(store, project="proj-a")
    _configure(store, project="proj-b")
    _publish(store, project="proj-a")
    hb = _publish(store, project="proj-b")
    store.pause_pipeline("proj-a", reason="test")
    assert store.claim_next_handoff("proj-a", claimed_by="e") is None
    claimed_b = store.claim_next_handoff("proj-b", claimed_by="e")
    assert claimed_b.id == hb.id


def test_claim_next_handoff_is_race_free_under_concurrent_callers(store):
    _configure(store)
    for i in range(20):
        _publish(store, task_id=f"t{i}", commit_sha=f"sha{i}")
    results: list = []
    lock = threading.Lock()

    def worker(worker_id):
        for _ in range(30):
            claimed = store.claim_next_handoff("proj-a", claimed_by=f"worker-{worker_id}")
            if claimed is not None:
                with lock:
                    results.append(claimed.id)
            time.sleep(0.001)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == len(set(results))
    assert len(results) <= 1  # only one handoff can ever be active per project in this test


# ---------------------------------------------------------------------------
# reconcile_stale_handoff_claims: restart safety.
# ---------------------------------------------------------------------------

def test_reconcile_recovers_an_expired_lease(store):
    _configure(store)
    handoff = _publish(store)
    store.claim_next_handoff("proj-a", claimed_by="dead", lease_seconds=-1)
    reconciled = store.reconcile_stale_handoff_claims("proj-a")
    assert handoff.id in reconciled
    assert store.get_handoff(handoff.id).status == READY_FOR_INTEGRATION


def test_reconcile_never_touches_a_healthy_lease(store):
    _configure(store)
    handoff = _publish(store)
    store.claim_next_handoff("proj-a", claimed_by="alive", lease_seconds=300)
    assert store.reconcile_stale_handoff_claims("proj-a") == []
    assert store.get_handoff(handoff.id).status == CLAIMED


def test_reconcile_survives_a_simulated_restart(tmp_path):
    db_path = tmp_path / "integration.db"
    store1 = IntegrationStore(db_path)
    _configure(store1)
    handoff = _publish(store1)
    store1.claim_next_handoff("proj-a", claimed_by="before-restart", lease_seconds=-1)

    store2 = IntegrationStore(db_path)  # fresh instance, same file -- "process restarted"
    reconciled = store2.reconcile_stale_handoff_claims()
    assert handoff.id in reconciled
    reclaimed = store2.claim_next_handoff("proj-a", claimed_by="after-restart")
    assert reclaimed.id == handoff.id


# ---------------------------------------------------------------------------
# retry_handoff
# ---------------------------------------------------------------------------

def test_retry_handoff_from_rework_required(store):
    _configure(store)
    handoff = _publish(store)
    store.claim_next_handoff("proj-a", claimed_by="e")
    store.transition_handoff(handoff.id, MERGING, event_type="TEST")
    store.transition_handoff(handoff.id, REWORK_REQUIRED, event_type="TEST", reason="conflict")
    retried = store.retry_handoff(handoff.id)
    assert retried.status == READY_FOR_INTEGRATION
    assert retried.claimed_by is None


def test_retry_on_a_non_reworkable_status_is_refused(store):
    _configure(store)
    handoff = _publish(store)
    with pytest.raises(InvalidHandoffTransitionError):
        store.retry_handoff(handoff.id)  # still READY_FOR_INTEGRATION


# ---------------------------------------------------------------------------
# Regression batches.
# ---------------------------------------------------------------------------

def _integrate(store, handoff_id):
    store.claim_next_handoff("proj-a", claimed_by="e")
    store.transition_handoff(handoff_id, MERGING, event_type="TEST")
    store.transition_handoff(handoff_id, TARGETED_TEST, event_type="TEST")
    store.transition_handoff(handoff_id, INTEGRATED, event_type="TEST")


def test_pending_batch_handoffs_only_lists_integrated_unassigned(store):
    _configure(store)
    h1 = _publish(store, task_id="t1", commit_sha="s1")
    _integrate(store, h1.id)
    pending = store.pending_batch_handoffs("proj-a")
    assert [h.id for h in pending] == [h1.id]


def test_create_batch_assigns_handoffs_and_transitions(store):
    _configure(store)
    h1 = _publish(store, task_id="t1", commit_sha="s1")
    _integrate(store, h1.id)
    batch = store.create_batch("proj-a", [h1.id])
    assert batch.status == REGRESSION_PENDING
    assert store.pending_batch_handoffs("proj-a") == []  # now assigned, no longer "pending"
    assert store.get_handoff(h1.id).regression_batch_id == batch.id


def test_batch_full_lifecycle_to_merge_ready_and_promote(store):
    _configure(store)
    h1 = _publish(store, task_id="t1", commit_sha="s1")
    _integrate(store, h1.id)
    batch = store.create_batch("proj-a", [h1.id])
    store.transition_batch(batch.id, REGRESSION_RUNNING, event_type="TEST")
    store.transition_batch(batch.id, MERGE_READY, event_type="TEST")
    promoted = store.promote_batch(batch.id, main_commit_sha="deadbeef")
    assert promoted.promoted_to_main is True
    assert promoted.promoted_at is not None


def test_promote_refuses_a_batch_not_merge_ready(store):
    _configure(store)
    h1 = _publish(store, task_id="t1", commit_sha="s1")
    _integrate(store, h1.id)
    batch = store.create_batch("proj-a", [h1.id])
    with pytest.raises(InvalidHandoffTransitionError):
        store.promote_batch(batch.id, main_commit_sha="deadbeef")


def test_regression_failed_can_be_retried_via_a_fresh_pending_batch(store):
    _configure(store)
    h1 = _publish(store, task_id="t1", commit_sha="s1")
    _integrate(store, h1.id)
    batch = store.create_batch("proj-a", [h1.id])
    store.transition_batch(batch.id, REGRESSION_RUNNING, event_type="TEST")
    store.transition_batch(batch.id, REGRESSION_FAILED, event_type="TEST", reason="tests failed")
    retried = store.transition_batch(batch.id, REGRESSION_PENDING, event_type="RETRY")
    assert retried.status == REGRESSION_PENDING


# ---------------------------------------------------------------------------
# Pipeline pause/resume.
# ---------------------------------------------------------------------------

def test_pause_and_resume_pipeline(store):
    _configure(store)
    store.pause_pipeline("proj-a", reason="manual pause")
    assert store.get_pipeline("proj-a")["paused"] is True
    assert store.get_pipeline("proj-a")["paused_reason"] == "manual pause"
    store.resume_pipeline("proj-a")
    assert store.get_pipeline("proj-a")["paused"] is False


def test_configure_pipeline_is_idempotent_and_updates_in_place(store):
    _configure(store, batch_size=3)
    _configure(store, batch_size=5)
    pipeline = store.get_pipeline("proj-a")
    assert pipeline["batch_size"] == 5


def test_list_pipelines_empty_when_nothing_configured(store):
    assert store.list_pipelines() == []


def test_list_pipelines_returns_every_configured_project(store):
    _configure(store, project="proj-a")
    _configure(store, project="proj-b")
    projects = {p["project"] for p in store.list_pipelines()}
    assert projects == {"proj-a", "proj-b"}


# ---------------------------------------------------------------------------
# IntegrationService.fleet_overview -- Dashboard Supervisor/Coordinator
# panel's own "integration lane trạng thái Waiting/Reviewing/Merging/Test/
# Regression/Rework" data source.
# ---------------------------------------------------------------------------

def test_fleet_overview_empty_when_nothing_configured(store):
    from terminal_mcp.integration_service import IntegrationService
    service = IntegrationService(store)
    assert service.fleet_overview() == {"projects": []}


def test_fleet_overview_maps_handoff_status_to_the_ui_lane_label(store):
    from terminal_mcp.integration_service import IntegrationService
    service = IntegrationService(store)
    _configure(store, project="proj-a")
    handoff = store.publish_handoff(project="proj-a", task_id="t1", origin_session="lane-a", branch="feature/x",
                                    commit_sha="a" * 40, base_sha="b" * 40)
    overview = service.fleet_overview()
    assert len(overview["projects"]) == 1
    row = overview["projects"][0]
    assert row["project"] == "proj-a"
    assert row["current_handoff"] is None  # READY_FOR_INTEGRATION isn't "current" (nothing has CLAIMED it yet)
    assert row["handoff_counts"]["READY_FOR_INTEGRATION"] == 1

    store.claim_next_handoff("proj-a", claimed_by="integration-engine")
    overview_after_claim = service.fleet_overview()
    row_after_claim = overview_after_claim["projects"][0]
    assert row_after_claim["current_handoff"]["id"] == handoff.id
    assert row_after_claim["current_lane"] == "Reviewing"  # CLAIMED -> "Reviewing"

    store.transition_handoff(handoff.id, "MERGING", event_type="TEST")
    row_merging = service.fleet_overview()["projects"][0]
    assert row_merging["current_lane"] == "Merging"

    store.transition_handoff(handoff.id, "TARGETED_TEST", event_type="TEST")
    row_test = service.fleet_overview()["projects"][0]
    assert row_test["current_lane"] == "Test"
