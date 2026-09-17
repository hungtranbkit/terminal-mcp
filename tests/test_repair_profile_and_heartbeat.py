"""What -Repair restores, whether the heartbeat task actually runs, and the
helper's failure code reaching a log.

All three come from one live run on 2026-09-15. Node 5420 enrolled, the
installer ran to completion, sent exactly ONE heartbeat -- its own one-shot
probe -- and exited non-zero. Three separate defects were visible in that
single run:

  * the helper downloads the GENERIC script, which is rendered with
    profile="minimal"; -Repair restored node_id and controller_url from
    node.json but never the profile, so an ai_coding enrollment reinstalled
    as Minimal and no Claude/Codex CLI was ever installed -- which is why
    agent_types could only ever be empty
  * the heartbeat task was registered with an -AtStartup trigger only, and
    the one thing that started it now had its errors silenced, so a task
    that never ran still reported OK
  * the helper reported a failure code, and the controller dropped it on
    the floor, so "failed" was the end of the diagnosis
"""
from __future__ import annotations

import re

import pytest

from terminal_mcp.enrollment import (
    EXIT_CODE_MAX,
    EXIT_CODE_MIN,
    FAILURE_CODES,
    normalize_exit_code,
    normalize_failure_code,
)
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


# ---------------------------------------------------------------------------
# 1. -Repair restores the profile the node was ENROLLED with
# ---------------------------------------------------------------------------

def test_the_generic_script_still_ships_as_minimal():
    """Unchanged: this is what /enroll/windows-setup.ps1 serves, and the
    catalogue is what lets -Repair correct it."""
    script = _script(PROFILE_MINIMAL)
    assert "$ProfileName     = 'minimal'" in script
    assert "$WingetPackages = @()" in script
    assert "$NpmTools = @()" in script


def test_every_profile_catalogue_is_baked_into_every_script():
    """The profile id is on disk in node.json; the table to resolve it
    against has to travel with the script."""
    script = _script(PROFILE_MINIMAL)
    catalogue = script[script.index("$ProfileCatalog = @{"):]
    catalogue = catalogue[:catalogue.index("\n}")]
    for profile in (PROFILE_MINIMAL, PROFILE_DEVELOPER, PROFILE_AI_CODING):
        assert f"{profile} = @{{" in catalogue, profile
    # ai_coding's actual payload, the thing that was never installed.
    for probe in ("claude", "codex", "opencode"):
        assert f"Probe = '{probe}'" in catalogue, probe
    for package in ("Git.Git", "OpenJS.NodeJS.LTS"):
        assert package in catalogue, package


def test_repair_rehydrates_profile_packages_and_npm_tools():
    script = _script()
    repair = script[script.index("if ($Repair) {"):script.index("Add-Step 'Enrollment' 'SKIP'")]
    assert "$bootstrap.profile" in repair
    assert "$ProfileCatalog.ContainsKey($savedProfile)" in repair
    assert "$WingetPackages = @($ProfileCatalog[$savedProfile].Winget)" in repair
    assert "$NpmTools = @($ProfileCatalog[$savedProfile].Npm)" in repair
    assert "$ProfileName = $savedProfile" in repair


def test_repair_still_restores_node_id_and_controller_url():
    """Backward compatibility: what -Repair already did must keep working."""
    script = _script()
    repair = script[script.index("if ($Repair) {"):script.index("Add-Step 'Enrollment' 'SKIP'")]
    assert "$NodeId = [string]$bootstrap.node_id" in repair
    assert "$ControllerUrl = [string]$bootstrap.controller_url" in repair


def test_an_unknown_profile_keeps_the_baked_values_rather_than_guessing():
    script = _script()
    repair = script[script.index("if ($Repair) {"):script.index("Add-Step 'Enrollment' 'SKIP'")]
    assert "'Profile restore' 'WARN'" in repair
    assert "unknown profile" in repair


def test_a_node_json_without_a_profile_changes_nothing():
    """minimal/developer nodes enrolled before this existed must not break."""
    script = _script()
    repair = script[script.index("if ($Repair) {"):script.index("Add-Step 'Enrollment' 'SKIP'")]
    # The whole rehydration sits behind a presence check.
    assert "if ($bootstrap.profile) {" in repair


@pytest.mark.parametrize("profile", [PROFILE_MINIMAL, PROFILE_DEVELOPER, PROFILE_AI_CODING])
def test_every_profile_still_renders_a_usable_script(profile):
    script = _script(profile)
    assert "param(" in script
    assert "$ProfileCatalog = @{" in script
    assert f"$ProfileName     = '{profile}'" in script


