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
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from . import work_store as ws
from .work_eligibility import Eligibility, evaluate as evaluate_eligibility, is_work_session
from .work_policy import load_policy

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
# A task the fleet is actively working: used to say which worker is busy and
# to show a run's running workers without asking each session.
RUNNING_STATUSES = frozenset({"DISPATCHING", "RUNNING", "VERIFYING", "DISPATCH_UNCERTAIN"})
ACTIVE_QUEUE_STATUSES = RUNNING_STATUSES | frozenset({"PRECHECK", "READY"})

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


# A session sitting at one of these is running a shell prompt, not an agent.
# Anything else in `current_command` is a program someone started.
_SHELL_COMMANDS = frozenset({
    "bash", "sh", "zsh", "fish", "dash", "ksh", "tcsh", "csh",
    "pwsh", "powershell", "cmd", "cmd.exe", "powershell.exe", "pwsh.exe",
})
# Session states that mean work is happening right now. WAITING_INPUT counts:
# a session holding a prompt open is mid-task, and sending into it would land
# on whatever question it is asking.
_ACTIVE_SESSION_STATES = frozenset({"RUNNING", "WAITING_INPUT"})


def _occupancy(row: dict[str, Any],
               status: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    """Is something actually running in this session, queue or no queue?

    Two independent signals, either of which is sufficient: a foreground
    command that is not a plain shell, or a session state that means work is
    in flight. Returns the evidence alongside the verdict so a caller never
    has to take the boolean on trust.
    """
    status = status or {}
    # The command can arrive on the session row or on the status payload,
    # depending on which listing the caller had. Neither listing carries it
    # today, which is why occupancy has to be fetched deliberately -- reading
    # only the row is what made every worker look free.
    command = str(row.get("current_command") or status.get("current_command") or "").strip()
    base = command.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    agent_running = bool(base) and base not in _SHELL_COMMANDS
    # terminal_status calls it `state`; terminal_input_context calls it
    # `status`. Accept both rather than depending on which one a caller used.
    state = str(status.get("state") or status.get("status") or "")
    active_state = state in _ACTIVE_SESSION_STATES
    return (agent_running or active_state,
            {"current_command": command or None, "session_state": state or None,
             "agent_running": agent_running, "active_state": active_state})


class WorkService:
    def __init__(self, store: ws.WorkStore, *, queue: Any = None, controller: Any = None,
                 fleet: Any = None, telemetry: Any = None) -> None:
        self.store = store
        self.queue = queue
        self.controller = controller
        self.fleet = fleet
        # Efficiency telemetry recorder, or None. Held, never auto-created:
        # constructing one opens a database, and a service built for a read
        # should not acquire a writer as a side effect. `enable_telemetry`
        # is the deliberate step.
        self.telemetry = telemetry

    # -- efficiency telemetry ------------------------------------------------

    def enable_telemetry(self, *, telemetry_store: Any = None, spec_store: Any = None,
                         clock: Any = None) -> dict[str, Any]:
        """Wire telemetry to the queue this run actually dispatches through.

        After this, a row is opened when the QUEUE dispatches a task and
        closed on the queue's own terminal transition -- no worker has to
        remember to report anything for the lifecycle numbers to exist. It
        composes with whatever sink the store already has (the event bus,
        normally) rather than displacing it.
        """
        from .work_telemetry_runtime import install

        queue_store = getattr(self.queue, "store", None)
        if queue_store is None:
            return {"error": "QUEUE_UNAVAILABLE",
                    "detail": "telemetry follows the queue's own transitions; "
                              "there is no queue store to listen to"}
        if self.telemetry is not None:
            return {"enabled": True, "already": True}
        self.telemetry = install(queue_store=queue_store,
                                 telemetry_store=telemetry_store,
                                 spec_store=spec_store, clock=clock)
        return {"enabled": True, "already": False}

    def telemetry_for_run(self, work_id: str, *, by: str = "module") -> dict[str, Any]:
        """What this run's tasks actually cost, per task and in aggregate.

        Reads the telemetry rows for the run's OWN queue tasks, so a run with
        no recorded rows reports that honestly instead of borrowing the
        fleet's numbers. Needs a telemetry store to read: with none attached
        the answer is that telemetry is not enabled, not an empty report that
        reads like a cheap run.
        """
        from .work_telemetry import aggregate, summarise

        run = self.store.get_run(work_id)
        if run is None:
            return {"error": "UNKNOWN_WORK", "work_id": work_id}
        telemetry_store = getattr(self.telemetry, "store", None)
        if telemetry_store is None:
            return {"error": "TELEMETRY_NOT_ENABLED", "work_id": work_id,
                    "detail": "no telemetry recorder is attached to this service"}
        rows: list[dict[str, Any]] = []
        for task in self.store.tasks_for(work_id):
            queue_task_id = task.get("queue_task_id")
            row = telemetry_store.for_task(queue_task_id) if queue_task_id else None
            if row:
                rows.append(row)
        return {"work_id": work_id, "tasks": rows, "summary": summarise(rows),
                "grouped": aggregate(rows, by=by),
                "tasks_without_telemetry":
                    len(self.store.tasks_for(work_id)) - len(rows)}

    # -- creation and planning ----------------------------------------------

    def create(self, *, title: str, goal: str, lane: str, project_id: str | None = None,
               done_criteria: Sequence[str] = (), created_by: str | None = None,
               tasks: Sequence[dict[str, Any]] = (),
               project_root: str | None = None) -> dict[str, Any]:
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
        # Bind the Work Policy at creation, not at execution: the binding is
        # what makes "which rules did this run operate under" answerable
        # later, and a run that never records one leaves that unanswerable.
        # A policy that cannot be read must not block the run -- the failure
        # is recorded in metadata so it is visible rather than assumed fine.
        metadata: dict[str, Any] = {}
        try:
            policy = load_policy(project_root or os.getcwd())
            metadata["policy"] = policy.binding().as_dict()
            if policy.drift:
                metadata["policy"]["drift"] = list(policy.drift)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            # Unreadable or malformed policy file. Programming errors are NOT
            # caught here: swallowing one would record a bug as a handled
            # "policy unavailable" state and hide it indefinitely.
            metadata["policy"] = {"error": type(exc).__name__, "detail": str(exc)[:200]}
            _LOGGER.warning("work create: policy could not be loaded: %s", exc)
        run = self.store.create_run(title=title, goal=goal, lane=lane, project_id=project_id,
                                    done_criteria=done_criteria, created_by=created_by,
                                    metadata=metadata)
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
            try:
                self.store.transition_run(work_id, ws.READY, actor=actor)
            except ws.WorkAnalysisGateError as exc:
                return {"error": "ANALYSIS_GATE_REFUSED", "work_id": work_id,
                        "detail": str(exc), "analysis_gate": exc.verdict.as_dict(),
                        "tasks": created}
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
                out[task["work_task_id"]] = self._with_waiting_age(dict(queue_task))
        return out

    # A task holding one of these is waiting on something external. The queue
    # is right to keep waiting -- completing without evidence is exactly what
    # it must never do -- but a screen that shows only the status name cannot
    # tell "verifying" from "stuck since yesterday", so both look identical
    # and the page appears frozen.
    WAITING_STATUSES = ("VERIFYING", "DISPATCH_UNCERTAIN", "WAITING_SESSION", "PRECHECK")
    # Past this, a wait has stopped being normal and wants a human.
    WAITING_STALE_SECONDS = 900.0

    def _with_waiting_age(self, queue_task: dict[str, Any]) -> dict[str, Any]:
        """Attach how long this task has been waiting, and whether that is odd.

        Derived from the queue's own transition event, so it cannot disagree
        with the status it describes. This is also what makes the payload
        change between polls: without it two reads are byte-identical and the
        UI has nothing to re-render, however correctly it polls.
        """
        status = str(queue_task.get("status") or "")
        if status not in self.WAITING_STATUSES or self.queue is None:
            return queue_task
        task_id = queue_task.get("id") or queue_task.get("task_id")
        try:
            since = self.queue.store.waiting_since(task_id, status)
        except Exception:  # noqa: BLE001 -- an age is a nicety, never a failure
            return queue_task
        if not since:
            return queue_task
        try:
            started = datetime.fromisoformat(since)
        except ValueError:
            return queue_task
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        seconds = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        queue_task["waiting_since"] = since
        queue_task["waiting_seconds"] = int(seconds)
        queue_task["waiting_stale"] = seconds >= self.WAITING_STALE_SECONDS
        if status == "VERIFYING" and seconds >= self.WAITING_STALE_SECONDS:
            queue_task["waiting_reason"] = (
                "no verified completion marker seen yet -- the worker never printed one, "
                "or it scrolled out of the capture window. Needs a marker or an explicit "
                "terminal_queue_verify; the queue will not complete a task without evidence.")
        elif seconds >= self.WAITING_STALE_SECONDS:
            queue_task["waiting_reason"] = f"waiting in {status} longer than expected"
        return queue_task

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
                          "queue_position": queue_task.get("position"),
                          "priority": queue_task.get("priority"),
                          "attempts": queue_task.get("attempt_count"),
                          "max_attempts": queue_task.get("max_attempts"),
                          "depends_on": queue_task.get("depends_on") or [],
                          # Which session is actually holding this task. The
                          # lane is where it was queued; `claimed_by` is who
                          # took it, and they can differ after a rebalance.
                          "worker_session": queue_task.get("session") or task.get("lane"),
                          "claimed_by": queue_task.get("claimed_by"),
                          "node_id": queue_task.get("node_id"),
                          "last_error": queue_task.get("last_error"),
                          # How long this task has been waiting, and why. This
                          # is the only field on the row that advances between
                          # polls while a task is parked, so without it the UI
                          # re-renders an identical DOM however correctly it
                          # polls -- which is what made the page look frozen.
                          "waiting_since": queue_task.get("waiting_since"),
                          "waiting_seconds": queue_task.get("waiting_seconds"),
                          "waiting_stale": queue_task.get("waiting_stale"),
                          "waiting_reason": queue_task.get("waiting_reason"),
                          "coordinator_reason": queue_task.get("coordinator_reason"),
                          "started_at": queue_task.get("started_at"),
                          "completed_at": queue_task.get("completed_at")})
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
        """Enough per row to triage a list without opening every run.

        `needs_you` and `blocked_tasks` are the two an operator scans for, so
        they are computed here rather than left for the UI to derive -- a
        second implementation of "is this stuck" is a second thing to get
        wrong.
        """
        works = []
        for run in self.store.list_runs(**kwargs):
            progress = self.progress(run.work_id)
            states = self._queue_states(run.work_id)
            running = sorted({str(q.get("session")) for q in states.values()
                              if str(q.get("status") or "") in RUNNING_STATUSES
                              and q.get("session")})
            pending = self.store.approvals_for(run.work_id, pending_only=True)
            works.append({**run.as_dict(), "progress": progress.as_dict(),
                          "running_workers": running,
                          "blocked_tasks": progress.blocked_tasks,
                          "needs_you": len(pending),
                          "needs_you_summary": pending[0]["summary"] if pending else None})
        return {"works": works}

    def workers(self, *, sessions: Iterable[dict[str, Any]],
                nodes: dict[str, dict[str, Any]] | None = None,
                statuses: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """The `-work` sessions, and only those.

        An ordinary session must never appear here even as a rejected
        candidate: this list is what the UI labels "Workers", and a human
        reading their own terminal in that list would reasonably conclude the
        runtime had taken it over. Rejections that matter -- a `-work` session
        that is stale or unwritable -- are kept, because those ARE workers and
        the operator needs to know why one is not being used.
        """
        out: list[dict[str, Any]] = []
        lanes: dict[str, dict[str, Any]] = {}
        for row in sessions:
            name = row.get("name") or row.get("session")
            if not is_work_session(name):
                continue
            node_id = row.get("node_id")
            verdict = evaluate_eligibility(
                name, status=(statuses or {}).get(name, {"exists": True}),
                node=(nodes or {}).get(node_id) if node_id else None,
                input_allowed=row.get("input_allowed"),
                input_denied_reason=row.get("input_denied_reason"))
            current = None
            if self.queue is not None:
                try:
                    lane = lanes.get(name) or self.queue.status(name)
                    lanes[name] = lane
                    task = lane.get("current_task")
                    if task and str(task.get("status") or "") in ACTIVE_QUEUE_STATUSES:
                        current = {"task_id": task.get("id"), "title": task.get("title"),
                                   "status": task.get("status")}
                except Exception:  # noqa: BLE001 -- a worker list never 5xxs
                    lane = {}
            # Occupancy is NOT eligibility, and it is not the queue's opinion
            # either. A `-work` session running Claude that nobody queued a
            # task on is busy: dispatching into it would type over a live
            # conversation. Deriving "idle" from "the queue has no task here"
            # reported exactly that session as free.
            occupied, evidence = _occupancy(row, (statuses or {}).get(name))
            offline = (not verdict.eligible and verdict.reason in
                       ("SESSION_MISSING", "SESSION_DEAD", "NODE_UNREACHABLE"))
            if offline:
                state = "OFFLINE"
            elif current:
                state = "BUSY"                    # the queue owns this one
            elif occupied:
                # Something real is running that the queue did not start.
                state = "RUNNING_MANUAL"
            elif verdict.eligible:
                state = "IDLE"                    # eligible AND demonstrably free
            else:
                state = "UNAVAILABLE"
            out.append({
                "session": name, "node_id": node_id,
                "agent_type": row.get("agent_type") or row.get("current_command"),
                "state": state, "eligible": verdict.eligible,
                "reason": verdict.reason, "detail": verdict.detail,
                "current_task": current,
                # Busy, but not because of anything this runtime scheduled.
                # Kept as its own field so a caller can tell "the queue is
                # working here" from "a human is", which the state name alone
                # cannot carry without overloading it.
                "busy_untracked": bool(occupied and not current and not offline),
                "occupancy": ("queue" if current else "manual" if occupied else "free"),
                "occupancy_evidence": evidence,
                "is_work_session": True})
        _ORDER = {"BUSY": 0, "RUNNING_MANUAL": 1, "IDLE": 2}
        out.sort(key=lambda w: (_ORDER.get(w["state"], 3), w["session"]))
        return {"workers": out}

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
