"""The public Add Node flow: what origin a MACHINE is told to call back to.

The bug these pin down is specific. On the public deployment the operator
reaches the Dashboard at an Access-gated hostname, and every machine-side
URL was derived from the Host header of that browser request. So the
enrollment code, the paired helper file name and the terminalmcp:// handle
all pointed a brand-new Windows machine at a hostname it can never
authenticate to. The failure is not a connection error the installer can
fall through -- Cloudflare Access answers with a perfectly successful
redirect to a login page, which is not bootstrap config.

nodes.onboarding.bootstrap_origin pins the machine-facing origin and
suppresses the browser Host entirely. The operator's Dashboard is
deliberately unaffected: it keeps its own hostname and its Access guard.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import helper_artifact as ha
from terminal_mcp.config import (
    AppConfig,
    DashboardConfig,
    InputPolicyConfig,
    NodesConfig,
    OnboardingConfig,
    PermissionsConfig,
    SessionLifecycleConfig,
    _load_onboarding_config,
)
from terminal_mcp.connection_store import ConnectionStore
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.enrollment import EnrollmentStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_onboarding import OnboardingService
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.node_transport import TransportStore

BOOTSTRAP = "https://terminal-bootstrap.mesflow.net"
DASHBOARD_HOST = "terminal-dashboard.mesflow.net"
BODY = b"MZ" + b"\x00" * 4094


def _config(tmp_path, *, bootstrap_origin: str = "") -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
        nodes=NodesConfig(onboarding=OnboardingConfig(bootstrap_origin=bootstrap_origin)),
    )


def _client(tmp_path, *, bootstrap_origin: str = "") -> TestClient:
    service = TerminalService(_config(tmp_path, bootstrap_origin=bootstrap_origin))
    server = build_mcp(service)
    register_dashboard(server, service)
    # Every request below arrives as the operator's browser does on the
    # public deployment: on the Access-gated Dashboard hostname.
    return TestClient(server.streamable_http_app(),
                      headers={"Origin": f"https://{DASHBOARD_HOST}", "Host": DASHBOARD_HOST})


def _onboarding(tmp_path, *, bootstrap_origin: str = "") -> OnboardingService:
    """The URL-choosing logic on its own, with no route in the way."""
    config = _config(tmp_path, bootstrap_origin=bootstrap_origin)
    terminal = TerminalService(config)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(terminal),
                                   local_workspace_root=str(tmp_path))
    return OnboardingService(
        config, controller=controller,
        connection_store=ConnectionStore(tmp_path / "connections.db"),
        enrollment_store=EnrollmentStore(tmp_path / "enrollment.db"),
        transport_store=TransportStore(tmp_path / "transports.db"),
        port_allocator=None, audit=terminal.audit,
        tailscale_detector=lambda: {"available": False, "ip": None,
                                    "hostname": None, "reason": "not_installed"},
    )


# ---------------------------------------------------------------------------
# controller_url: the public origin, and the Host that must not win
# ---------------------------------------------------------------------------

def test_the_pinned_origin_is_what_a_machine_is_told_to_call(tmp_path):
    onboarding = _onboarding(tmp_path, bootstrap_origin=BOOTSTRAP)

    assert onboarding.controller_url(request_base_url=f"https://{DASHBOARD_HOST}") == BOOTSTRAP
    assert onboarding.bootstrap_origin == BOOTSTRAP


def test_the_browser_host_is_not_a_candidate_at_all_when_pinned(tmp_path):
    """Not merely outranked -- removed. An Access-gated hostname left in the
    candidate list is one the installer will eventually try, and it answers
    with a login page rather than a connection error, so the installer
    cannot tell it apart from a working controller."""
    onboarding = _onboarding(tmp_path, bootstrap_origin=BOOTSTRAP)

    candidates = onboarding.controller_urls(request_base_url=f"https://{DASHBOARD_HOST}")

    assert candidates[0] == BOOTSTRAP
    assert not any(DASHBOARD_HOST in candidate for candidate in candidates), candidates


def test_the_lan_deployment_is_completely_unchanged(tmp_path):
    """With nothing pinned, the browser Host is still the second candidate:
    on a LAN the operator and the new machine really are on one network, and
    that guess is a good one. This change must not cost that."""
    onboarding = _onboarding(tmp_path)

    candidates = onboarding.controller_urls(request_base_url="http://192.168.1.5:8766")

    assert candidates[0] == "http://192.168.1.5:8766"
    assert onboarding.bootstrap_origin == ""


def test_the_generated_setup_script_carries_the_public_origin(tmp_path):
    """End to end through the route the Add Node form actually calls, with
    the request arriving on the Dashboard hostname."""
    client = _client(tmp_path, bootstrap_origin=BOOTSTRAP)

    response = client.post("/dashboard/api/nodes/onboard/enrollments",
                           json={"node_id": "win-work", "os": "windows",
                                 "profile": "minimal", "connectivity": {}})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["controller_url"] == BOOTSTRAP
    assert BOOTSTRAP in payload["quick_install_command"]
    assert DASHBOARD_HOST not in payload["quick_install_command"]
    assert DASHBOARD_HOST not in payload["script"]


def test_the_profiles_route_reports_the_public_origin_too(tmp_path):
    """It is what the form renders before anything is generated, so a
    mismatch here is a mismatch the operator sees first."""
    client = _client(tmp_path, bootstrap_origin=BOOTSTRAP)

    response = client.get("/dashboard/api/nodes/onboard/profiles")

    assert response.status_code == 200, response.text
    assert response.json()["controller_url"] == BOOTSTRAP


# ---------------------------------------------------------------------------
# the helper: paired file name and handle URL
# ---------------------------------------------------------------------------

@pytest.fixture
def published(tmp_path, monkeypatch):
    root = tmp_path / "helper"
    monkeypatch.setenv("TERMINAL_MCP_HELPER_ARTIFACT_DIR", str(root))
    source = tmp_path / "src.exe"
    source.write_bytes(BODY)
    ha.publish(source, target="windows-x64", version="0.1.0-dev",
               build_sha="abc1234", signed=False, root=root)
    return root


def _enroll(client) -> str:
    response = client.post("/dashboard/api/nodes/onboard/enrollments",
                           json={"node_id": "win-work", "os": "windows",
                                 "profile": "minimal", "connectivity": {}})
    assert response.status_code == 200, response.text
    return response.json()["enrollment"]["id"]


def test_the_paired_helper_filename_names_the_public_origin(tmp_path, published):
    """The file name is what the helper reads on a double-click, so the
    origin encoded in it is the one the machine dials. Naming the
    Access-gated Dashboard host here ships a helper that can redeem
    nothing."""
    client = _client(tmp_path, bootstrap_origin=BOOTSTRAP)
    enrollment_id = _enroll(client)
    issued = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{enrollment_id}/handle", json={})
    assert issued.status_code == 200, issued.text
    handle = issued.json()["handle"]

    response = client.post("/dashboard/api/nodes/onboard/helper/windows-x64",
                           json={"session": handle})

    assert response.status_code == 200, response.text
    disposition = response.headers["Content-Disposition"]
    assert "terminal-bootstrap.mesflow.net" in disposition, disposition
    assert DASHBOARD_HOST not in disposition, disposition
    # https is the default and is encoded by its ABSENCE of the http- prefix.
    assert "http-terminal-bootstrap" not in disposition


def test_the_handle_url_handed_to_the_helper_carries_the_public_origin(tmp_path):
    client = _client(tmp_path, bootstrap_origin=BOOTSTRAP)
    enrollment_id = _enroll(client)

    issued = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{enrollment_id}/handle", json={})

    assert issued.status_code == 200, issued.text
    payload = issued.json()
    assert "terminal-bootstrap.mesflow.net" in payload["url"], payload["url"]
    assert DASHBOARD_HOST not in payload["url"]
    assert "terminal-bootstrap.mesflow.net" in payload["controller"]


def test_the_helper_download_itself_stays_on_the_dashboard_hostname(tmp_path, published):
    """The ORIGIN written into the file name changes; where the binary is
    fetched from does not. It is still an operator-facing download behind
    the Dashboard's own guard -- the bootstrap hostname does not serve it,
    which is what deploy/cloudflare/bootstrap-ingress.template.yml encodes."""
    client = _client(tmp_path, bootstrap_origin=BOOTSTRAP)

    assert client.get("/dashboard/api/nodes/onboard/helper/windows-x64").status_code == 200

    text = (Path(__file__).resolve().parent.parent / "deploy" / "cloudflare"
            / "bootstrap-ingress.template.yml").read_text(encoding="utf-8")
    assert "onboard/helper" not in text.split("TO APPLY")[0].replace(
        "# The helper download is NOT on this hostname", "")


# ---------------------------------------------------------------------------
# the setting itself
# ---------------------------------------------------------------------------

def _load(value) -> OnboardingConfig:
    return _load_onboarding_config({"bootstrap_origin": value})


def test_the_origin_setting_accepts_a_plain_origin():
    assert _load(BOOTSTRAP).bootstrap_origin == BOOTSTRAP


def test_a_trailing_slash_is_normalised_away():
    """Otherwise every machine-side URL is built with a doubled slash."""
    assert _load(BOOTSTRAP + "/").bootstrap_origin == BOOTSTRAP


@pytest.mark.parametrize("bad", [
    "terminal-bootstrap.mesflow.net",                  # no scheme
    "ftp://terminal-bootstrap.mesflow.net",            # not http(s)
    "https://",                                        # no host
    "https://terminal-bootstrap.mesflow.net/enroll",   # a path, not an origin
    "https://terminal-bootstrap.mesflow.net?x=1",      # a query
    "https://terminal-bootstrap.mesflow.net#f",        # a fragment
])
def test_anything_that_is_not_an_origin_is_refused(bad):
    """A path here would be concatenated onto every machine-side route and
    produce 404s that read like the controller is down."""
    with pytest.raises(ValueError, match="bootstrap_origin"):
        _load(bad)


def test_the_default_is_empty_so_nothing_changes_until_it_is_set():
    assert OnboardingConfig().bootstrap_origin == ""
    assert _load_onboarding_config({}).bootstrap_origin == ""
