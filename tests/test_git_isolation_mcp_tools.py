"""Git isolation policy -- MCP tool surface (docs/REQUIREMENTS.md §20.4).
Exercises the real MCP call path, real git worktree subprocess calls --
never `window`/`window2`/`wtest`, never a real project checkout."""
from __future__ import annotations

import json
import subprocess

import pytest

from terminal_mcp.integration_service import IntegrationService
from terminal_mcp.integration_store import IntegrationStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "initial"], path)
    return path


@pytest.fixture
def server(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    # Explicit, isolated IntegrationStore -- build_mcp's own default
    # (no `integration=` passed) would otherwise share the ONE real,
    # session-wide default integration.db every other test file that
    # also omits `integration=` already reads/writes (conftest.py's
    # autouse XDG_STATE_HOME redirect is per SESSION, not per test) --
    # a real, project-name collision waiting to happen across files.
    integration = IntegrationService(IntegrationStore(tmp_path / "integration.db"))
    return build_mcp(queue=queue, integration=integration)


@pytest.mark.anyio
async def test_task_create_isolated_creates_real_worktree_through_mcp_path(server, repo):
    result = await _call(server, "terminal_task_create_isolated", title="Build thing",
                        prompt="implement the whole feature", repo_path=str(repo))
    assert "error" not in result
    from pathlib import Path
    assert Path(result["git_isolation"]["worktree_path"]).is_dir()

    status = await _call(server, "terminal_worktree_status", task_id=result["task_id"])
    assert status["exists"] is True
    assert status["branch"] == result["git_isolation"]["branch"]


@pytest.mark.anyio
async def test_worktree_status_refuses_non_isolated_task(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    result = await _call(server, "terminal_worktree_status", task_id=created["task_id"])
    assert result["error"] == "TASK_NOT_ISOLATED"


@pytest.mark.anyio
async def test_worktree_cleanup_through_mcp_path(server, repo):
    result = await _call(server, "terminal_task_create_isolated", title="Build thing",
                        prompt="implement the whole feature", repo_path=str(repo))
    from pathlib import Path
    worktree_path = result["git_isolation"]["worktree_path"]
    assert Path(worktree_path).is_dir()
    cleanup = await _call(server, "terminal_worktree_cleanup", task_id=result["task_id"])
    assert cleanup["removed"] is True
    assert not Path(worktree_path).exists()


@pytest.mark.anyio
async def test_integration_configure_accepts_mechanical_conflict_resolution_flag(server):
    result = await _call(server, "terminal_integration_configure", project="proj-x",
                        repo_path="/tmp/does-not-matter", allow_mechanical_conflict_resolution=True)
    assert result["allow_mechanical_conflict_resolution"] is True

    default_result = await _call(server, "terminal_integration_configure", project="proj-y",
                                repo_path="/tmp/does-not-matter")
    assert default_result["allow_mechanical_conflict_resolution"] is False
