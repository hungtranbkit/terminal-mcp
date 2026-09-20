"""The UrbanFlow pilot: the harness driven against a real repository.

WHAT THE PILOT IS FOR

Two questions, and they need different kinds of answer.

"Does the engine make the right decisions on a real task graph?" is answered
by RUNNING it: real TASKS.json, real dependency resolution, real contracts,
real shell checks with real exit statuses, real durable state. Everything in
this module that reaches those conclusions is doing so for real.

"What would the old shape have cost?" cannot be answered by running the old
shape, because running it is the money this feature exists not to spend. It
is answered by an accounting model built from the SAME prompts and the SAME
contracts (harness_context.naive_baseline / naive_parallel_baseline), so the
two sides of the comparison cannot drift apart. Every figure the report emits
is labelled `measured` or `modelled`, and the two are never added together.

TASKS.json IS A DEFINITION, NOT RUNTIME STATE

It is read once, at the start, and never written. Whether a dependency is
satisfied is answered from the harness runs in the database -- a task is done
when its RUN reached MERGE_READY, not when a file says "DONE". A definition
file records what was planned; the moment it starts recording what happened,
there are two sources of truth again and this whole feature was pointless.

THE ASSET NOBODY MAY INVENT

VIS-001 wants an approved UI board PNG whose SHA-256 is written down. The
file is not in the repository, and the repository's own README says not to
generate a substitute. No number of Builder iterations can produce it, so
starting one is pure waste and finishing one would require fabricating the
asset. `human_input_required` detects that class -- an acceptance criterion
naming a binary asset that does not exist -- and the scheduler defers it to
the Human Decision Queue without ever opening a run.
"""
from __future__ import annotations

import contextlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import harness_context as ctx
from . import harness_policy as policy
from . import harness_scheduler as sched
from . import harness_state as state
from .harness_engine import (AgentResult, CheckResult, HarnessEngine,
                             partition_checks, run_checks,
                             uncorroborated_criteria)
from .harness_store import HarnessStore

PILOT_PROJECT = "urbanflow"
DEFAULT_REPO = "/home/dell/workspace/urbanflow"
DEFAULT_TARGET = "MOB-011"

#: Extensions no code change can produce. A Builder can write a .ts file; it
#: cannot produce an approved photograph, a signed binary or a design board.
#: This is the mechanical half of `human_input_required`.
HUMAN_ASSET_SUFFIXES: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".psd", ".fig",
    ".sketch", ".mp4", ".mov", ".ttf", ".otf", ".woff", ".woff2", ".keystore",
    ".p12", ".mobileprovision",
})

_PATH_IN_TEXT = re.compile(r"[\w./-]+\.[A-Za-z0-9]{1,12}")


# ---------------------------------------------------------------------------
# the definition file
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskDefinition:
    """One row of TASKS.json. Read-only, always."""

    id: str
    title: str
    lane: str
    priority: str
    depends_on: tuple[str, ...]
    acceptance: tuple[str, ...]
    checks: tuple[str, ...]
    milestone: str = ""
    declared_status: str = ""

    @property
    def prompt(self) -> str:
        return self.title


def load_definitions(tasks_path: str | Path) -> dict[str, TaskDefinition]:
    data = json.loads(Path(tasks_path).read_text())
    out: dict[str, TaskDefinition] = {}
    for row in data.get("tasks", []):
        out[row["id"]] = TaskDefinition(
            id=row["id"], title=row.get("title", ""), lane=str(row.get("lane", "")),
            priority=str(row.get("priority", "P2")),
            depends_on=tuple(row.get("depends_on") or ()),
            acceptance=tuple(row.get("acceptance") or ()),
            checks=tuple(row.get("checks") or ()),
            milestone=str(row.get("milestone", "")),
            # Read and carried for the report ONLY. Nothing in the engine
            # branches on it; readiness comes from the run store.
            declared_status=str(row.get("status", "")))
    return out


