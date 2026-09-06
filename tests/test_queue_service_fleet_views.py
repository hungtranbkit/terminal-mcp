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
