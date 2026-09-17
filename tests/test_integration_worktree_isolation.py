"""The integrator must never touch the shared working tree.

The defect: `IntegrationEngine._merge` ran `git checkout <integration_branch>`
and `git merge` with `cwd=pipeline["repo_path"]` -- the tree a human or a
coding session is using right now. It switched their branch mid-edit, left
conflicted files in their tree on a failed merge, and `_promote` then moved
their HEAD again onto main.

Two invariants are proved here, and they are the ones that matter:

  1. after a merge -- clean OR conflicted -- the shared tree's branch, HEAD
     and porcelain status are byte-identical to what they were before;
  2. a conflict leaves its evidence recoverable rather than cleaned away.

Everything else in this file exists because a real integrator meets it: a
second integrator racing the first, a worktree left behind by a crash, stale
registrations, a missing branch, a dirty tree.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from terminal_mcp import integration_worktree as iw
from terminal_mcp.lease import ResourceLockStore


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


@pytest.fixture
def repo(tmp_path):
    """A real repo with main + integration, shared tree parked on main --
    the ordinary arrangement the integrator has to respect."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    (path / "base.txt").write_text("base\n")
    _git(["add", "."], path)
    _git(["commit", "-qm", "base"], path)
    _git(["branch", "integration"], path)
    return path


@pytest.fixture
def locks(tmp_path):
    return ResourceLockStore(tmp_path / "locks.db")


def _feature_commit(repo, branch, filename, content):
    _git(["checkout", "-q", "-b", branch], repo)
    (repo / filename).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-qm", f"add {filename}"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    _git(["checkout", "-q", "main"], repo)
    return sha


# -- invariant 1: the shared tree is never touched ----------------------------------

def test_acquiring_a_worktree_leaves_the_shared_tree_byte_identical(repo, locks):
    before = iw.shared_tree_state(str(repo))

    result = iw.acquire(str(repo), "proj", integration_branch="integration",
                        owner_id="integrator-1", locks=locks)

    assert "error" not in result, result
    assert iw.shared_tree_state(str(repo)) == before


def test_a_real_merge_in_the_worktree_leaves_the_shared_tree_byte_identical(repo, locks):
    """The headline invariant."""
    sha = _feature_commit(repo, "feature/a", "a.txt", "from a\n")
    before = iw.shared_tree_state(str(repo))

    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    _git(["merge", "--no-ff", "-m", "integrate a", sha], tree["worktree_path"])

    assert iw.shared_tree_state(str(repo)) == before
    # And the merge really happened, on the real integration branch.
    assert "a.txt" in _git(["show", "--stat", "integration"], repo).stdout


