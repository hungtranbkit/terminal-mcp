"""TMCP-HARNESS-001: the harness attached to the canonical queue.

`test_harness_core.py` proves the engine's rules in isolation. This file
proves the WIRING -- the part where the harness is allowed to touch the rest
of the system -- and almost every test here asserts a refusal:

* SHADOW writes nothing outside the harness tables   -> test_shadow_*
* no authority ever writes a final task state        -> test_*_final_*
* resume continues, never restarts                   -> test_resume_*
* cancel stops a run, never a task                   -> test_cancel_*
* the human queue holds only the closed list         -> test_human_*
* a repeated start opens no second run               -> test_repeated_*

SAFETY: every database is tmp_path-scoped. Nothing here touches the
production queue, a real session, or a model.
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_state as state
from terminal_mcp import queue_store as qs
from terminal_mcp.harness_engine import AgentResult, CheckResult
from terminal_mcp.harness_service import HarnessService
from terminal_mcp.harness_store import HarnessStore

from .test_harness_core import ScriptedRunner, passing_checks, failing_checks  # noqa: F401

pytestmark = pytest.mark.usefixtures("declared_toolchain")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

class FakeQueue:
    """Only what HarnessService uses: `.store`. Never a QueueService stub.

    The real QueueStore is used underneath, because the thing under test is
    whether the projection respects the queue's ACTUAL transition table -- a
    stubbed store would happily accept the invalid writes this file exists to
    prove do not happen.
    """

    def __init__(self, store: qs.QueueStore) -> None:
        self.store = store


@pytest.fixture
def queue_store(tmp_path):
    return qs.QueueStore(tmp_path / "queue.db")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "card.tsx").write_text("export const Card = () => null;\n")
    return root


def _service(queue_store, repo, *, runner=None, checks=passing_checks):
    return HarnessService(
        store=HarnessStore(queue_store.path),
        queue=FakeQueue(queue_store),
        repo_root=str(repo),
        runner=runner if runner is not None else ScriptedRunner(),
        check_runner=checks)


def _task(queue_store, *, prompt="build the commute card", title="Commute card"):
    task_id, = queue_store.append_tasks("demo", [{"title": title, "prompt": prompt}])
    return task_id


def _to_running(queue_store, task_id):
    for target in (qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING):
        queue_store.transition_task(task_id, target, event_type="TEST")


# ---------------------------------------------------------------------------
# one database, not two
# ---------------------------------------------------------------------------

def test_the_harness_store_defaults_to_the_queues_own_database(queue_store, repo):
    """A second database would be a second runtime source of truth, which is
    the defect the whole feature exists to remove."""
    service = HarnessService(queue=FakeQueue(queue_store), repo_root=str(repo))
    assert service.store.path == queue_store.path


# ---------------------------------------------------------------------------
# the definition is READ from the task, never restated
# ---------------------------------------------------------------------------

def test_start_reads_acceptance_from_the_tasks_own_requirement_contract(queue_store, repo):
    task_id = _task(queue_store)
    queue_store.set_requirement_contract(task_id, requirements=[
        {"id": "R1", "text": "the card shows an ETA"},
        {"id": "R2", "text": "the ETA refreshes every 30s"},
    ], actor="user")
    service = _service(queue_store, repo)

    result = service.start(task_id=task_id, checks=["npm test"])

    assert result["status"] == "OK"
    assert result["definition_source"] == "queue_task"
    contract_input = result["run"]["policy"]["declared_acceptance"]
    assert contract_input == ["the card shows an ETA", "the ETA refreshes every 30s"], \
        "the harness restates the task's requirements; it does not invent a second set"


def test_start_without_a_durable_task_needs_its_prompt_spelled_out(queue_store, repo):
    service = _service(queue_store, repo)
    result = service.start(task_id="NOT-A-TASK")
    assert result["status"] == "FAILED"
    assert result["error"] == "PROMPT_REQUIRED"


# ---------------------------------------------------------------------------
# exactly once
# ---------------------------------------------------------------------------

def test_repeated_start_on_the_same_definition_opens_no_second_run(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)

    first = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    second = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])

    assert first["created"] is True and second["created"] is False
    assert first["run"]["id"] == second["run"]["id"]
    assert len(service.store.list_runs()) == 1


# ---------------------------------------------------------------------------
# SHADOW writes nothing outside the harness tables
# ---------------------------------------------------------------------------

def test_shadow_never_writes_the_task_status(queue_store, repo):
    task_id = _task(queue_store)
    _to_running(queue_store, task_id)
    before = queue_store.get_task(task_id).status
    service = _service(queue_store, repo)

    service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                  write_authority=policy.SHADOW, steps=12)

    assert queue_store.get_task(task_id).status == before, \
        "a SHADOW run compares against the old pipeline; it does not drive it"


def test_shadow_records_what_it_would_have_written(queue_store, repo):
    """The comparison is the point of SHADOW, so the projection it withheld
    is still recorded -- on the run, where it can be read against the task."""
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    result = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                           write_authority=policy.SHADOW, steps=12)
    run = service.store.require_run(result["run"]["id"])
    assert run.shadow_of_task_status, "SHADOW still computes the projection"


def test_shadow_never_completes_the_task_even_when_the_run_finishes(queue_store, repo):
    task_id = _task(queue_store)
    _to_running(queue_store, task_id)
    service = _service(queue_store, repo)
    result = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                           write_authority=policy.SHADOW, steps=24)
    assert result["status"] == "OK"
    assert queue_store.get_task(task_id).status not in (
        qs.COMPLETED, qs.FAILED, qs.CANCELLED)


# ---------------------------------------------------------------------------
# no authority writes a FINAL task state
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("authority", [policy.SUPERVISED, policy.AUTONOMOUS])
def test_no_authority_writes_a_final_task_state(queue_store, repo, authority):
    """The single most dangerous thing a second state machine can do is close
    a task. Refused for every authority, including AUTONOMOUS."""
    task_id = _task(queue_store)
    _to_running(queue_store, task_id)
    service = _service(queue_store, repo)

    service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                  write_authority=authority, steps=24)

    assert queue_store.get_task(task_id).status not in (
        qs.COMPLETED, qs.FAILED, qs.CANCELLED, qs.SKIPPED)


def test_the_stage_map_contains_no_terminal_queue_status():
    """Structural: the projection cannot write a final state because no final
    state is reachable through its map."""
    from terminal_mcp.harness_service import (_STAGE_TO_QUEUE_STATUS,
                                              _TERMINAL_QUEUE_STATUSES)
    assert not (set(_STAGE_TO_QUEUE_STATUS.values()) & _TERMINAL_QUEUE_STATUSES)


def test_supervised_drives_the_task_through_the_queues_own_table(queue_store, repo):
    task_id = _task(queue_store)
    _to_running(queue_store, task_id)
    service = _service(queue_store, repo)

    service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                  write_authority=policy.SUPERVISED, steps=24)

    task = queue_store.get_task(task_id)
    assert task.status in (qs.RUNNING, qs.VERIFYING), task.status
    events = [row["event_type"] for row in queue_store.task_events(task_id)] \
        if hasattr(queue_store, "task_events") else []
    if events:
        assert "HARNESS_PROJECTION" in events


def test_a_projection_the_queue_refuses_is_recorded_not_forced(queue_store, repo):
    """A task parked in BLOCKED by a coordinator refusal must not be dragged
    back to RUNNING by a harness stage. The divergence is logged instead."""
    task_id = _task(queue_store)
    for target in (qs.PRECHECK, qs.BLOCKED):
        queue_store.transition_task(task_id, target, event_type="TEST")
    service = _service(queue_store, repo)

    service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                  write_authority=policy.SUPERVISED, steps=24)

    assert queue_store.get_task(task_id).status == qs.BLOCKED
    assert service.skipped_projections, "a refused projection is a fact, not a no-op"


# ---------------------------------------------------------------------------
# resume continues; it does not restart
# ---------------------------------------------------------------------------

def test_resume_returns_to_the_exact_stage_iteration_and_contract(queue_store, repo):
    task_id = _task(queue_store)
    runner = ScriptedRunner([
        AgentResult(payload={"ok": True}, session_id="builder-1", agent="builder",
                    commit="c1", context_percent=20.0),
    ])
    service = _service(queue_store, repo, runner=runner)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    engine = service.engine()
    engine.step(run_id)   # -> PLAN_READY
    engine.step(run_id)   # -> BUILDING
    before = service.store.require_run(run_id)
    engine.record_infra_failure(run_id, "the node went away")

    resumed = service.resume(run_id=run_id)

    assert resumed["status"] == "OK"
    assert resumed["resumed_to"] == before.stage, "back to the stage it was in"
    assert resumed["resumed_to"] != state.PLANNING
    assert resumed["iteration"] == before.current_iteration
    assert resumed["contract_id"] == before.contract_id, "the same contract, not a new one"
    assert resumed["planner_rerun"] is False


def test_resume_of_a_healthy_run_writes_no_stage_event(queue_store, repo):
    """A run already sitting where it should continue needs no transition.
    Advancing it to itself would record a stage change that did not happen."""
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    service.engine().step(run_id)
    before = service.store.require_run(run_id)
    stage_events = [e for e in service.store.events(run_id) if e["event_type"] == "STAGE"]

    resumed = service.resume(run_id=run_id)

    after_events = [e for e in service.store.events(run_id) if e["event_type"] == "STAGE"]
    assert resumed["status"] == "OK"
    assert resumed["resumed_to"] == before.stage
    assert len(after_events) == len(stage_events), "no invented stage change"


def test_resume_never_re_runs_the_planner(queue_store, repo):
    task_id = _task(queue_store)
    runner = ScriptedRunner([
        AgentResult(payload={"ok": True}, session_id="builder-1", agent="builder",
                    commit="c1", context_percent=20.0),
    ])
    service = _service(queue_store, repo, runner=runner)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    engine = service.engine()
    engine.step(run_id)
    engine.step(run_id)
    engine.record_infra_failure(run_id, "boom")
    planner_calls_before = [c for c in runner.calls if c["role"] == policy.PLANNER]

    service.resume(run_id=run_id, steps=1)

    planner_calls_after = [c for c in runner.calls if c["role"] == policy.PLANNER]
    assert planner_calls_after == planner_calls_before, \
        "resume is a continuation; re-planning is what the old retry did"


def test_resume_refuses_a_blocked_run_and_says_what_would_move_it(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    service.store.advance(run_id, state.BLOCKED, reason="needs a licensed font")
    service.store.open_decision(reason=policy.MISSING_HUMAN_INPUT, run_id=run_id,
                                task_id=task_id, question="which font?")

    result = service.resume(run_id=run_id)

    assert result["status"] == "FAILED"
    assert result["error"] == "NEEDS_DECISION"
    assert result["open_decisions"], "the refusal names what is actually waiting"


def test_resume_refuses_a_terminal_run(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    service.store.advance(run_id, state.CANCELLED, reason="done with it")

    result = service.resume(run_id=run_id)
    assert result["status"] == "FAILED"
    assert result["error"] == "RUN_IS_TERMINAL"


# ---------------------------------------------------------------------------
# cancel stops a run, not a task
# ---------------------------------------------------------------------------

def test_cancel_stops_the_run_and_leaves_the_task_alone(queue_store, repo):
    task_id = _task(queue_store)
    _to_running(queue_store, task_id)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            write_authority=policy.SUPERVISED)
    before = queue_store.get_task(task_id).status

    result = service.cancel(run_id=started["run"]["id"], actor="operator")

    assert result["status"] == "OK"
    assert result["stage"] == state.CANCELLED
    assert queue_store.get_task(task_id).status == before, \
        "what happens to the task after a cancelled attempt is a separate decision"


def test_cancelling_twice_is_not_an_error(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    service.cancel(run_id=started["run"]["id"])
    again = service.cancel(run_id=started["run"]["id"])
    assert again["status"] == "OK" and again["already"] is True


# ---------------------------------------------------------------------------
# the Human Decision Queue holds ONLY real blockers
# ---------------------------------------------------------------------------

def test_a_failing_check_revises_and_asks_nobody(queue_store, repo):
    """The single most expensive defect in the old pipeline, asserted from
    the service layer this time: a red check is the machine's problem."""
    task_id = _task(queue_store)
    service = _service(queue_store, repo, checks=failing_checks)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    engine = service.engine()

    stages = []
    while state.REVISING not in stages:
        outcome = engine.step(run_id)
        stages.append(outcome.to_stage)
        assert outcome.action != "idle", f"never revised; got {stages}"

    assert state.REVISING in stages
    assert service.human_decisions() == [], \
        "at the moment a check goes red, nobody is asked anything"


