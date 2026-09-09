"""Backlog -> Queue traceability, against the REAL QueueService/QueueStore
(not a stub): the whole point of this seam is that it uses the existing
canonical task-creation path, so testing it against a fake would prove
nothing about that claim.
"""
from __future__ import annotations

import pytest

from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import (COMPLETED, DISPATCHING, RUNNING, VERIFYING, QueueStore)
from tests.test_backlog import make_config, make_repo


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widget")


@pytest.fixture
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


@pytest.fixture
def svc(tmp_path, queue):
    return BacklogService(make_config(tmp_path), queue=queue)


def test_dispatch_creates_a_real_queue_task_and_links_both_ways(svc, queue, repo):
    tid = svc.add(str(repo), tasks=[{"title": "Ship it", "acceptance_criteria": ["tests pass"]}])["created_ids"][0]
    out = svc.dispatch(str(repo), task_id=tid)
    assert "error" not in out
    queue_task_id = out["queue_task_id"]
    assert queue_task_id

    # backlog -> queue
    item = svc.get(str(repo))["items"][0]
    assert item["queue_task_id"] == queue_task_id
    assert item["status"] == "READY"           # created, not yet assigned to a session

    # queue -> backlog (the reverse link is what makes it traceable)
    row = queue.task_status(queue_task_id)
    meta = row.get("metadata") or row.get("task", {}).get("metadata") or {}
    assert meta.get("backlog_id") == tid
    assert meta.get("acceptance_criteria") == ["tests pass"]


def test_dispatch_to_a_session_marks_in_progress(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.dispatch(str(repo), task_id=tid, session="win1")
    assert out["item"]["status"] == "IN_PROGRESS" and out["item"]["session"] == "win1"


def test_dispatch_is_not_repeatable(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    svc.dispatch(str(repo), task_id=tid)
    assert svc.dispatch(str(repo), task_id=tid)["error"] == "ALREADY_DISPATCHED"


def test_dispatch_prompt_carries_the_acceptance_criteria(svc, queue, repo):
    tid = svc.add(str(repo), tasks=[{"title": "T", "description": "D",
                                    "acceptance_criteria": ["A1", "A2"]}])["created_ids"][0]
    out = svc.dispatch(str(repo), task_id=tid)
    row = queue.task_status(out["queue_task_id"])
    prompt = row.get("prompt") or row.get("task", {}).get("prompt") or ""
    assert "A1" in prompt and "A2" in prompt and tid in prompt


def test_done_accepted_when_the_linked_queue_task_is_verified(svc, queue, repo):
    """The VERIFIED_DONE mapping: queue_store.COMPLETED is that state."""
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.dispatch(str(repo), task_id=tid)
    # No evidence on the item at all -- refused while the queue task is open.
    assert svc.complete(str(repo), task_id=tid)["error"] == "EVIDENCE_REQUIRED"
    # Walk the REAL lifecycle the engine walks -- the queue refuses
    # QUEUED -> COMPLETED outright, which is exactly the guarantee that
    # makes "linked queue task is COMPLETED" trustworthy as evidence.
    qid = out["queue_task_id"]
    for state in (DISPATCHING, RUNNING, VERIFYING, COMPLETED):
        queue.store.transition_task(qid, state, event_type="TEST_WALK")
    done = svc.complete(str(repo), task_id=tid)
    assert done["item"]["status"] == "DONE" and done["verified_by"] == "queue_task"


def test_backlog_without_queue_service_stays_planning_only(tmp_path, repo):
    planning_only = BacklogService(make_config(tmp_path))       # no queue wired
    tid = planning_only.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    assert planning_only.dispatch(str(repo), task_id=tid)["error"] == "QUEUE_UNAVAILABLE"
