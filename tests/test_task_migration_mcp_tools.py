"""Task Migration / Load Balancing -- MCP tool surface, real disposable
tmux sessions (task item 15's own "Live disposable E2E: tạo 3 workers
A/B/C; A có 8 queued task, B/C rảnh -> dry-run plan -> migrate
balanced"). Real MCP call path (server.call_tool), real tmux sessions,
real ControllerService -- NOT window/window2.

The concurrent claim-vs-reassign race and the restart-recovery proof
are already covered, more rigorously (real threading.Thread, a real
simulated process restart), at the store layer in
test_task_migration_store.py -- this file focuses on what only the real
MCP+tmux path can prove: the tools work end to end, and 'B offline'
really means an unresolvable real session, not a mocked one."""
from __future__ import annotations

import json
import time

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("migrate-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("migrate-*",), max_text_length=2000),
    )
    return TerminalService(config, bindings=BindingStore(tmp_path / "bindings.db"),
                          audit=AuditStore(tmp_path / "audit.db"))


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def rig(tmp_path, tmux_session_factory):
    service = _service(tmp_path)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(service, controller=controller, queue=queue)

    def make_idle_worker(name: str) -> None:
        tmux_session_factory(name, "bash -lc 'sleep 300'")

    return {"server": server, "queue": queue, "controller": controller, "make_worker": make_idle_worker}


@pytest.mark.anyio
async def test_three_workers_a_overloaded_b_c_idle_dry_run_then_apply(rig):
    server = rig["server"]
    for name in ("migrate-a", "migrate-b", "migrate-c"):
        rig["make_worker"](name)
        await _call(server, "terminal_task_set_project", session=name, project="proj-x")

    for i in range(8):
        await _call(server, "terminal_enqueue_task", session="migrate-a", prompt=f"feature {i}")

    # Dry-run first (item 10's own requirement) -- applies nothing.
    plan_result = await _call(server, "terminal_task_rebalance_plan", project="proj-x",
                              sessions=["migrate-a", "migrate-b", "migrate-c"])
    assert plan_result["move_count"] > 0
    status_a_before = await _call(server, "terminal_queue_status", session="migrate-a")
    assert status_a_before["queued_count"] == 8  # untouched by the dry-run

    rebalance_dry = await _call(server, "terminal_task_rebalance", project="proj-x",
                                sessions=["migrate-a", "migrate-b", "migrate-c"], dry_run=True)
    assert rebalance_dry["applied"] is False
    status_a_still = await _call(server, "terminal_queue_status", session="migrate-a")
    assert status_a_still["queued_count"] == 8

    # NOW actually apply.
    applied = await _call(server, "terminal_task_rebalance", project="proj-x",
                         sessions=["migrate-a", "migrate-b", "migrate-c"], dry_run=False)
    assert applied["applied"] is True
    assert all(r["status"] == "MIGRATED" for r in applied["results"])

    status_a = await _call(server, "terminal_queue_status", session="migrate-a")
    status_b = await _call(server, "terminal_queue_status", session="migrate-b")
    status_c = await _call(server, "terminal_queue_status", session="migrate-c")
    total = status_a["queued_count"] + status_b["queued_count"] + status_c["queued_count"]
    assert total == 8  # nothing lost, nothing duplicated
    assert status_a["queued_count"] < 8  # a is now less loaded
    assert status_b["queued_count"] > 0 or status_c["queued_count"] > 0  # actually redistributed

    # Every migrated task still has its own real, distinct assignment history.
    for row in status_b["tasks"] + status_c["tasks"]:
        assert row["original_owner"] == "migrate-a"
        assert len(row["migration_history"]) == 1


@pytest.mark.anyio
async def test_migrated_task_preserves_prompt_and_metadata_verbatim(rig):
    server = rig["server"]
    for name in ("migrate-a", "migrate-b"):
        rig["make_worker"](name)
        await _call(server, "terminal_task_set_project", session=name, project="proj-x")
    enqueue_result = await _call(server, "terminal_enqueue_task", session="migrate-a",
                                 prompt="implement the real feature", metadata={"ticket": "JIRA-123"})
    task_id = enqueue_result["task_id"]

    result = await _call(server, "terminal_task_reassign", task_id=task_id, to_session="migrate-b",
                         reason="load balancing", actor="chatgpt")
    assert result["task"]["session"] == "migrate-b"
    assert result["task"]["prompt"] == "implement the real feature"
    assert result["task"]["metadata"] == {"ticket": "JIRA-123"}

    history = await _call(server, "terminal_task_assignment_history", task_id=task_id)
    assert history["original_owner"] == "migrate-a"
    assert history["current_session"] == "migrate-b"


@pytest.mark.anyio
async def test_offline_session_is_never_a_rebalance_destination(rig):
    """item: 'simulate B offline' -- a session name that was never
    created (or no longer exists) is a REAL SESSION_NOT_FOUND from the
    real controller, not a mocked offline flag."""
    server = rig["server"]
    rig["make_worker"]("migrate-a")
    # migrate-b deliberately never created -- genuinely offline/missing.
    await _call(server, "terminal_task_set_project", session="migrate-a", project="proj-x")
    for i in range(8):
        await _call(server, "terminal_enqueue_task", session="migrate-a", prompt=f"feature {i}")

    plan_result = await _call(server, "terminal_task_rebalance_plan", project="proj-x",
                              sessions=["migrate-a", "migrate-b"])
    assert plan_result["move_count"] == 0  # no eligible (online) destination at all
    status_a = await _call(server, "terminal_queue_status", session="migrate-a")
    assert status_a["queued_count"] == 8  # nothing moved


@pytest.mark.anyio
async def test_reassign_refuses_a_running_task(rig):
    server = rig["server"]
    queue = rig["queue"]
    for name in ("migrate-a", "migrate-b"):
        rig["make_worker"](name)
    result = await _call(server, "terminal_enqueue_task", session="migrate-a", prompt="a real running task")
    task_id = result["task_id"]
    queue.store.transition_task(task_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(task_id, "READY", event_type="TEST")
    queue.store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(task_id, "RUNNING", event_type="TEST")

    reassign_result = await _call(server, "terminal_task_reassign", task_id=task_id, to_session="migrate-b",
                                  reason="test", actor="chatgpt")
    assert reassign_result["error"] == "TASK_ALREADY_CLAIMED"
    status = await _call(server, "terminal_task_status", task_id=task_id)
    assert status["task"]["session"] == "migrate-a"  # untouched
    assert status["task"]["status"] == "RUNNING"


@pytest.mark.anyio
async def test_restart_recovers_migrated_assignment_no_duplicate(rig, tmp_path):
    """item 13/15: controller restart mid-flow -- a brand new QueueStore
    instance, same db file, sees the exact same (already-committed)
    assignment; nothing duplicated, nothing lost."""
    server = rig["server"]
    queue = rig["queue"]
    for name in ("migrate-a", "migrate-b"):
        rig["make_worker"](name)
    result = await _call(server, "terminal_enqueue_task", session="migrate-a", prompt="a real task")
    task_id = result["task_id"]
    await _call(server, "terminal_task_reassign", task_id=task_id, to_session="migrate-b", reason="test",
               actor="chatgpt")

    store2 = QueueStore(tmp_path / "queue.db")  # fresh instance, same file -- "controller restarted"
    task = store2.get_task(task_id)
    assert task.session == "migrate-b"
    assert len(task.migration_history) == 1
    all_tasks = store2.lane_status("migrate-a")["tasks"] + store2.lane_status("migrate-b")["tasks"]
    assert len([t for t in all_tasks if t["id"] == task_id]) == 1  # exactly one copy, never duplicated
