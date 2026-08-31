"""Supervisor Loop v2 — a safe, auditable decision-and-send pipeline layered
on top of v1's watches/events/state machine.

Explicitly NOT an unrestricted autonomous shell loop: every send still goes
through TerminalService.terminal_send_text/terminal_send_bound — the exact
same terminal_input/whitelist/binding input_enabled/input_policy/
confirmation/sensitive-target/redaction/audit gates as every other input
path in this project. This module adds no new way to reach tmux; it only
adds a policy-gated decision to call the same guarded methods everything
else already calls.

Small durable state machine, not a queue/broker: one SQLite table per
watch's cumulative policy/counters (supervisor_policies) and one row per
send-attempt "action" (supervisor_actions), in the same db file as v1
(SupervisorStore's path), using SQLite's own atomic UPDATE ... WHERE state=?
as the compare-and-swap primitive for idempotency and concurrency — no
external lock/lease library, no message broker.

Decision interface: this module is provider-agnostic. It does not invoke
ChatGPT or any external service — there is no verified, already-configured
wake/callback mechanism in this repo to build on, and inventing one (a
webhook, a stored credential) was explicitly out of scope ("do not fake
one"). What IS implemented: the full local queue/claim/decide/approve/send
contract, exposed as MCP tools, that an external caller (a human, a script,
or a future ChatGPT relay) drives by calling supervisor2_claim_event /
supervisor2_submit_decision / supervisor2_review_action /
supervisor2_execute_send in turn. Everything up to and including the send
is fully automatic and local once a decision is approved; *producing* that
decision from an external model is the v3 wake/callback piece this
module deliberately does not fabricate.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .audit import sanitized_preview, text_fingerprint
from .models import SessionIdentity
from .redaction import redact_text
from .supervisor import SupervisorService, SupervisorStore, watch_key as make_watch_key

POLICY_MODES = ("observe_only", "suggest_only", "approved_auto_continue")
ACTION_STATES = (
    "claimed", "decided", "approved", "sent", "observing",
    "completed", "blocked", "rejected", "failed",
)
TERMINAL_ACTION_STATES = ("completed", "blocked", "rejected", "failed")
DEFAULT_LEASE_SECONDS = 300
# P0-8: minimum quiet time (no pane change, no newer ERROR) a
# COMPLETION_CANDIDATE must hold before it is promoted to VERIFIED_DONE
# and the chain is reset. A floor on top of the natural multi-poll gap
# (promotion can only happen on a later reconcile pass than the one that
# first saw the candidate) -- not itself sufficient on its own for a very
# slow poll_interval_seconds, but never less than this even for a fast one.
COMPLETION_VERIFY_QUIET_SECONDS = 10

# Content-based safety screen, checked against both the triggering event's
# output_preview and any proposed prompt: same conservative "known-shape
# marker, not a full classifier" philosophy as WAIT_PATTERNS/ERROR_PATTERNS
# in status.py. This is in addition to, not instead of, the *command*-based
# SENSITIVE_COMMANDS check core.py's _input_guard already applies to the
# pane itself (ssh/mysql/psql/sudo/passwd) — that one is untouched and
# still runs on every send regardless of anything here.
ATTENTION_STOP_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"enter (your |the )?password",
        r"api[_ -]?key",
        r"credential",
        r"\bsecret\b",
        r"\btoken\b",
        r"are you sure",
        r"irreversible",
        r"cannot be undone",
        r"permanently delete",
        r"force[ -]push",
        r"\brm -rf\b",
        r"drop (table|database)",
        r"\bsudo\b",
    )
)


def _stop_pattern_match(*texts: str) -> str | None:
    for text in texts:
        for pattern in ATTENTION_STOP_PATTERNS:
            if pattern.search(text or ""):
                return f"matched {pattern.pattern!r}"
    return None


class SupervisorV2Store:
    """Same connection/schema pattern as SupervisorStore (audit.py/
    bindings.py lineage): opens the same db file v1 already created (0700
    dir, 0600 file, WAL), adds two more tables."""

    def __init__(self, path) -> None:
        self.path = path
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_policies (
                    watch_key TEXT PRIMARY KEY,
                    policy_mode TEXT NOT NULL DEFAULT 'observe_only',
                    approved_template TEXT,
                    max_auto_actions INTEGER NOT NULL DEFAULT 5,
                    wall_clock_timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                    same_prompt_repeat_limit INTEGER NOT NULL DEFAULT 2,
                    no_progress_limit INTEGER NOT NULL DEFAULT 2,
                    auto_action_count INTEGER NOT NULL DEFAULT 0,
                    first_action_at TEXT,
                    last_prompt_hash TEXT,
                    last_prompt_repeat_count INTEGER NOT NULL DEFAULT 0,
                    no_progress_count INTEGER NOT NULL DEFAULT 0,
                    blocked_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_key TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    lease_expires_at TEXT,
                    proposed_prompt TEXT,
                    prompt_hash TEXT,
                    decision_reason TEXT,
                    approved_at TEXT,
                    approved_by TEXT,
                    output_hash_at_send TEXT,
                    send_result TEXT,
                    resulting_event_id INTEGER,
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # P0-5 revision CAS: the watch's output_hash captured at
            # *decision* time (distinct from output_hash_at_send, captured
            # at *send* time for reconciliation) -- execute_send re-checks
            # the watch's current hash against this immediately before
            # sending and aborts as STALE_DECISION if they differ, rather
            # than sending against evidence that's no longer current.
            existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(supervisor_actions)").fetchall()}
            if "expected_output_hash" not in existing_columns:
                connection.execute("ALTER TABLE supervisor_actions ADD COLUMN expected_output_hash TEXT")
            # P0-7/P0-8: completion-candidate -> verified-done tracking.
            # DONE_PATTERNS/a structured marker alone only ever produces a
            # *candidate* (completion_status='completion_candidate'); the
            # chain is reset (see _reconcile_observing_actions) only once
            # that candidate is independently corroborated by a quiet
            # window with no newer error, not from the prose/marker text
            # alone.
            for column, declaration in (
                ("completion_status", "TEXT"),
                ("completion_candidate_since", "TEXT"),
                ("completion_output_hash", "TEXT"),
            ):
                if column not in existing_columns:
                    connection.execute(f"ALTER TABLE supervisor_actions ADD COLUMN {column} {declaration}")
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
        like the poll loop this store is driven from."""
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- policy -------------------------------------------------------------

    def get_policy(self, key: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM supervisor_policies WHERE watch_key = ?", (key,)).fetchone()
        if row is not None:
            return dict(row)
        # No row yet: a plain v1 watch is observe_only by default with no
        # extra INSERT needed — the safe-by-default state costs nothing.
        return {
            "watch_key": key, "policy_mode": "observe_only", "approved_template": None,
            "max_auto_actions": 5, "wall_clock_timeout_seconds": 1800,
            "same_prompt_repeat_limit": 2, "no_progress_limit": 2,
            "auto_action_count": 0, "first_action_at": None, "last_prompt_hash": None,
            "last_prompt_repeat_count": 0, "no_progress_count": 0, "blocked_reason": None,
            "created_at": None, "updated_at": None,
        }

    def set_policy(self, key: str, *, policy_mode: str, approved_template: str | None,
                   max_auto_actions: int, wall_clock_timeout_seconds: int,
                   same_prompt_repeat_limit: int, no_progress_limit: int) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO supervisor_policies
                (watch_key, policy_mode, approved_template, max_auto_actions,
                 wall_clock_timeout_seconds, same_prompt_repeat_limit, no_progress_limit,
                 auto_action_count, first_action_at, last_prompt_hash, last_prompt_repeat_count,
                 no_progress_count, blocked_reason, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL, 0, 0, NULL, ?, ?)
                ON CONFLICT(watch_key) DO UPDATE SET
                    policy_mode = excluded.policy_mode, approved_template = excluded.approved_template,
                    max_auto_actions = excluded.max_auto_actions,
                    wall_clock_timeout_seconds = excluded.wall_clock_timeout_seconds,
                    same_prompt_repeat_limit = excluded.same_prompt_repeat_limit,
                    no_progress_limit = excluded.no_progress_limit,
                    blocked_reason = NULL, updated_at = excluded.updated_at""",
                (key, policy_mode, approved_template, max_auto_actions, wall_clock_timeout_seconds,
                 same_prompt_repeat_limit, no_progress_limit, now, now),
            )
        return self.get_policy(key)

    def _touch_policy_defaults(self, key: str) -> None:
        # Ensure a row exists before an UPDATE-only bookkeeping write (e.g.
        # incrementing no_progress_count) so that write isn't silently a no-op.
        if self.get_policy(key)["created_at"] is None:
            self.set_policy(key, policy_mode="observe_only", approved_template=None,
                            max_auto_actions=5, wall_clock_timeout_seconds=1800,
                            same_prompt_repeat_limit=2, no_progress_limit=2)

    def record_prompt(self, key: str, prompt_hash: str) -> int:
        """Update same-prompt-repeat bookkeeping; returns the new repeat count."""
        self._touch_policy_defaults(key)
        policy = self.get_policy(key)
        repeat = policy["last_prompt_repeat_count"] + 1 if policy["last_prompt_hash"] == prompt_hash else 1
        now = datetime.now(timezone.utc).isoformat()
        first_action_at = policy["first_action_at"] or now
        with self._connection() as connection:
            connection.execute(
                """UPDATE supervisor_policies SET last_prompt_hash = ?, last_prompt_repeat_count = ?,
                   first_action_at = ?, updated_at = ? WHERE watch_key = ?""",
                (prompt_hash, repeat, first_action_at, now, key),
            )
        return repeat

    def increment_auto_action_count(self, key: str) -> int:
        self._touch_policy_defaults(key)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                "UPDATE supervisor_policies SET auto_action_count = auto_action_count + 1, updated_at = ? WHERE watch_key = ?",
                (now, key),
            )
        return self.get_policy(key)["auto_action_count"]

    def record_progress_check(self, key: str, *, progressed: bool) -> int:
        self._touch_policy_defaults(key)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            if progressed:
                connection.execute(
                    "UPDATE supervisor_policies SET no_progress_count = 0, updated_at = ? WHERE watch_key = ?",
                    (now, key),
                )
            else:
                connection.execute(
                    "UPDATE supervisor_policies SET no_progress_count = no_progress_count + 1, updated_at = ? WHERE watch_key = ?",
                    (now, key),
                )
        return self.get_policy(key)["no_progress_count"]

    def reset_chain(self, key: str) -> None:
        """Called on a completed (DONE-reaching) chain: cumulative counters
        for a *new* piece of work start fresh."""
        self._touch_policy_defaults(key)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """UPDATE supervisor_policies SET auto_action_count = 0, first_action_at = NULL,
                   last_prompt_hash = NULL, last_prompt_repeat_count = 0, no_progress_count = 0,
                   updated_at = ? WHERE watch_key = ?""",
                (now, key),
            )

    def block_policy(self, key: str, reason: str) -> None:
        self._touch_policy_defaults(key)
        now = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                "UPDATE supervisor_policies SET blocked_reason = ?, updated_at = ? WHERE watch_key = ?",
                (reason, now, key),
            )

    # -- actions --------------------------------------------------------

    def open_action_for_watch(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM supervisor_actions WHERE watch_key = ? AND state NOT IN "
                f"({','.join('?' * len(TERMINAL_ACTION_STATES))}) ORDER BY id DESC LIMIT 1",
                (key, *TERMINAL_ACTION_STATES),
            ).fetchone()
        return dict(row) if row is not None else None

    def action_for_event(self, event_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM supervisor_actions WHERE event_id = ? AND state NOT IN "
                f"({','.join('?' * len(TERMINAL_ACTION_STATES))}) ORDER BY id DESC LIMIT 1",
                (event_id, *TERMINAL_ACTION_STATES),
            ).fetchone()
        return dict(row) if row is not None else None

    def any_action_for_event(self, event_id: int) -> bool:
        """Unlike action_for_event, this counts terminal-state actions too —
        an event that already reached completed/blocked/rejected/failed was
        already processed and must never be offered as fresh/actionable
        again, even though it's no longer the "open" action for its watch."""
        with self._connection() as connection:
            row = connection.execute("SELECT 1 FROM supervisor_actions WHERE event_id = ? LIMIT 1", (event_id,)).fetchone()
        return row is not None

    def create_claim(self, *, watch_key: str, event_id: int, claimed_by: str,
                     lease_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        lease = now.timestamp() + lease_seconds
        lease_iso = datetime.fromtimestamp(lease, tz=timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO supervisor_actions
                (watch_key, event_id, state, claimed_by, claimed_at, lease_expires_at,
                 created_at, updated_at)
                VALUES (?, ?, 'claimed', ?, ?, ?, ?, ?)""",
                (watch_key, event_id, claimed_by, now_iso, lease_iso, now_iso, now_iso),
            )
            row = connection.execute("SELECT * FROM supervisor_actions WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return dict(row)

    def get_action(self, action_id: int) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM supervisor_actions WHERE id = ?", (action_id,)).fetchone()
        return dict(row) if row is not None else None

    def list_actions(self, *, watch_key: str | None = None, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        clauses, params = [], []
        if watch_key is not None:
            clauses.append("watch_key = ?")
            params.append(watch_key)
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM supervisor_actions" + where + " ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def cas_update(self, action_id: int, *, expected_state: str, **fields) -> bool:
        """Compare-and-swap: only applies `fields` (plus state, updated_at)
        if the row is currently in `expected_state`. This single primitive
        is what makes claim-expiry, decision, approval, and send each
        exactly-once/idempotent under retry or concurrent callers — a
        losing caller just gets rowcount=0 back, never a double-effect."""
        fields = dict(fields)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE supervisor_actions SET {set_clause} WHERE id = ? AND state = ?",
                (*fields.values(), action_id, expected_state),
            )
        return cursor.rowcount == 1


@dataclass
class SupervisorV2Service:
    v1: SupervisorService
    store: SupervisorV2Store

    @property
    def config(self):
        # SupervisorLoop only ever needs .poll_interval_seconds from this —
        # delegating keeps a single source of truth (v1's config property).
        return self.v1.config

    # -- policy -----------------------------------------------------------

    def set_policy(self, binding: str | None = None, session: str | None = None, *,
                   policy_mode: str = "observe_only", approved_template: str | None = None,
                   max_auto_actions: int = 5, wall_clock_timeout_seconds: int = 1800,
                   same_prompt_repeat_limit: int = 2, no_progress_limit: int = 2) -> dict[str, Any]:
        key, error = self._resolve_watch_key(binding, session, require_watch=True)
        if error:
            return error
        if policy_mode not in POLICY_MODES:
            return {"error": "INVALID_POLICY_MODE", "allowed": list(POLICY_MODES)}
        if policy_mode == "approved_auto_continue" and not (approved_template or "").strip():
            return {"error": "APPROVED_TEMPLATE_REQUIRED"}
        for name, value in (("max_auto_actions", max_auto_actions), ("wall_clock_timeout_seconds", wall_clock_timeout_seconds),
                            ("same_prompt_repeat_limit", same_prompt_repeat_limit), ("no_progress_limit", no_progress_limit)):
            if value < 1:
                return {"error": "INVALID_LIMIT", "field": name}
        return self.store.set_policy(
            key, policy_mode=policy_mode, approved_template=approved_template,
            max_auto_actions=max_auto_actions, wall_clock_timeout_seconds=wall_clock_timeout_seconds,
            same_prompt_repeat_limit=same_prompt_repeat_limit, no_progress_limit=no_progress_limit,
        )

    def get_policy(self, binding: str | None = None, session: str | None = None) -> dict[str, Any]:
        key, error = self._resolve_watch_key(binding, session, require_watch=True)
        if error:
            return error
        return self.store.get_policy(key)

    # -- claim / decide / approve / send -----------------------------------

    def list_actionable_events(self, limit: int = 50) -> dict[str, Any]:
        events = self.v1.store.list_events(unacknowledged_only=False, limit=200)
        actionable = []
        seen_watch_keys: set[str] = set()
        for event in events:
            if event["event_type"] not in ("attention_required", "error_detected"):
                continue
            key = event["watch_key"]
            if key in seen_watch_keys:
                continue  # only the newest event per watch is ever current/actionable
            seen_watch_keys.add(key)
            watch = self.v1.store.get_watch(key)
            if watch is None or watch["state"] != event["state"]:
                continue  # superseded by a later transition — stale, never offered
            policy = self.store.get_policy(key)
            if policy["policy_mode"] == "observe_only" or policy["blocked_reason"]:
                continue
            if self.store.any_action_for_event(event["id"]):
                continue  # already processed (any terminal or open outcome) — never offered twice
            actionable.append({**event, "policy_mode": policy["policy_mode"]})
            if len(actionable) >= limit:
                break
        return {"events": actionable}

    def claim_event(self, event_id: int, claimed_by: str) -> dict[str, Any]:
        if not claimed_by:
            return {"error": "CLAIMED_BY_REQUIRED"}
        event = self._get_event(event_id)
        if event is None:
            return {"error": "EVENT_NOT_FOUND"}
        key = event["watch_key"]
        policy = self.store.get_policy(key)
        if policy["policy_mode"] == "observe_only":
            return {"error": "POLICY_OBSERVE_ONLY"}
        if policy["blocked_reason"]:
            return {"error": "POLICY_BLOCKED", "reason": policy["blocked_reason"]}
        watch = self.v1.store.get_watch(key)
        if watch is None or not watch["enabled"]:
            return {"error": "WATCH_NOT_ACTIVE"}
        if watch["state"] not in ("WAITING_INPUT", "ERROR"):
            # The underlying situation moved on (or was ambiguous/UNKNOWN)
            # since the event fired — never guess, hold for a human instead.
            return {"error": "STATE_NO_LONGER_ACTIONABLE", "current_state": watch["state"]}
        self._expire_stale_claim(key)
        if self.store.open_action_for_watch(key) is not None:
            return {"error": "ACTION_ALREADY_ACTIVE_FOR_WATCH"}
        if self.store.action_for_event(event_id) is not None:
            return {"error": "EVENT_ALREADY_CLAIMED"}
        stop = _stop_pattern_match(event.get("output_preview", ""), event.get("reason", ""))
        if stop:
            self.store.block_policy(key, f"attention_stop_pattern:{stop}")
            return {"error": "BLOCKED_FOR_REVIEW", "reason": stop}
        return self.store.create_claim(watch_key=key, event_id=event_id, claimed_by=claimed_by)

    def submit_decision(self, action_id: int, proposed_prompt: str, decision_reason: str = "") -> dict[str, Any]:
        action = self.store.get_action(action_id)
        if action is None:
            return {"error": "ACTION_NOT_FOUND"}
        if action["state"] != "claimed":
            return {"error": "INVALID_ACTION_STATE", "state": action["state"]}
        if not self._lease_valid(action):
            return {"error": "LEASE_EXPIRED"}
        key = action["watch_key"]
        policy = self.store.get_policy(key)
        watch = self.v1.store.get_watch(key)
        # Fail-safe redaction: a proposed prompt is short, decision-composed
        # text, not raw session output — but if it happens to echo something
        # sensitive from the observed pane, redact_text still strips it
        # before it is ever stored *or sent*, exactly like every other
        # output path in this project.
        prompt = redact_text(proposed_prompt or "")
        stop = _stop_pattern_match(prompt, (watch or {}).get("state", ""))
        if stop is None and watch is not None:
            recent = self.v1.terminal.terminal_status(watch["target"]) if watch["kind"] == "session" \
                else self.v1.terminal.terminal_status_bound(watch["target"])
            stop = _stop_pattern_match(recent.get("last_output", "") if "error" not in recent else "")
        if stop:
            self.store.cas_update(action_id, expected_state="claimed", state="blocked",
                                  stop_reason=f"attention_stop_pattern:{stop}")
            self.store.block_policy(key, f"attention_stop_pattern:{stop}")
            return {"error": "BLOCKED_FOR_REVIEW", "reason": stop}
        prompt_hash = text_fingerprint(prompt)
        repeat_count = self.store.record_prompt(key, prompt_hash)
        if repeat_count > policy["same_prompt_repeat_limit"]:
            self.store.cas_update(action_id, expected_state="claimed", state="blocked",
                                  stop_reason=f"same_prompt_repeated_{repeat_count}x")
            return {"error": "SAME_PROMPT_REPEAT_LIMIT"}
        if policy["auto_action_count"] >= policy["max_auto_actions"]:
            self.store.cas_update(action_id, expected_state="claimed", state="blocked",
                                  stop_reason="max_auto_actions_exceeded")
            return {"error": "MAX_AUTO_ACTIONS_EXCEEDED"}
        if policy["first_action_at"]:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(policy["first_action_at"])).total_seconds()
            if elapsed >= policy["wall_clock_timeout_seconds"]:
                self.store.cas_update(action_id, expected_state="claimed", state="blocked",
                                      stop_reason="wall_clock_timeout_exceeded")
                return {"error": "WALL_CLOCK_TIMEOUT_EXCEEDED"}

        # P0-5: the watch's output_hash *at decision time* -- execute_send
        # re-checks the watch's hash immediately before actually sending
        # and aborts (STALE_DECISION) rather than send if it has since
        # changed materially, instead of acting on stale evidence.
        ok = self.store.cas_update(
            action_id, expected_state="claimed", state="decided",
            proposed_prompt=prompt, prompt_hash=prompt_hash, decision_reason=sanitized_preview(decision_reason or "", 200),
            expected_output_hash=(watch or {}).get("last_output_hash"),
        )
        if not ok:
            return {"error": "INVALID_ACTION_STATE"}

        if policy["policy_mode"] == "approved_auto_continue":
            # The ONE bound enforced here, deliberately simple and strict:
            # the sent text must match the pre-approved template *exactly*.
            # No free-form filling of the template is permitted in this
            # implementation — the smallest, most defensible way to
            # guarantee "cannot escape the approved intent/scope" without a
            # separate template-matching engine.
            if prompt == redact_text(policy["approved_template"] or ""):
                self.store.cas_update(action_id, expected_state="decided", state="approved",
                                      approved_at=datetime.now(timezone.utc).isoformat(), approved_by="policy:approved_auto_continue")
        return self.store.get_action(action_id)

    def review_action(self, action_id: int, decision: str, reason: str = "", approved_by: str = "") -> dict[str, Any]:
        if decision not in ("approve", "reject", "hold"):
            return {"error": "INVALID_DECISION"}
        action = self.store.get_action(action_id)
        if action is None:
            return {"error": "ACTION_NOT_FOUND"}
        if action["state"] != "decided":
            return {"error": "INVALID_ACTION_STATE", "state": action["state"]}
        if decision == "approve":
            ok = self.store.cas_update(action_id, expected_state="decided", state="approved",
                                       approved_at=datetime.now(timezone.utc).isoformat(), approved_by=approved_by or "human")
        else:
            state = "rejected" if decision == "reject" else "blocked"
            ok = self.store.cas_update(action_id, expected_state="decided", state=state,
                                       stop_reason=sanitized_preview(reason or decision, 200))
        if not ok:
            return {"error": "INVALID_ACTION_STATE"}
        return self.store.get_action(action_id)

    def execute_send(self, action_id: int) -> dict[str, Any]:
        # Global v2 kill switch, independent of and in addition to the
        # per-watch policy_mode gate already enforced upstream (claim_event/
        # submit_decision refuse observe_only watches). An operator can
        # disable all v2 sending instance-wide without touching any
        # per-watch policy row.
        if not self.config.v2_enabled:
            return {"error": "V2_DISABLED"}
        action = self.store.get_action(action_id)
        if action is None:
            return {"error": "ACTION_NOT_FOUND"}
        if action["state"] != "approved":
            # Idempotent no-op: already sent (or never got approved) —
            # never a second send for the same action.
            return {"error": "ALREADY_SENT_OR_NOT_APPROVED", "state": action["state"]}
        if not self._lease_valid(action):
            self.store.cas_update(action_id, expected_state="approved", state="blocked", stop_reason="lease_expired")
            return {"error": "LEASE_EXPIRED"}
        watch = self.v1.store.get_watch(action["watch_key"])
        if watch is None:
            self.store.cas_update(action_id, expected_state="approved", state="failed", stop_reason="watch_not_found")
            return {"error": "WATCH_NOT_FOUND"}
        # P0-5 revision CAS: re-read the watch's CURRENT output_hash right
        # here, immediately before sending, and compare against the hash
        # recorded at *decision* time (submit_decision). If the pane has
        # moved on since the decision was made -- a human already
        # answered, the state changed, anything -- the decision is stale
        # and must not be acted on blindly. This is a HOLD requiring
        # re-evaluation, never a blind retry: no failure/iteration counter
        # is touched, only the policy is paused for review.
        if action["expected_output_hash"] is not None and watch["last_output_hash"] != action["expected_output_hash"]:
            self.store.cas_update(action_id, expected_state="approved", state="blocked",
                                  stop_reason="stale_decision")
            self.store.block_policy(action["watch_key"], "stale_decision")
            return {"error": "STALE_DECISION", "reason": "the watch's output changed since this decision was made"}
        # P0-2: for a session-kind watch, re-verify its pinned identity
        # right here too (a binding-kind watch's identity is already
        # re-checked inside terminal_send_bound itself, below).
        if watch["kind"] == "session" and watch["pinned_session_id"]:
            current = self.v1.terminal.resolve_identity(watch["target"])
            pinned = SessionIdentity(name=watch["target"], session_id=watch["pinned_session_id"],
                                     pane_id=watch["pinned_pane_id"] or "",
                                     created_epoch=watch["pinned_created_epoch"] or 0)
            if current is None or not pinned.matches(current):
                self.store.cas_update(action_id, expected_state="approved", state="blocked",
                                      stop_reason="identity_mismatch")
                self.store.block_policy(action["watch_key"], "identity_mismatch")
                return {"error": "IDENTITY_MISMATCH",
                       "reason": "the session this watch was created for no longer matches "
                                 "what currently answers to that name"}
        # Compare-and-swap the state to 'sent' BEFORE calling the guarded
        # send, so a retry/duplicate caller sees state != 'approved' and
        # stops here even if a first attempt's response was lost.
        claimed = self.store.cas_update(action_id, expected_state="approved", state="sent")
        if not claimed:
            return {"error": "ALREADY_SENT_OR_NOT_APPROVED"}
        # P0-4: a durable idempotency key derived from the action's own id
        # -- even if execute_send's own CAS above were somehow bypassed or
        # raced, terminal_send_text/_bound's own idempotency layer refuses
        # to send this exact action twice.
        # action_id alone is not safe as a global idempotency key: it is an
        # AUTOINCREMENT id *scoped to this supervisor db file*, so a fresh/
        # reset supervisor.db (a new install, a restored backup, a test)
        # restarts numbering at 1 while a separate, persistent audit.db
        # could still hold an old claim under that same number. Folding in
        # the action's own created_at timestamp makes the key unique
        # across any such db-generation change, not just within one.
        idempotency_key = f"supervisor2-action-{action_id}-{action['created_at']}"
        if watch["kind"] == "binding":
            result = self.v1.terminal.terminal_send_bound(watch["target"], action["proposed_prompt"],
                                                           press_enter=True, idempotency_key=idempotency_key)
        else:
            result = self.v1.terminal.terminal_send_text(watch["target"], action["proposed_prompt"],
                                                          press_enter=True, idempotency_key=idempotency_key)
        # send_result never carries the raw text (terminal_send_text/_bound
        # never return it — only a character count) — safe to store verbatim.
        if "error" in result:
            if result["error"] == "IDENTITY_MISMATCH":
                # Same HOLD-not-failure treatment as the pre-check above --
                # this is the binding-kind-watch path through
                # terminal_send_bound's own identity check.
                self.store.cas_update(action_id, expected_state="sent", state="blocked",
                                      stop_reason="identity_mismatch", send_result=json.dumps(result))
                self.store.block_policy(action["watch_key"], "identity_mismatch")
                return {"sent": False, **result}
            self.store.cas_update(action_id, expected_state="sent", state="failed",
                                  stop_reason=result["error"], send_result=json.dumps(result))
            return {"sent": False, **result}
        watch = self.v1.store.get_watch(action["watch_key"])
        if result.get("submit_status") == "SUBMIT_UNCONFIRMED":
            # P0-6: the text really was sent (sent=True stays accurate),
            # but Enter's submission could not be confirmed -- never count
            # this as a successful auto-action or let reconciliation
            # advance the chain as if it had progressed. Hold for review,
            # exactly like a content-screening block.
            self.store.cas_update(action_id, expected_state="sent", state="blocked",
                                  stop_reason="submit_unconfirmed", send_result=json.dumps(result))
            self.store.block_policy(action["watch_key"], "submit_unconfirmed")
            return {"sent": True, **result}
        self.store.cas_update(action_id, expected_state="sent", state="observing",
                              send_result=json.dumps(result), output_hash_at_send=watch["last_output_hash"])
        self.store.increment_auto_action_count(action["watch_key"])
        return {"sent": True, **result}

    def list_actions(self, target: str | None = None, state: str | None = None, limit: int = 50) -> dict[str, Any]:
        watch_key = None
        if target is not None:
            watch_key = make_watch_key("session", target)
            if self.v1.store.get_watch(watch_key) is None:
                watch_key = make_watch_key("binding", target)
        return {"actions": self.store.list_actions(watch_key=watch_key, state=state, limit=limit)}

    # -- reconciliation, called after every v1 poll ------------------------

    def run_once(self) -> dict[str, Any]:
        v1_result = self.v1.run_once()
        reconciled = self._reconcile_observing_actions()
        return {**v1_result, "v2_reconciled": reconciled}

    def _reconcile_observing_actions(self) -> list[dict[str, Any]]:
        updates = []
        for action in self.store.list_actions(state="observing", limit=100):
            key = action["watch_key"]
            watch = self.v1.store.get_watch(key)
            if watch is None:
                self.store.cas_update(action["id"], expected_state="observing", state="blocked", stop_reason="watch_removed")
                continue
            progressed = watch["last_output_hash"] != action["output_hash_at_send"]
            no_progress_count = self.store.record_progress_check(key, progressed=progressed)
            if progressed:
                if watch["state"] == "DONE":
                    result = self._advance_completion_candidate(action, watch)
                    updates.append({"action_id": action["id"], "watch_key": key, **result})
                    continue
                events = self.v1.store.list_events(target=watch["target"], limit=1)
                resulting_event_id = events[0]["id"] if events else None
                self.store.cas_update(action["id"], expected_state="observing", state="completed",
                                      resulting_event_id=resulting_event_id)
                updates.append({"action_id": action["id"], "watch_key": key, "result": "progressed", "state": watch["state"]})
            else:
                policy = self.store.get_policy(key)
                if no_progress_count > policy["no_progress_limit"]:
                    self.store.cas_update(action["id"], expected_state="observing", state="blocked",
                                          stop_reason=f"no_progress_limit_exceeded_{no_progress_count}x")
                    self.store.block_policy(key, "no_progress_limit_exceeded")
                    updates.append({"action_id": action["id"], "watch_key": key, "result": "stalled_no_progress"})
        return updates

    def _advance_completion_candidate(self, action: dict[str, Any], watch: dict[str, Any]) -> dict[str, Any]:
        """P0-7/P0-8: the watch reports DONE (from prose/a structured
        marker -- see status.py), but that alone is only a
        COMPLETION_CANDIDATE, never treated as verified. This action stays
        in 'observing' (the chain is NOT reset yet) until the candidate is
        independently corroborated: the pane must stay quiet (output_hash
        unchanged) for COMPLETION_VERIFY_QUIET_SECONDS with no newer ERROR
        in between. New output appearing re-arms the quiet window (a
        legitimately still-active target that merely printed a DONE-
        looking line keeps working) rather than being treated as a
        failure. Only once verified does this transition to 'completed'
        and reset the chain -- the actual enforcement of "do not reset the
        supervisor chain until VERIFIED_DONE"."""
        now = datetime.now(timezone.utc)
        current_hash = watch["last_output_hash"]
        if action["completion_status"] != "completion_candidate" or action["completion_output_hash"] != current_hash:
            # First time DONE was seen for this action, OR the pane moved
            # on since the last candidate snapshot -- (re-)arm the quiet
            # window against the CURRENT snapshot rather than an earlier
            # session-stale one.
            self.store.cas_update(action["id"], expected_state="observing",
                                  completion_status="completion_candidate",
                                  completion_candidate_since=now.isoformat(),
                                  completion_output_hash=current_hash)
            return {"result": "completion_candidate", "state": "DONE"}
        since = datetime.fromisoformat(action["completion_candidate_since"])
        quiet_seconds = (now - since).total_seconds()
        if quiet_seconds < COMPLETION_VERIFY_QUIET_SECONDS:
            return {"result": "completion_candidate_quiet_window", "state": "DONE",
                   "quiet_seconds": quiet_seconds}
        # Corroborated: quiet the whole window, still DONE (not reverted to
        # ERROR/WAITING_INPUT by a later poll) -- promote to verified.
        events = self.v1.store.list_events(target=watch["target"], limit=1)
        resulting_event_id = events[0]["id"] if events else None
        self.store.cas_update(action["id"], expected_state="observing", state="completed",
                              completion_status="verified_done", resulting_event_id=resulting_event_id)
        self.store.reset_chain(action["watch_key"])  # loop stops cleanly at VERIFIED_DONE
        return {"result": "verified_done", "state": "DONE"}

    # -- helpers ------------------------------------------------------------

    def _resolve_watch_key(self, binding: str | None, session: str | None, *, require_watch: bool) -> tuple[str | None, dict[str, Any] | None]:
        if (binding is None) == (session is None):
            return None, {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        key = make_watch_key("binding", binding) if binding is not None else make_watch_key("session", session)
        if require_watch and self.v1.store.get_watch(key) is None:
            return None, {"error": "WATCH_NOT_FOUND", "watch_key": key}
        return key, None

    def _get_event(self, event_id: int) -> dict[str, Any] | None:
        for event in self.v1.store.list_events(limit=500):
            if event["id"] == event_id:
                return event
        return None

    def _lease_valid(self, action: dict[str, Any]) -> bool:
        if not action.get("lease_expires_at"):
            return True
        return datetime.now(timezone.utc) <= datetime.fromisoformat(action["lease_expires_at"])

    def _expire_stale_claim(self, key: str) -> None:
        open_action = self.store.open_action_for_watch(key)
        if open_action is not None and open_action["state"] == "claimed" and not self._lease_valid(open_action):
            self.store.cas_update(open_action["id"], expected_state="claimed", state="blocked", stop_reason="lease_expired")


def build_supervisor_v2(v1: SupervisorService) -> SupervisorV2Service:
    return SupervisorV2Service(v1, SupervisorV2Store(v1.store.path))