def test_only_the_iteration_cap_escalates_and_it_is_not_a_verdict(queue_store, repo):
    """A run whose checks never go green does eventually stop -- but the
    reason on the record is that the LOOP did not converge, never that the
    code was judged bad. The distinction is what keeps humans off the
    critical path for work the machine could still have fixed."""
    task_id = _task(queue_store)
    service = _service(queue_store, repo, checks=failing_checks)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=64)
    run = service.store.require_run(started["run"]["id"])

    assert run.stage == state.BLOCKED
    # Stopped at or BEFORE the cap: a verdict identical to the previous one
    # means the obstacle is outside the Builder's reach, and spending the
    # remaining iterations to confirm that again is the waste this guard
    # exists to avoid (see `_repeating_itself`).
    assert 0 < run.current_iteration <= run.max_iterations
    reasons = [row["reason"] for row in service.human_decisions()]
    assert reasons == [policy.MAX_ITERATIONS_REACHED], reasons
    assert all("fail" not in row["question"].lower().split()
               for row in service.human_decisions()), \
        "the question put to a human is about the definition, not the test output"


def test_the_human_queue_is_the_closed_list_and_nothing_else(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    service.store.open_decision(reason=policy.MISSING_HUMAN_INPUT, run_id=run_id,
                                task_id=task_id, question="the approved board asset")

    rows = service.human_decisions()
    assert [row["reason"] for row in rows] == [policy.MISSING_HUMAN_INPUT]
    assert all(row["reason"] in policy.HUMAN_DECISION_REASONS for row in rows)


def test_a_reason_outside_the_closed_list_cannot_be_opened_at_all(queue_store, repo):
    service = _service(queue_store, repo)
    with pytest.raises(policy.NotAHumanDecision):
        service.store.open_decision(reason="tests_failed", question="the tests are red")


# ---------------------------------------------------------------------------
# review, and the merge gate
# ---------------------------------------------------------------------------

def test_review_is_read_only_without_a_decision_or_an_approval(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    before = service.store.require_run(started["run"]["id"]).stage

    result = service.review(run_id=started["run"]["id"])

    assert result["status"] == "OK"
    assert service.store.require_run(started["run"]["id"]).stage == before


def test_review_resolves_a_decision_without_advancing_the_run(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    decision = service.store.open_decision(
        reason=policy.MISSING_HUMAN_INPUT, run_id=run_id, task_id=task_id,
        question="which asset?")
    before = service.store.require_run(run_id).stage

    result = service.review(decision_id=decision["id"], resolution="use board-v3.png",
                            actor="operator")

    assert result["status"] == "OK"
    assert result["decision"]["status"] == "resolved"
    assert service.store.require_run(run_id).stage == before
    assert service.human_decisions() == []


def test_a_merge_approval_must_name_who_approved_it(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    result = service.review(run_id=started["run"]["id"], approve_merge=True)
    assert result["status"] == "FAILED"
    assert result["error"] == "ACTOR_REQUIRED"


def test_a_merge_is_refused_from_any_stage_but_merge_ready(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"])

    result = service.review(run_id=started["run"]["id"], approve_merge=True,
                            actor="operator")

    assert result["status"] == "FAILED"
    assert result["error"] == "MERGE_REFUSED"
    assert "MERGE_READY" in result["detail"]


def test_supervised_reaches_merge_ready_but_stops_there(queue_store, repo):
    """SUPERVISED may drive Planner/Builder/Evaluator/Revision. It may not
    take the merge decision -- that is the gate the whole mode exists for."""
    task_id = _task(queue_store)
    _to_running(queue_store, task_id)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            write_authority=policy.SUPERVISED, steps=24)
    run = service.store.require_run(started["run"]["id"])

    assert run.stage == state.MERGE_READY, run.stage
    assert run.stage != state.MERGED


# ---------------------------------------------------------------------------
# the read side the board and the project page use
# ---------------------------------------------------------------------------

def test_a_task_with_no_run_is_absent_rather_than_zeroed(queue_store, repo):
    harnessed = _task(queue_store)
    plain = _task(queue_store, title="Untouched")
    service = _service(queue_store, repo)
    service.start(task_id=harnessed, acceptance=["a"], checks=["npm test"])

    projections = service.projections_for_tasks([harnessed, plain])

    assert harnessed in projections
    assert plain not in projections, \
        "'not harnessed' and 'harnessed and idle' must not look the same"


def test_the_projection_carries_what_a_card_renders(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=4)

    row = service.projections_for_tasks([task_id])[task_id]

    for key in ("mode", "stage", "projected_stage", "iteration", "builder",
                "evaluator", "progress", "blocker", "efficiency"):
        assert key in row, key
    assert row["projected_stage"] in state.PROJECTED_STAGES
    for counter in ("llm_calls", "planner_skipped", "evaluator_skipped",
                    "reused_context_hits", "session_reused", "context_bytes"):
        assert counter in row["efficiency"], counter


def test_the_project_overview_totals_across_its_runs(queue_store, repo):
    first = _task(queue_store, title="One")
    second = _task(queue_store, title="Two")
    service = _service(queue_store, repo)
    service.start(task_id=first, project_id="urbanflow", acceptance=["a"],
                  checks=["npm test"], steps=4)
    service.start(task_id=second, project_id="urbanflow", acceptance=["a"],
                  checks=["npm test"], steps=4)

    overview = service.project_overview("urbanflow")

    assert overview["status"] == "OK"
    assert len(overview["runs"]) == 2
    assert overview["efficiency_totals"]["runs"] == 2
    assert sum(overview["by_stage"].values()) == 2


def test_status_of_one_run_is_the_stores_full_report(queue_store, repo):
    task_id = _task(queue_store)
    service = _service(queue_store, repo)
    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=4)

    report = service.status(run_id=started["run"]["id"])

    assert report["status"] == "OK"
    for key in ("run", "contract", "iterations", "evaluations", "checkpoints",
                "decisions", "efficiency", "events", "summary"):
        assert key in report, key


def test_status_for_an_unknown_run_is_an_honest_refusal(queue_store, repo):
    service = _service(queue_store, repo)
    result = service.status(run_id="hrn_nope")
    assert result["status"] == "FAILED"
    assert result["error"] == "NO_RUN"


def test_a_refused_drive_still_reports_the_steps_that_happened(queue_store, repo):
    """With no AgentRunner, planning succeeds deterministically and the
    Builder cannot be reached. The steps before that point are real -- the
    run's stage and its event log both show them -- so a report that omitted
    them would describe a run that does not exist."""
    task_id = _task(queue_store)
    service = HarnessService(store=HarnessStore(queue_store.path),
                             queue=FakeQueue(queue_store), repo_root=str(repo),
                             runner=None, check_runner=passing_checks)

    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=6)

    assert started["status"] == "OK"
    assert started["drive_error"], "the refusal is reported, not swallowed"
    assert "AgentRunner" in started["drive_error"]
    stages = [step["to_stage"] for step in started["steps"]]
    assert stages, "the steps that happened must be reported"
    assert state.PLAN_READY in stages
    run = service.store.require_run(started["run"]["id"])
    assert run.stage == stages[-1], \
        "the last reported step is where the run actually is"


def test_a_server_with_no_runner_cannot_spend_a_token(queue_store, repo):
    """The P0 posture, asserted: every action works, planning is
    deterministic, and nothing reaches a model."""
    task_id = _task(queue_store)
    service = HarnessService(store=HarnessStore(queue_store.path),
                             queue=FakeQueue(queue_store), repo_root=str(repo),
                             runner=None, check_runner=passing_checks)

    started = service.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=12)

    assert service.store.efficiency(started["run"]["id"])["llm_calls"] == 0
