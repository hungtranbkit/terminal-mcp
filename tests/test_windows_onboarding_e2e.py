"""End-to-end onboarding against a REAL controller process and a REAL
PowerShell interpreter.

The unit tests prove the pieces. This proves the wire: a real uvicorn
process serving the real dashboard app, and a real `pwsh` running the
exact request-building code lifted out of the exact script the dashboard
generated -- `Invoke-RestMethod`, `ConvertTo-Json`, the `Authorization:
Bearer` header, the metrics hashtable shape. A mismatch between what
PowerShell serialises and what Starlette/the controller expects is the
single most likely way this feature breaks in the field, and it is
invisible to a Python-only test.

What is NOT covered here, stated plainly rather than implied:
  - Add-WindowsCapability / sshd / Scheduled Tasks / winget / Tailscale
    need real Windows. Their CONFIGURATION is asserted statically (the
    AtStartup + SYSTEM triggers, the sshd drop-in, the ssh flags) and
    their syntax is parsed by a real PowerShell in
    tests/test_windows_onboarding.py.
  - Reboot persistence is verified by configuration, not by rebooting.
  - The rescue tunnel needs a gateway host; see docs for what is and is
    not verified there.

Skips cleanly where `pwsh`/`powershell` is not installed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_pwsh() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    # A tarball extracted into a scratch dir (how this gets run on a Linux
    # dev box that has no PowerShell package installed).
    for candidate in Path("/tmp").glob("*/**/pwsh/pwsh"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def controller_process(tmp_path):
    """A real terminal-mcp-http process, on loopback, with every store
    pointed at this test's own tmp_path -- it must never touch the real
    ~/.local/state/terminal-mcp databases or the real config.yaml."""
    port = _free_port()
    state = tmp_path / "state"
    state.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key_file = tmp_path / "controller.pub"
    key_file.write_text(
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ2mCPfL0DEqQ7bDX8ky4Qs+z0lyHkQKcQCZoV3mM1Rf e2e@test\n")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({
        "permissions": {"terminal_read": True, "terminal_input": True},
        "allowed_session_patterns": ["test-*"],
        "session_lifecycle": {"enabled": False, "allowed_cwd_roots": [str(workspace)]},
        "nodes": {
            "onboarding": {
                "enabled": True,
                "controller_url": f"http://127.0.0.1:{port}",
                "controller_ssh_public_key_file": str(key_file),
                "heartbeat_interval_seconds": 5,
            }
        },
    }))

    env = dict(os.environ)
    env.update({
        "TERMINAL_MCP_CONFIG": str(config_path),
        "TERMINAL_MCP_HTTP_PORT": str(port),
        "XDG_STATE_HOME": str(state),
        "TERMINAL_MCP_NODES_DB": str(state / "nodes.db"),
        "TERMINAL_MCP_CONNECTIONS_DB": str(state / "connections.db"),
        "TERMINAL_MCP_ENROLLMENT_DB": str(state / "enrollment.db"),
        "TERMINAL_MCP_TRANSPORTS_DB": str(state / "transports.db"),
        "TERMINAL_MCP_RESCUE_DB": str(state / "rescue.db"),
        "TERMINAL_MCP_RESCUE_KEYS_DIR": str(state / "rescue-keys"),
        "TERMINAL_MCP_AUDIT_DB": str(state / "audit.db"),
        "TERMINAL_MCP_BINDINGS_DB": str(state / "bindings.db"),
        "TERMINAL_MCP_GRANTS_DB": str(state / "grants.db"),
        "TERMINAL_MCP_LEASE_DB": str(state / "leases.db"),
        "TERMINAL_MCP_KILLED_SESSIONS_DB": str(state / "killed-sessions.db"),
        "TERMINAL_MCP_LAN_BIND": "",
    })
    # The REAL production entry point -- terminal_mcp.server_http.main(),
    # the same code path the systemd unit runs -- on a free port via
    # TERMINAL_MCP_HTTP_PORT. Not a hand-assembled test app: the point of
    # this file is to certify the wiring server_http.py actually performs,
    # including the persistent OnboardingService it constructs.
    process = subprocess.Popen(
        [sys.executable, "-c", "from terminal_mcp.server_http import main; main()"],
        cwd=str(REPO_ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    import urllib.error
    import urllib.request
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"controller exited early:\n{process.stdout.read() if process.stdout else ''}")
        try:
            with urllib.request.urlopen(f"{base}/dashboard/api/nodes/onboard/profiles", timeout=2) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    else:
        process.terminate()
        pytest.fail(f"controller did not come up:\n{process.stdout.read() if process.stdout else ''}")

    yield base, process
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _post(base: str, path: str, payload: dict, *, headers: dict | None = None):
    import urllib.error
    import urllib.request
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Origin": base, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def _get(base: str, path: str):
    import urllib.request
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return response.status, json.loads(response.read() or b"{}")


# The two request-building fragments the generated script actually uses.
# Lifted from the generated text rather than retyped, so this test cannot
# drift away from the artifact it is meant to certify.
def _extract_enroll_body(script: str) -> str:
    match = re.search(r"( {4}\$body = @\{\n.*?\n {4}\} \| ConvertTo-Json -Depth 5)", script, re.S)
    assert match, "could not find the enrollment request body in the generated script"
    return match.group(1)


def _extract_heartbeat_body(script: str) -> str:
    match = re.search(r"( {8}\$probe = @\{\n.*?\n {8}\} \| ConvertTo-Json -Depth 5)", script, re.S)
    assert match, "could not find the verification heartbeat body in the generated script"
    return match.group(1)


def test_real_powershell_enrolls_against_a_real_controller(controller_process, tmp_path):
    pwsh = _find_pwsh()
    if pwsh is None:
        pytest.skip("no PowerShell available on this host")
    base, _process = controller_process

    # 1. The operator generates a setup from the dashboard.
    status, created = _post(base, "/dashboard/api/nodes/onboard/enrollments",
                            {"node_id": "e2e-win", "profile": "minimal", "os": "windows"})
    assert status == 200, created
    code = created["code"]
    script = created["script"]
    assert code in script

    # The node does not exist yet -- a generated code is not a node.
    _status, nodes = _get(base, "/dashboard/api/nodes")
    assert "e2e-win" not in {node["id"] for node in nodes["nodes"]}

    # 2. The machine runs the script. We run the script's OWN enrollment
    #    and heartbeat request code, in a real PowerShell.
    harness = tmp_path / "harness.ps1"
    harness.write_text(f"""
