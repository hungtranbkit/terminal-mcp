"""publish_handoff_for_completed_task -- the QueueTask-COMPLETED ->
Handoff-publish glue (task item 4). Pure in-process, no real session."""
from __future__ import annotations

import pytest

from terminal_mcp.integration_store import IntegrationStore, publish_handoff_for_completed_task
from terminal_mcp.queue_store import COMPLETED, DISPATCHING, RUNNING, VERIFYING, QueueStore


@pytest.fixture
def stores(tmp_path):
    return {"queue": QueueStore(tmp_path / "queue.db"), "integration": IntegrationStore(tmp_path / "integration.db")}


def _complete_task(store, session="lane-a", metadata=None):
    (task_id,) = store.set_tasks(session, [{"prompt": "do the feature work", "metadata": metadata or {}}])
    store.transition_task(task_id, DISPATCHING, event_type="TEST")
    store.transition_task(task_id, RUNNING, event_type="TEST")
    store.transition_task(task_id, VERIFYING, event_type="TEST")
    return store.mark_completed_with_evidence(task_id, evidence={"marker": "ok"})


def test_task_without_integration_required_publishes_nothing(stores):
    task = _complete_task(stores["queue"])
    result = publish_handoff_for_completed_task(task, stores["integration"])
    assert result is None
    assert stores["integration"].list_handoffs("proj-a") == []


def test_task_with_integration_required_publishes_a_real_handoff(stores):
    stores["integration"].configure_pipeline("proj-a", repo_path="/tmp/does-not-matter")
    task = _complete_task(stores["queue"], session="lane-a", metadata={
        "integration_required": {
            "project": "proj-a", "branch": "feature/x", "commit_sha": "abc123", "base_sha": "base000",
            "changed_paths": ["src/a.py"],
        },
    })
    handoff = publish_handoff_for_completed_task(task, stores["integration"])
    assert handoff is not None
    assert handoff.project == "proj-a"
    assert handoff.task_id == task.id
    assert handoff.origin_session == "lane-a"
    assert handoff.branch == "feature/x"
    assert handoff.commit_sha == "abc123"


def test_missing_required_fields_publishes_nothing(stores):
    task = _complete_task(stores["queue"], metadata={
        "integration_required": {"project": "proj-a", "branch": "feature/x"},  # missing commit_sha/base_sha
    })
    assert publish_handoff_for_completed_task(task, stores["integration"]) is None


def test_verification_evidence_used_as_test_summary_when_not_overridden(stores):
    stores["integration"].configure_pipeline("proj-a", repo_path="/tmp/x")
    task = _complete_task(stores["queue"], metadata={
        "integration_required": {"project": "proj-a", "branch": "feature/x", "commit_sha": "abc", "base_sha": "base"},
    })
    handoff = publish_handoff_for_completed_task(task, stores["integration"])
    assert handoff.test_summary == {"marker": "ok"}


def test_git_isolation_worktree_path_carries_into_handoff_artifacts(stores):
    # Git isolation checkpoint (§20.4): same "carry task metadata into
    # artifacts, no schema change" precedent as docs_exempt above.
    stores["integration"].configure_pipeline("proj-a", repo_path="/tmp/does-not-matter")
    task = _complete_task(stores["queue"], metadata={
        "integration_required": {"project": "proj-a", "branch": "feature/x", "commit_sha": "abc",
                                 "base_sha": "base"},
        "git_isolation": {"repo_path": "/tmp/repo", "branch": "task/abc123-thing",
                          "base_sha": "base000", "worktree_path": "/tmp/repo/../.worktrees/task-abc123-thing"},
    })
    handoff = publish_handoff_for_completed_task(task, stores["integration"])
    assert handoff.artifacts["worktree_path"] == "/tmp/repo/../.worktrees/task-abc123-thing"


def test_no_git_isolation_metadata_means_no_worktree_path_artifact(stores):
    stores["integration"].configure_pipeline("proj-a", repo_path="/tmp/does-not-matter")
    task = _complete_task(stores["queue"], metadata={
        "integration_required": {"project": "proj-a", "branch": "feature/x", "commit_sha": "abc",
                                 "base_sha": "base"},
    })
    handoff = publish_handoff_for_completed_task(task, stores["integration"])
    assert "worktree_path" not in handoff.artifacts
