"""The orchestrator. Deterministic everywhere it can be, LLM only where it must.

WHAT MAKES THIS AN ENGINE AND NOT A LOOP

There is no `while True` in this file, no `time.sleep`, no thread and no
timer. `step()` performs exactly one stage transition and returns; the caller
decides whether to call it again. That is not a stylistic preference -- a
loop that lives inside the engine is a loop that runs whether or not there is
anything to do, and in the pipeline this replaces that loop woke a model
every sixty seconds to ask whether anything had changed. Over a day that is
1,440 model calls whose entire output is "no".

Readiness, dependencies, leases, routing, thresholds, budgets, retries and
status are all computed here, for free, from the database. The LLM is invoked
in exactly three places -- `_plan` (write a contract), `_build` (write code),
`_evaluate` (judge evidence) -- and each of those three has a documented
condition under which it is SKIPPED entirely.

THE THREE SKIPS, WHICH ARE WHERE THE MONEY IS

1. Planner. Skipped whenever the task definition already contains what a
   contract needs -- scope, acceptance, checks. Building a contract from
   those is a template substitution, and paying a frontier model to perform a
   template substitution is the purest form of the waste this feature exists
   to remove. `harness_policy.planner_required` decides, and it refuses to
   skip for CRITICAL work no matter how complete the definition looks.

2. Evaluator. LIGHT and STANDARD do not spawn an independent evaluator
   session. They run the contract's declared checks -- named before the
   Builder started -- and the ENGINE reads the exit statuses, not the
   Builder. That is the difference between self-testing and self-approval,
   and it is why a Builder claiming success while its test command exits
   non-zero is recorded as a failure (see `_verdict_from_checks`).

3. Revision context. A revision sent to the session that built the previous
   attempt is a delta: the failed criteria and nothing else. A revision sent
   to a REPLACEMENT session cannot be -- there is no shared history for the
   delta to be a delta of -- so it gets a full prompt plus the checkpoint.
   The engine decides which, from the session id, not from a flag a caller
   could get wrong.

WHY A FAILING CHECK NEVER PRODUCES A BLOCKED RUN

EVALUATING + fail goes to REVISING. The only way a run reaches BLOCKED from
EVALUATING is the max-iterations guard, which is a statement about the loop
not converging rather than a verdict about the code. Everything else that
stops a run is on the closed list in harness_policy.HUMAN_DECISION_REASONS,
and a red test is not on it.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from . import harness_context as ctx
from . import harness_policy as policy
from . import harness_state as state
from .harness_contract import (EvaluationResult, ExecutionContract,
                               InsufficientSpecification, MalformedVerdict,
                               builder_request_key, contract_request_key,
                               criterion_id, definition_hash,
                               evaluator_request_key, iteration_request_key,
                               parse_verdict, run_request_key)
from .harness_store import HarnessStore

#: Wall-clock ceiling for one declared check. A check that hangs is an
#: infrastructure failure, not a product failure -- the distinction matters
#: because one resumes and the other revises.
CHECK_TIMEOUT_SECONDS = 900.0


class HarnessError(RuntimeError):
    pass


class NoContract(HarnessError):
    pass


@dataclass
class AgentResult:
    """What one agent invocation produced.

    `infra_failure` is the field the whole retry rewrite turns on. An agent
    that crashed, timed out or lost its session sets it; an agent that
    completed and produced wrong code does NOT. The first resumes the same
    iteration; the second starts a new one.
    """

    payload: Any = None
    text: str = ""
    session_id: str | None = None
    context_percent: float | None = None
    commit: str | None = None
    agent: str | None = None
    infra_failure: bool = False
    error: str | None = None
    completion_tokens_estimate: int = 0


class AgentRunner(Protocol):
    """How the engine reaches a model. Injected, never imported.

    Kept to one method on purpose: everything the engine needs to decide is
    already decided before this is called (which role, which tier, which
    session, delta or full). A runner that had to make any of those decisions
    would be a second policy layer.
    """

    def run(self, *, role: str, prompt: "ctx.Prompt", run: Any, tier: str,
            session_id: str | None, iteration: int) -> AgentResult: ...


@dataclass
class CheckResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {"command": self.command, "exit_code": self.exit_code,
                "timed_out": self.timed_out, "output": self.output[-4000:]}


def run_checks(commands: Sequence[str], *, cwd: str | None,
               timeout: float = CHECK_TIMEOUT_SECONDS,
               env: dict[str, str] | None = None) -> list[CheckResult]:
    """Run the contract's declared checks and report what they did.

    Every check runs even if an earlier one failed. Stopping at the first
    failure would hand the next revision one problem at a time, and each
    revision costs a full build/evaluate round -- three failures found
    together cost one round, found separately they cost three.
    """
    results: list[CheckResult] = []
    for command in commands:
        text = str(command).strip()
        if not text:
            continue
        try:
            completed = subprocess.run(
                text, shell=True, cwd=cwd, capture_output=True, text=True,
                timeout=timeout, env=env)
            output = (completed.stdout or "") + (completed.stderr or "")
            results.append(CheckResult(command=text, exit_code=completed.returncode,
                                       output=output.strip()))
        except subprocess.TimeoutExpired:
            results.append(CheckResult(command=text, exit_code=124,
                                       output=f"timed out after {timeout}s",
                                       timed_out=True))
        except OSError as exc:
            results.append(CheckResult(command=text, exit_code=127, output=str(exc)))
    return results


#: Shell constructs that make a line a command even though the first word is
#: not a program on PATH.
_SHELL_BUILTINS = frozenset({
    "true", "false", "test", "echo", "cd", "export", "source", ".", "set",
    "exit", "read", "eval", "exec", "[",
})


def partition_checks(checks: Sequence[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(commands, prose). Which declared checks can actually be RUN.

    WHY THIS EXISTS, AND WHY IT IS NOT IN harness_policy

    The evaluator-skip is sound for one reason only: a declared check's exit
    status is not a matter of opinion. That reasoning collapses the moment a
    "check" is a phrase rather than a command. Real task files are full of
    them -- "typecheck", "component tests", "fixture validation" -- and every
    one of those would be handed to a shell, exit 127, and be recorded as a
    product failure of code that is fine.

    So a check counts as a check only if its first word resolves to a program
    on this machine or is a shell builtin. Everything else is a MANUAL check:
    still written into the contract, still shown to the Builder and the
    Evaluator, but never used as grounds to skip a Planner or an Evaluator.

    It lives here and not in harness_policy because it asks the filesystem
    what exists, and `mode_for`/`planner_required` are pure -- they have to
    give the same answer on the controller and on the node that will run the
    work, and `shutil.which` does not.
    """
    commands: list[str] = []
    prose: list[str] = []
    for raw in checks:
        text = str(raw or "").strip()
        if not text:
            continue
        # A pipeline, a redirect or a chain is a shell line by construction.
        if any(token in text for token in ("|", "&&", "||", ";", ">", "<", "$(")):
            commands.append(text)
            continue
        try:
            parts = shlex.split(text)
        except ValueError:
            prose.append(text)
            continue
        if not parts:
            prose.append(text)
            continue
        program = parts[0]
        if program in _SHELL_BUILTINS or shutil.which(program):
            commands.append(text)
        else:
            prose.append(text)
    return tuple(commands), tuple(prose)


