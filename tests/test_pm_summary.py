"""PM summary + backlog hygiene -- pm_summary.py (docs/REQUIREMENTS.md
§20.6 Phase D). Direct QueueService tests, no MCP layer, no real tmux.

SAFETY: every session name below is a disposable fixture string --
never `window`/`window2`."""
from __future__ import annotations

import pytest

from terminal_mcp.pm_summary import (
    close_task_with_confirmation, detect_duplicate_tasks, detect_stale_backlog_tasks,
    emergency_resume_all_lanes, emergency_stop_all_lanes, generate_summary,
)
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


# -- generate_summary ----------------------------------------------------------

def test_generate_summary_empty_fleet(queue):
    summary = generate_summary(queue)
    assert summary["board_counts"] == {"backlog": 0, "queued": 0, "running": 0, "blocked_review": 0, "done": 0}
    assert summary["total_pending_tasks"] == 0
    assert summary["active_incidents"] == 0
    assert "generated_at" in summary


def test_generate_summary_reflects_real_state(queue):
    queue.create_task("t1", "p1", session=None)
    queue.create_task("t2", "p2", session="lane-a")
    queue.create_incident_task("Prod down", "p", session="lane-b")
    summary = generate_summary(queue)
    assert summary["board_counts"]["backlog"] == 1
    assert summary["board_counts"]["queued"] == 2
    assert summary["active_incidents"] == 1


def test_generate_summary_includes_node_capacity_when_controller_given(queue):
    class _FakeNode:
        def __init__(self):
            self.id = "local"
            self.status = "online"
            self.capacity_status = "healthy"
            self.cpu_percent = 12.5
            self.ram_percent = 40.0

    class _FakeController:
        def list_nodes(self):
            return [_FakeNode()]

    summary = generate_summary(queue, controller=_FakeController())
    assert summary["node_capacity"] == [
        {"node_id": "local", "status": "online", "capacity_status": "healthy", "cpu_percent": 12.5,
         "ram_percent": 40.0},
    ]


def test_generate_summary_no_controller_omits_node_capacity(queue):
    summary = generate_summary(queue)
    assert "node_capacity" not in summary


def test_generate_summary_controller_failure_is_best_effort(queue):
    class _BrokenController:
        def list_nodes(self):
            raise RuntimeError("boom")

    summary = generate_summary(queue, controller=_BrokenController())
    assert summary["node_capacity"] == []  # never crashes the whole summary


# -- detect_stale_backlog_tasks -------------------------------------------------

def test_detect_stale_backlog_tasks_flags_old_tasks(queue):
    created = queue.create_task("old task", "p", session=None)
    # Backdate created_at directly in the store (real row mutation, not a mock).
    with queue.store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                           (created["task_id"],))
    result = detect_stale_backlog_tasks(queue, stale_after_hours=24.0)
    assert result["count"] == 1
    assert result["stale_tasks"][0]["id"] == created["task_id"]
    assert result["stale_tasks"][0]["age_hours"] > 24.0


def test_detect_stale_backlog_tasks_never_flags_fresh_tasks(queue):
    queue.create_task("fresh task", "p", session=None)
    result = detect_stale_backlog_tasks(queue, stale_after_hours=24.0)
    assert result["count"] == 0


def test_detect_stale_backlog_tasks_never_flags_running_or_done(queue):
    created = queue.create_task("t", "p", session="lane-a")
    task_id = created["task_id"]
    with queue.store._connection() as connection:
        connection.execute("UPDATE queue_tasks SET created_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
                           (task_id,))
    queue.store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(task_id, "RUNNING", event_type="TEST")
    result = detect_stale_backlog_tasks(queue, stale_after_hours=24.0)
    assert result["count"] == 0  # RUNNING, not backlog/queued


# -- detect_duplicate_tasks ------------------------------------------------------

def test_detect_duplicate_tasks_flags_identical_prompts(queue):
    queue.create_task("t1", "do the exact same thing", session=None)
    queue.create_task("t2", "do the exact same thing", session="lane-a")
    result = detect_duplicate_tasks(queue)
    assert result["count"] == 1
    assert result["duplicate_groups"][0]["count"] == 2


def test_detect_duplicate_tasks_never_flags_distinct_prompts(queue):
    queue.create_task("t1", "prompt A", session=None)
    queue.create_task("t2", "prompt B", session=None)
    result = detect_duplicate_tasks(queue)
    assert result["count"] == 0


