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
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "QueueTask":
        return cls(
            id=row["id"], session=row["session"], position=row["position"],
            title=row["title"], prompt=row["prompt"], status=row["status"],
            created_at=row["created_at"], started_at=row["started_at"], completed_at=row["completed_at"],
            attempt_count=row["attempt_count"], max_attempts=row["max_attempts"],
            completion_policy=_parse_json_object(row["completion_policy"]),
            last_error=row["last_error"], correlation_id=row["correlation_id"],
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
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "session": self.session, "position": self.position,
            "title": self.title, "prompt": self.prompt, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at, "completed_at": self.completed_at,
            "attempt_count": self.attempt_count, "max_attempts": self.max_attempts,
            "completion_policy": self.completion_policy, "last_error": self.last_error,
            "correlation_id": self.correlation_id, "metadata": self.metadata, "updated_at": self.updated_at,
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
]


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

    def pause_lane(self, session: str, *, reason: str | None = None) -> None:
        """Pauses dispatch for this session's lane. If a task is currently
        PRECHECK/READY/DISPATCHING/RUNNING/VERIFYING, it moves to PAUSED
        too (its prior status saved in paused_from_status so resume_lane
        can restore it) -- this is the mechanism item 10's "manual
        intervention -> pause lane, no race" requirement is built on, as
        well as a plain operator-requested pause of an otherwise-idle
        lane, and record_coordinator_decision's own NEEDS_HUMAN path."""
        with self._connection() as connection:
            self._pause_lane_locked(connection, session, reason=reason)

    def _pause_lane_locked(self, connection: sqlite3.Connection, session: str, *, reason: str | None) -> None:
        self._ensure_lane(connection, session)
        now = iso_now()
        connection.execute(
            "UPDATE queue_lanes SET paused = 1, paused_reason = ?, updated_at = ? WHERE session = ?",
            (reason, now, session),
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
                "UPDATE queue_lanes SET paused = 0, paused_reason = NULL, updated_at = ? WHERE session = ?",
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
                    "original_owner, project_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (task_id, session, next_position + offset, task.get("title") or "", task["prompt"], QUEUED, now,
                     int(task.get("max_attempts") or 3), json.dumps(task.get("completion_policy") or {}),
                     json.dumps(task.get("metadata") or {}), now, int(task.get("priority") or 0),
                     json.dumps(list(task.get("depends_on") or [])), session,
                     task.get("project_id")),
                )
                self._record_event_locked(connection, session=session, task_id=task_id, event_type="ENQUEUED",
                                          reason=None)
        return ids

    def append_tasks(self, session: str, tasks: list[dict[str, Any]]) -> list[str]:
        return self.set_tasks(session, tasks, replace_pending=False)

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

    def get_task(self, task_id: str) -> QueueTask | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        return QueueTask.from_row(row) if row else None

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
        return QueueTask.from_row(row)

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
                self._pause_lane_locked(connection, row["session"], reason=f"coordinator: {reason}")
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

    def mark_completed_with_evidence(self, task_id: str, *, evidence: dict[str, Any]) -> QueueTask:
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
        return self.transition_task(task_id, COMPLETED, event_type="VERIFIED",
                                    extra_fields={"verification_evidence": json.dumps(evidence)})

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
