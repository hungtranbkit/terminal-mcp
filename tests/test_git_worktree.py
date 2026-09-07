"""git_worktree.py -- real git worktree management (docs/REQUIREMENTS.md
§20.4). Every repo here is a disposable tmp_path fixture -- never a
real OfflinePOS checkout, never `window`/`window2`. Real `git worktree`
subprocess calls throughout, not mocks."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.git_worktree import (
    create_worktree, list_worktrees, remove_worktree, slugify, worktree_status,
)


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


def test_slugify_basic():
    assert slugify("Build WPF Settings Dialog!") == "build-wpf-settings-dialog"
    assert slugify("") == "task"
    assert slugify("___") == "task"


def test_create_worktree_real_git(tmp_path, repo):
    worktree_path = tmp_path / "worktrees" / "task-a"
    result = create_worktree(str(repo), "task/t1-build-thing", "main", str(worktree_path))
    assert "error" not in result
    assert result["branch"] == "task/t1-build-thing"
    assert worktree_path.is_dir()
    assert (worktree_path / "README.md").read_text() == "hello\n"
    # Real branch, checked out in the real worktree.
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path).stdout.strip()
    assert branch == "task/t1-build-thing"


def test_create_worktree_resolves_base_ref_to_a_real_commit(tmp_path, repo):
    expected_sha = _git(["rev-parse", "main"], repo).stdout.strip()
    worktree_path = tmp_path / "wt"
    result = create_worktree(str(repo), "task/t2", "main", str(worktree_path))
    assert result["base_sha"] == expected_sha


def test_create_worktree_refuses_unknown_base_ref(tmp_path, repo):
    result = create_worktree(str(repo), "task/t3", "no-such-ref", str(tmp_path / "wt"))
    assert result["error"] == "BASE_REF_NOT_FOUND"


def test_create_worktree_refuses_an_already_existing_branch(tmp_path, repo):
    create_worktree(str(repo), "task/dup", "main", str(tmp_path / "wt1"))
    result = create_worktree(str(repo), "task/dup", "main", str(tmp_path / "wt2"))
    assert result["error"] == "BRANCH_ALREADY_EXISTS"


def test_worktree_status_reports_real_state(tmp_path, repo):
    worktree_path = tmp_path / "wt"
    create_worktree(str(repo), "task/t4", "main", str(worktree_path))
    status = worktree_status(str(repo), str(worktree_path))
    assert status["exists"] is True
    assert status["branch"] == "task/t4"
    assert status["dirty"] is False

    (worktree_path / "new_file.txt").write_text("dirty\n")
    dirty_status = worktree_status(str(repo), str(worktree_path))
    assert dirty_status["dirty"] is True


def test_worktree_status_for_nonexistent_path(tmp_path, repo):
    status = worktree_status(str(repo), str(tmp_path / "never-created"))
    assert status["exists"] is False


def test_remove_worktree_real_cleanup(tmp_path, repo):
    worktree_path = tmp_path / "wt"
    create_worktree(str(repo), "task/t5", "main", str(worktree_path))
    result = remove_worktree(str(repo), str(worktree_path))
    assert result["removed"] is True
    assert not worktree_path.exists()


def test_remove_worktree_refuses_dirty_without_force(tmp_path, repo):
    worktree_path = tmp_path / "wt"
    create_worktree(str(repo), "task/t6", "main", str(worktree_path))
    (worktree_path / "untracked.txt").write_text("oops\n")
    result = remove_worktree(str(repo), str(worktree_path))
    assert result["error"] == "WORKTREE_DIRTY"
    assert worktree_path.exists()  # never removed


def test_remove_worktree_force_removes_dirty(tmp_path, repo):
    worktree_path = tmp_path / "wt"
    create_worktree(str(repo), "task/t7", "main", str(worktree_path))
    (worktree_path / "untracked.txt").write_text("oops\n")
    result = remove_worktree(str(repo), str(worktree_path), force=True)
    assert result["removed"] is True


def test_list_worktrees_includes_main_and_created_ones(tmp_path, repo):
    create_worktree(str(repo), "task/t8", "main", str(tmp_path / "wt8"))
    worktrees = list_worktrees(str(repo))
    branches = {w.get("branch") for w in worktrees}
    assert "task/t8" in branches
    assert len(worktrees) == 2  # main worktree + the new one