def test_a_conflicted_merge_leaves_the_shared_tree_byte_identical(repo, locks):
    """The case that used to leave conflict markers in somebody else's tree."""
    _git(["checkout", "-q", "integration"], repo)
    (repo / "shared.txt").write_text("integration side\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "integration edits shared"], repo)
    _git(["checkout", "-q", "main"], repo)

    _git(["checkout", "-q", "-b", "feature/b"], repo)
    (repo / "shared.txt").write_text("feature side\n")
    _git(["add", "."], repo)
    _git(["commit", "-qm", "feature edits shared"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    _git(["checkout", "-q", "main"], repo)

    before = iw.shared_tree_state(str(repo))
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    merge = _git(["merge", "--no-ff", "-m", "integrate b", sha],
                 tree["worktree_path"], check=False)

    assert merge.returncode != 0, "this fixture is meant to conflict"
    assert iw.shared_tree_state(str(repo)) == before, \
        "a conflicted merge must not reach the shared tree"


def test_the_worktree_lives_outside_the_shared_tree(repo, locks):
    """A worktree nested inside the repo shows up in its `git status` as
    untracked noise, and a careless `git clean` there would delete it."""
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    target = Path(tree["worktree_path"]).resolve()

    assert repo.resolve() not in target.parents
    assert iw.shared_tree_state(str(repo))["status"] == "", \
        "the shared tree must not even see it as untracked"


# -- invariant 2: a failure keeps its evidence ---------------------------------------

def test_a_worktree_left_mid_merge_is_adopted_as_evidence_not_wiped(repo, locks):
    """A crash mid-merge is exactly when the state is most worth keeping."""
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    path = tree["worktree_path"]
    # Simulate a crash: leave MERGE_HEAD behind.
    git_dir = _git(["rev-parse", "--absolute-git-dir"], path).stdout.strip()
    Path(git_dir, "MERGE_HEAD").write_text("deadbeef\n")

    second = iw.acquire(str(repo), "proj", integration_branch="integration",
                        owner_id="integrator-1", locks=locks)

    assert second["error"] == iw.WORKTREE_MID_MERGE
    assert Path(path).is_dir(), "the evidence must still be on disk"
    assert Path(git_dir, "MERGE_HEAD").exists()


def test_release_refuses_to_remove_a_worktree_mid_merge(repo, locks):
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    git_dir = _git(["rev-parse", "--absolute-git-dir"], tree["worktree_path"]).stdout.strip()
    Path(git_dir, "MERGE_HEAD").write_text("deadbeef\n")

    result = iw.release(str(repo), "proj", owner_id="integrator-1",
                        locks=locks, remove=True)

    assert result["removal"]["removed"] is False
    assert result["removal"]["reason"] == iw.WORKTREE_MID_MERGE
    assert Path(tree["worktree_path"]).is_dir()


def test_release_refuses_to_remove_a_dirty_worktree(repo, locks):
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    (Path(tree["worktree_path"]) / "scratch.txt").write_text("unsaved\n")

    result = iw.release(str(repo), "proj", owner_id="integrator-1",
                        locks=locks, remove=True)

    assert result["removal"]["removed"] is False
    assert result["removal"]["reason"] == iw.WORKTREE_DIRTY
    assert (Path(tree["worktree_path"]) / "scratch.txt").exists()


def test_a_dirty_worktree_is_refused_rather_than_merged_over(repo, locks):
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    (Path(tree["worktree_path"]) / "leftover.txt").write_text("half-done\n")
    _git(["add", "."], tree["worktree_path"])

    again = iw.acquire(str(repo), "proj", integration_branch="integration",
                       owner_id="integrator-1", locks=locks)

    assert again["error"] == iw.WORKTREE_DIRTY


def test_a_clean_worktree_is_removed_only_when_asked(repo, locks):
    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)

    kept = iw.release(str(repo), "proj", owner_id="integrator-1", locks=locks)
    assert "removal" not in kept
    assert Path(tree["worktree_path"]).is_dir(), "keeping it makes the next tick a resume"

    iw.acquire(str(repo), "proj", integration_branch="integration",
               owner_id="integrator-1", locks=locks)
    removed = iw.release(str(repo), "proj", owner_id="integrator-1",
                         locks=locks, remove=True)
    assert removed["removal"]["removed"] is True
    assert not Path(tree["worktree_path"]).exists()


# -- restart, idempotency, concurrency ------------------------------------------------

def test_acquiring_twice_resumes_the_same_worktree(repo, locks):
    """Deterministic path: a restarted integrator finds the SAME tree instead
    of leaking a second one."""
    first = iw.acquire(str(repo), "proj", integration_branch="integration",
                       owner_id="integrator-1", locks=locks)
    second = iw.acquire(str(repo), "proj", integration_branch="integration",
                        owner_id="integrator-1", locks=locks)

    assert second["worktree_path"] == first["worktree_path"]
    assert first.get("created") is True
    assert second.get("adopted") is True
    assert len(iw.integration_worktrees(str(repo))) == 1


def test_a_second_integrator_is_refused_with_the_holder_named(repo, locks):
    iw.acquire(str(repo), "proj", integration_branch="integration",
               owner_id="integrator-1", locks=locks)

    other = iw.acquire(str(repo), "proj", integration_branch="integration",
                       owner_id="integrator-2", locks=locks)

    assert other["error"] == iw.LOCK_HELD
    assert other["holder"], "a blocked integrator must be told who holds it"


def test_the_lock_is_released_so_the_next_integrator_can_take_it(repo, locks):
    iw.acquire(str(repo), "proj", integration_branch="integration",
               owner_id="integrator-1", locks=locks)
    iw.release(str(repo), "proj", owner_id="integrator-1", locks=locks)

    second = iw.acquire(str(repo), "proj", integration_branch="integration",
                        owner_id="integrator-2", locks=locks)
    assert "error" not in second, second


def test_two_projects_in_one_repo_get_separate_worktrees(repo, locks):
    """Distinct trees, so one project's merge cannot disturb another's."""
    a = iw.acquire(str(repo), "proj-a", integration_branch="integration",
                   owner_id="integrator-1", locks=locks)
    # proj-b wants the same branch, which git will not check out twice.
    b = iw.acquire(str(repo), "proj-b", integration_branch="integration",
                   owner_id="integrator-2", locks=locks)

    assert a["worktree_path"] != iw.worktree_path_for(str(repo), "proj-b")
    assert "error" in b and b["error"] == iw.CREATE_FAILED, \
        "git refuses one branch in two worktrees, and that is reported, not forced"


def test_stale_registration_for_a_deleted_directory_is_pruned(repo, locks):
    """A wiped scratch disk leaves git still believing the worktree exists,
    which would make `worktree add` refuse the path forever."""
    import shutil

    tree = iw.acquire(str(repo), "proj", integration_branch="integration",
                      owner_id="integrator-1", locks=locks)
    shutil.rmtree(tree["worktree_path"])
    iw.release(str(repo), "proj", owner_id="integrator-1", locks=locks)

    again = iw.acquire(str(repo), "proj", integration_branch="integration",
                       owner_id="integrator-1", locks=locks)

    assert "error" not in again, again
    assert Path(again["worktree_path"]).is_dir()


# -- evidence is validated before anything is created ---------------------------------

def test_a_missing_integration_branch_is_refused_before_any_worktree_exists(repo, locks):
    result = iw.acquire(str(repo), "proj", integration_branch="nope",
                        owner_id="integrator-1", locks=locks)

    assert result["error"] == iw.BRANCH_NOT_FOUND
    assert not Path(iw.worktree_path_for(str(repo), "proj")).exists()


def test_a_path_that_is_not_a_repository_is_refused(tmp_path, locks):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    result = iw.acquire(str(plain), "proj", integration_branch="integration",
                        owner_id="integrator-1", locks=locks)
    assert result["error"] == iw.REPO_NOT_FOUND


def test_a_missing_path_is_refused(tmp_path, locks):
    result = iw.acquire(str(tmp_path / "gone"), "proj",
                        integration_branch="integration",
                        owner_id="integrator-1", locks=locks)
    assert result["error"] == iw.REPO_NOT_FOUND
