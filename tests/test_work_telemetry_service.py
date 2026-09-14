"""Work Efficiency Telemetry -- service/query tests (TASK A).

The service's whole job is to answer "did the Analysis Gate reduce the
cost per delivered task?" without ever letting a missing measurement
masquerade as a cheap one. Most of what follows tests that second half.
"""
from __future__ import annotations

import pytest

from terminal_mcp.work_telemetry_service import WorkTelemetryService
from terminal_mcp.work_telemetry_store import (
    CONFIDENCE_ESTIMATED, CONFIDENCE_MEASURED, KIND_CUMULATIVE, PHASE_ANALYSIS, PHASE_CONTRACT,
    PHASE_DELIVERY, PHASE_IMPLEMENTATION, PHASE_UNKNOWN, PHASE_VERIFICATION,
    REENTRY_CONTRACT_GAP, REENTRY_TEST_FAILURE, STATUS_CANCELLED, STATUS_COMPLETED, STATUS_FAILED,
    WorkTelemetryStore,
)


@pytest.fixture
def store(tmp_path) -> WorkTelemetryStore:
    return WorkTelemetryStore(tmp_path / "work_telemetry.db")


@pytest.fixture
def service(store) -> WorkTelemetryService:
    return WorkTelemetryService(store)


def _full_task(store, task_id, *, work_id="outcome-1", project_id="git:acme/widget",
               analysis=(300, 40), contract=(200, 60), worker=(4000, 900), turns=6):
    store.start_task(task_id, work_id=work_id, project_id=project_id, session_id="window2",
                     phase=PHASE_ANALYSIS)
    store.record_usage(task_id, phase=PHASE_ANALYSIS, idempotency_key=f"{task_id}-a",
                       input_tokens=analysis[0], output_tokens=analysis[1],
                       confidence=CONFIDENCE_MEASURED, evidence_source="claude_usage_json")
    store.record_usage(task_id, phase=PHASE_CONTRACT, idempotency_key=f"{task_id}-c",
                       input_tokens=contract[0], output_tokens=contract[1],
                       confidence=CONFIDENCE_MEASURED, evidence_source="claude_usage_json")
    store.record_usage(task_id, phase=PHASE_IMPLEMENTATION, idempotency_key=f"{task_id}-i",
                       input_tokens=worker[0], output_tokens=worker[1], turn_count=turns,
                       confidence=CONFIDENCE_MEASURED, evidence_source="claude_usage_json")


