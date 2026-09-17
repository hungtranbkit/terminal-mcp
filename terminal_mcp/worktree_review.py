"""Worktree Janitor P5 -- the review queue and the report an operator reads.

Contract: docs/WORKTREE_JANITOR.md §"P5 — operator surface". Nothing here
removes anything; it decides what a human is shown and records what they
decide.

One service, two surfaces. The dashboard panel and `terminal_worktree_janitor_
report` both call this module, so the numbers on the screen and the numbers over
MCP cannot disagree -- which they would within a week if each surface summarised
the raw classifications itself.

Three things this module is deliberate about:

WHY IT IS BLOCKED, IN WORDS. A reason code is the right thing to branch on and
the wrong thing to show a person. `UNMERGED_UNPUSHED` tells an operator nothing
about what they must do; "not merged into main and not pushed anywhere -- this
work exists only here" tells them exactly why it is refused and what would
change it. The codes stay in the payload for machines; the sentences are for
the human who has to decide.

PATHS ONLY, NEVER CONTENTS. A worktree can be BLOCKED because it holds a
`.env` or a sqlite database. Naming the file is what makes the refusal
actionable; printing a line of it would put the secret on a dashboard, which is
the exact thing the janitor's own denial exists to prevent. The classifier
already only reports paths (P0), and this module adds no way to read further.

APPROVE IS NOT DELETE. Approving a REVIEW item moves it to CLEANUP_ELIGIBLE,
which is a nomination the executor will re-classify under its lock with fresh
evidence before touching anything. A human's approval cannot override a
predicate, and a worktree that has gone dirty since the review is still
refused. Abandon is the opposite and final: CLEANUP_ABANDONED stops the item
being re-proposed, so an operator who has decided "keep this" is not asked
again every sweep.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import worktree_cleanup as wc
from . import worktree_janitor as wj

_LOGGER = logging.getLogger(__name__)

APPROVE = "approve"
ABANDON = "abandon"
DECISIONS = (APPROVE, ABANDON)

# Human sentences for the machine-readable reason codes. A code with no entry
# falls back to the code itself rather than a made-up sentence -- inventing an
# explanation for a reason this table has not been taught is worse than showing
# the raw code, because an operator would act on the invention.
REASON_SENTENCES: dict[str, str] = {
    wj.MAIN_WORKTREE: "this is the repository's main checkout, not a task worktree",
    wj.PATH_NOT_ALLOWED: "the path is outside every configured allowed root",
    wj.SYMLINK_OR_MOUNT: "the path is a symlink or a mount point, so what it "
                         "points at could change underneath us",
    wj.DIRTY: "there are uncommitted changes -- removing it would discard them",
    wj.UNMERGED_UNPUSHED: "not merged into the integration branch and not pushed "
                          "anywhere -- this work exists only here",
    wj.PRESERVED_UNMERGED: "pushed to a remote but not merged yet -- the work is "
                           "safe, but nobody has integrated it",
    wj.DETACHED_HEAD: "HEAD is detached, so there is no branch whose merge state "
                      "could be checked",
    wj.VALUABLE_IGNORED_DATA: "it holds ignored files worth keeping (a database, "
                              "an .env, collected evidence)",
    wj.PROCESS_IN_USE: "a running process has its working directory inside it",
    wj.TMUX_IN_USE: "a tmux pane is sitting in it right now",
    wj.SESSION_IN_USE: "a registered session's working directory is inside it",
    wj.SERVICE_ROOT: "a service's WorkingDirectory points into it",
    wj.EVIDENCE_STALE: "the evidence was gathered too long ago to act on",
    wj.NODE_UNREACHABLE: "the node that owns it could not be asked",
    wj.GRACE_NOT_ELAPSED: "its task finished too recently -- the grace period is "
                          "still running",
    wj.ADMIN_ENTRY_STALE: "git still lists it but the directory is gone",
    wj.PERMISSION_DENIED: "part of it could not be read",
    wj.TASK_NOT_FINAL: "the task that owns it has not finished (it may still retry)",
    wj.ORPHAN_UNCONFIRMED: "no task claims it, and it has not been seen unclaimed "
                           "long enough to be sure",
    wj.GIT_UNAVAILABLE: "git could not be run against it",
    wj.NOT_A_WORKTREE: "it does not look like a git worktree",
    wj.CLEAN_AND_MERGED: "clean and already merged",
}


def explain(reason: str) -> str:
    return REASON_SENTENCES.get(reason, reason)


def explain_all(reasons: Any) -> list[str]:
    return [explain(str(r)) for r in (reasons or [])]


def build_report(candidates: list[dict[str, Any]], *, mode: str,
                 now: float | None = None) -> dict[str, Any]:
    """Summarise classifications for a human.

    `candidates` are P0 classification dicts, from a local scan or routed from a
    node -- this module never classifies anything itself, so the panel cannot
    drift from the engine's verdicts.

    `enforcing` is surfaced explicitly rather than left for the reader to infer
    from `mode`: an operator glancing at a list of AUTO_SAFE items needs to know
    at once whether anything is actually going to act on them. That is the
    observe_only badge."""
    now = time.time() if now is None else now
    by_class: dict[str, int] = {}
    bytes_by_class: dict[str, int] = {}
    blocked: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    oldest: dict[str, Any] | None = None

    for candidate in candidates or []:
        policy_class = str(candidate.get("policy_class") or wj.UNKNOWN)
        by_class[policy_class] = by_class.get(policy_class, 0) + 1
        size = int(candidate.get("size_bytes") or 0)
        bytes_by_class[policy_class] = bytes_by_class.get(policy_class, 0) + size

        row = _row(candidate, now=now)
        if policy_class == wj.BLOCKED:
            blocked.append(row)
        elif policy_class in (wj.REVIEW, wj.UNKNOWN):
            review.append(row)
        if row["age_seconds"] is not None and (
                oldest is None or oldest["age_seconds"] is None
                or row["age_seconds"] > oldest["age_seconds"]):
            oldest = row

    return {
        "mode": mode,
        # The badge. Named for what it MEANS, not for the config value, so a
        # template cannot accidentally render "observe_only" as reassurance.
        "enforcing": mode == "auto_execute",
        "observe_only": mode != "auto_execute",
        "counts": by_class,
        "reclaimable_bytes_by_class": bytes_by_class,
        # The only figure that represents real reclaimable space: BLOCKED and
        # REVIEW items are not going to be removed, so adding their bytes into a
        # headline total would promise space that is not coming.
        "reclaimable_bytes": bytes_by_class.get(wj.AUTO_SAFE, 0),
        "actionable_count": by_class.get(wj.AUTO_SAFE, 0),
        "blocked": blocked,
        "review_queue": review,
        "oldest_candidate": oldest,
        "total": len(candidates or []),
        # Stated on every report, because a worktree reclaim frees the worktree
        # filesystem and nothing else (contract §9).
        "filesystem_note": "reclaim frees only the filesystem holding these "
                           "worktrees; it does not free any other mount",
        "executor_invoked": False,
    }


def _row(candidate: dict[str, Any], *, now: float) -> dict[str, Any]:
    reasons = candidate.get("reasons") or []
    # The worktree's OWN age (its mtime), supplied by the classifier that ran on
    # the owning node -- not derived here, because a remote candidate's path does
    # not exist on this host to stat. Falls back to None rather than 0: "unknown
    # age" and "brand new" must not look the same in a panel sorted by age.
    evidence = candidate.get("evidence") or {}
    age = evidence.get("worktree_age_seconds")
    age = float(age) if isinstance(age, (int, float)) else None
    return {
        "worktree_path": candidate.get("worktree_path"),
        "node_id": candidate.get("node_id"),
        "task_id": candidate.get("task_id"),
        "branch": candidate.get("branch"),
        "policy_class": candidate.get("policy_class"),
        "size_bytes": candidate.get("size_bytes"),
        "size_partial": bool(candidate.get("size_partial")),
        # Codes for machines, sentences for people. Both, never one.
        "reasons": list(reasons),
        "explanations": explain_all(reasons),
        "age_seconds": age,
        # There is no delete-now affordance in this payload, by construction:
        # a UI cannot offer what the API never describes (AC4).
        "actions": _actions_for(candidate),
    }


def _actions_for(candidate: dict[str, Any]) -> list[str]:
    """What a human may do with this item. BLOCKED offers nothing: a human
    cannot approve past a predicate, and showing them a button that would be
    refused teaches them the refusals are negotiable."""
    policy_class = candidate.get("policy_class")
    if policy_class in (wj.REVIEW, wj.UNKNOWN):
        return [APPROVE, ABANDON]
    return []


class WorktreeReviewService:
    """Records operator decisions on the review queue.

    Holds a store (for the task metadata) and an audit sink. It does not hold an
    executor and cannot remove anything -- approving only changes a state."""

    def __init__(self, store: Any = None, *, audit: Any = None) -> None:
        self.store = store
        self.audit = audit

    def decide(self, task_id: str, decision: str, *, actor: str | None = None,
               worktree_path: str | None = None) -> dict[str, Any]:
        """approve -> CLEANUP_ELIGIBLE. abandon -> CLEANUP_ABANDONED.

        Approving is a NOMINATION, not an instruction: the executor re-classifies
        under its lock with fresh evidence and may still refuse. That is why this
        method cannot set CLEANUP_DONE and why a human's approval is not stored
        as an override of any predicate.

        Refuses a decision on a state where it makes no sense rather than
        silently coercing it -- notably CLEANUP_DONE, where the directory is
        already gone and 'approve' would be meaningless."""
        if decision not in DECISIONS:
            return {"error": "INVALID_DECISION", "decision": decision,
                    "allowed": list(DECISIONS)}
        if not task_id:
            return {"error": "TASK_ID_REQUIRED"}
        if self.store is None:
            return {"error": "STORE_UNAVAILABLE"}

        current = self._state(task_id)
        if current is None:
            return {"error": "NO_CLEANUP_RECORD", "task_id": task_id}
        if current == wc.CLEANUP_DONE:
            return {"error": "ALREADY_REMOVED", "task_id": task_id, "state": current,
                    "detail": "this worktree has already been reclaimed"}
        if current == wc.CLEANUP_ABANDONED and decision == APPROVE:
            return {"error": "ABANDONED", "task_id": task_id, "state": current,
                    "detail": "an operator already decided to keep this worktree; "
                              "clear that decision before approving"}

        target = wc.CLEANUP_ELIGIBLE if decision == APPROVE else wc.CLEANUP_ABANDONED
        patch = {"state": target, "reviewed_at": _now_iso(), "reviewed_by": actor,
                 "review_decision": decision}
        if decision == ABANDON:
            # Cleared so a later re-approval starts from a clean slate rather
            # than inheriting a stale failure count.
            patch["attempts"] = 0
        try:
            self.store.patch_worktree_cleanup(task_id, patch)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("could not record review decision", exc_info=True)
            return {"error": "RECORD_FAILED", "task_id": task_id, "detail": str(exc)[:200]}

        self._audit(task_id, decision, actor=actor, worktree_path=worktree_path,
                    from_state=current, to_state=target)
        return {"task_id": task_id, "decision": decision, "from_state": current,
                "state": target, "reviewed_by": actor,
                # Said plainly so no caller reads approve as "it is gone".
                "detail": ("approved -- the janitor will re-check it with fresh "
                           "evidence before removing anything"
                           if decision == APPROVE else
                           "abandoned -- it will not be proposed again")}

    def _state(self, task_id: str) -> str | None:
        try:
            task = self.store.get_task(task_id)
        except Exception:  # noqa: BLE001
            return None
        if task is None:
            return None
        record = (getattr(task, "metadata", None) or {}).get(wc.METADATA_KEY)
        if not isinstance(record, dict):
            return None
        return record.get("state")

    def _audit(self, task_id: str, decision: str, *, actor: str | None,
               worktree_path: str | None, from_state: str, to_state: str) -> None:
        """A human decision is the one thing in this feature with no automated
        trail of its own, so it is recorded with WHO decided -- `actor` and
        `reason` kept separate, per AuditStore's own contract."""
        if self.audit is None:
            return
        try:
            self.audit.record(
                action=f"worktree_review_{decision}", session=None, result="OK",
                reason=f"task={task_id} worktree={worktree_path or '?'} "
                       f"{from_state}->{to_state}",
                source_transport="dashboard", actor=actor,
                policy_source="worktree_janitor")
        except Exception:  # noqa: BLE001 -- never fail a decision because logging did
            _LOGGER.warning("could not audit review decision", exc_info=True)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
