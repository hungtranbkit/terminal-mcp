"""Add Node Linux wizard -> public bootstrap -> installer integration contract.

Guard against regressions where the wizard points to a stale installer or
systemd is given a quoted (and invalid) WorkingDirectory. These tests do not
touch a controller or execute apt/systemctl.
"""
from __future__ import annotations

from pathlib import Path

from terminal_mcp.linux_onboarding import build_linux_pair_command, render_linux_pair_script


def test_add_node_linux_command_fetches_current_verified_installer():
    cmd = build_linux_pair_command(
        controller_url="https://terminal-bootstrap.mesflow.net",
        node_id="nec1",
        enrollment_code="TMCP-ABCDE-ABCDE-ABCDE",
    )
    assert "https://terminal-bootstrap.mesflow.net/enroll/linux-pair.sh" in cmd
    assert "bash -s --" in cmd
    assert "nec1 TMCP-ABCDE-ABCDE-ABCDE" in cmd

    script = render_linux_pair_script()
    # The public script is not bound to any pairing code. Fetch trusted HEAD,
    # verify the installer against its git blob, and run it in --paired mode.
    assert "TMCP-ABCDE-ABCDE-ABCDE" not in script
    assert "fetch --quiet --no-tags origin refs/heads/main" in script
    assert 'UPSTREAM_COMMIT="$(git -C "$HOME/terminal-mcp" rev-parse --verify' in script
    assert 'ACTUAL_INSTALLER_BLOB="$(git -C "$HOME/terminal-mcp" hash-object "$FRESH_INSTALLER")"' in script
    assert 'if [[ "$EXPECTED_INSTALLER_BLOB" != "$ACTUAL_INSTALLER_BLOB" ]]' in script
    assert 'bash "$FRESH_INSTALLER"' in script
    assert '--source-dir "$SOURCE_DIR" --paired' in script


def test_live_wizard_installer_includes_systemd_validity_guards():
    installer = (Path(__file__).resolve().parents[1] / "deploy" / "install-node-agent.sh").read_text()
    assert 'WorkingDirectory=${REPO_DIR//%/%%}' in installer
    assert 'WorkingDirectory=$(systemd_quote "$REPO_DIR")' not in installer
    assert 'UNIT_LOAD_STATE="$(systemctl --user show terminal-node-agent.service' in installer
    assert installer.index('UNIT_LOAD_STATE=') < installer.index('systemctl --user enable terminal-node-agent.service')
    assert installer.index('UNIT_LOAD_STATE=') < installer.index('systemctl --user restart terminal-node-agent.service')
    assert "KillMode=process" in installer
