"""Task finalizer: settle stale supervised tasks on evidence, not on silence.

WHY

Live HP data on 2026-10-26: 25 FAILED and 27 BLOCKED direct tasks, many days
old. A FAILED row frequently meant only "the controller could not look"
(SESSION_RUNTIME_MISSING, NODE_UNREACHABLE, STATUS_PROBE_TIMEOUT) or "the
agent stopped without a marker six times" (CONTINUATION_LIMIT_REACHED); a
supervisor DONE meant only "the agent printed TMCP-DONE". None of those says
whether the work is verified, and nothing ever released the task's resource
locks or settled its GitHub issue.

WHAT

`decide()` is a pure function over four inputs -- the durable task row, one
read-only observation of the session (status + tail), recorded QA evidence
and a recorded review -- returning exactly one of

  RUNNING        still the supervisor's: nothing to finalize yet
  WAITING_INPUT  a named prompt the owner must answer (kept, leases kept)
  BLOCKED        explicit TMCP-BLOCKED / blocked task / provider usage limit
  FAILED         proven failure, or retries exhausted while unobservable
  QA_PENDING     the agent claimed DONE; no passing QA evidence yet
  DONE_VERIFIED  claimed DONE *and* passing QA evidence with references

Rules that must survive refactoring:

* An idle composer is never completion. IDLE without this task's TMCP-DONE
  marker is FAILED once the continuation budget is spent, never DONE.
* DONE_VERIFIED needs a PASS QA record naming a reviewer and at least one
  evidence reference (URL, path, SHA, log); "the agent said so" is QA_PENDING.
* Transient unobservability (node down, probe timeout) is retried with
  exponential backoff and at most `max_attempts` times, then FAILED with
  the reason -- never left RUNNING forever, never retried forever.
* A GitHub issue is closed as `completed` ONLY for DONE_VERIFIED with QA
  evidence, and as `not_planned` ONLY with a recorded OBSOLETE review. The
  finalizer never opens or reopens an issue, and only acts with apply=True.
* Leaving the agent's hands (any state but RUNNING/WAITING_INPUT) releases
  the resource locks the task held (owner_id == task_id).
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from .direct_task import MARKER_TAIL_LINES, DirectTaskStore, find_marker

RUNNING = "RUNNING"
WAITING_INPUT = "WAITING_INPUT"
BLOCKED = "BLOCKED"
FAILED = "FAILED"
QA_PENDING = "QA_PENDING"
DONE_VERIFIED = "DONE_VERIFIED"
STATES = (RUNNING, WAITING_INPUT, BLOCKED, FAILED, QA_PENDING, DONE_VERIFIED)
#: Settled for good: never observed again.
FINAL_STATES = (FAILED, DONE_VERIFIED)
#: The agent is no longer working on the task: its resource locks go back.
RELEASE_STATES = (BLOCKED, FAILED, QA_PENDING, DONE_VERIFIED)

DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 60.0
BACKOFF_CAP_SECONDS = 3600.0
#: Owner-wait states are re-checked, slowly, without consuming attempts.
OWNER_WAIT_RECHECK_SECONDS = 900.0

_GONE = {"SESSION_NOT_FOUND", "SESSION_RUNTIME_MISSING", "SESSION_DELETED"}
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def default_finalizer_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_FINALIZER_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "task_finalizer.db"


def _decision(state: str, reason: str, next_action: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "reason": reason[:500], "next_action": next_action,
            **{k: v for k, v in extra.items() if v is not None}}


def qa_passes(qa: dict[str, Any] | None) -> bool:
    return bool(isinstance(qa, dict) and qa.get("verdict") == "PASS" and qa.get("reviewer")
                and isinstance(qa.get("evidence"), list)
                and any(isinstance(item, str) and item.strip() for item in qa["evidence"]))


def decide(task: dict[str, Any], observation: dict[str, Any] | None, *,
           qa: dict[str, Any] | None = None, review: dict[str, Any] | None = None,
           attempts: int = 0, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict[str, Any]:
    """One finalization verdict. Pure: no I/O, no clock."""
    task_id = task["task_id"]
    observation = observation if isinstance(observation, dict) else {}
    tail = str(observation.get("output") or "")
    marker = find_marker(tail, task_id) if tail else None
    claimed_done = (marker is not None and marker[0] == "DONE") or task.get("state") == "DONE"

    if isinstance(review, dict) and review.get("verdict") == "OBSOLETE":
        return _decision(FAILED, f"OBSOLETE: reviewed by {review.get('reviewer')}: {review.get('reason')}",
                         "none; the review recorded this task as obsolete", obsolete=True)
    if claimed_done:
        if isinstance(qa, dict) and qa.get("verdict") == "FAIL":
            return _decision(FAILED, f"QA_FAILED: {qa.get('summary') or 'QA reported failure'}",
                             "Rework is a new task with the QA findings; this one stays failed.")
        if qa_passes(qa):
            return _decision(DONE_VERIFIED, f"DONE with QA PASS by {qa['reviewer']}",
                             "none", evidence=list(qa["evidence"]))
        return _decision(QA_PENDING, "agent reported DONE; no passing QA evidence recorded",
                         "Run QA and record it (verdict PASS|FAIL, reviewer, evidence refs).")
    if marker is not None and marker[0] == "BLOCKED":
        return _decision(BLOCKED, f"TMCP-BLOCKED: {marker[1] or 'no reason given'}",
                         "Owner resolves the stated blocker, then starts a follow-up task.")

    error = str(observation.get("error") or "")
    if error in _GONE or observation.get("exists") is False:
        return _decision(FAILED, f"SESSION_GONE: {error or 'session does not exist'}; no completion marker",
                         "The session is gone without TMCP-DONE; re-run the task in a new session.")
    if error:
        if attempts + 1 >= max_attempts:
            return _decision(FAILED, f"UNOBSERVABLE: {error} on {attempts + 1} attempts; outcome unverified",
                             "Inspect the session by hand once its node is back; nothing was resent.")
        return _decision(task.get("state") if task.get("state") in STATES else RUNNING,
                         f"observation failed ({error}); will retry", "none; retrying with backoff",
                         transient=True)

    prompt = observation.get("input_prompt") if isinstance(observation.get("input_prompt"), dict) else None
    if prompt or observation.get("input_required") or observation.get("state") == "WAITING_INPUT":
        return _decision(WAITING_INPUT, (prompt or {}).get("kind") or "INPUT_PROMPT",
                         (prompt or {}).get("next_action")
                         or "Owner answers the prompt in the session; never auto-answered.",
                         input_prompt=prompt)
    if observation.get("blocked_reason"):
        return _decision(BLOCKED, str(observation["blocked_reason"]),
                         ((observation.get("usage_limit") or {}).get("next_action")
                          or "Resolve the named blocker."))
    if task.get("state") == "BLOCKED":
        return _decision(BLOCKED, str(task.get("reason") or "blocked"),
                         "Owner resolves the stated blocker, then starts a follow-up task.")

    state = str(observation.get("state") or "UNKNOWN").upper()
    active = task.get("completed_at") is None
    if state == "RUNNING":
        if active:
            return _decision(RUNNING, "agent turn in flight", "none; the supervisor owns it")
        return _decision(task.get("state") if task.get("state") in STATES else FAILED,
                         f"{task.get('reason')}; the session is busy again (likely other work)",
                         "none; recheck later", transient=True)
    if state == "IDLE":
        spent = int(task.get("continuations") or 0) >= int(task.get("max_continuations") or 0)
        if active and not spent:
            return _decision(RUNNING, "idle between turns; continuation budget remains",
                             "none; the supervisor continues it")
        return _decision(FAILED, "IDLE_WITHOUT_COMPLETION_MARKER: the composer is idle and the task "
                         "printed no TMCP-DONE; an idle composer is not completion",
                         "Read the transcript; if the work is done, the agent prints the marker "
                         "or a reviewer records QA.")
    if attempts + 1 >= max_attempts:
        return _decision(FAILED, f"UNVERIFIED: session state stayed {state} for {attempts + 1} attempts",
                         "Inspect the session by hand; nothing was resent.")
    return _decision(task.get("state") if task.get("state") in STATES else RUNNING,
                     f"session state {state}; will retry", "none; retrying with backoff", transient=True)


class FinalizerStore:
    """Durable verdicts, QA/review records and issue links (SQLite/WAL)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_finalizer_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS task_finalizations (
                    task_id TEXT PRIMARY KEY,
                    state TEXT, reason TEXT, next_action TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL,
                    qa TEXT, review TEXT,
                    issue_repo TEXT, issue_number INTEGER, issue_closed_as TEXT,
                    leases_released INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL)""")
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

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM task_finalizations WHERE task_id = ?",
                                     (task_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("qa", "review"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def put(self, task_id: str, now: float, **fields: Any) -> dict[str, Any]:
        for key in ("qa", "review"):
            if key in fields and fields[key] is not None:
                fields[key] = json.dumps(fields[key], ensure_ascii=False)
        with self._connection() as connection:
            connection.execute("INSERT OR IGNORE INTO task_finalizations (task_id, updated_at) "
                               "VALUES (?, ?)", (task_id, now))
            if fields:
                assignments = ", ".join(f"{key} = ?" for key in fields)
                connection.execute(f"UPDATE task_finalizations SET {assignments}, updated_at = ? "
                                   "WHERE task_id = ?", (*fields.values(), now, task_id))
        return self.get(task_id) or {}


class GitHubIssues:
    """The two issue writes the finalizer may make, via the `gh` CLI."""

    def __init__(self, run: Callable[..., Any] = subprocess.run) -> None:
        self._run = run

    def state(self, repo: str, number: int) -> str | None:
        done = self._run(["gh", "api", f"repos/{repo}/issues/{number}", "-q", ".state"],
                         capture_output=True, text=True, timeout=30, check=False)
        return (done.stdout.strip() or None) if done.returncode == 0 else None

    def close(self, repo: str, number: int, *, reason: str, comment: str) -> bool:
        done = self._run(["gh", "issue", "close", str(number), "--repo", repo,
                          "--reason", "completed" if reason == "completed" else "not planned",
                          "--comment", comment],
                         capture_output=True, text=True, timeout=60, check=False)
        return done.returncode == 0


class TaskFinalizer:
    """Reconcile durable direct tasks with session, QA, review and issue."""

    def __init__(self, tasks: DirectTaskStore, store: FinalizerStore, *,
                 status: Callable[[str], dict], tail: Callable[[str, int], dict],
                 locks: Any = None, github: GitHubIssues | None = None,
                 clock: Callable[[], float] = time.time,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.tasks, self.store = tasks, store
        self._status, self._tail = status, tail
        self.locks, self.github = locks, github
        self.clock = clock
        self.max_attempts = max_attempts

    # -- evidence ------------------------------------------------------------

    def record_qa(self, task_id: str, *, verdict: str, reviewer: str, evidence: list[str],
                  summary: str | None = None) -> dict[str, Any]:
        if self.tasks.get(task_id) is None:
            return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
        verdict = str(verdict or "").upper()
        evidence = [str(item).strip() for item in (evidence or []) if str(item).strip()]
        if verdict not in ("PASS", "FAIL") or not str(reviewer or "").strip():
            return {"status": "FAILED", "error": "INVALID_QA",
                    "detail": "verdict PASS|FAIL and a reviewer are required"}
        if verdict == "PASS" and not evidence:
            return {"status": "FAILED", "error": "QA_EVIDENCE_REQUIRED",
                    "detail": "a PASS needs at least one evidence reference (URL, path, SHA, log)"}
        self.store.put(task_id, self.clock(), qa={
            "verdict": verdict, "reviewer": reviewer.strip(), "evidence": evidence[:20],
            "summary": (summary or "")[:500], "recorded_at": self.clock()}, next_attempt_at=None)
        return self.reconcile(task_id)

    def record_review(self, task_id: str, *, verdict: str, reviewer: str, reason: str) -> dict[str, Any]:
        if self.tasks.get(task_id) is None:
            return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
        if str(verdict or "").upper() != "OBSOLETE" or not str(reviewer or "").strip() \
                or not str(reason or "").strip():
            return {"status": "FAILED", "error": "INVALID_REVIEW",
                    "detail": "verdict OBSOLETE, a reviewer and a reason are required"}
        self.store.put(task_id, self.clock(), review={
            "verdict": "OBSOLETE", "reviewer": reviewer.strip(), "reason": reason.strip()[:500],
            "recorded_at": self.clock()}, next_attempt_at=None)
        return self.reconcile(task_id)

    def link_issue(self, task_id: str, *, repo: str, number: int) -> dict[str, Any]:
        if self.tasks.get(task_id) is None:
            return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
        if not isinstance(repo, str) or not _REPO.fullmatch(repo) \
                or isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return {"status": "FAILED", "error": "INVALID_ISSUE", "detail": "repo owner/name and number > 0"}
        self.store.put(task_id, self.clock(), issue_repo=repo, issue_number=number)
        return {"status": "OK", "task_id": task_id, "issue": f"{repo}#{number}"}

    # -- reconciliation ----------------------------------------------------

    def _observe(self, target: str) -> dict[str, Any]:
        observed = self._status(target) or {}
        if observed.get("error"):
            return observed
        tail = self._tail(target, MARKER_TAIL_LINES) or {}
        return {**observed, "output": "" if tail.get("error") else str(tail.get("output") or "")}

    def reconcile(self, task_id: str, *, apply_github: bool = False) -> dict[str, Any]:
        task = self.tasks.get(task_id)
        if task is None:
            return {"status": "FAILED", "error": "UNKNOWN_TASK_ID"}
        now = self.clock()
        row = self.store.get(task_id) or {}
        if row.get("state") in FINAL_STATES:
            return {"status": "OK", "task_id": task_id, "already_final": True,
                    **self._public(row), **self._settle_issue(task_id, row, apply_github, now)}
        try:
            observation = self._observe(task["target"])
        except Exception as exc:  # noqa: BLE001 -- an observation failure is evidence too
            observation = {"error": f"OBSERVE_FAILED: {type(exc).__name__}"}
        attempts = int(row.get("attempts") or 0)
        verdict = decide(task, observation, qa=row.get("qa"), review=row.get("review"),
                         attempts=attempts, max_attempts=self.max_attempts)
        if verdict.pop("transient", False):
            attempts += 1
            next_at = now + min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempts - 1))
        elif verdict["state"] in (WAITING_INPUT, BLOCKED, RUNNING):
            next_at = now + OWNER_WAIT_RECHECK_SECONDS
        else:
            next_at = None
        fields = {"state": verdict["state"], "reason": verdict["reason"],
                  "next_action": verdict["next_action"], "attempts": attempts,
                  "next_attempt_at": next_at}
        released = None
        if verdict["state"] in RELEASE_STATES and not row.get("leases_released") and self.locks is not None:
            released = int(self.locks.release_all(task_id))
            fields["leases_released"] = 1
        row = self.store.put(task_id, now, **fields)
        return {"status": "OK", "task_id": task_id, "target": task["target"],
                "task_state": task["state"], **self._public(row),
                **({"input_prompt": verdict["input_prompt"]} if verdict.get("input_prompt") else {}),
                **({"evidence": verdict["evidence"]} if verdict.get("evidence") else {}),
                **({"leases_released_count": released} if released is not None else {}),
                **self._settle_issue(task_id, row, apply_github, now)}

    def sweep(self, *, limit: int = 50, apply_github: bool = False) -> dict[str, Any]:
        """Reconcile every due, unsettled task once. Bounded: one pass, at most
        `limit` tasks, each due only after its own backoff."""
        now = self.clock()
        with self.tasks._connection() as connection:  # noqa: SLF001 -- read-only scan
            ids = [row["task_id"] for row in connection.execute(
                "SELECT task_id FROM direct_tasks WHERE state IN "
                "('DONE','BLOCKED','FAILED','RUNNING','WAITING_INPUT','EXECUTION_ACCEPTED',"
                "'STUCK_COMPOSER') ORDER BY updated_at DESC").fetchall()]
        counts: dict[str, int] = {}
        visited = 0
        for task_id in ids:
            if visited >= limit:
                break
            row = self.store.get(task_id) or {}
            if row.get("state") in FINAL_STATES and (row.get("issue_closed_as") or not row.get("issue_number")):
                continue
            if row.get("next_attempt_at") and row["next_attempt_at"] > now:
                continue
            visited += 1
            result = self.reconcile(task_id, apply_github=apply_github)
            counts[result.get("state", "ERROR")] = counts.get(result.get("state", "ERROR"), 0) + 1
        return {"status": "OK", "reconciled": visited, "states": counts}

    # -- issue ---------------------------------------------------------------

    def _settle_issue(self, task_id: str, row: dict[str, Any], apply: bool, now: float) -> dict[str, Any]:
        repo, number = row.get("issue_repo"), row.get("issue_number")
        if not repo or not number:
            return {}
        issue = {"issue": f"{repo}#{number}"}
        if row.get("issue_closed_as"):
            return {**issue, "issue_action": "none", "issue_closed_as": row["issue_closed_as"]}
        qa, review = row.get("qa"), row.get("review")
        if row.get("state") == DONE_VERIFIED and qa_passes(qa):
            reason = "completed"
            comment = (f"Closed by the Terminal MCP task finalizer: task {task_id} is DONE_VERIFIED. "
                       f"QA PASS by {qa['reviewer']}. Evidence: " + "; ".join(qa["evidence"]))
        elif isinstance(review, dict) and review.get("verdict") == "OBSOLETE":
            reason = "not_planned"
            comment = (f"Closed as not planned by the Terminal MCP task finalizer: task {task_id} "
                       f"was reviewed OBSOLETE by {review['reviewer']}: {review['reason']}")
        else:
            return {**issue, "issue_action": "none",
                    "issue_note": "left open: closing needs DONE_VERIFIED with QA evidence, or an OBSOLETE review"}
        if not apply or self.github is None:
            return {**issue, "issue_action": f"close_{reason}", "applied": False}
        current = self.github.state(repo, int(number))
        if current == "closed":
            self.store.put(task_id, now, issue_closed_as="already_closed")
            return {**issue, "issue_action": "none", "issue_closed_as": "already_closed"}
        if current != "open" or not self.github.close(repo, int(number), reason=reason, comment=comment):
            return {**issue, "issue_action": f"close_{reason}", "applied": False,
                    "issue_error": "GITHUB_UNAVAILABLE"}
        self.store.put(task_id, now, issue_closed_as=reason)
        return {**issue, "issue_action": f"close_{reason}", "applied": True}

    @staticmethod
    def _public(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in ("state", "reason", "next_action", "attempts",
                                              "next_attempt_at") if row.get(key) is not None}