# ---------------------------------------------------------------------------
# 2. the heartbeat task actually runs
# ---------------------------------------------------------------------------

def _heartbeat_block(script: str) -> str:
    start = script.index("$action = New-ScheduledTaskAction")
    end = script.index("Add-Step 'Profile packages'")
    return script[start:end]


def test_the_task_has_a_trigger_that_fires_without_a_reboot():
    """-AtStartup alone is why node 5420 sent exactly one heartbeat: the
    probe's. Nothing would have started the recurring task until someone
    rebooted the machine."""
    block = _heartbeat_block(_script())
    assert "New-ScheduledTaskTrigger -AtStartup" in block, "startup trigger must stay"
    assert "-Once -At (Get-Date).AddSeconds(30)" in block
    assert "-RepetitionInterval (New-TimeSpan -Minutes 5)" in block
    assert "-Trigger $triggers" in block


def test_the_start_is_verified_instead_of_silenced():
    block = _heartbeat_block(_script())
    # The silent form is gone.
    assert "Start-ScheduledTask -TaskName $TaskBeat -ErrorAction SilentlyContinue" not in block
    assert "Start-ScheduledTask -TaskName $TaskBeat\n" in block
    # And the state is actually polled.
    assert "Get-ScheduledTask -TaskName $TaskBeat" in block
    assert "$beatState -eq 'Running'" in block


def test_registered_but_not_running_is_a_failure_not_an_ok():
    """The exact silent-success path that made a dead task look installed."""
    block = _heartbeat_block(_script())
    assert "'Heartbeat task' 'FAIL'" in block
    assert "registered but did not start" in block
    # OK is only claimed on a verified Running state.
    ok_index = block.index("'Heartbeat task' 'OK'")
    assert "$beatState -eq 'Running'" in block[:ok_index]


def test_the_failure_tells_the_operator_what_to_run():
    block = _heartbeat_block(_script())
    assert "Get-ScheduledTaskInfo" in block


def test_registration_stays_idempotent():
    """A re-run or a -Repair must replace the task, never add a second."""
    block = _heartbeat_block(_script())
    assert "Register-ScheduledTask" in block and "-Force" in block
    assert "MultipleInstances IgnoreNew" in block


def test_the_task_still_runs_as_system_at_highest_level():
    block = _heartbeat_block(_script())
    assert "-UserId 'SYSTEM'" in block and "-RunLevel Highest" in block


# ---------------------------------------------------------------------------
# 3. the helper's failure code reaches a log, safely
# ---------------------------------------------------------------------------

def test_the_code_vocabulary_matches_the_helper_exactly():
    """helper/cmd/terminal-mcp-bootstrap/progress.go's closed set."""
    assert FAILURE_CODES == (
        "pairing_rejected", "controller_unreachable", "script_download_failed",
        "service_install_failed", "setup_launch_failed",
    )


@pytest.mark.parametrize("code", FAILURE_CODES)
def test_a_known_code_is_accepted(code):
    assert normalize_failure_code(code) == code


@pytest.mark.parametrize("bogus", [
    "", "   ", None, "rm -rf /", "pairing_rejected; DROP TABLE",
    "PAIRING_REJECTED", "unknown_code", 42, {"a": 1},
    "pairing_rejected\nnode_token=abc",
])
def test_anything_outside_the_set_becomes_none(bogus):
    """Nothing the machine sends may put free text into a log line."""
    assert normalize_failure_code(bogus) is None


@pytest.mark.parametrize("value,want", [
    (0, 0), (1, 1), (2, 2), (3, 3), (255, 255), (-1, -1), ("3", 3),
])
def test_a_plausible_exit_code_is_accepted(value, want):
    assert normalize_exit_code(value) == want


@pytest.mark.parametrize("bogus", [None, "", "abc", 256, -2, 10**9, [1], {"x": 1}, float("nan")])
def test_an_out_of_range_exit_code_becomes_none(bogus):
    assert normalize_exit_code(bogus) is None


def test_the_bounds_are_the_ones_the_installer_can_actually_produce():
    # -1 is the helper's own "no exit code" (timeout / could not start).
    assert EXIT_CODE_MIN == -1
    assert EXIT_CODE_MAX == 255


def test_the_route_normalises_before_logging_and_needs_no_migration():
    import inspect

    from terminal_mcp import dashboard
    source = inspect.getsource(dashboard)
    assert 'normalize_failure_code(body.get("code"))' in source
    assert 'normalize_exit_code(body.get("exit_code"))' in source
    # Log-only: no new column anywhere.
    assert "progress_code" not in source
    assert "ALTER TABLE enrollments" not in source
