"""Work Efficiency Telemetry -- the read/normalize layer over
work_telemetry_store.py (TASK A).

The store records raw, deduplicated measurements. This module answers
the questions the experiment was set up to ask, and it has exactly one
editorial rule, applied everywhere:

    A NUMBER IS NEVER REPORTED AS ZERO BECAUSE IT WAS NEVER MEASURED.

Every total below is None when nothing was measured, and every aggregate
carries its own coverage and confidence alongside it so a reader can
tell "cheap" from "unmeasured". `tokens_per_completed_task` in
particular divides ONLY by the completed tasks that actually have token
telemetry -- dividing the measured total by ALL completed tasks would
make the headline number fall every time telemetry coverage dropped,
which is the exact failure mode that would make this metric lie in the
direction its authors were hoping for.

This module has NO user interface, registers no MCP tool, touches no
dashboard, and never writes to the queue. It is a library other lanes
call. (TASK A scope: telemetry storage/service/schema only.)
"""
from __future__ import annotations

from typing import Any

from .work_telemetry_store import (
    CONFIDENCE_UNKNOWN, FIRST_PASS_FALSE, FIRST_PASS_TRUE, FIRST_PASS_UNKNOWN,
    PHASE_ANALYSIS, PHASE_CONTRACT, PHASE_UNKNOWN, STATUS_COMPLETED, WORKER_PHASES,
    WorkTelemetryStore, _weakest_confidence,
)

_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _add(left: int | None, right: int | None) -> int | None:
    """None + None is None (still unmeasured); None + 5 is 5. Addition
    that treats an absent operand as absent rather than as zero -- the
    same semantics SQL's own SUM() has, kept consistent in Python so a
    total assembled here cannot disagree with one assembled by the
    database."""
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _sum_phase_tokens(totals: dict[str, dict[str, Any]], phases: tuple[str, ...]) -> int | None:
    """Total of ALL token kinds across the given phases -- the single
    'what did this cost' number. Cache reads are included because they
    are real billed input; a reader that needs them separated has the
    per-kind breakdown in the same summary."""
    result: int | None = None
    for phase in phases:
        row = totals.get(phase)
        if row is None:
            continue
        for key in _TOKEN_KEYS:
            result = _add(result, row.get(key))
    return result


