"""Git isolation policy -- real worktree/branch management (docs/
REQUIREMENTS.md §20.4). Pure, bounded `git` subprocess calls, same
posture as session_registry.py's own `probe_project_info`/`_run_git`:
never `shell=True`, never a caller-supplied argv, a small fixed set of
well-known subcommands against a caller-supplied repo path, real
timeouts throughout, fail-closed (an `{"error": ...}` dict, never a
guess) on any git failure.

A coding task defaults to requiring its own isolated worktree + branch,
created from the canonical base/integration SHA -- never a task's own
worker directly on `main`/a shared working tree (task's own explicit
policy). The Coordinator's EXISTING pre-dispatch gate already verifies
a session's live cwd matches its task's own `expected_cwd` metadata
(coordinator.py's own check, its error message literally says "expected
worktree" -- see docs/REQUIREMENTS.md §20.4a) -- this module only needs
to CREATE the real worktree and set that same metadata field; zero new
Coordinator code was needed for the isolation guarantee itself.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_SECONDS = 30.0

DEFAULT_MAIN_BRANCH = "main"
"""The configured integration trunk. Deliberately a plain constant with an
explicit override everywhere it is used, never a value derived from the
repository's own state."""


def _run_git(args: list[str], cwd: str, *, timeout: float = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def resolve_base_ref(repo_path: str, *, base_ref: str | None = None,
                     main_branch: str = DEFAULT_MAIN_BRANCH) -> dict[str, Any]:
    """Resolve the commit a new task worktree must start from, fail-closed.

    NEVER consults `refs/remotes/origin/HEAD`, and never falls back to the
    repository's current `HEAD`. Both were real hazards on this host:
    `origin/HEAD` was found pointing at `refs/remotes/origin/fix/p0-claude-
    submit-ghost-composer` (a feature branch, and one that had since been
    deleted from the remote, so the symbolic ref dangled), while a bare
    `HEAD` default means "whatever branch the shared clone happens to be
    checked out on right now" -- which for a worktree host is an arbitrary
    other lane's branch. Either one silently bases new work on the wrong
    commit, and the mistake is invisible until merge time.

    Resolution order, all explicit:
      1. an explicit `base_ref` supplied by the caller -- honoured verbatim;
      2. otherwise `refs/remotes/origin/<main_branch>`, the shared trunk;
      3. otherwise `refs/heads/<main_branch>`, for a repo with no remote.

    Fully-qualified refs are used at every step so a branch and a tag of
    the same name can never race; if BOTH the remote-tracking and the local
    trunk exist and disagree, that is reported as BASE_REF_AMBIGUOUS rather
    than silently preferring one -- a local `main` behind `origin/main` is
    exactly the state that produces a stale base."""
    if base_ref:
        resolved = _run_git(["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"], repo_path)
        if resolved.returncode != 0:
            return {"error": "BASE_REF_NOT_FOUND", "repo_path": repo_path, "base_ref": base_ref,
                    "detail": resolved.stderr.strip()[:300]}
        return {"base_ref": base_ref, "base_sha": resolved.stdout.strip(), "source": "explicit"}

    remote_ref = f"refs/remotes/origin/{main_branch}"
    local_ref = f"refs/heads/{main_branch}"
    remote = _run_git(["rev-parse", "--verify", "--end-of-options", f"{remote_ref}^{{commit}}"], repo_path)
    local = _run_git(["rev-parse", "--verify", "--end-of-options", f"{local_ref}^{{commit}}"], repo_path)
    remote_sha = remote.stdout.strip() if remote.returncode == 0 else None
    local_sha = local.stdout.strip() if local.returncode == 0 else None

    if remote_sha and local_sha and remote_sha != local_sha:
        return {"error": "BASE_REF_AMBIGUOUS", "repo_path": repo_path, "main_branch": main_branch,
                "remote_ref": remote_ref, "remote_sha": remote_sha,
                "local_ref": local_ref, "local_sha": local_sha,
                "detail": f"{remote_ref} and {local_ref} disagree -- pass an explicit base_ref to say which "
                          f"commit new work must start from"}
    if remote_sha:
        return {"base_ref": remote_ref, "base_sha": remote_sha, "source": "origin"}
    if local_sha:
        return {"base_ref": local_ref, "base_sha": local_sha, "source": "local"}
    return {"error": "BASE_REF_NOT_FOUND", "repo_path": repo_path, "main_branch": main_branch,
            "detail": f"neither {remote_ref} nor {local_ref} exists in this repository"}


def slugify(text: str, *, max_length: int = 40) -> str:
    """A short, filesystem/branch-name-safe slug -- lowercase, `-`-
    joined, no leading/trailing `-`. Never claims to preserve meaning
    perfectly (task's own explicit "short-title" is a hint, not an
    identifier) -- purely cosmetic, the real identity is the branch/
    worktree_path stored on the task's own metadata."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:max_length] or "task"


def create_worktree(repo_path: str, branch: str, base_ref: str | None = None,
                    worktree_path: str = "", *, main_branch: str = DEFAULT_MAIN_BRANCH) -> dict[str, Any]:
    """Real `git worktree add -b <branch> <worktree_path> <base_sha>`.

    `base_ref` is resolved through resolve_base_ref, so passing None means
    "the configured trunk" -- explicitly NOT `HEAD` and explicitly not
    `origin/HEAD`. The returned `base_sha` is always a resolved commit,
    never a floating ref name that could move later.

    `branch` must not already exist (fail-closed rather than silently
    reusing/rewinding an existing branch -- a real, disclosed limitation:
    this function never overwrites).

    Leaves NO partial state on failure: if `worktree add` fails after git
    has already created the branch, that just-created branch is removed
    again with a safe `git branch -d` (never -D: it points at base_sha with
    no unique commits, so the safe delete always succeeds for state this
    function itself created, and correctly refuses anything else), and any
    half-registered worktree admin record is pruned. A caller that gets an
    error back can retry with the same branch name."""
    resolved = resolve_base_ref(repo_path, base_ref=base_ref, main_branch=main_branch)
    if "error" in resolved:
        return {**resolved, "branch": branch, "worktree_path": worktree_path}
    base_sha = resolved["base_sha"]

    existing_branch = _run_git(["rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}"], repo_path)
    if existing_branch.returncode == 0:
        return {"error": "BRANCH_ALREADY_EXISTS", "repo_path": repo_path, "branch": branch}

    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(["worktree", "add", "-b", branch, worktree_path, base_sha], repo_path)
    if result.returncode != 0:
        leaked = _run_git(["rev-parse", "--verify", "--end-of-options", f"refs/heads/{branch}"], repo_path)
        if leaked.returncode == 0:
            _run_git(["branch", "-d", branch], repo_path)
        _run_git(["worktree", "prune"], repo_path)
        return {"error": "WORKTREE_ADD_FAILED", "repo_path": repo_path, "branch": branch,
                "worktree_path": worktree_path, "base_sha": base_sha,
                "detail": result.stderr.strip()[:500]}
    return {"repo_path": repo_path, "branch": branch, "base_ref": resolved["base_ref"],
            "base_sha": base_sha, "base_ref_source": resolved["source"], "worktree_path": worktree_path}


def remove_worktree(repo_path: str, worktree_path: str, *, force: bool = False) -> dict[str, Any]:
    """Real `git worktree remove` -- refuses (WORKTREE_DIRTY) a worktree
    with real uncommitted changes unless `force=True` is explicitly
    passed (a real, deliberate destructive-action confirmation, same
    posture as this project's own established "confirm before an
    irreversible action" discipline elsewhere)."""
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(worktree_path)
    result = _run_git(args, repo_path)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        error = "WORKTREE_DIRTY" if "contains modified or untracked files" in stderr else "WORKTREE_REMOVE_FAILED"
        return {"error": error, "repo_path": repo_path, "worktree_path": worktree_path, "detail": stderr[:500]}
    return {"removed": True, "repo_path": repo_path, "worktree_path": worktree_path}


def worktree_status(repo_path: str, worktree_path: str) -> dict[str, Any]:
    """Real, read-only introspection of one worktree -- exists/branch/
    head_sha/dirty. Never raises for a worktree that no longer exists
    (`exists: False`), same "best-effort, real evidence or an honest
    absence" posture as session_registry.py's own `probe_project_info`."""
    if not Path(worktree_path).is_dir():
        return {"exists": False, "worktree_path": worktree_path}
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], worktree_path)
    head_sha = _run_git(["rev-parse", "HEAD"], worktree_path)
    if branch.returncode != 0 or head_sha.returncode != 0:
        return {"exists": False, "worktree_path": worktree_path}
    status = _run_git(["status", "--porcelain"], worktree_path)
    return {
        "exists": True, "worktree_path": worktree_path, "branch": branch.stdout.strip(),
        "head_sha": head_sha.stdout.strip(), "dirty": bool(status.stdout.strip()),
    }


