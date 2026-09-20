"""How much process a task gets, who may write, and when a human is asked.

DIFFICULTY TRIAGE IS A FUNCTION, NOT A SERVICE

The standalone Difficulty Triage service had its own store, its own loop, its
own UI and its own opinion, and the only consumer of that opinion was "how
many review steps should this get". That is one line of policy wearing three
layers of infrastructure. Here it is `mode_for()`: a pure, deterministic
function from a task definition to one of three modes. No state, no loop, no
second source of truth -- call it twice with the same task and it answers the
same thing, which is precisely what a service could never promise.

It delegates the risk judgment to task_classifier.EXCLUSIONS rather than
restating it, so "what counts as risky" has exactly one definition in this
repository. A second copy of that regex list would drift, and the direction
it would drift is towards under-escalating.

WRITE AUTHORITY IS A SEPARATE AXIS FROM MODE

Mode says how much process. Authority says whether the engine's conclusions
are allowed to touch anything outside the harness tables. They are
independent on purpose: a CRITICAL run in SHADOW does the full
Planner/Builder/Evaluator sequence and writes a complete audit trail while
changing no task status and merging nothing. That is what makes it possible
to compare Harness against the old pipeline on real work before handing it
the keys.

THE HUMAN DECISION LIST IS CLOSED

Seven reasons, and a failing test is not one of them. Anything not on this
list is the machine's problem to solve, which is what makes REVISING the
default response to a FAIL instead of a queue of humans reading test output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import task_classifier

# -- modes --------------------------------------------------------------------
LIGHT = "light"
STANDARD = "standard"
CRITICAL = "critical"
MODES: tuple[str, ...] = (LIGHT, STANDARD, CRITICAL)

#: Hard iteration caps per mode. Reaching one is an infrastructure fact about
#: the run (the loop is not converging), which is why it is the single edge
#: from EVALUATING to BLOCKED -- see harness_state.
MAX_ITERATIONS: dict[str, int] = {LIGHT: 1, STANDARD: 3, CRITICAL: 6}

#: Whether the mode runs a Planner at all. LIGHT synthesises its contract
#: deterministically from the task's own declared acceptance and checks: if a
#: task's definition is too thin for that, LIGHT is the wrong mode and
#: `mode_for` will not choose it.
USES_PLANNER: dict[str, bool] = {LIGHT: False, STANDARD: True, CRITICAL: True}

#: Whether the Evaluator must run in a NEW session with a fresh context that
#: never saw the Builder's reasoning. Only CRITICAL demands it; STANDARD lets
#: the Builder run its own declared checks, which is honest self-testing
#: rather than self-approval because the checks are named in the contract
#: before the Builder starts.
INDEPENDENT_EVALUATOR: dict[str, bool] = {LIGHT: False, STANDARD: False, CRITICAL: True}


# -- write authority ----------------------------------------------------------
SHADOW = "shadow"
SUPERVISED = "supervised"
AUTONOMOUS = "autonomous"
AUTHORITIES: tuple[str, ...] = (SHADOW, SUPERVISED, AUTONOMOUS)

#: SHADOW: harness tables only. No queue status write, no merge, no deploy.
#: SUPERVISED: harness may drive the run and write queue status, but MERGED
#:   requires an explicit human/PM approval recorded on the run.
#: AUTONOMOUS: harness may also take the merge decision itself, still only
#:   from MERGE_READY and still only through the existing Merge Executor.
WRITES_QUEUE_STATUS: dict[str, bool] = {SHADOW: False, SUPERVISED: True, AUTONOMOUS: True}
MAY_SELF_APPROVE_MERGE: dict[str, bool] = {SHADOW: False, SUPERVISED: False, AUTONOMOUS: True}


# -- human decision queue -----------------------------------------------------
MISSING_CREDENTIAL = "missing_credential"
PERMISSION_REQUIRED = "permission_required"
DESTRUCTIVE_ACTION = "destructive_action"
LEGAL_OR_DATA = "legal_license_or_data"
AMBIGUOUS_PRODUCT = "ambiguous_product_decision"
MAX_ITERATIONS_REACHED = "max_iterations_reached"
REPEATED_INFRA_FAILURE = "repeated_infra_failure"
CONTRADICTORY_ARCHITECTURE = "contradictory_architecture"

HUMAN_DECISION_REASONS: tuple[str, ...] = (
    MISSING_CREDENTIAL, PERMISSION_REQUIRED, DESTRUCTIVE_ACTION, LEGAL_OR_DATA,
    AMBIGUOUS_PRODUCT, MAX_ITERATIONS_REACHED, REPEATED_INFRA_FAILURE,
    CONTRADICTORY_ARCHITECTURE,
)

#: How many consecutive infrastructure failures on one run stop being an
#: infrastructure problem and start being a human's problem. Below this the
#: correct action is always RESUME, never escalate.
INFRA_FAILURE_ESCALATION_THRESHOLD = 3


class NotAHumanDecision(ValueError):
    """A caller tried to put something in front of a human that the machine
    is supposed to handle. Raised rather than accepted, because the old
    pipeline's defect was exactly this call succeeding for "tests failed"."""


