"""The integrator's own working tree -- it must never merge in the shared one.

THE DEFECT THIS CLOSES

`IntegrationEngine._merge` ran `git checkout <integration_branch>` and `git
merge` with `cwd=pipeline["repo_path"]` -- the SHARED repository working tree,
the one a human or a coding session is using right now. Three separate ways
that hurts, none of them hypothetical:

  * it switches someone else's branch out from under them mid-edit;
  * a conflict leaves conflicted files in THEIR tree, so their next `git
    status` is full of markers they did not create;
  * `_promote` then does `git checkout <main_branch>` and moves HEAD again.

A merge is not a read. It mutates a working tree, so it needs a working tree
of its own.

WHAT THIS MODULE GUARANTEES

  ACQUIRE      a dedicated worktree per (repo, project), created from the
               integration branch, at a DETERMINISTIC path -- deterministic is
               what makes a restart resume rather than accumulate.
  VALIDATE     repo evidence is checked BEFORE anything is created: the repo
               resolves, the branch exists, the path is not inside the shared
               tree.
  ISOLATE      every git mutation the engine performs happens there.
  RECOVER      a conflict or crash leaves the worktree in place with its
               evidence intact, and the next acquire adopts it.
  CLEAN UP     only when demonstrably safe. Never `--force`, never on a dirty
               or unmerged tree.

WHY CLEANUP IS THE CONSERVATIVE HALF

A janitor that deletes a worktree holding the only copy of a half-finished
merge destroys the evidence needed to understand why it failed. So `release`
refuses a dirty tree, refuses one mid-merge, and returns the reason. Leaving a
stale worktree costs disk; deleting a live one costs the investigation. The
periodic sweep for genuinely orphaned ones is Worktree Janitor P3
(blg_ab2a3b46730a) and deliberately not reimplemented here.

CONCURRENCY

Two integrator ticks for the same project must not share a worktree -- one
would abort the other's merge. The lock is `lease.ResourceLockStore`, the same
atomic check-and-set the rest of this system uses, keyed on the worktree
resource. A second integrator is told WHO holds it and until when, rather than
proceeding into a tree that is mid-merge.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .git_worktree import (create_worktree, list_worktrees, remove_worktree,
                           slugify, worktree_status)
from .lease import ResourceLockStore

# Errors, as stable strings: they reach the engine, the store and the
# dashboard, and a caller may branch on them.
REPO_NOT_FOUND = "REPO_NOT_FOUND"
BRANCH_NOT_FOUND = "INTEGRATION_BRANCH_NOT_FOUND"
LOCK_HELD = "INTEGRATION_WORKTREE_LOCKED"
WORKTREE_DIRTY = "INTEGRATION_WORKTREE_DIRTY"
WORKTREE_MID_MERGE = "INTEGRATION_WORKTREE_MID_MERGE"
WORKTREE_WRONG_BRANCH = "INTEGRATION_WORKTREE_WRONG_BRANCH"
CREATE_FAILED = "INTEGRATION_WORKTREE_CREATE_FAILED"
INSIDE_SHARED_TREE = "WORKTREE_PATH_INSIDE_SHARED_TREE"

DEFAULT_LOCK_TTL_SECONDS = 900.0
RESOURCE_PREFIX = "integration-worktree"

# Sibling of the repository, never inside it: a worktree nested in the shared
# tree shows up in that tree's own `git status` as untracked noise, and a
# careless `git clean` there would delete it.
WORKTREE_DIRNAME = ".terminal-mcp-integration"


def _run_git(args: list[str], cwd: str, *, timeout: float = 30.0):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout, check=False)


def worktree_path_for(repo_path: str, project: str) -> str:
    """Deterministic, per (repo, project), beside the repo.

    Deterministic on purpose: a restarted integrator must find the SAME tree
    rather than create a second one, which is what turns a crash into a resume
    instead of a leak.
    """
    repo = Path(repo_path).resolve()
    return str(repo.parent / WORKTREE_DIRNAME / f"{repo.name}-{slugify(project)}")


def branch_name_for(project: str, integration_branch: str) -> str:
    """The worktree checks out the integration branch itself.

    Not a copy of it: the whole point is that merges land on the real
    integration branch. Git refuses to check the same branch out in two
    worktrees at once, which is a second, independent guard against the shared
    tree also sitting on it.
    """
    return integration_branch


def _repo_evidence(repo_path: str, integration_branch: str) -> dict[str, Any] | None:
    """Refuse to act on a repo we cannot verify. Returns an error, or None."""
    if not Path(repo_path).is_dir():
        return {"error": REPO_NOT_FOUND, "repo_path": repo_path,
                "detail": "path does not exist"}
    top = _run_git(["rev-parse", "--show-toplevel"], repo_path)
    if top.returncode != 0:
        return {"error": REPO_NOT_FOUND, "repo_path": repo_path,
                "detail": top.stderr.strip()[:300] or "not a git repository"}
    branch = _run_git(["rev-parse", "--verify", f"refs/heads/{integration_branch}"], repo_path)
    if branch.returncode != 0:
        return {"error": BRANCH_NOT_FOUND, "repo_path": repo_path,
                "branch": integration_branch,
                "detail": "the integration branch does not exist in this repository"}
    return None


def _is_mid_merge(worktree_path: str) -> bool:
    """A worktree left mid-merge by a crash. Its state is evidence."""
    git_dir = _run_git(["rev-parse", "--git-dir"], worktree_path)
    if git_dir.returncode != 0:
        return False
    base = Path(worktree_path) / git_dir.stdout.strip() \
        if not os.path.isabs(git_dir.stdout.strip()) else Path(git_dir.stdout.strip())
    return any((base / marker).exists()
               for marker in ("MERGE_HEAD", "REVERT_HEAD", "CHERRY_PICK_HEAD"))


def prune_stale_metadata(repo_path: str) -> dict[str, Any]:
    """`git worktree prune` -- drops registrations whose directory is gone.

    Safe by construction: prune only removes BOOKKEEPING for a directory that
    no longer exists. It deletes no files and is not `--force`.
    """
    result = _run_git(["worktree", "prune"], repo_path)
    return {"pruned": result.returncode == 0,
            "detail": result.stderr.strip()[:300] or None}


def acquire(repo_path: str, project: str, *, integration_branch: str,
            owner_id: str, locks: ResourceLockStore | None = None,
            ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS) -> dict[str, Any]:
    """Get this project's integration worktree, creating it if needed.

    Order matters: evidence first, then the lock, then the tree. Taking the
    lock before validating would leave a lock held over a repo that was never
    usable, and creating before locking would race a second integrator.
    """
    evidence = _repo_evidence(repo_path, integration_branch)
    if evidence is not None:
        return evidence

    target = worktree_path_for(repo_path, project)
    resolved_repo = Path(repo_path).resolve()
    if resolved_repo == Path(target).resolve() or resolved_repo in Path(target).resolve().parents:
        return {"error": INSIDE_SHARED_TREE, "repo_path": repo_path,
                "worktree_path": target,
                "detail": "refusing a worktree nested inside the shared working tree"}

    locks = locks or ResourceLockStore()
    resource = f"{RESOURCE_PREFIX}:{Path(repo_path).resolve().name}"
    taken = locks.acquire(project, resource, owner_id, ttl_seconds=ttl_seconds,
                          reason=f"integration merge in {target}")
    if not taken.get("acquired"):
        return {"error": LOCK_HELD, "resource_key": resource,
                "holder": taken.get("holder"), "worktree_path": target,
                "detail": "another integrator holds this project's worktree"}

    # Registrations whose directory vanished (a deleted checkout, a wiped
    # scratch disk) would otherwise make `worktree add` refuse the path.
    prune_stale_metadata(repo_path)

    status = worktree_status(repo_path, target)
    if status.get("exists"):
        adopted = _adopt(repo_path, target, status, integration_branch)
        adopted.setdefault("resource_key", resource)
        adopted.setdefault("owner_id", owner_id)
        if "error" in adopted:
            # The tree is not usable, but it holds evidence -- keep the lock
            # released so a human or the janitor can look without racing us.
            locks.release(project, resource, owner_id)
        return adopted

    created = create_worktree(repo_path, branch_name_for(project, integration_branch),
                              integration_branch, target)
    if "error" in created:
        detail = created.get("detail", "")
        if created["error"] == "BRANCH_ALREADY_EXISTS":
            # The branch exists (it is the integration branch -- it always
            # does); check it out into the new path instead of creating it.
            add = _run_git(["worktree", "add", target, integration_branch], repo_path)
            if add.returncode != 0:
                locks.release(project, resource, owner_id)
                return {"error": CREATE_FAILED, "repo_path": repo_path,
                        "worktree_path": target, "detail": add.stderr.strip()[:500]}
        else:
            locks.release(project, resource, owner_id)
            return {"error": CREATE_FAILED, "repo_path": repo_path,
                    "worktree_path": target, "detail": detail[:500]}

    fresh = worktree_status(repo_path, target)
    return {"worktree_path": target, "branch": fresh.get("branch") or integration_branch,
            "head_sha": fresh.get("head_sha"), "created": True,
            "resource_key": resource, "owner_id": owner_id}


def _adopt(repo_path: str, target: str, status: dict[str, Any],
           integration_branch: str) -> dict[str, Any]:
    """Reuse an existing worktree, or refuse it with its state named.

    This is the restart path. A worktree left behind by a crashed tick is the
    right one to continue in -- unless it is mid-merge or dirty, in which case
    continuing would silently fold someone's half-finished state into the next
    merge.
    """
    if _is_mid_merge(target):
        return {"error": WORKTREE_MID_MERGE, "worktree_path": target,
                "branch": status.get("branch"), "head_sha": status.get("head_sha"),
                "detail": ("a previous merge/revert stopped part-way; its state is kept "
                           "for inspection and is not cleaned up automatically")}
    if status.get("dirty"):
        return {"error": WORKTREE_DIRTY, "worktree_path": target,
                "branch": status.get("branch"), "head_sha": status.get("head_sha"),
                "detail": "uncommitted changes present; refusing to merge over them"}
    if status.get("branch") != integration_branch:
        checkout = _run_git(["checkout", integration_branch], target)
        if checkout.returncode != 0:
            return {"error": WORKTREE_WRONG_BRANCH, "worktree_path": target,
                    "branch": status.get("branch"), "expected": integration_branch,
                    "detail": checkout.stderr.strip()[:300]}
    refreshed = worktree_status(repo_path, target)
    return {"worktree_path": target, "branch": refreshed.get("branch") or integration_branch,
            "head_sha": refreshed.get("head_sha"), "created": False, "adopted": True}


def acquire_promote_tree(repo_path: str, project: str, *, main_branch: str,
                         owner_id: str, locks: ResourceLockStore | None = None,
                         ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS) -> dict[str, Any]:
    """A separate worktree for the promote step, checked out on main.

    Promotion is the one operation that legitimately lands on the main branch,
    and it is the one that used to drag the shared tree onto main to do it.

    A real constraint, surfaced rather than worked around: git refuses to check
    the same branch out in two worktrees. So if the SHARED tree is itself
    sitting on `main_branch`, this fails closed with that reason. That is the
    correct outcome -- the alternative is moving someone else's HEAD, which is
    the whole defect. An operator resolves it by moving the shared tree off
    main, or by promoting from a checkout that is not in use.
    """
    evidence = _repo_evidence(repo_path, main_branch)
    if evidence is not None:
        return evidence

    target = worktree_path_for(repo_path, project) + "-promote"
    locks = locks or ResourceLockStore()
    resource = f"{RESOURCE_PREFIX}-promote:{Path(repo_path).resolve().name}"
    taken = locks.acquire(project, resource, owner_id, ttl_seconds=ttl_seconds,
                          reason=f"integration promote in {target}")
    if not taken.get("acquired"):
        return {"error": LOCK_HELD, "resource_key": resource,
                "holder": taken.get("holder"), "worktree_path": target}

    prune_stale_metadata(repo_path)
    status = worktree_status(repo_path, target)
    if status.get("exists"):
        adopted = _adopt(repo_path, target, status, main_branch)
        adopted.setdefault("resource_key", resource)
        if "error" in adopted:
            locks.release(project, resource, owner_id)
        return adopted

    add = _run_git(["worktree", "add", target, main_branch], repo_path)
    if add.returncode != 0:
        locks.release(project, resource, owner_id)
        return {"error": CREATE_FAILED, "repo_path": repo_path, "worktree_path": target,
                "detail": (add.stderr.strip()[:500]
                           or f"could not check out {main_branch} into its own worktree")}
    fresh = worktree_status(repo_path, target)
    return {"worktree_path": target, "branch": fresh.get("branch") or main_branch,
            "head_sha": fresh.get("head_sha"), "created": True,
            "resource_key": resource, "owner_id": owner_id}


def release(repo_path: str, project: str, *, owner_id: str,
            locks: ResourceLockStore | None = None, remove: bool = False,
            resource_key: str | None = None) -> dict[str, Any]:
    """Drop the lock, and optionally remove the worktree when that is SAFE.

    `remove=False` by default. Keeping the tree is cheap and makes the next
    tick a resume; removing it is only ever right when it is clean, and never
    with `--force` -- a forced removal of a tree holding a half-finished merge
    destroys exactly the evidence needed to explain the failure.
    """
    locks = locks or ResourceLockStore()
    resource = resource_key or f"{RESOURCE_PREFIX}:{Path(repo_path).resolve().name}"
    target = worktree_path_for(repo_path, project)

    removed: dict[str, Any] | None = None
    if remove:
        status = worktree_status(repo_path, target)
        if not status.get("exists"):
            removed = {"removed": False, "reason": "worktree does not exist"}
        elif _is_mid_merge(target):
            removed = {"removed": False, "reason": WORKTREE_MID_MERGE,
                       "detail": "left in place so the failed merge can be inspected"}
        elif status.get("dirty"):
            removed = {"removed": False, "reason": WORKTREE_DIRTY,
                       "detail": "left in place; uncommitted work is never discarded here"}
        else:
            # force is never passed. remove_worktree refuses a dirty tree on
            # its own too -- two independent guards, deliberately.
            removed = remove_worktree(repo_path, target, force=False)

    released = locks.release(project, resource, owner_id)
    return {"released": released, "worktree_path": target,
            **({"removal": removed} if removed is not None else {})}


def shared_tree_state(repo_path: str) -> dict[str, Any]:
    """A fingerprint of the SHARED tree, for proving it was left alone.

    Used by the regression tests, and useful in an incident: branch, HEAD and
    porcelain status together change if anything at all touched that tree.
    """
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    head = _run_git(["rev-parse", "HEAD"], repo_path)
    status = _run_git(["status", "--porcelain"], repo_path)
    return {"branch": branch.stdout.strip(), "head_sha": head.stdout.strip(),
            "status": status.stdout.strip()}


def integration_worktrees(repo_path: str) -> list[dict[str, Any]]:
    """Every worktree this module owns, for the dashboard and the janitor."""
    marker = f"/{WORKTREE_DIRNAME}/"
    return [w for w in list_worktrees(repo_path)
            if marker in str(w.get("worktree_path", ""))]
