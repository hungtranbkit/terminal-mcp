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

import sqlite3

import pytest

from terminal_mcp.queue_store import QueueStore
from terminal_mcp.schema import Migration, apply_migrations, get_schema_version
from terminal_mcp.work_telemetry_store import (
    CONFIDENCE_ESTIMATED, CONFIDENCE_MEASURED, FIRST_PASS_FALSE, FIRST_PASS_TRUE,
    FIRST_PASS_UNKNOWN, KIND_CUMULATIVE, KIND_DELTA, PHASE_ANALYSIS, PHASE_CONTRACT,
    PHASE_IMPLEMENTATION, REENTRY_CONTRACT_GAP, REENTRY_TEST_FAILURE, STATUS_CANCELLED,
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
