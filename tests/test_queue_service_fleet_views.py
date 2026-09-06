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
