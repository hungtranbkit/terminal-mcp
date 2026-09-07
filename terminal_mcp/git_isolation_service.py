"""Git isolation policy -- task creation glue (docs/REQUIREMENTS.md
§20.4). Wires git_worktree.py's real worktree mechanics into the
canonical task-creation path (QueueService.create_task) via the EXISTING
Coordinator gate: a task's `metadata.expected_cwd` is already checked,
live, before every dispatch (coordinator.py's own check -- its error
message literally says "expected worktree", written with exactly this
reuse in mind). This module needed ZERO changes to coordinator.py/
queue_store.py's dispatch logic for the isolation guarantee itself --
only a real worktree to point that existing check at.

Branch naming is `task/<short-random-id>-<slug>`, not `task/T<task_id>-
<slug>` literally: the worktree must exist BEFORE the task does (its
own metadata references the worktree path), so the real task_id
(minted by QueueService.create_task) isn't known yet at branch-creation
time -- a short random id is used for the branch name instead, and the
REAL correlation lives in `metadata.git_isolation` regardless of the
branch string. Traceability is never lost, just not embedded in the
branch name literally.
"""
from __future__ import annotations

import uuid
from typing import Any

from . import git_worktree
from .queue_service import QueueService


class GitIsolationService:
    def __init__(self, queue: QueueService) -> None:
        self.queue = queue

    def create_isolated_task(self, title: str, prompt: str, *, repo_path: str, base_ref: str = "HEAD",
                             session: str | None = None, priority: int = 0, project: str | None = None,
                             metadata: dict[str, Any] | None = None,
                             worktree_root: str | None = None) -> dict[str, Any]:
        """Creates a REAL worktree+branch for a coding task, then creates
        the task itself with `metadata.expected_cwd` pointed at it --
        the same field the Coordinator's own pre-dispatch gate already
        verifies (§8), so a session whose live cwd doesn't match this
        worktree is correctly refused (NEEDS_HUMAN) rather than silently
        proceeding on shared `main`/another task's own worktree.
        `worktree_root` defaults to `<repo_path>/../.terminal-mcp-
        worktrees/<branch>` -- override for a project with its own
        convention."""
        branch = f"task/{uuid.uuid4().hex[:8]}-{git_worktree.slugify(title)}"
        if worktree_root:
            worktree_path = f"{worktree_root.rstrip('/')}/{branch.replace('/', '-')}"
        else:
            import os
            worktree_path = os.path.join(os.path.dirname(os.path.normpath(repo_path)),
                                         ".terminal-mcp-worktrees", branch.replace("/", "-"))
        result = git_worktree.create_worktree(repo_path, branch, base_ref, worktree_path)
        if "error" in result:
            return result

        full_metadata = dict(metadata or {})
        full_metadata["expected_cwd"] = result["worktree_path"]
        full_metadata["git_isolation"] = {
            "repo_path": repo_path, "branch": result["branch"], "base_sha": result["base_sha"],
            "worktree_path": result["worktree_path"],
        }
        created = self.queue.create_task(title, prompt, session=session, priority=priority, project=project,
                                         metadata=full_metadata)
        if "error" in created:
            # Real, rare rollback: the worktree was created but the task
            # itself couldn't be (e.g. an invalid session name) -- clean
            # up rather than leak a real worktree nothing will ever use.
            git_worktree.remove_worktree(repo_path, result["worktree_path"], force=True)
            return created
        created["git_isolation"] = full_metadata["git_isolation"]
        return created

    def worktree_status_for_task(self, task_id: str) -> dict[str, Any]:
        status = self.queue.task_status(task_id)
        if "error" in status:
            return status
        isolation = (status["task"].get("metadata") or {}).get("git_isolation")
        if not isolation:
            return {"error": "TASK_NOT_ISOLATED", "task_id": task_id}
        result = git_worktree.worktree_status(isolation["repo_path"], isolation["worktree_path"])
        result["task_id"] = task_id
        result["branch"] = isolation["branch"]
        return result

    def cleanup_worktree_for_task(self, task_id: str, *, force: bool = False) -> dict[str, Any]:
        """Explicit, manual cleanup -- never automatic (a worktree might
        still be genuinely needed for debugging even after its own task
        reaches a terminal state; same "no background loop" posture as
        every other checkpoint in this section)."""
        status = self.queue.task_status(task_id)
        if "error" in status:
            return status
        isolation = (status["task"].get("metadata") or {}).get("git_isolation")
        if not isolation:
            return {"error": "TASK_NOT_ISOLATED", "task_id": task_id}
        result = git_worktree.remove_worktree(isolation["repo_path"], isolation["worktree_path"], force=force)
        result["task_id"] = task_id
        return result
