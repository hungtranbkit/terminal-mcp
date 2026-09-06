"""TaskMigrationPlanner -- load computation, eligibility gate, and
rebalance planning (task: "bổ sung Task Migration / Load Balancing").
Uses a FakeOps (no real tmux/ConPTY session), same pattern as
test_queue_engine.py's own FakeOps.

SAFETY: every session name here is disposable."""
from __future__ import annotations

import pytest

from terminal_mcp.queue_store import QueueStore
from terminal_mcp.task_migration import TaskMigrationPlanner


class FakeOps:
    def __init__(self):
        self.status_by_session = {}

    def set_status(self, session, response):
        self.status_by_session[session] = response

    def terminal_status(self, session):
        return self.status_by_session.get(session, {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def ops():
    return FakeOps()


# ---------------------------------------------------------------------------
# Item 3: load computed from queue depth/age/current runtime/health, not
# just a bare task count.
# ---------------------------------------------------------------------------

def test_compute_load_reflects_queue_depth(store, ops):
    store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])
    ops.set_status("lane-a", {"state": "IDLE"})
    planner = TaskMigrationPlanner(store, ops)
    load = planner.compute_load("lane-a")
    assert load.queued_depth == 3
    assert load.online is True


def test_compute_load_reports_offline_when_session_unreachable(store, ops):
    ops.set_status("lane-a", {"error": "SESSION_NOT_FOUND"})
    planner = TaskMigrationPlanner(store, ops)
    load = planner.compute_load("lane-a")
    assert load.online is False


def test_compute_load_reports_current_task_runtime(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "a"}])
    store.transition_task(task_id, "PRECHECK", event_type="TEST")
    store.transition_task(task_id, "READY", event_type="TEST")
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store.transition_task(task_id, "RUNNING", event_type="TEST")
    ops.set_status("lane-a", {"state": "RUNNING"})
    planner = TaskMigrationPlanner(store, ops)
    load = planner.compute_load("lane-a")
    assert load.has_active_task is True
    assert load.current_task_runtime_seconds is not None
    assert load.current_task_runtime_seconds >= 0


# ---------------------------------------------------------------------------
# Item 6: eligibility gate -- online + branch/worktree/node compatibility.
# ---------------------------------------------------------------------------

def test_eligible_destination_fails_closed_when_unreachable(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "a"}])
    task = store.get_task(task_id)
    ops.set_status("lane-b", {"error": "NODE_UNREACHABLE"})
    planner = TaskMigrationPlanner(store, ops)
    ok, reason = planner.eligible_destination(task, "lane-b")
    assert ok is False
    assert "unreachable" in reason.lower()


def test_eligible_destination_rejects_cwd_mismatch(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "a", "metadata": {"expected_cwd": "/repo/expected"}}])
    task = store.get_task(task_id)
    ops.set_status("lane-b", {"state": "IDLE", "cwd": "/repo/wrong"})
    planner = TaskMigrationPlanner(store, ops)
    ok, reason = planner.eligible_destination(task, "lane-b")
    assert ok is False
    assert "worktree" in reason.lower() or "cwd" in reason.lower()


def test_eligible_destination_passes_with_matching_cwd(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "a", "metadata": {"expected_cwd": "/repo/shared"}}])
    task = store.get_task(task_id)
    ops.set_status("lane-b", {"state": "IDLE", "cwd": "/repo/shared"})
    planner = TaskMigrationPlanner(store, ops)
    ok, reason = planner.eligible_destination(task, "lane-b")
    assert ok is True


def test_eligible_destination_passes_when_task_declares_no_constraints(store, ops):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "a"}])
    task = store.get_task(task_id)
    ops.set_status("lane-b", {"state": "IDLE", "cwd": "/anywhere"})
    planner = TaskMigrationPlanner(store, ops)
    ok, _ = planner.eligible_destination(task, "lane-b")
    assert ok is True


# ---------------------------------------------------------------------------
# Item 15's own throughput scenario: A has 8 queued, B/C idle -> plan
# balances across them.
# ---------------------------------------------------------------------------

def test_plan_rebalance_moves_from_overloaded_to_idle_sessions(store, ops):
    store.set_lane_project("lane-a", "proj-1")
    store.set_lane_project("lane-b", "proj-1")
    store.set_lane_project("lane-c", "proj-1")
    store.set_tasks("lane-a", [{"prompt": f"task-{i}"} for i in range(8)])
    for session in ("lane-a", "lane-b", "lane-c"):
        ops.set_status(session, {"state": "IDLE", "node_id": "local", "cwd": "/repo/shared"})
    planner = TaskMigrationPlanner(store, ops)
    plan = planner.plan_rebalance("proj-1", ["lane-a", "lane-b", "lane-c"])
    assert len(plan) > 0
    # Every move originates from lane-a (the only overloaded one).
    assert all(move["from_session"] == "lane-a" for move in plan)
    destinations = {move["to_session"] for move in plan}
    assert destinations <= {"lane-b", "lane-c"}


