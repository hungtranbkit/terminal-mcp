"""Real concurrent writers -- separate OS PROCESSES, not threads, because
the guarantee under test is an `fcntl.flock` advisory lock, which is
per-process. Threads in one interpreter would not exercise it honestly.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from terminal_mcp import backlog_store as store
from terminal_mcp.backlog_service import BacklogService
from tests.test_backlog import make_config, make_repo

REPO_ROOT = Path(__file__).resolve().parents[1]

WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, {root!r})
    from terminal_mcp.backlog_service import BacklogService
    from tests.test_backlog import make_config
    svc = BacklogService(make_config({allowed!r}))
    for n in range({count}):
        svc.add({repo!r}, tasks=[{{"title": "{tag}-%d" % n}}])
""")


def _spawn(tmp_path: Path, repo: Path, tag: str, count: int) -> subprocess.Popen:
    code = WRITER.format(root=str(REPO_ROOT), allowed=str(tmp_path), repo=str(repo),
                         count=count, tag=tag)
    return subprocess.Popen([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widget")


def test_parallel_writers_never_lose_an_item_or_corrupt_the_file(tmp_path, repo):
    """Two processes appending simultaneously. Without the lock this is
    a classic lost-update: both read revision N, both write N+1, one
    item vanishes. With it, every item survives."""
    per_writer = 12
    procs = [_spawn(tmp_path, repo, tag, per_writer) for tag in ("alpha", "beta")]
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"writer failed: {err or out}"

    path = store.backlog_path(repo)
    document = json.loads(path.read_text())          # parses => never corrupted
    titles = [i["title"] for i in document["items"]]
    assert len(titles) == per_writer * 2, f"lost updates: {len(titles)} of {per_writer * 2}"
    assert len({t for t in titles}) == per_writer * 2
    assert sorted(titles) == sorted([f"alpha-{n}" for n in range(per_writer)]
                                    + [f"beta-{n}" for n in range(per_writer)])
    # revision advanced once per successful write
    assert document["revision"] == per_writer * 2
    # ids stayed unique across processes
    assert len({i["id"] for i in document["items"]}) == per_writer * 2


def test_no_temp_files_survive_concurrent_writes(tmp_path, repo):
    procs = [_spawn(tmp_path, repo, tag, 6) for tag in ("a", "b")]
    for proc in procs:
        proc.communicate(timeout=120)
    leftovers = list(store.backlog_path(repo).parent.glob(".backlog-*.tmp"))
    assert leftovers == [], f"atomic write left temp files behind: {leftovers}"


def test_lock_file_does_not_pollute_the_item_list(tmp_path, repo):
    svc = BacklogService(make_config(tmp_path))
    svc.add(str(repo), tasks=[{"title": "x"}])
    assert svc.get(str(repo))["total"] == 1
    # the .lock file lives beside the backlog and is never parsed as data
    assert store.backlog_path(repo).with_name("backlog.json.lock").exists()
