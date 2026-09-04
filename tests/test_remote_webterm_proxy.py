"""Open Terminal for a REMOTE node, end-to-end: a real terminal-node-agent
subprocess (a real second process on a real port, exactly mirroring what
a genuine remote node would be) registered with a controller, and a real
WebSocket connection through the dashboard's /dashboard/ws/terminal route
-- which must detect the session isn't local and proxy the connection to
that real subprocess's own /v1/ws/terminal route (node_agent.py).

This is the one piece of the multi-node Windows support work that
couldn't be exercised via an in-process fake: the actual async relay
(dashboard.py's _proxy_remote_terminal_ws) opens a REAL outbound
`websockets.connect()` to a REAL server here, not a mock of the
websockets library's own API -- exactly the kind of assumption a unit
test could get subtly wrong without ever catching it.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import pytest
import yaml
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, DashboardConfig
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.host_metrics import NodeMetrics

REMOTE_TOKEN = "remote-webterm-proxy-test-token"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"remote node-agent did not listen on {host}:{port}")


@pytest.fixture
def remote_node_agent(tmp_path):
    """A REAL terminal-node-agent subprocess, on a real port, with its
    own isolated config/state -- never the real production deployment."""
    port = _free_port()
    workspace = tmp_path / "remote-workspace"
    workspace.mkdir()
    config_path = tmp_path / "remote-config.yaml"
    config_path.write_text(yaml.safe_dump({
        "permissions": {"terminal_read": True, "terminal_input": True},
        "allowed_session_patterns": ["remoteterm-*"],
        "input_policy": {"allowed_session_patterns": ["remoteterm-*"]},
        "session_lifecycle": {"enabled": True, "allowed_cwd_roots": [str(workspace)]},
    }))
    env = os.environ.copy()
    env["TERMINAL_MCP_CONFIG"] = str(config_path)
    env["TERMINAL_MCP_BINDINGS_DB"] = str(tmp_path / "remote-bindings.db")
    env["TERMINAL_MCP_AUDIT_DB"] = str(tmp_path / "remote-audit.db")
    env["TERMINAL_MCP_NODE_TOKEN"] = REMOTE_TOKEN
    process = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp.node_agent",
         "--node-id", "remote-webterm", "--controller-url", "http://127.0.0.1:1",
         "--host", "127.0.0.1", "--port", str(port), "--heartbeat-interval-seconds", "3600"],
        cwd=str(__import__("pathlib").Path(__file__).parents[1]),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield port, workspace
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _dashboard_config() -> AppConfig:
    # The LOCAL (controller-side) config's own whitelist gates web
    # terminal READ access even for a session that turns out to live on
    # a remote node (see dashboard.py's _resolve_remote_web_terminal_
    # access docstring: read authorization is settled BEFORE the remote
    # fallback is ever reached) -- "remoteterm-*" must be allowed here
    # for that check to pass and fall through to the remote lookup,
    # exactly like a real operator's config.yaml would need to allow it.
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "remoteterm-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*", "remoteterm-*")),
        dashboard=DashboardConfig(web_terminal_enabled=True),
    )


def _dashboard_client(tmp_path, remote_port):
    service = TerminalService(_dashboard_config())
    registry = NodeRegistry(tmp_path / "controller-nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=("shell",), agent_version=None)
    controller.register_remote_node("remote-webterm", display_name="Remote", hostname="remote-host",
                                    endpoint=f"http://127.0.0.1:{remote_port}", token=REMOTE_TOKEN)
    controller.registry.heartbeat(
        "remote-webterm",
        metrics=NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                            ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                            swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                            disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                            disk_free_bytes=99_000_000_000, disk_percent=1.0),
        tmux_session_count=0, agent_counts={}, agent_types=("shell",), agent_version=None, labels=(),
    )
    server = build_mcp(service)
    register_dashboard(server, service, controller=controller)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, controller


def _drain_until(ws, needle: bytes, *, deadline_seconds: float = 10.0) -> bytes:
    deadline = time.monotonic() + deadline_seconds
    buffer = b""
    while time.monotonic() < deadline:
        message = ws.receive()
        if message.get("type") == "websocket.disconnect":
            pytest.fail(f"disconnected before seeing {needle!r}; got so far: {buffer!r}")
        data = message.get("bytes")
        if data is not None:
            buffer += data
            if needle in buffer:
                return buffer
    pytest.fail(f"timed out waiting for {needle!r}; got so far: {buffer!r}")


def test_open_terminal_proxies_to_a_real_remote_node(remote_node_agent, tmp_path):
    remote_port, workspace = remote_node_agent
    client, controller = _dashboard_client(tmp_path, remote_port)

    # Create the session ON THE REMOTE via the controller's own routed
    # create -- proves the whole chain (scheduler/routing/create) lands
    # it on the real remote process, not locally.
    created = controller.terminal_create_session("remoteterm-webterm", "shell", str(workspace), node="remote-webterm")
    assert created.get("error") is None, created
    assert created["node_id"] == "remote-webterm"

    try:
        with client.websocket_connect("/dashboard/ws/terminal?session=remoteterm-webterm") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert ready["readonly"] is False
            ws.send_bytes(b"echo remote_proxy_marker\n")
            _drain_until(ws, b"remote_proxy_marker")
    finally:
        controller.terminal_kill_session("remoteterm-webterm", "remoteterm-webterm")


def test_open_terminal_remote_session_not_found_is_404(tmp_path, remote_node_agent):
    remote_port, _workspace = remote_node_agent
    client, _controller = _dashboard_client(tmp_path, remote_port)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/dashboard/ws/terminal?session=remoteterm-does-not-exist-anywhere"):
            pass
    assert excinfo.value.code == 4404
