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

import contextlib
import fnmatch
import json
import logging
import os
import secrets
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
from .status import (KNOWN_VERIFIER_KINDS, SUPERVISOR_STATES, classify_supervisor_state,
                     parse_completion_marker, parse_evidence_markers, to_legacy_event_type,
                     to_legacy_state, verify_completion_marker, verify_evidence_marker)
from .tmux import TmuxError

_LOGGER = logging.getLogger(__name__)

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
  "metadata": {...},
  "untrusted_output": true, "untrusted_fields": ["output_preview", "reason"],
  "content_source": "session" | "binding"
}
P0-9: the last three fields are additive (present on every event; schema
number unchanged, this is not a breaking shape) -- they mark
output_preview/reason as untrusted terminal content the watched program
produced, never an instruction from terminal-mcp or this event itself.
"""

EVENT_TYPES = (
    "state_changed", "attention_required", "completion_candidate", "verified_done",
    "error_detected", "stalled", "watch_target_missing",
)
_ATTENTION_EVENT_TYPES = {
    "WAITING_INPUT": "attention_required",
    "COMPLETION_CANDIDATE": "completion_candidate",
    "VERIFIED_DONE": "verified_done",
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


def _parse_required_verifiers(row: dict[str, Any]) -> tuple[str, ...]:
    """Defensive parse of the required_verifiers JSON column -- absent/
    NULL/malformed all mean "no required verifiers" (the clear generic
    default), never an error."""
    raw = row.get("required_verifiers")
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed if item in KNOWN_VERIFIER_KINDS)


class SupervisorStore:
    """SQLite persistence for watches + events, same pattern as audit.py/
    bindings.py: 0700 state dir, 0600 db file, WAL mode, row_factory=Row."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_supervisor_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
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
            # P0-2 identity pinning columns (session-kind watches only --
            # binding-kind watches defer to the binding's own pin). Safe
            # ALTER TABLE migration on an already-populated table; existing
            # rows get NULL, adopted lazily on first use exactly like
            # bindings.py's pinned_* columns.
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(watches)").fetchall()}
            for column, declaration in (
                ("pinned_session_id", "TEXT"),
                ("pinned_pane_id", "TEXT"),
                ("pinned_created_epoch", "INTEGER"),
                # COMPLETION_CANDIDATE -> VERIFIED_DONE promotion tracking
                # (native to v1 now -- available to any watch, not only
                # ones with Supervisor v2 configured). completion_
                # output_hash is the snapshot at the moment the candidate
                # was (last re-armed) detected -- distinct from
                # last_output_hash, which always reflects the current
                # poll, so a quiet-window check needs both.
                ("completion_candidate_since", "TEXT"),
                ("completion_output_hash", "TEXT"),
                # P0-7 phase 2: nonce delivery. A fresh, unguessable,
                # single-use token minted on every (re-)watch (a new
                # "attempt"); an external caller fetches it via
                # supervisor_get_completion_token and is responsible for
                # embedding it in whatever prompt it sends to the agent
                # (through the existing guarded send path -- this module
                # never sends anything itself). A structured marker whose
                # task_id/attempt/nonce all match the CURRENT, unconsumed
                # token is materially stronger evidence than prose alone
                # and skips the quiet-window wait; consuming it (setting
                # completion_nonce_consumed_at) makes it single-use, so a
                # stale marker copied from an earlier attempt or replayed
                # from old scrollback can never verify twice.
                ("completion_nonce", "TEXT"),
                ("completion_attempt", "INTEGER NOT NULL DEFAULT 0"),
                ("completion_nonce_consumed_at", "TEXT"),
                # P0-7/P0-8 phase 3: trusted verifier hooks. JSON list of
                # KNOWN_VERIFIER_KINDS strings; empty/NULL (the default) is
                # the "clear generic default" -- no verifier configured,
                # promotion behaves exactly as phases 1/2 already do. See
                # _verifiers_satisfied.
                ("required_verifiers", "TEXT"),
            ):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE watches ADD COLUMN {column} {declaration}")
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

    @contextlib.contextmanager
    def _connection(self):
        """Open a connection, commit/rollback its transaction on exit (the
        same semantics `with self._connect() as connection:` already had —
        sqlite3.Connection's own context manager only manages the
        transaction), and *also* always close the underlying OS handle,
        which that alone never does. Relying on garbage collection to
        eventually close it leaks one real file descriptor per call — fine
        for occasional use, fatal ("Too many open files") on a hot path
        like this store's own poll loop, which calls in here dozens of
        times a minute."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- watches ----------------------------------------------------------

    def get_watch(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
        return dict(row) if row is not None else None

    def list_watches(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM watches ORDER BY watch_key").fetchall()
        return [dict(row) for row in rows]

    def upsert_watch(self, kind: str, target: str, *, source: str, enabled: bool = True,
                     pinned_session_id: str | None = None, pinned_pane_id: str | None = None,
                     pinned_created_epoch: int | None = None,
                     required_verifiers: tuple[str, ...] | None = None) -> tuple[dict[str, Any], bool]:
        """Create a watch, or re-enable/replace source on an existing one.
        Never resets state/iteration/failure bookkeeping on an existing row —
        only creation or an explicit re-enable touches those. A re-enable
        (supervisor_watch called again for an already-known target) DOES
        re-pin identity -- that is the explicit "I know about this, treat
        whatever answers to this name right now as correct" action,
        exactly like a binding rebind. It also mints a fresh completion
        nonce and bumps completion_attempt -- a new watch/re-watch is
        exactly what "a new attempt" means (P0-7 phase 2 nonce delivery;
        see supervisor_get_completion_token).

        required_verifiers (P0-7/8 phase 3): None means "leave whatever was
        already configured alone" on a re-enable (sticky, unlike the pin/
        nonce fields above, which always refresh) -- passing None on a
        *fresh* watch simply stores the clear generic default of no
        required verifiers. Pass an explicit tuple (including ()) to set
        or clear it outright."""
        key = watch_key(kind, target)
        now = datetime.now(timezone.utc).isoformat()
        nonce = secrets.token_urlsafe(18)
        verifiers_json = json.dumps(list(required_verifiers)) if required_verifiers is not None else "[]"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO watches
                    (watch_key, kind, target, source, enabled, state, state_since,
                     last_output_hash, last_output_change_at, last_activity,
                     iteration_count, same_failure_count, disabled_reason, created_at, updated_at,
                     pinned_session_id, pinned_pane_id, pinned_created_epoch,
                     completion_nonce, completion_attempt, completion_nonce_consumed_at,
                     required_verifiers)
                    VALUES (?, ?, ?, ?, 1, 'UNKNOWN', ?, NULL, NULL, NULL, 0, 0, NULL, ?, ?, ?, ?, ?, ?, 1, NULL, ?)""",
                    (key, kind, target, source, now, now, now,
                     pinned_session_id, pinned_pane_id, pinned_created_epoch, nonce, verifiers_json),
                )
                created = True
            elif required_verifiers is None:
                connection.execute(
                    """UPDATE watches SET enabled = 1, disabled_reason = NULL, updated_at = ?,
                       pinned_session_id = ?, pinned_pane_id = ?, pinned_created_epoch = ?,
                       completion_nonce = ?, completion_attempt = completion_attempt + 1,
                       completion_nonce_consumed_at = NULL
                       WHERE watch_key = ?""",
                    (now, pinned_session_id, pinned_pane_id, pinned_created_epoch, nonce, key),
                )
                created = False
            else:
                connection.execute(
                    """UPDATE watches SET enabled = 1, disabled_reason = NULL, updated_at = ?,
                       pinned_session_id = ?, pinned_pane_id = ?, pinned_created_epoch = ?,
                       completion_nonce = ?, completion_attempt = completion_attempt + 1,
                       completion_nonce_consumed_at = NULL, required_verifiers = ?
                       WHERE watch_key = ?""",
                    (now, pinned_session_id, pinned_pane_id, pinned_created_epoch, nonce, verifiers_json, key),
                )
                created = False
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
        return dict(row), created

    def mark_nonce_consumed(self, key: str, nonce: str, now_iso: str) -> bool:
        """Single-use enforcement: only succeeds if `nonce` is still the
        watch's CURRENT, unconsumed token -- a second attempt to consume
        the same nonce (a replayed/pasted marker, or a genuine race) finds
        completion_nonce_consumed_at already set and fails, exactly the
        same compare-and-swap shape used throughout supervisor2.py."""
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE watches SET completion_nonce_consumed_at = ?
                   WHERE watch_key = ? AND completion_nonce = ? AND completion_nonce_consumed_at IS NULL""",
                (now_iso, key, nonce),
            )
        return cursor.rowcount == 1

    def adopt_pin(self, key: str, *, pinned_session_id: str, pinned_pane_id: str,
                  pinned_created_epoch: int) -> bool:
        """Lazily pin a pre-existing watch's identity the first time it is
        used after this feature was added (pinned_session_id was NULL)."""
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE watches SET pinned_session_id = ?, pinned_pane_id = ?, pinned_created_epoch = ?
                   WHERE watch_key = ? AND pinned_session_id IS NULL""",
                (pinned_session_id, pinned_pane_id, pinned_created_epoch, key),
            )
        return cursor.rowcount == 1

    def set_enabled(self, key: str, enabled: bool, *, disabled_reason: str | None = None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE watches SET enabled = ?, disabled_reason = ?, updated_at = ? WHERE watch_key = ?",
                (int(enabled), disabled_reason, now, key),
            )
        return cursor.rowcount == 1

    def delete_watch(self, key: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM watches WHERE watch_key = ?", (key,))
        return cursor.rowcount == 1

    def update_watch_progress(self, key: str, *, state: str, state_changed: bool,
                              output_hash: str | None, output_changed: bool,
                              iteration_count: int, same_failure_count: int,
                              now_iso: str, enabled: bool, disabled_reason: str | None,
                              completion_candidate_since: str | None = None,
                              completion_output_hash: str | None = None) -> None:
        with self._connection() as connection:
            row = connection.execute("SELECT state_since FROM watches WHERE watch_key = ?", (key,)).fetchone()
            state_since = now_iso if state_changed or row is None else row["state_since"]
            connection.execute(
                """UPDATE watches SET state = ?, state_since = ?, last_output_hash = ?,
                   last_output_change_at = CASE WHEN ? THEN ? ELSE last_output_change_at END,
                   last_activity = ?, iteration_count = ?, same_failure_count = ?,
                   enabled = ?, disabled_reason = ?, updated_at = ?,
                   completion_candidate_since = ?, completion_output_hash = ?
                   WHERE watch_key = ?""",
                (state, state_since, output_hash, int(output_changed), now_iso,
                 now_iso, iteration_count, same_failure_count,
                 int(enabled), disabled_reason, now_iso,
                 completion_candidate_since, completion_output_hash, key),
            )

    # -- events -------------------------------------------------------------

    def add_event(self, *, watch_key: str, kind: str, target: str, previous_state: str | None,
                  state: str, event_type: str, reason: str, output_preview: str,
                  output_hash: str | None, iteration_count: int,
                  metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
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
        with self._connection() as connection:
            rows = connection.execute(query, (*params, limit)).fetchall()
        return [self._event_from_row(row) for row in rows]

    def ack_event(self, event_id: int) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE supervisor_events SET acknowledged_at = ? WHERE id = ? AND acknowledged_at IS NULL",
                (now, event_id),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM supervisor_events WHERE id = ?", (event_id,)).fetchone()
        return self._event_from_row(row)

    def prune_events(self, retention: int) -> int:
        with self._connection() as connection:
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
        # P0-9: output_preview is a redacted excerpt of what the watched
        # program printed -- untrusted evidence about the target, never an
        # instruction to whatever (human or external model) is calling
        # supervisor_list_events/supervisor2_list_actionable_events. reason
        # is supervisor-authored, but is *derived from* matching that
        # untrusted text, so it gets the same label out of caution.
        item["untrusted_output"] = True
        item["untrusted_fields"] = ["output_preview", "reason"]
        item["content_source"] = item.get("kind", "session")
        # P0-7/P0-8 explicit legacy adapter (status.py's to_legacy_state/
        # to_legacy_event_type): additive, opt-in fields for a caller
        # written against the pre-COMPLETION_CANDIDATE/VERIFIED_DONE
        # vocabulary -- state/event_type themselves are never silently
        # coerced back to it.
        item["legacy_state"] = to_legacy_state(item["state"])
        item["legacy_event_type"] = to_legacy_event_type(item["event_type"])
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

    def watch(self, binding: str | None = None, session: str | None = None,
             required_verifiers: list[str] | None = None) -> dict[str, Any]:
        if (binding is None) == (session is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        if required_verifiers is not None:
            unknown = sorted(set(required_verifiers) - set(KNOWN_VERIFIER_KINDS))
            if unknown:
                # Fail closed on a typo/unknown kind rather than silently
                # ignoring it -- an operator who thinks a verifier is
                # required must never end up with one that quietly isn't.
                return {"error": "UNKNOWN_VERIFIER_KIND", "unknown": unknown,
                        "known": list(KNOWN_VERIFIER_KINDS)}
        pin: dict[str, Any] = {}
        if binding is not None:
            if self.terminal.bindings.get(binding) is None:
                return {"error": "BINDING_NOT_FOUND", "binding": binding}
            kind, target = "binding", binding
            # No separate pin here -- a binding-kind watch defers entirely
            # to the binding's own pinned identity (bindings.py), checked
            # at send time via terminal_send_bound.
        else:
            # Same predicate _guard()/terminal_status() already enforce — a
            # watch can never be created for a session outside the whitelist.
            if not session_allowed(session, self.terminal.config):
                return {"error": "ACCESS_DENIED", "session": session}
            kind, target = "session", session
            # P0-2: pin identity at (re-)watch time -- best-effort; a
            # session that doesn't exist yet (or a transient tmux error)
            # just leaves it unpinned, lazily adopted on the watch's next
            # successful poll instead of failing the watch call itself.
            try:
                info = self.terminal.tmux.get_session(session)
            except TmuxError:
                info = None
            if info is not None:
                pin = {"pinned_session_id": info.session_id, "pinned_pane_id": info.pane_id,
                      "pinned_created_epoch": info.created_epoch}
        verifiers = tuple(required_verifiers) if required_verifiers is not None else None
        row, created = self.store.upsert_watch(kind, target, source="manual",
                                                required_verifiers=verifiers, **pin)
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

    def get_completion_token(self, binding: str | None = None, session: str | None = None) -> dict[str, Any]:
        """P0-7 phase 2 nonce delivery: the current, unconsumed completion
        token for this watch's current attempt. This module never sends
        anything itself -- an external caller (a human, or an external
        model orchestrating via MCP) is responsible for embedding
        task_id/attempt/nonce in whatever prompt it sends to the agent,
        through the existing guarded terminal_send_text/terminal_send_bound
        path, instructing it to echo the values back inside a
        ###TERMINAL_MCP_COMPLETION marker on genuine completion (see
        status.py's COMPLETION_MARKER_RE for the exact format). Each
        (re-)watch mints a fresh nonce and bumps attempt -- calling
        supervisor_watch again is how an operator starts a new attempt
        with a fresh, unconsumed token."""
        if (binding is None) == (session is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        key = watch_key("binding", binding) if binding is not None else watch_key("session", session)
        row = self.store.get_watch(key)
        if row is None:
            return {"error": "WATCH_NOT_FOUND", "watch_key": key}
        return {
            "watch_key": key, "task_id": key, "attempt": row["completion_attempt"],
            "nonce": row["completion_nonce"],
            "consumed": row["completion_nonce_consumed_at"] is not None,
        }

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
            "last_poll_error": _LAST_POLL_ERROR[0],
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
            # Best-effort skip for *this* sync pass only -- logged, not
            # silently swallowed, so a persistently broken tmux/config is
            # discoverable from the service log rather than only from the
            # absence of expected watches.
            _LOGGER.warning("supervisor: could not list sessions for config-pattern watch sync", exc_info=True)
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
        # Reliability cleanup: max_iterations is a ceiling on being
        # *stalled*, not on being watched at all -- a watch whose output is
        # still actively changing (real ongoing work) must not be stopped
        # merely because the raw poll count is high; only fire once the
        # ceiling is reached AND this specific poll shows no progress.
        if iteration_count >= self.config.max_iterations and not output_changed:
            return self._transition(row, now_iso, iteration_count, new_state=state, event_type="stalled",
                                     reason=f"max_iterations ({self.config.max_iterations}) reached with no "
                                            f"progress on this poll: {reason}",
                                     output=output, output_hash=output_hash, same_failure_count=same_failure_count,
                                     disable=True, disabled_reason="max_iterations_exceeded")

        if state == "COMPLETION_CANDIDATE" or row["state"] == "VERIFIED_DONE":
            return self._handle_completion_candidate(
                row, now, now_iso, iteration_count, state, reason, output, output_hash, same_failure_count,
            )

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

    def _handle_completion_candidate(self, row: dict[str, Any], now: datetime, now_iso: str,
                                     iteration_count: int, state: str, reason: str, output: str,
                                     output_hash: str | None, same_failure_count: int) -> dict[str, Any] | None:
        """COMPLETION_CANDIDATE -> VERIFIED_DONE promotion, native to v1 so
        it applies to every watch, not only ones with Supervisor v2
        configured. See status.py's SUPERVISOR_STATES docstring for why
        this split exists: prose/marker evidence alone (state ==
        COMPLETION_CANDIDATE here) is never treated as proof by itself.
        Promotion requires the candidate to hold -- unchanged output, no
        state regression -- across a *later* poll for at least
        completion_verify_quiet_seconds, OR (P0-7 phase 2) a structured
        marker whose task_id/attempt/nonce match the watch's current,
        unconsumed completion token -- materially stronger evidence, since
        it proves the agent actually saw and echoed back something only
        this supervisor instance handed out for this specific attempt, so
        it skips the wait and promotes on this very poll. The nonce is
        consumed (single-use) at the moment it verifies. (P0-7/8 phase 3)
        If the watch has required_verifiers configured, promotion also
        requires each one satisfied -- see _verifiers_satisfied."""
        key = row["watch_key"]

        if row["state"] == "VERIFIED_DONE" and state != "VERIFIED_DONE":
            # Already verified; the same static completion evidence simply
            # remaining visible on a later poll (nothing regressed) is not
            # a re-entry into candidate status -- dedupe silently, exactly
            # like the normal same-state shortcut would for any other
            # state. A REAL regression (state came back as WAITING_INPUT/
            # ERROR/etc, handled above this method entirely) still goes
            # through the normal transition path unaffected.
            self.store.update_watch_progress(
                key, state="VERIFIED_DONE", state_changed=False, output_hash=output_hash,
                output_changed=output_hash != row["last_output_hash"], iteration_count=iteration_count,
                same_failure_count=same_failure_count, now_iso=now_iso, enabled=True, disabled_reason=None,
            )
            return None

        marker = parse_completion_marker(output)
        nonce_verified = verify_completion_marker(
            marker, task_id=key, attempt=row.get("completion_attempt") or 0,
            nonce=row.get("completion_nonce"), nonce_consumed=bool(row.get("completion_nonce_consumed_at")),
        )
        was_candidate = row["state"] == "COMPLETION_CANDIDATE"
        unchanged_since_candidate = was_candidate and output_hash == row.get("completion_output_hash")

        if not nonce_verified and not unchanged_since_candidate:
            # First time entering COMPLETION_CANDIDATE, or the pane moved
            # on since the last snapshot -- (re-)arm against the CURRENT
            # snapshot rather than an earlier, now-stale one. A legitimately
            # still-active target that merely printed a DONE-looking line
            # and kept working never gets falsely promoted from a snapshot
            # that's no longer current.
            self.store.update_watch_progress(
                key, state="COMPLETION_CANDIDATE", state_changed=not was_candidate,
                output_hash=output_hash, output_changed=output_hash != row["last_output_hash"],
                iteration_count=iteration_count, same_failure_count=same_failure_count,
                now_iso=now_iso, enabled=True, disabled_reason=None,
                completion_candidate_since=now_iso, completion_output_hash=output_hash,
            )
            if not was_candidate:
                return self.store.add_event(
                    watch_key=key, kind=row["kind"], target=row["target"], previous_state=row["state"],
                    state="COMPLETION_CANDIDATE", event_type="completion_candidate", reason=reason,
                    output_preview=sanitized_preview(output) if output else "",
                    output_hash=output_hash, iteration_count=iteration_count,
                    metadata={"source": row["source"]},
                )
            # (P0-7/8 phase 3 note: this re-arm-on-any-output-change rule
            # also applies when the new output is a required-verifier's own
            # evidence marker -- printing it re-arms a fresh quiet window
            # over the combined snapshot, which then has to hold quiet a
            # SECOND time before _verifiers_satisfied is even reached below.
            # The nonce fast-path has no such double-wait, since a nonce-
            # verified completion marker never goes through this branch at
            # all -- one more reason to prefer it when a verifier is
            # required.)
            return None  # re-armed silently -- still just a candidate

        quiet_seconds = 0.0
        if not nonce_verified:
            since = datetime.fromisoformat(row["completion_candidate_since"])
            quiet_seconds = (now - since).total_seconds()
            if quiet_seconds < self.config.completion_verify_quiet_seconds:
                # Still waiting on the quiet window -- bookkeeping only, and
                # deliberately do NOT touch completion_candidate_since/
                # completion_output_hash (that would re-arm the window).
                self.store.update_watch_progress(
                    key, state="COMPLETION_CANDIDATE", state_changed=False, output_hash=output_hash,
                    output_changed=False, iteration_count=iteration_count, same_failure_count=same_failure_count,
                    now_iso=now_iso, enabled=True, disabled_reason=None,
                    completion_candidate_since=row["completion_candidate_since"],
                    completion_output_hash=row["completion_output_hash"],
                )
                return None

        verifiers_ok, verifiers_reason = self._verifiers_satisfied(row, output)
        if not verifiers_ok:
            # P0-7/8 phase 3: an operator-required verifier that is missing
            # or reports failure blocks promotion outright, regardless of
            # which path (nonce fast-path or quiet-window) got here --
            # completion evidence strong enough to promote on its own is
            # not the same as evidence the operator additionally required.
            # Deliberately do NOT consume the nonce here: it stays valid
            # for this same attempt so the watch can promote as soon as the
            # missing/failing evidence is supplied, without forcing a
            # rewatch (a fresh attempt) just because a verifier lagged.
            self.store.update_watch_progress(
                key, state="COMPLETION_CANDIDATE", state_changed=not was_candidate,
                output_hash=output_hash, output_changed=output_hash != row["last_output_hash"],
                iteration_count=iteration_count, same_failure_count=same_failure_count,
                now_iso=now_iso, enabled=True, disabled_reason=None,
                completion_candidate_since=row["completion_candidate_since"] or now_iso,
                completion_output_hash=output_hash,
            )
            if not was_candidate:
                return self.store.add_event(
                    watch_key=key, kind=row["kind"], target=row["target"], previous_state=row["state"],
                    state="COMPLETION_CANDIDATE", event_type="completion_candidate",
                    reason=f"{reason}; {verifiers_reason}",
                    output_preview=sanitized_preview(output) if output else "",
                    output_hash=output_hash, iteration_count=iteration_count,
                    metadata={"source": row["source"]},
                )
            return None

        if nonce_verified:
            # Consume it -- if this loses a race (already consumed between
            # the check above and here), fall back to the ordinary
            # quiet-window path rather than promoting on a nonce that
            # turned out not to be exclusively ours after all.
            if not self.store.mark_nonce_consumed(key, row["completion_nonce"], now_iso):
                nonce_verified = False
                since = datetime.fromisoformat(row.get("completion_candidate_since") or now_iso)
                quiet_seconds = (now - since).total_seconds()
                if quiet_seconds < self.config.completion_verify_quiet_seconds:
                    return None

        verified_reason = (
            "nonce-verified completion marker (task_id/attempt/nonce matched)" if nonce_verified
            else f"quiet for {int(quiet_seconds)}s with no regression since candidate detected ({reason})"
        )
        return self._transition(
            row, now_iso, iteration_count, new_state="VERIFIED_DONE", event_type="verified_done",
            reason=verified_reason, output=output, output_hash=output_hash,
            same_failure_count=same_failure_count,
            completion_candidate_since=None, completion_output_hash=None,  # cleared -- verified now
        )

    def _verifiers_satisfied(self, row: dict[str, Any], output: str) -> tuple[bool, str]:
        """P0-7/8 phase 3: trusted verifier hooks. Never executes anything
        itself (no test runner, no `git diff`/`git status` invocation) --
        purely reads structured evidence markers the agent already printed
        into its own pane, the exact same untrusted-but-conservatively-
        parsed pattern as the completion marker. Each required kind must
        have a well-formed evidence marker bound to this watch's CURRENT,
        unconsumed nonce/attempt (verify_evidence_marker) reporting
        status=pass -- a marker for the wrong attempt, a missing marker, or
        one reporting status=fail all count as unsatisfied, never guessed
        at. A watch with no required_verifiers configured (the default)
        always returns satisfied -- strictly additive, opt-in evidence on
        top of the existing promotion path, never a replacement for it."""
        required = _parse_required_verifiers(row)
        if not required:
            return True, "no required verifiers configured"
        evidence = parse_evidence_markers(output)
        unsatisfied = []
        for kind in required:
            marker = evidence.get(kind)
            bound = verify_evidence_marker(
                marker, task_id=row["watch_key"], attempt=row.get("completion_attempt") or 0,
                nonce=row.get("completion_nonce"), nonce_consumed=bool(row.get("completion_nonce_consumed_at")),
            )
            if not bound:
                unsatisfied.append(f"{kind}: no matching evidence for this attempt")
            elif marker["status"] != "pass":
                unsatisfied.append(f"{kind}: status={marker['status']}")
        if unsatisfied:
            return False, "required verifier(s) not satisfied: " + "; ".join(unsatisfied)
        return True, "all required verifiers passed: " + ", ".join(required)

    def _transition(self, row: dict[str, Any], now_iso: str, iteration_count: int, *, new_state: str,
                    event_type: str, reason: str, output: str, output_hash: str | None,
                    same_failure_count: int = 0, disable: bool = False,
                    disabled_reason: str | None = None,
                    completion_candidate_since: str | None = None,
                    completion_output_hash: str | None = None) -> dict[str, Any]:
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
            completion_candidate_since=completion_candidate_since,
            completion_output_hash=completion_output_hash,
        )
        return event

    def _watch_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "watch_key": row["watch_key"], "kind": row["kind"], "target": row["target"],
            "source": row["source"], "enabled": bool(row["enabled"]), "state": row["state"],
            # Explicit legacy adapter (status.py's to_legacy_state) -- see
            # _event_from_row's identical field for why this exists rather
            # than state itself ever meaning the old vocabulary.
            "legacy_state": to_legacy_state(row["state"]),
            "state_since": row["state_since"], "last_activity": row["last_activity"],
            "iteration_count": row["iteration_count"], "same_failure_count": row["same_failure_count"],
            "disabled_reason": row["disabled_reason"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "required_verifiers": list(_parse_required_verifiers(row)),
        }


# -- background loop lifecycle -------------------------------------------
# Module-level (not a class attribute) so `supervisor_status` can report on
# it without every SupervisorService needing a back-reference; there is at
# most one loop per process (server_http.main() creates exactly one).

_ACTIVE_LOOP: "SupervisorLoop | None" = None
_LAST_POLL_AT: list[str | None] = [None]
# Reliability cleanup: the poll loop must never die silently from one bad
# cycle, but a swallowed exception with zero trace was just as bad in the
# other direction -- both the timestamp and a short, redacted-safe message
# are tracked here so supervisor_status() can surface "the loop is alive
# but its last cycle errored" instead of that being invisible.
_LAST_POLL_ERROR: list[dict[str, str] | None] = [None]


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
                _LAST_POLL_ERROR[0] = None  # a clean cycle clears any prior error
            except Exception as exc:
                # Never let one bad poll cycle kill the background loop --
                # but never let it vanish without a trace either. Logged
                # with a full traceback (service log/journalctl) and
                # tracked for supervisor_status() to surface; exc's own
                # message could in principle echo pane content through an
                # unusual failure path, so it goes through the same
                # sanitized_preview truncation/redaction as everything
                # else this project persists.
                _LOGGER.exception("supervisor: poll cycle failed, will retry next interval")
                _LAST_POLL_ERROR[0] = {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "error": sanitized_preview(f"{type(exc).__name__}: {exc}", 200),
                }
            self._stop_event.wait(interval)
