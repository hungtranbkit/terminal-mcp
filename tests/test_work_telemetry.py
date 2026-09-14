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


# -- provider usage: the counters a runtime reported, and only those ----------

def test_usage_records_only_the_counters_that_were_reported():
    usage = wt.ProviderUsage.from_report({"input_tokens": 100, "output_tokens": 20},
                                         provider="runtime")
    assert usage.available() is True
    assert usage.get("input_tokens").value == 100
    # Nothing was said about the cache, so nothing is claimed about it.
    missing = usage.get("cache_read_tokens")
    assert missing.value is None and missing.source == wt.UNAVAILABLE
    assert "cache_read_tokens" in usage.unreported()


def test_usage_from_nothing_is_unavailable_not_empty_success():
    assert wt.ProviderUsage.from_report({}).available() is False
    assert wt.ProviderUsage.from_report(None).available() is False
    assert wt.ProviderUsage.from_report("18000 tokens").available() is False


def test_a_booleans_worth_of_tokens_is_not_a_token_count():
    """`True` is an int in Python. It is not a measurement."""
    usage = wt.ProviderUsage.from_report({"input_tokens": True, "output_tokens": 4})
    assert "input_tokens" not in usage.counters
    assert "input_tokens" in usage.ignored


def test_provider_synonyms_map_to_one_counter_but_unknown_names_never_do():
    usage = wt.ProviderUsage.from_report({"prompt_tokens": 9, "completion_tokens": 3,
                                          "thinking_tokens": 5}, provider="other")
    assert usage.get("input_tokens").value == 9
    assert usage.get("output_tokens").value == 3
    # An unmapped name is kept visible rather than folded into a counter it
    # might not belong to.
    assert usage.ignored == ("thinking_tokens",)


def test_a_partial_usage_total_across_tasks_says_how_many_reported():
    rows = [wt.TaskTelemetry(task_id="a"), wt.TaskTelemetry(task_id="b")]
    rows[0].record_provider_usage({"input_tokens": 10}, provider="runtime")
    summary = wt.summarise(rows)
    counters = summary["provider_usage"]["counters"]
    assert summary["provider_usage"]["reporting_tasks"] == 1
    assert counters["input_tokens"]["source"] == wt.PARTIAL
    assert counters["input_tokens"]["missing_tasks"] == 1
    assert counters["output_tokens"]["source"] == wt.UNAVAILABLE


def test_no_task_reporting_usage_is_reported_as_exactly_that():
    summary = wt.summarise([wt.TaskTelemetry(task_id="a")])
    assert summary["provider_usage"]["available"] is False
    assert "none of 1 task(s)" in summary["provider_usage"]["note"]


# -- first-pass success is three-valued --------------------------------------

def test_an_unfinished_task_has_no_first_pass_verdict():
    record = wt.TaskTelemetry(task_id="t")
    record.mark_dispatched()
    assert record.first_pass_success is None


def test_a_row_with_no_observed_dispatch_cannot_claim_a_first_pass():
    """A worker-reported row says nothing about how many dispatches it took,
    and a gap in instrumentation must not become a success statistic."""
    record = wt.TaskTelemetry(task_id="t")
    record.finish("COMPLETED")
    assert record.first_pass_success is None
    assert "not knowable" in record.first_pass_basis


def test_unknown_first_pass_rows_are_excluded_from_the_rate_not_counted_as_failures():
    good = wt.TaskTelemetry(task_id="a")
    good.mark_dispatched()
    good.finish("COMPLETED")
    summary = wt.summarise([good, wt.TaskTelemetry(task_id="b")])
    assert summary["first_pass_rate"] == 1.0
    assert (summary["first_pass_judged"], summary["first_pass_unknown"]) == (1, 1)


# -- aggregation: per task, per module, per period ---------------------------

def _row(**kwargs):
    record = wt.TaskTelemetry(**kwargs)
    return record


