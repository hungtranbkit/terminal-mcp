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

ACTIVE_STATES = ("RUNNING", "WAITING_INPUT")
TERMINAL_STATES = ("DONE", "BLOCKED", "CANCELLED", "FAILED")
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
MARKER_TAIL_LINES = 60
LOOP_INTERVAL_SECONDS = 3.0
TASK_ID_PLACEHOLDER = "{TMCP_TASK_ID}"
_TASK_ID = re.compile(r"^dt_[0-9a-f]{16}$")
# Leading decoration an agent UI may put before its own output line (Claude's
# ●/⏺, Codex's •, list bullets, box-drawing borders). `>` is deliberately NOT
# stripped: it is the prompt-echo prefix in agent composers.
_MARKER_PREFIX = r"^[ \t●⏺•◦·*\-–—│|]*"
_AGENT_HINTS = ("claude", "codex")


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


class DirectTaskStore:
    """SQLite/WAL persistence so supervision survives a controller restart."""

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


class DirectTaskSupervisor:
    """Drives supervised tasks. All I/O goes through injected, guarded callables.

    ``send(target, text, idempotency_key)`` -> dict with ``status``
    (``SUBMIT_CONFIRMED`` on success); ``status(target)`` -> dict with
    ``state``/``reason`` or ``error``; ``tail(target, lines)`` -> dict with
    ``output``. In production these are CompactTerminalTools' own guarded
    send/status/tail, so authorization is re-checked on every dispatch.
    """

    def __init__(self, store: DirectTaskStore, *, send: Callable[[str, str, str], dict],
                 status: Callable[[str], dict], tail: Callable[[str, int], dict],
                 clock: Callable[[], float] = _now,
                 settle_seconds: float = DEFAULT_SETTLE_SECONDS) -> None:
        self.store = store
        self._send = send
        self._status = status
        self._tail = tail
        self.clock = clock
        self.settle_seconds = float(settle_seconds)
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
        if self.store.active(target):
            existing = self.store.active(target)[0]
            if not (idempotency_key and existing.get("idempotency_key") == idempotency_key):
                return {"status": "FAILED", "error": "TARGET_HAS_ACTIVE_SUPERVISED_TASK",
                        "task": self._view(existing),
                        "next_action": "complete or cancel that task first (supervise_complete / supervise_cancel)"}
        if mode == "auto":
            observed = self._status(target)
            if "error" in observed:
                return {"status": "FAILED", "error": "TARGET_UNAVAILABLE",
                        "reason": _clip(observed.get("reason") or observed.get("error"))}
            hint = f"{observed.get('reason', '')}".lower()
            mode = "agent" if any(agent in hint for agent in _AGENT_HINTS) else "shell"
        task_id = f"dt_{uuid.uuid4().hex[:16]}"
        # Lets a step reference its own task, e.g. a final shell step
        # `printf 'TMCP-%s:%s\n' DONE {TMCP_TASK_ID}` completes explicitly.
        items = [item.replace(TASK_ID_PLACEHOLDER, task_id) for item in items]
        if mode == "agent":
            # One line: a raw newline can submit an agent composer early.
            items[0] = f"{items[0]} {completion_instruction(task_id)}"
        now = self.clock()
        task = self.store.insert({
            "task_id": task_id, "target": target, "mode": mode, "state": "RUNNING",
            "steps": items, "continue_text": continue_text,
            "max_continuations": max_continuations, "idempotency_key": idempotency_key,
            "created_at": now, "updated_at": now,
        })
        if task.get("deduplicated"):
            return {"status": "OK", "deduplicated": True, "task": self._view(task)}
        with self._lock:
            task = self._dispatch(task, now)
        return {"status": "OK" if task["state"] in ACTIVE_STATES or task["state"] == "DONE" else "FAILED",
                "task": self._view(task)}

    def get(self, task_id: str, *, refresh: bool = True) -> dict[str, Any]:
        if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
            return {"status": "FAILED", "error": "INVALID_TASK_ID"}
        task = self.store.get(task_id)
        if task is None:
            return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
        if refresh and task["completed_at"] is None:
            with self._lock:
                task = self._tick_task(task)
        return {"status": "OK", "task": self._view(task)}

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
            "business_complete": False,
            "note": ("IDLE/composer means the agent turn ended, not that the supervised task is "
                     "done. The server continues it until an explicit DONE/BLOCKED, "
                     "WAITING_INPUT, cancel, or the continuation limit; do not resend."),
        }

    def tick(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.store.active():
            try:
                with self._lock:
                    task = self._tick_task(task)
            except Exception:  # noqa: BLE001 -- one bad task must not stall the rest
                _log.exception("direct task tick failed for %s", task.get("task_id"))
                continue
            counts[task["state"]] = counts.get(task["state"], 0) + 1
        return counts

    # -- internals -------------------------------------------------------------

    def _view(self, task: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": task["task_id"], "target": task["target"], "mode": task["mode"],
            "state": task["state"], "wait_reason": task.get("wait_reason"),
            "business_complete": task["state"] == "DONE",
            "turn_state": task.get("last_turn_state"),
            "steps_total": len(task["steps"]), "steps_dispatched": task["next_step"],
            "continuations": task["continuations"],
            "max_continuations": task["max_continuations"],
            "dispatches": task["dispatches"], "reason": task.get("reason"),
            "created_at": task["created_at"], "updated_at": task["updated_at"],
            "completed_at": task["completed_at"],
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

    def _dispatch(self, task: dict[str, Any], now: float) -> dict[str, Any]:
        """Send the next step or continuation; stop at an explicit limit."""
        if task["next_step"] < len(task["steps"]):
            text = task["steps"][task["next_step"]]
            advance = {"next_step": task["next_step"] + 1}
        elif task["mode"] == "agent" or task.get("continue_text"):
            if task["continuations"] >= task["max_continuations"]:
                return self.store.update(
                    task["task_id"], state="FAILED", completed_at=now,
                    reason=(f"CONTINUATION_LIMIT_REACHED: {task['continuations']} continuations "
                            "without an explicit DONE/BLOCKED"))
            number = task["continuations"] + 1
            text = (task["continue_text"] or
                    continuation_prompt(task["task_id"], number, task["max_continuations"]))
            advance = {"continuations": number}
        else:
            # A shell has nothing sensible to "continue"; every step ran and no
            # explicit outcome was seen. Hold for one instead of guessing DONE.
            return self.store.update(
                task["task_id"], state="WAITING_INPUT", wait_reason="AWAITING_EXPLICIT_COMPLETION",
                reason=("all steps dispatched; awaiting explicit completion (a TMCP-DONE marker "
                        "line or supervise_complete)"))
        key = f"direct-task:{task['task_id']}:{task['dispatches'] + 1}:{task['send_failures']}"
        try:
            sent = self._send(task["target"], text, key)
        except Exception as exc:  # noqa: BLE001 -- recorded as a send failure below
            sent = {"status": "FAILED", "reason": type(exc).__name__}
        if sent.get("status") != "SUBMIT_CONFIRMED":
            failures = task["send_failures"] + 1
            reason = _clip(f"dispatch not confirmed: {sent.get('status')}: "
                           f"{sent.get('reason') or sent.get('error') or ''}")
            if failures >= MAX_SEND_FAILURES:
                return self.store.update(task["task_id"], state="BLOCKED", send_failures=failures,
                                         completed_at=now, reason=reason)
            return self.store.update(task["task_id"], send_failures=failures, reason=reason,
                                     idle_since=None)
        return self.store.update(
            task["task_id"], **advance, dispatches=task["dispatches"] + 1, send_failures=0,
            last_dispatch_at=now, idle_since=None, state="RUNNING", wait_reason=None,
            last_turn_state="DISPATCHED", reason=None)

    def _tick_task(self, task: dict[str, Any]) -> dict[str, Any]:
        task = self.store.get(task["task_id"]) or task
        if task["completed_at"] is not None:
            return task
        now = self.clock()
        observed = self._status(task["target"])
        if "error" in observed or observed.get("exists") is False:
            failures = task["observe_failures"] + 1
            reason = _clip(f"target not observable: {observed.get('error') or observed.get('reason')}")
            if failures >= MAX_OBSERVE_FAILURES:
                return self.store.update(task["task_id"], state="FAILED", completed_at=now,
                                         observe_failures=failures, reason=reason)
            return self.store.update(task["task_id"], observe_failures=failures, reason=reason)
        turn_state = str(observed.get("state") or "UNKNOWN").upper()
        base = {"observe_failures": 0, "last_turn_state": turn_state}

        if turn_state == "WAITING_INPUT":
            return self.store.update(task["task_id"], **base, state="WAITING_INPUT",
                                     wait_reason="SESSION_INPUT", idle_since=None,
                                     reason=_clip(observed.get("reason")))
        if task["state"] == "WAITING_INPUT" and task.get("wait_reason") == "SESSION_INPUT":
            # A human answered the prompt; supervision resumes.
            task = self.store.update(task["task_id"], state="RUNNING", wait_reason=None, reason=None)
        if turn_state != "IDLE":
            return self.store.update(task["task_id"], **base, idle_since=None)

        # The turn ended. Only an explicit marker ends the task.
        tail = self._tail(task["target"], MARKER_TAIL_LINES)
        marker = find_marker(str(tail.get("output") or ""), task["task_id"]) if "error" not in tail else None
        if marker is not None:
            outcome, detail = marker
            return self.store.update(
                task["task_id"], **base, state=outcome, completed_at=now, wait_reason=None,
                reason=_clip(detail or f"target printed TMCP-{outcome}"))
        if task["state"] == "WAITING_INPUT":
            return self.store.update(task["task_id"], **base)
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
