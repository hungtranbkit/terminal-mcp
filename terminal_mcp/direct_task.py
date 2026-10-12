"""Supervised direct-session tasks: an agent turn ending is not the task ending.

WHY THIS EXISTS (2026-09-30 hotfix). With the durable queue retired, long
coding work is sent straight to a session (`send` / `send_wait`). Every time a
shell command or an agent turn finishes, the pane returns to its prompt or
composer and the status classifier correctly reports IDLE -- but IDLE was the
only completion signal a direct caller had, so "the agent stopped typing" was
read as "the business task is done" and long tasks stalled until a human
re-sent them.

A supervised task separates the two:

* ``turn_state``  -- what the pane is doing (RUNNING / IDLE / WAITING_INPUT),
* ``task state``  -- RUNNING until an EXPLICIT outcome: DONE, BLOCKED,
  WAITING_INPUT, CANCELLED, or FAILED (continuation limit / target lost).

When the pane settles IDLE and no explicit outcome has been seen, the
supervisor dispatches the next queued step or a bounded continuation prompt.
It never spins: continuations are capped, dispatches need a settled IDLE, and
send failures are capped too.

Explicit completion is either a client call (``supervise_complete``) or a
marker line printed by the target: ``TMCP-DONE:<task_id>`` or
``TMCP-BLOCKED:<task_id> <reason>``. The instruction text sent to an agent
deliberately never contains the contiguous marker, so a prompt echoed in the
pane can never complete its own task.

This is direct-session orchestration only. It creates no queue rows and does
not depend on the retired queue. Prompts are stored here (they must survive a
restart to be dispatched) in their own database, never in the content-free
run journal.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .redaction import redact_text

_log = logging.getLogger(__name__)

#: Not finished. DISPATCHING = persisted and (being) sent, but no independent
#: observation yet proves the agent received it; RUNNING only after one does.
ACTIVE_STATES = ("DISPATCHING", "RUNNING", "WAITING_INPUT", "STUCK_COMPOSER",
                 "DISPATCH_UNCERTAIN")
TERMINAL_STATES = ("DONE", "BLOCKED", "CANCELLED", "FAILED")
#: Dispatch receipt ladder (issue #52). Each rung is separate evidence;
#: SUBMIT_CONFIRMED (Enter + adapter ack) is NOT execution.
RECEIPT_PERSISTED = "PERSISTED"
RECEIPT_INPUT_SENT = "INPUT_SENT"          # persisted BEFORE the send call
RECEIPT_SUBMIT_CONFIRMED = "SUBMIT_CONFIRMED"
RECEIPT_EXECUTION_ACCEPTED = "EXECUTION_ACCEPTED"
RECEIPT_NOT_SENT = "NOT_SENT"              # guard refused before typing
RECEIPT_UNCERTAIN = "DISPATCH_UNCERTAIN"   # typed, outcome unknown
DEFAULT_MAX_CONTINUATIONS = 6
MAX_CONTINUATIONS_CAP = 30
MAX_STEPS = 20
MAX_TEXT_CHARS = 8_000
MAX_REASON_CHARS = 500
#: IDLE must be observed continuously this long, starting no earlier than the
#: last dispatch, before the next dispatch. Guards the gap between a submit and
#: the agent's first redraw, and brief idle frames between tool calls.
DEFAULT_SETTLE_SECONDS = 8.0
MAX_SEND_FAILURES = 3
MAX_OBSERVE_FAILURES = 20
#: A node/tunnel outage is not the task failing: keep observing (with
#: backoff) this long before giving up. A missing session fails faster.
NODE_UNREACHABLE_FAIL_SECONDS = 1_800.0
MAX_OBSERVE_BACKOFF_SECONDS = 60.0
#: Composer still holding the prompt this long after the send -> STUCK.
STUCK_GRACE_SECONDS = 6.0
#: Enter-only recoveries per dispatch (never a text resend), and their spacing.
MAX_RECOVERY_ATTEMPTS = 2
RECOVERY_COOLDOWN_SECONDS = 10.0
#: No independent execution evidence this long after a submit -> UNCERTAIN.
ACCEPT_TIMEOUT_SECONDS = 45.0
#: A finished-by-limit task whose marker shows up within this window is
#: upgraded to the explicit outcome (late completion).
LATE_COMPLETION_WINDOW_SECONDS = 86_400.0
MARKER_TAIL_LINES = 60
LOOP_INTERVAL_SECONDS = 3.0
TASK_ID_PLACEHOLDER = "{TMCP_TASK_ID}"
_TASK_ID = re.compile(r"^dt_[0-9a-f]{16}$")
# Leading decoration an agent UI may put before its own output line (Claude's
# ●/⏺, Codex's •, list bullets, box-drawing borders). `>` is deliberately NOT
# stripped: it is the prompt-echo prefix in agent composers.
_MARKER_PREFIX = r"^[ \t●⏺•◦·*\-–—│|]*"
_AGENT_HINTS = ("claude", "codex")
_COMMAND_IN_REASON = re.compile(r"current command is '([^']+)'")
_COMPOSER_MARKER = re.compile(r"^[│┃║]?\s*[❯›»>](?:\s|$)")
_PASTE_PLACEHOLDER = re.compile(r"\[Pasted (?:text #\d+|Content \d+ chars)", re.IGNORECASE)
# An agent's own output line after the submitted prompt: Claude ●/⏺,
# Codex • or its "─ Worked for" separator.
_AGENT_OUTPUT_LINE = re.compile(r"^\s*(?:[●⏺•]\s+\S|─+\s*Worked for\b)")
_BOOT_DIALOG = re.compile(r"hooks need review|trust this folder|do you trust the files",
                          re.IGNORECASE)
_DEFINITELY_NOT_TYPED = {"BLOCKED", "ERROR", None}
# classify_status's timer fallback ("current command is 'claude'; tmux
# activity age is 3s") -- weak evidence, see _verify_acceptance.
_WEAK_RUNNING = re.compile(r"tmux activity age is \d+s")
_NODE_ERRORS = ("NODE", "REMOTE", "UNREACHABLE", "OFFLINE", "TUNNEL", "CONNECT", "TIMEOUT")


def default_direct_task_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "direct_tasks.db"


def _now() -> float:
    return time.time()


def _clip(value: Any, limit: int = MAX_REASON_CHARS) -> str | None:
    if value is None:
        return None
    return redact_text(str(value))[:limit]


def completion_instruction(task_id: str) -> str:
    # Never write the contiguous marker here: this text is echoed in the pane.
    return (
        f"[Terminal MCP supervised task {task_id}] Work autonomously until the WHOLE task "
        "is complete; ending a turn does not end the task and you will be asked to "
        "continue. Only when every completion criterion is met and verified, print a "
        "final line made of the word TMCP-DONE, then a colon, then the task id "
        f"{task_id}. If you truly cannot proceed without a human, print TMCP-BLOCKED, a "
        "colon, the task id, a space and the reason instead."
    )


def continuation_prompt(task_id: str, number: int, limit: int) -> str:
    return (
        f"[Terminal MCP supervised task {task_id}, continuation {number}/{limit}] Your "
        "turn ended but the task is not marked complete. Continue working toward the "
        "original goal and its completion criteria; do not repeat finished work. When "
        "everything is verified print TMCP-DONE, a colon and the task id; if blocked "
        "print TMCP-BLOCKED, a colon, the task id, a space and the reason."
    )


def find_marker(output: str, task_id: str) -> tuple[str, str] | None:
    """Latest explicit outcome marker for ``task_id`` in ``output``."""
    pattern = re.compile(
        _MARKER_PREFIX + r"TMCP-(DONE|BLOCKED):" + re.escape(task_id)
        + r"(?![0-9A-Za-z_])[ \t:\-–—]*(.*)$", re.MULTILINE)
    found = None
    for match in pattern.finditer(output or ""):
        found = (match.group(1), match.group(2).strip())
    return found


def _collapse(text: str) -> str:
    return " ".join(str(text or "").split())


def _composer_region(lines: list[str]) -> list[str] | None:
    """Rows from the LAST composer marker to the pane bottom, or None.

    The composer is always the bottom-most marker row; a prompt echoed into
    the transcript sits above it. A long draft wraps below the marker row,
    so the whole region (not just the marker row) is the draft.
    """
    for index in range(len(lines) - 1, -1, -1):
        if _COMPOSER_MARKER.match(lines[index]):
            region = lines[index:]
            # Agent output below the marker row means this is a prompt echo
            # in the transcript, not a live composer.
            if any(_AGENT_OUTPUT_LINE.match(line) for line in region[1:]):
                return None
            return region
    return None


def composer_holds(output: str, needle: str) -> bool:
    """True when the pane's composer still holds the dispatched prompt."""
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    region = _composer_region(lines)
    if region is None:
        return False
    joined = _collapse(" ".join(line.strip(" │┃║") for line in region))
    if needle and _collapse(needle) in joined:
        return True
    return bool(_PASTE_PLACEHOLDER.search(joined))