class WorkTelemetryService:
    """Queries and normalized views over WorkTelemetryStore. Construct
    with an explicit store in tests; the default resolves the same
    XDG-aware path every other store in this project uses."""

    def __init__(self, store: WorkTelemetryStore | None = None) -> None:
        self.store = store or WorkTelemetryStore()

    # -- per task -------------------------------------------------------

    def task_summary(self, task_id: str) -> dict[str, Any] | None:
        """The full normalized record for one task, or None if this task
        has no telemetry at all (which is the honest answer for every
        task created before this store existed -- NOT a zero-filled row).

        Field contract (this is what other lanes read):

          work_id / task_id / session_id / project_id
          phase                      current phase marker
          analysis_tokens            all token kinds, ANALYSIS phase
          contract_tokens            all token kinds, CONTRACT phase
          worker_input_tokens        input, worker phases
          worker_output_tokens       output, worker phases
          cache_read_tokens          cache reads, worker phases
          cache_write_tokens         cache writes, worker phases
          worker_turn_count          turns, worker phases
          unattributed_tokens        all kinds, PHASE_UNKNOWN -- never
                                     folded into worker_* (see
                                     WORKER_PHASES' own docstring)
          total_tokens               every phase, every kind
          reentry_count / reentry_reasons / contract_gap_count
          first_pass_success         'TRUE' | 'FALSE' | 'UNKNOWN'
          started_at / completed_at / duration_seconds
          terminal_status
          evidence_source / evidence_sources / confidence
          has_token_telemetry        False => every token field is None
                                     because nothing was measured

        Every token field is None when unmeasured. None means UNKNOWN.
        It never means zero."""
        task = self.store.get_task(task_id)
        if task is None:
            return None
        totals = self.store.phase_totals(task_id)
        worker = {key: None for key in _TOKEN_KEYS}
        worker_turns: int | None = None
        for phase in WORKER_PHASES:
            row = totals.get(phase)
            if row is None:
                continue
            for key in _TOKEN_KEYS:
                worker[key] = _add(worker[key], row.get(key))
            worker_turns = _add(worker_turns, row.get("turn_count"))

        all_phases = tuple(totals.keys())
        reentries = self.store.list_reentries(task_id)
        # GROUP_CONCAT returns ONE comma-joined string per phase, not a
        # list -- splitting it is what makes `evidence_sources` a real set
        # of sources rather than a set of joined strings.
        sources = sorted({source
                          for row in totals.values()
                          for source in (row.get("evidence_sources") or "").split(",")
                          if source})
        sample_confidences = [c for row in totals.values()
                              for c in (row.get("confidences") or "").split(",") if c]
        counter_reset = any(bool(row.get("counter_reset")) for row in totals.values())

        summary = {
            "task_id": task["task_id"],
            "work_id": task["work_id"],
            "session_id": task["session_id"],
            "project_id": task["project_id"],
            "phase": task["phase"],
            "analysis_tokens": _sum_phase_tokens(totals, (PHASE_ANALYSIS,)),
            "contract_tokens": _sum_phase_tokens(totals, (PHASE_CONTRACT,)),
            "worker_input_tokens": worker["input_tokens"],
            "worker_output_tokens": worker["output_tokens"],
            "cache_read_tokens": worker["cache_read_tokens"],
            "cache_write_tokens": worker["cache_write_tokens"],
            "worker_turn_count": worker_turns,
            "unattributed_tokens": _sum_phase_tokens(totals, (PHASE_UNKNOWN,)),
            "total_tokens": _sum_phase_tokens(totals, all_phases),
            "reentry_count": task["reentry_count"],
            "reentry_reasons": [row["reason"] for row in reentries],
            "contract_gap_count": task["contract_gap_count"],
            "first_pass_success": task["first_pass_success"],
            "started_at": task["started_at"],
            "completed_at": task["completed_at"],
            "terminal_status": task["terminal_status"],
            "evidence_source": task["evidence_source"],
            "evidence_sources": sources,
            # Two DIFFERENT confidences, deliberately not merged:
            # `confidence` qualifies the TOKEN NUMBERS in this summary and
            # is therefore the weakest confidence of the samples that
            # produced them (UNKNOWN when there are none). Folding the
            # lifecycle row's own confidence into it would drag every
            # measured total down to UNKNOWN merely because whoever opened
            # the task did not state how they knew it had started -- a
            # fact about bookkeeping, not about the token counts.
            "confidence": (_weakest_confidence(*sample_confidences) if sample_confidences
                           else CONFIDENCE_UNKNOWN),
            "lifecycle_confidence": task["confidence"],
            "counter_reset_observed": counter_reset,
            "sample_count": sum(int(row.get("sample_count") or 0) for row in totals.values()),
        }
        summary["has_token_telemetry"] = summary["total_tokens"] is not None
        summary["duration_seconds"] = _duration_seconds(task["started_at"], task["completed_at"])
        return summary

    # -- aggregates -----------------------------------------------------

    def tokens_per_completed_task(self, *, project_id: str | None = None,
                                  work_id: str | None = None, since: str | None = None,
                                  ) -> dict[str, Any]:
        """The headline efficiency metric, with the honesty apparatus
        that makes it usable as evidence rather than as a talking point.

        Returns:
          completed_tasks           completed tasks in scope
          measured_tasks            ... of those, ones with real token data
          telemetry_coverage        measured/completed, None if no tasks
          total_tokens              sum over MEASURED tasks only (None if none)
          tokens_per_completed_task total_tokens / measured_tasks, or None
          analysis_tokens / contract_tokens / worker_* / turns per task
          first_pass_success_rate   over tasks whose tri-state RESOLVED;
                                    None while every one is still UNKNOWN
          resolved_first_pass_tasks the denominator for that rate
          reentries_per_task / contract_gaps_per_task
          reentry_reasons           {reason: count}
          confidence                weakest confidence of any input
          counter_reset_observed    a counter restarted somewhere in scope

        `tokens_per_completed_task` is None -- not 0 -- when nothing in
        scope was measured, and `telemetry_coverage` is what tells a
        reader whether the value is worth anything: comparing a
        0.2-coverage cohort against a 0.9-coverage one is not a
        comparison, and this makes that visible instead of arithmetic."""
        tasks = self.store.list_tasks(project_id=project_id, work_id=work_id,
                                      terminal_status=STATUS_COMPLETED, since=since)
        completed = len(tasks)
        measured = 0
        totals = {key: None for key in ("total_tokens", "analysis_tokens", "contract_tokens",
                                        "worker_input_tokens", "worker_output_tokens",
                                        "cache_read_tokens", "cache_write_tokens",
                                        "worker_turn_count")}
        reentries = 0
        gaps = 0
        reasons: dict[str, int] = {}
        first_pass_true = 0
        first_pass_resolved = 0
        confidences: list[str] = []
        counter_reset = False

        for task in tasks:
            summary = self.task_summary(task["task_id"])
            if summary is None:  # pragma: no cover -- list_tasks just returned it
                continue
            if summary["has_token_telemetry"]:
                measured += 1
                for key in totals:
                    totals[key] = _add(totals[key], summary[key])
                confidences.append(summary["confidence"])
            reentries += summary["reentry_count"]
            gaps += summary["contract_gap_count"]
            for reason in summary["reentry_reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
            if summary["first_pass_success"] in (FIRST_PASS_TRUE, FIRST_PASS_FALSE):
                first_pass_resolved += 1
                if summary["first_pass_success"] == FIRST_PASS_TRUE:
                    first_pass_true += 1
            counter_reset = counter_reset or summary["counter_reset_observed"]

        def per_measured(value: int | None) -> float | None:
            if value is None or measured == 0:
                return None
            return round(value / measured, 2)

        return {
            "scope": {"project_id": project_id, "work_id": work_id, "since": since},
            "completed_tasks": completed,
            "measured_tasks": measured,
            "telemetry_coverage": round(measured / completed, 4) if completed else None,
            "total_tokens": totals["total_tokens"],
            "tokens_per_completed_task": per_measured(totals["total_tokens"]),
            "analysis_tokens_per_task": per_measured(totals["analysis_tokens"]),
            "contract_tokens_per_task": per_measured(totals["contract_tokens"]),
            "worker_input_tokens_per_task": per_measured(totals["worker_input_tokens"]),
            "worker_output_tokens_per_task": per_measured(totals["worker_output_tokens"]),
            "cache_read_tokens_per_task": per_measured(totals["cache_read_tokens"]),
            "cache_write_tokens_per_task": per_measured(totals["cache_write_tokens"]),
            "worker_turns_per_task": per_measured(totals["worker_turn_count"]),
            "totals": totals,
            "reentries_per_task": round(reentries / completed, 2) if completed else None,
            "reentry_count": reentries,
            "reentry_reasons": reasons,
            "contract_gap_count": gaps,
            "contract_gaps_per_task": round(gaps / completed, 2) if completed else None,
            "first_pass_success_rate": (round(first_pass_true / first_pass_resolved, 4)
                                        if first_pass_resolved else None),
            "resolved_first_pass_tasks": first_pass_resolved,
            "unresolved_first_pass_tasks": completed - first_pass_resolved,
            "confidence": _weakest_confidence(*confidences) if confidences else CONFIDENCE_UNKNOWN,
            "counter_reset_observed": counter_reset,
        }

    def work_summary(self, work_id: str) -> dict[str, Any]:
        """Rollup for one Work/outcome: its tasks' summaries plus the
        same aggregate.

        `open_tasks` is reported separately and never merged into the
        completed-task aggregate -- a work item whose expensive task is
        still running would otherwise look cheap until the moment it
        finished."""
        tasks = self.store.list_tasks(work_id=work_id)
        summaries = [self.task_summary(task["task_id"]) for task in tasks]
        return {
            "work_id": work_id,
            "task_count": len(tasks),
            "open_tasks": sum(1 for task in tasks if task["completed_at"] is None),
            "first_pass_unknown_tasks": sum(
                1 for task in tasks if task["first_pass_success"] == FIRST_PASS_UNKNOWN),
            "tasks": summaries,
            "aggregate": self.tokens_per_completed_task(work_id=work_id),
        }


def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    """None unless both ends are real, parseable ISO timestamps -- a
    half-known duration is not a duration."""
    if not started_at or not completed_at:
        return None
    from datetime import datetime
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(completed_at)
    except ValueError:
        return None
    return round((end - start).total_seconds(), 3)
