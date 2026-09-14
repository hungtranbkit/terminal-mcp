"""Statistics, matching and the report's refusal to overclaim.

Every test here exists because the corresponding mistake would produce
a plausible-looking number rather than an error -- which is the only
kind of bug that matters in a benchmark."""
from __future__ import annotations

from typing import Any

import pytest

from terminal_mcp.bench import pricing, stats
from terminal_mcp.bench.matching import DEFAULT_CONTROLS, match_cohorts
from terminal_mcp.bench.model import (
    COHORT_LEGACY,
    COHORT_NEW,
    ENVIRONMENT_FAILURE,
    Reentry,
    TaskRecord,
    TaskUsage,
    USER_CHANGED_REQUIREMENT,
)
from terminal_mcp.bench.report import (
    ASSIGNMENT_OBSERVATIONAL,
    ASSIGNMENT_RANDOMISED,
    INSUFFICIENT,
    MIN_MATCHED_PER_GROUP,
    build_report,
    render_markdown,
)


def task(
    task_id: str,
    cohort: str,
    *,
    profile: str = "STANDARD",
    complexity: str = "medium",
    project: str = "p1",
    model: str = "claude-opus-5",
    input_tokens: int | None = 1000,
    output_tokens: int | None = 500,
    cache_read: int | None = 10_000,
    cache_write_5m: int | None = 2000,
    cache_write_1h: int | None = 0,
    turns: int | None = 3,
    reentries: tuple[Reentry, ...] = (),
    first_pass: bool | None = True,
    duration: float | None = 600.0,
    retries: int | None = 0,
    terminal_status: str | None = None,
    verification_evidence: bool | None = None,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        cohort=cohort,
        project=project,
        profile=profile,
        complexity=complexity,
        model=model,
        usage=TaskUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_5m_tokens=cache_write_5m,
            cache_write_1h_tokens=cache_write_1h,
        ),
        worker_turn_count=turns,
        reentries=reentries,
        first_pass_success=first_pass,
        duration_seconds=duration,
        retries=retries,
        source="fixture",
        terminal_status=terminal_status,
        verification_evidence=verification_evidence,
    )


def cohort_pair(n: int, **overrides: Any) -> list[TaskRecord]:
    legacy_overrides = {k[7:]: v for k, v in overrides.items() if k.startswith("legacy_")}
    new_overrides = {k[4:]: v for k, v in overrides.items() if k.startswith("new_")}
    records = [task(f"L{i}", COHORT_LEGACY, **legacy_overrides) for i in range(n)]
    records += [task(f"N{i}", COHORT_NEW, **new_overrides) for i in range(n)]
    return records


# --- statistics ------------------------------------------------------------


def test_percentiles_match_the_linear_interpolation_definition() -> None:
    values = [1, 2, 3, 4]
    assert stats.percentile(values, 0.5) == 2.5
    assert stats.percentile(values, 0.25) == pytest.approx(1.75)
    assert stats.percentile(values, 0.75) == pytest.approx(3.25)
    assert stats.percentile([], 0.5) is None


def test_one_extreme_task_cannot_move_the_median() -> None:
    """Outlier handling: the estimator is robust, and the outlier is
    reported rather than deleted."""
    normal = [100.0] * 20
    summary = stats.summarize(normal + [1_000_000.0])
    assert summary.median == 100.0
    assert summary.outliers == 1
    assert summary.maximum == 1_000_000.0


def test_outliers_are_not_flagged_below_four_points() -> None:
    assert stats.summarize([1.0, 2.0, 900.0]).outliers == 0


def test_bootstrap_is_deterministic() -> None:
    baseline = [float(v) for v in range(10, 40)]
    candidate = [float(v) for v in range(5, 35)]
    first = stats.savings(baseline, candidate)
    second = stats.savings(baseline, candidate)
    assert first.as_dict() == second.as_dict()


def test_savings_reports_the_lesser_of_point_and_lower_bound() -> None:
    baseline = [100.0] * 30
    candidate = [70.0] * 30
    estimate = stats.savings(baseline, candidate)
    assert estimate.point_percent == pytest.approx(30.0)
    assert estimate.reported_percent <= estimate.point_percent
    assert estimate.significant is True


