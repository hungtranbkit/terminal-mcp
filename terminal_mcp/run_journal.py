"""Durable, deliberately compact run/tool recovery journal.

This store records only enough state to resume orchestration after a client or
tool connection disappears.  It is not an audit log: prompts, tool arguments,
tool output, transcripts, and secrets have no columns here.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_text


MAX_ACTION_CHARS = 2_000
MAX_SUMMARY_CHARS = 4_000
MAX_ERROR_CHARS = 4_000
MAX_METADATA_CHARS = 4_000
MAX_METADATA_KEY_CHARS = 128
MAX_METADATA_VALUE_CHARS = 512

_FORBIDDEN_METADATA_KEYS = (
    "prompt", "argument", "args", "input", "output", "transcript",
    "secret", "password", "token", "credential", "authorization",
)


def default_run_journal_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "run_journal.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("journal text fields must be strings or None")
    return redact_text(value)[:limit]


def _metadata_json(metadata: dict[str, Any] | None) -> str:
    """Serialize small, scalar recovery metadata, never content payloads."""
    if metadata is None:
        return "{}"
    if not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary")

    clean: dict[str, str | int | float | bool | None] = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise TypeError("metadata keys must be strings")
        normalized = key.casefold().replace("-", "_")
        if any(part in normalized for part in _FORBIDDEN_METADATA_KEYS):
            raise ValueError(f"metadata key is not allowed in the run journal: {key}")
        if isinstance(value, str):
            safe_value: str | int | float | bool | None = redact_text(value)[:MAX_METADATA_VALUE_CHARS]
        elif value is None or isinstance(value, (bool, int, float)):
            safe_value = value
        else:
            raise TypeError("metadata values must be JSON scalars")
        clean[key[:MAX_METADATA_KEY_CHARS]] = safe_value

    raw = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if len(raw) > MAX_METADATA_CHARS:
        raise ValueError(f"metadata exceeds {MAX_METADATA_CHARS} characters")
    return raw


class RunJournalStore:
    """SQLite/WAL persistence for resumable runs and append-only entries."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_run_journal_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Tighten an existing state directory too (subject to platform support).
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _connection(self):
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
                CREATE TABLE IF NOT EXISTS journal_runs (
                    run_id TEXT PRIMARY KEY,
                    root_task_id TEXT,
                    project_id TEXT NOT NULL,
                    session TEXT NOT NULL,
                    binding TEXT NOT NULL,
                    state TEXT NOT NULL,
                    next_action TEXT,
                    result_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS journal_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES journal_runs(run_id),
                    event_key TEXT NOT NULL UNIQUE,
                    tool_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error TEXT,
                    checkpoint_ref TEXT,
                    next_action TEXT,
                    result_summary TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_entries_run ON journal_entries(run_id, id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_runs_recent "
                "ON journal_runs(updated_at DESC, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_journal_runs_project_recent "
                "ON journal_runs(project_id, updated_at DESC)"
            )
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        return {
            "run_id": row["run_id"],
            "root_task_id": row["root_task_id"],
            "project_id": row["project_id"],
            "session": row["session"],
            "binding": row["binding"],
            "state": row["state"],
            "next_action": row["next_action"],
            "result_summary": row["result_summary"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
            "metadata": metadata if isinstance(metadata, dict) else {},
        }

    @staticmethod
    def _entry(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "id", "run_id", "event_key", "tool_name", "state", "error",
            "checkpoint_ref", "next_action", "result_summary", "created_at",
        )}

    def start_run(
        self,
        project_id: str,
        session: str,
        binding: str,
        *,
        root_task_id: str | None = None,
        state: str = "running",
        next_action: str | None = None,
        result_summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM journal_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
            if row is None:
                now = _now_iso()
                values = (
                    run_id, root_task_id, project_id, session, binding, state,
                    _bounded(next_action, MAX_ACTION_CHARS),
                    _bounded(result_summary, MAX_SUMMARY_CHARS),
                    now, now, _metadata_json(metadata),
                )
                connection.execute(
                    """
                    INSERT INTO journal_runs
                        (run_id, root_task_id, project_id, session, binding, state,
                         next_action, result_summary, created_at, updated_at, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                row = connection.execute(
                    "SELECT * FROM journal_runs WHERE run_id = ?", (run_id,),
                ).fetchone()
        return self._run(row)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM journal_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return self._run(row)

    def _require_run(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM journal_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        return row

    def record(
        self,
        run_id: str,
        event_key: str,
        *,
        tool_name: str,
        state: str,
        error: str | None = None,
        checkpoint_ref: str | None = None,
        next_action: str | None = None,
        result_summary: str | None = None,
    ) -> dict[str, Any]:
        now = _now_iso()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            existing = connection.execute(
                "SELECT * FROM journal_entries WHERE event_key = ?", (event_key,),
            ).fetchone()
            if existing is not None:
                return self._entry(existing)
            safe_error = _bounded(error, MAX_ERROR_CHARS)
            safe_action = _bounded(next_action, MAX_ACTION_CHARS)
            safe_summary = _bounded(result_summary, MAX_SUMMARY_CHARS)
            cursor = connection.execute(
                """
                INSERT INTO journal_entries
                    (run_id, event_key, tool_name, state, error, checkpoint_ref,
                     next_action, result_summary, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, event_key, tool_name, state, safe_error, checkpoint_ref,
                 safe_action, safe_summary, now),
            )
            connection.execute(
                """
                UPDATE journal_runs
                SET state = ?, next_action = COALESCE(?, next_action),
                    result_summary = COALESCE(?, result_summary), updated_at = ?
                WHERE run_id = ?
                """,
                (state, safe_action, safe_summary, now, run_id),
            )
            row = connection.execute(
                "SELECT * FROM journal_entries WHERE id = ?", (cursor.lastrowid,),
            ).fetchone()
        return self._entry(row)

    def entries(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        after_id = max(0, int(after_id))
        with self._connection() as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM journal_entries
                WHERE run_id = ? AND id > ? ORDER BY id ASC LIMIT ?
                """,
                (run_id, after_id, limit),
            ).fetchall()
        return [self._entry(row) for row in rows]

    def recent(
        self,
        limit: int = 50,
        active_only: bool = False,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        clauses: list[str] = []
        values: list[Any] = []
        if active_only:
            clauses.append("completed_at IS NULL")
        if project_id is not None:
            clauses.append("project_id = ?")
            values.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM journal_runs {where} "
                "ORDER BY updated_at DESC, created_at DESC, run_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._run(row) for row in rows]

    def update_run(
        self,
        run_id: str,
        *,
        state: str | None = None,
        next_action: str | None = None,
        result_summary: str | None = None,
        completed: bool = False,
    ) -> dict[str, Any]:
        now = _now_iso()
        assignments = ["updated_at = ?"]
        values: list[Any] = [now]
        if state is not None:
            assignments.append("state = ?")
            values.append(state)
        if next_action is not None:
            assignments.append("next_action = ?")
            values.append(_bounded(next_action, MAX_ACTION_CHARS))
        if result_summary is not None:
            assignments.append("result_summary = ?")
            values.append(_bounded(result_summary, MAX_SUMMARY_CHARS))
        if completed:
            assignments.append("completed_at = COALESCE(completed_at, ?)")
            values.append(now)
        values.append(run_id)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_run(connection, run_id)
            connection.execute(
                f"UPDATE journal_runs SET {', '.join(assignments)} WHERE run_id = ?", values,
            )
            row = connection.execute(
                "SELECT * FROM journal_runs WHERE run_id = ?", (run_id,),
            ).fetchone()
        return self._run(row)

    def resume_recent(self, project_id: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        runs = self.recent(limit=limit, active_only=True, project_id=project_id)
        result: list[dict[str, Any]] = []
        with self._connection() as connection:
            for run in runs:
                row = connection.execute(
                    "SELECT * FROM journal_entries WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                    (run["run_id"],),
                ).fetchone()
                latest = self._entry(row) if row is not None else None
                result.append({**run, "latest_entry": latest, "cursor": latest["id"] if latest else 0})
        return result
