"""Installing the node agent from onboarding, and not offering a node that
cannot host a session.

The gap: a Windows node onboarded by windows-setup.ps1 heartbeats from a
scheduled task. It is online, healthy, and has NO node agent -- 8790 is
closed, node_transports is empty, and every create-session against it would
fail at the transport. Create Session offered it anyway, because the gate
only asked about agent_types.
"""
from __future__ import annotations

import pytest

from terminal_mcp.dashboard import SESSIONS_ADMIN_HTML as SESSIONS_PAGE
from terminal_mcp.windows_onboarding import (
    GENERIC_CODE,
    PROFILE_AI_CODING,
    PROFILE_DEVELOPER,
    PROFILE_MINIMAL,
    render_setup_script,
)


def _script(profile: str = PROFILE_MINIMAL) -> str:
    return render_setup_script(
        enrollment_code=GENERIC_CODE, controller_url="https://bootstrap.example",
        node_id="pending", display_name="pending", profile=profile)


def _without_comments(text: str) -> str:
    """PowerShell source with # comments stripped, so a structural check
    tests the code and not the prose explaining it."""
    out = []
    for line in text.splitlines():
        stripped = line.split("#", 1)[0] if not line.lstrip().startswith("#") else ""
        out.append(stripped)
    return "\n".join(out)


def _agent_section(script: str) -> str:
    start = script.index("Start-Stage 'Node agent (transport de tao session)'")
    return script[start:script.index("#  Verification + summary")]


# ---------------------------------------------------------------------------
# fetching the bundle: authenticated, verified, never trusted
# ---------------------------------------------------------------------------

def test_the_bundle_is_fetched_with_this_nodes_own_bearer_token():
    section = _agent_section(_script())
    assert 'Authorization = "Bearer $tok"' in section
    assert '/dashboard/api/nodes/$NodeId/agent-bundle' in section
    # The token comes from the file the installer already wrote, not a
    # second credential invented here.
    assert "Get-Content $TokenFile -Raw" in section


def test_metadata_is_fetched_first_so_a_current_bundle_is_not_redownloaded():
    section = _agent_section(_script())
    assert "-Method Head" in section
    assert "X-Terminal-Mcp-Agent-Sha256" in section
    assert "$upToDate" in section
    assert "bo qua tai lai" in section


def test_the_hash_is_verified_before_anything_is_extracted():
    """A bundle that does not match what the controller published is never
    unpacked, let alone installed."""
    section = _agent_section(_script())
    verify = section[section.index("Get-FileHash"):section.index("$staging =")]
    assert "-Algorithm SHA256" in verify
    assert "$gotSha -ne $wantSha" in verify
    assert "throw" in verify
    assert "Remove-Item $tmpZip" in verify
    # And the extraction genuinely happens after the check.
    assert section.index("Get-FileHash") < section.index("ZipFile]::OpenRead")


def test_extraction_refuses_a_member_that_escapes_the_directory():
    section = _agent_section(_script())
    assert "GetFullPath" in section
    assert "$dest.StartsWith($root" in section
    assert "outside the extraction directory" in section


def test_a_missing_hash_header_is_refused_rather_than_assumed():
    section = _agent_section(_script())
    assert 'if (-not $wantSha) { throw' in section


# ---------------------------------------------------------------------------
# installing: the existing script, the existing service, idempotently
# ---------------------------------------------------------------------------

def test_it_runs_the_install_script_shipped_inside_the_bundle():
    """The same script this release was tested against, not whatever an
    older setup left on the machine."""
    section = _agent_section(_script())
    assert "deploy\\install-node-agent.ps1" in section
    assert "-RepoDir $targetDir" in section
    assert "-ControllerUrl $ControllerUrl -NodeId $NodeId" in section
    assert "-Port 8790" in section
    # The exit code is captured first so it can be logged alongside the
    # transcript, then checked -- rather than tested inline and discarded.
    assert "$installerExit = $LASTEXITCODE" in section
    assert "if ($installerExit -ne 0)" in section


def test_it_binds_the_interface_the_controller_reaches_not_everything():
    section = _agent_section(_script())
    assert "Get-LocalAddresses" in section
    assert "$addr.tailscale_ip" in section and "$addr.lan_ip" in section
    assert "-BindHost $bindHost" in section
    # Checked against CODE, not the comment that says "never 0.0.0.0".
    assert "0.0.0.0" not in _without_comments(section)