def test_overlapping_distributions_report_no_saving() -> None:
    values = [float(v) for v in range(1, 41)]
    estimate = stats.savings(values, list(reversed(values)))
    assert estimate.significant is False
    assert estimate.reported_percent == 0.0


def test_rate_of_nothing_is_missing_not_zero() -> None:
    assert stats.rate(0, 0) is None
    assert stats.rate(0, 4) == 0.0


# --- pricing ---------------------------------------------------------------


def test_cost_units_weight_each_token_by_its_real_billing_ratio() -> None:
    units = pricing.cost_units(
        model="claude-opus-5",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=1000,
        cache_write_5m_tokens=400,
        cache_write_1h_tokens=100,
    )
    # 100 + 400*1.25 + 100*2 + 1000*0.1 + 200*5
    assert units == pytest.approx(100 + 500 + 200 + 100 + 1000)


def test_output_is_five_times_input_across_the_lineup() -> None:
    for model_id in ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"):
        assert pricing.PRICE_TABLE[model_id].output_multiplier == pytest.approx(5.0)


def test_fable_reads_cheaper_than_the_usual_tenth() -> None:
    assert pricing.PRICE_TABLE["claude-fable-5-1"].cache_read_multiplier == 0.025


def test_unknown_model_is_unpriceable_not_free() -> None:
    assert pricing.lookup("some-other-vendor-model") is None
    assert (
        pricing.cost_units(
            model="some-other-vendor-model",
            input_tokens=1,
            output_tokens=1,
            cache_read_tokens=1,
            cache_write_5m_tokens=1,
            cache_write_1h_tokens=1,
        )
        is None
    )


def test_dated_and_prefixed_model_ids_price_as_their_base() -> None:
    assert pricing.normalise_model_id("claude-opus-5-20260401") == "claude-opus-5"
    assert pricing.normalise_model_id("anthropic.claude-sonnet-5") == "claude-sonnet-5"
    assert pricing.normalise_model_id("claude-opus-4-5@20251101") is None


def test_collapsed_cache_write_is_unpriceable_unless_a_ttl_is_assumed() -> None:
    from terminal_mcp.bench.model import TTL_ASSUME_1H, TTL_ASSUME_5M

    usage = TaskUsage(
        input_tokens=100,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_total_tokens=1000,
    )
    assert usage.cost_units("claude-opus-5") is None
    assert usage.cost_units("claude-opus-5", ttl_policy=TTL_ASSUME_5M) == pytest.approx(1350.0)
    assert usage.cost_units("claude-opus-5", ttl_policy=TTL_ASSUME_1H) == pytest.approx(2100.0)


def test_input_tokens_are_not_the_prompt() -> None:
    usage = TaskUsage(
        input_tokens=100,
        output_tokens=0,
        cache_read_tokens=50_000,
        cache_write_5m_tokens=2000,
        cache_write_1h_tokens=0,
    )
    assert usage.total_prompt_tokens == 52_100
    assert usage.input_tokens == 100


# --- matching --------------------------------------------------------------


def test_strata_are_balanced_so_the_workload_mix_is_identical() -> None:
    records = [task(f"L{i}", COHORT_LEGACY, complexity="small") for i in range(5)]
    records += [task(f"L{i}b", COHORT_LEGACY, complexity="large") for i in range(5)]
    records += [task(f"N{i}", COHORT_NEW, complexity="small") for i in range(2)]
    result = match_cohorts(records)
    assert len(result.legacy) == len(result.new) == 2
    assert result.complexity_mix["legacy"] == result.complexity_mix["new_pipeline"] == {"small": 2}
    assert result.dropped["unmatched_stratum"] == 5
    assert result.dropped["surplus_in_stratum"] == 3


def test_unknown_control_values_form_their_own_stratum() -> None:
    records = [
        task("L1", COHORT_LEGACY, complexity=None),
        task("N1", COHORT_NEW, complexity="medium"),
    ]
    result = match_cohorts(records)
    assert result.matched_pairs == 0, "unknown complexity is never pooled with a known one"


def test_unknown_cohort_tasks_are_counted_and_excluded() -> None:
    records = cohort_pair(3) + [task("U1", "unknown")]
    result = match_cohorts(records)
    assert result.dropped["unknown_cohort"] == 1


