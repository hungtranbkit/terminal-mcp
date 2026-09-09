"""P0.5 Verify Queue -- verification as CLAIMABLE WORK routed by
capability, instead of a state only the implementing session can leave.

WHY THIS EXISTS
---------------
Today a task reaches VERIFYING and only one actor can ever move it on:
queue_engine.py's own tick, reading the SAME session the work ran in,
looking for a completion marker (see _check_completion). That is fine
when the implementer and the verifier are the same agent on the same
box. It cannot express "this WPF build must be verified on a Windows
node with dotnet, and the Linux agent that wrote the code must not be
the one to sign it off" -- there is no way to hand verification to a
DIFFERENT worker chosen for what it can actually run.

This module adds that, and NOTHING else.

NOT A SECOND STATE MACHINE (explicit design constraint)
-------------------------------------------------------
A verify_job is a SATELLITE of a queue_task, never a parallel copy of
it. The task keeps its existing status vocabulary and -- this is the
part worth checking against queue_store.VALID_TRANSITIONS -- P0.5 adds
ZERO task statuses and ZERO task transition edges. Every outcome below
lands on an edge that already existed before this file did:

    verify job outcome     task transition            already existed?
    ------------------     -----------------------    ----------------
    VERIFIED_PASS      ->  VERIFYING -> COMPLETED     yes, and only via
                           mark_completed_with_evidence (the existing
                           evidence gate -- reused, not reimplemented)
    VERIFIED_FAIL      ->  VERIFYING -> FAILED        yes
    NEEDS_REWORK       ->  VERIFYING -> FAILED        yes
    VERIFY_BLOCKED     ->  VERIFYING -> BLOCKED       yes
    VERIFY_CANCELLED   ->  (no task transition)       n/a

NEEDS_REWORK and VERIFIED_FAIL deliberately share ONE task status.
The task-level vocabulary has no separate "needs rework" state, and
inventing one would be exactly the duplicate state machine this design
refuses; the distinction is real and is kept where it belongs, on the
JOB. There is precedent for this in the codebase already:
record_coordinator_decision maps the Coordinator's own four-value
vocabulary (READY/BLOCKED/NEEDS_REWORK/NEEDS_HUMAN) onto existing task
statuses in exactly one place, for exactly this reason. FAILED is also
the status with a real, existing way out -- retry_task (FAILED ->
QUEUED) -- so "rework" is one already-tested call away rather than a new
edge nobody has exercised.

BACKWARD COMPATIBILITY IS STRUCTURAL, NOT A FLAG
-------------------------------------------------
In-session verification stays the default because nothing creates a
verify job unless a task's OWN completion_policy asks for one
(`completion_policy["verify"]`). No existing task has that key, so every
existing lane behaves precisely as it did. There is no global switch to
forget to leave off: a lane opts in per task, or it is untouched.

ROUTING IS ON REPORTED FACTS ONLY
----------------------------------
Capability matching is AND (a job needing playwright AND dotnet must not
match a node with one of them) and draws on a node's REPORTED fields
only -- capability_probe's probed tool list, plus `platform`,
`session_backend` and `shell_capabilities`, which the node agent also
reports about itself. Nothing is inferred: this module will not decide a
Windows box can build WebView2 because it is Windows, the same refusal
capability_probe.py's own docstring makes. A capability it has never
heard of (`webview2`, `browser`) becomes routable the moment a node
probes for it via TERMINAL_MCP_CAPABILITY_PROBES -- which is why there
is no per-application special case anywhere in this file.

KNOWN FLEET LIMITATION, STATED RATHER THAN PAPERED OVER: `macos` is not
a routable key today. The node agent has no Darwin branch -- only
windows_agent.py sets a platform explicitly -- so the MacBook node
reports platform="linux" (verified against the live registry, not
assumed). Adding real Darwin reporting would change what
choose_node(required_platform=...) matches for existing callers, which
is a production behaviour change and therefore not P0.5's to make. What
DOES work today, without redeploying anything: `windows` routes to
dell-5530 even though that node still reports an empty probed-capability
list, because `platform` is a reported fact of its own.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .queue_store import (
    BLOCKED,
    COMPLETED,
    FAILED,
    QueueStore,
    QueueTask,
    RUNNING,
    VERIFYING,
    iso_now,
    new_task_id,
)
from .redaction import redact_text

# -- Verify job state machine -------------------------------------------
#
#   VERIFY_PENDING -> VERIFY_CLAIMED -> VERIFY_RUNNING -> VERIFIED_PASS
#                                                      -> VERIFIED_FAIL
#                                                      -> NEEDS_REWORK
#                                                      -> VERIFY_BLOCKED
#
# with release/lease-expiry returning a claimed or running job to
# VERIFY_PENDING (never losing it), and VERIFY_CANCELLED for a job whose
# task left VERIFYING by some other route.

VERIFY_PENDING = "VERIFY_PENDING"
VERIFY_CLAIMED = "VERIFY_CLAIMED"
VERIFY_RUNNING = "VERIFY_RUNNING"
VERIFIED_PASS = "VERIFIED_PASS"
VERIFIED_FAIL = "VERIFIED_FAIL"
NEEDS_REWORK = "NEEDS_REWORK"
VERIFY_BLOCKED = "VERIFY_BLOCKED"
VERIFY_CANCELLED = "VERIFY_CANCELLED"

ALL_VERIFY_STATUSES = (VERIFY_PENDING, VERIFY_CLAIMED, VERIFY_RUNNING, VERIFIED_PASS,
                       VERIFIED_FAIL, NEEDS_REWORK, VERIFY_BLOCKED, VERIFY_CANCELLED)

OPEN_VERIFY_STATUSES = (VERIFY_PENDING, VERIFY_CLAIMED, VERIFY_RUNNING)
"""A job in one of these still owes an answer -- the set reconciliation
and duplicate-prevention both key off."""

LEASED_VERIFY_STATUSES = (VERIFY_CLAIMED, VERIFY_RUNNING)
"""Exactly the states that carry a claim_token/lease_expires_at, and so
exactly the states renew/release/handoff/complete operate on -- the same
shape as queue_store.LEASE_STATES for tasks."""

TERMINAL_VERIFY_STATUSES = (VERIFIED_PASS, VERIFIED_FAIL, NEEDS_REWORK, VERIFY_CANCELLED)
"""VERIFY_BLOCKED is deliberately NOT terminal, mirroring the task-level
BLOCKED: a job blocked on a missing environment becomes verifiable again
once the environment comes back, via an explicit requeue."""

VERIFY_TRANSITIONS: dict[str, frozenset[str]] = {
    VERIFY_PENDING: frozenset({
        VERIFY_CLAIMED, VERIFY_BLOCKED, VERIFY_CANCELLED,
        # An UNCLAIMED job can still reach VERIFIED_PASS: the in_session
        # fallback path (the pre-existing marker check in
        # queue_engine._check_completion) may complete the task before any
        # verifier claims the job. reconcile() then closes the job from
        # the evidence the TASK recorded -- so this edge is only ever
        # taken with real evidence behind it, never as a shortcut for a
        # verifier that could not be found. Caught by a test rather than
        # foreseen: the first draft omitted it and orphaned exactly that
        # job.
        VERIFIED_PASS,
    }),
    VERIFY_CLAIMED: frozenset({VERIFY_RUNNING, VERIFY_PENDING, VERIFIED_PASS, VERIFIED_FAIL,
                               NEEDS_REWORK, VERIFY_BLOCKED, VERIFY_CANCELLED}),
    VERIFY_RUNNING: frozenset({VERIFIED_PASS, VERIFIED_FAIL, NEEDS_REWORK, VERIFY_BLOCKED,
                               VERIFY_PENDING, VERIFY_CANCELLED}),
    VERIFY_BLOCKED: frozenset({VERIFY_PENDING, VERIFY_CANCELLED}),
    VERIFIED_PASS: frozenset(),
    VERIFIED_FAIL: frozenset(),
    NEEDS_REWORK: frozenset(),
    VERIFY_CANCELLED: frozenset(),
}

VERIFY_RESULT_TO_TASK_STATUS: dict[str, str] = {
    VERIFIED_PASS: COMPLETED,
    VERIFIED_FAIL: FAILED,
    NEEDS_REWORK: FAILED,
    VERIFY_BLOCKED: BLOCKED,
}
"""The ONE place the job vocabulary maps onto the task vocabulary. Kept
here, in a table, so the mapping can be read and tested directly rather
than being scattered across the methods that apply it."""

FALLBACK_IN_SESSION = "in_session"
"""No verifier matched (yet): the task stays in VERIFYING and the
EXISTING in-session marker check keeps running, exactly as it does for
every task today. If that path completes the task, reconcile() closes the
job as VERIFIED_PASS with the in-session evidence attached, so a job is
never orphaned and never has to be resolved twice."""

FALLBACK_HOLD = "hold"
"""No verifier matched: the task stays in VERIFYING and in-session
completion is SUPPRESSED -- the whole point of demanding an independent
verifier is not to accept the implementer's own word when one is
unavailable. The job waits, visibly, with a block_reason. It is never
auto-passed and never dropped."""

VALID_FALLBACKS = (FALLBACK_IN_SESSION, FALLBACK_HOLD)


class InvalidVerifyTransitionError(ValueError):
    """A verify job transition not in VERIFY_TRANSITIONS -- e.g. passing
    an already-failed job. Raised, never coerced, for the same reason
    InvalidTransitionError is: a caller bug must fail in a test rather
    than quietly rewrite a verification outcome."""


# -- Evidence gate -------------------------------------------------------

SELF_REPORT_KEYS = frozenset({
    "summary", "message", "note", "notes", "claim", "claimed", "agent_report",
    "report", "status_text", "description", "comment", "assertion",
})
"""Keys that carry only an agent SAYING it worked. A verification whose
entire evidence is one of these is exactly the "agent self-report is not
enough" case: accepted as commentary alongside real evidence, never as
the evidence itself."""


def evidence_verdict(evidence: Any) -> tuple[bool, str]:
    """Is this evidence good enough to justify VERIFIED_PASS?

    Returns (accepted, reason). Deliberately mechanical -- it cannot
    judge whether a test suite was the RIGHT one, only that something
    beyond a self-assertion was actually produced, and that nothing in
    what was produced contradicts a pass. That is the honest limit of a
    gate at this layer, and it is still the difference between "the agent
    said done" and "there is an exit code, a marker, or a test result".
    """
    if not isinstance(evidence, dict) or not evidence:
        return False, "evidence must be a non-empty object"

    def _empty(value: Any) -> bool:
        return value is None or value == "" or value == [] or value == {}

    substantive = [key for key, value in evidence.items()
                   if key not in SELF_REPORT_KEYS and not _empty(value)]
    if not substantive:
        return False, ("evidence contains only agent self-report keys "
                       f"({', '.join(sorted(SELF_REPORT_KEYS & set(evidence)))}) or empty values -- "
                       "a pass needs something checkable: exit_code, command, "
                       "test_results, completion_marker, commit_sha, artifact, ...")

    # A pass must not be contradicted by its own evidence. These checks
    # are why the gate is worth having at all: an agent that dutifully
    # attaches a real, failing test result should not get a PASS out of
    # it just because the payload was non-empty.
    exit_code = evidence.get("exit_code")
    if exit_code is not None:
        # bool is an int subclass in Python; exit_code=True must not be
        # read as "0-ish and therefore fine".
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return False, f"exit_code must be an integer, got {type(exit_code).__name__}"
        if exit_code != 0:
            return False, f"evidence reports exit_code={exit_code} -- not a pass"
    if evidence.get("passed") is False:
        return False, "evidence reports passed=false -- not a pass"
    failed = evidence.get("tests_failed")
    if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
        return False, f"evidence reports tests_failed={failed} -- not a pass"
    return True, "accepted"


def redact_structure(value: Any) -> Any:
    """Recursively run redaction.redact_text over every string in a
    structure. Verify payloads are stored durably and surfaced to the
    dashboard/ChatGPT, and a failure summary is exactly the kind of
    payload that carries a pasted stack trace with a token in a URL --
    so redaction happens HERE, at the boundary that persists it, rather
    than being left to each caller to remember."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: redact_structure(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(item) for item in value]
    return value


