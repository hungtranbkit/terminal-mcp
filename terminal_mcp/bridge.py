"""ask_chatgpt bridge -- Phase A (docs/ask-chatgpt-bridge.md).

Everything in this module is real and tested, but nothing in it talks to a
real browser or ChatGPT: MockBridgeTransport (below) is the only
ChatGptBridgeTransport implementation this project exercises today, so
BridgeService's state machine, capability/idempotency store, permission
gate, and loop protection can be fully proven correct before Phase C/D
ever introduces a browser dependency. Nothing here is wired into core.py/
mcp_app.py yet -- that is Phase B (see the design doc's phase table).

Reuses, deliberately, rather than reinvents:
  - the exact CAS-on-state discipline supervisor2.py's SupervisorV2Store
    already uses (`UPDATE ... WHERE ... AND state = ?`), for the same
    exactly-once-under-retry guarantee;
  - the exact fixed-TTL-no-renewal-thread posture lease.py's
    PaneLeaseStore already uses, for the bridge turn's own expiry;
  - audit.py's text_fingerprint/sanitized_preview (already the mechanism
    every tmux send already goes through) for prompt/response redaction --
    NOT a new redaction mechanism;
  - config.py's max_agent_bridge_depth / AGENT_BRIDGE_DEPTH_EXCEEDED,
    checked in exactly the same place/shape core.py's terminal_send_text
    already checks it (before anything else happens).

See docs/ask-chatgpt-bridge.md for the full design this implements,
section by section (referenced by section number in comments below).
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .audit import sanitized_preview, text_fingerprint
from .config import AppConfig
from .prompt_transport import (
    BridgeResponse,
    BridgeSubmission,
    BridgeTransportError,
    BridgeTurnContext,
    BridgeTurnHandle,
    ChatGptBridgeTransport,
    SubmissionOrigin,
)
from .schema import Migration, apply_migrations

# ---------------------------------------------------------------------------
# State machine (docs/ask-chatgpt-bridge.md §5)
# ---------------------------------------------------------------------------
BRIDGE_PREPARING = "PREPARING"
BRIDGE_COMPOSER_READY = "COMPOSER_READY"
BRIDGE_WRITING = "WRITING"
BRIDGE_VERIFIED = "VERIFIED"
BRIDGE_ACTIVATING = "ACTIVATING"
BRIDGE_ACCEPTED = "ACCEPTED"
BRIDGE_RESPONDING = "RESPONDING"
BRIDGE_COMPLETED = "COMPLETED"
BRIDGE_FAILED = "FAILED"
BRIDGE_UNKNOWN = "UNKNOWN"
BRIDGE_CANCELLED = "CANCELLED"
BRIDGE_STATES = (
    BRIDGE_PREPARING, BRIDGE_COMPOSER_READY, BRIDGE_WRITING, BRIDGE_VERIFIED, BRIDGE_ACTIVATING,
    BRIDGE_ACCEPTED, BRIDGE_RESPONDING, BRIDGE_COMPLETED, BRIDGE_FAILED, BRIDGE_UNKNOWN, BRIDGE_CANCELLED,
)
# Once in one of these, a bridge_turns row never changes state again --
# this is what "non-terminal" means for concurrency counting, cycle
# detection, and the expiry sweep below.
BRIDGE_TERMINAL_STATES = (BRIDGE_COMPLETED, BRIDGE_FAILED, BRIDGE_UNKNOWN, BRIDGE_CANCELLED)

OBSERVE_POLL_INTERVAL_SECONDS = 0.2
QUEUE_POLL_INTERVAL_SECONDS = 0.2

# Never eligible for the tool round-trip allowlist regardless of config --
# see AskChatGptConfig.__post_init__ (config.py) for the load-time half of
# this; check_tool_allowed below is the call-time half. Keeping the
# constant here too (not just importing config's stripped tuple) means
# even a row written by some future, different code path still can't
# grant this at read time.
NEVER_ROUND_TRIP_TOOLS = frozenset({"terminal_send_keys"})


def default_bridge_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_BRIDGE_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "bridge.db"


BRIDGE_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: bridge_turns as of the ask_chatgpt Phase A design", lambda connection: None),
]


class BridgeTurnStore:
    """One row per ask_chatgpt call (docs/ask-chatgpt-bridge.md §6). Same
    connection/schema/permission pattern as every other store in this
    project (audit.py/bindings.py/grants.py/lease.py/supervisor2.py
    lineage): own db file, 0700 dir, 0600 file, WAL, PRAGMA user_version
    migrations."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_bridge_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bridge_turns (
                    bridge_turn_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    source_session TEXT,
                    binding TEXT,
                    trace_id TEXT,
                    parent_turn_id TEXT,
                    depth INTEGER NOT NULL,
                    allowed_tools TEXT NOT NULL,
                    mode TEXT,
                    model TEXT,
                    effort TEXT,
                    state TEXT NOT NULL,
                    activation_attempts INTEGER NOT NULL DEFAULT 0,
                    error_stage TEXT,
                    prompt_sha256 TEXT NOT NULL,
                    prompt_preview TEXT NOT NULL,
                    response_sha256 TEXT,
                    response_preview TEXT,
                    response_length INTEGER,
                    created_at TEXT NOT NULL,
                    prepared_at TEXT,
                    written_at TEXT,
                    verified_at TEXT,
                    activated_at TEXT,
                    accepted_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            apply_migrations(connection, BRIDGE_MIGRATIONS)
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

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["allowed_tools"] = tuple(json.loads(data["allowed_tools"]))
        return data

    def claim(self, *, idempotency_key: str, source_session: str | None, binding: str | None,
              trace_id: str | None, parent_turn_id: str | None, depth: int,
              allowed_tools: tuple[str, ...], mode: str | None, model: str | None, effort: str | None,
              prompt: str, ttl_seconds: float) -> tuple[dict[str, Any], bool]:
        """Idempotent claim (docs/ask-chatgpt-bridge.md §6): one atomic
        `INSERT ... ON CONFLICT(idempotency_key) DO NOTHING`. Returns
        (row, inserted) -- inserted=True means THIS call created the row
        (the caller owns running the transport pipeline for it);
        inserted=False means a row for this idempotency_key already
        existed (a genuine replay, or a race against a concurrent caller
        using the same key) -- the caller must return the existing row's
        receipt and must NEVER touch the transport for it."""
        bridge_turn_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_iso = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                """INSERT INTO bridge_turns
                (bridge_turn_id, idempotency_key, source_session, binding, trace_id, parent_turn_id,
                 depth, allowed_tools, mode, model, effort, state, activation_attempts,
                 prompt_sha256, prompt_preview, created_at, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING""",
                (bridge_turn_id, idempotency_key, source_session, binding, trace_id, parent_turn_id,
                 depth, json.dumps(list(allowed_tools)), mode, model, effort, BRIDGE_PREPARING,
                 text_fingerprint(prompt), sanitized_preview(prompt), now_iso, expires_iso, now_iso),
            )
        inserted = cursor.rowcount == 1
        row = self.get_by_idempotency_key(idempotency_key)
        if row is None:
            # Unreachable in practice (we either just inserted it, or the
            # ON CONFLICT means a row with this idempotency_key already
            # existed) -- an explicit, non-strippable check rather than a
            # bare `assert`, matching this project's own house style of
            # never relying on `assert` for a real invariant.
            raise RuntimeError(f"bridge_turns row vanished immediately after claim() for {idempotency_key!r}")
        return row, inserted

    def get(self, bridge_turn_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bridge_turns WHERE bridge_turn_id = ?", (bridge_turn_id,)
            ).fetchone()
        return self._to_dict(row) if row is not None else None

    def get_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM bridge_turns WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._to_dict(row) if row is not None else None

    def find_non_terminal_by_trace(self, trace_id: str, *, source_session: str | None,
                                   binding: str | None) -> dict[str, Any] | None:
        """Cycle detection (docs/ask-chatgpt-bridge.md §8): a currently
        non-terminal row already bound to the same trace_id AND the same
        source_session/binding identity. Only ever consulted for a
        genuinely NEW claim (the idempotency-key replay check always runs
        first and takes priority -- see BridgeService.ask_chatgpt), so
        this never mistakes a retry of the same call for a cycle."""
        placeholders = ",".join("?" for _ in BRIDGE_TERMINAL_STATES)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT * FROM bridge_turns WHERE trace_id = ? AND state NOT IN ({placeholders}) "
                "AND source_session IS ? AND binding IS ? ORDER BY created_at LIMIT 1",
                (trace_id, *BRIDGE_TERMINAL_STATES, source_session, binding),
            ).fetchone()
        return self._to_dict(row) if row is not None else None

    def count_non_terminal(self) -> int:
        placeholders = ",".join("?" for _ in BRIDGE_TERMINAL_STATES)
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM bridge_turns WHERE state NOT IN ({placeholders})",
                BRIDGE_TERMINAL_STATES,
            ).fetchone()
        return row["n"]

    def list_expired(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        now_iso = (now or datetime.now(timezone.utc)).isoformat()
        placeholders = ",".join("?" for _ in BRIDGE_TERMINAL_STATES)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM bridge_turns WHERE expires_at < ? AND state NOT IN ({placeholders})",
                (now_iso, *BRIDGE_TERMINAL_STATES),
            ).fetchall()
        return [self._to_dict(row) for row in rows]

    def cas_update(self, bridge_turn_id: str, *, expected_state: str, **fields: Any) -> bool:
        """Compare-and-swap -- identical discipline to supervisor2.py's
        SupervisorV2Store.cas_update: only applies `fields` (plus
        updated_at) if the row is currently in `expected_state`. A losing
        caller gets False back, never a double-effect."""
        fields = dict(fields)
        fields["updated_at"] = datetime.now(timezone.utc).isoformat()
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE bridge_turns SET {set_clause} WHERE bridge_turn_id = ? AND state = ?",
                (*fields.values(), bridge_turn_id, expected_state),
            )
        return cursor.rowcount == 1

    def revoke(self, bridge_turn_id: str) -> bool:
        """Idempotent: True the first time (this call set revoked_at),
        False on any later call for the same bridge_turn_id (already
        set) -- mirrors WebTerminalProcess.close()'s own self._closed
        idempotency guard (webterm.py)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE bridge_turns SET revoked_at = ?, updated_at = ? "
                "WHERE bridge_turn_id = ? AND revoked_at IS NULL",
                (now_iso, now_iso, bridge_turn_id),
            )
        return cursor.rowcount == 1


# ---------------------------------------------------------------------------
# MockBridgeTransport -- Phase A/B's only ChatGptBridgeTransport
# implementation. No network, no browser, fully deterministic and
# configured per-instance via `scenario`, so BridgeService's own logic can
# be tested against every §5 outcome on demand.
# ---------------------------------------------------------------------------

SCENARIO_SUCCESS = "success"
SCENARIO_PREPARE_FAILS = "prepare_fails"
SCENARIO_VERIFY_MISMATCH = "verify_mismatch"
SCENARIO_SEND_NEVER_READY = "send_never_ready"
SCENARIO_AMBIGUOUS = "ambiguous"
SCENARIO_SERVER_ERROR = "server_error"
SCENARIO_SLOW = "slow"
SCENARIO_NEVER_COMPLETES = "never_completes"


class MockBridgeTransport:
    """Deterministic, in-process ChatGptBridgeTransport (docs/ask-chatgpt-
    bridge.md §4) -- the Phase A/B stand-in for ChatGptWebTransport
    (prompt_transport.py), which stays an unimplemented stub through this
    phase. `scenario` picks which §5 outcome this instance simulates;
    `responding_ticks` (SCENARIO_SLOW only) is how many RESPONDING polls
    happen before COMPLETED. Tracks call counts so a test can assert
    exactly-once semantics (submit_calls, close_calls, cancel_calls)."""

    def __init__(self, scenario: str = SCENARIO_SUCCESS, *, response_text: str = "mock response",
                 responding_ticks: int = 2) -> None:
        self.scenario = scenario
        self.response_text = response_text
        self.responding_ticks = responding_ticks
        self.submit_calls = 0
        self.close_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self._observe_calls: dict[str, int] = {}

    def prepareTurn(self, context: BridgeTurnContext) -> BridgeTurnHandle:
        if self.scenario == SCENARIO_PREPARE_FAILS:
            raise BridgeTransportError("PREPARE_FAILED", detail="mock composer never appeared")
        return BridgeTurnHandle(bridge_turn_id=context.bridge_turn_id, tab_id="mock-tab-1")

    def submit(self, handle: BridgeTurnHandle, prompt: str, metadata: SubmissionOrigin) -> BridgeSubmission:
        self.submit_calls += 1
        if self.scenario == SCENARIO_VERIFY_MISMATCH:
            raise BridgeTransportError("VERIFY_MISMATCH", detail="read-back did not match sent text")
        if self.scenario == SCENARIO_SEND_NEVER_READY:
            raise BridgeTransportError("SEND_CONTROL_NEVER_ENABLED", detail="send control stayed disabled")
        return BridgeSubmission(bridge_turn_id=handle.bridge_turn_id, prompt=prompt, activated=True)

    def proveAccepted(self, submission: BridgeSubmission) -> bool:
        return self.scenario != SCENARIO_AMBIGUOUS

    def observe(self, submission: BridgeSubmission) -> str:
        if self.scenario == SCENARIO_SERVER_ERROR:
            return BRIDGE_FAILED
        if self.scenario == SCENARIO_NEVER_COMPLETES:
            return BRIDGE_RESPONDING
        if self.scenario == SCENARIO_SLOW:
            seen = self._observe_calls.get(submission.bridge_turn_id, 0)
            self._observe_calls[submission.bridge_turn_id] = seen + 1
            if seen < self.responding_ticks:
                return BRIDGE_RESPONDING
        return BRIDGE_COMPLETED

    def collectResponse(self, submission: BridgeSubmission) -> BridgeResponse:
        return BridgeResponse(text=self.response_text)

    def cancel(self, submission: BridgeSubmission) -> bool:
        self.cancel_calls.append(submission.bridge_turn_id)
        return True

    def close(self, handle: BridgeTurnHandle) -> None:
        self.close_calls.append(handle.bridge_turn_id)


# ---------------------------------------------------------------------------
# BridgeService -- the ask_chatgpt pipeline itself. Not wired into
# TerminalService/mcp_app.py yet (Phase B); called directly by tests today,
# the same way SupervisorService/SessionLifecycleService were before their
# own MCP tools existed.
# ---------------------------------------------------------------------------


class BridgeService:
    def __init__(self, config: AppConfig, store: BridgeTurnStore | None = None,
                 transport: ChatGptBridgeTransport | None = None,
                 deliver_callback: Any = None) -> None:
        self.config = config
        self.store = store or BridgeTurnStore()
        # Defaults to a plain, always-succeeds mock -- a caller (a test)
        # that wants a different scenario constructs its own
        # MockBridgeTransport and passes it in; there is no implicit
        # "real" transport to fall back to in this phase (ChatGptWebTransport
        # still raises NotImplementedError on construction).
        self.transport = transport or MockBridgeTransport()
        # docs/ask-chatgpt-bridge.md §12, delivery mode 2 -- None (the
        # default) means deliver_to always fails closed
        # (DELIVERY_NOT_CONFIGURED) rather than silently dropping the
        # response; Phase B wires this to the real, unmodified
        # terminal_send_text/terminal_send_bound.
        self.deliver_callback = deliver_callback

    # -- the one entry point -------------------------------------------

    def ask_chatgpt(self, *, source_session: str | None = None, binding: str | None = None,
                     prompt: str, trace_id: str | None = None, parent_turn_id: str | None = None,
                     depth: int = 0, mode: str | None = None, model: str | None = None,
                     effort: str | None = None, deliver_to: dict[str, str] | None = None,
                     timeout_seconds: float, idempotency_key: str) -> dict[str, Any]:
        """docs/ask-chatgpt-bridge.md §3's input schema, §5's state
        machine, §6's capability, §8's loop protection -- all in one
        method, gates checked strictly in this order (each one before
        anything the previous gate would make unsafe):
          1. permissions.ask_chatgpt
          2. input validation
          3. max_agent_bridge_depth
          4. idempotent replay (a hit here skips every gate below --
             a retry of an already-refused/already-run call must return
             that SAME outcome, not re-evaluate gates that may have
             changed since)
          5. cycle detection
          6. bounded concurrency (a NEW claim only)
          7. mode/model/effort resolution
          8. claim + run the transport pipeline
        """
        if not self.config.permissions.ask_chatgpt:
            return {"error": "ASK_CHATGPT_DISABLED"}

        if (source_session is None) == (binding is None):
            return {"error": "INVALID_INPUT", "detail": "exactly one of source_session or binding is required"}
        if not prompt:
            return {"error": "INVALID_INPUT", "detail": "prompt is required"}
        if not idempotency_key:
            return {"error": "INVALID_INPUT", "detail": "idempotency_key is required"}
        bounds = self.config.ask_chatgpt
        if not bounds.min_timeout_seconds <= timeout_seconds <= bounds.max_timeout_seconds:
            return {"error": "INVALID_INPUT",
                    "detail": f"timeout_seconds must be between {bounds.min_timeout_seconds} "
                              f"and {bounds.max_timeout_seconds}"}

        if depth > self.config.max_agent_bridge_depth:
            return {"error": "AGENT_BRIDGE_DEPTH_EXCEEDED", "depth": depth,
                    "max_agent_bridge_depth": self.config.max_agent_bridge_depth}

        existing = self.store.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._build_receipt(existing)

        if trace_id and self.store.find_non_terminal_by_trace(
            trace_id, source_session=source_session, binding=binding
        ) is not None:
            return {"error": "CYCLE_DETECTED", "trace_id": trace_id}

        deadline = time.monotonic() + timeout_seconds
        while self.store.count_non_terminal() >= bounds.max_concurrent_turns:
            if time.monotonic() >= deadline:
                return {"error": "QUEUE_TIMEOUT", "max_concurrent_turns": bounds.max_concurrent_turns}
            time.sleep(QUEUE_POLL_INTERVAL_SECONDS)

        resolved_mode, mode_error = self._resolve_field(mode, bounds.default_mode, bounds.allowed_modes, "mode")
        if mode_error:
            return mode_error
        resolved_model, model_error = self._resolve_field(model, bounds.default_model, bounds.allowed_models, "model")
        if model_error:
            return model_error
        resolved_effort, effort_error = self._resolve_field(effort, bounds.default_effort, bounds.allowed_efforts, "effort")
        if effort_error:
            return effort_error

        allowed_tools = tuple(t for t in bounds.round_trip_allowed_tools if t not in NEVER_ROUND_TRIP_TOOLS)
        row, inserted = self.store.claim(
            idempotency_key=idempotency_key, source_session=source_session, binding=binding,
            trace_id=trace_id, parent_turn_id=parent_turn_id, depth=depth, allowed_tools=allowed_tools,
            mode=resolved_mode, model=resolved_model, effort=resolved_effort, prompt=prompt,
            ttl_seconds=bounds.bridge_turn_ttl_seconds,
        )
        if not inserted:
            # Lost a race against a concurrent caller using the same
            # idempotency_key -- their claim won, this call must not touch
            # the transport at all; return whatever state their pipeline
            # has reached (or will reach).
            return self._build_receipt(row)

        return self._run_pipeline(row, prompt=prompt, trace_id=trace_id, depth=depth,
                                  deliver_to=deliver_to, deadline=deadline)

    @staticmethod
    def _resolve_field(requested: str | None, default: str | None, allowed: tuple[str, ...],
                       field_name: str) -> tuple[str | None, dict[str, Any] | None]:
        """Explicit-or-configured-default, never inferred (docs/ask-
        chatgpt-bridge.md §2/§3) -- an explicitly requested value outside
        a non-empty allowlist is a named failure, never a silent
        substitution."""
        if requested is None:
            return default, None
        if allowed and requested not in allowed:
            return None, {"error": f"{field_name.upper()}_NOT_AVAILABLE", field_name: requested,
                          "allowed": list(allowed)}
        return requested, None

    # -- pipeline (only the winning claim() caller ever runs this) -----

    def _run_pipeline(self, row: dict[str, Any], *, prompt: str, trace_id: str | None, depth: int,
                      deliver_to: dict[str, str] | None, deadline: float) -> dict[str, Any]:
        bridge_turn_id = row["bridge_turn_id"]
        handle: BridgeTurnHandle | None = None
        try:
            context = BridgeTurnContext(bridge_turn_id=bridge_turn_id, source_session=row["source_session"],
                                        binding=row["binding"], mode=row["mode"], model=row["model"],
                                        effort=row["effort"])
            try:
                handle = self.transport.prepareTurn(context)
            except BridgeTransportError as exc:
                return self._finish(bridge_turn_id, terminal_state=BRIDGE_FAILED,
                                    expected_state=BRIDGE_PREPARING, error_stage=exc.reason)
            self.store.cas_update(bridge_turn_id, expected_state=BRIDGE_PREPARING,
                                  state=BRIDGE_COMPOSER_READY, prepared_at=_now_iso())
            self.store.cas_update(bridge_turn_id, expected_state=BRIDGE_COMPOSER_READY,
                                  state=BRIDGE_WRITING, written_at=_now_iso())

            metadata = SubmissionOrigin(origin="ask_chatgpt", trace_id=trace_id,
                                        parent_turn_id=row["parent_turn_id"], depth=depth)
            try:
                submission = self.transport.submit(handle, prompt, metadata)
            except BridgeTransportError as exc:
                return self._finish(bridge_turn_id, terminal_state=BRIDGE_FAILED,
                                    expected_state=BRIDGE_WRITING, error_stage=exc.reason)

            self.store.cas_update(bridge_turn_id, expected_state=BRIDGE_WRITING,
                                  state=BRIDGE_VERIFIED, verified_at=_now_iso())
            self.store.cas_update(bridge_turn_id, expected_state=BRIDGE_VERIFIED,
                                  state=BRIDGE_ACTIVATING, activation_attempts=1, activated_at=_now_iso())

            # Golden rule (docs/ask-chatgpt-bridge.md §5): once activation
            # has been attempted, an unconfirmed outcome is UNKNOWN --
            # terminal, never silently retried under this idempotency_key.
            if not self.transport.proveAccepted(submission):
                return self._finish(bridge_turn_id, terminal_state=BRIDGE_UNKNOWN,
                                    expected_state=BRIDGE_ACTIVATING, error_stage="ACTIVATION_UNCONFIRMED")

            self.store.cas_update(bridge_turn_id, expected_state=BRIDGE_ACTIVATING,
                                  state=BRIDGE_ACCEPTED, accepted_at=_now_iso())
            self.store.cas_update(bridge_turn_id, expected_state=BRIDGE_ACCEPTED, state=BRIDGE_RESPONDING)

            while True:
                status = self.transport.observe(submission)
                if status == BRIDGE_COMPLETED:
                    break
                if status == BRIDGE_FAILED:
                    return self._finish(bridge_turn_id, terminal_state=BRIDGE_FAILED,
                                        expected_state=BRIDGE_RESPONDING, error_stage="RESPONSE_FAILED")
                if time.monotonic() >= deadline:
                    # Ambiguous, not proven-failed -- see BridgeTransportError's
                    # own docstring on the FAILED/UNKNOWN distinction.
                    return self._finish(bridge_turn_id, terminal_state=BRIDGE_UNKNOWN,
                                        expected_state=BRIDGE_RESPONDING, error_stage="RESPONSE_TIMEOUT")
                time.sleep(OBSERVE_POLL_INTERVAL_SECONDS)

            response = self.transport.collectResponse(submission)
            self.store.cas_update(
                bridge_turn_id, expected_state=BRIDGE_RESPONDING, state=BRIDGE_COMPLETED,
                completed_at=_now_iso(), response_sha256=text_fingerprint(response.text),
                response_preview=sanitized_preview(response.text), response_length=len(response.text),
            )
            final_row = self.store.get(bridge_turn_id)
            receipt = self._build_receipt(final_row)
            # response_text is attached to THIS call's own in-memory return
            # value only -- never written to the store (only the sha256/
            # preview/length are persisted, docs/ask-chatgpt-bridge.md §9).
            receipt["response_text"] = response.text
            if deliver_to is not None:
                receipt["delivery"] = self._deliver(final_row, response.text, deliver_to,
                                                    trace_id=trace_id, depth=depth)
            return receipt
        finally:
            if handle is not None:
                with contextlib.suppress(Exception):
                    self.transport.close(handle)
            self.store.revoke(bridge_turn_id)

    def _finish(self, bridge_turn_id: str, *, terminal_state: str, expected_state: str,
               error_stage: str) -> dict[str, Any]:
        self.store.cas_update(bridge_turn_id, expected_state=expected_state,
                              state=terminal_state, error_stage=error_stage)
        return self._build_receipt(self.store.get(bridge_turn_id))

    def _deliver(self, row: dict[str, Any], response_text: str, deliver_to: dict[str, str], *,
                trace_id: str | None, depth: int) -> dict[str, Any]:
        """docs/ask-chatgpt-bridge.md §12, delivery mode 2 -- re-entry into
        a tmux session via the caller-injected `deliver_callback`
        (Phase B wires this to the real, unmodified terminal_send_text/
        terminal_send_bound; Phase A has no such callback by default, so
        this fails closed with a named reason rather than silently
        dropping the response -- the response text itself is never lost,
        it is already in this call's own return value regardless)."""
        if self.deliver_callback is None:
            return {"error": "DELIVERY_NOT_CONFIGURED"}
        origin = SubmissionOrigin(origin="ask_chatgpt", trace_id=trace_id,
                                  parent_turn_id=row["bridge_turn_id"], depth=depth)
        child = origin.child(origin="ask_chatgpt", turn_id=row["bridge_turn_id"])
        return self.deliver_callback(deliver_to, response_text, child)

    # -- capability lifecycle (docs/ask-chatgpt-bridge.md §6) -----------

    def sweep_expired(self) -> list[str]:
        """Transitions any non-terminal row past its expires_at to
        CANCELLED (reason=CAPABILITY_EXPIRED) and revokes it. In this
        phase ask_chatgpt() is fully synchronous end-to-end, so a row can
        only be found here after a crash mid-pipeline (or direct test
        manipulation) -- not a background thread, called explicitly (a
        future maintenance.py hook, or a test)."""
        swept = []
        for row in self.store.list_expired():
            ok = self.store.cas_update(row["bridge_turn_id"], expected_state=row["state"],
                                       state=BRIDGE_CANCELLED, error_stage="CAPABILITY_EXPIRED")
            if ok:
                self.store.revoke(row["bridge_turn_id"])
                swept.append(row["bridge_turn_id"])
        return swept

    def cancel_turn(self, bridge_turn_id: str) -> dict[str, Any]:
        """Explicit abort (docs/ask-chatgpt-bridge.md §6's "revoke on
        completion/abort/timeout") -- idempotent: a turn already terminal
        returns its current state unchanged, never an error, and never
        calls transport.cancel()/revoke() a second time."""
        row = self.store.get(bridge_turn_id)
        if row is None:
            return {"error": "BRIDGE_TURN_NOT_FOUND"}
        if row["state"] in BRIDGE_TERMINAL_STATES:
            return self._build_receipt(row)
        with contextlib.suppress(Exception):
            self.transport.cancel(BridgeSubmission(bridge_turn_id=bridge_turn_id, prompt=""))
        self.store.cas_update(bridge_turn_id, expected_state=row["state"], state=BRIDGE_CANCELLED)
        self.store.revoke(bridge_turn_id)
        return self._build_receipt(self.store.get(bridge_turn_id))

    def check_tool_allowed(self, bridge_turn_id: str, tool_name: str) -> bool:
        """docs/ask-chatgpt-bridge.md §7's round-trip allowlist check --
        frozen onto the row at claim time, never widened later. Phase A
        exposes this so the check itself is testable; nothing calls it
        yet (no round-trip caller exists before Phase E)."""
        if tool_name in NEVER_ROUND_TRIP_TOOLS:
            return False
        row = self.store.get(bridge_turn_id)
        if row is None:
            return False
        return tool_name in row["allowed_tools"]

    def get_turn(self, bridge_turn_id: str, *, source_session: str | None = None,
                binding: str | None = None) -> dict[str, Any]:
        """Ownership-checked read (docs/ask-chatgpt-bridge.md §6) -- a
        caller presenting a bridge_turn_id that does not match the exact
        session/binding identity the turn was claimed under gets
        FORBIDDEN, never the turn's data (receipt, response preview,
        anything), regardless of whether the turn is still in flight or
        long completed. `source_session`/`binding` here must be the
        CALLER's own resolved identity, never client-supplied claims
        about who they are -- the same posture core.py's
        `_read_authorized_with_grant` already applies to tmux panes,
        applied to a bridge turn instead."""
        row = self.store.get(bridge_turn_id)
        if row is None:
            return {"error": "BRIDGE_TURN_NOT_FOUND"}
        if row["source_session"] != source_session or row["binding"] != binding:
            return {"error": "FORBIDDEN"}
        return self._build_receipt(row)

    # -- receipt (docs/ask-chatgpt-bridge.md §9) -------------------------

    @staticmethod
    def _build_receipt(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "bridge_turn_id": row["bridge_turn_id"],
            "idempotency_key": row["idempotency_key"],
            "trace_id": row["trace_id"],
            "source_session": row["source_session"],
            "binding": row["binding"],
            "state": row["state"],
            "mode": row["mode"],
            "model": row["model"],
            "effort": row["effort"],
            "acceptance_evidence": row["accepted_at"] is not None,
            "activation_attempts": row["activation_attempts"],
            "created_at": row["created_at"],
            "prepared_at": row["prepared_at"],
            "written_at": row["written_at"],
            "verified_at": row["verified_at"],
            "activated_at": row["activated_at"],
            "accepted_at": row["accepted_at"],
            "completed_at": row["completed_at"],
            "response_length": row["response_length"],
            "response_sha256": row["response_sha256"],
            "error_stage": row["error_stage"],
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
