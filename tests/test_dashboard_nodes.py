"""dashboard.py's node-management routes (/dashboard/api/nodes, /node,
/node/drain, /node/test-connection, /nodes/{node_id}/heartbeat).

Every test here builds its own ControllerService with a tmp_path-scoped
NodeRegistry DB (never the real default ~/.local/state/terminal-mcp/
nodes.db) -- passing `controller=None` to register_dashboard would build
one against the REAL production registry path, which must never happen
from a test.
"""
from __future__ import annotations

import os

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry


def _config() -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )


def _client(tmp_path) -> tuple[TestClient, ControllerService]:
    service = TerminalService(_config())
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, controller


def test_nodes_list_shows_local_node_online_after_a_get(tmp_path):
    client, controller = _client(tmp_path)
    response = client.get("/dashboard/api/nodes")
    assert response.status_code == 200
    nodes = response.json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["id"] == "local"
    assert nodes[0]["status"] == "online"  # the GET itself triggers _refresh_local_heartbeat
    assert nodes[0]["capacity_status"] == "healthy"


def test_node_detail_returns_404_for_unknown_node(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.get("/dashboard/api/node", params={"id": "ghost"})
    assert response.status_code == 404
    assert response.json()["error"] == "NODE_NOT_FOUND"


def test_node_detail_missing_id_param_is_400(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.get("/dashboard/api/node")
    assert response.status_code == 400


def test_node_detail_for_local_includes_sessions(tmp_path):
    client, controller = _client(tmp_path)
    response = client.get("/dashboard/api/node", params={"id": "local"})
    assert response.status_code == 200
    body = response.json()
    assert body["node"]["id"] == "local"
    # terminal_list_sessions() reports the WHOLE real tmux server (with
    # allowed=False for non-whitelisted names), same as every other
    # discovery surface in this project -- not scoped to this test's own
    # disposable sessions, so just check the shape, not emptiness.
    assert isinstance(body["sessions"], list)
    assert all("name" in row and "allowed" in row for row in body["sessions"])


def test_drain_toggle_roundtrip(tmp_path):
    client, controller = _client(tmp_path)
    response = client.post("/dashboard/api/node/drain", json={"node_id": "local", "draining": True})
    assert response.status_code == 200
    assert response.json() == {"node_id": "local", "draining": True}
    assert controller.node_status("local").draining is True

    response2 = client.post("/dashboard/api/node/drain", json={"node_id": "local", "draining": False})
    assert response2.status_code == 200
    assert controller.node_status("local").draining is False


def test_drain_unknown_node_is_404(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.post("/dashboard/api/node/drain", json={"node_id": "ghost", "draining": True})
    assert response.status_code == 404


def test_drain_missing_node_id_is_400(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.post("/dashboard/api/node/drain", json={"draining": True})
    assert response.status_code == 400


def test_test_connection_local_ok(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.post("/dashboard/api/node/test-connection", json={"node_id": "local"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_test_connection_unknown_node_is_404(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.post("/dashboard/api/node/test-connection", json={"node_id": "ghost"})
    assert response.status_code == 404


def test_mutation_routes_reject_missing_origin_csrf_defense(tmp_path):
    service = TerminalService(_config())
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    no_origin_client = TestClient(server.streamable_http_app())  # no Origin header at all
    response = no_origin_client.post("/dashboard/api/node/drain", json={"node_id": "local", "draining": True})
    assert response.status_code in (400, 401, 403)


# -- remote heartbeat route: bearer-token auth, not the browser guard -------

def test_heartbeat_route_rejects_missing_token(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.post("/dashboard/api/nodes/m910/heartbeat", json={"metrics": {}, "tmux_session_count": 0})
    assert response.status_code == 401


def test_heartbeat_route_rejects_wrong_token(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_M910", "correct-token")
    client, _controller = _client(tmp_path)
    response = client.post("/dashboard/api/nodes/m910/heartbeat",
                           headers={"Authorization": "Bearer wrong-token"},
                           json={"metrics": {}, "tmux_session_count": 0})
    assert response.status_code == 401


def test_heartbeat_route_unregistered_node_is_404_even_with_correct_token(tmp_path, monkeypatch):
    # The token env var existing doesn't auto-register the node -- that's
    # a separate, explicit registry.register() step (task item 2's own
    # "Agent tự đăng ký" via register_remote_node, or manual admin action);
    # a correctly-authenticated push for a node nobody registered is still
    # a clean 404, never silently accepted as if it were the local node.
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_M910", "correct-token")
    client, controller = _client(tmp_path)
    response = client.post("/dashboard/api/nodes/m910/heartbeat",
                           headers={"Authorization": "Bearer correct-token"},
                           json={"metrics": {}, "tmux_session_count": 0})
    assert response.status_code == 404
    assert response.json()["error"] == "NODE_NOT_FOUND"


def test_heartbeat_route_accepts_correct_token_for_registered_node(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_M910", "correct-token")
    client, controller = _client(tmp_path)
    controller.registry.register("m910", display_name="M910", hostname="m910-host", endpoint="http://192.168.1.50:8790")

    metrics = {
        "cpu_percent": 15.0, "load1": 0.5, "load5": 0.5, "load15": 0.5, "cpu_count": 16,
        "ram_total_bytes": 32_000_000_000, "ram_used_bytes": 8_000_000_000, "ram_percent": 25.0,
        "swap_total_bytes": 0, "swap_used_bytes": 0, "swap_percent": 0.0,
        "disk_total_bytes": 1_000_000_000_000, "disk_used_bytes": 100_000_000_000,
        "disk_free_bytes": 900_000_000_000, "disk_percent": 10.0,
    }
    response = client.post("/dashboard/api/nodes/m910/heartbeat",
                           headers={"Authorization": "Bearer correct-token"},
                           json={"metrics": metrics, "tmux_session_count": 3,
                                 "agent_counts": {"claude": 2}, "agent_types": ["shell", "claude"],
                                 "agent_version": "0.13.0", "labels": ["m910"]})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "node_id": "m910"}

    node = controller.node_status("m910")
    assert node.status == "online"
    assert node.tmux_session_count == 3
    assert node.agent_counts == {"claude": 2}


def test_heartbeat_route_token_is_per_node_not_shared(tmp_path, monkeypatch):
    # A valid token for a DIFFERENT node must not authenticate this one --
    # env var lookup is namespaced per node_id (TERMINAL_MCP_NODE_TOKEN_
    # <NODE_ID>), so this proves node A's token can't be replayed as node B's.
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_NODE_A", "token-a")
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_NODE_B", "token-b")
    client, controller = _client(tmp_path)
    controller.registry.register("node-b", display_name="B", hostname="h", endpoint="http://x")
    response = client.post("/dashboard/api/nodes/node-b/heartbeat",
                           headers={"Authorization": "Bearer token-a"},
                           json={"metrics": {}, "tmux_session_count": 0})
    assert response.status_code == 401


def test_nodes_list_route_never_touches_production_registry_path(tmp_path, monkeypatch):
    # Guards the test-infra invariant itself: if register_dashboard's
    # default (controller=None) path were ever used by accident in a
    # test, it would read/write the REAL production nodes.db. This proves
    # our test fixture's explicit controller keeps the real default path
    # untouched by pointing TERMINAL_MCP_NODE_REGISTRY_DB somewhere that
    # would blow up if touched, then confirming default_registry_path is
    # never even consulted by our explicit-controller client.
    monkeypatch.setenv("TERMINAL_MCP_NODE_REGISTRY_DB", "/nonexistent-dir-must-not-be-touched/nodes.db")
    client, _controller = _client(tmp_path)
    response = client.get("/dashboard/api/nodes")
    assert response.status_code == 200  # succeeded using OUR tmp_path registry, not the env-var default


# -- /dashboard/nodes: the Nodes admin page itself (task item 4/16) ---------

def test_nodes_admin_page_served_with_read_guard(tmp_path):
    client, _controller = _client(tmp_path)
    response = client.get("/dashboard/nodes")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"
    assert "<title>Quản lý node</title>" in response.text
    # Talks to the exact same JSON routes covered above -- no separate,
    # undocumented backend surface for this page.
    assert "/dashboard/api/nodes" in response.text
    assert "/dashboard/api/node/drain" in response.text
    assert "/dashboard/api/node/test-connection" in response.text


def test_main_dashboard_links_to_the_nodes_admin_page():
    from terminal_mcp.dashboard import DASHBOARD_HTML
    assert 'href="/dashboard/nodes"' in DASHBOARD_HTML
    # Exactly one nav entry for it -- not a second, competing top list
    # (the earlier dashboard UI cleanup task's own constraint).
    assert DASHBOARD_HTML.count('href="/dashboard/nodes"') == 1


def test_sessions_sidebar_rows_carry_local_node_label(tmp_path, tmux_session_factory):
    tmux_session_factory("test-nodelabel")
    client, controller = _client(tmp_path)
    response = client.get("/dashboard/api/sessions")
    assert response.status_code == 200
    rows = response.json()["sessions"]
    row = next(r for r in rows if r["name"] == "test-nodelabel")
    assert row["node_id"] == "local"
    assert row["node_name"] == controller.node_status("local").display_name


def test_nodes_admin_page_read_guard_matches_main_dashboard(tmp_path):
    # Same guard function as every other read-only dashboard page --
    # blocked exactly when the main /dashboard page would be.
    service = TerminalService(_config())
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    no_origin_client = TestClient(server.streamable_http_app())
    response = no_origin_client.get("/dashboard/nodes")
    main_response = no_origin_client.get("/dashboard")
    assert response.status_code == main_response.status_code
