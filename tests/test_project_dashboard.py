"""The Projects surface: compact `turn` actions and the dashboard routes.

Projects are the top of the user-facing hierarchy, so they have to be reachable
from the ONE advertised tool and from a page a person actually opens. Sessions
appear here only as "where it is currently running".
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp import project_analyzer as pa
from terminal_mcp.agent_registry import AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.compact_tools import (
    AGENT_ARGS, AGENT_REQUIRED, TURN_ACTION_ALIASES, TURN_ACTIONS, TURN_HANDLER_ACTIONS,
    CompactTerminalTools,
)
from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import PROJECTS_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.project_runtime import ProjectRuntimeService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

BIG = "Web app with map, camera streams, AI analysis API, backend, deploy."

PROJECT_ACTIONS = ("project_plan", "project_bootstrap", "project_list", "project_get",
                   "project_update", "project_archive", "project_phase_status",
                   "project_advance", "project_reconcile_team", "project_start")


class Recorder:
    def __init__(self, result=None):
        self.calls: list[dict] = []
        self.result = result if result is not None else {"ok": True}

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def _tools(handlers):
    return CompactTerminalTools(terminal=None, controller=None, handlers=handlers)


# ---------------------------------------------------------------------------
# terminal_turn.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", PROJECT_ACTIONS)
def test_every_project_action_is_advertised_routed_and_argument_checked(action):
    assert action in TURN_ACTIONS
    assert action in TURN_HANDLER_ACTIONS
    assert action in AGENT_ARGS


@pytest.mark.parametrize("alias,canonical", [
    ("projects", "project_list"), ("project", "project_get"), ("plan", "project_plan"),
    ("bootstrap", "project_bootstrap"), ("new_project", "project_bootstrap"),
    ("phase", "project_phase_status"), ("advance", "project_advance"),
    ("reconcile", "project_reconcile_team"),
])
def test_project_aliases_resolve(alias, canonical):
    handler = Recorder()
    tools = _tools({TURN_HANDLER_ACTIONS[canonical]: handler})
    result = tools.turn(action=alias, target="demo", text="a description")
    assert result["action"] == canonical
    assert len(handler.calls) == 1


def test_project_start_takes_the_project_from_target_and_the_prompt_from_text():
    handler = Recorder({"status": "TASK_STARTED", "task_id": "t1", "session": "s1",
                        "project_id": "demo", "agent_id": "demo-core", "agent_role": "core",
                        "phase": "BUILD", "pm_agent_id": "demo-pm", "poll": False,
                        "agent_selection_reason": "score 90"})
    tools = _tools({"project_start": handler})

    result = tools.turn(action="project_start", target="demo", text="build the thing")

    assert handler.calls[0] == {"project_id": "demo", "prompt": "build the thing"}
    assert result["project_id"] == "demo"
    assert result["agent_id"] == "demo-core"
    assert result["pm_agent_id"] == "demo-pm"
    assert result["session"] == "s1"
    assert result["poll"] is False


def test_project_bootstrap_takes_the_name_from_target_and_description_from_text():
    handler = Recorder()
    tools = _tools({"project_bootstrap": handler})
    tools.turn(action="bootstrap", target="traffic-camera-ai", text=BIG)
    assert handler.calls[0] == {"name": "traffic-camera-ai", "description": BIG}


def test_a_missing_required_project_argument_is_named():
    tools = _tools({"project_start": Recorder()})
    assert tools.turn(action="project_start", text="x")["error"] == "MISSING_ARGS"


def test_every_project_required_arg_is_one_the_action_accepts():
    for action in PROJECT_ACTIONS:
        assert set(AGENT_REQUIRED.get(action, ())) <= AGENT_ARGS[action], action


def test_legacy_and_phase_b_actions_survive_phase_c():
    for action in ("start", "send", "inspect", "route_start", "agent_start", "enqueue_task",
                   "list_agents", "cleanup_candidates"):
        assert action in TURN_ACTIONS
    tools = _tools({})
    assert tools.turn(action="start", text="x")["error"] == "TARGET_REQUIRED"


# ---------------------------------------------------------------------------
# Dashboard.
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=True, default_input=True),
    )
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    agents = AgentService(AgentRegistryStore(tmp_path / "agents.db"), queue=queue, skill_roots=())
    projects = ProjectRuntimeService(agents)
    service = TerminalService(config)
    server = build_mcp(service, queue=queue, agents=agents, project_runtime=projects)
    register_dashboard(server, service, queue=queue, agents=agents, projects=projects)
    return (TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"}),
            projects, agents, queue)


def test_the_projects_page_is_served(client):
    test_client, *_ = client
    response = test_client.get("/dashboard/projects")
    assert response.status_code == 200
    assert "Projects" in response.text


def test_the_page_renders_every_field_the_pipeline_answer_needs():
    for token in ("pipeline", "phase_gate", "pm_agent_id", "upcoming", "phase_history",
                  "Agent Team", "New Project", "Advance to", "Reconcile team",
                  "running_tasks", "queued_tasks"):
        assert token in PROJECTS_HTML, token


def test_the_list_api_reports_phase_team_and_task_counts(client):
    test_client, projects, _agents, _queue = client
    projects.bootstrap("demo", description=BIG)

    body = test_client.get("/dashboard/api/projects").json()
    row = body["projects"][0]

    assert row["id"] == "demo"
    assert row["phase"] == pa.INTAKE
    assert row["complexity"] == pa.LARGE
    assert row["pm_agent_id"] == "demo-pm"
    assert row["agent_count"] == 2          # PM + analyst, not the whole pipeline
    assert "running_tasks" in row and "queued_tasks" in row
    assert row["phase_gate"]


def test_the_detail_api_carries_team_upcoming_and_history(client):
    test_client, projects, _agents, _queue = client
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", reason="scope agreed", handoff={"artifacts": ["scope.md"]})

    body = test_client.get("/dashboard/api/projects/detail?project_id=demo").json()
    project = body["project"]

    assert project["phase"] == pa.ANALYSIS
    roles = {agent["role"]: agent for agent in project["agents"]}
    assert roles["pm"]["state"] == "ACTIVE" and roles["pm"]["cross_phase"] is True
    assert roles["analyst"]["state"] == "DORMANT"
    assert pa.BUILD in project["upcoming"]
    assert project["phase_history"][0]["handoff"]["artifacts"] == ["scope.md"]


def test_the_wizard_plan_endpoint_writes_nothing(client):
    test_client, projects, _agents, _queue = client
    response = test_client.post("/dashboard/api/projects/plan",
                                json={"name": "wizard-demo", "description": BIG})
    assert response.status_code == 200
    plan = response.json()["plan"]
    assert plan["profile"]["complexity"] == pa.LARGE
    assert {agent["role"] for agent in plan["agents"]} == {"pm", "analyst"}
    assert projects.list_projects()["count"] == 0, "planning must create nothing"


def test_the_wizard_bootstrap_endpoint_creates_the_team(client):
    test_client, projects, _agents, _queue = client
    response = test_client.post("/dashboard/api/projects/bootstrap",
                                json={"name": "wizard-demo", "description": BIG,
                                      "roles": ["analyst"], "request_key": "w1"})
    assert response.status_code == 200
    body = response.json()
    assert body["pm_agent_id"] == "wizard-demo-pm"
    assert projects.list_projects()["count"] == 1


def test_the_advance_endpoint_moves_the_phase_and_reconciles(client):
    test_client, projects, agents, _queue = client
    projects.bootstrap("demo", description=BIG)
    response = test_client.post("/dashboard/api/projects/advance",
                                json={"project_id": "demo", "reason": "done"})
    assert response.status_code == 200
    assert response.json()["phase"] == pa.ANALYSIS
    assert agents.store.get_agent("demo-architect") is not None


def test_a_plan_request_without_a_name_is_refused(client):
    test_client, *_ = client
    assert test_client.post("/dashboard/api/projects/plan", json={}).status_code == 400


def test_global_tasks_cards_carry_the_project(client):
    test_client, projects, _agents, queue = client
    (task_id,) = queue.store.set_tasks("lane-a", [{"prompt": "work"}])
    queue.store.set_agent_binding(task_id, agent_id="demo-core", skill_ids=["core-engineering@1"])
    queue.store.set_task_project(task_id, "demo")

    body = test_client.get("/dashboard/api/tasks/board").json()
    card = next(row for column in ("backlog", "queued", "running", "blocked_review", "done")
                for row in body[column] if row["id"] == task_id)

    assert card["project_id"] == "demo"
    assert card["agent_id"] == "demo-core"
    assert card["skill_ids"] == ["core-engineering@1"]


def test_the_project_tools_work_on_a_default_build_with_nothing_injected(tmp_path, monkeypatch):
    """LIVE, hp-linux @ dbc5300: every project tool raised
    "'ProjectService' object has no attribute 'plan'".

    `projects` was already the name of the P0.7 ProjectService -- a composition
    view over queue/backlog/events, a completely different object -- and it is
    rebound further down build_mcp. The tool closures resolve the name at CALL
    time, so by then every project tool was calling the wrong thing. The test
    fixtures all passed the runtime in explicitly, which is exactly why they
    could not see it; this one builds the app the way production does."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=True, default_input=True))
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    server = build_mcp(TerminalService(config), queue=queue)

    runtime = queue.projects
    assert isinstance(runtime, ProjectRuntimeService), \
        "build_mcp must attach the project RUNTIME, not the P0.7 view"
    assert hasattr(runtime, "plan") and hasattr(runtime, "bootstrap")

    result = runtime.plan("default-build", description=BIG)
    assert result["plan"]["profile"]["complexity"] == pa.LARGE
