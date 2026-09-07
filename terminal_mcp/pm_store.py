"""PM/Orchestrator Agent -- persistence (docs/REQUIREMENTS.md §20.2).

Two small, related tables in ONE db file (same "one feature, one file"
posture as queue_store.py holding queue_tasks/queue_lanes/queue_events
together), same connection/schema/permission pattern as every other
store in this project (0700 state dir, 0600 db file, WAL, row_factory=
Row, schema.py's Migration/apply_migrations framework):

- `capability_profiles` -- one row per (node_id, session), the same
  `(node_id, session_name)` composite-identity convention session_
  registry.py already established (never a second naming scheme). What
  a given session CAN do: os/runtime_tools/project_affinity/role/
  skills. Declarative only -- created/updated by a human or an
  onboarding script, never inferred from a display name (task's own
  explicit "never inferred from display name").
- `pm_decisions` -- an APPEND-ONLY log, one row per routing decision
  ever made for a task (never overwritten -- task's own explicit
  "routing reason, task history ... phải persist", a real audit trail,
  not just the latest verdict). Queried by task_id for the Kanban
  card's own "why" and the `pm_explain` tool.

Deliberately NOT a new column on `queue_tasks`: the router only ever
READS a task (via QueueService) and, when it decides to actually
assign one, calls the EXISTING `QueueService.assign_task` -- so no
change to queue_store.py's schema/dispatch engine is needed for this
feature to exist. `online`/`busy`/`queue_depth` are NOT stored here --
they are live-derived by the caller (pm_service.py) from the real
node registry / queue store at decision time, never persisted as a
stale snapshot that could drift.
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

PM_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: capability_profiles + pm_decisions", lambda connection: None),
]


def default_pm_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_PM_STORE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "pm_store.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else [])


def _load_list(raw: str | None) -> tuple[Any, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    return tuple(parsed) if isinstance(parsed, list) else ()


def _load_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class CapabilityProfile:
    node_id: str
    session: str
    os: str | None
    runtime_tools: tuple[str, ...]
    project_affinity: str | None
    role: str | None
    skills: tuple[dict[str, Any], ...]  # [{"name": "wpf", "confidence": 0.9}, ...]
    permissions_note: str | None
    created_at: str
    updated_at: str

    def key(self) -> str:
        # Same "node_id/session" qualified-name shape controller.py's
        # own resolve_session and session_registry.py's SessionRecord.
        # key() already use -- never a third naming convention.
        return f"{self.node_id}/{self.session}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id, "session": self.session, "os": self.os,
            "runtime_tools": list(self.runtime_tools), "project_affinity": self.project_affinity,
            "role": self.role, "skills": [dict(s) for s in self.skills],
            "permissions_note": self.permissions_note,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PMDecision:
    id: int
    decision_id: str
    task_id: str
    mode: str  # SUGGEST | AUTO
    status: str  # ROUTED | SUGGESTED | APPROVED_AND_ASSIGNED | NO_ELIGIBLE_WORKER | BLOCKED
    chosen_node_id: str | None
    chosen_session: str | None
    reason: str
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "decision_id": self.decision_id, "task_id": self.task_id, "mode": self.mode,
            "status": self.status, "chosen_node_id": self.chosen_node_id, "chosen_session": self.chosen_session,
            "reason": self.reason, "score_breakdown": self.score_breakdown, "evidence": self.evidence,
            "created_at": self.created_at,
        }


def _profile_from_row(row: sqlite3.Row) -> CapabilityProfile:
    return CapabilityProfile(
        node_id=row["node_id"], session=row["session"], os=row["os"],
        runtime_tools=_load_list(row["runtime_tools"]), project_affinity=row["project_affinity"],
        role=row["role"], skills=_load_list(row["skills"]), permissions_note=row["permissions_note"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _decision_from_row(row: sqlite3.Row) -> PMDecision:
    return PMDecision(
        id=row["id"], decision_id=row["decision_id"], task_id=row["task_id"], mode=row["mode"],
        status=row["status"], chosen_node_id=row["chosen_node_id"], chosen_session=row["chosen_session"],
        reason=row["reason"], score_breakdown=_load_dict(row["score_breakdown"]),
        evidence=_load_dict(row["evidence"]), created_at=row["created_at"],
    )


class PMStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_pm_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_profiles (
                    node_id TEXT NOT NULL,
                    session TEXT NOT NULL,
                    os TEXT,
                    runtime_tools TEXT NOT NULL DEFAULT '[]',
                    project_affinity TEXT,
                    role TEXT,
                    skills TEXT NOT NULL DEFAULT '[]',
                    permissions_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (node_id, session)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pm_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    decision_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    chosen_node_id TEXT,
                    chosen_session TEXT,
                    reason TEXT NOT NULL,
                    score_breakdown TEXT NOT NULL DEFAULT '{}',
                    evidence TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pm_decisions_task ON pm_decisions(task_id, id)"
            )
            apply_migrations(connection, PM_MIGRATIONS)
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

    # -- capability profiles ----------------------------------------------

    def upsert_capability(self, node_id: str, session: str, *, os: str | None = None,
                          runtime_tools: list[str] | None = None, project_affinity: str | None = None,
                          role: str | None = None, skills: list[dict[str, Any]] | None = None,
                          permissions_note: str | None = None) -> CapabilityProfile:
        """INSERT-or-update, same shape as session_registry.py's own
        upsert_seen -- a repeat call for the same (node_id, session)
        updates the row in place (COALESCE-style: a field left None
        keeps its previous stored value rather than being blanked,
        so a caller updating just `skills` doesn't need to re-supply
        `os`/`runtime_tools` every time)."""
        now = _now_iso()
        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM capability_profiles WHERE node_id = ? AND session = ?", (node_id, session),
            ).fetchone()
            merged = {
                "os": os if os is not None else (existing["os"] if existing else None),
                "runtime_tools": runtime_tools if runtime_tools is not None
                    else (_load_list(existing["runtime_tools"]) if existing else ()),
                "project_affinity": project_affinity if project_affinity is not None
                    else (existing["project_affinity"] if existing else None),
                "role": role if role is not None else (existing["role"] if existing else None),
                "skills": skills if skills is not None else (_load_list(existing["skills"]) if existing else ()),
                "permissions_note": permissions_note if permissions_note is not None
                    else (existing["permissions_note"] if existing else None),
            }
            connection.execute(
                """
                INSERT INTO capability_profiles
                    (node_id, session, os, runtime_tools, project_affinity, role, skills, permissions_note,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(node_id, session) DO UPDATE SET
                    os = excluded.os, runtime_tools = excluded.runtime_tools,
                    project_affinity = excluded.project_affinity, role = excluded.role,
                    skills = excluded.skills, permissions_note = excluded.permissions_note,
                    updated_at = excluded.updated_at
                """,
                (node_id, session, merged["os"], _dump(list(merged["runtime_tools"])),
                 merged["project_affinity"], merged["role"], _dump(list(merged["skills"])),
                 merged["permissions_note"], now if existing is None else existing["created_at"], now),
            )
            row = connection.execute(
                "SELECT * FROM capability_profiles WHERE node_id = ? AND session = ?", (node_id, session),
            ).fetchone()
        return _profile_from_row(row)

    def get_capability(self, node_id: str, session: str) -> CapabilityProfile | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM capability_profiles WHERE node_id = ? AND session = ?", (node_id, session),
            ).fetchone()
        return _profile_from_row(row) if row is not None else None

    def list_capabilities(self) -> list[CapabilityProfile]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM capability_profiles ORDER BY node_id, session"
            ).fetchall()
        return [_profile_from_row(row) for row in rows]

    def delete_capability(self, node_id: str, session: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM capability_profiles WHERE node_id = ? AND session = ?", (node_id, session),
            )
        return cursor.rowcount > 0

    # -- PM decisions (append-only audit trail) ----------------------------

    def record_decision(self, *, task_id: str, mode: str, status: str, reason: str,
                        chosen_node_id: str | None = None, chosen_session: str | None = None,
                        score_breakdown: dict[str, Any] | None = None,
                        evidence: dict[str, Any] | None = None) -> PMDecision:
        decision_id = uuid.uuid4().hex
        now = _now_iso()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pm_decisions
                    (decision_id, task_id, mode, status, chosen_node_id, chosen_session, reason,
                     score_breakdown, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, task_id, mode, status, chosen_node_id, chosen_session, reason,
                 json.dumps(score_breakdown or {}), json.dumps(evidence or {}), now),
            )
            row = connection.execute("SELECT * FROM pm_decisions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return _decision_from_row(row)

    def list_decisions_for_task(self, task_id: str) -> list[PMDecision]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM pm_decisions WHERE task_id = ? ORDER BY id DESC", (task_id,),
            ).fetchall()
        return [_decision_from_row(row) for row in rows]

    def latest_decision_for_task(self, task_id: str) -> PMDecision | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM pm_decisions WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
            ).fetchone()
        return _decision_from_row(row) if row is not None else None

    def latest_decisions_for_tasks(self, task_ids: list[str]) -> dict[str, PMDecision]:
        """Bulk form of latest_decision_for_task -- ONE query for the
        Kanban board's own read (never N+1 per card, same discipline as
        queue_service.py's own pending_counts() bulk read)."""
        if not task_ids:
            return {}
        with self._connection() as connection:
            placeholders = ",".join("?" for _ in task_ids)
            rows = connection.execute(
                f"SELECT * FROM pm_decisions WHERE task_id IN ({placeholders}) ORDER BY id DESC",
                task_ids,
            ).fetchall()
        latest: dict[str, PMDecision] = {}
        for row in rows:
            decision = _decision_from_row(row)
            if decision.task_id not in latest:  # first row seen per task_id is the newest (ORDER BY id DESC)
                latest[decision.task_id] = decision
        return latest