# -- Capability routing --------------------------------------------------

def node_capability_set(node: Any) -> frozenset[str]:
    """Everything a node has REPORTED about itself that work can be
    routed on: probed tool capabilities, plus its platform,
    session_backend and shell capabilities.

    Platform/session_backend are included as routing keys because they
    are reported facts of the same standing as a probe -- the node agent
    states them about itself. They are NOT inferences: nothing here turns
    platform=windows into a "dotnet" or "webview2" capability. The
    practical payoff is that `windows` routes correctly to a node whose
    agent predates capability probing entirely."""
    values: set[str] = set()
    for name in ("capabilities", "shell_capabilities"):
        values.update(str(item) for item in getattr(node, name, ()) or ())
    for name in ("platform", "session_backend"):
        value = getattr(node, name, None)
        if value:
            values.add(str(value))
    return frozenset(values)


def match_verifier_nodes(nodes: Iterable[Any], required: Sequence[str], *,
                         online_only: bool = True) -> list[Any]:
    """Nodes whose reported capability set is a SUPERSET of `required`
    (AND semantics). An empty requirement matches every node, keeping an
    unconstrained caller's behaviour unchanged."""
    from .node_models import NODE_ONLINE
    wanted = frozenset(str(item) for item in required)
    matched = []
    for node in nodes:
        if online_only and getattr(node, "status", None) != NODE_ONLINE:
            continue
        if wanted.issubset(node_capability_set(node)):
            matched.append(node)
    return matched


