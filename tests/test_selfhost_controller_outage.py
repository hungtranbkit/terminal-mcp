"""A node must not die when its controller does.

The fleet is hub-and-spoke: every node agent points at ONE controller (M910).
The question this file answers with a real process, not by reading code: when
that controller is unreachable -- powered off, off the network, Tailscale down,
or simply crashed -- does the node keep serving its OWN sessions?

The controller here is a port with nothing listening on it, which is what a
dead controller looks like from a node's side. Everything is disposable: a
throwaway state directory, a throwaway config with no whitelist at all, a
session created and killed inside the test. No real node is touched and no
live session is restarted.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import json
import pathlib

import pytest
import yaml

TOKEN = "selfhost-outage-token"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _dead_port() -> int:
    """A port with nothing on it -- bind it, read the number, release it."""
    port = _free_port()
    with socket.socket() as probe:
        # Confirm it really is refusing connections before the test relies on it.
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    return port


def _wait_for_port(host: str, port: int, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"node agent did not listen on {host}:{port}")


def _call(port: int, path: str, method: str = "GET", body: dict | None = None, timeout: float = 25):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode())


@pytest.fixture(scope="module")
def orphaned_node(tmp_path_factory):
    """A real node agent whose controller never answers."""
    if not (ROOT / ".venv/bin/python").exists() and sys.executable is None:
        pytest.skip("no interpreter to spawn the agent with")
    base = tmp_path_factory.mktemp("selfhost")
    workspace = base / "ws"
    workspace.mkdir()
    config_path = base / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "permissions": {"terminal_read": True, "terminal_input": True},
        # Deliberately EMPTY: this node has no session-name whitelist at all.
        # Access comes from the default policy below, which is what a
        # post-whitelist deployment looks like.
        "allowed_session_patterns": [],
        "input_policy": {"allowed_session_patterns": [],
                         "allow_keys": ["Enter", "Escape", "Up", "Down", "Left", "Right", "Tab"]},
        "session_access": {"default_read": True, "default_input": True},
        "session_lifecycle": {"enabled": True, "allowed_cwd_roots": [str(workspace)]},
    }))
    port = _free_port()
    dead_controller = _dead_port()
    env = os.environ.copy()
    env["TERMINAL_MCP_CONFIG"] = str(config_path)
    env["XDG_STATE_HOME"] = str(base / "state")
    env["TERMINAL_MCP_NODE_TOKEN"] = TOKEN
    process = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp.node_agent",
         "--node-id", "selfhost-outage", "--controller-url", f"http://127.0.0.1:{dead_controller}",
         "--host", "127.0.0.1", "--port", str(port), "--heartbeat-interval-seconds", "1"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield port, workspace
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_agent_starts_and_stays_up_with_no_controller(orphaned_node):
    """Startup must not require the controller at all, and a heartbeat that
    can never be delivered must not take the process down with it."""
    port, _ = orphaned_node
    assert _call(port, "/v1/health")["status"] == "ok"
    time.sleep(3)  # several heartbeat intervals, every one of them failing
    assert _call(port, "/v1/health")["status"] == "ok"


def test_local_session_lifecycle_works_while_the_controller_is_dead(orphaned_node):
    """The whole local data plane: create, list, read, type, press a key.

    This is the property that decides whether losing M910 costs the fleet its
    work or only its single pane of glass.
    """
    port, workspace = orphaned_node
    name = f"outage-{int(time.time())}"
    created = _call(port, "/v1/sessions", "POST",
                    {"name": name, "agent_type": "shell", "cwd": str(workspace)})
    assert created.get("state") == "READY", created
    try:
        listed = {row["name"] for row in _call(port, "/v1/sessions")["sessions"]}
        assert name in listed

        sent = _call(port, f"/v1/sessions/{name}/send", "POST",
                     {"text": "echo OUTAGE_OK", "press_enter": True})
        assert "error" not in sent, sent
        time.sleep(1.5)
        assert "OUTAGE_OK" in _call(port, f"/v1/sessions/{name}/tail?lines=20").get("output", "")

        keyed = _call(port, f"/v1/sessions/{name}/send-keys", "POST", {"keys": ["Up"]})
        assert "error" not in keyed, keyed

        status = _call(port, f"/v1/sessions/{name}/status")
        assert status["exists"] is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_local_recovery_works_while_the_controller_is_dead(orphaned_node):
    """Recovery is not a controller-only capability: the node owns its own
    registry and can rebuild its own session from it."""
    port, workspace = orphaned_node
    name = f"outage-rec-{int(time.time())}"
    assert _call(port, "/v1/sessions", "POST",
                 {"name": name, "agent_type": "shell", "cwd": str(workspace)}).get("state") == "READY"
    try:
        records = {r["session_name"]: r for r in _call(port, "/v1/registry")["records"]}
        assert records[name]["status"] == "ACTIVE"

        subprocess.run(["tmux", "kill-session", "-t", name], check=True, capture_output=True)
        _call(port, "/v1/sessions")  # a listing is what reconciles the registry
        records = {r["session_name"]: r for r in _call(port, "/v1/registry")["records"]}
        assert records[name]["status"] == "MISSING"

        reopened = _call(port, f"/v1/sessions/{name}/registry-reopen", "POST", {})
        assert "error" not in reopened, reopened
        assert _call(port, f"/v1/sessions/{name}/status")["exists"] is True
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_recovery_is_idempotent_and_never_duplicates(orphaned_node):
    """A node rejoining, or a retry storm, must not end with two sessions
    answering to one name."""
    port, workspace = orphaned_node
    name = f"outage-idem-{int(time.time())}"
    assert _call(port, "/v1/sessions", "POST",
                 {"name": name, "agent_type": "shell", "cwd": str(workspace)}).get("state") == "READY"
    try:
        again = _call(port, f"/v1/sessions/{name}/registry-reopen", "POST", {})
        assert again.get("error") == "SESSION_ALREADY_EXISTS", again
        listing = subprocess.run(["tmux", "ls", "-F", "#{session_name}"],
                                 capture_output=True, text=True, check=False).stdout.splitlines()
        assert listing.count(name) == 1
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], check=False, capture_output=True)


def test_health_reports_the_protocol_contract(orphaned_node):
    """A peer must be able to ask what protocol this node speaks before
    routing to it. `agent_generation` cannot answer that -- it is a random
    token per process, so two builds look as different as two restarts.

    This also guards the wiring itself: the first version of it referenced
    contract_describe without importing it, and /v1/health answered 500.
    """
    from terminal_mcp import contract
    port, _ = orphaned_node
    health = _call(port, "/v1/health")
    assert health["contract_version"] == contract.CONTRACT_VERSION
    assert set(health["contract_capabilities"]) == set(contract.CAPABILITIES)
    # Must NOT collide with the probed tool-capability field.
    assert "capabilities" not in health


def test_a_node_needs_no_whitelist_to_serve_itself(orphaned_node):
    """This agent runs with allowed_session_patterns completely empty. Before
    the whitelist was retired that config was rejected outright and the agent
    would not start -- found by exactly this harness."""
    port, _ = orphaned_node
    assert _call(port, "/v1/health")["node_id"] == "selfhost-outage"
