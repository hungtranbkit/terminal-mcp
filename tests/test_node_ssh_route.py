"""Operator-only routes reuse the authenticated node's existing SSH identity."""
import json
import re
import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest

from terminal_mcp.config import ManagedSSHRouteConfig
from terminal_mcp.node_ssh import NodeSSHService


@pytest.fixture
def route_service(tmp_path, monkeypatch):
    public = "ssh-ed25519 dGVzdA=="
    fingerprint = NodeSSHService._fingerprint(public)
    route = ManagedSSHRouteConfig(address="192.168.100.97", port=22,
        registered_endpoint="http://100.81.85.120:8790", username="dell",
        hostname="dell-Latitude-5511", host_key_fingerprint=fingerprint)
    preflight = dict(status="ready", node_id="dell-linux", username="dell",
                     hostname=route.hostname, port=22, host_public_key=public)
    calls = []
    def manage(action, *args):
        calls.append(action)
        assert action == "status", "override must never install keys or rewrite trust"
        return dict(preflight)
    node = SimpleNamespace(id="dell-linux", endpoint=route.registered_endpoint)
    client = SimpleNamespace(ssh_manage=manage, base_url=route.registered_endpoint)
    controller = SimpleNamespace(registry=SimpleNamespace(get=lambda _: node),
                                 client_for=lambda _: client)
    service = NodeSSHService(controller, tmp_path, routes={"dell-linux": route})
    key, known, profile = service._paths("dell-linux")
    key.write_text("fixture-private-key")
    known.write_text(f"100.81.85.120 {public}\n")
    profile.write_text(json.dumps(dict(node_id="dell-linux", address="100.81.85.120",
        username="dell", port=22, expected_hostname=route.hostname,
        host_key_fingerprint=fingerprint, status="route_collision", verified=False)))
    banners, commands = [], []
    def banner(host, port):
        banners.append((host, port))
        return None if host == route.address else dict(status="route_collision", error="route_collision")
    monkeypatch.setattr(service, "_registered_route_diagnostic", banner)
    def run(argv, **kwargs):
        commands.append(argv)
        nonce = re.search(r"'([0-9a-f]{32})'", argv[-1])
        return subprocess.CompletedProcess(argv, 0,
            stdout=f"{nonce.group(1)}\ndell\n{route.hostname}\n" if nonce else "canary\n", stderr="")
    monkeypatch.setattr("terminal_mcp.node_ssh.subprocess.run", run)
    return service, route, preflight, node, client, key, known, profile, banners, commands


def test_explicit_lan_route_is_used_by_every_operation_without_changing_pin(route_service):
    service, route, _, node, _, key, known, profile, banners, commands = route_service
    before = key.read_bytes(), known.read_bytes()
    assert service.status("dell-linux")["status"] == "configured"
    assert service.setup("dell-linux")["status"] == "configured"
    assert service.verify("dell-linux")["status"] == "verified"
    assert service.status("dell-linux")["address"] == route.address
    assert service.connect("dell-linux")["status"] == "verified"
    assert service.exec("dell-linux", ["hostname"])["exit_code"] == 0
    commands.append(service.connect_argv("dell-linux"))
    assert banners and set(banners) == {(route.address, 22)}
    for argv in commands:
        assert argv[-2] == "dell@192.168.100.97"
        for option in ("HostKeyAlias=100.81.85.120", "UpdateHostKeys=no", "StrictHostKeyChecking=yes",
                       "IdentityAgent=none", "IdentitiesOnly=yes", "BatchMode=yes"):
            assert option in argv
        assert argv[1:3] == ["-F", "/dev/null"]
    assert before == (key.read_bytes(), known.read_bytes())
    assert json.loads(profile.read_text())["address"] == "100.81.85.120"
    assert node.endpoint == route.registered_endpoint


@pytest.mark.parametrize("mutation", ["endpoint", "client_endpoint", "node_id", "user", "hostname",
    "agent_pin", "profile_address", "profile_pin", "known_hosts", "missing_key", "agent_not_ready", "registry_id", "agent_port"])