class TestTaskSummary:
    def test_an_unknown_task_is_none_not_a_zero_filled_row(self, service):
        assert service.task_summary("task-from-before-telemetry-existed") is None

    def test_every_required_field_is_present(self, store, service):
        _full_task(store, "task-1")
        store.complete_task("task-1")
        summary = service.task_summary("task-1")
        for field in ("work_id", "task_id", "session_id", "phase", "analysis_tokens",
                      "contract_tokens", "worker_input_tokens", "worker_output_tokens",
                      "cache_read_tokens", "cache_write_tokens", "worker_turn_count",
                      "reentry_count", "reentry_reasons", "first_pass_success",
                      "contract_gap_count", "started_at", "completed_at", "evidence_source",
                      "confidence"):
            assert field in summary, field

    def test_phase_attribution(self, store, service):
        _full_task(store, "task-1")
        summary = service.task_summary("task-1")
        assert summary["analysis_tokens"] == 340       # 300 + 40
        assert summary["contract_tokens"] == 260       # 200 + 60
        assert summary["worker_input_tokens"] == 4000
        assert summary["worker_output_tokens"] == 900
        assert summary["worker_turn_count"] == 6
        assert summary["total_tokens"] == 340 + 260 + 4900

    def test_worker_phases_span_implementation_verification_and_delivery(self, store, service):
        for phase in (PHASE_IMPLEMENTATION, PHASE_VERIFICATION, PHASE_DELIVERY):
            store.record_usage("task-1", phase=phase, idempotency_key=phase,
                               input_tokens=100, turn_count=1)
        summary = service.task_summary("task-1")
        assert summary["worker_input_tokens"] == 300
        assert summary["worker_turn_count"] == 3

    def test_unattributed_usage_is_never_charged_to_the_worker(self, store, service):
        """Folding PHASE_UNKNOWN into worker_* would inflate exactly the
        number the Analysis Gate is supposed to reduce -- i.e. it would
        bias this experiment's own result."""
        store.record_usage("task-1", phase=PHASE_UNKNOWN, input_tokens=5000)
        summary = service.task_summary("task-1")
        assert summary["unattributed_tokens"] == 5000
        assert summary["worker_input_tokens"] is None
        assert summary["total_tokens"] == 5000

    def test_a_task_with_no_usage_reports_null_tokens_not_zero(self, store, service):
        store.start_task("task-1")
        summary = service.task_summary("task-1")
        assert summary["has_token_telemetry"] is False
        for field in ("analysis_tokens", "contract_tokens", "worker_input_tokens",
                      "worker_output_tokens", "cache_read_tokens", "cache_write_tokens",
                      "worker_turn_count", "total_tokens", "duration_seconds"):
            assert summary[field] is None, field
        assert summary["first_pass_success"] == "UNKNOWN"

    def test_cache_tokens_are_reported_when_measured(self, store, service):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=10,
                           cache_read_tokens=8000, cache_write_tokens=1200)
        summary = service.task_summary("task-1")
        assert summary["cache_read_tokens"] == 8000
        assert summary["cache_write_tokens"] == 1200

    def test_reentry_reasons_are_listed(self, store, service):
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="r1")
        store.record_reentry("task-1", reason=REENTRY_CONTRACT_GAP, idempotency_key="r2")
        summary = service.task_summary("task-1")
        assert summary["reentry_count"] == 2
        assert sorted(summary["reentry_reasons"]) == ["CONTRACT_GAP", "TEST_FAILURE"]
        assert summary["contract_gap_count"] == 1

    def test_confidence_is_the_weakest_of_the_samples_behind_the_numbers(self, store, service):
        store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="a",
                           input_tokens=10, confidence=CONFIDENCE_MEASURED)
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="b",
                           input_tokens=10, confidence=CONFIDENCE_ESTIMATED)
        assert service.task_summary("task-1")["confidence"] == CONFIDENCE_ESTIMATED

    def test_evidence_sources_are_split_not_reported_as_one_joined_string(self, store, service):
        store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="a",
                           input_tokens=1, evidence_source="claude_usage_json")
        store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="b",
                           input_tokens=1, evidence_source="operator")
        assert service.task_summary("task-1")["evidence_sources"] == ["claude_usage_json", "operator"]

    def test_a_counter_reset_is_surfaced_to_the_reader(self, store, service):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="s", idempotency_key="1", input_tokens=5000)
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="s", idempotency_key="2", input_tokens=100)
        assert service.task_summary("task-1")["counter_reset_observed"] is True

    def test_duration_needs_both_ends(self, store, service):
        store.start_task("task-1", started_at="2026-09-14T08:00:00+00:00")
        assert service.task_summary("task-1")["duration_seconds"] is None
        store.complete_task("task-1", completed_at="2026-09-14T08:30:00+00:00")
        assert service.task_summary("task-1")["duration_seconds"] == 1800.0


