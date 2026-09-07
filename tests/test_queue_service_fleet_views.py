"""QueueService.global_inbox / recent_events -- the fleet-wide views
backing the Dashboard Global Task Inbox + Supervisor/Coordinator panel
(task: "Global Task Inbox", "recent event timeline"). Direct
QueueService/QueueStore tests (no MCP layer, no real tmux) -- fast,
deterministic, and this is exactly where the shared _group_tasks
grouping logic (reused from session_task_board, already proven correct
per-session in test_task_manager_ui.py) needs proving fleet-wide:
tagged with the right `session`, aggregated correctly across lanes.

SAFETY: every session name below is a disposable fixture string --
never `window`/`window2`."""
from __future__ import annotations

import pytest

from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


def test_global_inbox_empty_when_nothing_ever_queued(queue):
    inbox = queue.global_inbox()
    assert inbox["summary"] == {"running": 0, "queued": 0, "waiting_dependency": 0, "blocked_rework": 0,
                                "total": 0, "sessions": 0, "paused_sessions": 0}


def test_global_inbox_tags_each_task_with_its_own_session(queue):
    queue.store.append_tasks("lane-a", [{"prompt": "task in lane-a"}])
    queue.store.append_tasks("lane-b", [{"prompt": "task in lane-b"}, {"prompt": "another in lane-b"}])
    inbox = queue.global_inbox()
    assert inbox["summary"]["total"] == 3
    assert inbox["summary"]["sessions"] == 2
    by_session = {t["session"] for t in inbox["queued"]}
    assert by_session == {"lane-a", "lane-b"}


