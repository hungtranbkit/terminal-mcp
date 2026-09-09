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
KNOWN_EVENT_TYPES = (TASK_CREATED, TASK_READY, WORKER_IDLE, WORKER_DONE, VERIFY_PENDING,
                     MERGE_CONFLICT, TEST_FAILED, PREVIEW_FAILED, USER_FEEDBACK)

PENDING = "PENDING"
CLAIMED = "CLAIMED"
ACKED = "ACKED"
FAILED = "FAILED"

DEFAULT_LEASE_SECONDS = 300.0
MAX_ATTEMPTS = 5

EVENT_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: project-scoped claimable event bus", lambda connection: None),
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
                idempotency_key: str | None = None) -> dict[str, Any]:
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
                "correlation_id, idempotency_key, status, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (event_id, project_id, type, entity_type, entity_id, body,
                 correlation_id, idempotency_key, PENDING, now))
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
            row = connection.execute(query, params).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE events SET status = ?, claimed_by = ?, claim_token = ?, "
                "lease_expires_at = ?, attempt_count = attempt_count + 1 WHERE seq = ?",
                (CLAIMED, consumer, token, _iso(now + timedelta(seconds=lease_seconds)), row["seq"]))
            claimed = connection.execute("SELECT * FROM events WHERE seq = ?", (row["seq"],)).fetchone()
        return _row_to_dict(claimed)

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