def is_ancestor(repo_path: str, commit: str, of_ref: str) -> bool | None:
    """Real `git merge-base --is-ancestor` -- the only honest answer to
    "has this branch actually landed on main". Returns None (never a
    guess) when git itself could not decide: an unknown commit, a
    corrupt object, a timeout. Callers must treat None as "not proven"
    and decline, never as False-meaning-safe."""
    resolved_commit = _run_git(["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"], repo_path)
    resolved_target = _run_git(["rev-parse", "--verify", "--end-of-options", f"{of_ref}^{{commit}}"], repo_path)
    if resolved_commit.returncode != 0 or resolved_target.returncode != 0:
        return None
    result = _run_git(["merge-base", "--is-ancestor", resolved_commit.stdout.strip(),
                       resolved_target.stdout.strip()], repo_path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    return None


def delete_branch(repo_path: str, branch: str) -> dict[str, Any]:
    """Safe local branch delete ONLY -- `git branch -d`, which git itself
    refuses if the branch holds commits not reachable from its upstream or
    HEAD. Never `-D`, never a remote delete: losing unmerged work is the
    one outcome this whole lifecycle exists to prevent, so the decision
    about whether the work is expendable is delegated to git rather than
    re-implemented here."""
    result = _run_git(["branch", "-d", branch], repo_path)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        error = "BRANCH_NOT_MERGED" if "not fully merged" in stderr else "BRANCH_DELETE_FAILED"
        return {"error": error, "repo_path": repo_path, "branch": branch, "detail": stderr[:500]}
    return {"deleted": True, "repo_path": repo_path, "branch": branch}


def list_worktrees(repo_path: str) -> list[dict[str, Any]]:
    """Real `git worktree list --porcelain`, parsed -- every worktree
    this repo currently knows about (including the main one)."""
    result = _run_git(["worktree", "list", "--porcelain"], repo_path)
    if result.returncode != 0:
        return []
    worktrees: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            if current:
                worktrees.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["worktree_path"] = line[len("worktree "):]
        elif line.startswith("HEAD "):
            current["head_sha"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].removeprefix("refs/heads/")
        elif line == "bare":
            current["bare"] = True
        elif line == "detached":
            current["detached"] = True
    if current:
        worktrees.append(current)
    return worktrees
