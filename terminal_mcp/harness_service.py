"""The ONE place the rest of the server reaches the Harness (TMCP-HARNESS-001).

WHY A SERVICE AND NOT A SECOND ENGINE

`HarnessEngine` decides stages; it deliberately knows nothing about the queue,
the dashboard, MCP or sessions. Something has to join those, and the failure
mode to avoid is the join happening five times -- once in the MCP tool, once
in the dashboard route, once in the PM, once in a loop, once in a test helper
-- each with its own idea of when a queue write is allowed. That is how the
old pipeline acquired five state machines in the first place.

So every caller goes through here, and this module owns exactly three things
the engine does not:

  1. WHERE the definition comes from. `start` reads the durable queue task --
     its title, prompt, project and requirement contract -- instead of making
     the caller restate them. A task's acceptance criteria already exist in
     `queue_tasks.requirement_contract`; asking a human to type them again
     into a harness call is how the two copies start to differ.
  2. WHETHER a queue write happens. `_project` is the injected
     `queue_projector`, and it is the only function in this repository that
     turns a harness stage into a task status.
  3. WHAT a caller is allowed to ask for. `resume` cannot restart, `cancel`
     cannot merge, `review` cannot resolve a decision that is not on the
     closed human list.

THE PROJECTION NEVER WRITES A FINAL TASK STATE

Not in SHADOW (which writes nothing at all), and not in SUPERVISED either.
`_TERMINAL_QUEUE_STATUSES` is refused unconditionally, by this module, for
every authority including AUTONOMOUS. A run reaching DONE does not complete
the queue task; the merge gate does, through the paths that already existed.

The reason is precise: "the harness believes this work is finished" and "this
task is finished" are different claims, and the second one is what closes a
task on a board a human is reading. A harness that could write COMPLETED
would be a second thing that completes tasks, which is the exact defect this
feature exists to remove -- only pointed the other way.

WHY THE PROJECTION SKIPS RATHER THAN FORCES

The queue has its own transition table and its own reasons (PRECHECK gates,
dispatch uncertainty, session waits). When the harness stage implies a queue
status the queue will not accept from where it currently is, this records a
skipped projection and moves on. Forcing it would mean the harness quietly
overriding a coordinator refusal, and an override nobody can see is worse
than a divergence everybody can.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from . import harness_policy as policy
from . import harness_state as state
from . import queue_store as qs
from . import requirement_contract as rc
from .harness_engine import HarnessEngine, HarnessError
from .harness_store import HarnessRun, HarnessStore, RunNotFound

#: Task statuses the projection will NEVER write, under any authority. A
#: task becomes COMPLETED/FAILED/CANCELLED through the queue's own paths --
#: verification, the merge gate, an operator -- never because a harness run
#: reached a stage. See the module docstring.
_TERMINAL_QUEUE_STATUSES: frozenset[str] = frozenset({
    qs.COMPLETED, qs.FAILED, qs.CANCELLED, qs.SKIPPED,
})

#: HarnessRun.projected_stage -> the queue status that means the same thing,
#: for the NON-terminal part of the lifecycle only. Deliberately partial:
#: a projected stage with no entry here (DONE, MERGE_READY) is a stage whose
#: queue meaning belongs to the merge gate, not to the harness.
_STAGE_TO_QUEUE_STATUS: dict[str, str] = {
    "PLANNING": qs.RUNNING,
    "BUILDING": qs.RUNNING,
    "EVALUATING": qs.VERIFYING,
    # A revision is more building. It returns the task to RUNNING rather
    # than leaving it VERIFYING, so a board never shows "being verified"
    # for work that is actively being rewritten.
    "REVISING": qs.RUNNING,
    "BLOCKED": qs.BLOCKED,
}


class HarnessUnavailable(RuntimeError):
    """This deployment has no harness wired. Raised, never silently ignored."""


def _clean(values: Sequence[str] | None) -> tuple[str, ...]:
    if not values:
        return ()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text:
            out.append(text)
    return tuple(out)


class HarnessService:
    """Start, observe, resume, cancel and review harness runs."""

    def __init__(self, *, store: HarnessStore | None = None,
                 queue: Any = None,
                 repo_root: str | None = None,
                 runner: Any = None,
                 owner: str | None = None,
                 check_runner: Callable[..., Any] | None = None) -> None:
        # The harness tables live in the QUEUE database (see queue_store's
        # migration ladder), so the default store is the queue's own file.
        # Passing the queue and letting the store default elsewhere would
        # reintroduce the second database this feature removed.
        self.queue = queue
        if store is not None:
            self.store = store
        elif queue is not None and getattr(queue, "store", None) is not None:
            self.store = HarnessStore(queue.store.path)
        else:
            self.store = HarnessStore()
        self.repo_root = repo_root
        self.runner = runner
        self.owner = owner
        self._check_runner = check_runner
        #: Recorded for the report; a projection that was refused is a fact
        #: worth keeping, not a silent no-op.
        self.skipped_projections: list[dict[str, Any]] = []

    # =====================================================================
    # engine construction
    # =====================================================================
    def engine(self) -> HarnessEngine:
        """A fresh engine bound to this service's projector.

        Built per call rather than held, because the projector closes over
        nothing mutable and an engine is cheap -- and because a long-lived
        engine would be a place for per-run state to accumulate outside the
        database, which is where every disagreement in the old pipeline
        started.
        """
        kwargs: dict[str, Any] = {}
        if self._check_runner is not None:
            kwargs["check_runner"] = self._check_runner
        return HarnessEngine(self.store, runner=self.runner,
                             repo_root=self.repo_root, owner=self.owner,
                             queue_projector=self._project, **kwargs)

    # =====================================================================
    # the projection -- the only harness -> queue write in the repository
    # =====================================================================
    def _project(self, run: HarnessRun, projected_stage: str) -> None:
        """Write the run's stage onto its queue task, or record why not.

        The engine only calls this when the run's authority permits a queue
        write (SHADOW never reaches here). Everything below is the SECOND
        gate: what a permitted write is still not allowed to do.
        """
        if self.queue is None or getattr(self.queue, "store", None) is None:
            return
        target = _STAGE_TO_QUEUE_STATUS.get(projected_stage)
        if target is None:
            self._skip(run, projected_stage, "no queue meaning: the merge gate owns it")
            return
        if target in _TERMINAL_QUEUE_STATUSES:  # defensive: the map has none
            self._skip(run, projected_stage, "refused: a harness stage never "
                                             "writes a final task state")
            return
        try:
            task = self.queue.store.get_task(run.task_id)
        except Exception:  # noqa: BLE001 -- a missing task is not a harness error
            task = None
        if task is None:
            self._skip(run, projected_stage, f"no durable task {run.task_id}")
            return
        if task.status == target:
            return
        if task.status in _TERMINAL_QUEUE_STATUSES:
            self._skip(run, projected_stage,
                       f"task is already {task.status}; the harness does not reopen it")
            return
        if not qs.is_valid_transition(task.status, target):
            # The queue's table, not ours. See the module docstring: a
            # skipped projection is visible; a forced one is not.
            self._skip(run, projected_stage,
                       f"queue refuses {task.status} -> {target}")
            return
        try:
            self.queue.store.transition_task(
                run.task_id, target, event_type="HARNESS_PROJECTION",
                reason=f"harness {run.stage} ({run.mode}/{run.write_authority}) "
                       f"run {run.id}")
        except Exception as exc:  # noqa: BLE001 -- never fail a run on a projection
            self._skip(run, projected_stage, f"queue write failed: {exc}")
            return
        self.store.record_event(
            run.id, event_type="QUEUE_PROJECTED", reason=f"task -> {target}",
            metadata={"task_id": run.task_id, "from": task.status, "to": target})

    def _skip(self, run: HarnessRun, projected_stage: str, why: str) -> None:
        entry = {"run_id": run.id, "task_id": run.task_id,
                 "projected_stage": projected_stage, "reason": why}
        self.skipped_projections.append(entry)
        self.store.record_event(run.id, event_type="QUEUE_PROJECTION_SKIPPED",
                                reason=why, metadata=entry)

    # =====================================================================
    # harness_start
    # =====================================================================
    def start(self, *, task_id: str, prompt: str | None = None,
              title: str | None = None, project_id: str | None = None,
              acceptance: Sequence[str] = (), checks: Sequence[str] = (),
              changed_paths: Sequence[str] = (),
              mode: str | None = None,
              write_authority: str = policy.SHADOW,
              node_id: str | None = None,
              actor: str | None = None,
              steps: int = 0) -> dict[str, Any]:
        """Open (or re-open) the run for one task. Idempotent on the definition.

        `steps` drives the engine that many times in this same call. It
        defaults to 0 -- starting a run and RUNNING it are separate decisions,
        and a caller that only wanted a run recorded should not discover it
        has also spent model calls.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            return {"status": "FAILED", "error": "TASK_ID_REQUIRED"}
        if write_authority not in policy.AUTHORITIES:
            return {"status": "FAILED", "error": "INVALID_WRITE_AUTHORITY",
                    "allowed": list(policy.AUTHORITIES)}
        if mode is not None and mode not in policy.MODES:
            return {"status": "FAILED", "error": "INVALID_MODE",
                    "allowed": list(policy.MODES)}

        definition = self._definition_for(task_id)
        resolved_prompt = (prompt or definition.get("prompt") or "").strip()
        if not resolved_prompt:
            return {"status": "FAILED", "error": "PROMPT_REQUIRED",
                    "detail": f"no durable task {task_id} and no prompt given"}
        resolved_acceptance = _clean(acceptance) or definition.get("acceptance", ())
        resolved_checks = _clean(checks) or definition.get("checks", ())

        engine = self.engine()
        try:
            run, created = engine.start(
                task_id=task_id, prompt=resolved_prompt,
                title=(title or definition.get("title") or "").strip(),
                project_id=project_id or definition.get("project_id"),
                acceptance=resolved_acceptance, checks=resolved_checks,
                changed_paths=_clean(changed_paths),
                requested_mode=mode, write_authority=write_authority,
                node_id=node_id, actor=actor)
        except Exception as exc:  # noqa: BLE001 -- surfaced, never swallowed
            return {"status": "FAILED", "error": "START_FAILED", "detail": str(exc)}

        outcomes, drive_error = self._drive(engine, run.id, steps)
        return {
            "status": "OK",
            "drive_error": drive_error,
            # `created=False` is the exactly-once answer: a repeated
            # harness_start for the same task definition returns the SAME
            # run rather than opening a second one.
            "created": created,
            "run": self.store.require_run(run.id).to_dict(),
            "definition_source": definition.get("source", "caller"),
            "steps": outcomes,
        }

    def _drive(self, engine: HarnessEngine, run_id: str, steps: int
               ) -> tuple[list[dict[str, Any]], str | None]:
        """Step the engine, and report a refusal as a RESULT rather than raise.

        The case this exists for is the ordinary one on a server with no
        AgentRunner wired: planning is deterministic and succeeds, then the
        Builder cannot be reached. That is a true and useful answer -- the
        run exists, it got as far as PLAN_READY, and here is precisely what
        stopped it -- whereas a traceback out of an MCP tool tells a caller
        only that something went wrong somewhere.

        Every step taken before the refusal is still returned, because they
        really happened and are already on the record.
        """
        if steps <= 0:
            return [], None
        outcomes: list[dict[str, Any]] = []
        try:
            for outcome in engine.drive(run_id, max_steps=int(steps)):
                outcomes.append(outcome.to_dict())
        except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
            return outcomes, f"{type(exc).__name__}: {exc}"
        return outcomes, None

    def _definition_for(self, task_id: str) -> dict[str, Any]:
        """The task's own durable definition, if this task_id is a real task.

        Acceptance criteria come from the task's requirement contract, which
        already exists and is already what the delivery gate checks. Reading
        it here is what keeps the ExecutionContract a RESTATEMENT of the
        task's requirements rather than a second, drifting copy of them.
        """
        if self.queue is None or getattr(self.queue, "store", None) is None:
            return {"source": "caller"}
        try:
            task = self.queue.store.get_task(task_id)
        except Exception:  # noqa: BLE001
            task = None
        if task is None:
            return {"source": "caller"}
        # Read through RequirementContract rather than reaching into the
        # stored dict: the contract is VERSIONED (amendments append a new
        # version), so "the requirements" means the current version's, and
        # `RequirementContract.requirements` is already the one function in
        # this repository that knows that. Hand-parsing the blob here would
        # be a second, quietly wrong answer to the same question -- it would
        # read an unamended contract correctly and an amended one not at all.
        contract = rc.RequirementContract.from_dict(task.requirement_contract)
        acceptance = [
            requirement.text for requirement in (contract.requirements() if contract else ())
            if requirement.kind == rc.KIND_ACCEPTANCE
        ]
        metadata = task.metadata or {}
        checks = _clean(metadata.get("checks") or metadata.get("required_checks"))
        return {
            "source": "queue_task",
            "prompt": task.prompt,
            "title": task.title,
            "project_id": task.project_id,
            "acceptance": tuple(acceptance),
            "checks": checks,
            "status": task.status,
        }

    # =====================================================================
    # harness_status
    # =====================================================================
    def status(self, *, run_id: str | None = None, task_id: str | None = None,
               project_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        """One run in full, or a project's runs in summary."""
        run = self._resolve(run_id, task_id)
        if run is None:
            if run_id or task_id:
                return {"status": "FAILED", "error": "NO_RUN",
                        "detail": f"no harness run for {run_id or task_id}"}
            runs = self.store.list_runs(project_id=project_id, limit=int(limit))
            return {"status": "OK",
                    "runs": [self._summary(item) for item in runs],
                    "human_decisions": self.store.list_decisions(
                        status="open", project_id=project_id)}
        report = self.store.run_report(run.id)
        report["status"] = "OK"
        report["summary"] = self._summary(run)
        return report

    def _summary(self, run: HarnessRun) -> dict[str, Any]:
        """The row a board renders. Every field is derived from the run."""
        efficiency = self.store.efficiency(run.id)
        return {
            "run_id": run.id,
            "task_id": run.task_id,
            "project_id": run.project_id,
            "title": run.title,
            "mode": run.mode,
            "stage": run.stage,
            "projected_stage": run.projected_stage,
            "write_authority": run.write_authority,
            "shadow": run.is_shadow,
            "iteration": run.current_iteration,
            "max_iterations": run.max_iterations,
            "builder": run.builder_agent or run.builder_session_id,
            "evaluator": run.evaluator_agent or run.evaluator_session_id,
            "progress": self._progress(run),
            "blocker": run.blocked_reason,
            "last_error": run.last_error,
            "efficiency": {
                "llm_calls": efficiency.get("llm_calls", 0),
                "prompt_tokens_estimate": efficiency.get("prompt_tokens_estimate", 0),
                "context_bytes": efficiency.get("context_bytes", 0),
                "planner_skipped": efficiency.get("planner_skipped", 0),
                "evaluator_skipped": efficiency.get("evaluator_skipped", 0),
                "reused_context_hits": efficiency.get("reused_context_hits", 0),
                "context_cache_misses": efficiency.get("context_cache_misses", 0),
                "session_reused": efficiency.get("session_reused", 0),
                "sessions_spawned": efficiency.get("sessions_spawned", 0),
                "delta_prompts": efficiency.get("delta_prompts", 0),
                "full_prompts": efficiency.get("full_prompts", 0),
            },
            "updated_at": run.updated_at,
        }

    @staticmethod
    def _progress(run: HarnessRun) -> dict[str, Any]:
        """Fraction of the stage ladder reached, for a progress bar.

        Derived from the stage, never stored: a stored percentage is a number
        that can disagree with the stage it claims to describe.
        """
        ladder = (state.INIT, state.PLANNING, state.PLAN_READY, state.BUILDING,
                  state.EVALUATING, state.MERGE_READY, state.MERGED, state.DONE)
        if run.stage in ladder:
            position = ladder.index(run.stage)
        elif run.stage == state.REVISING:
            position = ladder.index(state.BUILDING)
        else:
            position = 0
        return {"step": position, "of": len(ladder) - 1,
                "percent": round(100.0 * position / (len(ladder) - 1)),
                "iteration": run.current_iteration,
                "max_iterations": run.max_iterations}

    # =====================================================================
    # harness_resume
    # =====================================================================
    def resume(self, *, run_id: str | None = None, task_id: str | None = None,
               actor: str | None = None, steps: int = 0) -> dict[str, Any]:
        """Continue the EXACT run, stage and checkpoint. Never re-plans.

        Three cases, and none of them is "start over":

          * an interrupted run (FAILED_INFRA / RECOVERY_REQUIRED /
            CONTEXT_ROLLOVER) returns to the stage it was in, via
            `engine.resume`, which reads `resume_stage` -- a column written as
            the run ENTERED that stage, not reconstructed afterwards;
          * a run already sitting in a resumable stage needs no transition at
            all. It is already exactly where it should continue from, and
            advancing it anywhere -- including to itself -- would write a
            stage event that did not happen;
          * anything else (terminal, BLOCKED, NEEDS_REDEFINE) is refused with
            what it would take to move it, because those need a decision, not
            a retry.

        The guarantee this method makes is structural, not conventional: the
        stage machine has no edge from any resumable stage back to INIT (see
        harness_state.RESUMABLE_STAGES), so there is no sequence of calls
        here that re-runs the Planner.
        """
        run = self._resolve(run_id, task_id)
        if run is None:
            return {"status": "FAILED", "error": "NO_RUN",
                    "detail": f"no harness run for {run_id or task_id}"}
        before = run.stage
        checkpoint = self.store.latest_checkpoint(run.id)
        engine = self.engine()

        if run.stage in state.TERMINAL_STAGES:
            return {"status": "FAILED", "error": "RUN_IS_TERMINAL",
                    "stage": run.stage, "run_id": run.id,
                    "detail": f"{run.stage} runs do not resume; start a new run"}
        if run.stage in (state.BLOCKED, state.NEEDS_REDEFINE):
            return {"status": "FAILED", "error": "NEEDS_DECISION",
                    "stage": run.stage, "run_id": run.id,
                    "blocker": run.blocked_reason,
                    "open_decisions": self.store.list_decisions(
                        status="open", run_id=run.id),
                    "detail": "resolve the decision with harness_review; a resume "
                              "would not change why this stopped"}

        if run.stage in (state.FAILED_INFRA, state.RECOVERY_REQUIRED,
                         state.CONTEXT_ROLLOVER):
            outcome = engine.resume(run.id)
            resumed_to = outcome.to_stage
        elif run.stage in state.RESUMABLE_STAGES:
            resumed_to = run.stage
            self.store.record_event(
                run.id, event_type="RESUMED_IN_PLACE", actor=actor,
                reason=f"already at {run.stage}; no transition written",
                metadata={"checkpoint": (checkpoint or {}).get("id"),
                          "iteration": run.current_iteration})
        else:
            # Reachable only from INIT: a run that has taken no step has
            # nothing to continue. Say so, and say what does move it, rather
            # than leaving a caller to guess that "not resumable" means "not
            # started" here and "already finished" elsewhere.
            return {"status": "FAILED", "error": "NOT_RESUMABLE",
                    "stage": run.stage, "run_id": run.id,
                    "detail": f"a run at {run.stage} has taken no step yet; there is "
                              f"nothing to continue. Drive it with harness_start "
                              f"(steps=N) instead."}

        outcomes, drive_error = self._drive(engine, run.id, steps)
        current = self.store.require_run(run.id)
        return {
            "status": "OK",
            "run_id": run.id,
            "resumed_from": before,
            "resumed_to": resumed_to,
            # The three facts that prove this was a continuation and not a
            # restart. A caller (and a test) can check them without reading
            # the event log.
            "iteration": current.current_iteration,
            "checkpoint": checkpoint,
            "contract_id": current.contract_id,
            "planner_rerun": False,
            "worktree_path": current.worktree_path,
            "branch": current.branch,
            "steps": outcomes,
            "drive_error": drive_error,
            "run": current.to_dict(),
        }

    # =====================================================================
    # harness_cancel
    # =====================================================================
    def cancel(self, *, run_id: str | None = None, task_id: str | None = None,
               reason: str = "cancelled by operator",
               actor: str | None = None) -> dict[str, Any]:
        """Stop the run. Does not touch the task, and never merges.

        Cancelling a run says the ATTEMPT is over. What should happen to the
        queue task afterwards is a separate decision with its own tool, which
        is why this writes no task status even under AUTONOMOUS.
        """
        run = self._resolve(run_id, task_id)
        if run is None:
            return {"status": "FAILED", "error": "NO_RUN",
                    "detail": f"no harness run for {run_id or task_id}"}
        if run.stage == state.CANCELLED:
            return {"status": "OK", "run_id": run.id, "stage": state.CANCELLED,
                    "already": True, "run": run.to_dict()}
        if run.stage in state.TERMINAL_STAGES:
            return {"status": "FAILED", "error": "RUN_IS_TERMINAL",
                    "stage": run.stage, "run_id": run.id,
                    "detail": f"{run.stage} is already terminal"}
        try:
            updated = self.store.advance(
                run.id, state.CANCELLED, actor=actor or "operator",
                reason=reason, metadata={"cancelled_by": actor or "operator"})
        except Exception as exc:  # noqa: BLE001
            return {"status": "FAILED", "error": "CANCEL_REFUSED",
                    "stage": run.stage, "detail": str(exc)}
        return {"status": "OK", "run_id": run.id, "cancelled_from": run.stage,
                "stage": updated.stage, "reason": reason,
                "task_status_unchanged": True, "run": updated.to_dict()}

    # =====================================================================
    # harness_review
    # =====================================================================
    def review(self, *, run_id: str | None = None, task_id: str | None = None,
               decision_id: str | None = None, resolution: str | None = None,
               approve_merge: bool = False, actor: str | None = None,
               project_id: str | None = None) -> dict[str, Any]:
        """The human side: read what is waiting, resolve it, or approve a merge.

        With no decision_id and no approval this is READ-ONLY -- the review
        queue plus, if a run was named, everything that run is waiting on.
        """
        if decision_id:
            if not resolution:
                return {"status": "FAILED", "error": "RESOLUTION_REQUIRED",
                        "detail": "a decision is resolved with words, not a click"}
            try:
                decision = self.store.resolve_decision(
                    decision_id, resolution=resolution,
                    resolved_by=actor or "operator")
            except LookupError as exc:
                return {"status": "FAILED", "error": "NO_DECISION", "detail": str(exc)}
            return {"status": "OK", "decision": decision,
                    "detail": "the run stays where it is; resolving a decision "
                              "records the answer, it does not advance a stage"}

        run = self._resolve(run_id, task_id)
        if approve_merge:
            if run is None:
                return {"status": "FAILED", "error": "NO_RUN",
                        "detail": "approve_merge needs a run"}
            if not actor:
                return {"status": "FAILED", "error": "ACTOR_REQUIRED",
                        "detail": "a merge approval has to name who approved it"}
            try:
                outcome = self.engine().approve_merge(run.id, approved_by=actor)
            except HarnessError as exc:
                return {"status": "FAILED", "error": "MERGE_REFUSED",
                        "stage": run.stage, "detail": str(exc)}
            return {"status": "OK", "run_id": run.id, "approved_by": actor,
                    "outcome": outcome.to_dict(),
                    "run": self.store.require_run(run.id).to_dict()}

        queue_rows = self.human_decisions(
            project_id=project_id or (run.project_id if run else None))
        payload: dict[str, Any] = {"status": "OK", "human_decisions": queue_rows,
                                   "reasons": list(policy.HUMAN_DECISION_REASONS)}
        if run is not None:
            payload["run"] = self.store.require_run(run.id).to_dict()
            payload["summary"] = self._summary(run)
            payload["open_decisions"] = self.store.list_decisions(
                status="open", run_id=run.id)
            payload["evaluations"] = self.store.evaluations(run.id)
            payload["contract"] = (lambda c: c.to_dict() if c else None)(
                self.store.get_contract(run.id))
        return payload

    # =====================================================================
    # read-side projections, for the board and the project page
    # =====================================================================
    def human_decisions(self, *, project_id: str | None = None,
                        limit: int = 100) -> list[dict[str, Any]]:
        """The Human Decision Queue: ONLY the closed list of real blockers.

        `store.open_decision` already refuses anything outside
        `policy.HUMAN_DECISION_REASONS`, so this cannot contain a failing
        test -- a FAIL goes to REVISING and never opens a decision at all.
        The filter is repeated here anyway, cheaply, because this is the
        function a UI renders and a row that reached the queue by some path
        nobody anticipated should not be shown to a human as their problem.
        """
        rows = self.store.list_decisions(status="open", project_id=project_id,
                                         limit=int(limit))
        return [row for row in rows
                if row.get("reason") in policy.HUMAN_DECISION_REASONS]

    def projections_for_tasks(self, task_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """task_id -> the harness facts a task card shows. One bulk read.

        A task with no run is ABSENT from the mapping. The board must be able
        to tell "not harnessed" from "harnessed and idle", and a default row
        of zeroes cannot express the difference.
        """
        runs = self.store.runs_for_tasks(task_ids)
        return {task_id: self._summary(run) for task_id, run in runs.items()}

    def project_overview(self, project_id: str | None = None) -> dict[str, Any]:
        """What the Project page shows: every run, and the totals underneath."""
        runs = self.store.list_runs(project_id=project_id, limit=500)
        summaries = [self._summary(run) for run in runs]
        totals = self.store.efficiency_totals([run.id for run in runs])
        by_stage: dict[str, int] = {}
        for run in runs:
            by_stage[run.projected_stage] = by_stage.get(run.projected_stage, 0) + 1
        return {
            "status": "OK",
            "project_id": project_id,
            "runs": summaries,
            "by_stage": by_stage,
            "efficiency_totals": totals,
            "human_decisions": self.human_decisions(project_id=project_id),
            "cache": self.store.cache_stats(project_id=project_id),
        }

    # =====================================================================
    # helpers
    # =====================================================================
    def _resolve(self, run_id: str | None, task_id: str | None) -> HarnessRun | None:
        if run_id:
            try:
                return self.store.require_run(str(run_id).strip())
            except RunNotFound:
                return None
        if task_id:
            return self.store.run_for_task(str(task_id).strip(), active_only=False)
        return None