def test_aggregation_groups_by_module():
    rows = [_row(task_id="a", module="ui", started_at="2026-09-01T10:00:00+00:00"),
            _row(task_id="b", module="ui", started_at="2026-09-01T11:00:00+00:00"),
            _row(task_id="c", module="queue", started_at="2026-09-02T10:00:00+00:00")]
    for row in rows:
        row.record_files(2)
    result = wt.aggregate(rows, by="module")
    keys = {group["key"]: group["summary"]["tasks"] for group in result["groups"]}
    assert keys == {"ui": 2, "queue": 1}
    assert result["overall"]["files_read"] == 6


def test_a_row_with_no_module_is_named_not_bucketed_as_other():
    rows = [_row(task_id="a", module="ui"), _row(task_id="b")]
    result = wt.aggregate(rows, by="module")
    assert result["ungrouped_tasks"] == 1
    assert result["ungrouped_ids"] == ["b"]
    assert [g["key"] for g in result["groups"]] == ["ui"]


def test_aggregation_by_time_period():
    rows = [_row(task_id="a", started_at="2026-09-01T10:00:00+00:00"),
            _row(task_id="b", started_at="2026-09-01T23:00:00+00:00"),
            _row(task_id="c", started_at="2026-09-08T10:00:00+00:00")]
    days = wt.aggregate(rows, by=wt.PERIOD_DAY)
    assert [g["key"] for g in days["groups"]] == ["2026-09-01", "2026-09-08"]
    weeks = wt.aggregate(rows, by=wt.PERIOD_WEEK)
    assert [g["key"] for g in weeks["groups"]] == ["2026-W36", "2026-W37"]
    months = wt.aggregate(rows, by=wt.PERIOD_MONTH)
    assert [g["key"] for g in months["groups"]] == ["2026-09"]


def test_an_unreadable_timestamp_belongs_to_no_period():
    """Putting it in today's would quietly move work between windows."""
    assert wt.period_key("last tuesday", wt.PERIOD_DAY) is None
    result = wt.aggregate([_row(task_id="a", started_at="whenever")], by=wt.PERIOD_DAY)
    assert result["ungrouped_tasks"] == 1 and result["groups"] == []


def test_an_unknown_grouping_is_refused():
    assert wt.aggregate([], by="mood")["error"] == "UNKNOWN_GROUPING"


def test_aggregation_per_task_is_available_too():
    rows = [_row(task_id="a"), _row(task_id="b")]
    assert {g["key"] for g in wt.aggregate(rows, by="task")["groups"]} == {"a", "b"}


# -- baseline and savings ----------------------------------------------------

def test_without_a_baseline_no_saving_is_shown_at_all():
    summary = wt.summarise([_row(task_id="a")])
    result = wt.savings(summary, wt.Baseline.unavailable("nothing measured yet"))
    assert result["available"] is False
    assert "nothing measured yet" in result["reason"]
    assert "metrics" not in result


def test_a_measured_baseline_carries_its_window_and_task_count():
    rows = [_row(task_id="a"), _row(task_id="b"), _row(task_id="c")]
    for row in rows:
        row.record_files(10)
    baseline = wt.measure_baseline(rows, definition="the three tasks before the change",
                                   window=("2026-08-01", "2026-09-01"), min_tasks=3)
    assert baseline.source == wt.BASELINE_MEASURED
    assert baseline.metrics["files_read_per_task"] == 10.0
    payload = baseline.as_dict()
    assert payload["tasks"] == 3 and payload["window"]["since"] == "2026-08-01"


def test_too_few_rows_is_not_a_baseline():
    baseline = wt.measure_baseline([_row(task_id="a")], definition="one task",
                                   min_tasks=3)
    assert baseline.available() is False
    assert "at least 3" in baseline.note


def test_a_supplied_baseline_without_a_stated_definition_is_refused():
    baseline = wt.stated_baseline({"files_read_per_task": 20}, definition="  ")
    assert baseline.available() is False
    assert "how it was arrived at" in baseline.note


def test_a_supplied_baseline_with_a_definition_is_admissible():
    baseline = wt.stated_baseline(
        {"files_read_per_task": 20, "made_up_metric": 3},
        definition="hand-counted over 12 tasks in August, recorded in the handoff")
    assert baseline.source == wt.BASELINE_STATED
    assert baseline.metrics == {"files_read_per_task": 20}      # unknown metric dropped


