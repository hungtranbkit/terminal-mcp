"""Integration Agent -- the merge/test orchestration engine on top of
integration_store.py (task: "3-role model: Coding A/B + Integration
Agent"). See integration_store.py's own module docstring for the full
role split and why this is a separate store/engine from queue_store.py/
queue_engine.py.

`tick(project)` is the single public entry point: one bounded
reconciliation step (reconcile stale claims, then claim/merge/targeted-
test a handoff, or progress a regression batch) -- same granularity and
restart-safety posture as queue_engine.py's own tick(session).

REWORK ROUTING (item 9): a merge conflict or a failed targeted test
NEVER gets auto-resolved by guessing content -- `_merge`'s own conflict
handling always aborts the merge cleanly (`git merge --abort`, fully
mechanical/reversible) and routes a fresh, ordinary QueueTask back into
the OWNING coding session's own existing queue (queue_store.py's
append_tasks -- never replace_pending, so it can never cancel that
session's own current/other queued work). "Safe mechanical auto-
resolve" in this engine means exactly one thing: a merge that produces
NO conflict at all (including a merge that is a no-op because the
commit is already an ancestor -- see _merge's own idempotency note);
nothing here ever edits a conflicted file's content.

IDEMPOTENCY UNDER RESTART (item 6): merging the exact same commit_sha
into the integration branch twice is a NATURAL git no-op ("Already up
to date", exit 0, no new commit) whenever the first merge's commit is
already an ancestor of HEAD -- this is what makes a stale-claim
reconciliation (a handoff pushed back to READY_FOR_INTEGRATION after a
crash mid-merge) safely re-claimable and re-mergeable without a second,
duplicate merge commit ever being created, with no extra bookkeeping
needed beyond re-deriving merge_commit_sha via `git rev-parse HEAD`
after the (possibly no-op) merge completes.
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Any

from .integration_reviewer import IntegrationReviewGate
from .integration_reviewer import BLOCKED as REVIEW_BLOCKED
from .integration_reviewer import REWORK_REQUIRED as REVIEW_REWORK_REQUIRED
from .integration_store import (
    BLOCKED, CLAIMED, INTEGRATED, MERGE_READY, MERGING, READY_FOR_INTEGRATION, REGRESSION_FAILED,
    REGRESSION_PENDING, REGRESSION_RUNNING, REWORK_REQUIRED, TARGETED_TEST, Handoff, IntegrationStore,
)
from .queue_store import QueueStore

DEFAULT_CLAIMED_BY = "integration-engine"
DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_GIT_TIMEOUT_SECONDS = 60.0
DEFAULT_TEST_TIMEOUT_SECONDS = 1800.0
REWORK_PRIORITY = 10
"""A rework task jumps ahead of a session's other still-QUEUED feature
work (queue_store.py's own priority DESC ordering) -- it does NOT
preempt whatever that session is already actively running."""


@dataclass(frozen=True)
class EngineResult:
    project: str
    action: str
    handoff_id: str | None = None
    batch_id: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"project": self.project, "action": self.action, "handoff_id": self.handoff_id,
                "batch_id": self.batch_id, "detail": self.detail}


def _run_git(args: list[str], cwd: str, *, check: bool = True,
            timeout: float = DEFAULT_GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=check)


def _conflicted_paths(repo_path: str) -> list[str]:
    """Real, mechanical conflict detection -- `git diff --name-only
    --diff-filter=U` lists exactly the paths git itself considers
    unmerged. Never inspects file CONTENT to guess a resolution."""
    result = _run_git(["diff", "--name-only", "--diff-filter=U"], repo_path, check=False)
    return [line for line in result.stdout.splitlines() if line.strip()]


class IntegrationEngine:
    """WAITING_FOR_HANDOFF / event-driven posture (task follow-up:
    "chế độ WAIT/EVENT-DRIVEN cho session Integration/Test"): tick()
    does no polling or background work of its own -- it is only ever
    invoked externally (a test, terminal_integration_run_once, or a
    future scheduler), so an Integration Agent with an empty queue
    consumes ZERO resources between calls, by construction, without
    needing a separate "sleep" mode to opt into. Every call that finds
    nothing to do returns action="WAITING_FOR_HANDOFF" rather than
    silently no-op'ing, so a caller/dashboard can render that as an
    explicit, named state ("merge-test đang chờ việc") rather than an
    ambiguous no-op. The actual "wake" signal is publish_handoff itself
    (integration_store.py) -- it durably records a HANDOFF_PUBLISHED
    event exactly once per handoff (idempotent by construction: publish_
    handoff is called at most once per real completed task), which is
    exactly what the next tick() finds and claims; no separate wake-flag
    bookkeeping is needed since claim_next_handoff's own query already
    detects it. NOT YET BUILT (disclosed limitation, same posture as
    Phase 2's own): an autonomous background loop that calls tick() by
    itself the instant a handoff is published -- today something
    external (a human, ChatGPT, a future scheduler) still has to call
    terminal_integration_run_once; see the final report's own
    limitations section."""

    def __init__(self, store: IntegrationStore, queue_store: QueueStore, *,
                claimed_by: str = DEFAULT_CLAIMED_BY, lease_seconds: float = DEFAULT_LEASE_SECONDS,
                test_timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS,
                reviewer: IntegrationReviewGate | None = None) -> None:
        self.store = store
        self.queue_store = queue_store
        self.claimed_by = claimed_by
        self.lease_seconds = lease_seconds
        self.test_timeout_seconds = test_timeout_seconds
        self.reviewer = reviewer or IntegrationReviewGate()

    def tick(self, project: str) -> EngineResult:
        self.store.reconcile_stale_handoff_claims(project)
        pipeline = self.store.get_pipeline(project)
        if pipeline is None:
            return EngineResult(project, "NOT_CONFIGURED")
        if pipeline["paused"]:
            return EngineResult(project, "PAUSED", detail=pipeline.get("paused_reason") or "")

        active = self._active_handoff(project)
        if active is None:
            claimed = self.store.claim_next_handoff(project, claimed_by=self.claimed_by,
                                                     lease_seconds=self.lease_seconds)
            if claimed is None:
                return self._tick_batch(project, pipeline)
            return EngineResult(project, "CLAIMED", handoff_id=claimed.id)

        if active.status == CLAIMED:
            return self._review_then_merge(project, active, pipeline)
        if active.status == MERGING:
            # Should not normally be observed between ticks (merge runs
            # synchronously within the CLAIMED-handling tick above) --
            # only reachable if a previous tick crashed mid-merge before
            # reconcile_stale_handoff_claims' own lease expired yet.
            # Fail-closed rather than silently retry a possibly-mid-
            # flight git operation.
            return EngineResult(project, "NO_OP", handoff_id=active.id, detail="MERGING is mid-flight; waiting")
        if active.status == TARGETED_TEST:
            return self._run_targeted_test(project, active, pipeline)
        return EngineResult(project, "NO_OP", handoff_id=active.id, detail=f"status={active.status}")

    def _active_handoff(self, project: str) -> Handoff | None:
        for status in (CLAIMED, MERGING, TARGETED_TEST):
            found = self.store.list_handoffs(project, status=status, limit=1)
            if found:
                return found[0]
        return None

    # -- merge ---------------------------------------------------------------

    def _review_then_merge(self, project: str, handoff: Handoff, pipeline: dict[str, Any]) -> EngineResult:
        """CLAIMED -> [pre-merge review] -> MERGING -> [real git merge]
        -> TARGETED_TEST | REWORK_REQUIRED | BLOCKED. The review gate
        (integration_reviewer.py) runs BEFORE any git state is touched
        -- item 3/5's own "review phải là thật... trước merge"."""
        decision = self.reviewer.review(handoff, pipeline)
        if decision.status == REVIEW_BLOCKED:
            self.store.transition_handoff(handoff.id, BLOCKED, event_type="REVIEW_BLOCKED", reason=decision.reason,
                                          extra_fields={"targeted_test_result": {"review": decision.to_dict()}})
            return EngineResult(project, "BLOCKED", handoff_id=handoff.id, detail=decision.reason)
        if decision.status == REVIEW_REWORK_REQUIRED:
            rework_task_id = self._route_rework(project, handoff, pipeline, paths=list(handoff.changed_paths),
                                                reason=decision.reason, evidence=decision.to_dict())
            self.store.transition_handoff(handoff.id, REWORK_REQUIRED, event_type="REVIEW_REWORK_REQUIRED",
                                          reason=decision.reason, extra_fields={"rework_task_id": rework_task_id})
            return EngineResult(project, "REWORK_REQUIRED", handoff_id=handoff.id, detail=decision.reason)

        self.store.transition_handoff(handoff.id, MERGING, event_type="REVIEW_PASSED", reason=None,
                                      extra_fields={"targeted_test_result": {"review": decision.to_dict()}}
                                      if decision.risk_flags else None)
        return self._merge(project, handoff, pipeline)

    def _merge(self, project: str, handoff: Handoff, pipeline: dict[str, Any]) -> EngineResult:
        repo_path = pipeline["repo_path"]
        integration_branch = pipeline["integration_branch"]
        try:
            _run_git(["checkout", integration_branch], repo_path)
            merge_result = _run_git(
                ["merge", "--no-ff", "-m",
                 f"integrate {handoff.branch} ({handoff.commit_sha[:8]}) task={handoff.task_id}", handoff.commit_sha],
                repo_path, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.store.transition_handoff(handoff.id, BLOCKED, event_type="MERGE_FAILED",
                                          reason=f"git operation itself failed: {exc}")
            return EngineResult(project, "BLOCKED", handoff_id=handoff.id, detail=str(exc))

        if merge_result.returncode != 0:
            conflict_paths = _conflicted_paths(repo_path)
            _run_git(["merge", "--abort"], repo_path, check=False)
            if conflict_paths:
                rework_task_id = self._route_rework(
                    project, handoff, pipeline, paths=conflict_paths,
                    reason=f"merge conflict in {', '.join(conflict_paths)}",
                )
                self.store.transition_handoff(
                    handoff.id, REWORK_REQUIRED, event_type="MERGE_CONFLICT",
                    reason=f"merge conflict in {', '.join(conflict_paths)}",
                    extra_fields={"conflict_detected": True, "conflict_paths": conflict_paths,
                                 "rework_task_id": rework_task_id},
                )
                return EngineResult(project, "REWORK_REQUIRED", handoff_id=handoff.id,
                                   detail=f"conflict in {conflict_paths}")
            # A non-zero exit with NO conflicted paths is an unexpected
            # git failure (not a content conflict) -- fail-closed, never
            # guessed at.
            self.store.transition_handoff(handoff.id, BLOCKED, event_type="MERGE_FAILED",
                                          reason=merge_result.stderr.strip()[:500])
            return EngineResult(project, "BLOCKED", handoff_id=handoff.id, detail=merge_result.stderr.strip()[:200])

        merge_commit_sha = _run_git(["rev-parse", "HEAD"], repo_path).stdout.strip()
        self.store.transition_handoff(handoff.id, TARGETED_TEST, event_type="MERGED", reason=None,
                                      extra_fields={"merge_commit_sha": merge_commit_sha})
        return EngineResult(project, "MERGED", handoff_id=handoff.id, detail=merge_commit_sha)

    # -- targeted test ---------------------------------------------------------

    def _run_targeted_test(self, project: str, handoff: Handoff, pipeline: dict[str, Any]) -> EngineResult:
        repo_path = pipeline["repo_path"]
        command = pipeline["targeted_test_command"] or pipeline["full_regression_command"]
        if not command:
            # No test command configured at all -- fail-closed rather
            # than silently treat "nothing configured" as "passed".
            self.store.transition_handoff(handoff.id, BLOCKED, event_type="TARGETED_TEST_FAILED",
                                          reason="no targeted_test_command or full_regression_command configured")
            return EngineResult(project, "BLOCKED", handoff_id=handoff.id, detail="no test command configured")
        expanded = []
        for part in command:
            if part == "{paths}":
                expanded.extend(handoff.changed_paths or ["."])
            else:
                expanded.append(part)
        try:
            result = subprocess.run(expanded, cwd=repo_path, capture_output=True, text=True,
                                    timeout=self.test_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.store.transition_handoff(handoff.id, BLOCKED, event_type="TARGETED_TEST_FAILED",
                                          reason=f"test tooling itself failed to run: {exc}")
            return EngineResult(project, "BLOCKED", handoff_id=handoff.id, detail=str(exc))

        test_result = {"returncode": result.returncode, "command": expanded,
                      "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]}
        if result.returncode == 0:
            self.store.transition_handoff(handoff.id, INTEGRATED, event_type="TARGETED_TEST_PASSED", reason=None,
                                          extra_fields={"targeted_test_result": test_result})
            return EngineResult(project, "INTEGRATED", handoff_id=handoff.id)

        # A failed targeted test must NEVER leave its merge commit
        # sitting on the integration branch -- the branch's own git
        # history must always reflect "content that has actually
        # passed", the same invariant item 10's own "trước promote main
        # luôn phải full required checks green" depends on (a batch
        # regression only re-tests what's already been through here;
        # if a failed merge's content silently stayed on the branch, it
        # would ride along into main regardless of the batch's own
        # handoff bookkeeping). `git revert` (never reset/clean/force)
        # is the safe, mechanical, fully-reversible way to undo exactly
        # this one merge commit while keeping full history.
        revert_ok = self._revert_merge(handoff, pipeline)
        if not revert_ok:
            self.store.transition_handoff(
                handoff.id, BLOCKED, event_type="REVERT_FAILED",
                reason=f"targeted test failed (exit {result.returncode}) AND reverting the merge commit itself "
                      f"failed -- fail-closed, needs human attention",
                extra_fields={"targeted_test_result": test_result},
            )
            return EngineResult(project, "BLOCKED", handoff_id=handoff.id,
                               detail="targeted test failed and revert failed")

        rework_task_id = self._route_rework(
            project, handoff, pipeline, paths=list(handoff.changed_paths),
            reason=f"targeted test failed (exit {result.returncode})", evidence=test_result,
        )
        self.store.transition_handoff(
            handoff.id, REWORK_REQUIRED, event_type="TARGETED_TEST_FAILED",
            reason=f"targeted test failed (exit {result.returncode})",
            extra_fields={"targeted_test_result": test_result, "rework_task_id": rework_task_id},
        )
        return EngineResult(project, "REWORK_REQUIRED", handoff_id=handoff.id, detail=f"exit {result.returncode}")

    def _revert_merge(self, handoff: Handoff, pipeline: dict[str, Any]) -> bool:
        """Reverts exactly ONE merge commit (handoff.merge_commit_sha)
        on the integration branch -- `git revert -m 1` (mainline parent
        1, the integration branch's own prior history) so the branch's
        content returns to its pre-merge state while keeping full,
        honest history of the attempt (never squashed/hidden). Returns
        False (never raises) on any failure -- the caller fail-closes
        to BLOCKED rather than leaving an ambiguous state."""
        if not handoff.merge_commit_sha:
            return True  # nothing was ever merged -- nothing to revert
        repo_path = pipeline["repo_path"]
        try:
            _run_git(["checkout", pipeline["integration_branch"]], repo_path)
            result = _run_git(["revert", "--no-edit", "-m", "1", handoff.merge_commit_sha], repo_path, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return False
        if result.returncode != 0:
            _run_git(["revert", "--abort"], repo_path, check=False)
            return False
        return True

    # -- rework routing (item 9) ---------------------------------------------

    def resolve_rework_owner(self, handoff: Handoff, pipeline: dict[str, Any]) -> str:
        """Primary: the handoff's own origin_session (exact, no
        guessing -- it's literally who published this work). Fallback/
        override: pipeline['session_ownership'], an optional {path_
        prefix: session_name} map, consulted only if one of the
        affected paths matches a DIFFERENT session than origin_session
        -- for the case where ownership boundaries have since moved and
        an operator has explicitly configured the new mapping. Never an
        automatic/inferred guess beyond this explicit config."""
        ownership = pipeline.get("session_ownership") or {}
        for path in handoff.changed_paths:
            for prefix, session in ownership.items():
                if path.startswith(prefix):
                    return session
        return handoff.origin_session

    def _route_rework(self, project: str, handoff: Handoff, pipeline: dict[str, Any], *, paths: list[str],
                      reason: str, evidence: dict[str, Any] | None = None) -> str:
        owner = self.resolve_rework_owner(handoff, pipeline)
        prompt = (
            f"REWORK REQUIRED for branch '{handoff.branch}' (commit {handoff.commit_sha[:8]}, "
            f"originating task {handoff.task_id}): {reason}. Affected paths: {', '.join(paths) or '(unknown)'}. "
            f"Please fix on your own branch/worktree and complete the task normally -- the Integration Agent will "
            f"automatically pick up a fresh handoff once this task's own completion republishes one."
        )
        metadata: dict[str, Any] = {"rework_for_handoff": handoff.id, "rework_reason": reason,
                                    "rework_project": project}
        if evidence:
            metadata["rework_evidence"] = evidence
        (task_id,) = self.queue_store.append_tasks(owner, [{"prompt": prompt, "priority": REWORK_PRIORITY,
                                                            "metadata": metadata}])
        return task_id

    # -- regression batch progression ---------------------------------------

    def _tick_batch(self, project: str, pipeline: dict[str, Any]) -> EngineResult:
        batch = self.store.get_open_batch(project)
        if batch is None:
            return self._maybe_create_batch(project, pipeline)
        if batch.status == REGRESSION_PENDING:
            self.store.transition_batch(batch.id, REGRESSION_RUNNING, event_type="REGRESSION_STARTED")
            return EngineResult(project, "REGRESSION_RUNNING", batch_id=batch.id)
        if batch.status == REGRESSION_RUNNING:
            return self._run_full_regression(project, batch.id, pipeline)
        return EngineResult(project, "WAITING_FOR_HANDOFF")

    def _maybe_create_batch(self, project: str, pipeline: dict[str, Any]) -> EngineResult:
        pending = self.store.pending_batch_handoffs(project)
        if not pending:
            return EngineResult(project, "WAITING_FOR_HANDOFF")
        oldest_integrated_at = min(h.integrated_at for h in pending if h.integrated_at)
        age_seconds = time.time() - time.mktime(time.strptime(oldest_integrated_at, "%Y-%m-%dT%H:%M:%SZ"))
        if len(pending) >= pipeline["batch_size"] or age_seconds >= pipeline["batch_max_wait_seconds"]:
            batch = self.store.create_batch(project, [h.id for h in pending])
            return EngineResult(project, "BATCH_CREATED", batch_id=batch.id, detail=f"{len(pending)} handoffs")
        return EngineResult(project, "WAITING_FOR_HANDOFF", detail=f"{len(pending)} integrated, awaiting batch threshold")

    def _run_full_regression(self, project: str, batch_id: str, pipeline: dict[str, Any]) -> EngineResult:
        repo_path = pipeline["repo_path"]
        command = pipeline["full_regression_command"]
        if not command:
            self.store.transition_batch(batch_id, REGRESSION_FAILED, event_type="REGRESSION_FAILED",
                                        reason="no full_regression_command configured")
            self.store.pause_pipeline(project, reason=f"batch {batch_id}: no full_regression_command configured")
            return EngineResult(project, "REGRESSION_FAILED", batch_id=batch_id,
                               detail="no full_regression_command configured")
        try:
            result = subprocess.run(command, cwd=repo_path, capture_output=True, text=True,
                                    timeout=self.test_timeout_seconds)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.store.transition_batch(batch_id, REGRESSION_FAILED, event_type="REGRESSION_FAILED",
                                        reason=f"regression tooling itself failed to run: {exc}")
            self.store.pause_pipeline(project, reason=f"batch {batch_id}: regression tooling failed")
            return EngineResult(project, "REGRESSION_FAILED", batch_id=batch_id, detail=str(exc))

        test_result = {"returncode": result.returncode, "command": command,
                      "stdout_tail": result.stdout[-2000:], "stderr_tail": result.stderr[-2000:]}
        if result.returncode == 0:
            self.store.transition_batch(batch_id, MERGE_READY, event_type="REGRESSION_PASSED", reason=None,
                                        extra_fields={"full_test_result": test_result})
            if pipeline["auto_promote_enabled"]:
                promotion = self.promote_to_main(project, batch_id)
                return EngineResult(project, promotion["action"], batch_id=batch_id,
                                   detail=promotion.get("detail", ""))
            return EngineResult(project, "MERGE_READY", batch_id=batch_id)

        # Item 10: a full-regression FAILURE stops promotion for THIS
        # batch and pauses the whole project pipeline (never guesses
        # which single handoff in the batch is at fault -- that needs
        # an explicit human/ChatGPT decision, same posture as
        # Coordinator's own NEEDS_HUMAN). resume_pipeline + an explicit
        # retry (a fresh REGRESSION_PENDING batch, or terminal_
        # integration_force_regression) is the supported recovery path.
        self.store.transition_batch(batch_id, REGRESSION_FAILED, event_type="REGRESSION_FAILED",
                                    reason=f"exit {result.returncode}", extra_fields={"full_test_result": test_result})
        self.store.pause_pipeline(project, reason=f"full regression failed for batch {batch_id}")
        return EngineResult(project, "REGRESSION_FAILED", batch_id=batch_id, detail=f"exit {result.returncode}")

    # -- promotion to main ----------------------------------------------------

    def promote_to_main(self, project: str, batch_id: str) -> dict[str, Any]:
        """The ONLY path any commit ever reaches `main` in this feature
        (item 8: "Không merge thẳng main từ worker... Chỉ promote main
        khi integration branch green theo policy"). Refuses any batch
        not currently MERGE_READY. Prefers a fast-forward (main was
        never touched independently); falls back to a real merge commit
        only if fast-forward isn't possible; fails closed (never force-
        pushes/resets) if neither succeeds."""
        pipeline = self.store.get_pipeline(project)
        batch = self.store.get_batch(batch_id)
        if batch is None:
            return {"error": "BATCH_NOT_FOUND"}
        if batch.status != MERGE_READY:
            return {"error": "NOT_MERGE_READY", "status": batch.status}
        repo_path = pipeline["repo_path"]
        _run_git(["checkout", pipeline["main_branch"]], repo_path)
        result = _run_git(["merge", "--ff-only", pipeline["integration_branch"]], repo_path, check=False)
        if result.returncode != 0:
            result = _run_git(["merge", "--no-ff", "-m", f"promote integration batch {batch_id}",
                              pipeline["integration_branch"]], repo_path, check=False)
            if result.returncode != 0:
                return {"action": "PROMOTE_FAILED", "detail": result.stderr.strip()[:500]}
        main_commit_sha = _run_git(["rev-parse", "HEAD"], repo_path).stdout.strip()
        self.store.promote_batch(batch_id, main_commit_sha=main_commit_sha)
        return {"action": "PROMOTED", "main_commit_sha": main_commit_sha}
