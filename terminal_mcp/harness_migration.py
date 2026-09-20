"""What the Harness replaces, what it does not, and what is not yet decided.

WHY THIS IS A MODULE AND NOT A COMMENT IN A DESIGN DOC

The dangerous moment in a migration like this one is not the new code. It is
the six weeks afterwards, when somebody reads "the Harness is the execution
state machine now", concludes the PM scheduler is dead, and deletes it --
having never checked whether the Harness has actually executed a single piece
of real work yet. At the time of writing it has not: no AgentRunner is wired
on any server, so every run that exists stopped at PLAN_READY.

So the map is written down, in code, next to a test that asserts the one
thing that keeps it honest: NOTHING here has been removed, and the Harness is
not yet authoritative. When that changes, the test changes with it, and
changing it is a deliberate act with a diff.

THE FOUR VERDICTS

KEEP       -- not overlapping. The Harness decides stages; these run things.
              Untouched, and expected to stay that way.
GROUP      -- overlapping in PART. The Harness now owns the DECISION these
              made, but they still own their own infrastructure, and the
              decision layer reads them rather than replacing them.
DEPRECATE  -- overlapping in FULL, superseded, and still running. Left in
              place, still the authoritative path, because parity has not
              been demonstrated on real work. A pointer is added; no
              behaviour changes.
REMOVE     -- demonstrated to be dead. Nothing is in this list yet, and
              nothing may enter it on reasoning alone: the entry criteria
              are at the bottom of this file.

WHAT WAS ACTUALLY CHANGED IN THE OLD PATHS

Two places, both refusals, both narrow, both reversible:

  * queue_store.retry_task refuses a task a LIVE harness run owns, because a
    generic retry there would restart from the prompt and discard a contract,
    a worktree and an iteration history. A task with no run retries exactly
    as before.
  * queue_store.record_deploy refuses DEPLOYED_PROD while a live run has not
    MERGED. Test deploys are untouched.

Nothing else in the old orchestration was modified, disabled or removed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Verdict:
    """One subsystem, and what the Harness does to it."""

    module: str
    verdict: str
    why: str
    #: What must be true before this verdict may become REMOVE. Empty for
    #: KEEP, which is not on a path to removal at all.
    exit_criteria: str = ""


KEEP = "KEEP"
GROUP = "GROUP"
DEPRECATE = "DEPRECATE"
REMOVE = "REMOVE"

#: The one thing this module asserts about itself. Flipping it is a
#: deliberate act, reviewed as a diff, and it is what the test below reads.
HARNESS_IS_AUTHORITATIVE = False

#: Why not. Kept as prose because the answer is the useful part.
NOT_AUTHORITATIVE_BECAUSE = (
    "no AgentRunner is wired on any server, so no harness run has executed "
    "real work: every run reaches PLAN_READY and stops. Parity with the old "
    "pipeline cannot be claimed from runs that never built anything."
)


MAP: tuple[Verdict, ...] = (
    # -- KEEP ----------------------------------------------------------------
    Verdict("queue_store", KEEP,
            "The durable task lifecycle, its transition table and its events "
            "remain the runtime source of truth. The Harness PROJECTS onto "
            "it and is refused by it, never the other way round."),
    Verdict("queue_engine / queue_loop", KEEP,
            "Claiming, dispatch and leases. The Harness decides what a run "
            "should do next; it does not send anything to a session."),
    Verdict("coordinator", KEEP,
            "The pre-dispatch safety gate. Orthogonal: it answers 'is this "
            "safe to start', the Harness answers 'how far has it got'. A "
            "coordinator refusal is explicitly honoured -- the projection "
            "records a skip rather than dragging a BLOCKED task back."),
    Verdict("git_worktree / git_isolation_service", KEEP,
            "Worktree safety and isolation. The Harness records which "
            "worktree a run holds; it does not create or clean them."),
    Verdict("requirement_contract", KEEP,
            "The task's own requirements and the delivery gate. The "
            "ExecutionContract RESTATES these -- harness_service reads them "
            "through RequirementContract.requirements() -- rather than "
            "introducing a second, drifting definition of done."),
    Verdict("scheduler (node placement)", KEEP,
            "Which node runs work. Nothing to do with execution stages."),

    # -- GROUP ---------------------------------------------------------------
    Verdict("task_classifier", GROUP,
            "harness_policy.mode_for delegates the risk judgment here rather "
            "than restating it, so 'what counts as risky' keeps exactly one "
            "definition in this repository."),
    Verdict("pm_service / pm_store", GROUP,
            "Routing decisions and their evidence stay. What moves to the "
            "Harness is the notion of 'assignment' as a STAGE -- a PM "
            "decision is now enrichment on a card beside the run's stage, "
            "not a competing answer to 'what is this task doing'.",
            "a harness run drives a real task end to end and the PM's own "
            "assignment state is shown to add nothing a reader used"),
    Verdict("planner_service / planner_store", GROUP,
            "Task splitting stays; it is a different question from execution "
            "staging. Child progress is still the planner's to report.",
            "contract versioning through NEEDS_REDEFINE is shown to cover "
            "every real split the planner performs today"),

    # -- DEPRECATE -----------------------------------------------------------
    Verdict("generic retry (queue_store.retry_task)", DEPRECATE,
            "Superseded for harnessed tasks and REFUSED for them, because it "
            "cannot tell an infrastructure failure from a product failure "
            "and restarts from the prompt either way. Still the correct and "
            "only path for every task with no run.",
            "every executing task carries a harness run, so the "
            "restart-from-prompt branch is unreachable rather than merely "
            "unused"),
    Verdict("blocked auto-clear pipeline", DEPRECATE,
            "A loop whose entire job was to undo another loop's decision to "
            "put a failing test in front of a human. The Harness removes the "
            "cause: EVALUATING+fail goes to REVISING and never opens a "
            "decision. NOTE: this pipeline is not on this branch -- it lives "
            "on fix/lane-pause-autoclear -- so nothing here disables it.",
            "no harness-driven task has reached BLOCKED for a product "
            "failure over a meaningful window"),
    Verdict("project phase execution engine", DEPRECATE,
            "project_runtime.advance moves a PROJECT through phases, which "
            "is a real and separate concern. What is superseded is its use "
            "as a per-task execution state. Phase advancement is untouched.",
            "project pages read run stages for task-level progress and no "
            "caller reads a phase as a task's execution state"),

    # -- REMOVE --------------------------------------------------------------
    # Deliberately empty. See REMOVAL_REQUIRES below.
)


#: What has to be true before anything moves to REMOVE. All of it, evidenced,
#: not argued -- the cost of deleting a working path too early is an outage
#: in the thing that was quietly still doing the job.
REMOVAL_REQUIRES: tuple[str, ...] = (
    "HARNESS_IS_AUTHORITATIVE is True: an AgentRunner is wired and real work "
    "has run through it",
    "the subsystem's own exit_criteria above are met, with evidence on real "
    "tasks rather than fixtures",
    "a SHADOW comparison over a real workload shows the Harness and the old "
    "path agreeing, including on the cases the old path got wrong",
    "no caller outside tests reaches the path, demonstrated by a search "
    "rather than assumed from the call graph",
)


def by_verdict(verdict: str) -> tuple[Verdict, ...]:
    return tuple(entry for entry in MAP if entry.verdict == verdict)


def summary() -> dict[str, int]:
    return {name: len(by_verdict(name)) for name in (KEEP, GROUP, DEPRECATE, REMOVE)}
