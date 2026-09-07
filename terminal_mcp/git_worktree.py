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


def _run_git(args: list[str], cwd: str, *, timeout: float = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def slugify(text: str, *, max_length: int = 40) -> str:
    """A short, filesystem/branch-name-safe slug -- lowercase, `-`-
    joined, no leading/trailing `-`. Never claims to preserve meaning
    perfectly (task's own explicit "short-title" is a hint, not an
    identifier) -- purely cosmetic, the real identity is the branch/
    worktree_path stored on the task's own metadata."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:max_length] or "task"


def create_worktree(repo_path: str, branch: str, base_ref: str, worktree_path: str) -> dict[str, Any]:
    """Real `git worktree add -b <branch> <worktree_path> <base_ref>` --
    resolves `base_ref` to a real commit SHA first (fail-closed if it
    doesn't exist in this repo) so the returned `base_sha` is always a
    real, resolved commit, never a floating ref name that could move
    later. `branch` must not already exist (fail-closed rather than
    silently reusing/rewinding an existing branch -- a real, disclosed
    limitation: this function never overwrites)."""
    resolved = _run_git(["rev-parse", "--verify", f"{base_ref}^{{commit}}"], repo_path)
    if resolved.returncode != 0:
        return {"error": "BASE_REF_NOT_FOUND", "repo_path": repo_path, "base_ref": base_ref,
                "detail": resolved.stderr.strip()[:300]}
    base_sha = resolved.stdout.strip()

    existing_branch = _run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo_path)
    if existing_branch.returncode == 0:
        return {"error": "BRANCH_ALREADY_EXISTS", "repo_path": repo_path, "branch": branch}

    Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
    result = _run_git(["worktree", "add", "-b", branch, worktree_path, base_sha], repo_path)
    if result.returncode != 0:
        return {"error": "WORKTREE_ADD_FAILED", "repo_path": repo_path, "branch": branch,
                "worktree_path": worktree_path, "detail": result.stderr.strip()[:500]}
    return {"repo_path": repo_path, "branch": branch, "base_sha": base_sha, "worktree_path": worktree_path}


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
