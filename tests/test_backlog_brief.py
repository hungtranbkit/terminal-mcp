"""Project Brief <- Project Backlog, through the layer that actually owns
the join.

The attach lives in mcp_app (the composition layer), NOT in the node's
own TerminalService: only the controller holds the canonical
project-keyed backlog, so attaching it node-side would have silently
produced an empty list for every remote session.

The load-bearing property is that the join is NEVER fatal.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.backlog_db import BacklogDB
from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from tests.test_backlog import make_config, make_repo


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BACKLOG_DB", str(tmp_path / "backlog.db"))
    monkeypatch.setenv("TERMINAL_MCP_SESSION_KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    monkeypatch.setenv("TERMINAL_MCP_GRANTS_DB", str(tmp_path / "grants.db"))


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widget")


@pytest.fixture
def rig(tmp_path, repo):
    config = make_config(tmp_path)
    terminal = TerminalService(config)
    backlog = BacklogService(config, db=BacklogDB(tmp_path / "backlog.db"))
    terminal.session_knowledge.ensure_meta(
        terminal.REGISTRY_LOCAL_NODE_ID, "brief-sess", "inst-1",
        cwd=str(repo), agent_type="claude", backend_type="tmux", lifecycle_state="ACTIVE")
    terminal.grants.set_read("brief-sess", True, granted_by="test")
    server = build_mcp(terminal, backlog=backlog)
    return server, backlog, repo


async def _brief(server):
    result = await server.call_tool("terminal_knowledge_recover", {"session_name": "brief-sess"})
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.anyio
async def test_brief_carries_open_backlog_items(rig):
    server, backlog, repo = rig
    backlog.add(str(repo), tasks=[{"title": "Ship rate limiting", "priority": "P1"},
                                  {"title": "Fix flaky test", "priority": "P0"}])
    pb = (await _brief(server))["project_backlog"]
    assert pb["available"] is True
    assert pb["open_total"] == 2 and pb["unrun_total"] == 2
    assert [i["title"] for i in pb["open_items"]] == ["Fix flaky test", "Ship rate limiting"]
    assert pb["project"]["project_id"] == "git:github.com/acme/widget"


@pytest.mark.anyio
async def test_brief_text_mentions_the_open_items(rig):
    server, backlog, repo = rig
    backlog.add(str(repo), tasks=[{"title": "Ship rate limiting"}])
    text = (await _brief(server))["recovery_brief_text"]
    assert "open project backlog" in text
    assert "Ship rate limiting" in text and "[unrun]" in text


@pytest.mark.anyio
async def test_backlog_is_marked_untrusted(rig):
    server, backlog, repo = rig
    backlog.add(str(repo), tasks=[{"title": "x"}])
    out = await _brief(server)
    assert "project_backlog" in out["untrusted_fields"]
    assert out["untrusted_output"] is True


@pytest.mark.anyio
async def test_dispatched_items_are_not_counted_as_unrun(tmp_path, rig):
    from terminal_mcp.queue_service import QueueService
    from terminal_mcp.queue_store import QueueStore
    server, backlog, repo = rig
    backlog.queue = QueueService(QueueStore(tmp_path / "q.db"))
    ids = backlog.add(str(repo), tasks=[{"title": "a"}, {"title": "b"}])["created_ids"]
    backlog.dispatch(str(repo), task_id=ids[0])
    pb = (await _brief(server))["project_backlog"]
    assert pb["open_total"] == 2 and pb["unrun_total"] == 1
    assert [i["title"] for i in pb["unrun_items"]] == ["b"]


@pytest.mark.anyio
async def test_done_items_are_excluded(rig):
    server, backlog, repo = rig
    ids = backlog.add(str(repo), tasks=[{"title": "open one"}, {"title": "closed one"}])["created_ids"]
    backlog.complete(str(repo), task_id=ids[1], commit="abc123")
    pb = (await _brief(server))["project_backlog"]
    assert [i["title"] for i in pb["open_items"]] == ["open one"]


@pytest.mark.anyio
async def test_reads_the_store_each_time_no_cached_copy(rig):
    server, backlog, repo = rig
    backlog.add(str(repo), tasks=[{"title": "first"}])
    assert (await _brief(server))["project_backlog"]["open_total"] == 1
    backlog.add(str(repo), tasks=[{"title": "second"}])
    assert (await _brief(server))["project_backlog"]["open_total"] == 2


# ------------------------------------------------------------ never fatal
@pytest.mark.anyio
async def test_project_with_no_backlog_still_briefs(rig):
    server, _, _ = rig
    out = await _brief(server)
    assert out["project_backlog"]["available"] is True
    assert out["project_backlog"]["open_total"] == 0
    assert out["recovery_brief_text"]


@pytest.mark.anyio
async def test_session_not_in_a_repo_still_briefs(tmp_path):
    plain = tmp_path / "plain"; plain.mkdir()
    config = make_config(tmp_path)
    terminal = TerminalService(config)
    terminal.session_knowledge.ensure_meta(
        terminal.REGISTRY_LOCAL_NODE_ID, "brief-sess", "i",
        cwd=str(plain), agent_type="shell", backend_type="tmux", lifecycle_state="ACTIVE")
    terminal.grants.set_read("brief-sess", True, granted_by="test")
    server = build_mcp(terminal, backlog=BacklogService(config, db=BacklogDB(tmp_path / "b.db")))
    out = await _brief(server)
    assert out["project_backlog"]["available"] is False
    assert out["project_backlog"]["reason"] in ("SESSION_NOT_IN_A_REPO", "NOT_A_PROJECT")
    assert out["recovery_brief_text"]


@pytest.mark.anyio
async def test_repo_outside_allowed_roots_still_briefs(tmp_path):
    outside = make_repo(tmp_path.parent / f"outside-brief-{tmp_path.name}")
    allowed = tmp_path / "allowed"; allowed.mkdir()
    config = make_config(allowed)
    terminal = TerminalService(config)
    terminal.session_knowledge.ensure_meta(
        terminal.REGISTRY_LOCAL_NODE_ID, "brief-sess", "i",
        cwd=str(outside), agent_type="shell", backend_type="tmux", lifecycle_state="ACTIVE")
    terminal.grants.set_read("brief-sess", True, granted_by="test")
    server = build_mcp(terminal, backlog=BacklogService(config, db=BacklogDB(tmp_path / "b.db")))
    out = await _brief(server)
    assert out["project_backlog"]["available"] is False
    assert out["project_backlog"]["reason"] == "PATH_NOT_ALLOWED"
    assert out["recovery_brief_text"]


@pytest.mark.anyio
async def test_brief_works_with_no_backlog_service_at_all(tmp_path, repo):
    """A deployment without a backlog must brief exactly as before."""
    config = make_config(tmp_path)
    terminal = TerminalService(config)
    terminal.session_knowledge.ensure_meta(
        terminal.REGISTRY_LOCAL_NODE_ID, "brief-sess", "i",
        cwd=str(repo), agent_type="shell", backend_type="tmux", lifecycle_state="ACTIVE")
    terminal.grants.set_read("brief-sess", True, granted_by="test")
    server = build_mcp(terminal)
    out = await _brief(server)
    assert "project_backlog" not in out
    assert out["recovery_brief_text"]
