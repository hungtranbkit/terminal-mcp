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

import contextlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

# -- Task status state machine ------------------------------------------

QUEUED = "QUEUED"
DISPATCHING = "DISPATCHING"
RUNNING = "RUNNING"
VERIFYING = "VERIFYING"
COMPLETED = "COMPLETED"
BLOCKED = "BLOCKED"
PAUSED = "PAUSED"
SKIPPED = "SKIPPED"
CANCELLED = "CANCELLED"

ALL_STATUSES = (QUEUED, DISPATCHING, RUNNING, VERIFYING, COMPLETED, BLOCKED, PAUSED, SKIPPED, CANCELLED)
TERMINAL_STATUSES = (COMPLETED, SKIPPED, CANCELLED)
"""Once here, a task never transitions again -- not even via a manual
tool call. A BLOCKED task is deliberately NOT terminal (terminal_queue_
retry/skip/cancel all still apply to it -- see VALID_TRANSITIONS)."""

# Every ALLOWED (from_status -> {to_status, ...}) edge. Anything not
# listed here is refused by transition_task -- see its own docstring for
# why this is enforced centrally rather than trusted to each caller.
VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({DISPATCHING, SKIPPED, CANCELLED, PAUSED}),
    DISPATCHING: frozenset({
        RUNNING,  # submit confirmed (delivery_state == SUBMIT_CONFIRMED)
        QUEUED,   # DELIVERY_UNKNOWN reconciled as "definitely not sent" -- safe to retry
        BLOCKED,  # a hard send failure (ACCESS_DENIED, SESSION_NOT_FOUND, ...)
        CANCELLED, PAUSED,
    }),
    RUNNING: frozenset({VERIFYING, BLOCKED, CANCELLED, PAUSED}),
    VERIFYING: frozenset({
        COMPLETED,
        RUNNING,   # false alarm -- the agent wasn't actually done, re-arm
        BLOCKED, CANCELLED, PAUSED,
    }),
    BLOCKED: frozenset({QUEUED, SKIPPED, CANCELLED}),  # only ever via an explicit operator tool call
    PAUSED: frozenset({QUEUED, DISPATCHING, RUNNING, VERIFYING, CANCELLED}),  # resume (to paused_from_status) or cancel
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
        }


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


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


QUEUE_MIGRATIONS = [
    Migration(1, "initial Supervisor Queue v2 schema (queue_tasks/queue_lanes/queue_events)", _create_v1_schema),
]


