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
    KNOWN_REENTRY_REASONS,
    FLAG_CACHE_TTL_UNSPLIT,
    FLAG_EXCLUDED_REASON_ONLY,
    FLAG_FPS_DISAGREEMENT,
    FLAG_INCOMPLETE_USAGE,
    FLAG_NO_USAGE,
    FLAG_UNKNOWN_MODEL_PRICE,
    RISK_CLASSES,
    STALE_CONTEXT,
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
# This no longer gates the headline -- `stats.explained_by_missingness`
# does, exactly and without any threshold. An earlier version keyed the
# headline on the GAP in scoreability between arms, which was wrong: the
# identification interval's width is (1 - coverage), so it is governed
# by how much is missing, not by how much MORE is missing in one arm.
# Two arms at equal, moderate coverage can show a hundred-point apparent
# difference whose true rates are identical, and a gap rule scores that
# as perfectly safe. Equal coverage is not safety.
#
# What this constant still decides is when a lopsided denominator is
# worth SAYING. Differential measurement is a selection concern in its
# own right, separate from whether coverage can explain the result, so
# it is reported and demotes the band even when the effect survives.
#
# The primary rule is relative and comes from the arithmetic rather than
# from a chosen number. If an arm scores a fraction c of its tasks at
# observed rate r, its true rate lies in [r*c, r*c + (1-c)] -- the
# unscored tasks are, at the extremes, all failures or all successes.
# Working that through, a scoreability gap of G percentage points can
# account for up to exactly G points of apparent first-pass difference.
# So the headline moves whenever the gap is at least as large as the
# difference it would have to explain: at that point coverage alone is a
# complete explanation for the result, and the metric is uninformative
# no matter how clean the rest of the comparison is.
FPS_COVERAGE_GAP = 0.10


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
    # Render this metric's cells alongside another metric's, so the two
    # can never be quoted apart. primary_cost_tokens is paired with
    # cost_units for exactly that reason: on the same cohort they can
    # point in opposite directions, and seeing one without the other is
    # how the refuted metric comes back.
    paired_with: str | None = None


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
            "reentries_excl_stale",
            "Re-entries, also excluding STALE_CONTEXT",
            lambda r: float(r.counted_reentry_count_excluding(frozenset({STALE_CONTEXT})))
            if r.reentries or r.worker_turn_count is not None
            else None,
            unit="re-entries",
            note="staleness caused by a longer analysis prefix is a downstream cost of the "
            "treatment (a mediator), not a confounder -- so the row above keeps it. The gap "
            "between the two rows is the part of rework attributable to carrying more context",
        ),
        Metric(
            "primary_cost_tokens",
            "primary_cost_tokens (input + cache write)",
            lambda r: _f(r.usage.primary_cost_tokens),
            note="raw diagnostic from the original brief; NOT decided on -- it sums tokens of "
            "different unit cost and omits output, which biases it toward the up-front arm",
            paired_with="cost_units",
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
    first_pass_disagreements: int

    @property
    def first_pass_coverage(self) -> float | None:
        """Share of matched tasks whose first-pass outcome is scoreable
        at all. A first-class result, not a footnote: the HIGH
        decision-budget arm is required by its own contract to carry a
        live verification plan, so it is systematically MORE likely to
        record the evidence that makes a task scoreable. The treatment
        therefore changes the probability a task can be measured on the
        very metric being compared, and the direction is predictable --
        it selects well-run control-arm tasks out of the denominator and
        inflates the treatment arm's apparent first-pass success. That
        is structural, not an accident, so the rate is never printed
        without this number beside it."""
        return None if self.matched == 0 else self.first_pass_known / self.matched

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
            "first_pass_disagreements": self.first_pass_disagreements,
            "first_pass_coverage": self.first_pass_coverage,
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
    headline_metric: str
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "risk_class": self.risk_class,
            "headline_metric": self.headline_metric,
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
    unavailable_reasons: tuple[str, ...]
    profile_sources: tuple[str, ...]
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
            "unavailable_reentry_reasons": list(self.unavailable_reasons),
            "stratification_key_sources": list(self.profile_sources),
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
        first_pass_disagreements=sum(1 for r in records if r.first_pass_success_disagrees),
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
    # First-pass success is the headline outcome only while its
    # denominator does not depend on the arm. Where scoreability
    # differs between arms, the headline moves to worker turns, whose
    # denominator is every matched task regardless of treatment.
    legacy_fps_coverage = coverage[COHORT_LEGACY].first_pass_coverage
    new_fps_coverage = coverage[COHORT_NEW].first_pass_coverage
    headline_metric = "first_pass_success"
    legacy_rate = coverage[COHORT_LEGACY].first_pass_rate
    new_rate = coverage[COHORT_NEW].first_pass_rate
    legacy_interval = stats.identification_interval(
        None if legacy_rate is None else legacy_rate / 100.0, legacy_fps_coverage
    )
    new_interval = stats.identification_interval(
        None if new_rate is None else new_rate / 100.0, new_fps_coverage
    )
    if stats.explained_by_missingness(
        None if legacy_rate is None else legacy_rate / 100.0,
        legacy_fps_coverage,
        None if new_rate is None else new_rate / 100.0,
        new_fps_coverage,
    ):
        # Equal true rates are consistent with what was observed, so no
        # directional claim on this metric is licensed -- whatever the
        # coverage gap, whatever the sample size.
        headline_metric = "worker_turn_count"
        warnings.append(
            f"IDENTIFICATION / FPS_NOT_IDENTIFIED: in {risk_class}, first-pass success is "
            f"scoreable for "
            f"{legacy_fps_coverage:.0%} of matched legacy tasks and {new_fps_coverage:.0%} of "
            f"new-pipeline tasks, so their true rates lie anywhere in "
            f"[{legacy_interval[0]:.0%}, {legacy_interval[1]:.0%}] and "
            f"[{new_interval[0]:.0%}, {new_interval[1]:.0%}]. Those ranges overlap: equal true "
            "rates are consistent with the data, so the observed difference could be entirely "
            "an artefact of which tasks were scoreable. This is a missing-outcome problem, not "
            "a sample-size one -- more tasks will not shrink those ranges, only recording the "
            "outcomes will. The headline moves to worker turns, whose denominator does not "
            "depend on the arm"
        )
    if (
        legacy_fps_coverage is not None
        and new_fps_coverage is not None
        and abs(legacy_fps_coverage - new_fps_coverage) > FPS_COVERAGE_GAP
    ):
        # Reported even when the effect survives the test above: the
        # treatment changing the probability a task can be measured at
        # all is a selection concern on its own terms.
        warnings.append(
            f"SELECTION / FPS_COVERAGE_DIFFERS_BY_ARM: first-pass success is scoreable for "
            f"{legacy_fps_coverage:.0%} of matched legacy tasks and {new_fps_coverage:.0%} of "
            f"new-pipeline tasks in {risk_class}. The treatment changes the probability a task "
            "can be measured on the very metric being compared, so the missingness is plausibly "
            "non-random with respect to the arm. This is a separate problem from identification: "
            "even where the ranges above do NOT overlap, the point estimate inside an interval "
            "stops being readable as an estimate once the instrument itself was shaped by the "
            "treatment"
        )
    disagreements = sum(group.first_pass_disagreements for group in coverage.values())
    if disagreements:
        warnings.append(
            f"FPS_DISAGREEMENT: the stored first-pass outcome disagrees with the one recomputed "
            f"from the task's own re-entry rows on {disagreements} matched task(s) in "
            f"{risk_class}; the recomputed value is the one used"
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
        if any("FPS_COVERAGE_DIFFERS_BY_ARM" in warning for warning in warnings):
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
        headline_metric=headline_metric,
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
    reason_vocabulary: frozenset[str] | None = None,
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
    # Reasons this harness knows about that no contributing source can
    # physically record. They must render as UNAVAILABLE, never as a
    # zero row -- a structurally-always-zero row reads as evidence of
    # absence rather than absence of evidence.
    unavailable = (
        tuple(sorted(KNOWN_REENTRY_REASONS - reason_vocabulary))
        if reason_vocabulary is not None
        else ()
    )
    collected_warnings = list(warnings)
    if unavailable:
        collected_warnings.append(
            "REASON_UNAVAILABLE: the telemetry store's reason column is CHECK-constrained and "
            f"cannot record {', '.join(unavailable)}; such a re-entry is rejected at write time "
            "and lands in OTHER. Those rows are shown as UNAVAILABLE, not as zero"
        )
    profile_sources = tuple(
        sorted({record.profile_source for record in all_records if record.profile_source})
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
        unavailable_reasons=unavailable,
        profile_sources=profile_sources,
        warnings=tuple(dict.fromkeys(collected_warnings)),
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


def _interval(coverage: GroupCoverage) -> str:
    """The range the arm's true rate could occupy given what was never
    scored. Printed next to the observed rate so the rate is never read
    as more certain than it is."""
    rate = coverage.first_pass_rate
    interval = stats.identification_interval(
        None if rate is None else rate / 100.0, coverage.first_pass_coverage
    )
    if interval is None:
        return "—"
    if interval[1] - interval[0] < 1e-9:
        return f"{interval[0]:.0%} (fully scored)"
    return f"{interval[0]:.0%}–{interval[1]:.0%}"


def _share(value: float | None) -> float | None:
    return None if value is None else value * 100.0


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
        f"- Reporting floor: {report.min_matched_per_group} matched, measured tasks per arm, "
        "per risk class"
    )
    lines.append(
        "- Stratification key: **joined at report time**"
        + (
            f" from {', '.join(report.profile_sources)}"
            if report.profile_sources
            else " — no per-task snapshot of the key exists"
        )
        + ". The telemetry store records no decision_budget, profile or risk class per task, so "
        "the key is recomputed rather than read, and a recomputed key can in principle be "
        "recomputed after seeing the outcome."
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
        lines.extend(_render_group(group, report.unavailable_reasons))

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
    lines.append(
        "- Warnings about coverage come in two classes and are labelled as such. "
        "**IDENTIFICATION** asks whether a claim may be made at all: it assumes the worst about "
        "which tasks went unscored, which makes it always valid and therefore weak. "
        "**SELECTION** asks whether the number inside the interval can be read as an estimate: "
        "it fires when missingness is plausibly non-random with respect to the arm. An effect "
        "can survive identification while the instrument that produced it was shaped by the "
        "treatment, so the two are neither the same fact stated twice nor interchangeable — "
        "only IDENTIFICATION moves the headline."
    )
    lines.append(
        "- Missing **outcomes** are a **partial identification** problem, not a precision one. "
        "Where first-pass success is unscoreable for some tasks, each arm's true rate is only "
        "known to lie in a range, and **more tasks will not shrink those ranges — only recording "
        "the outcomes will**. So an \"equal true rates are consistent\" verdict sitting next to a "
        "tight confidence interval is not a contradiction: the interval describes sampling noise "
        "around a quantity that is not identified in the first place, and it is the weaker "
        "claim of the two."
    )
    for note in report.notes:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _points_opposite(left: MetricComparison, right: MetricComparison) -> bool:
    """True when two paired metrics disagree about which arm is better
    -- the exact failure mode that got primary_cost_tokens demoted."""
    for pair in ((left.legacy, left.new), (right.legacy, right.new)):
        if pair[0].median is None or pair[1].median is None:
            return False
    left_delta = left.legacy.median - left.new.median
    right_delta = right.legacy.median - right.new.median
    return (left_delta > 0) != (right_delta > 0)


def _render_group(group: RiskGroupReport, unavailable_reasons: Sequence[str] = ()) -> list[str]:
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
        f"| {_percent(new_cov.first_pass_rate)} |"
    )
    lines.append(
        f"| — scoreable for | {_percent(_share(legacy_cov.first_pass_coverage))} "
        f"({legacy_cov.first_pass_known}/{legacy_cov.matched}) "
        f"| {_percent(_share(new_cov.first_pass_coverage))} "
        f"({new_cov.first_pass_known}/{new_cov.matched}) |"
    )
    lines.append(
        f"| — true rate lies in | {_interval(legacy_cov)} | {_interval(new_cov)} |"
    )
    lines.append(
        f"| Re-entries from excluded reasons only | {legacy_cov.excluded_reason_only} "
        f"| {new_cov.excluded_reason_only} |"
    )
    binary_ok = _band(min(len(group.match.legacy), len(group.match.new)))[2]
    lines.append("")
    lines.append(
        f"**Headline metric for this class: `{group.headline_metric}`.** "
        + (
            "Enough first-pass outcomes were recorded for the observed difference to be a real "
            "one."
            if group.headline_metric == "first_pass_success"
            else "Too many first-pass outcomes are unrecorded for the observed difference to be "
            "distinguishable from an artefact of which tasks were scoreable — equal true rates "
            "are consistent with the data. Worker turns are the headline instead: every matched "
            "task has one, so its denominator cannot depend on the treatment."
        )
    )
    lines.append("")
    lines.append(
        "First-pass success is measured on the **three-condition** definition (completed, "
        "verified, no counted re-entries). The contract's fourth condition — that the task "
        "raised no clarification — **cannot be evaluated at all today**: there is no "
        "clarification concept in the telemetry store, so the Question Ledger condition is "
        "absent rather than satisfied."
    )
    if legacy_cov.first_pass_disagreements or new_cov.first_pass_disagreements:
        lines.append("")
        lines.append(
            f"The stored first-pass outcome disagrees with the value recomputed from the task's "
            f"own re-entry rows on {legacy_cov.first_pass_disagreements} legacy and "
            f"{new_cov.first_pass_disagreements} new-pipeline task(s). The recomputed value is "
            "used; the stored one is written by the party being measured."
        )
    if not (group.directional_claim_allowed and binary_ok):
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
    by_key = {comparison.metric.key: comparison for comparison in group.comparisons}
    lines.append("| Metric | Legacy | New pipeline | Conservative saving |")
    lines.append("| --- | --- | --- | --- |")
    for comparison in group.comparisons:
        unit = comparison.metric.unit
        pair = by_key.get(comparison.metric.paired_with or "")
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
        legacy_cell = _summary_cell(comparison.legacy, unit)
        new_cell = _summary_cell(comparison.new, unit)
        if pair is not None:
            # Never let the paired metrics be quoted apart.
            legacy_cell += f"<br>vs {pair.metric.key}: {_number(pair.legacy.median, pair.metric.unit)}"
            new_cell += f"<br>vs {pair.metric.key}: {_number(pair.new.median, pair.metric.unit)}"
            if _points_opposite(comparison, pair):
                saving += (
                    f"<br>⚠ disagrees with {pair.metric.key}, which is the metric decisions "
                    "use"
                )
        lines.append(f"| {label} | {legacy_cell} | {new_cell} | {saving} |")
    lines.append("")
    observed = set(group.reentry_reasons[COHORT_LEGACY]) | set(group.reentry_reasons[COHORT_NEW])
    reasons = sorted(observed | set(unavailable_reasons))
    if reasons:
        lines.append("| Re-entry reason | Legacy | New pipeline | Counted |")
        lines.append("| --- | ---: | ---: | --- |")
        for reason in reasons:
            counted = "no — reported separately" if reason in EXCLUDED_REENTRY_REASONS else "yes"
            if reason in unavailable_reasons:
                lines.append(
                    f"| `{reason}` | UNAVAILABLE | UNAVAILABLE | the store cannot record this "
                    "reason; such a re-entry lands in `OTHER` |"
                )
                continue
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
