"""The work_telemetry.db adapter, against the schema Task A committed.

Fixtures are built to the real column names from
docs/WORK_EFFICIENCY_TELEMETRY.md. Every test here pins a semantic that
a generic schema reader would get subtly, silently wrong."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.bench.model import COHORT_LEGACY, COHORT_NEW
from terminal_mcp.bench.work_telemetry import (
    FLAG_COUNTER_RESET,
    FLAG_PARTIAL_SAMPLES,
    WorkTelemetryDbSource,
)

TASK_COLUMNS = (
    "task_id TEXT PRIMARY KEY, work_id TEXT, project_id TEXT, first_pass_success TEXT, "
    "terminal_status TEXT, reentry_count INTEGER, contract_gap_count INTEGER, "
    "started_at TEXT, completed_at TEXT, metadata TEXT"
)
SAMPLE_COLUMNS = (
    "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, phase TEXT, kind TEXT, "
    "idempotency_key TEXT, input_tokens INTEGER, output_tokens INTEGER, "
    "cache_read_tokens INTEGER, cache_write_tokens INTEGER, turn_count INTEGER, "
    "reported_input_tokens INTEGER, reported_output_tokens INTEGER, "
    "counter_reset INTEGER, confidence TEXT, evidence_source TEXT"
)
REENTRY_COLUMNS = "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, reason TEXT, phase TEXT, occurred_at TEXT, detail TEXT"


def build(path: Path, tasks: list[dict], samples: list[dict], reentries: list[dict]) -> Path:
    connection = sqlite3.connect(path)
    connection.execute(f"CREATE TABLE telemetry_tasks ({TASK_COLUMNS})")
    connection.execute(f"CREATE TABLE telemetry_usage_samples ({SAMPLE_COLUMNS})")
    connection.execute(f"CREATE TABLE telemetry_reentries ({REENTRY_COLUMNS})")
    connection.execute("CREATE TABLE telemetry_counters (name TEXT, value INTEGER)")
    for table, rows in (
        ("telemetry_tasks", tasks),
        ("telemetry_usage_samples", samples),
        ("telemetry_reentries", reentries),
    ):
        for row in rows:
            keys = list(row)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})",
                tuple(row[key] for key in keys),
            )
    connection.commit()
    connection.close()
    return path


def base_task(task_id: str, **overrides) -> dict:
    payload = {
        "task_id": task_id,
        "project_id": "git:github.com/acme/widget",
        "first_pass_success": "TRUE",
        "terminal_status": "COMPLETED",
        "reentry_count": 0,
        "contract_gap_count": 0,
        "started_at": "2026-09-14T00:00:00+00:00",
        "completed_at": "2026-09-14T00:20:00+00:00",
        "metadata": json.dumps({"cohort": "new_pipeline"}),
    }
    payload.update(overrides)
    return payload


def sample(task_id: str, phase: str, **overrides) -> dict:
    payload = {
        "task_id": task_id,
        "phase": phase,
        "kind": "DELTA",
        "idempotency_key": f"{task_id}-{phase}-{overrides.get('turn_count', 1)}",
        "input_tokens": 100,
        "output_tokens": 200,
        "cache_read_tokens": 5000,
        "cache_write_tokens": 400,
        "turn_count": 1,
        "reported_input_tokens": 999_999,
        "reported_output_tokens": 999_999,
        "counter_reset": 0,
        "confidence": "MEASURED",
        "evidence_source": "claude_usage_json",
    }
    payload.update(overrides)
    return payload


def test_delta_samples_sum_and_reported_columns_are_ignored(tmp_path: Path) -> None:
    """`reported_*` is cumulative when kind='CUMULATIVE'. Summing it
    would be the exact double-count this harness exists to prevent."""
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [sample("t1", "IMPLEMENTATION") for _ in range(3)],
        [],
    )
    record = WorkTelemetryDbSource(path).load().records[0]
    assert record.usage.input_tokens == 300
    assert record.usage.output_tokens == 600
    assert record.usage.cache_write_tokens == 1200
    assert record.usage.input_tokens != 999_999 * 3


def test_only_worker_phases_count_toward_worker_turns(tmp_path: Path) -> None:
    """ANALYSIS and CONTRACT are the investment being measured, and
    UNKNOWN is unattributed. Counting either into the worker's turns
    would inflate the precise number under test."""
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [
            sample("t1", "ANALYSIS", turn_count=5),
            sample("t1", "CONTRACT", turn_count=4),
            sample("t1", "UNKNOWN", turn_count=7),
            sample("t1", "IMPLEMENTATION", turn_count=2),
            sample("t1", "VERIFICATION", turn_count=1),
            sample("t1", "DELIVERY", turn_count=1),
        ],
        [],
    )
    record = WorkTelemetryDbSource(path).load().records[0]
    assert record.worker_turn_count == 4, "2+1+1, not 20"
    # Tokens from every phase still count toward cost -- the investment
    # is real spend, and excluding it is what biases the comparison.
    assert record.usage.input_tokens == 600


def test_a_task_with_no_samples_is_unmeasured_not_zero(tmp_path: Path) -> None:
    path = build(tmp_path / "work_telemetry.db", [base_task("t1")], [], [])
    result = WorkTelemetryDbSource(path).load()
    record = result.records[0]
    assert record.usage.is_empty is True
    assert record.usage.input_tokens is None
    assert record.usage.primary_cost_tokens is None
    assert record.worker_turn_count is None
    assert any("never entered as zero" in warning for warning in result.warnings)


def test_a_genuine_zero_is_kept_as_zero(tmp_path: Path) -> None:
    """NULL means never measured, 0 means measured as zero. They are
    different facts and must stay different."""
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [sample("t1", "IMPLEMENTATION", input_tokens=0, output_tokens=0,
                cache_read_tokens=0, cache_write_tokens=0)],
        [],
    )
    usage = WorkTelemetryDbSource(path).load().records[0].usage
    assert usage.input_tokens == 0
    assert usage.is_empty is False
    assert usage.primary_cost_tokens == 0


def test_partially_null_samples_are_flagged(tmp_path: Path) -> None:
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [
            sample("t1", "IMPLEMENTATION"),
            sample("t1", "IMPLEMENTATION", input_tokens=None),
        ],
        [],
    )
    record = WorkTelemetryDbSource(path).load().records[0]
    assert FLAG_PARTIAL_SAMPLES in record.flags
    assert record.usage.input_tokens == 100


def test_counter_reset_is_flagged_not_excluded(tmp_path: Path) -> None:
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [sample("t1", "IMPLEMENTATION", counter_reset=1)],
        [],
    )
    record = WorkTelemetryDbSource(path).load().records[0]
    assert FLAG_COUNTER_RESET in record.flags
    assert record.usage.input_tokens == 100


def test_weakest_confidence_propagates(tmp_path: Path) -> None:
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [
            sample("t1", "IMPLEMENTATION", confidence="MEASURED"),
            sample("t1", "IMPLEMENTATION", confidence="ESTIMATED"),
            sample("t1", "IMPLEMENTATION", confidence="DERIVED"),
        ],
        [],
    )
    assert "CONFIDENCE_ESTIMATED" in WorkTelemetryDbSource(path).load().records[0].flags


def test_first_pass_success_is_tri_state(tmp_path: Path) -> None:
    path = build(
        tmp_path / "work_telemetry.db",
        [
            base_task("t1", first_pass_success="TRUE"),
            base_task("t2", first_pass_success="FALSE"),
            base_task("t3", first_pass_success="UNKNOWN", terminal_status="CANCELLED"),
        ],
        [],
        [],
    )
    by_id = {record.task_id: record for record in WorkTelemetryDbSource(path).load().records}
    assert by_id["t1"].first_pass_success is True
    assert by_id["t2"].first_pass_success is False
    assert by_id["t3"].first_pass_success is None, "UNKNOWN leaves the denominator"
    assert by_id["t3"].first_pass_success_adjusted is None


def test_reentry_reasons_come_through_for_the_exclusion_logic(tmp_path: Path) -> None:
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1", first_pass_success="FALSE", reentry_count=3)],
        [],
        [
            {"task_id": "t1", "reason": "USER_CHANGED_REQUIREMENT", "phase": "IMPLEMENTATION",
             "occurred_at": "2026-09-14T00:05:00+00:00", "detail": ""},
            {"task_id": "t1", "reason": "ENVIRONMENT_FAILURE", "phase": "VERIFICATION",
             "occurred_at": "2026-09-14T00:06:00+00:00", "detail": ""},
            {"task_id": "t1", "reason": "CONTRACT_GAP", "phase": "IMPLEMENTATION",
             "occurred_at": "2026-09-14T00:07:00+00:00", "detail": ""},
        ],
    )
    record = WorkTelemetryDbSource(path).load().records[0]
    assert record.counted_reentry_count == 1
    assert len(record.excluded_reentries) == 2
    assert record.first_pass_success_adjusted is False


def test_excluded_reasons_only_flips_first_pass_and_flags_the_task(tmp_path: Path) -> None:
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1", first_pass_success="FALSE", reentry_count=2)],
        [],
        [
            {"task_id": "t1", "reason": "ENVIRONMENT_FAILURE", "phase": "IMPLEMENTATION",
             "occurred_at": "2026-09-14T00:05:00+00:00", "detail": ""},
            {"task_id": "t1", "reason": "USER_CHANGED_REQUIREMENT", "phase": "IMPLEMENTATION",
             "occurred_at": "2026-09-14T00:06:00+00:00", "detail": ""},
        ],
    )
    record = WorkTelemetryDbSource(path).load().records[0]
    assert record.first_pass_success_adjusted is True
    assert record.has_excluded_reason_only is True


def test_retries_is_not_invented(tmp_path: Path) -> None:
    """A retry that costs a worker round trip IS a re-entry in this
    store, so reporting a separate retries number would double count."""
    path = build(tmp_path / "work_telemetry.db", [base_task("t1")], [], [])
    assert WorkTelemetryDbSource(path).load().records[0].retries is None


def test_duration_and_project_come_from_the_rollup(tmp_path: Path) -> None:
    path = build(tmp_path / "work_telemetry.db", [base_task("t1")], [], [])
    record = WorkTelemetryDbSource(path).load().records[0]
    assert record.duration_seconds == 1200.0
    assert record.project == "git:github.com/acme/widget"
    assert record.cohort == COHORT_NEW


def test_cohort_map_overrides_metadata(tmp_path: Path) -> None:
    path = build(tmp_path / "work_telemetry.db", [base_task("t1")], [], [])
    record = WorkTelemetryDbSource(path, cohort_map={"t1": "legacy"}).load().records[0]
    assert record.cohort == COHORT_LEGACY


def test_price_model_assumption_is_disclosed(tmp_path: Path) -> None:
    """The store records no model id, so cost is unpriceable unless an
    operator states an assumption -- and then the report must say so."""
    path = build(
        tmp_path / "work_telemetry.db",
        [base_task("t1")],
        [sample("t1", "IMPLEMENTATION")],
        [],
    )
    plain = WorkTelemetryDbSource(path).load().records[0]
    assert plain.model is None
    assert plain.cost_units() is None

    result = WorkTelemetryDbSource(path, price_model="claude-opus-5").load()
    priced = result.records[0]
    assert priced.model == "claude-opus-5"
    assert any("--price-model" in warning for warning in result.warnings)


def test_missing_store_is_reported_not_crashed(tmp_path: Path) -> None:
    result = WorkTelemetryDbSource(tmp_path / "absent.db").load()
    assert result.status.available is False
    assert "no worker has emitted telemetry" in result.status.detail


def test_empty_store_says_empty_not_absent(tmp_path: Path) -> None:
    path = build(tmp_path / "work_telemetry.db", [], [], [])
    result = WorkTelemetryDbSource(path).load()
    assert result.status.available is True
    assert "no worker has written a row yet" in result.status.detail
