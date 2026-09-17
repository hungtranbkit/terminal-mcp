"""Detecting per-user npm launchers from a SYSTEM heartbeat.

Node 54201 finished an ai_coding install cleanly -- exit 0, recurring
heartbeats, git/node/gh reported -- and still showed agent_types []. The
CLIs were installed and working:

    C:\\Users\\Admin\\AppData\\Roaming\\npm\\claude.cmd
    C:\\Users\\Admin\\AppData\\Roaming\\npm\\codex.cmd
    C:\\Users\\Admin\\AppData\\Roaming\\npm\\opencode.cmd

npm installs global CLIs PER USER on Windows. The heartbeat task runs as
SYSTEM, whose PATH does not contain that directory, so Get-Command found
nothing. Machine-wide tools (node, git, gh under Program Files) resolved
fine -- which is why the gap read as "npm failed" rather than "SYSTEM
cannot see it".

The rule these encode: record the ONE directory this install actually used,
probe it explicitly, and never report a capability that is not backed by a
real file.
"""
from __future__ import annotations

import pytest

from terminal_mcp.windows_onboarding import (
    GENERIC_CODE,
    PROFILE_AI_CODING,
    PROFILE_MINIMAL,
    render_setup_script,
)


def _script(profile: str = PROFILE_MINIMAL) -> str:
    return render_setup_script(
        enrollment_code=GENERIC_CODE, controller_url="https://bootstrap.example",
        node_id="pending", display_name="pending", profile=profile)


def _beat(script: str) -> str:
    """The generated heartbeat script, which is what runs as SYSTEM."""
    start = script.index("$beat = @\"")
    end = script.index("Set-Content -Path $BeatRunner")
    return script[start:end]


# ---------------------------------------------------------------------------
# the install side: recording where npm actually put things
# ---------------------------------------------------------------------------

def test_the_installer_asks_npm_where_its_global_bin_is():
    """npm's own answer is authoritative and survives a custom prefix."""
    script = _script()
    resolver = script[script.index("function Get-LauncherDirs"):script.index("$LauncherDirs = @(Get-LauncherDirs)")]
    assert "npm config get prefix" in resolver


def test_the_fallbacks_are_the_same_users_deterministic_paths():
    script = _script()
    resolver = script[script.index("function Get-LauncherDirs"):script.index("$LauncherDirs = @(Get-LauncherDirs)")]
    assert "Join-Path $env:APPDATA 'npm'" in resolver
    assert "Join-Path $env:LOCALAPPDATA 'npm'" in resolver
    # No guessing at other user profiles: this is the install user's dir only.
    assert "C:\\Users\\*" not in resolver
    assert "Get-ChildItem" not in resolver


def test_only_directories_that_exist_are_recorded():
    """A recorded path that is not there would make the heartbeat probe a
    directory that never existed."""
    script = _script()
    resolver = script[script.index("function Get-LauncherDirs"):script.index("$LauncherDirs = @(Get-LauncherDirs)")]
    assert resolver.count("Test-Path -LiteralPath") >= 2


def test_recorded_directories_are_deduplicated():
    script = _script()
    resolver = script[script.index("function Get-LauncherDirs"):script.index("$LauncherDirs = @(Get-LauncherDirs)")]
    assert "$seen" in resolver and "ToLowerInvariant()" in resolver


def test_a_node_with_no_npm_is_a_skip_not_a_failure():
    """A Minimal node has no npm and nothing to record."""
    script = _script()
    assert "'Launcher path' 'SKIP'" in script
    assert "'Launcher path' 'OK'" in script
    assert "'Launcher path' 'FAIL'" not in script


# ---------------------------------------------------------------------------
# the heartbeat side: resolving without widening SYSTEM's PATH
# ---------------------------------------------------------------------------

def test_the_beat_script_carries_the_recorded_directories():
    script = _script()
    beat = _beat(script)
    assert "$LauncherDirs = @($LauncherDirsLiteral)" in beat
    # Rendered with explicit quoting, because these are paths with spaces.
    assert "$LauncherDirsLiteral = ((" in script
    assert "-replace \"'\", \"''\"" in script


def test_detection_probes_path_first_then_the_recorded_directories():
    beat = _beat(_script())
    resolver = beat[beat.index("function Resolve-Tool"):beat.index("function Get-AgentTypes")]
    assert "Get-Command `$Name" in resolver
    assert "foreach (`$dir in `$LauncherDirs)" in resolver


def test_a_capability_is_only_reported_when_a_real_file_backs_it():
    """Never fabricate availability: PATH hits are re-checked on disk, and
    directory hits must be an actual leaf file."""
    beat = _beat(_script())
    resolver = beat[beat.index("function Resolve-Tool"):beat.index("function Get-AgentTypes")]
    assert "Test-Path -LiteralPath `$onPath.Source" in resolver
    assert "Test-Path -LiteralPath `$candidate -PathType Leaf" in resolver


def test_windows_executable_extensions_are_tried():
    """claude.cmd is what npm actually writes; a bare name does not resolve
    outside PATH."""
    beat = _beat(_script())
    resolver = beat[beat.index("function Resolve-Tool"):beat.index("function Get-AgentTypes")]
    for ext in ("'.cmd'", "'.exe'", "'.bat'"):
        assert ext in resolver, ext


def test_system_path_is_never_widened():
    """Detection must not change what this SYSTEM service can execute."""
    beat = _beat(_script())
    for banned in ("$env:PATH +=", "$env:Path +=", "setx", "[Environment]::SetEnvironmentVariable"):
        assert banned not in beat, banned


def test_agent_types_uses_the_resolver_not_bare_get_command():
    beat = _beat(_script())
    agent = beat[beat.index("function Get-AgentTypes"):beat.index("function Get-Capabilities")]
    assert "if (Resolve-Tool `$pair[1])" in agent
    assert "Get-Command `$pair[1]" not in agent
    for tool in ("claude", "codex", "opencode"):
        assert tool in agent, tool


def test_capabilities_uses_the_resolver_too():
    """python is frequently a per-user install as well."""
    beat = _beat(_script())
    caps = beat[beat.index("function Get-Capabilities"):]
    assert "if (Resolve-Tool `$pair[1])" in caps
    assert "Get-Command `$pair[1]" not in caps


@pytest.mark.parametrize("profile", [PROFILE_MINIMAL, PROFILE_AI_CODING])
def test_every_profile_still_renders_and_carries_the_resolver(profile):
    script = _script(profile)
    assert "function Get-LauncherDirs" in script
    assert "function Resolve-Tool" in _beat(script)


def test_the_regeneration_is_idempotent_by_construction():
    """heartbeat.ps1 is rewritten on every install and -Repair, so a node
    that ran an older script picks the resolver up on its next run without
    any migration."""
    script = _script()
    assert "Set-Content -Path $BeatRunner" in script
    # And the task registration that follows still replaces rather than adds.
    assert "Register-ScheduledTask" in script and "-Force" in script
