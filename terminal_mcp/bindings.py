from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


BINDING_NAME_RE = re.compile(r"^[a-z0-9_.-]{1,64}$")


def valid_binding_name(name: str) -> bool:
    return bool(BINDING_NAME_RE.fullmatch(name))


def default_binding_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_BINDINGS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "bindings.db"


@dataclass(frozen=True)
class Binding:
    name: str
    session: str
    read_enabled: bool
    input_enabled: bool
    created_at: str
    updated_at: str


class BindingStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_binding_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialize()

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

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    name TEXT PRIMARY KEY,
                    session TEXT NOT NULL,
                    read_enabled INTEGER NOT NULL DEFAULT 1,
                    input_enabled INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> Binding | None:
        if row is None:
            return None
        return Binding(
            name=row["name"], session=row["session"],
            read_enabled=bool(row["read_enabled"]), input_enabled=bool(row["input_enabled"]),
            created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def get(self, name: str) -> Binding | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM bindings WHERE name = ?", (name,)).fetchone()
        return self._from_row(row)

    def list(self) -> list[Binding]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM bindings ORDER BY name").fetchall()
        return [binding for row in rows if (binding := self._from_row(row)) is not None]

    def put(self, name: str, session: str, *, read_enabled: bool = True,
            input_enabled: bool = False, replace: bool = False) -> tuple[Binding | None, bool]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM bindings WHERE name = ?", (name,)).fetchone()
            if existing is not None and not replace:
                return self._from_row(existing), False
            if existing is None:
                connection.execute(
                    "INSERT INTO bindings (name, session, read_enabled, input_enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name, session, int(read_enabled), int(input_enabled), now, now),
                )
            else:
                connection.execute(
                    "UPDATE bindings SET session = ?, read_enabled = ?, input_enabled = ?, updated_at = ? WHERE name = ?",
                    (session, int(read_enabled), int(input_enabled), now, name),
                )
            row = connection.execute("SELECT * FROM bindings WHERE name = ?", (name,)).fetchone()
        return self._from_row(row), True

    def delete(self, name: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM bindings WHERE name = ?", (name,))
        return cursor.rowcount == 1