def test_mixed_complexity_warning_when_complexity_is_mostly_unrecorded() -> None:
    """Seeded fixture: complexity is missing on most tasks, so the
    control could not actually be enforced and the report must say so
    rather than present a clean-looking comparison."""
    records = [task(f"L{i}", COHORT_LEGACY, complexity=None) for i in range(12)]
    records += [task(f"N{i}", COHORT_NEW, complexity=None) for i in range(12)]
    result = match_cohorts(records)
    assert result.matched_pairs == 12
    assert any("MIXED_COMPLEXITY" in warning for warning in result.warnings)


def test_mixed_complexity_warning_when_the_control_is_switched_off() -> None:
    records = [task(f"L{i}", COHORT_LEGACY, complexity="small") for i in range(12)]
    records += [task(f"N{i}", COHORT_NEW, complexity="large") for i in range(12)]
    result = match_cohorts(records, controls=("profile", "project"))
    assert result.matched_pairs == 12
    assert any("total-variation distance" in warning for warning in result.warnings)


def test_unknown_control_dimension_is_rejected() -> None:
    with pytest.raises(ValueError):
        match_cohorts([], controls=("not_a_control",))


def test_matching_is_deterministic() -> None:
    records = cohort_pair(8)
    first = [r.task_id for r in match_cohorts(records).legacy]
    second = [r.task_id for r in match_cohorts(list(reversed(records))).legacy]
    assert first == second


# --- re-entry accounting ---------------------------------------------------


def test_excluded_reasons_are_separated_and_the_task_is_flagged_not_dropped() -> None:
    record = task(
        "L1",
        COHORT_LEGACY,
        first_pass=False,
        reentries=(
            Reentry(reason=USER_CHANGED_REQUIREMENT),
            Reentry(reason=ENVIRONMENT_FAILURE),
        ),
    )
    assert record.counted_reentry_count == 0
    assert len(record.excluded_reentries) == 2
    assert record.first_pass_success_adjusted is True
    assert record.has_excluded_reason_only is True


def test_a_reentry_with_no_reason_still_counts() -> None:
    record = task("L1", COHORT_LEGACY, first_pass=False, reentries=(Reentry(reason=None),))
    assert record.counted_reentry_count == 1
    assert record.first_pass_success_adjusted is False
    assert record.reentry_counts_by_reason() == {"UNSPECIFIED": 1}


def test_stale_context_is_counted_as_real_rework() -> None:
    from terminal_mcp.bench.model import STALE_CONTEXT

    record = task("L1", COHORT_LEGACY, first_pass=False, reentries=(Reentry(reason=STALE_CONTEXT),))
    assert record.counted_reentry_count == 1
    assert record.first_pass_success_adjusted is False


def test_unknown_first_pass_stays_unknown() -> None:
    assert task("L1", COHORT_LEGACY, first_pass=None).first_pass_success_adjusted is None


# --- report ----------------------------------------------------------------


def test_empty_input_is_insufficient_not_a_crash() -> None:
    report = build_report([])
    assert report.verdict == INSUFFICIENT
    assert report.groups == ()
    assert "INSUFFICIENT_DATA" in render_markdown(report)


def test_below_the_floor_no_savings_figure_is_emitted_at_all() -> None:
    report = build_report(cohort_pair(MIN_MATCHED_PER_GROUP - 1), assignment=ASSIGNMENT_RANDOMISED)
    assert report.verdict == INSUFFICIENT
    group = report.groups[0]
    assert group.confidence == INSUFFICIENT
    assert all(not comparison.reported for comparison in group.comparisons)
    assert all(comparison.savings.reported_percent is None for comparison in group.comparisons)


def test_the_harness_becomes_useful_automatically_at_the_threshold() -> None:
    """Nothing has to be re-enabled: crossing the floor with a real
    effect turns the same call into a stated comparison."""
    records = cohort_pair(
        60,
        legacy_turns=6,
        new_turns=3,
        legacy_input_tokens=4000,
        new_input_tokens=1500,
    )
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    assert report.verdict == "COMPARISON_AVAILABLE"
    group = report.groups[0]
    assert group.confidence == "MODERATE"
    turns = next(c for c in group.comparisons if c.metric.key == "worker_turn_count")
    assert turns.reported is True
    assert turns.savings.reported_percent == pytest.approx(50.0, abs=1.0)
    assert group.cost_ratio is not None and group.cost_ratio < 1.0


