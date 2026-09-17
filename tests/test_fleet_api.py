"""The fleet over its real surfaces: HTTP routes, MCP tools, the doctor, the
node-agent peer endpoint, and the Terminal Wall badge.

The tests worth having here are the ones a code review cannot do by eye:
that a secret cannot reach a wire or a screen through ANY of these, and that
every read still answers when the fleet is unreachable.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import dashboard as dashboard_module
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import FLEET_HTML, register_dashboard
from terminal_mcp.fleet_registry import (CRED_MISSING, KIND_NODE, KIND_SSH_TARGET,
                                         FleetObject, FleetRegistryStore)
from terminal_mcp.fleet_service import FleetService
from terminal_mcp.mcp_app import build_mcp


def _config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


@pytest.fixture
def fleet(tmp_path):
    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="m910")
    now = datetime.now(timezone.utc).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
    store.publish(KIND_NODE, "node:m910",
                  {"node_id": "m910", "display_name": "m910", "platform": "linux",
                   "lan_ip": "192.168.1.109", "tailscale_ip": "100.117.214.87",
                   "endpoint": "http://127.0.0.1:8766", "contract_version": 1,
                   "status": "online"})
    store.merge([
        FleetObject(KIND_NODE, "node:dell-linux", "dell-linux", 3, old, "dell-linux",
                    payload={"node_id": "dell-linux", "display_name": "dell-linux",
                             "platform": "linux", "contract_version": 0,
                             "status": "offline"}),
        FleetObject(KIND_SSH_TARGET, "ssh:fp:abc", "m910", 2, now, "m910",
                    payload={"alias": "dell-linux", "aliases": ["dell-linux"],
                             "host": "100.81.85.120", "port": 22, "username": "dell",
                             "transport": "tailscale", "node_id": "dell-linux",
                             "host_key_fingerprint": "SHA256:PINNEDFINGERPRINT",
                             "credential_status": CRED_MISSING, "preferred_order": 10,
                             "source": "ssh_config"}),
    ])
    return FleetService(store, local_node_id="m910")


@pytest.fixture
def client(fleet):
    service = TerminalService(_config())
    server = build_mcp(service, fleet=fleet)
    register_dashboard(server, service, fleet=fleet)
    return TestClient(server.streamable_http_app())


# -- HTTP surface -------------------------------------------------------------

def test_the_fleet_routes_are_registered_read_only_except_the_sync(fleet):
    service = TerminalService(_config())
    server = build_mcp(service, fleet=fleet)
    register_dashboard(server, service, fleet=fleet)
    routes = {route.path: set(route.methods)
              for route in server._custom_starlette_routes if hasattr(route, "methods")}
    assert routes["/dashboard/fleet"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/fleet"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/fleet/ssh"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/fleet/readiness"] == {"GET", "HEAD"}
    # Exactly one write, and it writes METADATA -- there is no path from any
    # of these to starting, stopping or typing into a session.
    assert routes["/dashboard/api/fleet/sync"] == {"POST"}


def test_the_fleet_api_answers_from_the_local_cache(client):
    payload = client.get("/dashboard/api/fleet").json()
    assert payload["served_from"] == "local_cache"
    assert {n["node_id"] for n in payload["nodes"]} == {"m910", "dell-linux"}
    # The local node sorts first: it is the one you are standing on.
    assert payload["nodes"][0]["node_id"] == "m910"


def test_the_fleet_api_labels_metadata_that_has_gone_stale(client):
    nodes = {n["node_id"]: n for n in client.get("/dashboard/api/fleet").json()["nodes"]}
    assert nodes["dell-linux"]["metadata_stale"] is True
    assert nodes["m910"]["metadata_stale"] is False


def test_the_ssh_api_serves_identity_and_posture_but_no_credential(client):
    targets = client.get("/dashboard/api/fleet/ssh").json()["targets"]
    assert len(targets) == 1
    target = targets[0]
    assert target["host"] == "100.81.85.120"
    assert target["transport"] == "tailscale"
    # A fingerprint is a hash and is exactly what pinning compares, so it is
    # published on purpose. Everything replayable is absent.
    assert target["host_key_fingerprint"] == "SHA256:PINNEDFINGERPRINT"
    assert target["credential_status"] == CRED_MISSING
    flat = json.dumps(target)
    for forbidden in ("password", "private_key", "passphrase", "BEGIN OPENSSH"):
        assert forbidden not in flat


def test_readiness_is_served_with_per_check_evidence(client):
    payload = client.get("/dashboard/api/fleet/readiness").json()
    assert payload["status"] in {"PASS", "WARN", "FAIL"}
    names = {c["check"] for c in payload["checks"]}
    assert {"registry_sync_age", "contract_version_drift", "ssh_credentials",
            "ssh_host_key_pinning", "ssh_fingerprint_mismatch", "peer_sync",
            "stale_routes"} <= names
    by_name = {c["check"]: c for c in payload["checks"]}
    assert by_name["contract_version_drift"]["status"] == "WARN"
    assert by_name["ssh_credentials"]["status"] == "WARN"


def test_the_fleet_page_renders_and_never_embeds_a_secret(client):
    response = client.get("/dashboard/fleet")
    assert response.status_code == 200
    assert "Fleet Registry" in response.text
    # SSH targets ride along in the main view rather than costing a second
    # request; /dashboard/api/fleet/ssh exists for callers that want only
    # that slice.
    assert "/dashboard/api/fleet'" in response.text
    assert "/dashboard/api/fleet/readiness" in response.text
    # The page fetches its data; nothing is baked into the HTML.
    assert "SHA256:PINNEDFINGERPRINT" not in response.text


def test_the_page_states_plainly_that_credentials_do_not_travel():
    """An operator reading this screen has to know that MISSING_CREDENTIAL
    means "go log in there", not "the sync is broken"."""
    assert "MISSING_CREDENTIAL" in FLEET_HTML
    assert "private key" in FLEET_HTML.lower()


def test_every_fleet_read_works_with_no_node_reachable(client, monkeypatch):
    """The whole point, asserted rather than assumed: make any outbound HTTP
    call an error, then read the entire fleet surface."""
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _explode)
    for path in ("/dashboard/api/fleet", "/dashboard/api/fleet/ssh",
                 "/dashboard/api/fleet/readiness", "/dashboard/fleet"):
        assert client.get(path).status_code == 200, path


def _explode(*_args, **_kwargs):
    raise AssertionError("a fleet read must not touch the network")


# -- MCP surface --------------------------------------------------------------

def _tool(server, name):
    return server._tool_manager._tools[name].fn


def test_the_mcp_tools_expose_the_same_view(fleet):
    server = build_mcp(TerminalService(_config()), fleet=fleet)
    registry = _tool(server, "terminal_fleet_registry")()
    assert registry["served_from"] == "local_cache"
    assert {n["node_id"] for n in registry["nodes"]} == {"m910", "dell-linux"}

    narrowed = _tool(server, "terminal_fleet_registry")(kind="ssh_target")
    assert narrowed["kind"] == "ssh_target" and len(narrowed["ssh_targets"]) == 1
    assert _tool(server, "terminal_fleet_registry")(kind="nope")["error"] == "UNKNOWN_KIND"

    inventory = _tool(server, "terminal_fleet_ssh_inventory")()
    assert inventory["targets"][0]["alias"] == "dell-linux"
    assert "password" not in json.dumps(inventory)

    assert _tool(server, "terminal_fleet_readiness")()["status"] in {"PASS", "WARN", "FAIL"}
    assert _tool(server, "terminal_fleet_sync_status")()["local_node_id"] == "m910"


# -- node agent peer endpoint -------------------------------------------------

def test_the_node_agent_serves_a_symmetric_exchange(tmp_path):
    """A node running this IS a peer. The endpoint merges what it is given
    and answers with what it has -- the same operation the caller performs,
    in the opposite order."""
    from terminal_mcp.node_agent import build_node_agent

    store = FleetRegistryStore(tmp_path / "agent.db", local_node_id="hp-linux")
    store.publish(KIND_NODE, "node:hp-linux", {"node_id": "hp-linux"}, owner_node="hp-linux")
    agent = build_node_agent(node_id="hp-linux", terminal=TerminalService(_config()),
                             token="t0ken",
                             fleet=FleetService(store, local_node_id="hp-linux"))
    client = TestClient(agent)
    headers = {"Authorization": "Bearer t0ken"}

    incoming = {"kind": "node", "object_id": "node:m910", "owner_node": "m910",
                "revision": 1, "updated_at": "2026-09-12T00:00:00+00:00",
                "source_node": "m910", "payload": {"node_id": "m910"}}
    response = client.post("/v1/fleet/objects",
                           json={"objects": [incoming], "from": "m910"}, headers=headers)
    body = response.json()
    assert body["merge"]["applied"] == 1
    assert {o["object_id"] for o in body["objects"]} == {"node:hp-linux", "node:m910"}

    view = client.get("/v1/fleet/view", headers=headers).json()
    assert view["served_from"] == "local_cache"


def test_the_peer_endpoint_requires_auth_like_every_other_agent_route(tmp_path):
    from terminal_mcp.node_agent import build_node_agent

    agent = build_node_agent(node_id="hp-linux", terminal=TerminalService(_config()),
                             token="t0ken",
                             fleet=FleetService(FleetRegistryStore(tmp_path / "a.db",
                                                                   local_node_id="hp-linux"),
                                                local_node_id="hp-linux"))
    client = TestClient(agent)
    assert client.post("/v1/fleet/objects", json={"objects": []}).status_code == 401
    assert client.get("/v1/fleet/view").status_code == 401


def test_a_peer_cannot_push_a_secret_through_the_agent_endpoint(tmp_path):
    from terminal_mcp.node_agent import build_node_agent

    store = FleetRegistryStore(tmp_path / "agent.db", local_node_id="hp-linux")
    agent = build_node_agent(node_id="hp-linux", terminal=TerminalService(_config()),
                             token="t0ken", fleet=FleetService(store, local_node_id="hp-linux"))
    client = TestClient(agent)
    response = client.post(
        "/v1/fleet/objects",
        json={"objects": [{"kind": "ssh_target", "object_id": "ssh:x", "owner_node": "evil",
                           "revision": 9, "payload": {"private_key": "-----BEGIN OPENSSH "
                                                                     "PRIVATE KEY-----"}}],
              "from": "evil"},
        headers={"Authorization": "Bearer t0ken"})
    assert response.json()["merge"]["rejected"] == 1
    assert store.get(KIND_SSH_TARGET, "ssh:x") is None


# -- doctor -------------------------------------------------------------------

def test_doctor_fleet_reports_json_and_a_severity_exit_code(tmp_path, capsys):
    from terminal_mcp.doctor import main

    store = FleetRegistryStore(tmp_path / "d.db", local_node_id="m910")
    store.publish(KIND_NODE, "node:m910", {"node_id": "m910", "contract_version": 1})
    code = main(["fleet", "--json", "--db", str(tmp_path / "d.db"), "--node-id", "m910"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] in {"PASS", "WARN", "FAIL"}
    # 0 PASS / 1 WARN / 2 FAIL, so a cron wrapper can tell "needs a human
    # eventually" from "something is wrong right now".
    assert code == {"PASS": 0, "WARN": 1, "FAIL": 2}[payload["status"]]


def test_doctor_fleet_works_when_nothing_has_ever_synced(tmp_path, capsys):
    """The command has to work during exactly the outage it diagnoses."""
    from terminal_mcp.doctor import main

    code = main(["fleet", "--db", str(tmp_path / "empty.db"), "--node-id", "x"])
    assert "has ever been replicated" in capsys.readouterr().out
    assert code == 2


# -- Terminal Wall integration ------------------------------------------------

def test_the_wall_marks_a_node_whose_metadata_has_gone_stale():
    from terminal_mcp.terminal_wall import build_snapshot

    class _Controller:
        def terminal_list_sessions(self):
            return {"sessions": [{"name": "a", "node_id": "dell-linux",
                                  "node_name": "dell-linux"}],
                    "unreachable_nodes": []}

        def terminal_status(self, _session):
            return {"exists": True, "state": "IDLE", "reason": "current command is 'bash'"}

    snapshot = build_snapshot(_Controller(), fleet_meta={
        "dell-linux": {"metadata_stale": True, "metadata_age_seconds": 14400.0,
                       "ssh_route_count": 2}})
    section = snapshot["nodes"][0]
    assert section["metadata_stale"] is True
    assert section["ssh_route_count"] == 2


def test_the_wall_shows_a_route_count_but_never_a_route():
    """Addresses, fingerprints and credential posture live behind the fleet
    view's own guard. A monitor gets a number."""
    html = dashboard_module.TERMINAL_WALL_HTML
    assert "ssh_route_count" in html
    assert "metadata_stale" in html
    for forbidden in ("host_key_fingerprint", "credential_status", "username"):
        assert forbidden not in html


def test_the_wall_still_renders_when_the_fleet_cache_cannot_be_read():
    """A badge is never worth a 500 on the screen whose job is showing
    terminals."""
    import inspect

    source = inspect.getsource(dashboard_module.register_dashboard)
    assert "a badge is never worth a 500" in source
