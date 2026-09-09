"""P0 Part B: a durable, cross-process pane lease -- the in-memory
PaneLockRegistry (core.py) only ever serializes sends *within one Python
process*; the HTTP server, a separate STDIO server process, and any other
process opening its own TerminalService each get their own independent
PaneLockRegistry, so two of them sending to the same tmux pane at the same
moment could still interleave their text/Enter keystrokes with nothing
stopping them. A SQLite row (same durable-local-primitive pattern as
audit.py/bindings.py/grants.py/supervisor.py/supervisor2.py in this
codebase, not a new dependency) closes that gap: whichever process holds
the current, unexpired lease row for a given pane is the only one allowed
to send to it, and every process (regardless of which one) sees the same
row through the same on-disk database file.

Design:
  - One row per pane_key (the caller's choice of identity string -- see
    core.py's TerminalService for exactly how it derives one from a
    resolved SessionIdentity or, failing that, a session name).
  - owner_id identifies *one send attempt*, not one process -- core.py
    uses each attempt's own correlation_id, so two concurrent attempts
    (whether from the same process's two threads or two entirely
    different processes) never collide on ownership, and a crashed
    attempt's lease expires and is reclaimed exactly like any other.
  - expires_at is a fixed TTL from acquire time (no background renewal
    thread): a real send+verify(+bounded recovery) attempt is itself
    bounded in wall-clock time (see core.py's own verification timeouts),
    so a lease sized comfortably above that worst case naturally covers
    one real attempt without needing renewal machinery, while still
    recovering promptly (this module's LEASE crash-recovery contract)
    if the holding process is killed mid-send.

P0.6 GENERALISATION -- ResourceLockStore
----------------------------------------
The hard part above is not the pane: it is the ATOMIC check-and-set in
acquire(), the one that had to be folded into a single statement's WHERE
clause because a SELECT-then-write lost a real, reproduced race. That
algorithm is not pane-specific at all, and the coordination roadmap needs
exactly it for a different subject: "no two agents edit the same file /
module / branch at once".

So the algorithm now lives in _LeaseTable, parameterised by table and key
column, and BOTH stores are thin specialisations of it. PaneLeaseStore's
table, columns, method names, signatures, return types and TTL are
unchanged -- it is on core.py's send hot path, the most safety-critical
path in this codebase, and P0.6 deliberately changes nothing about it.
ResourceLockStore adds what a resource lock needs and a pane lease does
not: project scoping (a lock on src/app.py in project A must not block
project B), a human-readable reason, a much longer default TTL with
renewal, all-or-nothing multi-acquire, and an audited operator override.

Deliberately NOT added: a waiter queue. A caller that cannot get a lock
is told WHO holds it and until when, and decides for itself whether to
wait, work on something else, or escalate -- blocking inside a lock
primitive is how a fleet deadlocks.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

# Comfortably above the worst real observed send+verify+recovery cycle
# (RECOVERY_VERIFY_TIMEOUT_SECONDS * 2 plus settle delays, core.py) with
# headroom for host scheduling jitter -- long enough that a genuinely
# in-flight attempt is never falsely reclaimed, short enough that a
# crashed holder's pane is not stuck unusable for long.
DEFAULT_LEASE_TTL_SECONDS = 20.0


def default_lease_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_LEASE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "leases.db"


DEFAULT_RESOURCE_LOCK_TTL_SECONDS = 300.0
"""Resource locks protect work measured in MINUTES (an agent editing a
module, holding a branch through a rebase), not the sub-20s bounded
send+verify a pane lease covers. 300s matches queue_store's own task
lease so a worker renewing its task and its locks does so on one cadence
-- and, exactly like that lease, a holder is expected to RENEW: a lock
that outlives its holder's crash by more than one TTL would strand the
resource for everyone."""

RESOURCE_KEY_SEPARATOR = "\x1f"
"""ASCII unit separator: the composite primary key is
project_id + SEP + resource_key, so the proven single-column atomic
acquire carries over verbatim while the parts stay separately queryable
in their own columns. Control characters are rejected in both inputs, so
the separator can never appear inside a part and collide two distinct
resources onto one key."""


def _create_resource_locks(connection: sqlite3.Connection) -> None:
    """P0.6 named-resource ownership lock. A SEPARATE table from
    pane_leases on purpose, in the SAME database file.

    Separate table: the two have genuinely different lifetimes (20s vs
    minutes) and different subjects, and pane_leases is on core.py's send
    hot path -- mixing long-lived agent locks into the table that every
    single send contends on would be a real risk for no gain.

    Same file: leases.db is already one of the stores /health/ready checks
    and the backup procedure covers, so a lock table here inherits both
    rather than adding a sixth thing to remember."""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS resource_locks (
            lock_key TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            renewed_at TEXT NOT NULL,
            project_id TEXT NOT NULL,
            resource_key TEXT NOT NULL,
            reason TEXT
        )
    """)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_project ON resource_locks(project_id)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_locks_owner ON resource_locks(owner_id)")


