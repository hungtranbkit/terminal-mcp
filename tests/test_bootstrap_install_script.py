from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.bootstrap_install_script import render_install_script

HANDLE = "0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("platform,installer", [
    ("linux", "install-node-agent.sh"),
    ("macos", "install-node-agent-macos.sh"),
])
def test_paired_installer_requires_explicit_user_consent_and_uses_https(platform, installer):
    script = render_install_script(platform=platform, handle=HANDLE,
                                   controller="https://bootstrap.example",
                                   dashboard_origin="https://dashboard.example")
    assert 'read -r -p "Continue on this machine? [y/N] "' in script
    assert 'TMCP_PAIR_CONTROLLER="https://bootstrap.example"' not in script
    assert 'os.environ["TMCP_PAIR_CONTROLLER"].rstrip("/") + "/dashboard/api/enroll/redeem"' in script
    assert "https://dashboard.example" in script
    assert "deploy/" + installer in script
    assert "--onboarding" in script
    assert "sudo " not in script
    assert "TERMINAL_MCP_ONBOARDING_NODE_TOKEN=\"$NODE_TOKEN\"" in script
    assert '0123456789abcdef0123456789abcdef' in script
    assert 'token_hex' not in script
    assert subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True).returncode == 0
    python = script.split("python3 - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    compile(python, "paired-bootstrap.py", "exec")


def test_paired_installer_rejects_non_https_or_malformed_handle():
    with pytest.raises(ValueError, match="HTTPS"):
        render_install_script(platform="linux", handle=HANDLE, controller="http://bootstrap.example",
                              dashboard_origin="https://dashboard.example")
    with pytest.raises(ValueError, match="handle"):
        render_install_script(platform="macos", handle="too-short", controller="https://bootstrap.example",
                              dashboard_origin="https://dashboard.example")
