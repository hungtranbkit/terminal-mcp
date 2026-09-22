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
from typing import Any, Callable

from .audit import sanitized_preview, text_fingerprint
from .config import AppConfig, SupervisorConfig
from .core import TerminalService
from . import scheduler_health
from .schema import Migration, apply_migrations
from .status import (KNOWN_VERIFIER_KINDS, SUPERVISOR_STATES, classify_supervisor_state,
                     parse_completion_marker, parse_evidence_markers, to_legacy_event_type,
                     to_legacy_state, verify_completion_marker, verify_evidence_marker)
from .tmux import TmuxError
from .verifier import VerifierPolicy, run_verifier

_LOGGER = logging.getLogger(__name__)

# P1 hardening item #10: SQL schema version tracking (PRAGMA user_version),
# distinct from EVENT_SCHEMA_VERSION below (that one versions the JSON
# *shape* of one persisted event for API consumers; this one versions the
# actual SQL tables) -- see schema.py's module docstring for the baseline-
# then-append pattern this follows.
SUPERVISOR_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: watches + supervisor_events (incl. every P0-2/P0-7/P0-8 column "
                "already added by the pre-existing ad-hoc pattern) as of the P1 hardening pass",
             lambda connection: None),
]

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
    "state_changed", "attention_required", "completion_candidate", "verifying", "verified_done",
    "verification_failed", "verification_blocked",
    "error_detected", "stalled", "watch_target_missing",
    # blg_8d65afc1b38b: a watch brought back from a recoverable disable.
    # Recorded as its own type so "coverage was lost and restored" is
    # visible in the event stream rather than inferred from a gap.
    "watch_reconciled",
    # Additive (P1 node-aware watch routing): the recoverable sibling of
    # watch_target_missing. "We could not reach the node", never "the
    # target is gone" -- consumers that only know the older list see an
    # unfamiliar event_type, not a wrong one.
    "watch_target_unavailable",
)
_ATTENTION_EVENT_TYPES = {
    "WAITING_INPUT": "attention_required",
    "COMPLETION_CANDIDATE": "completion_candidate",
    "VERIFYING": "verifying",
    "VERIFIED_DONE": "verified_done",
    "FAILED": "verification_failed",
    "BLOCKED": "verification_blocked",
    "ERROR": "error_detected",
}


def default_supervisor_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_SUPERVISOR_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "supervisor.db"


LOCAL_NODE_ID = "local"
"""Mirrors controller.LOCAL_NODE_ID. Duplicated as a literal rather than
imported because supervisor.py must keep working in a deployment that has
no controller wired at all (v1-only), and importing controller.py here
would make the multi-node layer a hard dependency of the local one."""

NODE_UNAVAILABLE_ERRORS = frozenset({"NODE_UNREACHABLE", "NODE_OFFLINE", "NODE_NOT_FOUND"})
"""Routing errors that mean "the NODE could not be asked right now",
never "the session is gone". A watch hitting one of these keeps its
identity and stays ENABLED -- the node coming back must resume the same
watch. Distinct from SESSION_NOT_FOUND/MISSING, which really are the
target being gone and keep their existing disable behaviour.

AMBIGUOUS_SESSION is deliberately NOT here: a watch that resolved to a
node stores that node_id and polls a qualified name, which cannot go
ambiguous later. Seeing it would mean a genuine identity problem, and
falling through to the existing error branch is the honest response."""


def watch_key(kind: str, target: str, node_id: str | None = None) -> str:
    """The canonical watch identity.

    A session watch is identified by (node_id, session_name), but the key
    STRING stays byte-identical to the pre-multi-node one for anything
    local: `session:<name>` when the node is the local node or unknown,
    `session:<node_id>/<name>` only when the target genuinely lives on
    another node. That is what lets every persisted legacy row keep
    loading, keeps `supervisor_unwatch(session=...)` finding the row it
    always found, and still gives two same-named sessions on two
    different nodes two distinct watches.

    The qualified form reuses the `node/session` convention
    controller.resolve_session already accepts and documents as "never
    ambiguous by construction" -- this is not a second addressing scheme.

    Binding keys are untouched: bindings remain local-node-scoped (see
    docs/multi-node.md's own Phase A/B limitation)."""
    if kind == "session" and node_id and node_id != LOCAL_NODE_ID:
        return f"{kind}:{node_id}/{target}"
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


def _parse_json_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item) for item in parsed)