#: Literals a criterion can name that a command's OUTPUT can be checked
#: against: version strings and content hashes. Deliberately only these two --
#: they are unambiguous, and a looser matcher would start reporting prose
#: mismatches, which is guessing.
_VERSION_LITERAL = re.compile(r"\bv?\d+(?:\.\d+)*(?:\.x|\.\*)?\b")
_HASH_LITERAL = re.compile(r"\b[0-9a-f]{32,64}\b")


def _literals(text: str) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for match in _HASH_LITERAL.finditer(text.lower()):
        found.append(("hash", match.group()))
    for match in _VERSION_LITERAL.finditer(text.lower()):
        token = match.group()
        # A bare integer is not a version claim; "16px" and "3 items" would
        # otherwise every one of them look like one.
        if "." in token or token.startswith("v"):
            found.append(("version", token))
    return tuple(found)


def uncorroborated_criteria(contract: "ExecutionContract",
                            results: Sequence[CheckResult]) -> tuple[str, ...]:
    """Criteria whose declared checks passed WITHOUT deciding them.

    WHY A GREEN CHECK IS NOT ALWAYS A PASS

    The evaluator-skip rests on the exit status being the answer. `node -v`
    exits 0 on every version ever released, so a criterion reading "node -v
    reports v24.x" is not decided by it -- and a run that records that
    criterion as passing has put a false statement into the audit trail, which
    is worse than having spent the money on an evaluator.

    The test is narrow and mechanical: when a criterion names a version or a
    hash, that literal must appear in the output of the checks that ran. When
    it names neither, this says nothing -- the check is accepted as the
    evidence, which is the documented LIGHT/STANDARD bargain. Narrow on
    purpose: an escalation rule that fires on prose would escalate everything
    and quietly restore the cost of an evaluator on every task.
    """
    output = "\n".join(r.output for r in results).lower()
    stale: list[str] = []
    for kind, text in contract.criteria():
        claims = _literals(text)
        if not claims:
            continue
        satisfied = False
        for claim_kind, literal in claims:
            if claim_kind == "hash":
                satisfied = satisfied or literal in output
                continue
            prefix = literal.rstrip("*").rstrip(".x").rstrip(".")
            satisfied = satisfied or any(
                token.startswith(prefix)
                for token in re.split(r"[\s,;:()\[\]]+", output))
        if not satisfied:
            stale.append(criterion_id(kind, text))
    return tuple(stale)


@dataclass
class StepOutcome:
    """What one `step()` did. Returned rather than logged-and-swallowed so a
    caller can decide whether to step again without re-reading the database."""

    run_id: str
    from_stage: str
    to_stage: str
    action: str
    detail: dict[str, Any] = field(default_factory=dict)
    llm_called: bool = False

    @property
    def done(self) -> bool:
        return self.to_stage in (state.MERGE_READY, state.MERGED, state.DONE,
                                 state.BLOCKED, state.CANCELLED,
                                 state.NEEDS_REDEFINE)

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "from_stage": self.from_stage,
                "to_stage": self.to_stage, "action": self.action,
                "llm_called": self.llm_called, "detail": self.detail}


