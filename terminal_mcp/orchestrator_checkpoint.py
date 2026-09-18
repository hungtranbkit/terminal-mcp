"""Durable chat-level orchestration checkpoints.

This store protects the coordination context that exists above individual
terminal sessions: goals, decisions, delegated tasks, blockers and the next
operator actions.  It is intentionally append-only and controller-local.
The ChatGPT conversation itself is never treated as the source of truth.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import redact_text

MAX_PROJECT_ID_CHARS = 512
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


class OrchestratorCheckpointError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message}


def default_orchestrator_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_ORCHESTRATOR_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "orchestrator.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact_recursive(value: Any) -> Any:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [_redact_recursive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_recursive(item) for item in value]
    if isinstance(value, dict):
        return {
            redact_text(str(key)): _redact_recursive(item)
            for key, item in value.items()
        }
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def _project_id(value: str) -> str:
    project_id = str(value or "").strip()
    if not project_id:
        raise OrchestratorCheckpointError("INVALID_PROJECT_ID", "project_id is required")
    if len(project_id) > MAX_PROJECT_ID_CHARS:
        raise OrchestratorCheckpointError(
            "INVALID_PROJECT_ID", f"project_id must be <= {MAX_PROJECT_ID_CHARS} characters"
        )
    # Identity fields cannot be silently rewritten: if it resembles a secret,
    # reject it rather than persist a credential or create an unrecoverable id.
    if redact_text(project_id) != project_id:
        raise OrchestratorCheckpointError(
            "INVALID_PROJECT_ID", "project_id must not contain credential material"
        )
    return project_id


def _list(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise OrchestratorCheckpointError("INVALID_CHECKPOINT", f"{name} must be a list")
    return list(value)


class OrchestratorCheckpointStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else default_orchestrator_db_path()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_checkpoints (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    current_goal TEXT NOT NULL DEFAULT '',
                    decisions_json TEXT NOT NULL DEFAULT '[]',
                    active_tasks_json TEXT NOT NULL DEFAULT '[]',
                    blockers_json TEXT NOT NULL DEFAULT '[]',
                    next_actions_json TEXT NOT NULL DEFAULT '[]',
                    merge_deploy_state TEXT NOT NULL DEFAULT '',
                    source_chat TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_checkpoints_project_created "
                "ON chat_checkpoints(project_id, created_at DESC)"
            )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "summary": row["summary"],
            "current_goal": row["current_goal"],
            "decisions": json.loads(row["decisions_json"]),
            "active_tasks": json.loads(row["active_tasks_json"]),
            "blockers": json.loads(row["blockers_json"]),
            "next_actions": json.loads(row["next_actions_json"]),
            "merge_deploy_state": row["merge_deploy_state"],
            "source_chat": row["source_chat"],
            "created_at": row["created_at"],
        }

    def checkpoint(
        self,
        project_id: str,
        summary: str,
        *,
        current_goal: str = "",
        decisions: list[Any] | None = None,
        active_tasks: list[Any] | None = None,
        blockers: list[Any] | None = None,
        next_actions: list[Any] | None = None,
        merge_deploy_state: str = "",
        source_chat: str | None = None,
    ) -> dict[str, Any]:
        project_id = _project_id(project_id)
        payload = {
            "summary": _redact_recursive(str(summary or "")),
            "current_goal": _redact_recursive(str(current_goal or "")),
            "decisions": _redact_recursive(_list(decisions, "decisions")),
            "active_tasks": _redact_recursive(_list(active_tasks, "active_tasks")),
            "blockers": _redact_recursive(_list(blockers, "blockers")),
            "next_actions": _redact_recursive(_list(next_actions, "next_actions")),
            "merge_deploy_state": _redact_recursive(str(merge_deploy_state or "")),
            "source_chat": None if source_chat is None else _redact_recursive(str(source_chat)),
        }
        checkpoint_id = f"chatcp_{uuid.uuid4().hex}"
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_checkpoints(
                    id, project_id, summary, current_goal, decisions_json,
                    active_tasks_json, blockers_json, next_actions_json,
                    merge_deploy_state, source_chat, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id, project_id, payload["summary"], payload["current_goal"],
                    json.dumps(payload["decisions"], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload["active_tasks"], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload["blockers"], ensure_ascii=False, sort_keys=True),
                    json.dumps(payload["next_actions"], ensure_ascii=False, sort_keys=True),
                    payload["merge_deploy_state"], payload["source_chat"], created_at,
                ),
            )
            row = connection.execute("SELECT * FROM chat_checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        assert row is not None
        return self._row(row)

    def recover(self, project_id: str) -> dict[str, Any]:
        project_id = _project_id(project_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_checkpoints WHERE project_id=? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        if row is None:
            raise OrchestratorCheckpointError(
                "CHECKPOINT_NOT_FOUND", f"no chat checkpoint exists for project {project_id}"
            )
        return self._row(row)

    def list(self, project_id: str | None = None, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        try:
            limit = int(limit)
        except (TypeError, ValueError) as exc:
            raise OrchestratorCheckpointError("INVALID_LIMIT", "limit must be an integer") from exc
        if limit < 1 or limit > MAX_LIMIT:
            raise OrchestratorCheckpointError(
                "INVALID_LIMIT", f"limit must be between 1 and {MAX_LIMIT}"
            )
        params: list[Any] = []
        where = ""
        if project_id is not None:
            where = " WHERE project_id=?"
            params.append(_project_id(project_id))
        with self._connect() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM chat_checkpoints" + where, params
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM chat_checkpoints" + where
                + " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        return {"items": [self._row(row) for row in rows], "total": total, "limit": limit}
