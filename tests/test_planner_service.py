"""Planner (task-breaking) -- real I/O glue (planner_service.py, docs/
REQUIREMENTS.md §20.3). Direct QueueService/PlannerStore tests (no MCP
layer, no real tmux).

SAFETY: every session name below is a disposable fixture string --
never `window`/`window2`/`wtest`."""
from __future__ import annotations

import pytest

from terminal_mcp.planner_service import MODE_AUTO, MODE_SUGGEST, PlannerService
from terminal_mcp.planner_store import PlannerStore
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def planner(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    planner_store = PlannerStore(tmp_path / "planner.db")
    return PlannerService(planner_store, queue)


def _children(n=2, **overrides):
    children = [{"title": f"child {i}", "prompt": f"do part {i}", "acceptance_criteria": f"part {i} works"}
               for i in range(n)]
    for child in children:
        child.update(overrides)
    return children


# -- propose_split: validation --------------------------------------------------

def test_propose_split_suggest_mode_persists_without_creating_children(planner):
    parent = planner.queue.create_task("Big task", "do the big thing", session=None)
    result = planner.propose_split(parent["task_id"], _children(2), mode=MODE_SUGGEST)
    assert result["proposal"]["status"] == "PROPOSED"
    board = planner.queue.board()
    assert board["counts"]["backlog"] == 1  # only the parent -- no children created yet


def test_propose_split_missing_acceptance_criteria_is_needs_clarification(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    bad_children = [{"title": "c1", "prompt": "do it"}]  # no acceptance_criteria
    result = planner.propose_split(parent["task_id"], bad_children, mode=MODE_SUGGEST)
    assert result["proposal"]["status"] == "NEEDS_CLARIFICATION"
    assert "acceptance_criteria" in result["proposal"]["reason"]


def test_propose_split_empty_children_is_needs_clarification(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.propose_split(parent["task_id"], [], mode=MODE_SUGGEST)
    assert result["proposal"]["status"] == "NEEDS_CLARIFICATION"


def test_propose_split_unknown_parent_reports_error(planner):
    result = planner.propose_split("no-such-task", _children(), mode=MODE_SUGGEST)
    assert result["error"] == "TASK_NOT_FOUND"


def test_propose_split_invalid_mode_rejected(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.propose_split(parent["task_id"], _children(), mode="BOGUS")
    assert result["error"] == "INVALID_MODE"


def test_propose_split_refuses_a_non_queued_parent(planner):
    parent = planner.queue.create_task("t", "p", session="lane-a")
    planner.queue.store.transition_task(parent["task_id"], "DISPATCHING", event_type="TEST")
    result = planner.propose_split(parent["task_id"], _children(), mode=MODE_SUGGEST)
    assert result["error"] == "PARENT_NOT_SPLITTABLE"


# -- approve_split / AUTO mode: real child creation -----------------------------

def test_approve_split_creates_real_children_with_parent_link(planner):
    parent = planner.queue.create_task("Big task", "do the big thing", session=None)
    proposed = planner.propose_split(parent["task_id"], _children(2), mode=MODE_SUGGEST)
    result = planner.approve_split(proposed["proposal"]["proposal_id"])
    assert len(result["child_task_ids"]) == 2
    for child_id in result["child_task_ids"]:
        child = planner.queue.task_status(child_id)["task"]
        assert child["metadata"]["parent_task_id"] == parent["task_id"]
        assert child["metadata"]["acceptance_criteria"]


def test_approve_split_parks_parent_in_blocked(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    proposed = planner.propose_split(parent["task_id"], _children(1), mode=MODE_SUGGEST)
    planner.approve_split(proposed["proposal"]["proposal_id"])
    parent_row = planner.queue.store.get_task(parent["task_id"])
    assert parent_row.status == "BLOCKED"
    assert parent_row.metadata["is_split_parent"] is True
    assert len(parent_row.metadata["child_task_ids"]) == 1


def test_approve_split_refuses_a_non_pending_proposal(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    proposed = planner.propose_split(parent["task_id"], _children(1), mode=MODE_SUGGEST)
    proposal_id = proposed["proposal"]["proposal_id"]
    planner.approve_split(proposal_id)
    second = planner.approve_split(proposal_id)
    assert second["error"] == "NO_PENDING_PROPOSAL"


def test_propose_split_auto_mode_creates_children_immediately(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.propose_split(parent["task_id"], _children(2), mode=MODE_AUTO)
    assert len(result["child_task_ids"]) == 2
    board = planner.queue.board()
    assert board["counts"]["blocked_review"] == 1  # the split parent
    assert board["counts"]["backlog"] == 2  # the 2 unassigned children


def test_propose_split_refuses_a_second_split_of_an_already_split_parent(planner):
    # An approved split always parks the parent in BLOCKED (never QUEUED
    # again) -- so the same PARENT_NOT_SPLITTABLE check that refuses a
    # RUNNING/dispatched parent also correctly refuses a re-split attempt.
    parent = planner.queue.create_task("t", "p", session=None)
    planner.propose_split(parent["task_id"], _children(1), mode=MODE_AUTO)
    result = planner.propose_split(parent["task_id"], _children(1), mode=MODE_AUTO)
    assert result["error"] == "PARENT_NOT_SPLITTABLE"
    assert result["status"] == "BLOCKED"


# -- dependency wiring (overlap-based serialization) ----------------------------

def test_split_wires_depends_on_for_declared_overlapping_children(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    children = [
        {"title": "first", "prompt": "p1", "acceptance_criteria": "a1"},
        {"title": "second (overlaps first)", "prompt": "p2", "acceptance_criteria": "a2",
         "depends_on_indices": [0]},
    ]
    result = planner.propose_split(parent["task_id"], children, mode=MODE_AUTO)
    first_id, second_id = result["child_task_ids"]
    second = planner.queue.task_status(second_id)["task"]
    assert second["depends_on"] == [first_id]
    first = planner.queue.task_status(first_id)["task"]
    assert first["depends_on"] == []


# -- children_progress / complete_parent_if_children_done -----------------------

def test_children_progress_counts_real_child_statuses(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.propose_split(parent["task_id"], _children(2, session="lane-a"), mode=MODE_AUTO)
    child_a, child_b = result["child_task_ids"]
    planner.queue.store.transition_task(child_a, "DISPATCHING", event_type="TEST")
    planner.queue.store.transition_task(child_a, "RUNNING", event_type="TEST")
    planner.queue.store.transition_task(child_a, "VERIFYING", event_type="TEST")
    planner.queue.store.mark_completed_with_evidence(child_a, evidence={"ok": True})

    progress = planner.children_progress(parent["task_id"])
    assert progress["total"] == 2
    assert progress["done"] == 1


def test_complete_parent_if_children_done_refuses_when_children_still_open(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    planner.propose_split(parent["task_id"], _children(1, session="lane-a"), mode=MODE_AUTO)
    result = planner.complete_parent_if_children_done(parent["task_id"])
    assert result["completed"] is False


def test_complete_parent_if_children_done_completes_once_all_children_done(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.propose_split(parent["task_id"], _children(1, session="lane-a"), mode=MODE_AUTO)
    (child_id,) = result["child_task_ids"]
    planner.queue.store.transition_task(child_id, "DISPATCHING", event_type="TEST")
    planner.queue.store.transition_task(child_id, "RUNNING", event_type="TEST")
    planner.queue.store.transition_task(child_id, "VERIFYING", event_type="TEST")
    planner.queue.store.mark_completed_with_evidence(child_id, evidence={"ok": True})

    completion = planner.complete_parent_if_children_done(parent["task_id"])
    assert completion["completed"] is True
    assert completion["task"]["status"] == "COMPLETED"
    board = planner.queue.board()
    assert board["counts"]["done"] == 2  # parent + child both now DONE


def test_complete_parent_refuses_a_task_that_was_never_split(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.complete_parent_if_children_done(parent["task_id"])
    assert result["error"] == "NOT_A_SPLIT_PARENT"


def test_complete_parent_refuses_unknown_task(planner):
    result = planner.complete_parent_if_children_done("no-such-task")
    assert result["error"] == "TASK_NOT_FOUND"


def test_a_cancelled_child_still_allows_parent_completion(planner):
    parent = planner.queue.create_task("t", "p", session=None)
    result = planner.propose_split(parent["task_id"], _children(2, session="lane-a"), mode=MODE_AUTO)
    done_child, cancelled_child = result["child_task_ids"]
    planner.queue.store.transition_task(done_child, "DISPATCHING", event_type="TEST")
    planner.queue.store.transition_task(done_child, "RUNNING", event_type="TEST")
    planner.queue.store.transition_task(done_child, "VERIFYING", event_type="TEST")
    planner.queue.store.mark_completed_with_evidence(done_child, evidence={"ok": True})
    planner.queue.store.transition_task(cancelled_child, "CANCELLED", event_type="TEST")

    completion = planner.complete_parent_if_children_done(parent["task_id"])
    assert completion["completed"] is True