class HarnessEngine:
    """One task definition in, one MERGE_READY run (or one human question) out."""

    def __init__(self, store: HarnessStore, *, assembler: ctx.ContextAssembler | None = None,
                 runner: AgentRunner | None = None,
                 repo_root: str | None = None,
                 owner: str | None = None,
                 check_runner: Callable[..., list[CheckResult]] = run_checks,
                 queue_projector: Callable[[Any, str], None] | None = None) -> None:
        self.store = store
        self.assembler = assembler or ctx.ContextAssembler(store, repo_root=repo_root)
        self.runner = runner
        self.repo_root = repo_root
        self.owner = owner
        self.check_runner = check_runner
        #: Called after every stage change when the run's authority allows a
        #: queue write. Injected so SHADOW mode is enforced by NOT CALLING IT
        #: rather than by the projector checking a flag it could get wrong.
        self.queue_projector = queue_projector

    # =====================================================================
    # starting
    # =====================================================================
    def start(self, *, task_id: str, prompt: str, title: str = "",
              project_id: str | None = None, acceptance: Sequence[str] = (),
              checks: Sequence[str] = (), changed_paths: Sequence[str] = (),
              requested_mode: str | None = None,
              write_authority: str = policy.SHADOW,
              node_id: str | None = None,
              inherit_builder_session: str | None = None,
              inherit_from_task: str | None = None,
              actor: str | None = None) -> tuple[Any, bool]:
        """(run, created). Idempotent on the task DEFINITION.

        Durable overrides are applied ON TOP of the triage, and only in the
        escalating direction for mode -- an operator may ask for more process
        than the triage chose, never less, and a de-escalation request is
        recorded in `reasons` rather than silently dropped.
        """
        # Prose is separated from commands BEFORE the triage sees them, so a
        # task whose "checks" are labels is correctly treated as a task with
        # no checks -- which is precisely the case that needs a Planner.
        commands, prose = partition_checks(checks)
        overrides = self.store.resolve_policy_overrides(project_id=project_id,
                                                        task_id=task_id)
        effective_mode = requested_mode or overrides.get("mode")
        authority = overrides.get("write_authority") or write_authority
        run_policy = policy.policy_for(
            f"{title}\n{prompt}", changed_paths=changed_paths,
            requested_mode=effective_mode, write_authority=authority,
            acceptance=acceptance, checks=commands)
        if overrides.get("max_iterations"):
            run_policy = policy.HarnessPolicy(
                **{**run_policy.to_dict(),
                   "critical_areas": run_policy.critical_areas,
                   "reasons": run_policy.reasons
                   + (f"max_iterations overridden to {overrides['max_iterations']}",),
                   "max_iterations": int(overrides["max_iterations"])})
        cost = policy.cost_policy_for(run_policy.mode)
        digest = definition_hash(project_id=project_id or "", task_id=task_id,
                                 mode=run_policy.mode, prompt=prompt,
                                 acceptance=acceptance, checks=commands)
        run, created = self.store.create_run(
            task_id=task_id, prompt=prompt, title=title, project_id=project_id,
            run_policy=run_policy, cost=cost,
            request_key=run_request_key(project_id or "", task_id, digest),
            definition_hash=digest, node_id=node_id, actor=actor)
        if created:
            # The task's own declared acceptance/checks are the input to the
            # contract, so they are stored on the run rather than re-derived
            # from a TASKS file later -- a definition file is a definition,
            # never runtime state.
            self.store.patch_run(run.id, policy={
                **run.policy, "declared_acceptance": list(acceptance),
                "declared_checks": list(commands),
                "declared_manual_checks": list(prose),
                "changed_paths": list(changed_paths),
                "session_inherited_from": inherit_from_task})
            if inherit_builder_session:
                # The scheduler decided this task lands on a lane's existing
                # session. Recorded on the run BEFORE any prompt is built, so
                # `_builder_prompt` reads it as a fact rather than being told.
                self.store.patch_run(run.id, builder_session_id=inherit_builder_session)
                self.store.record_event(
                    run.id, event_type="BUILDER_SESSION_INHERITED",
                    actor=inherit_builder_session,
                    reason=f"same lane as {inherit_from_task}",
                    metadata={"from_task": inherit_from_task,
                              "session": inherit_builder_session})
            run = self.store.require_run(run.id)
        return run, created

    # =====================================================================
    # one step
    # =====================================================================
    def step(self, run_id: str) -> StepOutcome:
        """Advance the run by exactly one stage. Never loops, never sleeps."""
        run = self.store.require_run(run_id)
        stage = run.stage
        if stage == state.INIT:
            return self._plan(run)
        if stage == state.PLANNING:
            return self._plan(run)
        if stage == state.PLAN_READY:
            return self._enter_build(run)
        if stage == state.BUILDING:
            return self._build(run)
        if stage == state.EVALUATING:
            return self._evaluate(run)
        if stage == state.REVISING:
            return self._enter_build(run)
        if stage in (state.FAILED_INFRA, state.RECOVERY_REQUIRED,
                     state.CONTEXT_ROLLOVER):
            return self.resume(run_id)
        return StepOutcome(run_id=run_id, from_stage=stage, to_stage=stage,
                           action="idle",
                           detail={"reason": f"{stage} is not a stage the engine drives"})

    def drive(self, run_id: str, *, max_steps: int = 24) -> list[StepOutcome]:
        """Step until the run stops moving or the step budget runs out.

        Bounded, synchronous and driven by the caller. `max_steps` is a
        guard against a bug in the transition table, not a retry policy --
        the retry policy is the iteration count, and it lives on the run.
        """
        outcomes: list[StepOutcome] = []
        for _ in range(max_steps):
            outcome = self.step(run_id)
            outcomes.append(outcome)
            if outcome.action == "idle" or outcome.done:
                break
        return outcomes

    # =====================================================================
    # planning
    # =====================================================================
    def _plan(self, run: Any) -> StepOutcome:
        existing = self.store.get_contract(run.id)
        if existing is not None and existing.frozen:
            return self._to(run, state.PLAN_READY, action="contract_already_frozen")

        acceptance = tuple(run.policy.get("declared_acceptance") or ())
        checks = tuple(run.policy.get("declared_checks") or ())
        required, why = policy.planner_required(
            run.mode, acceptance=acceptance, checks=checks, scope=run.title or run.prompt)

        if not required:
            # THE FIRST SKIP. A template substitution, not a reasoning task.
            contract = self._template_contract(run, acceptance, checks)
            self.store.bump_efficiency(run.id, planner_skipped=1,
                                       decision=f"planner skipped: {why}")
            self.store.save_contract(contract, actor="engine")
            self.store.freeze_contract(contract.id, actor="engine")
            return self._to(run, state.PLAN_READY, action="contract_from_template",
                            reason=why,
                            detail={"contract_hash": contract.content_hash,
                                    "criteria": len(contract.criteria())})

        if self.runner is None:
            raise HarnessError(
                f"run {run.id} needs a Planner ({why}) but no AgentRunner is configured")
        if run.stage != state.PLANNING:
            run = self.store.advance(run.id, state.PLANNING, actor="engine",
                                     reason=why, owner=self.owner)
        pack, hit = self._pack_for(run)
        prompt = self.assembler.planner_prompt(
            task_title=run.title, task_prompt=run.prompt, acceptance=acceptance,
            checks=checks, pack=pack, cache_hit=hit)
        tier = policy.tier_for(run.mode, policy.PLANNER,
                               ambiguous=bool(run.policy.get("critical_areas")))
        result = self.runner.run(role=policy.PLANNER, prompt=prompt, run=run,
                                 tier=tier, session_id=None, iteration=0)
        self.assembler.charge(run.id, prompt,
                              completion_estimate=result.completion_tokens_estimate)
        if result.infra_failure:
            return self.record_infra_failure(run.id, result.error or "planner failed")
        try:
            contract = self._contract_from_payload(run, result.payload, acceptance, checks)
        except InsufficientSpecification as exc:
            # Discovered HERE, before a Builder spends an hour on work whose
            # completion is not a decidable question.
            self.store.open_decision(
                reason=policy.AMBIGUOUS_PRODUCT, run_id=run.id, task_id=run.task_id,
                project_id=run.project_id,
                question=f"{run.title or run.task_id}: the definition cannot support a "
                         f"contract ({', '.join(exc.missing)}). What should 'done' mean?",
                detail={"missing": exc.missing})
            return self._to(run, state.NEEDS_REDEFINE, action="insufficient_specification",
                            reason=", ".join(exc.missing), llm_called=True)
        self.store.patch_run(run.id, planner_agent=result.agent)
        self.store.save_contract(contract, actor=result.agent or "planner")
        self.store.freeze_contract(contract.id, actor=result.agent or "planner")
        run = self.store.require_run(run.id)
        return self._to(run, state.PLAN_READY, action="contract_from_planner",
                        llm_called=True,
                        detail={"contract_hash": contract.content_hash,
                                "criteria": len(contract.criteria()),
                                "prompt": prompt.to_dict()})

    def _template_contract(self, run: Any, acceptance: Sequence[str],
                           checks: Sequence[str]) -> ExecutionContract:
        return ExecutionContract.build(
            run_id=run.id, task_id=run.task_id,
            scope=run.title or run.prompt,
            functional_acceptance=acceptance,
            required_checks=checks,
            manual_checks=run.policy.get("declared_manual_checks") or (),
            affected_areas=run.policy.get("changed_paths") or (),
            planner_agent=None)

    def _contract_from_payload(self, run: Any, payload: Any,
                               acceptance: Sequence[str],
                               checks: Sequence[str]) -> ExecutionContract:
        data = payload
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        return ExecutionContract.build(
            run_id=run.id, task_id=run.task_id,
            scope=str(data.get("scope") or run.title or run.prompt),
            out_of_scope=data.get("out_of_scope"),
            affected_areas=data.get("affected_areas") or run.policy.get("changed_paths"),
            dependencies=data.get("dependencies"),
            # The task's own declared acceptance is a FLOOR, not a suggestion:
            # a Planner may add criteria, never quietly drop the ones the task
            # was filed with.
            functional_acceptance=tuple(data.get("functional_acceptance") or ())
                                  + tuple(a for a in acceptance
                                          if a not in (data.get("functional_acceptance") or ())),
            visual_acceptance=data.get("visual_acceptance"),
            performance_acceptance=data.get("performance_acceptance"),
            security_acceptance=data.get("security_acceptance"),
            required_checks=tuple(data.get("required_checks") or ())
                            + tuple(c for c in checks
                                    if c not in (data.get("required_checks") or ())),
            manual_checks=tuple(data.get("manual_checks") or ())
                          + tuple(run.policy.get("declared_manual_checks") or ()),
            dev_command=data.get("dev_command"),
            test_command=data.get("test_command"),
            build_command=data.get("build_command"))

    # =====================================================================
    # building
    # =====================================================================
    def _enter_build(self, run: Any) -> StepOutcome:
        contract = self.store.get_contract(run.id)
        if contract is None:
            raise NoContract(f"run {run.id} has no contract to build against")
        next_iteration = run.current_iteration + 1
        if next_iteration > run.max_iterations:
            return self._block_max_iterations(run)
        self.store.start_iteration(
            run.id, next_iteration, builder_agent=run.builder_agent,
            builder_session_id=run.builder_session_id, base_commit=run.base_commit,
            request_key=iteration_request_key(run.id, next_iteration))
        run = self.store.require_run(run.id)
        return self._to(run, state.BUILDING, action="iteration_opened",
                        detail={"iteration": next_iteration})

    def _build(self, run: Any) -> StepOutcome:
        contract = self.store.get_contract(run.id)
        if contract is None:
            raise NoContract(f"run {run.id} has no contract to build against")
        if self.runner is None:
            raise HarnessError(f"run {run.id} needs a Builder but no AgentRunner is configured")
        iteration = run.current_iteration or 1
        previous = self.store.latest_evaluation(run.id, iteration - 1) if iteration > 1 else None
        checkpoint = self.store.latest_checkpoint(run.id)

        prompt, reused = self._builder_prompt(run, contract, iteration, previous, checkpoint)
        tier = policy.tier_for(run.mode, policy.BUILDER,
                               failed_iterations=max(0, iteration - 1))
        result = self.runner.run(role=policy.BUILDER, prompt=prompt, run=run, tier=tier,
                                 session_id=run.builder_session_id if reused else None,
                                 iteration=iteration)
        self.assembler.charge(run.id, prompt,
                              completion_estimate=result.completion_tokens_estimate)
        self.store.bump_efficiency(
            run.id, session_reused=1 if reused else 0,
            sessions_spawned=0 if reused else 1,
            decision=("builder session reused (delta prompt)" if prompt.delta
                      else "builder session fresh (full prompt)"))
        if result.session_id:
            self.store.patch_run(run.id, builder_session_id=result.session_id,
                                 builder_agent=result.agent or run.builder_agent)
        if result.commit:
            self.store.patch_run(run.id, result_commit=result.commit)
        if result.infra_failure:
            return self.record_infra_failure(run.id, result.error or "builder failed")

        # A checkpoint after EVERY build, not only before a rollover. The
        # cheap moment to record where the work is, is while the builder is
        # still there to say; the expensive moment is after it is gone.
        run = self.store.require_run(run.id)
        self.store.save_checkpoint(
            run.id, task_id=run.task_id, iteration=iteration, branch=run.branch,
            commit_sha=result.commit or run.result_commit,
            worktree_path=run.worktree_path, session_id=result.session_id,
            checks=[c for c in contract.required_checks], remaining=[],
            note="builder reported done")
        self._apply_context_ladder(run, result.context_percent)
        run = self.store.require_run(run.id)
        return self._to(run, state.EVALUATING, action="built", llm_called=True,
                        detail={"iteration": iteration, "delta": prompt.delta,
                                "session_reused": reused,
                                "prompt": prompt.to_dict(),
                                "claimed_verdict": _safe_payload(result.payload)})

    def _builder_prompt(self, run: Any, contract: ExecutionContract, iteration: int,
                        previous: dict[str, Any] | None,
                        checkpoint: dict[str, Any] | None) -> tuple[ctx.Prompt, bool]:
        """(prompt, session_reused). THE THIRD SKIP lives here.

        A delta is only correct when the receiving session is the one that
        built the previous attempt. The engine checks that the session exists
        AND that the context ladder still allows reuse -- a session at 94%
        occupancy is about to be replaced, and handing it a delta would put
        the delta in the session that is about to be thrown away.
        """
        inherited = run.policy.get("session_inherited_from")
        if (iteration == 1 and previous is None and run.builder_session_id and inherited):
            # Cross-task reuse: this session built the task next door in the
            # same lane, so it already holds these modules. Only the contract
            # is new, and the contract is small.
            return self.assembler.adjacent_task_prompt(
                contract=contract, previous_task_id=str(inherited)), True

        reusable = (previous is not None
                    and run.builder_session_id is not None
                    and checkpoint is not None
                    and checkpoint.get("session_id") == run.builder_session_id)
        if reusable:
            evaluation = _evaluation_from_row(previous, contract)
            if evaluation is not None and evaluation.failed_criteria:
                return self.assembler.revision_prompt(
                    evaluation=evaluation, contract=contract, iteration=iteration), True
        pack, hit = self._pack_for(run, contract=contract)
        return self.assembler.builder_prompt(
            contract=contract, iteration=iteration, pack=pack, cache_hit=hit,
            checkpoint=checkpoint if (checkpoint and not reusable) else None), False

    # =====================================================================
    # evaluating
    # =====================================================================
    def _evaluate(self, run: Any) -> StepOutcome:
        contract = self.store.get_contract(run.id)
        if contract is None:
            raise NoContract(f"run {run.id} has no contract to evaluate against")
        iteration = run.current_iteration or 1
        cwd = run.worktree_path or self.repo_root
        results = self.check_runner(contract.required_checks, cwd=cwd)
        self.store.record_artifact(
            run.id, kind="check_output", iteration=iteration,
            path=f".harness/{run.id}/iteration-{iteration}/checks.json",
            metadata={"checks": [r.to_dict() for r in results]})

        failed_checks = [r for r in results if not r.ok]
        if failed_checks:
            # THE SECOND SKIP, and the cheapest one in the system: a declared
            # check exited non-zero, which is not a matter of opinion. No
            # model is asked to confirm it.
            evaluation = self._verdict_from_checks(contract, iteration, results)
            self.store.bump_efficiency(
                run.id, evaluator_skipped=1,
                decision="evaluator skipped: a declared check failed, "
                         "which is not a judgement call")
            return self._settle(run, contract, evaluation, iteration,
                                action="failed_declared_check", llm_called=False,
                                detail={"failed_checks": [r.command for r in failed_checks]})

        independent, why = policy.evaluator_required(run.mode)
        stale = uncorroborated_criteria(contract, results)
        if stale and not independent:
            # The checks are green but do not decide these criteria. This is
            # the one case where a cheap mode buys an Evaluator call: not
            # because the mode changed, but because the evidence it was going
            # to rely on turned out not to be evidence for this criterion.
            independent, why = True, (
                f"declared checks passed but do not decide {len(stale)} criterion(s): "
                f"{', '.join(stale)}")
            self.store.bump_efficiency(run.id, decision=f"evaluator escalation: {why}")
            if self.runner is None:
                self.store.open_decision(
                    reason=policy.AMBIGUOUS_PRODUCT, run_id=run.id, task_id=run.task_id,
                    project_id=run.project_id,
                    question=f"{run.title or run.task_id}: {why}. What command decides them?",
                    detail={"uncorroborated": list(stale),
                            "checks": [r.to_dict() for r in results]})
                return self._to(run, state.BLOCKED, action="uncorroborated_criteria",
                                reason=why, detail={"uncorroborated": list(stale)})
        if not independent:
            evaluation = self._verdict_from_checks(contract, iteration, results)
            self.store.bump_efficiency(run.id, evaluator_skipped=1,
                                       decision=f"evaluator skipped: {why}")
            return self._settle(run, contract, evaluation, iteration,
                                action="verified_by_declared_checks", llm_called=False,
                                detail={"checks": [r.command for r in results]})

        if self.runner is None:
            raise HarnessError(
                f"run {run.id} needs an independent Evaluator ({why}) but no "
                f"AgentRunner is configured")
        prompt = self.assembler.evaluator_prompt(
            contract=contract, iteration=iteration, result_commit=run.result_commit,
            check_output="\n\n".join(f"$ {r.command}\n{r.output}" for r in results))
        tier = policy.tier_for(run.mode, policy.EVALUATOR,
                               failed_iterations=max(0, iteration - 1))
        # A FRESH session, always. The whole value of a CRITICAL evaluator is
        # that it never saw the Builder's reasoning, so reusing the builder's
        # session would buy the saving by destroying the thing being paid for.
        result = self.runner.run(role=policy.EVALUATOR, prompt=prompt, run=run,
                                 tier=tier, session_id=None, iteration=iteration)
        self.assembler.charge(run.id, prompt,
                              completion_estimate=result.completion_tokens_estimate)
        self.store.bump_efficiency(run.id, sessions_spawned=1)
        if result.infra_failure:
            return self.record_infra_failure(run.id, result.error or "evaluator failed")
        try:
            evaluation = parse_verdict(
                result.payload, contract, iteration=iteration,
                evaluator_agent=result.agent, evaluator_session_id=result.session_id,
                result_commit=run.result_commit)
        except MalformedVerdict as exc:
            # A verdict that cannot be checked is not a verdict. This is an
            # infrastructure failure of the evaluator, not a product failure
            # of the code -- so it resumes rather than burning an iteration.
            return self.record_infra_failure(
                run.id, "malformed verdict: " + "; ".join(exc.problems))
        return self._settle(run, contract, evaluation, iteration,
                            action="independent_evaluation", llm_called=True,
                            detail={"prompt": prompt.to_dict()})

    def _verdict_from_checks(self, contract: ExecutionContract, iteration: int,
                             results: Sequence[CheckResult]) -> EvaluationResult:
        """Build a structured verdict from check exit statuses alone.

        Every declared criterion is answered, because a partial verdict is a
        malformed one. When the checks all passed, each criterion is recorded
        as passing WITH the commands that were run as its evidence -- so the
        audit trail says exactly what was and was not actually verified,
        rather than implying a human-grade inspection that never happened.
        """
        from .harness_contract import CriterionResult, new_id
        ok = all(r.ok for r in results)
        failures = [r for r in results if not r.ok]
        evidence_pass = "; ".join(f"{r.command} -> exit 0" for r in results) or \
                        "contract declared no runnable check"
        evidence_fail = "\n".join(
            f"$ {r.command}\nexit {r.exit_code}\n{r.output[-1500:]}" for r in failures)
        criteria = tuple(
            CriterionResult(criterion_id=criterion_id(kind, text), kind=kind, text=text,
                            result=("pass" if ok else "fail"),
                            evidence=(evidence_pass if ok else evidence_fail))
            for kind, text in contract.criteria())
        return EvaluationResult(
            id=new_id("evl"), run_id=contract.run_id, iteration=iteration,
            contract_id=contract.id, contract_hash=contract.content_hash,
            result=("pass" if ok else "fail"), criteria=criteria,
            evaluator_agent="engine:declared-checks",
            summary=("all declared checks passed" if ok
                     else f"{len(failures)} declared check(s) failed"),
            failure_class=(None if ok else state.PRODUCT_FAILURE))

    def _settle(self, run: Any, contract: ExecutionContract,
                evaluation: EvaluationResult, iteration: int, *, action: str,
                llm_called: bool, detail: dict[str, Any] | None = None) -> StepOutcome:
        """The pass/fail edge. This is where FAIL becomes REVISING, not BLOCKED."""
        self.store.record_evaluation(evaluation)
        self.store.record_artifact(
            run.id, kind="verdict", iteration=iteration,
            path=f".harness/{run.id}/iteration-{iteration}/verdict.json",
            metadata=evaluation.to_dict())
        self.store.complete_iteration(
            run.id, iteration, verdict=evaluation.result,
            failure_class=evaluation.failure_class,
            result_commit=run.result_commit,
            evaluator_agent=evaluation.evaluator_agent,
            evaluator_session_id=evaluation.evaluator_session_id)
        info = dict(detail or {})
        info["verdict"] = evaluation.result
        info["failed_criteria"] = [c.criterion_id for c in evaluation.failed_criteria]

        if evaluation.result == state.PASS:
            return self._to(run, state.MERGE_READY, action=action, llm_called=llm_called,
                            reason="every declared criterion passed", detail=info)
        if evaluation.result == state.NEEDS_REDEFINE_VERDICT:
            self.store.open_decision(
                reason=policy.AMBIGUOUS_PRODUCT, run_id=run.id, task_id=run.task_id,
                project_id=run.project_id,
                question=f"{run.title or run.task_id}: the evaluator reports the contract "
                         f"itself is wrong. {evaluation.summary}",
                detail={"contract_hash": contract.content_hash})
            return self._to(run, state.NEEDS_REDEFINE, action=action,
                            llm_called=llm_called, reason=evaluation.summary, detail=info)
        if evaluation.result == state.BLOCKED_VERDICT:
            # The evaluator could not reach a verdict because something
            # outside the code stopped it -- a missing credential, a
            # permission. That IS on the human list.
            self.store.open_decision(
                reason=policy.PERMISSION_REQUIRED, run_id=run.id, task_id=run.task_id,
                project_id=run.project_id,
                question=f"{run.title or run.task_id}: evaluation is blocked. "
                         f"{evaluation.summary}",
                detail=info)
            return self._to(run, state.BLOCKED, action=action, llm_called=llm_called,
                            reason=evaluation.summary or "evaluation blocked", detail=info)

        # FAIL. The machine's problem, and the whole reason this edge exists.
        budget = policy.budget_action(run.tokens_spent_estimate, run.soft_token_budget)
        if budget != policy.BUDGET_OK:
            self.store.bump_efficiency(run.id, decision=f"budget: {budget}")
            info["budget_action"] = budget
        if iteration >= run.max_iterations:
            return self._block_max_iterations(run, detail=info)
        return self._to(run, state.REVISING, action=action, llm_called=llm_called,
                        reason=f"{len(evaluation.failed_criteria)} criteria failed; "
                               f"revising without human involvement", detail=info)

    def _block_max_iterations(self, run: Any, detail: dict[str, Any] | None = None
                              ) -> StepOutcome:
        """The ONE path from a verdict to a human, and it is not about the code.

        Reaching the iteration cap says the loop is not converging, which is a
        fact about the run. The question put to the human is therefore about
        the definition, not about the test output.
        """
        self.store.open_decision(
            reason=policy.MAX_ITERATIONS_REACHED, run_id=run.id, task_id=run.task_id,
            project_id=run.project_id,
            question=f"{run.title or run.task_id}: {run.max_iterations} iterations did not "
                     f"converge. Is the contract wrong, or is the work bigger than the task?",
            detail=detail or {})
        return self._to(run, state.BLOCKED, action="max_iterations",
                        reason=f"{run.max_iterations} iterations without a pass",
                        detail=detail or {})

    # =====================================================================
    # interruption, checkpoints and session replacement
    # =====================================================================
    def record_infra_failure(self, run_id: str, error: str) -> StepOutcome:
        """The machinery broke; the work did not.

        Counts consecutive failures, and only at the threshold does this
        become a human's problem. Below it, the correct action is always
        RESUME -- same run, same iteration, same worktree.
        """
        run = self.store.require_run(run_id)
        count = run.infra_failure_count + 1
        self.store.patch_run(run_id, infra_failure_count=count, last_error=error)
        run = self.store.require_run(run_id)
        if count >= policy.INFRA_FAILURE_ESCALATION_THRESHOLD:
            self.store.open_decision(
                reason=policy.REPEATED_INFRA_FAILURE, run_id=run.id, task_id=run.task_id,
                project_id=run.project_id,
                question=f"{run.title or run.task_id}: {count} consecutive infrastructure "
                         f"failures. Last error: {error}",
                detail={"error": error, "count": count})
            return self._to(run, state.BLOCKED, action="repeated_infra_failure",
                            reason=error, detail={"count": count})
        return self._to(run, state.FAILED_INFRA, action="infra_failure", reason=error,
                        detail={"count": count})

    def resume(self, run_id: str) -> StepOutcome:
        """Return to the stage the run was actually in. Never to INIT.

        There is no edge from a resumable stage back to INIT in the transition
        table, so "resume" cannot degrade into "restart from the prompt" even
        if a caller asks for it.
        """
        run = self.store.require_run(run_id)
        target = run.resume_stage or state.PLAN_READY
        if target not in state.RESUMABLE_STAGES:
            target = state.PLAN_READY
        checkpoint = self.store.latest_checkpoint(run.id)
        run = self.store.advance(
            run.id, target, actor="engine", owner=self.owner,
            reason=f"resumed at {target} from checkpoint "
                   f"{(checkpoint or {}).get('id') or '(none)'}",
            metadata={"checkpoint": (checkpoint or {}).get("id"),
                      "iteration": run.current_iteration})
        return StepOutcome(run_id=run.id, from_stage=state.FAILED_INFRA, to_stage=target,
                           action="resumed",
                           detail={"iteration": run.current_iteration,
                                   "checkpoint": (checkpoint or {}).get("id"),
                                   "worktree_path": run.worktree_path})

    def checkpoint(self, run_id: str, *, note: str = "", remaining: Sequence[str] = (),
                   checks_done: Sequence[str] = (), commit_sha: str | None = None
                   ) -> dict[str, Any]:
        run = self.store.require_run(run_id)
        return self.store.save_checkpoint(
            run.id, task_id=run.task_id, iteration=run.current_iteration,
            branch=run.branch, commit_sha=commit_sha or run.result_commit,
            worktree_path=run.worktree_path, session_id=run.builder_session_id,
            checks=list(checks_done), remaining=list(remaining), note=note)

    def replace_builder_session(self, run_id: str, *, new_session_id: str,
                                reason: str = "context rollover") -> dict[str, Any]:
        """Swap the Builder's session WITHOUT touching the run's place in the world.

        The run id, the iteration, the branch, the worktree and the contract
        are all left exactly as they are; only `builder_session_id` changes.
        The replacement then gets a FULL prompt plus the checkpoint, because it
        has no history a delta could refer to -- `_builder_prompt` works that
        out from the session id rather than from a flag.
        """
        run = self.store.require_run(run_id)
        checkpoint = self.store.latest_checkpoint(run.id) or self.checkpoint(
            run.id, note="auto-checkpoint before session replacement")
        previous = run.builder_session_id
        self.store.patch_run(run.id, builder_session_id=new_session_id)
        self.store.bump_efficiency(run.id, rollover_count=1, sessions_spawned=1,
                                   decision=f"builder session replaced: {reason}")
        self.store.record_event(
            run.id, event_type="BUILDER_SESSION_REPLACED",
            iteration=run.current_iteration, actor=new_session_id, reason=reason,
            metadata={"previous_session": previous, "new_session": new_session_id,
                      "checkpoint": checkpoint.get("id"),
                      "worktree_path": run.worktree_path, "branch": run.branch})
        return {"run_id": run.id, "iteration": run.current_iteration,
                "previous_session": previous, "new_session": new_session_id,
                "worktree_path": run.worktree_path, "branch": run.branch,
                "checkpoint": checkpoint}

    def _apply_context_ladder(self, run: Any, context_percent: float | None) -> None:
        action = policy.context_action(context_percent)
        if action == policy.CONTINUE:
            return
        self.store.bump_efficiency(
            run.id, decision=f"context {context_percent}% -> {action}")
        if action in policy.CONTEXT_FORCES_CHECKPOINT:
            self.store.save_checkpoint(
                run.id, task_id=run.task_id, iteration=run.current_iteration,
                branch=run.branch, commit_sha=run.result_commit,
                worktree_path=run.worktree_path, session_id=run.builder_session_id,
                checks=[], remaining=[], note=f"context ladder: {action}")

    # =====================================================================
    # merging
    # =====================================================================
    def approve_merge(self, run_id: str, *, approved_by: str,
                      merge: Callable[[Any], str | None] | None = None) -> StepOutcome:
        """MERGE_READY -> MERGED. The only stage a merge may be invoked from.

        SUPERVISED requires this call to name a human (or the PM acting for
        one). AUTONOMOUS may call it itself -- and still only from here, and
        still through the injected merge executor, because "the harness may
        decide to merge" and "the harness merges by its own means" are
        different permissions and only the first one is granted.
        """
        run = self.store.require_run(run_id)
        if run.stage != state.MERGE_READY:
            raise HarnessError(f"run {run_id} is {run.stage}, not MERGE_READY")
        authority = run.write_authority
        if not policy.MAY_SELF_APPROVE_MERGE.get(authority, False) and \
                approved_by.startswith("engine"):
            raise HarnessError(
                f"{authority} authority may not self-approve a merge; a human or the "
                f"PM must call approve_merge")
        commit = merge(run) if merge else None
        if commit:
            self.store.patch_run(run.id, result_commit=commit)
            run = self.store.require_run(run.id)
        return self._to(run, state.MERGED, action="merged", actor=approved_by,
                        reason=f"approved by {approved_by}",
                        detail={"commit": commit})

    # =====================================================================
    # helpers
    # =====================================================================
    def _pack_for(self, run: Any, contract: ExecutionContract | None = None
                  ) -> tuple[ctx.ContextPack | None, bool]:
        modules = list(contract.affected_areas if contract else ())
        if not modules:
            modules = list(run.policy.get("changed_paths") or ())
        if not modules or self.repo_root is None:
            return None, False
        return self.assembler.build_pack(
            project_id=run.project_id, modules=modules,
            tags=tuple(run.policy.get("critical_areas") or ()), run_id=run.id)

    def _to(self, run: Any, to_stage: str, *, action: str, reason: str | None = None,
            actor: str = "engine", detail: dict[str, Any] | None = None,
            llm_called: bool = False) -> StepOutcome:
        from_stage = run.stage
        if from_stage == to_stage:
            updated = run
        else:
            updated = self.store.advance(run.id, to_stage, actor=actor, reason=reason,
                                         owner=self.owner, metadata=detail)
        self._project_to_queue(updated)
        return StepOutcome(run_id=run.id, from_stage=from_stage, to_stage=to_stage,
                           action=action, detail=detail or {}, llm_called=llm_called)

    def _project_to_queue(self, run: Any) -> None:
        """SHADOW writes nothing outside the harness tables.

        Enforced by not calling the projector rather than by the projector
        checking -- a projector that had to check could be replaced with one
        that forgot to.
        """
        if self.queue_projector is None:
            return
        if not policy.WRITES_QUEUE_STATUS.get(run.write_authority, False):
            self.store.patch_run(run.id, shadow_of_task_status=run.projected_stage)
            return
        self.queue_projector(run, run.projected_stage)


