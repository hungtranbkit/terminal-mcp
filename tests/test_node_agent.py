"""node_agent.py: the terminal-node-agent HTTP surface (build_node_agent)
and its background heartbeat loop's failure resilience.

Uses Starlette's TestClient (in-process ASGI, no real socket/subprocess
needed) against a REAL TerminalService + real tmux sessions -- this
exercises the exact route handlers/auth/JSON marshalling a real remote
node would run, without the flakiness of picking a free port. A separate,
narrower live-subprocess smoke test was already run manually against a
real second process on a real port during development (see
docs/multi-node.md) -- this suite is the permanent, deterministic
regression coverage for the same surface.
"""
from __future__ import annotations

import subprocess

import anyio
import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.node_agent import _heartbeat_loop, build_node_agent

TOKEN = "test-node-token-abc123"


def _config(tmp_path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True),
        allowed_session_patterns=("agent-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=(),
            launch_commands=(("claude", "true"),),
        ),
    )


@pytest.fixture
def agent_client(tmp_path):
    # grants (and audit/bindings/leases/killed_sessions) default to this
    # host's REAL ~/.local/state/terminal-mcp/*.db when not given
    # explicitly -- isolated here so test_grant_read_and_grant_input_
    # round_trip (and any future grant-mutating test in this file) can
    # never leak a test session name ("agent-grant") into real
    # production state (found live -- since cleaned up).
    from terminal_mcp.grants import SessionGrantStore
    terminal = TerminalService(_config(tmp_path), grants=SessionGrantStore(tmp_path / "grants.db"))
    app = build_node_agent(node_id="test-node", terminal=terminal, token=TOKEN, workspace_root=str(tmp_path))
    created: list[str] = []
    client = TestClient(app)
    client.terminal = terminal
    client.created = created
    yield client
    for name in created:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# -- auth ------------------------------------------------------------------

def test_health_needs_no_auth(agent_client):
    response = agent_client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["node_id"] == "test-node"


def test_every_other_route_rejects_missing_token(agent_client):
    response = agent_client.get("/v1/sessions")
    assert response.status_code == 401
    assert response.json()["error"] == "UNAUTHORIZED"


def test_every_other_route_rejects_wrong_token(agent_client):
    response = agent_client.get("/v1/sessions", headers=_auth("wrong-token"))
    assert response.status_code == 401


def test_correct_token_is_accepted(agent_client):
    response = agent_client.get("/v1/sessions", headers=_auth())
    assert response.status_code == 200
    assert "sessions" in response.json()


