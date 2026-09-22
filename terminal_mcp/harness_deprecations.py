"""What Harness supersedes, named in one place, and still running.

WHY NOTHING HERE IS DELETED

Harness is SHADOW/SUPERVISED only. The old paths remain authoritative until
the pilot passes, which means every one of them must keep working exactly as
it does today. A feature that quietly disables its predecessor before proving
itself has no way back, and the way back is the whole reason for running in
shadow in the first place.

So this module deletes nothing, disables nothing and imports nothing at
module scope. It is an INVENTORY: for each path Harness replaces, what the
replacement is, what state the old path is in, and the specific gate that has
to be met before it can go. Written down because the alternative -- a comment
on each of six functions in six modules -- is six things to find, and the
question people actually ask is "what does Harness make redundant", which no
number of scattered comments answers.

`verify()` imports each named symbol and reports the ones that no longer
exist. An inventory that nobody checks becomes fiction within two refactors,
and a test runs it.

THE THREE STATUSES

* `parallel`  -- still fully live and authoritative. Harness does the same
                 job beside it and writes only to its own tables.
* `read_only` -- safe to stop WRITING through, because Harness now owns that
                 decision, but still read by a dashboard or a report.
* `frozen`    -- nothing should call it; kept so an in-flight task started
                 under the old shape can still finish.

Nothing is `frozen` yet, and nothing becomes `read_only` on this branch. That
is deliberate: the statuses exist so the transition is a reviewable one-line
change per path rather than a rewrite.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

PARALLEL = "parallel"
READ_ONLY = "read_only"
FROZEN = "frozen"
STATUSES: tuple[str, ...] = (PARALLEL, READ_ONLY, FROZEN)


@dataclass(frozen=True)
class SupersededPath:
    """One decision Harness now also makes, and where the old one lives."""

    module: str
    symbol: str
    #: What the old path decides today.
    decides: str
    #: What in Harness decides the same thing, and how it differs.
    superseded_by: str
    #: What has to be true before this can stop being called.
    retire_gate: str
    status: str = PARALLEL

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.symbol}"

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.dotted, "decides": self.decides,
                "superseded_by": self.superseded_by, "status": self.status,
                "retire_gate": self.retire_gate}


SUPERSEDED: tuple[SupersededPath, ...] = (
    SupersededPath(
        module="terminal_mcp.retry_recovery",
        symbol="plan_retry",
        decides="whether a failed attempt is retried, and with what text",
        superseded_by=(
            "harness_engine.record_infra_failure + resume(). The old plan has one "
            "notion of 'retry' covering both a crashed agent and wrong code; the "
            "engine splits them, because one resumes the same iteration in the same "
            "worktree and the other opens a new iteration against the same contract. "
            "There is no edge from a resumable stage back to INIT, so a resume "
            "cannot degrade into replaying the original prompt."),
        retire_gate="the pilot shows no run needing a generic retry decision"),
    SupersededPath(
        module="terminal_mcp.queue_store",
        symbol="QueueStore.reevaluate_ai_owned_blocked",
        decides="which BLOCKED tasks are cleared back to the queue in bulk",
        superseded_by=(
            "harness_state's transition table. This sweep exists only to undo a "
            "decision the old pipeline should not have taken -- putting a failing "
            "test in front of a human. EVALUATING+fail goes to REVISING, so there "
            "is nothing for it to clear."),
        retire_gate="no harness run reaches BLOCKED for a reason outside "
                    "harness_policy.HUMAN_DECISION_REASONS"),
    SupersededPath(
        module="terminal_mcp.work_inbox",
        symbol="rough_difficulty",
        decides="how hard a piece of work is, as a stored label",
        superseded_by=(
            "harness_policy.mode_for -- a pure function rather than a stored "
            "judgement, so two calls with the same task cannot disagree, and it "
            "delegates risk to task_classifier.EXCLUSIONS rather than keeping a "
            "second copy of that list."),
        retire_gate="every consumer reads mode_for, and the stored difficulty "
                    "column is no longer written"),
    SupersededPath(
        module="terminal_mcp.dor_gate",
        symbol="check_definition_of_ready",
        decides="whether a task is specified well enough to start",
        superseded_by=(
            "ExecutionContract.build, which raises InsufficientSpecification at "
            "PLANNING. Same question, asked where the answer is still cheap: the "
            "gate rejects at submission, the contract rejects before a Builder "
            "spends an hour on work whose completion is not decidable."),
        retire_gate="the harness path covers non-harness submissions too"),
    SupersededPath(
        module="terminal_mcp.project_runtime",
        symbol="ProjectRuntimeService.advance",
        decides="which project phase is active and which agents it needs",
        superseded_by=(
            "harness_scheduler.plan, which orders by dependency and critical path "
            "rather than by phase. A phase is a coarse grouping; the dependency "
            "graph is what actually says whether a task can start, and it is the "
            "thing the definition file already carries."),
        retire_gate="the project board renders harness run projections"),
    SupersededPath(
        module="terminal_mcp.queue_store",
        symbol="QueueStore.retry_task",
        decides="an operator's explicit retry of one task",
        superseded_by=(
            "harness_engine.resume for infrastructure and a new iteration for "
            "product failures. Kept live: an operator retry of a NON-harness task "
            "is still the only way to restart one, and most tasks are not harness "
            "tasks yet."),
        retire_gate="all queue tasks are driven by harness runs"),
)


def verify() -> dict[str, Any]:
    """Does every path named here still exist?

    An inventory nobody checks is fiction within two refactors. A symbol that
    has vanished is reported rather than raised: it usually means somebody
    already removed the old path, which is good news the report should carry
    rather than a crash.
    """
    present: list[str] = []
    missing: list[str] = []
    for entry in SUPERSEDED:
        try:
            module = importlib.import_module(entry.module)
        except ImportError:
            missing.append(entry.dotted)
            continue
        target: Any = module
        found = True
        for part in entry.symbol.split("."):
            target = getattr(target, part, None)
            if target is None:
                found = False
                break
        (present if found else missing).append(entry.dotted)
    return {"present": present, "missing": missing,
            "checked": len(SUPERSEDED), "ok": not missing}


def report() -> dict[str, Any]:
    """The answer to "what does Harness make redundant, and can it go yet"."""
    verified = verify()
    return {
        "paths": [entry.to_dict() for entry in SUPERSEDED],
        "by_status": {status: [e.dotted for e in SUPERSEDED if e.status == status]
                      for status in STATUSES},
        "verified": verified,
        "note": ("Nothing here is disabled. Harness runs in SHADOW or SUPERVISED "
                 "and the old paths remain authoritative until the pilot passes; "
                 "a feature that disables its predecessor before proving itself "
                 "has no way back."),
    }