def _safe_payload(payload: Any) -> Any:
    if payload is None or isinstance(payload, (str, int, float, bool)):
        return payload
    try:
        json.dumps(payload)
        return payload
    except (TypeError, ValueError):
        return str(payload)


def _evaluation_from_row(row: dict[str, Any], contract: ExecutionContract
                         ) -> EvaluationResult | None:
    """Rehydrate a stored verdict so a revision prompt can be built from it."""
    from .harness_contract import CriterionResult
    if not row:
        return None
    criteria = tuple(
        CriterionResult(criterion_id=c.get("criterion_id", ""), kind=c.get("kind", ""),
                        text=c.get("text", ""), result=c.get("result", ""),
                        evidence=c.get("evidence", ""), artifact=c.get("artifact"))
        for c in (row.get("criteria") or []))
    return EvaluationResult(
        id=row.get("id", ""), run_id=row.get("run_id", ""),
        iteration=int(row.get("iteration") or 0),
        contract_id=row.get("contract_id", ""),
        contract_hash=row.get("contract_hash", contract.content_hash),
        result=row.get("result", ""), criteria=criteria,
        evaluator_agent=row.get("evaluator_agent"),
        evaluator_session_id=row.get("evaluator_session_id"),
        result_commit=row.get("result_commit"), summary=row.get("summary") or "",
        failure_class=row.get("failure_class"))
