"""Planner (task-breaking) -- persistence (docs/REQUIREMENTS.md §20.3).
One table, `plan_proposals` -- an append-oriented record of every split
ever proposed for a parent task (SUGGEST computes+persists a proposal
without creating anything; AUTO or a later `approve_split` call applies
it for real). Same connection/schema/permission pattern as every other
store in this project (0700 state dir, 0600 db file, WAL, row_factory=
Row, schema.py's Migration/apply_migrations framework).

Deliberately NOT a new column on `queue_tasks`: `parent_task_id`/
`acceptance_criteria` live in each CHILD task's own `metadata` (created
via the existing QueueService.create_task) -- same implementation
choice, same reasoning, as the Kanban checkpoint's own UNASSIGNED_LANE
decision and the PM checkpoint's own metadata-based routing
requirements (see docs/REQUIREMENTS.md §20.1a/§20.2a): this feature
only ever creates ordinary tasks through the EXISTING canonical path
and reads them back through the EXISTING canonical board/status calls
-- no change to queue_store.py's own schema was needed for it either.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

PLANNER_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: plan_proposals", lambda connection: None),
]

PROPOSED = "PROPOSED"
APPROVED = "APPROVED"
REJECTED = "REJECTED"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


def default_planner_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_PLANNER_STORE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "planner_store.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


@dataclass(frozen=True)
class PlanProposal:
    id: int
    proposal_id: str
    parent_task_id: str
    mode: str  # SUGGEST | AUTO
    status: str  # PROPOSED | APPROVED | REJECTED | NEEDS_CLARIFICATION
    children_spec: tuple[dict[str, Any], ...]
    reason: str
    child_task_ids: tuple[str, ...] = ()
    created_at: str = ""
    decided_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "proposal_id": self.proposal_id, "parent_task_id": self.parent_task_id,
            "mode": self.mode, "status": self.status, "children_spec": list(self.children_spec),
            "reason": self.reason, "child_task_ids": list(self.child_task_ids),
            "created_at": self.created_at, "decided_at": self.decided_at,
        }


def _from_row(row: sqlite3.Row) -> PlanProposal:
    return PlanProposal(
        id=row["id"], proposal_id=row["proposal_id"], parent_task_id=row["parent_task_id"], mode=row["mode"],
        status=row["status"], children_spec=tuple(_load_list(row["children_spec"])), reason=row["reason"],
        child_task_ids=tuple(_load_list(row["child_task_ids"])), created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


class PlannerStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_planner_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proposal_id TEXT NOT NULL,
                    parent_task_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    children_spec TEXT NOT NULL DEFAULT '[]',
                    reason TEXT NOT NULL,
                    child_task_ids TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_plan_proposals_parent ON plan_proposals(parent_task_id, id)"
            )
            apply_migrations(connection, PLANNER_MIGRATIONS)
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

    def create_proposal(self, *, parent_task_id: str, mode: str, status: str,
                        children_spec: list[dict[str, Any]], reason: str,
                        child_task_ids: list[str] | None = None) -> PlanProposal:
        proposal_id = uuid.uuid4().hex
        now = _now_iso()
        decided_at = now if status in (APPROVED, REJECTED) else None
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO plan_proposals
                    (proposal_id, parent_task_id, mode, status, children_spec, reason, child_task_ids,
                     created_at, decided_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (proposal_id, parent_task_id, mode, status, json.dumps(children_spec), reason,
                 json.dumps(child_task_ids or []), now, decided_at),
            )
            row = connection.execute("SELECT * FROM plan_proposals WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _from_row(row)

    def get_proposal(self, proposal_id: str) -> PlanProposal | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM plan_proposals WHERE proposal_id = ?", (proposal_id,),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def mark_decided(self, proposal_id: str, *, status: str, child_task_ids: list[str] | None = None) -> PlanProposal:
        with self._connection() as connection:
            connection.execute(
                "UPDATE plan_proposals SET status = ?, child_task_ids = ?, decided_at = ? WHERE proposal_id = ?",
                (status, json.dumps(child_task_ids or []), _now_iso(), proposal_id),
            )
            row = connection.execute(
                "SELECT * FROM plan_proposals WHERE proposal_id = ?", (proposal_id,),
            ).fetchone()
        return _from_row(row)

    def list_proposals_for_parent(self, parent_task_id: str) -> list[PlanProposal]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM plan_proposals WHERE parent_task_id = ? ORDER BY id DESC", (parent_task_id,),
            ).fetchall()
        return [_from_row(row) for row in rows]