def test_plan_rebalance_never_proposes_a_move_below_the_imbalance_threshold(store, ops):
    store.set_lane_project("lane-a", "proj-1")
    store.set_lane_project("lane-b", "proj-1")
    store.set_tasks("lane-a", [{"prompt": "a"}])  # only 1 more than lane-b
    store.set_tasks("lane-b", [{"prompt": "b"}])
    for session in ("lane-a", "lane-b"):
        ops.set_status(session, {"state": "IDLE"})
    planner = TaskMigrationPlanner(store, ops)
    plan = planner.plan_rebalance("proj-1", ["lane-a", "lane-b"], imbalance_threshold=2)
    assert plan == []


def test_plan_rebalance_never_leaks_across_projects(store, ops):
    """item 11: sessions in a DIFFERENT project are never candidates,
    even if explicitly passed in the caller's own session list."""
    store.set_lane_project("lane-a", "proj-1")
    store.set_lane_project("lane-x", "proj-OTHER")
    store.set_tasks("lane-a", [{"prompt": f"task-{i}"} for i in range(8)])
    ops.set_status("lane-a", {"state": "IDLE"})
    ops.set_status("lane-x", {"state": "IDLE"})
    planner = TaskMigrationPlanner(store, ops)
    plan = planner.plan_rebalance("proj-1", ["lane-a", "lane-x"])
    assert all(move["to_session"] != "lane-x" for move in plan)


def test_plan_rebalance_never_moves_to_an_offline_session(store, ops):
    store.set_lane_project("lane-a", "proj-1")
    store.set_lane_project("lane-b", "proj-1")
    store.set_tasks("lane-a", [{"prompt": f"task-{i}"} for i in range(8)])
    ops.set_status("lane-a", {"state": "IDLE"})
    ops.set_status("lane-b", {"error": "SESSION_NOT_FOUND"})  # offline
    planner = TaskMigrationPlanner(store, ops)
    plan = planner.plan_rebalance("proj-1", ["lane-a", "lane-b"])
    assert plan == []  # no eligible destination at all


# ---------------------------------------------------------------------------
# Item 5: cooldown/hysteresis -- a recently-rebalanced session is never
# a SOURCE again right away.
# ---------------------------------------------------------------------------

def test_plan_rebalance_respects_cooldown_as_a_source(store, ops):
    store.set_lane_project("lane-a", "proj-1")
    store.set_lane_project("lane-b", "proj-1")
    store.set_tasks("lane-a", [{"prompt": f"task-{i}"} for i in range(8)])
    store.mark_rebalanced("lane-a")  # just touched -- in cooldown
    for session in ("lane-a", "lane-b"):
        ops.set_status(session, {"state": "IDLE"})
    planner = TaskMigrationPlanner(store, ops, cooldown_seconds=3600)
    plan = planner.plan_rebalance("proj-1", ["lane-a", "lane-b"])
    assert plan == []  # lane-a is in cooldown, never a source


def test_plan_rebalance_ignores_cooldown_when_respect_cooldown_false(store, ops):
    store.set_lane_project("lane-a", "proj-1")
    store.set_lane_project("lane-b", "proj-1")
    store.set_tasks("lane-a", [{"prompt": f"task-{i}"} for i in range(8)])
    store.mark_rebalanced("lane-a")
    for session in ("lane-a", "lane-b"):
        ops.set_status(session, {"state": "IDLE"})
    planner = TaskMigrationPlanner(store, ops, cooldown_seconds=3600)
    plan = planner.plan_rebalance("proj-1", ["lane-a", "lane-b"], respect_cooldown=False)
    assert len(plan) > 0  # explicit override (e.g. a manual force-rebalance)


# ---------------------------------------------------------------------------
# apply_plan: real reassignment, race-safe partial application.
# ---------------------------------------------------------------------------

def test_apply_plan_migrates_tasks_and_stamps_cooldown(store, ops):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}])
    plan = [{"task_id": ids[0], "from_session": "lane-a", "to_session": "lane-b", "reason": "test"}]
    planner = TaskMigrationPlanner(store, ops)
    results = planner.apply_plan(plan, actor="operator")
    assert results[0]["status"] == "MIGRATED"
    assert store.get_task(ids[0]).session == "lane-b"
    assert store.lane_status("lane-a")["last_rebalance_at"] is not None
    assert store.lane_status("lane-b")["last_rebalance_at"] is not None


def test_apply_plan_reports_failure_for_an_already_claimed_task_without_aborting_the_rest(store, ops):
    ids = store.set_tasks("lane-a", [{"prompt": "a"}, {"prompt": "b"}])
    store.claim_next_task("lane-a", claimed_by="engine-1")  # a is now claimed
    plan = [
        {"task_id": ids[0], "from_session": "lane-a", "to_session": "lane-b", "reason": "test"},
        {"task_id": ids[1], "from_session": "lane-a", "to_session": "lane-b", "reason": "test"},
    ]
    planner = TaskMigrationPlanner(store, ops)
    results = planner.apply_plan(plan, actor="operator")
    assert results[0]["status"] == "FAILED"
    assert results[1]["status"] == "MIGRATED"
    assert store.get_task(ids[1]).session == "lane-b"
