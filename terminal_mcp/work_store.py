"""Durable storage for the nouns Work Runtime adds, and only those.

WHAT THIS DELIBERATELY DOES NOT STORE

Tasks, their queue states, their dependencies, their dispatch bookkeeping and
their verification jobs already have a durable home in `queue_store.py` and
`verify_queue.py`. Copying any of that here would create two records of the
same fact and a race about which one is true. So a work task is a POINTER: a
row that says "queue task T on lane L belongs to work run W", plus the
work-level facts the queue has no opinion about (which are few).

What is genuinely new and therefore lives here:

  work_runs        the missing top-level noun -- a goal, its done criteria,
                   its lifecycle. `outcomes.py` is the closest existing thing
                   and is scoped to a backlog item, not to a user goal with
                   its own plan, approvals and state.
  work_tasks       the pointer above, plus per-work weight for progress.
  work_approvals   a gate with a requester, an approver and a decision. The
                   queue has PAUSED and NEEDS_HUMAN; it has no record of WHO
                   is being asked and no way to stop an agent resolving its
                   own gate.
  work_artifacts   evidence produced by a task, referenced not inlined.
  work_events      append-only. Every meaningful transition, so "why is this
                   run stuck" is answerable after the fact rather than
                   reconstructed from logs.

Same storage discipline as every other store here: SQLite, WAL, additive
migrations tracked by PRAGMA user_version, nothing destructive, and a schema
that an older build can still read.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import Migration, apply_migrations

# -- work run states. The template's vocabulary, unchanged. ------------------
DRAFT = "DRAFT"
PLANNING = "PLANNING"
READY = "READY"
RUNNING = "RUNNING"
VERIFYING = "VERIFYING"
WAITING_APPROVAL = "WAITING_APPROVAL"
BLOCKED = "BLOCKED"
PAUSED = "PAUSED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
COMPLETE = "COMPLETE"

WORK_STATES = (DRAFT, PLANNING, READY, RUNNING, VERIFYING, WAITING_APPROVAL,
               BLOCKED, PAUSED, FAILED, CANCELLED, COMPLETE)
TERMINAL_WORK_STATES = (COMPLETE, CANCELLED)

# Every allowed edge. Anything absent is refused -- centralised for the same
# reason queue_store centralises its own: a transition rule trusted to each
# caller is a rule that is wrong in one of them.
WORK_TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({PLANNING, READY, CANCELLED}),
    PLANNING: frozenset({READY, BLOCKED, FAILED, CANCELLED, DRAFT}),
    READY: frozenset({RUNNING, PAUSED, BLOCKED, CANCELLED}),
    RUNNING: frozenset({VERIFYING, WAITING_APPROVAL, BLOCKED, PAUSED, FAILED,
                        CANCELLED, COMPLETE, READY}),
    VERIFYING: frozenset({RUNNING, WAITING_APPROVAL, COMPLETE, BLOCKED, FAILED,
                          PAUSED, CANCELLED}),
    WAITING_APPROVAL: frozenset({RUNNING, VERIFYING, BLOCKED, CANCELLED, PAUSED, FAILED}),
    BLOCKED: frozenset({READY, RUNNING, PAUSED, CANCELLED, FAILED}),
    PAUSED: frozenset({READY, RUNNING, CANCELLED}),
    FAILED: frozenset({READY, CANCELLED}),      # retryable on purpose
    CANCELLED: frozenset(),
    COMPLETE: frozenset(),
}

# Approval decisions.
APPROVAL_PENDING = "PENDING"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"
APPROVAL_EXPIRED = "EXPIRED"


class WorkError(ValueError):
    """A refused operation, with a reason a caller can show a human."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _create_work_checkpoints(connection: sqlite3.Connection) -> None:
    connection.execute("""
        CREATE TABLE IF NOT EXISTS work_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            idempotency_key TEXT NOT NULL UNIQUE,
            work_id TEXT NOT NULL,
            work_task_id TEXT,
            queue_task_id TEXT,
            kind TEXT NOT NULL CHECK (kind IN ('CHECKPOINT', 'RESULT')),
            state TEXT NOT NULL,
            summary TEXT NOT NULL,
            completed TEXT NOT NULL DEFAULT '[]',
            remaining TEXT NOT NULL DEFAULT '[]',
            blockers TEXT NOT NULL DEFAULT '[]',
            next_hint TEXT,
            commit_sha TEXT,
            changed_files TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '[]',
            actor TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (work_id) REFERENCES work_runs(work_id) ON DELETE CASCADE,
            FOREIGN KEY (work_task_id) REFERENCES work_tasks(work_task_id) ON DELETE CASCADE
        )""")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_checkpoints_work "
        "ON work_checkpoints(work_id, created_at)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_work_checkpoints_task_kind "
        "ON work_checkpoints(work_task_id, kind, created_at)")


