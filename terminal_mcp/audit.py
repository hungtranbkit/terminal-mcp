from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .redaction import redact_text


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
               reason: str | None = None, source_transport: str = "mcp") -> None:
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO input_audit
                (timestamp, action, binding, session, text_sha256, text_preview,
                 text_length, keys, press_enter, result, reason, source_transport, server_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), action, binding, session,
                 text_fingerprint(text) if text is not None else None,
                 sanitized_preview(text) if text is not None else None,
                 len(text) if text is not None else None,
                 json.dumps(keys) if keys is not None else None, int(press_enter),
                 result, reason, source_transport, __version__),
            )

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
