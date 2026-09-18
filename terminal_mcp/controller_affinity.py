"""Persistent controller-to-backend affinity and cutover routing."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ACTIVE = "ACTIVE"
STANDBY = "STANDBY"
DRAINING = "DRAINING"
DOWN = "DOWN"
_STATES = {ACTIVE, STANDBY, DRAINING, DOWN}


class AffinityError(Exception):
    """A stable, machine-readable controller affinity failure."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def _default_db_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "controller_affinity.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid backend endpoint") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or port is None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("endpoint must be an HTTP loopback address with an explicit port")
    return endpoint


class ControllerAffinityStore:
    def __init__(self, db_path: str | os.PathLike[str] | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else _default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS backends (
                    backend_id TEXT PRIMARY KEY,
                    endpoint TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    healthy INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS affinities (
                    mcp_session_id TEXT PRIMARY KEY,
                    backend_id TEXT NOT NULL REFERENCES backends(backend_id),
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                );
                """
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _backend(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["healthy"] = bool(result["healthy"])
        return result

    def upsert_backend(
        self, backend_id: str, endpoint: str, generation: int,
        state: str = STANDBY, healthy: bool = True,
    ) -> dict:
        _validate_endpoint(endpoint)
        if state not in _STATES:
            raise ValueError("invalid backend state")
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO backends
                    (backend_id, endpoint, generation, state, healthy, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(backend_id) DO UPDATE SET
                    endpoint=excluded.endpoint,
                    generation=excluded.generation,
                    state=excluded.state,
                    healthy=excluded.healthy,
                    updated_at=excluded.updated_at
                """,
                (backend_id, endpoint, generation, state, int(healthy), now, now),
            )
        return self.get_backend(backend_id)

    def get_backend(self, backend_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backends WHERE backend_id = ?", (backend_id,)
            ).fetchone()
        return self._backend(row)

    def list_backends(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM backends ORDER BY generation, backend_id"
            ).fetchall()
        return [self._backend(row) for row in rows]

    def set_backend_state(
        self, backend_id: str, state: str, healthy: bool | None = None
    ) -> dict:
        if state not in _STATES:
            raise ValueError("invalid backend state")
        with self._connect() as connection:
            if healthy is None:
                cursor = connection.execute(
                    "UPDATE backends SET state = ?, updated_at = ? WHERE backend_id = ?",
                    (state, _now(), backend_id),
                )
            else:
                cursor = connection.execute(
                    """UPDATE backends SET state = ?, healthy = ?, updated_at = ?
                       WHERE backend_id = ?""",
                    (state, int(healthy), _now(), backend_id),
                )
            if cursor.rowcount != 1:
                raise AffinityError("BACKEND_NOT_FOUND")
        return self.get_backend(backend_id)

    def bind_session(self, session_id: str, backend_id: str) -> dict:
        now = _now()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT backend_id FROM affinities WHERE mcp_session_id = ?",
                (session_id,),
            ).fetchone()
            if existing:
                if existing["backend_id"] != backend_id:
                    raise AffinityError(
                        "SESSION_ALREADY_BOUND",
                        "session is already bound to a different backend",
                    )
                connection.execute(
                    "UPDATE affinities SET last_seen_at = ? WHERE mcp_session_id = ?",
                    (now, session_id),
                )
            else:
                try:
                    connection.execute(
                        """INSERT INTO affinities
                           (mcp_session_id, backend_id, created_at, last_seen_at)
                           VALUES (?, ?, ?, ?)""",
                        (session_id, backend_id, now, now),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AffinityError("BACKEND_NOT_FOUND") from exc
            row = connection.execute(
                "SELECT * FROM affinities WHERE mcp_session_id = ?", (session_id,)
            ).fetchone()
        return dict(row)

    def touch_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE affinities SET last_seen_at = ? WHERE mcp_session_id = ?",
                (_now(), session_id),
            )
        return cursor.rowcount == 1

    def release_session(self, session_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM affinities WHERE mcp_session_id = ?", (session_id,)
            )
        return cursor.rowcount == 1

    def sessions_for_backend(self, backend_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM affinities WHERE backend_id = ?
                   ORDER BY created_at, mcp_session_id""",
                (backend_id,),
            ).fetchall()
        return [dict(row) for row in rows]


class ControllerRouter:
    def __init__(self, store: ControllerAffinityStore) -> None:
        self.store = store

    @staticmethod
    def _one_active(connection: sqlite3.Connection) -> sqlite3.Row:
        rows = connection.execute(
            "SELECT * FROM backends WHERE state = ? AND healthy = 1",
            (ACTIVE,),
        ).fetchall()
        if not rows:
            raise AffinityError("NO_ACTIVE_BACKEND")
        if len(rows) > 1:
            raise AffinityError("AMBIGUOUS_ACTIVE_BACKEND")
        return rows[0]

    def select_backend(self, mcp_session_id: str | None = None) -> dict:
        with self.store.transaction() as connection:
            if mcp_session_id is not None:
                row = connection.execute(
                    """SELECT b.* FROM affinities a
                       JOIN backends b ON b.backend_id = a.backend_id
                       WHERE a.mcp_session_id = ?""",
                    (mcp_session_id,),
                ).fetchone()
                if row is not None:
                    if not row["healthy"] or row["state"] == DOWN:
                        raise AffinityError("SESSION_BACKEND_UNAVAILABLE")
                    connection.execute(
                        "UPDATE affinities SET last_seen_at = ? WHERE mcp_session_id = ?",
                        (_now(), mcp_session_id),
                    )
                    return self.store._backend(row)

            selected = self._one_active(connection)
            if mcp_session_id is not None:
                now = _now()
                connection.execute(
                    """INSERT INTO affinities
                       (mcp_session_id, backend_id, created_at, last_seen_at)
                       VALUES (?, ?, ?, ?)""",
                    (mcp_session_id, selected["backend_id"], now, now),
                )
            return self.store._backend(selected)

    def begin_cutover(self, old: str, new: str) -> None:
        with self.store.transaction() as connection:
            old_row = connection.execute(
                "SELECT * FROM backends WHERE backend_id = ?", (old,)
            ).fetchone()
            new_row = connection.execute(
                "SELECT * FROM backends WHERE backend_id = ?", (new,)
            ).fetchone()
            if old_row is None or new_row is None:
                raise AffinityError("BACKEND_NOT_FOUND")
            if not new_row["healthy"] or new_row["state"] == DOWN:
                raise AffinityError("NEW_BACKEND_UNAVAILABLE")
            now = _now()
            connection.execute(
                "UPDATE backends SET state = ?, updated_at = ? WHERE backend_id = ?",
                (ACTIVE, now, new),
            )
            connection.execute(
                "UPDATE backends SET state = ?, updated_at = ? WHERE backend_id = ?",
                (DRAINING, now, old),
            )

    def drain_status(self, old: str) -> dict:
        backend = self.store.get_backend(old)
        if backend is None:
            raise AffinityError("BACKEND_NOT_FOUND")
        sessions = self.store.sessions_for_backend(old)
        return {
            "state": backend["state"],
            "active_sessions": len(sessions),
            "sessions": sessions,
            "can_stop": backend["state"] == DRAINING and not sessions,
        }

    def complete_drain(self, old: str) -> dict:
        with self.store.transaction() as connection:
            backend = connection.execute(
                "SELECT * FROM backends WHERE backend_id = ?", (old,)
            ).fetchone()
            if backend is None:
                raise AffinityError("BACKEND_NOT_FOUND")
            count = connection.execute(
                "SELECT COUNT(*) FROM affinities WHERE backend_id = ?", (old,)
            ).fetchone()[0]
            if backend["state"] != DRAINING or count:
                raise AffinityError("DRAIN_NOT_COMPLETE")
            connection.execute(
                "UPDATE backends SET state = ?, updated_at = ? WHERE backend_id = ?",
                (STANDBY, _now(), old),
            )
            updated = connection.execute(
                "SELECT * FROM backends WHERE backend_id = ?", (old,)
            ).fetchone()
        return self.store._backend(updated)