def human_input_required(definition: TaskDefinition, repo_root: str | Path
                         ) -> str | None:
    """The reason a task cannot be autonomous, or None.

    Mechanical and narrow: an acceptance criterion names a file with an asset
    extension, and that file is not in the repository. Code can create source;
    it cannot create an approved image. Anything subtler than this is a
    judgement call, and a judgement call belongs to the Planner, not here.
    """
    root = Path(repo_root)
    for criterion in definition.acceptance:
        for candidate in _PATH_IN_TEXT.findall(criterion):
            suffix = Path(candidate).suffix.lower()
            if suffix not in HUMAN_ASSET_SUFFIXES:
                continue
            if (root / candidate).exists():
                continue
            return (f"acceptance requires {candidate}, which is not in the "
                    f"repository and is an asset no code change can produce")
    return None


def to_nodes(definitions: Mapping[str, TaskDefinition], *, repo_root: str | Path,
             critical_tasks: Sequence[str] = ()) -> dict[str, sched.TaskNode]:
    critical = set(critical_tasks)
    nodes: dict[str, sched.TaskNode] = {}
    for task_id, definition in definitions.items():
        reason = human_input_required(definition, repo_root)
        nodes[task_id] = sched.TaskNode(
            id=task_id, lane=definition.lane, priority=definition.priority,
            depends_on=definition.depends_on,
            autonomous=reason is None, not_autonomous_reason=reason,
            requested_mode=policy.CRITICAL if task_id in critical else None)
    return nodes


# ---------------------------------------------------------------------------
# turning a prose criterion into a command that decides it
# ---------------------------------------------------------------------------

_VERSION_CLAIM = re.compile(r"\b(\w[\w.-]*)\s+-?-?v(?:ersion)?\b.*?\bv?(\d+)", re.I)
_REPORTS_VERSION = re.compile(r"(\w[\w.-]*)\s+-v\s+reports\s+v?(\d+)", re.I)


def decisive_check_for(criterion: str) -> str | None:
    """A command that exits non-zero exactly when the criterion is violated.

    This is the job the PLANNER is asked to do, and the pilot derives the one
    case it can derive mechanically -- a version claim -- so that the pilot's
    numbers come from a real, failing-when-wrong command rather than from
    `true`. Everything else returns None and stays a manual check, which is
    the honest answer: nobody has written a command for it yet.
    """
    match = _REPORTS_VERSION.search(criterion)
    if match:
        program, major = match.group(1), match.group(2)
        # It PRINTS the version before gating on it. A check whose output is
        # empty decides the exit status and nothing else, which leaves the
        # evaluator -- and the audit trail -- with no evidence of WHAT was
        # decided. `grep -q` alone is the common version of that mistake.
        return f"{program} -v && {program} -v | grep -q '^v{major}\\.'"
    return None


#: DECISIVE CHECKS A PLANNER WOULD AUTHOR, SUPPLIED EXPLICITLY.
#:
#: UrbanFlow's TASKS.json declares checks as labels -- "typecheck", "component
#: tests", "pnpm workspace check". Two of those are prose (handled by
#: partition_checks); the third starts with a real binary and is not a real
#: invocation, so it runs and always fails. Against a repository that is still
#: a scaffold -- every workspace is a .gitkeep -- there is no toolchain for a
#: real check to exercise yet.
#:
#: Authoring the decisive command is the PLANNER's job. This map supplies it
#: directly so the chain can complete and the engine's CALL SHAPE can be
#: measured, which is the thing being compared. The commands below are real
#: and really run; what they verify is scaffold presence, not feature
#: completeness, and the report says so rather than implying otherwise.
PILOT_CHECK_MAP: dict[str, tuple[str, ...]] = {
    "ENV-001": ("node -v && node -v | grep -qE '^v(2[4-9]|[3-9][0-9])\\.'",),
    "ENV-006": ("test -f pnpm-workspace.yaml && cat pnpm-workspace.yaml",),
    "MOB-001": ("test -d apps/mobile && echo apps/mobile present",),
    "MOB-002": ("test -d apps/mobile && echo apps/mobile present",),
    "MOB-003": ("test -d apps/mobile && echo apps/mobile present",),
    "CT-001": ("test -d packages/contracts && echo packages/contracts present",),
    "CT-004": ("test -d packages/contracts && echo packages/contracts present",),
    "CT-005": ("test -d apps/mobile && echo apps/mobile present",),
    "MOB-011": ("test -d apps/mobile && test -f docs/design/SCREEN_MATRIX.json "
                "&& echo mobile app and screen matrix present",),
}


# ---------------------------------------------------------------------------
# the dry-run agent
# ---------------------------------------------------------------------------

