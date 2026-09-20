"""Who owns a stuck task: the AI, or a human?

THE PRODUCT RULE THIS ENCODES
-----------------------------
The dashboard used to merge BLOCKED / WAITING_SESSION / PAUSED / VERIFYING into
one "Blocked/Review" column, which read as "a human has to deal with all of
this". That framing was wrong in both directions: almost everything in it was
recoverable without a person, and the few things that genuinely needed one were
buried among them. An operator learned to ignore the column, so the real
decisions waited too.

The split is by OWNERSHIP, not by status:

  AI REVIEW / AI RECOVERY (owner=ai) -- the default, and it is a wide default:
    * VERIFYING / evidence wait. The AI inspects evidence, reverifies, reclaims
      a stale verifier lease, retries or reroutes the verifier. It never
      completes a task without evidence -- waiting is correct, giving up is not.
    * WAITING_SESSION, DISPATCH_UNCERTAIN, and transient infrastructure, node,
      session or dependency failures. Retry / resume / reassign / reroute, with
      bounded backoff and the existing idempotency keys.
    * BLOCKED, by default. A refusal is a diagnosis to work through, not a
      ticket to hand over.
    * A system/coordinator/transient lane pause, reconciled once its cause is
      gone.

  NEEDS APPROVAL / USER ACTION (owner=user) -- four classes, and ONLY these:
    * credentials or secrets the AI does not have and must not invent;
    * approval for a destructive action;
    * approval for a protected production deploy/merge;
    * an irreducible human business/product decision that cannot be inferred
      safely.

  PAUSED BY USER (owner=user) -- an explicit operator pause. Not AI Review, and
    never auto-resumed. A standing instruction outlives whatever is in the lane.

RETRY EXHAUSTION IS NOT AN ESCALATION
-------------------------------------
This is the rule most likely to be violated by accident, so it is stated here
and asserted in the tests. "We tried five times" says nothing about whether a
human can help -- usually it means the retry was the wrong move and the next
step is deeper diagnosis or a different route. Exhaustion therefore changes the
AI's NEXT ACTION (stop retrying, start diagnosing/rerouting) and never the
OWNER. The live fleet had a coordinator reason reading "exceeded max coordinator
review attempts (5) -- needs a human decision instead of another automatic
retry"; under this policy that task stays AI-owned, and only its next_action
changes.

CLASSIFICATION IS ON EXPLICIT SIGNALS ONLY
------------------------------------------
Escalation is recognised from the coordinator's OWN recorded pattern (it reports
which sensitive pattern it matched) or an explicit task-metadata flag -- never
from prose sentiment, and never from "this looks hard". The three pattern-backed
classes map exactly onto coordinator.SENSITIVE_PROMPT_PATTERNS, so a new pattern
there cannot silently become an escalation here: it lands in the AI's lap, which
is the safe direction for a policy whose failure mode is bothering a human.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .queue_store import (BLOCKED, DISPATCH_UNCERTAIN, FAILED, PAUSED, PAUSE_ORIGIN_COORDINATOR,
                          PAUSE_ORIGIN_PROJECT, PAUSE_ORIGIN_USER, VERIFYING, WAITING_SESSION,
                          pause_origin)

# --------------------------------------------------------------------------
# Vocabulary. These strings reach the API, the dashboard and the audit log, so
# they are stable identifiers.
# --------------------------------------------------------------------------
OWNER_AI = "ai"
OWNER_USER = "user"

BUCKET_AI_REVIEW = "ai_review"
BUCKET_NEEDS_APPROVAL = "needs_approval"
BUCKET_PAUSED_BY_USER = "paused_by_user"
BUCKETS = (BUCKET_AI_REVIEW, BUCKET_NEEDS_APPROVAL, BUCKET_PAUSED_BY_USER)

#: The ONLY four reasons work may leave AI Review.
APPROVAL_CREDENTIALS = "credentials_or_secrets"
APPROVAL_DESTRUCTIVE = "destructive_action"
APPROVAL_PROTECTED_DEPLOY = "protected_deploy_or_merge"
APPROVAL_BUSINESS_DECISION = "business_decision"
APPROVAL_CLASSES = (APPROVAL_CREDENTIALS, APPROVAL_DESTRUCTIVE,
                    APPROVAL_PROTECTED_DEPLOY, APPROVAL_BUSINESS_DECISION)

#: Audit actors (item 7). Two, because they are different jobs: the reviewer
#: decides ownership and next action, the reconciler takes the action.
ACTOR_AI_REVIEWER = "ai_reviewer"
ACTOR_AI_RECONCILER = "ai_reconciler"

# --------------------------------------------------------------------------
# Escalation detection.
#
# Keyed on the coordinator's own pattern text, which it records verbatim in its
# reason: f"task prompt matches a sensitive/destructive pattern ({pattern!r})".
# Mapping the pattern rather than re-scanning the prompt means this agrees with
# the gate that actually refused, instead of forming a second opinion about the
# same text.
# --------------------------------------------------------------------------
# NEEDLE -> class, matched as a SUBSTRING of the regex the coordinator
# reported. Ordered: the first needle found wins, so a needle that also occurs
# inside an unrelated pattern must come after the pattern it belongs to.
#
# Both spellings are listed on purpose. The coordinator's patterns became
# action-shaped in 2026-09 (a bare "token" in a prompt is vocabulary, not a
# credential disclosure -- see coordinator.SENSITIVE_PROMPT_PATTERNS), but the
# OLD pattern text is durable: it is quoted verbatim into coordinator_reason on
# every task refused before that change, and those rows still have to classify.
# Dropping the old needles would silently reclassify historic refusals as
# AI-owned.
_PATTERN_APPROVAL_CLASS: tuple[tuple[str, str], ...] = (
    # -- credentials / secrets the AI must not invent ----------------------
    ("enter (your |the )?password", APPROVAL_CREDENTIALS),       # pre-2026-09
    ("enter (?:your |the )?password", APPROVAL_CREDENTIALS),     # action-shaped
    ("exfiltrat", APPROVAL_CREDENTIALS),       # the disclose-a-credential verb set
    ("pastebin", APPROVAL_CREDENTIALS),        # credential -> somewhere public
    ("api[_ -]?key", APPROVAL_CREDENTIALS),
    ("credential", APPROVAL_CREDENTIALS),
    (r"\bsecret\b", APPROVAL_CREDENTIALS),
    (r"\btoken\b", APPROVAL_CREDENTIALS),
    (".env", APPROVAL_CREDENTIALS),            # the credential FILE, not a noun
    # -- destructive actions ------------------------------------------------
    ("force[ -]push", APPROVAL_DESTRUCTIVE),
    (r"\brm -rf\b", APPROVAL_DESTRUCTIVE),
    ("[a-z]*r[a-z]*f", APPROVAL_DESTRUCTIVE),  # rm -rf / -fr, either spelling
    ("drop (table|database)", APPROVAL_DESTRUCTIVE),
    ("(?:table|database)", APPROVAL_DESTRUCTIVE),
    ("truncate", APPROVAL_DESTRUCTIVE),
    ("mkfs", APPROVAL_DESTRUCTIVE),
    ("if=", APPROVAL_DESTRUCTIVE),             # dd if=...
    ("777", APPROVAL_DESTRUCTIVE),             # chmod 777
    (r"\bsudo\b", APPROVAL_DESTRUCTIVE),
    ("sudo", APPROVAL_DESTRUCTIVE),
    ("reset --hard", APPROVAL_DESTRUCTIVE),
    ("--hard", APPROVAL_DESTRUCTIVE),
    (r"\bgit clean\b", APPROVAL_DESTRUCTIVE),
    ("clean", APPROVAL_DESTRUCTIVE),
    # -- protected production deploy / merge --------------------------------
    ("merge (to |into )?main", APPROVAL_PROTECTED_DEPLOY),
    ("push (to |origin )?main", APPROVAL_PROTECTED_DEPLOY),
    ("(?:main|master)", APPROVAL_PROTECTED_DEPLOY),
    ("(?:prod|production)", APPROVAL_PROTECTED_DEPLOY),
    ("deploy", APPROVAL_PROTECTED_DEPLOY),
)

#: An explicit, deliberate marker a task (or a coordinator decision) can carry
#: to say "a human must choose". The ONLY route to APPROVAL_BUSINESS_DECISION,
#: because a business decision has no syntactic signature -- inferring one from
#: prose is exactly the guess this module refuses to make.
BUSINESS_DECISION_KEYS = ("needs_human_decision", "needs_business_decision")

_SENSITIVE_REASON = re.compile(
    r"sensitive/destructive pattern \((?P<quote>['\"])(?P<pattern>.+?)(?P=quote)\)")

# --------------------------------------------------------------------------
# Backoff and loop prevention (item 7).
# --------------------------------------------------------------------------
AI_BACKOFF_BASE_SECONDS = 30.0
AI_BACKOFF_CAP_SECONDS = 900.0
#: After this many automatic attempts the AI STOPS retrying and switches to
#: diagnosis/rerouting. It does NOT hand the task to a human -- see this
#: module's docstring on retry exhaustion.
AI_MAX_AUTOMATIC_ATTEMPTS = 6


def backoff_seconds(attempts: int) -> float:
    """Bounded exponential backoff. Deterministic (no jitter): these sweeps are
    idempotent and single-writer, so jitter would only make the next_check_at a
    caller displays unpredictable."""
    if attempts <= 0:
        return AI_BACKOFF_BASE_SECONDS
    return min(AI_BACKOFF_CAP_SECONDS, AI_BACKOFF_BASE_SECONDS * (2 ** min(attempts, 16)))


def retries_exhausted(attempts: int) -> bool:
    return attempts >= AI_MAX_AUTOMATIC_ATTEMPTS


# --------------------------------------------------------------------------
# Next actions. Deliberately a small, stable vocabulary a dispatcher can
# branch on, not free prose.
# --------------------------------------------------------------------------
ACTION_REVERIFY = "reverify_evidence"
ACTION_RECLAIM_VERIFIER = "reclaim_stale_verifier_lease"
ACTION_RETRY = "retry_with_backoff"
ACTION_RESUME_SESSION = "await_or_reassign_session"
ACTION_DIAGNOSE = "diagnose_and_reroute"
ACTION_RECONCILE_PAUSE = "reconcile_stale_pause"
ACTION_AWAIT_APPROVAL = "await_human_approval"
ACTION_AWAIT_USER_RESUME = "await_explicit_user_resume"
ACTION_NONE = "none"


@dataclass(frozen=True)
class Attention:
    """One stuck item, and who owns it.

    Shape is stable across buckets so a UI reads the same keys for every row;
    fields that do not apply are None rather than absent.
    """
    task_id: str | None
    session: str | None
    status: str
    owner: str
    bucket: str
    diagnosis: str
    next_action: str
    attempts: int = 0
    age_seconds: float | None = None
    last_check: str | None = None
    next_check_at: str | None = None
    approval_class: str | None = None
    retries_exhausted: bool = False
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id, "session": self.session, "status": self.status,
            "owner": self.owner, "bucket": self.bucket, "diagnosis": self.diagnosis,
            "next_action": self.next_action, "attempts": self.attempts,
            "age_seconds": self.age_seconds, "last_check": self.last_check,
            "next_check_at": self.next_check_at, "approval_class": self.approval_class,
            "retries_exhausted": self.retries_exhausted, "evidence": dict(self.evidence),
        }


def _loads(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def approval_class_for_reason(reason: str | None) -> str | None:
    """The escalation class a coordinator reason encodes, or None.

    Matches on the PATTERN the coordinator reported, not on the prompt. A
    refusal whose pattern is not in the table above is AI-owned -- a new
    sensitive pattern therefore cannot silently start paging a human."""
    if not reason:
        return None
    match = _SENSITIVE_REASON.search(reason)
    if not match:
        return None
    pattern = match.group("pattern")
    # The coordinator formats with %r, so a raw pattern arrives with its
    # escapes doubled; compare loosely rather than trying to unescape.
    normalized = pattern.replace("\\\\", "\\")
    for needle, approval in _PATTERN_APPROVAL_CLASS:
        if needle in normalized or needle in pattern:
            return approval
    return None


def _business_decision_requested(task: dict[str, Any]) -> bool:
    """Only an explicit, deliberate marker counts."""
    for source in (_loads(task.get("metadata")), _loads(task.get("coordinator_decision"))):
        if isinstance(source, dict):
            for key in BUSINESS_DECISION_KEYS:
                if source.get(key):
                    return True
            blockers = source.get("blockers")
            if isinstance(blockers, list) and any(
                    isinstance(b, str) and b.strip().lower() in BUSINESS_DECISION_KEYS for b in blockers):
                return True
    return False


def approval_class_for_task(task: dict[str, Any]) -> str | None:
    """The escalation class this task genuinely needs a human for, or None."""
    if _business_decision_requested(task):
        return APPROVAL_BUSINESS_DECISION
    return approval_class_for_reason(task.get("coordinator_reason")
                                     or (_loads(task.get("coordinator_decision")) or {}).get("reason"))


def _age(now_epoch: float | None, timestamp: str | None,
         to_epoch: Any) -> float | None:
    stamp = to_epoch(timestamp)
    if now_epoch is None or stamp is None:
        return None
    return max(0.0, now_epoch - stamp)


def _iso_after(timestamp: str | None, seconds: float, to_epoch: Any, from_epoch: Any) -> str | None:
    stamp = to_epoch(timestamp)
    if stamp is None:
        return None
    return from_epoch(stamp + seconds)


def iso_from_epoch(epoch: float) -> str:
    """The inverse of queue_store._epoch_or_none, in the one format this
    project stamps everywhere (UTC, second precision, trailing Z)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def classify_task(task: dict[str, Any], *, lane: dict[str, Any] | None = None,
                  now_epoch: float | None = None, to_epoch: Any = None,
                  from_epoch: Any = None) -> Attention | None:
    """Ownership for ONE task, or None if it needs no attention at all.

    `to_epoch`/`from_epoch` are injectable so a test can pin the clock; they
    default to queue_store's own ISO format rather than a second one.
    """
    from .queue_store import _epoch_or_none  # local: avoid an import cycle at module load

    to_epoch = to_epoch or _epoch_or_none
    from_epoch = from_epoch or iso_from_epoch
    if now_epoch is None:
        now_epoch = time.time()
    status = str(task.get("status") or "")
    if status not in (VERIFYING, WAITING_SESSION, DISPATCH_UNCERTAIN, BLOCKED, FAILED, PAUSED):
        return None

    task_id = task.get("id")
    session = task.get("session")
    attempts = int(task.get("attempt_count") or 0)
    coordinator_attempts = int(task.get("coordinator_attempts") or 0)
    last_check = task.get("coordinator_checked_at") or task.get("updated_at")
    age = _age(now_epoch, task.get("updated_at"), to_epoch)
    exhausted = retries_exhausted(max(attempts, coordinator_attempts))
    wait = backoff_seconds(max(attempts, coordinator_attempts))
    next_check = _iso_after(last_check, wait, to_epoch, from_epoch)

    def item(owner, bucket, diagnosis, action, *, approval=None, evidence=None):
        return Attention(
            task_id=task_id, session=session, status=status, owner=owner, bucket=bucket,
            diagnosis=diagnosis, next_action=action, attempts=max(attempts, coordinator_attempts),
            age_seconds=age, last_check=last_check,
            next_check_at=None if approval or bucket != BUCKET_AI_REVIEW else next_check,
            approval_class=approval, retries_exhausted=exhausted,
            evidence=evidence or {})

    # A task PAUSED because its LANE was paused belongs to whoever owns that
    # pause -- an operator pause is not the task's own problem.
    if status == PAUSED:
        origin = pause_origin((lane or {}).get("paused_origin"), (lane or {}).get("paused_reason"))
        if origin == PAUSE_ORIGIN_COORDINATOR:
            approval = approval_class_for_task(task)
            if approval:
                return item(OWNER_USER, BUCKET_NEEDS_APPROVAL,
                            f"coordinator refused pending human approval ({approval})",
                            ACTION_AWAIT_APPROVAL, approval=approval)
            return item(OWNER_AI, BUCKET_AI_REVIEW,
                        "lane paused by a coordinator guard; re-evaluate the refusal",
                        ACTION_DIAGNOSE if exhausted else ACTION_RETRY)
        if origin in (PAUSE_ORIGIN_USER, PAUSE_ORIGIN_PROJECT) or origin is None:
            return item(OWNER_USER, BUCKET_PAUSED_BY_USER,
                        "lane paused explicitly; only an operator resume lifts it",
                        ACTION_AWAIT_USER_RESUME)

    if status == VERIFYING:
        # Item 1: evidence wait is AI-owned, and completing without evidence is
        # never one of the options.
        return item(OWNER_AI, BUCKET_AI_REVIEW,
                    "awaiting verification evidence; no completion without evidence",
                    ACTION_DIAGNOSE if exhausted else ACTION_REVERIFY,
                    evidence={"verification_evidence": bool(task.get("verification_evidence"))})

    if status in (WAITING_SESSION, DISPATCH_UNCERTAIN):
        # Item 2: transient infrastructure. Retry/resume/reassign with backoff.
        return item(OWNER_AI, BUCKET_AI_REVIEW,
                    ("session or node unreachable; awaiting recovery or reassignment"
                     if status == WAITING_SESSION else
                     "delivery outcome unproven; re-checking for real activity"),
                    ACTION_DIAGNOSE if exhausted else ACTION_RESUME_SESSION)

    # BLOCKED / FAILED: AI-owned by default (item 3). Escalate only on one of
    # the four true classes -- never on retry exhaustion.
    approval = approval_class_for_task(task)
    if approval:
        return item(OWNER_USER, BUCKET_NEEDS_APPROVAL,
                    f"requires human authorization ({approval})",
                    ACTION_AWAIT_APPROVAL, approval=approval)
    diagnosis = task.get("coordinator_reason") or task.get("last_error") or "blocked without a recorded reason"
    return item(OWNER_AI, BUCKET_AI_REVIEW, str(diagnosis)[:400],
                ACTION_DIAGNOSE if exhausted else ACTION_RETRY)


