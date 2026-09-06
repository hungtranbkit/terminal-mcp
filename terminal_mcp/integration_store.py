"""Integration Agent -- persistence + state machine for the per-project
merge/test pipeline (task: "3-role model: Coding A/B + Integration
Agent" -- Supervisor Queue v2's own natural extension for throughput on
projects with 2+ parallel coding sessions).

WHY A SEPARATE STORE FROM queue_store.py (audited first, per repo
convention): queue_store.py's whole schema is scoped by SESSION (one
lane per session). This feature's own scoping unit is different --
PROJECT (task's own explicit item 6: "Per-project integration queue,
không global queue"). A project has exactly one Integration Agent
pipeline but MAY be fed by multiple coding sessions (window, window2,
...), so "session" is the wrong primary key here. Rather than bolt a
second, incompatible scoping concept onto queue_tasks/queue_lanes, this
is its own store, same SQLite/Migration conventions as queue_store.py
(0700 dir, 0600 file, WAL, row_factory=Row, schema.py's Migration/
apply_migrations) -- but the two stores are DELIBERATELY linked only
through plain data: a Handoff carries the originating session name and
QueueTask id (task_id/origin_session) as provenance, and a REWORK_
REQUIRED handoff's remediation is a perfectly ordinary new task pushed
into that session's own existing queue_store lane via queue_service
(item 2's own "coding worker vẫn có thể tiếp tục feature kế tiếp" --
rework never blocks a coding session's own queue, it just becomes
another queued task in it).

THREE ROLES, one more time, explicitly (per the task's own item 3):
  - Coding sessions (window/window2/...): code features on their own
    branch/worktree continuously. Never blocked on merge/test. Never
    self-merge to main.
  - Coordinator Agent (coordinator.py, unchanged by this feature):
    still the ONLY gate before a coding task dispatches -- dependency/
    risk/identity checks. Explicitly NOT a merge/test bottleneck; it
    has no opinion about integration state beyond an optional
    dependency reference (a coding task CAN declare
    metadata.depends_on_handoff -- see queue_store.py's existing
    depends_on gating, unchanged).
  - Integration Agent (integration_engine.py, layered on this store):
    the ONLY thing that merges, tests, and (only when green AND
    policy-permitted) promotes to main. Runs against its own
    per-project queue of Handoffs, completely decoupled from the
    coding sessions' own per-session task queues -- a coding session
    publishes a Handoff and immediately moves on to its next task; it
    never waits for the Integration Agent's own pipeline.

HANDOFF is an IMMUTABLE publication (task's own item 4: "publish
immutable handoff {project, task_id, branch, commit_sha, base_sha,
changed_paths, test_summary, artifacts}"): once created by
publish_handoff, its own provenance fields (project/task_id/
origin_session/branch/commit_sha/base_sha/changed_paths/test_summary/
artifacts) are NEVER modified -- only its STATUS and integration-
specific fields (merge_commit_sha/conflict info/targeted_test_result/
rework_task_id/regression_batch_id) change, exactly the same "state
machine on top of an otherwise-frozen record" discipline queue_store.py
already applies to a QueueTask's own prompt/title/metadata.

State machine (task's own item 7, kept verbatim):
  READY_FOR_INTEGRATION -> CLAIMED -> MERGING -> TARGETED_TEST
    -> INTEGRATED | REWORK_REQUIRED | BLOCKED
Batch regression (a project-level concept, spanning many handoffs):
  REGRESSION_PENDING -> REGRESSION_RUNNING -> MERGE_READY | REGRESSION_FAILED
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

# -- Handoff status state machine ----------------------------------------

READY_FOR_INTEGRATION = "READY_FOR_INTEGRATION"
CLAIMED = "CLAIMED"
MERGING = "MERGING"
TARGETED_TEST = "TARGETED_TEST"
INTEGRATED = "INTEGRATED"
REWORK_REQUIRED = "REWORK_REQUIRED"
BLOCKED = "BLOCKED"

ALL_HANDOFF_STATUSES = (READY_FOR_INTEGRATION, CLAIMED, MERGING, TARGETED_TEST, INTEGRATED, REWORK_REQUIRED, BLOCKED)
HANDOFF_TERMINAL_STATUSES = (INTEGRATED,)
"""REWORK_REQUIRED/BLOCKED are deliberately NOT terminal -- an operator
can retry_handoff them (mirroring queue_store.py's BLOCKED/FAILED
retry_task), and the common case (a rework task republishing a fresh
Handoff) never needs to touch the old row again anyway."""

HANDOFF_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    READY_FOR_INTEGRATION: frozenset({CLAIMED, BLOCKED}),
    CLAIMED: frozenset({
        MERGING,  # pre-merge review passed
        REWORK_REQUIRED,  # pre-merge review found something real, routed to the owning session
        READY_FOR_INTEGRATION,  # stale-claim reconcile
        BLOCKED,
    }),
    MERGING: frozenset({
        TARGETED_TEST,  # merge succeeded (no conflict, or a safe mechanical auto-resolve)
        REWORK_REQUIRED,  # a real conflict, never guessed at -- routed to the owning session
        BLOCKED,  # the git operation itself failed unexpectedly (fail-closed)
        READY_FOR_INTEGRATION,  # stale-claim reconcile after a crash mid-merge
    }),
    TARGETED_TEST: frozenset({
        INTEGRATED,           # targeted test passed
        REWORK_REQUIRED,      # targeted test failed
        BLOCKED,              # the test tooling itself failed to run
        READY_FOR_INTEGRATION,  # stale-claim reconcile
    }),
    INTEGRATED: frozenset(),
    REWORK_REQUIRED: frozenset({READY_FOR_INTEGRATION}),  # only via explicit retry_handoff
    BLOCKED: frozenset({READY_FOR_INTEGRATION}),           # only via explicit retry_handoff
}


class InvalidHandoffTransitionError(ValueError):
    pass


def is_valid_handoff_transition(from_status: str, to_status: str) -> bool:
    return to_status in HANDOFF_VALID_TRANSITIONS.get(from_status, frozenset())


# -- Batch/regression status ----------------------------------------------

REGRESSION_PENDING = "REGRESSION_PENDING"
REGRESSION_RUNNING = "REGRESSION_RUNNING"
MERGE_READY = "MERGE_READY"
REGRESSION_FAILED = "REGRESSION_FAILED"

ALL_BATCH_STATUSES = (REGRESSION_PENDING, REGRESSION_RUNNING, MERGE_READY, REGRESSION_FAILED)
BATCH_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    REGRESSION_PENDING: frozenset({REGRESSION_RUNNING}),
    REGRESSION_RUNNING: frozenset({MERGE_READY, REGRESSION_FAILED}),
    MERGE_READY: frozenset(),  # promotion to main is a separate, explicit action -- see promote_batch
    REGRESSION_FAILED: frozenset({REGRESSION_PENDING}),  # explicit re-run after rework lands
}


def is_valid_batch_transition(from_status: str, to_status: str) -> bool:
    return to_status in BATCH_VALID_TRANSITIONS.get(from_status, frozenset())


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id() -> str:
    return uuid.uuid4().hex


def _parse_json_object(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def default_integration_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_INTEGRATION_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "integration.db"


@dataclass(frozen=True)
class Handoff:
    id: str
    project: str
    task_id: str
    origin_session: str
    branch: str
    commit_sha: str
    base_sha: str
    changed_paths: tuple[str, ...]
    test_summary: dict[str, Any]
    artifacts: dict[str, Any]
    status: str
    created_at: str
    updated_at: str
    claimed_by: str | None = None
    claim_token: str | None = None
    lease_expires_at: str | None = None
    merge_commit_sha: str | None = None
    conflict_detected: bool = False
    conflict_paths: tuple[str, ...] = ()
    targeted_test_result: dict[str, Any] = field(default_factory=dict)
    rework_task_id: str | None = None
    rework_reason: str | None = None
    regression_batch_id: str | None = None
    integrated_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Handoff":
        return cls(
            id=row["id"], project=row["project"], task_id=row["task_id"], origin_session=row["origin_session"],
            branch=row["branch"], commit_sha=row["commit_sha"], base_sha=row["base_sha"],
            changed_paths=tuple(_parse_json_list(row["changed_paths"])),
            test_summary=_parse_json_object(row["test_summary"]), artifacts=_parse_json_object(row["artifacts"]),
            status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
            claimed_by=row["claimed_by"], claim_token=row["claim_token"], lease_expires_at=row["lease_expires_at"],
            merge_commit_sha=row["merge_commit_sha"], conflict_detected=bool(row["conflict_detected"]),
            conflict_paths=tuple(_parse_json_list(row["conflict_paths"])),
            targeted_test_result=_parse_json_object(row["targeted_test_result"]),
            rework_task_id=row["rework_task_id"], rework_reason=row["rework_reason"],
            regression_batch_id=row["regression_batch_id"], integrated_at=row["integrated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "project": self.project, "task_id": self.task_id, "origin_session": self.origin_session,
            "branch": self.branch, "commit_sha": self.commit_sha, "base_sha": self.base_sha,
            "changed_paths": list(self.changed_paths), "test_summary": self.test_summary,
            "artifacts": self.artifacts, "status": self.status, "created_at": self.created_at,
            "updated_at": self.updated_at, "claimed_by": self.claimed_by, "claim_token": self.claim_token,
            "lease_expires_at": self.lease_expires_at, "merge_commit_sha": self.merge_commit_sha,
            "conflict_detected": self.conflict_detected, "conflict_paths": list(self.conflict_paths),
            "targeted_test_result": self.targeted_test_result, "rework_task_id": self.rework_task_id,
            "rework_reason": self.rework_reason, "regression_batch_id": self.regression_batch_id,
            "integrated_at": self.integrated_at,
        }


@dataclass(frozen=True)
class RegressionBatch:
    id: str
    project: str
    status: str
    handoff_ids: tuple[str, ...]
    created_at: str
    started_at: str | None
    finished_at: str | None
    full_test_result: dict[str, Any]
    promoted_to_main: bool
    promoted_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RegressionBatch":
        return cls(
            id=row["id"], project=row["project"], status=row["status"],
            handoff_ids=tuple(_parse_json_list(row["handoff_ids"])),
            created_at=row["created_at"], started_at=row["started_at"], finished_at=row["finished_at"],
            full_test_result=_parse_json_object(row["full_test_result"]),
            promoted_to_main=bool(row["promoted_to_main"]), promoted_at=row["promoted_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "project": self.project, "status": self.status, "handoff_ids": list(self.handoff_ids),
            "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
            "full_test_result": self.full_test_result, "promoted_to_main": self.promoted_to_main,
            "promoted_at": self.promoted_at,
        }


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE integration_pipelines (
            project TEXT PRIMARY KEY,
            paused INTEGER NOT NULL DEFAULT 0,
            paused_reason TEXT,
            repo_path TEXT NOT NULL,
            integration_branch TEXT NOT NULL DEFAULT 'integration',
            main_branch TEXT NOT NULL DEFAULT 'main',
            targeted_test_command TEXT,
            full_regression_command TEXT,
            batch_size INTEGER NOT NULL DEFAULT 3,
            batch_max_wait_seconds REAL NOT NULL DEFAULT 1800,
            auto_promote_enabled INTEGER NOT NULL DEFAULT 0,
            session_ownership TEXT,
            review_depth TEXT NOT NULL DEFAULT 'basic',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE integration_handoffs (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            task_id TEXT NOT NULL,
            origin_session TEXT NOT NULL,
            branch TEXT NOT NULL,
            commit_sha TEXT NOT NULL,
            base_sha TEXT NOT NULL,
            changed_paths TEXT,
            test_summary TEXT,
            artifacts TEXT,
            status TEXT NOT NULL DEFAULT 'READY_FOR_INTEGRATION',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            claimed_by TEXT,
            claim_token TEXT,
            lease_expires_at TEXT,
            merge_commit_sha TEXT,
            conflict_detected INTEGER NOT NULL DEFAULT 0,
            conflict_paths TEXT,
            targeted_test_result TEXT,
            rework_task_id TEXT,
            rework_reason TEXT,
            regression_batch_id TEXT,
            integrated_at TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_integration_handoffs_project_status "
                       "ON integration_handoffs(project, status, created_at)")
    connection.execute(
        """
        CREATE TABLE integration_batches (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'REGRESSION_PENDING',
            handoff_ids TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            full_test_result TEXT,
            promoted_to_main INTEGER NOT NULL DEFAULT 0,
            promoted_at TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_integration_batches_project ON integration_batches(project, created_at)")
    connection.execute(
        """
        CREATE TABLE integration_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            project TEXT NOT NULL,
            handoff_id TEXT,
            batch_id TEXT,
            event_type TEXT NOT NULL,
            from_status TEXT,
            to_status TEXT,
            reason TEXT,
            metadata TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_integration_events_project ON integration_events(project, id)")


INTEGRATION_MIGRATIONS = [
    Migration(1, "initial Integration Agent schema (pipelines/handoffs/batches/events)", _create_v1_schema),
]


class IntegrationStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_integration_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, INTEGRATION_MIGRATIONS)
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
            yield connection
            connection.commit()
        finally:
            connection.close()

    # -- project pipeline config -------------------------------------------

    def configure_pipeline(self, project: str, *, repo_path: str, integration_branch: str = "integration",
                           main_branch: str = "main", targeted_test_command: list[str] | None = None,
                           full_regression_command: list[str] | None = None, batch_size: int = 3,
                           batch_max_wait_seconds: float = 1800, auto_promote_enabled: bool = False,
                           session_ownership: dict[str, str] | None = None,
                           review_depth: str = "basic") -> dict[str, Any]:
        """Creates or updates `project`'s pipeline config. session_ownership
        is an OPTIONAL {path_prefix: session_name} map -- the rework-
        routing fallback heuristic (item 9) used only when a handoff's
        own origin_session no longer looks like the right owner (see
        integration_engine.py's own resolve_rework_owner). review_depth
        ("basic" | "deep") governs integration_reviewer.py's own pre-
        merge review gate depth."""
        now = iso_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO integration_pipelines (project, paused, paused_reason, repo_path, integration_branch, "
                "main_branch, targeted_test_command, full_regression_command, batch_size, batch_max_wait_seconds, "
                "auto_promote_enabled, session_ownership, review_depth, created_at, updated_at) "
                "VALUES (?, 0, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(project) DO UPDATE SET repo_path=excluded.repo_path, "
                "integration_branch=excluded.integration_branch, main_branch=excluded.main_branch, "
                "targeted_test_command=excluded.targeted_test_command, "
                "full_regression_command=excluded.full_regression_command, batch_size=excluded.batch_size, "
                "batch_max_wait_seconds=excluded.batch_max_wait_seconds, "
                "auto_promote_enabled=excluded.auto_promote_enabled, "
                "session_ownership=excluded.session_ownership, review_depth=excluded.review_depth, "
                "updated_at=excluded.updated_at",
                (project, repo_path, integration_branch, main_branch,
                 json.dumps(targeted_test_command) if targeted_test_command else None,
                 json.dumps(full_regression_command) if full_regression_command else None,
                 batch_size, batch_max_wait_seconds, int(auto_promote_enabled),
                 json.dumps(session_ownership) if session_ownership else None, review_depth, now, now),
            )
        return self.get_pipeline(project)

    def get_pipeline(self, project: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM integration_pipelines WHERE project = ?", (project,)).fetchone()
        if row is None:
            return None
        return {
            "project": row["project"], "paused": bool(row["paused"]), "paused_reason": row["paused_reason"],
            "repo_path": row["repo_path"], "integration_branch": row["integration_branch"],
            "main_branch": row["main_branch"],
            "targeted_test_command": _parse_json_list(row["targeted_test_command"]),
            "full_regression_command": _parse_json_list(row["full_regression_command"]),
            "batch_size": row["batch_size"], "batch_max_wait_seconds": row["batch_max_wait_seconds"],
            "auto_promote_enabled": bool(row["auto_promote_enabled"]),
            "session_ownership": _parse_json_object(row["session_ownership"]),
            "review_depth": row["review_depth"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def rename_session(self, old_session: str, new_session: str) -> dict[str, Any]:
        """Rename Session feature: a coding session's own handoffs
        (origin_session -- who this Handoff came FROM, read by
        integration_engine.py's resolve_rework_owner to route a failed
        review/test back to the right worker) must keep pointing at the
        SAME worker after a rename, not silently stop matching. Also
        rewrites any `session_ownership` VALUE (never a key -- keys are
        path prefixes, not session names) equal to old_session, across
        every project's pipeline config, for the same reason. Purely a
        string rewrite -- no handoff/batch/pipeline row's own identity,
        status, or history changes."""
        now = iso_now()
        with self._connection() as connection:
            handoffs_updated = connection.execute(
                "UPDATE integration_handoffs SET origin_session = ?, updated_at = ? WHERE origin_session = ?",
                (new_session, now, old_session),
            ).rowcount
            pipelines_updated = 0
            for row in connection.execute("SELECT project, session_ownership FROM integration_pipelines").fetchall():
                ownership = _parse_json_object(row["session_ownership"])
                if not ownership or old_session not in ownership.values():
                    continue
                rewritten = {path: (new_session if owner == old_session else owner)
                            for path, owner in ownership.items()}
                connection.execute(
                    "UPDATE integration_pipelines SET session_ownership = ?, updated_at = ? WHERE project = ?",
                    (json.dumps(rewritten), now, row["project"]),
                )
                pipelines_updated += 1
        return {"old_session": old_session, "new_session": new_session,
               "handoffs_updated": handoffs_updated, "pipelines_updated": pipelines_updated}

    def pause_pipeline(self, project: str, *, reason: str | None = None) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE integration_pipelines SET paused = 1, paused_reason = ?, updated_at = ? "
                              "WHERE project = ?", (reason, iso_now(), project))
            self._record_event_locked(connection, project=project, handoff_id=None, batch_id=None,
                                      event_type="PIPELINE_PAUSED", reason=reason)

    def resume_pipeline(self, project: str) -> None:
        with self._connection() as connection:
            connection.execute("UPDATE integration_pipelines SET paused = 0, paused_reason = NULL, updated_at = ? "
                              "WHERE project = ?", (iso_now(), project))
            self._record_event_locked(connection, project=project, handoff_id=None, batch_id=None,
                                      event_type="PIPELINE_RESUMED", reason=None)

    # -- handoffs ------------------------------------------------------------

    def publish_handoff(self, *, project: str, task_id: str, origin_session: str, branch: str, commit_sha: str,
                        base_sha: str, changed_paths: list[str] | None = None,
                        test_summary: dict[str, Any] | None = None,
                        artifacts: dict[str, Any] | None = None) -> Handoff:
        """The ONE way a Handoff is created -- immutable provenance
        fields, set exactly once. A coding session (or queue_engine.py's
        own auto-publish hook, once a COMPLETED task declares
        metadata.integration_required) calls this and immediately moves
        on; nothing here blocks on integration."""
        handoff_id = new_id()
        now = iso_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO integration_handoffs (id, project, task_id, origin_session, branch, commit_sha, "
                "base_sha, changed_paths, test_summary, artifacts, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (handoff_id, project, task_id, origin_session, branch, commit_sha, base_sha,
                 json.dumps(changed_paths or []), json.dumps(test_summary or {}), json.dumps(artifacts or {}),
                 READY_FOR_INTEGRATION, now, now),
            )
            self._record_event_locked(connection, project=project, handoff_id=handoff_id, batch_id=None,
                                      event_type="HANDOFF_PUBLISHED", reason=None,
                                      metadata={"branch": branch, "commit_sha": commit_sha})
        return self.get_handoff(handoff_id)

    def get_handoff(self, handoff_id: str) -> Handoff | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM integration_handoffs WHERE id = ?", (handoff_id,)).fetchone()
        return Handoff.from_row(row) if row else None

    def list_handoffs(self, project: str, *, status: str | None = None, limit: int = 100) -> list[Handoff]:
        with self._connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM integration_handoffs WHERE project = ? AND status = ? "
                    "ORDER BY created_at ASC LIMIT ?", (project, status, limit)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM integration_handoffs WHERE project = ? ORDER BY created_at ASC LIMIT ?",
                    (project, limit)).fetchall()
        return [Handoff.from_row(row) for row in rows]

    _ACTIVE_HANDOFF_STATUSES = (CLAIMED, MERGING, TARGETED_TEST)
    """A project's Integration Agent processes ONE handoff at a time --
    same one-at-a-time discipline queue_store.py's own _ACTIVE_STATUSES
    enforces per session, applied here per project."""

    def claim_next_handoff(self, project: str, *, claimed_by: str, lease_seconds: float = 300.0) -> Handoff | None:
        """Atomic claim (same BEGIN IMMEDIATE pattern as queue_store.py's
        claim_next_task, and for the identical reason -- closes the
        TOCTOU window a concurrent/re-entered Integration Agent tick
        could otherwise double-claim the same handoff through)."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            pipeline = connection.execute("SELECT paused FROM integration_pipelines WHERE project = ?",
                                          (project,)).fetchone()
            if pipeline is None or pipeline["paused"]:
                connection.rollback()
                return None
            active = connection.execute(
                "SELECT id FROM integration_handoffs WHERE project = ? AND status IN (?, ?, ?)",
                (project, *self._ACTIVE_HANDOFF_STATUSES),
            ).fetchone()
            if active is not None:
                connection.rollback()
                return None
            row = connection.execute(
                "SELECT * FROM integration_handoffs WHERE project = ? AND status = ? ORDER BY created_at ASC LIMIT 1",
                (project, READY_FOR_INTEGRATION),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            claim_token = new_id()
            lease_expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + lease_seconds))
            updated = self._transition_handoff_locked(
                connection, row["id"], row["status"], CLAIMED, event_type="CLAIMED", reason=None,
                extra_fields={"claimed_by": claimed_by, "claim_token": claim_token,
                             "lease_expires_at": lease_expires_at},
            )
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def transition_handoff(self, handoff_id: str, to_status: str, *, event_type: str, reason: str | None = None,
                           extra_fields: dict[str, Any] | None = None) -> Handoff:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM integration_handoffs WHERE id = ?", (handoff_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such handoff: {handoff_id}")
            return self._transition_handoff_locked(connection, handoff_id, row["status"], to_status,
                                                    event_type=event_type, reason=reason, extra_fields=extra_fields)

    def _transition_handoff_locked(self, connection: sqlite3.Connection, handoff_id: str, from_status: str,
                                   to_status: str, *, event_type: str, reason: str | None,
                                   extra_fields: dict[str, Any] | None = None) -> Handoff:
        if not is_valid_handoff_transition(from_status, to_status):
            raise InvalidHandoffTransitionError(f"{handoff_id}: {from_status} -> {to_status} is not valid")
        now = iso_now()
        fields: dict[str, Any] = {"status": to_status, "updated_at": now}
        if to_status == INTEGRATED:
            fields["integrated_at"] = now
        if to_status == READY_FOR_INTEGRATION:
            fields.setdefault("claimed_by", None)
            fields.setdefault("claim_token", None)
            fields.setdefault("lease_expires_at", None)
        if reason is not None and to_status in (REWORK_REQUIRED, BLOCKED):
            fields["rework_reason"] = reason
        if extra_fields:
            for key, value in extra_fields.items():
                if isinstance(value, (list, tuple)):
                    fields[key] = json.dumps(list(value))
                elif isinstance(value, dict):
                    fields[key] = json.dumps(value)
                else:
                    fields[key] = value
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        connection.execute(f"UPDATE integration_handoffs SET {set_clause} WHERE id = ?",
                          (*fields.values(), handoff_id))
        row = connection.execute("SELECT * FROM integration_handoffs WHERE id = ?", (handoff_id,)).fetchone()
        project = row["project"]
        self._record_event_locked(connection, project=project, handoff_id=handoff_id, batch_id=None,
                                  event_type=event_type, reason=reason, from_status=from_status, to_status=to_status)
        return Handoff.from_row(row)

    def retry_handoff(self, handoff_id: str) -> Handoff:
        """REWORK_REQUIRED|BLOCKED -> READY_FOR_INTEGRATION, explicit
        operator action only -- mirrors queue_store.py's retry_task."""
        return self.transition_handoff(handoff_id, READY_FOR_INTEGRATION, event_type="RETRIED",
                                       extra_fields={"claimed_by": None, "claim_token": None,
                                                    "lease_expires_at": None})

    def reconcile_stale_handoff_claims(self, project: str | None = None, *, now: str | None = None) -> list[str]:
        """Restart-safe reconciliation (item 6's own idempotency/lease
        requirement, item: 'restart controller giữa chừng để verify
        không double-dispatch/double-merge') -- identical philosophy to
        queue_store.py's reconcile_stale_claims: CLAIMED/MERGING/
        TARGETED_TEST past their own lease deadline reconcile back to
        READY_FOR_INTEGRATION. A future re-claim re-derives its own
        merge/test idempotency from the handoff's own immutable
        commit_sha (see integration_engine.py) rather than blindly
        re-merging."""
        now = now or iso_now()
        with self._connection() as connection:
            clause = "project = ? AND " if project else ""
            params: tuple[Any, ...] = (project,) if project else ()
            rows = connection.execute(
                f"SELECT id, status FROM integration_handoffs WHERE {clause}status IN (?, ?, ?) "
                f"AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (*params, CLAIMED, MERGING, TARGETED_TEST, now),
            ).fetchall()
            reconciled = []
            for row in rows:
                self._transition_handoff_locked(connection, row["id"], row["status"], READY_FOR_INTEGRATION,
                                                event_type="RECOVERED_AFTER_RESTART",
                                                reason="stale lease reconciled after restart",
                                                extra_fields={"claimed_by": None, "claim_token": None,
                                                             "lease_expires_at": None})
                reconciled.append(row["id"])
        return reconciled

    # -- regression batches --------------------------------------------------

    def create_batch(self, project: str, handoff_ids: list[str]) -> RegressionBatch:
        batch_id = new_id()
        now = iso_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO integration_batches (id, project, status, handoff_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?)", (batch_id, project, REGRESSION_PENDING, json.dumps(handoff_ids), now),
            )
            for handoff_id in handoff_ids:
                connection.execute("UPDATE integration_handoffs SET regression_batch_id = ? WHERE id = ?",
                                  (batch_id, handoff_id))
            self._record_event_locked(connection, project=project, handoff_id=None, batch_id=batch_id,
                                      event_type="BATCH_CREATED", reason=None,
                                      metadata={"handoff_ids": handoff_ids})
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> RegressionBatch | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM integration_batches WHERE id = ?", (batch_id,)).fetchone()
        return RegressionBatch.from_row(row) if row else None

    def transition_batch(self, batch_id: str, to_status: str, *, event_type: str, reason: str | None = None,
                         extra_fields: dict[str, Any] | None = None) -> RegressionBatch:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM integration_batches WHERE id = ?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such batch: {batch_id}")
            if not is_valid_batch_transition(row["status"], to_status):
                raise InvalidHandoffTransitionError(f"{batch_id}: {row['status']} -> {to_status} is not valid")
            now = iso_now()
            fields: dict[str, Any] = {"status": to_status}
            if to_status == REGRESSION_RUNNING:
                fields["started_at"] = now
            if to_status in (MERGE_READY, REGRESSION_FAILED):
                fields["finished_at"] = now
            if extra_fields:
                for key, value in extra_fields.items():
                    fields[key] = json.dumps(value) if isinstance(value, dict) else value
            set_clause = ", ".join(f"{key} = ?" for key in fields)
            connection.execute(f"UPDATE integration_batches SET {set_clause} WHERE id = ?",
                              (*fields.values(), batch_id))
            updated_row = connection.execute("SELECT * FROM integration_batches WHERE id = ?", (batch_id,)).fetchone()
            self._record_event_locked(connection, project=row["project"], handoff_id=None, batch_id=batch_id,
                                      event_type=event_type, reason=reason, from_status=row["status"],
                                      to_status=to_status)
        return RegressionBatch.from_row(updated_row)

    def promote_batch(self, batch_id: str, *, main_commit_sha: str) -> RegressionBatch:
        """Records that a MERGE_READY batch was actually promoted to
        main (the git operation itself is integration_engine.py's job;
        this just durably records the outcome). Refuses any batch not
        currently MERGE_READY."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM integration_batches WHERE id = ?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(f"no such batch: {batch_id}")
            if row["status"] != MERGE_READY:
                raise InvalidHandoffTransitionError(f"{batch_id}: cannot promote a batch in status {row['status']!r}")
            now = iso_now()
            connection.execute(
                "UPDATE integration_batches SET promoted_to_main = 1, promoted_at = ? WHERE id = ?",
                (now, batch_id),
            )
            self._record_event_locked(connection, project=row["project"], handoff_id=None, batch_id=batch_id,
                                      event_type="PROMOTED_TO_MAIN", reason=None,
                                      metadata={"main_commit_sha": main_commit_sha})
            updated_row = connection.execute("SELECT * FROM integration_batches WHERE id = ?", (batch_id,)).fetchone()
        return RegressionBatch.from_row(updated_row)

    def pending_batch_handoffs(self, project: str) -> list[Handoff]:
        """INTEGRATED handoffs not yet assigned to any regression batch --
        the pool a new batch is created from once the project's own
        batch_size/batch_max_wait_seconds policy says it's time."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM integration_handoffs WHERE project = ? AND status = ? "
                "AND regression_batch_id IS NULL ORDER BY integrated_at ASC",
                (project, INTEGRATED),
            ).fetchall()
        return [Handoff.from_row(row) for row in rows]

    def get_open_batch(self, project: str) -> RegressionBatch | None:
        """The most recent REGRESSION_PENDING/REGRESSION_RUNNING batch
        for this project, if any -- integration_engine.py's own
        _tick_batch uses this instead of reaching into this store's
        private connection directly."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM integration_batches WHERE project = ? AND status IN (?, ?) "
                "ORDER BY created_at DESC LIMIT 1", (project, REGRESSION_PENDING, REGRESSION_RUNNING),
            ).fetchone()
        return self.get_batch(row["id"]) if row is not None else None

    def list_batches(self, project: str, limit: int = 50) -> list[RegressionBatch]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM integration_batches WHERE project = ? ORDER BY created_at DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        return [RegressionBatch.from_row(row) for row in rows]

    # -- events --------------------------------------------------------

    def _record_event_locked(self, connection: sqlite3.Connection, *, project: str, handoff_id: str | None,
                             batch_id: str | None, event_type: str, reason: str | None,
                             from_status: str | None = None, to_status: str | None = None,
                             metadata: dict[str, Any] | None = None) -> None:
        connection.execute(
            "INSERT INTO integration_events (timestamp, project, handoff_id, batch_id, event_type, from_status, "
            "to_status, reason, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (iso_now(), project, handoff_id, batch_id, event_type, from_status, to_status, reason,
             json.dumps(metadata) if metadata else None),
        )

    def list_events(self, project: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM integration_events WHERE project = ? ORDER BY id DESC LIMIT ?", (project, limit),
            ).fetchall()
        return [dict(row) for row in rows]


def publish_handoff_for_completed_task(task: Any, store: IntegrationStore) -> Handoff | None:
    """The glue between a coding session's own QueueTask (queue_store.py)
    reaching COMPLETED and this feature's own Handoff publication --
    item 4's own "publish immutable handoff {project, task_id, branch,
    commit_sha, base_sha, changed_paths, test_summary, artifacts}".

    Deliberately OPT-IN per task, never automatic for every completed
    task: only fires when the task's own metadata explicitly declares
    `integration_required` -- a dict with `project` (required) and the
    rest of the Handoff's own provenance fields (branch/commit_sha/
    base_sha/changed_paths -- all required; test_summary/artifacts
    optional). Returns None (does nothing) for a task that never opted
    in -- this is the ONLY place a Handoff is ever auto-published, kept
    as a small, pure function (no engine/service state) so
    queue_engine.py/queue_service.py can each wire it in as their own
    plain `on_completed` callback without importing this store's own
    class directly at their own top level (see queue_engine.py's own
    on_completed docstring for why that decoupling matters)."""
    spec = (task.metadata or {}).get("integration_required")
    if not isinstance(spec, dict):
        return None
    required = ("project", "branch", "commit_sha", "base_sha")
    if not all(spec.get(field) for field in required):
        return None
    return store.publish_handoff(
        project=spec["project"], task_id=task.id, origin_session=task.session, branch=spec["branch"],
        commit_sha=spec["commit_sha"], base_sha=spec["base_sha"], changed_paths=spec.get("changed_paths") or [],
        test_summary=spec.get("test_summary") or task.verification_evidence or {},
        artifacts=spec.get("artifacts") or {},
    )