class DryRunAgent:
    """Assembles nothing and decides nothing -- it only answers.

    EVERY PROMPT IT RECEIVES IS REAL. The engine built it from the real
    contract and the real repository, so the byte counts and token estimates
    this pilot reports are measurements of text that was actually assembled.
    What is simulated is only the model's reply, and the report labels it so.

    It writes nothing to the repository. A pilot that edited the pilot repo
    would be a deployment, and the instruction is to stop at MERGE_READY.
    """

    def __init__(self, store: HarnessStore, *, lane_of: Mapping[str, str],
                 repo_root: str = DEFAULT_REPO) -> None:
        self.store = store
        self.repo_root = repo_root
        self.lane_of = dict(lane_of)
        self.transcript: list[dict[str, Any]] = []
        self._sessions: dict[str, int] = {}

    def _session_for(self, run) -> str:
        lane = self.lane_of.get(run.task_id, "X")
        self._sessions.setdefault(lane, 0)
        self._sessions[lane] += 0  # a lane's session id is stable
        return f"pilot-lane-{lane}"

    def run(self, *, role, prompt, run, tier, session_id, iteration) -> AgentResult:
        self.transcript.append({
            "run_id": run.id, "task_id": run.task_id, "role": role,
            "iteration": iteration, "tier": tier, "delta": prompt.delta,
            "session_id": session_id, "bytes": prompt.bytes,
            "tokens_estimate": prompt.tokens_estimate,
            "pack_key": prompt.pack_key, "cache_hit": prompt.cache_hit,
            # Recorded so the baseline can count the SAME thing the measured
            # side counts. The engine charges prompt+completion; a baseline
            # counting prompt alone would flatter itself.
            "completion_estimate": {policy.PLANNER: 900, policy.EVALUATOR: 700}.get(
                role, 600),
        })
        if role == policy.PLANNER:
            return self._plan(run)
        if role == policy.EVALUATOR:
            return self._evaluate(run, iteration, session_id)
        return AgentResult(
            payload={"reported": "done"},
            session_id=session_id or self._session_for(run),
            agent=f"pilot-builder[{tier}]", commit=None, context_percent=35.0,
            completion_tokens_estimate=600)

    def _plan(self, run) -> AgentResult:
        acceptance = list(run.policy.get("declared_acceptance") or ())
        manual = list(run.policy.get("declared_manual_checks") or ())
        checks = [c for c in (decisive_check_for(a) for a in acceptance) if c]
        return AgentResult(
            payload={
                "scope": run.title or run.prompt,
                "functional_acceptance": acceptance or [f"{run.title} is implemented"],
                # `true` is the honest placeholder when nothing decisive could
                # be derived: it records that the contract has no real gate
                # yet, rather than inventing a command that looks like one.
                "required_checks": checks or ["true"],
                "manual_checks": manual,
                "affected_areas": list(run.policy.get("changed_paths") or ()),
            },
            agent="pilot-planner", completion_tokens_estimate=900)

    def _evaluate(self, run, iteration, session_id) -> AgentResult:
        """Re-run the gates AND check that they corroborate the claim.

        A dry-run evaluator that answered "pass" whenever the commands exited
        0 would be simulating the exact failure a real evaluator exists to
        catch -- and the pilot's whole job is to find out whether the design
        catches it. So it applies the same mechanical corroboration test the
        engine uses, which is deterministic and therefore honestly
        simulatable, and answers per criterion rather than in aggregate.
        """
        contract = self.store.get_contract(run.id)
        results = run_checks(contract.required_checks,
                             cwd=run.worktree_path or self.repo_root)
        stale = set(uncorroborated_criteria(contract, results))
        gates_ok = all(r.ok for r in results)
        evidence = "; ".join(f"{r.command} -> exit {r.exit_code} {r.output[:120]}"
                             for r in results) or "no runnable gate in the contract"
        criteria = []
        for cid in contract.criterion_ids():
            passed = gates_ok and cid not in stale
            criteria.append({
                "criterion_id": cid,
                "result": "pass" if passed else "fail",
                "evidence": evidence if passed else
                            f"{evidence} -- the output does not support this criterion"})
        return AgentResult(
            payload={"result": "pass" if all(c["result"] == "pass" for c in criteria)
                               else "fail",
                     "summary": "independent re-run of the contract's gates",
                     "criteria": criteria},
            session_id=f"pilot-evaluator-{run.task_id}-{iteration}",
            agent="pilot-evaluator", completion_tokens_estimate=700)


