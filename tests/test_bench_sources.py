"""Source adapters: read-only posture, schema tolerance, and the two
counting traps that would silently invent a result.

The fixtures here are synthetic on purpose. The real telemetry these
adapters are built for does not exist on this host yet -- queue.db has
the right tables and zero rows, work.db and ai_usage.db have never been
created -- so the only way to prove the harness is correct BEFORE the
data lands is to build databases with the exact shapes that would break
it and show that it does not break."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from terminal_mcp.bench import sources
from terminal_mcp.bench.model import COHORT_LEGACY, COHORT_NEW, COHORT_UNKNOWN


def _usage_db(path: Path, rows: list[dict], *, table: str = "task_usage", columns: str | None = None) -> None:
    connection = sqlite3.connect(path)
    columns = columns or (
        "task_id TEXT, turn_id TEXT, turn_index INTEGER, input_tokens INTEGER, "
        "output_tokens INTEGER, cache_read_tokens INTEGER, "
        "cache_write_5m_tokens INTEGER, cache_write_1h_tokens INTEGER, "
        "cohort TEXT, model TEXT"
    )
    connection.execute(f"CREATE TABLE {table} ({columns})")
    if rows:
        keys = list(rows[0])
        placeholders = ", ".join("?" for _ in keys)
        connection.executemany(
            f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})",
            [tuple(row[key] for key in keys) for row in rows],
        )
    connection.commit()
    connection.close()


def test_repeated_turn_rows_are_not_double_counted(tmp_path: Path) -> None:
    """The exact shape observed in a real Claude Code transcript in this
    workspace: one logical turn emits four rows, each carrying the FULL
    usage block for that turn. Naive summation overstated output tokens
    by 1.9x there. The aggregator must fold each turn once."""
    path = tmp_path / "ai_usage.db"
    rows = []
    for turn in range(3):
        for _duplicate in range(4):
            rows.append(
                {
                    "task_id": "t1",
                    "turn_id": f"req_{turn}",
                    "turn_index": turn,
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_tokens": 1000,
                    "cache_write_5m_tokens": 50,
                    "cache_write_1h_tokens": 0,
                    "cohort": "new_pipeline",
                    "model": "claude-opus-5",
                }
            )
    _usage_db(path, rows)

    result = sources.UsageDbSource(path).load()
    usage = result.usage_by_task["t1"]

    assert usage.output_tokens == 300, "3 turns x 100, not 12 rows x 100"
    assert usage.input_tokens == 30
    assert usage.cache_read_tokens == 3000
    assert usage.cache_write_tokens == 150
    assert any("double-count guard" in warning for warning in result.warnings)


def test_cumulative_rows_are_taken_not_summed(tmp_path: Path) -> None:
    """A schema that says it stores a running total must never be
    summed -- the last row already IS the task total."""
    path = tmp_path / "work.db"
    _usage_db(
        path,
        [
            {
                "task_id": "t1",
                "turn_index": index,
                "input_tokens": value,
                "output_tokens": value * 2,
                "cache_read_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "is_cumulative": 1,
                "model": "claude-opus-5",
            }
            for index, value in enumerate((100, 250, 400), start=1)
        ],
        columns=(
            "task_id TEXT, turn_index INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_tokens INTEGER, cache_write_5m_tokens INTEGER, "
            "cache_write_1h_tokens INTEGER, is_cumulative INTEGER, model TEXT"
        ),
    )
    usage = sources.UsageDbSource(path).load().usage_by_task["t1"]
    assert usage.input_tokens == 400, "cumulative rows take the last value, never 100+250+400"
    assert usage.output_tokens == 800


def test_absent_columns_become_missing_not_zero(tmp_path: Path) -> None:
    path = tmp_path / "ai_usage.db"
    _usage_db(
        path,
        [{"task_id": "t1", "input_tokens": 10, "output_tokens": 20}],
        columns="task_id TEXT, input_tokens INTEGER, output_tokens INTEGER",
    )
    result = sources.UsageDbSource(path).load()
    usage = result.usage_by_task["t1"]
    assert usage.cache_read_tokens is None
    assert usage.cache_write_tokens is None
    assert usage.primary_cost_tokens is None, "a partial sum is never emitted"
    assert usage.is_complete is False
    assert any("recorded as missing, not as zero" in warning for warning in result.warnings)


def test_null_values_become_missing_not_zero(tmp_path: Path) -> None:
    path = tmp_path / "ai_usage.db"
    _usage_db(
        path,
        [
            {
                "task_id": "t1",
                "turn_id": "a",
                "turn_index": 0,
                "input_tokens": None,
                "output_tokens": 20,
                "cache_read_tokens": None,
                "cache_write_5m_tokens": None,
                "cache_write_1h_tokens": None,
                "cohort": "legacy",
                "model": "claude-opus-5",
            }
        ],
    )
    usage = sources.UsageDbSource(path).load().usage_by_task["t1"]
    assert usage.input_tokens is None
    assert usage.output_tokens == 20


def test_missing_file_is_reported_not_crashed(tmp_path: Path) -> None:
    result = sources.UsageDbSource(tmp_path / "nope.db", name="work.db").load()
    assert result.status.available is False
    assert result.records == ()
    assert "does not exist" in result.status.detail


def test_unrecognised_schema_is_reported_not_guessed(tmp_path: Path) -> None:
    path = tmp_path / "work.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE something_else (a TEXT, b TEXT)")
    connection.commit()
    connection.close()
    result = sources.UsageDbSource(path).load()
    assert result.status.available is False
    assert "no table carrying both a task identifier and token columns" in result.status.detail


def test_source_opens_read_only(tmp_path: Path) -> None:
    path = tmp_path / "ai_usage.db"
    _usage_db(path, [{"task_id": "t1", "input_tokens": 1}], columns="task_id TEXT, input_tokens INTEGER")
    connection, temp = sources.open_readonly(path)
    try:
        with pytest.raises(sqlite3.Error):
            connection.execute("INSERT INTO task_usage (task_id, input_tokens) VALUES ('x', 1)")
    finally:
        connection.close()
        if temp is not None:
            temp.unlink(missing_ok=True)


# --- queue.db: worker turns must not come from attempt_count ---------------


def _queue_db(path: Path, task: dict, events: list[dict]) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE queue_tasks (
            id TEXT PRIMARY KEY, session TEXT, position INTEGER, title TEXT, prompt TEXT,
            status TEXT, created_at TEXT, started_at TEXT, completed_at TEXT,
            attempt_count INTEGER, metadata TEXT, updated_at TEXT, project_id TEXT,
            dispatch_idempotency_key TEXT, verification_evidence TEXT)"""
    )
    connection.execute(
        """CREATE TABLE queue_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, session TEXT, task_id TEXT,
            event_type TEXT, from_status TEXT, to_status TEXT, reason TEXT, metadata TEXT)"""
    )
    connection.execute(
        "INSERT INTO queue_tasks (id, session, position, title, prompt, status, created_at, "
        "started_at, completed_at, attempt_count, metadata, updated_at, project_id, "
        "dispatch_idempotency_key, verification_evidence) "
        "VALUES (:id, 's', 0, '', '', :status, :created_at, :started_at, :completed_at, "
        ":attempt_count, :metadata, :created_at, :project_id, :key, :evidence)",
        task,
    )
    for event in events:
        connection.execute(
            "INSERT INTO queue_events (timestamp, session, task_id, event_type, reason, metadata) "
            "VALUES (:timestamp, 's', :task_id, :event_type, :reason, :metadata)",
            event,
        )
    connection.commit()
    connection.close()


