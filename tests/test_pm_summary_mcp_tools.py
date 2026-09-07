"""PM summary + backlog hygiene (docs/REQUIREMENTS.md §20.6 Phase D) --
MCP tool surface. Exercises the real MCP call path."""
from __future__ import annotations

import json

import pytest

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
    return build_mcp(queue=queue)


@pytest.mark.anyio
async def test_pm_summary_through_real_mcp_path(server):
    await _call(server, "terminal_task_create", title="t1", prompt="p1")
    result = await _call(server, "terminal_pm_summary")
    assert result["board_counts"]["backlog"] == 1
    assert "generated_at" in result


@pytest.mark.anyio
async def test_detect_duplicate_tasks_through_real_mcp_path(server):
    await _call(server, "terminal_task_create", title="t1", prompt="the exact same prompt")
    await _call(server, "terminal_task_create", title="t2", prompt="the exact same prompt")
    result = await _call(server, "terminal_pm_detect_duplicate_tasks")
    assert result["count"] == 1


@pytest.mark.anyio
async def test_close_task_with_confirmation_through_real_mcp_path(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    refused = await _call(server, "terminal_pm_close_task_with_confirmation", task_id=created["task_id"],
                         reason="obsolete")
    assert refused["error"] == "CONFIRMATION_REQUIRED"

    confirmed = await _call(server, "terminal_pm_close_task_with_confirmation", task_id=created["task_id"],
                           reason="obsolete", confirmed=True)
    assert confirmed["task"]["status"] == "CANCELLED"


@pytest.mark.anyio
async def test_detect_stale_backlog_through_real_mcp_path(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    result = await _call(server, "terminal_pm_detect_stale_backlog", stale_after_hours=0.0)
    # 0-hour threshold -- a task created just now is already "stale" by this loose bound.
    assert result["count"] == 1
    assert result["stale_tasks"][0]["id"] == created["task_id"]