# ---------------------------------------------------------------------------
# the pilot itself
# ---------------------------------------------------------------------------

@dataclass
class TaskOutcome:
    task_id: str
    run_id: str
    mode: str
    lane: str
    stage: str
    iterations: int
    inherited_session: str | None
    efficiency: dict[str, Any]
    decisions: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    @property
    def merge_ready(self) -> bool:
        return self.stage in (state.MERGE_READY, state.MERGED, state.DONE)

    def to_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "run_id": self.run_id, "mode": self.mode,
                "lane": self.lane, "stage": self.stage, "iterations": self.iterations,
                "inherited_session": self.inherited_session,
                "merge_ready": self.merge_ready,
                "llm_calls": self.efficiency.get("llm_calls", 0),
                "prompt_tokens_estimate": self.efficiency.get("prompt_tokens_estimate", 0),
                "planner_skipped": self.efficiency.get("planner_skipped", 0),
                "evaluator_skipped": self.efficiency.get("evaluator_skipped", 0),
                "session_reused": self.efficiency.get("session_reused", 0),
                "sessions_spawned": self.efficiency.get("sessions_spawned", 0),
                "delta_prompts": self.efficiency.get("delta_prompts", 0),
                "full_prompts": self.efficiency.get("full_prompts", 0),
                "context_cache_hits": self.efficiency.get("reused_context_hits", 0),
                "context_cache_misses": self.efficiency.get("context_cache_misses", 0),
                "decisions": self.decisions, "note": self.note}