def test_without_randomisation_everything_stays_descriptive() -> None:
    records = cohort_pair(60, legacy_turns=6, new_turns=3)
    report = build_report(records, assignment=ASSIGNMENT_OBSERVATIONAL)
    group = report.groups[0]
    assert group.directional_claim_allowed is False
    assert all(not comparison.reported for comparison in group.comparisons)
    assert any("NOT_RANDOMISED" in warning for warning in group.warnings)


def test_a_binary_outcome_needs_a_much_larger_sample_than_a_count() -> None:
    report = build_report(cohort_pair(45), assignment=ASSIGNMENT_RANDOMISED)
    text = render_markdown(report)
    assert "First-pass success above is **descriptive only**" in text


def test_risk_classes_are_never_pooled() -> None:
    records = cohort_pair(12)
    records += [task(f"HL{i}", COHORT_LEGACY, profile="HIGH_RISK") for i in range(12)]
    records += [task(f"HN{i}", COHORT_NEW, profile="HIGH_RISK") for i in range(12)]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    classes = {group.risk_class for group in report.groups}
    assert classes == {"STANDARD", "HIGH_RISK"}
    for group in report.groups:
        assert len(group.match.legacy) == 12
    assert "There is no overall number on purpose" in render_markdown(report)


def test_an_unrecognised_profile_gets_its_own_class_not_standard() -> None:
    report = build_report(cohort_pair(12, legacy_profile="weird", new_profile="weird"))
    assert [group.risk_class for group in report.groups] == ["UNKNOWN_RISK"]


def test_missing_telemetry_shrinks_n_rather_than_dragging_the_median_down() -> None:
    """The seeded missing-telemetry fixture. Half the new-pipeline arm
    has no token data at all. If missing were treated as zero, the new
    arm's median cost would collapse and the harness would report a
    ~50% saving that does not exist."""
    records = [task(f"L{i}", COHORT_LEGACY) for i in range(20)]
    records += [task(f"N{i}", COHORT_NEW) for i in range(10)]
    records += [
        task(
            f"NM{i}",
            COHORT_NEW,
            input_tokens=None,
            output_tokens=None,
            cache_read=None,
            cache_write_5m=None,
            cache_write_1h=None,
        )
        for i in range(10)
    ]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    group = report.groups[0]
    cost = next(c for c in group.comparisons if c.metric.key == "cost_units")
    assert cost.legacy.n == 20
    assert cost.new.n == 10, "tasks with no telemetry are absent from the metric, not zero in it"
    assert cost.legacy.median == pytest.approx(cost.new.median)
    assert group.coverage["new_pipeline"].with_complete_usage == 10
    assert any("THIN_TELEMETRY" in warning for warning in group.warnings)


def test_thin_telemetry_demotes_the_confidence_band() -> None:
    records = [task(f"L{i}", COHORT_LEGACY) for i in range(50)]
    records += [task(f"N{i}", COHORT_NEW) for i in range(25)]
    records += [
        task(f"NM{i}", COHORT_NEW, input_tokens=None, output_tokens=None, cache_read=None,
             cache_write_5m=None, cache_write_1h=None)
        for i in range(25)
    ]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    # 50 matched per arm is the MODERATE band; half the new arm having
    # no telemetry knocks it down one.
    assert report.groups[0].confidence == "LOW"
    clean = build_report(cohort_pair(50), assignment=ASSIGNMENT_RANDOMISED)
    assert clean.groups[0].confidence == "MODERATE"


def test_an_invalid_assignment_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_report([], assignment="whatever")


def test_the_report_round_trips_through_json() -> None:
    import json

    report = build_report(cohort_pair(12), assignment=ASSIGNMENT_RANDOMISED)
    payload = json.loads(json.dumps(report.as_dict()))
    assert payload["price_table_version"] == pricing.PRICE_TABLE_VERSION
    assert payload["risk_groups"][0]["risk_class"] == "STANDARD"


# --- cross-lane conformance: gaps found by the critic lane against the
# --- committed telemetry schema (EM-C1..C5)


