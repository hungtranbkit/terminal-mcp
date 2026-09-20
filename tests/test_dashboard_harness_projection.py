"""TMCP-HARNESS-001: the board READS the run; it keeps no second state.

The defect this defends against is specific and was routine on the old
board: a task rendered as "Blocked" while the queue called it RUNNING, the
project called it "phase: implement" and the bug subsystem called it
"awaiting executor" -- four answers, no fact of the matter.

Every harness field on a card here is a pure function of HarnessRun.stage
(harness_state.project_stage), so the board cannot display a stage the engine
does not believe in. These tests hold that: the card's stage is asserted
against the RUN's stage, never against a literal the dashboard could drift
from independently.

SAFETY: tmp_path-scoped queue database throughout; no real session, no model.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_state as state
from terminal_mcp import queue_store as qs
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.harness_service import HarnessService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService

from .test_harness_core import ScriptedRunner, passing_checks

pytestmark = pytest.mark.usefixtures("declared_toolchain")


@pytest.fixture
def rig(read_config, tmp_path):
    """A dashboard whose queue AND harness share one real, temporary store."""
    queue = QueueService(qs.QueueStore(tmp_path / "queue.db"))
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    harness = HarnessService(queue=queue, repo_root=str(repo),
                             runner=ScriptedRunner(), check_runner=passing_checks)
    service = TerminalService(read_config)
    server = build_mcp(service, queue=queue)
    register_dashboard(server, service, queue=queue, harness=harness)
    client = TestClient(server.streamable_http_app(),
                        headers={"Origin": "http://testserver"})
    return client, queue, harness


def _task(queue, title="Commute card", prompt="build the commute card"):
    task_id, = queue.store.append_tasks("demo", [{"title": title, "prompt": prompt}])
    return task_id


def _card(body, task_id):
    for column in ("backlog", "queued", "running", "blocked_review", "done"):
        for row in body.get(column, []):
            if row["id"] == task_id:
                return row
    raise AssertionError(f"{task_id} is on no column of the board")


# ---------------------------------------------------------------------------
# the card shows the run, and only the run
# ---------------------------------------------------------------------------

def test_an_unharnessed_task_carries_no_harness_block(rig):
    client, queue, _harness = rig
    task_id = _task(queue)

    body = client.get("/dashboard/api/tasks/board").json()

    assert "harness" not in _card(body, task_id), \
        "'never harnessed' and 'harnessed and idle' must not look the same"
    assert body["counts"]["harnessed"] == 0


def test_the_card_renders_the_runs_own_stage(rig):
    client, queue, harness = rig
    task_id = _task(queue)
    started = harness.start(task_id=task_id, acceptance=["the card shows an ETA"],
                            checks=["npm test"], steps=2)
    run = harness.store.require_run(started["run"]["id"])

    card = _card(client.get("/dashboard/api/tasks/board").json(), task_id)

    assert card["harness"]["stage"] == run.stage
    assert card["harness"]["projected_stage"] == run.projected_stage
    assert card["harness"]["projected_stage"] in state.PROJECTED_STAGES


def test_the_card_carries_mode_iteration_builder_evaluator_progress_and_blocker(rig):
    client, queue, harness = rig
    task_id = _task(queue)
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"], steps=2)

    block = _card(client.get("/dashboard/api/tasks/board").json(), task_id)["harness"]

    for key in ("mode", "stage", "iteration", "max_iterations", "builder",
                "evaluator", "progress", "blocker", "write_authority", "shadow"):
        assert key in block, key
    assert block["mode"] in policy.MODES
    assert set(block["progress"]) >= {"step", "of", "percent", "iteration"}


def test_the_card_carries_the_efficiency_counters_the_cost_claim_rests_on(rig):
    client, queue, harness = rig
    task_id = _task(queue)
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"], steps=4)

    efficiency = _card(client.get("/dashboard/api/tasks/board").json(),
                       task_id)["harness"]["efficiency"]

    for counter in ("llm_calls", "prompt_tokens_estimate", "context_bytes",
                    "planner_skipped", "evaluator_skipped", "reused_context_hits",
                    "context_cache_misses", "session_reused", "sessions_spawned",
                    "delta_prompts", "full_prompts"):
        assert counter in efficiency, counter


def test_a_shadow_run_shows_its_stage_while_the_task_status_stays_put(rig):
    """The whole point of SHADOW: the board can show what the harness WOULD
    have done, beside what the old pipeline actually did."""
    client, queue, harness = rig
    task_id = _task(queue)
    for target in (qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING):
        queue.store.transition_task(task_id, target, event_type="TEST")
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                  write_authority=policy.SHADOW, steps=24)

    card = _card(client.get("/dashboard/api/tasks/board").json(), task_id)

    assert card["status"] == qs.RUNNING, "SHADOW wrote nothing to the task"
    assert card["harness"]["shadow"] is True
    assert card["harness"]["stage"] != state.INIT, "the run really did advance"


# ---------------------------------------------------------------------------
# the Human Decision Queue
# ---------------------------------------------------------------------------

def test_the_board_carries_a_human_decision_queue_that_starts_empty(rig):
    client, queue, harness = rig
    _task(queue)
    body = client.get("/dashboard/api/tasks/board").json()
    assert body["human_decisions"] == []
    assert body["counts"]["human_decisions"] == 0


def test_only_the_closed_list_reaches_the_human_decision_queue(rig):
    client, queue, harness = rig
    task_id = _task(queue)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    harness.store.open_decision(
        reason=policy.MISSING_HUMAN_INPUT, run_id=started["run"]["id"],
        task_id=task_id, question="which approved board asset?")

    response = client.get("/dashboard/api/harness/decisions")
    body = response.json()

    assert response.status_code == 200
    assert [row["reason"] for row in body["decisions"]] == [policy.MISSING_HUMAN_INPUT]
    assert set(body["reasons"]) == set(policy.HUMAN_DECISION_REASONS)
    assert "tests_failed" not in body["reasons"]


def test_a_failing_evaluator_never_appears_in_the_human_queue(rig):
    """FAIL -> REVISING is a stage, not a question. Asserted at the route."""
    client, queue, harness = rig
    task_id = _task(queue)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    run_id = started["run"]["id"]
    harness.store.advance(run_id, state.PLAN_READY, reason="planned")
    harness.store.advance(run_id, state.BUILDING, reason="building")
    harness.store.advance(run_id, state.EVALUATING, reason="evaluating")
    harness.store.advance(run_id, state.REVISING, reason="a check went red")

    body = client.get("/dashboard/api/harness/decisions").json()

    assert body["decisions"] == []
    card = _card(client.get("/dashboard/api/tasks/board").json(), task_id)
    assert card["harness"]["projected_stage"] == "REVISING"


# ---------------------------------------------------------------------------
# the project panel and the run detail
# ---------------------------------------------------------------------------

def test_the_project_panel_totals_across_its_runs(rig):
    client, queue, harness = rig
    first, second = _task(queue, "One"), _task(queue, "Two")
    harness.start(task_id=first, project_id="urbanflow", acceptance=["a"],
                  checks=["npm test"], steps=4)
    harness.start(task_id=second, project_id="urbanflow", acceptance=["a"],
                  checks=["npm test"], steps=4)

    body = client.get("/dashboard/api/harness/runs?project_id=urbanflow").json()

    assert body["status"] == "OK"
    assert len(body["runs"]) == 2
    assert body["efficiency_totals"]["runs"] == 2
    for counter in ("llm_calls", "planner_skipped", "evaluator_skipped",
                    "reused_context_hits", "session_reused", "context_bytes"):
        assert counter in body["efficiency_totals"], counter
    assert sum(body["by_stage"].values()) == 2


def test_the_run_detail_is_the_same_read_the_tool_returns(rig):
    """One read behind both surfaces, so a page and a tool call can never
    describe the same run differently."""
    client, queue, harness = rig
    task_id = _task(queue)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=4)
    run_id = started["run"]["id"]

    body = client.get(f"/dashboard/api/harness/run?run_id={run_id}").json()

    assert body["status"] == "OK"
    for key in ("run", "contract", "iterations", "evaluations", "checkpoints",
                "decisions", "efficiency", "events", "summary"):
        assert key in body, key
    assert body["run"]["id"] == harness.status(run_id=run_id)["run"]["id"]


def test_the_run_detail_can_be_reached_by_task_id(rig):
    client, queue, harness = rig
    task_id = _task(queue)
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"])

    body = client.get(f"/dashboard/api/harness/run?task_id={task_id}").json()

    assert body["status"] == "OK"
    assert body["run"]["task_id"] == task_id


def test_the_run_detail_refuses_without_an_identifier(rig):
    client, _queue, _harness = rig
    response = client.get("/dashboard/api/harness/run")
    assert response.status_code == 400
    assert response.json()["error"] == "RUN_ID_OR_TASK_ID_REQUIRED"


def test_an_unknown_run_is_a_404_not_an_empty_success(rig):
    client, _queue, _harness = rig
    response = client.get("/dashboard/api/harness/run?run_id=hrn_nope")
    assert response.status_code == 404
    assert response.json()["error"] == "NO_RUN"


# ---------------------------------------------------------------------------
# what the dashboard is NOT allowed to do
# ---------------------------------------------------------------------------

def test_the_dashboard_exposes_no_way_to_drive_a_run(read_config, tmp_path):
    """Starting, resuming and cancelling happen on the one tool surface.
    A write route here would be a second place a run is driven."""
    queue = QueueService(qs.QueueStore(tmp_path / "queue.db"))
    service = TerminalService(read_config)
    server = build_mcp(service, queue=queue)
    register_dashboard(server, service, queue=queue,
                       harness=HarnessService(queue=queue))

    harness_routes = {
        route.path: set(route.methods)
        for route in server._custom_starlette_routes
        if hasattr(route, "methods") and "/harness/" in route.path
    }

    assert harness_routes, "the read routes must exist for this to mean anything"
    for path, methods in harness_routes.items():
        assert methods <= {"GET", "HEAD"}, f"{path} can write: {methods}"


# ---------------------------------------------------------------------------
# the page renders what the route supplies
# ---------------------------------------------------------------------------

def test_the_board_page_renders_the_harness_block():
    """The route can serve `harness` on every card and the page can ignore
    it; then the API is "done" and a human still sees the old board."""
    from terminal_mcp.dashboard import GLOBAL_TASKS_HTML

    assert "task.harness" in GLOBAL_TASKS_HTML
    for field in ("projected_stage", "write_authority", "max_iterations",
                  "builder", "evaluator", "blocker", "efficiency"):
        assert field in GLOBAL_TASKS_HTML, field


def test_the_page_shows_the_task_status_and_the_run_stage_separately(rig):
    """They answer different questions -- where the task is in the durable
    queue, and how far the AI attempt has got. The old board collapsed them
    into one label and could not say which it meant."""
    from terminal_mcp.dashboard import GLOBAL_TASKS_HTML

    client, queue, harness = rig
    task_id = _task(queue)
    for target in (qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING):
        queue.store.transition_task(task_id, target, event_type="TEST")
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                  write_authority=policy.SHADOW, steps=24)

    card = _card(client.get("/dashboard/api/tasks/board").json(), task_id)

    assert card["status"] == qs.RUNNING
    assert card["harness"]["projected_stage"] != card["status"], \
        "the two labels are independent; the card carries both"
    # The page renders the status chip and the harness chip from the two
    # separate fields, never one derived from the other.
    assert "status-${task.status}" in GLOBAL_TASKS_HTML
    assert "clean(h.projected_stage)" in GLOBAL_TASKS_HTML


def test_a_shadow_run_is_visibly_marked_on_the_page():
    """A run advancing WITHOUT driving the task must not look like one that
    is driving it -- that is the entire comparison SHADOW exists for."""
    from terminal_mcp.dashboard import GLOBAL_TASKS_HTML

    assert "h.shadow" in GLOBAL_TASKS_HTML
    assert "without writing its status" in GLOBAL_TASKS_HTML