WORK_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: work_runs/work_tasks/work_approvals/work_artifacts/work_events",
              lambda connection: None),
    Migration(2, "add durable work checkpoint and result records", _create_work_checkpoints),
]


def default_work_db_path(state_home: str | None = None) -> Path:
    override = os.environ.get("TERMINAL_MCP_WORK_DB")
    if override:
        return Path(override).expanduser()
    base = Path(state_home).expanduser() if state_home else (
        Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")))
    return Path(base) / "terminal-mcp" / "work.db"


@dataclass(frozen=True)
class WorkRun:
    work_id: str
    title: str
    goal: str
    state: str
    project_id: str | None = None
    lane: str | None = None            # the `-work` session this run drives
    done_criteria: tuple[str, ...] = ()
    created_by: str | None = None
    created_at: str = ""
    updated_at: str = ""
    paused_reason: str | None = None
    failure_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"work_id": self.work_id, "title": self.title, "goal": self.goal,
                "state": self.state, "project_id": self.project_id, "lane": self.lane,
                "done_criteria": list(self.done_criteria), "created_by": self.created_by,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "paused_reason": self.paused_reason, "failure_reason": self.failure_reason,
                "metadata": dict(self.metadata)}


class WorkStore:
    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_work_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._create()
        apply_migrations(self._connection, WORK_MIGRATIONS)

    def _create(self) -> None:
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS work_runs (
                    work_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    state TEXT NOT NULL,
                    project_id TEXT,
                    lane TEXT,
                    done_criteria TEXT NOT NULL DEFAULT '[]',
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    paused_reason TEXT,
                    failure_reason TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )""")
            # A POINTER at a queue task, never a copy of it. `queue_task_id`
            # is the single source of truth for that task's state; what lives
            # here is only what the queue has no opinion about.
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS work_tasks (
                    work_task_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL,
                    queue_task_id TEXT,
                    lane TEXT,
                    title TEXT NOT NULL,
                    weight REAL NOT NULL DEFAULT 1.0,
                    required INTEGER NOT NULL DEFAULT 1,
                    position INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (work_id) REFERENCES work_runs(work_id) ON DELETE CASCADE
                )""")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_work_tasks_work ON work_tasks(work_id)")
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_work_tasks_queue "
                "ON work_tasks(queue_task_id) WHERE queue_task_id IS NOT NULL")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS work_approvals (
                    approval_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL,
                    work_task_id TEXT,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail TEXT,
                    requested_by TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decision TEXT NOT NULL DEFAULT 'PENDING',
                    decided_by TEXT,
                    decided_at TEXT,
                    decision_note TEXT,
                    FOREIGN KEY (work_id) REFERENCES work_runs(work_id) ON DELETE CASCADE
                )""")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_work_approvals_work "
                "ON work_approvals(work_id, decision)")
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS work_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL,
                    work_task_id TEXT,
                    kind TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    summary TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (work_id) REFERENCES work_runs(work_id) ON DELETE CASCADE
                )""")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_work_artifacts_work ON work_artifacts(work_id)")
            # Append-only. Never updated, never deleted outside retention --
            # "why is this run stuck" has to be answerable after the fact
            # rather than reconstructed from process logs.
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS work_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    work_id TEXT NOT NULL,
                    work_task_id TEXT,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    detail TEXT,
                    actor TEXT,
                    created_at TEXT NOT NULL
                )""")
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_work_events_work ON work_events(work_id, id)")

    # -- runs ----------------------------------------------------------------

    def create_run(self, *, title: str, goal: str, lane: str | None = None,
                   project_id: str | None = None,
                   done_criteria: Sequence[str] = (), created_by: str | None = None,
                   metadata: dict[str, Any] | None = None,
                   state: str = DRAFT) -> WorkRun:
        if state not in WORK_STATES:
            raise WorkError(f"unknown work state {state!r}")
        now = _now()
        run = WorkRun(work_id=new_id("work"), title=title.strip(), goal=goal.strip(),
                      state=state, project_id=project_id, lane=lane,
                      done_criteria=tuple(done_criteria), created_by=created_by,
                      created_at=now, updated_at=now, metadata=dict(metadata or {}))
        with self._connection:
            self._connection.execute(
                "INSERT INTO work_runs (work_id, title, goal, state, project_id, lane, "
                "done_criteria, created_by, created_at, updated_at, metadata) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run.work_id, run.title, run.goal, run.state, run.project_id, run.lane,
                 json.dumps(list(run.done_criteria)), run.created_by, now, now,
                 json.dumps(run.metadata)))
        self.record_event(run.work_id, kind="run_created", summary=f"work created: {run.title}",
                          actor=created_by)
        return run

    def get_run(self, work_id: str) -> WorkRun | None:
        row = self._connection.execute(
            "SELECT * FROM work_runs WHERE work_id = ?", (work_id,)).fetchone()
        return _row_to_run(row) if row else None

    def list_runs(self, *, state: str | None = None, project_id: str | None = None,
                  lane: str | None = None, include_terminal: bool = True,
                  limit: int = 100) -> list[WorkRun]:
        sql, args = "SELECT * FROM work_runs WHERE 1=1", []
        if state:
            sql += " AND state = ?"
            args.append(state)
        if project_id:
            sql += " AND project_id = ?"
            args.append(project_id)
        if lane:
            sql += " AND lane = ?"
            args.append(lane)
        if not include_terminal:
            sql += f" AND state NOT IN ({','.join('?' * len(TERMINAL_WORK_STATES))})"
            args.extend(TERMINAL_WORK_STATES)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        return [_row_to_run(row) for row in self._connection.execute(sql, args)]

    def transition_run(self, work_id: str, to_state: str, *, actor: str | None = None,
                       reason: str | None = None) -> WorkRun:
        """Validated centrally. An unlisted edge is refused rather than
        applied, so an impossible state cannot be reached by any caller."""
        run = self.get_run(work_id)
        if run is None:
            raise WorkError(f"unknown work run {work_id!r}")
        if to_state not in WORK_STATES:
            raise WorkError(f"unknown work state {to_state!r}")
        if to_state == run.state:
            return run
        allowed = WORK_TRANSITIONS.get(run.state, frozenset())
        if to_state not in allowed:
            raise WorkError(
                f"{work_id}: {run.state} -> {to_state} is not a valid transition "
                f"(allowed: {sorted(allowed) or 'none -- terminal state'})")
        now = _now()
        with self._connection:
            self._connection.execute(
                "UPDATE work_runs SET state = ?, updated_at = ?, "
                "paused_reason = ?, failure_reason = ? WHERE work_id = ?",
                (to_state, now,
                 reason if to_state in (PAUSED, BLOCKED) else None,
                 reason if to_state == FAILED else None,
                 work_id))
        self.record_event(work_id, kind="run_state",
                          summary=f"{run.state} -> {to_state}", detail=reason, actor=actor)
        return self.get_run(work_id)  # type: ignore[return-value]

    # -- tasks ---------------------------------------------------------------

    def add_task(self, work_id: str, *, title: str, queue_task_id: str | None = None,
                 lane: str | None = None, weight: float = 1.0, required: bool = True,
                 position: int | None = None) -> dict[str, Any]:
        if self.get_run(work_id) is None:
            raise WorkError(f"unknown work run {work_id!r}")
        now = _now()
        if position is None:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM work_tasks WHERE work_id = ?",
                (work_id,)).fetchone()
            position = int(row[0])
        task_id = new_id("wtask")
        with self._connection:
            self._connection.execute(
                "INSERT INTO work_tasks (work_task_id, work_id, queue_task_id, lane, title, "
                "weight, required, position, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, work_id, queue_task_id, lane, title, float(weight),
                 1 if required else 0, position, now, now))
        self.record_event(work_id, work_task_id=task_id, kind="task_added", summary=title)
        return self.get_task(task_id)  # type: ignore[return-value]

    def get_task(self, work_task_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM work_tasks WHERE work_task_id = ?", (work_task_id,)).fetchone()
        return dict(row) if row else None

    def tasks_for(self, work_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM work_tasks WHERE work_id = ? ORDER BY position, created_at",
            (work_id,))]

    def bind_queue_task(self, work_task_id: str, queue_task_id: str) -> None:
        """Attach the queue task that will actually run this work task.

        Separate from add_task because a plan is written before anything is
        enqueued, and the binding must be idempotent: re-binding the same
        queue task is a no-op, and binding a DIFFERENT one is refused rather
        than silently orphaning the first.
        """
        current = self.get_task(work_task_id)
        if current is None:
            raise WorkError(f"unknown work task {work_task_id!r}")
        existing = current.get("queue_task_id")
        if existing and existing != queue_task_id:
            raise WorkError(
                f"{work_task_id} is already bound to queue task {existing!r}; "
                f"rebinding would orphan it")
        if existing == queue_task_id:
            return
        with self._connection:
            self._connection.execute(
                "UPDATE work_tasks SET queue_task_id = ?, updated_at = ? WHERE work_task_id = ?",
                (queue_task_id, _now(), work_task_id))

    # -- checkpoints --------------------------------------------------------

    def record_checkpoint(self, work_id: str, *, idempotency_key: str, state: str,
                          summary: str, work_task_id: str | None = None,
                          queue_task_id: str | None = None,
                          completed: Any = (), remaining: Any = (), blockers: Any = (),
                          next_hint: str | None = None, commit_sha: str | None = None,
                          changed_files: Any = (), evidence: Any = (),
                          actor: str | None = None) -> dict[str, Any]:
        return self._record_checkpoint(
            work_id, kind="CHECKPOINT", idempotency_key=idempotency_key, state=state,
            summary=summary, work_task_id=work_task_id, queue_task_id=queue_task_id,
            completed=completed, remaining=remaining, blockers=blockers,
            next_hint=next_hint, commit_sha=commit_sha, changed_files=changed_files,
            evidence=evidence, actor=actor)

    def record_result_manifest(self, work_id: str, *, idempotency_key: str, state: str,
                               summary: str, work_task_id: str | None = None,
                               queue_task_id: str | None = None,
                               completed: Any = (), remaining: Any = (), blockers: Any = (),
                               next_hint: str | None = None, commit_sha: str | None = None,
                               changed_files: Any = (), evidence: Any = (),
                               actor: str | None = None) -> dict[str, Any]:
        return self._record_checkpoint(
            work_id, kind="RESULT", idempotency_key=idempotency_key, state=state,
            summary=summary, work_task_id=work_task_id, queue_task_id=queue_task_id,
            completed=completed, remaining=remaining, blockers=blockers,
            next_hint=next_hint, commit_sha=commit_sha, changed_files=changed_files,
            evidence=evidence, actor=actor)

    def _record_checkpoint(self, work_id: str, *, kind: str, idempotency_key: str,
                           state: str, summary: str, work_task_id: str | None,
                           queue_task_id: str | None, completed: Any, remaining: Any,
                           blockers: Any, next_hint: str | None, commit_sha: str | None,
                           changed_files: Any, evidence: Any,
                           actor: str | None) -> dict[str, Any]:
        # Replays return the first durable result without re-validating or
        # emitting another event. The idempotency key names that original
        # operation, not the arguments of a later retry.
        existing = self._checkpoint_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing
        if not idempotency_key or not idempotency_key.strip():
            raise WorkError("idempotency_key is required")
        if self.get_run(work_id) is None:
            raise WorkError(f"unknown work run {work_id!r}")

        if work_task_id is not None:
            task = self.get_task(work_task_id)
            if task is None:
                raise WorkError(f"unknown work task {work_task_id!r}")
            if task["work_id"] != work_id:
                raise WorkError(
                    f"work task {work_task_id!r} does not belong to work run {work_id!r}")
            bound_queue_task = task.get("queue_task_id")
            if queue_task_id is not None and bound_queue_task is not None \
                    and queue_task_id != bound_queue_task:
                raise WorkError(
                    f"work task {work_task_id!r} is bound to queue task "
                    f"{bound_queue_task!r}, not {queue_task_id!r}")
            if queue_task_id is None:
                queue_task_id = bound_queue_task

        encoded = [json.dumps(value) for value in
                   (completed, remaining, blockers, changed_files, evidence)]
        checkpoint_id = new_id("wcp")
        created_at = _now()
        with self._connection:
            cursor = self._connection.execute(
                "INSERT INTO work_checkpoints (checkpoint_id, idempotency_key, work_id, "
                "work_task_id, queue_task_id, kind, state, summary, completed, remaining, "
                "blockers, next_hint, commit_sha, changed_files, evidence, actor, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(idempotency_key) DO NOTHING",
                (checkpoint_id, idempotency_key, work_id, work_task_id, queue_task_id,
                 kind, state, summary, encoded[0], encoded[1], encoded[2], next_hint,
                 commit_sha, encoded[3], encoded[4], actor, created_at))
            if cursor.rowcount:
                self._connection.execute(
                    "INSERT INTO work_events (work_id, work_task_id, kind, summary, detail, "
                    "actor, created_at) VALUES (?,?,?,?,?,?,?)",
                    (work_id, work_task_id,
                     "checkpoint_recorded" if kind == "CHECKPOINT" else "result_recorded",
                     summary, checkpoint_id, actor, created_at))
        result = self._checkpoint_by_idempotency_key(idempotency_key)
        assert result is not None
        return result

    def latest_checkpoint(self, work_task_id: str) -> dict[str, Any] | None:
        return self._latest_for_task(work_task_id, "CHECKPOINT")

    def latest_result(self, work_task_id: str) -> dict[str, Any] | None:
        return self._latest_for_task(work_task_id, "RESULT")

    def _latest_for_task(self, work_task_id: str, kind: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM work_checkpoints WHERE work_task_id = ? AND kind = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (work_task_id, kind)).fetchone()
        return _row_to_checkpoint(row) if row else None

    def checkpoints_for(self, work_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return [_row_to_checkpoint(row) for row in self._connection.execute(
            "SELECT * FROM work_checkpoints WHERE work_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (work_id, max(1, min(int(limit), 1000))))]

    def _checkpoint_by_idempotency_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM work_checkpoints WHERE idempotency_key = ?",
            (idempotency_key,)).fetchone()
        return _row_to_checkpoint(row) if row else None

    # -- approvals -----------------------------------------------------------

    def request_approval(self, work_id: str, *, kind: str, summary: str,
                         requested_by: str, work_task_id: str | None = None,
                         detail: str | None = None) -> dict[str, Any]:
        """Open a gate. Idempotent per (work, task, kind): asking twice for the
        same thing returns the open request rather than stacking gates a human
        then has to clear one by one."""
        if self.get_run(work_id) is None:
            raise WorkError(f"unknown work run {work_id!r}")
        existing = self._connection.execute(
            "SELECT * FROM work_approvals WHERE work_id = ? AND kind = ? "
            "AND COALESCE(work_task_id,'') = COALESCE(?,'') AND decision = ?",
            (work_id, kind, work_task_id, APPROVAL_PENDING)).fetchone()
        if existing:
            return dict(existing)
        approval_id = new_id("appr")
        with self._connection:
            self._connection.execute(
                "INSERT INTO work_approvals (approval_id, work_id, work_task_id, kind, "
                "summary, detail, requested_by, requested_at) VALUES (?,?,?,?,?,?,?,?)",
                (approval_id, work_id, work_task_id, kind, summary, detail,
                 requested_by, _now()))
        self.record_event(work_id, work_task_id=work_task_id, kind="approval_requested",
                          summary=summary, detail=kind, actor=requested_by)
        return dict(self._connection.execute(
            "SELECT * FROM work_approvals WHERE approval_id = ?", (approval_id,)).fetchone())

    def decide_approval(self, approval_id: str, *, decision: str, decided_by: str,
                        note: str | None = None) -> dict[str, Any]:
        """Approve or reject. A requester may not decide their own gate.

        That rule is enforced here rather than in a caller because it is the
        entire value of the gate: an agent that can approve what it asked for
        has not been gated at all.
        """
        if decision not in (APPROVAL_APPROVED, APPROVAL_REJECTED):
            raise WorkError(f"decision must be {APPROVAL_APPROVED} or {APPROVAL_REJECTED}")
        row = self._connection.execute(
            "SELECT * FROM work_approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if row is None:
            raise WorkError(f"unknown approval {approval_id!r}")
        if row["decision"] != APPROVAL_PENDING:
            raise WorkError(f"{approval_id} was already {row['decision']}")
        if not decided_by or not str(decided_by).strip():
            raise WorkError("decided_by is required: an approval must name a decider")
        if str(decided_by).strip() == str(row["requested_by"]).strip():
            raise WorkError(
                f"{decided_by!r} requested this approval and may not also decide it")
        with self._connection:
            self._connection.execute(
                "UPDATE work_approvals SET decision = ?, decided_by = ?, decided_at = ?, "
                "decision_note = ? WHERE approval_id = ?",
                (decision, decided_by, _now(), note, approval_id))
        self.record_event(row["work_id"], work_task_id=row["work_task_id"],
                          kind="approval_decided", summary=f"{decision}: {row['summary']}",
                          detail=note, actor=decided_by)
        return dict(self._connection.execute(
            "SELECT * FROM work_approvals WHERE approval_id = ?", (approval_id,)).fetchone())

    def approvals_for(self, work_id: str, *, pending_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM work_approvals WHERE work_id = ?"
        args: list[Any] = [work_id]
        if pending_only:
            sql += " AND decision = ?"
            args.append(APPROVAL_PENDING)
        sql += " ORDER BY requested_at"
        return [dict(row) for row in self._connection.execute(sql, args)]

    def pending_approvals(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM work_approvals WHERE decision = ? ORDER BY requested_at LIMIT ?",
            (APPROVAL_PENDING, max(1, min(int(limit), 500))))]

    # -- artifacts + events --------------------------------------------------

    def add_artifact(self, work_id: str, *, kind: str, reference: str,
                     summary: str | None = None,
                     work_task_id: str | None = None) -> dict[str, Any]:
        """Evidence is REFERENCED, never inlined: a path, a commit, a URL, a
        test-run id. Storing content here would turn the work log into a
        second place secrets could come to rest."""
        artifact_id = new_id("art")
        with self._connection:
            self._connection.execute(
                "INSERT INTO work_artifacts (artifact_id, work_id, work_task_id, kind, "
                "reference, summary, created_at) VALUES (?,?,?,?,?,?,?)",
                (artifact_id, work_id, work_task_id, kind, reference, summary, _now()))
        return dict(self._connection.execute(
            "SELECT * FROM work_artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone())

    def artifacts_for(self, work_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM work_artifacts WHERE work_id = ? ORDER BY created_at", (work_id,))]

    def record_event(self, work_id: str, *, kind: str, summary: str,
                     detail: str | None = None, actor: str | None = None,
                     work_task_id: str | None = None) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO work_events (work_id, work_task_id, kind, summary, detail, "
                "actor, created_at) VALUES (?,?,?,?,?,?,?)",
                (work_id, work_task_id, kind, summary, detail, actor, _now()))

    def events_for(self, work_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        return [dict(row) for row in self._connection.execute(
            "SELECT * FROM work_events WHERE work_id = ? ORDER BY id DESC LIMIT ?",
            (work_id, max(1, min(int(limit), 1000))))]

    def close(self) -> None:
        self._connection.close()


def _row_to_run(row: sqlite3.Row) -> WorkRun:
    return WorkRun(
        work_id=row["work_id"], title=row["title"], goal=row["goal"], state=row["state"],
        project_id=row["project_id"], lane=row["lane"],
        done_criteria=tuple(json.loads(row["done_criteria"] or "[]")),
        created_by=row["created_by"], created_at=row["created_at"],
        updated_at=row["updated_at"], paused_reason=row["paused_reason"],
        failure_reason=row["failure_reason"],
        metadata=json.loads(row["metadata"] or "{}"))


def _row_to_checkpoint(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for field_name in ("completed", "remaining", "blockers", "changed_files", "evidence"):
        result[field_name] = json.loads(result[field_name])
    return result
