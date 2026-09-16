"""Lifecycle Close-Loop V1 -- durable idempotency + cleanup provenance.

The lifecycle this store serves is the one nothing owned end to end
before it:

    VERIFIED_PASS -> handoff -> merge/promote main -> release row
                  -> VERIFIED_PROD -> worktree/branch CLEANED

Every edge above is a side effect that must happen EXACTLY once and must
survive a crash between "the effect happened" and "we recorded that it
happened". The established pattern for that in this project is audit.py's
own claim/settle pair (`claim_idempotency_key` + `store_idempotent_result`),
and this module reuses that pattern verbatim in shape -- first claim wins,
an abandoned claim is reclaimable after a timeout, a settled key replays
its stored result instead of repeating the effect.

It does NOT reuse audit.py's own TABLE. `idempotent_sends` is scoped to
terminal_send_text/_keys attempts and is pruned on that feature's own
retention cadence (`maintenance.idempotency_key_retention_days`); a
lifecycle key must outlive a send key by a different order of magnitude
(a worktree may sit VERIFIED_PROD for weeks before a reaper pass reaches
it), and mixing the two would make either retention setting wrong for the
other.

It also deliberately gets its OWN database file with its OWN Migration
list starting at 1, rather than appending to queue_store.QUEUE_MIGRATIONS:
that list is a single global ordering several lanes edit in parallel, so
appending to it both conflicts textually and races on the version NUMBER
(two lanes each adding "v10"). A satellite store keeps the property that
deleting lifecycle.db changes no other subsystem's behaviour -- the
reconcile pass simply re-derives what it finds from git and the release
store on its next run.

SQLite has no cross-database foreign keys, so task_id/release_id here are
plain TEXT and joins happen in the read layer -- the same convention
outcomes.py already uses for work_id.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

DEFAULT_STALE_CLAIM_SECONDS = 900.0
"""A lifecycle side effect is a bounded git/SQLite operation measured in
seconds, but the process running it can be killed at any moment. 900s is
deliberately far above the slowest real operation (a full `git merge` +
targeted test) and far below "a human will notice": a claim still unsettled
after fifteen minutes belonged to a process that is not coming back, and
the next reconcile pass should be allowed to redo that edge. Compare
audit.py's own 30s default, which covers a single keystroke send."""

CLAIM_KIND_HANDOFF = "handoff"
CLAIM_KIND_RELEASE = "release"
CLAIM_KIND_CLEANUP = "cleanup"

CLEANUP_CLEANED = "CLEANED"
CLEANUP_BLOCKED = "BLOCKED"


def handoff_request_key(task_id: str, commit_sha: str) -> str:
    """Natural key for "this task, at this commit, produced its handoff".

    Keyed on the COMMIT, not the task alone and not the verify attempt.
    The commit is what the merge queue actually consumes, so it is the
    only key under which "already published" and "genuinely new work"
    mean the right thing in both directions: a duplicate PASS delivery or
    a restart mid-publish replays onto the same commit and collapses,
    while a real rework cycle produces a NEW commit and correctly earns
    its own handoff. Keying on the attempt instead would have disagreed
    with the existence check (which can only look the task up), and
    keying on the task alone would have locked a reworked task out of
    integration permanently."""
    return f"lifecycle:handoff:{task_id}:{commit_sha}"


def release_request_key(task_id: str, main_commit_sha: str) -> str:
    """Natural key for "this task reached main at this exact commit".
    Keyed on the resolved main SHA rather than the batch id so a crash
    between the git promotion and the release INSERT replays onto the
    same key when reconcile re-derives the SHA from git itself."""
    return f"lifecycle:promote:{task_id}:{main_commit_sha}"