def require_human_reason(reason: str) -> str:
    if reason not in HUMAN_DECISION_REASONS:
        raise NotAHumanDecision(
            f"{reason!r} is not a human decision; the closed list is "
            f"{', '.join(HUMAN_DECISION_REASONS)}")
    return reason


# -- context / resource thresholds -------------------------------------------
#: Reused verbatim from the existing resource-health policy so there is one
#: ladder, not two. The percentages are context-window occupancy for the
#: session currently holding the run.
CONTINUE = "continue"
WATCH = "watch"
CHECKPOINT_AFTER_SLICE = "checkpoint_after_slice"
ROLLOVER = "rollover"
REPLACE_SESSION = "replace_session"

CONTEXT_LADDER: tuple[tuple[float, str], ...] = (
    (70.0, CONTINUE),
    (85.0, WATCH),
    (92.0, CHECKPOINT_AFTER_SLICE),
    (97.0, ROLLOVER),
)


def context_action(percent: float | None) -> str:
    """Context occupancy -> what the engine does about it.

    `None` (the window is genuinely unknown -- see ai_context_window.py on why
    that is a real answer and not a failure) is CONTINUE: acting on a number
    nobody measured is worse than acting on none.
    """
    if percent is None:
        return CONTINUE
    for ceiling, action in CONTEXT_LADDER:
        if percent < ceiling:
            return action
    return REPLACE_SESSION


#: Actions that require the engine to stop handing the session new work.
CONTEXT_STOPS_NEW_WORK: frozenset[str] = frozenset({ROLLOVER, REPLACE_SESSION})
#: Actions that require a durable checkpoint before anything else happens.
CONTEXT_FORCES_CHECKPOINT: frozenset[str] = frozenset(
    {CHECKPOINT_AFTER_SLICE, ROLLOVER, REPLACE_SESSION})


# -- mode triage --------------------------------------------------------------
#: Task kinds that are CRITICAL regardless of how small the diff looks. Same
#: posture as task_classifier's exclusions: "small" and "low risk" are
#: different properties, and the expensive mistakes live where they disagree.
CRITICAL_AREAS: tuple[str, ...] = (
    "auth", "routing", "risk", "database", "migration", "security",
    "deploy", "release", "ui_milestone",
)

_AREA_FOR_EXCLUSION: dict[str, str] = {
    "auth/authorization": "auth",
    "payment": "risk",
    "database/schema/migration": "migration",
    "destructive data": "risk",
    "credentials/secrets": "security",
    "session consistency/concurrency": "risk",
    "security-sensitive": "security",
    "broad infrastructure": "deploy",
}


@dataclass(frozen=True)
class HarnessPolicy:
    """The complete, durable answer to "how is this run allowed to behave"."""

    mode: str = STANDARD
    write_authority: str = SHADOW
    max_iterations: int = 3
    independent_evaluator: bool = False
    uses_planner: bool = True
    critical_areas: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "write_authority": self.write_authority,
            "max_iterations": self.max_iterations,
            "independent_evaluator": self.independent_evaluator,
            "uses_planner": self.uses_planner,
            "critical_areas": list(self.critical_areas),
            "reasons": list(self.reasons),
        }

    @property
    def writes_queue_status(self) -> bool:
        return WRITES_QUEUE_STATUS.get(self.write_authority, False)

    @property
    def may_self_approve_merge(self) -> bool:
        return MAY_SELF_APPROVE_MERGE.get(self.write_authority, False)


