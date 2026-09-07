"""Delivery discipline: Definition of Ready + WIP limits (docs/
REQUIREMENTS.md §20.6 Phase A) -- MCP tool surface. Exercises the real
MCP call path, same pattern as test_pm_mcp_tools.py."""
from __future__ import annotations

import json

import pytest

from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.pm_service import PMService
from terminal_mcp.pm_store import PMStore
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
    pm = PMService(PMStore(tmp_path / "pm.db"), queue)
    return build_mcp(queue=queue, pm=pm)


@pytest.mark.anyio
async def test_task_check_dor_ready_when_not_opted_in(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    result = await _call(server, "terminal_task_check_dor", task_id=created["task_id"])
    assert result["status"] == "READY"


@pytest.mark.anyio
async def test_task_check_dor_needs_clarification_when_opted_in_and_incomplete(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p",
                         metadata={"dor_required": True})
    result = await _call(server, "terminal_task_check_dor", task_id=created["task_id"])
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "acceptance_criteria" in result["missing_fields"]


@pytest.mark.anyio
async def test_task_assign_refuses_dor_incomplete_task_through_real_mcp_path(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p",
                         metadata={"dor_required": True})
    result = await _call(server, "terminal_task_assign", task_id=created["task_id"], session="lane-a")
    assert result["error"] == "NEEDS_CLARIFICATION"
    board = await _call(server, "terminal_task_board")
    assert board["counts"]["backlog"] == 1


@pytest.mark.anyio
async def test_task_assign_allows_dor_complete_task(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p", metadata={
        "dor_required": True, "acceptance_criteria": "works", "project": "P", "risk_level": "LOW",
    })
    result = await _call(server, "terminal_task_assign", task_id=created["task_id"], session="lane-a")
    assert "error" not in result


@pytest.mark.anyio
async def test_pm_wip_limit_through_real_mcp_path(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a", max_queued=1)
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-b")
    await _call(server, "terminal_task_create", title="existing", prompt="p", assigned_session_id="worker-a")
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    routed = await _call(server, "terminal_pm_route_task", task_id=created["task_id"], mode="SUGGEST")
    assert routed["decision"]["chosen_session"] == "worker-b"
