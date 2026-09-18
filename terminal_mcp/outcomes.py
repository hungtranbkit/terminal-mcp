"""Orchestration V1 -- the OUTCOME layer.

WHAT WAS MISSING
----------------
Before this, the hierarchy was exactly two levels, welded 1:1:

    backlog item  ->  queue task

`backlog_service.dispatch` created ONE queue task per item and then refused
ever to do it again (`ALREADY_DISPATCHED`). So there was no way to express
"this deliverable took five tasks", and therefore no way to ask whether the
DELIVERABLE was done -- only whether individual tasks were. A project report
could say "12 tasks completed" and still not answer "did the overview screen
ship?".

An OUTCOME is that missing noun: one user-visible thing, with acceptance
criteria, that N tasks deliver.

    backlog item  ->  outcome  ->  N tasks  ->  verify -> merge -> preview

THE RULE THAT MAKES THIS WORTH HAVING
--------------------------------------
**An outcome is NOT done because its children are done.** That is the single
most important behaviour in this module, and the reason it is not just a
`parent_task_id` column.

Children completing means the work someone *planned* finished. It says
nothing about whether the thing a user can see actually works. Every real
project has the failure where five tasks pass, every test is green, and the
screen is still broken -- because nobody wrote the task that would have
caught it. Rolling an outcome to DONE off its children would encode exactly
that mistake as a feature.

So `complete()` requires evidence **per acceptance criterion**, and refuses
while any child task is still open. Children-done is a NECESSARY condition
that this module enforces; it is never a SUFFICIENT one.

RELATIONSHIP TO THE EXISTING EVIDENCE GATES
-------------------------------------------
This is the third evidence gate in the codebase and deliberately the
strictest, because it guards the largest claim:

  - `queue_store.mark_completed_with_evidence` -- task-level, requires
    non-empty evidence.
  - `verify_queue.complete` -- job-level, requires evidence that is more
    than a self-report and is not self-contradicted.
  - here -- outcome-level, requires evidence NAMED AGAINST EACH acceptance
    criterion, so "it works" cannot stand in for "each thing we said it must
    do, does".

It reuses `verify_queue.evidence_verdict` for the per-criterion check rather
than inventing a fourth notion of what evidence is.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Sequence

from .queue_store import (
    COMPLETED, TERMINAL_STATUSES, QueueStore, iso_now, new_task_id,
)
from .verify_queue import evidence_verdict, redact_structure

OUTCOME_OPEN = "OPEN"
OUTCOME_IN_PROGRESS = "IN_PROGRESS"
OUTCOME_AWAITING_ACCEPTANCE = "AWAITING_ACCEPTANCE"
"""Every child task is finished, but the acceptance criteria have not been
evidenced. This state exists precisely to make the gap visible: it is the
state a naive implementation would have called DONE."""
OUTCOME_DONE = "DONE"
OUTCOME_BLOCKED = "BLOCKED"
OUTCOME_CANCELLED = "CANCELLED"

ALL_OUTCOME_STATUSES = (OUTCOME_OPEN, OUTCOME_IN_PROGRESS, OUTCOME_AWAITING_ACCEPTANCE,
                        OUTCOME_DONE, OUTCOME_BLOCKED, OUTCOME_CANCELLED)
OPEN_OUTCOME_STATUSES = (OUTCOME_OPEN, OUTCOME_IN_PROGRESS, OUTCOME_AWAITING_ACCEPTANCE,
                         OUTCOME_BLOCKED)
TERMINAL_OUTCOME_STATUSES = (OUTCOME_DONE, OUTCOME_CANCELLED)

OUTCOME_TRANSITIONS: dict[str, frozenset[str]] = {
    OUTCOME_OPEN: frozenset({OUTCOME_IN_PROGRESS, OUTCOME_AWAITING_ACCEPTANCE,
                             OUTCOME_BLOCKED, OUTCOME_CANCELLED}),
    OUTCOME_IN_PROGRESS: frozenset({OUTCOME_AWAITING_ACCEPTANCE, OUTCOME_OPEN,
                                    OUTCOME_BLOCKED, OUTCOME_CANCELLED}),
    OUTCOME_AWAITING_ACCEPTANCE: frozenset({OUTCOME_DONE, OUTCOME_IN_PROGRESS,
                                            OUTCOME_BLOCKED, OUTCOME_CANCELLED}),
    OUTCOME_BLOCKED: frozenset({OUTCOME_OPEN, OUTCOME_IN_PROGRESS, OUTCOME_CANCELLED}),
    OUTCOME_DONE: frozenset(),
    OUTCOME_CANCELLED: frozenset(),
}


class OutcomeError(ValueError):
    """A refused outcome operation -- an invalid transition, or a DONE that
    the acceptance evidence does not support."""


@dataclass(frozen=True)
class Outcome:
    id: str
    project_id: str
    title: str
    status: str
    created_at: str
    updated_at: str
    backlog_id: str | None = None
    description: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    priority: str = "P2"
    evidence: dict[str, Any] = field(default_factory=dict)
    blocked_reason: str | None = None
    history: tuple[dict[str, Any], ...] = ()
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Outcome":
        return cls(
            id=row["id"], project_id=row["project_id"], title=row["title"],
            status=row["status"], created_at=row["created_at"], updated_at=row["updated_at"],
            backlog_id=row["backlog_id"], description=row["description"] or "",
            acceptance_criteria=tuple(json.loads(row["acceptance_criteria"] or "[]")),
            priority=row["priority"], evidence=json.loads(row["evidence"] or "{}"),
            blocked_reason=row["blocked_reason"],
            history=tuple(json.loads(row["history"] or "[]")),
            completed_at=row["completed_at"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "project_id": self.project_id, "backlog_id": self.backlog_id,
            "title": self.title, "description": self.description,
            "acceptance_criteria": list(self.acceptance_criteria), "status": self.status,
            "priority": self.priority, "evidence": self.evidence,
            "blocked_reason": self.blocked_reason, "history": list(self.history),
            "created_at": self.created_at, "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


class OutcomeStore:
    """Outcomes over the SAME database queue_store owns, so an outcome and
    the tasks rolling up into it can be read and written in one transaction
    -- a cross-store join could observe a torn state mid-transition."""

    def __init__(self, store: QueueStore | None = None) -> None:
        self.store = store or QueueStore()

    # -- writes ----------------------------------------------------------

    def create(self, project_id: str, title: str, *, acceptance_criteria: Sequence[str],
               description: str = "", backlog_id: str | None = None, priority: str = "P2",
               actor: str = "mcp") -> Outcome:
        """An outcome REQUIRES acceptance criteria at creation.

        Not a nicety: `complete()` demands evidence per criterion, so an
        outcome with none could be completed with an empty payload -- the
        exact hole this layer exists to close. Refusing at creation is the
        only place the omission is still cheap to fix."""
        project_id = str(project_id or "").strip()
        title = str(title or "").strip()
        criteria = [str(c).strip() for c in (acceptance_criteria or ()) if str(c).strip()]
        if not project_id:
            raise OutcomeError("project_id is required")
        if not title:
            raise OutcomeError("title is required")
        if not criteria:
            raise OutcomeError(
                "an outcome requires at least one acceptance criterion -- completion is gated "
                "per criterion, so an outcome without any could be completed with no evidence")
        now = iso_now()
        outcome_id = new_task_id()
        with self.store._connection() as connection:
            connection.execute(
                "INSERT INTO outcomes (id, project_id, backlog_id, title, description, "
                "acceptance_criteria, status, priority, history, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (outcome_id, project_id, backlog_id, title, description,
                 json.dumps(criteria), OUTCOME_OPEN, priority,
                 json.dumps([{"at": now, "event": "CREATED", "actor": actor}]), now, now))
        return self.get(outcome_id)  # type: ignore[return-value]

    def attach_task(self, outcome_id: str, task_id: str, *, actor: str = "mcp") -> dict[str, Any]:
        """Link an EXISTING task to an outcome. Many tasks per outcome --
        that is the whole point; the previous model allowed exactly one."""
        with self.store._connection() as connection:
            outcome = connection.execute("SELECT * FROM outcomes WHERE id = ?",
                                         (outcome_id,)).fetchone()
            if outcome is None:
                raise OutcomeError(f"no such outcome: {outcome_id}")
            if outcome["status"] in TERMINAL_OUTCOME_STATUSES:
                raise OutcomeError(f"outcome is {outcome['status']} -- cannot attach more work")
            task = connection.execute("SELECT id, project_id FROM queue_tasks WHERE id = ?",
                                      (task_id,)).fetchone()
            if task is None:
                raise OutcomeError(f"no such task: {task_id}")
            connection.execute("UPDATE queue_tasks SET outcome_id = ?, updated_at = ? WHERE id = ?",
                               (outcome_id, iso_now(), task_id))
            # A task adopted into an outcome inherits the outcome's project
            # when it had none, so project views see it immediately.
            if not task["project_id"]:
                connection.execute("UPDATE queue_tasks SET project_id = ? WHERE id = ?",
                                   (outcome["project_id"], task_id))
            self._append_history_locked(connection, outcome, actor=actor,
                                        event="TASK_ATTACHED", task_id=task_id)
        return {"outcome_id": outcome_id, "task_id": task_id, "attached": True}

    def refresh_status(self, outcome_id: str, *, actor: str = "rollup") -> Outcome:
        """Derive OPEN / IN_PROGRESS / AWAITING_ACCEPTANCE from the child
        tasks. Deliberately CANNOT reach DONE -- see complete()."""
        outcome = self.get(outcome_id)
        if outcome is None:
            raise OutcomeError(f"no such outcome: {outcome_id}")
        if outcome.status in TERMINAL_OUTCOME_STATUSES or outcome.status == OUTCOME_BLOCKED:
            return outcome
        progress = self.progress(outcome_id)
        if progress["total"] == 0:
            target = OUTCOME_OPEN
        elif progress["open"] == 0:
            target = OUTCOME_AWAITING_ACCEPTANCE
        else:
            target = OUTCOME_IN_PROGRESS
        if target == outcome.status:
            return outcome
        return self._transition(outcome_id, target, actor=actor,
                                reason=f"rollup: {progress['completed']}/{progress['total']} tasks complete")

    def complete(self, outcome_id: str, *, evidence: dict[str, Any],
                 actor: str = "mcp") -> dict[str, Any]:
        """DONE -- the only route to it, gated on evidence PER ACCEPTANCE
        CRITERION.

        Two independent conditions, both required:

        1. NO CHILD TASK IS STILL OPEN. Necessary, never sufficient.
        2. EVERY acceptance criterion has its own evidence entry that
           passes verify_queue.evidence_verdict. `evidence` is a mapping of
           criterion -> evidence object; a criterion with no entry, or with
           an entry that is only a self-report, refuses the whole call.

        Returns a structured refusal rather than raising, so a caller that
        supplied thin evidence is told exactly which criterion is missing
        and can fix it -- the outcome stays completable meanwhile."""
        outcome = self.get(outcome_id)
        if outcome is None:
            return {"ok": False, "error": "OUTCOME_NOT_FOUND", "outcome_id": outcome_id}
        if outcome.status == OUTCOME_DONE:
            return {"ok": False, "error": "ALREADY_DONE", "outcome_id": outcome_id}
        if outcome.status == OUTCOME_CANCELLED:
            return {"ok": False, "error": "OUTCOME_CANCELLED", "outcome_id": outcome_id}

        progress = self.progress(outcome_id)
        if progress["open"] > 0:
            return {"ok": False, "error": "TASKS_STILL_OPEN", "outcome_id": outcome_id,
                    "open_tasks": progress["open_task_ids"],
                    "reason": f"{progress['open']} of {progress['total']} child tasks are not "
                              f"finished -- children being done is necessary before an outcome "
                              f"can be accepted (it is never sufficient on its own)"}

        if not isinstance(evidence, dict):
            return {"ok": False, "error": "EVIDENCE_REQUIRED", "outcome_id": outcome_id,
                    "reason": "evidence must map each acceptance criterion to its own evidence"}
        missing, rejected = [], {}
        for criterion in outcome.acceptance_criteria:
            entry = evidence.get(criterion)
            if entry is None:
                missing.append(criterion)
                continue
            accepted, why = evidence_verdict(entry)
            if not accepted:
                rejected[criterion] = why
        if missing or rejected:
            return {"ok": False, "error": "ACCEPTANCE_EVIDENCE_REQUIRED", "outcome_id": outcome_id,
                    "missing_criteria": missing, "rejected_criteria": rejected,
                    "reason": "an outcome is not done because its tasks are done -- every "
                              "acceptance criterion needs its own checkable evidence"}

        clean = redact_structure(evidence)
        updated = self._transition(outcome_id, OUTCOME_DONE, actor=actor,
                                   reason="all acceptance criteria evidenced",
                                   fields={"evidence": json.dumps(clean),
                                           "completed_at": iso_now()})
        return {"ok": True, "outcome": updated.to_dict()}

    def block(self, outcome_id: str, *, reason: str, actor: str = "mcp") -> Outcome:
        return self._transition(outcome_id, OUTCOME_BLOCKED, actor=actor, reason=reason,
                                fields={"blocked_reason": reason})

    def unblock(self, outcome_id: str, *, actor: str = "mcp") -> Outcome:
        self._transition(outcome_id, OUTCOME_OPEN, actor=actor, reason="unblocked",
                         fields={"blocked_reason": None})
        return self.refresh_status(outcome_id, actor=actor)

    def cancel(self, outcome_id: str, *, reason: str, actor: str = "mcp") -> Outcome:
        return self._transition(outcome_id, OUTCOME_CANCELLED, actor=actor, reason=reason)

    # -- reads -----------------------------------------------------------

    def get(self, outcome_id: str) -> Outcome | None:
        with self.store._connection() as connection:
            row = connection.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
        return Outcome.from_row(row) if row is not None else None

    def list_outcomes(self, *, project_id: str | None = None, status: str | None = None,
                      backlog_id: str | None = None, limit: int = 200) -> list[Outcome]:
        clauses, params = [], []
        for column, value in (("project_id", project_id), ("status", status),
                              ("backlog_id", backlog_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self.store._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM outcomes {where}ORDER BY priority, created_at LIMIT ?",
                (*params, limit)).fetchall()
        return [Outcome.from_row(row) for row in rows]

    def tasks_for(self, outcome_id: str) -> list[Any]:
        from .queue_store import QueueTask
        with self.store._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM queue_tasks WHERE outcome_id = ? ORDER BY position", (outcome_id,)
            ).fetchall()
        return [QueueTask.from_row(row) for row in rows]

    def progress(self, outcome_id: str) -> dict[str, Any]:
        """Child-task rollup. Reported as a fraction of tasks, and named
        `progress` rather than `completion` on purpose: it measures work
        done, not whether the outcome is achieved."""
        tasks = self.tasks_for(outcome_id)
        open_ids = [t.id for t in tasks if t.status not in TERMINAL_STATUSES]
        by_status: dict[str, int] = {}
        for task in tasks:
            by_status[task.status] = by_status.get(task.status, 0) + 1
        completed = by_status.get(COMPLETED, 0)
        return {"total": len(tasks), "completed": completed, "open": len(open_ids),
                "open_task_ids": open_ids, "by_status": by_status,
                "percent": round(100.0 * completed / len(tasks), 1) if tasks else 0.0}

    def trace(self, outcome_id: str) -> dict[str, Any]:
        """backlog_id -> outcome -> tasks -> workers/branches/evidence, in
        one read. The chain a project report has to be able to walk."""
        outcome = self.get(outcome_id)
        if outcome is None:
            return {"error": "OUTCOME_NOT_FOUND", "outcome_id": outcome_id}
        tasks = self.tasks_for(outcome_id)
        return {
            "outcome": outcome.to_dict(),
            "progress": self.progress(outcome_id),
            "tasks": [{"task_id": t.id, "title": t.title, "status": t.status,
                       "session": t.session, "worker": t.claimed_by,
                       "branch": t.metadata.get("branch"),
                       "commit_sha": t.metadata.get("commit_sha"),
                       "verification_evidence": t.verification_evidence} for t in tasks],
        }

    # -- internals -------------------------------------------------------

    def _transition(self, outcome_id: str, to_status: str, *, actor: str, reason: str | None,
                    fields: dict[str, Any] | None = None) -> Outcome:
        with self.store._connection() as connection:
            row = connection.execute("SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
            if row is None:
                raise OutcomeError(f"no such outcome: {outcome_id}")
            if to_status not in OUTCOME_TRANSITIONS.get(row["status"], frozenset()):
                raise OutcomeError(
                    f"{outcome_id}: {row['status']} -> {to_status} is not a valid outcome transition")
            payload: dict[str, Any] = {"status": to_status, "updated_at": iso_now()}
            payload.update(fields or {})
            payload["history"] = self._history_json(row, actor=actor, event=to_status, reason=reason)
            clause = ", ".join(f"{key} = ?" for key in payload)
            connection.execute(f"UPDATE outcomes SET {clause} WHERE id = ?",
                               (*payload.values(), outcome_id))
            updated = connection.execute("SELECT * FROM outcomes WHERE id = ?",
                                         (outcome_id,)).fetchone()
        return Outcome.from_row(updated)

    def _append_history_locked(self, connection, row, *, actor: str, event: str,
                               reason: str | None = None, **extra: Any) -> None:
        connection.execute("UPDATE outcomes SET history = ?, updated_at = ? WHERE id = ?",
                           (self._history_json(row, actor=actor, event=event, reason=reason, **extra),
                            iso_now(), row["id"]))

    @staticmethod
    def _history_json(row, *, actor: str, event: str, reason: str | None = None,
                      **extra: Any) -> str:
        history = list(json.loads(row["history"] or "[]"))
        entry: dict[str, Any] = {"at": iso_now(), "event": event, "actor": actor}
        if reason:
            entry["reason"] = reason
        entry.update({k: v for k, v in extra.items() if v is not None})
        history.append(entry)
        return json.dumps(history)
