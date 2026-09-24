"""Telemetry filled by the runtime, not by a worker's memory of its own task.

Two properties are defended here, because they are what make an efficiency
number worth acting on:

  A row exists because the QUEUE dispatched a task, and it closes because the
  QUEUE reached a terminal state. Neither depends on a worker remembering to
  report, so a crashed or forgetful task is still in the record.

  A number that nobody measured is absent, and says why. Provider usage stays
  UNAVAILABLE until something that actually has those counters reports them,
  a derived figure is labelled an estimate, and a saving with no admissible
  baseline is not shown at all.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_telemetry as wt
from terminal_mcp import work_telemetry_runtime as wtr
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture()
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


@pytest.fixture()
def telemetry(tmp_path):
    store = wt.TelemetryStore(tmp_path / "telemetry.db")
    yield store
    store.close()


@pytest.fixture()
def wired(queue, telemetry):
    """A recorder on a real QueueStore's real event hook."""
    recorder = wtr.install(queue_store=queue.store, telemetry_store=telemetry)
    return queue, recorder


def _queued_task(queue, *, session="demo-work", prompt="do the thing"):
    result = queue.enqueue(session, prompt, title="a task")
    assert not result.get("error"), result
    return result["task_id"]


def _dispatch(queue, task_id):
    """The real path a task takes to DISPATCHING, through the real store."""
    queue.store.transition_task(task_id, "PRECHECK", event_type="CLAIMED")
    queue.store.transition_task(task_id, "READY", event_type="COORDINATOR_READY")
    queue.store.transition_task(task_id, "DISPATCHING", event_type="DISPATCHED")


# -- the row opens because the runtime dispatched -----------------------------

def test_dispatching_a_task_opens_its_row_without_anyone_reporting(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)

    row = telemetry.for_task(task_id)
    assert row is not None
    assert row["task_id"] == task_id
    assert row["lane"] == "demo-work"
    assert row["dispatch_count"] == 1
    # Nothing has finished, so nothing claims to have.
    assert row["finished_at"] is None and row["first_pass_success"] is None


def test_a_transition_for_a_task_never_seen_dispatched_opens_nothing(wired, telemetry):
    """A row whose start time is 'whenever this process attached' would make
    every duration in the table wrong, so none is opened."""
    queue, _ = wired
    task_id = _queued_task(queue)
    queue.store.transition_task(task_id, "CANCELLED", event_type="CANCELLED")
    assert telemetry.for_task(task_id) is None


