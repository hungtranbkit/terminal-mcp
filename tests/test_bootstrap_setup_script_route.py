"""The generic setup script, as a MACHINE on the public bootstrap hostname
fetches it.

The gap these close is exact. Machine-side URLs are pinned to the public
bootstrap origin, and both install paths fetch the setup script from that
origin -- the helper as /enroll/windows-setup.ps1, the Win + R command as
/w. Neither path was in the bootstrap ingress allowlist, so the edge 404'd
them. A live ai_coding enrollment on 2026-09-15 redeemed successfully,
reported installing_service, and then died there: 404 public, 200 loopback.
The node registered and could never heartbeat, so its agent_types stayed
empty and no Agent Type could be chosen for it.

Allowing the script is safe and always was -- the route's own contract is
that the machine fetching it has no Access session and cannot obtain one,
so the generic script carries no credential. That property is not assumed
here; it is asserted.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (
    AppConfig,
    DashboardConfig,
    InputPolicyConfig,
    NodesConfig,
    OnboardingConfig,
    PermissionsConfig,
    SessionLifecycleConfig,
)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp

BOOTSTRAP = "https://terminal-bootstrap.mesflow.net"
BOOTSTRAP_HOST = "terminal-bootstrap.mesflow.net"
SETUP_PATHS = ("/w", "/enroll/windows-setup.ps1")

TEMPLATE = Path(__file__).resolve().parent.parent / "deploy" / "cloudflare" / \
    "bootstrap-ingress.template.yml"


def _client(tmp_path, *, bootstrap_origin: str = BOOTSTRAP) -> TestClient:
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
        nodes=NodesConfig(onboarding=OnboardingConfig(bootstrap_origin=bootstrap_origin)),
    )
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    # Arrives exactly as a machine on the public bootstrap hostname does.
    return TestClient(server.streamable_http_app(),
                      headers={"Origin": BOOTSTRAP, "Host": BOOTSTRAP_HOST})


# ---------------------------------------------------------------------------
# 200 for the exact script, on both paths a machine uses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", SETUP_PATHS)
def test_the_setup_script_is_served_to_an_unauthenticated_machine(tmp_path, path):
    response = _client(tmp_path).get(path)

    assert response.status_code == 200, response.text
    assert "param(" in response.text
    assert response.headers["X-Terminal-Mcp-Setup-Version"]


def test_both_paths_serve_byte_identical_content(tmp_path):
    """They are one route under two names; a difference would mean the
    helper and the Win + R path were installing different things."""
    client = _client(tmp_path)
    short, long = client.get("/w"), client.get("/enroll/windows-setup.ps1")
    assert short.status_code == long.status_code == 200
    assert short.text == long.text
    assert short.headers["X-Terminal-Mcp-Setup-Sha256"] == long.headers["X-Terminal-Mcp-Setup-Sha256"]


def test_the_script_points_the_machine_back_at_the_public_origin(tmp_path):
    """The whole reason the script has to be reachable here: the machine
    that fetches it is the machine that must call back to this origin."""
    text = _client(tmp_path).get("/enroll/windows-setup.ps1").text
    assert BOOTSTRAP in text
    assert "terminal-dashboard" not in text


# ---------------------------------------------------------------------------
# the no-secret property, asserted rather than assumed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", SETUP_PATHS)
def test_the_served_script_carries_no_secret(tmp_path, path):
    text = _client(tmp_path).get(path).text

    # No real enrollment code: the generic placeholder is baked instead,
    # and the real one is supplied by the operator or the redeemed payload.
    assert "TMCP-00000-00000-00000" in text
    # A placeholder repeats one character per group (TMCP-00000-..., and
    # TMCP-XXXXX-... in the -EnrollmentCode help text). Anything with real
    # entropy in a group is a generated code and must never be in here.
    def _is_placeholder(code: str) -> bool:
        return all(len(set(group)) == 1 for group in code.split("-")[1:])

    real_codes = [m for m in re.findall(
        r"TMCP-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}", text)
        if not _is_placeholder(m)]
    assert not real_codes, real_codes
    # No 64-hex secret of any kind (node token, sha of a credential, ...).
    assert not re.search(r"\b[0-9a-f]{64}\b", text)
    # No private key material, ever.
    for marker in ("BEGIN OPENSSH PRIVATE KEY", "BEGIN RSA PRIVATE KEY",
                   "BEGIN EC PRIVATE KEY", "BEGIN PRIVATE KEY"):
        assert marker not in text, marker
    # No pairing handle shape (32 hex) and no bearer-looking assignment.
    assert not re.search(r"\b[0-9a-f]{32}\b", text)
    assert not re.search(r"(?i)(node_token|bearer)\s*[:=]\s*['\"][^'\"]{16,}", text)


def test_a_machine_needs_no_access_cookie_for_it(tmp_path):
    """If this ever became Access-guarded the ingress entry would be
    pointless: the machine cannot obtain a session."""
    response = _client(tmp_path).get("/enroll/windows-setup.ps1")
    assert response.status_code == 200
    assert "CLOUDFLARE_ACCESS" not in response.text


# ---------------------------------------------------------------------------
# what stays 404: the allowlist did not widen beyond these two paths
# ---------------------------------------------------------------------------

ALLOWED = (
    r"^/dashboard/api/enroll/(consume|redeem|progress)$",
    r"^/(w|enroll/windows-setup\.ps1)$",
    r"^/dashboard/api/nodes/[A-Za-z0-9_-]{1,64}/heartbeat$",
    r"^/health/(live|ready)$",
)


@pytest.mark.parametrize("path", [
    "/mcp", "/mcp/", "/dashboard", "/dashboard/", "/dashboard/nodes",
    "/dashboard/api/nodes", "/dashboard/api/session/input",
    "/dashboard/api/nodes/win-work/token/refresh",
    "/dashboard/api/nodes/onboard/helper/windows-x64",
    "/terminal", "/webterm", "/supervisor", "/login", "/logout", "/app",
    "/admin", "/health/metrics", "/version",
    "/enroll", "/enroll/", "/enroll/consume", "/enroll/windows-setup.ps1.bak",
    "/w/", "/ws",
])
def test_nothing_else_became_reachable(path):
    for pattern in ALLOWED:
        assert not re.match(pattern, path), f"{path} matched {pattern}"


def test_the_template_and_this_suite_agree_on_the_allowlist():
    declared = re.findall(r"path:\s*(\S+)", TEMPLATE.read_text(encoding="utf-8"))
    assert set(declared) == set(ALLOWED), declared


def test_the_deploy_delta_is_exactly_one_new_rule():
    """The whole change, stated as a number so a wider edit fails here."""
    body = TEMPLATE.read_text(encoding="utf-8").split("ingress:", 1)[1]
    rules = [line for line in body.splitlines() if line.strip().startswith("- hostname:")]
    assert len(rules) == 5, "4 routed rules + 1 catch-all 404"
    assert body.count("http_status:404") == 1
    assert body.rindex("http_status:404") > body.rindex("path:")


# ---------------------------------------------------------------------------
# What the script is FOR: a node that heartbeats, and therefore has agents
# ---------------------------------------------------------------------------
#
# The visible symptom of the 404 was "Agent Type unavailable". The chain is
# script -> node agent -> heartbeat -> agent_types. These pin the last link,
# so a node that actually runs the installer reports what it has.

from terminal_mcp.agent_availability import available_agent_types      # noqa: E402


def test_a_node_with_no_launchers_reports_shell_only(monkeypatch):
    """Which is exactly what an unreachable-script node looks like today:
    registered, nothing installed, only a plain shell possible."""
    monkeypatch.setattr("terminal_mcp.launcher_resolution.shutil.which", lambda _name, path=None: None)
    assert available_agent_types((("claude", "claude"), ("codex", "codex"))) == ("shell",)


def test_an_ai_coding_node_reports_shell_claude_and_codex(monkeypatch):
    """After the installer runs and the CLIs exist, the heartbeat carries
    all three -- which is what makes Claude/Codex selectable for that node
    in Create Session."""
    installed = {"claude": r"C:\Program Files\nodejs\claude.cmd",
                 "codex": r"C:\Program Files\nodejs\codex.cmd"}
    monkeypatch.setattr("terminal_mcp.launcher_resolution.shutil.which",
                        lambda name, path=None: installed.get(name))

    assert available_agent_types((("claude", "claude"), ("codex", "codex"))) \
        == ("shell", "claude", "codex")


def test_a_launcher_named_but_not_installed_is_not_reported(monkeypatch):
    """Configuring a launcher must never be enough on its own: that would
    schedule work onto a node that fails at launch time instead."""
    monkeypatch.setattr("terminal_mcp.launcher_resolution.shutil.which",
                        lambda name, path=None: r"C:\x\claude.cmd" if name == "claude" else None)

    assert available_agent_types((("claude", "claude"), ("codex", "codex"))) \
        == ("shell", "claude")


def test_the_create_session_gate_matches_what_the_heartbeat_reports():
    """The dashboard's own rule, asserted against the page it ships: shell
    needs nothing, anything else must appear in that node's agent_types.
    An empty list is therefore shell-only -- which is why dell5420 offered
    nothing while it could not heartbeat."""
    from terminal_mcp.dashboard import SESSIONS_ADMIN_HTML as page
    assert "return agentType === 'shell' || (node.agent_types || []).includes(agentType);" in page