def test_port_8790_is_firewalled_to_the_overlay_not_the_internet():
    section = _agent_section(_script())
    assert "-LocalPort 8790" in section
    assert "100.64.0.0/10" in section
    assert "-Direction Inbound" in section
    # Replacing the rule keeps a re-run idempotent.
    assert "Remove-NetFirewallRule" in section


# ---------------------------------------------------------------------------
# readiness: listening is not the same as usable
# ---------------------------------------------------------------------------

def test_readiness_checks_both_liveness_and_the_controllers_own_credential():
    """A service that is up but rejects the controller is not ready."""
    section = _agent_section(_script())
    assert "/v1/health" in section
    assert "/v1/sessions" in section
    assert "$healthy" in section and "$authed" in section
    assert "refused this node's own token" in section
    # Both must pass before the transport is called real.
    assert section.index("if (-not $healthy)") < section.index("$script:AgentReady = $true")
    assert section.index("if (-not $authed)") < section.index("$script:AgentReady = $true")


def test_the_transport_is_only_recorded_after_both_checks_pass():
    section = _agent_section(_script())
    assert "$script:AgentReady = $false" in _script()
    marker = section.index("$script:AgentReady = $true")
    # The install record is written only on the success path.
    assert "$installedFile" in section[marker:marker + 400]
    assert "installed.json" in section, "the record has a stable name"


# ---------------------------------------------------------------------------
# failure: roll back rather than leave a half-installed agent
# ---------------------------------------------------------------------------

def test_a_failed_install_rolls_back_to_the_previous_bundle():
    section = _agent_section(_script())
    assert "'Node agent rollback' 'WARN'" in section
    assert "$previous.dir" in section
    assert "quay ve ban" in section


def test_with_nothing_to_roll_back_to_the_service_is_stopped_not_left_broken():
    """A node with no agent is honest; a node with a broken one is not."""
    section = _agent_section(_script())
    assert "Stop-Service" in section
    assert "TerminalMCPNodeAgent" in section


def test_a_failure_says_what_to_do_and_notes_ssh_still_works():
    section = _agent_section(_script())
    assert "'Node agent' 'FAIL'" in section
    assert "-Repair" in section
    assert "SSH" in section


# ---------------------------------------------------------------------------
# which profiles get an agent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", [PROFILE_MINIMAL, PROFILE_DEVELOPER])
def test_minimal_and_developer_stay_ssh_only(profile):
    """The agent needs Python and a service; those profiles promise
    neither."""
    section = _agent_section(_script(profile))
    assert "$ProfileName -ne 'ai_coding'" in section
    assert "'Node agent' 'SKIP'" in section


def test_a_failed_enrollment_skips_rather_than_cascading():
    section = _agent_section(_script())
    assert "-not $script:EnrollmentOk" in section
    assert "chua co node token" in section


# ---------------------------------------------------------------------------
# the heartbeat advertises transport only when it is real
# ---------------------------------------------------------------------------

def test_the_beat_advertises_session_transport_only_after_health_and_auth():
    script = _script()
    beat = script[script.index("$beat = @\""):script.index("Set-Content -Path $BeatRunner")]
    caps = beat[beat.index("function Get-Capabilities"):]
    assert "/v1/health" in caps and "/v1/sessions" in caps
    assert "'session_transport'" in caps
    assert "Bearer `$token" in caps
    # Inside a try, so an agent that is down simply does not advertise.
    assert "} catch { }" in caps


# ---------------------------------------------------------------------------
# Create Session must not offer a node it cannot reach
# ---------------------------------------------------------------------------

def test_the_ui_requires_a_transport_not_just_an_agent_type():
    assert "function nodeHasTransport" in SESSIONS_PAGE
    assert "'session_transport'" in SESSIONS_PAGE
    assert "chưa có node agent" in SESSIONS_PAGE


def test_a_script_only_heartbeat_is_not_treated_as_a_transport():
    """windows-setup/1.0.0 is the setup script's scheduled task, which has
    no agent behind it."""
    assert "startsWith('windows-setup/')" in SESSIONS_PAGE


def test_nodes_that_predate_session_transport_keep_working():
    """Every existing node reports a real agent_version and no
    session_transport capability; gating on the capability alone would have
    disabled the entire fleet."""
    page = SESSIONS_PAGE
    gate = page[page.index("function nodeHasTransport"):page.index("function nodeSummaryLabel")]
    assert "node.id === 'local'" in gate, "the in-process local node needs no transport"
    assert "version !== ''" in gate
    # Positive signals only -- no list of known-bad nodes.
    assert "dell" not in gate and "5420" not in gate