class TestTokensPerCompletedTask:
    def test_no_tasks_reports_none_rather_than_zero(self, service):
        result = service.tokens_per_completed_task(project_id="git:acme/widget")
        assert result["completed_tasks"] == 0
        assert result["total_tokens"] is None
        assert result["tokens_per_completed_task"] is None
        assert result["telemetry_coverage"] is None
        assert result["first_pass_success_rate"] is None

    def test_the_mean_is_over_measured_tasks_only(self, store, service):
        """The failure this guards against: a cohort where half the tasks
        have no telemetry would otherwise show a per-task cost that falls
        as COVERAGE falls -- an efficiency 'win' produced entirely by
        losing data."""
        _full_task(store, "task-1", worker=(4000, 900))
        _full_task(store, "task-2", worker=(4000, 900))
        store.start_task("task-3", project_id="git:acme/widget")  # completed, never measured
        for task_id in ("task-1", "task-2", "task-3"):
            store.complete_task(task_id)

        result = service.tokens_per_completed_task(project_id="git:acme/widget")
        assert result["completed_tasks"] == 3
        assert result["measured_tasks"] == 2
        assert result["telemetry_coverage"] == round(2 / 3, 4)
        assert result["total_tokens"] == 2 * (340 + 260 + 4900)
        assert result["tokens_per_completed_task"] == 5500.0

    def test_open_tasks_are_not_counted(self, store, service):
        _full_task(store, "task-1")
        _full_task(store, "task-2")
        store.complete_task("task-1")
        result = service.tokens_per_completed_task(project_id="git:acme/widget")
        assert result["completed_tasks"] == 1
        assert result["tokens_per_completed_task"] == 5500.0

    def test_failed_and_cancelled_tasks_are_not_completed_tasks(self, store, service):
        _full_task(store, "task-1")
        _full_task(store, "task-2")
        _full_task(store, "task-3")
        store.complete_task("task-1", status=STATUS_COMPLETED)
        store.complete_task("task-2", status=STATUS_FAILED)
        store.complete_task("task-3", status=STATUS_CANCELLED)
        assert service.tokens_per_completed_task(project_id="git:acme/widget")["completed_tasks"] == 1

    def test_phase_breakdown_per_task(self, store, service):
        _full_task(store, "task-1")
        _full_task(store, "task-2")
        store.complete_task("task-1")
        store.complete_task("task-2")
        result = service.tokens_per_completed_task(project_id="git:acme/widget")
        assert result["analysis_tokens_per_task"] == 340.0
        assert result["contract_tokens_per_task"] == 260.0
        assert result["worker_input_tokens_per_task"] == 4000.0
        assert result["worker_output_tokens_per_task"] == 900.0
        assert result["worker_turns_per_task"] == 6.0

    def test_an_unmeasured_phase_stays_none_in_the_aggregate(self, store, service):
        store.start_task("task-1", project_id="p")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=100)
        store.complete_task("task-1")
        result = service.tokens_per_completed_task(project_id="p")
        assert result["worker_input_tokens_per_task"] == 100.0
        assert result["analysis_tokens_per_task"] is None
        assert result["contract_tokens_per_task"] is None

    def test_first_pass_rate_ignores_tasks_whose_tristate_never_resolved(self, store, service):
        for task_id in ("task-1", "task-2"):
            store.start_task(task_id, project_id="p")
            store.complete_task(task_id)
        store.start_task("task-3", project_id="p")
        store.record_reentry("task-3", reason=REENTRY_TEST_FAILURE, idempotency_key="r")
        store.complete_task("task-3")
        result = service.tokens_per_completed_task(project_id="p")
        assert result["resolved_first_pass_tasks"] == 3
        assert result["unresolved_first_pass_tasks"] == 0
        assert result["first_pass_success_rate"] == round(2 / 3, 4)

    def test_reentry_reasons_are_tallied(self, store, service):
        store.start_task("task-1", project_id="p")
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="r1")
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="r2")
        store.record_reentry("task-1", reason=REENTRY_CONTRACT_GAP, idempotency_key="r3")
        store.complete_task("task-1")
        result = service.tokens_per_completed_task(project_id="p")
        assert result["reentry_reasons"] == {"TEST_FAILURE": 2, "CONTRACT_GAP": 1}
        assert result["reentries_per_task"] == 3.0
        assert result["contract_gaps_per_task"] == 1.0

    def test_scope_filters_do_not_leak_across_projects(self, store, service):
        _full_task(store, "task-1", project_id="project-a")
        _full_task(store, "task-2", project_id="project-b")
        store.complete_task("task-1")
        store.complete_task("task-2")
        assert service.tokens_per_completed_task(project_id="project-a")["completed_tasks"] == 1

    def test_since_filters_by_completion(self, store, service):
        store.start_task("task-old", project_id="p")
        store.complete_task("task-old", completed_at="2026-01-01T00:00:00+00:00")
        store.start_task("task-new", project_id="p")
        store.complete_task("task-new", completed_at="2026-09-14T00:00:00+00:00")
        result = service.tokens_per_completed_task(project_id="p", since="2026-06-01T00:00:00+00:00")
        assert result["completed_tasks"] == 1

    def test_duplicate_reports_do_not_inflate_the_metric(self, store, service):
        """End-to-end version of the idempotency guarantee: the number a
        reader sees is unchanged by a producer that retried everything."""
        _full_task(store, "task-1")
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="r1")
        store.complete_task("task-1")
        before = service.tokens_per_completed_task(project_id="git:acme/widget")

        _full_task(store, "task-1")  # same idempotency keys -- a full replay
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="r1")
        store.complete_task("task-1")
        assert service.tokens_per_completed_task(project_id="git:acme/widget") == before


