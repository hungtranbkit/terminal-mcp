"""dashboard.py's LAN-discovery/remote-connect routes -- CSRF/auth guard,
SSRF rejection (route layer, on top of remote_connect.py's own unit
tests), no-duplicate-node registration, credential never appearing in a
response, Windows always-manual-fallback, and a REAL end-to-end agent-
token connect against a real terminal-node-agent subprocess (mirroring
test_remote_webterm_proxy.py's own real-subprocess pattern) proving the
scheduler sees the newly-connected node once it reports healthy.

SSH bootstrap/test routes are exercised here with remote_connect's own
module-level functions monkeypatched (deterministic, no real network) --
remote_connect.py's own test suite already covers the real subprocess/
argv-building/host-key-pinning mechanics in detail, including a real
localhost-sshd smoke path (pytest -m real_ssh)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from terminal_mcp import remote_connect
from terminal_mcp.config import AppConfig, DashboardConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.connection_store import ConnectionStore
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry


def _config(tmp_path, allow_public: bool = False) -> AppConfig:
    from terminal_mcp.config import NodesConfig, RemoteConnectConfig
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(web_terminal_enabled=True),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
        nodes=NodesConfig(remote_connect=RemoteConnectConfig(allow_public_manual_add=allow_public)),
    )


def _client(tmp_path, **config_kwargs):
    config = _config(tmp_path, **config_kwargs)
    service = TerminalService(config)
    server = build_mcp(service)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service), local_workspace_root=str(tmp_path))
    connection_store = ConnectionStore(tmp_path / "connections.db")
    register_dashboard(server, service, controller=controller, connection_store=connection_store)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, controller, connection_store


# ---------------------------------------------------------------------------
# CSRF/auth guard applies to every new route exactly like every existing one
# ---------------------------------------------------------------------------


def test_discovery_scan_requires_origin(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())  # deliberately NO Origin header
    r = client.post("/dashboard/api/nodes/discovery/scan", json={})
    assert r.status_code == 403
    assert r.json()["error"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.parametrize("path,body", [
    ("/dashboard/api/nodes/discovery/scan", {}),
    ("/dashboard/api/nodes/discovery/cancel", {}),
    ("/dashboard/api/nodes/connect/ssh/trust-hostkey", {"transport_type": "lan_ssh", "host": "1.2.3.4", "username": "x"}),
    ("/dashboard/api/nodes/connect/ssh/test", {"transport_type": "lan_ssh", "host": "1.2.3.4", "username": "x"}),
    ("/dashboard/api/nodes/connect/ssh/bootstrap", {}),
    ("/dashboard/api/nodes/connect/windows/bootstrap", {}),
    ("/dashboard/api/nodes/connect/agent-token", {}),
])
def test_every_connect_route_requires_csrf_origin(tmp_path, path, body):
    client, _controller, _store = _client(tmp_path)
    r = client.post(path, json=body, headers={"Origin": "http://evil.example.com"})
    assert r.status_code == 403
    assert r.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_discovery_status_is_a_read_route_no_csrf_needed(tmp_path):
    config = _config(tmp_path)
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())  # no Origin -- fine for a GET
    r = client.get("/dashboard/api/nodes/discovery/status")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# SSRF -- route-layer confirmation (unit coverage already in
# test_remote_connect.py; this proves the ROUTE actually calls the
# validator before doing anything else).
# ---------------------------------------------------------------------------


def test_connect_agent_token_rejects_public_endpoint(tmp_path):
    client, _controller, _store = _client(tmp_path)
    r = client.post("/dashboard/api/nodes/connect/agent-token",
                    json={"node_id": "n1", "endpoint": "http://8.8.8.8:8790", "token": "x"})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_REQUEST"


def test_connect_agent_token_allows_public_when_config_opts_in(tmp_path):
    client, _controller, _store = _client(tmp_path, allow_public=True)
    # Still fails (nothing real listens at 8.8.8.8:8790) but for the
    # RIGHT reason (unreachable), not SSRF rejection -- proves the opt-in
    # config flag actually reaches the route.
    r = client.post("/dashboard/api/nodes/connect/agent-token",
                    json={"node_id": "n1", "endpoint": "http://8.8.8.8:8790", "token": "x"})
    assert r.status_code == 502
    assert r.json()["error"] == "AGENT_NOT_REACHABLE"


def test_connect_ssh_test_rejects_public_ip(tmp_path):
    client, _controller, _store = _client(tmp_path)
    r = client.post("/dashboard/api/nodes/connect/ssh/test",
                    json={"transport_type": "lan_ssh", "host": "8.8.8.8", "username": "pi"})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_REQUEST"


def test_connect_ssh_test_rejects_bad_transport_type(tmp_path):
    client, _controller, _store = _client(tmp_path)
    r = client.post("/dashboard/api/nodes/connect/ssh/test",
                    json={"transport_type": "telnet", "host": "1.2.3.4", "username": "pi"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# SSH flow, monkeypatched at the remote_connect module boundary --
# deterministic, no real network/SSH needed here.
# ---------------------------------------------------------------------------


def test_connect_ssh_trust_hostkey_then_test_reports_ok(tmp_path, monkeypatch):
    client, _controller, _store = _client(tmp_path)

    def fake_probe_host_key(target, runner=None, timeout=15.0):
        return remote_connect.HostKeyProbeResult(ok=True, fingerprint="SHA256:fixed", key_line="1.2.3.4 ssh-ed25519 AAA")
    monkeypatch.setattr(remote_connect, "probe_host_key", fake_probe_host_key)

    body = {"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi"}
    r = client.post("/dashboard/api/nodes/connect/ssh/test", json=body)
    assert r.json()["stage"] == "host_key_new"

    r = client.post("/dashboard/api/nodes/connect/ssh/trust-hostkey", json=body)
    assert r.status_code == 200
    assert r.json()["fingerprint"] == "SHA256:fixed"

    r = client.post("/dashboard/api/nodes/connect/ssh/test", json=body)
    assert r.json()["stage"] == "ok"


def test_connect_ssh_bootstrap_refuses_without_trusted_hostkey(tmp_path):
    client, _controller, _store = _client(tmp_path)
    body = {"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi", "node_id": "pi01",
           "controller_url": "http://10.0.0.1:8766", "credential": {"password": "secret"}}
    r = client.post("/dashboard/api/nodes/connect/ssh/bootstrap", json=body)
    assert r.status_code == 409
    assert r.json()["error"] == "HOST_KEY_NOT_TRUSTED"


def test_connect_ssh_bootstrap_never_echoes_the_credential_back(tmp_path, monkeypatch):
    client, controller, store = _client(tmp_path)
    monkeypatch.setattr(remote_connect, "probe_host_key",
                        lambda target, runner=None, timeout=15.0: remote_connect.HostKeyProbeResult(
                            ok=True, fingerprint="SHA256:fixed", key_line="192.168.1.50 ssh-ed25519 AAA"))
    monkeypatch.setattr(remote_connect, "test_connection",
                        lambda *a, **k: remote_connect.ConnectionTestResult(stage="ok", ok=True, fingerprint="SHA256:fixed"))
    client.post("/dashboard/api/nodes/connect/ssh/trust-hostkey",
               json={"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi"})

    SECRET = "the-super-secret-password-must-never-leak"

    def fake_bootstrap(target, credential, **kwargs):
        assert credential.password == SECRET  # the backend DID receive it (used once, in memory)
        return remote_connect.BootstrapResult(ok=False, stdout="normal output, no secret here",
                                              stderr="normal stderr, no secret here", returncode=1)
    monkeypatch.setattr(remote_connect, "run_linux_bootstrap", fake_bootstrap)

    body = {"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi", "node_id": "pi01",
           "controller_url": "http://10.0.0.1:8766", "credential": {"password": SECRET}}
    r = client.post("/dashboard/api/nodes/connect/ssh/bootstrap", json=body)
    assert r.status_code == 502
    assert SECRET not in r.text  # never echoed back, even on failure


def test_connect_ssh_bootstrap_success_registers_node_and_saves_connection(tmp_path, monkeypatch):
    client, controller, store = _client(tmp_path)
    monkeypatch.setattr(remote_connect, "probe_host_key",
                        lambda target, runner=None, timeout=15.0: remote_connect.HostKeyProbeResult(
                            ok=True, fingerprint="SHA256:fixed", key_line="192.168.1.50 ssh-ed25519 AAA"))
    client.post("/dashboard/api/nodes/connect/ssh/trust-hostkey",
               json={"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi"})

    monkeypatch.setattr(remote_connect, "run_linux_bootstrap",
                        lambda target, credential, **kwargs: remote_connect.BootstrapResult(
                            ok=True, stdout="bootstrap ok", stderr="", returncode=0))

    from terminal_mcp.node_client import RemoteNodeClient
    monkeypatch.setattr(RemoteNodeClient, "ping", lambda self: (True, 1.2, None))

    body = {"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi", "node_id": "pi01",
           "controller_url": "http://10.0.0.1:8766", "credential": {"password": "x"}}
    r = client.post("/dashboard/api/nodes/connect/ssh/bootstrap", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert data["node_id"] == "pi01"
    assert controller.node_status("pi01") is not None

    saved = store.get("pi01")
    assert saved is not None
    assert saved.transport_type == "lan_ssh"
    assert saved.host_key_fingerprint == "SHA256:fixed"
    token = store.read_token(saved.token_file)
    assert token is not None
    # The token was generated server-side and is NOT the literal
    # "x" password the operator typed -- never persisted, never reused as
    # the node-agent's own bearer secret.
    assert token != "x"


def test_connect_ssh_bootstrap_duplicate_node_id_rejected(tmp_path, monkeypatch):
    client, controller, store = _client(tmp_path)
    controller.register_remote_node("pi01", display_name="pi01", hostname="pi01", endpoint="http://1.2.3.4:8790", token="t")
    body = {"transport_type": "lan_ssh", "host": "192.168.1.50", "username": "pi", "node_id": "pi01",
           "controller_url": "http://10.0.0.1:8766", "credential": {"password": "x"}}
    r = client.post("/dashboard/api/nodes/connect/ssh/bootstrap", json=body)
    assert r.status_code == 409
    assert r.json()["error"] == "NODE_ALREADY_EXISTS"


def test_connect_cloudflare_ssh_bootstrap_requires_agent_endpoint_host(tmp_path, monkeypatch):
    client, _controller, _store = _client(tmp_path)
    monkeypatch.setattr(remote_connect, "probe_host_key",
                        lambda target, runner=None, timeout=15.0: remote_connect.HostKeyProbeResult(
                            ok=True, fingerprint="SHA256:fixed", key_line="ssh.example.com ssh-ed25519 AAA"))
    client.post("/dashboard/api/nodes/connect/ssh/trust-hostkey",
               json={"transport_type": "cloudflare_ssh", "host": "ssh.example.com", "username": "pi"})
    body = {"transport_type": "cloudflare_ssh", "host": "ssh.example.com", "username": "pi", "node_id": "m910",
           "controller_url": "http://10.0.0.1:8766", "credential": {"password": "x"}}  # no agent_endpoint_host
    r = client.post("/dashboard/api/nodes/connect/ssh/bootstrap", json=body)
    assert r.status_code == 400
    assert r.json()["error"] == "AGENT_ENDPOINT_REQUIRED"


# ---------------------------------------------------------------------------
# Windows -- always manual, never claims a live install.
# ---------------------------------------------------------------------------


def test_connect_windows_bootstrap_always_manual(tmp_path):
    client, _controller, _store = _client(tmp_path)
    r = client.post("/dashboard/api/nodes/connect/windows/bootstrap",
                    json={"node_id": "winbox", "hostname": "winbox.local", "controller_url": "http://10.0.0.1:8766"})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "manual_required"
    assert "install-node-agent.ps1" in data["install_command"]


# ---------------------------------------------------------------------------
# Rate limit on rescan (cooldown) -- route-level confirmation.
# ---------------------------------------------------------------------------


def test_discovery_scan_cooldown_prevents_immediate_rescan(tmp_path, monkeypatch):
    from terminal_mcp import lan_discovery
    monkeypatch.setattr(lan_discovery, "local_ipv4_subnets", lambda max_hosts: [])
    client, _controller, _store = _client(tmp_path)
    with client:
        r1 = client.post("/dashboard/api/nodes/discovery/scan", json={})
        assert r1.json()["started"] is True
        r2 = client.post("/dashboard/api/nodes/discovery/scan", json={})
        assert r2.json()["started"] is False  # cooldown -- same scan_id returned, not a new one
        assert r2.json()["scan_id"] == r1.json()["scan_id"]


def test_discovery_disabled_returns_403(tmp_path):
    from terminal_mcp.config import NodesConfig, DiscoveryConfig
    config = _config(tmp_path)
    from dataclasses import replace
    config = replace(config, nodes=replace(config.nodes, discovery=DiscoveryConfig(enabled=False)))
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    r = client.post("/dashboard/api/nodes/discovery/scan", json={})
    assert r.status_code == 403
    assert r.json()["error"] == "DISCOVERY_DISABLED"


# ---------------------------------------------------------------------------
# Real end-to-end: agent-token connect against a REAL terminal-node-agent
# subprocess, then a real heartbeat push, then the scheduler sees it.
# ---------------------------------------------------------------------------

REMOTE_TOKEN = "connect-agent-token-real-e2e-test-token"


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
def real_remote_node_agent(tmp_path):
    port = _free_port()
    workspace = tmp_path / "remote-workspace"
    workspace.mkdir()
    config_path = tmp_path / "remote-config.yaml"
    config_path.write_text(yaml.safe_dump({
        "permissions": {"terminal_read": True, "terminal_input": True},
        "allowed_session_patterns": ["test-*"],
        "input_policy": {"allowed_session_patterns": ["test-*"]},
    }))
    env = os.environ.copy()
    env["TERMINAL_MCP_CONFIG"] = str(config_path)
    env["TERMINAL_MCP_BINDINGS_DB"] = str(tmp_path / "remote-bindings.db")
    env["TERMINAL_MCP_AUDIT_DB"] = str(tmp_path / "remote-audit.db")
    env["TERMINAL_MCP_NODE_TOKEN"] = REMOTE_TOKEN
    process = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp.node_agent",
         "--node-id", "real-connect-e2e", "--controller-url", "http://127.0.0.1:1",
         "--host", "127.0.0.1", "--port", str(port), "--heartbeat-interval-seconds", "3600"],
        cwd=str(Path(__file__).parents[1]), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def test_agent_token_connect_real_e2e_then_scheduler_sees_it_once_healthy(tmp_path, real_remote_node_agent):
    port = real_remote_node_agent
    # allow_public=True here purely because the disposable test agent
    # binds to 127.0.0.1 (loopback, excluded by is_lan_scannable
    # regardless of "public" status) for portability across dev/CI hosts
    # -- a real LAN target (what this override is actually FOR) is
    # already covered by test_connect_agent_token_allows_public_when_
    # config_opts_in and the SSRF-rejection tests above.
    client, controller, store = _client(tmp_path, allow_public=True)

    endpoint = f"http://127.0.0.1:{port}"
    r = client.post("/dashboard/api/nodes/connect/agent-token", json={
        "node_id": "real-connect-e2e", "endpoint": endpoint, "token": REMOTE_TOKEN,
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert controller.node_status("real-connect-e2e") is not None
    saved = store.get("real-connect-e2e")
    assert saved is not None
    assert saved.transport_type == "agent_token"
    assert store.read_token(saved.token_file) == REMOTE_TOKEN

    # Duplicate connect of the SAME node_id is refused, never silently
    # re-paired/duplicated.
    r2 = client.post("/dashboard/api/nodes/connect/agent-token", json={
        "node_id": "real-connect-e2e", "endpoint": endpoint, "token": REMOTE_TOKEN,
    })
    assert r2.status_code == 409
    assert r2.json()["error"] == "NODE_ALREADY_EXISTS"

    # No heartbeat has arrived yet -- the scheduler must NOT place work
    # there (status is still offline, not "online just because it's
    # registered").
    from terminal_mcp.scheduler import choose_node
    placement = choose_node(controller.list_nodes(), required_agent_type="shell")
    assert placement.node_id != "real-connect-e2e"

    # A real heartbeat push (the SAME route the real node-agent's own
    # background loop would call) makes it eligible.
    hb = client.post("/dashboard/api/nodes/real-connect-e2e/heartbeat",
                     headers={"Authorization": f"Bearer {REMOTE_TOKEN}"},
                     json={"metrics": {"cpu_percent": 5.0, "ram_percent": 10.0, "load1": None, "load5": None,
                                       "load15": None, "cpu_count": 4, "ram_total_bytes": 16_000_000_000,
                                       "ram_used_bytes": 1_000_000_000, "swap_total_bytes": 0, "swap_used_bytes": 0,
                                       "disk_total_bytes": 100_000_000_000, "disk_used_bytes": 10_000_000_000,
                                       "disk_free_bytes": 90_000_000_000},  # comfortably above scheduler's 1 GiB floor
                           "tmux_session_count": 0, "agent_counts": {}, "agent_types": ["shell"],
                           "agent_version": "0.1", "labels": []})
    assert hb.status_code == 200, hb.text

    placement = choose_node(controller.list_nodes(), required_agent_type="shell")
    assert placement.node_id == "real-connect-e2e"
