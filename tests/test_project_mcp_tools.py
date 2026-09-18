"""P0.7 project APIs -- MCP tool surface, through the real
server.call_tool path. Driving ProjectService directly cannot catch a
tool wrapper that dropped a parameter, and the pause/resume pair is
exactly where a dropped parameter would be dangerous."""
from __future__ import annotations

import json

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from tests.test_backlog import make_config

PROJECT = "git:github.com/acme/widget"


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def _isolated_backlog_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BACKLOG_DB", str(tmp_path / "backlog.db"))


@pytest.fixture
def rig(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    backlog = BacklogService(make_config(tmp_path), queue=queue)
    return build_mcp(queue=queue, backlog=backlog), queue, backlog


def scoped_task(queue, *, session, title="t", project_id=PROJECT):
    (task_id,) = queue.store.set_tasks(session, [{"title": title, "prompt": "do it"}],
                                       replace_pending=False)
    queue.store.set_task_project(task_id, project_id)
    return task_id


@pytest.mark.anyio
async def test_status_through_the_real_tool(rig):
    server, queue, _ = rig
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    queue.store.transition_task(task_id, qs.RUNNING, event_type="R")

    status = await _call(server, "terminal_project_status", project_id=PROJECT)
    assert status["project_id"] == PROJECT
    assert status["task_counts"]["RUNNING"] == 1
    assert [lane["session"] for lane in status["lanes"]] == ["lane-a"]
    # Wired subsystems report structures, not null, in a full build.
    assert status["verification"] is not None
    assert status["resource_locks"] is not None


@pytest.mark.anyio
async def test_submit_goal_creates_a_backlog_item_and_no_queue_task(rig):
    server, queue, backlog = rig
    result = await _call(server, "terminal_project_submit_goal", project_id=PROJECT,
                         goal="Ship offline sync", priority="P1",
                         acceptance_criteria=["works offline"])
    assert result["submitted"] is True
    assert result["item"]["title"] == "Ship offline sync"
    # The crucial negative: submitting a goal must never start work.
    assert queue.store.project_task_counts(PROJECT) == {}
    assert backlog.get(project_id=PROJECT)["total"] == 1


@pytest.mark.anyio
async def test_pause_and_resume_through_the_tools(rig):
    server, queue, _ = rig
    scoped_task(queue, session="lane-a")
    scoped_task(queue, session="lane-b")

    paused = await _call(server, "terminal_project_pause", project_id=PROJECT,
                         reason="release freeze")
    assert sorted(paused["paused"]) == ["lane-a", "lane-b"]
    assert queue.store.lane_status("lane-a")["paused"] is True

    resumed = await _call(server, "terminal_project_resume", project_id=PROJECT)
    assert sorted(resumed["resumed"]) == ["lane-a", "lane-b"]
    assert queue.store.lane_status("lane-a")["paused"] is False


@pytest.mark.anyio
async def test_resume_through_the_tool_skips_a_foreign_pause(rig):
    """The asymmetry must survive the tool boundary -- this is where a
    dropped parameter would silently undo an operator's pause."""
    server, queue, _ = rig
    scoped_task(queue, session="ours")
    scoped_task(queue, session="theirs")
    await _call(server, "terminal_project_pause", project_id=PROJECT, reason="freeze")
    queue.store.resume_lane("theirs")
    queue.store.pause_lane("theirs", reason="operator: disk full")

    result = await _call(server, "terminal_project_resume", project_id=PROJECT)
    assert result["resumed"] == ["ours"]
    assert queue.store.lane_status("theirs")["paused"] is True
    assert "disk full" in result["skipped"][0]["paused_reason"]

    forced = await _call(server, "terminal_project_resume", project_id=PROJECT, force=True)
    assert forced["forced"] is True and forced["resumed"] == ["theirs"]


@pytest.mark.anyio
async def test_events_and_report_through_the_tools(rig):
    server, queue, _ = rig
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHING")

    events = await _call(server, "terminal_project_events", project_id=PROJECT)
    assert any(e["event_type"] == "DISPATCHING" for e in events["queue"])

    report = await _call(server, "terminal_project_report", project_id=PROJECT, window_hours=1)
    assert report["throughput"]["dispatched"] == 1
    assert report["current"]["DISPATCHING"] == 1


@pytest.mark.anyio
async def test_assign_through_the_tool(rig):
    server, queue, _ = rig
    task_id = scoped_task(queue, session=qs.UNASSIGNED_LANE)
    result = await _call(server, "terminal_project_assign", project_id=PROJECT,
                         task_id=task_id, session="lane-target")
    assert "error" not in result.get("result", {}), result
    assert queue.store.get_task(task_id).session == "lane-target"


@pytest.mark.anyio
async def test_assign_refuses_another_projects_task_through_the_tool(rig):
    server, queue, _ = rig
    task_id = scoped_task(queue, session="lane-a", project_id="git:github.com/acme/other")
    result = await _call(server, "terminal_project_assign", project_id=PROJECT,
                         task_id=task_id, session="lane-b")
    assert result["error"] == "TASK_NOT_IN_PROJECT"
    assert queue.store.get_task(task_id).session == "lane-a"


@pytest.mark.anyio
async def test_project_tools_validate_their_input(rig):
    server, _, _ = rig
    for tool in ("terminal_project_status", "terminal_project_report",
                 "terminal_project_pause", "terminal_project_resume"):
        assert (await _call(server, tool, project_id=""))["error"] == "INVALID_REQUEST", tool


@pytest.fixture
def anyio_backend():
    return "asyncio"