def mode_for(description: str, *, changed_paths: Sequence[str] = (),
             requested_mode: str | None = None,
             acceptance: Sequence[Any] = (),
             checks: Sequence[Any] = ()) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """(mode, critical_areas, reasons). Pure. This IS the difficulty triage.

    An explicitly requested mode is honoured in the ESCALATING direction only.
    A caller may ask for more process than the triage would choose; asking for
    less on a task that touches auth or a migration is refused silently by
    keeping the higher mode, with the refusal recorded in `reasons` so the
    dashboard can show why the request did not take effect.
    """
    classification = task_classifier.classify(description, changed_paths=changed_paths)
    reasons: list[str] = []
    areas: list[str] = []
    for hit in classification.exclusions_hit:
        area = _AREA_FOR_EXCLUSION.get(hit)
        if area and area not in areas:
            areas.append(area)
        reasons.append(f"risk area: {hit}")

    if areas:
        chosen = CRITICAL
    elif classification.mode == task_classifier.FAST_FIX and (acceptance or checks):
        # A fast fix WITH a declared acceptance criterion and a declared check
        # is exactly the case LIGHT exists for: there is something concrete to
        # verify and no risk area to protect.
        chosen = LIGHT
        reasons.append("fast fix with declared acceptance and checks")
    elif classification.mode == task_classifier.SAFE:
        chosen = CRITICAL
        reasons.append("classifier escalated to SAFE")
    else:
        chosen = STANDARD
        reasons.append(f"classifier mode {classification.mode}")

    if requested_mode:
        requested = str(requested_mode).lower()
        if requested not in MODES:
            reasons.append(f"ignored unknown requested mode {requested_mode!r}")
        elif MODES.index(requested) > MODES.index(chosen):
            reasons.append(f"escalated by request from {chosen} to {requested}")
            chosen = requested
        elif MODES.index(requested) < MODES.index(chosen):
            reasons.append(
                f"refused de-escalation to {requested}: triage requires {chosen}")
    return chosen, tuple(areas), tuple(reasons)


def policy_for(description: str, *, changed_paths: Sequence[str] = (),
               requested_mode: str | None = None,
               write_authority: str = SHADOW,
               acceptance: Sequence[Any] = (),
               checks: Sequence[Any] = ()) -> HarnessPolicy:
    """The durable policy row for a run. One call, fully determined."""
    mode, areas, reasons = mode_for(description, changed_paths=changed_paths,
                                    requested_mode=requested_mode,
                                    acceptance=acceptance, checks=checks)
    authority = str(write_authority or SHADOW).lower()
    if authority not in AUTHORITIES:
        reasons = reasons + (f"unknown write_authority {write_authority!r}; using shadow",)
        authority = SHADOW
    return HarnessPolicy(
        mode=mode,
        write_authority=authority,
        max_iterations=MAX_ITERATIONS[mode],
        independent_evaluator=INDEPENDENT_EVALUATOR[mode],
        uses_planner=USES_PLANNER[mode],
        critical_areas=areas,
        reasons=reasons,
    )


# =============================================================================
# TOKEN / COST MINIMISATION
# =============================================================================
"""WHY COST IS A POLICY FIELD AND NOT A LATER OPTIMISATION

The expensive parts of an autonomous run are not the reasoning. They are the
bookkeeping: a PM model woken every few seconds to ask whether anything
changed, a full project context resent on every revision, a Planner invoked
for a task whose acceptance was already written down, a fresh Evaluator
session spun up for a one-line CSS change, a client polling `status` in a
loop and paying for a model call each time.

Every one of those is a decision that deterministic code can make correctly
and for free. So the rule this section encodes is: an LLM is invoked to
reason, to write code, or to judge evidence -- and for nothing else. Polling,
readiness, dependencies, leases, routing, thresholds and status are all
deterministic, and the savings from that are structural rather than
incremental.

The budget is SOFT on purpose. A hard cap stops a run that has already spent
its money and leaves nothing to show for it; a soft budget checkpoints,
narrows the next prompt to the unresolved criteria, and downgrades reasoning
it can do without. Stopping is never the cheap option once work is underway.
"""

