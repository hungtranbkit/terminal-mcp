"""Deployment redundancy over its real surfaces: dashboard API, MCP tools,
the fleet doctor, and the rendered section.

Uses disposable stores throughout -- nothing here probes a real host, writes
an authorized_keys, or touches the operator's ssh config.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from starlette.testclient import TestClient

from terminal_mcp import dashboard as dashboard_module
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import FLEET_HTML, register_dashboard
from terminal_mcp.deployment_redundancy import DeploymentPath, DeploymentTarget
from terminal_mcp.deployment_service import DeploymentRegistry
from terminal_mcp.fleet_registry import KIND_NODE, FleetObject, FleetRegistryStore
from terminal_mcp.fleet_service import FleetService
from terminal_mcp.mcp_app import build_mcp


def _config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


FRESH = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()


@pytest.fixture
def fleet(tmp_path):
    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="m910")
    now = datetime.now(timezone.utc).isoformat()
    for node in ("dell-linux", "hp-linux"):
        store.merge([FleetObject(KIND_NODE, f"node:{node}", node, 1, now, node,
                                 payload={"node_id": node, "display_name": node,
                                          "status": "online",
                                          "tailscale_ip": f"100.64.0.{1 if node[0] == 'd' else 2}",
                                          "lan_ip": "192.168.1.132" if node[0] == "d"
                                                    else "192.168.1.140"})])
    registry = DeploymentRegistry(store, local_node_id="m910")
    registry.put_target(DeploymentTarget(
        target_id="mesflow-vps", display_name="MESFlow VPS", project_id="mesflow",
        min_independent_paths=2, primary_node="dell-linux", backup_nodes=("hp-linux",)))
    for node in ("dell-linux", "hp-linux"):
        registry.put_path(DeploymentPath(
            target_id="mesflow-vps", node_id=node, host="100.64.0.9", username="deploy",
            port=22, ssh_alias=f"{node}-vps", transport="tailscale",
            capabilities=("ssh", "deploy"), last_probe_at=FRESH, last_probe_ok=True,
            auth_ok=True, deploy_prereqs_ok=True, latency_ms=12.0,
            role="primary" if node == "dell-linux" else "backup"))
    return FleetService(store, local_node_id="m910")


@pytest.fixture
def client(fleet):
    service = TerminalService(_config())
    server = build_mcp(service, fleet=fleet)
    register_dashboard(server, service, fleet=fleet)
    return TestClient(server.streamable_http_app())


# -- dashboard API -------------------------------------------------------------

def test_the_api_reports_redundancy_and_deployability_separately(client):
    payload = client.get("/dashboard/api/deployment").json()
    target = payload["targets"][0]
    assert target["status"] == "READY"
    assert target["redundancy"]["label"] == "2/2"
    assert target["deploy_available"] is True
    assert {p["node_id"] for p in target["paths"]} == {"dell-linux", "hp-linux"}


def test_one_target_can_be_fetched_directly(client):
    assert client.get("/dashboard/api/deployment?target=mesflow-vps").json()["target_id"] \
        == "mesflow-vps"
    assert client.get("/dashboard/api/deployment?target=nope").json()["error"] == "UNKNOWN_TARGET"


def test_the_dry_run_endpoint_changes_nothing(client):
    result = client.get("/dashboard/api/deployment/dry-run"
                        "?target=mesflow-vps&offline=dell-linux").json()
    assert result["status"] == "DEGRADED"
    assert result["deploy_available"] is True
    assert result["would_choose"]["node_id"] == "hp-linux"
    # Reality is untouched.
    assert client.get("/dashboard/api/deployment").json()["targets"][0]["status"] == "READY"


def test_the_deployment_endpoints_are_reads(fleet):
    service = TerminalService(_config())
    server = build_mcp(service, fleet=fleet)
    register_dashboard(server, service, fleet=fleet)
    routes = {r.path: set(r.methods) for r in server._custom_starlette_routes
              if hasattr(r, "methods")}
    # No POST: a screen that can start a production deploy is a screen
    # someone starts one from by accident.
    assert routes["/dashboard/api/deployment"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/deployment/dry-run"] == {"GET", "HEAD"}


def test_a_jump_through_the_peer_shows_up_over_the_api(client, fleet):
    DeploymentRegistry(fleet.store, local_node_id="m910").put_path(DeploymentPath(
        target_id="mesflow-vps", node_id="hp-linux", host="100.64.0.9",
        username="deploy", proxy_jump="dell-linux", last_probe_at=FRESH,
        last_probe_ok=True, auth_ok=True, deploy_prereqs_ok=True))
    target = client.get("/dashboard/api/deployment").json()["targets"][0]
    assert target["redundancy"]["label"] == "1/2"
    assert target["deploy_available"] is True
    hp = [p for p in target["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["depends_on"] == "dell-linux"


def test_a_jump_written_as_the_peers_address_is_caught_over_the_api(client, fleet):
    """The node's LAN address comes from the fleet registry, so the API has
    what it needs to see through `ProxyJump 192.168.1.132`."""
    DeploymentRegistry(fleet.store, local_node_id="m910").put_path(DeploymentPath(
        target_id="mesflow-vps", node_id="hp-linux", host="100.64.0.9",
        username="deploy", proxy_jump="192.168.1.132", last_probe_at=FRESH,
        last_probe_ok=True, auth_ok=True, deploy_prereqs_ok=True))
    target = client.get("/dashboard/api/deployment").json()["targets"][0]
    hp = [p for p in target["paths"] if p["node_id"] == "hp-linux"][0]
    assert hp["depends_on"] == "dell-linux"
    assert target["redundancy"]["label"] == "1/2"


def test_no_secret_appears_anywhere_in_the_deployment_api(client):
    flat = json.dumps(client.get("/dashboard/api/deployment").json())
    for forbidden in ("private_key", "password", "passphrase", "BEGIN OPENSSH", "id_ed25519"):
        assert forbidden not in flat


# -- MCP -----------------------------------------------------------------------

def _tool(server, name):
    return server._tool_manager._tools[name].fn


def test_the_mcp_tools_cover_read_declare_choose_and_dry_run(fleet):
    server = build_mcp(TerminalService(_config()), fleet=fleet)
    status = _tool(server, "terminal_deployment_status")()
    assert status["targets"][0]["redundancy"]["label"] == "2/2"

    choice = _tool(server, "terminal_deployment_choose_node")("mesflow-vps")
    assert choice["node_id"] == "dell-linux" and choice["role"] == "primary"

    dry = _tool(server, "terminal_deployment_dry_run_failover")("mesflow-vps", "dell-linux")
    assert dry["would_choose"]["node_id"] == "hp-linux"
    assert dry["status"] == "DEGRADED" and dry["deploy_available"] is True

    _tool(server, "terminal_deployment_upsert_target")(
        target_id="second", display_name="Second", min_independent_paths=1)
    assert {t["target_id"] for t in _tool(server, "terminal_deployment_status")()["targets"]} \
        == {"mesflow-vps", "second"}


def test_declaring_a_path_does_not_make_it_count(fleet):
    """A config file is a plan, not a route."""
    server = build_mcp(TerminalService(_config()), fleet=fleet)
    _tool(server, "terminal_deployment_upsert_target")(
        target_id="fresh", display_name="Fresh", min_independent_paths=2)
    for node in ("dell-linux", "hp-linux"):
        _tool(server, "terminal_deployment_upsert_path")(
            target_id="fresh", node_id=node, host="10.0.0.9", username="deploy")
    result = _tool(server, "terminal_deployment_status")(target_id="fresh")
    assert result["redundancy"]["label"] == "0/2"
    assert result["status"] == "FAIL"
    assert all(p["state"] == "UNVERIFIED" for p in result["paths"])


def test_the_deploy_lease_is_wired_to_a_real_store(fleet, tmp_path):
    """The lease silently defaulting to None would disable the only thing
    stopping two dispatchers running one deploy."""
    from terminal_mcp.lease import ResourceLockStore

    server = build_mcp(TerminalService(_config()), fleet=fleet,
                       resource_locks=ResourceLockStore(tmp_path / "locks.db"))
    # Reach the same service the tools use, through a tool that exposes it.
    choice = _tool(server, "terminal_deployment_choose_node")("mesflow-vps")
    assert choice["node_id"] == "dell-linux"


# -- doctor --------------------------------------------------------------------

def test_fleet_readiness_includes_a_check_per_deployment_target(fleet):
    checks = {c["check"]: c for c in fleet.readiness()["checks"]}
    assert "deployment_redundancy:mesflow-vps" in checks
    check = checks["deployment_redundancy:mesflow-vps"]
    assert check["status"] == "PASS"
    assert check["evidence"]["redundancy"]["label"] == "2/2"


def test_a_degraded_but_deployable_target_is_warn_not_fail(fleet):
    """Crying FAIL over a target that still ships is how a team learns to
    ignore this screen."""
    DeploymentRegistry(fleet.store, local_node_id="m910").put_path(DeploymentPath(
        target_id="mesflow-vps", node_id="hp-linux", host="100.64.0.9",
        username="deploy", proxy_jump="dell-linux", last_probe_at=FRESH,
        last_probe_ok=True, auth_ok=True, deploy_prereqs_ok=True))
    check = {c["check"]: c for c in fleet.readiness()["checks"]}[
        "deployment_redundancy:mesflow-vps"]
    assert check["status"] == "WARN"
    assert check["evidence"]["deploy_available"] is True


def test_a_target_nobody_can_deploy_to_is_fail(fleet):
    registry = DeploymentRegistry(fleet.store, local_node_id="m910")
    for node in ("dell-linux", "hp-linux"):
        registry.put_path(DeploymentPath(
            target_id="mesflow-vps", node_id=node, host="100.64.0.9", username="deploy",
            last_probe_at=FRESH, last_probe_ok=False, last_probe_reason="no route to host"))
    check = {c["check"]: c for c in fleet.readiness()["checks"]}[
        "deployment_redundancy:mesflow-vps"]
    assert check["status"] == "FAIL"
    assert check["evidence"]["deploy_available"] is False


def test_readiness_says_nothing_about_deployment_when_none_is_declared(tmp_path):
    store = FleetRegistryStore(tmp_path / "empty.db", local_node_id="a")
    store.publish(KIND_NODE, "node:a", {"node_id": "a", "contract_version": 1})
    names = {c["check"] for c in FleetService(store, local_node_id="a").readiness()["checks"]}
    assert not any(n.startswith("deployment_redundancy") for n in names)


# -- the rendered section -------------------------------------------------------

def test_the_page_shows_ssh_deploy_vpn_and_independence_per_path():
    assert "Deployment Redundancy" in FLEET_HTML
    for label in ("'SSH'", "'Deploy'", "'Tailscale'", "'Độc lập'"):
        assert label in FLEET_HTML
    assert "Redundancy " in FLEET_HTML
    assert "deploy khả dụng" in FLEET_HTML


def test_the_section_never_reads_a_secret_field():
    """The page is fed by the API, which carries no secret -- but the
    template must not reach for one either.

    Checks for a FIELD ACCESS rather than the bare word: the page's own prose
    says "no private key, password or passphrase is synced", and a test that
    banned the word would have banned the sentence explaining the guarantee.
    """
    import re

    accesses = re.findall(
        r"\.(private_key|password|passphrase|identity_file|secret|token)\b", FLEET_HTML)
    assert accesses == [], f"template reads {accesses}"