def test_metrics_route_returns_real_host_metrics(agent_client):
    response = agent_client.get("/v1/metrics", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert "cpu_percent" in body
    assert "ram_percent" in body


# -- full session CRUD round trip through real tmux -------------------------

def test_full_session_lifecycle_round_trip(agent_client):
    create = agent_client.post("/v1/sessions", headers=_auth(),
                               json={"name": "agent-rt", "agent_type": "shell", "cwd": None})
    assert create.status_code == 200
    assert "error" not in create.json()
    agent_client.created.append("agent-rt")

    status = agent_client.get("/v1/sessions/agent-rt/status", headers=_auth())
    assert status.status_code == 200
    assert status.json().get("error") is None

    send = agent_client.post("/v1/sessions/agent-rt/send", headers=_auth(),
                             json={"text": "echo node-agent-test", "press_enter": True})
    assert send.status_code == 200
    assert send.json().get("error") is None

    tail = agent_client.get("/v1/sessions/agent-rt/tail", headers=_auth(), params={"lines": 10})
    assert tail.status_code == 200

    listing = agent_client.get("/v1/sessions", headers=_auth())
    assert any(row["name"] == "agent-rt" for row in listing.json()["sessions"])

    kill = agent_client.post("/v1/sessions/agent-rt/kill", headers=_auth(),
                             json={"confirm_name": "agent-rt"})
    assert kill.status_code == 200
    assert kill.json().get("error") is None

    reopen = agent_client.post("/v1/sessions/agent-rt/reopen", headers=_auth(), json={})
    assert reopen.status_code == 200
    assert reopen.json().get("error") is None

    killed_list = agent_client.get("/v1/killed-sessions", headers=_auth())
    assert killed_list.status_code == 200


def test_grant_read_and_grant_input_round_trip(agent_client):
    # The node-agent-side half of the multi-node grant routing fix: the
    # controller's RemoteNodeClient calls THESE routes for a session that
    # lives on this node -- must apply to this node's own TerminalService.
    create = agent_client.post("/v1/sessions", headers=_auth(),
                               json={"name": "agent-grant", "agent_type": "shell", "cwd": None})
    assert create.status_code == 200
    agent_client.created.append("agent-grant")

    grant_read = agent_client.post("/v1/sessions/agent-grant/grant-read", headers=_auth(),
                                   json={"enabled": True, "granted_by": "op@example.com"})
    assert grant_read.status_code == 200
    body = grant_read.json()
    assert body.get("error") is None
    assert body["read_enabled"] is True
    assert agent_client.terminal.grants.get("agent-grant").read_enabled is True

    grant_input = agent_client.post("/v1/sessions/agent-grant/grant-input", headers=_auth(),
                                    json={"enabled": True})
    assert grant_input.status_code == 200
    assert grant_input.json()["input_enabled"] is True

    revoke = agent_client.post("/v1/sessions/agent-grant/grant-read", headers=_auth(),
                               json={"enabled": False})
    assert revoke.status_code == 200
    assert revoke.json()["read_enabled"] is False
    assert revoke.json()["input_enabled"] is False  # revoking read cascades, same as calling TerminalService directly


def test_grant_routes_require_auth(agent_client):
    response = agent_client.post("/v1/sessions/agent-grant/grant-read", json={"enabled": True})
    assert response.status_code == 401
    response2 = agent_client.post("/v1/sessions/agent-grant/grant-input", json={"enabled": True})
    assert response2.status_code == 401


def test_status_of_unknown_session_is_a_normal_200_not_a_transport_error(agent_client):
    # node_agent.py never uses a non-200 status for application-level
    # results (see node_client.py's own RemoteNodeClient docstring for why
    # this distinction matters) -- a nonexistent session comes back as a
    # normal 200 with exists:false, exactly like calling TerminalService
    # directly would, never an HTTP-level error.
    response = agent_client.get("/v1/sessions/agent-does-not-exist/status", headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is False
    assert "error" not in body


def test_delete_route_uses_http_delete_method(agent_client):
    agent_client.post("/v1/sessions", headers=_auth(), json={"name": "agent-del", "agent_type": "shell"})
    agent_client.created.append("agent-del")
    response = agent_client.delete("/v1/sessions/agent-del", headers=_auth())
    assert response.status_code == 200


# -- heartbeat loop resilience -----------------------------------------------

@pytest.mark.anyio
async def test_heartbeat_loop_survives_unreachable_controller():
    # Points at a port nothing is listening on -- the loop must catch the
    # connection failure and simply return after one iteration instead of
    # raising, so a real controller outage can never crash the agent
    # process or the tmux sessions it's serving underneath it.
    terminal = TerminalService(_config_for_loop_test())
    with anyio.move_on_after(2.0) as scope:
        await _heartbeat_loop(
            node_id="test-node", terminal=terminal, controller_url="http://127.0.0.1:1",
            token=TOKEN, workspace_root="/", interval_seconds=100.0,  # long sleep -- we only need ONE failed attempt
        )
    # move_on_after cancels _heartbeat_loop's internal `await anyio.sleep`
    # after the first failed push -- reaching here at all (no exception
    # propagated out of the `with` block) IS the assertion.
    assert scope.cancelled_caught


def _config_for_loop_test() -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("agent-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=("/tmp",), protected_sessions=()),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"
