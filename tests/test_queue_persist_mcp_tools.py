"""P0 persist-before-dispatch -- new MCP tool surface (terminal_enqueue_
task, terminal_task_status, terminal_queue_metrics) and the
queue_conflict_warning on the low-level terminal_send_text bypass path.
Real MCP call path (server.call_tool), same pattern as
test_queue_mcp_tools.py.

SAFETY: every session here is disposable, never window/window2."""
from __future__ import annotations

import json
import time

import pytest

@pytest.fixture(autouse=True)
def _enable_legacy_queue_for_existing_engine_tests(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_ENABLE_QUEUE", "1")

from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def server(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    return build_mcp(queue=queue), queue


@pytest.mark.anyio
async def test_enqueue_task_returns_task_accepted_with_queue_position(server):
    mcp_server, queue = server
    result = await _call(mcp_server, "terminal_enqueue_task", session="lane-a", prompt="do the feature work")
    assert result["status"] == "TASK_ACCEPTED"
    assert result["queue_position"] == 1
    assert result["session"] == "lane-a"
    task = queue.store.get_task(result["task_id"])
    assert task is not None
    assert task.prompt == "do the feature work"  # stored verbatim
    assert task.status == "QUEUED"


@pytest.mark.anyio
async def test_enqueue_task_never_cancels_existing_queued_tasks(server):
    mcp_server, queue = server
    first = await _call(mcp_server, "terminal_enqueue_task", session="lane-a", prompt="first task")
    second = await _call(mcp_server, "terminal_enqueue_task", session="lane-a", prompt="second task")
    assert queue.store.get_task(first["task_id"]).status == "QUEUED"  # never cancelled
    assert second["queue_position"] == 2


@pytest.mark.anyio
async def test_ten_sequential_enqueues_via_mcp_preserve_order_none_missing(server):
    mcp_server, queue = server
    task_ids = []
    for i in range(10):
        result = await _call(mcp_server, "terminal_enqueue_task", session="lane-a", prompt=f"task {i}")
        assert result["status"] == "TASK_ACCEPTED"
        task_ids.append(result["task_id"])
    status = await _call(mcp_server, "terminal_queue_status", session="lane-a")
    assert [t["prompt"] for t in status["tasks"]] == [f"task {i}" for i in range(10)]
    assert len(set(task_ids)) == 10


@pytest.mark.anyio
async def test_task_status_looks_up_by_id_without_knowing_the_session(server):
    mcp_server, queue = server
    result = await _call(mcp_server, "terminal_enqueue_task", session="lane-a", prompt="track me")
    status = await _call(mcp_server, "terminal_task_status", task_id=result["task_id"])
    assert status["task"]["session"] == "lane-a"
    assert status["task"]["prompt"] == "track me"
    assert status["queue_position"] == 1


@pytest.mark.anyio
async def test_task_status_not_found(server):
    mcp_server, queue = server
    result = await _call(mcp_server, "terminal_task_status", task_id="does-not-exist")
    assert result["error"] == "TASK_NOT_FOUND"


@pytest.mark.anyio
async def test_queue_metrics_reports_zero_missed_dropped_and_real_depth(server):
    mcp_server, queue = server
    for i in range(3):
        await _call(mcp_server, "terminal_enqueue_task", session="lane-a", prompt=f"task {i}")
    metrics = await _call(mcp_server, "terminal_queue_metrics", session="lane-a")
    assert metrics["queued_depth"] == 3
    assert metrics["missed_count"] == 0
    assert metrics["dropped_count"] == 0


@pytest.mark.anyio
async def test_native_queue_submit_tools_fail_closed_without_server_opt_in(server, monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_ENABLE_QUEUE", raising=False)
    mcp_server, _queue = server
    before = await _call(mcp_server, "terminal_queue_metrics", session="lane-a")
    assert before["queued_depth"] == 0

    enqueued = await _call(
        mcp_server, "terminal_enqueue_task", session="lane-a", prompt="must not persist"
    )
    assert enqueued["error"] == "QUEUE_DISABLED_USE_DIRECT_SESSION"
    assert enqueued["next_action"] == "create_session_then_send"

    routed = await _call(mcp_server, "terminal_route_start", prompt="must not route")
    assert routed["error"] == "QUEUE_DISABLED_USE_DIRECT_SESSION"

    after = await _call(mcp_server, "terminal_queue_metrics", session="lane-a")
    assert after["queued_depth"] == 0