def test_reclaim_cycles_do_not_inflate_worker_turn_count(tmp_path: Path) -> None:
    """The regression the critic lane asked for, and the reason
    attempt_count is banned as a metric source: the queue bumps it on
    reconcile-and-reclaim, so a flaky NODE would score identically to a
    lane with genuinely bad contracts. Four reclaim cycles of ONE
    dispatch are one worker turn."""
    path = tmp_path / "queue.db"
    _queue_db(
        path,
        {
            "id": "t1",
            "status": "COMPLETED",
            "created_at": "2026-09-14T00:00:00+00:00",
            "started_at": "2026-09-14T00:00:00+00:00",
            "completed_at": "2026-09-14T00:10:00+00:00",
            "attempt_count": 4,
            "metadata": json.dumps({"cohort": "new_pipeline", "profile": "STANDARD"}),
            "project_id": "p1",
            "key": "dispatch-aaa",
            "evidence": json.dumps({"tests": "pass"}),
        },
        [
            {
                "timestamp": f"2026-09-14T00:0{n}:00+00:00",
                "task_id": "t1",
                "event_type": "DISPATCHED",
                "reason": None,
                "metadata": json.dumps({"dispatch_idempotency_key": "dispatch-aaa"}),
            }
            for n in range(4)
        ],
    )
    record = sources.QueueDbSource(path).load().records[0]

    assert record.worker_turn_count == 1, "4 reclaims of the same dispatch key are one turn"
    assert record.retries == 0
    assert record.first_pass_success is True
    assert record.duration_seconds == 600.0


