"""SQLite persistence for durable Archify generation jobs."""

from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArchifyStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS archify_jobs (
                    id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    diagram_type TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    error_code TEXT,
                    error_message TEXT,
                    error_detail TEXT,
                    ir_path TEXT,
                    metadata_path TEXT,
                    html_path TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_archify_jobs_recent "
                "ON archify_jobs(created_at DESC)"
            )
        with self._lock:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create(self, project_path: str, project_name: str, diagram_type: str,
               prompt: str) -> dict[str, Any]:
        job_id = f"arch_{uuid.uuid4().hex}"
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO archify_jobs "
                "(id,project_path,project_name,diagram_type,prompt,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,'queued',?,?)",
                (job_id, project_path, project_name, diagram_type, prompt, now, now),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM archify_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 1000))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archify_jobs ORDER BY created_at DESC LIMIT ?", (safe_limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def queued(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM archify_jobs WHERE status='queued' ORDER BY created_at",
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_running(self) -> int:
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE archify_jobs SET status='failed', completed_at=?, updated_at=?, "
                "error_code='interrupted', error_message='Generation was interrupted by restart.' "
                "WHERE status='running'",
                (now, now),
            )
            return cursor.rowcount

    def mark_running(self, job_id: str) -> bool:
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE archify_jobs SET status='running', started_at=?, updated_at=? "
                "WHERE id=? AND status='queued'", (now, now, job_id),
            )
            return cursor.rowcount == 1

    def complete(self, job_id: str, *, ir_path: str, metadata_path: str,
                 html_path: str) -> None:
        now = _now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE archify_jobs SET status='completed', completed_at=?, updated_at=?, "
                "ir_path=?, metadata_path=?, html_path=?, error_code=NULL, error_message=NULL, "
                "error_detail=NULL WHERE id=? AND status='running'",
                (now, now, ir_path, metadata_path, html_path, job_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"invalid Archify completion transition for {job_id}")

    def fail(self, job_id: str, *, code: str, message: str, detail: str = "") -> None:
        now = _now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE archify_jobs SET status='failed', completed_at=?, updated_at=?, "
                "error_code=?, error_message=?, error_detail=? "
                "WHERE id=? AND status IN ('queued','running')",
                (now, now, code, message, detail[:4096], job_id),
            )
