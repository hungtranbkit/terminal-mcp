"""Lifecycle Close-Loop V1 -- worktree base safety.

The hazard these tests pin is a real one found on the hp-linux host: the
worktree-hosting clone's `refs/remotes/origin/HEAD` pointed at
`refs/remotes/origin/fix/p0-claude-submit-ghost-composer` -- a feature
branch, and one that had since been deleted from the remote. Anything
resolving the default branch through `origin/HEAD` (or through a bare
`HEAD`, which on a worktree host is whatever lane that clone is sitting
on) silently based new work on the wrong commit.

Every repo here is a disposable tmp_path fixture with real `git`
subprocess calls, same posture as tests/test_git_worktree.py.
"""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp import git_worktree
from terminal_mcp.git_isolation_service import GitIsolationService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


@pytest.fixture
def origin_repo(tmp_path):
    """A real bare 'remote' plus a clone, so refs/remotes/origin/* genuinely
    exist -- resolve_base_ref's whole job is choosing between real remote
    and local refs, which a single standalone repo cannot exercise."""
    bare = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", "-b", "main", str(bare)], tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-q", "-b", "main"], seed)
    _git(["config", "user.email", "test@example.com"], seed)
    _git(["config", "user.name", "Test"], seed)
    (seed / "README.md").write_text("trunk\n")
    _git(["add", "."], seed)
    _git(["commit", "-q", "-m", "trunk commit"], seed)
    # A second branch that is NOT main, to point origin/HEAD at.
    _git(["checkout", "-q", "-b", "fix/decoy"], seed)
    (seed / "decoy.txt").write_text("decoy\n")
    _git(["add", "."], seed)
    _git(["commit", "-q", "-m", "decoy commit"], seed)
    _git(["remote", "add", "origin", str(bare)], seed)
    _git(["push", "-q", "origin", "main", "fix/decoy"], seed)

    clone = tmp_path / "clone"
    _git(["clone", "-q", str(bare), str(clone)], tmp_path)
    _git(["config", "user.email", "test@example.com"], clone)
    _git(["config", "user.name", "Test"], clone)
    return clone


def _sha(repo, ref):
    return _git(["rev-parse", ref], repo).stdout.strip()


def test_isolated_task_uses_configured_main_not_origin_head(tmp_path, origin_repo):
    """The headline regression. origin/HEAD is deliberately pointed at a
    feature branch, exactly as found in production; the new task must still
    start from origin/main."""
    _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/fix/decoy"], origin_repo)
    assert _sha(origin_repo, "origin/HEAD") == _sha(origin_repo, "origin/fix/decoy")
    assert _sha(origin_repo, "origin/HEAD") != _sha(origin_repo, "origin/main")

    # Also check out the decoy locally, so a bare `HEAD` default would be
    # wrong too -- both old hazards are live at once here.
    _git(["checkout", "-q", "-B", "fix/decoy", "origin/fix/decoy"], origin_repo)

    queue = QueueService(store=QueueStore(tmp_path / "queue.db"))
    service = GitIsolationService(queue)
    created = service.create_isolated_task(
        "build the thing", "do it", repo_path=str(origin_repo),
        worktree_root=str(tmp_path / "trees"))

    assert "error" not in created, created
    isolation = created["git_isolation"]
    assert isolation["base_sha"] == _sha(origin_repo, "origin/main")
    assert isolation["base_ref"] == "refs/remotes/origin/main"
    assert isolation["base_ref_source"] == "origin"
    # And the worktree really is on that commit, not just the metadata.
    assert _sha(isolation["worktree_path"], "HEAD") == _sha(origin_repo, "origin/main")


def test_resolve_base_ref_never_consults_origin_head(origin_repo):
    _git(["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/fix/decoy"], origin_repo)
    resolved = git_worktree.resolve_base_ref(str(origin_repo))
    assert resolved["base_sha"] == _sha(origin_repo, "origin/main")


