"""P0.2 Event Bus -- one persistent, project-scoped, claimable event log.

WHY A NEW STORE (the audit's own justification, not a preference): this
project already has three event logs -- queue_events (241 live rows),
integration_events, supervisor_events -- and none of them is a bus. They
are append-only, per-store, per-session audit records with no subscribe,
no claim, no cross-store ordering and no fan-out. Nothing existing could
be extended into a bus without changing what those logs mean, so they are
left exactly as they are and simply EMIT here in addition.

WHAT IS REUSED, deliberately and verbatim in shape:
  - `claim_next` uses `BEGIN IMMEDIATE` before the read, exactly like
    queue_store.claim_next_task, so two consumers can never claim the
    same event (the same TOCTOU class that method's own docstring
    documents having fixed).
  - claim_token + lease_expires_at, so a crashed consumer's event becomes
    reclaimable on expiry rather than being lost -- the PaneLeaseStore
    contract, applied to events.
  - idempotency_key is UNIQUE: re-publishing the same logical event is a
    no-op that returns the ORIGINAL event, matching audit.idempotent_sends'
    established "same key => same result, never a duplicate" behaviour.

ORDERING, stated honestly: `seq` is a single AUTOINCREMENT counter, so
events are totally ordered *within this database*. What is PROMISED is
per-project ordering (filter by project_id, order by seq). A global total
order across the other stores' logs is NOT promised and must not be
assumed -- those are separate databases with independent clocks.

PAYLOAD SAFETY: `payload` is for safe metadata and references only. Raw
prompt text never belongs here -- audit.py hashes prompts rather than
storing them, and this store follows the same rule (see publish()).
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .schema import Migration, apply_migrations

# The vocabulary the coordination roadmap names. Declared as constants so a
# typo is a NameError instead of a silently-never-matched string.
TASK_CREATED = "TASK_CREATED"
TASK_READY = "TASK_READY"
WORKER_IDLE = "WORKER_IDLE"
WORKER_DONE = "WORKER_DONE"
VERIFY_PENDING = "VERIFY_PENDING"
MERGE_CONFLICT = "MERGE_CONFLICT"
TEST_FAILED = "TEST_FAILED"
PREVIEW_FAILED = "PREVIEW_FAILED"
USER_FEEDBACK = "USER_FEEDBACK"
KNOWN_EVENT_TYPES = (
    # Goal / planning
    "GOAL_SUBMITTED", "REQUIREMENT_CHANGED",
    # Task lifecycle -- emitted by queue_store's own transition chokepoint
    "TASK_CREATED", "TASK_READY", "TASK_CLAIMED", "TASK_STARTED", "WORKER_DONE",
    "TASK_COMPLETED", "TASK_FAILED", "TASK_BLOCKED",
    # Leases
    "LEASE_RELEASED", "LEASE_EXPIRED", "TASK_HANDOFF",
    # Verification
    "VERIFY_PENDING", "VERIFY_CLAIMED", "VERIFY_PASS", "VERIFY_FAIL", "VERIFY_BLOCKED",
    # Outcome
    "OUTCOME_CREATED", "OUTCOME_AWAITING_ACCEPTANCE", "OUTCOME_DONE", "OUTCOME_BLOCKED",
    # Integration / delivery
    "MERGE_PENDING", "MERGE_CONFLICT", "TEST_FAILED", "PREVIEW_FAILED",
    # Fleet / resources
    "WORKER_IDLE", "NODE_LOST", "RESOURCE_CONFLICT",
    # Human
    "USER_FEEDBACK", "PROJECT_BLOCKED",
)
"""The vocabulary this system actually emits, aligned with the orchestration
design. Still NOT enforced by publish() -- a caller may emit any non-empty
string, deliberately, so an experiment does not require a schema change. This
tuple is the documented set producers use and consumers can rely on."""

PENDING = "PENDING"
CLAIMED = "CLAIMED"
ACKED = "ACKED"
FAILED = "FAILED"

DEFAULT_LEASE_SECONDS = 300.0
MAX_ATTEMPTS = 5

def _add_v2_provenance_and_cursors(connection: sqlite3.Connection) -> None:
    """Orchestration V1. Three additions the event-driven coordinator needs
    and the bus did not have.

    `actor` -- the PUBLISHER was anonymous. `claimed_by` records who
    consumed an event; nothing recorded who emitted it, so "why did this
    happen" could not be answered from the log itself.

    `causation_id` -- there was only a flat `correlation_id` (and nothing
    in the repo ever populated it). Correlation groups events that belong
    to one activity; causation says THIS event happened BECAUSE OF that
    one. A coordinator that reacts to events by emitting more events makes
    a chain, and without causation the chain is unreconstructable -- which
    is exactly how a feedback loop hides.

    `event_cursors` -- consumption was claim/ack ONLY: one event, one
    consumer, and an ack destroyed it for everyone else. A coordinator that
    wants to READ the stream (rather than consume it exclusively) had no
    durable resume point, so a restart either reprocessed from the
    beginning or silently skipped. A cursor is a per-consumer high-water
    mark, advanced only forward, that survives restart and never competes
    with another consumer for the same event."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(events)")}
    if "actor" not in columns:
        connection.execute("ALTER TABLE events ADD COLUMN actor TEXT")
    if "causation_id" not in columns:
        connection.execute("ALTER TABLE events ADD COLUMN causation_id TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_causation ON events(causation_id)")
    connection.execute("""
        CREATE TABLE IF NOT EXISTS event_cursors (
            consumer TEXT NOT NULL,
            project_id TEXT NOT NULL DEFAULT '',
            last_seq INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (consumer, project_id)
        )
    """)


