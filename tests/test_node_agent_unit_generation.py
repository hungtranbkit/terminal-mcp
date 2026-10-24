"""Validate the generated node-agent service against systemd's parser."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _render_unit(script: str, repo_dir: Path, output: Path) -> None:
    funcs = script.split("systemd_quote() {", 1)[1].split("UNIT_CONTENT=$(cat <<EOF", 1)[0]
    template = script.split("UNIT_CONTENT=$(cat <<EOF", 1)[1].split("\nEOF\n)", 1)[0]
    values = {
        "REPO_DIR": str(repo_dir),
        "NODE_ID": "nec1",
        "CONTROLLER_URL": "https://terminal-bootstrap.mesflow.net",
        "TOKEN_FILE": str(repo_dir / "node-agent.token"),
        "AGENT_HOST": "100.90.90.90",
        "AGENT_PORT": "8790",
        "HEARTBEAT_INTERVAL": "20",
        "HOME": str(repo_dir.parent),
    }
    # Source *actual* helper functions and unit template from installer.
    source = (
        "set -euo pipefail\n"
        + "systemd_quote() {" + funcs
        + "UNIT_CONTENT=$(cat <<EOF" + template + "\nEOF\n)\n"
        + 'printf "%s\\n" "$UNIT_CONTENT" > "$OUTFILE"\n'
    )
    result = subprocess.run(["bash", "-c", source], text=True, capture_output=True,
                            env={**os.environ, **values, "OUTFILE": str(output)}, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("dirname", ["terminal-mcp", "my workspace", "repo%20"])
def test_generated_unit_working_directory_is_valid(tmp_path: Path, dirname: str):
    script = (Path(__file__).resolve().parents[1] / "deploy" /
              "install-node-agent.sh").read_text()
    repo = tmp_path / dirname
    (repo / ".venv" / "bin").mkdir(parents=True)
    exe = repo / ".venv" / "bin" / "terminal-node-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    unit = tmp_path / "terminal-node-agent.service"
    _render_unit(script, repo, unit)
    text = unit.read_text()
    assert f"WorkingDirectory={str(repo).replace('%', '%%')}" in text
    assert 'WorkingDirectory="' not in text
    assert "KillMode=process" in text
    # Codex execution needs mount-family syscalls inside its own Linux
    # sandbox. A stricter unit caused SIGSYS exit 159 even for read-only pwd.
    assert "SystemCallFilter=@system-service @mount" in text
    assert "NoNewPrivileges=yes" in text
    if shutil.which("systemd-analyze"):
        check = subprocess.run(["systemd-analyze", "verify", str(unit)],
                               capture_output=True, text=True, timeout=10)
        assert check.returncode == 0, check.stderr


def test_installer_validates_load_state_before_restart():
    script = (Path(__file__).resolve().parents[1] / "deploy" /
              "install-node-agent.sh").read_text()
    assert script.index("UNIT_LOAD_STATE=") < script.index("systemctl --user enable terminal-node-agent.service")
    assert "UNIT_LOAD_STATE\" != \"loaded\"" in script
