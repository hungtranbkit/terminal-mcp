"""Supervisor Queue v2 -- persistence + state machine (task: "Supervisor
Queue v2 cho Terminal MCP").

ONE SESSION = ONE PERSISTENT AUTONOMOUS TASK QUEUE ("lane"). A caller
(ChatGPT, a human, another tool) pushes an ordered list of tasks into a
session's own lane via `set_tasks`/`append_tasks`; the queue ENGINE
(queue_engine.py, built on top of this store -- not this module) is
what actually watches sessions and dispatches QUEUED tasks in order,
reusing the exact same reliable-submission/status-classification
machinery every other send path already uses (core.py's
terminal_send_text idempotency_key, adapters.py's delivery_state
vocabulary, status.py's classify_status) rather than inventing a
second one -- see queue_engine.py's own module docstring.

This module is deliberately narrow: durable storage + a validated state
machine for one task's lifecycle, nothing that itself touches a
session, sends anything, or runs on a timer. That split is what makes
the state-machine correctness (test matrix item A) and the persistence/
restart-recovery correctness (item B) independently testable, in-
process, with no real tmux/ConPTY session involved at all.

Schema/persistence pattern: same posture as audit.py/grants.py/
supervisor.py (0700 state dir, 0600 db file, WAL, row_factory=Row), but
migrations go through schema.py's own Migration/apply_migrations
(PRAGMA user_version) from day one -- this is a brand NEW store, so
there is no pre-existing "ALTER TABLE ADD COLUMN IF NOT EXISTS" history
to preserve compatibility with; every future schema change is a
regular, ordered, tracked Migration appended to QUEUE_MIGRATIONS.

SAFETY (explicit, repeated user constraint for the whole feature): this
store has no opinion about WHICH sessions it's used against. The
constraint that the real production `window`/`window2` sessions must
never be queued until the acceptance demo passes and the user/ChatGPT
explicitly confirms is enforced by OPERATOR DISCIPLINE (nothing in this
codebase calls set_tasks/append_tasks against those names during this
feature's own development), not by a technical allow/deny-list here --
same posture as every other terminal-mcp safety invariant that depends
on which session name a caller chooses to act on.
"""
from __future__ import annotations

import calendar
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

_LOGGER = logging.getLogger(__name__)

from . import requirement_contract as rc
from . import retry_recovery

from . import worktree_cleanup as wj
from .schema import Migration, apply_migrations

# -- Task status state machine ------------------------------------------
#
# Phase 2 (task: "Supervisor Queue v2 Phase 2 -- Coordinator Agent") adds
# PRECHECK, READY, and FAILED on top of Phase 1's set, and requires a
# Coordinator Agent gate review between QUEUED and actual dispatch:
#
#   QUEUED -> PRECHECK -> READY -> DISPATCHING -> RUNNING -> VERIFYING
#     -> COMPLETED | BLOCKED | FAILED | CANCELLED
#
# Naming note (disclosed, deliberate): the task spec calls the post-
# review, about-to-send state "DISPATCHED" and the success state
# "VERIFIED_DONE". This module keeps Phase 1's existing DISPATCHING/
# COMPLETED names for those exact same concepts instead of introducing
# synonyms -- Phase 1 shipped with real tests and real callers already
# depending on those two names, and "Migration DB idempotent, backward
# compatible" is an explicit Phase 2 requirement. DISPATCHING here means
# exactly what the spec's DISPATCHED means (coordinator-approved, actual
# send attempt in flight); COMPLETED here means exactly what the spec's
# VERIFIED_DONE means (verified, not just claimed-done).

QUEUED = "QUEUED"
PRECHECK = "PRECHECK"      # Phase 2: claimed by the engine, awaiting Coordinator Agent review
READY = "READY"            # Phase 2: coordinator said READY, about to attempt dispatch
DISPATCHING = "DISPATCHING"  # == spec's "DISPATCHED": send attempt in flight
DISPATCH_UNCERTAIN = "DISPATCH_UNCERTAIN"
"""P0 (task: "persist-before-dispatch"): a send attempt returned
delivery_state == DELIVERY_UNKNOWN (Enter was sent, no adapter evidence
either way). Distinct from a bare re-QUEUED task specifically so a
dashboard/ChatGPT/operator can SEE "this one's outcome is genuinely
unknown right now" rather than it silently looking like an ordinary
still-waiting task -- the whole point of this status existing at all.
Never resolved by guessing: reconcile_uncertain_and_waiting either
confirms real activity (-> RUNNING) or, after a grace period with none,
falls back to QUEUED for a safe re-attempt (same sticky dispatch_
idempotency_key, so a send that actually DID land is still deduped by
core.py's own idempotent_sends store rather than repeated)."""
RUNNING = "RUNNING"
VERIFYING = "VERIFYING"
COMPLETED = "COMPLETED"    # == spec's "VERIFIED_DONE"
BLOCKED = "BLOCKED"        # coordinator/gate refusal, or an unrecoverable-without-human send failure
FAILED = "FAILED"          # Phase 2: engine/worker-detected execution failure (distinct from a gate refusal)
WAITING_SESSION = "WAITING_SESSION"
"""P0 (task: "persist-before-dispatch"): the task's own target session
could not be found/reached at all (SESSION_NOT_FOUND, NODE_UNREACHABLE,
AMBIGUOUS_SESSION) -- NOT a coordinator refusal (PAUSED/NEEDS_HUMAN)
and NOT an execution failure (FAILED): the session/node itself is
simply not there right now, which is routine and auto-recoverable (a
node reconnecting, a session being recreated) rather than something a
human needs to decide about. The task is never dropped -- it just waits
here until the session becomes resolvable again, then reconcile_
uncertain_and_waiting returns it to QUEUED for a completely fresh
claim+review (never resumes "in place": the session may have a new
identity by the time it's back, so a full re-review is the safe
default, not an optimization worth the complexity of doing otherwise)."""
PAUSED = "PAUSED"
SKIPPED = "SKIPPED"
CANCELLED = "CANCELLED"

ALL_STATUSES = (QUEUED, PRECHECK, READY, DISPATCHING, DISPATCH_UNCERTAIN, RUNNING, VERIFYING, COMPLETED, BLOCKED,
                FAILED, WAITING_SESSION, PAUSED, SKIPPED, CANCELLED)

# -- routing state (TMCP-TASK-ROUTER-001) --------------------------------
#
# DELIBERATELY NOT A TASK STATUS. A task's status says how far through its
# lifecycle it is; routing state says whether a RUNTIME has been found for it.
# They are independent -- a QUEUED task may be BOUND (a session is claimed,
# dispatch is imminent) or WAITING_RUNTIME (nothing eligible exists yet), and
# collapsing the two would mean re-deciding every edge in VALID_TRANSITIONS
# for a fact that is not a lifecycle stage at all. Same reasoning, and the
# same shape, as v10's `deploy_state`.
UNROUTED = "UNROUTED"
"""No routing decision has ever been made for this task. This is the state the
production bug lived in: a task nobody had tried to place looked exactly like
one deliberately left alone, so no reconcile could tell them apart."""
BOUND = "BOUND"
"""A specific, eligible session is claimed for this task -- `execution_session`
names it, and no other task may be bound to that session until this one
settles. The claim is what makes concurrent routing safe."""
SPAWNED = "SPAWNED"
"""Bound to a session the router itself created because nothing eligible
already existed. Distinct from BOUND so "did we grow the fleet for this" is
answerable without reading the evidence blob."""
WAITING_RUNTIME = "WAITING_RUNTIME"
"""Reuse and spawn were both impossible, and `routing_evidence` says exactly
why, for every candidate that was considered. The ONLY legitimate way for a
task to sit queued -- an unexplained QUEUED is now a bug by construction."""
ROUTING_STATES = (UNROUTED, BOUND, SPAWNED, WAITING_RUNTIME)

#: Routing states that hold a live claim on `execution_session`.
ROUTING_BOUND_STATES = frozenset({BOUND, SPAWNED})

UNASSIGNED_LANE = "__unassigned__"
"""Unified Task System checkpoint (2026-09-07, docs/REQUIREMENTS.md §20):
a reserved, real lane name for a Global Task with no session assigned
yet ("Backlog" in the Kanban UI) -- deliberately NOT a schema change
(queue_tasks.session stays NOT NULL, unchanged from every existing
caller's own assumption) specifically to avoid rippling a nullable-
session change through queue_engine.py's dispatch loop, the Coordinator
gate, and every existing test that assumes a real session string.
`auto_dispatch_enabled` defaults to False for every lane including this
one (§7), so this lane is never ticked by the background loop -- it is
completely inert with respect to dispatch, which is exactly what an
unassigned task needs (nothing to send it to yet). §20.1's own doc
entry describes the nullable-column alternative that was considered and
why this reserved-lane approach was chosen instead for the real,
shipped implementation -- kept in sync with this comment, never
contradicting it."""

# ---------------------------------------------------------------------------
# WHO paused a lane. A lane pause is not one thing: a coordinator pause is a
# GUARD around one specific task it refused to dispatch, while an operator
# pause is a standing instruction. Only the first kind can ever be reconciled
# automatically, so the two must be distinguishable -- and before this column
# they were not: both were free text in `paused_reason`.
#
# Real incident this closes (live, 2026-09-19, lane
# terminal-mcp-session-health): the coordinator refused one task over a
# `merge into main` pattern, which paused the whole lane; the task was
# CANCELLED 32 seconds later, and the lane stayed paused for 3.5 hours with
# nothing left to guard. Three later tasks were enqueued into a lane that was
# already closed and never got claimed. Nothing in the system ever revisited
# it, because resume_lane is only ever called by a human.
PAUSE_ORIGIN_COORDINATOR = "coordinator"
"""Set by record_coordinator_decision's NEEDS_HUMAN path. Guards ONE task;
reconcilable once that task is no longer awaiting a human."""
PAUSE_ORIGIN_USER = "user"
"""An explicit operator/API pause (QueueService.pause). NEVER auto-cleared --
a standing instruction outlives whatever task happened to be in the lane."""
PAUSE_ORIGIN_PROJECT = "project"
"""project_service.py's own project-level pause. Also never auto-cleared: it
is released by the project, not by task movement."""

# Legacy prefixes, for rows written before `paused_origin` existed. The
# coordinator's own format has always been f"coordinator: {reason}" (see
# record_coordinator_decision), and project_service writes
# f"project-pause[{id}]: ..." -- so an old row is still classifiable without a
# backfill that would have to guess.
_LEGACY_PAUSE_PREFIXES = ((PAUSE_ORIGIN_COORDINATOR, "coordinator:"),
                          (PAUSE_ORIGIN_PROJECT, "project-pause["))


def pause_origin(paused_origin: str | None, paused_reason: str | None) -> str | None:
    """Who paused this lane: the stored origin, else inferred from a legacy
    reason prefix, else None.

    None means UNKNOWN, and unknown is never treated as reconcilable -- the
    safe direction, because clearing a pause somebody set deliberately is the
    one failure mode worth designing against.
    """
    if paused_origin:
        return paused_origin
    for origin, prefix in _LEGACY_PAUSE_PREFIXES:
        if paused_reason and paused_reason.startswith(prefix):
            return origin
    return None


MOVABLE_STATUSES = (QUEUED, BLOCKED, PAUSED, WAITING_SESSION)
"""Unified Task System checkpoint: which statuses `move_task_to_session`
will move at all -- deliberately narrower than "not yet terminal".
PRECHECK/READY/DISPATCHING/DISPATCH_UNCERTAIN are excluded even though
they're not RUNNING either: the engine's own background tick could be
mid-review/mid-dispatch for exactly this task at the same moment a
human/PM tries to move it, a real race this project's own standing
discipline (never risk a double-dispatch/lost-task race for a cosmetic
convenience) says to avoid rather than "probably fine". A task in one
of those states must reach QUEUED/BLOCKED/WAITING_SESSION/PAUSED (or a
terminal state, which is simply not movable at all -- already done)
before it can be reassigned."""
TERMINAL_STATUSES = (COMPLETED, SKIPPED, CANCELLED)
"""Once here, a task never transitions again -- not even via a manual
tool call. BLOCKED/FAILED are deliberately NOT terminal (terminal_queue_
retry/skip/cancel all still apply to them -- see VALID_TRANSITIONS)."""

# Every ALLOWED (from_status -> {to_status, ...}) edge. Anything not
# listed here is refused by transition_task -- see its own docstring for
# why this is enforced centrally rather than trusted to each caller.
# Phase 1's edges are kept EXACTLY as they were (backward compatible --
# QUEUED -> DISPATCHING directly, e.g., remains valid for any caller
# that bypasses the Phase 2 coordinator gate on purpose, such as a test
# or a future non-gated queue mode); Phase 2 only ADDS new states/edges
# on top, never removes or narrows an existing one.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({
        PRECHECK,     # Phase 2: engine claimed this task, coordinator review starting
        DISPATCHING,  # Phase 1 compat: direct dispatch, bypassing the coordinator gate
        SKIPPED, CANCELLED, PAUSED,
        # RECONCILIATION ONLY, and only for a task that was never dispatched.
        # When the work is done by direct sends into the session -- which
        # record_manual_dispatch already notes on the task -- the engine has no
        # attempt, no nonce and no marker, so none of its paths can ever close
        # the task: it stays QUEUED forever while the work has shipped, and the
        # queue reports "pending" about a finished job. Reached ONLY through
        # QueueService.verify's undispatched branch, which still demands
        # explicit evidence and records RECONCILED, never VERIFIED.
        COMPLETED,
    }),
    PRECHECK: frozenset({
        READY,        # coordinator: READY
        QUEUED,       # coordinator: NEEDS_REWORK -- back of a remediation task, retry later
        BLOCKED,      # coordinator: BLOCKED
        WAITING_SESSION,  # session/node unreachable -- checked BEFORE the coordinator gate even runs
        PAUSED,       # coordinator: NEEDS_HUMAN -- pauses the whole lane
        CANCELLED,
    }),
    READY: frozenset({DISPATCHING, WAITING_SESSION, CANCELLED, PAUSED}),
    DISPATCHING: frozenset({
        RUNNING,  # submit confirmed (delivery_state == SUBMIT_CONFIRMED)
        DISPATCH_UNCERTAIN,  # delivery_state == DELIVERY_UNKNOWN -- outcome genuinely unknown
        QUEUED,   # legacy/compat direct path -- reconciled as "definitely not sent" -- safe to retry
        WAITING_SESSION,  # the session/node itself vanished mid-send
        BLOCKED,  # a hard send failure needing human attention
        FAILED,   # a hard send failure the engine can auto-classify as retryable
        CANCELLED, PAUSED,
    }),
    DISPATCH_UNCERTAIN: frozenset({
        RUNNING,   # later evidence confirms it really was delivered
        QUEUED,    # grace period elapsed with no confirming evidence -- safe re-attempt (same sticky key)
        CANCELLED, PAUSED,
    }),
    RUNNING: frozenset({VERIFYING, WAITING_SESSION, BLOCKED, FAILED, CANCELLED, PAUSED}),
    VERIFYING: frozenset({
        COMPLETED,
        RUNNING,   # false alarm -- the agent wasn't actually done, re-arm
        WAITING_SESSION, BLOCKED, FAILED, CANCELLED, PAUSED,
    }),
    BLOCKED: frozenset({
        QUEUED, SKIPPED, CANCELLED,
        # Planner checkpoint (§20.3): a SPLIT PARENT is deliberately
        # parked in BLOCKED the moment its children are created (it has
        # no more real work of its own to dispatch -- see planner_
        # service.py's own _apply_split) and reaches COMPLETED only when
        # every real child does too (§20.1's own parent-completion
        # rule), never via the ordinary DISPATCHING->RUNNING->VERIFYING
        # path. Guarded at the CALLER (PlannerService.complete_parent_
        # if_children_done refuses unless metadata.is_split_parent is
        # True) -- this is a targeted, additive transition for exactly
        # that one real, new need, never a generic "any BLOCKED task can
        # be marked done" escape hatch; every OTHER existing BLOCKED
        # case (a real Coordinator refusal) is completely unaffected --
        # QUEUED/SKIPPED/CANCELLED are its only reachable states.
        COMPLETED,
    }),  # only ever via an explicit operator tool call (or the guarded split-parent completion above)
    FAILED: frozenset({QUEUED, SKIPPED, CANCELLED}),   # only ever via an explicit operator tool call
    WAITING_SESSION: frozenset({
        QUEUED,  # the ONLY outgoing edge -- session is resolvable again, fresh claim+review from scratch
        CANCELLED,
    }),
    PAUSED: frozenset({
        QUEUED, PRECHECK, READY, DISPATCHING, DISPATCH_UNCERTAIN, RUNNING, VERIFYING, WAITING_SESSION, CANCELLED,
        COMPLETED,  # reconciliation only -- see the QUEUED entry above
    }),  # resume (to paused_from_status) or cancel
    COMPLETED: frozenset(),
    SKIPPED: frozenset(),
    CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    """Raised by transition_task when (from_status -> to_status) is not
    in VALID_TRANSITIONS -- e.g. COMPLETED -> RUNNING, or QUEUED ->
    VERIFYING (skipping DISPATCHING/RUNNING). Never silently coerced or
    ignored: an engine bug that tries an invalid transition needs to
    fail loudly in a test, not quietly corrupt a task's history."""


class RequirementsNotCoveredError(ValueError):
    """Raised by mark_completed_with_evidence when the task carries a
    requirement contract whose required criteria are not all covered (or
    explicitly waived with an actor and a reason), or whose evidence was
    reconciled against a stale contract version.

    Carries the full `GateDecision` so a caller can report WHICH requirement
    ids are missing rather than only that something was refused -- the
    reported MESFlow failure was exactly a missing id nobody could name.
    """

    def __init__(self, decision: "rc.GateDecision") -> None:
        super().__init__(f"{decision.reason}: {decision.detail}")
        self.decision = decision


class TaskAlreadyClaimedError(ValueError):
    """Raised by reassign_task (Task Migration/Load Balancing, item 12's
    own race-safety requirement) when the task is no longer in a
    migratable status by the time the reassignment's own atomic
    transaction runs -- e.g. a dispatcher concurrently claimed it
    (QUEUED -> PRECHECK) in the window between a caller's own eligibility
    check and this call. The caller (task_migration.py) reports this as
    TASK_ALREADY_CLAIMED rather than silently reassigning out from under
    a live claim."""


def is_valid_transition(from_status: str, to_status: str) -> bool:
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())