# -- Records -------------------------------------------------------------

@dataclass(frozen=True)
class VerifyJob:
    id: str
    task_id: str
    attempt: int
    session: str
    status: str
    created_at: str
    updated_at: str
    project_id: str | None = None
    backlog_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    require_independent: bool = True
    fallback: str = FALLBACK_IN_SESSION
    implementer: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    verifier: str | None = None
    verifier_node_id: str | None = None
    claim_token: str | None = None
    lease_expires_at: str | None = None
    claim_count: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)
    failure_summary: dict[str, Any] = field(default_factory=dict)
    block_reason: str | None = None
    history: tuple[dict[str, Any], ...] = ()
    claimed_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "VerifyJob":
        return cls(
            id=row["id"], task_id=row["task_id"], attempt=row["attempt"], session=row["session"],
            status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
            project_id=row["project_id"], backlog_id=row["backlog_id"],
            required_capabilities=tuple(json.loads(row["required_capabilities"] or "[]")),
            require_independent=bool(row["require_independent"]), fallback=row["fallback"],
            implementer=row["implementer"], branch=row["branch"], commit_sha=row["commit_sha"],
            verifier=row["verifier"], verifier_node_id=row["verifier_node_id"],
            claim_token=row["claim_token"], lease_expires_at=row["lease_expires_at"],
            claim_count=row["claim_count"],
            evidence=json.loads(row["evidence"] or "{}"),
            failure_summary=json.loads(row["failure_summary"] or "{}"),
            block_reason=row["block_reason"],
            history=tuple(json.loads(row["history"] or "[]")),
            claimed_at=row["claimed_at"], completed_at=row["completed_at"],
        )

    def to_dict(self, *, include_token: bool = False) -> dict[str, Any]:
        """`claim_token` is the HOLDER's capability, not an observability
        field -- omitted unless a caller that just minted it asks for it,
        the same rule queue_store.lease_holder follows."""
        data = {
            "id": self.id, "task_id": self.task_id, "attempt": self.attempt,
            "session": self.session, "status": self.status,
            "project_id": self.project_id, "backlog_id": self.backlog_id,
            "required_capabilities": list(self.required_capabilities),
            "require_independent": self.require_independent, "fallback": self.fallback,
            "implementer": self.implementer, "branch": self.branch, "commit_sha": self.commit_sha,
            "verifier": self.verifier, "verifier_node_id": self.verifier_node_id,
            "lease_expires_at": self.lease_expires_at, "claim_count": self.claim_count,
            "evidence": self.evidence, "failure_summary": self.failure_summary,
            "block_reason": self.block_reason, "history": list(self.history),
            "created_at": self.created_at, "updated_at": self.updated_at,
            "claimed_at": self.claimed_at, "completed_at": self.completed_at,
        }
        if include_token:
            data["claim_token"] = self.claim_token
        return data