def cleanup_request_key(task_id: str, release_id: str) -> str:
    return f"lifecycle:cleanup:{task_id}:{release_id}"


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    """Every statement is IF NOT EXISTS on purpose. Python's sqlite3 does
    not open a transaction for DDL, so a crash part-way through a
    migration leaves the tables it already created on disk while
    PRAGMA user_version still reads the OLD value -- the next startup
    re-runs this whole function. A bare CREATE TABLE would then fail with
    "table already exists" and the database file would be permanently
    unopenable. (queue_store's own v6/v7/v8 follow this rule for the same
    reason; its v1 predates the discovery.)"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_requests (
            request_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            result_json TEXT,
            created_at TEXT NOT NULL,
            settled_at TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_lifecycle_requests_kind "
        "ON lifecycle_requests(kind, created_at)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_cleanups (
            task_id TEXT PRIMARY KEY,
            release_id TEXT,
            project TEXT,
            repo_path TEXT,
            worktree_path TEXT,
            branch TEXT,
            outcome TEXT NOT NULL,
            reason TEXT,
            detail TEXT,
            recorded_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_lifecycle_cleanups_outcome "
        "ON lifecycle_cleanups(outcome, recorded_at)"
    )


LIFECYCLE_MIGRATIONS = [
    Migration(1, "lifecycle close-loop: request idempotency + cleanup provenance", _create_v1_schema),
]


def default_lifecycle_store_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_LIFECYCLE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "lifecycle.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LifecycleStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_lifecycle_store_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, LIFECYCLE_MIGRATIONS)
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

    # -- claim / settle ---------------------------------------------------

    def claim(self, request_key: str, *, kind: str,
              stale_after_seconds: float = DEFAULT_STALE_CLAIM_SECONDS) -> bool:
        """True if THIS caller is the one that gets to perform the effect.
        False if a prior settled call already did it, or a concurrent
        in-flight claim younger than `stale_after_seconds` still owns it.

        Same two-step as audit.claim_idempotency_key: an INSERT OR IGNORE
        race decides the first owner, then an UPDATE gated on BOTH
        (result_json IS NULL AND created_at < cutoff) reclaims only a
        genuinely abandoned claim -- a live concurrent one is never
        disturbed, and a settled one is never redone."""
        now = _now_iso()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO lifecycle_requests (request_key, kind, result_json, created_at) "
                "VALUES (?, ?, NULL, ?)",
                (request_key, kind, now),
            )
            if cursor.rowcount == 1:
                return True
            cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)).isoformat()
            reclaim = connection.execute(
                "UPDATE lifecycle_requests SET created_at = ? "
                "WHERE request_key = ? AND result_json IS NULL AND created_at < ?",
                (now, request_key, cutoff),
            )
        return reclaim.rowcount == 1

    def settle(self, request_key: str, result: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE lifecycle_requests SET result_json = ?, settled_at = ? WHERE request_key = ?",
                (json.dumps(result), _now_iso(), request_key),
            )

    def result_for(self, request_key: str) -> dict[str, Any] | None:
        """None means either never claimed, or claimed but still in
        flight -- callers tell those apart by claim()'s own return value,
        never by this method alone."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT result_json FROM lifecycle_requests WHERE request_key = ?", (request_key,),
            ).fetchone()
        if row is None or row["result_json"] is None:
            return None
        return json.loads(row["result_json"])

    def release_claim(self, request_key: str) -> None:
        """Hand an UNSETTLED claim straight back, for the case where the
        caller decided not to perform the effect after all (a guard
        declined, an input turned out to be missing). Deliberately refuses
        to touch a SETTLED row: a completed effect must never become
        re-runnable just because some later caller mishandled its key."""
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM lifecycle_requests WHERE request_key = ? AND result_json IS NULL",
                (request_key,),
            )

    # -- cleanup provenance ------------------------------------------------

    def record_cleanup(self, *, task_id: str, release_id: str | None, project: str | None,
                       repo_path: str | None, worktree_path: str | None, branch: str | None,
                       outcome: str, reason: str | None = None,
                       detail: dict[str, Any] | None = None) -> None:
        """One row per task, last write wins. A BLOCKED row is a real,
        queryable answer to "why is this worktree still here" -- it is
        overwritten by a later CLEANED once the blocker clears, so the
        table always reflects current truth rather than an append-only
        log nobody reads."""
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO lifecycle_cleanups (task_id, release_id, project, repo_path, worktree_path, "
                "branch, outcome, reason, detail, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET release_id=excluded.release_id, project=excluded.project, "
                "repo_path=excluded.repo_path, worktree_path=excluded.worktree_path, branch=excluded.branch, "
                "outcome=excluded.outcome, reason=excluded.reason, detail=excluded.detail, "
                "recorded_at=excluded.recorded_at",
                (task_id, release_id, project, repo_path, worktree_path, branch, outcome, reason,
                 json.dumps(detail or {}), _now_iso()),
            )

    def get_cleanup(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM lifecycle_cleanups WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["detail"] = json.loads(data.get("detail") or "{}")
        return data

    def list_cleanups(self, *, outcome: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM lifecycle_cleanups"
        params: list[Any] = []
        if outcome:
            query += " WHERE outcome = ?"
            params.append(outcome)
        query += " ORDER BY recorded_at DESC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        with self._connection() as connection:
            rows = connection.execute(query, params).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["detail"] = json.loads(data.get("detail") or "{}")
            result.append(data)
        return result

    def prune_settled(self, older_than_days: int) -> int:
        """Housekeeping only. An unsettled claim is NEVER pruned here --
        that is what claim()'s own staleness reclaim is for, and deleting
        an in-flight row would hand a second caller the same effect."""
        if older_than_days <= 0:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM lifecycle_requests WHERE result_json IS NOT NULL AND settled_at < ?",
                (cutoff,),
            )
        return cursor.rowcount