def echoed_then_answered(output: str, needle: str) -> bool:
    """The prompt was echoed into the transcript AND the agent wrote output
    after it -- independent evidence the agent took the prompt."""
    lines = [line for line in str(output or "").splitlines() if line.strip()]
    region = _composer_region(lines)
    history = lines[:len(lines) - len(region)] if region else lines
    wanted = _collapse(needle)
    last = None
    for index, line in enumerate(history):
        if wanted and wanted in _collapse(line):
            last = index
    if last is None:
        return False
    return any(_AGENT_OUTPUT_LINE.match(line) for line in history[last + 1:])


def agent_of(observed: dict[str, Any]) -> str | None:
    """Which agent CLI runs in the pane, from structured fields first."""
    resource = observed.get("resource")
    if isinstance(resource, dict) and str(resource.get("agent") or "").lower() in _AGENT_HINTS:
        return str(resource["agent"]).lower()
    for key in ("agent", "agent_type", "pane_current_command", "current_command"):
        value = str(observed.get(key) or "").lower()
        if value in _AGENT_HINTS:
            return value
    reason = str(observed.get("reason") or "")
    match = _COMMAND_IN_REASON.search(reason)
    if match:
        command = match.group(1).lower()
        return command if command in _AGENT_HINTS else "shell"
    lowered = reason.lower()
    for agent in _AGENT_HINTS:
        if agent in lowered:
            return agent
    return None


def _error_hop(observed: dict[str, Any]) -> str:
    code = str(observed.get("error") or "").upper()
    if code == "SESSION_NOT_FOUND" or observed.get("exists") is False:
        return "session"
    if code == "STATUS_PROBE_TIMEOUT":
        return "controller->node"
    if any(word in code for word in _NODE_ERRORS):
        return "node"
    return "controller"