def test_a_reason_the_store_cannot_record_renders_unavailable_not_zero() -> None:
    """EM-C1. `telemetry_reentries.reason` is CHECK-constrained and does
    not admit STALE_CONTEXT, so such a re-entry is rejected at write
    time and lands in OTHER. A zero row for it would read as evidence of
    absence rather than absence of evidence."""
    from terminal_mcp.bench.work_telemetry import STORE_REENTRY_REASONS

    report = build_report(cohort_pair(12), reason_vocabulary=STORE_REENTRY_REASONS)
    assert "STALE_CONTEXT" in report.unavailable_reasons
    text = render_markdown(report)
    assert "| `STALE_CONTEXT` | UNAVAILABLE | UNAVAILABLE" in text
    assert any("REASON_UNAVAILABLE" in warning for warning in report.warnings)


def test_a_recordable_reason_is_not_marked_unavailable() -> None:
    from terminal_mcp.bench.work_telemetry import STORE_REENTRY_REASONS

    report = build_report(cohort_pair(12), reason_vocabulary=STORE_REENTRY_REASONS)
    assert "CONTRACT_GAP" not in report.unavailable_reasons
    assert "ENVIRONMENT_FAILURE" not in report.unavailable_reasons


def test_without_a_declared_vocabulary_nothing_is_marked_unavailable() -> None:
    report = build_report(cohort_pair(12))
    assert report.unavailable_reasons == ()


def test_the_three_condition_fps_definition_is_stated(  ) -> None:
    """EM-C2. There is no clarification concept anywhere in the store,
    so the contract's fourth condition cannot be evaluated. The report
    must say the condition is absent rather than let the definition
    drift silently."""
    text = render_markdown(build_report(cohort_pair(12)))
    assert "three-condition" in text
    assert "cannot be evaluated at all today" in text


def test_first_pass_success_is_recomputed_not_read() -> None:
    """EM-C3. The stored tri-state is written by the party being
    measured. Recomputing it from that task's own re-entry rows and
    flagging disagreement is the anti-gaming fix."""
    honest = task("t1", COHORT_LEGACY, first_pass=True, terminal_status="COMPLETED")
    assert honest.first_pass_success_recomputed is True
    assert honest.first_pass_success_disagrees is False

    flattering = TaskRecord(
        task_id="t2",
        cohort=COHORT_LEGACY,
        first_pass_success=True,           # what the reporter claimed
        terminal_status="COMPLETED",
        reentries=(Reentry(reason="CONTRACT_GAP"),),  # what actually happened
    )
    assert flattering.first_pass_success_recomputed is False
    assert flattering.first_pass_success_disagrees is True
    assert flattering.first_pass_success_adjusted is False, "the recomputed value wins"


def test_cancelled_tasks_leave_the_denominator() -> None:
    record = TaskRecord(task_id="t1", cohort=COHORT_LEGACY, terminal_status="CANCELLED")
    assert record.first_pass_success_recomputed is None
    assert record.first_pass_success_adjusted is None


def test_unverified_tasks_are_unscoreable_even_with_a_terminal_status() -> None:
    record = TaskRecord(
        task_id="t1",
        cohort=COHORT_LEGACY,
        terminal_status="COMPLETED",
        verification_evidence=False,
    )
    assert record.first_pass_success_recomputed is None


def test_disagreement_is_counted_and_warned_per_group() -> None:
    records = [
        TaskRecord(
            task_id=f"L{i}",
            cohort=COHORT_LEGACY,
            profile="STANDARD",
            complexity="medium",
            project="p1",
            first_pass_success=True,
            terminal_status="COMPLETED",
            reentries=(Reentry(reason="CONTRACT_GAP"),),
            worker_turn_count=4,
        )
        for i in range(12)
    ]
    records += [task(f"N{i}", COHORT_NEW, terminal_status="COMPLETED") for i in range(12)]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    group = report.groups[0]
    assert group.coverage[COHORT_LEGACY].first_pass_disagreements == 12
    assert any("FPS_DISAGREEMENT" in warning for warning in group.warnings)
    assert "disagrees with the value recomputed" in render_markdown(report)


