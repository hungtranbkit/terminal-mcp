"""Lifecycle Close-Loop V1 -- the three edges nothing owned.

Before this module, each stage of the delivery pipeline was individually
complete and none of them was connected to the next:

  * verify_queue.py ran a full VERIFY_PENDING -> VERIFIED_PASS machine and
    was referenced by no integration module at all, so a PASS enqueued no
    merge and a merge required no PASS;
  * integration_engine.promote_to_main moved `main` and returned
    `{"action": "PROMOTED"}`, never creating a release -- release_service
    was imported only by mcp_app.py and its own tests, so DEPLOYED was
    reachable only by a human typing;
  * git_isolation_service.cleanup_worktree_for_task was documented
    "never automatic", and maintenance.py pruned audit rows and WAL but
    no worktree, so every worktree a task ever created accumulated.

This service supplies exactly those three edges and nothing else. It is
deliberately NOT a deploy executor: nothing here builds, ships or restarts
anything, and DEPLOYING -> DEPLOYED remains a human transition through
ReleaseService.

TWO LAYERS, ONE MEANING
Each edge is reachable two ways and both produce the same result:

  fast path   a hook fired right after the upstream state committed
              (VerifyQueue.on_verified_pass, IntegrationEngine.on_promoted)
  repair path reconcile(), which re-derives the same edge from durable
              state and fixes whatever the hook missed -- hook never wired,
              process killed between commit and hook, hook raised.

The hooks are therefore never load-bearing; they only shorten latency. All
three edges are guarded by lifecycle_store's claim/settle keys so running
both layers concurrently performs each side effect once.

CLEANUP IS NOT A CONSEQUENCE OF MERGING
Code reaching `main` is not evidence that the worktree it came from is
finished with -- a bug found in production is exactly when someone needs
that tree still standing. Cleanup therefore waits for VERIFIED_PROD, the
one state that means production has been checked, and every guard below
must pass before anything is removed. Nothing here ever forces: no
`worktree remove --force`, no `branch -D`, no remote deletion.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from . import git_worktree
from .integration_store import publish_handoff_for_completed_task
from .lifecycle_store import (
    CLAIM_KIND_CLEANUP,
    CLAIM_KIND_HANDOFF,
    CLAIM_KIND_RELEASE,
    CLEANUP_BLOCKED,
    CLEANUP_CLEANED,
    LifecycleStore,
    cleanup_request_key,
    handoff_request_key,
    release_request_key,
)
from .release_store import MERGED, VERIFIED_PROD

_LOGGER = logging.getLogger(__name__)

DEFAULT_RECONCILE_LIMIT = 50
"""Every reconcile pass is bounded. An unbounded sweep over a backlog that
grew while the loop was down would hold the maintenance thread for an
unpredictable time and, worse, make a single bad row able to stall every
other repair behind it forever. A bounded pass always finishes and always
makes progress; the next tick picks up the remainder."""

DEFAULT_ENVIRONMENT = "dev"
"""Releases this service creates automatically land in the LOWEST
environment on purpose. release_service.py requires a rollback plan and an
explicit human `approved_by` before anything prod-bound moves, and an
automatic promotion has neither -- so an auto-created release records that
main moved and then stops, leaving the environment promotion exactly as
manual as it was before this module existed."""


class LifecycleService:
    """Owns the three lifecycle edges. Holds no state of its own beyond its
    store; every decision is re-derived from git, the queue, the
    integration store and the release store on each call, so two instances
    (a background loop and a manual MCP call) never disagree."""

    def __init__(self, *, queue: Any, integration: Any, release: Any,
                 store: LifecycleStore | None = None,
                 leases: Any = None, resource_locks: Any = None,
                 session_registry: Any = None,
                 events: Any = None,
                 worktree_roots: tuple[str, ...] = (),
                 main_branch: str = git_worktree.DEFAULT_MAIN_BRANCH,
                 environment: str = DEFAULT_ENVIRONMENT,
                 allow_unverified_integration: bool = True) -> None:
        self.queue = queue
        self.integration = integration
        self.release = release
        self.store = store or LifecycleStore()
        self.leases = leases
        self.resource_locks = resource_locks
        self.session_registry = session_registry
        self.events = events
        self.allow_unverified_integration = allow_unverified_integration
        # realpath, not abspath: the containment check below resolves the
        # candidate worktree with realpath too, and comparing a resolved
        # path against an unresolved root silently fails to match whenever
        # any component of the root is a symlink -- which would refuse
        # every cleanup on such a host rather than merely mis-report one.
        self.worktree_roots = tuple(
            os.path.realpath(os.path.expanduser(r)) for r in worktree_roots)
        self.main_branch = main_branch
        self.environment = environment

    # -- helpers ----------------------------------------------------------

    @property
    def _queue_store(self) -> Any:
        return getattr(self.queue, "store", self.queue)

    @property
    def _integration_store(self) -> Any:
        return getattr(self.integration, "store", self.integration)

    @property
    def _release_store(self) -> Any:
        return getattr(self.release, "store", self.release)

    def _task(self, task_id: str) -> Any:
        return self._queue_store.get_task(task_id)

    # -- edge 1: VERIFIED_PASS -> integration handoff ----------------------

    def on_verified_pass(self, job: Any) -> dict[str, Any]:
        """VerifyQueue hook. Kept to exactly the signature that queue hands
        it so the wiring in mcp_app.py stays a one-liner."""
        return self.ensure_handoff_for_task(job.task_id)

    def ensure_handoff_for_task(self, task_id: str) -> dict[str, Any]:
        """Publish the integration handoff for a task whose verification
        passed -- at most once per (task, commit) pair.

        Opt-in is unchanged: publish_handoff_for_completed_task only acts
        on a task whose own metadata declares `integration_required`, and
        a task without it is reported SKIPPED_NOT_OPTED_IN rather than
        forced into the merge queue. What changed is the TRIGGER -- this
        runs off the verify verdict, where the old path ran off task
        completion and so let an unverified task into integration."""
        task = self._task(task_id)
        if task is None:
            return {"action": "SKIPPED", "reason": "TASK_NOT_FOUND", "task_id": task_id}

        spec = (task.metadata or {}).get("integration_required")
        if not isinstance(spec, dict):
            return {"action": "SKIPPED", "reason": "SKIPPED_NOT_OPTED_IN", "task_id": task_id}
        commit_sha = spec.get("commit_sha")
        if not commit_sha:
            return {"action": "SKIPPED", "reason": "INCOMPLETE_INTEGRATION_SPEC", "task_id": task_id}

        existing = self._integration_store.find_handoff_for_task(task_id, commit_sha=commit_sha)
        if existing is not None:
            return {"action": "ALREADY_PUBLISHED", "task_id": task_id, "handoff_id": existing.id}

        key = handoff_request_key(task_id, commit_sha)
        if not self.store.claim(key, kind=CLAIM_KIND_HANDOFF):
            prior = self.store.result_for(key)
            return prior or {"action": "IN_FLIGHT", "task_id": task_id, "request_key": key}

        try:
            handoff = publish_handoff_for_completed_task(task, self._integration_store)
        except Exception as exc:  # noqa: BLE001 -- hand the claim back, never strand the key
            self.store.release_claim(key)
            _LOGGER.exception("lifecycle: handoff publication failed for task %r", task_id)
            return {"action": "FAILED", "reason": "HANDOFF_PUBLISH_FAILED", "task_id": task_id,
                    "detail": str(exc)[:300]}

        if handoff is None:
            # The task opted in but its spec is incomplete (the helper
            # requires project/branch/commit_sha/base_sha). Nothing
            # happened, so the key must not stay claimed -- a corrected
            # task should be publishable on the next pass.
            self.store.release_claim(key)
            return {"action": "SKIPPED", "reason": "INCOMPLETE_INTEGRATION_SPEC", "task_id": task_id}

        result = {"action": "PUBLISHED", "task_id": task_id, "handoff_id": handoff.id,
                  "project": handoff.project, "branch": handoff.branch}
        self.store.settle(key, result)
        return result

    def on_task_completed(self, task: Any) -> dict[str, Any]:
        """The LEGACY trigger, kept working but no longer silent.

        Before this module, a task reaching COMPLETED published its handoff
        directly (mcp_app.py's own `_on_task_completed`), which meant
        integration never required a verification at all -- the verify
        queue could be bypassed entirely just by completing. That path is
        preserved for backward compatibility, because a deployment that
        never opted into the verify queue would otherwise stop integrating
        the moment this lands, but it is now:

          * SUPPRESSED whenever a verify job exists for the task -- the
            verdict owns the edge in that case, and a PASS publishes
            through on_verified_pass while a FAIL/NEEDS_REWORK publishes
            nothing at all;
          * gated by `lifecycle.allow_unverified_integration`, so an
            operator can turn the bypass off outright;
          * and LOUD when it does fire: a VERIFICATION_BYPASSED audit
            event naming the task, so "this reached main unverified" is a
            queryable fact rather than an invisible default.
        """
        verify_queue = getattr(self.queue, "verify_queue", None)
        if verify_queue is not None:
            try:
                if verify_queue.list_jobs(task_id=task.id, limit=1):
                    return {"action": "DEFERRED_TO_VERIFY", "task_id": task.id}
            except Exception:  # noqa: BLE001 -- fall through to the gate below
                _LOGGER.exception("lifecycle: verify-job lookup failed for task %r", task.id)

        spec = (task.metadata or {}).get("integration_required")
        if not isinstance(spec, dict):
            return {"action": "SKIPPED", "reason": "SKIPPED_NOT_OPTED_IN", "task_id": task.id}

        if not self.allow_unverified_integration:
            _LOGGER.warning(
                "lifecycle: refusing to publish a handoff for task %r -- it has no verify job and "
                "lifecycle.allow_unverified_integration is off", task.id)
            return {"action": "REFUSED", "reason": "UNVERIFIED_INTEGRATION_DISABLED", "task_id": task.id}

        self._record_bypass(task)
        return self.ensure_handoff_for_task(task.id)

    def _record_bypass(self, task: Any) -> None:
        _LOGGER.warning(
            "lifecycle: task %r is entering integration WITHOUT a verification -- "
            "published from task completion, not from a VERIFIED_PASS verdict", task.id)
        if self.events is None:
            return
        try:
            self.events.publish(
                "VERIFICATION_BYPASSED",
                project_id=getattr(task, "project_id", None),
                entity_type="task", entity_id=task.id, actor="lifecycle-service",
                # Keyed on the task so a retried completion records the
                # bypass exactly once rather than inflating the count.
                idempotency_key=f"lifecycle:bypass:{task.id}",
                payload={"reason": "handoff published from task completion; "
                                  "no verify job exists for this task"})
        except Exception:  # noqa: BLE001 -- an audit glitch must not block the handoff
            _LOGGER.exception("lifecycle: could not record the verification bypass event")

    # -- edge 2: promoted to main -> durable release -----------------------

    def on_promoted(self, project: str, batch_id: str, main_commit_sha: str) -> dict[str, Any]:
        """IntegrationEngine hook, fired after promote_batch committed."""
        return self.ensure_releases_for_batch(project, batch_id, main_commit_sha=main_commit_sha)

    def ensure_releases_for_batch(self, project: str, batch_id: str, *,
                                  main_commit_sha: str | None = None) -> dict[str, Any]:
        """One release per task in a promoted batch.

        Per TASK rather than per batch because the release row is what the
        reaper later reads to find a worktree, and worktrees belong to
        tasks. `artifact_ref` carries the main SHA: this project has no
        build artifact concept, and the commit on main is the honest
        answer to "what was released".

        Refuses a batch that was not actually promoted -- a failed,
        conflicted or still-running integration must never produce a
        release."""
        batch = self._integration_store.get_batch(batch_id)
        if batch is None:
            return {"action": "SKIPPED", "reason": "BATCH_NOT_FOUND", "batch_id": batch_id}
        if not batch.promoted_to_main:
            return {"action": "SKIPPED", "reason": "BATCH_NOT_PROMOTED", "batch_id": batch_id,
                    "status": batch.status}
        main_sha = main_commit_sha or batch.main_commit_sha
        if not main_sha:
            return {"action": "SKIPPED", "reason": "MAIN_COMMIT_SHA_UNKNOWN", "batch_id": batch_id}

        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for handoff in self._integration_store.list_handoffs_for_batch(batch_id):
            outcome = self._ensure_release_for_handoff(project, handoff, main_sha)
            (created if outcome.get("action") in ("CREATED", "ALREADY_EXISTS") else skipped).append(outcome)
        return {"action": "RELEASES_ENSURED", "batch_id": batch_id, "project": project,
                "main_commit_sha": main_sha, "releases": created, "skipped": skipped}

    def _ensure_release_for_handoff(self, project: str, handoff: Any, main_sha: str) -> dict[str, Any]:
        key = release_request_key(handoff.task_id, main_sha)

        # Read-through BEFORE claiming: the release row itself is the
        # durable truth, and its UNIQUE request_key means a release that
        # already exists answers the question no matter what state the
        # claim row is in (settled, in-flight, or pruned away entirely).
        existing = self._release_store.find_by_request_key(key)
        if existing is not None:
            return {"action": "ALREADY_EXISTS", "task_id": handoff.task_id, "release_id": existing.id}

        if not self.store.claim(key, kind=CLAIM_KIND_RELEASE):
            prior = self.store.result_for(key)
            return prior or {"action": "IN_FLIGHT", "task_id": handoff.task_id, "request_key": key}

        try:
            release = self._release_store.create_release(
                project=project, task_id=handoff.task_id, environment=self.environment,
                artifact_ref=main_sha, request_key=key,
            )
        except Exception as exc:  # noqa: BLE001
            self.store.release_claim(key)
            _LOGGER.exception("lifecycle: release creation failed for task %r", handoff.task_id)
            return {"action": "FAILED", "reason": "RELEASE_CREATE_FAILED", "task_id": handoff.task_id,
                    "detail": str(exc)[:300]}

        result = {"action": "CREATED", "task_id": handoff.task_id, "release_id": release.id,
                  "main_commit_sha": main_sha, "status": release.status}
        self.store.settle(key, result)
        return result

    # -- edge 3: VERIFIED_PROD -> safe cleanup -----------------------------

    def cleanup_release(self, release_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Remove the worktree and local branch a finished release came
        from -- but only once every guard below is PROVEN, never merely
        un-contradicted.

        Refusals are first-class results, not errors: a BLOCKED outcome is
        recorded with its reason so "why is this worktree still here" has a
        queryable answer, and a later pass re-checks and can still clean it
        once the blocker clears."""
        release = self._release_store.get_release(release_id)
        if release is None:
            return {"action": "SKIPPED", "reason": "RELEASE_NOT_FOUND", "release_id": release_id}
        if release.status != VERIFIED_PROD:
            return {"action": "SKIPPED", "reason": "NOT_VERIFIED_PROD", "release_id": release_id,
                    "status": release.status}

        # Answered BEFORE the guards run. Once a tree is removed the guards
        # would report WORKTREE_MISSING, and _blocked would overwrite this
        # task's CLEANED provenance row with a BLOCKED one -- which would
        # then stop reconcile skipping it, so every later pass would churn
        # on an already-finished task forever.
        prior = self.store.get_cleanup(release.task_id)
        if prior and prior.get("outcome") == CLEANUP_CLEANED:
            return {"action": "ALREADY_CLEANED", "release_id": release_id, "task_id": release.task_id,
                    "worktree_path": prior.get("worktree_path"), "branch": prior.get("branch")}

        task = self._task(release.task_id)
        if prior and (prior.get("detail") or {}).get("action") == "PARTIAL":
            # Resume a half-finished cleanup: the tree was removed, the
            # branch delete was refused. Re-running the full guard set here
            # would report WORKTREE_MISSING and bury the real reason
            # forever, so only the remaining step is retried -- and it is
            # still `git branch -d`, so it still cannot lose commits.
            return self._finish_partial_cleanup(release, prior)

        if task is None:
            return self._blocked(release, None, "TASK_NOT_FOUND", "the task this release came from is gone")
        isolation = (task.metadata or {}).get("git_isolation")
        if not isinstance(isolation, dict):
            return {"action": "SKIPPED", "reason": "TASK_NOT_ISOLATED", "release_id": release_id,
                    "task_id": release.task_id}

        repo_path = isolation.get("repo_path")
        worktree_path = isolation.get("worktree_path")
        branch = isolation.get("branch")
        main_branch = isolation.get("main_branch") or self.main_branch
        if not (repo_path and worktree_path and branch):
            return self._blocked(release, isolation, "PATH_MISMATCH",
                                 "git_isolation metadata is missing repo_path/worktree_path/branch")

        verdict = self._cleanup_guards(release, task, isolation, main_branch=main_branch)
        if verdict is not None:
            return verdict
        if dry_run:
            return {"action": "WOULD_CLEAN", "release_id": release_id, "task_id": release.task_id,
                    "worktree_path": worktree_path, "branch": branch}

        key = cleanup_request_key(release.task_id, release.id)
        if not self.store.claim(key, kind=CLAIM_KIND_CLEANUP):
            prior = self.store.result_for(key)
            return prior or {"action": "IN_FLIGHT", "release_id": release_id, "request_key": key}

        removed = git_worktree.remove_worktree(repo_path, worktree_path, force=False)
        if "error" in removed:
            self.store.release_claim(key)
            return self._blocked(release, isolation, removed["error"], removed.get("detail"))

        deleted = git_worktree.delete_branch(repo_path, branch)
        if "error" in deleted:
            # The worktree is gone but the branch survives. That is a
            # legitimate half-state, not a failure to retry blindly: the
            # tree cannot be removed twice, so the key is SETTLED with what
            # actually happened and the branch is reported as still
            # present rather than silently forced away.
            result = {"action": "PARTIAL", "release_id": release_id, "task_id": release.task_id,
                      "worktree_removed": True, "branch_deleted": False,
                      "reason": deleted["error"], "detail": deleted.get("detail"),
                      "worktree_path": worktree_path, "branch": branch}
            self.store.settle(key, result)
            self.store.record_cleanup(
                task_id=release.task_id, release_id=release.id, project=release.project,
                repo_path=repo_path, worktree_path=worktree_path, branch=branch,
                outcome=CLEANUP_BLOCKED, reason=deleted["error"], detail=result)
            return result

        result = {"action": "CLEANED", "release_id": release_id, "task_id": release.task_id,
                  "worktree_removed": True, "branch_deleted": True,
                  "worktree_path": worktree_path, "branch": branch}
        self.store.settle(key, result)
        self.store.record_cleanup(
            task_id=release.task_id, release_id=release.id, project=release.project,
            repo_path=repo_path, worktree_path=worktree_path, branch=branch,
            outcome=CLEANUP_CLEANED, reason=None, detail=result)
        return result

    def _finish_partial_cleanup(self, release: Any, prior: dict[str, Any]) -> dict[str, Any]:
        repo_path, branch = prior.get("repo_path"), prior.get("branch")
        if not (repo_path and branch):
            return {"action": "BLOCKED", "release_id": release.id, "task_id": release.task_id,
                    "reason": "PATH_MISMATCH", "detail": "partial cleanup record has no repo_path/branch"}
        if self._active_owner(release, self._task(release.task_id), {
                "branch": branch, "worktree_path": prior.get("worktree_path") or ""}) is not None:
            return {"action": "BLOCKED", "release_id": release.id, "task_id": release.task_id,
                    "reason": "ACTIVE_OWNER", "branch": branch}
        deleted = git_worktree.delete_branch(repo_path, branch)
        if "error" in deleted:
            result = {**prior.get("detail", {}), "reason": deleted["error"],
                      "detail": deleted.get("detail")}
            self.store.record_cleanup(
                task_id=release.task_id, release_id=release.id, project=release.project,
                repo_path=repo_path, worktree_path=prior.get("worktree_path"), branch=branch,
                outcome=CLEANUP_BLOCKED, reason=deleted["error"], detail=result)
            return result
        result = {"action": "CLEANED", "release_id": release.id, "task_id": release.task_id,
                  "worktree_removed": True, "branch_deleted": True,
                  "worktree_path": prior.get("worktree_path"), "branch": branch}
        self.store.record_cleanup(
            task_id=release.task_id, release_id=release.id, project=release.project,
            repo_path=repo_path, worktree_path=prior.get("worktree_path"), branch=branch,
            outcome=CLEANUP_CLEANED, reason=None, detail=result)
        return result

    def _cleanup_guards(self, release: Any, task: Any, isolation: dict[str, Any], *,
                        main_branch: str) -> dict[str, Any] | None:
        """Returns a BLOCKED result, or None meaning every guard passed.

        Ordered cheapest-and-most-structural first so a misconfigured path
        is reported as PATH_MISMATCH rather than as whatever git happens to
        say about a directory outside the fleet."""
        repo_path = isolation["repo_path"]
        worktree_path = isolation["worktree_path"]
        branch = isolation["branch"]

        # 1. Path containment. An absolute, symlink-resolved prefix check
        #    against an explicitly configured root list -- a path that is
        #    merely string-prefixed by a root ("/w/trees-evil" under
        #    "/w/trees") is rejected by comparing on path components.
        if self.worktree_roots:
            resolved = os.path.realpath(worktree_path)
            if not any(resolved == root or resolved.startswith(root + os.sep)
                       for root in self.worktree_roots):
                return self._blocked(release, isolation, "PATH_MISMATCH",
                                     f"{resolved} is not under any configured worktree root")

        # 2. The worktree exists and is the one this task recorded.
        status = git_worktree.worktree_status(repo_path, worktree_path)
        if not status.get("exists"):
            # Reported as BLOCKED, not silently treated as done: a worktree
            # that vanished without this service removing it is something
            # an operator should see. The branch is deliberately left alone
            # -- a missing directory is no evidence at all about whether
            # the commits on that branch are safe to drop.
            return self._blocked(release, isolation, "WORKTREE_MISSING",
                                 "recorded worktree path does not exist")
        if status.get("branch") != branch:
            return self._blocked(release, isolation, "PATH_MISMATCH",
                                 f"worktree is on {status.get('branch')!r}, task recorded {branch!r}")

        # 3. Clean tree. Uncommitted work is unrecoverable once removed.
        if status.get("dirty"):
            return self._blocked(release, isolation, "WORKTREE_DIRTY",
                                 "worktree has uncommitted or untracked changes")

        # 4. The branch actually landed. Asked of git directly rather than
        #    inferred from the release existing: a release row proves a
        #    promotion was RECORDED, only merge-base proves the commits are
        #    reachable from the trunk right now.
        head = status.get("head_sha")
        merged = git_worktree.is_ancestor(repo_path, head, f"refs/heads/{main_branch}")
        if merged is None:
            merged = git_worktree.is_ancestor(repo_path, head, f"refs/remotes/origin/{main_branch}")
        if merged is not True:
            return self._blocked(release, isolation, "BRANCH_NOT_MERGED",
                                 f"{head} is not an ancestor of {main_branch}"
                                 if merged is False else
                                 f"could not prove {head} is contained in {main_branch}")

        # 5. Nobody still owns it.
        owner = self._active_owner(release, task, isolation)
        if owner is not None:
            return self._blocked(release, isolation, "ACTIVE_OWNER", owner)
        return None

    def _active_owner(self, release: Any, task: Any, isolation: dict[str, Any]) -> str | None:
        """Any evidence a worker is still using this tree. Every probe is
        best-effort and independently optional, but a probe that RAISES is
        treated as ownership -- an unavailable check is not permission to
        delete."""
        worktree_path = isolation["worktree_path"]

        # A live task lease on the owning task.
        claimed_by = getattr(task, "claimed_by", None)
        if claimed_by and getattr(task, "lease_expires_at", None):
            return f"task {task.id} is still claimed by {claimed_by}"

        # A held resource lock naming this branch or path.
        if self.resource_locks is not None:
            try:
                for lock in self.resource_locks.list_locks(project_id=release.project):
                    resource = str(lock.get("resource_key") or "")
                    if isolation["branch"] in resource or worktree_path in resource:
                        return f"resource lock {resource!r} held by {lock.get('owner_id')}"
            except Exception:  # noqa: BLE001
                return "resource lock store unavailable -- declining rather than guessing"

        # A live session whose cwd is inside the tree.
        if self.session_registry is not None:
            try:
                resolved = os.path.realpath(worktree_path)
                for record in self.session_registry.list():
                    cwd = getattr(record, "cwd", None)
                    if not cwd:
                        continue
                    real_cwd = os.path.realpath(cwd)
                    if real_cwd == resolved or real_cwd.startswith(resolved + os.sep):
                        return f"session {record.session_name!r} is live inside this worktree"
            except Exception:  # noqa: BLE001
                return "session registry unavailable -- declining rather than guessing"
        return None

    def _blocked(self, release: Any, isolation: dict[str, Any] | None, reason: str,
                 detail: str | None) -> dict[str, Any]:
        result = {"action": "BLOCKED", "release_id": release.id, "task_id": release.task_id,
                  "reason": reason, "detail": detail}
        if isolation:
            result["worktree_path"] = isolation.get("worktree_path")
            result["branch"] = isolation.get("branch")
        self.store.record_cleanup(
            task_id=release.task_id, release_id=release.id, project=release.project,
            repo_path=(isolation or {}).get("repo_path"),
            worktree_path=(isolation or {}).get("worktree_path"),
            branch=(isolation or {}).get("branch"),
            outcome=CLEANUP_BLOCKED, reason=reason, detail=result)
        return result

    # -- the reconcile pass ------------------------------------------------

    def reconcile(self, *, project: str | None = None,
                  limit: int = DEFAULT_RECONCILE_LIMIT) -> dict[str, Any]:
        """One bounded, deterministic repair pass over all three edges.

        Safe to run concurrently with the hooks and with itself: every edge
        it takes is claim-guarded, and an edge already completed is a plain
        no-op that costs one indexed read. Ordered upstream-first so a
        single pass can carry a task from PASS all the way to CLEANED
        rather than needing three separate ticks."""
        limit = max(1, min(limit, 1000))
        summary: dict[str, Any] = {"handoffs": [], "releases": [], "cleanups": [], "limit": limit}

        # Edge 1: verified, opted in, but no handoff.
        try:
            for task_id in self._verified_task_ids(project=project, limit=limit):
                outcome = self.ensure_handoff_for_task(task_id)
                if outcome.get("action") != "ALREADY_PUBLISHED":
                    summary["handoffs"].append(outcome)
        except Exception:  # noqa: BLE001 -- one broken edge must not stop the others
            _LOGGER.exception("lifecycle reconcile: handoff sweep failed")
            summary["handoffs_error"] = True

        # Edge 2: promoted to main, but no release.
        try:
            for batch in self._integration_store.list_promoted_batches(project=project, limit=limit):
                if batch.main_commit_sha is None:
                    continue
                summary["releases"].append(
                    self.ensure_releases_for_batch(batch.project, batch.id,
                                                   main_commit_sha=batch.main_commit_sha))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("lifecycle reconcile: release sweep failed")
            summary["releases_error"] = True

        # Edge 3: verified in production, worktree still standing.
        try:
            for release in self._release_store.list_by_status(VERIFIED_PROD, project=project, limit=limit):
                prior = self.store.get_cleanup(release.task_id)
                if prior and prior.get("outcome") == CLEANUP_CLEANED:
                    continue
                summary["cleanups"].append(self.cleanup_release(release.id))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("lifecycle reconcile: cleanup sweep failed")
            summary["cleanups_error"] = True

        summary["counts"] = {kind: len(summary[kind]) for kind in ("handoffs", "releases", "cleanups")}
        return summary

    def _verified_task_ids(self, *, project: str | None, limit: int) -> list[str]:
        """Tasks whose verification passed. Whether each still NEEDS a
        handoff is decided by ensure_handoff_for_task itself, against the
        commit in the task's own metadata -- doing it here would duplicate
        that key and let the two drift apart. The two stores live in
        separate database files, so there is no join to push this into:
        SQLite has no cross-database join, the same constraint outcomes.py
        works around for work_id."""
        verify_queue = getattr(self.queue, "verify_queue", None)
        if verify_queue is None:
            return []
        from .verify_queue import VERIFIED_PASS

        seen: list[str] = []
        for job in verify_queue.list_jobs(status=VERIFIED_PASS, project_id=project, limit=limit):
            if job.task_id not in seen:
                seen.append(job.task_id)
        return seen

    # -- read-only surface -------------------------------------------------

    def status(self, *, project: str | None = None) -> dict[str, Any]:
        """What the close-loop currently sees. Read-only: changes nothing,
        so it is safe to call from a dashboard or a health check."""
        merged = self._release_store.list_by_status(MERGED, project=project, limit=200)
        verified = self._release_store.list_by_status(VERIFIED_PROD, project=project, limit=200)
        return {
            "releases_merged": len(merged),
            "releases_verified_prod": len(verified),
            "cleanups_blocked": self.store.list_cleanups(outcome=CLEANUP_BLOCKED, limit=100),
            "cleanups_done": len(self.store.list_cleanups(outcome=CLEANUP_CLEANED, limit=1000)),
            "main_branch": self.main_branch,
            "worktree_roots": list(self.worktree_roots),
        }
