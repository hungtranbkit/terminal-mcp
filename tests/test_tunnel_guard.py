"""deploy/tunnel/terminal-mcp-tunnel-guard: one tunnel id, one controller.

Regression for the 2026-10-06 split brain: the guard hard-coded m910 as the
primary after the controller moved to HP, so the Dell fallback always joined
the tunnel and ChatGPT calls alternated between two registries.
"""
from __future__ import annotations

import os
import socket
import stat
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "deploy" / "tunnel" / "terminal-mcp-tunnel-guard"


@pytest.fixture
def fake_systemctl(tmp_path):
    log = tmp_path / "systemctl.log"
    script = tmp_path / "systemctl"
    script.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> {log}\n"
        "[ \"$2\" = is-active ] && exit 0\n"
        "exit 0\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script, log


def _run(tmp_path, mode, *, env_lines, systemctl):
    env_file = tmp_path / "tunnel-guard.env"
    env_file.write_text("\n".join(env_lines) + "\n")
    env = {**os.environ, "TERMINAL_MCP_GUARD_ENV": str(env_file),
           "TERMINAL_MCP_GUARD_SYSTEMCTL": str(systemctl),
           "TERMINAL_MCP_GUARD_ATTEMPTS": "1", "TERMINAL_MCP_GUARD_GAP": "0",
           "TERMINAL_MCP_GUARD_TIMEOUT": "2"}
    for key in ("TERMINAL_MCP_PRIMARY_HOST", "TERMINAL_MCP_PRIMARY_PORT", "TERMINAL_MCP_GUARD_UNITS"):
        env.pop(key, None)
    return subprocess.run(["bash", str(GUARD), mode], env=env, capture_output=True, text=True, timeout=30)


@pytest.fixture
def listening_port():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    yield server.getsockname()[1]
    server.close()


def _closed_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def test_missing_primary_fails_closed(tmp_path, fake_systemctl):
    systemctl, log = fake_systemctl
    result = _run(tmp_path, "condition", env_lines=[], systemctl=systemctl)
    assert result.returncode == 1
    assert "TERMINAL_MCP_PRIMARY_HOST is not set" in result.stderr
    assert not log.exists()


def test_primary_serving_blocks_fallback_and_enforce_stops_duplicate(tmp_path, fake_systemctl, listening_port):
    systemctl, log = fake_systemctl
    env = ["TERMINAL_MCP_PRIMARY_HOST=127.0.0.1", f"TERMINAL_MCP_PRIMARY_PORT={listening_port}",
           'TERMINAL_MCP_GUARD_UNITS="terminal-mcp-tunnel.service"']
    assert _run(tmp_path, "condition", env_lines=env, systemctl=systemctl).returncode == 1
    enforced = _run(tmp_path, "enforce", env_lines=env, systemctl=systemctl)
    assert enforced.returncode == 0
    calls = log.read_text().splitlines()
    assert "--user stop terminal-mcp-tunnel.service" in calls
    # Only listed units are ever touched (a dashboard tunnel only this host
    # serves must not be taken down).
    assert not any("cloudflared" in call for call in calls)


def test_primary_down_allows_fallback_and_enforce_leaves_it_running(tmp_path, fake_systemctl):
    systemctl, log = fake_systemctl
    env = ["TERMINAL_MCP_PRIMARY_HOST=127.0.0.1", f"TERMINAL_MCP_PRIMARY_PORT={_closed_port()}"]
    assert _run(tmp_path, "condition", env_lines=env, systemctl=systemctl).returncode == 0
    assert _run(tmp_path, "enforce", env_lines=env, systemctl=systemctl).returncode == 0
    assert not any(" stop " in f" {call} " for call in log.read_text().splitlines())
