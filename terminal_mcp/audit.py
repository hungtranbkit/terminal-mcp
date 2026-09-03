from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .redaction import redact_text
from .schema import Migration, apply_migrations

# P1 hardening item #10: version 1 is the baseline -- everything above
# (input_audit/idempotent_sends, including every column the pre-existing
# ad-hoc pattern already added) as of this db's first open under the
# versioned-migration system. A no-op apply: its only job is stamping
# PRAGMA user_version so future schema changes append Migration(2, ...),
# Migration(3, ...) here instead of another bare, untracked ALTER TABLE.
def _add_correlation_id_column(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE input_audit ADD COLUMN correlation_id TEXT")


def _add_loop_protection_columns(connection: sqlite3.Connection) -> None:
    connection.execute("ALTER TABLE input_audit ADD COLUMN origin TEXT")
    connection.execute("ALTER TABLE input_audit ADD COLUMN trace_id TEXT")
    connection.execute("ALTER TABLE input_audit ADD COLUMN parent_turn_id TEXT")
    connection.execute("ALTER TABLE input_audit ADD COLUMN depth INTEGER")


AUDIT_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: input_audit + idempotent_sends as of the P1 hardening pass", lambda connection: None),
    # P0 Part A.5: every send attempt (not just idempotency-keyed ones) now
    # carries a correlation_id, ties an audit row to the exact adapter-
    # evidence decision that produced it, and lets a caller reconciling
    # after a lost response look the attempt up by something narrower than
    # session+timestamp.
    Migration(2, "add input_audit.correlation_id for P0 delivery-state reconciliation", _add_correlation_id_column),
    # Prompt-submission reliability upgrade, P11: schema-only prep for
    # future agent-bridge loop protection (e.g. a ChatGPT-Web-adapter turn
    # re-entering a Codex/Claude session) -- all four columns are optional
    # and NULL for every current caller (terminal_send_text/_granted accept
    # them but nothing sets them yet outside tests). See
    # docs/prompt-submission.md.
    Migration(3, "add input_audit.origin/trace_id/parent_turn_id/depth for P11 loop-protection metadata",
               _add_loop_protection_columns),
]


def default_audit_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_AUDIT_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "audit.db"


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitized_preview(text: str, limit: int = 240) -> str:
    compact = " ".join(redact_text(text).split())
    return compact[:limit]


class AuditStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_audit_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            # P0 audit re-pass: every other store in this project (bindings/
            # grants/lease/supervisor/supervisor2) sets WAL mode; this one
            # was missed. WAL is a property of the database FILE, not the
            # connection, so setting it once here (matching bindings.py/
            # grants.py's exact pattern) is sufficient -- it persists across
            # every future connection/process, never needs repeating per-
            # connect. Matters specifically for this store: audit.record()
            # runs on every terminal_send_text/_keys call, a genuinely hot
            # path, from potentially several processes at once (HTTP,
            # STDIO, dashboard, Supervisor v2) -- the P0 Part B concurrent-
            # access work already covers every other store's readers/
            # writers not blocking each other under that exact load; this
            # store had been left on SQLite's default rollback-journal mode,
            # which blocks readers against writers (and vice versa) far more
            # than WAL does.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS input_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL, action TEXT NOT NULL,
                    binding TEXT, session TEXT,
                    text_sha256 TEXT, text_preview TEXT, text_length INTEGER,
                    keys TEXT, press_enter INTEGER NOT NULL DEFAULT 0,
                    result TEXT NOT NULL, reason TEXT,
                    source_transport TEXT NOT NULL, server_version TEXT NOT NULL
                )
            """)
            # P0-4: durable idempotency keys for input submissions. A row is
            # first inserted with result_json = NULL to *claim* the key
            # (INSERT OR IGNORE -- only the first caller for a given key
            # ever wins the claim, even across process restarts, since this
            # is on disk), then updated with the real result once the send
            # completes. Never stores raw prompt text -- result_json is
            # exactly the same response dict callers already get back
            # (sent/characters/press_enter/submit_status/error, no text).
            connection.execute("""
                CREATE TABLE IF NOT EXISTS idempotent_sends (
                    idempotency_key TEXT PRIMARY KEY,
                    result_json TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            apply_migrations(connection, AUDIT_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        """Open a connection, commit/rollback its transaction on exit (the
        same semantics `with self._connect() as connection:` already had —
        sqlite3.Connection's own context manager only manages the
        transaction), and *also* always close the underlying OS handle,
        which that alone never does. Relying on garbage collection to
        eventually close it leaks one real file descriptor per call — fine
        for occasional use, fatal ("Too many open files") for anything
        called on a hot path (e.g. a poll loop)."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, *, action: str, session: str | None, result: str,
               binding: str | None = None, text: str | None = None,
               keys: list[str] | None = None, press_enter: bool = False,
               reason: str | None = None, source_transport: str = "mcp",
               correlation_id: str | None = None, origin: str | None = None,
               trace_id: str | None = None, parent_turn_id: str | None = None,
               depth: int | None = None) -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO input_audit
                (timestamp, action, binding, session, text_sha256, text_preview,
                 text_length, keys, press_enter, result, reason, source_transport, server_version,
                 correlation_id, origin, trace_id, parent_turn_id, depth)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), action, binding, session,
                 text_fingerprint(text) if text is not None else None,
                 sanitized_preview(text) if text is not None else None,
                 len(text) if text is not None else None,
                 json.dumps(keys) if keys is not None else None, int(press_enter),
                 result, reason, source_transport, __version__, correlation_id,
                 origin, trace_id, parent_turn_id, depth),
            )

    def prune(self, retention: int) -> int:
        """P1 hardening item #9: input_audit has no other retention limit
        -- every terminal_send_text/_keys call ever recorded stays forever
        otherwise. Same "keep the most recent N rows" shape as
        SupervisorStore.prune_events; called periodically (not on this
        hot record() path itself -- see maintenance.py) so a busy send
        volume never pays this DELETE's cost per-call."""
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM input_audit WHERE id NOT IN "
                "(SELECT id FROM input_audit ORDER BY id DESC LIMIT ?)",
                (retention,),
            )
        return cursor.rowcount

    def prune_idempotency_keys(self, older_than_days: int) -> int:
        """Unlike input_audit's "keep the most recent N" shape, this is
        time-based: an idempotency key only needs to outlive a plausible
        retry window (a caller re-sending the exact same key after a
        dropped response), not stay forever -- but it must never be
        pruned so aggressively that a legitimate delayed retry sees a
        fresh, un-deduplicated send instead of its original stored result."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM idempotent_sends WHERE created_at < ?", (cutoff,),
            )
        return cursor.rowcount

    def claim_idempotency_key(self, key: str, *, stale_after_seconds: float = 30.0) -> bool:
        """Returns True if this call is the one that gets to actually
        perform the action (first claim of this key, ever -- durable
        across process restart since it's on disk); False if another
        caller (a prior completed call, or a concurrent in-flight one)
        already claimed it first.

        P0 Part A.5/B: a claim whose result_json is still NULL after
        `stale_after_seconds` is reclaimed rather than left blocking
        forever -- a claimant that crashed (process killed, host restart)
        between claiming and storing its result would otherwise leave every
        future retry of that exact key permanently stuck reporting
        DUPLICATE_IN_PROGRESS for an attempt that will never actually
        finish. Reclaim is a plain UPDATE gated on both conditions
        (result_json IS NULL AND created_at < cutoff) so a genuinely
        in-flight concurrent claim (any age under the threshold) is never
        disturbed -- this only ever fires for an abandoned claim, and the
        reclaiming caller becomes the new sole owner exactly as if it had
        won the original INSERT OR IGNORE race."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO idempotent_sends (idempotency_key, result_json, created_at) VALUES (?, NULL, ?)",
                (key, now),
            )
            if cursor.rowcount == 1:
                return True
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
            reclaim = connection.execute(
                "UPDATE idempotent_sends SET created_at = ? "
                "WHERE idempotency_key = ? AND result_json IS NULL AND created_at < ?",
                (now, key, cutoff),
            )
        return reclaim.rowcount == 1

    def store_idempotent_result(self, key: str, result: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE idempotent_sends SET result_json = ? WHERE idempotency_key = ?",
                (json.dumps(result), key),
            )

    def get_idempotent_result(self, key: str) -> dict[str, Any] | None:
        """None means either the key was never claimed, or it was claimed
        but the claiming call hasn't stored a result yet (still in flight)
        -- callers distinguish those two cases via claim_idempotency_key's
        own return value, not this method alone."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM idempotent_sends WHERE idempotency_key = ?", (key,),
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    def list(self, limit: int = 50, binding: str | None = None,
             session: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        clauses, params = [], []
        if binding is not None:
            clauses.append("binding = ?")
            params.append(binding)
        if session is not None:
            clauses.append("session = ?")
            params.append(session)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = "SELECT * FROM input_audit" + where + " ORDER BY id DESC LIMIT ?"
        with self._connection() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["preview"] = item.pop("text_preview")
            item["keys"] = json.loads(item["keys"]) if item["keys"] else None
            item["press_enter"] = bool(item["press_enter"])
            results.append(item)
        return results
