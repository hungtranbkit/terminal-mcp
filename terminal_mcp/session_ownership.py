"""Durable session ownership: which node owns which session name.

WHY THIS EXISTS

The controller used to know where a session lived from exactly two signals:
an in-memory location cache with a 20s TTL, and a live probe of every node's
session list. Both are lossy by design. Live on dell-linux (2026-10-05):
create_session answered SUBMIT_CONFIRMED, and a minute later inspect/send/
delete for the same name answered SESSION_LOCATION_UNKNOWN -- the cache had
expired and the owner's /v1/sessions listing was slow or failed (dell holds
20 sessions; the listing scales with pane count), so the probe was
"incomplete" and the ownership the controller had known a minute earlier was
simply gone. A controller restart lost it the same way, for every session.

This store is the positive evidence that does not expire:

  ACTIVE   the owning node created or listed it; routing goes there.
  MISSING  a COMPLETE listing from the owning node itself came back without
           it. Only that answer may set MISSING -- a timeout, an offline
           node, a missing client or a probe that never reached the owner
           proves nothing and changes nothing.
  KILLED   an explicit delete/kill through the controller. The tombstone
           names the removed instance (instance_id / created_epoch) so an
           inventory taken before the delete and applied after it cannot
           bring the row back.

A row is never deleted by observation; a same-name session seen alive again
as a genuinely NEW instance advances `generation`.

Same connection/schema/permission pattern as the other stores here.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

ACTIVE = "ACTIVE"
MISSING = "MISSING"
KILLED = "KILLED"


def default_session_ownership_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_SESSION_OWNERSHIP_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "session_ownership.db"


def epoch_from_iso(value: Any) -> int | None:
    """tmux creation time as listed by a node (`created`, ISO-8601)."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except ValueError:
        return None


@dataclass(frozen=True)
class Ownership:
    session: str
    node_id: str
    state: str
    generation: int
    instance_id: str | None
    created_epoch: int | None
    last_confirmed_at: float | None
    missing_at: float | None
    killed_at: float | None
    updated_at: float

    def public(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "state": self.state, "generation": self.generation,
                "instance_id": self.instance_id, "last_confirmed_at": self.last_confirmed_at,
                "missing_at": self.missing_at, "killed_at": self.killed_at}


def _row(row: sqlite3.Row) -> Ownership:
    return Ownership(session=row["session"], node_id=row["node_id"], state=row["state"],
                     generation=int(row["generation"]), instance_id=row["instance_id"],
                     created_epoch=row["created_epoch"], last_confirmed_at=row["last_confirmed_at"],
                     missing_at=row["missing_at"], killed_at=row["killed_at"], updated_at=row["updated_at"])


def _is_stale_sighting(row: Ownership, instance_id: str | None, created_epoch: int | None) -> bool:
    """A sighting of the very instance a tombstone removed."""
    if row.state != KILLED:
        return False
    if instance_id and row.instance_id:
        return instance_id == row.instance_id
    if created_epoch is not None and row.created_epoch is not None:
        return created_epoch <= row.created_epoch
    if created_epoch is not None and row.killed_at is not None:
        return created_epoch < int(row.killed_at)
    # No way to tell which instance was seen: an explicit delete wins.
    return True


def _is_new_instance(row: Ownership, instance_id: str | None, created_epoch: int | None) -> bool:
    if instance_id and row.instance_id:
        return instance_id != row.instance_id
    if created_epoch is not None and row.created_epoch is not None:
        return created_epoch != row.created_epoch
    return False


