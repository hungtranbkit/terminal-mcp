"""P0 Part B: a durable, cross-process pane lease -- the in-memory
PaneLockRegistry (core.py) only ever serializes sends *within one Python
process*; the HTTP server, a separate STDIO server process, and any other
process opening its own TerminalService each get their own independent
PaneLockRegistry, so two of them sending to the same tmux pane at the same
moment could still interleave their text/Enter keystrokes with nothing
stopping them. A SQLite row (same durable-local-primitive pattern as
audit.py/bindings.py/grants.py/supervisor.py/supervisor2.py in this
codebase, not a new dependency) closes that gap: whichever process holds
the current, unexpired lease row for a given pane is the only one allowed
to send to it, and every process (regardless of which one) sees the same
row through the same on-disk database file.

Design:
  - One row per pane_key (the caller's choice of identity string -- see
    core.py's TerminalService for exactly how it derives one from a
    resolved SessionIdentity or, failing that, a session name).
  - owner_id identifies *one send attempt*, not one process -- core.py
    uses each attempt's own correlation_id, so two concurrent attempts
    (whether from the same process's two threads or two entirely
    different processes) never collide on ownership, and a crashed
    attempt's lease expires and is reclaimed exactly like any other.
  - expires_at is a fixed TTL from acquire time (no background renewal
    thread): a real send+verify(+bounded recovery) attempt is itself
    bounded in wall-clock time (see core.py's own verification timeouts),
    so a lease sized comfortably above that worst case naturally covers
    one real attempt without needing renewal machinery, while still
    recovering promptly (this module's LEASE crash-recovery contract)
    if the holding process is killed mid-send.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

# Comfortably above the worst real observed send+verify+recovery cycle
# (RECOVERY_VERIFY_TIMEOUT_SECONDS * 2 plus settle delays, core.py) with
# headroom for host scheduling jitter -- long enough that a genuinely
# in-flight attempt is never falsely reclaimed, short enough that a
# crashed holder's pane is not stuck unusable for long.
DEFAULT_LEASE_TTL_SECONDS = 20.0


def default_lease_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_LEASE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "leases.db"


LEASE_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: pane_leases as of the P0 Part B cross-process lease", lambda connection: None),
]


class PaneLeaseStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_lease_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS pane_leases (
                    pane_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    renewed_at TEXT NOT NULL
                )
            """)
            apply_migrations(connection, LEASE_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextlib.contextmanager
    def _connection(self):
        # Same fd-leak-avoiding pattern as every other store in this
        # codebase (see audit.py's _connection docstring for the full
        # rationale) -- commit/rollback the transaction, then always
        # close the OS handle too.
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def acquire(self, pane_key: str, owner_id: str, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> bool:
        """True if `owner_id` now holds the lease for `pane_key` -- either
        because no one held it, the current holder's lease has expired
        (crash recovery: reclaimed exactly as if it had been released), or
        `owner_id` already held it (idempotent re-acquire, e.g. a retry of
        the exact same attempt). False if a *different*, still-unexpired
        owner currently holds it -- the caller must not send in that case.

        A single atomic INSERT ... ON CONFLICT DO UPDATE ... WHERE, not a
        separate SELECT-then-INSERT/UPDATE: this connection's default
        (deferred) isolation mode does not take any lock for a bare SELECT,
        so two connections' SELECTs can both see "no conflicting holder"
        and both then "win" a subsequent write -- a real, reproduced race
        (caught by this module's own concurrent-thread tests) between two
        genuinely concurrent acquire() calls for the same pane_key, not a
        theoretical one. Folding the whole decision into one statement's
        WHERE clause makes SQLite's own write-lock cover the entire
        check-and-set, eliminating the gap outright rather than working
        around it with an explicit BEGIN IMMEDIATE."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO pane_leases (pane_key, owner_id, acquired_at, expires_at, renewed_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(pane_key) DO UPDATE SET owner_id = excluded.owner_id, "
                "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at, "
                "renewed_at = excluded.renewed_at "
                "WHERE pane_leases.owner_id = excluded.owner_id OR pane_leases.expires_at < excluded.acquired_at",
                (pane_key, owner_id, now_iso, expires_iso, now_iso),
            )
        return cursor.rowcount == 1

    def renew(self, pane_key: str, owner_id: str, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> bool:
        """Extend an already-held lease -- only succeeds if `owner_id` is
        still the current, unexpired holder. Not used by the base
        send+verify path today (its bounded worst-case duration fits
        inside DEFAULT_LEASE_TTL_SECONDS with headroom -- see the module
        docstring), available for a future caller with a longer-running
        hold (e.g. a multi-step interactive recovery) to extend rather
        than risk expiry mid-operation."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE pane_leases SET expires_at = ?, renewed_at = ? "
                "WHERE pane_key = ? AND owner_id = ? AND expires_at >= ?",
                (expires_iso, now_iso, pane_key, owner_id, now_iso),
            )
        return cursor.rowcount == 1

    def release(self, pane_key: str, owner_id: str) -> bool:
        """True if `owner_id`'s own (possibly already-expired, but not yet
        reclaimed by anyone else) lease row was removed. False if some
        other owner_id now holds the row -- meaning it already expired and
        was reclaimed before this release ran; never deletes someone
        else's active lease."""
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM pane_leases WHERE pane_key = ? AND owner_id = ?", (pane_key, owner_id),
            )
        return cursor.rowcount == 1

    def holder(self, pane_key: str) -> dict[str, Any] | None:
        """Diagnostic/test read: the current row for `pane_key`, if any,
        regardless of whether it has expired (callers that care about
        expiry compare `expires_at` themselves) -- not consulted by the
        acquire/release hot path above, which does its own atomic check."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT pane_key, owner_id, acquired_at, expires_at, renewed_at FROM pane_leases WHERE pane_key = ?",
                (pane_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def prune_expired(self, *, grace_seconds: float = 300.0) -> int:
        """Housekeeping only (see maintenance.py) -- acquire()'s own
        expiry check already makes an expired row harmless to a new
        acquirer without this ever running; this just keeps the table
        from accumulating rows for panes no one has touched again since.
        grace_seconds keeps a just-expired row around briefly for
        diagnostics (holder()) rather than deleting it the instant it
        expires."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM pane_leases WHERE expires_at < ?", (cutoff,))
        return cursor.rowcount
