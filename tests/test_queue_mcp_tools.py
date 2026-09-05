"""Supervisor Queue v2 -- MCP tool surface (task: "Supervisor Queue v2
cho Terminal MCP"). Exercises the exact same MCP call path a real
ChatGPT/Claude Code client uses (server.call_tool), same pattern as
test_p0_hardening.py's own idempotency-key MCP-layer regression tests
-- calling QueueService directly in Python is not enough to catch a
future "the tool wrapper forgot to expose a parameter" gap.

SAFETY: `session` below is always a disposable string like "lane-a" --
never `window`/`window2`. This phase's tools are pure CRUD over
queue_store.py; nothing here sends anything to a real session."""
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
async def test_queue_set_then_status_round_trips_through_the_real_mcp_tool_surface(server):
    result = await _call(server, "terminal_queue_set", session="lane-a",
                         tasks=[{"prompt": "task one"}, {"prompt": "task two"}])
    assert len(result["task_ids"]) == 2

    status = await _call(server, "terminal_queue_status", session="lane-a")
    assert status["total_count"] == 2
    assert status["queued_count"] == 2
    assert [t["prompt"] for t in status["tasks"]] == ["task one", "task two"]


@pytest.mark.anyio
async def test_queue_append_does_not_disturb_existing_pending_tasks(server):
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "first"}])
    await _call(server, "terminal_queue_append", session="lane-a", tasks=[{"prompt": "second"}])
    status = await _call(server, "terminal_queue_status", session="lane-a")
    assert [t["prompt"] for t in status["tasks"]] == ["first", "second"]


@pytest.mark.anyio
async def test_queue_set_replace_pending_cancels_old_queued_tasks(server):
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "stale"}])
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "fresh"}])
    status = await _call(server, "terminal_queue_status", session="lane-a")
    by_prompt = {t["prompt"]: t["status"] for t in status["tasks"]}
    assert by_prompt["stale"] == "CANCELLED"
    assert by_prompt["fresh"] == "QUEUED"


@pytest.mark.anyio
async def test_queue_pause_and_resume_through_mcp(server):
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "a"}])
    paused = await _call(server, "terminal_queue_pause", session="lane-a", reason="testing")
    assert paused["paused"] is True
    assert paused["paused_reason"] == "testing"
    resumed = await _call(server, "terminal_queue_resume", session="lane-a")
    assert resumed["paused"] is False


@pytest.mark.anyio
async def test_queue_skip_and_cancel_through_mcp(server):
    result = await _call(server, "terminal_queue_set", session="lane-a",
                         tasks=[{"prompt": "a"}, {"prompt": "b"}])
    task_a, task_b = result["task_ids"]
    skipped = await _call(server, "terminal_queue_skip", session="lane-a", task_id=task_a)
    assert skipped["task"]["status"] == "SKIPPED"
    cancelled = await _call(server, "terminal_queue_cancel", session="lane-a", task_id=task_b)
    assert cancelled["task"]["status"] == "CANCELLED"


@pytest.mark.anyio
async def test_queue_reorder_through_mcp(server):
    result = await _call(server, "terminal_queue_set", session="lane-a",
                         tasks=[{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}])
    ids = result["task_ids"]
    status = await _call(server, "terminal_queue_reorder", session="lane-a",
                         ordered_task_ids=[ids[2], ids[0], ids[1]])
    assert [t["prompt"] for t in status["tasks"]] == ["c", "a", "b"]


@pytest.mark.anyio
async def test_queue_clear_through_mcp(server):
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "a"}, {"prompt": "b"}])
    result = await _call(server, "terminal_queue_clear", session="lane-a")
    assert result["cleared"] == 2


@pytest.mark.anyio
async def test_queue_events_through_mcp(server):
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "a"}])
    events = await _call(server, "terminal_queue_events", session="lane-a")
    assert any(e["event_type"] == "ENQUEUED" for e in events["events"])


@pytest.mark.anyio
async def test_queue_list_all_through_mcp_shows_every_lane_independently(server):
    await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "a"}])
    await _call(server, "terminal_queue_set", session="lane-b", tasks=[{"prompt": "b1"}, {"prompt": "b2"}])
    result = await _call(server, "terminal_queue_list_all")
    by_session = {lane["session"]: lane for lane in result["lanes"]}
    assert by_session["lane-a"]["total_count"] == 1
    assert by_session["lane-b"]["total_count"] == 2


@pytest.mark.anyio
async def test_queue_set_rejects_a_task_with_no_prompt(server):
    result = await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"title": "no prompt here"}])
    assert result["error"] == "TASK_PROMPT_REQUIRED"


@pytest.mark.anyio
async def test_queue_set_rejects_an_invalid_session_name(server):
    result = await _call(server, "terminal_queue_set", session="../etc/passwd", tasks=[{"prompt": "a"}])
    assert result["error"] == "INVALID_SESSION_NAME"


@pytest.mark.anyio
async def test_queue_retry_on_a_non_blocked_task_is_refused_not_silently_applied(server):
    result = await _call(server, "terminal_queue_set", session="lane-a", tasks=[{"prompt": "a"}])
    task_id = result["task_ids"][0]
    retried = await _call(server, "terminal_queue_retry", session="lane-a", task_id=task_id)
    assert retried["error"] == "INVALID_TRANSITION"  # still QUEUED, retry is only valid from BLOCKED
