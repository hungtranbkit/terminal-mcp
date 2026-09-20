"""The ONE execution state machine for AI work inside Terminal MCP (TMCP-HARNESS-001).

WHY THIS FILE IS THE ONLY PLACE STAGES ARE DEFINED

Before Harness, "what is this task actually doing right now" was answered by
at least five different state machines that did not agree with each other:
the queue's own task status, the Project Agent's execution phase, the PM
scheduler's notion of assignment, the Bug Planner->Executor subsystem's own
pipeline, and the dashboard's rendering of all of them. A task could be
RUNNING in the queue, "phase: implement" on the project, "awaiting executor"
in the bug subsystem and "Blocked" on the board, simultaneously, and there
was no fact of the matter about which was right.

Harness replaces the *decision* layer, not the infrastructure. The durable
queue, the sessions, the leases, the worktree safety, the resource scheduler
and the telemetry all stay exactly as they are and keep being the runtime
source of truth for their own concerns. What converges here is only this:
"AI decides the work progressed from X to Y". That sentence now has exactly
one implementation, and it is `advance()` below.

THE STAGES ARE NOT THE QUEUE'S STATUSES

A HarnessRun stage is a property of the RUN -- an attempt to satisfy one
ExecutionContract -- not of the queue task. The queue task keeps its own
QUEUED/RUNNING/COMPLETED lifecycle unchanged, which is what keeps every
existing caller, every existing test and every legacy API working during the
migration. A run PROJECTS onto the task (see `project_stage`), never the
other way round, and in SHADOW mode it does not even do that.

WHY FAIL IS NOT BLOCKED

The single most expensive defect in the old pipeline: an evaluator saying
"tests failed" put the task in front of a human. Humans were then the
bottleneck on work the machine could have fixed itself, and the Blocked
auto-clear pipeline existed only to undo that mistake in bulk -- a loop whose
entire job was to cancel another loop's decision. Here, EVALUATING+fail goes
to REVISING automatically and deterministically. A human is involved only for
the seven classes in harness_policy.HUMAN_DECISION_REASONS, none of which a
failing test is.
"""
from __future__ import annotations

# -- stages -------------------------------------------------------------------
INIT = "INIT"
PLANNING = "PLANNING"
PLAN_READY = "PLAN_READY"
BUILDING = "BUILDING"
EVALUATING = "EVALUATING"
REVISING = "REVISING"
MERGE_READY = "MERGE_READY"
MERGED = "MERGED"
DONE = "DONE"

# -- special stages -----------------------------------------------------------
BLOCKED = "BLOCKED"
NEEDS_REDEFINE = "NEEDS_REDEFINE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"
CONTEXT_ROLLOVER = "CONTEXT_ROLLOVER"
FAILED_INFRA = "FAILED_INFRA"
CANCELLED = "CANCELLED"

STAGES: tuple[str, ...] = (
    INIT, PLANNING, PLAN_READY, BUILDING, EVALUATING, REVISING, MERGE_READY,
    MERGED, DONE, BLOCKED, NEEDS_REDEFINE, RECOVERY_REQUIRED, CONTEXT_ROLLOVER,
    FAILED_INFRA, CANCELLED,
)

#: Stages from which nothing further happens on its own.
TERMINAL_STAGES: frozenset[str] = frozenset({DONE, CANCELLED})

#: Stages where the run holds live execution and a lease matters.
ACTIVE_STAGES: frozenset[str] = frozenset({
    PLANNING, PLAN_READY, BUILDING, EVALUATING, REVISING,
})

#: Stages a run can be RESUMED from after an infrastructure interruption --
#: resume returns to the stage the run was actually in, never to PLANNING.
#: This is the structural reason "infra retry" can never restart a task from
#: its prompt: there is no edge from any of these back to INIT.
RESUMABLE_STAGES: frozenset[str] = frozenset({
    PLANNING, PLAN_READY, BUILDING, EVALUATING, REVISING, MERGE_READY,
})

#: Stages that mean a human has been asked something (see
#: harness_policy.HUMAN_DECISION_REASONS for the closed list of whys).
HUMAN_STAGES: frozenset[str] = frozenset({BLOCKED})


VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    # LIGHT mode synthesises its contract deterministically from the task
    # definition, so it reaches PLAN_READY without a Planner ever running.
    INIT: frozenset({PLANNING, PLAN_READY, BLOCKED, CANCELLED, FAILED_INFRA}),
    PLANNING: frozenset({PLAN_READY, NEEDS_REDEFINE, BLOCKED, CANCELLED,
                         FAILED_INFRA, RECOVERY_REQUIRED, CONTEXT_ROLLOVER}),
    PLAN_READY: frozenset({BUILDING, NEEDS_REDEFINE, BLOCKED, CANCELLED, FAILED_INFRA}),
    BUILDING: frozenset({EVALUATING, RECOVERY_REQUIRED, CONTEXT_ROLLOVER,
                         FAILED_INFRA, BLOCKED, NEEDS_REDEFINE, CANCELLED}),
    # The pass/fail edge. MERGE_READY on pass; REVISING on fail -- never
    # BLOCKED for a product failure. BLOCKED from here is reachable only
    # through the max-iterations guard, which is an infrastructure fact
    # about the run, not a verdict about the code.
    EVALUATING: frozenset({MERGE_READY, REVISING, NEEDS_REDEFINE, BLOCKED,
                           RECOVERY_REQUIRED, CONTEXT_ROLLOVER, FAILED_INFRA,
                           CANCELLED}),
    REVISING: frozenset({BUILDING, NEEDS_REDEFINE, BLOCKED, CANCELLED, FAILED_INFRA}),
    # The ONLY stage a Merge Executor may be invoked from.
    MERGE_READY: frozenset({MERGED, BLOCKED, CANCELLED, REVISING}),
    MERGED: frozenset({DONE, BLOCKED}),
    DONE: frozenset(),
    # Recovery/rollover return to the stage the run was in. They never
    # re-enter PLANNING (a redefine is a different, explicit decision).
    RECOVERY_REQUIRED: frozenset({PLANNING, PLAN_READY, BUILDING, EVALUATING,
                                  REVISING, BLOCKED, CANCELLED}),
    CONTEXT_ROLLOVER: frozenset({PLANNING, PLAN_READY, BUILDING, EVALUATING,
                                 REVISING, BLOCKED, CANCELLED}),
    FAILED_INFRA: frozenset({PLANNING, PLAN_READY, BUILDING, EVALUATING,
                             REVISING, MERGE_READY, BLOCKED, CANCELLED}),
    # A redefine is the only path that legitimately re-runs the Planner.
    NEEDS_REDEFINE: frozenset({PLANNING, BLOCKED, CANCELLED}),
    BLOCKED: frozenset({PLANNING, PLAN_READY, BUILDING, EVALUATING, REVISING,
                        MERGE_READY, NEEDS_REDEFINE, CANCELLED}),
    CANCELLED: frozenset(),
}


class InvalidStageTransition(ValueError):
    """Refused because the stage machine has no such edge.

    Raised rather than silently corrected: a caller asking for an edge that
    does not exist is a bug in the caller, and quietly snapping it to the
    nearest legal stage is how two state machines start disagreeing again.
    """

    def __init__(self, from_stage: str, to_stage: str) -> None:
        self.from_stage = from_stage
        self.to_stage = to_stage
        super().__init__(f"no harness transition {from_stage} -> {to_stage}")


def is_valid_transition(from_stage: str, to_stage: str) -> bool:
    return to_stage in VALID_TRANSITIONS.get(from_stage, frozenset())


def require_transition(from_stage: str, to_stage: str) -> None:
    if not is_valid_transition(from_stage, to_stage):
        raise InvalidStageTransition(from_stage, to_stage)


# -- verdicts -----------------------------------------------------------------
PASS = "pass"
FAIL = "fail"
BLOCKED_VERDICT = "blocked"
NEEDS_REDEFINE_VERDICT = "needs_redefine"
VERDICTS: tuple[str, ...] = (PASS, FAIL, BLOCKED_VERDICT, NEEDS_REDEFINE_VERDICT)


# -- failure classes ----------------------------------------------------------
#: WHY THIS DISTINCTION IS THE WHOLE POINT OF THE RETRY REWRITE.
#: An infrastructure failure means the WORK is fine and the machinery broke:
#: the correct response is to resume the same run, same iteration, same
#: checkpoint, same worktree. A product failure means the machinery worked
#: and the work is wrong: the correct response is a new iteration against the
#: SAME contract. The old generic retry could not tell these apart, so it did
#: the one thing that is wrong for both -- restart from the original prompt.
INFRA_FAILURE = "infra"
PRODUCT_FAILURE = "product"
CONTRACT_FAILURE = "contract"
FAILURE_CLASSES: tuple[str, ...] = (INFRA_FAILURE, PRODUCT_FAILURE, CONTRACT_FAILURE)


# -- Global Task projection ---------------------------------------------------
#: The Global Task board renders THIS, derived from the run. It owns no
#: lifecycle of its own; every label here is a pure function of the stage, so
#: the board can never show a state the engine does not believe in.
_PROJECTION: dict[str, str] = {
    INIT: "PLANNING",
    PLANNING: "PLANNING",
    PLAN_READY: "PLANNING",
    BUILDING: "BUILDING",
    EVALUATING: "EVALUATING",
    REVISING: "REVISING",
    MERGE_READY: "MERGE_READY",
    MERGED: "MERGE_READY",
    DONE: "DONE",
    BLOCKED: "BLOCKED",
    NEEDS_REDEFINE: "BLOCKED",
    RECOVERY_REQUIRED: "BUILDING",
    CONTEXT_ROLLOVER: "BUILDING",
    FAILED_INFRA: "BUILDING",
    CANCELLED: "DONE",
}

#: The closed set of labels the Global Task UI may render.
PROJECTED_STAGES: tuple[str, ...] = (
    "PLANNING", "BUILDING", "EVALUATING", "REVISING", "MERGE_READY", "BLOCKED", "DONE",
)


def project_stage(stage: str) -> str:
    """HarnessRun.stage -> the label Global Task shows. Pure and total."""
    return _PROJECTION.get(stage, "BLOCKED")


def is_terminal(stage: str) -> bool:
    return stage in TERMINAL_STAGES
