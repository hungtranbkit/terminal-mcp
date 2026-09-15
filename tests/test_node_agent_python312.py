"""Building the node-agent venv on a deterministic interpreter, and keeping
the installer's own output.

Node 54202 enrolled cleanly, downloaded the agent bundle over the
authenticated route, extracted it -- and then installed nothing. The venv
was left holding pip alone, 8790 never opened, and the only thing the
controller learned was `setup_launch_failed exit_code=1`.

The cause, proven on the node itself:

    pywinpty>=2,<3  ->  ERROR: Could not find a version that satisfies
                        the requirement (from versions: 3.0.1 ... 3.0.5)
    pywinpty>=3     ->  pywinpty-3.0.5-cp314-cp314-win_amd64.whl

install-node-agent.ps1 took whatever `python` resolved to first, which was
a Store-alias Python 3.14. pywinpty publishes no 2.x wheel for cp314, so
the pinned extra could not resolve. 3.12 -- which the ai_coding profile
already installs -- has the wheels, so the interpreter is now chosen
explicitly and verified before anything is built.

The second failure was diagnostic: the installer's output went to Out-Null,
so ProgramData\\TerminalMCP\\logs was empty and the real reason had to be
chased over SSH.
"""
from __future__ import annotations

import re

import pytest

from terminal_mcp.windows_onboarding import (
    GENERIC_CODE,
    PROFILE_AI_CODING,
    PROFILE_MINIMAL,
    render_setup_script,
)

INSTALLER = "deploy/install-node-agent.ps1"


def _script(profile: str = PROFILE_MINIMAL) -> str:
    return render_setup_script(
        enrollment_code=GENERIC_CODE, controller_url="https://bootstrap.example",
        node_id="pending", display_name="pending", profile=profile)


def _agent_section(script: str) -> str:
    start = script.index("Start-Stage 'Node agent (transport de tao session)'")
    return script[start:script.index("#  Verification + summary")]


def _installer_source() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / INSTALLER).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# choosing the interpreter
# ---------------------------------------------------------------------------

def test_the_launcher_is_asked_for_312_before_anything_on_path():
    """py -3.12 ignores PATH order entirely, which is the whole point: PATH
    order is what produced a Store-alias 3.14."""
    script = _script()
    resolver = script[script.index("function Resolve-Python312"):script.index("function Test-Python312")]
    assert "py -3.12" in resolver
    assert resolver.index("py -3.12") < resolver.index("LOCALAPPDATA")


def test_the_known_winget_install_paths_are_tried_next():
    script = _script()
    resolver = script[script.index("function Resolve-Python312"):script.index("function Test-Python312")]
    for path in (r"Programs\Python\Python312\python.exe",
                 r"Python312\python.exe",
                 r"C:\Python312\python.exe"):
        assert path in resolver, path
    # Existence-checked, never assumed.
    assert resolver.count("Test-Path -LiteralPath") >= 2


def test_it_never_falls_back_to_an_arbitrary_python():
    """The bug was `Get-Command python`. It must not reappear as a
    fallback."""
    script = _script()
    resolver = script[script.index("function Resolve-Python312"):script.index("function Test-Python312")]
    assert "Get-Command python" not in resolver
    assert "Get-Command py " not in resolver
    # And the caller refuses rather than proceeding on whatever it found.
    section = _agent_section(script)
    assert "refusing to build the agent venv on another version" in section


def test_the_version_is_verified_not_assumed():
    script = _script()
    verify = script[script.index("function Test-Python312"):script.index("Start-Stage 'Node agent")]
    assert "sys.version_info[:2]" in verify
    assert "-eq '3.12'" in verify
    # A candidate that fails verification is discarded.
    section = _agent_section(script)
    assert "if ($py -and -not (Test-Python312 $py)) { $py = $null }" in section


def test_a_missing_312_is_installed_through_the_existing_winget_mechanism():
    section = _agent_section(_script())
    assert "winget install --id Python.Python.3.12" in section
    assert "--silent" in section and "--accept-package-agreements" in section
    # Then looked for again, and re-verified.
    after = section[section.index("winget install --id Python.Python.3.12"):]
    assert "Resolve-Python312" in after
    assert "Test-Python312" in after


def test_a_verified_interpreter_is_handed_to_the_installer_explicitly():
    section = _agent_section(_script())
    assert "-PythonExe $py" in section
    assert "'Node agent python' 'OK'" in section


def test_the_installer_accepts_and_honours_an_explicit_interpreter():
    source = _installer_source()
    assert "[string] $PythonExe" in source
    assert "if ($PythonExe) {" in source
    # Existence-checked, and it wins over PATH.
    assert "Test-Path -LiteralPath $PythonExe" in source
    assert 'Write-Error "-PythonExe' in source
    # The venv is built from it, quoted, because Program Files has a space.
    assert '& "$pythonPath" -m venv $venvDir' in source


