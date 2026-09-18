"""Concurrent writers against the controller-side backlog.

The concurrency model CHANGED with the store: it used to be N processes
racing on a repo file guarded by fcntl.flock; it is now the controller as
single writer, so the real races are (a) its own concurrent threads and
(b) two AGENTS editing the same project, which `expected_revision`
guards. These tests cover both, rather than continuing to test a file
lock that no longer governs anything.
"""
from __future__ import annotations

import threading

import pytest

from terminal_mcp.backlog_db import BacklogDB
from terminal_mcp.backlog_service import BacklogService
from tests.test_backlog import make_config, make_repo


@pytest.fixture(autouse=True)
def _isolated_backlog_db(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BACKLOG_DB", str(tmp_path / "backlog.db"))


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widget")


@pytest.fixture
def svc(tmp_path):
    return BacklogService(make_config(tmp_path), db=BacklogDB(tmp_path / "backlog.db"))


def test_parallel_threads_never_lose_an_item(svc, repo):
    """Without the per-project lock this is a lost update: two threads
    read the same item list, both append, one append vanishes."""
    per_thread = 12
    errors: list[str] = []

    def writer(tag: str) -> None:
        for n in range(per_thread):
            out = svc.add(str(repo), tasks=[{"title": f"{tag}-{n}"}])
            if "error" in out:
                errors.append(out["error"])

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert errors == [], errors
    items = svc.get(str(repo))["items"]
    titles = sorted(i["title"] for i in items)
    assert len(titles) == per_thread * 2, f"lost updates: {len(titles)}"
    assert titles == sorted([f"alpha-{n}" for n in range(per_thread)]
                            + [f"beta-{n}" for n in range(per_thread)])
    assert len({i["id"] for i in items}) == per_thread * 2      # ids stayed unique


def test_revision_advances_once_per_write(svc, repo):
    svc.add(str(repo), tasks=[{"title": "a"}])
    svc.add(str(repo), tasks=[{"title": "b"}])
    assert svc.get(str(repo))["revision"] == 2


def test_two_agents_conflict_safely(svc, repo):
    """The cross-AGENT race the controller cannot serialize away: both
    read revision N, both try to write. The stale one is refused."""
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    rev = svc.get(str(repo))["revision"]
    assert "error" not in svc.update(str(repo), task_id=tid, patch={"title": "agent-A"},
                                     expected_revision=rev)
    out = svc.update(str(repo), task_id=tid, patch={"title": "agent-B"}, expected_revision=rev)
    assert out["error"] == "REVISION_CONFLICT"
    assert svc.get(str(repo))["items"][0]["title"] == "agent-A"   # A's write survived


def test_concurrent_writes_to_DIFFERENT_projects_do_not_block_each_other(tmp_path, svc):
    """The lock is per PROJECT, not global -- two projects must not
    serialize behind each other."""
    a = make_repo(tmp_path / "a", remote="https://github.com/acme/a.git")
    b = make_repo(tmp_path / "b", remote="https://github.com/acme/b.git")
    done: list[str] = []

    def writer(repo_path, tag):
        for n in range(8):
            svc.add(str(repo_path), tasks=[{"title": f"{tag}-{n}"}])
        done.append(tag)

    threads = [threading.Thread(target=writer, args=(a, "A")),
               threading.Thread(target=writer, args=(b, "B"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(done) == ["A", "B"]
    assert svc.get(str(a))["total"] == 8
    assert svc.get(str(b))["total"] == 8
