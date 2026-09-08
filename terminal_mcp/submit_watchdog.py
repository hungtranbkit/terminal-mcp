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

FINAL_ACKS = {ACK_ACCEPTED, ACK_RUNNING, ACK_STUCK}


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
                updated_at REAL NOT NULL
            )""")
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
        )

    def create(self, *, idempotency_key: str, session: str, agent_type: str,
               prompt: str) -> tuple[Submission, bool]:
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
                 ack_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                       (sid, idempotency_key, session, agent_type, prompt, digest,
                        ACK_QUEUED, now, now))
            row = db.execute("SELECT * FROM prompt_submissions WHERE submission_id=?", (sid,)).fetchone()
        created = self._row(row)
        assert created is not None
        return created, True

    def update(self, submission_id: str, *, ack_state: str | None = None,
               enter_count: int | None = None, attempts: int | None = None,
               evidence: str | None = None) -> Submission:
        current = self.get(submission_id)
        if current is None:
            raise KeyError(submission_id)
        evidence_list = list(current.evidence)
        if evidence and evidence not in evidence_list:
            evidence_list.append(evidence)
        with self._connection() as db:
            db.execute("""UPDATE prompt_submissions SET ack_state=?, enter_count=?, attempts=?,
                evidence_json=?, updated_at=? WHERE submission_id=?""",
                       (ack_state or current.ack_state,
                        current.enter_count if enter_count is None else enter_count,
                        current.attempts if attempts is None else attempts,
                        json.dumps(evidence_list), time.time(), submission_id))
        result = self.get(submission_id)
        assert result is not None
        return result

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
            rows = db.execute("SELECT * FROM prompt_submissions WHERE ack_state IN (?, ?, ?, ?)",
                              (ACK_QUEUED, ACK_INJECTED, ACK_SUBMITTING, ACK_STUCK)).fetchall()
        return [self._row(row) for row in rows if self._row(row) is not None]  # type: ignore[misc]


@dataclass(frozen=True)
class WatchdogConfig:
    poll_interval_seconds: float = 0.4
    timeout_seconds: float = 5.0
    max_enter_attempts: int = 3


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
            inject: Callable[[str], None] | None = None) -> dict[str, Any]:
        record = self.store.get(submission_id)
        if record is None:
            raise KeyError(submission_id)
        if record.ack_state in FINAL_ACKS:
            return record.public()
        if record.ack_state == ACK_QUEUED:
            if inject is None:
                self.store.update(submission_id, ack_state=ACK_STUCK, evidence="inject_callback_missing")
                return self.store.get(submission_id).public()  # type: ignore[union-attr]
            inject(record.prompt)  # exactly once; retries never call inject
            record = self.store.update(submission_id, ack_state=ACK_INJECTED, evidence="text_injected_once")
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
            if state in (ACK_ACCEPTED, ACK_RUNNING):
                self.store.update(submission_id, ack_state=state, evidence=reason)
                return self.store.get(submission_id).public()  # type: ignore[union-attr]
            if state in ("PAGER", "INCOMPLETE"):
                # Never use Enter to advance a pager or to submit a draft
                # whose full buffer cannot yet be observed. Keep polling;
                # the sweeper may later see the composer after the UI settles.
                self.store.update(submission_id, evidence=reason)
                time.sleep(self.config.poll_interval_seconds)
                continue
            if current.enter_count >= self.config.max_enter_attempts:
                break
            # A slow TUI may still be consuming the previous Enter. Require
            # either a composer redraw or two stable polls (~0.8s by default)
            # before another Enter; this preserves recovery for swallowed
            # Enter while preventing queued duplicate submissions.
            if unchanged and unchanged_polls < 2 and current.enter_count > 0:
                time.sleep(self.config.poll_interval_seconds)
                continue
            send_enter()
            current = self.store.update(submission_id, enter_count=current.enter_count + 1,
                                        attempts=current.attempts + 1, evidence=reason or "enter_sent")
            time.sleep(self.config.poll_interval_seconds)
        self.store.update(submission_id, ack_state=ACK_STUCK, evidence="recovery: submit_evidence_timeout")
        return self.store.get(submission_id).public()  # type: ignore[union-attr]


class SubmissionSweeper:
    def __init__(self, store: SubmissionStore, recover: Callable[[Submission], None],
                 interval_seconds: float = 1.5) -> None:
        self.store, self.recover, self.interval_seconds = store, recover, interval_seconds
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
            for record in self.store.active():
                try:
                    self.recover(record)
                except Exception:
                    # Recovery is best effort; the durable record remains for
                    # the next pass and the caller's normal audit path owns
                    # detailed error reporting.
                    continue
