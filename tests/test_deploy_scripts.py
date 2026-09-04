"""Static hygiene checks for the deploy/ PowerShell scripts.

These scripts only run on Windows, so this repo's Linux-based test suite
can't execute them -- but it can still catch the two mistakes that matter
most for unattended, security-sensitive infra scripts: a hardcoded secret,
and an accidental drift away from the documented safety invariants (never
touch battery/DC power policy, never touch the SSH firewall rule, never run
the node agent as SYSTEM). Real syntax/behavior verification happens live
on the target Windows node (see docs/multi-node.md).
"""
from __future__ import annotations

import re
from pathlib import Path

DEPLOY_DIR = Path(__file__).resolve().parent.parent / "deploy"
STABILITY_SCRIPT = DEPLOY_DIR / "configure-windows-node-stability.ps1"

# Patterns that would indicate a secret got hardcoded into the script.
_SECRET_PATTERNS = [
    re.compile(r"(?i)token\s*=\s*[\"'][A-Za-z0-9_\-]{16,}[\"']"),
    re.compile(r"(?i)password\s*=\s*[\"'][^\"']+[\"']"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _read() -> str:
    assert STABILITY_SCRIPT.exists(), f"missing {STABILITY_SCRIPT}"
    return STABILITY_SCRIPT.read_text(encoding="utf-8")


def test_stability_script_exists():
    assert STABILITY_SCRIPT.exists()


def test_stability_script_never_hardcodes_a_secret():
    text = _read()
    for pattern in _SECRET_PATTERNS:
        assert not pattern.search(text), f"possible hardcoded secret matching {pattern.pattern!r}"
    # The script must not reference the node bearer-token env var pattern at
    # all -- it has no business touching auth material.
    assert "TERMINAL_MCP_NODE_TOKEN" not in text


def test_stability_script_only_touches_ac_power_never_dc_battery():
    text = _read()
    assert "setacvalueindex" in text
    assert "setdcvalueindex" not in text


def test_stability_script_never_modifies_ssh_firewall_or_service():
    text = _read()
    # It may READ/report sshd + firewall state (Get-Service, Get-NetFirewallRule)
    # and may mention mutating cmdlets in advisory *comment/string* text (e.g.
    # "review manually: 'Set-Service sshd ...'") but must never actually call
    # one as a statement -- i.e. at the start of a line (ignoring indentation).
    forbidden = re.compile(
        r"(?m)^\s*(Set-NetFirewallRule|New-NetFirewallRule|Remove-NetFirewallRule|"
        r"Set-Service\s+sshd|Restart-Service\s+sshd|Stop-Service\s+sshd)\b"
    )
    match = forbidden.search(text)
    assert not match, f"script must never call {match.group(0)!r} as a statement -- see header safety notes"


def test_stability_script_keeps_the_node_agent_task_running_as_the_original_user_not_system():
    text = _read()
    # "SYSTEM" legitimately appears in comments explaining *why* this script
    # avoids it -- what must never appear is an actual identity assignment
    # naming SYSTEM (e.g. -UserId "SYSTEM" / "NT AUTHORITY\SYSTEM").
    identity_as_system = re.compile(r"(?i)-UserId\s+[\"']?(NT AUTHORITY\\)?SYSTEM[\"']?")
    assert not identity_as_system.search(text)
    assert "S4U" in text, "expected the LogonType=S4U login-independent, non-SYSTEM fix"


def test_stability_script_is_idempotent_by_construction():
    text = _read()
    # Every mutating section should be gated by a "already correct" check --
    # spot-check that the Ok/Change logging helpers used throughout exist,
    # which is how this script signals idempotency (see module docstring).
    assert "Log-Ok" in text
    assert "Log-Change" in text
    assert "Log-Skip" in text


def test_stability_script_never_disables_windows_update():
    text = _read()
    assert "Stop-Service wuauserv" not in text
    assert "Disable-ScheduledTask" not in text or "WindowsUpdate" not in text
    assert "wuauserv" not in text  # only touches Active Hours, not the service


def test_stability_script_adds_startup_trigger_not_only_logon():
    text = _read()
    assert "AtStartup" in text
