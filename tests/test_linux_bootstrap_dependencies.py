"""Regression tests for Linux dependency bootstrap, without host package changes."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from terminal_mcp.linux_onboarding import render_linux_pair_script


def _run_preflight(tmp_path: Path, *, tmux_installed: bool, apt_available: bool = True):
    installed = tmp_path / "tmux-installed"
    log = tmp_path / "apt-log"
    if tmux_installed:
        installed.touch()
    mocks = r'''
command() {
  if [[ "$1" == "-v" && "$2" == "tmux" ]]; then
    [[ -f "$TMCP_TEST_TMUX" ]]
  elif [[ "$1" == "-v" && "$2" == "apt-get" && "$TMCP_TEST_NO_APT" == "1" ]]; then
    return 1
  else
    builtin command "$@"
  fi
}
python3() {
  if [[ "$1" == "-m" && "$2" == "venv" ]]; then mkdir -p "$3"; return 0; fi
  /usr/bin/python3 "$@"
}
apt-get() {
  printf '%s\n' "$*" >> "$TMCP_TEST_APT_LOG"
  if [[ "$1" == "install" ]]; then touch "$TMCP_TEST_TMUX"; fi
}
env() {
  [[ "$1" == "DEBIAN_FRONTEND=noninteractive" ]] && shift
  [[ "$1" == "apt-get" ]] && shift
  apt-get "$@"
}
sudo() {
  if [[ "$1" == "-n" && "$2" == "-v" ]]; then return 0; fi
  [[ "$1" == "env" ]] || return 4
  shift
  env "$@"
}
'''
    full = render_linux_pair_script()
    assert "if ! command -v tailscale" in full
    prefix = full.split("if ! command -v tailscale", 1)[0]
    preflight = prefix.replace('CONTROLLER_URL="${1:-}"', mocks + '\nCONTROLLER_URL="${1:-}"', 1)
    preflight += '\necho TMCP_PREFLIGHT_OK\n'
    envvars = os.environ.copy()
    envvars.update({
        "TMCP_TEST_TMUX": str(installed),
        "TMCP_TEST_APT_LOG": str(log),
        "TMCP_TEST_NO_APT": "0" if apt_available else "1",
    })
    result = subprocess.run(
        ["bash", "-c", preflight, "--", "https://terminal-bootstrap.mesflow.net",
         "nec1", "TMCP-ABCDE-ABCDE-ABCDE"],
        text=True, capture_output=True, timeout=20, env=envvars,
    )
    return result, log.read_text() if log.exists() else ""


def test_missing_tmux_installs_before_pairing(tmp_path):
    result, apt_log = _run_preflight(tmp_path, tmux_installed=False)
    assert result.returncode == 0, result.stderr
    assert "TMCP_PREFLIGHT_OK" in result.stdout
    assert "update -qq" in apt_log
    assert "install -y --no-install-recommends tmux" in apt_log


def test_existing_tmux_does_not_run_apt(tmp_path):
    result, apt_log = _run_preflight(tmp_path, tmux_installed=True)
    assert result.returncode == 0, result.stderr
    assert apt_log == ""


def test_missing_package_manager_fails_without_pairing(tmp_path):
    result, apt_log = _run_preflight(tmp_path, tmux_installed=False, apt_available=False)
    assert result.returncode == 2
    assert "install them manually" in result.stderr
    assert "TMCP_PREFLIGHT_OK" not in result.stdout
    assert apt_log == ""


def test_entire_setup_script_has_valid_bash_syntax():
    result = subprocess.run(["bash", "-n"], input=render_linux_pair_script(),
                            text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
