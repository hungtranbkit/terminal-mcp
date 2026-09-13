"""Telemetry. The single rule: never fabricate precision."""

from __future__ import annotations

import pytest

from terminal_mcp import work_telemetry as wt


@pytest.fixture()
def store(tmp_path):
    store = wt.TelemetryStore(tmp_path / "telemetry.db")
    yield store
    store.close()


# -- provenance --------------------------------------------------------------

def test_a_reported_count_is_exact_and_says_who_reported_it():
    count = wt.TokenCount.reported(18400, by="provider api")
    assert (count.value, count.source) == (18400, wt.EXACT)
    assert "provider api" in count.method
    assert count.display() == "18,400"


def test_an_estimate_is_labelled_wherever_it_is_displayed():
    count = wt.TokenCount.estimate_from_text("x" * 4000)
    assert count.source == wt.ESTIMATED
    assert "estimated" in count.display()
    assert count.method                      # says how it was derived


def test_a_missing_count_is_none_not_zero():
    count = wt.TokenCount.unavailable()
    # Zero would be read as "this task was free", which is a different claim.
    assert count.value is None
    assert count.source == wt.UNAVAILABLE
    assert "unavailable" in count.display()


def test_a_new_record_starts_with_no_token_claim():
    assert wt.TaskTelemetry().tokens.source == wt.UNAVAILABLE


# -- counters are counted ----------------------------------------------------

def test_counters_accumulate():
    record = wt.TaskTelemetry(task_id="t1")
    record.record_files(3)
    record.record_files()
    record.record_search(2)
    record.record_runbook(hit=True, cached=True)
    record.record_runbook(hit=False)
    record.record_redefine()
    record.record_assist()
    assert (record.files_read, record.search_rounds) == (4, 2)
    assert (record.runbook_hits, record.runbook_misses, record.cache_hits) == (1, 1, 1)
    assert (record.redefine_count, record.assist_requests) == (1, 1)


def test_time_to_preview_records_the_first_one_only():
    record = wt.TaskTelemetry(started_at="2026-01-01T00:00:00+00:00")
    record.mark_preview("2026-01-01T00:01:00+00:00")
    record.mark_preview("2026-01-01T00:09:00+00:00")
    assert record.seconds_to_preview() == 60


def test_a_task_that_never_previewed_reports_none_not_zero():
    record = wt.TaskTelemetry()
    # Zero seconds would read as "previewed instantly".
    assert record.seconds_to_preview() is None
    assert "to_preview=n/a" in record.one_line()


def test_duration_is_none_until_the_task_finishes():
    record = wt.TaskTelemetry(started_at="2026-01-01T00:00:00+00:00")
    assert record.duration_seconds() is None
    record.finish("COMPLETED", when="2026-01-01T00:02:00+00:00")
    assert record.duration_seconds() == 120
    assert record.outcome == "COMPLETED"


def test_the_one_line_summary_shows_missing_tokens_plainly():
    assert "tokens=unavailable" in wt.TaskTelemetry(task_id="t1").one_line()


# -- aggregation does not launder provenance ---------------------------------

def _with_tokens(count):
    record = wt.TaskTelemetry()
    record.tokens = count
    return record


def test_a_total_over_complete_exact_inputs_is_exact():
    summary = wt.summarise([_with_tokens(wt.TokenCount.reported(100, by="p")),
                            _with_tokens(wt.TokenCount.reported(50, by="p"))])
    assert summary["tokens"] == {**summary["tokens"], "value": 150,
                                 "source": wt.EXACT, "complete": True}


def test_a_total_missing_some_inputs_is_partial_not_exact():
    summary = wt.summarise([_with_tokens(wt.TokenCount.reported(100, by="p")),
                            wt.TaskTelemetry()])
    tokens = summary["tokens"]
    # The arithmetic is exact; the total is not the fleet's usage.
    assert tokens["source"] == wt.PARTIAL
    assert tokens["complete"] is False
    assert tokens["missing_tasks"] == 1
    assert "reported nothing" in tokens["method"]