def verifier_policy_from_row(row: dict[str, Any], *, default_timeout_seconds: float) -> VerifierPolicy:
    """P0 Part C: build the immutable VerifierPolicy run_verifier actually
    executes from a watch row -- entirely from operator-configured columns
    (set once, at watch time, via SupervisorService.watch), never from
    anything the target pane printed."""
    timeout = row.get("verifier_timeout_seconds")
    return VerifierPolicy(
        worktree=row.get("verifier_worktree") or None,
        require_git_clean=bool(row.get("verifier_require_git_clean")),
        require_commit_matches=row.get("verifier_require_commit_matches") or None,
        test_command=_parse_json_list(row.get("verifier_test_command")),
        test_timeout_seconds=float(timeout) if timeout else default_timeout_seconds,
        checklist=_parse_json_list(row.get("verifier_checklist")),
    )


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
                # Node-aware watch identity (P1: supervisor remote-session
                # watch identity/routing). NULL on every pre-existing row
                # and on every purely-local watch, which is exactly what
                # makes this additive: a NULL node_id means "resolve the
                # way this always did", so a legacy database keeps behaving
                # identically after the upgrade with no backfill step.
                ("node_id", "TEXT"),
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
                # P0 Part C: independent-verifier policy, operator-
                # configured at watch time (see SupervisorService.watch),
                # never derived from pane content. NULL/empty means "no
                # verifier policy" -- see VerifierPolicy.is_configured and
                # _handle_completion_candidate's BLOCKED path for an
                # autonomous watch with none set.
                ("verifier_worktree", "TEXT"),
                ("verifier_require_git_clean", "INTEGER NOT NULL DEFAULT 0"),
                ("verifier_require_commit_matches", "TEXT"),
                ("verifier_test_command", "TEXT"),  # JSON list
                ("verifier_timeout_seconds", "REAL"),
                ("verifier_checklist", "TEXT"),  # JSON list
                # Durable VERIFYING marker + last verifier outcome, written
                # before/after run_verifier() actually executes -- see
                # set_verifying/record_verifier_result and _poll_one's
                # restart-reconciliation check.
                ("verifying_since", "TEXT"),
                ("last_verifier_result", "TEXT"),  # JSON (verifier.run_verifier's return shape)
                ("last_verifier_pass", "INTEGER"),
                ("last_verifier_checked_at", "TEXT"),
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
            # blg_8d65afc1b38b: how many times reconciliation has tried to
            # bring this watch back. Additive, defaulted, same ALTER-if-
            # absent idiom as the P0-2 columns above -- an existing row
            # simply starts at zero.
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(watches)").fetchall()}
            if "reconcile_attempts" not in existing_columns:
                connection.execute("ALTER TABLE watches ADD COLUMN reconcile_attempts INTEGER NOT NULL DEFAULT 0")
            apply_migrations(connection, SUPERVISOR_MIGRATIONS)
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

    def rename_target(self, old_target: str, new_target: str) -> int:
        """Rename Session feature: re-keys any "session"-kind watch
        pointed at `old_target` onto `new_target` -- watch_key is
        DERIVED from (kind, target), so this isn't a plain column
        update: every other column (state/iteration_count/
        same_failure_count/pins/completion nonce/...) is carried over
        onto a row under the NEW key, and the old row is removed,
        exactly the same "never lose history, just re-key" contract
        bindings.py/session_registry.py already give this feature.
        "binding"-kind watches are untouched here -- a binding watch's
        target is a BINDING name, not a session name; the session it
        actually points at is handled separately by
        BindingStore.rename_session_references. Returns the number of
        watches re-keyed (0 is the common case: most sessions have no
        active watch)."""
        old_key = watch_key("session", old_target)
        new_key = watch_key("session", new_target)
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (old_key,)).fetchone()
            if row is None:
                # A fleet watch is stored under its QUALIFIED target
                # ("node/session") while a rename arrives bare, from the node
                # that performed it. An exact-key lookup therefore missed it
                # and the watch kept pointing at a name that no longer exists
                # -- disabled as target_missing on its next poll, for a session
                # that was alive the whole time under a new name.
                row = self._find_watch_by_bare_target(connection, old_target)
                if row is None:
                    return 0
                node_prefix = row["target"].partition("/")[0]
                if "/" not in new_target:
                    new_target = f"{node_prefix}/{new_target}"
                    new_key = watch_key("session", new_target)
                old_key = row["watch_key"]
            data = dict(row)
            data["watch_key"] = new_key
            data["target"] = new_target
            columns = list(data.keys())
            connection.execute(
                f"INSERT OR REPLACE INTO watches ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                tuple(data[c] for c in columns),
            )
            connection.execute("DELETE FROM watches WHERE watch_key = ?", (old_key,))
        return 1

    @staticmethod
    def _find_watch_by_bare_target(connection, bare_target: str):
        """The one session-kind watch whose target is `node/<bare_target>`.

        Returns None when there is no match OR more than one: two nodes holding
        the same bare name is exactly the ambiguity controller.resolve_session
        refuses to guess about, and re-keying the wrong node's watch would point
        it at a session on a machine that never renamed anything."""
        if "/" in bare_target:
            return None
        rows = connection.execute(
            "SELECT * FROM watches WHERE kind = 'session' AND target LIKE ? AND target NOT LIKE ?",
            (f"%/{bare_target}", f"%/%/{bare_target}")).fetchall()
        matches = [row for row in rows if row["target"].partition("/")[2] == bare_target]
        return matches[0] if len(matches) == 1 else None

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
                     required_verifiers: tuple[str, ...] | None = None,
                     node_id: str | None = None) -> tuple[dict[str, Any], bool]:
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
        key = watch_key(kind, target, node_id)
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
                     required_verifiers, node_id)
                    VALUES (?, ?, ?, ?, 1, 'UNKNOWN', ?, NULL, NULL, NULL, 0, 0, NULL, ?, ?, ?, ?, ?, ?, 1, NULL,
                            ?, ?)""",
                    (key, kind, target, source, now, now, now,
                     pinned_session_id, pinned_pane_id, pinned_created_epoch, nonce, verifiers_json, node_id),
                )
                created = True
            elif required_verifiers is None:
                connection.execute(
                    """UPDATE watches SET enabled = 1, disabled_reason = NULL, updated_at = ?,
                       pinned_session_id = ?, pinned_pane_id = ?, pinned_created_epoch = ?,
                       completion_nonce = ?, completion_attempt = completion_attempt + 1,
                       completion_nonce_consumed_at = NULL, node_id = COALESCE(?, node_id)
                       WHERE watch_key = ?""",
                    (now, pinned_session_id, pinned_pane_id, pinned_created_epoch, nonce, node_id, key),
                )
                created = False
            else:
                connection.execute(
                    """UPDATE watches SET enabled = 1, disabled_reason = NULL, updated_at = ?,
                       pinned_session_id = ?, pinned_pane_id = ?, pinned_created_epoch = ?,
                       completion_nonce = ?, completion_attempt = completion_attempt + 1,
                       completion_nonce_consumed_at = NULL, required_verifiers = ?,
                       node_id = COALESCE(?, node_id)
                       WHERE watch_key = ?""",
                    (now, pinned_session_id, pinned_pane_id, pinned_created_epoch, nonce, verifiers_json,
                     node_id, key),
                )
                created = False
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
        return dict(row), created

    def set_verifier_policy(self, key: str, *, worktree: str | None, require_git_clean: bool,
                            require_commit_matches: str | None, test_command: tuple[str, ...],
                            timeout_seconds: float | None, checklist: tuple[str, ...]) -> dict[str, Any] | None:
        """P0 Part C: configure (or clear, by passing every field back to
        its empty default) a watch's independent-verifier policy. A
        deliberately separate call from upsert_watch/watch (not folded
        into the create/re-enable flow) -- unlike the pin/nonce fields,
        which always refresh on every watch()/re-watch, a verifier policy
        is operator-set-and-forget: it should NOT need to be re-supplied
        on every re-watch just to keep it, and should not silently reset
        to empty because a re-watch call omitted it. Returns the updated
        row, or None if the watch doesn't exist."""
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE watches SET verifier_worktree = ?, verifier_require_git_clean = ?,
                   verifier_require_commit_matches = ?, verifier_test_command = ?,
                   verifier_timeout_seconds = ?, verifier_checklist = ?, updated_at = ?
                   WHERE watch_key = ?""",
                (worktree, int(require_git_clean), require_commit_matches, json.dumps(list(test_command)),
                 timeout_seconds, json.dumps(list(checklist)),
                 datetime.now(timezone.utc).isoformat(), key),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute("SELECT * FROM watches WHERE watch_key = ?", (key,)).fetchone()
        return dict(row)

    def set_verifying(self, key: str, now_iso: str) -> None:
        """P0 Part C: durable pre-verification write -- committed BEFORE
        run_verifier() actually executes, so a crash mid-verification
        leaves an observable VERIFYING row (not a silent gap) that the
        next poll cycle safely re-verifies (see SupervisorService._poll_one's
        restart-reconciliation check)."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE watches SET state = 'VERIFYING', verifying_since = ?, updated_at = ? WHERE watch_key = ?",
                (now_iso, now_iso, key),
            )

    def record_verifier_result(self, key: str, now_iso: str, result: dict[str, Any]) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE watches SET last_verifier_result = ?, last_verifier_pass = ?,
                   last_verifier_checked_at = ?, updated_at = ? WHERE watch_key = ?""",
                (json.dumps(result), int(bool(result.get("overall_pass"))), now_iso, now_iso, key),
            )

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

    def reenable_watch(self, key: str, *, attempts: int) -> bool:
        """Bring a disabled watch back and record the attempt.

        Separate from set_enabled on purpose: this is the ONLY path that
        turns a watch back on, so the attempt counter can never be
        bypassed by a caller that just flips `enabled`."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE watches SET enabled = 1, disabled_reason = NULL, reconcile_attempts = ?, "
                "updated_at = ? WHERE watch_key = ? AND enabled = 0",
                (attempts, now, key))
        return cursor.rowcount == 1

    def reset_iterations(self, key: str) -> None:
        """Clear the poll ceiling and the failure streak for a watch that
        reconciliation just brought back. Without this, a watch re-enabled
        at iteration_count >= max_iterations disables itself again on its
        next quiet poll and the fix looks like it did nothing."""
        with self._connection() as connection:
            connection.execute(
                "UPDATE watches SET iteration_count = 0, same_failure_count = 0 WHERE watch_key = ?", (key,))

    def note_reconcile_attempt(self, key: str, *, attempts: int) -> None:
        """Record a try that did NOT re-enable (still missing, backing
        off) so the backoff actually advances instead of retrying forever
        at the same interval."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                "UPDATE watches SET reconcile_attempts = ?, updated_at = ? WHERE watch_key = ?",
                (attempts, now, key),
            )

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


def _status_is_absent(result: dict[str, Any]) -> bool:
    """Does this status answer mean "I cannot see that session"?

    One predicate for the three shapes that all mean the same thing -- an error,
    state MISSING, or exists=False -- so the poll path and the resolution
    fallback below cannot disagree about what counts as absent."""
    if "error" in result:
        return True
    return result.get("state") == "MISSING" or result.get("exists") is False


def bare_session_name(target: str) -> str:
    """The session name without its node qualifier.

    Grants, whitelist patterns and `_read_authorized` are all keyed by the
    SESSION name; a node-qualified "hp/hp1" matches none of them. Asking the
    read gate about the qualified form therefore denied every fleet watch --
    `watch(session="hp/hp1")` returned ACCESS_DENIED outright, which is why the
    watches in production are all bare names that then resolved against the
    wrong node. Authorization is asked about the session; routing is done with
    the qualified target."""
    node, sep, bare = target.partition("/")
    return bare if sep and bare else target


def _deduplicate(names: list[str]) -> list[str]:
    """Order-preserving unique. A session can appear both in the local list and
    in a fleet listing that includes the local node; seeding it twice is
    harmless but re-upserting it churns updated_at for no reason."""
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


@dataclass
class SupervisorService:
    """Tool-facing surface + the actual per-tick polling logic. Shared by
    supervisor_run_once (one synchronous pass, for tests/manual use) and the
    background SupervisorLoop thread (repeated passes on a timer) — the
    poll logic itself is identical either way, only the driver differs."""

    terminal: TerminalService
    store: SupervisorStore
    # P0 Part C: set post-construction by whoever wires v1 and v2 together
    # (build_supervisor_v2, supervisor2.py) -- a duck-typed callback rather
    # than an import of supervisor2 here, which would be a real layering
    # violation (v1 has no business knowing v2's module exists). Default
    # (no v2 wired up at all -- a v1-only deployment) means "nothing is
    # ever autonomous", which preserves today's quiet-window-only
    # promotion behavior unchanged for exactly the deployments Part C's
    # new restriction was never meant to touch: nothing acts on
    # VERIFIED_DONE automatically without v2's approved_auto_continue
    # policy anyway, so gating a v1-only watch on an independent verifier
    # it likely has none configured for would be a pure regression with
    # no safety benefit.
    # Fleet-aware session status. Set post-construction by whoever holds a
    # ControllerService (mcp_app.build_mcp), a duck-typed callback rather than
    # an import of controller.py here -- the same layering choice
    # autonomous_check below already makes, and for the same reason.
    #
    # Why this exists: every status call in this file used to go to the LOCAL
    # TerminalService. A watch on another node's session therefore resolved
    # against local tmux, came back MISSING, and was disabled permanently on
    # its FIRST poll. That is not hypothetical -- it is what six of the ten
    # watches in production are: wtest/win2 (the Windows node) and
    # hp1/hp2/hp3-work/hp-work (the hp node), every one of them
    # disabled_reason=target_missing at iteration_count=1 while the sessions
    # themselves were alive on their own nodes.
    #
    # None means local-only, which is exactly what a single-node deployment
    # and every existing test already got.
    fleet_status: Callable[[str], dict[str, Any]] | None = None
    # Fleet-wide session names for config-pattern seeding, same wiring and
    # same None-means-local default.
    fleet_sessions: Callable[[], list[str]] | None = None
    # Node-aware watch routing (P1: supervisor remote-session watch
    # identity/routing). Duck-typed and set post-construction by whoever
    # wires the multi-node layer (mcp_app/server_http/dashboard), the same
    # posture as autonomous_check below and for the same reason: a v1-only
    # or single-node deployment must keep working with this left as None,
    # in which case every code path below falls back to exactly the local
    # TerminalService behaviour it had before.
    #
    # Only two methods are ever used: resolve_session(name) and
    # terminal_status(name). That is deliberately the SAME remote-aware
    # read adapter terminal_list_sessions/terminal_status already go
    # through -- this module must never grow its own node RPC.
    controller: Any = None
    autonomous_check: Callable[[str], bool] | None = None
    # Called when an autonomous watch's completion gate resolves to FAILED
    # or BLOCKED (see _handle_completion_candidate) -- v2 wires this to
    # actually halt further autonomous action-taking for that watch
    # (SupervisorV2Store.block_policy), not just record a state string.
    on_autonomous_verification_blocked: Callable[[str, str], None] | None = None

    @property
    def config(self) -> SupervisorConfig:
        return self.terminal.config.supervisor

    def _is_autonomous(self, key: str) -> bool:
        try:
            return bool(self.autonomous_check and self.autonomous_check(key))
        except Exception:
            # Never let a broken hook mask a task's real completion status
            # or accidentally treat a non-autonomous watch as autonomous --
            # fail toward the *more* conservative behavior (treat as
            # autonomous, i.e. still require independent verification)
            # only when we genuinely cannot tell; a raising hook is exactly
            # that "genuinely cannot tell" case.
            _LOGGER.exception("supervisor: autonomous_check hook raised, treating watch as autonomous",
                              extra={"watch_key": key})
            return True

    def set_verifier_policy(self, binding: str | None = None, session: str | None = None, *,
                            worktree: str | None = None, require_git_clean: bool = False,
                            require_commit_matches: str | None = None,
                            test_command: list[str] | None = None,
                            timeout_seconds: float | None = None,
                            checklist: list[str] | None = None) -> dict[str, Any]:
        """P0 Part C: configure the independent verifier for an existing
        watch -- never exposed as anything derived from pane content; every
        argument here is exactly what the caller (an operator, or an
        MCP/dashboard caller acting on the operator's explicit instruction)
        passed in. worktree/test_command are real subprocess targets (see
        verifier.py) -- test_command in particular must be a literal
        argv list (e.g. ["pytest", "-q"]), never a shell string."""
        if (binding is None) == (session is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        kind, target = ("binding", binding) if binding is not None else ("session", session)
        key = watch_key(kind, target)
        if test_command is not None and (not isinstance(test_command, list)
                                         or not all(isinstance(item, str) for item in test_command)):
            return {"error": "INVALID_TEST_COMMAND", "reason": "test_command must be a list of strings (argv), "
                                                                "never a shell string"}
        row = self.store.set_verifier_policy(
            key, worktree=worktree, require_git_clean=require_git_clean,
            require_commit_matches=require_commit_matches, test_command=tuple(test_command or ()),
            timeout_seconds=timeout_seconds, checklist=tuple(checklist or ()),
        )
        if row is None:
            return {"error": "WATCH_NOT_FOUND", "watch_key": key}
        policy = verifier_policy_from_row(row, default_timeout_seconds=self.config.verifier_timeout_seconds)
        return {"watch_key": key, "configured": policy.is_configured, "worktree": policy.worktree,
                "require_git_clean": policy.require_git_clean,
                "require_commit_matches": policy.require_commit_matches,
                "test_command": list(policy.test_command), "checklist": list(policy.checklist)}

    # -- watch management (supervisor_watch / _unwatch / _list_watches) ---

    def _resolve_watch_node(self, session: str) -> tuple[str | None, str, dict[str, Any] | None]:
        """(node_id, bare_name, error) for a session a caller wants watched.

        Returns node_id=None to mean "behave exactly as before" -- no
        controller wired, or the name does not resolve anywhere right now.
        The second case is deliberate and preserves a real existing
        behaviour: watching a session that does not exist YET is allowed
        today (the watch simply stays unpinned until its first successful
        poll), and making that an error would be a regression.

        An AMBIGUOUS_SESSION resolution is returned as an error and NO
        watch is created -- picking one of two same-named sessions on two
        different nodes would silently watch the wrong machine."""
        if self.controller is None:
            return None, session, None
        if "/" in session:
            # Already qualified: unambiguous by construction, and the
            # caller's explicit answer to a previous ambiguity error.
            node_id, _, bare = session.partition("/")
            return node_id, bare, None
        try:
            resolution = self.controller.resolve_session(session)
        except Exception:  # noqa: BLE001 -- resolution is an enhancement; never fail a watch over it
            _LOGGER.warning("supervisor: node resolution failed for %r", session, exc_info=True)
            return None, session, None
        error = resolution.get("error")
        if error == "AMBIGUOUS_SESSION":
            nodes = list(resolution.get("nodes", []))
            # The hint is composed HERE rather than passed through from the
            # router's own detail: this is the one message whose whole job
            # is to tell the caller how to answer, so it must always name a
            # concrete qualified form even if the router phrased its own
            # detail differently.
            example = f"{nodes[0]}/{session}" if nodes else f"<node_id>/{session}"
            return None, session, {
                "error": "AMBIGUOUS_SESSION", "session": session, "nodes": nodes,
                "detail": (f"session {session!r} exists on more than one node ({nodes or 'unknown'}) -- "
                           f"no watch was created. Re-issue it with a qualified name, e.g. {example!r}."),
                "router_detail": resolution.get("detail"),
            }
        if error is not None:
            # SESSION_NOT_FOUND and friends: fall through to the legacy
            # local behaviour rather than refusing the watch.
            return None, session, None
        return resolution.get("node_id"), resolution.get("session", session), None

    def _status_for(self, row: dict[str, Any]) -> dict[str, Any]:
        """The ONE place a watch reads its target's status.

        Routed through the controller for a watch that carries a remote
        node identity, and through the local TerminalService for
        everything else -- which is every binding watch, every legacy row
        (node_id NULL) and every local session, so their behaviour is
        unchanged byte for byte.

        The remote read uses the QUALIFIED name, so it addresses the node
        the watch was bound to rather than re-resolving a bare name that
        could since have become ambiguous or moved."""
        kind, target = row["kind"], row["target"]
        if kind == "binding":
            return self.terminal.terminal_status_bound(target)
        node_id = row.get("node_id")
        if self.controller is not None and node_id and node_id != LOCAL_NODE_ID:
            return self.controller.terminal_status(f"{node_id}/{target}")
        return self._fleet_status_for(kind, target)

    def watch(self, binding: str | None = None, session: str | None = None,
             required_verifiers: list[str] | None = None,
             source: str = "manual") -> dict[str, Any]:
        """`source` records who asked for this watch ("manual", or
        "auto-discovery" from worker_discovery.py). It is stored on creation
        and is provenance only -- it changes no behaviour here, and callers
        that omit it keep the original "manual" value exactly as before."""
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
            kind, target, node_id = "binding", binding, None
            # No separate pin here -- a binding-kind watch defers entirely
            # to the binding's own pinned identity (bindings.py), checked
            # at send time via terminal_send_bound.
        else:
            # P0 HOTFIX: same canonical decision _guard()/terminal_status()
            # themselves use (static whitelist OR an active dashboard read
            # grant) -- a watch can never be created for a session outside
            # both, but a granted-only session (never in the static
            # whitelist) is now watchable too, same as it is readable.
            # Node resolution happens AFTER the read check below, never
            # before: resolving probes every online node's session list,
            # so answering "which nodes hold this name" for a caller who
            # is not allowed to read it would leak other nodes' session
            # inventory. Authorise the bare name first, then resolve.
            bare_name = session.partition("/")[2] if "/" in session else session
            if not self.terminal._read_authorized(bare_name):
                return {"error": "ACCESS_DENIED", "session": bare_name}
            node_id, target, ambiguity = self._resolve_watch_node(session)
            if ambiguity is not None:
                # Explicitly NO watch is created here -- an ambiguous name
                # must be answered by the caller, not guessed at.
                return ambiguity
            kind = "session"
            # P0-2: pin identity at (re-)watch time -- best-effort; a
            # session that doesn't exist yet (or a transient tmux error)
            # just leaves it unpinned, lazily adopted on the watch's next
            # successful poll instead of failing the watch call itself.
            try:
                # Local tmux only -- a remote session's identity is pinned
                # by its own node, and probing this host's tmux for a name
                # that lives elsewhere would either miss or, worse, match a
                # DIFFERENT local session that happens to share the name.
                info = (None if (node_id and node_id != LOCAL_NODE_ID)
                        else self.terminal.tmux.get_session(target))
            except TmuxError:
                info = None
            if info is not None:
                pin = {"pinned_session_id": info.session_id, "pinned_pane_id": info.pane_id,
                      "pinned_created_epoch": info.created_epoch}
        verifiers = tuple(required_verifiers) if required_verifiers is not None else None
        row, created = self.store.upsert_watch(kind, target, source=source,
                                                required_verifiers=verifiers,
                                                node_id=node_id if kind == "session" else None, **pin)
        return {**self._watch_view(row), "created": created}

    def _existing_watch_key(self, kind: str, target: str) -> str:
        """The key of an ALREADY-PERSISTED watch, for callers that address
        it by name (unwatch, completion token).

        Legacy first, always: `session:<name>` is tried before any node
        resolution, so an existing local row -- including every row
        written before this feature existed -- is found by exactly the
        lookup that always found it, with no controller call at all. Only
        when no such row exists is the name resolved to a node and the
        qualified key tried, which is what lets `supervisor_unwatch(
        session="win2")` still address a watch that lives on dell-5530.

        Falls back to the legacy key when nothing matches, so the caller
        gets the same WATCH_NOT_FOUND (naming the key it looked for) that
        it got before."""
        legacy = watch_key(kind, target)
        if kind == "binding" or self.store.get_watch(legacy) is not None:
            return legacy
        if "/" in target:
            node_id, _, bare = target.partition("/")
            return watch_key(kind, bare, node_id)
        node_id, bare, ambiguity = self._resolve_watch_node(target)
        if ambiguity is None and node_id:
            qualified = watch_key(kind, bare, node_id)
            if self.store.get_watch(qualified) is not None:
                return qualified
        return legacy

    def unwatch(self, binding: str | None = None, session: str | None = None, delete: bool = False) -> dict[str, Any]:
        if (binding is None) == (session is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        key = (self._existing_watch_key("binding", binding) if binding is not None
               else self._existing_watch_key("session", session))
        if delete:
            if not self.store.delete_watch(key):
                return {"error": "WATCH_NOT_FOUND", "watch_key": key}
            return {"watch_key": key, "deleted": True}
        if not self.store.set_enabled(key, False, disabled_reason="manual_unwatch"):
            return {"error": "WATCH_NOT_FOUND", "watch_key": key}
        return {"watch_key": key, "disabled": True}

    def list_watches(self) -> dict[str, Any]:
        return {"watches": [self._watch_view(row) for row in self.store.list_watches()]}

    def rename_session(self, old_session: str, new_session: str) -> int:
        """Thin passthrough to SupervisorStore.rename_target -- the
        wiring-layer coordination point mcp_app.py's own
        terminal_rename_session tool calls, same posture as
        unwatch(delete=False) already being called from there on
        kill/delete."""
        return self.store.rename_target(old_session, new_session)

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
        key = (self._existing_watch_key("binding", binding) if binding is not None
               else self._existing_watch_key("session", session))
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
            # blg_8d65afc1b38b: "4 watches, 0 enabled" was the whole symptom
            # and the old status could not say WHY any of them were off, nor
            # whether they were ever coming back. Split by recoverability so
            # a glance answers both.
            "disabled_watch_count": sum(1 for row in watches if not row["enabled"]),
            "recoverable_disabled_count": sum(
                1 for row in watches if not row["enabled"]
                and row["disabled_reason"] in scheduler_health.RECOVERABLE_DISABLE_REASONS),
            "intentionally_excluded_count": sum(
                1 for row in watches if not row["enabled"]
                and row["disabled_reason"] in scheduler_health.INTENTIONAL_DISABLE_REASONS),
            "disabled_reasons": {
                row["watch_key"]: row["disabled_reason"]
                for row in watches if not row["enabled"] and row["disabled_reason"]},
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

    def reconcile_watches(self) -> dict[str, Any]:
        """Bring back watches that were disabled for a RECOVERABLE reason
        and whose target is alive again.

        blg_8d65afc1b38b, and the reason production showed
        `watch_count=4, enabled_watch_count=0` while four workers were
        running: every disable path in this file is permanent, because
        until now nothing anywhere called set_enabled(..., True). A watch
        that hit `max_iterations_exceeded` while its worker carried on
        working stayed off forever, and the worker left scheduling
        visibility with it.

        What this does NOT do is override a person. `manual_unwatch` and
        the autonomous-verification refusals are deliberate exclusions and
        are left alone -- see scheduler_health.INTENTIONAL_DISABLE_REASONS.
        Recovery is backed off exponentially and capped, so a target that
        is alive but instantly re-fails does not spin.
        """
        now = datetime.now(timezone.utc)
        restored, skipped = [], []
        for row in self.store.list_watches():
            if row["enabled"]:
                continue
            attempts = int(row.get("reconcile_attempts") or 0)
            try:
                since = (now - datetime.fromisoformat(row["updated_at"])).total_seconds()
            except (ValueError, TypeError):
                since = 0.0
            # Backoff/cap/intentional exclusions do not depend on target
            # liveness.  Check them before touching tmux or a remote node;
            # the old ordering performed every expensive liveness probe and
            # only then discovered that the watch was not eligible yet.
            preflight = scheduler_health.watch_recovery_action(
                disabled_reason=row["disabled_reason"], target_alive=True,
                attempts=attempts, seconds_since_disabled=since)
            if not preflight.should_reenable:
                skipped.append({"watch_key": row["watch_key"], "target": row["target"],
                                "disabled_reason": row["disabled_reason"], "reason": preflight.reason})
                continue
            target_alive = self._target_alive(row)
            action = scheduler_health.watch_recovery_action(
                disabled_reason=row["disabled_reason"], target_alive=target_alive,
                attempts=attempts, seconds_since_disabled=since)
            if not action.should_reenable:
                # A failed due probe is a real attempt.  Persist both the
                # counter and timestamp so the next 20-second supervisor
                # cycle observes exponential backoff instead of probing the
                # same missing/degraded target forever.
                self.store.note_reconcile_attempt(row["watch_key"], attempts=attempts + 1)
                skipped.append({"watch_key": row["watch_key"], "target": row["target"],
                                "disabled_reason": row["disabled_reason"], "reason": action.reason})
                continue
            if self.store.reenable_watch(row["watch_key"], attempts=attempts + 1):
                # Reset the poll ceiling too. Re-enabling a watch that is
                # still at iteration_count >= max_iterations would have it
                # disable itself again on the very next quiet poll, which
                # looks like the fix not working.
                self.store.reset_iterations(row["watch_key"])
                restored.append({"watch_key": row["watch_key"], "target": row["target"],
                                 "was": row["disabled_reason"], "attempt": attempts + 1})
                self.store.add_event(
                    watch_key=row["watch_key"], kind=row["kind"], target=row["target"],
                    previous_state=row["state"], state=row["state"],
                    event_type="watch_reconciled",
                    reason=f"re-enabled after {row['disabled_reason']}: {action.reason}",
                    output_preview="", output_hash=row["last_output_hash"],
                    iteration_count=0,
                    metadata={"was_disabled_reason": row["disabled_reason"], "attempt": attempts + 1})
        if restored:
            _LOGGER.info("supervisor: reconciled %d watch(es) back into coverage", len(restored),
                         extra={"restored": [item["watch_key"] for item in restored]})
        return {"restored": restored, "skipped": skipped}

    def _target_alive(self, row: dict[str, Any]) -> bool:
        """Cheap liveness probe for reconciliation. Any error at all means
        'not yet' -- reconciliation simply looks again next pass, which is
        the whole point of it being a loop rather than a one-shot."""
        try:
            if row["kind"] == "binding" and self.terminal.bindings.get(row["target"]) is None:
                return False
            result = self._status_for(row)
        except Exception:  # noqa: BLE001 -- a probe that raises is simply "not alive yet"
            return False
        if "error" in result:
            return False
        return not (result.get("state") == "MISSING" or result.get("exists") is False)

    def run_once(self) -> dict[str, Any]:
        """One synchronous pass over every enabled watch (config-seeded ones
        included). Used by the background loop and directly exposed as
        supervisor_run_once for deterministic/manual testing."""
        self._sync_config_watches()
        # Before polling: give back coverage that was lost to a recoverable
        # disable. Doing it here rather than in a separate loop means a
        # restored watch is polled in the SAME pass, so recovery costs one
        # cycle rather than two.
        reconciled = self.reconcile_watches()
        events = []
        for row in self.store.list_watches():
            if not row["enabled"]:
                continue
            try:
                event = self._poll_one(row)
            except Exception:
                # P1 item #7/#8: isolate one watch's failure to that watch
                # -- never let an unexpected exception for a single target
                # abort the rest of this poll cycle (every OTHER enabled
                # watch would otherwise silently starve, potentially
                # indefinitely if the same watch fails again next cycle
                # too, since the loop would abort at the same point every
                # time). Logged with the failing watch's own identity as
                # structured fields (see logging_setup.py's JSON
                # formatter) so it's directly correlatable, not folded
                # into the generic "poll cycle failed" catch-all in
                # SupervisorLoop._run -- this watch's row is left
                # otherwise untouched (no state/failure-count bookkeeping
                # mutated on an exception path that never got far enough
                # to know what really happened).
                _LOGGER.exception(
                    "supervisor: polling one watch raised, skipping it for this cycle only",
                    extra={"watch_key": row["watch_key"], "kind": row["kind"], "target": row["target"]},
                )
                continue
            if event is not None:
                events.append(event)
        self.store.prune_events(self.config.event_retention)
        _LAST_POLL_AT[0] = datetime.now(timezone.utc).isoformat()
        return {"polled": True, "events": events, "reconciled": reconciled}

    def _sync_config_watches(self) -> None:
        for binding_name in self.config.watched_bindings:
            if self.terminal.bindings.get(binding_name) is not None:
                self.store.upsert_watch("binding", binding_name, source="config_binding")
        if not self.config.watched_session_patterns:
            return
        # Fleet-aware seeding. With only the local list, a watched_session_
        # patterns entry could never match a session on another node -- a
        # config asking to watch "hp*" simply did nothing on a controller whose
        # hp sessions all live on the hp node, with no error to explain it.
        names: list[str] = []
        try:
            names.extend(item.name for item in self.terminal.tmux.list_sessions())
        except Exception:
            # Best-effort skip for *this* sync pass only -- logged, not
            # silently swallowed, so a persistently broken tmux/config is
            # discoverable from the service log rather than only from the
            # absence of expected watches.
            _LOGGER.warning("supervisor: could not list local sessions for config-pattern watch sync",
                            exc_info=True)
        if self.fleet_sessions is not None:
            try:
                names.extend(self.fleet_sessions())
            except Exception:
                # One unreachable node must not cost the local seeding that
                # already succeeded above.
                _LOGGER.warning("supervisor: could not list fleet sessions for config-pattern watch sync",
                                exc_info=True)
        if not names:
            return
        for name in _deduplicate(names):
            # Readability is the supervisor's real prerequisite: it watches a
            # session by CAPTURING its output, so a session it cannot read is
            # a watch that can only ever report nothing. That used to be
            # approximated by the session-name whitelist; it is now asked
            # directly of the canonical gate (grants + session_access
            # defaults), so a granted session is watchable and an ungranted
            # one is not, regardless of what it is called.
            # The read gate is asked about the BARE name: grants are keyed by
            # session name, and a qualified "node/session" would match no grant
            # at all -- silently excluding every remote session from seeding
            # while looking like an authorization decision.
            if not self.terminal._read_authorized(bare_session_name(name)):
                continue
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in self.config.watched_session_patterns):
                self.store.upsert_watch("session", name, source="config_pattern")

    def _fleet_status_for(self, kind: str, target: str) -> dict[str, Any]:
        """The ONE place a watch's target is observed.

        Both polling and reconciliation go through here, deliberately: when
        they disagreed, a watch could be disabled by a poll that asked the whole
        fleet and then never revived by a reconciliation that only asked the
        local node -- disabled forever by two functions that were each
        individually correct.

        Bindings stay local. A binding's target is a binding NAME, and bindings
        are local-node-scoped in this phase (see docs/multi-node.md and
        controller.terminal_input_context's own note), so routing one to a
        remote node would resolve a name that node has never heard of."""
        if kind == "binding":
            return self.terminal.terminal_status_bound(target)
        if self.fleet_status is None:
            return self.terminal.terminal_status(target)
        if "/" in target:
            # Node-qualified: only the fleet can resolve it, and local tmux
            # cannot hold a name with a slash in it anyway.
            return self.fleet_status(target)

        # LOCAL FIRST, then the fleet. The order is the whole point, and it is
        # not a preference -- routing a bare name through the controller
        # unconditionally makes every local watch depend on the local node being
        # registered and ONLINE. A stale local heartbeat then answers
        # SESSION_NOT_FOUND for a session that is plainly running right here,
        # which would disable the local watches that currently work
        # (terminal-mcp-main, mcp-work, gatefix2-work) in order to fix the remote
        # ones. queue_loop.py already documents this exact hazard and injects a
        # heartbeat refresher to avoid it.
        #
        # Asking local first means the fleet is consulted only when local cannot
        # answer -- precisely the case that used to end in a permanent disable.
        local = self.terminal.terminal_status(target)
        if not _status_is_absent(local):
            return local
        fleet = self.fleet_status(target)
        if not _status_is_absent(fleet):
            return fleet
        # Nothing can see it. Prefer the fleet's error when it has one: "that
        # node is unreachable" tells an operator something, where a local
        # "MISSING" for a session that was never local tells them nothing.
        return fleet if "error" in fleet else local

    def _poll_one(self, row: dict[str, Any]) -> dict[str, Any] | None:
        kind, target, key = row["kind"], row["target"], row["watch_key"]
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        iteration_count = row["iteration_count"] + 1

        if row["state"] == "VERIFYING":
            # P0 Part C: restart-during-VERIFYING reconciliation. VERIFYING
            # is durable (SupervisorStore.set_verifying commits it BEFORE
            # run_verifier executes) precisely so a crash mid-verification
            # leaves this observable, safely-retryable row instead of a
            # silent gap -- resolve it by simply re-running verification,
            # independent of anything about the pane's current state (the
            # verifier never looks at the pane at all).
            return self._run_verification(row, now_iso, iteration_count)

        result = self._status_for(row)

        if result.get("error") in NODE_UNAVAILABLE_ERRORS:
            # The NODE could not be asked -- that says nothing about
            # whether the session still exists, so this must never disable
            # the watch or drop its node identity. The row stays ENABLED
            # with its node_id intact, so the very next poll after the node
            # comes back resumes this same watch rather than needing it to
            # be re-created (and re-resolved, which would be impossible
            # while the node is down).
            return self._transition(row, now_iso, iteration_count, new_state="UNKNOWN",
                                     event_type="watch_target_unavailable",
                                     reason=f"{result['error']}: node temporarily unavailable, watch retained "
                                            f"({result.get('detail', 'no detail')})",
                                     output="", output_hash=row["last_output_hash"])

        if "error" in result:
            # Stop observing, but record WHY in a way reconciliation can act on.
            # This used to collapse every error into access_denied_or_error,
            # which lost the one distinction that matters: a node that is merely
            # unreachable right now is a watch to bring back when it returns,
            # whereas a genuinely revoked grant is not the same thing at all.
            reason_code = scheduler_health.disable_reason_for_status_error(result["error"])
            return self._transition(row, now_iso, iteration_count, new_state="UNKNOWN",
                                     event_type="watch_target_missing",
                                     reason=f"{result['error']}: no longer observable",
                                     output="", output_hash=row["last_output_hash"],
                                     disable=True, disabled_reason=reason_code)

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

        # P0 Part C.1: this is exactly the point the pre-Part-C code always
        # promoted straight to VERIFIED_DONE off quiet-window/nonce/self-
        # reported-evidence alone. For a watch under autonomous policy
        # (v2's approved_auto_continue -- see autonomous_check), that is
        # now prohibited: quiet-window/marker evidence is prose/CLAIM-
        # grade, not proof, and an autonomous chain resetting off it would
        # be exactly the false-positive risk this feature closes. Such a
        # watch instead moves to VERIFYING while a real, independent
        # verifier (verifier.py -- outside the pane, real subprocess
        # execution) actually runs; a non-autonomous watch (the default,
        # and every watch before this feature existed) is completely
        # unaffected -- unchanged, direct promotion, exactly as before.
        if not self._is_autonomous(key):
            return self._transition(
                row, now_iso, iteration_count, new_state="VERIFIED_DONE", event_type="verified_done",
                reason=verified_reason, output=output, output_hash=output_hash,
                same_failure_count=same_failure_count,
                completion_candidate_since=None, completion_output_hash=None,  # cleared -- verified now
            )

        self.store.add_event(
            watch_key=key, kind=row["kind"], target=row["target"], previous_state=row["state"],
            state="VERIFYING", event_type="verifying",
            reason=f"{verified_reason}; autonomous watch -- independent verification required before VERIFIED_DONE",
            output_preview=sanitized_preview(output) if output else "",
            output_hash=output_hash, iteration_count=iteration_count, metadata={"source": row["source"]},
        )
        self.store.set_verifying(key, now_iso)
        verifying_row = {**row, "state": "VERIFYING", "last_output_hash": output_hash}
        return self._run_verification(verifying_row, now_iso, iteration_count)

    def _run_verification(self, row: dict[str, Any], now_iso: str, iteration_count: int) -> dict[str, Any] | None:
        """P0 Part C: actually run (or re-run, on restart reconciliation --
        see _poll_one) the independent verifier for an autonomous watch
        currently in VERIFYING, and resolve it to VERIFIED_DONE (pass) or
        FAILED (ran, failed) or BLOCKED (no verifier policy configured at
        all, or one that could not even run). `row` must already reflect
        VERIFYING as its current state (both callers ensure this) so the
        resulting event's previous_state is accurate."""
        key = row["watch_key"]
        policy = verifier_policy_from_row(row, default_timeout_seconds=self.config.verifier_timeout_seconds)
        if not policy.is_configured:
            reason = ("autonomous completion requires a configured independent verifier policy "
                     "(supervisor_set_verifier_policy) -- none is set for this watch")
            self.store.record_verifier_result(key, now_iso, {"checked_at": now_iso, "overall_pass": False,
                                                              "reasons": [reason], "git": None, "test": None})
            # disable=True: BLOCKED/FAILED are terminal until an operator
            # acts (configures a verifier, or fixes whatever it reported)
            # and explicitly re-watches -- without this, the *same* still-
            # done-looking pane output would re-arm COMPLETION_CANDIDATE on
            # every subsequent poll and re-run the verifier (a real
            # subprocess -- potentially an actual test suite) again and
            # again, forever, for a condition that cannot resolve itself.
            event = self._transition(row, now_iso, iteration_count, new_state="BLOCKED",
                                     event_type="verification_blocked", reason=reason,
                                     output="", output_hash=row["last_output_hash"],
                                     completion_candidate_since=None, completion_output_hash=None,
                                     disable=True, disabled_reason="autonomous_completion_blocked_no_verifier")
            self._notify_verification_blocked(key, reason)
            return event

        result = run_verifier(policy)
        self.store.record_verifier_result(key, now_iso, result)
        if result["overall_pass"]:
            commit_sha = (result.get("git") or {}).get("commit_sha")
            reason = f"independent verifier passed (commit={commit_sha or 'n/a'})"
            return self._transition(row, now_iso, iteration_count, new_state="VERIFIED_DONE",
                                    event_type="verified_done", reason=reason,
                                    output="", output_hash=row["last_output_hash"],
                                    completion_candidate_since=None, completion_output_hash=None)
        reason = "independent verifier failed: " + "; ".join(result.get("reasons") or ["unknown failure"])
        event = self._transition(row, now_iso, iteration_count, new_state="FAILED",
                                 event_type="verification_failed", reason=reason,
                                 output="", output_hash=row["last_output_hash"],
                                 completion_candidate_since=None, completion_output_hash=None,
                                 disable=True, disabled_reason="autonomous_verification_failed")
        self._notify_verification_blocked(key, reason)
        return event

    def _notify_verification_blocked(self, key: str, reason: str) -> None:
        if self.on_autonomous_verification_blocked is None:
            return
        try:
            self.on_autonomous_verification_blocked(key, reason)
        except Exception:
            _LOGGER.exception("supervisor: on_autonomous_verification_blocked hook raised",
                              extra={"watch_key": key})

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
            # Node-aware identity. NULL/absent for a binding watch, a
            # legacy row and a purely-local session -- so an existing
            # consumer sees the same fields it always did, plus one that
            # is None exactly where it used to have no concept at all.
            "node_id": row.get("node_id"),
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
            # P0 Part C: independent-verifier policy + its most recent
            # result, if any (None/None until a verifier has actually run
            # for this watch at least once).
            "verifier_configured": verifier_policy_from_row(
                row, default_timeout_seconds=self.config.verifier_timeout_seconds).is_configured,
            "last_verifier_pass": bool(row["last_verifier_pass"]) if row.get("last_verifier_pass") is not None else None,
            "last_verifier_checked_at": row.get("last_verifier_checked_at"),
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
