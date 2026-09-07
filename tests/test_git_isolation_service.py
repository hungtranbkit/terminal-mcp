"""GitIsolationService -- real worktree-backed task creation (docs/
REQUIREMENTS.md §20.4). Every repo here is a disposable tmp_path
fixture, real `git worktree` subprocess calls throughout -- never a
real OfflinePOS checkout, never `window`/`window2`."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.coordinator import CoordinatorGate, SessionSnapshot
from terminal_mcp.git_isolation_service import GitIsolationService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


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
def isolation(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    return GitIsolationService(queue)


def test_create_isolated_task_creates_a_real_worktree(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("Build thing", "implement the thing", repo_path=str(repo),
                                            session="lane-a")
    assert "error" not in result
    worktree_path = result["git_isolation"]["worktree_path"]
    assert (worktree_path).__class__ is str
    from pathlib import Path
    assert Path(worktree_path).is_dir()

    task = isolation.queue.task_status(result["task_id"])["task"]
    assert task["metadata"]["expected_cwd"] == worktree_path
    assert task["metadata"]["git_isolation"]["branch"].startswith("task/")


def test_create_isolated_task_unassigned_still_gets_a_real_worktree(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("Backlog isolated task", "p", repo_path=str(repo))
    assert "error" not in result
    assert result["assigned"] is False
    task = isolation.queue.task_status(result["task_id"])["task"]
    assert task["metadata"]["expected_cwd"]


def test_create_isolated_task_rolls_back_worktree_on_invalid_session(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("t", "p", repo_path=str(repo), session="../not valid")
    assert "error" in result
    # No leaked worktree directory for the failed task.
    worktrees = _git(["worktree", "list", "--porcelain"], repo).stdout
    assert worktrees.count("worktree ") == 1  # only the main one


def test_worktree_status_for_task(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("t", "p", repo_path=str(repo))
    status = isolation.worktree_status_for_task(result["task_id"])
    assert status["exists"] is True
    assert status["branch"] == result["git_isolation"]["branch"]


def test_worktree_status_for_non_isolated_task(tmp_path, isolation):
    created = isolation.queue.create_task("t", "p", session=None)
    result = isolation.worktree_status_for_task(created["task_id"])
    assert result["error"] == "TASK_NOT_ISOLATED"


def test_cleanup_worktree_for_task_real_removal(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("t", "p", repo_path=str(repo))
    from pathlib import Path
    worktree_path = result["git_isolation"]["worktree_path"]
    assert Path(worktree_path).is_dir()
    cleanup = isolation.cleanup_worktree_for_task(result["task_id"])
    assert cleanup["removed"] is True
    assert not Path(worktree_path).exists()


def test_two_isolated_tasks_get_two_independent_worktrees(tmp_path, repo, isolation):
    a = isolation.create_isolated_task("Task A", "p", repo_path=str(repo), session="lane-a")
    b = isolation.create_isolated_task("Task B", "p", repo_path=str(repo), session="lane-b")
    assert a["git_isolation"]["worktree_path"] != b["git_isolation"]["worktree_path"]
    assert a["git_isolation"]["branch"] != b["git_isolation"]["branch"]


# ---------------------------------------------------------------------------
# The REAL payoff: the Coordinator's EXISTING pre-dispatch gate (§8,
# zero new code) refuses a session whose live cwd doesn't match its
# task's own isolated worktree -- never "continue anyway" on shared main.
# ---------------------------------------------------------------------------

def test_coordinator_gate_refuses_a_session_not_on_its_own_worktree(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("Build the thing", "implement the whole feature end to end",
                                            repo_path=str(repo), session="lane-a")
    task = isolation.queue.store.get_task(result["task_id"])

    gate = CoordinatorGate()
    # Session is really still on the shared repo_path (main), NOT the
    # isolated worktree -- a real, live cwd mismatch.
    snapshot = SessionSnapshot(node_id="local", cwd=str(repo), current_command="bash", state="IDLE",
                               input_required=False, reader_alive=True)
    decision = gate.review(task, store=isolation.queue.store, session=snapshot)
    assert decision.status == "NEEDS_HUMAN"
    assert "worktree" in decision.reason


def test_coordinator_gate_accepts_a_session_correctly_on_its_own_worktree(tmp_path, repo, isolation):
    result = isolation.create_isolated_task("Build the thing", "implement the whole feature end to end",
                                            repo_path=str(repo), session="lane-a")
    task = isolation.queue.store.get_task(result["task_id"])
    worktree_path = task.metadata["expected_cwd"]

    gate = CoordinatorGate()
    snapshot = SessionSnapshot(node_id="local", cwd=worktree_path, current_command="bash", state="IDLE",
                               input_required=False, reader_alive=True)
    decision = gate.review(task, store=isolation.queue.store, session=snapshot)
    assert decision.status == "READY"
