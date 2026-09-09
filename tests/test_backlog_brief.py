"""Project Brief <- Project Backlog.

The brief already knows which REPO a session was working in, and the
backlog is keyed on exactly that repo -- so the brief shows what the
project still intends to do instead of keeping a second, drifting list.

The load-bearing property here is that this join is NEVER fatal: context
recovery must not start depending on a backlog file being present,
readable, or inside an allowed root.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp import backlog_store as store
from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.core import TerminalService
from tests.test_backlog import make_config, make_repo


@pytest.fixture(autouse=True)
def _isolated_stores(tmp_path, monkeypatch):
    """conftest redirects XDG_STATE_HOME once per test SESSION, so every
    test here would otherwise share ONE session_knowledge.db and one
    grants.db. These tests all drive the same session name ("brief-sess")
    against DIFFERENT repos, so meta from an earlier test leaks in and the
    brief resolves the wrong project -- a failure that only appears when
    the file runs as a whole. Give each test its own stores."""
    monkeypatch.setenv("TERMINAL_MCP_SESSION_KNOWLEDGE_DB", str(tmp_path / "knowledge.db"))
    monkeypatch.setenv("TERMINAL_MCP_GRANTS_DB", str(tmp_path / "grants.db"))


def _service(tmp_path, repo):
    """A TerminalService whose knowledge store already has meta for a
    session rooted in `repo` -- the real precondition for a brief."""
    config = make_config(tmp_path)
    svc = TerminalService(config)
    # ensure_meta probes the real git metadata from cwd itself, so the
    # brief's repo_root comes from the actual repo -- not a value the test
    # hand-feeds it.
    svc.session_knowledge.ensure_meta(
        svc.REGISTRY_LOCAL_NODE_ID, "brief-sess", "inst-1",
        cwd=str(repo), agent_type="claude", backend_type="tmux", lifecycle_state="ACTIVE")
    return svc


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widget")


def _brief(svc):
    svc.grants.set_read("brief-sess", True, granted_by="test")
    return svc.terminal_knowledge_recover("brief-sess")


def test_brief_carries_open_backlog_items(tmp_path, repo):
    BacklogService(make_config(tmp_path)).add(str(repo), tasks=[
        {"title": "Ship rate limiting", "priority": "P1"},
        {"title": "Fix flaky test", "priority": "P0"},
    ])
    out = _brief(_service(tmp_path, repo))
    pb = out["project_backlog"]
    assert pb["available"] is True
    assert pb["open_total"] == 2 and pb["unrun_total"] == 2
    titles = [i["title"] for i in pb["open_items"]]
    assert titles == ["Fix flaky test", "Ship rate limiting"]      # P0 first
    assert pb["project"]["project_id"] == "git:github.com/acme/widget"


def test_brief_text_mentions_the_open_items(tmp_path, repo):
    BacklogService(make_config(tmp_path)).add(str(repo), tasks=[{"title": "Ship rate limiting"}])
    out = _brief(_service(tmp_path, repo))
    text = out["recovery_brief_text"]
    assert "open project backlog" in text
    assert "Ship rate limiting" in text
    assert "[unrun]" in text                                        # never dispatched


def test_backlog_is_marked_untrusted(tmp_path, repo):
    """Titles are agent-written; a confused agent could park injection
    text in one and it would land in another agent's brief."""
    BacklogService(make_config(tmp_path)).add(str(repo), tasks=[{"title": "x"}])
    out = _brief(_service(tmp_path, repo))
    assert "project_backlog" in out["untrusted_fields"]
    assert out["untrusted_output"] is True


def test_dispatched_items_are_not_counted_as_unrun(tmp_path, repo):
    from terminal_mcp.queue_service import QueueService
    from terminal_mcp.queue_store import QueueStore
    backlog = BacklogService(make_config(tmp_path), queue=QueueService(QueueStore(tmp_path / "q.db")))
    ids = backlog.add(str(repo), tasks=[{"title": "a"}, {"title": "b"}])["created_ids"]
    backlog.dispatch(str(repo), task_id=ids[0])
    pb = _brief(_service(tmp_path, repo))["project_backlog"]
    assert pb["open_total"] == 2 and pb["unrun_total"] == 1
    assert [i["title"] for i in pb["unrun_items"]] == ["b"]


def test_done_items_are_excluded(tmp_path, repo):
    backlog = BacklogService(make_config(tmp_path))
    ids = backlog.add(str(repo), tasks=[{"title": "open one"}, {"title": "closed one"}])["created_ids"]
    backlog.complete(str(repo), task_id=ids[1], commit="abc123")
    pb = _brief(_service(tmp_path, repo))["project_backlog"]
    assert [i["title"] for i in pb["open_items"]] == ["open one"]


# ------------------------------------------------- never fatal
def test_project_with_no_backlog_file_still_briefs(tmp_path, repo):
    out = _brief(_service(tmp_path, repo))
    assert out["project_backlog"]["available"] is True              # resolvable, just empty
    assert out["project_backlog"]["open_total"] == 0
    assert out["recovery_brief_text"]                                # brief itself intact


def test_session_not_in_a_repo_still_briefs(tmp_path):
    plain = tmp_path / "plain"; plain.mkdir()
    config = make_config(tmp_path)
    svc = TerminalService(config)
    svc.session_knowledge.ensure_meta(
        svc.REGISTRY_LOCAL_NODE_ID, "brief-sess", "i",
        cwd=str(plain), agent_type="shell", backend_type="tmux", lifecycle_state="ACTIVE")
    out = _brief(svc)
    assert out["project_backlog"] == {"available": False, "reason": "SESSION_NOT_IN_A_REPO"}
    assert "recovery_brief_text" in out


def test_repo_outside_allowed_roots_still_briefs(tmp_path):
    """A session whose repo sits outside allowed_cwd_roots must degrade,
    not explode -- the path gate is shared with session creation."""
    outside = make_repo(tmp_path.parent / f"outside-brief-{tmp_path.name}")
    allowed = tmp_path / "allowed"; allowed.mkdir()
    config = make_config(allowed)
    svc = TerminalService(config)
    svc.session_knowledge.ensure_meta(
        svc.REGISTRY_LOCAL_NODE_ID, "brief-sess", "i",
        cwd=str(outside), agent_type="shell", backend_type="tmux", lifecycle_state="ACTIVE")
    out = _brief(svc)
    assert out["project_backlog"]["available"] is False
    assert out["project_backlog"]["reason"] == "PATH_NOT_ALLOWED"
    assert "recovery_brief_text" in out


def test_corrupt_backlog_file_still_briefs(tmp_path, repo):
    BacklogService(make_config(tmp_path)).add(str(repo), tasks=[{"title": "x"}])
    store.backlog_path(repo).write_text("{ not json")
    out = _brief(_service(tmp_path, repo))
    assert out["project_backlog"]["available"] is False
    assert out["project_backlog"]["reason"] == "BACKLOG_UNREADABLE"
    assert out["recovery_brief_text"]                                # brief still usable


def test_no_second_source_of_truth(tmp_path, repo):
    """The brief must READ the backlog file, never cache its own copy --
    an edit made after the brief was first taken must show up."""
    backlog = BacklogService(make_config(tmp_path))
    backlog.add(str(repo), tasks=[{"title": "first"}])
    svc = _service(tmp_path, repo)
    assert _brief(svc)["project_backlog"]["open_total"] == 1
    backlog.add(str(repo), tasks=[{"title": "second"}])
    assert _brief(svc)["project_backlog"]["open_total"] == 2
