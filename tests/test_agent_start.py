"""agent_start -- identity on top of the Phase A router, and nothing else.

Every test here is really asking one of two questions:

  * did agent_start REUSE the router (so deleted worktrees, busy sessions and
    the atomic claim all still apply), rather than growing a second scheduler?
  * does the agent's identity survive the things that destroy a session?
"""
from __future__ import annotations

import pytest

from terminal_mcp.agent_registry import AGENT_DISABLED, AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.config import RouterConfig
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import BOUND, QUEUED, TERMINAL_STATUSES, WAITING_RUNTIME, QueueStore
from terminal_mcp.task_router import TaskRouter

from tests.test_task_router import (  # reuse the Phase A fakes rather than a second set
    FakeController, FakeRecord, RecordingEngine, StubbornEngine, _row,
)


@pytest.fixture
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


@pytest.fixture
def registry(tmp_path):
    return AgentRegistryStore(tmp_path / "agents.db")


def _config(**overrides):
    return type("Cfg", (), {"router": RouterConfig(**overrides)})()


def _wire(registry, queue, controller, *, engine=None, config=None):
    """Exactly the composition build_mcp performs: the agent service's capacity
    rule is installed as a ROUTER hook, so it holds on the rescue path too."""
    service = AgentService(registry, queue=queue, skill_roots=())
    router = TaskRouter(
        queue.store, controller=controller, queue=queue,
        engine=engine if engine is not None else RecordingEngine(queue.store),
        session_registry=controller.session_registry, config=config or _config(),
        capacity_check=service.capacity_block_reason)
    service.router = router
    queue.router = router
    return service, router


def _fleet(*names, agent_type="claude", state="IDLE"):
    controller = FakeController(
        sessions=[_row(name) for name in names],
        records=[FakeRecord(node_id="local", session_name=name, agent_type=agent_type,
                            last_known_state=state) for name in names])
    return controller


# ---------------------------------------------------------------------------
# agent_start.
# ---------------------------------------------------------------------------

def test_agent_start_routes_to_a_healthy_idle_session_and_records_the_run(registry, queue):
    registry.create_agent("builder", name="Builder", project_id="nova", runtime="claude")
    registry.register_skill("lint", version="1", body="run the linter")
    registry.bind_skill("builder", "lint")
    controller = _fleet("agent-a")
    service, _router = _wire(registry, queue, controller)

    receipt = service.agent_start("builder", "do the work")

    assert receipt["session"] == "agent-a"
    assert receipt["dispatched"] is True
    assert receipt["agent_id"] == "builder"
    assert receipt["skills"] == ["lint@1"]

    task = queue.store.get_task(receipt["task_id"])
    assert task.agent_id == "builder"
    assert task.skill_ids == ("lint@1",)
    assert task.execution_session == "agent-a"

    runs = registry.recent_runs("builder")
    assert runs[0]["task_id"] == receipt["task_id"]
    assert runs[0]["session"] == "agent-a"
    assert runs[0]["status"] == "STARTED"


def test_agent_start_is_idempotent_under_a_request_key(registry, queue):
    registry.create_agent("builder")
    controller = _fleet("agent-a")
    service, _router = _wire(registry, queue, controller)

    first = service.agent_start("builder", "work", request_key="req-1")
    second = service.agent_start("builder", "work", request_key="req-1")

    assert first["task_id"] == second["task_id"]
    assert second["deduplicated"] is True
    assert len(registry.recent_runs("builder")) == 1


def test_agent_start_refuses_a_disabled_agent_rather_than_queueing_for_nobody(registry, queue):
    registry.create_agent("builder")
    registry.disable_agent("builder")
    service, _router = _wire(registry, queue, _fleet("agent-a"))

    result = service.agent_start("builder", "work")

    assert result["error"] == "AGENT_DISABLED"
    assert queue.store.routable_tasks() == []


def test_agent_start_on_an_unknown_agent_is_an_error(registry, queue):
    service, _router = _wire(registry, queue, _fleet("agent-a"))
    assert service.agent_start("ghost", "work")["error"] == "AGENT_NOT_FOUND"


