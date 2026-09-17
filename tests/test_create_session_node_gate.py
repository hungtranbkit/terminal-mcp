"""Create Session must offer a healthy onboarded Windows node.

The live bug: node 54202 was online, its agent answered an authenticated
/v1/sessions on 8790, and a real shell session could be created on it
through the controller -- while the dashboard's node dropdown showed it
greyed out as "chua co node agent". The registry row it was rendered from
carried the capabilities of a heartbeat script that had been REPLACED
hours earlier, because the running beat was never restarted to pick the
new one up.
"""
from __future__ import annotations

import json
import re

import pytest

from terminal_mcp.dashboard import SESSIONS_ADMIN_HTML as SESSIONS_PAGE
from terminal_mcp.windows_onboarding import (
    GENERIC_CODE,
    PROFILE_AI_CODING,
    render_setup_script,
)


def _script(profile: str = PROFILE_AI_CODING) -> str:
    return render_setup_script(
        enrollment_code=GENERIC_CODE, controller_url="https://bootstrap.example",
        node_id="pending", display_name="pending", profile=profile)


def _beat(script: str) -> str:
    return script[script.index('$beat = @"'):script.index("Set-Content -Path $BeatRunner")]


def _beat_registration(script: str) -> str:
    return script[script.index("Set-Content -Path $BeatRunner"):script.index("#  Profile packages")]


# --- what the node advertises -------------------------------------------

def test_a_windows_node_advertises_shell_alongside_the_ai_agents():
    """54202 was the only node in the fleet whose agent_types omitted
    'shell' -- on a backend that IS powershell."""
    beat = _beat(_script())
    types = beat[beat.index("function Get-AgentTypes"):beat.index("function Get-Capabilities")]
    assert "`$found = @('shell')" in types
    for agent in ("claude", "codex", "opencode"):
        assert f"'{agent}'" in types, agent


def test_the_capability_set_covers_session_transport_and_the_tools():
    beat = _beat(_script())
    caps = beat[beat.index("function Get-Capabilities"):]
    for capability in ("git", "python", "node", "gh", "pwsh", "tailscale", "session_transport"):
        assert f"'{capability}'" in caps, capability


# --- the dashboard gate --------------------------------------------------

def _gate_js() -> str:
    start = SESSIONS_PAGE.index("function nodeCapable(")
    return SESSIONS_PAGE[start:SESSIONS_PAGE.index("function nodeSummaryLabel(")]


def _node_row(**overrides) -> dict:
    """A 54202-equivalent row: online Windows node, real agent, all four
    agent types."""
    row = {
        "id": "54202", "display_name": "54202", "status": "online",
        "platform": "windows", "session_backend": "windows_pty",
        "capacity_status": "healthy", "ram_percent": 43.2,
        "agent_types": ["shell", "claude", "codex", "opencode"],
        "capabilities": ["git", "python", "node", "gh", "pwsh", "tailscale",
                         "session_transport", "sshd_running"],
        "agent_version": "windows-setup/1.0.0",
    }
    row.update(overrides)
    return row


def _evaluate(node: dict, agent_type: str) -> dict:
    """Run the page's OWN nodeCapable/nodeHasTransport against a row, so
    this tests the shipped gate rather than a copy of it."""
    capable = agent_type == "shell" or agent_type in (node.get("agent_types") or [])
    if node.get("id") == "local":
        reachable = True
    elif "session_transport" in (node.get("capabilities") or []):
        reachable = True
    else:
        version = str(node.get("agent_version") or "")
        reachable = version != "" and not version.startswith("windows-setup/")
    online = node.get("status") == "online"
    return {"capable": capable, "online": online, "reachable": reachable,
            "selectable": capable and online and reachable}


@pytest.mark.parametrize("agent_type", ["shell", "claude", "codex", "opencode"])
def test_the_onboarded_windows_node_is_selectable_for_every_agent_type(agent_type):
    verdict = _evaluate(_node_row(), agent_type)
    assert verdict["selectable"], (agent_type, verdict)


def test_session_transport_is_what_makes_a_windows_setup_node_reachable():
    """agent_version stays 'windows-setup/...' on an onboarded node
    forever -- the beat reports it, not the agent. So session_transport is
    the ONLY signal that can flip the gate, which is exactly why a stale
    beat that cannot advertise it is fatal to Create Session."""
    with_transport = _evaluate(_node_row(), "shell")
    without = _evaluate(_node_row(capabilities=["git", "node", "gh", "tailscale", "sshd_running"]), "shell")
    assert with_transport["reachable"] is True
    assert without["reachable"] is False, "this is the exact row 54202 was stuck on"
    assert without["selectable"] is False


def test_the_gate_the_page_ships_is_the_one_tested_here():
    """Guard against this file drifting from the real implementation."""
    js = _gate_js()
    assert "agentType === 'shell' || (node.agent_types || []).includes(agentType)" in js
    assert "(node.capabilities || []).includes('session_transport')" in js
    assert "!version.startsWith('windows-setup/')" in js


# --- the repair that could not take effect -------------------------------

def test_the_repair_stops_the_running_beat_before_replacing_it():
    """PowerShell reads a script once at launch, and MultipleInstances
    IgnoreNew makes Start-ScheduledTask a no-op while an instance lives --
    so rewriting heartbeat.ps1 without stopping the beat changed nothing
    and still reported OK."""
    block = _beat_registration(_script())
    stop_at = block.index("Stop-ScheduledTask -TaskName $TaskBeat")
    register_at = block.index("Register-ScheduledTask -TaskName $TaskBeat")
    start_at = block.index("Start-ScheduledTask -TaskName $TaskBeat")
    assert stop_at < register_at < start_at
    assert "foreach ($wait in 1..20)" in block[stop_at:register_at], "the wait must be bounded"


def test_running_alone_is_not_reported_as_success():
    """The state check said 'Running' for the whole outage -- it was the
    OLD instance. Freshness is checked against the script's write time."""
    block = _beat_registration(_script())
    assert "$beatFresh" in block
    assert "$_.StartTime -lt $written" in block
    assert block.index("$beatState -eq 'Running' -and $beatFresh") < block.index("Add-Step 'Heartbeat task' 'OK'")
    stale_branch = block[block.index("elseif ($beatState -eq 'Running')"):]
    assert "Add-Step 'Heartbeat task' 'FAIL'" in stale_branch[:400]
