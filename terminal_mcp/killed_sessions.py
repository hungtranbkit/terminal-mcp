"""Reopen metadata for a session killed through terminal_kill_session
(core.py) -- one row per session NAME (not a history log: killing the
same name twice just replaces the previous row, and a successful Reopen
or a same-named session reappearing through any other path clears it
outright, since at that point there is nothing left to "reopen").

Deliberately minimal and deliberately never a secret store: only what
Reopen actually needs (docs/... none yet -- see dashboard.py's Kill/Reopen
routes and core.py's terminal_kill_session/terminal_reopen_session for the
design this backs). No prompt text, no pane content, no environment
variables -- just the name, an already-safety-validated working directory
(or none, if what was observed didn't validate), and an agent_type/
launch_command classification (or none, if the pane's command at kill
time didn't match anything this deployment's own session_lifecycle.
launch_commands recognizes). `metadata_complete` is the one field the
dashboard needs to decide whether to warn "may not auto-reopen" before a
Kill, and whether Reopen can proceed without asking the caller to supply
agent_type/working_directory explicitly.

Same connection/schema/permission pattern as every other store in this
project (audit.py/bindings.py/grants.py/lease.py lineage)."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .schema import Migration, apply_migrations

KILLED_SESSION_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: killed_sessions as of the Kill/Reopen design", lambda connection: None),
]


def default_killed_sessions_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_KILLED_SESSIONS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "killed_sessions.db"


@dataclass(frozen=True)
class KilledSessionRecord:
    name: str
    agent_type: str | None          # "claude" | "codex" | "shell" | None (unrecognized)
    working_directory: str | None   # already validated against allowed_cwd_roots at kill time
    observed_command: str | None    # raw pane_current_command at kill time -- informational only,
                                     # never used directly as a launcher (agent_type/launch_command is)
    metadata_complete: bool         # True only if BOTH agent_type and working_directory are known
    killed_by: str | None
    killed_at: str


def _from_row(row: sqlite3.Row | None) -> KilledSessionRecord | None:
    if row is None:
        return None
    return KilledSessionRecord(
        name=row["name"], agent_type=row["agent_type"], working_directory=row["working_directory"],
        observed_command=row["observed_command"], metadata_complete=bool(row["metadata_complete"]),
        killed_by=row["killed_by"], killed_at=row["killed_at"],
    )


class KilledSessionStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_killed_sessions_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS killed_sessions (
                    name TEXT PRIMARY KEY,
                    agent_type TEXT,
                    working_directory TEXT,
                    observed_command TEXT,
                    metadata_complete INTEGER NOT NULL,
                    killed_by TEXT,
                    killed_at TEXT NOT NULL
                )
                """
            )
            apply_migrations(connection, KILLED_SESSION_MIGRATIONS)
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

    def record(self, name: str, *, agent_type: str | None, working_directory: str | None,
              observed_command: str | None, killed_by: str | None) -> KilledSessionRecord:
        metadata_complete = agent_type is not None and (agent_type == "shell" or working_directory is not None)
        # "shell" needs no launcher token to reopen safely (lifecycle.py's
        # create() starts a plain default shell for it) -- a known
        # working_directory is still preferred but its absence alone
        # doesn't make a shell un-reopenable the way it would for
        # claude/codex, since resolve_cwd() already has a safe fallback
        # (the server's own home directory) for an omitted cwd.
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO killed_sessions
                (name, agent_type, working_directory, observed_command, metadata_complete, killed_by, killed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    agent_type = excluded.agent_type, working_directory = excluded.working_directory,
                    observed_command = excluded.observed_command, metadata_complete = excluded.metadata_complete,
                    killed_by = excluded.killed_by, killed_at = excluded.killed_at""",
                (name, agent_type, working_directory, observed_command, int(metadata_complete),
                 killed_by, now_iso),
            )
        return self.get(name)

    def get(self, name: str) -> KilledSessionRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM killed_sessions WHERE name = ?", (name,)).fetchone()
        return _from_row(row)

    def list(self, *, limit: int = 100) -> list[KilledSessionRecord]:
        limit = max(1, min(limit, 500))
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM killed_sessions ORDER BY killed_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [record for record in (_from_row(row) for row in rows) if record is not None]

    def clear(self, name: str) -> bool:
        """Idempotent: True the first time a row for `name` is removed,
        False if there was nothing to clear. Called on a successful
        Reopen, and defensively any time a session by this name is
        observed to exist again through any other path -- a stale
        killed_sessions row must never linger once there's a real,
        different-lifetime session using the name again."""
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM killed_sessions WHERE name = ?", (name,))
        return cursor.rowcount == 1