# -- soft token budgets per run, by mode -------------------------------------
#: Deliberately generous relative to a single call and tight relative to a
#: runaway loop: these exist to catch a run that is not converging, not to
#: ration a run that is working.
SOFT_TOKEN_BUDGET: dict[str, int] = {
    LIGHT: 60_000,
    STANDARD: 250_000,
    CRITICAL: 700_000,
}

#: What the engine does as a run approaches its soft budget. Fractions of the
#: budget, checked after each stage rather than mid-call.
BUDGET_OK = "ok"
BUDGET_NARROW = "narrow_context"
BUDGET_CHECKPOINT = "checkpoint_and_summarise"
BUDGET_ESCALATE = "escalate_needs_redefine"

BUDGET_LADDER: tuple[tuple[float, str], ...] = (
    (0.75, BUDGET_OK),
    (0.90, BUDGET_NARROW),
    (1.00, BUDGET_CHECKPOINT),
)


def budget_action(spent: int, budget: int | None) -> str:
    """Tokens spent so far -> what the next stage does differently.

    Never returns "stop". A run over budget checkpoints, summarises what is
    still unresolved and escalates the DEFINITION as the suspect -- because a
    run that has burned its whole budget without converging is far more often
    a specification problem than an effort problem.
    """
    if not budget or budget <= 0:
        return BUDGET_OK
    ratio = float(spent) / float(budget)
    for ceiling, action in BUDGET_LADDER:
        if ratio < ceiling:
            return action
    return BUDGET_ESCALATE


# -- session reuse ------------------------------------------------------------
#: Below this context occupancy a Builder session is REUSED across steps.
#: Spawning a session per small step throws away the one thing that makes the
#: second step cheap -- the model already having the file open -- and pays a
#: fresh context load for it. Above it, continuity is worth less than the
#: room, and CONTEXT_LADDER takes over.
SESSION_REUSE_CONTEXT_CEILING = 85.0


def may_reuse_builder(context_percent: float | None) -> bool:
    if context_percent is None:
        return True
    return context_percent < SESSION_REUSE_CONTEXT_CEILING


# -- skill injection bounds ---------------------------------------------------
MAX_INJECTED_SKILLS = 4
MAX_SKILL_BYTES = 24_000


# -- model / runtime selection ------------------------------------------------
#: Roles, as the scheduler understands them. Not a vendor list: the values are
#: capability TIERS, and the existing runtime registry maps a tier to whatever
#: runtimes this deployment actually has. Hardcoding a vendor here would make
#: the cheapest available option unreachable on any fleet that has a different
#: one.
TIER_ECONOMY = "economy"
TIER_BALANCED = "balanced"
TIER_FRONTIER = "frontier"
TIERS: tuple[str, ...] = (TIER_ECONOMY, TIER_BALANCED, TIER_FRONTIER)

PLANNER = "planner"
BUILDER = "builder"
EVALUATOR = "evaluator"
ROLES: tuple[str, ...] = (PLANNER, BUILDER, EVALUATOR)

#: (mode, role) -> capability tier. The strongest tier is reserved for the two
#: situations that actually need it: ambiguous architecture (a CRITICAL
#: Planner) and work that has already failed more than once.
_TIER_MATRIX: dict[tuple[str, str], str] = {
    (LIGHT, PLANNER): TIER_ECONOMY,
    (LIGHT, BUILDER): TIER_BALANCED,
    (LIGHT, EVALUATOR): TIER_ECONOMY,
    (STANDARD, PLANNER): TIER_BALANCED,
    (STANDARD, BUILDER): TIER_BALANCED,
    (STANDARD, EVALUATOR): TIER_ECONOMY,
    (CRITICAL, PLANNER): TIER_FRONTIER,
    (CRITICAL, BUILDER): TIER_BALANCED,
    (CRITICAL, EVALUATOR): TIER_BALANCED,
}

#: Consecutive failed iterations after which every role on the run is promoted
#: to the strongest tier. Repeated failure is the evidence that the cheap
#: option is not working, and continuing to pay for it is the expensive
#: mistake.
ESCALATE_TIER_AFTER_FAILURES = 2