LEASE_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: pane_leases as of the P0 Part B cross-process lease", lambda connection: None),
    Migration(2, "P0.6: resource_locks -- named-resource ownership lock sharing pane_leases' "
                 "atomic acquire algorithm and database file, but never its table",
              _create_resource_locks),
]


class _LeaseTable:
    """The durable check-and-set lease algorithm, parameterised by table.

    Everything here was PaneLeaseStore's, unchanged; it moved so a second
    subject (resource locks) reuses the atomic acquire rather than growing
    a second, subtly-different copy of the one statement in this codebase
    whose exact shape was arrived at by reproducing a real race.

    `_table`/`_key_column`/`_extra_columns` are module-level constants set
    by subclasses, never caller input -- they are interpolated into SQL,
    which is only safe on exactly that condition."""

    _table: str = ""
    _key_column: str = ""
    _extra_columns: tuple[str, ...] = ()

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_lease_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS pane_leases (
                    pane_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    renewed_at TEXT NOT NULL
                )
            """)
            apply_migrations(connection, LEASE_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextlib.contextmanager
    def _connection(self):
        # Same fd-leak-avoiding pattern as every other store in this
        # codebase (see audit.py's _connection docstring for the full
        # rationale) -- commit/rollback the transaction, then always
        # close the OS handle too.
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _acquire_sql(self) -> str:
        columns = (self._key_column, "owner_id", "acquired_at", "expires_at",
                   "renewed_at", *self._extra_columns)
        assignments = ", ".join(f"{name} = excluded.{name}"
                                for name in columns if name != self._key_column)
        return (
            f"INSERT INTO {self._table} ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))}) "
            f"ON CONFLICT({self._key_column}) DO UPDATE SET {assignments} "
            f"WHERE {self._table}.owner_id = excluded.owner_id "
            f"OR {self._table}.expires_at < excluded.acquired_at"
        )

    def _acquire_locked(self, connection: sqlite3.Connection, key: str, owner_id: str, *,
                        ttl_seconds: float, extras: tuple[Any, ...] = ()) -> bool:
        """One atomic INSERT ... ON CONFLICT DO UPDATE ... WHERE -- NOT a
        SELECT then a write. This connection's default (deferred) isolation
        takes no lock for a bare SELECT, so two connections could both see
        "no conflicting holder" and both then win the write: a real,
        reproduced race, not a theoretical one. Folding the whole decision
        into the statement's WHERE makes SQLite's own write lock cover the
        entire check-and-set."""
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        cursor = connection.execute(
            self._acquire_sql(),
            (key, owner_id, now_iso, expires_iso, now_iso, *extras),
        )
        return cursor.rowcount == 1

    def _renew_locked(self, connection: sqlite3.Connection, key: str, owner_id: str, *,
                      ttl_seconds: float) -> bool:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        cursor = connection.execute(
            f"UPDATE {self._table} SET expires_at = ?, renewed_at = ? "
            f"WHERE {self._key_column} = ? AND owner_id = ? AND expires_at >= ?",
            (expires_iso, now_iso, key, owner_id, now_iso),
        )
        return cursor.rowcount == 1

    def _release_locked(self, connection: sqlite3.Connection, key: str, owner_id: str) -> bool:
        cursor = connection.execute(
            f"DELETE FROM {self._table} WHERE {self._key_column} = ? AND owner_id = ?", (key, owner_id))
        return cursor.rowcount == 1

    def _holder_row(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM {self._table} WHERE {self._key_column} = ?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def prune_expired(self, *, grace_seconds: float = 300.0) -> int:
        """Housekeeping only (see maintenance.py) -- acquire()'s own
        expiry check already makes an expired row harmless to a new
        acquirer without this ever running; this just keeps the table
        from accumulating rows for keys no one has touched again since.
        grace_seconds keeps a just-expired row around briefly for
        diagnostics (holder()) rather than deleting it the instant it
        expires."""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=grace_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(f"DELETE FROM {self._table} WHERE expires_at < ?", (cutoff,))
        return cursor.rowcount


class PaneLeaseStore(_LeaseTable):
    """UNCHANGED by P0.6 in table, columns, method names, signatures,
    return types and TTL. It is on core.py's send hot path; the only thing
    that moved is where the shared algorithm lives."""

    _table = "pane_leases"
    _key_column = "pane_key"

    def acquire(self, pane_key: str, owner_id: str, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> bool:
        """True if `owner_id` now holds the lease for `pane_key` -- either
        because no one held it, the current holder's lease has expired
        (crash recovery: reclaimed exactly as if it had been released), or
        `owner_id` already held it (idempotent re-acquire, e.g. a retry of
        the exact same attempt). False if a *different*, still-unexpired
        owner currently holds it -- the caller must not send in that case."""
        with self._connection() as connection:
            return self._acquire_locked(connection, pane_key, owner_id, ttl_seconds=ttl_seconds)

    def renew(self, pane_key: str, owner_id: str, *, ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS) -> bool:
        """Extend an already-held lease -- only succeeds if `owner_id` is
        still the current, unexpired holder. Not used by the base
        send+verify path today (its bounded worst-case duration fits
        inside DEFAULT_LEASE_TTL_SECONDS with headroom -- see the module
        docstring), available for a future caller with a longer-running
        hold (e.g. a multi-step interactive recovery) to extend rather
        than risk expiry mid-operation."""
        with self._connection() as connection:
            return self._renew_locked(connection, pane_key, owner_id, ttl_seconds=ttl_seconds)

    def release(self, pane_key: str, owner_id: str) -> bool:
        """True if `owner_id`'s own (possibly already-expired, but not yet
        reclaimed by anyone else) lease row was removed. False if some
        other owner_id now holds the row -- meaning it already expired and
        was reclaimed before this release ran; never deletes someone
        else's active lease."""
        with self._connection() as connection:
            return self._release_locked(connection, pane_key, owner_id)

    def holder(self, pane_key: str) -> dict[str, Any] | None:
        """Diagnostic/test read: the current row for `pane_key`, if any,
        regardless of whether it has expired (callers that care about
        expiry compare `expires_at` themselves) -- not consulted by the
        acquire/release hot path above, which does its own atomic check."""
        return self._holder_row(pane_key)