param([string] $ControllerUrl, [string] $EnrollmentCode, [string] $Out)
$ErrorActionPreference = 'Stop'
$ScriptVersion = '{created["script_version"]}'
$NodeId = 'e2e-win'
$addresses = @{{ tailscale_ip = $null; lan_ip = '192.168.44.7' }}
# The real script has generated a rescue keypair by this point; this
# deployment has no gateway, so an empty value is the realistic input.
$rescuePublicKey = ''

# The ONE Windows-only cmdlet the extracted block calls. Stubbed, and
# stubbed narrowly: everything else below -- the hashtable shape,
# ConvertTo-Json, Invoke-RestMethod, the Bearer header -- is the script's
# own code running for real against a real controller.
function Get-CimInstance {{ param([Parameter(Position = 0)] $ClassName)
    [pscustomobject]@{{ Caption = 'Windows 11 Pro (e2e stub)' }} }}

{_extract_enroll_body(script)}

$response = Invoke-RestMethod -Method Post -Uri "$ControllerUrl/dashboard/api/enroll/consume" `
    -ContentType 'application/json' -Body $body -TimeoutSec 45 -UseBasicParsing
$bootstrap = $response
$tok = [string]$bootstrap.node_token

{_extract_heartbeat_body(script)}

Invoke-RestMethod -Method Post -Uri "$ControllerUrl/dashboard/api/nodes/$NodeId/heartbeat" `
    -Headers @{{ Authorization = "Bearer $tok" }} -ContentType 'application/json' `
    -Body $probe -TimeoutSec 20 -UseBasicParsing | Out-Null

# Report back what the node received, MINUS the token -- the same
# exclusion the real script applies before writing node.json.
$bootstrap | Select-Object * -ExcludeProperty node_token |
    ConvertTo-Json -Depth 8 | Set-Content -Path $Out -Encoding utf8
'ENROLLED'
""".replace("$env:COMPUTERNAME", "'E2E-WIN-PC'"), encoding="utf-8")

    out = tmp_path / "node.json"
    result = subprocess.run(
        [pwsh, "-NoProfile", "-File", str(harness), "-ControllerUrl", base,
         "-EnrollmentCode", code, "-Out", str(out)],
        capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, f"PowerShell enrollment failed:\n{result.stdout}\n{result.stderr}"
    assert "ENROLLED" in result.stdout

    # 3. The node is now real, online, and reported as Windows.
    _status, nodes = _get(base, "/dashboard/api/nodes")
    node = next((row for row in nodes["nodes"] if row["id"] == "e2e-win"), None)
    assert node is not None, "the enrolled node did not appear on the Nodes page"
    assert node["status"] == "online"
    assert node["platform"] == "windows"
    assert node["session_backend"] == "windows_pty"

    # 4. Its transports were recorded from what it reported.
    _status, onboarding = _get(base, "/dashboard/api/nodes/e2e-win/onboarding")
    assert onboarding["registered"] is True and onboarding["has_credentials"] is True
    assert {t["kind"] for t in onboarding["transports"]} == {"lan"}
    # Exact, with no `or` fallback: an alternative spelling accepted here
    # is how the agent-port-vs-ssh-port bug reached staging unnoticed.
    lan = onboarding["transports"][0]
    assert lan["endpoint"] == "ssh://192.168.44.7:22"
    assert (lan["host"], lan["port"]) == ("192.168.44.7", 22)

    # 5. What the node wrote to disk carries no token, and does carry the
    #    controller's PUBLIC key.
    persisted = json.loads(out.read_text(encoding="utf-8"))
    assert "node_token" not in json.dumps(persisted)
    assert persisted["ssh"]["authorized_key"].startswith("ssh-ed25519 ")
    assert persisted["ssh"]["password_authentication"] is False
    assert persisted["rescue"]["configured"] is False  # no gateway in this deployment

    # 6. Replay of the same code is refused, from a real client.
    status, replay = _post(base, "/dashboard/api/enroll/consume",
                           {"code": code, "hostname": "ATTACKER"})
    assert status == 401 and replay["error"] == "ENROLLMENT_ALREADY_USED"

    # 7. Remove Node revokes everything.
    status, removal = _post(base, "/dashboard/api/nodes/e2e-win/remove", {})
    assert status == 200 and removal["deregistered"] is True
    _status, nodes = _get(base, "/dashboard/api/nodes")
    assert "e2e-win" not in {row["id"] for row in nodes["nodes"]}


def test_generic_script_is_downloadable_from_a_real_controller(controller_process):
    base, _process = controller_process
    import urllib.request
    with urllib.request.urlopen(base + "/enroll/windows-setup.ps1", timeout=30) as response:
        assert response.status == 200
        text = response.read().decode()
        assert response.headers["X-Terminal-Mcp-Setup-Version"]
        assert response.headers["Content-Disposition"].endswith('filename="windows-setup.ps1"')
    assert "#Requires -Version 5.1" in text
    assert not re.search(r"\b[0-9a-f]{64}\b", text), "the public script must carry no token-shaped secret"