def test_any_estimate_makes_the_whole_total_an_estimate():
    summary = wt.summarise([_with_tokens(wt.TokenCount.reported(100, by="p")),
                            _with_tokens(wt.TokenCount.estimate_from_text("x" * 400))])
    assert summary["tokens"]["source"] == wt.ESTIMATED


def test_a_total_with_no_inputs_at_all_is_unavailable():
    summary = wt.summarise([wt.TaskTelemetry(), wt.TaskTelemetry()])
    assert summary["tokens"]["value"] is None
    assert summary["tokens"]["source"] == wt.UNAVAILABLE
    assert "none of 2 tasks" in summary["tokens"]["method"]


def test_an_empty_history_says_so_instead_of_reporting_zeroes():
    summary = wt.summarise([])
    assert summary["tasks"] == 0
    assert "no telemetry" in summary["note"]


def test_the_hit_rate_is_none_when_nothing_was_attempted():
    assert wt.summarise([wt.TaskTelemetry()])["runbook_hit_rate"] is None


def test_tasks_without_a_preview_are_counted_not_dropped():
    previewed = wt.TaskTelemetry(started_at="2026-01-01T00:00:00+00:00")
    previewed.mark_preview("2026-01-01T00:00:30+00:00")
    summary = wt.summarise([previewed, wt.TaskTelemetry()])
    assert summary["tasks_without_preview"] == 1
    assert summary["median_seconds_to_preview"] == 30


# -- targets -----------------------------------------------------------------

def test_targets_report_unknown_rather_than_assuming_success():
    verdict = wt.against_targets(wt.summarise([wt.TaskTelemetry()]))
    assert verdict["runbook_hit_rate"]["status"] == "UNKNOWN"
    assert verdict["tokens"]["status"] == "UNKNOWN"


def test_targets_flag_a_partial_token_total_rather_than_grading_it():
    summary = wt.summarise([_with_tokens(wt.TokenCount.reported(100, by="p")),
                            wt.TaskTelemetry()])
    assert wt.against_targets(summary)["tokens"]["status"] == "PARTIAL"


def test_targets_compare_real_aggregates():
    records = []
    for _ in range(8):
        record = wt.TaskTelemetry()
        record.record_runbook(hit=True)
        records.append(record)
    for _ in range(2):
        record = wt.TaskTelemetry()
        record.record_runbook(hit=False)
        records.append(record)
    verdict = wt.against_targets(wt.summarise(records))
    assert verdict["runbook_hit_rate"]["status"] == "MEETS"
    assert verdict["runbook_hit_rate"]["actual"] == 0.8


def test_a_below_target_hit_rate_is_reported_as_below():
    records = [wt.TaskTelemetry() for _ in range(2)]
    records[0].record_runbook(hit=True)
    records[1].record_runbook(hit=False)
    assert wt.against_targets(wt.summarise(records))["runbook_hit_rate"]["status"] == "BELOW"


# -- persistence -------------------------------------------------------------

def test_records_round_trip(store):
    record = wt.TaskTelemetry(task_id="t1", work_id="w1", module="work_ui")
    record.record_files(3)
    record.tokens = wt.TokenCount.reported(900, by="provider")
    store.save(record)
    loaded = store.get(record.telemetry_id)
    assert loaded["files_read"] == 3
    assert loaded["tokens"] == {"value": 900, "source": wt.EXACT,
                                "method": "reported by provider"}


def test_saving_twice_updates_rather_than_duplicates(store):
    record = wt.TaskTelemetry(task_id="t1", work_id="w1")
    store.save(record)
    record.record_files(5)
    record.finish("COMPLETED")
    store.save(record)
    rows = store.for_work("w1")
    assert len(rows) == 1 and rows[0]["files_read"] == 5


def test_the_store_summary_carries_targets(store):
    record = wt.TaskTelemetry(task_id="t1", project_id="p1")
    record.record_runbook(hit=True)
    store.save(record)
    summary = store.summary(project_id="p1")
    assert summary["tasks"] == 1
    assert "targets" in summary
