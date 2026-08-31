"""Supervisor Loop v1 — local detection + a durable event queue.

Scope, deliberately narrow: this watches whitelisted tmux sessions/bindings,
classifies state changes using the project's existing, already-guarded
observation path (TerminalService.terminal_status / terminal_status_bound,
which already enforce the whitelist and never expose a denied session), and
persists meaningful transitions as events in SQLite. It never sends input,
never executes a shell command, and never bypasses terminal_input/
input_policy/binding/confirmation/audit — those remain exactly as they were.

v1 solves: automatic local detection of "this session needs attention" and
a queryable, durable event history, without requiring a human to poll by
hand. v2 (not built here): an external wake-up (e.g. a webhook relay) that
notices a queued attention_required event and invokes ChatGPT with an
approved, human-reviewed prompt — see the module-level EVENT_SCHEMA_VERSION
docstring below for the JSON contract v2 can build a forwarder against.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import sanitized_preview, text_fingerprint
from .config import AppConfig, SupervisorConfig
from .core import TerminalService
from .permissions import session_allowed
from .status import SUPERVISOR_STATES, classify_supervisor_state

EVENT_SCHEMA_VERSION = 1
"""JSON shape of one persisted event, stable for a future v2 webhook
forwarder to build against without needing to read this module's SQL:

