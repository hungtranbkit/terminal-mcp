"""Planner (task-breaking) -- MCP tool surface (docs/REQUIREMENTS.md
§20.3). Exercises the real MCP call path, same pattern as
test_pm_mcp_tools.py.

SAFETY: every session name below is disposable -- never
`window`/`window2`/`wtest`."""
from __future__ import annotations

import json

import pytest

from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.planner_service import PlannerService
from terminal_mcp.planner_store import PlannerStore
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
    planner = PlannerService(PlannerStore(tmp_path / "planner.db"), queue)
    return build_mcp(queue=queue, planner=planner)


@pytest.mark.anyio
async def test_task_split_suggest_then_approve_through_real_mcp_path(server):
    created = await _call(server, "terminal_task_create", title="Big task", prompt="do the big thing")
    parent_id = created["task_id"]
    children = [
        {"title": "part 1", "prompt": "do part 1", "acceptance_criteria": "part 1 works"},
        {"title": "part 2", "prompt": "do part 2", "acceptance_criteria": "part 2 works"},
    ]
    proposed = await _call(server, "terminal_task_split", parent_task_id=parent_id, children=children,
                          mode="SUGGEST")
    assert proposed["proposal"]["status"] == "PROPOSED"
    board = await _call(server, "terminal_task_board")
    assert board["counts"]["backlog"] == 1  # nothing created yet

    approved = await _call(server, "terminal_task_approve_plan",
                          proposal_id=proposed["proposal"]["proposal_id"])
    assert len(approved["child_task_ids"]) == 2
    board_after = await _call(server, "terminal_task_board")
    assert board_after["counts"]["blocked_review"] == 1  # the split parent
    assert board_after["counts"]["backlog"] == 2  # the 2 unassigned children


@pytest.mark.anyio
async def test_task_split_missing_acceptance_criteria_is_needs_clarification(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    result = await _call(server, "terminal_task_split", parent_task_id=created["task_id"],
                        children=[{"title": "c", "prompt": "do it"}], mode="SUGGEST")
    assert result["proposal"]["status"] == "NEEDS_CLARIFICATION"


@pytest.mark.anyio
async def test_task_children_reports_real_progress_through_mcp_path(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    children = [{"title": "c1", "prompt": "p1", "acceptance_criteria": "a1", "session": "lane-a"}]
    split = await _call(server, "terminal_task_split", parent_task_id=created["task_id"],
                       children=children, mode="AUTO")
    (child_id,) = split["child_task_ids"]

    progress = await _call(server, "terminal_task_children", parent_task_id=created["task_id"])
    assert progress["total"] == 1
    assert progress["done"] == 0
    assert progress["children"][0]["id"] == child_id

    # Not done yet -- terminal_task_complete_parent must correctly refuse.
    completion = await _call(server, "terminal_task_complete_parent", parent_task_id=created["task_id"])
    assert completion["completed"] is False


@pytest.mark.anyio
async def test_task_complete_parent_refuses_non_split_task(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    result = await _call(server, "terminal_task_complete_parent", parent_task_id=created["task_id"])
    assert result["error"] == "NOT_A_SPLIT_PARENT"