class DirectTaskStore:
    """SQLite/WAL persistence so supervision survives a controller restart."""

    #: Columns added for issue #52, migrated in place on open. Each ALTER is
    #: its own statement; a crash part-way just leaves the rest for next time.
    _ADDED_COLUMNS = (
        ("receipt", "TEXT"), ("dispatch_key", "TEXT"), ("dispatch_needle", "TEXT"),
        ("pending_advance", "TEXT"), ("input_sent_at", "REAL"),
        ("submit_confirmed_at", "REAL"), ("accepted_at", "REAL"),
        ("recovery_attempts", "INTEGER NOT NULL DEFAULT 0"), ("last_recovery_at", "REAL"),
        ("agent", "TEXT"), ("submission_id", "TEXT"), ("last_error", "TEXT"),
        ("last_error_hop", "TEXT"), ("unreachable_since", "REAL"), ("next_observe_at", "REAL"),
        ("late_completion", "INTEGER NOT NULL DEFAULT 0"),
    )

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_direct_task_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS direct_tasks (
                    task_id TEXT PRIMARY KEY,
                    target TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    state TEXT NOT NULL,
                    wait_reason TEXT,
                    steps TEXT NOT NULL,
                    next_step INTEGER NOT NULL DEFAULT 0,
                    continue_text TEXT,
                    continuations INTEGER NOT NULL DEFAULT 0,
                    max_continuations INTEGER NOT NULL,
                    dispatches INTEGER NOT NULL DEFAULT 0,
                    send_failures INTEGER NOT NULL DEFAULT 0,
                    observe_failures INTEGER NOT NULL DEFAULT 0,
                    last_dispatch_at REAL,
                    idle_since REAL,
                    last_turn_state TEXT,
                    reason TEXT,
                    idempotency_key TEXT UNIQUE,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_direct_tasks_active "
                "ON direct_tasks(completed_at, target)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS direct_task_meta (key TEXT PRIMARY KEY, value TEXT)")
            existing = {row["name"] for row in connection.execute(
                "PRAGMA table_info(direct_tasks)").fetchall()}
            for name, kind in self._ADDED_COLUMNS:
                if name not in existing:
                    connection.execute(f"ALTER TABLE direct_tasks ADD COLUMN {name} {kind}")
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["steps"] = json.loads(result["steps"])
        return result

    def insert(self, task: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if task.get("idempotency_key"):
                existing = connection.execute(
                    "SELECT * FROM direct_tasks WHERE idempotency_key = ?",
                    (task["idempotency_key"],)).fetchone()
                if existing is not None:
                    return {**self._row(existing), "deduplicated": True}
            values = {**task, "steps": json.dumps(task["steps"])}
            columns = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            connection.execute(f"INSERT INTO direct_tasks ({columns}) VALUES ({marks})",
                               tuple(values.values()))
            row = connection.execute("SELECT * FROM direct_tasks WHERE task_id = ?",
                                     (task["task_id"],)).fetchone()
        return self._row(row)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._row(connection.execute(
                "SELECT * FROM direct_tasks WHERE task_id = ?", (task_id,)).fetchone())

    def by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._row(connection.execute(
                "SELECT * FROM direct_tasks WHERE idempotency_key = ?", (key,)).fetchone())

    def update(self, task_id: str, *, expect_active: bool = True, **fields: Any) -> dict[str, Any] | None:
        """Update one task; with ``expect_active`` a finished task is left untouched."""
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        guard = " AND completed_at IS NULL" if expect_active else ""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"UPDATE direct_tasks SET {assignments} WHERE task_id = ?{guard}",
                               (*fields.values(), task_id))
            return self._row(connection.execute(
                "SELECT * FROM direct_tasks WHERE task_id = ?", (task_id,)).fetchone())

    def active(self, target: str | None = None) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if target is None:
                rows = connection.execute(
                    "SELECT * FROM direct_tasks WHERE completed_at IS NULL ORDER BY created_at").fetchall()
            else:
                bare = target.split("/", 1)[-1]
                rows = connection.execute(
                    "SELECT * FROM direct_tasks WHERE completed_at IS NULL AND target IN (?, ?) "
                    "ORDER BY created_at", (target, bare)).fetchall()
        return [self._row(row) for row in rows]

    def set_meta(self, key: str, value: str) -> None:
        with self._connection() as connection:
            connection.execute("INSERT INTO direct_task_meta (key, value) VALUES (?, ?) "
                               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def get_meta(self, key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT value FROM direct_task_meta WHERE key = ?",
                                     (key,)).fetchone()
        return None if row is None else row["value"]


_NEXT_ACTIONS = {
    "DISPATCHING": ("none -- the server is verifying that the agent actually received the "
                    "prompt; check supervise_status(task_id) when asked. Do not resend."),
    "RUNNING": ("none -- execution was observed; the server continues the task until an "
                "explicit TMCP-DONE/TMCP-BLOCKED. Check supervise_status(task_id) when asked."),
    "WAITING_INPUT": ("a human must answer the session's prompt/dialog (inspect the target; "
                      "never auto-trust a hooks review). Supervision resumes by itself; do not "
                      "resend."),
    "STUCK_COMPOSER": ("the prompt is sitting unsent in the agent composer; the server tries a "
                       "bounded Enter-only recovery. If it persists, press Enter in the session "
                       "or supervise_cancel. Never resend the text."),
    "DISPATCH_UNCERTAIN": ("no independent evidence the agent received the prompt. Inspect the "
                           "target; if it really was not received, supervise_cancel and "
                           "supervise again with a NEW idempotency_key. Do not resend blindly."),
    "DONE": "none -- explicitly completed",
    "BLOCKED": "read `reason`; a human decision is needed",
    "CANCELLED": "none",
    "FAILED": "read `reason`",
}


class DirectTaskSupervisor:
    """Drives supervised tasks. All I/O goes through injected, guarded callables.

    ``send(target, text, idempotency_key)`` -> dict with ``status``
    (``SUBMIT_CONFIRMED`` on success) and ``evidence.delivery_state``;
    ``status(target)`` -> dict with ``state``/``reason`` or ``error``;
    ``tail(target, lines)`` -> dict with ``output``; optional
    ``activate(target, idempotency_key)`` presses Enter on the composer's own
    content (guarded, never retypes text) for a stuck-composer recovery. In
    production these are CompactTerminalTools' own guarded send/status/tail,
    so authorization is re-checked on every dispatch.

    Issue #52 contract: persist before send; a submit receipt is never
    RUNNING -- only an independent observation (busy footer, an answer after
    the echoed prompt, or the marker) is; an unconfirmed outcome is verified by
    observation, never by a blind resend.
    """

    def __init__(self, store: DirectTaskStore, *, send: Callable[[str, str, str], dict],
                 status: Callable[[str], dict], tail: Callable[[str, int], dict],
                 activate: Callable[[str, str], dict] | None = None,
                 clock: Callable[[], float] = _now,
                 settle_seconds: float = DEFAULT_SETTLE_SECONDS,
                 accept_wait_seconds: float = 0.0,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.store = store
        self._send = send
        self._status = status
        self._tail = tail
        self._activate = activate
        self.clock = clock
        self.settle_seconds = float(settle_seconds)
        self.accept_wait_seconds = float(accept_wait_seconds)
        self._sleep = sleep
        self._lock = threading.Lock()

    # -- public API ----------------------------------------------------------

    def start(self, target: str, *, text: str | None = None, steps: list[str] | None = None,
              max_continuations: int | None = None, continue_text: str | None = None,
              mode: str = "auto", idempotency_key: str | None = None) -> dict[str, Any]:
        items = [item for item in ([text] if text else []) + list(steps or [])]
        if not items:
            return {"status": "FAILED", "error": "TEXT_OR_STEPS_REQUIRED"}
        if len(items) > MAX_STEPS or any(
                not isinstance(item, str) or not item.strip() or len(item) > MAX_TEXT_CHARS
                for item in items):
            return {"status": "FAILED", "error": "INVALID_STEPS",
                    "limits": {"max_steps": MAX_STEPS, "max_chars": MAX_TEXT_CHARS}}
        if max_continuations is None:
            max_continuations = DEFAULT_MAX_CONTINUATIONS
        if (isinstance(max_continuations, bool) or not isinstance(max_continuations, int)
                or not 0 <= max_continuations <= MAX_CONTINUATIONS_CAP):
            return {"status": "FAILED", "error": "INVALID_MAX_CONTINUATIONS",
                    "allowed": f"0..{MAX_CONTINUATIONS_CAP}"}
        if continue_text is not None and (not isinstance(continue_text, str)
                                          or not continue_text.strip()
                                          or len(continue_text) > MAX_TEXT_CHARS):
            return {"status": "FAILED", "error": "INVALID_CONTINUE_TEXT"}
        if mode not in {"auto", "agent", "shell"}:
            return {"status": "FAILED", "error": "INVALID_MODE", "allowed": ["auto", "agent", "shell"]}
        if idempotency_key:
            # Idempotency first: a retried supervise (lost response, new
            # client, sidecar timeout) returns the SAME task, whatever state
            # it reached, and never sends anything again.
            existing = self.store.by_idempotency_key(idempotency_key)
            if existing is not None:
                return {"status": "OK", "deduplicated": True, "task": self._view(existing)}
        active = self.store.active(target)
        if active:
            return {"status": "FAILED", "error": "TARGET_HAS_ACTIVE_SUPERVISED_TASK",
                    "task": self._view(active[0]),
                    "next_action": "complete or cancel that task first (supervise_complete / supervise_cancel)"}
        task_id = f"dt_{uuid.uuid4().hex[:16]}"
        # Lets a step reference its own task, e.g. a final shell step
        # `printf 'TMCP-%s:%s\n' DONE {TMCP_TASK_ID}` completes explicitly.
        items = [item.replace(TASK_ID_PLACEHOLDER, task_id) for item in items]
        now = self.clock()
        # PERSIST FIRST: the task and its stable id exist before anything is
        # observed or sent, so a crash, a timeout or a lost response can never
        # lose it, and the caller always gets a task_id back.
        task = self.store.insert({
            "task_id": task_id, "target": target, "mode": mode, "state": "DISPATCHING",
            "receipt": RECEIPT_PERSISTED, "steps": items, "continue_text": continue_text,
            "max_continuations": max_continuations, "idempotency_key": idempotency_key,
            "created_at": now, "updated_at": now,
        })
        if task.get("deduplicated"):
            return {"status": "OK", "deduplicated": True, "task": self._view(task)}
        with self._lock:
            task = self._tick_task(task)
        task = self._await_acceptance(task)
        return {"status": "OK" if task["state"] in ACTIVE_STATES or task["state"] == "DONE" else "FAILED",
                "task": self._view(task)}

    def get(self, task_id: str, *, refresh: bool = True) -> dict[str, Any]:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "FAILED", "error": "INVALID_TASK_ID"}
        task = self.store.get(task_id)
        if task is None:
            return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
        if refresh:
            with self._lock:
                task = (self._tick_task(task) if task["completed_at"] is None
                        else self._late_completion(task))
        return {"status": "OK", "task": self._view(task)}

    def get_by_key(self, idempotency_key: str, *, refresh: bool = True) -> dict[str, Any]:
        """Status for a client that lost the task_id but kept its key."""
        task = self.store.by_idempotency_key(idempotency_key) if idempotency_key else None
        if task is None:
            return {"status": "FAILED", "error": "UNKNOWN_IDEMPOTENCY_KEY"}
        return self.get(task["task_id"], refresh=refresh)

    def complete(self, task_id: str, *, outcome: str = "DONE", reason: str | None = None) -> dict[str, Any]:
        outcome = (outcome or "DONE").upper()
        if outcome not in {"DONE", "BLOCKED"}:
            return {"status": "FAILED", "error": "INVALID_OUTCOME", "allowed": ["DONE", "BLOCKED"]}
        return self._finish(task_id, outcome, reason or f"explicit {outcome} from client")

    def cancel(self, task_id: str, *, reason: str | None = None) -> dict[str, Any]:
        return self._finish(task_id, "CANCELLED", reason or "cancelled by client")

    def active_for(self, target: str) -> list[dict[str, Any]]:
        return [self._view(task) for task in self.store.active(target)]

    def annotation(self, target: str) -> dict[str, Any] | None:
        """What a caller must know when a pane reads IDLE: the task is not over."""
        active = self.store.active(target)
        if not active:
            return None
        task = active[0]
        return {
            "task_id": task["task_id"], "task_state": task["state"],
            "receipt": task.get("receipt"),
            "business_complete": False,
            "note": ("IDLE/composer means the agent turn ended, not that the supervised task is "
                     "done. The server continues it until an explicit DONE/BLOCKED, "
                     "WAITING_INPUT, cancel, or the continuation limit; do not resend."),
        }

    def tick(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        with contextlib.suppress(Exception):
            self.store.set_meta("last_tick_at", repr(self.clock()))
        for task in self.store.active():
            try:
                with self._lock:
                    task = self._tick_task(task)
            except Exception:  # noqa: BLE001 -- one bad task must not stall the rest
                _log.exception("direct task tick failed for %s", task.get("task_id"))
                continue
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        return counts

    def watcher_health(self) -> dict[str, Any]:
        raw = None
        with contextlib.suppress(Exception):
            raw = self.store.get_meta("last_tick_at")
        if raw is None:
            return {"alive": False, "last_tick_age_s": None}
        age = max(0.0, self.clock() - float(raw))
        return {"alive": age <= max(30.0, LOOP_INTERVAL_SECONDS * 10),
                "last_tick_age_s": round(age, 1)}

    # -- internals -------------------------------------------------------------

    def _view(self, task: dict[str, Any]) -> dict[str, Any]:
        state = task["state"]
        next_action = _NEXT_ACTIONS.get(state, "inspect")
        if state == "STUCK_COMPOSER" and (task.get("recovery_attempts") or 0) >= MAX_RECOVERY_ATTEMPTS:
            next_action = ("bounded Enter-only recovery is exhausted: press Enter in the session "
                           "or supervise_cancel. Never resend the text.")
        return {
            "task_id": task["task_id"], "target": task["target"], "mode": task["mode"],
            "agent": task.get("agent"),
            "state": state, "wait_reason": task.get("wait_reason"),
            "business_complete": state == "DONE",
            "running_confirmed": task.get("accepted_at") is not None,
            "receipt": task.get("receipt") or (RECEIPT_EXECUTION_ACCEPTED if task["dispatches"] else None),
            "receipts": {
                "persisted_at": task["created_at"],
                "input_sent_at": task.get("input_sent_at"),
                "submit_confirmed_at": task.get("submit_confirmed_at"),
                "execution_accepted_at": task.get("accepted_at"),
            },
            "submission_id": task.get("submission_id"),
            "turn_state": task.get("last_turn_state"),
            "steps_total": len(task["steps"]), "steps_dispatched": task["next_step"],
            "continuations": task["continuations"],
            "max_continuations": task["max_continuations"],
            "dispatches": task["dispatches"],
            "recovery_attempts": task.get("recovery_attempts") or 0,
            "reason": task.get("reason"),
            "last_error": task.get("last_error"), "last_error_hop": task.get("last_error_hop"),
            "late_completion": bool(task.get("late_completion")),
            "created_at": task["created_at"], "updated_at": task["updated_at"],
            "completed_at": task["completed_at"],
            "next_action": next_action,
            "watcher": self.watcher_health(),
            "marker_format": "TMCP-DONE:<task_id> | TMCP-BLOCKED:<task_id> <reason>",
        }

    def _finish(self, task_id: str, state: str, reason: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "FAILED", "error": "INVALID_TASK_ID"}
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
            if task["completed_at"] is not None:
                return {"status": "OK", "already_finished": True, "task": self._view(task)}
            task = self.store.update(task_id, state=state, reason=_clip(reason),
                                     completed_at=self.clock())
        return {"status": "OK", "task": self._view(task)}

    def _await_acceptance(self, task: dict[str, Any]) -> dict[str, Any]:
        """Bounded in-call look for execution evidence, so a fast start can
        honestly say RUNNING. Never resends; the loop carries the rest."""
        if self.accept_wait_seconds <= 0:
            return task
        deadline = self.clock() + self.accept_wait_seconds
        while (task["completed_at"] is None and task["state"] == "DISPATCHING"
               and task.get("receipt") == RECEIPT_SUBMIT_CONFIRMED and self.clock() < deadline):
            self._sleep(1.0)
            with self._lock:
                task = self._tick_task(task)
        return task

    def _late_completion(self, task: dict[str, Any]) -> dict[str, Any]:
        """A marker printed after the task was failed by a limit or an
        outage still records the agent's explicit outcome."""
        if (task["state"] != "FAILED" or task.get("late_completion")
                or self.clock() - (task["completed_at"] or 0) > LATE_COMPLETION_WINDOW_SECONDS):
            return task
        tail = self._tail(task["target"], MARKER_TAIL_LINES)
        marker = find_marker(str(tail.get("output") or ""), task["task_id"]) if "error" not in tail else None
        if marker is None:
            return task
        outcome, detail = marker
        return self.store.update(
            task["task_id"], expect_active=False, state=outcome, late_completion=1,
            reason=_clip(f"late completion after {task.get('reason')}: "
                         f"{detail or 'target printed TMCP-' + outcome}"))

    def _needle(self, task: dict[str, Any], text: str) -> str:
        if task["mode"] == "agent" and task["task_id"] in text:
            return task["task_id"]
        return _collapse(text)[:40]

    def _next_text(self, task: dict[str, Any], now: float) -> tuple[str | None, dict[str, Any], dict[str, Any] | None]:
        """(text, counters to apply once typed, or a terminal update)."""
        if task["next_step"] < len(task["steps"]):
            text = task["steps"][task["next_step"]]
            if task["mode"] == "agent" and task["next_step"] == 0 and task["task_id"] not in text:
                # One line: a raw newline can submit an agent composer early.
                text = f"{text} {completion_instruction(task['task_id'])}"
            return text, {"next_step": task["next_step"] + 1}, None
        if task["mode"] == "agent" or task.get("continue_text"):
            if task["continuations"] >= task["max_continuations"]:
                return None, {}, {
                    "state": "FAILED", "completed_at": now,
                    "reason": (f"CONTINUATION_LIMIT_REACHED: {task['continuations']} continuations "
                               "without an explicit DONE/BLOCKED")}
            number = task["continuations"] + 1
            text = (task["continue_text"] or
                    continuation_prompt(task["task_id"], number, task["max_continuations"]))
            return text, {"continuations": number}, None
        # A shell has nothing sensible to "continue"; every step ran and no
        # explicit outcome was seen. Hold for one instead of guessing DONE.
        return None, {}, {
            "state": "WAITING_INPUT", "wait_reason": "AWAITING_EXPLICIT_COMPLETION",
            "reason": ("all steps dispatched; awaiting explicit completion (a TMCP-DONE marker "
                       "line or supervise_complete)")}

    def _dispatch(self, task: dict[str, Any], now: float) -> dict[str, Any]:
        """Send the next step or continuation; stop at an explicit limit."""
        text, advance, terminal = self._next_text(task, now)
        if terminal is not None:
            return self.store.update(task["task_id"], **terminal)
        key = f"direct-task:{task['task_id']}:{task['dispatches'] + 1}:{task['send_failures']}"
        needle = self._needle(task, text)
        # Persist the intent BEFORE the send: after a crash between here and
        # the result, the task is verified by observation, never re-typed.
        task = self.store.update(
            task["task_id"], receipt=RECEIPT_INPUT_SENT, dispatch_key=key, dispatch_needle=needle,
            pending_advance=json.dumps(advance), input_sent_at=now, submit_confirmed_at=None,
            accepted_at=None,
            recovery_attempts=0, last_recovery_at=None, state="DISPATCHING", wait_reason=None,
            idle_since=None)
        try:
            sent = self._send(task["target"], text, key)
        except Exception as exc:  # noqa: BLE001 -- outcome unknown, verified below
            sent = {"status": "FAILED", "reason": type(exc).__name__, "evidence": {"delivery_state": "UNKNOWN"}}
        return self._record_send(task, sent, now)

    def _record_send(self, task: dict[str, Any], sent: dict[str, Any], now: float) -> dict[str, Any]:
        status = sent.get("status")
        evidence = sent.get("evidence") if isinstance(sent.get("evidence"), dict) else {}
        delivery = evidence.get("delivery_state") or evidence.get("submit_status")
        reason = _clip(f"{status}: {sent.get('reason') or sent.get('error') or ''}")
        advance = json.loads(task.get("pending_advance") or "{}")
        common = {**advance, "pending_advance": None,
                  "submission_id": sent.get("submission_id") or task.get("submission_id")}
        if status == "SUBMIT_CONFIRMED":
            # Enter + adapter ack. Still DISPATCHING: not proof of execution.
            return self.store.update(
                task["task_id"], **common, dispatches=task["dispatches"] + 1, send_failures=0,
                last_dispatch_at=now, receipt=RECEIPT_SUBMIT_CONFIRMED, submit_confirmed_at=now,
                last_turn_state="DISPATCHED", reason=None)
        if status == "BLOCKED" or delivery in _DEFINITELY_NOT_TYPED and sent.get("error"):
            # Refused before anything was typed: safe to retry, bounded.
            failures = task["send_failures"] + 1
            update = {"receipt": RECEIPT_NOT_SENT, "send_failures": failures, "pending_advance": None,
                      "reason": _clip(f"dispatch not confirmed: {reason}"), "idle_since": None}
            if failures >= MAX_SEND_FAILURES:
                return self.store.update(task["task_id"], **update, state="BLOCKED", completed_at=now)
            return self.store.update(task["task_id"], **update)
        # Typed (or maybe typed) but unconfirmed: the step is consumed -- it
        # is in the pane -- and the outcome is settled by observation only.
        return self.store.update(
            task["task_id"], **common, dispatches=task["dispatches"] + 1, last_dispatch_at=now,
            receipt=RECEIPT_UNCERTAIN, last_turn_state="DISPATCHED",
            reason=_clip(f"dispatch outcome unconfirmed ({delivery or status}): verifying by "
                         f"observation, not resending: {reason}"))

    def _observe_failed(self, task: dict[str, Any], observed: dict[str, Any], now: float) -> dict[str, Any]:
        hop = _error_hop(observed)
        failures = task["observe_failures"] + 1
        error = _clip(f"{observed.get('error') or 'not observable'}: {observed.get('reason') or ''}")
        backoff = min(MAX_OBSERVE_BACKOFF_SECONDS, LOOP_INTERVAL_SECONDS * (2 ** min(failures, 5)))
        fields = {"observe_failures": failures, "last_error": error, "last_error_hop": hop,
                  "reason": _clip(f"target not observable ({hop}): {error}"),
                  "next_observe_at": now + backoff}
        if hop == "session":
            if failures >= MAX_OBSERVE_FAILURES // 4:
                return self.store.update(task["task_id"], **fields, state="FAILED", completed_at=now)
            return self.store.update(task["task_id"], **fields)
        since = task.get("unreachable_since") or now
        fields["unreachable_since"] = since
        if now - since >= NODE_UNREACHABLE_FAIL_SECONDS and failures >= MAX_OBSERVE_FAILURES:
            return self.store.update(task["task_id"], **fields, state="FAILED", completed_at=now)
        return self.store.update(task["task_id"], **fields)

    def _tick_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task = self.store.get(task["task_id"]) or task
        if task["completed_at"] is not None:
            return task
        now = self.clock()
        if task.get("next_observe_at") and now < task["next_observe_at"]:
            return task
        observed = self._status(task["target"])
        if "error" in observed or observed.get("exists") is False:
            return self._observe_failed(task, observed, now)
        turn_state = str(observed.get("state") or "UNKNOWN").upper()
        base = {"observe_failures": 0, "last_turn_state": turn_state, "unreachable_since": None,
                "next_observe_at": None}
        if task.get("last_error_hop"):
            base.update(last_error=None, last_error_hop=None)
        if task["mode"] == "auto":
            agent = agent_of(observed)
            if agent is None:
                return self.store.update(task["task_id"], **base,
                                         reason="MODE_UNRESOLVED: waiting to identify the pane's CLI")
            task = self.store.update(task["task_id"], mode="shell" if agent == "shell" else "agent",
                                     agent=None if agent == "shell" else agent)
        elif not task.get("agent") and (agent := agent_of(observed)) in _AGENT_HINTS:
            task = self.store.update(task["task_id"], agent=agent)

        output = None
        if turn_state != "RUNNING" or task.get("receipt") != RECEIPT_EXECUTION_ACCEPTED:
            tail = self._tail(task["target"], MARKER_TAIL_LINES)
            output = str(tail.get("output") or "") if "error" not in tail else None
            marker = find_marker(output or "", task["task_id"])
            if marker is not None:
                outcome, detail = marker
                return self.store.update(
                    task["task_id"], **base, state=outcome, completed_at=now, wait_reason=None,
                    reason=_clip(detail or f"target printed TMCP-{outcome}"))

        receipt = task.get("receipt")
        if task["dispatches"] == 0 and receipt in (None, RECEIPT_PERSISTED, RECEIPT_NOT_SENT):
            return self._first_dispatch(task, base, turn_state, observed, output, now)
        if receipt in (RECEIPT_INPUT_SENT, RECEIPT_SUBMIT_CONFIRMED, RECEIPT_UNCERTAIN):
            return self._verify_acceptance(task, base, turn_state, observed, output, now)
        return self._supervise_running(task, base, turn_state, observed, now)

    def _first_dispatch(self, task, base, turn_state, observed, output, now):
        if turn_state == "WAITING_INPUT" or (output and _BOOT_DIALOG.search(output)):
            # Never type into a dialog -- in particular never let our Enter
            # answer a hooks-review/trust menu. Nothing was sent.
            return self.store.update(task["task_id"], **base, state="WAITING_INPUT",
                                     wait_reason="SESSION_INPUT_BEFORE_DISPATCH", idle_since=None,
                                     reason=_clip(observed.get("reason")))
        if turn_state == "RUNNING":
            # Busy with something else: wait for it rather than queueing
            # text into a working agent.
            return self.store.update(task["task_id"], **base, state="DISPATCHING",
                                     wait_reason="TARGET_BUSY", idle_since=None)
        task = self.store.update(task["task_id"], **base, wait_reason=None)
        return self._dispatch(task, now)

    def _verify_acceptance(self, task, base, turn_state, observed, output, now):
        """Independent evidence that the agent took the dispatched prompt."""
        needle = task.get("dispatch_needle") or ""
        sent_at = task.get("input_sent_at") or task.get("last_dispatch_at") or now
        if task.get("receipt") == RECEIPT_INPUT_SENT:
            # Intent persisted but no recorded result. Sends run under the
            # same lock as ticks, so this is never an in-flight send: a crash
            # hit it. Apply the step (it may be in the pane) and verify.
            advance = json.loads(task.get("pending_advance") or "{}")
            task = self.store.update(task["task_id"], **advance, pending_advance=None,
                                     dispatches=task["dispatches"] + 1, last_dispatch_at=sent_at,
                                     receipt=RECEIPT_UNCERTAIN,
                                     reason="send outcome lost (restart/timeout); verifying by observation")
        holds = output is not None and composer_holds(output, needle)
        # A RUNNING read from the tmux activity-age fallback is a redraw, not
        # execution evidence (a dialog redraw reads the same); only the CLI's
        # own busy footer/adapter signal counts.
        accepted = (turn_state == "RUNNING" and not holds
                    and not _WEAK_RUNNING.search(str(observed.get("reason") or "")))
        if not accepted and output is not None and not holds:
            if task["mode"] == "shell":
                accepted = bool(needle) and needle in _collapse(output) and turn_state == "IDLE"
            else:
                accepted = echoed_then_answered(output, needle)
        if accepted:
            return self.store.update(
                task["task_id"], **base, state="RUNNING", receipt=RECEIPT_EXECUTION_ACCEPTED,
                accepted_at=now, wait_reason=None, idle_since=None, reason=None,
                last_dispatch_at=now if turn_state != "RUNNING" else task.get("last_dispatch_at"))
        if turn_state == "WAITING_INPUT":
            return self.store.update(task["task_id"], **base, state="WAITING_INPUT",
                                     wait_reason="SESSION_INPUT", idle_since=None,
                                     reason=_clip(observed.get("reason")))
        if holds:
            if now - sent_at < STUCK_GRACE_SECONDS:
                return self.store.update(task["task_id"], **base, state="DISPATCHING")
            return self._recover_stuck(task, base, now)
        if now - sent_at >= ACCEPT_TIMEOUT_SECONDS:
            return self.store.update(
                task["task_id"], **base, state="DISPATCH_UNCERTAIN",
                wait_reason="NO_EXECUTION_EVIDENCE",
                reason=_clip(f"no execution evidence {now - sent_at:.0f}s after dispatch "
                             f"(turn {turn_state}, composer empty); not resending"))
        return self.store.update(task["task_id"], **base,
                                 state="DISPATCHING" if task["state"] != "DISPATCH_UNCERTAIN"
                                 else "DISPATCH_UNCERTAIN")

    def _recover_stuck(self, task, base, now):
        attempts = task.get("recovery_attempts") or 0
        fields = {**base, "state": "STUCK_COMPOSER", "wait_reason": "STUCK_COMPOSER"}
        if (self._activate is None or attempts >= MAX_RECOVERY_ATTEMPTS
                # Recovery only when verified NOT executing: never press Enter
                # into a pane that is busy with a turn.
                or base.get("last_turn_state") == "RUNNING"
                or (task.get("last_recovery_at") and
                    now - task["last_recovery_at"] < RECOVERY_COOLDOWN_SECONDS)):
            reason = ("prompt is still in the composer"
                      + ("; bounded Enter-only recovery exhausted" if attempts >= MAX_RECOVERY_ATTEMPTS
                         else ""))
            return self.store.update(task["task_id"], **fields, reason=reason)
        key = f"direct-task:{task['task_id']}:recover:{task['dispatches']}:{attempts + 1}"
        task = self.store.update(task["task_id"], **fields, recovery_attempts=attempts + 1,
                                 last_recovery_at=now)
        try:
            result = self._activate(task["target"], key)
        except Exception as exc:  # noqa: BLE001
            result = {"status": "FAILED", "reason": type(exc).__name__}
        return self.store.update(
            task["task_id"],
            reason=_clip(f"prompt was still in the composer; Enter-only recovery "
                         f"{attempts + 1}/{MAX_RECOVERY_ATTEMPTS}: {result.get('status')}"))

    def _supervise_running(self, task, base, turn_state, observed, now):
        if turn_state == "WAITING_INPUT":
            return self.store.update(task["task_id"], **base, state="WAITING_INPUT",
                                     wait_reason="SESSION_INPUT", idle_since=None,
                                     reason=_clip(observed.get("reason")))
        if task["state"] == "WAITING_INPUT" and task.get("wait_reason") == "AWAITING_EXPLICIT_COMPLETION":
            return self.store.update(task["task_id"], **base)
        if task["state"] in ("WAITING_INPUT", "STUCK_COMPOSER", "DISPATCH_UNCERTAIN"):
            # A human answered the prompt; supervision resumes.
            task = self.store.update(task["task_id"], state="RUNNING", wait_reason=None, reason=None)
        if turn_state != "IDLE":
            return self.store.update(task["task_id"], **base, idle_since=None)
        # The turn ended. Only an explicit marker (checked above) ends the task.
        if task["last_dispatch_at"] is not None and now - task["last_dispatch_at"] < self.settle_seconds:
            return self.store.update(task["task_id"], **base, idle_since=None)
        if task["idle_since"] is None:
            return self.store.update(task["task_id"], **base, idle_since=now)
        if now - task["idle_since"] < self.settle_seconds:
            return self.store.update(task["task_id"], **base)
        task = self.store.update(task["task_id"], **base)
        return self._dispatch(task, now)


class DirectTaskLoop:
    """Daemon ticker; supervision also advances on every status read."""

    def __init__(self, supervisor: DirectTaskSupervisor,
                 interval: float = LOOP_INTERVAL_SECONDS) -> None:
        self.supervisor = supervisor
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="direct-task-loop")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.supervisor.tick()
            except Exception:  # noqa: BLE001 -- keep supervising
                _log.exception("direct task loop tick failed")