class ResourceLockConflict(ValueError):
    """acquire_many could not take every requested lock. Carries the
    conflicting resource and its holder so the caller can report something
    actionable instead of "busy"."""

    def __init__(self, resource_key: str, holder: dict[str, Any] | None) -> None:
        super().__init__(f"{resource_key} is held by {(holder or {}).get('owner_id', 'another owner')}")
        self.resource_key = resource_key
        self.holder = holder


def _validate_part(name: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ValueError(f"{name} must be a non-empty string")
    if any(character < " " or character == "\x7f" for character in value):
        raise ValueError(f"{name} must not contain control characters")
    if len(value) > 512:
        raise ValueError(f"{name} exceeds 512 characters")
    return value


def resource_lock_key(project_id: str, resource_key: str) -> str:
    """The composite primary key. Both parts are validated to be free of
    control characters, so the separator cannot appear inside either and
    two distinct resources can never collide onto one key."""
    return _validate_part("project_id", project_id) + RESOURCE_KEY_SEPARATOR + \
        _validate_part("resource_key", resource_key)


class ResourceLockStore(_LeaseTable):
    """P0.6: "no two agents touch the same file/module/branch at once."

    Same TTL + owner semantics as the pane lease, and the SAME atomic
    acquire (inherited, not copied). What it adds is what a shared-resource
    lock needs and a pane does not:

      - PROJECT SCOPING. `src/app.py` in one project is a different
        resource from `src/app.py` in another; a global key space would
        make two unrelated projects block each other.
      - A REASON. A human (or ChatGPT) looking at a held branch needs to
        know why, not just that.
      - A LONGER TTL WITH RENEWAL, because the work is minutes not seconds.
      - ALL-OR-NOTHING MULTI-ACQUIRE, which is the deadlock story: two
        agents each needing {a, b} and taking them one at a time can end up
        holding one apiece forever. acquire_many takes them in ONE
        transaction or takes none.
      - AN AUDITED OPERATOR OVERRIDE, because a long-lived lock whose owner
        is gone but whose TTL has not yet lapsed still needs a way out.

    Returns the CURRENT HOLDER on contention rather than a bare False: a
    coordination primitive that only says "no" leaves the caller with
    nothing to act on."""

    _table = "resource_locks"
    _key_column = "lock_key"
    _extra_columns = ("project_id", "resource_key", "reason")

    def acquire(self, project_id: str, resource_key: str, owner_id: str, *,
                ttl_seconds: float = DEFAULT_RESOURCE_LOCK_TTL_SECONDS,
                reason: str | None = None) -> dict[str, Any]:
        """Take one lock. Idempotent for the same owner (re-acquiring
        refreshes the TTL, exactly like the pane lease), and reclaims a
        lock whose holder's TTL has lapsed.

        Returns {"acquired": bool, "lock"/"holder": ...} -- on failure the
        holder is included so the caller can say WHO has it and until
        when."""
        project_id = _validate_part("project_id", project_id)
        resource_key = _validate_part("resource_key", resource_key)
        owner_id = _validate_part("owner_id", owner_id)
        key = resource_lock_key(project_id, resource_key)
        with self._connection() as connection:
            ok = self._acquire_locked(connection, key, owner_id, ttl_seconds=ttl_seconds,
                                      extras=(project_id, resource_key, reason))
        if ok:
            return {"acquired": True, "lock": self._public(self._holder_row(key))}
        return {"acquired": False, "holder": self._public(self._holder_row(key)),
                "project_id": project_id, "resource_key": resource_key}

    def acquire_many(self, project_id: str, resource_keys: list[str], owner_id: str, *,
                     ttl_seconds: float = DEFAULT_RESOURCE_LOCK_TTL_SECONDS,
                     reason: str | None = None) -> dict[str, Any]:
        """ALL of `resource_keys`, or NONE of them, in one transaction.

        This is the deadlock story, and the reason it is not just a loop
        over acquire(): two agents that each need {a, b} and take them one
        at a time can finish holding one apiece and wait on each other
        forever. Taking the whole set under a single write lock makes that
        impossible -- the loser gets nothing and can retry cleanly.

        Keys are sorted before acquisition, so even two callers using
        different set orders contend in the same order."""
        project_id = _validate_part("project_id", project_id)
        owner_id = _validate_part("owner_id", owner_id)
        wanted = sorted({_validate_part("resource_key", key) for key in (resource_keys or [])})
        if not wanted:
            raise ValueError("resource_keys must contain at least one key")
        connection = self._connect()
        try:
            # BEGIN IMMEDIATE: every key in the set must be decided under
            # one write lock, or a concurrent caller could slip between
            # two of our acquires and produce exactly the partial hold
            # this method exists to prevent.
            connection.execute("BEGIN IMMEDIATE")
            for resource_key in wanted:
                key = resource_lock_key(project_id, resource_key)
                if not self._acquire_locked(connection, key, owner_id,
                                            ttl_seconds=ttl_seconds,
                                            extras=(project_id, resource_key, reason)):
                    row = connection.execute(
                        "SELECT * FROM resource_locks WHERE lock_key = ?", (key,)).fetchone()
                    connection.rollback()
                    return {"acquired": False, "conflict": resource_key,
                            "holder": self._public(dict(row) if row else None),
                            "requested": wanted, "project_id": project_id}
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"acquired": True, "project_id": project_id, "resource_keys": wanted,
                "locks": [self._public(self._holder_row(resource_lock_key(project_id, k)))
                          for k in wanted]}

    def renew(self, project_id: str, resource_key: str, owner_id: str, *,
              ttl_seconds: float = DEFAULT_RESOURCE_LOCK_TTL_SECONDS) -> dict[str, Any]:
        """Extend a lock this owner still holds. A holder that stops
        renewing is treated as gone and its lock becomes reclaimable --
        the same contract as the queue task lease."""
        key = resource_lock_key(project_id, resource_key)
        with self._connection() as connection:
            ok = self._renew_locked(connection, key, owner_id, ttl_seconds=ttl_seconds)
        if not ok:
            return {"renewed": False, "holder": self._public(self._holder_row(key)),
                    "reason": "not the current holder, or the lock already expired"}
        return {"renewed": True, "lock": self._public(self._holder_row(key))}

    def release(self, project_id: str, resource_key: str, owner_id: str) -> bool:
        """Only ever removes THIS owner's row -- never someone else's
        active lock, exactly like the pane lease."""
        key = resource_lock_key(project_id, resource_key)
        with self._connection() as connection:
            return self._release_locked(connection, key, owner_id)

    def release_all(self, owner_id: str, *, project_id: str | None = None) -> int:
        """Every lock this owner holds, released at once -- what a worker
        calls when it finishes or aborts a task, so a completed agent never
        leaves a resource pinned until its TTL lapses."""
        owner_id = _validate_part("owner_id", owner_id)
        clause = " AND project_id = ?" if project_id else ""
        params: tuple[Any, ...] = (owner_id, project_id) if project_id else (owner_id,)
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM resource_locks WHERE owner_id = ?{clause}", params)
        return cursor.rowcount

    def holder(self, project_id: str, resource_key: str) -> dict[str, Any] | None:
        """Who holds this resource and until when, or None if the row is
        gone. An EXPIRED row is still returned (with expired: true) rather
        than hidden -- "nobody holds this" and "the last holder died and
        nobody has taken it since" are different answers to an operator."""
        return self._public(self._holder_row(resource_lock_key(project_id, resource_key)))

    def list_locks(self, *, project_id: str | None = None, owner_id: str | None = None,
                   include_expired: bool = False) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if owner_id:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if not include_expired:
            clauses.append("expires_at >= ?")
            params.append(datetime.now(timezone.utc).isoformat())
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM resource_locks {where}ORDER BY project_id, resource_key", params).fetchall()
        return [self._public(dict(row)) for row in rows]

    def force_release(self, project_id: str, resource_key: str, *, actor: str,
                      reason: str) -> dict[str, Any]:
        """Operator override: drop a lock REGARDLESS of owner.

        Deliberately a separate, differently-named verb rather than a flag
        on release(): breaking someone else's lock is not the same action
        as giving up your own, and it should be impossible to do by
        accident or by passing the wrong owner_id. Requires a reason and
        returns whose lock was broken, so the audit trail names both."""
        _validate_part("actor", actor)
        _validate_part("reason", reason)
        key = resource_lock_key(project_id, resource_key)
        previous = self._public(self._holder_row(key))
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM resource_locks WHERE lock_key = ?", (key,))
        return {"released": cursor.rowcount == 1, "previous_holder": previous,
                "actor": actor, "reason": reason,
                "project_id": project_id, "resource_key": resource_key}

    @staticmethod
    def _public(row: dict[str, Any] | None) -> dict[str, Any] | None:
        """The read shape. `lock_key` is an internal composite and is not
        exposed -- callers address a lock by (project_id, resource_key),
        and leaking the separator-joined form would invite someone to
        build it by hand and skip validation."""
        if row is None:
            return None
        expires_at = row.get("expires_at")
        return {
            "project_id": row.get("project_id"), "resource_key": row.get("resource_key"),
            "owner_id": row.get("owner_id"), "reason": row.get("reason"),
            "acquired_at": row.get("acquired_at"), "renewed_at": row.get("renewed_at"),
            "expires_at": expires_at,
            "expired": bool(expires_at and expires_at < datetime.now(timezone.utc).isoformat()),
        }