class QueueStore:
    """SQLite persistence for the whole Supervisor Queue v2 feature --
    same pattern as supervisor.py's SupervisorStore (0700 state dir,
    0600 db file, WAL, row_factory=Row), migrations via schema.py."""

    def __init__(self, path: str | Path | None = None) -> None:
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
        finally:
            connection.close()

    # -- lane-level -------------------------------------------------------

    def _ensure_lane(self, connection: sqlite3.Connection, session: str) -> None:
        now = iso_now()
        connection.execute(
            "INSERT OR IGNORE INTO queue_lanes (session, paused, paused_reason, created_at, updated_at) "
            "VALUES (?, 0, NULL, ?, ?)", (session, now, now),
        )

    def pause_lane(self, session: str, *, reason: str | None = None) -> None:
        """Pauses dispatch for this session's lane. If a task is currently
        DISPATCHING/RUNNING/VERIFYING, it moves to PAUSED too (its prior
        status saved in paused_from_status so resume_lane can restore
        it) -- this is the mechanism item 10's "manual intervention ->
        pause lane, no race" requirement is built on, as well as a plain
        operator-requested pause of an otherwise-idle lane."""
        with self._connection() as connection:
            self._ensure_lane(connection, session)
            now = iso_now()
            connection.execute(
                "UPDATE queue_lanes SET paused = 1, paused_reason = ?, updated_at = ? WHERE session = ?",
                (reason, now, session),
            )
            active = connection.execute(
                "SELECT id, status FROM queue_tasks WHERE session = ? AND status IN (?, ?, ?)",
                (session, DISPATCHING, RUNNING, VERIFYING),
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
        active = next((t for t in tasks if t["status"] in (DISPATCHING, RUNNING, VERIFYING, PAUSED)), None)
        queued_count = sum(1 for t in tasks if t["status"] == QUEUED)
        completed_count = sum(1 for t in tasks if t["status"] == COMPLETED)
        return {
            "session": session,
            "paused": bool(lane_row["paused"]),
            "paused_reason": lane_row["paused_reason"],
            "tasks": tasks,
            "current_task": active,
            "queued_count": queued_count,
            "completed_count": completed_count,
            "total_count": len(tasks),
        }

    def list_all_lanes(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            sessions = [row["session"] for row in connection.execute("SELECT session FROM queue_lanes").fetchall()]
        return [self.lane_status(session) for session in sessions]

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
                    "attempt_count, max_attempts, completion_policy, metadata, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
                    (task_id, session, next_position + offset, task.get("title") or "", task["prompt"], QUEUED, now,
                     int(task.get("max_attempts") or 3), json.dumps(task.get("completion_policy") or {}),
                     json.dumps(task.get("metadata") or {}), now),
                )
                self._record_event_locked(connection, session=session, task_id=task_id, event_type="ENQUEUED",
                                          reason=None)
        return ids

    def append_tasks(self, session: str, tasks: list[dict[str, Any]]) -> list[str]:
        return self.set_tasks(session, tasks, replace_pending=False)

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
            lane = connection.execute("SELECT paused FROM queue_lanes WHERE session = ?", (session,)).fetchone()
            if lane is None or lane["paused"]:
                return None
            active = connection.execute(
                "SELECT id FROM queue_tasks WHERE session = ? AND status IN (?, ?, ?, ?, ?)",
                (session, DISPATCHING, RUNNING, VERIFYING, PAUSED, BLOCKED),
            ).fetchone()
            if active is not None:
                return None  # one task at a time, per session -- BLOCKED stops the lane too
            row = connection.execute(
                "SELECT * FROM queue_tasks WHERE session = ? AND status = ? ORDER BY position ASC LIMIT 1",
                (session, QUEUED),
            ).fetchone()
        return QueueTask.from_row(row) if row else None

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
        if reason is not None and to_status in (BLOCKED,):
            fields["last_error"] = reason
        if extra_fields:
            fields.update(extra_fields)
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        connection.execute(f"UPDATE queue_tasks SET {set_clause} WHERE id = ?", (*fields.values(), task_id))
        row = connection.execute("SELECT * FROM queue_tasks WHERE id = ?", (task_id,)).fetchone()
        self._record_event_locked(connection, session=row["session"], task_id=task_id, event_type=event_type,
                                  reason=reason, from_status=from_status, to_status=to_status)
        return QueueTask.from_row(row)

    def retry_task(self, task_id: str) -> QueueTask:
        """BLOCKED -> QUEUED, explicit operator action only (item 11:
        "task lỗi -> BLOCKED và dừng queue... không tự skip... user có
        thể retry"). Does NOT reset attempt_count -- max_attempts is a
        lifetime cap across manual retries too, not just automatic ones."""
        task = self.get_task(task_id)
        if task is None:
            raise KeyError(f"no such task: {task_id}")
        return self.transition_task(task_id, QUEUED, event_type="RETRIED")

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
        task, never an in-flight (DISPATCHING/RUNNING/VERIFYING/PAUSED)
        or already-terminal one. only_pending=False additionally cancels
        BLOCKED tasks (an explicit "give up on the whole backlog"
        action) -- still never touches an in-flight task, which has no
        safe/instant way to be cancelled out from under a live send."""
        statuses = [QUEUED] if only_pending else [QUEUED, BLOCKED]
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
        connection.execute(
            "INSERT INTO queue_events (timestamp, session, task_id, event_type, from_status, to_status, reason, "
            "metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iso_now(), session, task_id, event_type, from_status, to_status, reason,
             json.dumps(metadata) if metadata else None),
        )

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