def test_the_stratification_key_join_is_named_in_the_header() -> None:
    """EM-C4. The key is recomputed at report time, and a recomputed key
    can be recomputed after seeing the outcome. Say where it came from."""
    records = [
        TaskRecord(task_id=f"L{i}", cohort=COHORT_LEGACY, profile="STANDARD",
                   profile_source="queue_tasks.analysis")
        for i in range(12)
    ]
    text = render_markdown(build_report(records))
    assert "joined at report time" in text
    assert "queue_tasks.analysis" in text
    assert "no per-task snapshot of the key exists" in render_markdown(build_report(cohort_pair(4)))


def test_fps_stops_being_the_headline_when_its_denominator_depends_on_the_arm() -> None:
    """EM-C5, the structural one. The HIGH decision-budget arm is
    required by its own contract to carry a verification plan, so it is
    systematically more likely to record the evidence that makes a task
    scoreable at all — which inflates its apparent first-pass success by
    selecting well-run control tasks out of the denominator."""
    records = [task(f"L{i}", COHORT_LEGACY, first_pass=None) for i in range(20)]
    records += [task(f"L{i}s", COHORT_LEGACY, first_pass=True) for i in range(5)]
    records += [task(f"N{i}", COHORT_NEW, first_pass=True) for i in range(25)]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    group = report.groups[0]
    assert group.headline_metric == "worker_turn_count"
    assert any("FPS_COVERAGE_DIFFERS_BY_ARM" in warning for warning in group.warnings)
    text = render_markdown(report)
    assert "Headline metric for this class: `worker_turn_count`" in text
    assert "scoreable for" in text


def test_fps_stays_the_headline_when_coverage_matches() -> None:
    report = build_report(cohort_pair(25), assignment=ASSIGNMENT_RANDOMISED)
    assert report.groups[0].headline_metric == "first_pass_success"


def test_fps_rate_is_never_rendered_without_its_coverage() -> None:
    text = render_markdown(build_report(cohort_pair(12)))
    assert "| — scoreable for |" in text


def test_primary_cost_tokens_never_renders_without_cost_units_beside_it() -> None:
    """The critic lane's one condition for keeping the brief's metric."""
    text = render_markdown(build_report(cohort_pair(12), assignment=ASSIGNMENT_RANDOMISED))
    row = next(line for line in text.splitlines() if "primary_cost_tokens (input" in line)
    assert "vs cost_units:" in row


def test_a_divergence_between_the_paired_metrics_is_called_out() -> None:
    """The demo case: the new arm shifts spend into output, so
    primary_cost_tokens shows a saving while real cost rises."""
    records = [
        task(f"L{i}", COHORT_LEGACY, input_tokens=6000, output_tokens=8000, cache_write_5m=4000)
        for i in range(20)
    ]
    records += [
        task(f"N{i}", COHORT_NEW, input_tokens=3000, output_tokens=20000, cache_write_5m=3000)
        for i in range(20)
    ]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    group = report.groups[0]
    primary = next(c for c in group.comparisons if c.metric.key == "primary_cost_tokens")
    cost = next(c for c in group.comparisons if c.metric.key == "cost_units")
    assert primary.legacy.median > primary.new.median, "brief metric says the new arm is cheaper"
    assert cost.legacy.median < cost.new.median, "real cost says it is more expensive"
    assert "disagrees with cost_units" in render_markdown(report)


def test_stale_context_is_reported_both_in_and_out_of_the_comparison() -> None:
    from terminal_mcp.bench.model import STALE_CONTEXT

    records = [
        task(f"L{i}", COHORT_LEGACY, first_pass=False, reentries=(Reentry(reason="CONTRACT_GAP"),))
        for i in range(12)
    ]
    records += [
        task(f"N{i}", COHORT_NEW, first_pass=False, reentries=(Reentry(reason=STALE_CONTEXT),))
        for i in range(12)
    ]
    report = build_report(records, assignment=ASSIGNMENT_RANDOMISED)
    group = report.groups[0]
    with_stale = next(c for c in group.comparisons if c.metric.key == "reentries")
    without_stale = next(c for c in group.comparisons if c.metric.key == "reentries_excl_stale")
    assert with_stale.new.median == 1.0, "staleness is a real downstream cost of the treatment"
    assert without_stale.new.median == 0.0
    assert with_stale.legacy.median == without_stale.legacy.median == 1.0