def tier_for(mode: str, role: str, *, failed_iterations: int = 0,
             ambiguous: bool = False) -> str:
    """Capability tier for one role on one run. Deterministic and cheap."""
    if ambiguous or failed_iterations >= ESCALATE_TIER_AFTER_FAILURES:
        return TIER_FRONTIER
    return _TIER_MATRIX.get((mode, role), TIER_BALANCED)


# -- what a run is allowed to skip -------------------------------------------
def planner_required(mode: str, *, acceptance: Sequence[Any] = (),
                     checks: Sequence[Any] = (), scope: str = "") -> tuple[bool, str]:
    """(required, why). The Planner is the most expensive stage in the system
    and the easiest to skip correctly.

    LIGHT never plans. STANDARD plans only when the task definition cannot
    support a deterministic contract template -- when the acceptance, the
    checks or the scope are missing, which is exactly the case a template
    cannot invent. CRITICAL always plans, because the whole reason a task is
    CRITICAL is that its blast radius deserves a second opinion on what
    "done" means before anyone writes code.
    """
    if mode == LIGHT:
        return False, "LIGHT mode builds against the task's own declared acceptance"
    if mode == CRITICAL:
        return True, "CRITICAL mode always plans before building"
    missing = []
    if not acceptance:
        missing.append("acceptance")
    if not checks:
        missing.append("checks")
    if not str(scope or "").strip():
        missing.append("scope")
    if missing:
        return True, "task definition lacks " + ", ".join(missing)
    return False, "deterministic contract template covers this task definition"


def evaluator_required(mode: str) -> tuple[bool, str]:
    """(required, why) for an INDEPENDENT evaluator session.

    LIGHT and STANDARD run their contract's declared checks in the Builder's
    own session. That is not self-approval: the checks were named in the
    contract before the Builder started and their exit status is not a matter
    of opinion. A separate session would re-load the whole context to run the
    same command and report the same number.
    """
    if INDEPENDENT_EVALUATOR.get(mode, False):
        return True, "CRITICAL mode requires an independent evaluator context"
    return False, f"{mode} mode verifies through declared checks in the builder session"


@dataclass(frozen=True)
class CostPolicy:
    """The durable cost half of a run's policy."""

    soft_token_budget: int = SOFT_TOKEN_BUDGET[STANDARD]
    max_injected_skills: int = MAX_INJECTED_SKILLS
    max_skill_bytes: int = MAX_SKILL_BYTES
    session_reuse_ceiling: float = SESSION_REUSE_CONTEXT_CEILING
    delta_prompts: bool = True
    context_cache: bool = True
    planner_tier: str = TIER_BALANCED
    builder_tier: str = TIER_BALANCED
    evaluator_tier: str = TIER_ECONOMY
    #: No parallel planner pool, no multi-evaluator consensus. Both are off by
    #: default because both multiply cost by a constant to buy variance
    #: reduction nobody measured. Concurrency belongs BETWEEN independent
    #: tasks, where the dependency graph proves it is safe, not within one.
    parallel_planners: int = 1
    evaluator_consensus: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "soft_token_budget": self.soft_token_budget,
            "max_injected_skills": self.max_injected_skills,
            "max_skill_bytes": self.max_skill_bytes,
            "session_reuse_ceiling": self.session_reuse_ceiling,
            "delta_prompts": self.delta_prompts,
            "context_cache": self.context_cache,
            "planner_tier": self.planner_tier,
            "builder_tier": self.builder_tier,
            "evaluator_tier": self.evaluator_tier,
            "parallel_planners": self.parallel_planners,
            "evaluator_consensus": self.evaluator_consensus,
        }


def cost_policy_for(mode: str, *, failed_iterations: int = 0,
                    ambiguous: bool = False,
                    soft_token_budget: int | None = None) -> CostPolicy:
    return CostPolicy(
        soft_token_budget=soft_token_budget or SOFT_TOKEN_BUDGET.get(mode, SOFT_TOKEN_BUDGET[STANDARD]),
        planner_tier=tier_for(mode, PLANNER, failed_iterations=failed_iterations, ambiguous=ambiguous),
        builder_tier=tier_for(mode, BUILDER, failed_iterations=failed_iterations, ambiguous=ambiguous),
        evaluator_tier=tier_for(mode, EVALUATOR, failed_iterations=failed_iterations, ambiguous=ambiguous),
    )
