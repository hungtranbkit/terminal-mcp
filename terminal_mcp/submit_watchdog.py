"""Durable, evidence-driven prompt submission for interactive agents.

The watchdog deliberately separates text injection from Enter recovery.  A
retry is therefore never allowed to type the prompt again.  The store keeps
the exact prompt locally (0600 SQLite) so a process restart can reconcile a
submission without guessing or re-injecting it; prompt text is never logged
or returned by the status APIs.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

ACK_QUEUED = "QUEUED"
ACK_INJECTED = "INJECTED"
ACK_SUBMITTING = "SUBMITTING"
ACK_ACCEPTED = "ACCEPTED"
ACK_RUNNING = "RUNNING"
ACK_STUCK = "STUCK"
ACK_BLOCKED_APPROVAL = "BLOCKED_APPROVAL"
ACK_NODE_UNAVAILABLE = "NODE_UNAVAILABLE"

FINAL_ACKS = {ACK_ACCEPTED, ACK_RUNNING, ACK_STUCK, ACK_BLOCKED_APPROVAL, ACK_NODE_UNAVAILABLE}


@dataclass(frozen=True)
class Submission:
    submission_id: str
    idempotency_key: str
    session: str
    agent_type: str
    prompt: str
    prompt_sha256: str
    ack_state: str
    enter_count: int
    attempts: int
    evidence: tuple[str, ...]
    created_at: float
    updated_at: float
    node_id: str | None = None
    first_seen: float | None = None
    last_check: float | None = None
    last_action: str | None = None
    execution_started: bool = False
    terminal_state: str | None = None
    stop_reason: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "idempotency_key": self.idempotency_key,
            "session": self.session,
            "agent_type": self.agent_type,
            "ack_state": self.ack_state,
            "enter_count": self.enter_count,
            "attempts": self.attempts,
            "evidence": list(self.evidence),
            "prompt_sha256": self.prompt_sha256,
            "characters": len(self.prompt),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "node_id": self.node_id,
            "first_seen": self.first_seen,
            "last_check": self.last_check,
            "last_action": self.last_action,
            "execution_started": self.execution_started,
            "terminal_state": self.terminal_state,
            "stop_reason": self.stop_reason,
        }


class SubmissionStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS prompt_submissions (
                submission_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                session TEXT NOT NULL, agent_type TEXT NOT NULL, prompt TEXT NOT NULL,
                prompt_sha256 TEXT NOT NULL, ack_state TEXT NOT NULL,
                enter_count INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                evidence_json TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL,
                updated_at REAL NOT NULL, node_id TEXT, first_seen REAL,
                last_check REAL, last_action TEXT, execution_started INTEGER NOT NULL DEFAULT 0,
                terminal_state TEXT, stop_reason TEXT
            )""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(prompt_submissions)")}
            for name, declaration in (("node_id", "TEXT"), ("first_seen", "REAL"),
                                      ("last_check", "REAL"), ("last_action", "TEXT"),
                                      ("execution_started", "INTEGER NOT NULL DEFAULT 0"),
                                      ("terminal_state", "TEXT"), ("stop_reason", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE prompt_submissions ADD COLUMN {name} {declaration}")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @contextlib.contextmanager
    def _connection(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Submission | None:
        if row is None:
            return None
        return Submission(
            submission_id=row["submission_id"], idempotency_key=row["idempotency_key"],
            session=row["session"], agent_type=row["agent_type"], prompt=row["prompt"],
            prompt_sha256=row["prompt_sha256"], ack_state=row["ack_state"],
            enter_count=row["enter_count"], attempts=row["attempts"],
            evidence=tuple(json.loads(row["evidence_json"])),
            created_at=row["created_at"], updated_at=row["updated_at"],
            node_id=row["node_id"], first_seen=row["first_seen"], last_check=row["last_check"],
            last_action=row["last_action"], execution_started=bool(row["execution_started"]),
            terminal_state=row["terminal_state"], stop_reason=row["stop_reason"],
        )

    def create(self, *, idempotency_key: str, session: str, agent_type: str,
               prompt: str, node_id: str | None = None) -> tuple[Submission, bool]:
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        now = time.time()
        with self._connection() as db:
            existing = db.execute("SELECT * FROM prompt_submissions WHERE idempotency_key=?",
                                  (idempotency_key,)).fetchone()
            if existing is not None:
                current = self._row(existing)
                assert current is not None
                if current.prompt_sha256 != digest:
                    raise ValueError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PROMPT")
                return current, False
            sid = uuid.uuid4().hex
            db.execute("""INSERT INTO prompt_submissions
                (submission_id,idempotency_key,session,agent_type,prompt,prompt_sha256,
                ack_state,created_at,updated_at,node_id,first_seen,last_check,terminal_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (sid, idempotency_key, session, agent_type, prompt, digest,
                        ACK_QUEUED, now, now, node_id, now, now, ACK_QUEUED))
            row = db.execute("SELECT * FROM prompt_submissions WHERE submission_id=?", (sid,)).fetchone()
        created = self._row(row)
        assert created is not None
        return created, True

    def update(self, submission_id: str, *, ack_state: str | None = None,
               enter_count: int | None = None, attempts: int | None = None,
               evidence: str | None = None, node_id: str | None = None,
               last_action: str | None = None, execution_started: bool | None = None,
               terminal_state: str | None = None, stop_reason: str | None = None) -> Submission:
        current = self.get(submission_id)
        if current is None:
            raise KeyError(submission_id)
        evidence_list = list(current.evidence)
        if evidence and evidence not in evidence_list:
            evidence_list.append(evidence)
        with self._connection() as db:
            now = time.time()
            db.execute("""UPDATE prompt_submissions SET ack_state=?, enter_count=?, attempts=?,
                evidence_json=?, updated_at=?, node_id=?, last_check=?, last_action=?,
                execution_started=?, terminal_state=?, stop_reason=? WHERE submission_id=?""",
                       (ack_state or current.ack_state,
                        current.enter_count if enter_count is None else enter_count,
                        current.attempts if attempts is None else attempts,
                        json.dumps(evidence_list), now, node_id or current.node_id, now,
                        last_action or current.last_action,
                        int(current.execution_started if execution_started is None else execution_started),
                        terminal_state or ack_state or current.terminal_state,
                        stop_reason or current.stop_reason, submission_id))
        result = self.get(submission_id)
        assert result is not None
        return result

    def reserve_enter(self, submission_id: str, *, cap: int, action: str) -> Submission | None:
        """Persist an Enter reservation before sending it.

        Both the foreground submit path and the sweeper use this compare-and-
        increment so concurrent recovery can never emit a seventh Enter.
        Counting a failed send is intentional: ambiguity fails closed.
        """
        now = time.time()
        with self._connection() as db:
            changed = db.execute(
                """UPDATE prompt_submissions SET enter_count=enter_count+1,
                   attempts=attempts+1, updated_at=?, last_check=?, last_action=?
                   WHERE submission_id=? AND enter_count < ?
                     AND ack_state IN (?, ?, ?)""",
                (now, now, action, submission_id, cap, ACK_QUEUED, ACK_INJECTED, ACK_SUBMITTING),
            ).rowcount
        return self.get(submission_id) if changed else None

    def get(self, submission_id: str) -> Submission | None:
        with self._connection() as db:
            return self._row(db.execute("SELECT * FROM prompt_submissions WHERE submission_id=?",
                                        (submission_id,)).fetchone())

    def by_key(self, key: str) -> Submission | None:
        with self._connection() as db:
            return self._row(db.execute("SELECT * FROM prompt_submissions WHERE idempotency_key=?",
                                        (key,)).fetchone())

    def active(self) -> list[Submission]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM prompt_submissions WHERE ack_state IN (?, ?, ?)",
                              (ACK_QUEUED, ACK_INJECTED, ACK_SUBMITTING)).fetchall()
        return [self._row(row) for row in rows if self._row(row) is not None]  # type: ignore[misc]

    def watcher_status(self) -> dict[str, Any]:
        with self._connection() as db:
            rows = db.execute("SELECT * FROM prompt_submissions ORDER BY updated_at DESC LIMIT 100").fetchall()
        records = [self._row(row) for row in rows]
        records = [row for row in records if row is not None]
        pending = [row for row in records if row.ack_state in {ACK_QUEUED, ACK_INJECTED, ACK_SUBMITTING}]
        return {
            "active": True,
            "pending_submissions": len(pending),
            "recovered_submissions": sum(row.execution_started for row in records),
            "stuck_submissions": sum(row.ack_state == ACK_STUCK for row in records),
            "approval_blocked_count": sum(row.ack_state == ACK_BLOCKED_APPROVAL for row in records),
            "node_unavailable_count": sum(row.ack_state == ACK_NODE_UNAVAILABLE for row in records),
            "submissions": [row.public() for row in records[:50]],
        }


