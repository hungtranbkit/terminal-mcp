"""The backlog through the REAL MCP surface -- the exact path ChatGPT
takes. Exercises the documented workflow end to end:
get -> add -> update -> dispatch -> verify -> complete.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import COMPLETED, DISPATCHING, RUNNING, VERIFYING, QueueStore
from tests.test_backlog import make_config, make_repo


@pytest.fixture(autouse=True)
def _isolated_backlog_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BACKLOG_DB", str(tmp_path / "backlog.db"))


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def rig(tmp_path):
    repo = make_repo(tmp_path / "widget")
    config = make_config(tmp_path)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    backlog = BacklogService(config, queue=queue)
    server = build_mcp(TerminalService(config), queue=queue, backlog=backlog)
    return server, repo, queue


async def _call(server, tool, **kwargs):
    result = await server.call_tool(tool, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_all_backlog_tools_are_registered(rig):
    server, _, _ = rig
    names = {t.name for t in await server.list_tools()}
    for tool in ("terminal_backlog_get", "terminal_backlog_add", "terminal_backlog_update",
                 "terminal_backlog_bulk_update", "terminal_backlog_claim",
                 "terminal_backlog_dispatch", "terminal_backlog_block",
                 "terminal_backlog_complete", "terminal_backlog_validate"):
        assert tool in names, tool


@pytest.mark.anyio
async def test_tool_descriptions_teach_the_workflow(rig):
    """Item 14: ChatGPT must be able to learn the flow from the schema
    alone, without out-of-band instructions."""
    server, _, _ = rig
    tools = {t.name: (t.description or "") for t in await server.list_tools()}
    assert "get -> analyse -> add/update -> dispatch -> verify -> complete" in tools["terminal_backlog_get"]
    assert "EVIDENCE_REQUIRED" in tools["terminal_backlog_complete"]
    assert "REVISION_CONFLICT" in tools["terminal_backlog_update"]


@pytest.mark.anyio
async def test_full_chatgpt_workflow(rig):
    server, repo, queue = rig
    path = str(repo)

    empty = await _call(server, "terminal_backlog_get", path=path)
    assert empty["exists"] is False and empty["items"] == []
    project_id = empty["project"]["project_id"]

    added = await _call(server, "terminal_backlog_add", path=path, tasks=[
        {"title": "Add rate limiting", "priority": "P1", "type": "feature",
         "acceptance_criteria": ["429 after N requests"]},
        {"title": "Fix flaky test", "priority": "P0", "type": "bug"},
    ])
    assert len(added["created_ids"]) == 2

    listed = await _call(server, "terminal_backlog_get", path=path)
    assert listed["total"] == 2 and listed["project"]["project_id"] == project_id
    assert listed["counts"]["BACKLOG"] == 2 and listed["open_total"] == 2
    top = listed["items"][0]
    assert top["priority"] == "P0"                      # ordering is by priority

    groomed = await _call(server, "terminal_backlog_update", path=path,
                          task_id=top["id"], patch={"status": "READY"},
                          expected_revision=listed["revision"])
    assert groomed["item"]["status"] == "READY"

    dispatched = await _call(server, "terminal_backlog_dispatch", path=path, task_id=top["id"])
    qid = dispatched["queue_task_id"]
    assert qid and dispatched["item"]["status"] == "READY"

    # not verified yet -> DONE refused
    refused = await _call(server, "terminal_backlog_complete", path=path, task_id=top["id"])
    assert refused["error"] == "EVIDENCE_REQUIRED"

    for state in (DISPATCHING, RUNNING, VERIFYING, COMPLETED):
        queue.store.transition_task(qid, state, event_type="TEST_WALK")
    done = await _call(server, "terminal_backlog_complete", path=path, task_id=top["id"])
    assert done["item"]["status"] == "DONE" and done["verified_by"] == "queue_task"

    final = await _call(server, "terminal_backlog_get", path=path)
    assert final["counts"]["DONE"] == 1 and final["open_total"] == 1


@pytest.mark.anyio
async def test_path_security_through_the_tool(rig, tmp_path):
    server, _, _ = rig
    out = await _call(server, "terminal_backlog_get", path="/etc")
    assert out["error"] in ("PATH_NOT_ALLOWED", "NOT_A_PROJECT")


@pytest.mark.anyio
async def test_revision_conflict_through_the_tool(rig):
    server, repo, _ = rig
    path = str(repo)
    tid = (await _call(server, "terminal_backlog_add", path=path,
                       tasks=[{"title": "x"}]))["created_ids"][0]
    stale = (await _call(server, "terminal_backlog_get", path=path))["revision"]
    await _call(server, "terminal_backlog_update", path=path, task_id=tid, patch={"title": "A"})
    out = await _call(server, "terminal_backlog_update", path=path, task_id=tid,
                      patch={"title": "B"}, expected_revision=stale)
    assert out["error"] == "REVISION_CONFLICT"


@pytest.mark.anyio
async def test_backlog_tools_absent_when_not_configured(tmp_path):
    """A deployment that deliberately opts OUT must not advertise the
    tools at all, rather than registering ones that always fail.

    `default_optional_services=False` is that opt-out. It became explicit
    when build_mcp started defaulting backlog/events: previously "not
    configured" was the accident of server.py calling build_mcp() bare,
    which silently gave the stdio surface 19 fewer tools than HTTP."""
    config = make_config(tmp_path)
    server = build_mcp(TerminalService(config), default_optional_services=False)
    names = {t.name for t in await server.list_tools()}
    assert not any(n.startswith("terminal_backlog_") for n in names)
