"""windows_agent.py: capability detection (real, on this actual Linux
dev host -- honest evidence of what shutil.which finds here, not a
mock), and a REAL subprocess smoke test of the HTTP/heartbeat surface
(everything reachable without a real Windows OS). The one thing this
CANNOT verify is a real pywinpty/ConPTY session spawn succeeding -- that
call is expected, and confirmed here, to fail cleanly (not crash the
process) on this non-Windows host; see windows_backend.py's own module
docstring."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from terminal_mcp.windows_agent import detect_shell_capabilities, detect_wsl_available

REPO_ROOT = Path(__file__).parents[1]


def test_detect_shell_capabilities_is_empty_on_this_linux_host():
    # Real, honest evidence: no powershell.exe/pwsh.exe/cmd.exe exists
    # anywhere on THIS actual host's PATH -- not asserting a mock.
    assert detect_shell_capabilities() == ()


def test_detect_wsl_available_is_false_on_this_linux_host():
    assert detect_wsl_available() is False


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
    raise AssertionError(f"terminal-windows-node-agent did not listen on {host}:{port}")


@pytest.fixture
def windows_agent_process(tmp_path):
    port = _free_port()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "permissions": {"terminal_read": True, "terminal_input": True},
        "allowed_session_patterns": ["winagent-*"],
        "session_lifecycle": {"enabled": True, "allowed_cwd_roots": [str(tmp_path)]},
    }))
    env = os.environ.copy()
    env["TERMINAL_MCP_CONFIG"] = str(config_path)
    env["TERMINAL_MCP_BINDINGS_DB"] = str(tmp_path / "bindings.db")
    env["TERMINAL_MCP_AUDIT_DB"] = str(tmp_path / "audit.db")
    env["TERMINAL_MCP_NODE_TOKEN"] = "windows-agent-test-token"
    process = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp.windows_agent",
         "--node-id", "test-windows-node", "--controller-url", "http://127.0.0.1:1",
         "--host", "127.0.0.1", "--port", str(port), "--heartbeat-interval-seconds", "3600"],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        _wait_for_port("127.0.0.1", port)
        yield port, process
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


def _get(port: int, path: str, token: str = "windows-agent-test-token") -> tuple[int, dict]:
    import urllib.request
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read())
        except ValueError:
            return exc.code, {}


def test_windows_agent_real_subprocess_health_and_sessions_surface(windows_agent_process):
    port, process = windows_agent_process

    status, body = _get(port, "/v1/health", token="")  # health needs no auth
    assert status == 200
    assert body["node_id"] == "test-windows-node"

    status, body = _get(port, "/v1/sessions")
    assert status == 200
    assert body["sessions"] == []  # nothing created yet -- listing itself works fine

    # A real session CREATE, on this non-Windows host, must fail cleanly
    # (the lazy `import winpty` inside _default_process_factory raises,
    # caught by WindowsSessionBackend.new_session and turned into a
    # normal TmuxError -> a normal 200-with-error-body response, exactly
    # like any other backend failure -- never an unhandled exception
    # that would crash this process or return a raw 500).
    import urllib.request
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/sessions", method="POST",
        data=json.dumps({"name": "winagent-smoke", "agent_type": "shell"}).encode(),
        headers={"Authorization": "Bearer windows-agent-test-token", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
        body = json.loads(response.read())
    assert "error" in body  # a clean application-level error, not a crash

    # The process itself must still be alive and serving after that
    # failure -- confirms the exception was actually caught, not just
    # luckily non-fatal by timing.
    status, body = _get(port, "/v1/health", token="")
    assert status == 200
    assert process.poll() is None


def test_windows_agent_heartbeat_loop_survives_unreachable_controller(windows_agent_process):
    # The subprocess is already pointed at an unreachable controller-url
    # (http://127.0.0.1:1) with a long heartbeat interval so it only
    # tries once during this test's lifetime -- if that failed push ever
    # crashed the process, health would stop responding.
    port, process = windows_agent_process
    time.sleep(1.0)  # let at least one heartbeat attempt (and its failure) happen
    status, body = _get(port, "/v1/health", token="")
    assert status == 200
    assert process.poll() is None