def test_the_agents_own_repo_and_runtime_become_the_task_profile(registry, queue, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    registry.create_agent("builder", project_id="nova", repo=str(repo), runtime="claude")
    controller = FakeController(
        sessions=[_row("wrong-repo"), _row("right-repo")],
        records=[FakeRecord(node_id="local", session_name="wrong-repo",
                            cwd=str(tmp_path / "other"), repo_root=str(tmp_path / "other")),
                 FakeRecord(node_id="local", session_name="right-repo",
                            cwd=str(repo), repo_root=str(repo))])
    service, _router = _wire(registry, queue, controller)

    receipt = service.agent_start("builder", "work")

    assert receipt["session"] == "right-repo"
    assert "same repo" in receipt["routing_reason"]


def test_an_explicit_target_through_agent_start_is_still_hard_affinity(registry, queue):
    registry.create_agent("builder")
    controller = _fleet("preferred", "named")
    service, _router = _wire(registry, queue, controller)

    receipt = service.agent_start("builder", "work", target="named")

    assert receipt["session"] == "named"
    assert receipt["explicit_target"] is True


# ---------------------------------------------------------------------------
# max_sessions.
# ---------------------------------------------------------------------------

def test_an_agent_at_max_sessions_queues_its_next_task_instead_of_taking_a_second(registry, queue):
    registry.create_agent("builder")                      # max_sessions defaults to 1
    controller = _fleet("agent-a", "agent-b")
    service, _router = _wire(registry, queue, controller)

    first = service.agent_start("builder", "first")
    second = service.agent_start("builder", "second")

    assert first["dispatched"] is True
    assert second["session"] is None
    assert second["routing_state"] == WAITING_RUNTIME
    assert "max_sessions" in second["routing_reason"]
    # The second session was free the whole time -- the limit is about the
    # AGENT, not about session availability.
    assert queue.store.tasks_bound_to_session("agent-b") == []


def test_raising_max_sessions_lets_the_second_task_run(registry, queue):
    registry.create_agent("builder", max_sessions=2)
    controller = _fleet("agent-a", "agent-b")
    service, _router = _wire(registry, queue, controller)

    first = service.agent_start("builder", "first")
    second = service.agent_start("builder", "second")

    assert {first["session"], second["session"]} == {"agent-a", "agent-b"}


def test_capacity_frees_up_when_the_holding_task_settles(registry, queue):
    registry.create_agent("builder")
    controller = _fleet("agent-a", "agent-b")
    service, router = _wire(registry, queue, controller)
    first = service.agent_start("builder", "first")
    second = service.agent_start("builder", "second")
    assert second["session"] is None

    for status in ("RUNNING", "VERIFYING", "COMPLETED"):
        queue.store.transition_task(first["task_id"], status, event_type="TEST")
    report = router.rescue_once()

    assert report["routed"] == 1
    assert queue.store.get_task(second["task_id"]).execution_session in ("agent-a", "agent-b")


def test_the_capacity_limit_also_holds_on_the_rescue_path(registry, queue):
    """A rule that only ran on submission would be bypassed by the reconcile
    ten seconds later, which is the whole reason it is a router hook."""
    registry.create_agent("builder")
    controller = _fleet("agent-a", "agent-b")
    service, router = _wire(registry, queue, controller)
    service.agent_start("builder", "first")
    second = service.agent_start("builder", "second")

    report = router.rescue_once()

    assert report["routed"] == 0
    assert queue.store.get_task(second["task_id"]).execution_session is None


def test_a_disabled_agents_queued_work_is_held_with_that_stated_as_the_reason(registry, queue):
    """Work already in the queue when its owner is disabled must stop moving,
    and say why. Disabling an agent is a decision; silently continuing to
    place its tasks would ignore it."""
    registry.create_agent("builder")
    controller = _fleet("agent-a")
    # An engine that will not claim leaves the task QUEUED and unbound, which
    # is exactly the state a queued-but-not-started task is really in.
    service, router = _wire(registry, queue, controller, engine=StubbornEngine())
    receipt = service.agent_start("builder", "work")
    assert queue.store.get_task(receipt["task_id"]).status == QUEUED

    registry.disable_agent("builder")
    report = router.rescue_once()

    assert report["routed"] == 0
    task = queue.store.get_task(receipt["task_id"])
    assert task.routing_state == WAITING_RUNTIME
    assert "disabled" in task.routing_evidence["reason"]
    assert task.agent_id == "builder"      # still owned, just not running


def test_a_task_with_no_agent_is_never_touched_by_the_capacity_rule(registry, queue):
    """Phase A behaviour must be identical for everything that predates agents."""
    controller = _fleet("agent-a")
    service, router = _wire(registry, queue, controller)
    receipt = router.route_start("plain routed work")
    assert receipt["dispatched"] is True
    assert service.capacity_block_reason(queue.store.get_task(receipt["task_id"])) is None


# ---------------------------------------------------------------------------
# Durable identity vs disposable runtime.
# ---------------------------------------------------------------------------

def test_ownership_survives_the_session_being_taken_away_and_replaced(registry, queue):
    """THE PHASE B THESIS. The runtime is disposable; the owner is not."""
    registry.create_agent("builder", max_sessions=1)
    controller = _fleet("first-session", "second-session")
    service, router = _wire(registry, queue, controller)
    receipt = service.agent_start("builder", "long running work")
    task_id = receipt["task_id"]
    assert queue.store.get_task(task_id).execution_session == "first-session"

    # The session turns out to be unusable and is handed back.
    queue.store.release_execution_binding(task_id, reason="session vanished")
    released = queue.store.get_task(task_id)
    assert released.execution_session is None
    assert released.agent_id == "builder"          # ownership untouched

    # Re-routed to a different runtime; still the same agent's task.
    queue.store.transition_task(task_id, QUEUED, event_type="TEST") \
        if released.status not in (QUEUED,) else None
    router.invalidate()
    outcome = router.route_task(task_id)

    rebound = queue.store.get_task(task_id)
    assert outcome.outcome in ("ROUTED", "ALREADY_BOUND")
    assert rebound.agent_id == "builder"
    assert rebound.skill_ids == released.skill_ids
    assert [row["id"] for row in queue.store.tasks_for_agent("builder")] == [task_id]


def test_tasks_for_agent_finds_work_regardless_of_which_lane_it_landed_in(registry, queue):
    registry.create_agent("builder", max_sessions=3)
    controller = _fleet("s1", "s2", "s3")
    service, _router = _wire(registry, queue, controller)
    ids = {service.agent_start("builder", f"work {index}")["task_id"] for index in range(3)}

    found = {row["id"] for row in queue.store.tasks_for_agent("builder")}

    assert found == ids
    lanes = {row["session"] for row in queue.store.tasks_for_agent("builder")}
    assert len(lanes) > 1, "the point is that ownership is not the lane"


# ---------------------------------------------------------------------------
# The agent view the dashboard reads.
# ---------------------------------------------------------------------------

def test_the_agent_view_reports_current_task_runtime_and_queue_depth(registry, queue):
    registry.create_agent("builder", project_id="nova", model="opus")
    registry.register_skill("lint", version="1", body="x")
    registry.bind_skill("builder", "lint")
    controller = _fleet("agent-a")
    service, _router = _wire(registry, queue, controller)
    started = service.agent_start("builder", "the current work")
    service.agent_start("builder", "the waiting work")

    view = service.get_agent("builder")["agent"]

    assert view["project_id"] == "nova"
    assert view["model"] == "opus"
    assert view["at_capacity"] is True
    assert view["current_task"]["task_id"] == started["task_id"]
    assert view["current_task"]["session"] == "agent-a"
    assert view["current_task"]["node_id"] == "local"
    assert view["queued_tasks"] == 1
    assert [skill["skill_id"] for skill in view["skills"]] == ["lint"]
    assert view["recent_runs"]


def test_a_failed_run_shows_up_in_recent_failures(registry, queue):
    registry.create_agent("builder")
    service, _router = _wire(registry, queue, _fleet("agent-a"))
    service.agent_start("builder", "work")
    registry.record_run("builder", task_id="t-broken", status="FAILED", detail="it blew up")

    view = service.get_agent("builder")["agent"]

    assert [run["task_id"] for run in view["recent_failures"]] == ["t-broken"]


def test_list_agents_is_empty_and_honest_before_anything_is_registered(registry, queue):
    service, _router = _wire(registry, queue, _fleet("agent-a"))
    assert service.list_agents() == {"agents": [], "count": 0}