EVENT_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: project-scoped claimable event bus", lambda connection: None),
    Migration(2, "Orchestration V1: events.actor + events.causation_id + event_cursors "
                 "(durable per-consumer high-water mark, non-destructive reads)",
              _add_v2_provenance_and_cursors),
]


def default_event_bus_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_EVENT_BUS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "events.db"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


class EventBus:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_event_bus_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Schema setup uses a PLAIN connection, not _connection(): that
        # helper always opens a transaction, and SQLite refuses to switch
        # journal_mode from inside one.
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    id TEXT NOT NULL UNIQUE,
                    project_id TEXT,
                    type TEXT NOT NULL,
                    entity_type TEXT,
                    entity_id TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    correlation_id TEXT,
                    idempotency_key TEXT UNIQUE,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    created_at TEXT NOT NULL,
                    claimed_by TEXT,
                    claim_token TEXT,
                    lease_expires_at TEXT,
                    acked_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_claimable "
                               "ON events(status, project_id, type, seq)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_project_seq "
                               "ON events(project_id, seq)")
            apply_migrations(connection, EVENT_MIGRATIONS)
            connection.commit()
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            with __import__("contextlib").suppress(Exception):
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------- publish
    def publish(self, type: str, *, project_id: str | None = None,
                entity_type: str | None = None, entity_id: str | None = None,
                payload: dict[str, Any] | None = None, correlation_id: str | None = None,
                idempotency_key: str | None = None, actor: str | None = None,
                causation_id: str | None = None) -> dict[str, Any]:
        """Append one event. `project_id=None` is a legitimate, unscoped
        (global) event -- it is simply never returned by a project-scoped
        claim.

        Re-publishing with an idempotency_key that already exists returns
        the ORIGINAL event unchanged and creates nothing, so a retried
        producer cannot duplicate work."""
        if not type:
            raise ValueError("event type is required")
        body = json.dumps(payload or {}, ensure_ascii=False)
        if len(body) > 64_000:
            raise ValueError("event payload too large -- store a reference, not the content")
        event_id = uuid.uuid4().hex
        now = _iso(_now())
        with self._connection(immediate=True) as connection:
            if idempotency_key:
                existing = connection.execute(
                    "SELECT * FROM events WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
                if existing is not None:
                    return _row_to_dict(existing) | {"duplicate": True}
            connection.execute(
                "INSERT INTO events (id, project_id, type, entity_type, entity_id, payload, "
                "correlation_id, idempotency_key, status, created_at, actor, causation_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, project_id, type, entity_type, entity_id, body,
                 correlation_id, idempotency_key, PENDING, now, actor, causation_id))
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_dict(row) | {"duplicate": False}

    # ---------------------------------------------------------------- read
    def list_events(self, *, project_id: str | None = None, types: list[str] | None = None,
                    status: str | None = None, since_seq: int | None = None,
                    limit: int = 100) -> list[dict[str, Any]]:
        """Ordered by `seq` ascending -- per-project ordering is the
        guarantee (see this module's docstring)."""
        query = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(project_id)
        if types:
            query += f" AND type IN ({','.join('?' * len(types))})"
            params.extend(types)
        if status:
            query += " AND status = ?"
            params.append(status)
        if since_seq is not None:
            query += " AND seq > ?"
            params.append(int(since_seq))
        query += " ORDER BY seq ASC LIMIT ?"
        params.append(int(limit))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def get(self, event_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_dict(row) if row else None

    # --------------------------------------------------------------- claim
    def claim_next(self, *, consumer: str, project_id: str | None = None,
                   types: list[str] | None = None,
                   lease_seconds: float = DEFAULT_LEASE_SECONDS) -> dict[str, Any] | None:
        """Claim the OLDEST eligible event, atomically.

        `BEGIN IMMEDIATE` takes SQLite's write lock BEFORE the read, so a
        second concurrent consumer blocks and then correctly sees the
        event already CLAIMED -- the same fix, in the same shape, as
        queue_store.claim_next_task.

        An event whose lease has EXPIRED is eligible again: a consumer
        that crashed mid-handling never strands its event."""
        now = _now()
        now_iso = _iso(now)
        query = ("SELECT * FROM events WHERE (status = ? OR (status = ? AND lease_expires_at < ?)) ")
        params: list[Any] = [PENDING, CLAIMED, now_iso]
        if project_id is not None:
            query += "AND project_id = ? "
            params.append(project_id)
        if types:
            query += f"AND type IN ({','.join('?' * len(types))}) "
            params.extend(types)
        query += "AND attempt_count < ? ORDER BY seq ASC LIMIT 1"
        params.append(MAX_ATTEMPTS)
        token = uuid.uuid4().hex
        with self._connection(immediate=True) as connection:
            # Dead-letter FIRST, in the same write lock. MAX_ATTEMPTS used to
            # be a claim FILTER only: an event that burned its budget simply
            # stopped being returned here, and sat in PENDING/CLAIMED forever
            # -- never FAILED, absent from stats(), invisible to every
            # operator view. A poison event has to become visible, not just
            # stop moving.
            self._dead_letter_exhausted_locked(connection, now_iso, project_id)
            row = connection.execute(query, params).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE events SET status = ?, claimed_by = ?, claim_token = ?, "
                "lease_expires_at = ?, attempt_count = attempt_count + 1 WHERE seq = ?",
                (CLAIMED, consumer, token, _iso(now + timedelta(seconds=lease_seconds)), row["seq"]))
            claimed = connection.execute("SELECT * FROM events WHERE seq = ?", (row["seq"],)).fetchone()
        return _row_to_dict(claimed)

    def _dead_letter_exhausted_locked(self, connection, now_iso: str,
                                      project_id: str | None) -> int:
        """Move every event that has exhausted MAX_ATTEMPTS and is not
        actively leased into FAILED, with a reason. Idempotent: a row
        already FAILED/ACKED is not matched."""
        clause = "AND project_id = ? " if project_id is not None else ""
        params: list[Any] = [FAILED,
                             f"dead-lettered after {MAX_ATTEMPTS} delivery attempts without an ack",
                             PENDING, CLAIMED, now_iso, MAX_ATTEMPTS]
        if project_id is not None:
            params.append(project_id)
        cursor = connection.execute(
            "UPDATE events SET status = ?, last_error = COALESCE(last_error, ?), "
            "claimed_by = NULL, claim_token = NULL, lease_expires_at = NULL "
            f"WHERE (status = ? OR (status = ? AND lease_expires_at < ?)) "
            f"AND attempt_count >= ? {clause}", params)
        return cursor.rowcount

    # ------------------------------------------------- consumer cursors
    #
    # A NON-DESTRUCTIVE alternative to claim/ack. claim/ack is the right
    # model for WORK (one event, one worker, exactly-once effort). A cursor
    # is the right model for OBSERVATION (many independent readers, each
    # with its own durable position, none consuming the event from the
    # others). The coordinator needs the second: it reads the stream to
    # decide, and other consumers must still see the same events.

    def read_since(self, consumer: str, *, project_id: str | None = None,
                   types: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Events this consumer has not yet acknowledged, oldest first.

        Does NOT advance the cursor -- reading and committing a position are
        deliberately separate calls, so a consumer that crashes mid-handling
        re-reads rather than silently skipping. That makes delivery
        at-least-once, which is why every producer stamps an
        idempotency_key."""
        scope = project_id or ""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT last_seq FROM event_cursors WHERE consumer = ? AND project_id = ?",
                (consumer, scope)).fetchone()
            last_seq = row["last_seq"] if row else 0
            query = "SELECT * FROM events WHERE seq > ? "
            params: list[Any] = [last_seq]
            if project_id is not None:
                query += "AND project_id = ? "
                params.append(project_id)
            if types:
                query += f"AND type IN ({','.join('?' * len(types))}) "
                params.extend(types)
            query += "ORDER BY seq ASC LIMIT ?"
            params.append(limit)
            rows = connection.execute(query, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def commit_cursor(self, consumer: str, seq: int, *,
                      project_id: str | None = None) -> int:
        """Advance a consumer's high-water mark. MONOTONIC: a lower seq is
        ignored rather than applied, so an out-of-order or replayed commit
        can never rewind a consumer and cause reprocessing."""
        scope = project_id or ""
        now = _iso(_now())
        with self._connection(immediate=True) as connection:
            connection.execute(
                "INSERT INTO event_cursors (consumer, project_id, last_seq, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(consumer, project_id) DO UPDATE SET "
                "last_seq = MAX(event_cursors.last_seq, excluded.last_seq), "
                "updated_at = excluded.updated_at",
                (consumer, scope, int(seq), now))
            row = connection.execute(
                "SELECT last_seq FROM event_cursors WHERE consumer = ? AND project_id = ?",
                (consumer, scope)).fetchone()
        return row["last_seq"]

    def cursor(self, consumer: str, *, project_id: str | None = None) -> dict[str, Any]:
        scope = project_id or ""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM event_cursors WHERE consumer = ? AND project_id = ?",
                (consumer, scope)).fetchone()
        return dict(row) if row is not None else {
            "consumer": consumer, "project_id": scope, "last_seq": 0, "updated_at": None}

    def causation_chain(self, event_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """Walk BACKWARDS from an event to the one that caused it, and so
        on. The read that answers "why did this happen" -- and the one that
        makes a coordinator feedback loop visible instead of merely
        suspected."""
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current = event_id
        with self._connection() as connection:
            while current and current not in seen and len(chain) < limit:
                seen.add(current)
                row = connection.execute("SELECT * FROM events WHERE id = ?", (current,)).fetchone()
                if row is None:
                    break
                chain.append(_row_to_dict(row))
                current = row["causation_id"]
        return chain

    def dead_letters(self, *, project_id: str | None = None,
                     limit: int = 100) -> list[dict[str, Any]]:
        """Events that gave up. The read an operator needs to answer "what
        did the bus stop trying to deliver, and why"."""
        clause = "AND project_id = ? " if project_id is not None else ""
        params: list[Any] = [FAILED]
        if project_id is not None:
            params.append(project_id)
        params.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM events WHERE status = ? {clause}ORDER BY seq DESC LIMIT ?",
                params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def ack(self, event_id: str, claim_token: str) -> bool:
        """Only the CURRENT token may ack -- a consumer whose lease expired
        and was reclaimed by someone else cannot ack the other's work."""
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE events SET status = ?, acked_at = ?, lease_expires_at = NULL "
                "WHERE id = ? AND claim_token = ? AND status = ?",
                (ACKED, _iso(_now()), event_id, claim_token, CLAIMED))
        return cursor.rowcount > 0

    def release(self, event_id: str, claim_token: str, *, error: str | None = None) -> bool:
        """Hand an event back for someone else to take. Does NOT count as a
        new attempt beyond the one already recorded by the claim."""
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE events SET status = ?, claimed_by = NULL, claim_token = NULL, "
                "lease_expires_at = NULL, last_error = ? WHERE id = ? AND claim_token = ?",
                (PENDING, error, event_id, claim_token))
        return cursor.rowcount > 0

    def fail(self, event_id: str, claim_token: str, *, error: str) -> bool:
        """Terminal failure: stop redelivering. Kept distinct from release
        so a poison event cannot loop forever."""
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE events SET status = ?, last_error = ?, lease_expires_at = NULL "
                "WHERE id = ? AND claim_token = ?", (FAILED, error, event_id, claim_token))
        return cursor.rowcount > 0

    def retry(self, event_id: str) -> bool:
        """Operator action: put a FAILED event back in play and reset its
        attempt budget."""
        with self._connection(immediate=True) as connection:
            cursor = connection.execute(
                "UPDATE events SET status = ?, attempt_count = 0, claimed_by = NULL, "
                "claim_token = NULL, lease_expires_at = NULL WHERE id = ? AND status = ?",
                (PENDING, event_id, FAILED))
        return cursor.rowcount > 0

    def stats(self, *, project_id: str | None = None) -> dict[str, int]:
        query = "SELECT status, COUNT(*) AS n FROM events"
        params: list[Any] = []
        if project_id is not None:
            query += " WHERE project_id = ?"
            params.append(project_id)
        query += " GROUP BY status"
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        return {r["status"]: r["n"] for r in rows}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["payload"] = json.loads(data.get("payload") or "{}")
    except (TypeError, ValueError):
        data["payload"] = {}
    return data