@dataclass(frozen=True)
class QueueTask:
    id: str
    session: str
    position: int
    title: str
    prompt: str
    status: str
    created_at: str
    started_at: str | None
    completed_at: str | None
    attempt_count: int
    max_attempts: int
    completion_policy: dict[str, Any]
    last_error: str | None
    correlation_id: str | None
    metadata: dict[str, Any]
    updated_at: str
    paused_from_status: str | None = None
    # -- Phase 2 additions (migration v2) --------------------------------
    priority: int = 0
    depends_on: tuple[str, ...] = ()
    node_id: str | None = None
    claimed_by: str | None = None
    claim_token: str | None = None
    lease_expires_at: str | None = None
    coordinator_decision: dict[str, Any] = field(default_factory=dict)
    coordinator_reason: str | None = None
    coordinator_checked_at: str | None = None
    coordinator_attempts: int = 0
    verification_evidence: dict[str, Any] = field(default_factory=dict)
    verification_nonce: str | None = None
    dispatch_idempotency_key: str | None = None
    uncertain_or_waiting_since: str | None = None
    original_owner: str | None = None
    migration_history: tuple[dict[str, Any], ...] = ()
    at_risk: bool = False
    # P0.1 Project Dimension. None = legacy/unscoped: every pre-existing
    # query is unfiltered unless a caller explicitly asks for a project,
    # so a task without one behaves exactly as it did before.
    project_id: str | None = None
    # Orchestration V1: which user-visible deliverable this task rolls up
    # into. Nullable -- a task with no outcome behaves exactly as before.
    outcome_id: str | None = None
    # The caller's own key for the REQUEST that created this task, so a
    # retry returns this task instead of making a second one. Nullable:
    # a task created without one behaves exactly as before.
    request_key: str | None = None
    # Requirement Contract (migration v10). All nullable: a task without a
    # contract reconciles to NO_CONTRACT and completes exactly as before.
    requirement_contract: dict[str, Any] = field(default_factory=dict)
    evidence_matrix: dict[str, Any] = field(default_factory=dict)
    # Deployment is a fact about an artifact, NOT a task status -- see
    # _add_v10_requirement_contract. Deploying never changes `status`.
    deploy_state: str | None = None
    # TMCP-TASK-ROUTER-001 (migration v13). All nullable: a task created
    # before the router existed reads UNROUTED/None and behaves exactly as
    # it did. `session` remains the lane; these say which runtime is
    # actually executing the work and why that one was chosen.
    execution_session: str | None = None
    execution_node_id: str | None = None
    routing_state: str | None = None
    routing_evidence: dict[str, Any] = field(default_factory=dict)
    agent_id: str | None = None
    skill_ids: tuple[str, ...] = ()

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QueueTask":
        return cls(
            id=row["id"], session=row["session"], position=row["position"],
            title=row["title"], prompt=row["prompt"], status=row["status"],
            created_at=row["created_at"], started_at=row["started_at"], completed_at=row["completed_at"],
            attempt_count=row["attempt_count"], max_attempts=row["max_attempts"],
            completion_policy=_parse_json_object(row["completion_policy"]),
            last_error=row["last_error"], correlation_id=row["correlation_id"],
            request_key=(row["request_key"] if "request_key" in row.keys() else None),
            requirement_contract=(_parse_json_object(row["requirement_contract"])
                                  if "requirement_contract" in row.keys() else {}),
            evidence_matrix=(_parse_json_object(row["evidence_matrix"])
                             if "evidence_matrix" in row.keys() else {}),
            deploy_state=(row["deploy_state"] if "deploy_state" in row.keys() else None),
            metadata=_parse_json_object(row["metadata"]), updated_at=row["updated_at"],
            paused_from_status=row["paused_from_status"],
            priority=row["priority"], depends_on=tuple(_parse_json_list(row["depends_on"])),
            node_id=row["node_id"], claimed_by=row["claimed_by"], claim_token=row["claim_token"],
            lease_expires_at=row["lease_expires_at"],
            coordinator_decision=_parse_json_object(row["coordinator_decision"]),
            coordinator_reason=row["coordinator_reason"], coordinator_checked_at=row["coordinator_checked_at"],
            coordinator_attempts=row["coordinator_attempts"],
            verification_evidence=_parse_json_object(row["verification_evidence"]),
            verification_nonce=row["verification_nonce"],
            dispatch_idempotency_key=row["dispatch_idempotency_key"],
            uncertain_or_waiting_since=row["uncertain_or_waiting_since"],
            original_owner=row["original_owner"],
            migration_history=tuple(_parse_json_dict_list(row["migration_history"])),
            at_risk=bool(row["at_risk"]),
            project_id=(row["project_id"] if "project_id" in row.keys() else None),
            outcome_id=(row["outcome_id"] if "outcome_id" in row.keys() else None),
            execution_session=(row["execution_session"] if "execution_session" in row.keys() else None),
            execution_node_id=(row["execution_node_id"] if "execution_node_id" in row.keys() else None),
            routing_state=(row["routing_state"] if "routing_state" in row.keys() else None),
            routing_evidence=(_parse_json_object(row["routing_evidence"])
                              if "routing_evidence" in row.keys() else {}),
            agent_id=(row["agent_id"] if "agent_id" in row.keys() else None),
            skill_ids=(tuple(_parse_json_list(row["skill_ids"])) if "skill_ids" in row.keys() else ()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "session": self.session, "position": self.position,
            "title": self.title, "prompt": self.prompt, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at, "completed_at": self.completed_at,
            "attempt_count": self.attempt_count, "max_attempts": self.max_attempts,
            "completion_policy": self.completion_policy, "last_error": self.last_error,
            "correlation_id": self.correlation_id, "request_key": self.request_key,
            "metadata": self.metadata, "updated_at": self.updated_at,
            "paused_from_status": self.paused_from_status,
            "priority": self.priority, "depends_on": list(self.depends_on), "node_id": self.node_id,
            "claimed_by": self.claimed_by, "claim_token": self.claim_token,
            "lease_expires_at": self.lease_expires_at,
            "coordinator_decision": self.coordinator_decision, "coordinator_reason": self.coordinator_reason,
            "coordinator_checked_at": self.coordinator_checked_at, "coordinator_attempts": self.coordinator_attempts,
            "verification_evidence": self.verification_evidence,
            "verification_nonce": self.verification_nonce,
            "dispatch_idempotency_key": self.dispatch_idempotency_key,
            "uncertain_or_waiting_since": self.uncertain_or_waiting_since,
            "original_owner": self.original_owner,
            "migration_history": list(self.migration_history),
            "at_risk": self.at_risk,
            "project_id": self.project_id,
            "outcome_id": self.outcome_id,
            "execution_session": self.execution_session,
            "execution_node_id": self.execution_node_id,
            # A task the router has never seen reads UNROUTED rather than
            # null: "no decision yet" is itself the answer a dashboard needs,
            # and a missing key would read as "not applicable".
            "routing_state": self.routing_state or UNROUTED,
            "routing_evidence": self.routing_evidence,
            "agent_id": self.agent_id,
            "skill_ids": list(self.skill_ids),
        }


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _parse_json_dict_list(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_task_id() -> str:
    return uuid.uuid4().hex


def default_queue_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_QUEUE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "queue.db"


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE queue_tasks (
            id TEXT PRIMARY KEY,
            session TEXT NOT NULL,
            position INTEGER NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'QUEUED',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            completion_policy TEXT,
            last_error TEXT,
            correlation_id TEXT,
            metadata TEXT,
            updated_at TEXT NOT NULL,
            paused_from_status TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_queue_tasks_session_position ON queue_tasks(session, position)")
    connection.execute("CREATE INDEX idx_queue_tasks_session_status ON queue_tasks(session, status)")
    connection.execute(
        """
        CREATE TABLE queue_lanes (
            session TEXT PRIMARY KEY,
            paused INTEGER NOT NULL DEFAULT 0,
            paused_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE queue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            session TEXT NOT NULL,
            task_id TEXT,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            reason TEXT,
            metadata TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_queue_events_session ON queue_events(session, id)")


def _add_v2_coordinator_columns(connection: sqlite3.Connection) -> None:
    """Phase 2 (task: "Supervisor Queue v2 Phase 2 -- Coordinator
    Agent"): additive only -- every new column is nullable or has a
    default, so every existing Phase 1 row (and every Phase 1 caller
    that only ever set the columns it knew about) keeps working
    unchanged. Applied via schema.py's own tracked Migration/
    apply_migrations (PRAGMA user_version), so this runs exactly once,
    idempotent across restarts, same guarantee as v1's own creation."""
    for column, declaration in (
        ("priority", "INTEGER NOT NULL DEFAULT 0"),
        ("depends_on", "TEXT"),          # JSON list of task_ids
        ("node_id", "TEXT"),             # pinned at PRECHECK/claim time -- see claim_next_task
        ("claimed_by", "TEXT"),          # engine/worker instance id holding the current lease
        ("claim_token", "TEXT"),         # durable idempotency/lease token -- see claim_next_task
        ("lease_expires_at", "TEXT"),    # reconcile_stale_claims' own expiry check
        ("coordinator_decision", "TEXT"),  # full structured CoordinatorDecision, JSON
        ("coordinator_reason", "TEXT"),
        ("coordinator_checked_at", "TEXT"),
        ("coordinator_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("verification_evidence", "TEXT"),  # JSON -- what actually proved COMPLETED, not just the label
        ("verification_nonce", "TEXT"),  # minted at dispatch time -- status.py's verify_completion_marker input
        # Minted ONCE per real send attempt and then STICKY across a
        # reconcile-and-reclaim cycle (reconcile_stale_claims never
        # clears it -- only an explicit retry_task/skip_task/cancel_task
        # after a real BLOCKED/FAILED does, which IS a genuinely new
        # attempt). This is what makes "restart mid-dispatch, then
        # re-claim and re-dispatch" actually idempotent at the
        # terminal_send_text layer: attempt_count keeps bumping (for
        # audit visibility -- see _transition_locked), but the ACTUAL
        # idempotency_key used for the send stays the same until a real
        # outcome (RUNNING, or an explicit operator retry) is reached,
        # so a crash between "send succeeded" and "RUNNING transition
        # committed" can never cause a second real keystroke -- core.py's
        # own idempotent_sends store returns the original result for the
        # reused key instead.
        ("dispatch_idempotency_key", "TEXT"),
    ):
        connection.execute(f"ALTER TABLE queue_tasks ADD COLUMN {column} {declaration}")
    connection.execute("CREATE INDEX idx_queue_tasks_priority ON queue_tasks(session, priority, position)")
    # Explicit, per-session, OFF-by-default opt-in for an AUTOMATIC
    # background dispatch loop (queue_engine.py's own module docstring;
    # task's own explicit constraint: "Không bật auto-dispatch cho
    # session production hiện hữu mặc định. Feature flag/policy opt-in
    # per session."). Calling queue_engine.tick() directly (a test, a
    # manual terminal_queue_run_once tool call) is NEVER gated by this --
    # it only controls whether an unattended poll loop is allowed to
    # touch this lane on its own.
    connection.execute("ALTER TABLE queue_lanes ADD COLUMN auto_dispatch_enabled INTEGER NOT NULL DEFAULT 0")


def _add_v3_uncertain_waiting_column(connection: sqlite3.Connection) -> None:
    """P0 (task: "persist-before-dispatch"): a single timestamp column,
    reused for BOTH new statuses (DISPATCH_UNCERTAIN and
    WAITING_SESSION) -- each task is only ever in one of the two at a
    time, so one column is enough; reconcile_uncertain_and_waiting uses
    it as the grace-period clock for both. Additive only, same
    backward-compatible posture as migration v2."""
    connection.execute("ALTER TABLE queue_tasks ADD COLUMN uncertain_or_waiting_since TEXT")


def _add_v4_migration_columns(connection: sqlite3.Connection) -> None:
    """Task Migration / Load Balancing (task: "bổ sung Task Migration /
    Load Balancing vào Queue/Coordinator"). Additive only. original_owner
    is set ONCE (at creation, to the task's initial session) and never
    changes again -- migration_history is the full, ordered provenance
    trail; `session` itself (the task's CURRENT assignment) is the only
    column reassign_task ever actually moves. at_risk marks a RUNNING
    task whose own session went offline (item 8) -- informational only,
    never auto-migrated. queue_lanes gets `project` (an optional
    grouping key -- rebalancing only ever considers same-project lanes;
    None only matches None, so unconfigured lanes are never accidentally
    mixed) and `last_rebalance_at` (the cooldown/hysteresis clock, item
    5's own "cooldown để task không ping-pong qua lại")."""
    for column, declaration in (
        ("original_owner", "TEXT"),
        ("migration_history", "TEXT"),  # JSON list of {from, to, reason, time, actor}
        ("at_risk", "INTEGER NOT NULL DEFAULT 0"),
    ):
        connection.execute(f"ALTER TABLE queue_tasks ADD COLUMN {column} {declaration}")
    connection.execute("UPDATE queue_tasks SET original_owner = session WHERE original_owner IS NULL")
    for column, declaration in (
        ("project", "TEXT"),
        ("last_rebalance_at", "TEXT"),
    ):
        connection.execute(f"ALTER TABLE queue_lanes ADD COLUMN {column} {declaration}")


def _add_v5_dispatch_idempotency_key_if_missing(connection: sqlite3.Connection) -> None:
    """Real, live-discovered data-integrity fix (P0 QUEUE + SUPERVISOR
    LIVE TEST checkpoint, 2026-09-07): this project's own real, existing
    `queue.db` (7 lanes, including window/window2/wtest) was found to
    have `PRAGMA user_version = 4` -- meaning migrations 1-4 are all
    considered already applied -- yet its `queue_tasks` table was
    genuinely missing `dispatch_idempotency_key`, a column `Migration(2,
    ...)`'s own function (`_add_v2_coordinator_columns`, above) has
    included since the very commit that introduced it (`04f14d0`,
    confirmed via `git log -S`). The only honest explanation: this
    specific, real database file was created against an earlier, in-
    development state of that same migration's body (mid-iteration on
    that feature, before `04f14d0` reached its own final form) and had
    its `user_version` already stamped to 2 (then 3, then 4) at that
    point -- `apply_migrations` correctly never re-runs a migration
    version it already recorded as complete, so this column silently
    never arrived. A fresh `queue.db` created from this codebase today
    would never hit this (migration 2 already includes the column from
    the start) -- this migration exists ONLY to heal that one real,
    already-existing file (and any other one migrated the same way)
    without ever touching/renumbering the historical migration list
    itself. Checks column existence first (real `PRAGMA table_info`,
    not a try/except) so it is correctly a no-op on a database that
    already has the column -- safe either way, never a duplicate-column
    error, never a guess."""
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
    if "dispatch_idempotency_key" not in existing_columns:
        connection.execute("ALTER TABLE queue_tasks ADD COLUMN dispatch_idempotency_key TEXT")


def _add_v6_project_dimension(connection: sqlite3.Connection) -> None:
    """P0.1 Project Dimension. Additive and NULLABLE on purpose: a task
    with project_id IS NULL behaves EXACTLY as before this migration --
    every existing query is unfiltered unless a caller opts in, so a
    legacy database keeps working untouched.

    Only queue_tasks gains a column. `queue_lanes.project` already exists
    (migration v4) and was simply never populated -- it is REUSED as the
    lane-level project key rather than adding a second, competing column.
    The canonical value in both is the project_identity id
    (e.g. "git:github.com/acme/widget"), never a free-text label.

    An index is added because project-scoped listing/claiming is the whole
    point; without it every project query degrades to a table scan as the
    queue grows."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
    if "project_id" not in columns:
        connection.execute("ALTER TABLE queue_tasks ADD COLUMN project_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_tasks_project_status "
        "ON queue_tasks(project_id, status)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_lanes_project ON queue_lanes(project)")


def _add_v7_verify_jobs(connection: sqlite3.Connection) -> None:
    """P0.5 Verify Queue. A verify_jobs row is a SATELLITE of a queue_task,
    never a second copy of it: the task keeps its own existing status
    (VERIFYING while a job is outstanding, then COMPLETED/FAILED/BLOCKED
    exactly as today) and this table records WHO must verify it, WITH what
    capabilities, and WHAT the outcome was. No task status is added and no
    task transition edge changes -- see verify_queue.py's own module
    docstring for the full mapping and why it is deliberately not a second
    state machine over the same task.

    UNIQUE(task_id, attempt) is the duplicate-prevention primitive the
    whole "no duplicate verify job on retry/restart" requirement rests on:
    a retry bumps attempt_count (see _transition_locked), so a genuine
    re-attempt gets its own job while a repeated ensure_verify_job for the
    SAME attempt is an idempotent no-op rather than a second job. Nothing
    below relies on an in-process guard.

    Entirely additive: a database that never creates a verify job is
    byte-for-byte the same queue as before, which is what keeps in-session
    verification the untouched default."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS verify_jobs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            session TEXT NOT NULL,
            project_id TEXT,
            backlog_id TEXT,
            status TEXT NOT NULL,
            required_capabilities TEXT NOT NULL DEFAULT '[]',
            require_independent INTEGER NOT NULL DEFAULT 1,
            fallback TEXT NOT NULL DEFAULT 'in_session',
            implementer TEXT,
            branch TEXT,
            commit_sha TEXT,
            verifier TEXT,
            verifier_node_id TEXT,
            claim_token TEXT,
            lease_expires_at TEXT,
            claim_count INTEGER NOT NULL DEFAULT 0,
            evidence TEXT,
            failure_summary TEXT,
            block_reason TEXT,
            history TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT,
            UNIQUE (task_id, attempt)
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_status_created "
        "ON verify_jobs(status, created_at)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_project_status "
        "ON verify_jobs(project_id, status)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_verify_jobs_task ON verify_jobs(task_id)")


def _add_v8_outcomes(connection: sqlite3.Connection) -> None:
    """Orchestration V1: the OUTCOME layer -- the unit of user-visible
    completion that sits BETWEEN a backlog item and the tasks that deliver it.

    Before this, the hierarchy was exactly two levels and welded 1:1:
    backlog_service.dispatch created one queue task per item and then refused
    ever to do it again (ALREADY_DISPATCHED). There was no way to say "this
    deliverable took five tasks", and therefore no way to say whether the
    DELIVERABLE was done -- only whether individual tasks were.

    queue_tasks.outcome_id is nullable and additive: every existing task and
    every existing query behaves exactly as before. An outcome lives in the
    SAME database as the tasks that roll up into it, so status rollup is one
    query rather than a cross-store join that could observe a torn state."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS outcomes (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            backlog_id TEXT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            acceptance_criteria TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'P2',
            evidence TEXT NOT NULL DEFAULT '{}',
            blocked_reason TEXT,
            history TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcomes_project_status ON outcomes(project_id, status)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcomes_backlog ON outcomes(backlog_id)")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
    if "outcome_id" not in columns:
        connection.execute("ALTER TABLE queue_tasks ADD COLUMN outcome_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_tasks_outcome ON queue_tasks(outcome_id, status)")


def _add_v9_request_key(connection: sqlite3.Connection) -> None:
    """Idempotent task CREATION.

    The queue already had idempotency for the DISPATCH side -- a sticky
    key derived from (task_id, attempt) so a retried send cannot deliver a
    prompt twice. Creation had none, so a caller that retried after a
    timeout, or a client that resent on reconnect, silently produced a
    SECOND task for one request. For an agent-driven caller that is the
    common case, not the rare one.

    `request_key` is nullable and additive: every existing row and every
    existing call behaves exactly as before. The UNIQUE index is PARTIAL --
    NULLs are excluded -- so tasks created without a key are unaffected and
    can still be created freely. The uniqueness is enforced by the database
    rather than by a read-then-write in the service, because two concurrent
    retries of the same request would otherwise both find nothing and both
    insert."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
    if "request_key" not in columns:
        connection.execute("ALTER TABLE queue_tasks ADD COLUMN request_key TEXT")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_tasks_request_key "
        "ON queue_tasks(request_key) WHERE request_key IS NOT NULL")


def _add_v10_requirement_contract(connection: sqlite3.Connection) -> None:
    """What the task was asked for, what proved it, and where it was deployed.

    See docs/MISS_TASK_ROOT_CAUSE.md. RC3: a missing acceptance criterion was
    not a representable fact, so no gate could enforce it. RC6: there was no
    DEPLOYED concept at all, so "I got it onto TEST" had only one word
    available -- COMPLETED -- and the conflation was structural.

    All three columns are nullable and additive. A task created before this
    migration reads NULL, takes the no-contract path, and behaves exactly as it
    did. `deploy_state` is deliberately NOT a task status: deployment is a fact
    about an artifact, not a stage of a task's lifecycle, and modelling it as a
    status would have meant editing VALID_TRANSITIONS and re-deciding every
    edge. A task can be DEPLOYED_TEST while still RUNNING.
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
    for column, declaration in (
        ("requirement_contract", "TEXT"),   # JSON RequirementContract, append-only versions
        ("evidence_matrix", "TEXT"),        # JSON EvidenceMatrix, requirement_id -> status
        ("deploy_state", "TEXT"),           # NOT_DEPLOYED/DEPLOYED_TEST/DEPLOYED_PROD, never a status
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE queue_tasks ADD COLUMN {column} {declaration}")


def _add_v11_project_packets(connection: sqlite3.Connection) -> None:
    """Durable project-feeder packets (additive, restart-safe).

    A packet is the feeder's idempotency/lease unit, separate from queue
    tasks so a reconnect cannot create a second bundle.  The queue remains
    the source of truth for execution state; this table only records the
    durable dispatch decision and its lease/checkpoint.
    """
    connection.execute("""
        CREATE TABLE IF NOT EXISTS project_packets (
            packet_id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            lane TEXT NOT NULL,
            worker TEXT NOT NULL,
            task_ids TEXT NOT NULL,
            task_shas TEXT NOT NULL,
            request_key TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL,
            lease_expires_at TEXT,
            heartbeat_at TEXT,
            checkpoint TEXT NOT NULL DEFAULT '{}',
            telemetry TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_project_packets_worker_state "
                       "ON project_packets(worker, state)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_project_packets_project_state "
                       "ON project_packets(project_id, state)")


def _add_v12_long_task_watches(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS long_task_watches (
            task_id TEXT PRIMARY KEY, submission_id TEXT NOT NULL UNIQUE,
            request_key TEXT NOT NULL UNIQUE, session TEXT NOT NULL, node_id TEXT,
            state TEXT NOT NULL, expected_minutes INTEGER, long_task INTEGER NOT NULL DEFAULT 1,
            execution_started_at TEXT, first_checkpoint_at TEXT, last_progress_at TEXT,
            recovery_enter_count INTEGER NOT NULL DEFAULT 0, resume_count INTEGER NOT NULL DEFAULT 0,
            output_hash TEXT, status_hash TEXT, blocker TEXT, reason TEXT,
            watch_lease_expires_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_long_task_watches_state ON long_task_watches(state)")


def _add_v14_paused_origin(connection: sqlite3.Connection) -> None:
    """Additive, nullable: NULL means "written before this column existed",
    which `pause_origin()` classifies from the legacy reason prefix instead of
    guessing. No backfill -- a wrong backfill would silently make an operator
    pause look auto-clearable, and the prefix inference is already exact for
    both writers that ever set one."""
    connection.execute("ALTER TABLE queue_lanes ADD COLUMN paused_origin TEXT")


def _add_v13_task_routing(connection: sqlite3.Connection) -> None:
    """TMCP-TASK-ROUTER-001: where a task is actually being EXECUTED, who
    owns it, and why the router chose that.

    THE BUG THIS EXISTS TO MAKE IMPOSSIBLE. A durable task sat QUEUED while
    its target session was IDLE, and nothing in the database could say why --
    `session` was both "the lane this row lives in" and "the runtime that will
    run it", so there was nowhere to record that a routing decision had (or
    had not) been made, and no way for a restart-safe reconcile to tell a task
    nobody had ever tried to place from one deliberately left alone.

    Six nullable, additive columns, no status-enum change (same posture as
    v10's `deploy_state`, and for the same reason: routing is a fact about
    WHERE a task runs, not a stage of its lifecycle, so modelling it as a
    status would mean re-deciding every edge in VALID_TRANSITIONS):

      execution_session / execution_node_id -- the runtime currently bound to
        this task. `session` stays the lane, unchanged, so every existing
        query and every existing caller behaves exactly as before; ownership
        of the TASK is stable while its execution binding may change.
      routing_state -- UNROUTED / BOUND / SPAWNED / WAITING_RUNTIME. The
        explicit answer to "why is this still queued", which is the thing
        that was previously unrepresentable.
      routing_evidence -- JSON: the chosen candidate's score, the reason, and
        the top rejected candidates with their rejection reasons. Persisted
        rather than recomputed so the dashboard shows what the router
        ACTUALLY decided at the time, not what it would decide now.
      agent_id / skill_ids -- the Agent+Skill bridge. Nullable on purpose:
        this is a minimal bridge onto the existing store, never a second,
        parallel task/agent database (Phase B's registries populate these
        same columns rather than replacing them).
    """
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(queue_tasks)")}
    for column, declaration in (
        ("execution_session", "TEXT"),
        ("execution_node_id", "TEXT"),
        ("routing_state", "TEXT"),
        ("routing_evidence", "TEXT"),
        ("agent_id", "TEXT"),
        ("skill_ids", "TEXT"),
    ):
        if column not in columns:
            connection.execute(f"ALTER TABLE queue_tasks ADD COLUMN {column} {declaration}")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_tasks_execution_session "
        "ON queue_tasks(execution_session) WHERE execution_session IS NOT NULL")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_queue_tasks_routing_state "
        "ON queue_tasks(routing_state) WHERE routing_state IS NOT NULL")


QUEUE_MIGRATIONS = [
    Migration(1, "initial Supervisor Queue v2 schema (queue_tasks/queue_lanes/queue_events)", _create_v1_schema),
    Migration(2, "Phase 2: Coordinator Agent columns (priority/depends_on/node_id/claim lease/"
                 "coordinator_decision/verification_evidence)", _add_v2_coordinator_columns),
    Migration(3, "P0 persist-before-dispatch: DISPATCH_UNCERTAIN/WAITING_SESSION grace-period column",
             _add_v3_uncertain_waiting_column),
    Migration(4, "Task Migration/Load Balancing: original_owner/migration_history/at_risk, "
                 "lane project/last_rebalance_at", _add_v4_migration_columns),
    Migration(5, "heal a real, already-migrated database missing dispatch_idempotency_key "
                 "(see this function's own docstring)", _add_v5_dispatch_idempotency_key_if_missing),
    Migration(6, "P0.1 Project Dimension: queue_tasks.project_id (nullable) + project indexes; "
                 "queue_lanes.project (added v4, never populated) is REUSED as the lane key",
              _add_v6_project_dimension),
    Migration(7, "P0.5 Verify Queue: verify_jobs satellite table (UNIQUE(task_id, attempt) is the "
                 "duplicate-prevention primitive); no task status or transition edge changes",
              _add_v7_verify_jobs),
    Migration(8, "Orchestration V1: outcomes table + queue_tasks.outcome_id (nullable, additive) -- "
                 "the user-visible deliverable one backlog item may need N tasks to reach",
              _add_v8_outcomes),
    Migration(9, "idempotent creation: queue_tasks.request_key (nullable) + a PARTIAL unique "
                 "index, so a retried create returns the SAME task instead of a second one",
              _add_v9_request_key),
    Migration(10, "Requirement Contract: queue_tasks.requirement_contract/evidence_matrix "
              "(nullable) so a missing acceptance criterion is a representable fact, "
              "plus deploy_state kept OFF the status enum so deploying never reads as done",
             _add_v10_requirement_contract),
    Migration(11, "durable project feeder packets with leases/checkpoints/idempotency",
             _add_v11_project_packets),
    Migration(12, "durable long-task execution watches", _add_v12_long_task_watches),
    Migration(13, "TMCP-TASK-ROUTER-001: execution_session/execution_node_id/routing_state/"
              "routing_evidence + the nullable agent_id/skill_ids bridge",
              _add_v13_task_routing),
    Migration(14, "TMCP-BLOCKED-REVIEW-AUTOCLEAR-001: queue_lanes.paused_origin, so a "
              "coordinator guard pause is distinguishable from a standing operator pause",
              _add_v14_paused_origin),
]


def _epoch_or_none(value: Any) -> float | None:
    """An ISO timestamp -> epoch seconds, or None. Never raises: a sweep must
    not die on one unparseable row."""
    if not value:
        return None
    try:
        from datetime import datetime

        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


class QueueStore:
    """SQLite persistence for the whole Supervisor Queue v2 feature --
    same pattern as supervisor.py's SupervisorStore (0700 state dir,
    0600 db file, WAL, row_factory=Row), migrations via schema.py."""

    def __init__(self, path: str | Path | None = None, *,
                 event_sink: Any = None) -> None:
        # Orchestration V1: an OPTIONAL callable invoked once per recorded
        # queue event, AFTER the transaction that produced it has committed.
        #
        # After-commit, not inside: the bus is a different database, so there
        # is no cross-store atomicity to be had. Publishing inside the
        # transaction would mean an event could exist for a transition that
        # then rolled back -- strictly worse than the reverse. Publishing
        # after means a crash in the gap loses the event, which is why every
        # event carries an idempotency_key derived from the queue_events row
        # id: the next producer to touch that row republishes harmlessly.
        #
        # A sink that raises is swallowed. A publishing glitch must never
        # un-commit a real state transition -- the same posture
        # queue_engine._notify_completed already takes.
        self._event_sink = event_sink
        self._pending_events = threading.local()
        self.path = Path(path) if path is not None else default_queue_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, QUEUE_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
            self._drain_events()
        except BaseException:
            self._discard_events()
            raise
        finally:
            connection.close()

    def _queue_event(self, entry: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        if not hasattr(self._pending_events, "items"):
            self._pending_events.items = []
        self._pending_events.items.append(entry)

    def _drain_events(self) -> None:
        """Publish everything the just-committed transaction recorded."""
        if self._event_sink is None:
            return
        items = getattr(self._pending_events, "items", None)
        if not items:
            return
        self._pending_events.items = []
        for entry in items:
            try:
                self._event_sink(entry)
            except Exception:  # noqa: BLE001 -- see __init__: never un-commit a transition
                pass

    def _discard_events(self) -> None:
        """The transaction rolled back, so nothing happened -- an event
        describing it would be a lie."""
        if hasattr(self._pending_events, "items"):
            self._pending_events.items = []

    # -- lane-level -------------------------------------------------------

    def _ensure_lane(self, connection: sqlite3.Connection, session: str) -> None:
        now = iso_now()
        connection.execute(
            "INSERT OR IGNORE INTO queue_lanes (session, paused, paused_reason, created_at, updated_at) "
            "VALUES (?, 0, NULL, ?, ?)", (session, now, now),
        )

    def pause_lane(self, session: str, *, reason: str | None = None,
                   origin: str | None = None) -> None:
        """Pauses dispatch for this session's lane. If a task is currently
        PRECHECK/READY/DISPATCHING/RUNNING/VERIFYING, it moves to PAUSED
        too (its prior status saved in paused_from_status so resume_lane
        can restore it) -- this is the mechanism item 10's "manual
        intervention -> pause lane, no race" requirement is built on, as
        well as a plain operator-requested pause of an otherwise-idle
        lane, and record_coordinator_decision's own NEEDS_HUMAN path."""
        with self._connection() as connection:
            self._pause_lane_locked(connection, session, reason=reason, origin=origin)

    def _pause_lane_locked(self, connection: sqlite3.Connection, session: str, *, reason: str | None,
                           origin: str | None = None) -> None:
        self._ensure_lane(connection, session)
        now = iso_now()
        connection.execute(
            "UPDATE queue_lanes SET paused = 1, paused_reason = ?, paused_origin = ?, updated_at = ? "
            "WHERE session = ?",
            (reason, origin, now, session),
        )
        active = connection.execute(
            "SELECT id, status FROM queue_tasks WHERE session = ? AND status IN (?, ?, ?, ?, ?, ?, ?)",
            (session, PRECHECK, READY, DISPATCHING, DISPATCH_UNCERTAIN, RUNNING, VERIFYING, WAITING_SESSION),
        ).fetchall()
        for row in active:
            self._transition_locked(connection, row["id"], row["status"], PAUSED,
                                    event_type="PAUSED", reason=reason,
                                    extra_fields={"paused_from_status": row["status"]})
        self._record_event_locked(connection, session=session, task_id=None, event_type="LANE_PAUSED",
                                  reason=reason)

    def resume_lane(self, session: str) -> None:
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            now = iso_now()
            connection.execute(
                "UPDATE queue_lanes SET paused = 0, paused_reason = NULL, paused_origin = NULL, "
                "updated_at = ? WHERE session = ?",
                (now, session),
            )
            paused_tasks = connection.execute(
                "SELECT id, paused_from_status FROM queue_tasks WHERE session = ? AND status = ?",
                (session, PAUSED),
            ).fetchall()
            for row in paused_tasks:
                restore_to = row["paused_from_status"] or QUEUED
                self._transition_locked(connection, row["id"], PAUSED, restore_to,
                                        event_type="RESUMED", reason=None,
                                        extra_fields={"paused_from_status": None})
            self._record_event_locked(connection, session=session, task_id=None, event_type="LANE_RESUMED", reason=None)

    def lane_status(self, session: str) -> dict[str, Any]:
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            lane_row = connection.execute("SELECT * FROM queue_lanes WHERE session = ?", (session,)).fetchone()
            task_rows = connection.execute(
                "SELECT * FROM queue_tasks WHERE session = ? ORDER BY position ASC", (session,),
            ).fetchall()
        tasks = [QueueTask.from_row(row).to_dict() for row in task_rows]
        # Phase 2: PRECHECK/READY/BLOCKED/FAILED all count as "the lane's
        # current task" too, not just the Phase 1 in-flight set -- same
        # _ACTIVE_STATUSES the store's own dispatch-gating query uses
        # (defined further down this class), kept in sync deliberately
        # rather than duplicating the literal tuple here.
        active = next((t for t in tasks if t["status"] in self._ACTIVE_STATUSES), None)
        queued_count = sum(1 for t in tasks if t["status"] == QUEUED)
        completed_count = sum(1 for t in tasks if t["status"] == COMPLETED)
        return {
            "session": session,
            "paused": bool(lane_row["paused"]),
            "paused_reason": lane_row["paused_reason"],
            "paused_origin": pause_origin(lane_row["paused_origin"], lane_row["paused_reason"]),
            "auto_dispatch_enabled": bool(lane_row["auto_dispatch_enabled"]),
            "project": lane_row["project"],
            "last_rebalance_at": lane_row["last_rebalance_at"],
            "tasks": tasks,
            "current_task": active,
            "queued_count": queued_count,
            "completed_count": completed_count,
            "total_count": len(tasks),
        }

    # ---------------------------------------------------- P0.1 project reads
    def list_tasks_for_project(self, project_id: str, *, status: str | None = None,
                               limit: int = 200) -> list[QueueTask]:
        """Project-scoped listing. A SEPARATE method rather than a new
        argument on an existing one: every current caller keeps its exact
        signature and behaviour, and nothing starts filtering implicitly.
        Tasks with project_id IS NULL (every legacy row) are never
        returned here -- they belong to no project by definition."""
        query = "SELECT * FROM queue_tasks WHERE project_id = ?"
        params: list[Any] = [project_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY priority DESC, position ASC LIMIT ?"
        params.append(int(limit))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [QueueTask.from_row(row) for row in rows]

    def lanes_for_project(self, project_id: str) -> list[str]:
        """Which lanes belong to a project -- the read every project-level
        operation needs before it can act on anything.

        A lane counts if EITHER its own `queue_lanes.project` names the
        project (an explicitly scoped lane) OR it currently holds a task
        scoped to it. Both, because the two were populated at different
        times: `queue_lanes.project` has existed since migration v4 and
        `queue_tasks.project_id` since P0.1, and a real deployment has
        lanes with one, the other, or both. Taking the union means a
        project-level pause cannot silently miss a lane that is genuinely
        running its work."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT session FROM queue_lanes WHERE project = ? "
                "UNION "
                "SELECT DISTINCT session FROM queue_tasks WHERE project_id = ? "
                "ORDER BY session",
                (project_id, project_id)).fetchall()
        return [row["session"] for row in rows]

    _PROJECT_EVENT_SCOPE = (
        "(session IN (SELECT session FROM queue_lanes WHERE project = ?) "
        " OR session IN (SELECT DISTINCT session FROM queue_tasks WHERE project_id = ?) "
        " OR task_id IN (SELECT id FROM queue_tasks WHERE project_id = ?))"
    )
    """queue_events carries no project_id of its own -- it predates P0.1
    and is keyed by session/task. Rather than add a denormalised column
    that could drift out of step with the task it describes, a project's
    events are DERIVED the same way its lanes are: by the lane's project,
    by a task in that lane, or by the event's own task. The task_id arm
    matters on its own -- a task moved between lanes still has its events
    attributed to the project, not to whichever lane it happened to sit in
    at the time."""

    def project_events(self, project_id: str, *, since: str | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
        """Newest-first queue events for everything belonging to a project."""
        params: list[Any] = [project_id, project_id, project_id]
        clause = ""
        if since:
            clause = " AND timestamp >= ?"
            params.append(since)
        params.append(int(limit))
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM queue_events WHERE {self._PROJECT_EVENT_SCOPE}{clause} "
                f"ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(row) for row in rows]

    def project_transition_counts(self, project_id: str, *, since: str | None = None) -> dict[str, int]:
        """to_status -> how many transitions INTO it, for a project, in a
        window. This is what makes a report about throughput rather than a
        snapshot: `project_task_counts` says what the queue looks like
        now, this says what actually happened."""
        params: list[Any] = [project_id, project_id, project_id]
        clause = ""
        if since:
            clause = " AND timestamp >= ?"
            params.append(since)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT to_status, COUNT(*) AS n FROM queue_events "
                f"WHERE {self._PROJECT_EVENT_SCOPE}{clause} AND to_status IS NOT NULL "
                f"GROUP BY to_status", params).fetchall()
        return {row["to_status"]: row["n"] for row in rows}

    def project_task_counts(self, project_id: str) -> dict[str, int]:
        """status -> count for one project: the cheap read a project
        status API needs without pulling every row."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM queue_tasks WHERE project_id = ? GROUP BY status",
                (project_id,)).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def set_task_project(self, task_id: str, project_id: str | None) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE queue_tasks SET project_id = ?, updated_at = ? WHERE id = ?",
                (project_id, iso_now(), task_id))
        return cursor.rowcount > 0

    def backfill_project_ids(self, resolver: Any, *, dry_run: bool = True) -> dict[str, Any]:
        """P0.1 backfill. `resolver(session) -> project_id | None` is
        supplied by the caller, so this store never learns how a project
        is derived.

        SAFE BY CONSTRUCTION: only ever fills rows where project_id IS
        NULL, so it can never overwrite or re-home a task that already
        has one; and it defaults to dry_run so the plan is inspectable
        before anything is written."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, session FROM queue_tasks WHERE project_id IS NULL").fetchall()
        planned: dict[str, str] = {}
        unresolved = 0
        cache: dict[str, str | None] = {}
        for row in rows:
            session = row["session"]
            if session not in cache:
                try:
                    cache[session] = resolver(session)
                except Exception:  # noqa: BLE001 - one bad resolve must not abort the backfill
                    cache[session] = None
            project_id = cache[session]
            if project_id:
                planned[row["id"]] = project_id
            else:
                unresolved += 1
        if not dry_run and planned:
            now = iso_now()
            with self._connection() as connection:
                connection.executemany(
                    "UPDATE queue_tasks SET project_id = ?, updated_at = ? WHERE id = ? AND project_id IS NULL",
                    [(pid, now, tid) for tid, pid in planned.items()])
        by_project: dict[str, int] = {}
        for pid in planned.values():
            by_project[pid] = by_project.get(pid, 0) + 1
        return {"dry_run": dry_run, "candidates": len(rows), "planned": len(planned),
                "unresolved": unresolved, "by_project": by_project}

    def list_all_lanes(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            sessions = [row["session"] for row in connection.execute("SELECT session FROM queue_lanes").fetchall()]
        return [self.lane_status(session) for session in sessions]

    def tasks_with_statuses(self, statuses: Sequence[str]) -> list[QueueTask]:
        """Bounded-shape governor read from local durable state only."""
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM queue_tasks WHERE status IN ({placeholders})",
                tuple(statuses)).fetchall()
        return [QueueTask.from_row(row) for row in rows]

    def set_auto_dispatch(self, session: str, enabled: bool) -> None:
        """The explicit, per-session opt-in an automatic background
        dispatch loop is required to check (see queue_engine.py's own
        module docstring and the task's own explicit constraint) --
        OFF by default for every lane, including one that already has
        tasks queued. Calling queue_engine.tick() directly is never
        gated by this."""
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            connection.execute(
                "UPDATE queue_lanes SET auto_dispatch_enabled = ?, updated_at = ? WHERE session = ?",
                (1 if enabled else 0, iso_now(), session),
            )
            self._record_event_locked(connection, session=session, task_id=None,
                                      event_type="AUTO_DISPATCH_ENABLED" if enabled else "AUTO_DISPATCH_DISABLED",
                                      reason=None)

    # -- durable project feeder packets -----------------------------------
    def reserve_project_packet(self, *, packet_id: str, project_id: str, lane: str,
                               worker: str, task_ids: list[str], task_shas: dict[str, str],
                               request_key: str, lease_expires_at: str | None,
                               telemetry: dict[str, Any]) -> dict[str, Any]:
        """Atomically reserve one packet per idempotency key/worker.

        A live packet is never replaced.  A stale packet is returned to the
        caller for explicit recovery, keeping duplicate feeder cycles safe
        across process restarts.
        """
        now = iso_now()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM project_packets WHERE request_key = ?", (request_key,)).fetchone()
            if existing is not None:
                return self._packet_row(existing, deduplicated=True)
            active = connection.execute(
                "SELECT * FROM project_packets WHERE worker = ? AND state IN ('RESERVED','RUNNING') "
                "ORDER BY created_at DESC LIMIT 1", (worker,)).fetchone()
            if active is not None:
                return self._packet_row(active, blocked="worker_packet_active")
            connection.execute(
                "INSERT INTO project_packets(packet_id, project_id, lane, worker, task_ids, task_shas, "
                "request_key, state, lease_expires_at, heartbeat_at, checkpoint, telemetry, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?, '{}', ?, ?, ?)",
                (packet_id, project_id, lane, worker, json.dumps(task_ids, sort_keys=True),
                 json.dumps(task_shas, sort_keys=True), request_key, lease_expires_at, now,
                 json.dumps(telemetry, sort_keys=True), now, now))
            row = connection.execute("SELECT * FROM project_packets WHERE packet_id = ?", (packet_id,)).fetchone()
            return self._packet_row(row)

    @staticmethod
    def _packet_row(row: sqlite3.Row, **extra: Any) -> dict[str, Any]:
        result = {
            "packet_id": row["packet_id"], "project_id": row["project_id"],
            "lane": row["lane"], "worker": row["worker"],
            "task_ids": _parse_json_list(row["task_ids"]),
            "task_shas": _parse_json_object(row["task_shas"]), "request_key": row["request_key"],
            "state": row["state"], "lease_expires_at": row["lease_expires_at"],
            "heartbeat_at": row["heartbeat_at"], "checkpoint": _parse_json_object(row["checkpoint"]),
            "telemetry": _parse_json_object(row["telemetry"]), "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        result.update(extra)
        return result

    def update_project_packet(self, packet_id: str, *, state: str | None = None,
                              checkpoint: dict[str, Any] | None = None,
                              telemetry: dict[str, Any] | None = None,
                              lease_expires_at: str | None = None) -> dict[str, Any] | None:
        fields = ["heartbeat_at = ?", "updated_at = ?"]
        values: list[Any] = [iso_now(), iso_now()]
        if state is not None:
            fields.append("state = ?"); values.append(state)
        if checkpoint is not None:
            fields.append("checkpoint = ?"); values.append(json.dumps(checkpoint, sort_keys=True))
        if telemetry is not None:
            fields.append("telemetry = ?"); values.append(json.dumps(telemetry, sort_keys=True))
        if lease_expires_at is not None:
            fields.append("lease_expires_at = ?"); values.append(lease_expires_at)
        values.append(packet_id)
        with self._connection() as connection:
            connection.execute(f"UPDATE project_packets SET {', '.join(fields)} WHERE packet_id = ?", values)
            row = connection.execute("SELECT * FROM project_packets WHERE packet_id = ?", (packet_id,)).fetchone()
        return self._packet_row(row) if row else None

    def project_packet(self, packet_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM project_packets WHERE packet_id = ?", (packet_id,)).fetchone()
        return self._packet_row(row) if row else None

    def project_packet_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM project_packets WHERE request_key = ?", (request_key,)).fetchone()
        return self._packet_row(row) if row else None

    def active_project_packet(self, worker: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM project_packets WHERE worker = ? AND state IN ('RESERVED','RUNNING') "
                "ORDER BY created_at DESC LIMIT 1", (worker,)).fetchone()
        return self._packet_row(row) if row else None

    # -- long-task watches -------------------------------------------------

    def ensure_long_task_watch(self, task_id: str, submission_id: str, request_key: str,
                               session: str, *, node_id: str | None = None,
                               expected_minutes: int | None = None) -> dict[str, Any]:
        now = iso_now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO long_task_watches
                   (task_id, submission_id, request_key, session, node_id, state,
                    expected_minutes, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'EXECUTION_START_PENDING', ?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET updated_at=excluded.updated_at,
                     node_id=COALESCE(excluded.node_id, long_task_watches.node_id)""",
                (task_id, submission_id, request_key, session, node_id, expected_minutes, now, now),
            )
        return self.long_task_watch(task_id) or {}

    def update_long_task_watch(self, task_id: str, **fields: Any) -> bool:
        allowed = {"state", "execution_started_at", "first_checkpoint_at", "last_progress_at",
                   "recovery_enter_count", "resume_count", "output_hash", "status_hash",
                   "blocker", "reason", "watch_lease_expires_at", "node_id"}
        values = {k: v for k, v in fields.items() if k in allowed}
        if not values:
            return False
        values["updated_at"] = iso_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE long_task_watches SET {assignments} WHERE task_id = ?",
                (*values.values(), task_id),
            )
        return cursor.rowcount > 0

    def long_task_watch(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM long_task_watches WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_long_task_watches(self, *, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM long_task_watches"
        if active_only:
            query += " WHERE state NOT IN ('CHECKPOINTED','BLOCKED','DONE','FAILED','STUCK')"
        query += " ORDER BY updated_at DESC"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    # -- task CRUD ---------------------------------------------------------

    def set_tasks(self, session: str, tasks: list[dict[str, Any]], *, replace_pending: bool = True) -> list[str]:
        """queue_set: pushes an array of tasks into `session`'s lane in
        one call. replace_pending=True (queue_set's own default) cancels
        every currently-QUEUED (not yet dispatched) task first -- RUNNING/
        DISPATCHING/VERIFYING/PAUSED/BLOCKED tasks are NEVER touched by
        this, matching "empty queue never auto-generates tasks" and
        "a session disappearing/reconnecting must never blindly resend a
        RUNNING task" (this call can't affect a RUNNING task either way).
        replace_pending=False is queue_append's own behavior: pure
        append, nothing existing is touched. Returns the new tasks' ids,
        in the same order given."""
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            if replace_pending:
                stale = connection.execute(
                    "SELECT id FROM queue_tasks WHERE session = ? AND status = ?", (session, QUEUED),
                ).fetchall()
                for row in stale:
                    self._transition_locked(connection, row["id"], QUEUED, CANCELLED,
                                            event_type="CANCELLED", reason="superseded by queue_set")
            max_position_row = connection.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_position FROM queue_tasks WHERE session = ?", (session,),
            ).fetchone()
            next_position = max_position_row["max_position"] + 1
            now = iso_now()
            ids = []
            for offset, task in enumerate(tasks):
                task_id = task.get("id") or new_task_id()
                ids.append(task_id)
                connection.execute(
                    "INSERT INTO queue_tasks (id, session, position, title, prompt, status, created_at, "
                    "attempt_count, max_attempts, completion_policy, metadata, updated_at, priority, depends_on, "
                    "original_owner, project_id, request_key) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, session, next_position + offset, task.get("title") or "", task["prompt"], QUEUED, now,
                     int(task.get("max_attempts") or 3), json.dumps(task.get("completion_policy") or {}),
                     json.dumps(task.get("metadata") or {}), now, int(task.get("priority") or 0),
                     json.dumps(list(task.get("depends_on") or [])), session,
                     task.get("project_id"), task.get("request_key") or None),
                )
                self._record_event_locked(connection, session=session, task_id=task_id, event_type="ENQUEUED",
                                          reason=None)
        return ids

    def append_tasks(self, session: str, tasks: list[dict[str, Any]]) -> list[str]:
        return self.set_tasks(session, tasks, replace_pending=False)

    def waiting_since(self, task_id: str, status: str) -> str | None:
        """When this task most recently ENTERED `status`, from its own events.

        Derived rather than stored: the transition is already written to
        queue_events, and a second copy on the row could disagree with it.
        """
        with self._connection() as connection:
            row = connection.execute(
                "SELECT timestamp FROM queue_events WHERE task_id = ? AND event_type = ? "
                "ORDER BY id DESC LIMIT 1", (task_id, status)).fetchone()
        return row["timestamp"] if row else None

    def record_manual_dispatch(self, task_id: str, *, detail: dict[str, Any]) -> dict[str, Any] | None:
        """Reconcile a send that went round the queue onto the task itself.

        An event alone was not enough. Every API and every UI reads the TASK,
        so a task whose prompt had really been delivered by hand still looked
        untouched -- the queue said PAUSED, the worker was busy, and the
        screen showed neither. Recording it here means "dispatched outside
        the queue" is a visible fact rather than a discrepancy someone has to
        notice.

        Deliberately does NOT move the task's status. The queue genuinely did
        not dispatch it, and claiming otherwise would make the state machine
        lie about its own behaviour. What changes is that the bypass is now
        on the record.
        """
        with self._connection() as connection:
            row = connection.execute("SELECT metadata FROM queue_tasks WHERE id = ?",
                                     (task_id,)).fetchone()
            if row is None:
                return None
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except ValueError:
                metadata = {}
            history = list(metadata.get("manual_dispatch_history") or [])
            history.append(detail)
            metadata["manual_dispatch"] = detail
            metadata["manual_dispatch_history"] = history[-10:]
            connection.execute("UPDATE queue_tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                               (json.dumps(metadata), iso_now(), task_id))
        return detail

    def task_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        """The task a previous call with this key already created, if any.

        Returns the WHOLE task rather than just its id: a caller retrying
        wants the same answer it would have got the first time, including
        the state the task has reached since.
        """
        if not request_key:
            return None
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM queue_tasks WHERE request_key = ?", (request_key,)).fetchone()
        return QueueTask.from_row(row).to_dict() if row is not None else None

    def move_task_to_session(self, task_id: str, new_session: str) -> dict[str, Any]:
        """Unified Task System checkpoint (2026-09-07): reassigns an
        EXISTING task row to a different lane (Global/Unassigned ->
        a real session, or session A -> session B) -- the SAME row,
        `id`/`created_at`/`metadata`/history all preserved unchanged
        (task's own explicit "không duplicate record khi assign/move").
        Only ever moves a task in `MOVABLE_STATUSES` (see its own
        docstring for why PRECHECK/READY/DISPATCHING/DISPATCH_UNCERTAIN/
        RUNNING/VERIFYING and every terminal status are refused) --
        returns `{"error": "TASK_NOT_MOVABLE", "status": <current>}`
        rather than silently no-op'ing or forcing it through. Appended
        at the END of the target lane's own position order (never
        inserts ahead of what's already queued there) -- ensures the
        target lane's own `_ensure_lane` row exists first, same as
        `set_tasks` already does for a brand-new lane."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            task = QueueTask.from_row(row)
            if task.status not in MOVABLE_STATUSES:
                return {"error": "TASK_NOT_MOVABLE", "task_id": task_id, "status": task.status}
            old_session = task.session
            if old_session == new_session:
                return QueueTask.from_row(row).to_dict()  # no-op, not an error -- already there
            self._ensure_lane(connection, new_session)
            max_position_row = connection.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_position FROM queue_tasks WHERE session = ?",
                (new_session,),
            ).fetchone()
            new_position = max_position_row["max_position"] + 1
            now = iso_now()
            connection.execute(
                "UPDATE queue_tasks SET session = ?, position = ?, updated_at = ? WHERE id = ?",
                (new_session, new_position, now, task_id),
            )
            self._record_event_locked(connection, session=new_session, task_id=task_id, event_type="TASK_ASSIGNED",
                                      reason=f"moved from {old_session!r} to {new_session!r}")
            updated_row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        return QueueTask.from_row(updated_row).to_dict()

    # -- execution binding (TMCP-TASK-ROUTER-001) --------------------------
    #
    # `session` is the LANE and stays exactly what it always was.
    # `execution_session` is the RUNTIME claim, and these three methods are
    # the only things that write it. They live here, in the store, rather
    # than in the router, for the same reason claim_next_task does: the
    # check and the write have to be one transaction or two routers racing
    # over one idle session both win.

    def tasks_bound_to_session(self, session: str) -> list[QueueTask]:
        """Every non-terminal task currently claiming `session` as its runtime.

        Terminal tasks are excluded deliberately: a COMPLETED task must not go
        on holding a session hostage, and excluding them here is what releases
        the claim without needing a hook on every transition edge."""
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM queue_tasks WHERE execution_session = ? "
                f"AND routing_state IN (?, ?) AND status NOT IN ({placeholders}) "
                f"ORDER BY position",
                (session, BOUND, SPAWNED, *TERMINAL_STATUSES),
            ).fetchall()
        return [QueueTask.from_row(row) for row in rows]

    def bind_task_to_session(self, task_id: str, session: str, *, node_id: str | None = None,
                             routing_state: str = BOUND, evidence: dict[str, Any] | None = None,
                             agent_id: str | None = None,
                             skill_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """Atomically claim `session` as this task's runtime, and move the row
        into that lane in the same transaction.

        THE WHOLE POINT IS THE ATOMICITY. Two routers (two MCP calls, a
        rescue cycle overlapping a route_start, the same loop re-entered)
        looking at the same fleet will reach the same conclusion at the same
        moment, because the scoring is deterministic -- that is a feature
        everywhere except here, where it means both would pick the one idle
        session. `BEGIN IMMEDIATE` takes SQLite's write lock before the
        eligibility read, so the second caller sees the first caller's claim
        and is refused SESSION_ALREADY_CLAIMED rather than double-dispatching.

        Idempotent for the SAME (task, session) pair: re-binding a task to the
        runtime it already holds returns the task, not an error. A retried
        route must not look like a failure.

        Refusals are values, never exceptions -- the router turns each one
        into a rejection reason a human reads on the dashboard.
        """
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            task = QueueTask.from_row(row)
            if task.status in TERMINAL_STATUSES:
                connection.rollback()
                return {"error": "TASK_ALREADY_SETTLED", "task_id": task_id, "status": task.status}
            already_bound = task.routing_state in ROUTING_BOUND_STATES and task.execution_session
            if already_bound and task.execution_session == session:
                connection.rollback()
                return {**task.to_dict(), "rebound": False}
            if already_bound:
                connection.rollback()
                return {"error": "TASK_ALREADY_BOUND", "task_id": task_id,
                        "execution_session": task.execution_session}
            if task.status not in MOVABLE_STATUSES:
                # PRECHECK/READY/DISPATCHING/RUNNING: the engine is mid-flight
                # for this exact task. Re-homing it now is the double-dispatch
                # race MOVABLE_STATUSES was drawn to avoid -- see its docstring.
                connection.rollback()
                return {"error": "TASK_NOT_BINDABLE", "task_id": task_id, "status": task.status}
            placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
            holder = connection.execute(
                f"SELECT id FROM queue_tasks WHERE execution_session = ? AND id != ? "
                f"AND routing_state IN (?, ?) AND status NOT IN ({placeholders}) LIMIT 1",
                (session, task_id, BOUND, SPAWNED, *TERMINAL_STATUSES),
            ).fetchone()
            if holder is not None:
                connection.rollback()
                return {"error": "SESSION_ALREADY_CLAIMED", "task_id": task_id,
                        "session": session, "held_by": holder["id"]}

            now = iso_now()
            if task.status == WAITING_SESSION:
                # Its old session was unreachable; this one is not. QUEUED is
                # the only outgoing edge WAITING_SESSION has, and taking it
                # here is what lets the engine claim the task at all -- left
                # as-is the task would be bound to a live runtime and still
                # unclaimable, which is the bound-but-stuck state this whole
                # feature exists to remove. A fresh claim + full coordinator
                # review is exactly what that status's own contract promises.
                self._transition_locked(
                    connection, task_id, WAITING_SESSION, QUEUED,
                    event_type="REROUTED",
                    reason=f"re-homed from {task.session!r} to {session!r} for a fresh claim")
            if task.session != session:
                self._ensure_lane(connection, session)
                max_position = connection.execute(
                    "SELECT COALESCE(MAX(position), -1) AS max_position FROM queue_tasks WHERE session = ?",
                    (session,),
                ).fetchone()["max_position"]
                connection.execute(
                    "UPDATE queue_tasks SET session = ?, position = ? WHERE id = ?",
                    (session, max_position + 1, task_id),
                )
            connection.execute(
                "UPDATE queue_tasks SET execution_session = ?, execution_node_id = ?, routing_state = ?, "
                "routing_evidence = ?, node_id = COALESCE(?, node_id), updated_at = ? WHERE id = ?",
                (session, node_id, routing_state, json.dumps(evidence) if evidence else None,
                 node_id, now, task_id),
            )
            if agent_id is not None or skill_ids is not None:
                self._write_agent_binding_locked(connection, task_id, agent_id=agent_id, skill_ids=skill_ids)
            self._record_event_locked(
                connection, session=session, task_id=task_id, event_type="TASK_ROUTED",
                reason=(evidence or {}).get("reason"),
                metadata={"routing_state": routing_state, "node_id": node_id,
                          "from_lane": task.session, "score": (evidence or {}).get("score")},
            )
            updated = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            connection.commit()
            self._drain_events()
            return {**QueueTask.from_row(updated).to_dict(), "rebound": True}
        except Exception:
            connection.rollback()
            self._discard_events()
            raise
        finally:
            connection.close()

    def release_execution_binding(self, task_id: str, *, reason: str) -> dict[str, Any]:
        """Give the runtime back, keeping the task itself exactly where it is.

        Used when the bound session turns out to be gone, unhealthy or no
        longer eligible. The task is NOT failed and NOT moved -- it simply
        becomes routable again, which is the whole difference between a task
        that is waiting for a runtime and one that is stuck."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            task = QueueTask.from_row(row)
            if task.routing_state not in ROUTING_BOUND_STATES:
                return {**task.to_dict(), "released": False}
            connection.execute(
                "UPDATE queue_tasks SET execution_session = NULL, execution_node_id = NULL, "
                "routing_state = ?, updated_at = ? WHERE id = ?",
                (UNROUTED, iso_now(), task_id),
            )
            self._record_event_locked(
                connection, session=task.session, task_id=task_id,
                event_type="TASK_ROUTE_RELEASED", reason=reason,
                metadata={"released_session": task.execution_session})
            updated = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        return {**QueueTask.from_row(updated).to_dict(), "released": True}

    def record_routing_deferral(self, task_id: str, *, evidence: dict[str, Any]) -> dict[str, Any]:
        """No runtime could be found, and here is exactly why -- per candidate.

        This is what makes an unexplained QUEUED impossible. A task that stays
        queued now carries WAITING_RUNTIME plus the rejection reason for every
        session the router looked at, so the dashboard answers "why is this
        not running" from the record instead of from a guess."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            task = QueueTask.from_row(row)
            if task.routing_state in ROUTING_BOUND_STATES:
                # Something bound it between the scoring pass and here. The
                # binding is the newer, stronger fact -- never overwrite it
                # with this pass's stale "nothing available".
                return {**task.to_dict(), "deferred": False}
            connection.execute(
                "UPDATE queue_tasks SET routing_state = ?, routing_evidence = ?, updated_at = ? WHERE id = ?",
                (WAITING_RUNTIME, json.dumps(evidence), iso_now(), task_id),
            )
            self._record_event_locked(
                connection, session=task.session, task_id=task_id,
                event_type="TASK_ROUTE_DEFERRED", reason=evidence.get("reason"),
                metadata={"candidates_considered": evidence.get("candidates_considered")})
            updated = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        return {**QueueTask.from_row(updated).to_dict(), "deferred": True}

    def _write_agent_binding_locked(self, connection: sqlite3.Connection, task_id: str, *,
                                    agent_id: str | None, skill_ids: Sequence[str] | None) -> None:
        if agent_id is not None:
            connection.execute("UPDATE queue_tasks SET agent_id = ? WHERE id = ?", (agent_id, task_id))
        if skill_ids is not None:
            connection.execute("UPDATE queue_tasks SET skill_ids = ? WHERE id = ?",
                               (json.dumps([str(skill) for skill in skill_ids]), task_id))

    def set_agent_binding(self, task_id: str, *, agent_id: str | None = None,
                          skill_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """The Agent+Skill bridge, written onto the task row itself.

        A bridge, not a registry: these columns carry whatever agent/skill
        identity a caller already has, so the router can match on it today
        without a second store existing. Phase B's Agent Registry populates
        these same columns -- it does not replace them."""
        with self._connection() as connection:
            row = connection.execute("SELECT id FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return {"error": "TASK_NOT_FOUND", "task_id": task_id}
            self._write_agent_binding_locked(connection, task_id, agent_id=agent_id, skill_ids=skill_ids)
            connection.execute("UPDATE queue_tasks SET updated_at = ? WHERE id = ?", (iso_now(), task_id))
            updated = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        return QueueTask.from_row(updated).to_dict()

    ROUTER_OWNED_METADATA_KEY = "router_owned"
    """Set by route_start on a task created with no target. It marks a task
    whose placement the ROUTER chose and may therefore choose again -- as
    opposed to one a caller deliberately put in a specific lane."""

    def routable_tasks(self, *, limit: int = 200) -> list[QueueTask]:
        """Tasks that are waiting for a runtime AND that routing is allowed to
        place. Those are two different questions, and conflating them breaks a
        safety gate this project built on purpose.

        WHAT IS IN SCOPE, AND WHY IT IS NOT "EVERY QUEUED TASK". A task sitting
        in a named lane was deliberately put there by a caller, and that lane's
        `auto_dispatch_enabled` flag is the operator's answer to "may anything
        autonomous drive this session?" -- the mechanism that keeps real
        production sessions safe (see queue_loop.py's two-gate docstring).
        Re-homing such a task would walk straight through that gate: the
        operator said "not this lane" and the router would answer "fine, a
        different one then". So a task is rescuable only when routing it takes
        nothing away from anybody:

          * it is in the UNASSIGNED lane -- nobody chose a session for it, so
            there is no decision to override. This is the case the production
            bug lived in;
          * it is WAITING_SESSION -- the session it was pinned to is gone, so
            the original choice can no longer be honoured however much we
            respect it;
          * it is router-owned -- the router placed it in the first place, so
            re-placing it changes nothing a human decided.

        ONE PER REAL LANE, because a lane is serial: the second task in a lane
        is not waiting for a runtime, it is waiting for the task ahead of it.
        Each real lane therefore contributes exactly what the engine itself
        would pick next (`_next_dispatchable_locked` -- the same function
        claim_next_task uses, so a paused lane, an already-active lane and an
        unmet dependency are honoured without a second set of rules that could
        drift from the engine's). The UNASSIGNED lane is different in kind:
        nothing in it is queued behind anything, so every unbound task there is
        routable, each potentially to a different session.

        Restart-safe by construction: every input is a durable row, so a
        controller that has just come up re-derives the identical work list
        with nothing carried across the restart.
        """
        with self._connection() as connection:
            lanes = [row["session"] for row in connection.execute(
                "SELECT DISTINCT session FROM queue_tasks WHERE status IN (?, ?) "
                "AND (routing_state IS NULL OR routing_state NOT IN (?, ?))",
                (QUEUED, WAITING_SESSION, BOUND, SPAWNED),
            ).fetchall()]
            found: list[QueueTask] = []
            for session in lanes:
                if session == UNASSIGNED_LANE:
                    rows = connection.execute(
                        "SELECT * FROM queue_tasks WHERE session = ? AND status IN (?, ?) "
                        "AND (routing_state IS NULL OR routing_state NOT IN (?, ?)) "
                        "ORDER BY priority DESC, position ASC",
                        (UNASSIGNED_LANE, QUEUED, WAITING_SESSION, BOUND, SPAWNED),
                    ).fetchall()
                    found.extend(QueueTask.from_row(row) for row in rows)
                    continue
                candidate = self._next_dispatchable_locked(connection, session)
                if candidate is None:
                    # WAITING_SESSION never reaches _next_dispatchable_locked
                    # (it filters on QUEUED). A task whose session vanished is
                    # exactly the one re-routing exists for, so pick it up here.
                    row = connection.execute(
                        "SELECT * FROM queue_tasks WHERE session = ? AND status = ? "
                        "AND (routing_state IS NULL OR routing_state NOT IN (?, ?)) "
                        "ORDER BY priority DESC, position ASC LIMIT 1",
                        (session, WAITING_SESSION, BOUND, SPAWNED),
                    ).fetchone()
                    candidate = QueueTask.from_row(row) if row is not None else None
                if candidate is None or candidate.routing_state in ROUTING_BOUND_STATES:
                    continue
                if self._is_rescuable(candidate):
                    found.append(candidate)
        found.sort(key=lambda task: (-task.priority, task.created_at, task.position))
        return found[:limit]

    @classmethod
    def _is_rescuable(cls, task: QueueTask) -> bool:
        """See routable_tasks: may the router place THIS task, or would doing
        so override somebody's explicit choice of lane?"""
        if task.session == UNASSIGNED_LANE:
            return True
        if task.status == WAITING_SESSION:
            return True
        return bool((task.metadata or {}).get(cls.ROUTER_OWNED_METADATA_KEY))
    def tasks_for_agent(self, agent_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        """Every task this AGENT owns, newest first.

        Ownership, not execution: the query is on `agent_id`, which is written
        once at creation and never moves, so a task stays findable through any
        number of session rebindings. That is the whole point of the column --
        see agent_service.py on durable identity vs disposable runtime."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM queue_tasks WHERE agent_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (agent_id, limit)).fetchall()
        return [QueueTask.from_row(row).to_dict() for row in rows]

    def get_task(self, task_id: str) -> QueueTask | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        return QueueTask.from_row(row) if row else None

    # -- durable recovery state (TMCP-RETRY-CONTEXT-002) --------------------
    # A retry can only continue work it can still SEE. Scrollback is destroyed
    # by exactly the events that cause a retry -- lease expiry, a crashed
    # engine, a dead agent process -- so the identity and progress a retry
    # needs have to be written to disk BEFORE that happens, not read off a
    # pane afterwards. These two methods are that durable record; the
    # decisions made from it live in retry_recovery.py.

    def record_recovery_state(self, task_id: str, *, identity: dict[str, Any] | None = None,
                              capsule: dict[str, Any] | None = None) -> dict[str, Any]:
        """Merge recovery identity and/or a progress capsule into the task's
        own metadata, under `metadata["recovery"]`.

        MERGE, NEVER REPLACE, and this is the whole point rather than a
        convenience. Invariant 6 of the hotfix forbids truncating or deleting
        persisted recovery state, and the realistic way that happens is not a
        DELETE -- it is a later, thinner write clobbering a richer earlier one
        (a lease-expiry snapshot that knows only the session name overwriting a
        capsule that knew five completed steps). So:

          * a key whose new value is None/empty is DROPPED from the update
            rather than written, which is what makes a partial writer safe;
          * `capsule` merges key-by-key on top of the stored capsule for the
            same reason;
          * `conversation_id` is additionally WRITE-ONCE-ish: an existing one is
            never replaced by a different value, because a resume token is the
            single most expensive thing here to lose and a mid-recovery
            relaunch is exactly when something might try.

        Returns the merged `recovery` block as stored.
        """
        def _prune(data: dict[str, Any] | None) -> dict[str, Any]:
            return {k: v for k, v in (data or {}).items() if v not in (None, "", [], {}, ())}

        with self._connection() as connection:
            row = connection.execute("SELECT metadata FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such task: {task_id}")
            try:
                metadata = json.loads(row["metadata"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            recovery = metadata.get("recovery")
            recovery = dict(recovery) if isinstance(recovery, dict) else {}

            stored_capsule = recovery.get("capsule")
            stored_capsule = dict(stored_capsule) if isinstance(stored_capsule, dict) else {}

            incoming = _prune(identity)
            # Never trade a known resume token for a different one.
            existing_conversation = recovery.get("conversation_id")
            if existing_conversation and incoming.get("conversation_id") not in (None, existing_conversation):
                incoming.pop("conversation_id", None)
            recovery.update(incoming)

            merged_capsule = {**stored_capsule, **_prune(capsule)}
            if merged_capsule:
                recovery["capsule"] = merged_capsule
            recovery["updated_at"] = iso_now()
            metadata["recovery"] = recovery
            connection.execute("UPDATE queue_tasks SET metadata = ?, updated_at = ? WHERE id = ?",
                               (json.dumps(metadata), iso_now(), task_id))
        return recovery

    def get_recovery_state(self, task_id: str) -> dict[str, Any]:
        """The persisted recovery block, or {} -- read fresh from disk on every
        call, so a NEW process (or a restarted service) sees exactly what the
        previous one wrote with no in-memory carry-over."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        recovery = (task.metadata or {}).get("recovery")
        return dict(recovery) if isinstance(recovery, dict) else {}

    def active_owner_for(self, *, task_id: str | None = None,
                         request_key: str | None = None) -> dict[str, Any] | None:
        """Who currently owns this logical task, if anyone.

        "Owns" means a task that is genuinely in flight -- claimed or already
        dispatched -- not merely present. Looked up by request_key as well as
        id because the thing that must not be duplicated is the logical
        REQUEST: the same work re-enqueued under a new task id and handed to a
        second agent is the exact duplication invariant 7 forbids, and an
        id-only check cannot see it.
        """
        in_flight = (PRECHECK, READY, DISPATCHING, RUNNING, VERIFYING)
        placeholders = ",".join("?" for _ in in_flight)
        with self._connection() as connection:
            row = None
            if task_id:
                row = connection.execute(
                    f"SELECT id, session, status, claimed_by, request_key FROM queue_tasks "
                    f"WHERE id = ? AND status IN ({placeholders})", (task_id, *in_flight)).fetchone()
            if row is None and request_key:
                row = connection.execute(
                    f"SELECT id, session, status, claimed_by, request_key FROM queue_tasks "
                    f"WHERE request_key = ? AND status IN ({placeholders})",
                    (request_key, *in_flight)).fetchone()
        if row is None:
            return None
        return {"task_id": row["id"], "session": row["session"], "status": row["status"],
                "owner": row["claimed_by"] or row["session"], "request_key": row["request_key"]}

    def next_dispatchable_task(self, session: str) -> QueueTask | None:
        """The one task queue_engine.py may dispatch right now for this
        session, or None. Deliberately enforces "only one task RUNNING
        per session at a time", "never dequeue while paused", AND item
        11's own failure policy ("task lỗi -> BLOCKED và dừng queue,
        không tự skip sang task tiếp theo") here, in the store, rather
        than trusting the engine's own poll loop to remember all three
        -- a single, race-free source of truth (this query runs inside
        the connection's own transaction). A BLOCKED task stops its
        lane exactly like an in-flight one does: nothing else in this
        session dispatches until an operator explicitly retries/skips/
        cancels it (see retry_task/skip_task/cancel_task)."""
        with self._connection() as connection:
            task = self._next_dispatchable_locked(connection, session)
        return task

    _ACTIVE_STATUSES = (PRECHECK, READY, DISPATCHING, DISPATCH_UNCERTAIN, RUNNING, VERIFYING, WAITING_SESSION,
                       PAUSED, BLOCKED, FAILED)
    """Phase 2/P0: any status in this set means the lane already has a
    task occupying it -- nothing else may be claimed/dispatched until it
    reaches a terminal state or an operator explicitly retries/skips/
    cancels it (BLOCKED/FAILED) or resumes it (PAUSED). Includes
    PRECHECK/READY (mid coordinator-gate review) and DISPATCH_UNCERTAIN/
    WAITING_SESSION (P0: outcome genuinely unknown / session
    unreachable -- still occupies the lane, never silently dropped)."""

    def _next_dispatchable_locked(self, connection: sqlite3.Connection, session: str) -> QueueTask | None:
        lane = connection.execute("SELECT paused FROM queue_lanes WHERE session = ?", (session,)).fetchone()
        if lane is None or lane["paused"]:
            return None
        placeholders = ", ".join("?" for _ in self._ACTIVE_STATUSES)
        active = connection.execute(
            f"SELECT id FROM queue_tasks WHERE session = ? AND status IN ({placeholders})",
            (session, *self._ACTIVE_STATUSES),
        ).fetchone()
        if active is not None:
            return None  # one task at a time, per session
        candidates = connection.execute(
            "SELECT * FROM queue_tasks WHERE session = ? AND status = ? ORDER BY priority DESC, position ASC",
            (session, QUEUED),
        ).fetchall()
        for row in candidates:
            task = QueueTask.from_row(row)
            if self._dependencies_satisfied_locked(connection, task):
                return task
        return None  # every QUEUED task (if any) is still waiting on an unmet dependency

    class DependencyError(ValueError):
        """A depends_on edge that would deadlock the lane forever.

        Raised at CREATION time, never at dispatch. The dispatch check
        (_dependencies_satisfied_locked) is deliberately fail-closed: an
        unsatisfiable dependency simply never becomes dispatchable. That is
        the right behaviour for a dependency that is merely NOT DONE YET,
        and exactly the wrong behaviour for one that can never be satisfied
        -- a cycle, or an id that does not exist. Those produce a lane that
        is silently, permanently idle: no event, no error, no alarm. So they
        are refused up front instead."""

    def dependency_cycle(self, task_id: str, depends_on: Sequence[str]) -> list[str] | None:
        """The cycle `task_id` would join by depending on `depends_on`, or
        None. Returns the actual path so an operator sees WHICH tasks form
        it rather than just "cycle detected"."""
        with self._connection() as connection:
            return self._dependency_cycle_locked(connection, task_id, list(depends_on or ()))

    def _dependency_cycle_locked(self, connection: sqlite3.Connection, task_id: str,
                                 depends_on: list[str]) -> list[str] | None:
        # Iterative DFS from each proposed edge back through existing
        # depends_on rows. Iterative, not recursive: a deep chain must not
        # blow the Python stack inside a write transaction.
        for first in depends_on:
            stack: list[tuple[str, list[str]]] = [(first, [task_id, first])]
            seen: set[str] = set()
            while stack:
                current, path = stack.pop()
                if current == task_id:
                    return path
                if current in seen:
                    continue
                seen.add(current)
                row = connection.execute(
                    "SELECT depends_on FROM queue_tasks WHERE id = ?", (current,)).fetchone()
                if row is None:
                    continue
                for nxt in _parse_json_list(row["depends_on"]):
                    stack.append((nxt, [*path, nxt]))
        return None

    def validate_dependencies(self, task_id: str, depends_on: Sequence[str], *,
                              require_existing: bool = True) -> None:
        """Refuse a depends_on set that can never be satisfied. Raises
        DependencyError; returns None when the edges are sound.

        `require_existing` is an escape hatch for a caller that legitimately
        creates a batch of interdependent tasks and cannot order them --
        it still checks for cycles, only skipping the existence check."""
        wanted = [str(d) for d in (depends_on or ()) if str(d)]
        if not wanted:
            return
        if task_id in wanted:
            raise self.DependencyError(f"{task_id} cannot depend on itself")
        with self._connection() as connection:
            if require_existing:
                missing = [d for d in wanted if connection.execute(
                    "SELECT 1 FROM queue_tasks WHERE id = ?", (d,)).fetchone() is None]
                if missing:
                    raise self.DependencyError(
                        f"depends_on references task(s) that do not exist: {', '.join(missing)} -- "
                        f"a missing dependency is treated as unmet forever, so the task would "
                        f"never dispatch")
            cycle = self._dependency_cycle_locked(connection, task_id, wanted)
        if cycle:
            raise self.DependencyError("dependency cycle: " + " -> ".join(cycle))

    def dependency_deadlocks(self) -> list[dict[str, Any]]:
        """Diagnostic over EXISTING rows: every non-terminal task whose
        dependencies can never be satisfied, and why. This is what turns a
        silently-idle lane into an answerable question."""
        stuck = []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, session, status, depends_on FROM queue_tasks "
                "WHERE depends_on != '[]' AND depends_on IS NOT NULL").fetchall()
            for row in rows:
                if row["status"] in TERMINAL_STATUSES:
                    continue
                deps = _parse_json_list(row["depends_on"])
                cycle = self._dependency_cycle_locked(connection, row["id"], deps)
                missing = [d for d in deps if connection.execute(
                    "SELECT 1 FROM queue_tasks WHERE id = ?", (d,)).fetchone() is None]
                if cycle or missing:
                    stuck.append({"task_id": row["id"], "session": row["session"],
                                  "status": row["status"], "cycle": cycle,
                                  "missing_dependencies": missing})
        return stuck

    def _dependencies_satisfied_locked(self, connection: sqlite3.Connection, task: QueueTask) -> bool:
        """Phase 2 dependency gating (item 3's own "task kế có đủ
        prerequisite... phụ thuộc task chưa xong không"): a purely
        mechanical, deterministic claim-time filter -- NOT something the
        Coordinator Agent has to reason about. A declared dependency
        (task.depends_on, a list of task_ids -- may reference a task in
        a DIFFERENT session/lane) must be COMPLETED for this task to
        even become a dispatch candidate; a missing dependency id is
        treated as unmet (fail-closed, never silently ignored). Same-
        lane dependencies are normally redundant with strict FIFO
        ordering (the prior task must already be COMPLETED for THIS one
        to ever be reached), but a same-lane id is checked identically
        for correctness if ever declared explicitly."""
        for dep_id in task.depends_on:
            dep_row = connection.execute("SELECT status FROM queue_tasks WHERE id = ?", (dep_id,)).fetchone()
            if dep_row is None or dep_row["status"] != COMPLETED:
                return False
        return True

    def transition_task(self, task_id: str, to_status: str, *, event_type: str, reason: str | None = None,
                        extra_fields: dict[str, Any] | None = None) -> QueueTask:
        """The ONLY way any task's status ever changes -- validates
        (from_status -> to_status) against VALID_TRANSITIONS, raising
        InvalidTransitionError rather than silently applying an
        impossible transition (e.g. a double-dispatch race landing
        DISPATCHING -> RUNNING twice, or a stale engine tick trying to
        complete an already-CANCELLED task). Always paired with a
        queue_events row, in the same transaction -- an event with no
        corresponding row update, or vice versa, can never happen."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such task: {task_id}")
            from_status = row["status"]
            updated = self._transition_locked(connection, task_id, from_status, to_status,
                                              event_type=event_type, reason=reason, extra_fields=extra_fields)
        return updated

    def _transition_locked(self, connection: sqlite3.Connection, task_id: str, from_status: str, to_status: str, *,
                           event_type: str, reason: str | None, extra_fields: dict[str, Any] | None = None) -> QueueTask:
        if not is_valid_transition(from_status, to_status):
            raise InvalidTransitionError(f"{task_id}: {from_status} -> {to_status} is not a valid transition")
        now = iso_now()
        fields = {"status": to_status, "updated_at": now}
        if to_status == RUNNING and from_status != VERIFYING:
            fields["started_at"] = now
        if to_status in (COMPLETED,):
            fields["completed_at"] = now
        if to_status == DISPATCHING:
            fields["attempt_count"] = connection.execute(
                "SELECT attempt_count FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()["attempt_count"] + 1
        if reason is not None and to_status in (BLOCKED, FAILED):
            fields["last_error"] = reason
        if extra_fields:
            fields.update(extra_fields)
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        connection.execute(f"UPDATE queue_tasks SET {set_clause} WHERE id = ?", (*fields.values(), task_id))
        row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        self._record_event_locked(connection, session=row["session"], task_id=task_id, event_type=event_type,
                                  reason=reason, from_status=from_status, to_status=to_status)
        # Worktree Janitor P1 (docs/WORKTREE_JANITOR.md §2 "The lifecycle
        # chokepoint"): this is the ONLY place a task's status changes, so it
        # is the only correct hook site -- on_completed has two call sites and
        # would leave the other path silently unmarked. Written inside the SAME
        # transaction as the status update, so a crash cannot leave the status
        # and the cleanup record disagreeing. MARKING ONLY: nothing deletes.
        row = self._apply_worktree_cleanup_locked(
            connection, row, from_status=from_status, to_status=to_status, now=now)
        return QueueTask.from_row(row)

    def _apply_worktree_cleanup_locked(self, connection: sqlite3.Connection, row: sqlite3.Row, *,
                                       from_status: str, to_status: str, now: str) -> sqlite3.Row:
        """Mark or clear this task's worktree-cleanup record. Never deletes."""
        metadata = _parse_json_object(row["metadata"])
        decision = wj.decide(
            from_status=from_status, to_status=to_status, metadata=metadata,
            attempt_count=row["attempt_count"], max_attempts=row["max_attempts"],
            terminal_statuses=TERMINAL_STATUSES)
        if not decision.changes_record:
            return row
        updated = wj.apply(metadata, decision, now=now)
        connection.execute("UPDATE queue_tasks SET metadata = ? WHERE id = ?",
                           (json.dumps(updated), row["id"]))
        self._record_event_locked(connection, session=row["session"], task_id=row["id"],
                                  event_type=decision.event_type, reason=decision.reason)
        return connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (row["id"],)).fetchone()

    # -- Phase 2: atomic claim + Coordinator Agent gate + reconciliation ---

    def claim_next_task(self, session: str, *, claimed_by: str, lease_seconds: float = 300.0) -> QueueTask | None:
        """The ONLY entry point queue_engine.py uses to pick up a new
        task -- QUEUED -> PRECHECK, atomically. Fixes a real Phase 1
        design gap found during this Phase 2's own safety audit (item:
        "Nếu phát hiện Phase 1 còn flaw ảnh hưởng safety thì sửa trước
        Phase 2"): Phase 1's next_dispatchable_task was READ-ONLY by
        design (no engine existed yet to race against), so a caller
        reading it and then separately calling transition_task had a
        genuine TOCTOU window -- two concurrent engine workers (or two
        ticks of the same one, re-entered) could both read the same
        QUEUED task before either had transitioned it, and both attempt
        to dispatch it. This method closes that window: `BEGIN
        IMMEDIATE` acquires SQLite's write lock BEFORE the read, so a
        second concurrent caller blocks (then sees the now-PRECHECK
        state and correctly gets None) rather than racing. `claimed_by`
        and a fresh claim_token/lease_expires_at are stamped in the SAME
        transaction as the QUEUED -> PRECHECK transition -- this is the
        durable claim/lease item 8 asks for, and what
        reconcile_stale_claims uses to detect an engine that crashed
        mid-claim."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            task = self._next_dispatchable_locked(connection, session)
            if task is None:
                connection.rollback()
                self._discard_events()
                return None
            claim_token = new_task_id()
            lease_expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             time.gmtime(time.time() + lease_seconds))
            updated = self._transition_locked(
                connection, task.id, task.status, PRECHECK, event_type="CLAIMED", reason=None,
                extra_fields={"claimed_by": claimed_by, "claim_token": claim_token,
                             "lease_expires_at": lease_expires_at},
            )
            connection.commit()
            self._drain_events()
            return updated
        except Exception:
            connection.rollback()
            self._discard_events()
            raise
        finally:
            connection.close()

    def list_isolated_worktree_paths(self, *, limit: int = 10_000) -> set[str]:
        """Every worktree path ANY task claims, in ANY status.

        Deliberately NOT derived from list_worktree_cleanup_tasks: that one only
        returns tasks which already carry a cleanup record, so a task that is
        still RUNNING -- the most important one to protect -- would be absent and
        its worktree would look unclaimed to the orphan sweep. Keyed on
        git_isolation.worktree_path, which every isolated task has from the
        moment it is created.

        Read-only and bounded. Returns a set because the only question asked of
        it is membership."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT json_extract(metadata, '$.git_isolation.worktree_path') AS path "
                    "FROM queue_tasks "
                    "WHERE json_extract(metadata, '$.git_isolation.worktree_path') IS NOT NULL "
                    "LIMIT ?", (int(limit),)).fetchall()
        except sqlite3.Error:
            _LOGGER.warning("could not list isolated worktree paths", exc_info=True)
            raise
        return {row["path"] for row in rows if row["path"]}

    def list_worktree_cleanup_tasks(self, *, states: tuple[str, ...] = (),
                                    limit: int = 200) -> list[dict[str, Any]]:
        """Tasks carrying a worktree-cleanup record, optionally filtered by
        state. READ-ONLY, bounded, and the janitor sweep's only way in.

        Filtered in SQL with json_extract rather than by loading every task and
        sifting in Python: the sweep runs on a timer against a store that grows
        forever, and "read everything then discard most of it" is how a
        background loop quietly becomes the most expensive thing on the box.

        Returns plain dicts (not QueueTask) carrying exactly what the sweep
        needs -- id, status, attempt_count, max_attempts, metadata and the
        derived terminal_at -- so the executor's `task` contract is satisfied
        without the caller re-reading each row."""
        if limit <= 0:
            return []
        sql = ["SELECT id, session, status, attempt_count, max_attempts, metadata, "
               "completed_at, updated_at FROM queue_tasks "
               "WHERE json_extract(metadata, '$.worktree_cleanup.state') IS NOT NULL"]
        params: list[Any] = []
        if states:
            placeholders = ",".join("?" for _ in states)
            sql.append(f"AND json_extract(metadata, '$.worktree_cleanup.state') IN ({placeholders})")
            params.extend(states)
        sql.append("ORDER BY updated_at ASC LIMIT ?")
        params.append(int(limit))
        try:
            with self._connection() as connection:
                rows = connection.execute(" ".join(sql), params).fetchall()
        except sqlite3.Error:
            # A malformed metadata blob makes json_extract raise for the whole
            # query. A sweep that cannot read is a sweep that does nothing --
            # never one that crashes the loop.
            _LOGGER.warning("could not list worktree cleanup tasks", exc_info=True)
            return []
        tasks = []
        for row in rows:
            metadata = _parse_json_object(row["metadata"])
            record = metadata.get(wj.METADATA_KEY) or {}
            tasks.append({
                "id": row["id"], "session": row["session"], "status": row["status"],
                "attempt_count": row["attempt_count"], "max_attempts": row["max_attempts"],
                "metadata": metadata,
                # The executor's grace check wants a timestamp. completed_at is
                # the real terminal moment when there is one; updated_at is the
                # honest fallback for SKIPPED/CANCELLED, which do not set it.
                "terminal_at": _epoch_or_none(row["completed_at"] or row["updated_at"]),
                "cleanup_state": record.get("state"),
                "worktree_path": (metadata.get(wj.ISOLATION_KEY) or {}).get("worktree_path"),
                "repo_path": (metadata.get(wj.ISOLATION_KEY) or {}).get("repo_path"),
            })
        return tasks

    def patch_worktree_cleanup(self, task_id: str, patch: dict[str, Any]) -> None:
        """Merge fields into a task's worktree-cleanup record (P2's executor).

        Kept here rather than in the executor because the metadata column is
        this store's own, and a read-modify-write of it belongs inside the
        store's connection. Merges rather than replaces: the executor updates
        state/attempts/reclaimed_bytes without having to know, or preserve,
        every field P1 wrote.

        Never raises: the directory's real state is the truth, and a failed
        metadata write is reconciled by the next sweep. Turning a completed
        removal into a reported failure because a bookkeeping UPDATE lost a
        race would be strictly worse."""
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT metadata FROM queue_tasks WHERE id = ?",
                                         (task_id,)).fetchone()
                if row is None:
                    return
                metadata = _parse_json_object(row["metadata"])
                record = metadata.get(wj.METADATA_KEY)
                record = dict(record) if isinstance(record, dict) else {}
                record.update(patch)
                metadata[wj.METADATA_KEY] = record
                connection.execute("UPDATE queue_tasks SET metadata = ? WHERE id = ?",
                                   (json.dumps(metadata), task_id))
        except Exception:  # noqa: BLE001 -- bookkeeping must not mask the real outcome
            _LOGGER.warning("could not patch worktree cleanup for %s", task_id, exc_info=True)

    def record_delivery_verdict(self, task_id: str, verdict: dict[str, Any]) -> None:
        """Record one prompt-delivery verdict on the task (delivery_gate.py).

        Written into the EXISTING metadata JSON column under
        `delivery_verdict`, deliberately without a migration: this is
        diagnostic evidence, not something dispatch queries or indexes, and
        adding a column to advance an advisory-by-default feature would be a
        schema change nobody needs yet. If a future phase needs to QUERY
        verdicts, that is the point to add a tracked Migration.

        Only the LAST verdict is kept, plus a bounded history of the last
        few, so a task retried many times cannot grow its metadata without
        limit. Never raises: an unrecordable verdict must not fail a
        dispatch -- the verdict's own effect on the transition is decided by
        the caller and does not depend on this write succeeding.

        Shares the metadata column with the worktree-janitor cleanup record
        (wj.METADATA_KEY), which is why this reads-modifies-writes the whole
        object through the same _parse_json_object helper the janitor uses
        instead of json.loads'ing its own way: two writers with two parsers
        on one column is how one of them silently drops the other's key."""
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT metadata FROM queue_tasks WHERE id = ?",
                                         (task_id,)).fetchone()
                if row is None:
                    return
                metadata = _parse_json_object(row["metadata"])
                entry = {**verdict, "at": iso_now()}
                metadata["delivery_verdict"] = entry
                history = metadata.get("delivery_verdict_history")
                if not isinstance(history, list):
                    history = []
                history.append(entry)
                metadata["delivery_verdict_history"] = history[-5:]
                connection.execute("UPDATE queue_tasks SET metadata = ? WHERE id = ?",
                                   (json.dumps(metadata), task_id))
        except Exception:  # noqa: BLE001 -- diagnostics must never break dispatch
            _LOGGER.warning("could not record delivery verdict for %s", task_id, exc_info=True)

    def record_coordinator_decision(self, task_id: str, *, status: str, reason: str,
                                    blockers: list[str] | None = None, required_actions: list[str] | None = None,
                                    evidence: dict[str, Any] | None = None) -> QueueTask:
        """Applies one Coordinator Agent review outcome (task item 4):
        status is one of READY | BLOCKED | NEEDS_REWORK | NEEDS_HUMAN
        (the Coordinator's own vocabulary -- distinct from, and mapped
        onto, this store's task-status vocabulary right here, in ONE
        place, so the mapping is never duplicated/drifted between
        callers):
          READY        -> PRECHECK -> READY (task dispatchable next tick)
          BLOCKED      -> PRECHECK -> BLOCKED (stops this lane; explicit
                          retry/skip/cancel only)
          NEEDS_REWORK -> PRECHECK -> QUEUED (goes back to the back of
                          the "ready to reconsider" line -- the caller is
                          expected to have enqueued/prioritized a
                          remediation task ahead of it first; see item 5)
          NEEDS_HUMAN  -> PRECHECK -> PAUSED, and pauses the WHOLE lane
                          (item 6: "dừng queue session đó") -- a plain
                          resume_lane is what un-pauses it once a human
                          has acted.
        The full decision (status/reason/blockers/required_actions/
        evidence) is stored verbatim on the task (coordinator_decision,
        JSON) for audit/dashboard -- evidence is expected to already be
        redacted by the caller (CoordinatorGate) per this project's
        existing redaction policy before it ever reaches here; this
        method does not itself redact anything."""
        decision = {"status": status, "reason": reason, "blockers": blockers or [],
                    "required_actions": required_actions or [], "evidence": evidence or {}}
        target_status = {"READY": READY, "BLOCKED": BLOCKED, "NEEDS_REWORK": QUEUED,
                         "NEEDS_HUMAN": PAUSED}.get(status)
        if target_status is None:
            raise ValueError(f"unknown coordinator decision status: {status!r}")
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such task: {task_id}")
            now = iso_now()
            extra = {
                "coordinator_decision": json.dumps(decision), "coordinator_reason": reason,
                "coordinator_checked_at": now, "coordinator_attempts": row["coordinator_attempts"] + 1,
            }
            if target_status in (QUEUED, BLOCKED):
                # NEEDS_REWORK (-> QUEUED) / BLOCKED: the claim this
                # PRECHECK review was holding is no longer active -- clear
                # it so reconcile_stale_claims never mistakes this for an
                # abandoned-but-still-claimed task. (target_status ==
                # READY is the one case that deliberately KEEPS the claim:
                # the same claimed_by/claim_token/lease continues straight
                # into dispatch.)
                extra.update({"claimed_by": None, "claim_token": None, "lease_expires_at": None})
            if target_status == PAUSED:
                # NEEDS_HUMAN: resume_lane should hand this back to QUEUED
                # for a FRESH claim + coordinator review (paused_from_status
                # = QUEUED), never straight back into PRECHECK holding a
                # now-stale claim_token/lease -- also clear the stale claim
                # itself so a reconcile pass never mistakes this for an
                # abandoned-but-still-claimed task.
                extra.update({"paused_from_status": QUEUED, "claimed_by": None, "claim_token": None,
                             "lease_expires_at": None})
            updated = self._transition_locked(connection, task_id, row["status"], target_status,
                                              event_type="COORDINATOR_DECISION", reason=reason, extra_fields=extra)
            self._record_event_locked(connection, session=row["session"], task_id=task_id,
                                      event_type=f"COORDINATOR_{status}", reason=reason, metadata=decision)
            if status == "NEEDS_HUMAN":
                # Item 6: NEEDS_HUMAN stops this session's queue but never
                # another session's -- pause_lane only ever touches the
                # ONE session named here. The task itself is already
                # PAUSED (above, in the SAME transaction) so
                # _pause_lane_locked's own active-task sweep will not
                # find/re-touch it -- it only sets the lane's own paused
                # flag at this point.
                self._pause_lane_locked(connection, row["session"], reason=f"coordinator: {reason}",
                                        origin=PAUSE_ORIGIN_COORDINATOR)
        return updated

    # ------------------------------------------------- P0.4 task lease API
    # claim_next_task already stamps claim_token + lease_expires_at
    # atomically; what was missing is everything AFTER the claim. Note
    # reconcile_stale_claims' own docstring already assumed "a healthy
    # engine keeps renewing ... well within the lease" -- renew_task_lease
    # is the method that assumption was written against and which did not
    # exist until now. Without it a genuinely-alive worker on a long task
    # silently loses its claim at TTL and gets reconciled out from under
    # itself.
    #
    # Every verb here requires the CURRENT claim_token: a holder whose
    # lease already expired and was reclaimed by someone else can never
    # renew, release or hand off the new holder's work. Same rule as
    # event_bus.ack and lease.PaneLeaseStore.

    LEASE_STATES = (PRECHECK, DISPATCHING, RUNNING, VERIFYING)

    def renew_task_lease(self, task_id: str, claim_token: str, *,
                         lease_seconds: float = 300.0) -> QueueTask | None:
        """Extend an ACTIVE claim. Returns the updated task, or None if the
        token does not match the current holder (or the task is no longer
        in a leaseable state) -- never raises for a lost race, because
        losing a lease is an ordinary outcome a worker must handle."""
        new_expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                   time.gmtime(time.time() + lease_seconds))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM queue_tasks WHERE id = ? AND claim_token = ?",
                (task_id, claim_token)).fetchone()
            if row is None or row["status"] not in self.LEASE_STATES:
                connection.rollback()
                self._discard_events()
                return None
            connection.execute(
                "UPDATE queue_tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND claim_token = ?",
                (new_expiry, iso_now(), task_id, claim_token))
            connection.commit()
            self._drain_events()
        except Exception:
            connection.rollback()
            self._discard_events()
            raise
        finally:
            connection.close()
        return self.get_task(task_id)

    def release_task_claim(self, task_id: str, claim_token: str, *,
                           reason: str | None = None) -> QueueTask | None:
        """Give a claim back BEFORE its TTL expires -- the graceful form of
        what reconcile_stale_claims does forcibly after a crash. The task
        returns to QUEUED with its claim fields cleared, exactly the shape
        reconciliation produces, so a released task and a reconciled one
        are indistinguishable downstream.

        Returns None if the token is not the current holder's."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM queue_tasks WHERE id = ? AND claim_token = ?",
                (task_id, claim_token)).fetchone()
            if row is None or row["status"] not in self.LEASE_STATES:
                connection.rollback()
                self._discard_events()
                return None
            updated = self._transition_locked(
                connection, task_id, row["status"], QUEUED,
                event_type="CLAIM_RELEASED", reason=reason or "claim released by holder",
                extra_fields={"claimed_by": None, "claim_token": None, "lease_expires_at": None})
            connection.commit()
            self._drain_events()
            return updated
        except Exception:
            connection.rollback()
            self._discard_events()
            raise
        finally:
            connection.close()

    def handoff_task(self, task_id: str, claim_token: str, *, to_worker: str,
                     to_session: str | None = None, reason: str,
                     lease_seconds: float = 300.0) -> QueueTask | None:
        """Transfer an ACTIVE claim to another worker without the task ever
        returning to the queue -- the verb worker -> verifier needs.

        Distinct from reassign_task on purpose: that moves a task's LANE
        and deliberately REFUSES a task that is actively claimed
        (TaskAlreadyClaimedError), which is exactly the case here. This
        moves the CLAIM, keeps the same task_id/prompt/attempt_count, and
        appends to the same append-only `migration_history` column so the
        provenance trail stays in one place rather than two.

        `to_session` is optional: handing work to a verifier on the SAME
        lane is the common case, and moving lanes as well is allowed but
        never implied."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, session, claimed_by, migration_history FROM queue_tasks "
                "WHERE id = ? AND claim_token = ?", (task_id, claim_token)).fetchone()
            if row is None or row["status"] not in self.LEASE_STATES:
                connection.rollback()
                self._discard_events()
                return None
            # _parse_json_dict_list, NOT _parse_json_list: migration_history
            # holds DICTS (reassign_task writes them the same way). The list
            # variant coerces every element with str(), which turned each
            # prior entry into a Python repr string that QueueTask.from_row
            # then silently dropped -- so the first handoff of a task erased
            # all of its earlier migration/handoff provenance.
            history = _parse_json_dict_list(row["migration_history"])
            history.append({"at": iso_now(), "event": "handoff",
                            "from_worker": row["claimed_by"], "to_worker": to_worker,
                            "from_session": row["session"],
                            "to_session": to_session or row["session"], "reason": reason})
            new_token = new_task_id()
            new_expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(time.time() + lease_seconds))
            connection.execute(
                "UPDATE queue_tasks SET claimed_by = ?, claim_token = ?, lease_expires_at = ?, "
                "session = ?, migration_history = ?, updated_at = ? WHERE id = ? AND claim_token = ?",
                (to_worker, new_token, new_expiry, to_session or row["session"],
                 json.dumps(history), iso_now(), task_id, claim_token))
            self._record_event_locked(connection, session=row["session"], task_id=task_id,
                                      event_type="CLAIM_HANDOFF",
                                      reason=f"{row['claimed_by']} -> {to_worker}: {reason}")
            connection.commit()
            self._drain_events()
        except Exception:
            connection.rollback()
            self._discard_events()
            raise
        finally:
            connection.close()
        return self.get_task(task_id)

    def lease_holder(self, task_id: str) -> dict[str, Any] | None:
        """Who currently holds this task and until when -- the read a
        supervisor/dashboard needs without exposing the token itself."""
        task = self.get_task(task_id)
        if task is None or not task.claim_token:
            return None
        return {"task_id": task.id, "claimed_by": task.claimed_by,
                "lease_expires_at": task.lease_expires_at, "status": task.status,
                "session": task.session}

    def reconcile_stale_claims(self, session: str | None = None, *, now: str | None = None) -> list[str]:
        """Restart-safe reconciliation (item 8): a task stuck in
        PRECHECK or DISPATCHING past its own lease_expires_at means the
        engine instance that claimed it died (process crash, node-agent
        restart, ...) before finishing that step -- NOT that the step
        itself is still safely in progress (a healthy engine keeps
        renewing/finishing well within the lease). PRECHECK is always
        safe to reconcile straight back to QUEUED (nothing was ever sent
        to any session in that state). DISPATCHING is reconciled to
        QUEUED too -- this deliberately does NOT re-check delivery
        evidence here; that is queue_engine.py's own job on its next
        claim of the task (it re-derives an idempotency_key from
        (task_id, attempt_count) before ever calling terminal_send_text
        again, so even if the original send DID go through, the durable
        idempotency store -- core.py's terminal_send_text idempotency_
        key -- returns the original result instead of sending twice;
        see queue_engine.py's own module docstring). Returns the ids
        actually reconciled."""
        now = now or iso_now()
        with self._connection() as connection:
            clause = "session = ? AND " if session else ""
            params: tuple[Any, ...] = (session,) if session else ()
            rows = connection.execute(
                f"SELECT id, status FROM queue_tasks WHERE {clause}status IN (?, ?) "
                f"AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (*params, PRECHECK, DISPATCHING, now),
            ).fetchall()
            reconciled = []
            for row in rows:
                self._transition_locked(connection, row["id"], row["status"], QUEUED,
                                        event_type="RECOVERED_AFTER_RESTART",
                                        reason="stale lease reconciled after restart",
                                        extra_fields={"claimed_by": None, "claim_token": None,
                                                     "lease_expires_at": None})
                reconciled.append(row["id"])
        return reconciled

    def mark_dispatch_uncertain(self, task_id: str, *, reason: str) -> QueueTask:
        """DISPATCHING -> DISPATCH_UNCERTAIN, stamping the grace-period
        clock (item 2). Called by queue_engine.py the moment a send
        comes back delivery_state == DELIVERY_UNKNOWN -- never QUEUED
        directly anymore, so this genuinely-uncertain outcome is never
        silently indistinguishable from an ordinary still-waiting task."""
        return self.transition_task(task_id, DISPATCH_UNCERTAIN, event_type="SUBMIT_UNKNOWN", reason=reason,
                                    extra_fields={"uncertain_or_waiting_since": iso_now()})

    def mark_waiting_session(self, task_id: str, *, reason: str) -> QueueTask:
        """Any active status -> WAITING_SESSION, stamping the grace-
        period clock (item 9) -- called when the task's own target
        session/node cannot be resolved at all (SESSION_NOT_FOUND/
        NODE_UNREACHABLE/AMBIGUOUS_SESSION), which is routine and
        auto-recoverable, never a reason to drop the task or treat it
        as a coordinator/execution failure."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return self.transition_task(task_id, WAITING_SESSION, event_type="WAITING_SESSION", reason=reason,
                                    extra_fields={"uncertain_or_waiting_since": iso_now()})

    def reconcile_stale_lane_pause(self, session: str | None = None) -> list[str]:
        """Clear a COORDINATOR lane pause that no longer guards anything.

        A coordinator pause is not a standing instruction -- it is a guard put
        around ONE task the gate refused to dispatch (record_coordinator_
        decision's NEEDS_HUMAN path), and that task is left PAUSED for a human
        to look at. Once no task on the lane is PAUSED any more -- it was
        cancelled, skipped, completed or retried -- the guard has nothing left
        to protect, and every later task enqueued into that lane is stranded
        behind it. Nothing in the system used to revisit that: resume_lane is
        only ever called by a human, and the engine's tick returns PAUSED
        before it looks at any task.

        Deliberately narrow, in three ways:
          * ONLY PAUSE_ORIGIN_COORDINATOR. A user pause (a standing operator
            instruction) and a project pause (released by the project) are
            never touched, and an UNKNOWN origin is treated as not-clearable
            -- clearing a pause somebody set deliberately is the one failure
            mode worth designing against.
          * "Nothing left to guard" is read off real task rows, never a timer.
            A pause whose task is still PAUSED stays, however old it is.
          * It clears the LANE flag only. No task is dispatched, nothing is
            sent to any session; a lane with auto_dispatch_enabled=0 simply
            becomes claimable again by an explicit terminal_queue_run_once.

        Idempotent: a lane already un-paused matches nothing, so repeat sweeps
        write no rows and emit no events. Returns the sessions actually
        reconciled.
        """
        reconciled: list[str] = []
        with self._connection() as connection:
            clause = "AND session = ? " if session else ""
            params: tuple[Any, ...] = (session,) if session else ()
            rows = connection.execute(
                f"SELECT session, paused_reason, paused_origin FROM queue_lanes "
                f"WHERE paused = 1 {clause}",
                params,
            ).fetchall()
            for row in rows:
                if pause_origin(row["paused_origin"], row["paused_reason"]) != PAUSE_ORIGIN_COORDINATOR:
                    continue
                still_guarding = connection.execute(
                    "SELECT 1 FROM queue_tasks WHERE session = ? AND status = ? LIMIT 1",
                    (row["session"], PAUSED),
                ).fetchone()
                if still_guarding is not None:
                    continue
                connection.execute(
                    "UPDATE queue_lanes SET paused = 0, paused_reason = NULL, paused_origin = NULL, "
                    "updated_at = ? WHERE session = ?",
                    (iso_now(), row["session"]),
                )
                self._record_event_locked(
                    connection, session=row["session"], task_id=None,
                    event_type="LANE_PAUSE_RECONCILED",
                    reason=("coordinator pause no longer guards any task "
                            f"(was: {row['paused_reason'] or 'no reason recorded'})"),
                )
                reconciled.append(row["session"])
        return reconciled

    # ------------------------------------------------- AI-owned reconciliation
    # TMCP-AI-OWNS-AI-REVIEW-001. Every sweep below is something the AI owns
    # under ai_review.py's ownership policy, is idempotent, is bounded, and
    # records actor=ai_reconciler so the audit log says WHO acted -- an
    # automatic recovery and an operator's retry must never be
    # indistinguishable after the fact.

    def reclaim_stale_verify_leases(self, *, now: str | None = None) -> list[str]:
        """Return verify jobs whose verifier lease expired to VERIFY_PENDING.

        A verify job carries lease_expires_at but nothing ever swept it, so a
        verifier that died mid-check parked its task in VERIFYING forever --
        the one state whose only other exit is a human calling
        terminal_queue_verify. Reclaiming makes the job claimable again by any
        capable verifier, which is AI-owned recovery (ai_review item 1), and
        loses nothing: the task stays in VERIFYING and is never completed
        without evidence.

        Idempotent: a job already PENDING has no lease to expire.
        """
        stamp = now or iso_now()
        reclaimed: list[str] = []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, task_id, session, status, verifier, claim_count FROM verify_jobs "
                "WHERE status IN ('VERIFY_CLAIMED', 'VERIFY_RUNNING') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (stamp,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE verify_jobs SET status = 'VERIFY_PENDING', claim_token = NULL, "
                    "lease_expires_at = NULL, verifier = NULL, verifier_node_id = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (stamp, row["id"]),
                )
                self._record_event_locked(
                    connection, session=row["session"], task_id=row["task_id"],
                    event_type="AI_VERIFY_LEASE_RECLAIMED",
                    from_status=row["status"], to_status="VERIFY_PENDING",
                    reason=(f"verifier lease expired (verifier={row['verifier'] or 'unknown'}); "
                            "job returned to the pool for another capable verifier"),
                    metadata={"actor": "ai_reconciler", "verify_job_id": row["id"],
                              "claim_count": row["claim_count"]},
                )
                reclaimed.append(row["id"])
        return reclaimed

    def reevaluate_ai_owned_blocked(self, *, now: str | None = None,
                                    session: str | None = None) -> list[str]:
        """Re-queue a BLOCKED task the AI owns, once its backoff has elapsed.

        BLOCKED is AI-owned by default (ai_review item 3): a coordinator
        refusal is a diagnosis to work through, not a ticket to hand over. A
        stale refusal must be RE-EVALUATED rather than living forever, so this
        returns the task to QUEUED for a fresh claim + coordinator review.

        Three bounds, all deliberate:
          * A task whose refusal encodes one of the four true approval classes
            (ai_review.approval_class_for_task) is NEVER touched -- it belongs
            to a human and re-queueing it would re-refuse it in a loop.
          * Bounded exponential backoff off the last coordinator check, so a
            task that keeps being refused is retried at 30s, 60s, ... capped at
            15 minutes rather than every cycle.
          * At AI_MAX_AUTOMATIC_ATTEMPTS the automatic retry STOPS. The task
            stays AI-owned and visible in AI Review with next_action
            diagnose_and_reroute -- retry exhaustion is explicitly not an
            escalation to a human.

        Idempotent: a task already QUEUED is not BLOCKED and matches nothing.
        """
        from .ai_review import (AI_MAX_AUTOMATIC_ATTEMPTS, approval_class_for_task,
                               backoff_seconds)

        stamp = now or iso_now()
        now_epoch = _epoch_or_none(stamp)
        requeued: list[str] = []
        with self._connection() as connection:
            clause = "AND session = ? " if session else ""
            params: tuple[Any, ...] = (BLOCKED, session) if session else (BLOCKED,)
            rows = connection.execute(
                f"SELECT * FROM queue_tasks WHERE status = ? {clause}", params,
            ).fetchall()
            for row in rows:
                task = dict(row)
                if approval_class_for_task(task) is not None:
                    continue  # a human owns this one
                attempts = max(int(task.get("coordinator_attempts") or 0),
                               int(task.get("attempt_count") or 0))
                if attempts >= AI_MAX_AUTOMATIC_ATTEMPTS:
                    continue  # stop retrying; AI Review keeps it with a diagnose next_action
                last = _epoch_or_none(task.get("coordinator_checked_at") or task.get("updated_at"))
                if now_epoch is not None and last is not None and \
                        now_epoch - last < backoff_seconds(attempts):
                    continue  # still inside its backoff window
                self._transition_locked(
                    connection, task["id"], BLOCKED, QUEUED,
                    event_type="AI_BLOCKED_REEVALUATED",
                    reason=(f"re-evaluating a stale coordinator refusal after {attempts} attempt(s): "
                            f"{(task.get('coordinator_reason') or 'no reason recorded')[:160]}"),
                    extra_fields={"claimed_by": None, "claim_token": None, "lease_expires_at": None},
                )
                self._record_event_locked(
                    connection, session=task["session"], task_id=task["id"],
                    event_type="AI_RECONCILED", from_status=BLOCKED, to_status=QUEUED,
                    reason="AI Review owns BLOCKED; refusal re-evaluated rather than left to expire",
                    metadata={"actor": "ai_reconciler", "attempts": attempts},
                )
                requeued.append(task["id"])
        return requeued

    def reconcile_uncertain_and_waiting(self, session: str | None = None, *, grace_seconds: float = 60.0,
                                        now: str | None = None) -> list[str]:
        """Restart-safe AND ordinary-operation reconciliation (item 2/9):
        a DISPATCH_UNCERTAIN or WAITING_SESSION task older than
        grace_seconds (by its own uncertain_or_waiting_since clock, which
        survives a restart same as lease_expires_at does) falls back to
        QUEUED for a fresh attempt -- WAITING_SESSION always via this
        path (its only outgoing edge); DISPATCH_UNCERTAIN only reaches
        here if queue_engine.py's own more specific re-check (did the
        session show real activity since?) didn't already resolve it to
        RUNNING first. Never drops a task -- the worst case is an extra,
        safely-deduped retry attempt, never silence."""
        now_epoch = now or iso_now()
        # calendar.timegm (NOT time.mktime, which wrongly assumes its
        # struct_time input is LOCAL time) -- these timestamps are
        # always UTC (iso_now() stamps via time.gmtime()), so this must
        # use the UTC-correct inverse or every host whose local
        # timezone isn't UTC computes a cutoff off by that offset (a
        # real bug found and fixed in this same task -- see this
        # module's own CHANGELOG-equivalent commit message).
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                              time.gmtime(calendar.timegm(time.strptime(now_epoch, "%Y-%m-%dT%H:%M:%SZ")) - grace_seconds))
        with self._connection() as connection:
            clause = "session = ? AND " if session else ""
            params: tuple[Any, ...] = (session,) if session else ()
            rows = connection.execute(
                f"SELECT id, status FROM queue_tasks WHERE {clause}status IN (?, ?) "
                f"AND uncertain_or_waiting_since IS NOT NULL AND uncertain_or_waiting_since < ?",
                (*params, DISPATCH_UNCERTAIN, WAITING_SESSION, cutoff),
            ).fetchall()
            reconciled = []
            for row in rows:
                self._transition_locked(connection, row["id"], row["status"], QUEUED,
                                        event_type="RECONCILED_AFTER_GRACE_PERIOD",
                                        reason=f"{row['status']} exceeded {grace_seconds}s grace period",
                                        extra_fields={"claimed_by": None, "claim_token": None,
                                                     "lease_expires_at": None,
                                                     "uncertain_or_waiting_since": None})
                reconciled.append(row["id"])
        return reconciled

    def queue_position(self, task_id: str) -> int | None:
        """1-indexed position of this task among its own lane's
        currently-QUEUED tasks (priority DESC, position ASC -- the same
        order claim_next_task itself uses), for the TASK_ACCEPTED
        acknowledgment shape (item 8). None if the task isn't currently
        QUEUED at all (it's already further along, or terminal)."""
        task = self.get_task(task_id)
        if task is None or task.status != QUEUED:
            return None
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id FROM queue_tasks WHERE session = ? AND status = ? ORDER BY priority DESC, position ASC",
                (task.session, QUEUED),
            ).fetchall()
        for index, row in enumerate(rows, start=1):
            if row["id"] == task_id:
                return index
        return None

    def metrics(self, session: str) -> dict[str, Any]:
        """P0 item 14's own required metrics. missed_count/dropped_count
        are always 0 here, structurally -- not a runtime measurement
        that COULD read nonzero, but a direct consequence of persist-
        before-dispatch's own design: set_tasks/append_tasks write the
        durable row in the SAME call that returns a task_id to the
        caller (queue_service.py's own TASK_ACCEPTED response), so there
        is no code path in this store where a caller could be told a
        task was accepted and no row exists for it -- surfaced as an
        explicit field so a dashboard/test can assert on it directly
        rather than trusting the absence of a bug report."""
        with self._connection() as connection:
            rows = connection.execute("SELECT status, created_at FROM queue_tasks WHERE session = ?",
                                      (session,)).fetchall()
        queued = [row for row in rows if row["status"] == QUEUED]
        uncertain = [row for row in rows if row["status"] == DISPATCH_UNCERTAIN]
        waiting_session = [row for row in rows if row["status"] == WAITING_SESSION]
        now_epoch = calendar.timegm(time.strptime(iso_now(), "%Y-%m-%dT%H:%M:%SZ"))

        def _age_seconds(created_at: str) -> float:
            # calendar.timegm, not time.mktime -- see reconcile_uncertain_
            # and_waiting's own comment for why (a real bug found and
            # fixed in this task, affecting every non-UTC host).
            return now_epoch - calendar.timegm(time.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ"))

        oldest_queued_age = max((_age_seconds(row["created_at"]) for row in queued), default=0.0)
        return {
            "session": session,
            "queued_depth": len(queued),
            "oldest_queued_age_seconds": oldest_queued_age,
            "dispatch_uncertain_count": len(uncertain),
            "waiting_session_count": len(waiting_session),
            "missed_count": 0,   # structural guarantee -- see docstring
            "dropped_count": 0,  # structural guarantee -- see docstring
        }

    def ensure_verification_nonce(self, task_id: str) -> str:
        """Idempotent: returns the task's existing verification_nonce if
        it already has one (e.g. a retry re-dispatching the same
        attempt), otherwise mints a fresh one and stores it. Called by
        queue_engine.py right before dispatch, so the completion-marker
        wrapper it adds to the prompt (item 7's own "wrapper rất ngắn")
        can embed a real, per-task nonce -- reusing status.py's own
        nonce+task_id+attempt-bound completion marker protocol (item
        11) instead of a second one."""
        with self._connection() as connection:
            row = connection.execute("SELECT verification_nonce FROM queue_tasks WHERE id = ?",
                                     (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such task: {task_id}")
            if row["verification_nonce"]:
                return row["verification_nonce"]
            nonce = new_task_id()
            connection.execute("UPDATE queue_tasks SET verification_nonce = ?, updated_at = ? WHERE id = ?",
                              (nonce, iso_now(), task_id))
        return nonce

    def mark_completed_with_evidence(self, task_id: str, *, evidence: dict[str, Any],
                                     event_type: str = "VERIFIED") -> QueueTask:
        """VERIFYING -> COMPLETED, but ONLY through here -- unlike a bare
        transition_task(..., COMPLETED), this REQUIRES evidence (item 11:
        "Không phụ thuộc heuristic 'final report' đơn thuần... fallback
        cần explicit coordinator verification") and stores it
        (verification_evidence) so the Coordinator Agent's own "did the
        previous task really finish" check (item 3's first bullet) has
        something concrete to look at instead of just trusting the
        status label. `evidence` is expected to already be redacted by
        the caller per existing policy."""
        if not evidence:
            raise ValueError("mark_completed_with_evidence requires non-empty evidence -- "
                            "use transition_task directly only for a test/legacy no-evidence path")
        # Requirement Contract gate. Non-emptiness was never the question:
        # queue_engine passes {"completion_marker": marker}, a string the agent
        # emitted about itself, and it satisfied this check for free (RC2). The
        # question is whether the evidence covers what was ASKED, which needs
        # the contract -- so ask it here, where COMPLETED is actually reached.
        decision = self.completion_decision(task_id)
        if not decision.verified_done:
            self.record_event(
                session=self.get_task(task_id).session, task_id=task_id,
                event_type="COMPLETION_REFUSED_REQUIREMENTS",
                reason=f"{decision.reason}: {', '.join(decision.blocking_ids()) or decision.detail}")
            raise RequirementsNotCoveredError(decision)
        # `event_type` keeps a marker-verified completion ("VERIFIED") distinct
        # from an operator reconciliation ("RECONCILED") in the event log. Both
        # carry evidence and both pass the contract gate above; conflating them
        # would hide which completions a human vouched for rather than the
        # engine having proved.
        return self.transition_task(task_id, COMPLETED, event_type=event_type,
                                    extra_fields={"verification_evidence": json.dumps(evidence)})

    # -- Requirement Contract ------------------------------------------------

    def get_requirement_contract(self, task_id: str) -> "rc.RequirementContract | None":
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return rc.RequirementContract.from_dict(task.requirement_contract)

    def get_evidence_matrix(self, task_id: str) -> "rc.EvidenceMatrix":
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return rc.EvidenceMatrix.from_dict(task.evidence_matrix)

    def set_requirement_contract(self, task_id: str, *,
                                 requirements: Sequence[dict[str, Any]] = (),
                                 prompt: str | None = None,
                                 actor: str | None = None) -> "rc.RequirementContract":
        """Create v1 of the contract for a task that has none.

        `prompt` defaults to the task's own prompt, so the original wording is
        preserved without the caller having to restate it -- v1 does not need
        perfect parsing, it needs to not lose the source.
        """
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.requirement_contract:
            raise rc.ContractError(
                f"{task_id} already has a contract (v"
                f"{task.requirement_contract.get('contract_version')}); "
                f"use amend_requirement_contract -- a contract is never overwritten")
        contract = rc.create_contract(
            prompt=prompt if prompt is not None else task.prompt,
            requirements=requirements, created_at=iso_now(), actor=actor)
        self._write_contract(task_id, contract)
        self.record_event(session=task.session, task_id=task_id,
                          event_type="REQUIREMENT_CONTRACT_SET",
                          reason=f"v1 with {len(contract.required_ids())} required criteria")
        return contract

    def amend_requirement_contract(self, task_id: str, *, prompt: str,
                                   requirements: Sequence[dict[str, Any]] = (),
                                   actor: str | None = None) -> "rc.RequirementContract":
        """Append a version. The prior version is never touched.

        This is the operation the reported failure had no way to express: a
        follow-up instruction became either a second task that knew nothing of
        the first contract, or a raw send that left the task row unchanged.
        """
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        existing = rc.RequirementContract.from_dict(task.requirement_contract)
        if existing is None:
            # An amendment to a task that never had a contract creates v1 from
            # the task's own prompt first, so the original is not lost by the
            # act of amending it.
            existing = rc.create_contract(prompt=task.prompt, created_at=iso_now(),
                                          actor=actor)
        amended = rc.amend_contract(existing, prompt=prompt, requirements=requirements,
                                    created_at=iso_now(), actor=actor)
        self._write_contract(task_id, amended)
        self.record_event(
            session=task.session, task_id=task_id, event_type="REQUIREMENT_CONTRACT_AMENDED",
            reason=f"v{amended.contract_version}: +{len(requirements)} requirement(s)")
        return amended

    def set_evidence_matrix(self, task_id: str, matrix: "rc.EvidenceMatrix") -> None:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE queue_tasks SET evidence_matrix = ?, updated_at = ? WHERE id = ?",
                (json.dumps(matrix.to_dict()), iso_now(), task_id))

    def _write_contract(self, task_id: str, contract: "rc.RequirementContract") -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE queue_tasks SET requirement_contract = ?, updated_at = ? WHERE id = ?",
                (json.dumps(contract.to_dict()), iso_now(), task_id))

    def completion_decision(self, task_id: str, *,
                            detectors: Sequence["rc.Detector"] = ()) -> "rc.GateDecision":
        """Reconcile this task's evidence against its LATEST contract version.

        Exposed separately from the gate so a caller (a tool, the dashboard)
        can show the Requirement | Status | Evidence checklist WITHOUT
        attempting completion -- `GateDecision.to_dict()` is that payload.
        """
        return rc.reconcile(self.get_requirement_contract(task_id),
                            self.get_evidence_matrix(task_id),
                            detectors=detectors or (
                                rc.detector_evidence_must_not_be_self_reported,))

    # -- deployment is not completion ---------------------------------------

    NOT_DEPLOYED = "NOT_DEPLOYED"
    DEPLOYED_TEST = "DEPLOYED_TEST"
    DEPLOYED_PROD = "DEPLOYED_PROD"
    DEPLOY_STATES = (NOT_DEPLOYED, DEPLOYED_TEST, DEPLOYED_PROD)

    def record_deploy(self, task_id: str, *, deploy_state: str, reference: str | None = None,
                      actor: str | None = None) -> QueueTask:
        """Record WHERE this task's work was deployed. Never changes `status`.

        RC6: there was no deployment concept at all, so "it is on TEST" had
        only `COMPLETED` available to say it with. Keeping this off the status
        enum is the whole point -- a deployed task is still un-verified until
        its requirements reconcile, and a reader can now see both facts at
        once instead of one standing in for the other.
        """
        if deploy_state not in self.DEPLOY_STATES:
            raise ValueError(f"unknown deploy_state {deploy_state!r}; "
                             f"expected one of {list(self.DEPLOY_STATES)}")
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        with self._connection() as connection:
            connection.execute(
                "UPDATE queue_tasks SET deploy_state = ?, updated_at = ? WHERE id = ?",
                (deploy_state, iso_now(), task_id))
        self.record_event(session=task.session, task_id=task_id, event_type="DEPLOY_RECORDED",
                          reason=f"{deploy_state}{f' ({reference})' if reference else ''}"
                                 f"{f' by {actor}' if actor else ''}")
        return self.get_task(task_id)

    # -- Task Migration / Load Balancing -----------------------------------

    _MIGRATABLE_STATUSES = (QUEUED, WAITING_SESSION, BLOCKED, FAILED)
    """Task Migration item 2: "Chỉ migrate task QUEUED/WAITING/READY chưa
    thực thi." Deliberately narrower than that in one respect (READY is
    EXCLUDED here, disclosed): READY means the Coordinator Agent has
    ALREADY approved this exact task for THIS session (session/cwd/
    node_id identity checks already passed against the source session --
    see coordinator.py's own review()) -- moving it to a different
    session at that point would silently invalidate a review that
    already happened without re-running it. reassign_task instead moves
    a READY task back through QUEUED (a fresh claim+review will happen
    at the NEW destination regardless) rather than trying to carry a
    stale approval across a session change; BLOCKED/FAILED are included
    (a operator explicitly moving a stuck task to a healthier session is
    exactly item 8's own use case) even though they're not, technically,
    "chưa thực thi" -- they never actually ran to completion either."""

    def reassign_task(self, task_id: str, to_session: str, *, reason: str, actor: str) -> QueueTask:
        """Moves ONE task's OWNERSHIP (its own `session` column -- the
        lane it belongs to) from its current session to `to_session`.
        NEVER creates a new task (item 1) -- same task_id, same prompt/
        metadata/dependencies/attempt_count/priority throughout; only
        `session` and (append-only) `migration_history` change.
        original_owner is set once, at creation, and never touched here.

        RACE SAFETY (item 12): atomic `BEGIN IMMEDIATE` transaction,
        re-checking the task's status INSIDE it -- if a dispatcher
        concurrently claimed this exact task (QUEUED -> PRECHECK)
        between whatever caller-side check led here and this call
        actually running, this raises TaskAlreadyClaimedError instead of
        silently reassigning out from under a live claim.

        REFUSES any task not currently in _MIGRATABLE_STATUSES --
        RUNNING/DISPATCHING/VERIFYING/PRECHECK/READY/PAUSED are never
        hot-migrated (item 2's own "Task RUNNING mặc định không được
        chuyển nóng"); use mark_at_risk for a RUNNING task whose session
        went offline instead of trying to move it."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                connection.rollback()
                self._discard_events()
                raise KeyError(f"no such task: {task_id}")
            if row["status"] not in self._MIGRATABLE_STATUSES:
                connection.rollback()
                self._discard_events()
                raise TaskAlreadyClaimedError(
                    f"{task_id}: status is {row['status']!r}, no longer eligible for reassignment "
                    f"(expected one of {self._MIGRATABLE_STATUSES})"
                )
            from_session = row["session"]
            history = _parse_json_dict_list(row["migration_history"])
            history.append({"from": from_session, "to": to_session, "reason": reason, "actor": actor,
                           "time": iso_now()})
            max_position_row = connection.execute(
                "SELECT COALESCE(MAX(position), -1) AS max_position FROM queue_tasks WHERE session = ?",
                (to_session,),
            ).fetchone()
            new_position = max_position_row["max_position"] + 1
            self._ensure_lane(connection, to_session)
            now = iso_now()
            # Migrating a task OUT of PRECHECK/READY isn't possible here
            # (excluded from _MIGRATABLE_STATUSES above) -- but a
            # WAITING_SESSION/BLOCKED/FAILED task moving to a new home
            # should get a fully fresh start there: back to QUEUED, own
            # claim/lease state cleared, so the destination's own next
            # claim_next_task treats it exactly like any other ordinary
            # QUEUED task (a fresh Coordinator review included).
            connection.execute(
                "UPDATE queue_tasks SET session = ?, position = ?, status = ?, migration_history = ?, "
                "claimed_by = NULL, claim_token = NULL, lease_expires_at = NULL, "
                "uncertain_or_waiting_since = NULL, updated_at = ? WHERE id = ?",
                (to_session, new_position, QUEUED, json.dumps(history), now, task_id),
            )
            self._record_event_locked(connection, session=from_session, task_id=task_id, event_type="MIGRATED_OUT",
                                      reason=reason, metadata={"to": to_session, "actor": actor})
            self._record_event_locked(connection, session=to_session, task_id=task_id, event_type="MIGRATED_IN",
                                      reason=reason, metadata={"from": from_session, "actor": actor})
            connection.commit()
            self._drain_events()
        except Exception:
            connection.rollback()
            self._discard_events()
            raise
        finally:
            connection.close()
        return self.get_task(task_id)

    def assignment_history(self, task_id: str) -> dict[str, Any]:
        """item 10's own `task_assignment_history` tool -- the task's
        full migration_history plus its own never-changing
        original_owner and its CURRENT session."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return {"task_id": task_id, "original_owner": task.original_owner, "current_session": task.session,
               "migration_history": list(task.migration_history)}

    def mark_at_risk(self, task_id: str, *, at_risk: bool = True) -> QueueTask:
        """Item 8: a RUNNING task whose own session went offline is
        marked AT_RISK -- purely informational (dashboard-visible),
        never auto-migrated, never auto-recovered by force-takeover.
        Does NOT change the task's own status -- at_risk is an
        orthogonal flag, not a state-machine transition."""
        with self._connection() as connection:
            connection.execute("UPDATE queue_tasks SET at_risk = ?, updated_at = ? WHERE id = ?",
                              (1 if at_risk else 0, iso_now(), task_id))
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such task: {task_id}")
            self._record_event_locked(connection, session=row["session"], task_id=task_id,
                                      event_type="AT_RISK" if at_risk else "AT_RISK_CLEARED", reason=None)
        return self.get_task(task_id)

    def set_lane_project(self, session: str, project: str | None) -> None:
        """Rebalancing (task_migration.py) only ever considers lanes
        sharing the SAME project value -- None only matches None, so an
        unconfigured lane is never accidentally grouped with another
        unconfigured one just because both happen to be blank."""
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            connection.execute("UPDATE queue_lanes SET project = ?, updated_at = ? WHERE session = ?",
                              (project, iso_now(), session))

    def mark_rebalanced(self, session: str) -> None:
        """Stamps the cooldown/hysteresis clock (item 5) -- called once
        per session touched by an APPLIED (not dry-run) rebalance plan."""
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            connection.execute("UPDATE queue_lanes SET last_rebalance_at = ?, updated_at = ? WHERE session = ?",
                              (iso_now(), iso_now(), session))

    def rename_session(self, old_session: str, new_session: str) -> dict[str, Any]:
        """Rename Session feature: this queue's `session` is the SAME
        stable identity every task/lane/event row already keys on --
        renaming updates that one string, in place, everywhere it
        appears, so every existing task_id/priority/dependency/
        migration_history/queue_position reference keeps working
        completely unchanged; nothing here is a new task or a new
        migration (deliberately does NOT append to migration_history or
        touch original_owner -- a rename is not a hand-off between
        sessions, it is the SAME session/coding-worker under a new
        display name, unlike reassign_task above).

        Fails clean (ValueError) if `new_session` already names a lane
        with any row of its own -- the caller (mcp_app.py's
        terminal_rename_session tool) is expected to have already
        checked this isn't a real collision via the fleet/backend layer,
        but this store never trusts that alone."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_new_lane = connection.execute(
                "SELECT 1 FROM queue_lanes WHERE session = ?", (new_session,),
            ).fetchone()
            if existing_new_lane is not None:
                connection.rollback()
                self._discard_events()
                raise ValueError(f"a queue lane already exists for {new_session!r}")
            now = iso_now()
            connection.execute(
                "UPDATE queue_lanes SET session = ?, updated_at = ? WHERE session = ?",
                (new_session, now, old_session),
            )
            tasks_updated = connection.execute(
                "UPDATE queue_tasks SET session = ?, updated_at = ? WHERE session = ?",
                (new_session, now, old_session),
            ).rowcount
            connection.execute(
                "UPDATE queue_tasks SET original_owner = ? WHERE original_owner = ?",
                (new_session, old_session),
            )
            connection.execute(
                "UPDATE queue_events SET session = ? WHERE session = ?", (new_session, old_session),
            )
            self._record_event_locked(connection, session=new_session, task_id=None, event_type="SESSION_RENAMED",
                                      reason=f"renamed from {old_session!r}", metadata={"from": old_session})
        return {"old_session": old_session, "new_session": new_session, "tasks_updated": tasks_updated}

    def retry_task(self, task_id: str) -> QueueTask:
        """BLOCKED|FAILED -> QUEUED, explicit operator action only (item
        11: "task lỗi -> BLOCKED và dừng queue... không tự skip... user
        có thể retry"). Does NOT reset attempt_count -- max_attempts is
        a lifetime cap across manual retries too, not just automatic
        ones. DOES clear dispatch_idempotency_key -- unlike an automatic
        stale-lease reconciliation (which keeps it, because the outcome
        of THAT send attempt is still genuinely unknown), an explicit
        operator retry after a real BLOCKED/FAILED is a deliberate new
        attempt, so a fresh idempotency_key is correct here."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        # TMCP-RETRY-CONTEXT-002 invariant 7: one owner at a time, and a
        # duplicate retry reconciles to a no-op instead of producing a second
        # owner. The check is by LOGICAL task (id, then request_key) because the
        # duplication that actually hurts is the same request running twice on
        # two agents -- an id-only check cannot see that. A retry of a task that
        # is already in flight returns that task unchanged rather than raising:
        # the caller asked for it to be running, and it is.
        owner = self.active_owner_for(task_id=task_id, request_key=task.request_key)
        is_noop, reason = retry_recovery.duplicate_retry_is_noop(
            task_id=task_id, request_key=task.request_key, active_owner=owner)
        if is_noop:
            self.record_event(session=task.session, task_id=task_id,
                              event_type="RETRY_DEDUPED", reason=reason)
            return self.get_task(owner["task_id"]) or task
        return self.transition_task(task_id, QUEUED, event_type="RETRIED",
                                    extra_fields={"dispatch_idempotency_key": None})

    def skip_task(self, task_id: str) -> QueueTask:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return self.transition_task(task_id, SKIPPED, event_type="SKIPPED")

    def cancel_task(self, task_id: str) -> QueueTask:
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return self.transition_task(task_id, CANCELLED, event_type="CANCELLED")

    def reorder_tasks(self, session: str, ordered_task_ids: list[str]) -> None:
        """Only ever reorders QUEUED tasks -- silently ignores any id in
        ordered_task_ids that isn't currently QUEUED for this session
        (reordering a RUNNING/COMPLETED/etc. task has no meaning), and
        never touches the position of a QUEUED task not mentioned."""
        with self._connection() as connection:
            queued = connection.execute(
                "SELECT id, position FROM queue_tasks WHERE session = ? AND status = ? ORDER BY position ASC",
                (session, QUEUED),
            ).fetchall()
            queued_ids = {row["id"] for row in queued}
            positions = sorted(row["position"] for row in queued)
            wanted = [task_id for task_id in ordered_task_ids if task_id in queued_ids]
            for task_id, position in zip(wanted, positions):
                connection.execute("UPDATE queue_tasks SET position = ?, updated_at = ? WHERE id = ?",
                                  (position, iso_now(), task_id))

    def clear_tasks(self, session: str, *, only_pending: bool = True) -> int:
        """only_pending=True (the safe default): cancels every QUEUED
        task, never an in-flight (PRECHECK/READY/DISPATCHING/RUNNING/
        VERIFYING/PAUSED) or already-terminal one. only_pending=False
        additionally cancels BLOCKED/FAILED tasks (an explicit "give up
        on the whole backlog" action) -- still never touches an
        in-flight task, which has no safe/instant way to be cancelled
        out from under a live send."""
        statuses = [QUEUED] if only_pending else [QUEUED, BLOCKED, FAILED]
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT id, status FROM queue_tasks WHERE session = ? AND status IN "
                f"({','.join('?' for _ in statuses)})", (session, *statuses),
            ).fetchall()
            for row in rows:
                self._transition_locked(connection, row["id"], row["status"], CANCELLED,
                                        event_type="CANCELLED", reason="queue_clear")
        return len(rows)

    # -- events --------------------------------------------------------

    def _record_event_locked(self, connection: sqlite3.Connection, *, session: str, task_id: str | None,
                             event_type: str, reason: str | None, from_status: str | None = None,
                             to_status: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        cursor = connection.execute(
            "INSERT INTO queue_events (timestamp, session, task_id, event_type, from_status, to_status, reason, "
            "metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iso_now(), session, task_id, event_type, from_status, to_status, reason,
             json.dumps(metadata) if metadata else None),
        )
        # Every queue event flows through here -- all 14 call sites -- so
        # hooking the bus at this one point gives complete coverage instead
        # of each caller having to remember to publish.
        if self._event_sink is not None:
            project_id = None
            outcome_id = None
            if task_id:
                row = connection.execute(
                    "SELECT project_id, outcome_id FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
                if row is not None:
                    project_id = row["project_id"]
                    outcome_id = row["outcome_id"] if "outcome_id" in row.keys() else None
            self._queue_event({
                "queue_event_id": cursor.lastrowid, "session": session, "task_id": task_id,
                "event_type": event_type, "from_status": from_status, "to_status": to_status,
                "reason": reason, "project_id": project_id, "outcome_id": outcome_id,
            })

    def record_event(self, *, session: str, task_id: str | None, event_type: str, reason: str | None = None,
                     metadata: dict[str, Any] | None = None) -> None:
        """Public entry point for events the engine wants recorded that
        aren't themselves a task transition (SUBMIT_CONFIRMED/
        SUBMIT_UNKNOWN, MANUAL_INTERVENTION, RECOVERED_AFTER_RESTART,
        PROGRESS, ...) -- transition_task already records its own event
        for anything that IS a status change."""
        with self._connection() as connection:
            self._record_event_locked(connection, session=session, task_id=task_id, event_type=event_type,
                                      reason=reason, metadata=metadata)

    def list_events(self, session: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM queue_events WHERE session = ? ORDER BY id DESC LIMIT ?", (session, limit),
            ).fetchall()
        return [dict(row) for row in rows]