def test_detect_duplicate_tasks_never_counts_done_tasks(queue):
    a = queue.create_task("t1", "same prompt", session="lane-a")["task_id"]
    queue.create_task("t2", "same prompt", session="lane-b")
    queue.store.transition_task(a, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(a, "RUNNING", event_type="TEST")
    queue.store.transition_task(a, "VERIFYING", event_type="TEST")
    queue.store.mark_completed_with_evidence(a, evidence={"ok": True})
    result = detect_duplicate_tasks(queue)
    assert result["count"] == 0  # only 1 still-open task with that prompt now


# -- close_task_with_confirmation -----------------------------------------------

def test_close_task_refuses_without_confirmation(queue):
    created = queue.create_task("t", "p", session=None)
    result = close_task_with_confirmation(queue, created["task_id"], reason="obsolete")
    assert result["error"] == "CONFIRMATION_REQUIRED"
    # Never actually closed.
    assert queue.store.get_task(created["task_id"]).status == "QUEUED"


def test_close_task_requires_a_reason(queue):
    created = queue.create_task("t", "p", session=None)
    result = close_task_with_confirmation(queue, created["task_id"], reason="", confirmed=True)
    assert result["error"] == "REASON_REQUIRED"


def test_close_task_with_confirmation_and_reason_succeeds(queue):
    created = queue.create_task("t", "p", session=None)
    result = close_task_with_confirmation(queue, created["task_id"], reason="duplicate of t2", confirmed=True)
    assert "error" not in result
    assert result["task"]["status"] == "CANCELLED"


def test_close_task_unknown_id(queue):
    result = close_task_with_confirmation(queue, "no-such-id", reason="x", confirmed=True)
    assert result["error"] == "TASK_NOT_FOUND"


# -- emergency_stop_all_lanes / emergency_resume_all_lanes (§20.6 Phase E) ------

def test_emergency_stop_refuses_without_confirmation(queue):
    queue.create_task("t", "p", session="lane-a")
    result = emergency_stop_all_lanes(queue, reason="prod incident")
    assert result["error"] == "CONFIRMATION_REQUIRED"
    assert queue.store.lane_status("lane-a")["paused"] is False


def test_emergency_stop_requires_a_reason(queue):
    queue.create_task("t", "p", session="lane-a")
    result = emergency_stop_all_lanes(queue, reason="", confirmed=True)
    assert result["error"] == "REASON_REQUIRED"


def test_emergency_stop_pauses_every_lane(queue):
    queue.create_task("t1", "p1", session="lane-a")
    queue.create_task("t2", "p2", session="lane-b")
    result = emergency_stop_all_lanes(queue, reason="prod incident", confirmed=True)
    assert "error" not in result
    assert set(result["paused_lanes"]) == {"lane-a", "lane-b"}
    assert result["count"] == 2
    for session in ("lane-a", "lane-b"):
        status = queue.store.lane_status(session)
        assert status["paused"] is True
        assert "prod incident" in status["paused_reason"]
        assert status["paused_reason"].startswith("EMERGENCY STOP:")


def test_emergency_stop_never_touches_an_already_paused_lane(queue):
    queue.create_task("t", "p", session="lane-a")
    queue.pause("lane-a", reason="manual maintenance window")
    result = emergency_stop_all_lanes(queue, reason="prod incident", confirmed=True)
    assert result["paused_lanes"] == []  # already-paused lane left untouched
    status = queue.store.lane_status("lane-a")
    assert status["paused_reason"] == "manual maintenance window"


def test_emergency_resume_only_lifts_lanes_the_emergency_stop_itself_paused(queue):
    queue.create_task("t1", "p1", session="lane-a")
    queue.create_task("t2", "p2", session="lane-b")
    queue.pause("lane-b", reason="manual maintenance window")  # pre-existing, unrelated pause
    emergency_stop_all_lanes(queue, reason="prod incident", confirmed=True)
    # lane-a: paused by emergency stop. lane-b: was ALREADY paused before
    # the emergency stop (never touched by it, per the test above), so
    # its own unrelated pause reason is exactly what's still there.
    assert queue.store.lane_status("lane-a")["paused"] is True
    assert queue.store.lane_status("lane-b")["paused_reason"] == "manual maintenance window"

    result = emergency_resume_all_lanes(queue)
    assert result["resumed_lanes"] == ["lane-a"]
    assert queue.store.lane_status("lane-a")["paused"] is False
    # lane-b's own unrelated, pre-existing pause is left exactly as it was.
    assert queue.store.lane_status("lane-b")["paused"] is True
    assert queue.store.lane_status("lane-b")["paused_reason"] == "manual maintenance window"


def test_emergency_resume_is_a_no_op_when_nothing_was_emergency_stopped(queue):
    queue.create_task("t", "p", session="lane-a")
    result = emergency_resume_all_lanes(queue)
    assert result == {"resumed_lanes": [], "count": 0}
