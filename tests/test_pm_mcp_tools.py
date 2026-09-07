"""PM/Orchestrator Agent -- MCP tool surface (docs/REQUIREMENTS.md
§20.2). Exercises the real MCP call path (server.call_tool), same
pattern as test_queue_mcp_tools.py -- calling PMService directly in
Python is not enough to catch a future "the tool wrapper forgot to
expose a parameter" gap.

SAFETY: every session name below is disposable ("worker-a" etc.) --
never `window`/`window2`/`wtest`."""
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
async def test_pm_set_capability_then_list(server):
    created = await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a",
                          os="linux", role="developer", skills=[{"name": "docker"}])
    assert created["capability"]["session"] == "worker-a"
    listed = await _call(server, "terminal_pm_list_capabilities")
    assert len(listed["capabilities"]) == 1


@pytest.mark.anyio
async def test_pm_delete_capability(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a")
    result = await _call(server, "terminal_pm_delete_capability", node_id="local", session="worker-a")
    assert result["deleted"] is True
    listed = await _call(server, "terminal_pm_list_capabilities")
    assert listed["capabilities"] == []


@pytest.mark.anyio
async def test_pm_route_task_suggest_then_approve_through_real_mcp_path(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a", os="linux")
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    task_id = created["task_id"]

    routed = await _call(server, "terminal_pm_route_task", task_id=task_id, mode="SUGGEST")
    assert routed["decision"]["status"] == "SUGGESTED"
    assert routed["decision"]["chosen_session"] == "worker-a"

    board = await _call(server, "terminal_task_board")
    assert board["counts"]["backlog"] == 1  # SUGGEST never assigns

    approved = await _call(server, "terminal_pm_approve_routing", task_id=task_id)
    assert approved["decision"]["status"] == "APPROVED_AND_ASSIGNED"
    board_after = await _call(server, "terminal_task_board")
    assert board_after["counts"]["backlog"] == 0
    assert board_after["counts"]["queued"] == 1


@pytest.mark.anyio
async def test_pm_route_task_auto_mode_assigns_immediately(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a", os="linux")
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    routed = await _call(server, "terminal_pm_route_task", task_id=created["task_id"], mode="AUTO")
    assert routed["decision"]["status"] == "ROUTED"
    board = await _call(server, "terminal_task_board")
    assert board["counts"]["queued"] == 1


@pytest.mark.anyio
async def test_pm_eligible_workers_through_real_mcp_path(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="linux-a", os="linux")
    await _call(server, "terminal_pm_set_capability", node_id="windows-node", session="win-a", os="windows")
    created = await _call(server, "terminal_task_create", title="t", prompt="p",
                          metadata={"required_os": "windows"})
    result = await _call(server, "terminal_pm_eligible_workers", task_id=created["task_id"])
    assert result["eligible"] == ["windows-node/win-a"]


@pytest.mark.anyio
async def test_pm_explain_returns_real_decision_history(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a")
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    await _call(server, "terminal_pm_route_task", task_id=created["task_id"], mode="SUGGEST")
    explained = await _call(server, "terminal_pm_explain", task_id=created["task_id"])
    assert len(explained["decisions"]) == 1
    assert explained["decisions"][0]["status"] == "SUGGESTED"


@pytest.mark.anyio
async def test_pm_route_all_unassigned_through_real_mcp_path(server):
    await _call(server, "terminal_pm_set_capability", node_id="local", session="worker-a")
    await _call(server, "terminal_task_create", title="t1", prompt="p1")
    await _call(server, "terminal_task_create", title="t2", prompt="p2")
    result = await _call(server, "terminal_pm_route_all_unassigned", mode="SUGGEST")
    assert result["routed_count"] == 2


@pytest.mark.anyio
async def test_pm_route_task_no_eligible_worker_never_drops_task(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    routed = await _call(server, "terminal_pm_route_task", task_id=created["task_id"], mode="SUGGEST")
    assert routed["decision"]["status"] == "NO_ELIGIBLE_WORKER"
    board = await _call(server, "terminal_task_board")
    assert board["counts"]["backlog"] == 1
