"""Phase B on the ONE advertised surface, and on the screens people read.

The compact `turn` surface is the only tool a connector sees, so an Agent
runtime it cannot reach is a runtime that needs a second control plane -- the
exact thing that surface exists to prevent. And an Agents page that does not
say which session an agent currently holds is a page that cannot answer the
question Phase B was built for.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.agent_registry import AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.compact_tools import (
    AGENT_ARGS, AGENT_REQUIRED, TURN_ACTION_ALIASES, TURN_ACTIONS, TURN_HANDLER_ACTIONS,
    CompactTerminalTools,
)
from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import AGENTS_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


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
# terminal_turn mappings.
# ---------------------------------------------------------------------------

PHASE_B_ACTIONS = ("agent_start", "list_agents", "get_agent", "create_agent", "update_agent",
                   "list_skills", "register_skill", "bind_agent_skill", "cleanup_candidates")


@pytest.mark.parametrize("action", PHASE_B_ACTIONS)
def test_every_phase_b_action_is_advertised_and_routed(action):
    assert action in TURN_ACTIONS
    assert action in TURN_HANDLER_ACTIONS
    assert action in AGENT_ARGS


@pytest.mark.parametrize("alias,canonical", [
    ("agents", "list_agents"), ("agent", "get_agent"), ("skills", "list_skills"),
    ("skill", "register_skill"), ("bind", "bind_agent_skill"),
    ("bind_skill", "bind_agent_skill"), ("run_agent", "agent_start"),
    ("cleanup", "cleanup_candidates"), ("stale_sessions", "cleanup_candidates"),
])
def test_phase_b_aliases_resolve(alias, canonical):
    handler = Recorder()
    tools = _tools({TURN_HANDLER_ACTIONS[canonical]: handler})
    result = tools.turn(action=alias, target="builder", text="lint")
    assert result["action"] == canonical
    assert len(handler.calls) == 1


def test_agent_start_takes_the_agent_from_target_and_the_prompt_from_text():
    handler = Recorder({"status": "TASK_STARTED", "task_id": "t1", "session": "agent-a",
                        "routing_state": "BOUND", "dispatched": True, "poll": False,
                        "agent_id": "builder", "skills": ["lint@1"]})
    tools = _tools({"agent_start": handler})

    result = tools.turn(action="agent_start", target="builder", text="do the work")

    assert handler.calls[0] == {"agent_id": "builder", "prompt": "do the work"}
    assert result["status"] == "OK"
    assert result["task_id"] == "t1"
    assert result["session"] == "agent-a"
    assert result["agent_id"] == "builder"
    assert result["skills"] == ["lint@1"]
    assert result["poll"] is False


def test_bind_agent_skill_reads_both_ids_from_the_two_positionals():
    handler = Recorder()
    tools = _tools({"bind_agent_skill": handler})
    tools.turn(action="bind_agent_skill", target="builder", text="lint",
               args={"kind": "TASK", "version": "2"})
    assert handler.calls[0] == {"agent_id": "builder", "skill_id": "lint",
                                "kind": "TASK", "version": "2"}


def test_a_missing_required_argument_is_named_rather_than_crashing():
    tools = _tools({"agent_start": Recorder()})
    result = tools.turn(action="agent_start", text="work")
    assert result["error"] == "MISSING_ARGS"
    assert result["missing"] == ["agent_id"]


def test_an_unknown_argument_is_refused_by_name():
    tools = _tools({"create_agent": Recorder()})
    result = tools.turn(action="create_agent", target="builder", args={"nonsense": 1})
    assert result["error"] == "UNKNOWN_ARGS"
    assert result["unknown"] == ["nonsense"]


def test_an_unwired_phase_b_action_refuses_honestly():
    tools = _tools({})
    assert tools.turn(action="list_agents")["error"] == "ACTION_UNAVAILABLE"


def test_cleanup_candidates_is_reachable_and_read_only():
    handler = Recorder({"candidates": [], "report_only": True})
    tools = _tools({"cleanup_candidates": handler})
    result = tools.turn(action="cleanup_candidates", args={"limit": 5})
    assert handler.calls[0] == {"limit": 5}
    assert result["result"]["report_only"] is True


def test_every_required_arg_is_one_the_action_actually_accepts():
    """A required field outside the allowed set could never be supplied."""
    for action, required in AGENT_REQUIRED.items():
        assert set(required) <= AGENT_ARGS[action], action


def test_legacy_actions_are_untouched_by_phase_b():
    tools = _tools({"route_start": Recorder()})
    assert tools.turn(action="start", text="x")["error"] == "TARGET_REQUIRED"
    assert tools.turn(action="route_start")["error"] == "TEXT_REQUIRED"
    for legacy in ("inspect", "send", "send_wait", "wait", "resume", "start",
                   "enqueue_task", "task_status", "route_start"):
        assert legacy in TURN_ACTIONS


# ---------------------------------------------------------------------------
# Agents dashboard.
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
    service = TerminalService(config)
    server = build_mcp(service, queue=queue, agents=agents)
    register_dashboard(server, service, queue=queue, agents=agents)
    return TestClient(server.streamable_http_app(),
                      headers={"Origin": "http://testserver"}), agents, queue


def test_the_agents_page_is_served(client):
    test_client, _agents, _queue = client
    response = test_client.get("/dashboard/agents")
    assert response.status_code == 200
    assert "Agents" in response.text


def test_the_agents_api_reports_identity_runtime_and_skills(client):
    test_client, agents, queue = client
    agents.store.create_agent("builder", name="Builder", project_id="nova", model="opus")
    agents.store.register_skill("lint", version="1", body="x")
    agents.store.bind_skill("builder", "lint")
    (task_id,) = queue.store.set_tasks("lane-a", [{"prompt": "work"}])
    queue.store.set_agent_binding(task_id, agent_id="builder", skill_ids=["lint@1"])
    queue.store.bind_task_to_session(task_id, "runner-1", node_id="hp-linux",
                                     evidence={"reason": "score 40"})

    body = test_client.get("/dashboard/api/agents").json()
    agent = body["agents"][0]

    assert agent["id"] == "builder"
    assert agent["project_id"] == "nova"
    assert agent["model"] == "opus"
    assert agent["current_task"]["session"] == "runner-1"
    assert agent["current_task"]["node_id"] == "hp-linux"
    assert [skill["skill_id"] for skill in agent["skills"]] == ["lint"]
    assert agent["at_capacity"] is True
    assert "recent_runs" in agent


def test_the_agents_page_renders_every_field_the_runtime_answer_needs():
    for field in ("current task", "runtime session", "node", "model", "context",
                  "queue", "project", "max sessions"):
        assert field in AGENTS_HTML, field
    assert "recent_runs" in AGENTS_HTML
    assert "at_capacity" in AGENTS_HTML


def test_global_tasks_cards_carry_agent_and_skills(client):
    test_client, _agents, queue = client
    (task_id,) = queue.store.set_tasks("lane-a", [{"prompt": "work"}])
    queue.store.set_agent_binding(task_id, agent_id="builder", skill_ids=["lint@1", "qa@3"])

    body = test_client.get("/dashboard/api/tasks/board").json()
    card = next(row for column in ("backlog", "queued", "running", "blocked_review", "done")
                for row in body[column] if row["id"] == task_id)

    assert card["agent_id"] == "builder"
    assert card["skill_ids"] == ["lint@1", "qa@3"]
