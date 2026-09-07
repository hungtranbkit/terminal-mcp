"""PM/Orchestrator persistence -- pm_store.py (docs/REQUIREMENTS.md
§20.2). Pure store-level tests, same posture as test_queue_store.py:
tmp_path-scoped db, disposable fixture names only."""
from __future__ import annotations

import pytest

from terminal_mcp.pm_store import PMStore


@pytest.fixture
def store(tmp_path):
    return PMStore(tmp_path / "pm.db")


# -- capability profiles -------------------------------------------------------

def test_upsert_capability_creates_a_new_profile(store):
    profile = store.upsert_capability("local", "worker-a", os="linux", role="developer",
                                      skills=[{"name": "docker", "confidence": 0.9}])
    assert profile.node_id == "local"
    assert profile.session == "worker-a"
    assert profile.os == "linux"
    assert profile.role == "developer"
    assert profile.skills == ({"name": "docker", "confidence": 0.9},)
    assert profile.key() == "local/worker-a"


def test_upsert_capability_is_keyed_by_node_and_session_composite(store):
    store.upsert_capability("node-1", "worker-a", os="linux")
    store.upsert_capability("node-2", "worker-a", os="windows")  # same session name, different node
    profiles = {p.key(): p for p in store.list_capabilities()}
    assert profiles["node-1/worker-a"].os == "linux"
    assert profiles["node-2/worker-a"].os == "windows"


def test_upsert_capability_updates_in_place_preserving_unspecified_fields(store):
    store.upsert_capability("local", "worker-a", os="linux", role="developer")
    updated = store.upsert_capability("local", "worker-a", skills=[{"name": "docker"}])
    assert updated.os == "linux"  # preserved, not blanked
    assert updated.role == "developer"  # preserved
    assert updated.skills == ({"name": "docker"},)  # newly set


def test_upsert_capability_updated_at_changes_created_at_does_not(store):
    first = store.upsert_capability("local", "worker-a", os="linux")
    second = store.upsert_capability("local", "worker-a", os="windows")
    assert second.created_at == first.created_at
    assert second.os == "windows"


def test_get_capability_returns_none_for_unknown(store):
    assert store.get_capability("local", "no-such-worker") is None


def test_list_capabilities_empty_initially(store):
    assert store.list_capabilities() == []


def test_delete_capability_returns_false_when_nothing_deleted(store):
    assert store.delete_capability("local", "no-such-worker") is False


def test_delete_capability_removes_the_row(store):
    store.upsert_capability("local", "worker-a")
    assert store.delete_capability("local", "worker-a") is True
    assert store.get_capability("local", "worker-a") is None


# -- pm_decisions (append-only audit trail) ------------------------------------

def test_record_decision_and_list_for_task(store):
    store.record_decision(task_id="t1", mode="SUGGEST", status="SUGGESTED", reason="routed to worker-a",
                          chosen_node_id="local", chosen_session="worker-a", score_breakdown={"total": 5.0})
    decisions = store.list_decisions_for_task("t1")
    assert len(decisions) == 1
    assert decisions[0].chosen_session == "worker-a"
    assert decisions[0].score_breakdown == {"total": 5.0}
    assert decisions[0].decision_id  # a real, non-empty id was minted


def test_decisions_are_append_only_never_overwritten(store):
    store.record_decision(task_id="t1", mode="SUGGEST", status="SUGGESTED", reason="first")
    store.record_decision(task_id="t1", mode="SUGGEST", status="APPROVED_AND_ASSIGNED", reason="approved")
    decisions = store.list_decisions_for_task("t1")
    assert len(decisions) == 2  # both rows preserved, real history


def test_list_decisions_for_task_newest_first(store):
    store.record_decision(task_id="t1", mode="SUGGEST", status="SUGGESTED", reason="first")
    store.record_decision(task_id="t1", mode="SUGGEST", status="APPROVED_AND_ASSIGNED", reason="second")
    decisions = store.list_decisions_for_task("t1")
    assert decisions[0].reason == "second"
    assert decisions[1].reason == "first"


def test_latest_decision_for_task(store):
    store.record_decision(task_id="t1", mode="SUGGEST", status="SUGGESTED", reason="first")
    store.record_decision(task_id="t1", mode="SUGGEST", status="APPROVED_AND_ASSIGNED", reason="second")
    latest = store.latest_decision_for_task("t1")
    assert latest.reason == "second"


def test_latest_decision_for_task_none_when_no_history(store):
    assert store.latest_decision_for_task("no-such-task") is None


def test_latest_decisions_for_tasks_bulk_read(store):
    store.record_decision(task_id="t1", mode="SUGGEST", status="SUGGESTED", reason="a1")
    store.record_decision(task_id="t1", mode="SUGGEST", status="APPROVED_AND_ASSIGNED", reason="a2")
    store.record_decision(task_id="t2", mode="SUGGEST", status="NO_ELIGIBLE_WORKER", reason="b1")
    latest = store.latest_decisions_for_tasks(["t1", "t2", "t3-never-decided"])
    assert latest["t1"].reason == "a2"
    assert latest["t2"].reason == "b1"
    assert "t3-never-decided" not in latest


def test_latest_decisions_for_tasks_empty_list_returns_empty_dict(store):
    assert store.latest_decisions_for_tasks([]) == {}