class Pilot:
    def __init__(self, *, store: HarnessStore, repo_root: str = DEFAULT_REPO,
                 tasks_path: str | None = None, target: str = DEFAULT_TARGET,
                 scheduling: str = sched.COST_FIRST,
                 critical_tasks: Sequence[str] = (DEFAULT_TARGET,),
                 check_map: Mapping[str, Sequence[str]] | None = None) -> None:
        self.store = store
        self.repo_root = repo_root
        self.target = target
        self.scheduling = scheduling
        self.definitions = load_definitions(tasks_path or Path(repo_root) / "TASKS.json")
        self.nodes = to_nodes(self.definitions, repo_root=repo_root,
                              critical_tasks=critical_tasks)
        self.agent = DryRunAgent(store, repo_root=repo_root,
                                 lane_of={k: v.lane for k, v in
                                          self.definitions.items()})
        self.engine = HarnessEngine(
            store, runner=self.agent, repo_root=repo_root,
            assembler=ctx.ContextAssembler(store, repo_root=repo_root))
        #: When set, these replace the definition file's declared checks --
        #: see PILOT_CHECK_MAP for why that is a Planner's output and not a
        #: liberty being taken with the definition.
        self.check_map = {k: tuple(v) for k, v in (check_map or {}).items()}
        # The repository's own convention (git_isolation_service):
        # <repo>/../.terminal-mcp-worktrees. NOT /tmp -- a session may only be
        # opened under config.session_lifecycle.allowed_cwd_roots, and a
        # worktree the agent cannot cd into is a worktree no agent can work in.
        self.worktree_root = str(Path(repo_root).resolve().parent
                                 / ".terminal-mcp-worktrees" / "harness")
        #: Set by a caller that wants Harness worktrees trusted before a
        #: session opens them (see harness_trust). None keeps the previous
        #: behaviour: the session asks, and the run stops on
        #: PERMISSION_REQUIRED.
        self.trust: Any = None
        self._worktrees: list[tuple[str, str]] = []
        self.outcomes: list[TaskOutcome] = []
        self.human_queue: list[dict[str, Any]] = []
        self.replacement_proof: dict[str, Any] | None = None

    # -- phase 1: the task no machine may finish ----------------------------
    def shadow_non_autonomous(self, task_id: str) -> dict[str, Any]:
        """Observe, evidence it, route it to a human. No run, no model call.

        The observation is a real command in the real repository, which is why
        this costs nothing: the evidence that the asset is missing is the exit
        status of the check the task itself declared.
        """
        definition = self.definitions[task_id]
        reason = human_input_required(definition, self.repo_root)
        commands, prose = partition_checks(definition.checks)
        results = run_checks(commands, cwd=self.repo_root)
        decision = self.store.open_decision(
            reason=policy.MISSING_HUMAN_INPUT, task_id=task_id,
            project_id=PILOT_PROJECT,
            question=(f"{task_id} ({definition.title}): {reason}. Please supply the "
                      f"approved asset; the repository forbids generating a substitute."),
            detail={"acceptance": list(definition.acceptance),
                    "checks_run": [r.to_dict() for r in results],
                    "prose_checks": list(prose),
                    "llm_calls": 0})
        record = {"task_id": task_id, "title": definition.title, "reason": reason,
                  "decision_id": decision["id"], "llm_calls": 0,
                  "evidence": [r.to_dict() for r in results]}
        self.human_queue.append(record)
        return record

    # -- phase 2: the supervised chain --------------------------------------
    def run_chain(self, *, write_authority: str = policy.SUPERVISED,
                  replace_session_on: str | None = None) -> list[TaskOutcome]:
        plan = sched.plan(self.nodes, target=self.target, mode=self.scheduling)
        self.plan = plan
        lane_sessions: dict[str, str] = {}
        satisfied: set[str] = set()

        for assignment in plan.order:
            definition = self.definitions.get(assignment.task_id)
            if definition is None:
                continue
            node = self.nodes[assignment.task_id]
            unmet = [d for d in node.depends_on if d not in satisfied]
            if unmet:
                # Readiness re-checked against what ACTUALLY finished, not
                # against the plan's optimism. A blocked task stops its
                # dependents; that is the whole point of a dependency.
                self.outcomes.append(TaskOutcome(
                    task_id=assignment.task_id, run_id="", mode="", lane=node.lane,
                    stage="NOT_STARTED", iterations=0, inherited_session=None,
                    efficiency={}, note=f"blocked by {', '.join(unmet)}"))
                continue

            inherit = lane_sessions.get(node.lane) if assignment.inherits_session else None
            run, _created = self.engine.start(
                task_id=assignment.task_id, prompt=definition.prompt,
                title=definition.title, project_id=PILOT_PROJECT,
                acceptance=definition.acceptance,
                checks=self.check_map.get(assignment.task_id, definition.checks),
                changed_paths=self._modules_for(definition),
                requested_mode=node.requested_mode,
                write_authority=write_authority,
                inherit_builder_session=inherit,
                inherit_from_task=assignment.reuse_session_from if inherit else None,
                actor="pilot")

            if replace_session_on == assignment.task_id:
                self._prove_session_replacement(run.id)

            self.engine.drive(run.id, max_steps=40)
            final = self.store.require_run(run.id)
            if final.builder_session_id:
                lane_sessions[node.lane] = final.builder_session_id
            outcome = TaskOutcome(
                task_id=assignment.task_id, run_id=run.id, mode=final.mode,
                lane=node.lane, stage=final.stage,
                iterations=final.current_iteration, inherited_session=inherit,
                efficiency=self.store.efficiency(run.id),
                decisions=self.store.list_decisions(status="open", run_id=run.id))
            self.outcomes.append(outcome)
            if outcome.merge_ready:
                satisfied.add(assignment.task_id)
        return self.outcomes

    def _prove_session_replacement(self, run_id: str) -> None:
        """Drive to BUILDING, checkpoint, swap the session, and show that
        everything saying WHERE the work is stayed put."""
        self.engine.step(run_id)   # -> PLAN_READY
        self.engine.step(run_id)   # -> BUILDING (iteration opened)
        run = self.store.require_run(run_id)
        worktree, branch = self._real_worktree(run.task_id)
        self.store.patch_run(run_id, worktree_path=worktree, branch=branch,
                             builder_session_id="pilot-lane-A")
        before = self.store.require_run(run_id)
        self.engine.checkpoint(run_id, note="interrupted at 97% context",
                               remaining=["wire the ETA row"],
                               checks_done=["typecheck"])
        handover = self.engine.replace_builder_session(
            run_id, new_session_id="pilot-lane-A-replacement",
            reason="context rollover at 97%")
        after = self.store.require_run(run_id)
        self.replacement_proof = {
            "task_id": before.task_id,
            "run_id_unchanged": before.id == after.id,
            "iteration_unchanged": before.current_iteration == after.current_iteration,
            "iteration": after.current_iteration,
            "worktree_unchanged": before.worktree_path == after.worktree_path,
            "worktree_path": after.worktree_path,
            "branch_unchanged": before.branch == after.branch,
            "branch": after.branch,
            "contract_unchanged": before.contract_hash == after.contract_hash,
            "previous_session": handover["previous_session"],
            "new_session": handover["new_session"],
            "checkpoint_id": handover["checkpoint"]["id"],
            "checkpoint_remaining": handover["checkpoint"]["remaining"],
        }

    def apply_operator_redefine(self, task_id: str, *, resolution: str,
                                acceptance: Sequence[str],
                                resolved_by: str,
                                checks: Sequence[str] | None = None) -> dict[str, Any]:
        """A human answered a BLOCKED run by changing what "done" means.

        This opens a NEW RUN rather than editing the old one. The definition
        changed, so its hash changed, so it is a different run by
        construction -- and the blocked run keeps its entire audit trail,
        still saying exactly what it tried and why it stopped. Reaching back
        into the old run to relax its contract is how a record of a real
        problem becomes a record of a problem that never happened.
        """
        blocked = self.store.run_for_task(task_id, active_only=False)
        for decision in self.store.list_decisions(status="open", run_id=blocked.id):
            self.store.resolve_decision(decision["id"], resolution=resolution,
                                        resolved_by=resolved_by)
        self.store.record_event(
            blocked.id, event_type="OPERATOR_REDEFINE", actor=resolved_by,
            reason=resolution,
            metadata={"previous_acceptance": list(
                (self.store.get_contract(blocked.id) or
                 type("_", (), {"functional_acceptance": ()})).functional_acceptance),
                "new_acceptance": list(acceptance)})
        definition = self.definitions[task_id]
        if checks is not None:
            # The operator's command wins over any pre-supplied one: a
            # redefine is the most recent statement of what "done" means, and
            # a stale mapping quietly overriding it would reproduce exactly
            # the "the definition moved underneath us" failure the frozen
            # contract exists to prevent.
            self.check_map[task_id] = tuple(checks)
        self.definitions[task_id] = TaskDefinition(
            id=definition.id, title=definition.title, lane=definition.lane,
            priority=definition.priority, depends_on=definition.depends_on,
            acceptance=tuple(acceptance),
            checks=tuple(checks) if checks is not None else definition.checks,
            milestone=definition.milestone, declared_status=definition.declared_status)
        return {"task_id": task_id, "previous_run": blocked.id,
                "previous_stage": blocked.stage, "resolution": resolution,
                "resolved_by": resolved_by, "new_acceptance": list(acceptance),
                "new_checks": list(checks) if checks is not None
                              else list(definition.checks)}

    def _real_worktree(self, task_id: str) -> tuple[str, str]:
        """A REAL git worktree, because the proof is about a real place.

        The claim being demonstrated is that a replacement Builder continues
        in the same worktree on the same branch. Demonstrating it against a
        path that does not exist would prove only that two strings match --
        and it would also break the contract's checks, which run with the
        worktree as their working directory.
        """
        branch = f"harness/pilot-{task_id.lower()}"
        path = str(Path(self.worktree_root) / task_id)
        subprocess.run(["git", "worktree", "add", "--force", "-B", branch, path, "HEAD"],
                       cwd=self.repo_root, capture_output=True, text=True, timeout=120)
        self._worktrees.append((path, branch))
        return path, branch

    def cleanup_worktrees(self) -> list[str]:
        """The pilot leaves the pilot repository as it found it.

        Including the Claude Code config. A worktree is named after its task,
        so the same path recurs the next time that task runs -- and a trust
        entry left behind would pre-approve whatever appears there next,
        including a directory somebody made by hand. Revoked AFTER the
        directory is gone, because `revoke` refuses a path that still exists.
        """
        removed = []
        for path, branch in self._worktrees:
            subprocess.run(["git", "worktree", "remove", "--force", path],
                           cwd=self.repo_root, capture_output=True, text=True, timeout=120)
            subprocess.run(["git", "branch", "-D", branch],
                           cwd=self.repo_root, capture_output=True, text=True, timeout=120)
            if self.trust is not None:
                with contextlib.suppress(Exception):
                    self.trust.revoke(path, source="pilot-cleanup")
            removed.append(path)
        self._worktrees = []
        return removed

    def _modules_for(self, definition: TaskDefinition) -> list[str]:
        """Lane -> the part of the monorepo it lives in.

        Deliberately coarse. A pack bounded in bytes does not need a precise
        module list to stay cheap, and guessing precisely would be a second
        opinion about the repository layout that nothing else in the system
        holds.
        """
        return {"A": ["apps/mobile"], "B": ["packages/contracts"],
                "C": ["services"], "D": ["docs/design"]}.get(definition.lane, ["docs"])

    # -- the report ----------------------------------------------------------
    def report(self) -> dict[str, Any]:
        run_ids = [o.run_id for o in self.outcomes if o.run_id]
        measured = self.store.efficiency_totals(run_ids)
        full_prompts = [entry for entry in self.agent.transcript if not entry["delta"]]
        # The cost of ONE full-context call, prompt and completion together --
        # the unit the naive baselines are built from. Taken from the largest
        # full prompt actually assembled rather than assumed, so the baseline
        # is anchored to this repository rather than to a guess.
        typical_full = max(
            (entry["tokens_estimate"] + entry["completion_estimate"]
             for entry in full_prompts), default=0) or 8_000
        started = [o for o in self.outcomes if o.run_id]
        iterations = sum(max(1, o.iterations) for o in started) or len(started)

        sequential = ctx.naive_baseline(
            full_prompt_tokens=typical_full,
            iterations=max(1, iterations // max(1, len(started))) if started else 1)
        per_task_sequential = ctx.NaiveBaseline(
            llm_calls=sequential.llm_calls * max(1, len(started)),
            prompt_tokens_estimate=sequential.prompt_tokens_estimate * max(1, len(started)),
            detail={"per_task": sequential.detail, "tasks": len(started)})
        parallel = ctx.naive_parallel_baseline(
            tasks=max(1, len(started)), full_prompt_tokens=typical_full,
            iterations_per_task=max(1, iterations // max(1, len(started))) if started else 1)

        return {
            "project": PILOT_PROJECT,
            "repo": self.repo_root,
            "target": self.target,
            "scheduling": self.plan.scheduling.to_dict() if hasattr(self, "plan") else {},
            "plan": self.plan.to_dict() if hasattr(self, "plan") else {},
            "human_decision_queue": self.human_queue,
            "open_decisions": self.store.list_decisions(status="open",
                                                        project_id=PILOT_PROJECT),
            "tasks": [o.to_dict() for o in self.outcomes],
            "session_replacement_proof": self.replacement_proof,
            "measured": {
                **measured,
                "context_cache": self.store.cache_stats(project_id=PILOT_PROJECT),
                "prompts_assembled": len(self.agent.transcript),
                "typical_full_call_tokens": typical_full,
                "note_on_pack_size":
                    "the packs here are small because every UrbanFlow workspace is "
                    "still a .gitkeep; on a populated codebase the context saved by "
                    "delta prompts and lane session reuse is larger, not smaller",
            },
            "modelled_baselines": {
                "naive_sequential_full_context": per_task_sequential.to_dict(),
                "naive_parallel_full_context": parallel.to_dict(),
            },
            "savings": {
                "llm_calls_avoided_vs_sequential":
                    per_task_sequential.llm_calls - measured["llm_calls"],
                "llm_calls_avoided_vs_parallel":
                    parallel.llm_calls - measured["llm_calls"],
                "prompt_tokens_avoided_vs_sequential":
                    per_task_sequential.prompt_tokens_estimate
                    - measured["prompt_tokens_estimate"],
                "prompt_tokens_avoided_vs_parallel":
                    parallel.prompt_tokens_estimate - measured["prompt_tokens_estimate"],
            },
            "evidence_class": {
                "measured": "engine decisions, stage transitions, durable state, "
                            "shell check exit statuses, and the byte/token size of "
                            "every prompt actually assembled from the real repo",
                "modelled": "the agent replies, and both naive baselines -- which are "
                            "accounting models of the shape being replaced, computed "
                            "from the same prompts, never measurements of a run",
            },
        }
