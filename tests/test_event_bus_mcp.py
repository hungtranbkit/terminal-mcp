"""Event bus through the real MCP surface, plus the P0 no-autonomy guarantee."""
from __future__ import annotations

import json

import pytest

from terminal_mcp.core import TerminalService
from terminal_mcp.event_bus import EventBus
from terminal_mcp.mcp_app import build_mcp
from tests.test_backlog import make_config


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def rig(tmp_path):
    bus = EventBus(tmp_path / "events.db")
    server = build_mcp(TerminalService(make_config(tmp_path)), events=bus)
    return server, bus


async def _call(server, tool, **kwargs):
    result = await server.call_tool(tool, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_all_event_tools_registered(rig):
    server, _ = rig
    names = {t.name for t in await server.list_tools()}
    for tool in ("terminal_event_publish", "terminal_event_list", "terminal_event_claim",
                 "terminal_event_ack", "terminal_event_release", "terminal_event_retry",
                 "terminal_event_stats"):
        assert tool in names, tool


@pytest.mark.anyio
async def test_tools_absent_without_a_bus(tmp_path):
    server = build_mcp(TerminalService(make_config(tmp_path)))
    names = {t.name for t in await server.list_tools()}
    assert not any(n.startswith("terminal_event_") for n in names)


@pytest.mark.anyio
async def test_publish_claim_ack_round_trip(rig):
    server, _ = rig
    pub = await _call(server, "terminal_event_publish", type="TASK_CREATED",
                      project_id="git:acme/w", entity_type="task", entity_id="t1",
                      payload={"title": "x"})
    assert pub["status"] == "PENDING" and pub["duplicate"] is False

    claimed = await _call(server, "terminal_event_claim", consumer="w1", project_id="git:acme/w")
    assert claimed["id"] == pub["id"] and claimed["claim_token"]

    acked = await _call(server, "terminal_event_ack", event_id=claimed["id"],
                        claim_token=claimed["claim_token"])
    assert acked["acked"] is True
    assert (await _call(server, "terminal_event_stats"))["stats"] == {"ACKED": 1}


@pytest.mark.anyio
async def test_claim_returns_empty_when_nothing_matches(rig):
    server, _ = rig
    assert await _call(server, "terminal_event_claim", consumer="w", project_id="none") == {}


@pytest.mark.anyio
async def test_idempotent_publish_through_the_tool(rig):
    server, _ = rig
    a = await _call(server, "terminal_event_publish", type="TASK_CREATED",
                    project_id="p", idempotency_key="k")
    b = await _call(server, "terminal_event_publish", type="TASK_CREATED",
                    project_id="p", idempotency_key="k")
    assert b["id"] == a["id"] and b["duplicate"] is True


@pytest.mark.anyio
async def test_list_is_project_scoped_and_ordered(rig):
    server, _ = rig
    for n in range(3):
        await _call(server, "terminal_event_publish", type="TASK_CREATED",
                    project_id="p1", entity_id=f"a{n}")
        await _call(server, "terminal_event_publish", type="TASK_CREATED",
                    project_id="p2", entity_id=f"b{n}")
    out = await _call(server, "terminal_event_list", project_id="p1")
    assert [e["entity_id"] for e in out["events"]] == ["a0", "a1", "a2"]
    assert "TASK_CREATED" in out["known_types"]


@pytest.mark.anyio
async def test_known_event_vocabulary_is_advertised(rig):
    server, _ = rig
    out = await _call(server, "terminal_event_list")
    for t in ("TASK_CREATED", "TASK_READY", "WORKER_IDLE", "WORKER_DONE", "VERIFY_PENDING",
              "MERGE_CONFLICT", "TEST_FAILED", "PREVIEW_FAILED", "USER_FEEDBACK"):
        assert t in out["known_types"], t


@pytest.mark.anyio
async def test_P0_starts_no_autonomous_consumer(rig):
    """The whole point of this phase: the bus exists, but nothing consumes
    it on its own. Publishing must never cause a claim to happen."""
    server, bus = rig
    await _call(server, "terminal_event_publish", type="TASK_CREATED", project_id="p")
    assert bus.stats() == {"PENDING": 1}
    assert bus.list_events()[0]["claimed_by"] is None