def test_stale_or_wrong_identity_fails_closed_before_any_ssh(route_service, mutation):
    service, route, preflight, node, client, key, known, profile, banners, commands = route_service
    if mutation == "endpoint": node.endpoint = "http://100.81.85.121:8790"
    elif mutation == "client_endpoint": client.base_url = "http://100.81.85.121:8790"
    elif mutation == "registry_id": node.id = "m910"
    elif mutation == "agent_port": preflight["port"] = 2222
    elif mutation == "node_id": preflight["node_id"] = "m910"
    elif mutation == "user": preflight["username"] = "root"
    elif mutation == "hostname": preflight["hostname"] = "wrong-host"
    elif mutation == "agent_pin": preflight["host_public_key"] = "ssh-ed25519 d3Jvbmc="
    elif mutation == "known_hosts": known.write_text("100.81.85.120 ssh-ed25519 d3Jvbmc=\n")
    elif mutation == "missing_key": key.unlink()
    elif mutation == "agent_not_ready": preflight["status"] = "needs_admin"
    else:
        value = json.loads(profile.read_text())
        value["address" if mutation == "profile_address" else "host_key_fingerprint"] = "wrong"
        profile.write_text(json.dumps(value))
    for operation in (service.status, service.setup, service.verify, service.connect):
        result = operation("dell-linux")
        assert not result.get("verified") and not result.get("connect_available")
        assert result["status"] not in {"configured", "verified"}
    with pytest.raises(ValueError): service.connect_argv("dell-linux")
    assert not commands


def test_changed_route_requires_new_proof_and_never_reuses_old_verified_status(route_service):
    service, route, *_ = route_service
    assert service.verify("dell-linux")["verified"]
    service.routes["dell-linux"] = replace(route, address="192.168.100.98")
    with pytest.raises(ValueError): service.connect_argv("dell-linux")
    assert not service.status("dell-linux")["verified"]


def test_read_only_canary_does_not_modify_live_profile(route_service):
    service, _, _, _, _, key, known, profile, *_ = route_service
    before = [p.read_bytes() for p in (key, known, profile)]
    assert service.verify("dell-linux", persist=False)["verified"]
    assert before == [p.read_bytes() for p in (key, known, profile)]


@pytest.mark.parametrize("failure", ["route_collision", "route_unreachable", "route_banner_mismatch"])
def test_wrong_route_has_no_fallback(route_service, monkeypatch, failure):
    service, _, _, _, _, _, _, _, _, commands = route_service
    monkeypatch.setattr(service, "_registered_route_diagnostic",
                        lambda *_: dict(error=failure, status=failure))
    for operation in (service.status, service.setup, service.verify, service.connect):
        assert operation("dell-linux")["detail"] == failure
    assert not commands


@pytest.mark.parametrize("failure", ["nonce", "user", "hostname", "host_key", "auth"])
def test_route_proof_requires_nonce_user_hostname_and_strict_host_key(route_service, monkeypatch, failure):
    service, *_ = route_service
    def run(argv, **kwargs):
        nonce = re.search(r"'([0-9a-f]{32})'", argv[-1]).group(1)
        lines = [nonce, "dell", "dell-Latitude-5511"]
        if failure in {"nonce", "user", "hostname"}:
            lines[{"nonce": 0, "user": 1, "hostname": 2}[failure]] = "wrong"
        return subprocess.CompletedProcess(argv, 255 if failure in {"host_key", "auth"} else 0,
            stdout="\n".join(lines), stderr="Host key verification failed" if failure == "host_key" else "Permission denied")
    monkeypatch.setattr("terminal_mcp.node_ssh.subprocess.run", run)
    assert not service.verify("dell-linux")["verified"]
    with pytest.raises(ValueError): service.connect_argv("dell-linux")


def test_route_removed_cannot_reuse_its_verified_proof(route_service):
    service, *_ = route_service
    assert service.verify("dell-linux")["verified"]
    service.routes.clear()
    with pytest.raises(ValueError): service.connect_argv("dell-linux")


def test_connect_rechecks_agent_after_successful_verification(route_service):
    service, _, preflight, *_ = route_service
    assert service.verify("dell-linux")["verified"]
    preflight["username"] = "root"
    with pytest.raises(ValueError): service.connect_argv("dell-linux")
    assert service.exec("dell-linux", ["hostname"])["status"] == "route_binding_failed"


def test_explicit_port_preserves_original_known_hosts_alias(route_service, monkeypatch):
    service, route, preflight, _, _, _, known, profile, _, commands = route_service
    value = json.loads(profile.read_text())
    value["port"] = preflight["port"] = 2222
    profile.write_text(json.dumps(value))
    known.write_text("[100.81.85.120]:2222 ssh-ed25519 dGVzdA==\n")
    service.routes["dell-linux"] = replace(route, port=2200)
    monkeypatch.setattr(service, "_registered_route_diagnostic", lambda *_: None)
    assert service.verify("dell-linux")["verified"]
    assert "HostKeyAlias=[100.81.85.120]:2222" in commands[-1]
    assert commands[-1][commands[-1].index("-p") + 1] == "2200"