def _lease_expiry(lease_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + lease_seconds))


class VerifyQueue:
    """Claimable verification work over the SAME database queue_store.py
    owns.

    Deliberately shares that store's connection rather than opening its
    own: creating a verify job and moving its task RUNNING -> VERIFYING
    must be ONE transaction (a job whose task never entered VERIFYING, or
    a task in VERIFYING with no job, are both states nothing would ever
    clean up). For the task half it calls QueueStore._transition_locked,
    the single chokepoint that validates every task transition against
    VALID_TRANSITIONS -- writing the task row with raw SQL from here to
    avoid touching an underscore-prefixed method would be the actual
    mistake, since it would put a second, unvalidated writer on
    queue_tasks."""

    def __init__(self, store: QueueStore | None = None, *, registry: Any = None) -> None:
        self.store = store or QueueStore()
        self.registry = registry

    # -- internals -------------------------------------------------------

    @contextlib.contextmanager
    def _immediate(self):
        """BEGIN IMMEDIATE for every mutating path -- the write lock is
        taken BEFORE the read, so two verifiers racing for the same job
        serialise instead of both believing they claimed it."""
        connection = self.store._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _row(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM verify_jobs WHERE id = ?", (job_id,)).fetchone()

    @staticmethod
    def _append_history(row: sqlite3.Row, *, actor: str | None, event: str,
                        reason: str | None = None, **extra: Any) -> str:
        """Append-only audit trail: actor + time + event + reason for
        every state change, on the job itself (requirement: audit
        actor/time/reason). Never rewritten, only appended."""
        history = list(json.loads(row["history"] or "[]"))
        entry: dict[str, Any] = {"at": iso_now(), "event": event, "actor": actor}
        if reason:
            entry["reason"] = reason
        entry.update({key: value for key, value in extra.items() if value is not None})
        history.append(entry)
        return json.dumps(history)

    def _set_status_locked(self, connection: sqlite3.Connection, row: sqlite3.Row, to_status: str, *,
                           actor: str | None, reason: str | None, fields: dict[str, Any] | None = None,
                           event: str | None = None) -> VerifyJob:
        from_status = row["status"]
        if to_status not in VERIFY_TRANSITIONS.get(from_status, frozenset()):
            raise InvalidVerifyTransitionError(
                f"{row['id']}: {from_status} -> {to_status} is not a valid verify transition")
        payload: dict[str, Any] = {"status": to_status, "updated_at": iso_now()}
        payload.update(fields or {})
        payload["history"] = self._append_history(
            row, actor=actor, event=event or to_status, reason=reason,
            from_status=from_status, to_status=to_status)
        clause = ", ".join(f"{key} = ?" for key in payload)
        connection.execute(f"UPDATE verify_jobs SET {clause} WHERE id = ?",
                           (*payload.values(), row["id"]))
        return VerifyJob.from_row(self._row(connection, row["id"]))

    # -- creation --------------------------------------------------------

    def ensure_verify_job(self, task: QueueTask, *, required_capabilities: Sequence[str] = (),
                          require_independent: bool = True, fallback: str = FALLBACK_IN_SESSION,
                          backlog_id: str | None = None, branch: str | None = None,
                          commit_sha: str | None = None, actor: str = "queue-engine") -> VerifyJob:
        """IDEMPOTENT per (task_id, attempt): the same task at the same
        attempt always yields the SAME job, whether this is called twice
        by a re-entered tick, again after a process restart, or again on
        a reconnect. That guarantee is the UNIQUE(task_id, attempt)
        constraint plus this transaction, not an in-memory set -- a
        restart wipes an in-memory set and would create a duplicate.

        A genuine RETRY does get its own job: retry_task -> QUEUED ->
        DISPATCHING bumps attempt_count, so the next verification of the
        reworked implementation is a distinct row with its own evidence,
        rather than the previous attempt's verdict being overwritten and
        the history of the first failure lost.

        Moves the task RUNNING -> VERIFYING in the same transaction when
        it is not already there, so 'job exists' and 'task is awaiting
        verification' can never disagree."""
        if fallback not in VALID_FALLBACKS:
            raise ValueError(f"fallback must be one of {VALID_FALLBACKS}, got {fallback!r}")
        with self._immediate() as connection:
            existing = connection.execute(
                "SELECT * FROM verify_jobs WHERE task_id = ? AND attempt = ?",
                (task.id, task.attempt_count)).fetchone()
            if existing is not None:
                return VerifyJob.from_row(existing)

            current = connection.execute(
                "SELECT status, session, project_id, claimed_by FROM queue_tasks WHERE id = ?",
                (task.id,)).fetchone()
            if current is None:
                raise KeyError(f"no such task: {task.id}")
            if current["status"] == RUNNING:
                self.store._transition_locked(
                    connection, task.id, RUNNING, VERIFYING,
                    event_type="VERIFY_REQUESTED",
                    reason="verify job created -- awaiting an independent verifier"
                    if require_independent else "verify job created")
            elif current["status"] != VERIFYING:
                raise InvalidVerifyTransitionError(
                    f"{task.id}: cannot open a verify job from status {current['status']} "
                    f"-- a task must be RUNNING or VERIFYING")

            now = iso_now()
            job_id = new_task_id()
            history = json.dumps([{
                "at": now, "event": "CREATED", "actor": actor,
                "required_capabilities": list(required_capabilities),
                "fallback": fallback,
            }])
            connection.execute(
                "INSERT INTO verify_jobs (id, task_id, attempt, session, project_id, backlog_id, status, "
                "required_capabilities, require_independent, fallback, implementer, branch, commit_sha, "
                "history, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, task.id, task.attempt_count, current["session"], current["project_id"],
                 backlog_id, VERIFY_PENDING, json.dumps([str(c) for c in required_capabilities]),
                 int(require_independent), fallback, current["claimed_by"], branch, commit_sha,
                 history, now, now))
            return VerifyJob.from_row(self._row(connection, job_id))

    @staticmethod
    def verify_policy_for(task: QueueTask) -> dict[str, Any] | None:
        """The per-task opt-in. Returns the `verify` block of a task's own
        completion_policy, or None when it has none -- which is every task
        that exists today, and therefore why nothing changes for them."""
        policy = task.completion_policy.get("verify") if isinstance(task.completion_policy, dict) else None
        return policy if isinstance(policy, dict) and policy else None

    # -- claiming --------------------------------------------------------

    def claim_next(self, *, verifier: str, capabilities: Sequence[str] = (),
                   project_id: str | None = None, verifier_node_id: str | None = None,
                   lease_seconds: float = 600.0) -> VerifyJob | None:
        """Claim the oldest VERIFY_PENDING job this verifier can actually
        do. `capabilities` is what the verifier HAS; a job matches when
        its required set is a subset of that (AND semantics).

        Refuses a job whose implementer is this same verifier when the job
        asked for independence -- the point of routing verification
        elsewhere is not to hand it straight back. Returns None when
        nothing matches, which is an ordinary outcome, not an error."""
        have = frozenset(str(item) for item in capabilities)
        with self._immediate() as connection:
            clause = "AND project_id = ? " if project_id else ""
            params: tuple[Any, ...] = (project_id,) if project_id else ()
            rows = connection.execute(
                f"SELECT * FROM verify_jobs WHERE status = ? {clause}ORDER BY created_at, id",
                (VERIFY_PENDING, *params)).fetchall()
            for row in rows:
                required = frozenset(json.loads(row["required_capabilities"] or "[]"))
                if not required.issubset(have):
                    continue
                if row["require_independent"] and row["implementer"] and row["implementer"] == verifier:
                    continue
                token = new_task_id()
                return self._set_status_locked(
                    connection, row, VERIFY_CLAIMED, actor=verifier, event="CLAIMED",
                    reason=None,
                    fields={"verifier": verifier, "verifier_node_id": verifier_node_id,
                            "claim_token": token, "lease_expires_at": _lease_expiry(lease_seconds),
                            "claimed_at": iso_now(), "claim_count": row["claim_count"] + 1,
                            "block_reason": None})
        return None

    def start(self, job_id: str, claim_token: str, *, detail: str | None = None) -> VerifyJob | None:
        """VERIFY_CLAIMED -> VERIFY_RUNNING. Separate from the claim so
        "held but not started" is distinguishable from "actively being
        verified" in the dashboard -- the same reason PRECHECK and RUNNING
        are separate task states."""
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["claim_token"] != claim_token or row["status"] != VERIFY_CLAIMED:
                return None
            return self._set_status_locked(connection, row, VERIFY_RUNNING,
                                           actor=row["verifier"], reason=detail, event="STARTED")

    def renew(self, job_id: str, claim_token: str, *, lease_seconds: float = 600.0) -> VerifyJob | None:
        """Extend an active verify lease. Returns None on a lost race --
        losing a lease is an ordinary outcome, exactly as it is for
        queue_store.renew_task_lease."""
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["claim_token"] != claim_token or row["status"] not in LEASED_VERIFY_STATUSES:
                return None
            connection.execute(
                "UPDATE verify_jobs SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND claim_token = ?",
                (_lease_expiry(lease_seconds), iso_now(), job_id, claim_token))
            return VerifyJob.from_row(self._row(connection, job_id))

    def release(self, job_id: str, claim_token: str, *, reason: str | None = None) -> VerifyJob | None:
        """Give a verify claim back before its TTL. The job returns to
        VERIFY_PENDING with its claim fields cleared -- the same shape
        reconcile() produces for an expired lease, so a released job and a
        recovered one are indistinguishable to whoever claims next."""
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["claim_token"] != claim_token or row["status"] not in LEASED_VERIFY_STATUSES:
                return None
            return self._set_status_locked(
                connection, row, VERIFY_PENDING, actor=row["verifier"], event="RELEASED",
                reason=reason or "verify claim released by holder",
                fields={"verifier": None, "verifier_node_id": None, "claim_token": None,
                        "lease_expires_at": None, "claimed_at": None})

    def handoff(self, job_id: str, claim_token: str, *, to_verifier: str, reason: str,
                to_node_id: str | None = None, lease_seconds: float = 600.0) -> VerifyJob | None:
        """Pass an ACTIVE verify claim to another verifier without the job
        returning to the pending pool. The token is ROTATED, which is what
        makes "the previous holder can no longer mutate this result" a
        property of the data rather than of everyone remembering to stop:
        every mutating method here matches on claim_token, so the old
        token stops working the instant this commits."""
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["claim_token"] != claim_token or row["status"] not in LEASED_VERIFY_STATUSES:
                return None
            token = new_task_id()
            history = self._append_history(row, actor=row["verifier"], event="HANDOFF", reason=reason,
                                           from_verifier=row["verifier"], to_verifier=to_verifier)
            connection.execute(
                "UPDATE verify_jobs SET verifier = ?, verifier_node_id = ?, claim_token = ?, "
                "lease_expires_at = ?, claimed_at = ?, claim_count = ?, history = ?, updated_at = ? "
                "WHERE id = ? AND claim_token = ?",
                (to_verifier, to_node_id, token, _lease_expiry(lease_seconds), iso_now(),
                 row["claim_count"] + 1, history, iso_now(), job_id, claim_token))
            return VerifyJob.from_row(self._row(connection, job_id))

    # -- outcomes --------------------------------------------------------

    def complete(self, job_id: str, claim_token: str, *, evidence: dict[str, Any],
                 actor: str | None = None) -> dict[str, Any]:
        """VERIFIED_PASS -- the only path to it, and EVIDENCE-GATED.

        The evidence must clear evidence_verdict: more than a
        self-report, and not contradicted by its own contents. That is
        STRICTLY STRONGER than the pre-existing task-side gate
        (mark_completed_with_evidence, which requires only that evidence
        be non-empty) -- P0.5 tightens the bar for this path, it does not
        route around the old one.

        The task row is written through QueueStore._transition_locked
        rather than by calling mark_completed_with_evidence, for one
        reason only: that method opens its OWN connection, and the job
        verdict and the task completion must commit together or not at
        all. The same VERIFYING -> COMPLETED edge, the same
        verification_evidence column, the same VALID_TRANSITIONS
        validation and the same paired queue_events row all still apply,
        because _transition_locked is exactly what
        mark_completed_with_evidence itself calls.

        Returns a structured refusal rather than raising when evidence is
        rejected: a verifier handing over weak evidence needs to be told
        WHY so it can produce real evidence, and the job stays claimed and
        retryable meanwhile."""
        accepted, reason = evidence_verdict(evidence)
        if not accepted:
            return {"ok": False, "error": "EVIDENCE_REJECTED", "job_id": job_id, "reason": reason}
        clean = redact_structure(evidence)
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["claim_token"] != claim_token or row["status"] not in LEASED_VERIFY_STATUSES:
                return {"ok": False, "error": "NOT_LEASE_HOLDER", "job_id": job_id,
                        "reason": "claim_token is not the current holder, or the job is not claimed"}
            task_row = connection.execute("SELECT status FROM queue_tasks WHERE id = ?",
                                          (row["task_id"],)).fetchone()
            if task_row is None or task_row["status"] != VERIFYING:
                return {"ok": False, "error": "TASK_NOT_VERIFYING", "job_id": job_id,
                        "task_id": row["task_id"],
                        "reason": f"task is {task_row['status'] if task_row else 'missing'}, not VERIFYING"}
            self.store._transition_locked(
                connection, row["task_id"], VERIFYING, COMPLETED, event_type="VERIFIED",
                reason=f"verified by {row['verifier']}",
                extra_fields={"verification_evidence": json.dumps(clean)})
            job = self._set_status_locked(
                connection, row, VERIFIED_PASS, actor=actor or row["verifier"], event="VERIFIED_PASS",
                reason=None, fields={"evidence": json.dumps(clean), "completed_at": iso_now(),
                                     "claim_token": None, "lease_expires_at": None})
        return {"ok": True, "job": job.to_dict(), "task_status": COMPLETED}

    def fail(self, job_id: str, claim_token: str, *, result: str, failure_summary: dict[str, Any],
             actor: str | None = None) -> dict[str, Any]:
        """A NEGATIVE verdict: VERIFIED_FAIL, NEEDS_REWORK or
        VERIFY_BLOCKED. Requires a structured, non-empty failure_summary
        -- "it failed" with no detail is not a verification result anyone
        can act on -- and redacts every string in it before it is stored
        or surfaced.

        VERIFIED_FAIL and NEEDS_REWORK both land the task in FAILED (see
        VERIFY_RESULT_TO_TASK_STATUS and this module's own docstring); the
        distinction is preserved on the job. VERIFY_BLOCKED lands the task
        in BLOCKED, needing a human."""
        if result not in VERIFY_RESULT_TO_TASK_STATUS or result == VERIFIED_PASS:
            return {"ok": False, "error": "INVALID_RESULT", "job_id": job_id,
                    "reason": f"result must be one of VERIFIED_FAIL, NEEDS_REWORK, VERIFY_BLOCKED -- got {result!r}"}
        if not isinstance(failure_summary, dict) or not failure_summary:
            return {"ok": False, "error": "FAILURE_SUMMARY_REQUIRED", "job_id": job_id,
                    "reason": "a negative verdict must carry a structured, non-empty failure_summary"}
        clean = redact_structure(failure_summary)
        target_status = VERIFY_RESULT_TO_TASK_STATUS[result]
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["claim_token"] != claim_token or row["status"] not in LEASED_VERIFY_STATUSES:
                return {"ok": False, "error": "NOT_LEASE_HOLDER", "job_id": job_id,
                        "reason": "claim_token is not the current holder, or the job is not claimed"}
            task_row = connection.execute("SELECT status FROM queue_tasks WHERE id = ?",
                                          (row["task_id"],)).fetchone()
            if task_row is None or task_row["status"] != VERIFYING:
                return {"ok": False, "error": "TASK_NOT_VERIFYING", "job_id": job_id,
                        "task_id": row["task_id"],
                        "reason": f"task is {task_row['status'] if task_row else 'missing'}, not VERIFYING"}
            headline = str(clean.get("headline") or clean.get("reason") or result)
            self.store._transition_locked(
                connection, row["task_id"], VERIFYING, target_status,
                event_type=result, reason=f"{result} by {row['verifier']}: {headline}")
            fields = {"failure_summary": json.dumps(clean), "completed_at": iso_now(),
                      "claim_token": None, "lease_expires_at": None}
            if result == VERIFY_BLOCKED:
                fields["block_reason"] = headline
                fields["completed_at"] = None  # VERIFY_BLOCKED is recoverable, not terminal
            job = self._set_status_locked(connection, row, result, actor=actor or row["verifier"],
                                          event=result, reason=headline, fields=fields)
        return {"ok": True, "job": job.to_dict(), "task_status": target_status}

    def requeue(self, job_id: str, *, actor: str, reason: str) -> VerifyJob | None:
        """VERIFY_BLOCKED -> VERIFY_PENDING. The explicit way back for a
        job blocked on an environment that has since come back."""
        with self._immediate() as connection:
            row = self._row(connection, job_id)
            if row is None or row["status"] != VERIFY_BLOCKED:
                return None
            return self._set_status_locked(connection, row, VERIFY_PENDING, actor=actor,
                                           event="REQUEUED", reason=reason,
                                           fields={"block_reason": None, "claim_token": None,
                                                   "lease_expires_at": None, "verifier": None,
                                                   "verifier_node_id": None, "claimed_at": None})

    # -- reads -----------------------------------------------------------

    def get(self, job_id: str) -> VerifyJob | None:
        with self.store._connection() as connection:
            row = self._row(connection, job_id)
        return VerifyJob.from_row(row) if row is not None else None

    def open_job_for_task(self, task_id: str) -> VerifyJob | None:
        """The job that still owes an answer for this task, if any."""
        placeholders = ", ".join("?" * len(OPEN_VERIFY_STATUSES))
        with self.store._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM verify_jobs WHERE task_id = ? AND status IN ({placeholders}) "
                f"ORDER BY attempt DESC LIMIT 1", (task_id, *OPEN_VERIFY_STATUSES)).fetchone()
        return VerifyJob.from_row(row) if row is not None else None

    def list_jobs(self, *, status: str | None = None, project_id: str | None = None,
                  task_id: str | None = None, limit: int = 100) -> list[VerifyJob]:
        clauses, params = [], []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self.store._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM verify_jobs {where}ORDER BY created_at DESC, id LIMIT ?",
                (*params, limit)).fetchall()
        return [VerifyJob.from_row(row) for row in rows]

    def stats(self, *, project_id: str | None = None) -> dict[str, int]:
        clause = "WHERE project_id = ? " if project_id else ""
        params: tuple[Any, ...] = (project_id,) if project_id else ()
        with self.store._connection() as connection:
            rows = connection.execute(
                f"SELECT status, COUNT(*) AS count FROM verify_jobs {clause}GROUP BY status",
                params).fetchall()
        counts = {status: 0 for status in ALL_VERIFY_STATUSES}
        counts.update({row["status"]: row["count"] for row in rows})
        return counts

    def routability(self, job: VerifyJob) -> dict[str, Any]:
        """Can anything in the fleet actually verify this job right now,
        and if not, WHY -- the answer requirement 5 needs so a held job is
        visibly held rather than mysteriously stuck.

        Returns unknown (never a guess) when no registry is wired, which
        is the honest answer for a VerifyQueue constructed without one."""
        if self.registry is None:
            return {"routable": None, "reason": "no node registry wired -- routability unknown",
                    "candidates": []}
        nodes = match_verifier_nodes(self.registry.list(), job.required_capabilities)
        candidates = [node.id for node in nodes]
        if not candidates:
            return {"routable": False, "candidates": [],
                    "reason": "no online node reports every required capability: "
                              f"{', '.join(job.required_capabilities) or '(none)'}"}
        if job.require_independent and job.implementer and candidates == [job.implementer]:
            return {"routable": False, "candidates": candidates,
                    "reason": "the only capable node is the implementer, and this job "
                              "requires an independent verifier"}
        return {"routable": True, "candidates": candidates, "reason": "capable verifier available"}

    def trace(self, task_id: str) -> dict[str, Any]:
        """The full traceability chain for one task, in one read:
        backlog_id -> task -> implementer -> branch/commit -> verify job
        -> verifier -> evidence -> result, plus the audit trail of who
        changed what and when. Assembled from what is actually stored --
        a field nothing ever set comes back null rather than inferred."""
        task = self.store.get_task(task_id)
        jobs = self.list_jobs(task_id=task_id, limit=50)
        if task is None:
            return {"error": "TASK_NOT_FOUND", "task_id": task_id}
        latest = jobs[0] if jobs else None
        return {
            "task_id": task.id,
            "backlog_id": (latest.backlog_id if latest else None) or task.metadata.get("backlog_id"),
            "project_id": task.project_id,
            "session": task.session,
            "task_status": task.status,
            "attempt_count": task.attempt_count,
            "implementer": (latest.implementer if latest else None) or task.claimed_by,
            "branch": (latest.branch if latest else None) or task.metadata.get("branch"),
            "commit_sha": (latest.commit_sha if latest else None) or task.metadata.get("commit_sha"),
            "verification_evidence": task.verification_evidence,
            "verify_jobs": [job.to_dict() for job in jobs],
            "migration_history": list(task.migration_history),
        }

    # -- recovery --------------------------------------------------------

    def reconcile(self, *, now: str | None = None, actor: str = "reconciler") -> dict[str, Any]:
        """Restart/crash safety, in two parts -- neither of which may ever
        lose a job or invent a verdict.

        1. EXPIRED LEASES. A VERIFY_CLAIMED/VERIFY_RUNNING job past its
           lease_expires_at means the verifier died; the job goes back to
           VERIFY_PENDING with claim fields cleared, ready for a fresh
           claim by anyone capable. claim_count is NOT reset, so a job
           that keeps killing its verifier is visible as such.

        2. ORPHANED JOBS. A job still open whose task has left VERIFYING
           by some OTHER route. If the task reached COMPLETED -- the
           in_session fallback path did its job -- the verify job closes
           as VERIFIED_PASS carrying the task's own recorded evidence, so
           the outcome is recorded once, honestly, from real evidence
           rather than being marked passed by this reconciler's fiat. Any
           other terminal/diverted status closes the job as
           VERIFY_CANCELLED with the task's status as the reason.

        Idempotent: running it twice changes nothing the second time."""
        now = now or iso_now()
        expired: list[str] = []
        closed: list[dict[str, str]] = []
        placeholders = ", ".join("?" * len(LEASED_VERIFY_STATUSES))
        with self._immediate() as connection:
            rows = connection.execute(
                f"SELECT * FROM verify_jobs WHERE status IN ({placeholders}) "
                f"AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (*LEASED_VERIFY_STATUSES, now)).fetchall()
            for row in rows:
                self._set_status_locked(
                    connection, row, VERIFY_PENDING, actor=actor, event="LEASE_EXPIRED",
                    reason=f"verify lease expired (held by {row['verifier']}) -- returned to the pool",
                    fields={"verifier": None, "verifier_node_id": None, "claim_token": None,
                            "lease_expires_at": None, "claimed_at": None})
                expired.append(row["id"])

            open_placeholders = ", ".join("?" * len(OPEN_VERIFY_STATUSES))
            rows = connection.execute(
                f"SELECT v.*, t.status AS task_status, t.verification_evidence AS task_evidence "
                f"FROM verify_jobs v LEFT JOIN queue_tasks t ON t.id = v.task_id "
                f"WHERE v.status IN ({open_placeholders})", OPEN_VERIFY_STATUSES).fetchall()
            for row in rows:
                task_status = row["task_status"]
                if task_status == VERIFYING:
                    continue
                if task_status == COMPLETED:
                    evidence = json.loads(row["task_evidence"] or "{}")
                    self._set_status_locked(
                        connection, row, VERIFIED_PASS, actor=actor, event="VERIFIED_PASS",
                        reason="task completed through the in-session evidence gate before a "
                               "verifier claimed this job",
                        fields={"evidence": json.dumps(evidence), "completed_at": iso_now(),
                                "claim_token": None, "lease_expires_at": None})
                else:
                    self._set_status_locked(
                        connection, row, VERIFY_CANCELLED, actor=actor, event="VERIFY_CANCELLED",
                        reason=f"task left VERIFYING (now {task_status or 'missing'}) -- "
                               f"nothing left to verify",
                        fields={"completed_at": iso_now(), "claim_token": None,
                                "lease_expires_at": None})
                closed.append({"job_id": row["id"], "task_status": task_status or "missing"})
        return {"leases_expired": expired, "jobs_closed": closed,
                "expired_count": len(expired), "closed_count": len(closed)}