def test_every_saving_is_labelled_derived_and_points_the_right_way():
    before = [_row(task_id=f"b{i}") for i in range(3)]
    for row in before:
        row.record_files(10)
        row.record_runbook(hit=False)
    baseline = wt.measure_baseline(before, definition="before the change", min_tasks=3)

    after = [_row(task_id="a")]
    after[0].record_files(4)
    after[0].record_runbook(hit=True)
    result = wt.savings(wt.summarise(after), baseline)

    files = result["metrics"]["files_read_per_task"]
    assert files["status"] == "IMPROVED"        # fewer files is better
    assert files["change"] == -6.0 and files["percent_change"] == -60.0
    assert files["source"] == wt.ESTIMATED      # derived, never presented as measured
    assert "derived" in files["method"]
    # ...and for a rate, MORE is the improvement.
    assert result["metrics"]["runbook_hit_rate"]["status"] == "IMPROVED"


def test_a_metric_missing_on_either_side_is_unknown_rather_than_zero():
    baseline = wt.stated_baseline({"files_read_per_task": 5},
                                  definition="stated in the handoff")
    result = wt.savings(wt.summarise([_row(task_id="a")]), baseline)
    assert result["metrics"]["first_pass_rate"]["status"] == "UNKNOWN"


# -- store: querying the three axes savings are read along -------------------

def test_the_store_queries_a_half_open_time_window(store):
    for task_id, started in (("a", "2026-09-01T00:00:00+00:00"),
                             ("b", "2026-09-02T00:00:00+00:00"),
                             ("c", "2026-09-03T00:00:00+00:00")):
        store.save(wt.TaskTelemetry(task_id=task_id, module="ui", started_at=started))
    window = store.query(since="2026-09-02T00:00:00+00:00",
                         until="2026-09-03T00:00:00+00:00")
    # Half-open, so two adjacent windows can never both contain task b.
    assert [row["task_id"] for row in window] == ["b"]


def test_the_store_aggregates_per_module(store):
    store.save(wt.TaskTelemetry(task_id="a", module="ui"))
    store.save(wt.TaskTelemetry(task_id="b", module="queue"))
    grouped = store.aggregate(by="module")
    assert {g["key"] for g in grouped["groups"]} == {"ui", "queue"}


def test_a_report_with_no_baseline_still_reports_the_window_and_says_why(store):
    store.save(wt.TaskTelemetry(task_id="a", module="ui"))
    report = store.report(by="module")
    assert report["tasks"] == 1
    assert report["savings"]["available"] is False
    assert "no baseline" in report["savings"]["reason"]


def test_a_report_measures_its_baseline_from_an_earlier_window(store):
    for index in range(3):
        row = wt.TaskTelemetry(task_id=f"old{index}", module="ui",
                               started_at=f"2026-08-0{index + 1}T00:00:00+00:00")
        row.record_files(10)
        store.save(row)
    recent = wt.TaskTelemetry(task_id="new", module="ui",
                              started_at="2026-09-01T00:00:00+00:00")
    recent.record_files(4)
    store.save(recent)

    report = store.report(module="ui", since="2026-09-01T00:00:00+00:00",
                          baseline_window=("2026-08-01T00:00:00+00:00",
                                           "2026-09-01T00:00:00+00:00"),
                          baseline_min_tasks=3)
    assert report["savings"]["available"] is True
    assert report["savings"]["baseline"]["source"] == wt.BASELINE_MEASURED
    assert report["savings"]["metrics"]["files_read_per_task"]["change"] == -6.0


def test_a_stored_row_can_be_reopened_and_continued(store):
    record = wt.TaskTelemetry(task_id="t1", module="ui")
    record.mark_dispatched(lane="demo-work")
    record.record_provider_usage({"total_tokens": 50}, provider="runtime")
    store.save(record)

    reloaded = wt.TaskTelemetry.from_dict(store.for_task("t1"))
    assert reloaded.telemetry_id == record.telemetry_id
    assert reloaded.dispatch_count == 1 and reloaded.lane == "demo-work"
    assert reloaded.usage.get("total_tokens").value == 50
    reloaded.finish("COMPLETED")
    store.save(reloaded)
    assert len(store.query(task_id="t1")) == 1
