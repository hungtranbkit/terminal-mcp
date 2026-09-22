"""Work Efficiency Telemetry -- storage/schema tests (TASK A acceptance).

Each acceptance item from the task has at least one test that fails if
the guarantee is lost:

  * migration from an existing database   -> TestMigration
  * duplicate event is idempotent          -> TestIdempotency
  * a re-entry increments exactly once     -> TestReentry
  * completion derives first_pass_success  -> TestFirstPassSuccess
  * missing telemetry is NULL, never 0     -> TestMissingIsNotZero
  * crash / retry / restart                -> TestCrashRetryRestart
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest

from terminal_mcp.queue_store import QueueStore
from terminal_mcp.schema import Migration, apply_migrations, get_schema_version
from terminal_mcp.work_telemetry_store import (
    CONFIDENCE_ESTIMATED, CONFIDENCE_MEASURED, FIRST_PASS_FALSE, FIRST_PASS_TRUE,
    FIRST_PASS_UNKNOWN, KIND_CUMULATIVE, KIND_DELTA, PHASE_ANALYSIS, PHASE_CONTRACT,
    MODEL_UNKNOWN, PHASE_IMPLEMENTATION, REENTRY_CONTRACT_GAP, REENTRY_REASONS, REENTRY_STALE_CONTEXT,
    REENTRY_TEST_FAILURE, STATUS_CANCELLED,
    STATUS_COMPLETED, STATUS_FAILED, TELEMETRY_MIGRATIONS, TelemetryValidationError,
    WorkTelemetryStore, default_telemetry_db_path,
)


@pytest.fixture
def store(tmp_path) -> WorkTelemetryStore:
    return WorkTelemetryStore(tmp_path / "work_telemetry.db")


class TestMigration:
    def test_fresh_database_is_stamped_at_the_latest_version(self, store):
        with sqlite3.connect(store.path) as connection:
            assert get_schema_version(connection) == max(m.version for m in TELEMETRY_MIGRATIONS)

    def test_reopening_an_existing_database_applies_nothing_and_keeps_its_rows(self, tmp_path):
        path = tmp_path / "work_telemetry.db"
        first = WorkTelemetryStore(path)
        first.start_task("task-1", work_id="outcome-1")
        first.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=42)

        # A second open is exactly what every process restart does.
        with sqlite3.connect(path) as connection:
            version_before = get_schema_version(connection)
            assert apply_migrations(connection, TELEMETRY_MIGRATIONS) == []

        second = WorkTelemetryStore(path)
        assert second.get_task("task-1")["work_id"] == "outcome-1"
        assert len(second.list_samples("task-1")) == 1
        with sqlite3.connect(path) as connection:
            assert get_schema_version(connection) == version_before

    def test_the_queue_database_is_never_touched(self, tmp_path):
        """The whole point of a separate file: enabling telemetry must be
        incapable of altering the database that real work lives in."""
        queue_path = tmp_path / "queue.db"
        queue = QueueStore(queue_path)
        queue.set_tasks("lane-a", [{"prompt": "do the thing"}])
        with sqlite3.connect(queue_path) as connection:
            queue_version_before = get_schema_version(connection)
            tasks_before = connection.execute("SELECT COUNT(*) FROM queue_tasks").fetchone()[0]

        telemetry = WorkTelemetryStore(tmp_path / "work_telemetry.db")
        telemetry.start_task("task-1")
        telemetry.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=1)

        with sqlite3.connect(queue_path) as connection:
            assert get_schema_version(connection) == queue_version_before
            assert connection.execute("SELECT COUNT(*) FROM queue_tasks").fetchone()[0] == tasks_before
            # and telemetry put none of its own tables in there
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert not any(name.startswith("telemetry_") for name in names)

    def test_a_task_that_predates_telemetry_reads_back_as_unknown_not_zero(self, store):
        """Old tasks keep working: no row, no invented numbers."""
        assert store.get_task("task-created-last-month") is None
        assert store.phase_totals("task-created-last-month") == {}

    def test_a_partially_applied_migration_is_resumable(self, tmp_path):
        """The real crash-during-migration path, and it is NOT the one
        you would assume: `apply_migrations` only stamps user_version
        after a migration commits, but Python's sqlite3 does not wrap DDL
        in a transaction, so a crash partway through leaves SOME tables
        already created with the version still at 0. The retry re-runs
        the whole migration over a half-built database -- survivable only
        because every statement in it is IF NOT EXISTS.

        The half-built state is built from the REAL schema function (then
        dropping what a crash would not have reached yet), so this stays
        true if the schema changes; a hand-written stub table would pass
        while proving nothing."""
        from terminal_mcp.work_telemetry_store import _create_v1_schema

        path = tmp_path / "half.db"
        with sqlite3.connect(path) as connection:
            _create_v1_schema(connection)
            for table in ("telemetry_reentries", "telemetry_contract_gaps", "telemetry_counters"):
                connection.execute(f"DROP TABLE {table}")
            assert get_schema_version(connection) == 0  # the crash never stamped it

        # The retry -- an ordinary restart -- must recover, not brick.
        store = WorkTelemetryStore(path)
        store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=5)
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        assert store.phase_totals("task-1")[PHASE_ANALYSIS]["input_tokens"] == 5
        assert store.get_task("task-1")["reentry_count"] == 1
        with sqlite3.connect(path) as connection:
            assert get_schema_version(connection) == max(m.version for m in TELEMETRY_MIGRATIONS)

    def test_default_path_honours_the_project_wide_env_var_order(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.delenv("TERMINAL_MCP_WORK_TELEMETRY_DB", raising=False)
        assert default_telemetry_db_path() == tmp_path / "state" / "terminal-mcp" / "work_telemetry.db"
        monkeypatch.setenv("TERMINAL_MCP_WORK_TELEMETRY_DB", str(tmp_path / "explicit.db"))
        assert default_telemetry_db_path() == tmp_path / "explicit.db"


class TestIdempotency:
    def test_a_duplicate_usage_sample_changes_nothing(self, store):
        first = store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="evt-1",
                                   input_tokens=100, output_tokens=20)
        second = store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="evt-1",
                                    input_tokens=100, output_tokens=20)
        assert first["applied"] is True and first["duplicate"] is False
        assert second["applied"] is False and second["duplicate"] is True
        assert len(store.list_samples("task-1")) == 1
        assert store.phase_totals("task-1")[PHASE_ANALYSIS]["input_tokens"] == 100

    def test_an_identical_report_with_no_key_still_dedupes(self, store):
        """The derived key covers the whole content of the report, so a
        producer replaying the same snapshot adds nothing."""
        kwargs = dict(phase=PHASE_ANALYSIS, input_tokens=100, observed_at="2026-09-14T00:00:00+00:00")
        store.record_usage("task-1", **kwargs)
        again = store.record_usage("task-1", **kwargs)
        assert again["duplicate"] is True
        assert len(store.list_samples("task-1")) == 1

    def test_a_duplicate_cumulative_snapshot_does_not_advance_the_counter(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-7", idempotency_key="snap-1", input_tokens=1000)
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-7", idempotency_key="snap-1", input_tokens=1000)
        assert store.get_counter("session-7")["last_input_tokens"] == 1000
        assert store.phase_totals("task-1")[PHASE_IMPLEMENTATION]["input_tokens"] == 1000

    def test_cumulative_snapshots_are_summed_as_deltas_not_as_snapshots(self, store):
        for index, total in enumerate((1000, 2500, 4000), start=1):
            store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                               counter_id="session-7", idempotency_key=f"snap-{index}",
                               input_tokens=total, turn_count=index * 2)
        # Naive summing would give 7500 and 12 turns.
        totals = store.phase_totals("task-1")[PHASE_IMPLEMENTATION]
        assert totals["input_tokens"] == 4000
        assert totals["turn_count"] == 6

    def test_a_cumulative_sample_without_a_counter_id_is_refused(self, store):
        with pytest.raises(TelemetryValidationError, match="counter_id"):
            store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                               input_tokens=10)

    def test_delta_samples_are_stored_verbatim_and_accumulate(self, store):
        for index in range(3):
            store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_DELTA,
                               idempotency_key=f"turn-{index}", input_tokens=100, turn_count=1)
        totals = store.phase_totals("task-1")[PHASE_IMPLEMENTATION]
        assert totals["input_tokens"] == 300
        assert totals["turn_count"] == 3

    def test_two_counters_do_not_interfere(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-a", idempotency_key="a1", input_tokens=500)
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-b", idempotency_key="b1", input_tokens=700)
        assert store.phase_totals("task-1")[PHASE_IMPLEMENTATION]["input_tokens"] == 1200

    def test_one_counter_spanning_two_tasks_attributes_only_the_increment(self, store):
        """A worker session that finishes task-1 and starts task-2 keeps
        one climbing counter; task-2 must be charged the rise only."""
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-7", idempotency_key="s1", input_tokens=1000)
        store.record_usage("task-2", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-7", idempotency_key="s2", input_tokens=1600)
        assert store.phase_totals("task-1")[PHASE_IMPLEMENTATION]["input_tokens"] == 1000
        assert store.phase_totals("task-2")[PHASE_IMPLEMENTATION]["input_tokens"] == 600


class TestReentry:
    def test_a_reentry_increments_exactly_once(self, store):
        store.start_task("task-1")
        first = store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        assert first["task"]["reentry_count"] == 1

    def test_a_duplicate_reentry_does_not_increment_again(self, store):
        store.start_task("task-1")
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        again = store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        assert again["duplicate"] is True
        assert again["task"]["reentry_count"] == 1
        assert len(store.list_reentries("task-1")) == 1

    def test_distinct_reentries_each_increment(self, store):
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        result = store.record_reentry("task-1", reason="IMPLEMENTATION_BUG", idempotency_key="re-2")
        assert result["task"]["reentry_count"] == 2

    def test_a_contract_gap_reentry_counts_in_both_places(self, store):
        result = store.record_reentry("task-1", reason=REENTRY_CONTRACT_GAP, idempotency_key="re-1")
        assert result["task"]["reentry_count"] == 1
        assert result["task"]["contract_gap_count"] == 1

    def test_a_contract_gap_without_a_reentry_counts_only_as_a_gap(self, store):
        result = store.record_contract_gap("task-1", idempotency_key="gap-1",
                                           detail="error shape unspecified")
        assert result["task"]["contract_gap_count"] == 1
        assert result["task"]["reentry_count"] == 0

    def test_a_duplicate_contract_gap_does_not_increment_again(self, store):
        store.record_contract_gap("task-1", idempotency_key="gap-1")
        again = store.record_contract_gap("task-1", idempotency_key="gap-1")
        assert again["duplicate"] is True
        assert again["task"]["contract_gap_count"] == 1

    def test_an_unknown_reason_is_refused_rather_than_coerced_to_other(self, store):
        with pytest.raises(TelemetryValidationError, match="reason"):
            store.record_reentry("task-1", reason="FLAKY_VIBES", idempotency_key="re-1")
        assert store.get_task("task-1") is None

    def test_recording_a_reentry_opens_the_rollup_for_an_unseen_task(self, store):
        """Producers are not required to call start_task first -- a
        re-entry observed for a task nobody opened is still real."""
        store.record_reentry("task-unseen", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        assert store.get_task("task-unseen")["reentry_count"] == 1


class TestFirstPassSuccess:
    def test_it_is_unknown_until_completion(self, store):
        store.start_task("task-1")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=10)
        assert store.get_task("task-1")["first_pass_success"] == FIRST_PASS_UNKNOWN

    def test_completed_with_no_reentry_is_true(self, store):
        store.start_task("task-1")
        row = store.complete_task("task-1", status=STATUS_COMPLETED)
        assert row["first_pass_success"] == FIRST_PASS_TRUE
        assert row["terminal_status"] == STATUS_COMPLETED

    def test_completed_after_a_reentry_is_false(self, store):
        store.start_task("task-1")
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        assert store.complete_task("task-1")["first_pass_success"] == FIRST_PASS_FALSE

    def test_failed_is_false(self, store):
        store.start_task("task-1")
        assert store.complete_task("task-1", status=STATUS_FAILED)["first_pass_success"] == FIRST_PASS_FALSE

    def test_cancelled_stays_unknown(self, store):
        """A cancelled task never got its first pass -- claiming either
        verdict would be a statement about something that never happened."""
        store.start_task("task-1")
        row = store.complete_task("task-1", status=STATUS_CANCELLED)
        assert row["first_pass_success"] == FIRST_PASS_UNKNOWN
        assert row["terminal_status"] == STATUS_CANCELLED

    def test_contract_gaps_alone_do_not_falsify_it(self, store):
        """A gap absorbed without a re-entry cost no extra round trip --
        which is what this flag measures. The gap is still counted."""
        store.start_task("task-1")
        store.record_contract_gap("task-1", idempotency_key="gap-1")
        row = store.complete_task("task-1")
        assert row["first_pass_success"] == FIRST_PASS_TRUE
        assert row["contract_gap_count"] == 1

    def test_completing_twice_keeps_the_first_verdict_and_timestamp(self, store):
        store.start_task("task-1")
        first = store.complete_task("task-1", completed_at="2026-09-14T10:00:00+00:00")
        store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        second = store.complete_task("task-1", completed_at="2026-09-14T12:00:00+00:00")
        assert second["completed_at"] == first["completed_at"] == "2026-09-14T10:00:00+00:00"
        assert second["first_pass_success"] == FIRST_PASS_TRUE

    def test_reopening_clears_the_verdict_so_it_can_be_rederived(self, store):
        store.start_task("task-1")
        store.complete_task("task-1")
        store.reopen_task("task-1")
        assert store.get_task("task-1")["first_pass_success"] == FIRST_PASS_UNKNOWN
        store.record_reentry("task-1", reason="DELIVERY_FAILURE", idempotency_key="re-1")
        assert store.complete_task("task-1")["first_pass_success"] == FIRST_PASS_FALSE


class TestMissingIsNotZero:
    def test_unreported_token_kinds_are_null_not_zero(self, store):
        store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=100)
        sample = store.list_samples("task-1")[0]
        assert sample["input_tokens"] == 100
        assert sample["output_tokens"] is None
        assert sample["cache_read_tokens"] is None
        assert sample["cache_write_tokens"] is None
        assert sample["turn_count"] is None

    def test_a_real_measured_zero_is_kept_as_zero(self, store):
        """0 is a measurement. None is the absence of one. Passing 0 must
        not be quietly turned into 'unknown' either."""
        store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=0, output_tokens=0)
        sample = store.list_samples("task-1")[0]
        assert sample["input_tokens"] == 0 and sample["output_tokens"] == 0

    def test_a_phase_with_no_samples_has_no_row_at_all(self, store):
        store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=100)
        totals = store.phase_totals("task-1")
        assert PHASE_CONTRACT not in totals

    def test_the_rollup_row_carries_no_token_columns(self, store):
        """Structural guard: the moment a DEFAULT 0 token column appears
        on the rollup, 'never measured' and 'measured zero' become the
        same value and this store starts lying."""
        store.start_task("task-1")
        assert not [key for key in store.get_task("task-1") if key.endswith("_tokens")]

    def test_a_negative_token_count_is_refused(self, store):
        with pytest.raises(TelemetryValidationError, match="input_tokens"):
            store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=-1)

    def test_a_non_integer_token_count_is_refused_not_truncated(self, store):
        with pytest.raises(TelemetryValidationError, match="int"):
            store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=12.7)

    def test_an_unknown_phase_is_refused(self, store):
        with pytest.raises(TelemetryValidationError, match="phase"):
            store.record_usage("task-1", phase="VIBING", input_tokens=1)


class TestCrashRetryRestart:
    def test_a_producer_restart_keeps_the_counter_baseline(self, tmp_path):
        """The double-count hazard at its most realistic: the collector
        process dies and a NEW one re-reads the same cumulative usage
        file. Only the rise since the last observation may be charged."""
        path = tmp_path / "work_telemetry.db"
        WorkTelemetryStore(path).record_usage(
            "task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE, counter_id="session-7",
            idempotency_key="snap-1", input_tokens=1000)
        restarted = WorkTelemetryStore(path)
        restarted.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                               counter_id="session-7", idempotency_key="snap-2", input_tokens=1200)
        assert restarted.phase_totals("task-1")[PHASE_IMPLEMENTATION]["input_tokens"] == 1200

    def test_a_worker_restart_resetting_its_counter_is_charged_in_full_and_flagged(self, store):
        """The agent process was killed and relaunched: its counter falls
        back to a low number and climbs again. That new number is genuine
        new usage, so it counts -- and the row says a reset happened, so
        a human can distrust the total on purpose."""
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                           counter_id="session-7", idempotency_key="snap-1", input_tokens=5000)
        result = store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                                    counter_id="session-7", idempotency_key="snap-2",
                                    input_tokens=300)
        assert result["counter_reset"] is True
        assert result["sample"]["input_tokens"] == 300
        assert store.phase_totals("task-1")[PHASE_IMPLEMENTATION]["input_tokens"] == 5300
        assert store.get_counter("session-7")["last_input_tokens"] == 300

    def test_a_retry_after_a_crashed_write_applies_the_event_exactly_once(self, store, monkeypatch):
        """A producer that crashes somewhere inside record_reentry and
        retries with the same key must end at exactly one re-entry. The
        invariant asserted is the strong one -- a row and its increment
        are either both present or both absent, never one without the
        other -- because that is what makes the retry safe at all."""
        import terminal_mcp.work_telemetry_store as module

        calls = {"n": 0}
        real_iso_now = module._iso_now

        def crashing_iso_now():
            calls["n"] += 1
            if calls["n"] == 3:  # mid-transaction, after the INSERT
                raise RuntimeError("process killed")
            return real_iso_now()

        monkeypatch.setattr(module, "_iso_now", crashing_iso_now)
        with pytest.raises(RuntimeError):
            store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        monkeypatch.setattr(module, "_iso_now", real_iso_now)

        task = store.get_task("task-1")
        rows = store.list_reentries("task-1")
        assert len(rows) == (0 if task is None else task["reentry_count"])

        retried = store.record_reentry("task-1", reason=REENTRY_TEST_FAILURE, idempotency_key="re-1")
        assert retried["task"]["reentry_count"] == 1
        assert len(store.list_reentries("task-1")) == 1

    def test_a_restart_preserves_started_at(self, tmp_path):
        path = tmp_path / "work_telemetry.db"
        first = WorkTelemetryStore(path).start_task("task-1", started_at="2026-09-14T08:00:00+00:00")
        again = WorkTelemetryStore(path).start_task("task-1")
        assert again["started_at"] == first["started_at"]

    def test_a_later_report_never_erases_identity_already_known(self, store):
        store.start_task("task-1", work_id="outcome-1", session_id="window2", project_id="git:acme")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=1)
        task = store.get_task("task-1")
        assert (task["work_id"], task["session_id"], task["project_id"]) == (
            "outcome-1", "window2", "git:acme")

    def test_identity_learned_late_is_filled_in(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=1)
        store.complete_task("task-1", work_id="outcome-9")
        assert store.get_task("task-1")["work_id"] == "outcome-9"

    def test_the_weakest_confidence_wins_on_the_rollup(self, store):
        store.start_task("task-1", confidence=CONFIDENCE_MEASURED)
        store.record_usage("task-1", phase=PHASE_ANALYSIS, input_tokens=1,
                           confidence=CONFIDENCE_ESTIMATED)
        assert store.get_task("task-1")["confidence"] == CONFIDENCE_ESTIMATED


class TestModelIdAndCacheTtl:
    """Migration v2: the fields that make a token count priceable.

    Requested by the benchmark-harness lane, which could not turn any
    total into cost without them -- output bills 5x input, a cache read
    ~0.1x, and a cache write 1.25x at the 5-minute TTL against 2x at the
    one-hour one, all per-model.
    """

    def test_migrating_a_v1_database_keeps_its_rows_and_adds_the_columns(self, tmp_path):
        """The acceptance case: an existing database with real rows in it
        gains the new columns without losing or rewriting anything."""
        path = tmp_path / "work_telemetry.db"
        v1_only = [m for m in TELEMETRY_MIGRATIONS if m.version == 1]
        with sqlite3.connect(path) as connection:
            apply_migrations(connection, v1_only)
            assert get_schema_version(connection) == 1
        # A v1-era producer's rows, written as v1 SQL -- today's code
        # cannot stand in for one, since it writes the v2 columns.
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO telemetry_tasks (task_id, phase, started_at, reentry_count, "
                "created_at, updated_at) VALUES ('task-1','ANALYSIS','2026-09-01T00:00:00+00:00',"
                "1,'2026-09-01T00:00:00+00:00','2026-09-01T00:00:00+00:00')")
            connection.execute(
                "INSERT INTO telemetry_usage_samples (idempotency_key, task_id, phase, kind, "
                "observed_at, recorded_at, input_tokens, cache_write_tokens) "
                "VALUES ('old-1','task-1','ANALYSIS','DELTA','2026-09-01T00:00:00+00:00',"
                "'2026-09-01T00:00:00+00:00',100,500)")

        upgraded = WorkTelemetryStore(path)  # real open -> applies v2

        with sqlite3.connect(path) as connection:
            assert get_schema_version(connection) == max(m.version for m in TELEMETRY_MIGRATIONS)
        sample = upgraded.list_samples("task-1")[0]
        assert sample["input_tokens"] == 100
        assert sample["cache_write_tokens"] == 500
        # New columns exist and read back as UNKNOWN for the old row.
        assert sample["model_id"] is None
        assert sample["cache_write_5m_tokens"] is None
        assert sample["cache_write_1h_tokens"] is None
        assert upgraded.get_task("task-1")["reentry_count"] == 1
        # And the old row's idempotency key still dedupes after the upgrade.
        assert upgraded.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="old-1",
                                     input_tokens=100)["duplicate"] is True

    def test_model_id_is_stored_verbatim(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, input_tokens=10,
                           model_id="claude-opus-5")
        assert store.list_samples("task-1")[0]["model_id"] == "claude-opus-5"

    def test_model_totals_split_a_task_that_spans_models(self, store):
        store.record_usage("task-1", phase=PHASE_ANALYSIS, idempotency_key="a",
                           input_tokens=300, output_tokens=40, model_id="claude-opus-5")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="b",
                           input_tokens=4000, output_tokens=900, model_id="claude-sonnet-5")
        totals = store.model_totals("task-1")
        assert totals["claude-opus-5"]["input_tokens"] == 300
        assert totals["claude-sonnet-5"]["output_tokens"] == 900

    def test_usage_with_no_model_is_grouped_under_unknown_not_dropped(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="a",
                           input_tokens=10, model_id="claude-opus-5")
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, idempotency_key="b",
                           input_tokens=7)
        totals = store.model_totals("task-1")
        assert totals[MODEL_UNKNOWN]["input_tokens"] == 7
        assert sum(row["input_tokens"] for row in totals.values()) == 17

    def test_the_ttl_split_is_stored_and_the_total_derived_from_it(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION,
                           cache_write_5m_tokens=800, cache_write_1h_tokens=200)
        sample = store.list_samples("task-1")[0]
        assert (sample["cache_write_5m_tokens"], sample["cache_write_1h_tokens"]) == (800, 200)
        assert sample["cache_write_tokens"] == 1000

    def test_a_collapsed_total_with_no_split_still_works(self, store):
        """Backward compatibility: a producer that only has the collapsed
        number keeps working and reports the TTL mix as unknown."""
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, cache_write_tokens=1000)
        sample = store.list_samples("task-1")[0]
        assert sample["cache_write_tokens"] == 1000
        assert sample["cache_write_5m_tokens"] is None
        assert sample["cache_write_1h_tokens"] is None

    def test_a_split_that_disagrees_with_its_total_is_refused(self, store):
        with pytest.raises(TelemetryValidationError, match="disagrees"):
            store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, cache_write_tokens=999,
                               cache_write_5m_tokens=800, cache_write_1h_tokens=200)

    def test_a_split_that_agrees_with_its_total_is_accepted(self, store):
        store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, cache_write_tokens=1000,
                           cache_write_5m_tokens=800, cache_write_1h_tokens=200)
        assert store.list_samples("task-1")[0]["cache_write_tokens"] == 1000

    def test_cumulative_snapshots_of_the_ttl_split_are_differenced_too(self, store):
        """The new columns go through the same counter machinery -- if
        they did not, they would be the one place snapshots still summed
        as snapshots."""
        for index, (five_m, one_h) in enumerate(((800, 200), (1500, 500), (2000, 900)), start=1):
            store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, kind=KIND_CUMULATIVE,
                               counter_id="session-7", idempotency_key=f"snap-{index}",
                               cache_write_5m_tokens=five_m, cache_write_1h_tokens=one_h)
        totals = store.phase_totals("task-1")[PHASE_IMPLEMENTATION]
        assert totals["cache_write_5m_tokens"] == 2000   # not 4300
        assert totals["cache_write_1h_tokens"] == 900    # not 1600
        counter = store.get_counter("session-7")
        assert counter["last_cache_write_5m_tokens"] == 2000
        assert counter["last_cache_write_1h_tokens"] == 900

    def test_a_negative_split_is_refused(self, store):
        with pytest.raises(TelemetryValidationError, match="cache_write_5m_tokens"):
            store.record_usage("task-1", phase=PHASE_IMPLEMENTATION, cache_write_5m_tokens=-1)


class TestStaleContextReason:
    """Migration v3: a CHECK-set rebuild, which is the one genuinely
    dangerous migration in this store (DDL is not transactional, so a
    crash re-runs the rebuild over whatever state it left behind)."""

    def test_stale_context_is_accepted(self, store):
        result = store.record_reentry("task-1", reason=REENTRY_STALE_CONTEXT,
                                      idempotency_key="re-1", detail="cached prefix predates HEAD")
        assert result["task"]["reentry_count"] == 1
        assert store.list_reentries("task-1")[0]["reason"] == "STALE_CONTEXT"

    def test_stale_context_does_not_count_as_a_contract_gap(self, store):
        """The whole point of the value existing: a cache-coherence
        failure must not be charged to contract quality, because the
        analysis-heavy arm carries the larger cached prefix and would
        absorb the bias."""
        result = store.record_reentry("task-1", reason=REENTRY_STALE_CONTEXT,
                                      idempotency_key="re-1")
        assert result["task"]["contract_gap_count"] == 0
        assert result["task"]["reentry_count"] == 1

    def test_an_unknown_reason_is_still_refused_after_the_rebuild(self, store):
        """The rebuilt CHECK must still be a real constraint, not a
        table that silently accepts anything."""
        with pytest.raises(TelemetryValidationError, match="reason"):
            store.record_reentry("task-1", reason="STALE_VIBES", idempotency_key="re-1")
        with sqlite3.connect(store.path) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO telemetry_reentries (idempotency_key, task_id, reason, "
                    "occurred_at, recorded_at) VALUES ('x','t','STALE_VIBES','now','now')")

    def test_migrating_a_v2_database_keeps_its_reentry_rows(self, tmp_path):
        path = tmp_path / "work_telemetry.db"
        upto_v2 = [m for m in TELEMETRY_MIGRATIONS if m.version <= 2]
        with sqlite3.connect(path) as connection:
            apply_migrations(connection, upto_v2)
            for index in range(3):
                connection.execute(
                    "INSERT INTO telemetry_reentries (idempotency_key, task_id, reason, "
                    "occurred_at, recorded_at) VALUES (?,?,?,?,?)",
                    (f"re-{index}", "task-1", "TEST_FAILURE", "2026-09-01T00:00:00+00:00",
                     "2026-09-01T00:00:00+00:00"))

        upgraded = WorkTelemetryStore(path)

        rows = upgraded.list_reentries("task-1")
        assert [row["idempotency_key"] for row in rows] == ["re-0", "re-1", "re-2"]
        assert [row["id"] for row in rows] == [1, 2, 3]  # ids preserved, not renumbered
        # The pre-existing keys still dedupe across the rebuild.
        assert upgraded.record_reentry("task-1", reason=REENTRY_TEST_FAILURE,
                                       idempotency_key="re-0")["duplicate"] is True
        # And the new value now works on the upgraded database.
        upgraded.record_reentry("task-1", reason=REENTRY_STALE_CONTEXT, idempotency_key="re-new")
        assert len(upgraded.list_reentries("task-1")) == 4

    @pytest.mark.parametrize("crash_after", ["create", "copy", "drop", "rename"])
    def test_the_rebuild_survives_a_crash_at_every_step(self, tmp_path, crash_after):
        """Each crash point is re-entered by the retry with user_version
        still at 2. No row may be lost or duplicated at any of them."""
        path = tmp_path / "work_telemetry.db"
        upto_v2 = [m for m in TELEMETRY_MIGRATIONS if m.version <= 2]
        with sqlite3.connect(path) as connection:
            apply_migrations(connection, upto_v2)
            for index in range(3):
                connection.execute(
                    "INSERT INTO telemetry_reentries (idempotency_key, task_id, reason, "
                    "occurred_at, recorded_at) VALUES (?,?,?,?,?)",
                    (f"re-{index}", "task-1", "TEST_FAILURE", "2026-09-01T00:00:00+00:00",
                     "2026-09-01T00:00:00+00:00"))

        # Reproduce the partial state a crash at this step would leave.
        with sqlite3.connect(path) as connection:
            statements = {
                "create": 1, "copy": 2, "drop": 3, "rename": 4,
            }[crash_after]
            connection.execute("""
                CREATE TABLE IF NOT EXISTS telemetry_reentries_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idempotency_key TEXT NOT NULL UNIQUE, task_id TEXT NOT NULL, work_id TEXT,
                    reason TEXT NOT NULL, phase TEXT, occurred_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL, detail TEXT, evidence_source TEXT,
                    confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
                    CHECK (reason IN ('TEST_FAILURE','CONTRACT_GAP','IMPLEMENTATION_BUG',
                                      'ENVIRONMENT_FAILURE','USER_CHANGED_REQUIREMENT',
                                      'DELIVERY_FAILURE','MERGE_CONFLICT','STALE_CONTEXT','OTHER'))
                )""")
            if statements >= 2:
                connection.execute(
                    "INSERT OR IGNORE INTO telemetry_reentries_v3 (id, idempotency_key, task_id, "
                    "work_id, reason, phase, occurred_at, recorded_at, detail, evidence_source, "
                    "confidence) SELECT id, idempotency_key, task_id, work_id, reason, phase, "
                    "occurred_at, recorded_at, detail, evidence_source, confidence "
                    "FROM telemetry_reentries")
            if statements >= 3:
                connection.execute("DROP TABLE telemetry_reentries")
            if statements >= 4:
                connection.execute(
                    "ALTER TABLE telemetry_reentries_v3 RENAME TO telemetry_reentries")
            assert get_schema_version(connection) == 2  # the crash never stamped v3

        recovered = WorkTelemetryStore(path)  # the retry

        rows = recovered.list_reentries("task-1")
        assert [row["idempotency_key"] for row in rows] == ["re-0", "re-1", "re-2"], crash_after
        recovered.record_reentry("task-1", reason=REENTRY_STALE_CONTEXT, idempotency_key="re-new")
        assert len(recovered.list_reentries("task-1")) == 4
        with sqlite3.connect(path) as connection:
            assert get_schema_version(connection) == max(m.version for m in TELEMETRY_MIGRATIONS)
            leftovers = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "telemetry_reentries_v3" not in leftovers

    def test_an_already_migrated_database_skips_the_rebuild(self, store):
        """The inspection guard: re-running v3 against a table that
        already admits STALE_CONTEXT must not rebuild it again."""
        from terminal_mcp.work_telemetry_store import _add_v3_stale_context_reason
        store.record_reentry("task-1", reason=REENTRY_STALE_CONTEXT, idempotency_key="re-1")
        with sqlite3.connect(store.path) as connection:
            before = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='telemetry_reentries'").fetchone()[0]
            _add_v3_stale_context_reason(connection)
            after = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='telemetry_reentries'").fetchone()[0]
        assert before == after
        assert len(store.list_reentries("task-1")) == 1

    def test_the_rebuild_guard_is_not_fooled_by_a_comment(self):
        """Regression: the guard used to text-match the stored CREATE
        statement, which sqlite_master keeps verbatim -- comments and
        all. A comment merely NAMING the value made the guard believe the
        constraint already admitted it, so the rebuild was skipped and
        every STALE_CONTEXT write failed on a fresh database."""
        from terminal_mcp.work_telemetry_store import _reentry_check_admits

        connection = sqlite3.connect(":memory:")
        connection.execute("""
            CREATE TABLE telemetry_reentries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                task_id TEXT NOT NULL, reason TEXT NOT NULL,
                occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                -- a comment that mentions STALE_CONTEXT but does not admit it
                CHECK (reason IN ('TEST_FAILURE','OTHER'))
            )""")
        assert "STALE_CONTEXT" in connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='telemetry_reentries'").fetchone()[0]
        assert _reentry_check_admits(connection, "STALE_CONTEXT") is False
        assert _reentry_check_admits(connection, "TEST_FAILURE") is True
        # The probe leaves nothing behind.
        assert connection.execute("SELECT COUNT(*) FROM telemetry_reentries").fetchone()[0] == 0


class TestDocumentedContract:
    """The integration contract is what other lanes build against, so
    the parts of it that can drift silently are pinned here.

    Prompted by the benchmark lane's observation that a
    version-to-vocabulary table in a READER goes stale the moment a
    migration adds a reason. The same is true of the one in this
    project's own doc -- the difference is that this can be made to fail
    the build instead of quietly misinforming someone."""

    @staticmethod
    def _doc() -> str:
        path = pathlib.Path(__file__).resolve().parents[1] / "docs" / "WORK_EFFICIENCY_TELEMETRY.md"
        return path.read_text(encoding="utf-8")

    def test_every_reentry_reason_is_documented(self):
        doc = self._doc()
        missing = [reason for reason in REENTRY_REASONS if f"`{reason}`" not in doc]
        assert not missing, (
            f"re-entry reasons missing from the integration contract: {missing}. "
            f"Producers read that doc to decide what they may emit.")

    def test_the_version_vocabulary_table_covers_the_current_schema(self):
        """If a migration is added, the documented table must gain its
        row -- otherwise a read-only reader using the fallback silently
        reports an out-of-date vocabulary."""
        doc = self._doc()
        current = max(m.version for m in TELEMETRY_MIGRATIONS)
        assert f"| {current} |" in doc, (
            f"schema is at v{current} but the version-to-vocabulary table in "
            f"docs/WORK_EFFICIENCY_TELEMETRY.md does not have a row for it")

    def test_the_documented_schema_version_matches_the_code(self):
        doc = self._doc()
        current = max(m.version for m in TELEMETRY_MIGRATIONS)
        assert f"Schema version {current}" in doc, (
            f"doc does not state the current schema version (v{current})")
