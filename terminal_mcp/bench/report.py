"""Assembles the comparison and renders it.

The design rule is that this report must be MORE convincing when it
says nothing than when it says something. A benchmark that can only
produce a number will produce one whether or not the data supports it,
so every escape from INSUFFICIENT_DATA is gated:

* RISK CLASSES ARE NEVER POOLED. HIGH_RISK, STANDARD and FAST_FIX get
  their own comparison each. The effect can genuinely run in opposite
  directions across them, and pooling cancels a real result into a
  null -- so there is deliberately no "overall" number anywhere in this
  report, only per-class ones.
* SAMPLE SIZE BANDS ARE POWER-BASED, NOT ROUND NUMBERS. 10 is a
  REPORTING FLOOR, not a sample size. Below 10 nothing is shown but
  distributions. 10-39 (LOW) is descriptive only: distributions, no
  direction, no percentage -- that is the region where a "significant"
  bootstrap result is more likely noise than signal. A count metric
  such as worker turns needs ~40-100/arm to detect a realistic effect,
  and a BINARY like first-pass success needs ~100/arm for a 40%->60%
  shift at 80% power -- hence MODERATE at 40 and HIGH at 100, with
  binary metrics held to the higher bar.
* WITHOUT RANDOMISATION, EVERY CLAIM IS DESCRIPTIVE. Matching controls
  make an observational study, and at these sample sizes matching
  cannot balance task difficulty, which is the confounder that swamps
  the effect. So `assignment` is a required, disclosed input: only
  `randomised` (assigned at task creation, before anyone read the task
  closely enough to judge difficulty) permits a directional claim.
  `interleaved` is weaker but blind at the moment of assignment;
  `observational` is descriptive regardless of N.
* THE DECISION QUANTITY IS COST PER COMPLETED TASK. A turn reduction
  that costs more than it saves is a loss, and an insignificant turn
  reduction bought cheaply can still be a win -- so the cost ratio is
  reported next to the turn delta, never instead of it.

`MIN_MATCHED_PER_GROUP`, `CONFIDENCE_BANDS` and `ASSIGNMENT_*` are the
first constants in the file on purpose: the measurement contract is
expected to keep moving, and moving it should be a one-line edit.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Sequence

from . import pricing, stats
from .matching import ALL_CONTROLS, DEFAULT_CONTROLS, MatchResult, match_cohorts
from .model import (
    COHORT_LEGACY,
    COHORT_NEW,
    EXCLUDED_REENTRY_REASONS,
    FLAG_CACHE_TTL_UNSPLIT,
    FLAG_EXCLUDED_REASON_ONLY,
    FLAG_INCOMPLETE_USAGE,
    FLAG_NO_USAGE,
    FLAG_UNKNOWN_MODEL_PRICE,
    RISK_CLASSES,
    TTL_SPLIT_REQUIRED,
    SourceStatus,
    TaskRecord,
)

HARNESS_VERSION = "1.1.0"

# Reporting floor. Below this, no comparison is stated at all.
MIN_MATCHED_PER_GROUP = 10

# (minimum matched tasks per arm, label, directional claim allowed for a
# COUNT metric, directional claim allowed for a BINARY metric).
CONFIDENCE_BANDS: tuple[tuple[int, str, bool, bool], ...] = (
    (100, "HIGH", True, True),
    (40, "MODERATE", True, False),
    (MIN_MATCHED_PER_GROUP, "LOW", False, False),
)
INSUFFICIENT = "INSUFFICIENT_DATA"
_BAND_ORDER = (INSUFFICIENT, "LOW", "MODERATE", "HIGH")

# How tasks were put into cohorts. Only the first permits a directional
# claim; the others are descriptive at any sample size.
ASSIGNMENT_RANDOMISED = "randomised"
ASSIGNMENT_INTERLEAVED = "interleaved"
ASSIGNMENT_OBSERVATIONAL = "observational"
ASSIGNMENTS = (ASSIGNMENT_RANDOMISED, ASSIGNMENT_INTERLEAVED, ASSIGNMENT_OBSERVATIONAL)

MIN_USAGE_COVERAGE = 0.80


@dataclass(frozen=True)
class Metric:
    key: str
    label: str
    extract: Callable[[TaskRecord], float | None]
    unit: str = "tokens"
    binary: bool = False
    headline: bool = False
    primary: bool = False
    note: str = ""


def _cost_units(ttl_policy: str) -> Callable[[TaskRecord], float | None]:
    return lambda record: record.cost_units(ttl_policy=ttl_policy)


def build_metrics(ttl_policy: str = TTL_SPLIT_REQUIRED) -> tuple[Metric, ...]:
    return (
        Metric(
            "cost_units",
            "Cost (base-input-equivalents)",
            _cost_units(ttl_policy),
            unit="units",
            headline=True,
            note="input + 1.25x cache-write-5m + 2x cache-write-1h + R_read x cache-read "
            "+ 5x output, priced per model",
        ),
        Metric(
            "worker_turn_count",
            "Worker turns",
            lambda r: None if r.worker_turn_count is None else float(r.worker_turn_count),
            unit="turns",
            primary=True,
            note="counted by collapsing dispatch_idempotency_key, never from attempt_count",
        ),
        Metric(
            "reentries",
            "Re-entries (excluded reasons removed)",
            lambda r: float(r.counted_reentry_count) if r.reentries or r.worker_turn_count is not None else None,
            unit="re-entries",
        ),
        Metric(
            "primary_cost_tokens",
            "primary_cost_tokens (input + cache write)",
            lambda r: _f(r.usage.primary_cost_tokens),
            note="raw diagnostic from the original brief; NOT decided on -- it sums tokens of "
            "different unit cost and omits output, which biases it toward the up-front arm",
        ),
        Metric("input_tokens", "Input tokens (uncached remainder)", lambda r: _f(r.usage.input_tokens)),
        Metric("output_tokens", "Output tokens", lambda r: _f(r.usage.output_tokens)),
        Metric("cache_read_tokens", "Cache read tokens", lambda r: _f(r.usage.cache_read_tokens)),
        Metric("cache_write_tokens", "Cache write tokens", lambda r: _f(r.usage.cache_write_tokens)),
        Metric("total_prompt_tokens", "Total prompt tokens", lambda r: _f(r.usage.total_prompt_tokens)),
        Metric("retries", "Retries", lambda r: _f(r.retries), unit="retries"),
        Metric("duration_seconds", "Duration", lambda r: r.duration_seconds, unit="seconds"),
    )


def _f(value: int | None) -> float | None:
    return None if value is None else float(value)


def annotate(records: Iterable[TaskRecord], *, ttl_policy: str = TTL_SPLIT_REQUIRED) -> tuple[TaskRecord, ...]:
    """Attach the flags the report explains itself with. Purely
    additive -- no record is ever removed here."""
    annotated: list[TaskRecord] = []
    for record in records:
        flags: list[str] = []
        if record.usage.is_empty:
            flags.append(FLAG_NO_USAGE)
        elif not record.usage.is_complete:
            flags.append(FLAG_INCOMPLETE_USAGE)
        if record.has_excluded_reason_only:
            flags.append(FLAG_EXCLUDED_REASON_ONLY)
        if not record.usage.is_empty and not record.usage.has_ttl_split:
            flags.append(FLAG_CACHE_TTL_UNSPLIT)
        if not record.usage.is_empty and pricing.lookup(record.model) is None:
            flags.append(FLAG_UNKNOWN_MODEL_PRICE)
        annotated.append(record.with_flags(*flags) if flags else record)
    return tuple(annotated)


@dataclass(frozen=True)
class MetricComparison:
    metric: Metric
    legacy: stats.Summary
    new: stats.Summary
    savings: stats.SavingsEstimate
    reported: bool
    suppressed_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.metric.key,
            "label": self.metric.label,
            "unit": self.metric.unit,
            "headline": self.metric.headline,
            "primary": self.metric.primary,
            "note": self.metric.note,
            "legacy": self.legacy.as_dict(),
            "new_pipeline": self.new.as_dict(),
            "savings": self.savings.as_dict() if self.reported else None,
            "reported": self.reported,
            "suppressed_reason": self.suppressed_reason,
        }


@dataclass(frozen=True)
class GroupCoverage:
    matched: int
    with_complete_usage: int
    with_any_usage: int
    priceable: int
    excluded_reason_only: int
    first_pass_known: int
    first_pass_successes: int

    @property
    def usage_coverage(self) -> float | None:
        return None if self.matched == 0 else self.with_complete_usage / self.matched

    @property
    def first_pass_rate(self) -> float | None:
        return stats.rate(self.first_pass_successes, self.first_pass_known)

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "with_complete_usage": self.with_complete_usage,
            "with_any_usage": self.with_any_usage,
            "priceable": self.priceable,
            "excluded_reason_only": self.excluded_reason_only,
            "first_pass_known": self.first_pass_known,
            "first_pass_successes": self.first_pass_successes,
            "usage_coverage": self.usage_coverage,
            "first_pass_success_rate_percent": self.first_pass_rate,
        }


@dataclass(frozen=True)
class RiskGroupReport:
    """One never-pooled risk class."""

    risk_class: str
    verdict: str
    confidence: str
    directional_claim_allowed: bool
    match: MatchResult
    coverage: dict[str, GroupCoverage]
    comparisons: tuple[MetricComparison, ...]
    cost_ratio: float | None
    reentry_reasons: dict[str, dict[str, int]]
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "directional_claim_allowed": self.directional_claim_allowed,
            "matching": self.match.as_dict(),
            "coverage": {name: cov.as_dict() for name, cov in self.coverage.items()},
            "metrics": [comparison.as_dict() for comparison in self.comparisons],
            "cost_ratio_new_over_legacy": self.cost_ratio,
            "reentry_reasons": self.reentry_reasons,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class BenchmarkReport:
    generated_at: str
    harness_version: str
    price_table_version: str
    assignment: str
    ttl_policy: str
    verdict: str
    min_matched_per_group: int
    sources: tuple[SourceStatus, ...]
    groups: tuple[RiskGroupReport, ...]
    loaded_tasks: int
    warnings: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "harness_version": self.harness_version,
            "price_table_version": self.price_table_version,
            "assignment": self.assignment,
            "cache_ttl_policy": self.ttl_policy,
            "verdict": self.verdict,
            "min_matched_per_group": self.min_matched_per_group,
            "loaded_tasks": self.loaded_tasks,
            "sources": [status.as_dict() for status in self.sources],
            "risk_groups": [group.as_dict() for group in self.groups],
            "warnings": list(self.warnings),
            "notes": list(self.notes),
        }


def _coverage(records: Sequence[TaskRecord], ttl_policy: str) -> GroupCoverage:
    known = [r.first_pass_success_adjusted for r in records if r.first_pass_success_adjusted is not None]
    return GroupCoverage(
        matched=len(records),
        with_complete_usage=sum(1 for r in records if r.usage.is_complete),
        with_any_usage=sum(1 for r in records if not r.usage.is_empty),
        priceable=sum(1 for r in records if r.cost_units(ttl_policy=ttl_policy) is not None),
        excluded_reason_only=sum(1 for r in records if r.has_excluded_reason_only),
        first_pass_known=len(known),
        first_pass_successes=sum(1 for value in known if value),
    )


def _reason_totals(records: Sequence[TaskRecord]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for record in records:
        for reason, count in record.reentry_counts_by_reason().items():
            totals[reason] = totals.get(reason, 0) + count
    return totals


def _values(records: Sequence[TaskRecord], metric: Metric) -> list[float]:
    """Only tasks that actually have this measurement. Missing is
    skipped, never imputed -- which is why each Summary carries its own
    `n` rather than inheriting the group size."""
    collected: list[float] = []
    for record in records:
        value = metric.extract(record)
        if value is not None:
            collected.append(float(value))
    return collected


def _demote(label: str, steps: int = 1) -> str:
    """Knock a band down, but never past LOW. INSUFFICIENT_DATA is
    reserved for the sample-size floor -- a group that HAS enough
    measured tasks but earns every warning is a weak comparison, which
    is what LOW means, not an absent one."""
    return _BAND_ORDER[max(1, _BAND_ORDER.index(label) - steps)]


def _band(matched_per_group: int) -> tuple[str, bool, bool]:
    for threshold, label, count_ok, binary_ok in CONFIDENCE_BANDS:
        if matched_per_group >= threshold:
            return label, count_ok, binary_ok
    return INSUFFICIENT, False, False


def _build_group(
    risk_class: str,
    records: Sequence[TaskRecord],
    *,
    controls: Sequence[str],
    metrics: Sequence[Metric],
    assignment: str,
    ttl_policy: str,
    min_matched_per_group: int,
    bootstrap_seed: int,
) -> RiskGroupReport:
    match = match_cohorts(records, controls=controls)
    legacy = annotate(match.legacy, ttl_policy=ttl_policy)
    new = annotate(match.new, ttl_policy=ttl_policy)
    coverage = {COHORT_LEGACY: _coverage(legacy, ttl_policy), COHORT_NEW: _coverage(new, ttl_policy)}
    matched_per_group = min(len(legacy), len(new))
    # The floor counts MEASURED tasks, not matched ones. Comparing a
    # 0.2-coverage cohort against a 0.9-coverage one is not a
    # comparison, and any statistic taken over all completed tasks
    # falls as coverage falls -- which looks exactly like an efficiency
    # win. So a group only clears the floor when both arms have enough
    # tasks that actually carry telemetry.
    measured_per_group = min(
        coverage[COHORT_LEGACY].with_any_usage, coverage[COHORT_NEW].with_any_usage
    )
    enough = matched_per_group >= min_matched_per_group and measured_per_group >= min_matched_per_group

    warnings: list[str] = list(match.warnings)
    band_size = min(matched_per_group, measured_per_group)
    confidence, count_ok, binary_ok = _band(band_size) if enough else (INSUFFICIENT, False, False)

    if matched_per_group >= min_matched_per_group and measured_per_group < min_matched_per_group:
        warnings.append(
            f"LOW_COVERAGE: {risk_class} has {matched_per_group} matched tasks per arm but only "
            f"{measured_per_group} carry telemetry; the floor counts measured tasks, so no "
            "comparison is stated"
        )
    legacy_share = coverage[COHORT_LEGACY].usage_coverage
    new_share = coverage[COHORT_NEW].usage_coverage
    if legacy_share is not None and new_share is not None and abs(legacy_share - new_share) > 0.20:
        warnings.append(
            f"COVERAGE_IMBALANCE: telemetry coverage differs between arms in {risk_class} "
            f"({legacy_share:.0%} legacy vs {new_share:.0%} new-pipeline); a coverage difference "
            "alone can look identical to an efficiency win"
        )

    if enough:
        for name, group in coverage.items():
            share = group.usage_coverage
            if share is not None and share < MIN_USAGE_COVERAGE:
                warnings.append(
                    f"THIN_TELEMETRY: only {share:.0%} of matched {name} tasks in {risk_class} "
                    "carry complete token telemetry; token medians describe that subset, not the "
                    "whole cohort"
                )
        if any("MIXED_COMPLEXITY" in warning for warning in warnings):
            confidence = _demote(confidence)
        if any("THIN_TELEMETRY" in warning for warning in warnings):
            confidence = _demote(confidence)
        if any("COVERAGE_IMBALANCE" in warning for warning in warnings):
            confidence = _demote(confidence)

    directional = enough and assignment == ASSIGNMENT_RANDOMISED and count_ok
    if enough and assignment != ASSIGNMENT_RANDOMISED:
        warnings.append(
            f"NOT_RANDOMISED: cohort assignment was '{assignment}', so every figure in "
            f"{risk_class} is a description of what happened, not a measured effect of the "
            "pipeline -- task difficulty is not balanced by matching at this sample size"
        )

    comparisons: list[MetricComparison] = []
    for metric in metrics:
        legacy_values = _values(legacy, metric)
        new_values = _values(new, metric)
        legacy_summary = stats.summarize(legacy_values)
        new_summary = stats.summarize(new_values)
        available = min(len(legacy_values), len(new_values))
        reason: str | None = None
        if not enough:
            reason = (
                f"fewer than {min_matched_per_group} matched, measured tasks per group "
                f"({matched_per_group} matched, {measured_per_group} measured)"
            )
        elif available < min_matched_per_group:
            reason = f"only {available} matched task(s) per group record this metric"
        elif not directional:
            allowed = binary_ok if metric.binary else count_ok
            if assignment != ASSIGNMENT_RANDOMISED:
                reason = f"descriptive only — assignment was '{assignment}', not randomised"
            elif not allowed:
                reason = (
                    f"descriptive only — {confidence} band ({band_size}/arm) has "
                    f"insufficient power for a directional claim on this metric"
                )
        elif metric.binary and not binary_ok:
            reason = (
                f"descriptive only — a binary metric needs ~100 matched tasks per arm; "
                f"{band_size} available"
            )
        if reason is not None:
            comparisons.append(
                MetricComparison(metric, legacy_summary, new_summary, stats.EMPTY_SAVINGS, False, reason)
            )
            continue
        estimate = stats.savings(legacy_values, new_values, seed=bootstrap_seed)
        comparisons.append(MetricComparison(metric, legacy_summary, new_summary, estimate, True, None))

    cost_ratio = _cost_ratio(legacy, new, ttl_policy)
    return RiskGroupReport(
        risk_class=risk_class,
        verdict=INSUFFICIENT if not enough else "COMPARISON_AVAILABLE",
        confidence=confidence,
        directional_claim_allowed=directional,
        match=match,
        coverage=coverage,
        comparisons=tuple(comparisons),
        cost_ratio=cost_ratio,
        reentry_reasons={
            COHORT_LEGACY: _reason_totals(legacy),
            COHORT_NEW: _reason_totals(new),
        },
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _cost_ratio(
    legacy: Sequence[TaskRecord], new: Sequence[TaskRecord], ttl_policy: str
) -> float | None:
    """Median cost per completed task, new / legacy. Below 1.0 the new
    pipeline is cheaper per task -- the quantity a decision is actually
    made on, as opposed to a turn delta that says nothing about what
    the turns cost."""
    legacy_costs = [r.cost_units(ttl_policy=ttl_policy) for r in legacy]
    new_costs = [r.cost_units(ttl_policy=ttl_policy) for r in new]
    legacy_median = stats.median([c for c in legacy_costs if c is not None])
    new_median = stats.median([c for c in new_costs if c is not None])
    if legacy_median is None or new_median is None or legacy_median == 0:
        return None
    return new_median / legacy_median


def build_report(
    records: Iterable[TaskRecord],
    *,
    controls: Sequence[str] = DEFAULT_CONTROLS,
    sources: Sequence[SourceStatus] = (),
    warnings: Sequence[str] = (),
    assignment: str = ASSIGNMENT_OBSERVATIONAL,
    ttl_policy: str = TTL_SPLIT_REQUIRED,
    min_matched_per_group: int = MIN_MATCHED_PER_GROUP,
    bootstrap_seed: int = stats.DEFAULT_BOOTSTRAP_SEED,
    notes: Sequence[str] = (),
) -> BenchmarkReport:
    if assignment not in ASSIGNMENTS:
        raise ValueError(f"assignment must be one of {ASSIGNMENTS}, got {assignment!r}")
    # Validated here, not only inside match_cohorts: with zero loaded
    # tasks no stratum is ever built, so a typo'd control would sail
    # through and produce an innocent-looking empty report instead of
    # an error.
    unknown_controls = [name for name in controls if name not in ALL_CONTROLS]
    if unknown_controls:
        raise ValueError(f"unknown control dimension(s): {', '.join(unknown_controls)}")
    all_records = annotate(records, ttl_policy=ttl_policy)
    metrics = build_metrics(ttl_policy)

    by_class: dict[str, list[TaskRecord]] = {name: [] for name in RISK_CLASSES}
    for record in all_records:
        by_class[record.risk_class].append(record)

    groups = tuple(
        _build_group(
            name,
            by_class[name],
            controls=controls,
            metrics=metrics,
            assignment=assignment,
            ttl_policy=ttl_policy,
            min_matched_per_group=min_matched_per_group,
            bootstrap_seed=bootstrap_seed,
        )
        for name in RISK_CLASSES
        if by_class[name]
    )
    verdict = (
        "COMPARISON_AVAILABLE"
        if any(group.verdict == "COMPARISON_AVAILABLE" for group in groups)
        else INSUFFICIENT
    )
    return BenchmarkReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        harness_version=HARNESS_VERSION,
        price_table_version=pricing.PRICE_TABLE_VERSION,
        assignment=assignment,
        ttl_policy=ttl_policy,
        verdict=verdict,
        min_matched_per_group=min_matched_per_group,
        sources=tuple(sources),
        groups=groups,
        loaded_tasks=len(all_records),
        warnings=tuple(dict.fromkeys(warnings)),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _number(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "seconds":
        return f"{value:,.1f}s"
    if value == int(value):
        return f"{int(value):,}"
    return f"{value:,.2f}"


def _summary_cell(summary: stats.Summary, unit: str) -> str:
    if summary.n == 0:
        return "— (n=0)"
    body = (
        f"{_number(summary.median, unit)} "
        f"[{_number(summary.p25, unit)}–{_number(summary.p75, unit)}] (n={summary.n}"
    )
    if summary.outliers:
        body += f", {summary.outliers} outlier{'s' if summary.outliers != 1 else ''}"
    return body + ")"


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def render_markdown(report: BenchmarkReport) -> str:
    lines: list[str] = []
    lines.append("# Before/After Efficiency Benchmark")
    lines.append("")
    lines.append(f"- Generated: `{report.generated_at}` (harness v{report.harness_version})")
    lines.append(f"- Verdict: **{report.verdict}**")
    lines.append(f"- Cohort assignment: **{report.assignment}**")
    lines.append(f"- Price table: `{report.price_table_version}`, cache-TTL policy `{report.ttl_policy}`")
    lines.append(f"- Tasks loaded from all sources: {report.loaded_tasks}")
    lines.append(
        f"- Reporting floor: {report.min_matched_per_group} matched tasks per arm, per risk class"
    )
    lines.append("")

    if report.verdict == INSUFFICIENT:
        lines.append(
            "> **INSUFFICIENT_DATA — no efficiency claim is made anywhere in this report.** "
            f"No risk class reached {report.min_matched_per_group} matched tasks in both arms. "
            "Whatever distributions exist are shown below so the shape of the data is visible; "
            "none of them is a result. This report becomes a comparison automatically once the "
            "threshold is met — nothing needs to be re-enabled."
        )
        lines.append("")

    lines.append("## Sources")
    lines.append("")
    lines.append("| Source | Path | Available | Tasks | Detail |")
    lines.append("| --- | --- | --- | ---: | --- |")
    for status in report.sources:
        lines.append(
            f"| `{status.name}` | `{status.path or '—'}` | {'yes' if status.available else 'no'} "
            f"| {status.task_count} | {status.detail} |"
        )
    lines.append("")

    if not report.groups:
        lines.append("## Risk classes")
        lines.append("")
        lines.append("No tasks were loaded, so there is nothing to stratify.")
        lines.append("")
    for group in report.groups:
        lines.extend(_render_group(group))

    if report.warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in report.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## How to read this")
    lines.append("")
    lines.append(
        "- **Risk classes are never pooled.** There is no overall number on purpose: the effect "
        "can run in opposite directions across HIGH_RISK / STANDARD / FAST_FIX, and pooling "
        "cancels a real result into a null."
    )
    lines.append(
        "- Figures are **medians** with a p25–p75 spread, over tasks matched pairwise within a "
        "risk class. A single extreme task cannot move them; outliers are counted, not deleted."
    )
    lines.append(
        "- **Cost (base-input-equivalents)** is the metric decisions are made on: every token "
        "converted at its real billing ratio (cache-write 1.25x/2x by TTL, cache-read ~0.1x, "
        "output 5x). `primary_cost_tokens` is shown because the brief named it, but it is a raw "
        "diagnostic — it sums tokens of different unit cost and omits output entirely."
    )
    lines.append(
        "- **worker_turn_count** is the primary statistical metric (a count has far more power "
        "than a binary at these sizes); first-pass success is the headline outcome but needs "
        "~100 matched tasks per arm before any direction is claimed."
    )
    lines.append(
        "- A saving is the **lesser** of the point estimate and the lower bound of a 90% seeded "
        "bootstrap interval, floored at 0%, and is only ever stated **under randomisation**. "
        f"Assignment here was `{report.assignment}`"
        + (
            "."
            if report.assignment == ASSIGNMENT_RANDOMISED
            else " — so every figure is descriptive, whatever the sample size."
        )
    )
    lines.append(
        "- A missing measurement is counted as missing, never as zero. Each cell's `n` is the "
        "number of tasks that actually recorded that metric."
    )
    for note in report.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _render_group(group: RiskGroupReport) -> list[str]:
    lines: list[str] = []
    lines.append(f"## Risk class: {group.risk_class}")
    lines.append("")
    lines.append(f"- Verdict: **{group.verdict}** · confidence **{group.confidence}**")
    lines.append(
        f"- Matched tasks: legacy {len(group.match.legacy)} / new-pipeline {len(group.match.new)} "
        f"across {sum(1 for s in group.match.strata if s.matched_size > 0)} stratum/strata "
        f"(controls: {', '.join(group.match.controls)})"
    )
    if group.cost_ratio is not None:
        lines.append(
            f"- Cost per completed task (new / legacy): **{group.cost_ratio:.2f}x** "
            + ("— cheaper" if group.cost_ratio < 1 else "— more expensive" if group.cost_ratio > 1 else "— level")
        )
    else:
        lines.append("- Cost per completed task: — (not priceable from the available telemetry)")
    legacy_cov = group.coverage[COHORT_LEGACY]
    new_cov = group.coverage[COHORT_NEW]
    lines.append("")
    lines.append("| | Legacy | New pipeline |")
    lines.append("| --- | ---: | ---: |")
    lines.append(f"| Matched tasks | {legacy_cov.matched} | {new_cov.matched} |")
    lines.append(
        f"| Complete token telemetry | {legacy_cov.with_complete_usage} | {new_cov.with_complete_usage} |"
    )
    lines.append(f"| Priceable (cost units computable) | {legacy_cov.priceable} | {new_cov.priceable} |")
    lines.append(
        f"| First-pass success (verified only) | {_percent(legacy_cov.first_pass_rate)} "
        f"({legacy_cov.first_pass_known} known) | {_percent(new_cov.first_pass_rate)} "
        f"({new_cov.first_pass_known} known) |"
    )
    lines.append(
        f"| Re-entries from excluded reasons only | {legacy_cov.excluded_reason_only} "
        f"| {new_cov.excluded_reason_only} |"
    )
    binary_ok = _band(min(len(group.match.legacy), len(group.match.new)))[2]
    if group.directional_claim_allowed and binary_ok:
        lines.append("")
        lines.append(
            "First-pass success is powered for a directional claim in this class "
            f"({min(len(group.match.legacy), len(group.match.new))} matched tasks per arm)."
        )
    else:
        lines.append("")
        lines.append(
            "First-pass success above is **descriptive only** — a 40%→60% shift needs roughly "
            "100 matched tasks per arm under randomisation before a direction can be claimed."
        )
    lines.append("")
    lines.append(
        "Tasks left out: "
        + ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in sorted(group.match.dropped.items()))
        + "."
    )
    lines.append("")
    lines.append("| Metric | Legacy | New pipeline | Conservative saving |")
    lines.append("| --- | --- | --- | --- |")
    for comparison in group.comparisons:
        unit = comparison.metric.unit
        if comparison.reported:
            estimate = comparison.savings
            if estimate.reported_percent is None:
                saving = "—"
            elif not estimate.significant:
                saving = "no measurable difference"
            else:
                saving = f"**{estimate.reported_percent:.1f}%**"
        else:
            saving = f"not stated ({comparison.suppressed_reason})"
        label = comparison.metric.label
        if comparison.metric.headline:
            label += " ⭐ decision metric"
        elif comparison.metric.primary:
            label += " ◆ primary statistical metric"
        lines.append(
            f"| {label} | {_summary_cell(comparison.legacy, unit)} "
            f"| {_summary_cell(comparison.new, unit)} | {saving} |"
        )
    lines.append("")
    reasons = sorted(set(group.reentry_reasons[COHORT_LEGACY]) | set(group.reentry_reasons[COHORT_NEW]))
    if reasons:
        lines.append("| Re-entry reason | Legacy | New pipeline | Counted |")
        lines.append("| --- | ---: | ---: | --- |")
        for reason in reasons:
            counted = "no — reported separately" if reason in EXCLUDED_REENTRY_REASONS else "yes"
            lines.append(
                f"| `{reason}` | {group.reentry_reasons[COHORT_LEGACY].get(reason, 0)} "
                f"| {group.reentry_reasons[COHORT_NEW].get(reason, 0)} | {counted} |"
            )
        lines.append("")
    if group.warnings:
        for warning in group.warnings:
            lines.append(f"> ⚠ {warning}")
        lines.append("")
    return lines
