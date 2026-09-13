"""Work Runtime V1 -- the binding layer.

Everything here composes primitives that already exist. It creates a work
run, writes its plan as real queue tasks on a `-work` lane, reads their state
back from the queue, decides whether the outcome contract is satisfied, and
opens approval gates. It owns no queue, no dispatcher and no second state
machine for tasks.

THE TWO RULES THAT MATTER

Only `-work` sessions are driven. Every path that could cause a dispatch
consults `work_eligibility.evaluate` and refuses otherwise -- see that
module for why the check lives in one place.

A run is not done because a worker said so. `evaluate_contract` requires
every REQUIRED task to be genuinely complete in the QUEUE's own record, no
blockers, and no pending approvals. A model writing "STATUS: done" moves
nothing on its own.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import work_store as ws
from .work_eligibility import Eligibility, evaluate as evaluate_eligibility, is_work_session

_LOGGER = logging.getLogger(__name__)

# Queue statuses that mean a task is genuinely finished, and the ones that
# mean it is stuck. Read from queue_store rather than restated, so a change
# there cannot silently desynchronise the contract.
try:  # pragma: no cover - import shape only
    from .queue_store import (BLOCKED as Q_BLOCKED, CANCELLED as Q_CANCELLED,
                              COMPLETED as Q_COMPLETED, FAILED as Q_FAILED,
                              SKIPPED as Q_SKIPPED)
except Exception:  # pragma: no cover - defensive
    Q_COMPLETED, Q_SKIPPED, Q_CANCELLED, Q_BLOCKED, Q_FAILED = (
        "COMPLETED", "SKIPPED", "CANCELLED", "BLOCKED", "FAILED")

DONE_STATUSES = frozenset({Q_COMPLETED})
# SKIPPED counts as resolved but NOT as delivered: a skipped required task
# means the deliverable is incomplete, which is exactly the distinction the
# outcome contract exists to make.
RESOLVED_STATUSES = frozenset({Q_COMPLETED, Q_SKIPPED, Q_CANCELLED})
STUCK_STATUSES = frozenset({Q_BLOCKED, Q_FAILED})

# Kinds of approval gate. Named so a policy can require one without the
# caller inventing a string.
APPROVAL_DEPLOY = "production_deploy"
APPROVAL_DESTRUCTIVE = "destructive_change"
APPROVAL_CREDENTIAL = "credential_change"
APPROVAL_CUSTOM = "custom"
HIGH_RISK_APPROVALS = (APPROVAL_DEPLOY, APPROVAL_DESTRUCTIVE, APPROVAL_CREDENTIAL)


@dataclass(frozen=True)
class Progress:
    """Computed from task weights and the QUEUE's own states.

    Never parsed out of anything a model wrote: a percentage an agent
    reports about itself is a claim, not a measurement.
    """

    total_weight: float
    done_weight: float
    percent: int
    total_tasks: int
    done_tasks: int
    blocked_tasks: int

    def as_dict(self) -> dict[str, Any]:
        return {"percent": self.percent, "total_weight": round(self.total_weight, 2),
                "done_weight": round(self.done_weight, 2), "total_tasks": self.total_tasks,
                "done_tasks": self.done_tasks, "blocked_tasks": self.blocked_tasks}


class WorkService:
    def __init__(self, store: ws.WorkStore, *, queue: Any = None, controller: Any = None,
                 fleet: Any = None) -> None:
        self.store = store
        self.queue = queue
        self.controller = controller
        self.fleet = fleet

    # -- creation and planning ----------------------------------------------

    def create(self, *, title: str, goal: str, lane: str, project_id: str | None = None,
               done_criteria: Sequence[str] = (), created_by: str | None = None,
               tasks: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
        """Create a run on a `-work` lane, optionally with its initial plan.

        The lane is validated by NAME here, before anything is written: a run
        bound to an ordinary session would be a durable record inviting every
        later scheduling decision to do the wrong thing.
        """
        if not is_work_session(lane):
            return {"error": "LANE_NOT_A_WORK_SESSION", "lane": lane,
                    "detail": (f"{lane!r} does not end in '-work'. Work runs are only "
                               f"created on opt-in work sessions; ordinary sessions keep "
                               f"their existing behaviour untouched.")}
        run = self.store.create_run(title=title, goal=goal, lane=lane, project_id=project_id,
                                    done_criteria=done_criteria, created_by=created_by)
        added: list[dict[str, Any]] = []
        if tasks:
            plan = self.plan(run.work_id, tasks, actor=created_by)
            if plan.get("error"):
                return plan
            added = plan["tasks"]
        return {"work": self.store.get_run(run.work_id).as_dict(), "tasks": added}

    def plan(self, work_id: str, tasks: Sequence[dict[str, Any]], *,
             actor: str | None = None) -> dict[str, Any]:
        """Write the plan as REAL queue tasks on the run's lane.

        This is the whole reuse decision made concrete: the plan is not a
        parallel task table, it is rows in the queue that already knows how to
        order, gate, dispatch and recover them. What work_store keeps is the
        pointer plus per-task weight.
        """
        run = self.store.get_run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        if run.state in ws.TERMINAL_WORK_STATES:
            return {"error": "WORK_ALREADY_FINISHED", "work_id": work_id, "state": run.state}
        if not tasks:
            return {"error": "TASKS_REQUIRED", "work_id": work_id}
        if self.queue is None:
            return {"error": "QUEUE_UNAVAILABLE", "work_id": work_id}

        if run.state == ws.DRAFT:
            self.store.transition_run(work_id, ws.PLANNING, actor=actor)

        created: list[dict[str, Any]] = []
        for entry in tasks:
            prompt = str(entry.get("prompt") or "").strip()
            title = str(entry.get("title") or prompt[:60] or "task").strip()
            if not prompt:
                return {"error": "TASK_PROMPT_REQUIRED", "task": entry}
            work_task = self.store.add_task(
                work_id, title=title, lane=run.lane,
                weight=float(entry.get("weight") or 1.0),
                required=bool(entry.get("required", True)))
            # The prompt is stored VERBATIM by the queue -- this layer adds
            # no wrapper and rewrites nothing, matching queue_service's own
            # rule about never rewriting a caller's business request.
            result = self.queue.enqueue(
                run.lane, prompt, title=title,
                priority=int(entry.get("priority") or 0),
                metadata={"work_id": work_id, "work_task_id": work_task["work_task_id"]})
            if result.get("error"):
                self.store.record_event(work_id, kind="enqueue_failed",
                                        summary=f"{title}: {result['error']}",
                                        work_task_id=work_task["work_task_id"])
                return {"error": result["error"], "work_id": work_id, "task": title}
            self.store.bind_queue_task(work_task["work_task_id"], result["task_id"])
            created.append({**self.store.get_task(work_task["work_task_id"]),
                            "queue_position": result.get("queue_position")})

        if self.store.get_run(work_id).state == ws.PLANNING:
            self.store.transition_run(work_id, ws.READY, actor=actor)
        self.store.record_event(work_id, kind="planned",
                                summary=f"{len(created)} task(s) queued on {run.lane}",
                                actor=actor)
        return {"work_id": work_id, "tasks": created}

    # -- reading -------------------------------------------------------------

    def _queue_states(self, work_id: str) -> dict[str, dict[str, Any]]:
        """Current queue state for every bound task, keyed by work_task_id.

        One read of the lane rather than one per task: the queue is the
        source of truth and asking it N times per status call would make the
        UI's refresh cost grow with the plan.
        """
        run = self.store.get_run(work_id)
        if run is None or self.queue is None or not run.lane:
            return {}
        try:
            listing = self.queue.status(run.lane)
        except Exception:  # noqa: BLE001 -- a status read never raises upward
            _LOGGER.exception("work: queue status unavailable for %s", run.lane)
            return {}
        # The queue's own API is asymmetric on purpose-but-unhelpfully: an
        # enqueue result returns `task_id`, while a lane listing returns the
        # same value as `id`. Accept both rather than depending on which
        # shape a given call happens to produce.
        by_queue_id = {(t.get("id") or t.get("task_id")): t
                       for t in (listing.get("tasks") or [])}
        out: dict[str, dict[str, Any]] = {}
        for task in self.store.tasks_for(work_id):
            queue_task = by_queue_id.get(task.get("queue_task_id"))
            if queue_task:
                out[task["work_task_id"]] = queue_task
        return out

    def progress(self, work_id: str) -> Progress:
        tasks = self.store.tasks_for(work_id)
        states = self._queue_states(work_id)
        total = done = 0.0
        done_count = blocked_count = 0
        for task in tasks:
            weight = float(task.get("weight") or 1.0)
            total += weight
            status = str((states.get(task["work_task_id"]) or {}).get("status") or "")
            if status in DONE_STATUSES:
                done += weight
                done_count += 1
            elif status in STUCK_STATUSES:
                blocked_count += 1
        percent = int(round((done / total) * 100)) if total else 0
        return Progress(total_weight=total, done_weight=done, percent=percent,
                        total_tasks=len(tasks), done_tasks=done_count,
                        blocked_tasks=blocked_count)

    def evaluate_contract(self, work_id: str) -> dict[str, Any]:
        """Is this run actually done? Structured, not self-reported.

        A run is COMPLETE only when every REQUIRED task is COMPLETED in the
        queue's own record, nothing is blocked or failed, and no approval is
        still pending. Each unmet condition is named, because "not done" with
        no reason is the answer that makes an operator distrust the whole
        screen.
        """
        run = self.store.get_run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        tasks = self.store.tasks_for(work_id)
        states = self._queue_states(work_id)
        unmet: list[dict[str, Any]] = []

        if not tasks:
            unmet.append({"reason": "NO_PLAN", "detail": "the run has no tasks"})

        for task in tasks:
            queue_task = states.get(task["work_task_id"])
            status = str((queue_task or {}).get("status") or "UNBOUND")
            if not task.get("required"):
                continue
            if status not in DONE_STATUSES:
                unmet.append({"reason": "REQUIRED_TASK_NOT_COMPLETE",
                              "work_task_id": task["work_task_id"],
                              "title": task.get("title"), "queue_status": status})

        blocked = [t for t in tasks
                   if str((states.get(t["work_task_id"]) or {}).get("status") or "")
                   in STUCK_STATUSES]
        for task in blocked:
            unmet.append({"reason": "TASK_BLOCKED", "work_task_id": task["work_task_id"],
                          "title": task.get("title")})

        pending = self.store.approvals_for(work_id, pending_only=True)
        for approval in pending:
            unmet.append({"reason": "APPROVAL_PENDING", "approval_id": approval["approval_id"],
                          "summary": approval["summary"]})

        return {"work_id": work_id, "satisfied": not unmet, "unmet": unmet,
                "done_criteria": list(run.done_criteria),
                "progress": self.progress(work_id).as_dict()}

    def status(self, work_id: str) -> dict[str, Any]:
        run = self.store.get_run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        states = self._queue_states(work_id)
        tasks = []
        for task in self.store.tasks_for(work_id):
            queue_task = states.get(task["work_task_id"]) or {}
            tasks.append({**task,
                          "queue_status": queue_task.get("status"),
                          "queue_position": queue_task.get("queue_position"),
                          "attempts": queue_task.get("attempt_count"),
                          "depends_on": queue_task.get("depends_on") or []})
        contract = self.evaluate_contract(work_id)
        return {
            "work": run.as_dict(),
            "progress": self.progress(work_id).as_dict(),
            "tasks": tasks,
            "approvals": self.store.approvals_for(work_id),
            "pending_approvals": self.store.approvals_for(work_id, pending_only=True),
            "artifacts": self.store.artifacts_for(work_id),
            "events": self.store.events_for(work_id, limit=50),
            "contract": {"satisfied": contract["satisfied"], "unmet": contract["unmet"]},
            "lane_is_work_session": is_work_session(run.lane),
        }

    def list_runs(self, **kwargs: Any) -> dict[str, Any]:
        runs = self.store.list_runs(**kwargs)
        return {"works": [{**run.as_dict(), "progress": self.progress(run.work_id).as_dict()}
                          for run in runs]}

    # -- control -------------------------------------------------------------

    def control(self, work_id: str, action: str, *, actor: str | None = None,
                reason: str | None = None) -> dict[str, Any]:
        """pause / resume / cancel / block / fail, with the queue kept in step.

        Pause stops NEW dispatch; it deliberately does not kill a worker that
        is mid-task. Cancel stops the lane but does not reach out and destroy
        an external process -- a half-finished build is the worker's to wind
        down, not this layer's to kill.
        """
        run = self.store.get_run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        action = (action or "").casefold()
        mapping = {"pause": ws.PAUSED, "resume": ws.READY, "cancel": ws.CANCELLED,
                   "block": ws.BLOCKED, "fail": ws.FAILED}
        if action not in mapping:
            return {"error": "UNKNOWN_ACTION", "action": action,
                    "known": sorted(mapping)}
        try:
            updated = self.store.transition_run(work_id, mapping[action], actor=actor,
                                                reason=reason)
        except ws.WorkError as exc:
            return {"error": "INVALID_TRANSITION", "detail": str(exc)}
        if self.queue is not None and run.lane:
            try:
                if action == "pause":
                    self.queue.pause(run.lane, reason=reason or f"work {work_id} paused")
                elif action == "resume":
                    self.queue.resume(run.lane)
            except Exception:  # noqa: BLE001 -- the work state is still correct
                _LOGGER.exception("work: queue %s failed for lane %s", action, run.lane)
                self.store.record_event(work_id, kind="queue_control_failed",
                                        summary=f"{action} on {run.lane} failed", actor=actor)
        return {"work": updated.as_dict()}

    # -- approvals -----------------------------------------------------------

    def request_approval(self, work_id: str, *, kind: str, summary: str,
                         requested_by: str, work_task_id: str | None = None,
                         detail: str | None = None) -> dict[str, Any]:
        run = self.store.get_run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        approval = self.store.request_approval(
            work_id, kind=kind, summary=summary, requested_by=requested_by,
            work_task_id=work_task_id, detail=detail)
        # The RUN waits, but only if it was running: independent branches are
        # allowed to continue, which is why the gate lives on the approval
        # record rather than on the whole lane.
        if run.state in (ws.RUNNING, ws.VERIFYING):
            try:
                self.store.transition_run(work_id, ws.WAITING_APPROVAL, actor=requested_by,
                                          reason=summary)
            except ws.WorkError:
                pass
        return {"approval": approval}

    def decide_approval(self, approval_id: str, *, decision: str, decided_by: str,
                        note: str | None = None) -> dict[str, Any]:
        try:
            approval = self.store.decide_approval(approval_id, decision=decision,
                                                  decided_by=decided_by, note=note)
        except ws.WorkError as exc:
            return {"error": "APPROVAL_REFUSED", "detail": str(exc)}
        work_id = approval["work_id"]
        run = self.store.get_run(work_id)
        if run and run.state == ws.WAITING_APPROVAL:
            still_pending = self.store.approvals_for(work_id, pending_only=True)
            if not still_pending:
                target = ws.RUNNING if decision == ws.APPROVAL_APPROVED else ws.BLOCKED
                try:
                    self.store.transition_run(work_id, target, actor=decided_by,
                                              reason=None if target == ws.RUNNING
                                              else "approval rejected")
                except ws.WorkError:
                    pass
        return {"approval": approval, "work": (self.store.get_run(work_id) or run).as_dict()}

    # -- observability ---------------------------------------------------------

    def observability(self) -> dict[str, Any]:
        """The counters an operator needs to answer "is Work healthy".

        Deliberately cheap: counts over the runs this store already holds,
        no fan-out to any node. A health view that needs the fleet to be up
        is useless exactly when it is needed.
        """
        runs = self.store.list_runs(include_terminal=False, limit=500)
        by_state: dict[str, int] = {}
        queued = runnable = blocked = 0
        for run in runs:
            by_state[run.state] = by_state.get(run.state, 0) + 1
            progress = self.progress(run.work_id)
            blocked += progress.blocked_tasks
            queued += max(0, progress.total_tasks - progress.done_tasks)
            if run.state in (ws.READY, ws.RUNNING):
                runnable += 1
        pending = self.store.pending_approvals()
        return {"active_runs": len(runs), "by_state": by_state,
                "queued_tasks": queued, "runnable_runs": runnable,
                "blocked_tasks": blocked,
                "waiting_approvals": len(pending),
                "approvals": [{"approval_id": a["approval_id"], "work_id": a["work_id"],
                               "kind": a["kind"], "summary": a["summary"],
                               "requested_by": a["requested_by"]} for a in pending[:20]]}

    def readiness(self, *, loop_status: dict[str, Any] | None = None) -> dict[str, Any]:
        """PASS/WARN/FAIL for the Work runtime.

        A disabled coordinator is PASS, not a warning: Work is opt-in, and a
        deployment that has not turned it on is in a correct state, not a
        degraded one. Reporting otherwise would train an operator to ignore
        this section on every box that does not use the feature.
        """
        checks: list[dict[str, Any]] = []
        counters = self.observability()

        if loop_status is None:
            checks.append({"check": "work_coordinator", "status": "PASS",
                           "summary": "Work runtime is not enabled on this controller",
                           "evidence": {"enabled": False}})
        elif not loop_status.get("enabled"):
            checks.append({"check": "work_coordinator", "status": "PASS",
                           "summary": "Work coordinator disabled by config (opt-in feature)",
                           "evidence": loop_status})
        elif not loop_status.get("running"):
            checks.append({"check": "work_coordinator", "status": "FAIL",
                           "summary": "Work coordinator is enabled but not running; "
                                      "runs will not advance",
                           "evidence": loop_status})
        elif loop_status.get("last_error"):
            checks.append({"check": "work_coordinator", "status": "WARN",
                           "summary": f"last tick reported {loop_status['last_error']}",
                           "evidence": loop_status})
        else:
            checks.append({"check": "work_coordinator", "status": "PASS",
                           "summary": f"ticking every {loop_status.get('interval_seconds')}s",
                           "evidence": {"ticks": loop_status.get("ticks"),
                                        "age_seconds": loop_status.get("age_seconds")}})

        # A waiting approval is not a fault -- it is the system correctly
        # asking a human. WARN so it is visible, never FAIL.
        checks.append({
            "check": "work_waiting_approvals",
            "status": "WARN" if counters["waiting_approvals"] else "PASS",
            "summary": (f"{counters['waiting_approvals']} approval(s) waiting on a human"
                        if counters["waiting_approvals"] else "no approval is waiting"),
            "evidence": {"approvals": counters["approvals"]}})
        checks.append({
            "check": "work_blocked_tasks",
            "status": "WARN" if counters["blocked_tasks"] else "PASS",
            "summary": (f"{counters['blocked_tasks']} task(s) blocked"
                        if counters["blocked_tasks"] else "no task is blocked"),
            "evidence": {"blocked_tasks": counters["blocked_tasks"]}})

        worst = ("FAIL" if any(c["status"] == "FAIL" for c in checks)
                 else "WARN" if any(c["status"] == "WARN" for c in checks) else "PASS")
        return {"status": worst, "checks": checks, "counters": counters}

    # -- worker eligibility ---------------------------------------------------

    def eligible_workers(self, *, sessions: Iterable[dict[str, Any]],
                         nodes: dict[str, dict[str, Any]] | None = None,
                         statuses: dict[str, dict[str, Any]] | None = None,
                         ) -> list[dict[str, Any]]:
        """Every session the Work runtime may drive, with a reason for each
        one it may not.

        Returns the rejections too, on purpose: an operator asking "why is my
        work not running" needs to see that the candidate was skipped and
        why, not an empty list.
        """
        out: list[dict[str, Any]] = []
        for row in sessions:
            name = row.get("name") or row.get("session")
            if not name:
                continue
            node_id = row.get("node_id")
            verdict = evaluate_eligibility(
                name,
                status=(statuses or {}).get(name, {"exists": True}),
                node=(nodes or {}).get(node_id) if node_id else None,
                input_allowed=row.get("input_allowed"),
                input_denied_reason=row.get("input_denied_reason"))
            out.append({**verdict.as_dict(), "node_id": node_id or verdict.node_id})
        return out