def classify_lane_pause(lane: dict[str, Any]) -> Attention | None:
    """Ownership for a paused lane that has no PAUSED task to speak for it.

    A lane can be paused with nothing in it (an operator pause of an idle
    lane, or a coordinator guard whose task was already resolved). Without
    this, such a lane would be invisible in every bucket -- which is exactly
    how one sat paused for three and a half hours.
    """
    if not lane.get("paused"):
        return None
    origin = pause_origin(lane.get("paused_origin"), lane.get("paused_reason"))
    session = lane.get("session")
    if origin == PAUSE_ORIGIN_COORDINATOR:
        return Attention(
            task_id=None, session=session, status="LANE_PAUSED", owner=OWNER_AI,
            bucket=BUCKET_AI_REVIEW,
            diagnosis=f"coordinator lane pause: {lane.get('paused_reason') or 'no reason recorded'}"[:400],
            next_action=ACTION_RECONCILE_PAUSE)
    return Attention(
        task_id=None, session=session, status="LANE_PAUSED", owner=OWNER_USER,
        bucket=BUCKET_PAUSED_BY_USER,
        diagnosis=f"explicit pause: {lane.get('paused_reason') or 'no reason recorded'}"[:400],
        next_action=ACTION_AWAIT_USER_RESUME)


def summarize(items: list[Attention]) -> dict[str, Any]:
    """Counts per bucket plus the metrics item 7 asks for. Always reports every
    bucket and every approval class, including zeros -- a missing key reads as
    "not measured", which is a different claim from "none"."""
    counts = {bucket: 0 for bucket in BUCKETS}
    approvals = {approval: 0 for approval in APPROVAL_CLASSES}
    exhausted = 0
    oldest: float | None = None
    for entry in items:
        counts[entry.bucket] = counts.get(entry.bucket, 0) + 1
        if entry.approval_class:
            approvals[entry.approval_class] = approvals.get(entry.approval_class, 0) + 1
        if entry.retries_exhausted:
            exhausted += 1
        if entry.age_seconds is not None:
            oldest = entry.age_seconds if oldest is None else max(oldest, entry.age_seconds)
    return {
        "counts": counts,
        "ai_owned": counts[BUCKET_AI_REVIEW],
        "human_owned": counts[BUCKET_NEEDS_APPROVAL] + counts[BUCKET_PAUSED_BY_USER],
        "approval_classes": approvals,
        "retries_exhausted": exhausted,
        "oldest_age_seconds": oldest,
        "policy": {
            "max_automatic_attempts": AI_MAX_AUTOMATIC_ATTEMPTS,
            "backoff_base_seconds": AI_BACKOFF_BASE_SECONDS,
            "backoff_cap_seconds": AI_BACKOFF_CAP_SECONDS,
            "escalation_classes": list(APPROVAL_CLASSES),
            "retry_exhaustion_escalates": False,
        },
    }
