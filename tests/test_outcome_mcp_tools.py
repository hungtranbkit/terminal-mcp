"""Orchestration V1 outcome layer -- MCP tool surface, through the real
server.call_tool path. The acceptance gate is the thing that must survive
the tool boundary intact: a wrapper that dropped `evidence` would turn the
strictest gate in the system into a rubber stamp."""
from __future__ import annotations

import json

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

PROJECT = "git:github.com/acme/widget"
CRITERIA = ["renders offline", "no data loss"]
GOOD = {"command": "pytest -q", "exit_code": 0, "test_results": "9 passed"}


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def rig(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    return build_mcp(queue=queue), queue


def finished_task(queue, *, title="t") -> str:
    (task_id,) = queue.store.set_tasks("lane-a", [{"title": title, "prompt": "p"}],
                                       replace_pending=False)
    for target, event in ((qs.DISPATCHING, "D"), (qs.RUNNING, "R"), (qs.VERIFYING, "V")):
        queue.store.transition_task(task_id, target, event_type=event)
    queue.store.mark_completed_with_evidence(task_id, evidence={"exit_code": 0})
    return task_id


@pytest.mark.anyio
async def test_full_outcome_lifecycle_through_the_tools(rig):
    server, queue = rig
    created = await _call(server, "terminal_outcome_create", project_id=PROJECT,
                          title="Overview ships", acceptance_criteria=CRITERIA,
                          backlog_id="BL-7")
    outcome_id = created["outcome"]["id"]
    assert created["outcome"]["status"] == "OPEN"

    for i in range(3):
        attached = await _call(server, "terminal_outcome_attach_task",
                               outcome_id=outcome_id, task_id=finished_task(queue, title=f"t{i}"))
        assert attached["attached"] is True

    status = await _call(server, "terminal_outcome_status", outcome_id=outcome_id)
    assert status["outcome"]["status"] == "AWAITING_ACCEPTANCE"
    assert status["progress"]["total"] == 3 and status["progress"]["percent"] == 100.0

    done = await _call(server, "terminal_outcome_complete", outcome_id=outcome_id,
                       evidence={CRITERIA[0]: GOOD, CRITERIA[1]: {"artifact": "trace.zip"}})
    assert done["ok"] is True and done["outcome"]["status"] == "DONE"

    trace = await _call(server, "terminal_outcome_trace", outcome_id=outcome_id)
    assert trace["outcome"]["backlog_id"] == "BL-7"
    assert len(trace["tasks"]) == 3


@pytest.mark.anyio
async def test_the_acceptance_gate_survives_the_tool_boundary(rig):
    """A wrapper that dropped `evidence` would make the strictest gate in
    the system a rubber stamp."""
    server, queue = rig
    outcome_id = (await _call(server, "terminal_outcome_create", project_id=PROJECT,
                              title="Ship", acceptance_criteria=CRITERIA))["outcome"]["id"]
    await _call(server, "terminal_outcome_attach_task", outcome_id=outcome_id,
                task_id=finished_task(queue))
    await _call(server, "terminal_outcome_status", outcome_id=outcome_id)

    empty = await _call(server, "terminal_outcome_complete", outcome_id=outcome_id, evidence={})
    assert empty["error"] == "ACCEPTANCE_EVIDENCE_REQUIRED"
    assert sorted(empty["missing_criteria"]) == sorted(CRITERIA)

    self_report = await _call(server, "terminal_outcome_complete", outcome_id=outcome_id,
                              evidence={c: {"summary": "looks fine"} for c in CRITERIA})
    assert self_report["error"] == "ACCEPTANCE_EVIDENCE_REQUIRED"
    assert set(self_report["rejected_criteria"]) == set(CRITERIA)
    assert (await _call(server, "terminal_outcome_status",
                        outcome_id=outcome_id))["outcome"]["status"] == "AWAITING_ACCEPTANCE"


@pytest.mark.anyio
async def test_open_children_block_completion_through_the_tool(rig):
    server, queue = rig
    outcome_id = (await _call(server, "terminal_outcome_create", project_id=PROJECT,
                              title="Ship", acceptance_criteria=["c"]))["outcome"]["id"]
    (open_id,) = queue.store.set_tasks("lane-b", [{"title": "wip", "prompt": "p"}])
    await _call(server, "terminal_outcome_attach_task", outcome_id=outcome_id, task_id=open_id)

    refused = await _call(server, "terminal_outcome_complete", outcome_id=outcome_id,
                          evidence={"c": GOOD})
    assert refused["error"] == "TASKS_STILL_OPEN"
    assert refused["open_tasks"] == [open_id]


@pytest.mark.anyio
async def test_creation_refuses_an_outcome_with_no_criteria(rig):
    server, _ = rig
    result = await _call(server, "terminal_outcome_create", project_id=PROJECT,
                         title="Vague", acceptance_criteria=[])
    assert result["error"] == "INVALID_OUTCOME"
    assert "acceptance criterion" in result["detail"]


@pytest.mark.anyio
async def test_block_unblock_and_listing_through_the_tools(rig):
    server, queue = rig
    outcome_id = (await _call(server, "terminal_outcome_create", project_id=PROJECT,
                              title="Ship", acceptance_criteria=["c"]))["outcome"]["id"]
    (task_id,) = queue.store.set_tasks("lane-c", [{"title": "wip", "prompt": "p"}])
    await _call(server, "terminal_outcome_attach_task", outcome_id=outcome_id, task_id=task_id)

    blocked = await _call(server, "terminal_outcome_block", outcome_id=outcome_id,
                          reason="waiting on a design decision")
    assert blocked["outcome"]["status"] == "BLOCKED"
    listed = await _call(server, "terminal_outcome_list", project_id=PROJECT, status="BLOCKED")
    assert listed["count"] == 1
    assert (await _call(server, "terminal_outcome_unblock",
                        outcome_id=outcome_id))["outcome"]["status"] == "IN_PROGRESS"


@pytest.mark.anyio
async def test_unknown_ids_are_structured_refusals(rig):
    server, queue = rig
    assert (await _call(server, "terminal_outcome_status",
                        outcome_id="nope"))["error"] == "OUTCOME_NOT_FOUND"
    assert (await _call(server, "terminal_outcome_attach_task", outcome_id="nope",
                        task_id="x"))["error"] == "ATTACH_REFUSED"
    assert (await _call(server, "terminal_outcome_complete", outcome_id="nope",
                        evidence={}))["error"] == "OUTCOME_NOT_FOUND"
    assert "error" in await _call(server, "terminal_outcome_trace", outcome_id="nope")


@pytest.fixture
def anyio_backend():
    return "asyncio"