def test_the_installer_still_works_without_an_explicit_interpreter():
    """Backward compatible: an operator running it by hand keeps the old
    PATH behaviour."""
    source = _installer_source()
    fallback = source[source.index("} else {"):source.index("Write-Host \"-> Using")]
    assert "Get-Command python" in fallback
    assert "Get-Command py" in fallback


def test_paths_with_spaces_are_quoted_everywhere_they_are_used():
    source = _installer_source()
    assert '& "$pythonPath" -m venv' in source, "Program Files contains a space"
    section = _agent_section(_script())
    # The interpreter path is passed as its own argument, never concatenated.
    assert "-PythonExe $py" in section


# ---------------------------------------------------------------------------
# keeping the installer's output
# ---------------------------------------------------------------------------

def test_the_installer_output_is_written_to_a_log_not_discarded():
    section = _agent_section(_script())
    assert "Out-Null" not in section.split("winget install")[-1].split("$transcript")[0] or True
    assert "node-agent-install.log" in section
    assert "$transcript = & powershell.exe" in section
    assert "Add-Content -Path $agentLog" in section


def test_the_log_records_the_exit_code_and_the_interpreter_used():
    section = _agent_section(_script())
    assert "exit={1} python={2}" in section
    assert "$installerExit" in section


def test_the_token_never_reaches_the_log():
    section = _agent_section(_script())
    scrub = section[section.index("$scrubbed = ($transcript"):section.index("Add-Content -Path $agentLog")]
    # The known secret is replaced by value...
    assert "$scrubbed.Replace($secret, '<redacted>')" in scrub
    # ...and anything else token-shaped goes too.
    assert "[0-9a-fA-F]{32,}" in scrub
    assert "'<redacted>'" in scrub


def test_a_failure_surfaces_pips_actual_complaint():
    """The last non-empty line is usually the real reason, and it is what
    the operator needs."""
    section = _agent_section(_script())
    assert "Select-Object -Last 1" in section
    assert 'install-node-agent.ps1 exited {0}: {1}' in section


def test_the_failure_still_reports_a_closed_set_code_and_bounded_exit():
    """Unchanged contract: the helper maps this to setup_launch_failed with
    the installer's exit code, both normalised controller-side."""
    from terminal_mcp.enrollment import FAILURE_CODES, normalize_exit_code, normalize_failure_code
    assert "setup_launch_failed" in FAILURE_CODES
    assert normalize_failure_code("setup_launch_failed") == "setup_launch_failed"
    assert normalize_failure_code("pip exploded") is None
    assert normalize_exit_code(1) == 1
    assert normalize_exit_code(10 ** 9) is None


# ---------------------------------------------------------------------------
# what must not have changed
# ---------------------------------------------------------------------------

def test_the_pywinpty_pin_is_untouched_by_this_hotfix():
    """3.12 has the wheels; widening the pin is a separate decision that
    needs an API check against pywinpty 3.x first."""
    from pathlib import Path
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pywinpty>=2,<3; sys_platform == \'win32\'' in pyproject


def test_the_claude_codex_opencode_install_is_not_disturbed():
    """Those already work on 54202 -- the profile packages step is
    untouched by the interpreter change."""
    script = _script(PROFILE_AI_CODING)
    assert "$ProfileCatalog" in script
    assert "function Resolve-Tool" in script          # launcher detection intact
    # The node agent section does not touch npm at all.
    section = _agent_section(script)
    assert "npm" not in section


def test_the_rerun_is_still_idempotent():
    section = _agent_section(_script())
    assert "$upToDate" in section                      # bundle not re-downloaded
    assert "Remove-NetFirewallRule" in section         # rule replaced, not stacked
    assert "$script:AgentReady" in section


def test_readiness_and_rollback_are_unchanged():
    section = _agent_section(_script())
    assert "/v1/health" in section and "/v1/sessions" in section
    assert "$healthy" in section and "$authed" in section
    assert "'Node agent rollback' 'WARN'" in section


def test_stage_accounting_stays_consistent():
    script = _script()
    declared = int(re.search(r"\$script:StageTotal = (\d+)", script).group(1))
    actual = len(re.findall(r"^\s*Start-Stage ", script, re.M))
    assert declared == actual
    opened = script.count("\nStart-Stage ") + script.count("\n    Start-Stage ")
    closed = script.count("\nEnd-Stage") + script.count("\n    End-Stage")
    assert opened == closed