@dataclass(frozen=True)
class WatchdogConfig:
    poll_interval_seconds: float = 0.4
    timeout_seconds: float = 5.0
    # Two DIFFERENT caps, deliberately:
    #
    #   max_enter_attempts -- what ONE foreground run() (the caller-facing
    #     terminal_send_text) may spend: one normal Enter and at most one
    #     evidence-gated recovery Enter.  Never turn an ambiguous send into
    #     an unbounded key spam loop (which can double-submit destructive
    #     confirmations), and never make the caller wait on a six-Enter
    #     ladder inline.
    #   max_total_enters -- the DURABLE hard ceiling for the submission as a
    #     whole, shared by that foreground run and every background watcher
    #     pass (SubmissionSweeper -> recover_submission, one new Enter per
    #     pass with backoff).  This is the "six-Enter verified prompt
    #     recovery" budget; it can only ever lower, never raise, the
    #     per-run cap above.
    max_enter_attempts: int = 2
    max_total_enters: int = 6
    retry_agent_types: frozenset[str] = frozenset({"codex"})


class VerifiedSubmitWatchdog:
    """Run one verified submission against a backend/adapter pair.

    Callbacks are intentionally tiny, making the same implementation usable
    by tmux and ConPTY. `capture` returns adapter-visible lines; `evidence`
    returns `(state, reason)` where state is accepted/running/composer/pager/
    incomplete/unknown. `send_enter` is the only operation allowed after the
    one-shot text injection.
    """
    def __init__(self, store: SubmissionStore, config: WatchdogConfig | None = None) -> None:
        self.store = store
        self.config = config or WatchdogConfig()

    def run(self, submission_id: str, *, capture: Callable[[], list[str]],
            send_enter: Callable[[], None],
            evidence: Callable[[list[str], Submission], tuple[str, str]],
            inject: Callable[[str], None] | None = None,
            max_new_enters: int | None = None) -> dict[str, Any]:
        record = self.store.get(submission_id)
        if record is None:
            raise KeyError(submission_id)
        if record.ack_state in FINAL_ACKS:
            result = record.public()
            result.update({"first_enter_effect": "already_submitted",
                           "recovery_enter_sent": record.enter_count > 1,
                           "composer_before": "unknown", "composer_after": "unknown",
                           "submit_reason": "already_submitted",
                           "submit_latency_ms": 0})
            return result
        started = time.monotonic()
        first_enter_effect: str | None = None
        composer_before = "unknown"
        composer_after = "unknown"

        def finish() -> dict[str, Any]:
            current = self.store.get(submission_id)
            assert current is not None
            if current.ack_state in (ACK_ACCEPTED, ACK_RUNNING):
                after = "cleared_or_executing"
            elif composer_after == "unknown":
                after = "unknown"
            else:
                after = composer_after
            return {
                **current.public(),
                "first_enter_effect": first_enter_effect or "not_sent",
                "recovery_enter_sent": current.enter_count > 1,
                "composer_before": composer_before,
                "composer_after": after,
                "submit_reason": (current.evidence[-1] if current.evidence
                                   else current.ack_state),
                "submit_latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
        if record.ack_state == ACK_QUEUED:
            if inject is None:
                self.store.update(submission_id, ack_state=ACK_STUCK, evidence="inject_callback_missing")
                return finish()
            inject(record.prompt)  # exactly once; retries never call inject
            record = self.store.update(submission_id, ack_state=ACK_INJECTED, evidence="text_injected_once")
        # This watchdog is Codex's only multi-Enter policy.  If a record is
        # ever created by another front door/backend, fail closed to the
        # single-submit contract: one initial Enter may already have happened,
        # but no automatic retry is permitted.
        # A background watcher pass (max_new_enters is not None) is bounded by
        # its own per-pass budget plus the durable total; a foreground run gets
        # the per-run contract cap.  Both are clamped by max_total_enters, the
        # single durable ceiling shared across processes and restarts.
        per_run_cap = (self.config.max_total_enters if max_new_enters is not None
                       else self.config.max_enter_attempts)
        max_enter_attempts = min(per_run_cap, self.config.max_total_enters)
        max_enter_attempts = (max_enter_attempts
                              if record.agent_type in self.config.retry_agent_types else 1)
        initial_enter_count = record.enter_count
        self.store.update(submission_id, ack_state=ACK_SUBMITTING)
        deadline = time.monotonic() + self.config.timeout_seconds
        last_lines: list[str] | None = None
        unchanged_polls = 0
        while time.monotonic() < deadline:
            current = self.store.get(submission_id)
            assert current is not None
            lines = capture()
            unchanged = lines == last_lines
            if unchanged:
                unchanged_polls += 1
            else:
                unchanged_polls = 0
            last_lines = list(lines)
            state, reason = evidence(lines, current)
            # Execution/ack evidence observed before any activation key is
            # not attributable to this submission (it may be stale work from
            # an earlier turn). Never let it become a false confirmation.
            if current.enter_count == 0 and state in (ACK_ACCEPTED, ACK_RUNNING):
                state, reason = "COMPOSER", "pre_activation_evidence_withheld"
            if current.enter_count == 0:
                composer_before = "present" if state == "COMPOSER" else state.lower()
            if state == "COMPOSER":
                composer_after = "present"
            elif state in (ACK_ACCEPTED, ACK_RUNNING):
                composer_after = "cleared_or_executing"
            else:
                composer_after = state.lower()
            if state in (ACK_ACCEPTED, ACK_RUNNING):
                if current.enter_count == 1 and first_enter_effect is None:
                    first_enter_effect = "submitted"
                self.store.update(submission_id, ack_state=state, evidence=reason,
                                  execution_started=True, last_action="execution_evidence")
                return finish()
            if state.upper() in {"PAGER", "WAITING_APPROVAL", "WAITING_INPUT", "INPUT_REQUIRED"} or any(
                    word in reason.casefold() for word in
                    ("approval", "permission", "confirmation", "numbered choice", "numbered-choice", "input_required")):
                self.store.update(submission_id, ack_state=ACK_BLOCKED_APPROVAL,
                                  evidence=reason or "approval_or_input_required",
                                  last_action="approval_blocked", stop_reason="approval_or_input_required")
                return finish()
            if state in ("PAGER", "INCOMPLETE"):
                # Never use Enter to advance a pager or to submit a draft
                # whose full buffer cannot yet be observed. Keep polling;
                # the sweeper may later see the composer after the UI settles.
                self.store.update(submission_id, evidence=reason)
                time.sleep(self.config.poll_interval_seconds)
                continue
            if current.enter_count >= max_enter_attempts:
                self.store.update(submission_id, ack_state=ACK_STUCK, evidence="recovery: enter_cap_reached",
                                  last_action="enter_cap_reached", stop_reason="max_enter_cap")
                return finish()
            if max_new_enters is not None and current.enter_count - initial_enter_count >= max_new_enters:
                # Leave it active for the next bounded sweeper pass.  The
                # persisted count and backoff decide whether another Enter is
                # ever allowed; a single pass never sends two.
                self.store.update(submission_id, ack_state=ACK_SUBMITTING,
                                  last_action="watcher_cycle_complete")
                return finish()
            # A slow TUI may still be consuming the previous Enter. Require
            # either a composer redraw or two stable polls (~0.8s by default)
            # before another Enter; this preserves recovery for swallowed
            # Enter while preventing queued duplicate submissions.
            if unchanged and unchanged_polls < 2 and current.enter_count > 0:
                time.sleep(self.config.poll_interval_seconds)
                continue
            if current.enter_count == 0:
                first_enter_effect = "composition_commit_or_submit"
            else:
                first_enter_effect = first_enter_effect or "composition_commit_or_submit"
            action = "watcher_enter" if max_new_enters is not None else "submit_enter"
            current = self.store.reserve_enter(submission_id, cap=max_enter_attempts, action=action)
            if current is None:
                self.store.update(submission_id, ack_state=ACK_STUCK, evidence="recovery: enter_cap_reached",
                                  last_action="enter_cap_reached", stop_reason="max_enter_cap")
                return finish()
            send_enter()
            current = self.store.update(submission_id, evidence=reason or "enter_sent", last_action=action)
            time.sleep(self.config.poll_interval_seconds)
        if max_new_enters is not None:
            self.store.update(submission_id, ack_state=ACK_SUBMITTING, evidence="watcher_cycle_timeout",
                              last_action="watcher_cycle_complete")
            return finish()
        self.store.update(submission_id, ack_state=ACK_STUCK, evidence="recovery: submit_evidence_timeout",
                          stop_reason="execution_evidence_timeout")
        return finish()


class SubmissionSweeper:
    """Continuous verified-start watcher using the durable submission store."""
    def __init__(self, store: SubmissionStore, recover: Callable[[Submission], None],
                 interval_seconds: float = 3.0, ttl_seconds: float = 600.0,
                 backoff_seconds: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 8.0)) -> None:
        self.store, self.recover, self.interval_seconds = store, recover, interval_seconds
        self.ttl_seconds, self.backoff_seconds = ttl_seconds, backoff_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="submit-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self.run_once()

    def run_once(self) -> None:
        for record in self.store.active():
            try:
                now = time.time()
                if now - record.created_at > self.ttl_seconds:
                    self.store.update(record.submission_id, ack_state=ACK_STUCK,
                                      evidence="watcher_stale_submission_ttl",
                                      last_action="watcher_stale", stop_reason="stale_submission_ttl")
                    continue
                delay = self.backoff_seconds[min(record.enter_count, len(self.backoff_seconds) - 1)]
                if record.last_check and now - record.last_check < delay:
                    continue
                self.store.update(record.submission_id, last_action="watcher_check")
                self.recover(record)
            except Exception:
                # Recovery is best effort; the durable record remains for
                # the next pass and the caller's normal audit path owns
                # detailed error reporting.
                continue
