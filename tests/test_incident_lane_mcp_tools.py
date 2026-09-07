"""Incident lane (docs/REQUIREMENTS.md §20.6 Phase B) -- MCP tool
surface. NOT a parallel queue -- a real task in the SAME lane,
fast-tracked by priority. Exercises the real MCP call path, same
pattern as test_dor_gate_mcp_tools.py."""
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
async def test_task_create_incident_through_real_mcp_path(server):
    created = await _call(server, "terminal_task_create_incident", title="Prod down",
                         prompt="investigate the outage", assigned_session_id="lane-a", risk_level="CRITICAL")
    assert "error" not in created
    status = await _call(server, "terminal_task_status", task_id=created["task_id"])
    assert status["task"]["metadata"]["type"] == "incident"
    assert status["task"]["metadata"]["risk_level"] == "CRITICAL"


@pytest.mark.anyio
async def test_list_active_incidents_through_real_mcp_path(server):
    await _call(server, "terminal_task_create", title="normal", prompt="p", assigned_session_id="lane-a")
    incident = await _call(server, "terminal_task_create_incident", title="Prod down", prompt="p",
                          assigned_session_id="lane-b")
    result = await _call(server, "terminal_list_active_incidents")
    assert result["count"] == 1
    assert result["incidents"][0]["id"] == incident["task_id"]


@pytest.mark.anyio
async def test_incident_still_enforces_dor_when_opted_in(server):
    result = await _call(server, "terminal_task_create_incident", title="Prod down", prompt="p",
                        assigned_session_id="lane-a", metadata={"dor_required": True})
    assert result["error"] == "NEEDS_CLARIFICATION"