class TestWorkSummary:
    def test_it_rolls_up_the_tasks_of_one_work_item(self, store, service):
        _full_task(store, "task-1", work_id="outcome-1")
        _full_task(store, "task-2", work_id="outcome-1")
        _full_task(store, "task-3", work_id="outcome-2")
        store.complete_task("task-1")
        summary = service.work_summary("outcome-1")
        assert summary["task_count"] == 2
        assert summary["open_tasks"] == 1
        assert summary["first_pass_unknown_tasks"] == 1
        assert summary["aggregate"]["completed_tasks"] == 1

    def test_an_unknown_work_item_is_empty_not_an_error(self, service):
        summary = service.work_summary("outcome-never-seen")
        assert summary["task_count"] == 0
        assert summary["aggregate"]["tokens_per_completed_task"] is None


class TestModelAttributionAndCacheTtl:
    """What a cost model needs from a summary (migration v2)."""

    def test_the_ttl_split_is_never_added_into_a_total(self, store, service):
        """cache_write_5m/1h are a breakdown OF cache_write_tokens. If
        they were summed as separate kinds, every cache write would be
        counted three times."""
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=100,
                           cache_write_5m_tokens=800, cache_write_1h_tokens=200)
        summary = service.task_summary("task-1")
        assert summary["cache_write_tokens"] == 1000
        assert summary["cache_write_5m_tokens"] == 800
        assert summary["cache_write_1h_tokens"] == 200
        assert summary["total_tokens"] == 1100  # 100 input + 1000 cache write, not 3100

    def test_a_task_spanning_models_reports_each_one_separately(self, store, service):
        store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="a",
                           input_tokens=300, output_tokens=40, model_id="claude-opus-5")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="b",
                           input_tokens=4000, output_tokens=900, model_id="claude-sonnet-5")
        summary = service.task_summary("task-1")
        assert summary["model_ids"] == ["claude-opus-5", "claude-sonnet-5"]
        assert summary["model_totals"]["claude-opus-5"]["input_tokens"] == 300
        assert summary["model_totals"]["claude-sonnet-5"]["output_tokens"] == 900
        assert summary["has_model_attribution"] is True

    def test_unattributed_usage_makes_the_summary_say_so(self, store, service):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=10)
        summary = service.task_summary("task-1")
        assert summary["model_ids"] == ["UNKNOWN"]
        assert summary["has_model_attribution"] is False

    def test_a_task_with_no_usage_has_no_models_and_claims_no_attribution(self, store, service):
        store.start_task("task-1")
        summary = service.task_summary("task-1")
        assert summary["model_ids"] == []
        assert summary["has_model_attribution"] is False

    def test_the_aggregate_splits_tokens_by_model(self, store, service):
        for task_id in ("task-1", "task-2"):
            store.start_task(task_id, project_id="p")
            store.record_usage(task_id, phase=PHASE_ANALYSIS, idempotency_key=f"{task_id}-a",
                               input_tokens=300, model_id="claude-opus-5")
            store.record_usage(task_id, phase=PHASE_IMPLEMENTATION, idempotency_key=f"{task_id}-i",
                               input_tokens=4000, output_tokens=900, model_id="claude-sonnet-5")
            store.complete_task(task_id)
        result = service.tokens_per_completed_task(project_id="p")
        assert result["model_ids"] == ["claude-opus-5", "claude-sonnet-5"]
        assert result["tokens_by_model"]["claude-opus-5"]["input_tokens"] == 600
        assert result["tokens_by_model"]["claude-sonnet-5"]["output_tokens"] == 1800
        assert result["tokens_by_model"]["claude-sonnet-5"]["task_count"] == 2
        assert result["unattributed_model_tasks"] == 0

    def test_the_aggregate_counts_tasks_whose_model_is_unknown(self, store, service):
        store.start_task("task-1", project_id="p")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=10)
        store.complete_task("task-1")
        result = service.tokens_per_completed_task(project_id="p")
        assert result["unattributed_model_tasks"] == 1

    def test_per_model_totals_sum_to_the_scope_total(self, store, service):
        """A cost model prices per model and then adds up; that sum must
        not silently disagree with the headline token total."""
        store.start_task("task-1", project_id="p")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="a",
                           input_tokens=100, output_tokens=20, model_id="claude-opus-5")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="b",
                           input_tokens=7, model_id="claude-sonnet-5")
        store.complete_task("task-1")
        result = service.tokens_per_completed_task(project_id="p")
        per_model = sum(
            value
            for row in result["tokens_by_model"].values()
            for key, value in row.items()
            if key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
            and value is not None)
        assert per_model == result["total_tokens"] == 127