def test_queue_events_that_say_nothing_about_cost_are_ignored(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    assert recorder.on_queue_event(
        {"task_id": task_id, "event_type": "PROGRESS", "to_status": None}) is None


# -- preview and finish come from real transitions ----------------------------

def test_first_preview_is_a_real_transition_and_says_which_one(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    queue.store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    queue.store.transition_task(task_id, "VERIFYING", event_type="VERIFY_REQUESTED")

    row = telemetry.for_task(task_id)
    assert row["first_preview_at"] is not None
    assert row["running_at"] is not None
    # The basis is recorded on the row: a preview time nobody can trace back
    # to a transition is a number nobody can check.
    assert "VERIFYING" in row["preview_basis"]
    assert row["time_to_preview_seconds"] is not None


def test_only_the_first_preview_counts(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    queue.store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    queue.store.transition_task(task_id, "VERIFYING", event_type="VERIFY_REQUESTED")
    first = telemetry.for_task(task_id)["first_preview_at"]
    # A false alarm sends it back to RUNNING and then to VERIFYING again.
    queue.store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    queue.store.transition_task(task_id, "VERIFYING", event_type="VERIFY_REQUESTED")
    assert telemetry.for_task(task_id)["first_preview_at"] == first


def test_completion_closes_the_row_and_records_a_first_pass_success(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    queue.store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    queue.store.transition_task(task_id, "VERIFYING", event_type="VERIFY_REQUESTED")
    queue.store.transition_task(task_id, "COMPLETED", event_type="COMPLETED")

    row = telemetry.for_task(task_id)
    assert row["outcome"] == "COMPLETED"
    assert row["finished_at"] is not None
    assert row["first_pass_success"] is True
    assert "one dispatch" in row["first_pass_basis"]


def test_a_retry_is_the_same_row_and_is_not_a_first_pass_success(wired, telemetry):
    """A second row would turn one task that went wrong into two that look
    cheap, and would make the retry invisible."""
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    queue.store.transition_task(task_id, "FAILED", event_type="FAILED",
                                reason="send failed")
    queue.store.transition_task(task_id, "QUEUED", event_type="RETRY")
    _dispatch(queue, task_id)
    queue.store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    queue.store.transition_task(task_id, "VERIFYING", event_type="VERIFY_REQUESTED")
    queue.store.transition_task(task_id, "COMPLETED", event_type="COMPLETED")

    rows = telemetry.query(task_id=task_id)
    assert len(rows) == 1
    row = rows[0]
    assert row["dispatch_count"] == 2 and row["reopened"] == 1
    assert row["failed_excursions"] == 1
    assert row["first_pass_success"] is False
    assert "2 dispatches" in row["first_pass_basis"]


def test_a_blocked_task_is_finished_as_blocked_not_as_done(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    queue.store.transition_task(task_id, "BLOCKED", event_type="BLOCKED",
                                reason="needs a human")
    row = telemetry.for_task(task_id)
    assert row["outcome"] == "BLOCKED"
    assert row["first_pass_success"] is False


# -- attaching never displaces what is already on the hook --------------------

def test_attaching_telemetry_keeps_the_existing_event_sink(queue, telemetry):
    seen: list[dict] = []
    queue.store._event_sink = seen.append
    wtr.install(queue_store=queue.store, telemetry_store=telemetry)

    task_id = _queued_task(queue)
    _dispatch(queue, task_id)

    assert telemetry.for_task(task_id) is not None      # telemetry recorded
    assert any(e.get("to_status") == "DISPATCHING" for e in seen)  # bus still fed


def test_a_telemetry_failure_never_disturbs_the_transition(queue, telemetry):
    class Exploding:
        def for_task(self, task_id):
            raise RuntimeError("telemetry database is on fire")

        def save(self, record):
            raise RuntimeError("telemetry database is on fire")

    wtr.attach(queue.store, wtr.RuntimeTelemetry(Exploding()))
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)     # must not raise
    assert queue.store.get_task(task_id).status == "DISPATCHING"


# -- attributes: what IS known, never what is guessed -------------------------

def test_the_row_carries_the_spec_facts_when_a_spec_exists(queue, telemetry, tmp_path):
    from terminal_mcp.work_spec import WorkSpec, WorkSpecStore

    specs = WorkSpecStore(tmp_path / "specs.db")
    task_id = _queued_task(queue)
    specs.save(WorkSpec(spec_id="spec_1", title="fix the thing", task_type="BUG",
                        queue_task_id=task_id, likely_module="work_ui",
                        execution_mode="FAST_FIX", difficulty="EASY",
                        hypothesis="an off-by-one", likely_files=("a.py",),
                        redefine_count=2))
    wtr.install(queue_store=queue.store, telemetry_store=telemetry, spec_store=specs)
    _dispatch(queue, task_id)

    row = telemetry.for_task(task_id)
    assert row["module"] == "work_ui"
    assert row["execution_mode"] == "FAST_FIX"
    assert row["difficulty"] == "EASY"
    assert row["spec_level"].startswith("L1")
    # The planner's own redefine count, not a number this module invented.
    assert row["redefine_count"] == 2


def test_a_task_with_no_spec_gets_no_module_rather_than_a_guessed_one(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    row = telemetry.for_task(task_id)
    assert row["module"] is None and row["difficulty"] is None


def test_the_work_id_comes_off_the_queue_rows_own_metadata(queue, telemetry):
    result = queue.enqueue("demo-work", "a planned task", title="t",
                           metadata={"work_id": "work_42"})
    wtr.install(queue_store=queue.store, telemetry_store=telemetry)
    _dispatch(queue, result["task_id"])
    assert telemetry.for_task(result["task_id"])["work_id"] == "work_42"


def test_a_broken_attribute_source_is_noted_and_the_row_still_opens(queue, telemetry):
    def exploding(task_id):
        raise RuntimeError("spec store unavailable")

    recorder = wtr.RuntimeTelemetry(telemetry, attributes=exploding)
    wtr.attach(queue.store, recorder)
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    row = telemetry.for_task(task_id)
    assert row is not None
    assert any("attributes unavailable" in note for note in row["notes"])


# -- counters from the real retrieval call sites ------------------------------

def test_a_signal_lands_on_the_open_row(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    recorder.note(task_id, "files_read", 4, source="test")
    recorder.note(task_id, "search_calls", 2, source="test")

    row = telemetry.for_task(task_id)
    assert row["files_read"] == 4
    # The contract's name and the stored name are the same number.
    assert row["search_calls"] == row["search_rounds"] == 2
    assert "test" in row["signal_sources"]


def test_a_signal_before_the_row_exists_is_kept_not_dropped(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    recorder.note(task_id, "context_pack_hits", 1, source="early")
    _dispatch(queue, task_id)
    row = telemetry.for_task(task_id)
    assert row["context_pack_hits"] == 1
    assert "early" in row["signal_sources"]


def test_an_unknown_signal_is_refused_rather_than_inventing_a_counter(wired):
    _, recorder = wired
    assert recorder.note("t1", "vibes", 3) is False


def test_the_ambient_signal_does_nothing_when_nothing_is_listening():
    assert wtr.note("files_read", 2) is False


def test_the_ambient_signal_reaches_the_active_recorder(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    with wtr.observing(recorder, task_id):
        wtr.note("knowledge_hits", 3, source="unit")
    assert telemetry.for_task(task_id)["knowledge_hits"] == 3
    # ...and stops reaching it once the block is over.
    wtr.note("knowledge_hits", 5)
    assert telemetry.for_task(task_id)["knowledge_hits"] == 3


def test_a_real_context_pack_build_counts_itself(wired, telemetry, tmp_path):
    """The instrumentation lives at the retrieval, so the count is what
    happened rather than what a worker remembers happening."""
    from terminal_mcp.context_pack import build_context_pack
    from terminal_mcp.project_knowledge import ProjectKnowledge

    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)

    knowledge = ProjectKnowledge(tmp_path)
    knowledge.record_module("work_ui", paths=["a.py"], summary="the UI")
    with wtr.observing(recorder, task_id):
        pack = build_context_pack("work_ui", knowledge=knowledge)
    assert pack.summary == "the UI"
    row = telemetry.for_task(task_id)
    assert row["context_pack_hits"] == 1
    assert row["knowledge_hits"] == 1
    assert any("context_pack" in source for source in row["signal_sources"])


def test_a_real_runbook_call_counts_as_a_hit_and_a_miss_as_a_miss(wired, telemetry,
                                                                  tmp_path):
    from terminal_mcp.procedures import ProcedureRegistry
    from terminal_mcp.project_knowledge import ProjectKnowledge

    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)

    registry = ProcedureRegistry(ProjectKnowledge(tmp_path))
    with wtr.observing(recorder, task_id):
        registry.run("nothing_registered_by_this_name")
    assert telemetry.for_task(task_id)["runbook_misses"] == 1


# -- provider usage: only what the runtime reported ---------------------------

def test_usage_is_unavailable_until_something_actually_reports_it(wired, telemetry):
    queue, _ = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    usage = telemetry.for_task(task_id)["usage"]
    assert usage["available"] is False
    assert usage["total"]["source"] == wt.UNAVAILABLE
    assert usage["total"]["value"] is None      # not zero: zero is a claim


def test_reported_counters_are_recorded_with_the_provider_that_reported_them(wired,
                                                                             telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    result = recorder.record_provider_usage(
        task_id, {"input_tokens": 1200, "output_tokens": 300,
                  "cache_read_input_tokens": 9000},
        provider="claude-code")
    assert result["recorded"] is True

    usage = telemetry.for_task(task_id)["usage"]
    assert usage["counters"]["input_tokens"] == {
        "value": 1200, "source": wt.EXACT, "method": "reported by claude-code"}
    assert usage["counters"]["cache_read_tokens"]["value"] == 9000
    assert "total_tokens" in usage["unreported"]


def test_a_total_nobody_reported_is_derived_and_labelled_an_estimate(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    recorder.record_provider_usage(task_id, {"input_tokens": 10, "output_tokens": 5},
                                   provider="runtime")
    total = telemetry.for_task(task_id)["usage"]["total"]
    assert total["value"] == 15
    assert total["source"] == wt.ESTIMATED        # exact arithmetic, derived figure
    assert "derived" in total["method"]


def test_a_reported_total_is_exact_and_becomes_the_headline_figure(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    recorder.record_provider_usage(task_id, {"total_tokens": 4321}, provider="runtime")
    row = telemetry.for_task(task_id)
    assert row["usage"]["total"]["source"] == wt.EXACT
    assert row["tokens"] == {"value": 4321, "source": wt.EXACT,
                             "method": "reported by runtime"}


def test_an_unreadable_counter_is_ignored_and_named_never_read_as_zero(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    recorder.record_provider_usage(
        task_id, {"input_tokens": "lots", "output_tokens": 7, "made_up": 3},
        provider="runtime")
    usage = telemetry.for_task(task_id)["usage"]
    assert "input_tokens" not in usage["counters"]
    assert set(usage["ignored"]) == {"input_tokens", "made_up"}
    assert usage["counters"]["output_tokens"]["value"] == 7


def test_a_later_empty_report_does_not_erase_a_real_one(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    recorder.record_provider_usage(task_id, {"total_tokens": 100}, provider="runtime")
    recorder.record_provider_usage(task_id, {}, provider="runtime")
    assert telemetry.for_task(task_id)["usage"]["total"]["value"] == 100


def test_usage_for_a_task_with_no_row_is_refused_rather_than_invented(wired):
    _, recorder = wired
    assert recorder.record_provider_usage("nope", {"total_tokens": 5})["recorded"] is False


# -- a truer preview signal, when a caller has one ----------------------------

def test_a_caller_with_a_real_preview_must_state_its_basis(wired, telemetry):
    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    assert recorder.mark_preview(task_id, basis="   ") is False
    assert recorder.mark_preview(task_id, basis="deploy preview URL returned") is True
    assert telemetry.for_task(task_id)["preview_basis"] == "deploy preview URL returned"


# -- the work runtime's own wiring --------------------------------------------

def test_work_service_enables_telemetry_on_its_own_queue(tmp_path, telemetry):
    from terminal_mcp.work_service import WorkService
    from terminal_mcp.work_store import WorkStore

    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    service = WorkService(WorkStore(tmp_path / "work.db"), queue=queue)
    assert service.telemetry_for_run("nope")["error"] == "UNKNOWN_WORK"

    assert service.enable_telemetry(telemetry_store=telemetry) == {"enabled": True,
                                                                   "already": False}
    created = service.create(title="ship it", goal="g", lane="demo-work",
                             tasks=[{"prompt": "do the thing", "title": "t"}])
    work_id = created["work"]["work_id"]
    task_id = created["tasks"][0]["queue_task_id"]
    _dispatch(queue, task_id)
    queue.store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    queue.store.transition_task(task_id, "VERIFYING", event_type="VERIFY_REQUESTED")
    queue.store.transition_task(task_id, "COMPLETED", event_type="COMPLETED")

    report = service.telemetry_for_run(work_id)
    assert report["summary"]["tasks"] == 1
    assert report["summary"]["first_pass_rate"] == 1.0
    assert report["tasks_without_telemetry"] == 0
    assert report["tasks"][0]["work_id"] == work_id


def test_a_run_without_telemetry_says_so_rather_than_reporting_zero(tmp_path):
    from terminal_mcp.work_service import WorkService
    from terminal_mcp.work_store import WorkStore

    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    service = WorkService(WorkStore(tmp_path / "work.db"), queue=queue)
    created = service.create(title="t", goal="g", lane="demo-work",
                             tasks=[{"prompt": "p", "title": "t"}])
    answer = service.telemetry_for_run(created["work"]["work_id"])
    assert answer["error"] == "TELEMETRY_NOT_ENABLED"


def test_enabling_telemetry_without_a_queue_is_refused(tmp_path):
    from terminal_mcp.work_service import WorkService
    from terminal_mcp.work_store import WorkStore

    service = WorkService(WorkStore(tmp_path / "work.db"))
    assert service.enable_telemetry()["error"] == "QUEUE_UNAVAILABLE"


def test_attaching_the_same_recorder_twice_does_not_double_count(queue, telemetry):
    """Two hooks for one recorder would count two dispatches where there was
    one -- the exact kind of quietly doubled number this module exists to
    prevent."""
    recorder = wtr.install(queue_store=queue.store, telemetry_store=telemetry)
    wtr.attach(queue.store, recorder)
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)
    assert telemetry.for_task(task_id)["dispatch_count"] == 1


def test_a_real_repo_read_and_search_count_themselves(wired, telemetry, tmp_path):
    """files_read and search_calls come from the read path itself, so they
    are what happened rather than what a worker remembers happening."""
    import subprocess

    from terminal_mcp.repo_read import RepoReadPolicy, repo_read, repo_search

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "mod.py").write_text("needle = 1\n")

    queue, recorder = wired
    task_id = _queued_task(queue)
    _dispatch(queue, task_id)

    policy = RepoReadPolicy(allowed_roots=(str(repo),))
    with wtr.observing(recorder, task_id):
        assert not repo_read(str(repo), policy, file="mod.py").get("error")
        # A refused read is not a read.
        assert repo_read(str(repo), policy, file="nope.py").get("error")
        repo_search(str(repo), policy, query="needle")

    row = telemetry.for_task(task_id)
    assert row["files_read"] == 1
    assert row["search_calls"] == 1


def test_buffered_signals_for_tasks_that_never_dispatch_are_bounded(wired):
    """A process left running must not grow a dictionary forever because of
    tasks that never reached the queue."""
    _, recorder = wired
    for index in range(wtr.MAX_PENDING_TASKS + 10):
        recorder.note(f"ghost{index}", "files_read", 1)
    assert len(recorder._pending) == wtr.MAX_PENDING_TASKS
