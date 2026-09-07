"""Release lifecycle -- docs/REQUIREMENTS.md §20.6 Phase C. A genuinely
new, small state machine LAYERED ON TOP of an already-`COMPLETED`/
`INTEGRATED` task -- never an overload of `queue_store.py`'s own task
states (a release is its own row, referencing a `task_id` for
provenance, same "new concept, own table, no ripple into the existing
dispatch engine" discipline as every other Unified Task System
checkpoint in this project).

State machine (task's own explicit spec):
  MERGED -> RELEASE_CANDIDATE -> DEPLOYING -> DEPLOYED -> VERIFIED_PROD
                                      \\-> ROLLED_BACK (deploy itself failed)
                          DEPLOYED -> ROLLED_BACK (a post-deploy issue found)
                    VERIFIED_PROD -> ROLLED_BACK (an issue found even after verification)
`ROLLED_BACK` and `VERIFIED_PROD` are NOT both terminal -- only
`ROLLED_BACK` is; `VERIFIED_PROD` can still roll back later (a real
production incident discovered after the fact).
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

MERGED = "MERGED"
RELEASE_CANDIDATE = "RELEASE_CANDIDATE"
DEPLOYING = "DEPLOYING"
DEPLOYED = "DEPLOYED"
VERIFIED_PROD = "VERIFIED_PROD"
ROLLED_BACK = "ROLLED_BACK"
ALL_RELEASE_STATUSES = (MERGED, RELEASE_CANDIDATE, DEPLOYING, DEPLOYED, VERIFIED_PROD, ROLLED_BACK)

VALID_RELEASE_TRANSITIONS: dict[str, frozenset[str]] = {
    MERGED: frozenset({RELEASE_CANDIDATE}),
    RELEASE_CANDIDATE: frozenset({DEPLOYING}),
    DEPLOYING: frozenset({DEPLOYED, ROLLED_BACK}),
    DEPLOYED: frozenset({VERIFIED_PROD, ROLLED_BACK}),
    VERIFIED_PROD: frozenset({ROLLED_BACK}),
    ROLLED_BACK: frozenset(),
}

ENVIRONMENTS = ("dev", "test", "staging", "prod")


def is_valid_release_transition(from_status: str, to_status: str) -> bool:
    return to_status in VALID_RELEASE_TRANSITIONS.get(from_status, frozenset())


class InvalidReleaseTransitionError(ValueError):
    pass


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS releases (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            task_id TEXT NOT NULL,
            environment TEXT NOT NULL,
            status TEXT NOT NULL,
            artifact_ref TEXT NOT NULL,
            known_good_artifact_ref TEXT,
            rollback_plan TEXT,
            approved_by TEXT,
            approved_at TEXT,
            rollback_reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            deployed_at TEXT,
            verified_at TEXT,
            rolled_back_at TEXT
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_releases_project ON releases(project, created_at)")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS release_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT NOT NULL,
            reason TEXT,
            actor TEXT
        )
        """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_release_events_release ON release_events(release_id, id)")


RELEASE_MIGRATIONS = [
    Migration(1, "initial release lifecycle schema (releases/release_events)", _create_v1_schema),
]


def default_release_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_RELEASE_STORE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "release_store.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Release:
    id: str
    project: str
    task_id: str
    environment: str
    status: str
    artifact_ref: str
    known_good_artifact_ref: str | None
    rollback_plan: str | None
    approved_by: str | None
    approved_at: str | None
    rollback_reason: str | None
    created_at: str
    updated_at: str
    deployed_at: str | None
    verified_at: str | None
    rolled_back_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "project": self.project, "task_id": self.task_id, "environment": self.environment,
            "status": self.status, "artifact_ref": self.artifact_ref,
            "known_good_artifact_ref": self.known_good_artifact_ref, "rollback_plan": self.rollback_plan,
            "approved_by": self.approved_by, "approved_at": self.approved_at,
            "rollback_reason": self.rollback_reason, "created_at": self.created_at, "updated_at": self.updated_at,
            "deployed_at": self.deployed_at, "verified_at": self.verified_at, "rolled_back_at": self.rolled_back_at,
        }


def _from_row(row: sqlite3.Row) -> Release:
    return Release(
        id=row["id"], project=row["project"], task_id=row["task_id"], environment=row["environment"],
        status=row["status"], artifact_ref=row["artifact_ref"],
        known_good_artifact_ref=row["known_good_artifact_ref"], rollback_plan=row["rollback_plan"],
        approved_by=row["approved_by"], approved_at=row["approved_at"], rollback_reason=row["rollback_reason"],
        created_at=row["created_at"], updated_at=row["updated_at"], deployed_at=row["deployed_at"],
        verified_at=row["verified_at"], rolled_back_at=row["rolled_back_at"],
    )


class ReleaseStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_release_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, RELEASE_MIGRATIONS)
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

    def create_release(self, *, project: str, task_id: str, environment: str, artifact_ref: str,
                       known_good_artifact_ref: str | None = None, rollback_plan: str | None = None) -> Release:
        now = _now_iso()
        release_id = uuid.uuid4().hex
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO releases
                    (id, project, task_id, environment, status, artifact_ref, known_good_artifact_ref,
                     rollback_plan, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (release_id, project, task_id, environment, MERGED, artifact_ref, known_good_artifact_ref,
                 rollback_plan, now, now),
            )
            self._record_event_locked(connection, release_id=release_id, from_status=None, to_status=MERGED,
                                      reason="release created", actor=None)
            row = connection.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        return _from_row(row)

    def get_release(self, release_id: str) -> Release | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        return _from_row(row) if row is not None else None

    def list_releases(self, *, project: str | None = None) -> list[Release]:
        with self._connection() as connection:
            if project is not None:
                rows = connection.execute(
                    "SELECT * FROM releases WHERE project = ? ORDER BY created_at DESC", (project,),
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM releases ORDER BY created_at DESC").fetchall()
        return [_from_row(row) for row in rows]

    def _record_event_locked(self, connection: sqlite3.Connection, *, release_id: str, from_status: str | None,
                             to_status: str, reason: str | None, actor: str | None) -> None:
        connection.execute(
            "INSERT INTO release_events (release_id, timestamp, from_status, to_status, reason, actor) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (release_id, _now_iso(), from_status, to_status, reason, actor),
        )

    def list_events(self, release_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM release_events WHERE release_id = ? ORDER BY id ASC", (release_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def transition_release(self, release_id: str, to_status: str, *, reason: str | None = None,
                           actor: str | None = None, extra_fields: dict[str, Any] | None = None) -> Release:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such release: {release_id}")
            from_status = row["status"]
            if not is_valid_release_transition(from_status, to_status):
                raise InvalidReleaseTransitionError(
                    f"{release_id}: {from_status} -> {to_status} is not a valid release transition")
            now = _now_iso()
            fields: dict[str, Any] = {"status": to_status, "updated_at": now}
            if to_status == DEPLOYED:
                fields["deployed_at"] = now
            if to_status == VERIFIED_PROD:
                fields["verified_at"] = now
            if to_status == ROLLED_BACK:
                fields["rolled_back_at"] = now
                if reason:
                    fields["rollback_reason"] = reason
            if extra_fields:
                fields.update(extra_fields)
            set_clause = ", ".join(f"{key} = ?" for key in fields)
            connection.execute(f"UPDATE releases SET {set_clause} WHERE id = ?", (*fields.values(), release_id))
            self._record_event_locked(connection, release_id=release_id, from_status=from_status,
                                      to_status=to_status, reason=reason, actor=actor)
            updated_row = connection.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        return _from_row(updated_row)
