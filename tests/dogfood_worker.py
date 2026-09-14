"""The dogfood's worker: a real run, routed through the shipped contract.

WHAT THIS IS, AND WHAT IT IS NOT

`bug_spec.gate` and `bug_spec.budget_check` are already covered by unit
tests, which prove the functions compute the right verdict from the right
numbers. That is not the same claim as "the budget constrains a run". A
function that returns NEEDS_REDEFINE constrains nothing until something is
actually stopped by it.

So this is a worker session: it opens real files in this repository, runs
real `git grep`, and asks the SHIPPED functions for permission before each
one. Nothing here re-implements the budget or the gate -- every verdict comes
from `bug_spec`, and every count goes to a real `work_telemetry` row.

WHAT IT HONESTLY PROVES

That a worker which routes its reads and searches through the contract is
stopped by it -- on this repository, with real I/O, at the exact limits the
contract declares. It does NOT prove that an unmediated agent obeys a budget
it merely read in a prompt; nothing in a test can prove that, and claiming it
would be the kind of invented certainty the rest of this system refuses.

PERMISSION IS ASKED BEFORE THE WORK, NOT AFTER

`budget_check` is asked about the count this operation WOULD reach, so the
sixth file of a five-file budget is never opened. Checking afterwards would
report the overrun accurately and permit it anyway, which is how a budget
becomes a statistic.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from terminal_mcp import bug_spec
from terminal_mcp.bug_spec import BugSpec, BugSpecStore
from terminal_mcp.work_telemetry import TaskTelemetry


class SpecNotExecutable(RuntimeError):
    """Raised when a worker tries to investigate an incomplete spec.

    The refusal carries the reply the worker owes its planner, so handing the
    spec back costs nothing further -- which is the point: earning the missing
    detail by reading the repository IS the re-analysis the spec exists to
    avoid.
    """

    def __init__(self, reply: dict[str, Any]) -> None:
        super().__init__(f"{reply.get('STATUS')}: {', '.join(reply.get('MISSING') or [])}")
        self.reply = reply


class BudgetRefused(RuntimeError):
    """Raised when the next read or search would exceed the spec's budget."""

    def __init__(self, check: dict[str, Any]) -> None:
        super().__init__("; ".join(check.get("exceeded") or ["over budget"]))
        self.check = check


@dataclass
class DogfoodWorker:
    """One worker session over one spec, against a real repository."""

    spec: BugSpec
    repo_root: Path
    telemetry: TaskTelemetry
    spec_store: BugSpecStore | None = None

    # What actually happened, for the dogfood to assert against. These are
    # records of real operations -- a path in `opened` was really read.
    opened: list[str] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    escalation: dict[str, Any] | None = None
    blocked_by: dict[str, Any] | None = None

    # -- the gate, before anything is opened ---------------------------------

    def start(self) -> dict[str, Any]:
        """Run the pre-flight gate. An incomplete spec blocks the session.

        Blocking is the whole behaviour under test. A worker that reads "the
        spec is thin" and starts looking around has turned a 30-second
        hand-back into an open-ended investigation, and the planner never
        finds out its spec was unusable.
        """
        report = bug_spec.gate(self.spec)
        if not report["ready"]:
            self.blocked_by = report["reply_to_planner"]
            self.telemetry.record_redefine()
        return report

    # -- permission ----------------------------------------------------------

    def _permit(self, *, files: int = 0, searches: int = 0) -> dict[str, Any]:
        if self.blocked_by is not None:
            raise SpecNotExecutable(self.blocked_by)
        check = bug_spec.budget_check(
            self.spec,
            files_read=self.telemetry.files_read + files,
            search_rounds=self.telemetry.search_rounds + searches)
        if not check["within_budget"] and self.escalation is None:
            self.refusals.append(check)
            raise BudgetRefused(check)
        return check

    # -- real work -----------------------------------------------------------

    def read(self, relative_path: str) -> str:
        """Really open a file in the repository, if the budget allows it."""
        self._permit(files=1)
        target = (self.repo_root / relative_path).resolve()
        # Containment, for the same reason repo_read enforces it: a spec is
        # written by another agent, and a path that escapes the repository is
        # a bug wherever it came from.
        if not target.is_relative_to(self.repo_root.resolve()):
            raise ValueError(f"{relative_path} escapes the repository")
        text = target.read_text(encoding="utf-8")
        self.telemetry.record_files()
        self.opened.append(relative_path)
        return text

    def search(self, term: str) -> list[str]:
        """Really run one `git grep` round, if the budget allows it."""
        self._permit(searches=1)
        done = subprocess.run(["git", "grep", "-l", "--", term],
                              cwd=str(self.repo_root), capture_output=True,
                              text=True, timeout=60, check=False)
        self.telemetry.record_search()
        self.searched.append(term)
        # git grep exits 1 on "no match", which is an answer, not a failure.
        return [line for line in done.stdout.splitlines() if line.strip()]

    # -- the two ways a session may legitimately continue or stop -------------

    def escalate(self, *, why: str, known: str, missing: str,
                 redefine_request: str) -> dict[str, Any]:
        """Record an explicit escalation, and only then continue past budget.

        The report is `bug_spec.escalation`'s, unchanged: four short fields and
        TASK_CONTINUES, so the planner adds detail to the SAME task rather than
        a new one being opened and the work so far thrown away.
        """
        report = bug_spec.escalation(self.spec, why=why, known=known,
                                     missing=missing, redefine_request=redefine_request)
        self.escalation = report
        self.telemetry.record_budget_escalation(why)
        return report

    def verify_plan(self, status: str, *, note: str = "",
                    adjusted_files: tuple[str, ...] = ()) -> dict[str, Any]:
        """Answer the plan-verification handshake, on the row AND on the spec.

        Both, deliberately. The spec carries the verdict to the next planner
        for this module; the telemetry row carries it to whoever asks how
        often this fleet's hypotheses turn out to be wrong.
        """
        self.telemetry.record_plan_outcome(status, note=note)
        if self.spec_store is not None:
            self.spec_store.record_plan_outcome(self.spec.bug_id, status=status,
                                                note=note, adjusted_files=adjusted_files)
        return {"plan_status": status, "note": note}