def test_distinct_dispatches_are_separate_worker_turns(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    _queue_db(
        path,
        {
            "id": "t1",
            "status": "COMPLETED",
            "created_at": "2026-09-14T00:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "attempt_count": 3,
            "metadata": json.dumps({"cohort": "legacy"}),
            "project_id": "p1",
            "key": None,
            "evidence": None,
        },
        [
            {
                "timestamp": "2026-09-14T00:00:00+00:00",
                "task_id": "t1",
                "event_type": "DISPATCHED",
                "reason": None,
                "metadata": json.dumps({"dispatch_idempotency_key": key}),
            }
            for key in ("d1", "d2", "d3")
        ],
    )
    record = sources.QueueDbSource(path).load().records[0]
    assert record.worker_turn_count == 3
    assert record.retries == 2


def test_first_pass_success_needs_verification_evidence(tmp_path: Path) -> None:
    """Without evidence, "no recorded rework" is indistinguishable from
    "nobody checked" -- and scoring the latter as a success rewards a
    vague contract."""
    path = tmp_path / "queue.db"
    _queue_db(
        path,
        {
            "id": "t1",
            "status": "COMPLETED",
            "created_at": "2026-09-14T00:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "attempt_count": 1,
            "metadata": json.dumps({"cohort": "legacy"}),
            "project_id": "p1",
            "key": "d1",
            "evidence": None,
        },
        [],
    )
    record = sources.QueueDbSource(path).load().records[0]
    assert record.first_pass_success is None
    assert record.first_pass_success_adjusted is None


def test_reentry_reasons_are_read_from_events(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    _queue_db(
        path,
        {
            "id": "t1",
            "status": "COMPLETED",
            "created_at": "2026-09-14T00:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "attempt_count": 1,
            "metadata": json.dumps({"cohort": "legacy"}),
            "project_id": "p1",
            "key": "d1",
            "evidence": None,
        },
        [
            {
                "timestamp": "2026-09-14T00:01:00+00:00",
                "task_id": "t1",
                "event_type": "REENTRY",
                "reason": "environment_failure",
                "metadata": None,
            },
            {
                "timestamp": "2026-09-14T00:02:00+00:00",
                "task_id": "t1",
                "event_type": "REWORK",
                "reason": "CONTRACT_GAP",
                "metadata": None,
            },
        ],
    )
    record = sources.QueueDbSource(path).load().records[0]
    assert record.reentry_counts_by_reason() == {"ENVIRONMENT_FAILURE": 1, "CONTRACT_GAP": 1}
    assert record.counted_reentry_count == 1
    assert len(record.excluded_reentries) == 1


def test_unlabelled_cohort_stays_unknown(tmp_path: Path) -> None:
    path = tmp_path / "queue.db"
    _queue_db(
        path,
        {
            "id": "t1",
            "status": "COMPLETED",
            "created_at": "2026-09-14T00:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "attempt_count": 1,
            "metadata": "{}",
            "project_id": "p1",
            "key": None,
            "evidence": None,
        },
        [],
    )
    assert sources.QueueDbSource(path).load().records[0].cohort == COHORT_UNKNOWN
    mapped = sources.QueueDbSource(path, cohort_map={"t1": "before"}).load().records[0]
    assert mapped.cohort == COHORT_LEGACY


def test_merge_never_overwrites_a_known_value_with_missing(tmp_path: Path) -> None:
    from terminal_mcp.bench.model import TaskRecord, TaskUsage

    spine = TaskRecord(task_id="t1", cohort=COHORT_NEW, project="p1", source="queue.db")
    tokens = TaskRecord(
        task_id="t1",
        project=None,
        usage=TaskUsage(input_tokens=5, output_tokens=6),
        source="work.db",
    )
    merged = sources.merge_records([[spine], [tokens]])[0]
    assert merged.project == "p1"
    assert merged.cohort == COHORT_NEW
    assert merged.usage.input_tokens == 5
    assert merged.source == "queue.db+work.db"
