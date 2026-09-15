"""Worktree Janitor P2 -- the executor. THE FIRST CODE THAT MAY DELETE.

Contract: docs/WORKTREE_JANITOR.md §6 (concurrency/locks/idempotency), §8 (audit
and reclaimed bytes), and invariants I1, I3, I6, I7, I8.

Everything before this shipped read-only. This module removes real directories,
so it is written to the opposite standard: not "does it work" but "what does it
do on the worst day". The order of operations below is the answer, and each step
exists because skipping it loses work.

  1. Refuse unless the operator opted in. mode must be auto_execute AND
     dry_run must be explicitly False. Two separate gates, because one of them
     is a config an operator set once and forgot, and the other is a decision
     at the call site.
  2. Take the lock BEFORE looking. If classification happened outside the lock,
     another janitor could remove the same worktree between our verdict and our
     removal, or a human could start working in it.
  3. RE-CLASSIFY under the lock with fresh evidence. The scan that nominated
     this candidate may be minutes old. A worktree that was clean then may be
     someone's working directory now -- that is failure mode F11, and the only
     defence is to look again at the last possible moment.
  4. Audit the ATTEMPT before touching anything. If the process dies mid-removal
     the log still says what was about to happen. An audit row written only on
     success is not an audit trail, it is a success log.
  5. Remove with force=False, always. `git worktree remove` refuses a dirty
     worktree by itself; that refusal is a second independent guard behind our
     own cleanliness check, and passing --force would discard both.
  6. Audit the RESULT with observed reclaimed bytes.
  7. Record CLEANUP_DONE on the task, or CLEANUP_BLOCKED/attempts on failure.
  8. Prune only after a real removal (or a positively identified stale admin
     entry). Never speculatively -- another worktree may be mid-creation (F7).

What this module deliberately does NOT do: decide. `worktree_janitor.classify`
owns that, and this module refuses to act on anything it did not classify
AUTO_SAFE itself, under the lock, moments ago.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import git_worktree, worktree_cleanup, worktree_janitor as wj

# -- outcomes ------------------------------------------------------------
REMOVED = "REMOVED"
"""A real directory is gone and the task is CLEANUP_DONE."""
WOULD_REMOVE = "WOULD_REMOVE"
"""dry_run, or a mode short of auto_execute. Nothing was touched."""
SKIPPED = "SKIPPED"
"""Not actionable: the candidate is not AUTO_SAFE, or the gate is closed."""
ABORTED = "ABORTED"
"""It WAS AUTO_SAFE at scan time and is not any more. Nothing was touched."""
FAILED = "FAILED"
"""The removal was attempted and did not complete. Nothing partial is left
behind by us -- git either removes a worktree or refuses."""
ALREADY_GONE = "ALREADY_GONE"
"""The directory was already absent. Converges to CLEANUP_DONE (F6)."""
OUTCOMES = (REMOVED, WOULD_REMOVE, SKIPPED, ABORTED, FAILED, ALREADY_GONE)

# -- reason codes --------------------------------------------------------
NOT_ACTIONABLE = "NOT_ACTIONABLE"
MODE_NOT_AUTO_EXECUTE = "MODE_NOT_AUTO_EXECUTE"
DRY_RUN = "DRY_RUN"
EVIDENCE_CHANGED = "EVIDENCE_CHANGED"
LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
REMOVE_REFUSED = "REMOVE_REFUSED"

AUDIT_ATTEMPT = "worktree_cleanup_attempt"
AUDIT_RESULT = "worktree_cleanup_result"

LOCK_KEY_PREFIX = "worktree_janitor"
DEFAULT_LOCK_PROJECT = "worktree-janitor"


def lock_key(node_id: str | None, worktree_path: str) -> str:
    """`worktree_janitor:<node_id>:<worktree_path>` -- the contract's spelling,
    verbatim. `node_id` is normalised to "local" rather than left empty so two
    nodes can never collide on one key by both omitting it."""
    return f"{LOCK_KEY_PREFIX}:{node_id or 'local'}:{worktree_path}"


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str
    worktree_path: str
    reason: str | None = None
    detail: str | None = None
    reclaimed_bytes: int | None = None
    reclaimed_partial: bool = False
    task_id: str | None = None
    node_id: str | None = None
    policy_class: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    pruned: bool = False

    @property
    def deleted_something(self) -> bool:
        return self.outcome == REMOVED

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "worktree_path": self.worktree_path,
                "reason": self.reason, "detail": self.detail,
                "reclaimed_bytes": self.reclaimed_bytes,
                "reclaimed_partial": self.reclaimed_partial,
                "task_id": self.task_id, "node_id": self.node_id,
                "policy_class": self.policy_class, "evidence": self.evidence,
                "pruned": self.pruned, "deleted_something": self.deleted_something}


class WorktreeExecutor:
    """Wires the classifier, the lock, the audit log and git_worktree together.

    `audit`, `locks` and `store` are all optional so the executor can be
    exercised in isolation, but in production all three are the SAME instances
    the rest of the server uses -- never a second audit database or a second
    lock table."""

    def __init__(self, policy: wj.JanitorPolicy, *, audit: Any = None, locks: Any = None,
                 store: Any = None, node_id: str | None = None,
                 owner_id: str | None = None, max_attempts: int = 3,
                 lock_project: str = DEFAULT_LOCK_PROJECT) -> None:
        self.policy = policy
        self.audit = audit
        self.locks = locks
        self.store = store
        self.node_id = node_id or "local"
        self.owner_id = owner_id or f"worktree-janitor-{self.node_id}"
        self.max_attempts = max_attempts
        self.lock_project = lock_project

    # -- gates ------------------------------------------------------------

    def _gate(self, dry_run: bool) -> tuple[bool, str | None]:
        """I8 + AC8. Two independent gates, both must open."""
        if self.policy.mode != "auto_execute":
            return False, MODE_NOT_AUTO_EXECUTE
        if dry_run:
            return False, DRY_RUN
        return True, None

    # -- the one public entry point ---------------------------------------

    def execute(self, candidate: dict[str, Any], *, task: dict[str, Any] | None = None,
                repo_path: str | None = None, dry_run: bool = True,
                classification: wj.Classification | None = None,
                **probe_overrides: Any) -> ExecutionResult:
        """Attempt to reclaim ONE worktree.

        `dry_run` defaults to True: a caller that forgets the argument gets a
        report, never a deletion. `classification` may be passed in (a scan's
        verdict) but is NEVER trusted for the removal decision -- it is only
        used to decide whether this candidate is worth locking at all, and a
        fresh classification under the lock is what actually authorises."""
        path = str(candidate.get("worktree_path") or "")
        node_id = str(candidate.get("node_id") or self.node_id)
        if not path:
            return ExecutionResult(SKIPPED, "", reason=NOT_ACTIONABLE,
                                   detail="candidate carries no worktree_path")

        # Cheap pre-filter: don't take a lock for something already known to be
        # non-actionable. The authoritative check is still under the lock.
        if classification is not None and not classification.actionable:
            return self._skip(path, node_id, task, classification,
                              reason=NOT_ACTIONABLE,
                              detail=f"scan classified this {classification.policy_class}")

        allowed, gate_reason = self._gate(dry_run)

        acquired = False
        if self.locks is not None:
            key = lock_key(node_id, path)
            try:
                holder = self.locks.acquire(self.lock_project, key, self.owner_id,
                                            reason="worktree janitor cleanup")
            except Exception as exc:  # noqa: BLE001 -- a lock store failure is never a licence to proceed
                return ExecutionResult(SKIPPED, path, reason=LOCK_UNAVAILABLE,
                                       detail=f"lock store error: {exc}"[:200],
                                       task_id=(task or {}).get("id"), node_id=node_id)
            if not _lock_granted(holder, self.owner_id):
                return ExecutionResult(SKIPPED, path, reason=LOCK_UNAVAILABLE,
                                       detail="another janitor holds this worktree",
                                       task_id=(task or {}).get("id"), node_id=node_id)
            acquired = True
        try:
            return self._execute_locked(path, node_id, task, repo_path, allowed, gate_reason,
                                        probe_overrides)
        finally:
            if acquired:
                try:
                    self.locks.release(self.lock_project, lock_key(node_id, path), self.owner_id)
                except Exception:  # noqa: BLE001 -- a stuck lock expires by TTL; never mask the real result
                    pass

    def _execute_locked(self, path: str, node_id: str, task: dict[str, Any] | None,
                        repo_path: str | None, allowed: bool, gate_reason: str | None,
                        probe_overrides: dict[str, Any]) -> ExecutionResult:
        # F6: the directory may already be gone -- a previous run that died
        # between the removal and the metadata write. That is not an error, it
        # is the state converging.
        if not Path(path).exists():
            return self._converge_already_gone(path, node_id, task, repo_path)

        # Step 3. FRESH classification under the lock. This is what authorises.
        fresh = wj.classify({"worktree_path": path, "node_id": node_id}, self.policy,
                            task=task, repo_roots=(repo_path,) if repo_path else (),
                            evidence_collected_at=time.time(), **probe_overrides)
        if not fresh.actionable:
            # It was nominated and is no longer safe. ABORTED, not SKIPPED: the
            # distinction tells an operator that something changed under them
            # (F11), which is worth noticing.
            return self._abort(path, node_id, task, fresh)

        if not allowed:
            return self._would_remove(path, node_id, task, fresh, gate_reason)

        if self._attempts(task) >= self.max_attempts:
            self._mark_review(task, ATTEMPTS_EXHAUSTED)
            return ExecutionResult(SKIPPED, path, reason=ATTEMPTS_EXHAUSTED,
                                   detail=f"{self.max_attempts} attempts already failed; "
                                          f"left for review",
                                   task_id=(task or {}).get("id"), node_id=node_id,
                                   policy_class=fresh.policy_class)

        # Step 4. Audit the ATTEMPT, before anything is touched.
        predicted, predicted_partial = wj.reclaimable_bytes(path)
        started = time.monotonic()
        self._audit_row(AUDIT_ATTEMPT, "ATTEMPT", path, node_id, task, fresh,
                        reclaimed_bytes=predicted, partial=predicted_partial)

        # Step 5. force=False. ALWAYS. See I1.
        owning_repo = repo_path or (task or {}).get("repo_path") or _repo_of(fresh)
        if not owning_repo:
            detail = "cannot determine the owning repo for `git worktree remove`"
            self._audit_row(AUDIT_RESULT, "FAILED", path, node_id, task, fresh,
                            reason=REMOVE_REFUSED, detail=detail,
                            latency_ms=(time.monotonic() - started) * 1000)
            self._bump_attempt(task, detail)
            return ExecutionResult(FAILED, path, reason=REMOVE_REFUSED, detail=detail,
                                   task_id=(task or {}).get("id"), node_id=node_id,
                                   policy_class=fresh.policy_class)

        result = git_worktree.remove_worktree(owning_repo, path, force=False)
        latency_ms = (time.monotonic() - started) * 1000

        if "error" in result:
            # git refused. Its own dirty-check is a second guard behind ours,
            # and a refusal here means one of them saw something we must not
            # override. No retry with force, ever.
            self._audit_row(AUDIT_RESULT, "DENIED", path, node_id, task, fresh,
                            reason=result["error"], detail=str(result.get("detail"))[:300],
                            latency_ms=latency_ms)
            self._bump_attempt(task, f"{result['error']}: {str(result.get('detail'))[:200]}")
            return ExecutionResult(FAILED, path, reason=result["error"],
                                   detail=str(result.get("detail"))[:300],
                                   task_id=(task or {}).get("id"), node_id=node_id,
                                   policy_class=fresh.policy_class)

        # Step 6. Observed reclaim. The directory is gone, so the predicted
        # figure IS the observed one -- reported as such rather than re-walking
        # a path that no longer exists and calling the resulting 0 a measurement.
        observed, observed_partial = predicted, predicted_partial
        # Step 8. Prune only now, after a real removal.
        pruned = self._prune(owning_repo)
        self._audit_row(AUDIT_RESULT, "OK", path, node_id, task, fresh,
                        reclaimed_bytes=observed, partial=observed_partial,
                        latency_ms=latency_ms)
        # Step 7.
        self._mark_done(task, observed)
        return ExecutionResult(REMOVED, path, reclaimed_bytes=observed,
                               reclaimed_partial=observed_partial,
                               task_id=(task or {}).get("id"), node_id=node_id,
                               policy_class=fresh.policy_class,
                               evidence=fresh.evidence, pruned=pruned)

    # -- outcome helpers --------------------------------------------------

    def _skip(self, path, node_id, task, classification, *, reason, detail):
        return ExecutionResult(SKIPPED, path, reason=reason, detail=detail,
                               task_id=(task or {}).get("id"), node_id=node_id,
                               policy_class=getattr(classification, "policy_class", None))

    def _abort(self, path, node_id, task, fresh):
        detail = (f"re-classified {fresh.policy_class} under the lock: "
                  f"{', '.join(fresh.reasons[:4])}")
        self._audit_row(AUDIT_RESULT, "DENIED", path, node_id, task, fresh,
                        reason=EVIDENCE_CHANGED, detail=detail)
        return ExecutionResult(ABORTED, path, reason=EVIDENCE_CHANGED, detail=detail,
                               task_id=(task or {}).get("id"), node_id=node_id,
                               policy_class=fresh.policy_class, evidence=fresh.evidence)

    def _would_remove(self, path, node_id, task, fresh, gate_reason):
        predicted, partial = wj.reclaimable_bytes(path)
        return ExecutionResult(WOULD_REMOVE, path, reason=gate_reason,
                               detail=("dry run -- nothing was touched"
                                       if gate_reason == DRY_RUN else
                                       f"mode is {self.policy.mode!r}, not auto_execute"),
                               reclaimed_bytes=predicted, reclaimed_partial=partial,
                               task_id=(task or {}).get("id"), node_id=node_id,
                               policy_class=fresh.policy_class, evidence=fresh.evidence)

    def _converge_already_gone(self, path, node_id, task, repo_path):
        """F6. Idempotent: a directory that is already absent means a previous
        attempt succeeded and died before recording it."""
        pruned = self._prune(repo_path) if repo_path else False
        self._audit_row(AUDIT_RESULT, "OK", path, node_id, task, None,
                        reason=ALREADY_GONE,
                        detail="directory already absent; converged to CLEANUP_DONE")
        self._mark_done(task, 0)
        return ExecutionResult(ALREADY_GONE, path, reason=ALREADY_GONE,
                               reclaimed_bytes=0, task_id=(task or {}).get("id"),
                               node_id=node_id, pruned=pruned)

    def _prune(self, repo_path: str | None) -> bool:
        """`git worktree prune`, and ONLY from here -- after a real removal or a
        positively identified stale admin entry. Never speculative (F7)."""
        if not repo_path:
            return False
        try:
            result = git_worktree._run_git(["worktree", "prune"], repo_path)
        except Exception:  # noqa: BLE001 -- a failed prune leaves a stale entry, not lost work
            return False
        return result.returncode == 0

    # -- audit ------------------------------------------------------------

    def _audit_row(self, action: str, result: str, path: str, node_id: str,
                   task: dict[str, Any] | None, classification: wj.Classification | None, *,
                   reason: str | None = None, detail: str | None = None,
                   reclaimed_bytes: int | None = None, partial: bool = False,
                   latency_ms: float | None = None) -> None:
        """I6. Paths, sizes, policy classes and reason codes ONLY.

        Never file contents, never a diff, never the contents of a valuable
        ignored file -- and `text=` is deliberately left unset so AuditStore
        does not fingerprint or preview anything. This log is the one an
        operator greps freely; an audit row that carried the secret would
        defeat the denial it is recording."""
        if self.audit is None:
            return
        parts = [f"worktree={path}", f"outcome={result}"]
        if classification is not None:
            parts.append(f"class={classification.policy_class}")
            if classification.reasons:
                parts.append("reasons=" + ",".join(classification.reasons[:6]))
            if classification.branch:
                parts.append(f"branch={classification.branch}")
            if classification.head:
                parts.append(f"head={classification.head[:12]}")
        if reclaimed_bytes is not None:
            parts.append(f"reclaimed_bytes={reclaimed_bytes}{'+' if partial else ''}")
        if reason:
            parts.append(f"reason={reason}")
        if detail:
            parts.append(f"detail={detail[:200]}")
        try:
            self.audit.record(action=action, session=None, result=result,
                              reason=" ".join(parts)[:1000], source_transport="janitor",
                              actor=self.owner_id, node_id=node_id,
                              latency_ms=latency_ms, policy_source="worktree_janitor",
                              policy_version=self.policy.mode)
        except Exception:  # noqa: BLE001 -- never let logging change the outcome
            pass

    # -- task metadata ----------------------------------------------------

    def _attempts(self, task: dict[str, Any] | None) -> int:
        record = ((task or {}).get("metadata") or {}).get(worktree_cleanup.METADATA_KEY) or {}
        try:
            return int(record.get("attempts") or 0)
        except (TypeError, ValueError):
            return 0

    def _mark_done(self, task: dict[str, Any] | None, reclaimed: int) -> None:
        self._patch(task, {"state": worktree_cleanup.CLEANUP_DONE,
                           "removed_at": _now_iso(), "reclaimed_bytes": reclaimed,
                           "last_error": None})

    def _mark_review(self, task: dict[str, Any] | None, reason: str) -> None:
        self._patch(task, {"state": worktree_cleanup.CLEANUP_REVIEW, "last_error": reason})

    def _bump_attempt(self, task: dict[str, Any] | None, error: str) -> None:
        attempts = self._attempts(task) + 1
        patch = {"attempts": attempts, "last_error": error[:300]}
        if attempts >= self.max_attempts:
            # Bounded: never an infinite retry loop.
            patch["state"] = worktree_cleanup.CLEANUP_REVIEW
        self._patch(task, patch)

    def _patch(self, task: dict[str, Any] | None, patch: dict[str, Any]) -> None:
        if self.store is None or not task or not task.get("id"):
            return
        try:
            self.store.patch_worktree_cleanup(task["id"], patch)
        except Exception:  # noqa: BLE001 -- the directory state is the truth; a
            # failed metadata write is reconciled by the next sweep (F6), and
            # must not turn a completed removal into a reported failure.
            pass


def _lock_granted(response: Any, owner_id: str) -> bool:
    """Did we actually get the lock?

    ResourceLockStore.acquire returns {"acquired": bool, "lock"|"holder": row}.
    This reads `acquired` and nothing else, and an unrecognised shape is
    REFUSED rather than assumed granted.

    The first version of this helper looked for a top-level "owner_id", which
    that payload does not have -- so it read None, concluded "nobody owns it",
    and returned True for a lock another janitor was holding. Fail-OPEN, in the
    one place that exists to prevent two janitors deleting the same directory.
    The concurrency test caught it. A permissive default has no business in this
    module: an unreadable answer means we did not get the lock."""
    if isinstance(response, bool):
        return response  # a simple boolean-style lock (used by tests/doubles)
    if isinstance(response, dict):
        if "acquired" in response:
            return bool(response["acquired"])
        return False  # a dict we do not understand is not a grant
    return False


def _repo_of(classification: wj.Classification) -> str | None:
    return (classification.evidence or {}).get("repo_path")


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