def test_resolve_base_ref_reports_ambiguity_instead_of_guessing(origin_repo):
    """A local `main` that has drifted from `origin/main` is exactly the
    state that produces a stale base. Refuse rather than pick."""
    _git(["checkout", "-q", "main"], origin_repo)
    (origin_repo / "local-only.txt").write_text("drift\n")
    _git(["add", "."], origin_repo)
    _git(["commit", "-q", "-m", "local drift"], origin_repo)

    resolved = git_worktree.resolve_base_ref(str(origin_repo))
    assert resolved["error"] == "BASE_REF_AMBIGUOUS"
    assert resolved["remote_sha"] != resolved["local_sha"]
    # An explicit base_ref is always honoured, ambiguity or not.
    explicit = git_worktree.resolve_base_ref(str(origin_repo), base_ref="refs/remotes/origin/main")
    assert explicit["base_sha"] == _sha(origin_repo, "origin/main")


def test_resolve_base_ref_falls_back_to_local_trunk_without_a_remote(tmp_path):
    repo = tmp_path / "solo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@e.com"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "a.txt").write_text("a\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)

    resolved = git_worktree.resolve_base_ref(str(repo))
    assert resolved["source"] == "local"
    assert resolved["base_sha"] == _sha(repo, "main")


def test_missing_base_ref_leaves_no_partial_worktree(tmp_path, origin_repo):
    """A failed creation must leave neither a branch nor a directory
    behind, so the same branch name is retryable."""
    result = git_worktree.create_worktree(
        str(origin_repo), "task/doomed", "refs/heads/nope", str(tmp_path / "trees" / "doomed"))
    assert result["error"] == "BASE_REF_NOT_FOUND"
    assert not (tmp_path / "trees" / "doomed").exists()
    assert _git(["rev-parse", "--verify", "refs/heads/task/doomed"], origin_repo,
                check=False).returncode != 0


def test_missing_trunk_leaves_no_partial_worktree(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(["init", "-q", "-b", "trunkless"], repo)
    result = git_worktree.create_worktree(str(repo), "task/x", None, str(tmp_path / "wt"))
    assert result["error"] == "BASE_REF_NOT_FOUND"
    assert not (tmp_path / "wt").exists()


def test_failed_worktree_add_cleans_up_the_branch_it_created(tmp_path, origin_repo):
    """`worktree add` fails when its target path is already a non-empty
    directory. The branch git created on the way must not survive."""
    blocked = tmp_path / "trees" / "blocked"
    blocked.mkdir(parents=True)
    (blocked / "in-the-way.txt").write_text("occupied\n")

    result = git_worktree.create_worktree(str(origin_repo), "task/blocked", None, str(blocked))
    assert result["error"] == "WORKTREE_ADD_FAILED"
    assert _git(["rev-parse", "--verify", "refs/heads/task/blocked"], origin_repo,
                check=False).returncode != 0, "a branch leaked from a failed worktree add"


def test_create_worktree_still_accepts_an_explicit_base_ref(tmp_path, origin_repo):
    """Backward compatibility: the old positional call shape keeps working."""
    result = git_worktree.create_worktree(
        str(origin_repo), "task/explicit", "origin/fix/decoy", str(tmp_path / "wt"))
    assert "error" not in result
    assert result["base_sha"] == _sha(origin_repo, "origin/fix/decoy")
    assert result["base_ref_source"] == "explicit"


def test_delete_branch_refuses_unmerged_work(tmp_path, origin_repo):
    """The reaper's branch delete must never be able to lose commits."""
    created = git_worktree.create_worktree(
        str(origin_repo), "task/unmerged", None, str(tmp_path / "wt"))
    assert "error" not in created
    wt = created["worktree_path"]
    (tmp_path / "wt" / "new.txt").write_text("unmerged work\n")
    _git(["add", "."], wt)
    _git(["commit", "-q", "-m", "work nobody merged"], wt)

    assert "error" not in git_worktree.remove_worktree(str(origin_repo), wt)
    result = git_worktree.delete_branch(str(origin_repo), "task/unmerged")
    assert result["error"] == "BRANCH_NOT_MERGED"
    assert _git(["rev-parse", "--verify", "refs/heads/task/unmerged"], origin_repo,
                check=False).returncode == 0, "the unmerged branch must still exist"


def test_is_ancestor_reports_unknown_as_none(origin_repo):
    assert git_worktree.is_ancestor(str(origin_repo), "origin/main", "refs/remotes/origin/main") is True
    assert git_worktree.is_ancestor(str(origin_repo), "origin/fix/decoy",
                                    "refs/remotes/origin/main") is False
    assert git_worktree.is_ancestor(str(origin_repo), "0" * 40, "refs/remotes/origin/main") is None
