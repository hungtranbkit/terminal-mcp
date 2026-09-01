"""Per-session dashboard-only access grants.

Explicit, durable, DASHBOARD-scoped authorization for a tmux session
outside the static config.yaml whitelist (allowed_session_patterns /
input_policy.allowed_session_patterns). Deliberately NOT reachable from
any MCP tool: the raw MCP tool surface (terminal_tail/terminal_status/
terminal_send_text/terminal_bind/...) is completely unaffected by this
file and continues enforcing ONLY the static whitelist, exactly as
before -- see core.py's *_granted methods and dashboard.py's routes,
which are the only callers. A grant widens what the DASHBOARD (already
gated by Cloudflare Access + CSRF + dashboard.mutations_enabled) can do
for ONE specific session, nothing else, ever.

Same read-then-input ordering as the rest of this project (input requires
read first; revoking read also revokes input, never leaves it dangling),
and the same identity-pinning-at-grant-time/re-checked-at-use-time
discipline terminal_send_bound already uses for bindings -- a session
recreated under the same name never silently keeps a prior input grant;
see core.py's terminal_send_text_granted for the re-check.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema import Migration, apply_migrations

GRANT_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: session_grants", lambda connection: None),
]


def default_grant_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_GRANTS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "grants.db"


@dataclass(frozen=True)
class SessionGrant:
    session: str
    read_enabled: bool
    input_enabled: bool
    pinned_session_id: str | None
    pinned_pane_id: str | None
    pinned_created_epoch: int | None
    granted_by: str | None
    created_at: str
    updated_at: str


def _from_row(row: sqlite3.Row | None) -> SessionGrant | None:
    if row is None:
        return None
    return SessionGrant(
        session=row["session"], read_enabled=bool(row["read_enabled"]),
        input_enabled=bool(row["input_enabled"]),
        pinned_session_id=row["pinned_session_id"], pinned_pane_id=row["pinned_pane_id"],
        pinned_created_epoch=row["pinned_created_epoch"], granted_by=row["granted_by"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


class SessionGrantStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_grant_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_grants (
                    session TEXT PRIMARY KEY,
                    read_enabled INTEGER NOT NULL DEFAULT 0,
                    input_enabled INTEGER NOT NULL DEFAULT 0,
                    pinned_session_id TEXT,
                    pinned_pane_id TEXT,
                    pinned_created_epoch INTEGER,
                    granted_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            apply_migrations(connection, GRANT_MIGRATIONS)
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
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, session: str) -> SessionGrant | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM session_grants WHERE session = ?", (session,)).fetchone()
        return _from_row(row)

    def list(self) -> list[SessionGrant]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM session_grants ORDER BY session").fetchall()
        return [grant for grant in (_from_row(row) for row in rows) if grant is not None]

    def set_read(self, session: str, enabled: bool, *, granted_by: str | None) -> SessionGrant:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO session_grants
                (session, read_enabled, input_enabled, granted_by, created_at, updated_at)
                VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(session) DO UPDATE SET
                    read_enabled = excluded.read_enabled, granted_by = excluded.granted_by,
                    updated_at = excluded.updated_at""",
                (session, int(enabled), granted_by, now, now),
            )
            if not enabled:
                # Revoking read also revokes input and clears its pin --
                # input without read access is meaningless and must never
                # be left dangling for a later read-re-grant to silently
                # reactivate.
                connection.execute(
                    """UPDATE session_grants SET input_enabled = 0, pinned_session_id = NULL,
                       pinned_pane_id = NULL, pinned_created_epoch = NULL, updated_at = ?
                       WHERE session = ?""",
                    (now, session),
                )
        return self.get(session)  # type: ignore[return-value]

    def set_input(self, session: str, enabled: bool, *, granted_by: str | None,
                  pinned_session_id: str | None = None, pinned_pane_id: str | None = None,
                  pinned_created_epoch: int | None = None) -> SessionGrant | None:
        """Returns None (does nothing) if read isn't already granted --
        callers (core.py) are expected to check that and return their own
        explicit error rather than silently no-op through this."""
        existing = self.get(session)
        if existing is None or not existing.read_enabled:
            return None
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """UPDATE session_grants SET input_enabled = ?, pinned_session_id = ?,
                   pinned_pane_id = ?, pinned_created_epoch = ?, granted_by = ?, updated_at = ?
                   WHERE session = ?""",
                (int(enabled),
                 pinned_session_id if enabled else None,
                 pinned_pane_id if enabled else None,
                 pinned_created_epoch if enabled else None,
                 granted_by, now, session),
            )
        return self.get(session)
