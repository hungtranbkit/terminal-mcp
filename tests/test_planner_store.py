"""Planner persistence -- planner_store.py (docs/REQUIREMENTS.md §20.3).
Pure store-level tests, same posture as test_pm_store.py."""
from __future__ import annotations

import pytest

from terminal_mcp.planner_store import APPROVED, PROPOSED, PlannerStore


@pytest.fixture
def store(tmp_path):
    return PlannerStore(tmp_path / "planner.db")


def test_create_proposal_and_get(store):
    proposal = store.create_proposal(parent_task_id="p1", mode="SUGGEST", status=PROPOSED,
                                     children_spec=[{"prompt": "a", "acceptance_criteria": "done"}],
                                     reason="split into 1 child")
    assert proposal.parent_task_id == "p1"
    assert proposal.status == PROPOSED
    fetched = store.get_proposal(proposal.proposal_id)
    assert fetched.proposal_id == proposal.proposal_id


def test_get_proposal_unknown_returns_none(store):
    assert store.get_proposal("no-such-id") is None


def test_mark_decided_updates_status_and_child_ids(store):
    proposal = store.create_proposal(parent_task_id="p1", mode="SUGGEST", status=PROPOSED,
                                     children_spec=[{"prompt": "a", "acceptance_criteria": "done"}],
                                     reason="r")
    updated = store.mark_decided(proposal.proposal_id, status=APPROVED, child_task_ids=["c1", "c2"])
    assert updated.status == APPROVED
    assert updated.child_task_ids == ("c1", "c2")
    assert updated.decided_at is not None


def test_list_proposals_for_parent_newest_first(store):
    store.create_proposal(parent_task_id="p1", mode="SUGGEST", status=PROPOSED,
                          children_spec=[], reason="first")
    store.create_proposal(parent_task_id="p1", mode="SUGGEST", status=APPROVED,
                          children_spec=[], reason="second")
    proposals = store.list_proposals_for_parent("p1")
    assert len(proposals) == 2
    assert proposals[0].reason == "second"
    assert proposals[1].reason == "first"


def test_list_proposals_for_parent_empty_when_none(store):
    assert store.list_proposals_for_parent("no-such-parent") == []