def test_global_inbox_groups_blocked_and_waiting_dependency_across_lanes(queue):
    (blocked_id,) = queue.store.append_tasks("lane-a", [{"prompt": "will be blocked"}])
    queue.store.transition_task(blocked_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(blocked_id, "BLOCKED", event_type="TEST", reason="a real failure")

    (dep_target_id,) = queue.store.append_tasks("lane-b", [{"prompt": "dependency target, not done"}])
    queue.store.append_tasks("lane-b", [{"prompt": "depends on the above", "depends_on": [dep_target_id]}])

    inbox = queue.global_inbox()
    assert inbox["summary"]["blocked_rework"] == 1
    assert inbox["blocked_rework"][0]["session"] == "lane-a"
    assert inbox["summary"]["waiting_dependency"] == 1
    assert inbox["waiting_dependency"][0]["session"] == "lane-b"


def test_global_inbox_reports_paused_session_count(queue):
    queue.store.append_tasks("lane-a", [{"prompt": "a"}])
    queue.store.append_tasks("lane-b", [{"prompt": "b"}])
    queue.pause("lane-a")
    inbox = queue.global_inbox()
    assert inbox["summary"]["paused_sessions"] == 1


def test_recent_events_merges_and_sorts_across_sessions(queue):
    queue.store.append_tasks("lane-a", [{"prompt": "a"}])
    queue.store.append_tasks("lane-b", [{"prompt": "b"}])
    queue.store.append_tasks("lane-a", [{"prompt": "a2"}])

    result = queue.recent_events(limit=50)
    sessions_seen = {e["session"] for e in result["events"]}
    assert {"lane-a", "lane-b"} <= sessions_seen
    timestamps = [e["timestamp"] for e in result["events"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_recent_events_respects_limit(queue):
    for i in range(10):
        queue.store.append_tasks(f"lane-{i}", [{"prompt": f"task {i}"}])
    result = queue.recent_events(limit=3)
    assert len(result["events"]) == 3


# ---------------------------------------------------------------------------
# Dashboard Task button pending-count badge (2026-09-07 checkpoint) --
# QueueService.count_pending/pending_counts is the ONE canonical
# definition (PENDING_STATUSES) the badge/status()/session_task_board all
# share -- these tests exercise the real state machine (transition_task),
# never a hand-built fake status string, so a future status-vocabulary
# change that misses updating PENDING_STATUSES would break a REAL
# transition path here, not just a hardcoded list.
# ---------------------------------------------------------------------------

def test_pending_counts_empty_when_nothing_ever_queued(queue):
    assert queue.pending_counts() == {}


def test_pending_counts_counts_queued_tasks(queue):
    queue.store.append_tasks("lane-a", [{"prompt": "1"}, {"prompt": "2"}, {"prompt": "3"}])
    assert queue.pending_counts() == {"lane-a": 3}


def test_pending_counts_excludes_running_and_verifying(queue):
    (running_id, verifying_id, queued_id) = queue.store.append_tasks(
        "lane-a", [{"prompt": "r"}, {"prompt": "v"}, {"prompt": "q"}])
    for tid in (running_id, verifying_id):
        queue.store.transition_task(tid, "PRECHECK", event_type="TEST")
        queue.store.transition_task(tid, "READY", event_type="TEST")
        queue.store.transition_task(tid, "DISPATCHING", event_type="TEST")
        queue.store.transition_task(tid, "RUNNING", event_type="TEST")
    queue.store.transition_task(verifying_id, "VERIFYING", event_type="TEST")
    # running_id: RUNNING (not pending); verifying_id: VERIFYING (not
    # pending); queued_id: still QUEUED (pending) -- only 1 counts.
    assert queue.pending_counts() == {"lane-a": 1}


def test_pending_counts_excludes_terminal_statuses(queue):
    (completed_id, failed_id, cancelled_id, skipped_id) = queue.store.append_tasks(
        "lane-a", [{"prompt": "c"}, {"prompt": "f"}, {"prompt": "x"}, {"prompt": "s"}])
    for tid in (completed_id, failed_id):
        queue.store.transition_task(tid, "PRECHECK", event_type="TEST")
        queue.store.transition_task(tid, "READY", event_type="TEST")
        queue.store.transition_task(tid, "DISPATCHING", event_type="TEST")
        queue.store.transition_task(tid, "RUNNING", event_type="TEST")
    queue.store.transition_task(completed_id, "VERIFYING", event_type="TEST")
    queue.store.transition_task(completed_id, "COMPLETED", event_type="TEST")
    queue.store.transition_task(failed_id, "FAILED", event_type="TEST", reason="boom")
    queue.store.transition_task(skipped_id, "SKIPPED", event_type="TEST")  # from QUEUED, never dispatched
    queue.store.transition_task(cancelled_id, "CANCELLED", event_type="TEST")
    assert queue.pending_counts() == {"lane-a": 0}


def test_pending_counts_includes_blocked_and_waiting_session_and_precheck_ready(queue):
    (blocked_id,) = queue.store.append_tasks("lane-a", [{"prompt": "b"}])
    queue.store.transition_task(blocked_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(blocked_id, "BLOCKED", event_type="TEST", reason="gate refusal")

    (waiting_id,) = queue.store.append_tasks("lane-b", [{"prompt": "w"}])
    queue.store.transition_task(waiting_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(waiting_id, "WAITING_SESSION", event_type="TEST")

    (precheck_id,) = queue.store.append_tasks("lane-c", [{"prompt": "p"}])
    queue.store.transition_task(precheck_id, "PRECHECK", event_type="TEST")

    (ready_id,) = queue.store.append_tasks("lane-d", [{"prompt": "r"}])
    queue.store.transition_task(ready_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(ready_id, "READY", event_type="TEST")

    counts = queue.pending_counts()
    assert counts == {"lane-a": 1, "lane-b": 1, "lane-c": 1, "lane-d": 1}


def test_pending_counts_across_multiple_lanes_independent(queue):
    queue.store.append_tasks("lane-a", [{"prompt": "1"}, {"prompt": "2"}])
    queue.store.append_tasks("lane-b", [{"prompt": "1"}])
    assert queue.pending_counts() == {"lane-a": 2, "lane-b": 1}


def test_status_response_includes_pending_count(queue):
    queue.store.append_tasks("lane-a", [{"prompt": "1"}, {"prompt": "2"}])
    status = queue.status("lane-a")
    assert status["pending_count"] == 2


def test_count_pending_over_99_is_a_real_count_not_capped_server_side(queue):
    # The "99+" DISPLAY cap is a frontend-only concern (never truncate the
    # real backend count) -- confirms the helper itself returns the true
    # number so the frontend's own >99 formatting has real data to work
    # with, not a pre-clamped value.
    tasks = [{"prompt": f"t{i}"} for i in range(150)]
    queue.store.append_tasks("lane-a", tasks)
    assert queue.pending_counts()["lane-a"] == 150


# ---------------------------------------------------------------------------
# Unified Task System checkpoint: create_task / assign_task / board (Kanban).
# ---------------------------------------------------------------------------

def test_create_task_without_session_lands_unassigned(queue):
    result = queue.create_task("Investigate flaky test", "look into it", session=None)
    assert result["status"] == "TASK_ACCEPTED"
    assert result["assigned"] is False
    task = queue.store.get_task(result["task_id"])
    assert task.session == "__unassigned__"


def test_create_task_with_session_is_assigned_directly(queue):
    result = queue.create_task("Ship the thing", "do it", session="lane-a")
    assert result["assigned"] is True
    assert result["session"] == "lane-a"
    task = queue.store.get_task(result["task_id"])
    assert task.session == "lane-a"


def test_create_task_rejects_invalid_session_name(queue):
    result = queue.create_task("x", "y", session="../not valid")
    assert "error" in result


def test_create_task_requires_a_prompt(queue):
    assert queue.create_task("title only", "", session=None) == {"error": "TASK_PROMPT_REQUIRED"}


def test_create_task_folds_project_into_metadata_without_schema_change(queue):
    result = queue.create_task("t", "p", session=None, project="OfflinePOS")
    task = queue.store.get_task(result["task_id"])
    assert task.metadata.get("project") == "OfflinePOS"


def test_assign_task_moves_same_task_id_no_duplicate(queue):
    created = queue.create_task("t", "p", session=None)
    task_id = created["task_id"]
    result = queue.assign_task(task_id, "lane-a")
    assert "error" not in result
    assert result["task"]["id"] == task_id
    assert result["task"]["session"] == "lane-a"
    lanes = {lane["session"]: lane["total_count"] for lane in queue.store.list_all_lanes()}
    assert lanes.get("__unassigned__", 0) == 0
    assert lanes["lane-a"] == 1


def test_assign_task_rejects_invalid_session_name(queue):
    created = queue.create_task("t", "p", session=None)
    result = queue.assign_task(created["task_id"], "../not valid")
    assert "error" in result


def test_assign_task_refuses_a_running_task(queue):
    (task_id,) = queue.store.append_tasks("lane-a", [{"prompt": "p"}])
    queue.store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(task_id, "RUNNING", event_type="TEST")
    result = queue.assign_task(task_id, "lane-b")
    assert result == {"error": "TASK_NOT_MOVABLE", "task_id": task_id, "status": "RUNNING"}


def test_board_empty_when_nothing_ever_created(queue):
    board = queue.board()
    assert board["counts"] == {"backlog": 0, "queued": 0, "running": 0, "blocked_review": 0, "done": 0}


def test_board_groups_tasks_into_the_five_real_lifecycle_columns(queue):
    unassigned = queue.create_task("u", "p", session=None)["task_id"]
    queued = queue.create_task("q", "p", session="lane-a")["task_id"]
    running = queue.create_task("r", "p", session="lane-a")["task_id"]
    queue.store.transition_task(running, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(running, "RUNNING", event_type="TEST")
    blocked = queue.create_task("b", "p", session="lane-b")["task_id"]
    queue.store.transition_task(blocked, "PRECHECK", event_type="TEST")
    queue.store.transition_task(blocked, "BLOCKED", event_type="TEST", reason="x")
    done = queue.create_task("d", "p", session="lane-b")["task_id"]
    queue.store.transition_task(done, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(done, "RUNNING", event_type="TEST")
    queue.store.transition_task(done, "VERIFYING", event_type="TEST")
    queue.store.mark_completed_with_evidence(done, evidence={"marker_found": True})

    board = queue.board()
    assert board["counts"] == {"backlog": 1, "queued": 1, "running": 1, "blocked_review": 1, "done": 1}
    assert board["backlog"][0]["id"] == unassigned
    assert board["backlog"][0]["session"] is None  # sentinel never leaks to the caller
    assert board["queued"][0]["id"] == queued
    assert board["running"][0]["id"] == running
    assert board["blocked_review"][0]["id"] == blocked
    assert board["done"][0]["id"] == done


def test_board_after_assign_moves_task_from_backlog_to_queued(queue):
    created = queue.create_task("u", "p", session=None)
    assert queue.board()["counts"]["backlog"] == 1
    queue.assign_task(created["task_id"], "lane-a")
    counts = queue.board()["counts"]
    assert counts["backlog"] == 0
    assert counts["queued"] == 1


# ---------------------------------------------------------------------------
# Definition of Ready checkpoint (§20.6 Phase A): opt-in gate before a
# task may leave UNASSIGNED (assign_task) or be created already-assigned
# (create_task with session=).
# ---------------------------------------------------------------------------

def test_assign_task_refuses_dor_required_task_missing_fields(queue):
    created = queue.create_task("t", "p", session=None, metadata={"dor_required": True})
    result = queue.assign_task(created["task_id"], "lane-a")
    assert result["error"] == "NEEDS_CLARIFICATION"
    assert "acceptance_criteria" in result["missing_fields"]
    # Refused -- task must still be exactly where it was.
    assert queue.board()["counts"]["backlog"] == 1


def test_assign_task_allows_dor_required_task_with_all_fields(queue):
    created = queue.create_task("t", "p", session=None, metadata={
        "dor_required": True, "acceptance_criteria": "works", "project": "P", "risk_level": "LOW",
    })
    result = queue.assign_task(created["task_id"], "lane-a")
    assert "error" not in result


def test_assign_task_never_checks_dor_when_not_opted_in(queue):
    created = queue.create_task("t", "p", session=None)  # no dor_required at all
    result = queue.assign_task(created["task_id"], "lane-a")
    assert "error" not in result


def test_create_task_with_session_enforces_dor_when_opted_in(queue):
    result = queue.create_task("t", "p", session="lane-a", metadata={"dor_required": True})
    assert result["error"] == "NEEDS_CLARIFICATION"
    assert queue.board()["counts"]["queued"] == 0  # never created at all


def test_create_task_unassigned_never_checks_dor_at_creation_time(queue):
    # DoR is about LEAVING unassigned -- creating it AS unassigned is
    # exactly where DoR belongs unchecked; the gate applies once it's
    # actually assigned (assign_task, tested above).
    result = queue.create_task("t", "p", session=None, metadata={"dor_required": True})
    assert "error" not in result
    assert queue.board()["counts"]["backlog"] == 1


# ---------------------------------------------------------------------------
# Incident lane (§20.6 Phase B): a real task in the SAME lane, fast-
# tracked by priority -- never a parallel queue.
# ---------------------------------------------------------------------------

def test_create_incident_task_tags_type_and_boosts_priority(queue):
    result = queue.create_incident_task("Prod down", "investigate the outage", session="lane-a")
    task = queue.store.get_task(result["task_id"])
    assert task.metadata["type"] == "incident"
    assert task.priority == queue.INCIDENT_PRIORITY


def test_incident_task_dispatches_ahead_of_normal_queued_work(queue):
    normal_id = queue.create_task("normal work", "p", session="lane-a")["task_id"]
    incident_id = queue.create_incident_task("Prod down", "p", session="lane-a")["task_id"]
    # Real claim ordering (ORDER BY priority DESC, position ASC) --
    # the incident, created SECOND, still claims FIRST.
    claimed = queue.store.claim_next_task("lane-a", claimed_by="engine-1")
    assert claimed.id == incident_id
    assert claimed.id != normal_id


def test_incident_task_still_goes_through_dor_when_opted_in(queue):
    result = queue.create_incident_task("Prod down", "p", session="lane-a",
                                        metadata={"dor_required": True})
    assert result["error"] == "NEEDS_CLARIFICATION"  # incidents are not exempt from DoR


def test_incident_task_carries_risk_level(queue):
    result = queue.create_incident_task("Prod down", "p", session=None, risk_level="CRITICAL")
    task = queue.store.get_task(result["task_id"])
    assert task.metadata["risk_level"] == "CRITICAL"


def test_list_active_incidents_excludes_terminal_and_non_incidents(queue):
    queue.create_task("normal", "p", session="lane-a")  # not an incident
    active = queue.create_incident_task("Active incident", "p", session="lane-b")["task_id"]
    done = queue.create_incident_task("Resolved incident", "p", session="lane-c")["task_id"]
    queue.store.transition_task(done, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(done, "RUNNING", event_type="TEST")
    queue.store.transition_task(done, "VERIFYING", event_type="TEST")
    queue.store.mark_completed_with_evidence(done, evidence={"resolved": True})

    result = queue.list_active_incidents()
    assert result["count"] == 1
    assert result["incidents"][0]["id"] == active


def test_list_active_incidents_empty_when_none(queue):
    queue.create_task("normal", "p", session="lane-a")
    assert queue.list_active_incidents() == {"incidents": [], "count": 0}