{
  "schema_version": 1, "id": int, "timestamp": "2026-...Z",
  "watch_key": "session:claude-mesflow" | "binding:mesflow-dev",
  "kind": "session" | "binding", "target": str,
  "previous_state": str | null, "state": str,
  "event_type": "state_changed" | "attention_required" | "completed" |
                 "error_detected" | "stalled" | "watch_target_missing",
  "reason": str, "output_preview": str, "output_hash": str | null,
  "iteration_count": int, "acknowledged_at": str | null,
  "metadata": {...}
}
"""

EVENT_TYPES = (
    "state_changed", "attention_required", "completed",
    "error_detected", "stalled", "watch_target_missing",
)
_ATTENTION_EVENT_TYPES = {
    "WAITING_INPUT": "attention_required",
    "DONE": "completed",
    "ERROR": "error_detected",
}


def default_supervisor_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_SUPERVISOR_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "supervisor.db"


def watch_key(kind: str, target: str) -> str:
    return f"{kind}:{target}"


class SupervisorStore:
    """SQLite persistence for watches + events, same pattern as audit.py/
    bindings.py: 0700 state dir, 0600 db file, WAL mode, row_factory=Row."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_supervisor_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS watches (
                    watch_key TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'UNKNOWN',
                    state_since TEXT NOT NULL,
                    last_output_hash TEXT,
                    last_output_change_at TEXT,
                    last_activity TEXT,
                    iteration_count INTEGER NOT NULL DEFAULT 0,
                    same_failure_count INTEGER NOT NULL DEFAULT 0,
                    disabled_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    watch_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    previous_state TEXT,
                    state TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reason TEXT,
                    output_preview TEXT,
                    output_hash TEXT,
                    iteration_count INTEGER NOT NULL,
                    acknowledged_at TEXT,
                    metadata TEXT
                )
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    # -- watches ----------------------------------------------------------

    def get_watch(self, key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def list_watches(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM watches ORDER BY watch_key").fetchall()
        return [dict(row) for row in rows]

    def upsert_watch(self, kind: str, target: str, *, source: str, enabled: bool = True) -> tuple[dict[str, Any], bool]:
        """Create a watch, or re-enable/replace source on an existing one.
        Never resets state/iteration/failure bookkeeping on an existing row —
        only creation or an explicit re-enable touches those."""
        key = watch_key(kind, target)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO watches
                    (watch_key, kind, target, source, enabled, state, state_since,
                     last_output_hash, last_output_change_at, last_activity,
                     iteration_count, same_failure_count, disabled_reason, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, 'UNKNOWN', ?, NULL, NULL, NULL, 0, 0, NULL, ?, ?)""",
                    (key, kind, target, source, now, now, now),
                )
                created = True
            else:
                connection.execute(
                    "UPDATE watches SET enabled = 1, disabled_reason = NULL, updated_at = ? WHERE watch_key = ?",
                    (now, key),
                )
                created = False
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
        return dict(row), created

    def set_enabled(self, key: str, enabled: bool, *, disabled_reason: str | None = None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE watches SET enabled = ?, disabled_reason = ?, updated_at = ? WHERE watch_key = ?",
                (int(enabled), disabled_reason, now, key),
            )
        return cursor.rowcount == 1

    def delete_watch(self, key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM watches WHERE watch_key = ?", (key,))
        return cursor.rowcount == 1

    def update_watch_progress(self, key: str, *, state: str, state_changed: bool,
                              output_hash: str | None, output_changed: bool,
                              iteration_count: int, same_failure_count: int,
                              now_iso: str, enabled: bool, disabled_reason: str | None) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT state_since FROM watches WHERE watch_key = ?", (key,)).fetchone()
            state_since = now_iso if state_changed or row is None else row["state_since"]
            connection.execute(
                """UPDATE watches SET state = ?, state_since = ?, last_output_hash = ?,
                   last_output_change_at = CASE WHEN ? THEN ? ELSE last_output_change_at END,
                   last_activity = ?, iteration_count = ?, same_failure_count = ?,
                   enabled = ?, disabled_reason = ?, updated_at = ?
                   WHERE watch_key = ?""",
                (state, state_since, output_hash, int(output_changed), now_iso,
                 now_iso, iteration_count, same_failure_count,
                 int(enabled), disabled_reason, now_iso, key),
            )

    # -- events -------------------------------------------------------------

    def add_event(self, *, watch_key: str, kind: str, target: str, previous_state: str | None,
                  state: str, event_type: str, reason: str, output_preview: str,
                  output_hash: str | None, iteration_count: int,
                  metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO supervisor_events
                (timestamp, watch_key, kind, target, previous_state, state, event_type,
                 reason, output_preview, output_hash, iteration_count, acknowledged_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (now, watch_key, kind, target, previous_state, state, event_type, reason,
                 output_preview, output_hash, iteration_count, json.dumps(metadata or {})),
            )
            row = connection.execute("SELECT * FROM supervisor_events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._event_from_row(row)

    def list_events(self, *, target: str | None = None, state: str | None = None,
                    unacknowledged_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        clauses, params = [], []
        if target is not None:
            clauses.append("(target = ? OR watch_key = ?)")
            params.extend([target, target])
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if unacknowledged_only:
            clauses.append("acknowledged_at IS NULL")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = "SELECT * FROM supervisor_events" + where + " ORDER BY id DESC LIMIT ?"
        with self._connect() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [self._event_from_row(row) for row in rows]

    def ack_event(self, event_id: int) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE supervisor_events SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL",
                (now, event_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM supervisor_events WHERE id = ?", (event_id,)).fetchone()
        return self._event_from_row(row)

    def prune_events(self, retention: int) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM supervisor_events WHERE id NOT IN "
                "(SELECT id FROM supervisor_events ORDER BY id DESC LIMIT ?)",
                (retention,),
            )
        return cursor.rowcount

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["schema_version"] = EVENT_SCHEMA_VERSION
        try:
            item["metadata"] = json.loads(item["metadata"]) if item["metadata"] else {}
        except (TypeError, ValueError):
            item["metadata"] = {}
        return item


@dataclass
class SupervisorService:
    """Tool-facing surface + the actual per-tick polling logic. Shared by
    supervisor_run_once (one synchronous pass, for tests/manual use) and the
    background SupervisorLoop thread (repeated passes on a timer) — the
    poll logic itself is identical either way, only the driver differs."""

    terminal: TerminalService
    store: SupervisorStore

    @property
    def config(self) -> SupervisorConfig:
        return self.terminal.config.supervisor

    # -- watch management (supervisor_watch / _unwatch / _list_watches) ---

    def watch(self, binding: str | None = None, session: str | None = None) -> dict[str, Any]:
        if (binding is None) == (session is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        if binding is not None:
            if self.terminal.bindings.get(binding) is None:
                return {"error": "BINDING_NOT_FOUND", "binding": binding}
            kind, target = "binding", binding
        else:
            # Same predicate _guard()/terminal_status() already enforce — a
            # watch can never be created for a session outside the whitelist.
            if not session_allowed(session, self.terminal.config):
                return {"error": "ACCESS_DENIED", "session": session}
            kind, target = "session", session
        row, created = self.store.upsert_watch(kind, target, source="manual")
        return {**self._watch_view(row), "created": created}

    def unwatch(self, binding: str | None = None, session: str | None = None, delete: bool = False) -> dict[str, Any]:
        if (binding is None) == (session is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        key = watch_key("binding", binding) if binding is not None else watch_key("session", session)
        if delete:
            if not self.store.delete_watch(key):
                return {"error": "WATCH_NOT_FOUND", "watch_key": key}
            return {"watch_key": key, "deleted": True}
        if not self.store.set_enabled(key, False, disabled_reason="manual_unwatch"):
            return {"error": "WATCH_NOT_FOUND", "watch_key": key}
        return {"watch_key": key, "disabled": True}

    def list_watches(self) -> dict[str, Any]:
        return {"watches": [self._watch_view(row) for row in self.store.list_watches()]}

    def status(self) -> dict[str, Any]:
        watches = self.store.list_watches()
        counts: dict[str, int] = {state: 0 for state in SUPERVISOR_STATES}
        stalled = 0
        for row in watches:
            if row["enabled"]:
                counts[row["state"]] = counts.get(row["state"], 0) + 1
            if row["disabled_reason"] in ("same_failure_limit_exceeded", "max_iterations_exceeded"):
                stalled += 1
        return {
            "config_enabled": self.config.enabled,
            "loop_running": _ACTIVE_LOOP is not None and _ACTIVE_LOOP.is_alive(),
            "poll_interval_seconds": self.config.poll_interval_seconds,
            "last_poll_at": _LAST_POLL_AT[0],
            "watch_count": len(watches),
            "enabled_watch_count": sum(1 for row in watches if row["enabled"]),
            "state_counts": counts,
            "stalled_count": stalled,
        }

    def list_events(self, target: str | None = None, state: str | None = None,
                    unacknowledged_only: bool = False, limit: int = 50) -> dict[str, Any]:
        if state is not None and state not in SUPERVISOR_STATES:
            return {"error": "INVALID_STATE", "events": []}
        return {"events": self.store.list_events(target=target, state=state,
                                                  unacknowledged_only=unacknowledged_only, limit=limit)}

    def ack_event(self, event_id: int) -> dict[str, Any]:
        event = self.store.ack_event(event_id)
        if event is None:
            return {"error": "EVENT_NOT_FOUND_OR_ALREADY_ACKNOWLEDGED", "id": event_id}
        return {"acknowledged": True, "event": event}

    # -- polling --------------------------------------------------------

    def run_once(self) -> dict[str, Any]:
        """One synchronous pass over every enabled watch (config-seeded ones
        included). Used by the background loop and directly exposed as
        supervisor_run_once for deterministic/manual testing."""
        self._sync_config_watches()
        events = []
        for row in self.store.list_watches():
            if not row["enabled"]:
                continue
            event = self._poll_one(row)
            if event is not None:
                events.append(event)
        self.store.prune_events(self.config.event_retention)
        _LAST_POLL_AT[0] = datetime.now(timezone.utc).isoformat()
        return {"polled": True, "events": events}

    def _sync_config_watches(self) -> None:
        for binding_name in self.config.watched_bindings:
            if self.terminal.bindings.get(binding_name) is not None:
                self.store.upsert_watch("binding", binding_name, source="config_binding")
        if not self.config.watched_session_patterns:
            return
        try:
            sessions = self.terminal.tmux.list_sessions()
        except Exception:
            return
        for item in sessions:
            if not session_allowed(item.name, self.terminal.config):
                continue
            if any(fnmatch.fnmatchcase(item.name, pattern) for pattern in self.config.watched_session_patterns):
                self.store.upsert_watch("session", item.name, source="config_pattern")

    def _poll_one(self, row: dict[str, Any]) -> dict[str, Any] | None:
        kind, target, key = row["kind"], row["target"], row["watch_key"]
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        iteration_count = row["iteration_count"] + 1

        result = (self.terminal.terminal_status_bound(target) if kind == "binding"
                  else self.terminal.terminal_status(target))

        if "error" in result:
            # Never observe a denied/errored target: stop watching it rather
            # than retry against something the whitelist has since excluded.
            return self._transition(row, now_iso, iteration_count, new_state="UNKNOWN",
                                     event_type="watch_target_missing",
                                     reason=f"{result['error']}: no longer observable",
                                     output="", output_hash=row["last_output_hash"],
                                     disable=True, disabled_reason="access_denied_or_error")

        if result.get("state") == "MISSING" or result.get("exists") is False:
            return self._transition(row, now_iso, iteration_count, new_state="UNKNOWN",
                                     event_type="watch_target_missing",
                                     reason=result.get("reason", "session/binding no longer exists"),
                                     output="", output_hash=row["last_output_hash"],
                                     disable=True, disabled_reason="target_missing")

        base_state = result.get("state", "UNKNOWN")
        base_reason = result.get("reason", "")
        output = result.get("last_output", "") or ""
        state, reason = classify_supervisor_state(base_state, base_reason, output)

        output_hash = text_fingerprint(output) if output else row["last_output_hash"]
        output_changed = output_hash != row["last_output_hash"]

        if state in ("RUNNING", "UNKNOWN") and not output_changed:
            reference = row["last_output_change_at"] or row["created_at"]
            try:
                quiet_for = (now - datetime.fromisoformat(reference)).total_seconds()
            except ValueError:
                quiet_for = 0
            if quiet_for >= self.config.idle_threshold_seconds:
                state = "IDLE"
                reason = f"no new output for {int(quiet_for)}s (idle_threshold={self.config.idle_threshold_seconds}s)"

        same_failure_count = row["same_failure_count"] + 1 if (state == "ERROR" and not output_changed) else (
            1 if state == "ERROR" else 0)

        if same_failure_count > self.config.same_failure_limit:
            return self._transition(row, now_iso, iteration_count, new_state=state, event_type="stalled",
                                     reason=f"same ERROR repeated {same_failure_count}x "
                                            f"(same_failure_limit={self.config.same_failure_limit}): {reason}",
                                     output=output, output_hash=output_hash, same_failure_count=same_failure_count,
                                     disable=True, disabled_reason="same_failure_limit_exceeded")
        if iteration_count >= self.config.max_iterations:
            return self._transition(row, now_iso, iteration_count, new_state=state, event_type="stalled",
                                     reason=f"max_iterations ({self.config.max_iterations}) reached: {reason}",
                                     output=output, output_hash=output_hash, same_failure_count=same_failure_count,
                                     disable=True, disabled_reason="max_iterations_exceeded")

        if state == row["state"]:
            # No meaningful transition: update bookkeeping only, emit nothing
            # (dedupe — repeated identical state/output never re-alerts).
            self.store.update_watch_progress(
                key, state=state, state_changed=False, output_hash=output_hash,
                output_changed=output_changed, iteration_count=iteration_count,
                same_failure_count=same_failure_count, now_iso=now_iso,
                enabled=True, disabled_reason=None,
            )
            return None

        event_type = _ATTENTION_EVENT_TYPES.get(state, "state_changed")
        return self._transition(row, now_iso, iteration_count, new_state=state, event_type=event_type,
                                 reason=reason, output=output, output_hash=output_hash,
                                 same_failure_count=same_failure_count)

    def _transition(self, row: dict[str, Any], now_iso: str, iteration_count: int, *, new_state: str,
                    event_type: str, reason: str, output: str, output_hash: str | None,
                    same_failure_count: int = 0, disable: bool = False,
                    disabled_reason: str | None = None) -> dict[str, Any]:
        key, kind, target = row["watch_key"], row["kind"], row["target"]
        event = self.store.add_event(
            watch_key=key, kind=kind, target=target, previous_state=row["state"],
            state=new_state, event_type=event_type, reason=reason,
            output_preview=sanitized_preview(output) if output else "",
            output_hash=output_hash, iteration_count=iteration_count,
            metadata={"source": row["source"]},
        )
        self.store.update_watch_progress(
            key, state=new_state, state_changed=True, output_hash=output_hash,
            output_changed=output_hash != row["last_output_hash"], iteration_count=iteration_count,
            same_failure_count=same_failure_count, now_iso=now_iso,
            enabled=not disable, disabled_reason=disabled_reason,
        )
        return event

    def _watch_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "watch_key": row["watch_key"], "kind": row["kind"], "target": row["target"],
            "source": row["source"], "enabled": bool(row["enabled"]), "state": row["state"],
            "state_since": row["state_since"], "last_activity": row["last_activity"],
            "iteration_count": row["iteration_count"], "same_failure_count": row["same_failure_count"],
            "disabled_reason": row["disabled_reason"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


# -- background loop lifecycle -------------------------------------------
# Module-level (not a class attribute) so `supervisor_status` can report on
# it without every SupervisorService needing a back-reference; there is at
# most one loop per process (server_http.main() creates exactly one).

_ACTIVE_LOOP: "SupervisorLoop | None" = None
_LAST_POLL_AT: list[str | None] = [None]


class SupervisorLoop:
    """Runs SupervisorService.run_once() on a timer in a daemon background
    thread. Deliberately not asyncio-integrated with the MCP server's own
    event loop (server.run() is a blocking, framework-owned call) — a plain
    daemon thread with an interruptible stop Event is the simplest correct
    way to add a background poller here without touching that machinery."""

    def __init__(self, service: SupervisorService) -> None:
        self.service = service
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        global _ACTIVE_LOOP
        if self._thread is not None:
            return  # already started; never spawn a second loop for this instance
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-supervisor", daemon=True)
        self._thread.start()
        _ACTIVE_LOOP = self

    def stop(self, timeout: float = 5.0) -> None:
        global _ACTIVE_LOOP
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if _ACTIVE_LOOP is self:
            _ACTIVE_LOOP = None

    def _run(self) -> None:
        interval = max(5, self.service.config.poll_interval_seconds)
        while not self._stop_event.is_set():
            try:
                self.service.run_once()
            except Exception:
                pass  # never let one bad poll cycle kill the background loop
            self._stop_event.wait(interval)