class SessionOwnershipStore:
    def __init__(self, path: str | Path | None = None, *, clock=time.time) -> None:
        self.path = Path(path) if path is not None else default_session_ownership_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._clock = clock
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS session_ownership (
                    session TEXT NOT NULL, node_id TEXT NOT NULL, state TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1, instance_id TEXT, created_epoch INTEGER,
                    first_seen_at REAL NOT NULL, last_confirmed_at REAL, missing_at REAL,
                    killed_at REAL, updated_at REAL NOT NULL,
                    PRIMARY KEY (session, node_id))""")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- reads ----------------------------------------------------------------

    def owners(self, session: str) -> list[Ownership]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM session_ownership WHERE session = ? ORDER BY node_id",
                                      (session,)).fetchall()
        return [_row(row) for row in rows]

    def get(self, session: str, node_id: str) -> Ownership | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM session_ownership WHERE session = ? AND node_id = ?",
                                     (session, node_id)).fetchone()
        return _row(row) if row else None

    def active_on(self, node_id: str) -> list[Ownership]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM session_ownership WHERE node_id = ? AND state = ?",
                                      (node_id, ACTIVE)).fetchall()
        return [_row(row) for row in rows]

    # -- writes ---------------------------------------------------------------

    def _present(self, connection: sqlite3.Connection, session: str, node_id: str, *,
                 instance_id: str | None, created_epoch: int | None, explicit: bool, now: float) -> None:
        found = connection.execute("SELECT * FROM session_ownership WHERE session = ? AND node_id = ?",
                                   (session, node_id)).fetchone()
        if found is None:
            connection.execute(
                "INSERT INTO session_ownership (session, node_id, state, generation, instance_id, "
                "created_epoch, first_seen_at, last_confirmed_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (session, node_id, ACTIVE, instance_id, created_epoch, now, now, now))
            return
        row = _row(found)
        if not explicit and _is_stale_sighting(row, instance_id, created_epoch):
            return  # the deleted instance, listed before its delete: never resurrect
        if not explicit and created_epoch is not None and row.created_epoch is not None \
                and created_epoch < row.created_epoch:
            return  # an OLDER instance than the one recorded: a stale inventory
        if row.state == KILLED or _is_new_instance(row, instance_id, created_epoch):
            # A new instance: its identity replaces the old one outright.
            connection.execute(
                "UPDATE session_ownership SET state = ?, generation = generation + 1, instance_id = ?, "
                "created_epoch = ?, last_confirmed_at = ?, missing_at = NULL, killed_at = NULL, "
                "updated_at = ? WHERE session = ? AND node_id = ?",
                (ACTIVE, instance_id, created_epoch, now, now, session, node_id))
            return
        connection.execute(
            "UPDATE session_ownership SET state = ?, instance_id = COALESCE(?, instance_id), "
            "created_epoch = COALESCE(?, created_epoch), last_confirmed_at = ?, missing_at = NULL, "
            "updated_at = ? WHERE session = ? AND node_id = ?",
            (ACTIVE, instance_id, created_epoch, now, now, session, node_id))

    def record_created(self, session: str, node_id: str, *, instance_id: str | None,
                       created_epoch: int | None) -> Ownership:
        """An explicit create on `node_id`: always ACTIVE, a new generation if
        the name was previously tombstoned or held by another instance."""
        now = self._clock()
        with self._connection() as connection:
            self._present(connection, session, node_id, instance_id=instance_id,
                          created_epoch=created_epoch, explicit=True, now=now)
        record = self.get(session, node_id)
        assert record is not None
        return record

    def observe_present(self, session: str, node_id: str, *, instance_id: str | None = None,
                        created_epoch: int | None = None) -> None:
        with self._connection() as connection:
            self._present(connection, session, node_id, instance_id=instance_id,
                          created_epoch=created_epoch, explicit=False, now=self._clock())

    def reconcile_node(self, node_id: str, listed: dict[str, int | None]) -> list[str]:
        """Apply ONE COMPLETE listing from `node_id` ({name: created_epoch}).

        Names present are confirmed ACTIVE (stale sightings of tombstoned
        instances excepted); ACTIVE rows on this node that are absent become
        MISSING. Only call this with a listing that actually came back from
        the node -- a failed or partial listing must never reach here.
        Returns the names newly marked MISSING."""
        now = self._clock()
        newly_missing: list[str] = []
        with self._connection() as connection:
            for name, created_epoch in listed.items():
                self._present(connection, name, node_id, instance_id=None, created_epoch=created_epoch,
                              explicit=False, now=now)
            for row in connection.execute("SELECT session FROM session_ownership WHERE node_id = ? AND state = ?",
                                          (node_id, ACTIVE)).fetchall():
                if row["session"] not in listed:
                    newly_missing.append(row["session"])
            for name in newly_missing:
                connection.execute("UPDATE session_ownership SET state = ?, missing_at = ?, updated_at = ? "
                                   "WHERE session = ? AND node_id = ? AND state = ?",
                                   (MISSING, now, now, name, node_id, ACTIVE))
        return newly_missing

    def tombstone(self, session: str, node_id: str, *, instance_id: str | None = None,
                  created_epoch: int | None = None) -> Ownership:
        """Explicit delete/kill. Keeps the row; records which instance died.
        A repeated delete keeps the first tombstone's instance."""
        now = self._clock()
        with self._connection() as connection:
            found = connection.execute("SELECT * FROM session_ownership WHERE session = ? AND node_id = ?",
                                       (session, node_id)).fetchone()
            if found is None:
                connection.execute(
                    "INSERT INTO session_ownership (session, node_id, state, generation, instance_id, "
                    "created_epoch, first_seen_at, killed_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
                    (session, node_id, KILLED, instance_id, created_epoch, now, now, now))
            elif _row(found).state != KILLED:
                connection.execute(
                    "UPDATE session_ownership SET state = ?, killed_at = ?, updated_at = ?, "
                    "instance_id = COALESCE(?, instance_id), created_epoch = COALESCE(?, created_epoch) "
                    "WHERE session = ? AND node_id = ?",
                    (KILLED, now, now, instance_id, created_epoch, session, node_id))
        record = self.get(session, node_id)
        assert record is not None
        return record

    def rename(self, old: str, new: str, node_id: str) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM session_ownership WHERE session = ? AND node_id = ?", (new, node_id))
            connection.execute("UPDATE session_ownership SET session = ?, updated_at = ? "
                               "WHERE session = ? AND node_id = ?", (new, self._clock(), old, node_id))
