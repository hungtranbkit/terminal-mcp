"""Windows node onboarding: enrollment lifecycle, script generation,
rescue port allocation, the transport resolver's primary->rescue
failover, and the dashboard routes that tie them together.

The security-shaped assertions here are the point of the file, not
decoration:

  * an enrollment code is single-use even under two SIMULTANEOUS
    consumers (real threads, real sqlite, no mocking of the store)
  * the downloadable script carries no reusable secret
  * no list/status route can return a token, a code, or a private key
  * a browser without the dashboard's own mutation guard cannot create,
    revoke, or remove anything
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import enrollment as enrollment_mod
from terminal_mcp import rescue_gateway
from terminal_mcp.config import (
    AppConfig,
    DashboardConfig,
    InputPolicyConfig,
    NodesConfig,
    OnboardingConfig,
    PermissionsConfig,
    RescueTunnelConfig,
    SessionLifecycleConfig,
    TailscaleOnboardConfig,
)
from terminal_mcp.connection_store import ConnectionStore
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.enrollment import (
    ERR_ALREADY_USED,
    ERR_EXPIRED,
    ERR_NOT_FOUND,
    ERR_REVOKED,
    EnrollmentStore,
    generate_code,
    normalize_code,
    suggest_node_id,
    validate_node_id,
)
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_onboarding import OnboardingError, OnboardingService, read_controller_ssh_public_key
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.node_transport import (
    HEALTH_DISABLED,
    HEALTH_FAILING,
    HEALTH_HEALTHY,
    KIND_LAN,
    KIND_REVERSE_SSH,
    KIND_TAILSCALE,
    REASON_ALL_FAILED,
    REASON_NO_TRANSPORTS,
    TransportResolver,
    TransportStore,
    probe_reverse_tunnel,
)
from terminal_mcp.windows_onboarding import (
    GENERIC_CODE,
    PROFILES,
    render_setup_script,
    script_fingerprint,
)

CONTROLLER_PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ2mCPfL0DEqQ7bDX8ky4Qs+z0lyHkQKcQCZoV3mM1Rf controller@test"
)
NODE_PUBLIC_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHrAqJ5v3rV0y8ZUf5pQmT4wKcE0uS1bN7dLxYgP9oJk terminal-mcp-rescue-win1"
)
GATEWAY_HOST_KEY = (
    "gw.example.net ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBnRk9m0aQ4rXv2sT6yUpL8cWfHjD3eZgN5tKqM7bVxA"
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _onboarding_config(tmp_path: Path, *, rescue: bool = False, tailscale: bool = True,
                       enabled: bool = True) -> OnboardingConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    key_file = tmp_path / "controller_key.pub"
    key_file.write_text(CONTROLLER_PUBLIC_KEY, encoding="utf-8")
    return OnboardingConfig(
        enabled=enabled,
        enrollment_ttl_seconds=900,
        controller_url="http://controller.test:8766",
        controller_ssh_public_key_file=str(key_file),
        tailscale=TailscaleOnboardConfig(enabled=tailscale, auth_key_env="TEST_TS_AUTH_KEY"),
        rescue=RescueTunnelConfig(
            enabled=rescue, gateway_host="gw.example.net", gateway_port=22, gateway_user="tunnel",
            gateway_host_key=GATEWAY_HOST_KEY, port_range_start=22000, port_range_end=22004),
    )


def _config(tmp_path: Path, **kwargs) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
        nodes=NodesConfig(onboarding=_onboarding_config(tmp_path, **kwargs)),
    )


def _service(tmp_path: Path, *, config: AppConfig | None = None, tailscale_available: bool = True):
    config = config or _config(tmp_path)
    terminal = TerminalService(config)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(terminal),
                                   local_workspace_root=str(tmp_path))
    connection_store = ConnectionStore(tmp_path / "connections.db")
    detected = ({"available": True, "ip": "100.100.1.1", "hostname": "ctl", "reason": None}
                if tailscale_available else
                {"available": False, "ip": None, "hostname": None, "reason": "tailscale_not_installed"})
    onboarding = OnboardingService(
        config, controller=controller, connection_store=connection_store,
        enrollment_store=EnrollmentStore(tmp_path / "enrollment.db"),
        transport_store=TransportStore(tmp_path / "transports.db"),
        port_allocator=rescue_gateway.RescuePortAllocator(
            tmp_path / "rescue.db",
            port_range=(config.nodes.onboarding.rescue.port_range_start,
                        config.nodes.onboarding.rescue.port_range_end)),
        audit=terminal.audit,
        # Exactly what server_http.py's real main() passes -- without it a
        # freshly-enrolled node's first heartbeat would 401, which is a
        # production behavior the tests must exercise, not paper over.
        token_env_setter=lambda node_id, token: os.environ.__setitem__(
            f"TERMINAL_MCP_NODE_TOKEN_{node_id.upper().replace('-', '_')}", token),
        tailscale_detector=lambda: detected,
    )
    return terminal, controller, connection_store, onboarding


@pytest.fixture
def rescue_keys_dir(tmp_path, monkeypatch):
    directory = tmp_path / "rescue-keys"
    monkeypatch.setenv("TERMINAL_MCP_RESCUE_KEYS_DIR", str(directory))
    return directory


def _client(tmp_path, monkeypatch, **kwargs):
    monkeypatch.setenv("TERMINAL_MCP_RESCUE_KEYS_DIR", str(tmp_path / "rescue-keys"))
    config = kwargs.pop("config", None) or _config(tmp_path, **kwargs)
    terminal, controller, connection_store, onboarding = _service(
        tmp_path, config=config, tailscale_available=kwargs.pop("tailscale_available", True))
    server = build_mcp(terminal)
    register_dashboard(server, terminal, controller=controller, connection_store=connection_store,
                       onboarding=onboarding)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, controller, onboarding


# ---------------------------------------------------------------------------
# enrollment codes
# ---------------------------------------------------------------------------

def test_generated_code_shape_and_normalization():
    code = generate_code()
    assert re.fullmatch(r"TMCP-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}", code)
    # A human typing it back: lower case, no dashes, and the three
    # Crockford confusables all land on the same canonical code.
    assert normalize_code(code.lower()) == code
    assert normalize_code(code.replace("-", "")) == code
    assert normalize_code(f"  {code}  ") == code
    assert normalize_code("nonsense") == ""
    assert normalize_code("") == ""


def test_code_is_stored_hashed_never_plaintext(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    record, code = store.create(node_id="win1")
    raw = (tmp_path / "e.db").read_bytes()
    assert code.encode() not in raw, "the plaintext enrollment code must never touch the database file"
    assert enrollment_mod.hash_code(code).encode() in raw
    # And the record the dashboard sees carries no code at all.
    assert "code" not in record.to_dict()
    assert code not in json.dumps(record.to_dict())


def test_consume_is_single_use(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    _record, code = store.create(node_id="win1")
    consumed, error = store.consume(code, hostname="WIN1")
    assert error is None and consumed is not None and consumed.node_id == "win1"
    replayed, error = store.consume(code, hostname="ATTACKER")
    assert replayed is None and error == ERR_ALREADY_USED


def test_consume_rejects_unknown_expired_and_revoked(tmp_path):
    store = EnrollmentStore(tmp_path / "e.db")
    assert store.consume(generate_code(), hostname="x")[1] == ERR_NOT_FOUND
    assert store.consume("total garbage", hostname="x")[1] == ERR_NOT_FOUND

    record, code = store.create(node_id="win2", ttl_seconds=60)
    future = datetime.now(timezone.utc) + timedelta(seconds=120)
    assert store.consume(code, hostname="x", now=future)[1] == ERR_EXPIRED
    # ...and an expired code READS as expired without ever being persisted so.
    assert store.get(record.id, now=future).status == enrollment_mod.STATUS_EXPIRED

    record3, code3 = store.create(node_id="win3")
    assert store.revoke(record3.id, by="operator") is True
    assert store.consume(code3, hostname="x")[1] == ERR_REVOKED
    # Revoking twice is a no-op, not an error that hides the first one.
    assert store.revoke(record3.id) is False


def test_concurrent_consume_yields_exactly_one_winner(tmp_path):
    """Two installers, same code, same instant. Exactly one may win --
    this is the property the whole single-use design rests on, so it is
    tested against real threads and a real sqlite file rather than
    reasoned about."""
    store = EnrollmentStore(tmp_path / "e.db")
    _record, code = store.create(node_id="race")
    barrier = threading.Barrier(8)

    def attempt(index: int):
        barrier.wait()
        return store.consume(code, hostname=f"host{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    winners = [record for record, error in results if record is not None]
    losers = [error for record, error in results if record is None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert all(error == ERR_ALREADY_USED for error in losers), losers


def test_node_id_validation_and_suggestion():
    assert validate_node_id("  WIN-Work ") == "win-work"
    for bad in ("", "local", "-leading", "a" * 65, "has space", "bad!char"):
        with pytest.raises(ValueError):
            validate_node_id(bad)
    assert suggest_node_id("DESKTOP-AB12CD") == "desktop-ab12cd"
    assert suggest_node_id("Rum's MacBook Pro") == "rum-s-macbook-pro"
    assert suggest_node_id("local").startswith("node-")  # reserved -> falls back
    assert suggest_node_id("").startswith("node-")


# ---------------------------------------------------------------------------
# rescue gateway
# ---------------------------------------------------------------------------

def test_gateway_describe_names_each_missing_piece(tmp_path):
    disabled = rescue_gateway.describe(RescueTunnelConfig(enabled=False))
    assert (disabled.configured, disabled.reason) == (False, rescue_gateway.REASON_DISABLED)

    no_host = rescue_gateway.describe(RescueTunnelConfig(enabled=True))
    assert no_host.reason == rescue_gateway.REASON_NO_HOST

    no_user = rescue_gateway.describe(RescueTunnelConfig(enabled=True, gateway_host="gw.example.net"))
    assert no_user.reason == rescue_gateway.REASON_NO_USER

    no_key = rescue_gateway.describe(RescueTunnelConfig(
        enabled=True, gateway_host="gw.example.net", gateway_user="tunnel"))
    assert no_key.reason == rescue_gateway.REASON_NO_HOST_KEY

    # A PRIVATE key pasted where the public host key belongs is refused.
    private_ish = rescue_gateway.describe(RescueTunnelConfig(
        enabled=True, gateway_host="gw.example.net", gateway_user="tunnel",
        gateway_host_key="-----BEGIN OPENSSH PRIVATE KEY-----"))
    assert private_ish.reason == rescue_gateway.REASON_NO_HOST_KEY

    good = rescue_gateway.describe(RescueTunnelConfig(
        enabled=True, gateway_host="gw.example.net", gateway_user="tunnel",
        gateway_host_key=GATEWAY_HOST_KEY))
    assert good.configured and good.reason is None and good.host_key == GATEWAY_HOST_KEY
    # to_dict hides the host key unless explicitly asked -- it is public,
    # but there is no reason for it to ride along in every status payload.
    assert "host_key" not in good.to_dict()
    assert good.to_dict(include_host_key=True)["host_key"] == GATEWAY_HOST_KEY


def test_port_allocation_is_idempotent_and_collision_free(tmp_path):
    allocator = rescue_gateway.RescuePortAllocator(tmp_path / "r.db", port_range=(22000, 22002))
    first = allocator.allocate("win1", public_key=NODE_PUBLIC_KEY)
    again = allocator.allocate("win1")
    assert first.port == again.port, "re-running the installer must not leak a second port"
    assert again.public_key == NODE_PUBLIC_KEY, "a repair with no key must keep the stored one"

    second = allocator.allocate("win2")
    assert second.port != first.port
    third = allocator.allocate("win3")
    assert len({first.port, second.port, third.port}) == 3
    with pytest.raises(RuntimeError, match=rescue_gateway.REASON_EXHAUSTED):
        allocator.allocate("win4")

    assert allocator.release("win2") is True
    assert allocator.allocate("win4").port == second.port  # freed port is reusable
    assert allocator.release("nobody") is False


def test_concurrent_port_allocation_never_double_assigns(tmp_path):
    allocator = rescue_gateway.RescuePortAllocator(tmp_path / "r.db", port_range=(23000, 23099))
    barrier = threading.Barrier(16)

    def allocate(index: int):
        barrier.wait()
        return rescue_gateway.RescuePortAllocator(
            tmp_path / "r.db", port_range=(23000, 23099)).allocate(f"node{index}").port

    with ThreadPoolExecutor(max_workers=16) as pool:
        ports = list(pool.map(allocate, range(16)))
    assert len(set(ports)) == 16, f"ports collided: {sorted(ports)}"


def test_tunnel_ssh_options_bind_loopback_and_fail_closed():
    gateway = rescue_gateway.describe(RescueTunnelConfig(
        enabled=True, gateway_host="gw.example.net", gateway_user="tunnel",
        gateway_host_key=GATEWAY_HOST_KEY))
    argv = rescue_gateway.build_tunnel_ssh_options(
        gateway, reverse_port=22001, identity_file="C:/k/id", known_hosts_file="C:/k/known_hosts")
    joined = " ".join(argv)
    # The reverse listener must be pinned to the gateway's loopback -- the
    # bare "-R 22001:..." form would inherit the gateway's GatewayPorts
    # setting and could become world-reachable.
    assert "-R" in argv and "127.0.0.1:22001:127.0.0.1:22" in argv
    assert "ExitOnForwardFailure=yes" in argv
    assert "ServerAliveInterval=30" in argv and "ServerAliveCountMax=3" in argv
    assert "StrictHostKeyChecking=yes" in argv
    assert "UserKnownHostsFile=C:/k/known_hosts" in argv
    assert "StrictHostKeyChecking=no" not in joined
    assert argv[-1] == "tunnel@gw.example.net"


def test_authorized_keys_line_is_restricted_to_one_port():
    line = rescue_gateway.build_authorized_keys_line(
        reverse_port=22001, public_key=NODE_PUBLIC_KEY, node_id="win1")
    assert line.startswith('restrict,port-forwarding,permitlisten="127.0.0.1:22001" ')
    assert "terminal-mcp-rescue-win1" in line
    # Only the key type + material, never a stray comment from the node.
    assert NODE_PUBLIC_KEY.split()[1] in line
    assert "terminal-mcp-rescue-win1 terminal-mcp-rescue-win1" not in line


# ---------------------------------------------------------------------------
# script generation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_script_carries_no_reusable_secret(profile):
    code = generate_code()
    script = render_setup_script(enrollment_code=code, controller_url="http://controller.test:8766",
                                 node_id="win1", profile=profile)
    # The one-time code IS in there by design; nothing else is.
    assert code in script
    for forbidden in ("BEGIN OPENSSH PRIVATE KEY", "tskey-", "Bearer ey", "authkey=tskey"):
        assert forbidden not in script
    # No 64-hex bearer-token-shaped literal anywhere.
    assert not re.search(r"\b[0-9a-f]{64}\b", script)
    # And no unresolved template placeholder shipped to a machine.
    assert "@@" not in script


def test_script_is_deterministic_and_fingerprinted():
    args = dict(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                controller_url="http://controller.test:8766", node_id="win1", profile="developer")
    first, second = render_setup_script(**args), render_setup_script(**args)
    assert first == second
    assert script_fingerprint(first) == script_fingerprint(second)
    assert len(script_fingerprint(first)) == 64


def test_script_rejects_anything_unsafe_to_embed():
    ok = dict(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
              controller_url="http://controller.test:8766", node_id="win1")
    for bad in (
        {"enrollment_code": "'; rm -rf /; #"},
        {"enrollment_code": "TMCP-SHORT"},
        {"controller_url": "file:///etc/passwd"},
        {"controller_url": "http://host/\n$(evil)"},
        {"node_id": "win1'; Stop-Computer; #"},
        {"profile": "definitely-not-a-profile"},
    ):
        with pytest.raises(ValueError):
            render_setup_script(**{**ok, **bad})


def test_script_contains_the_persistence_and_hardening_the_task_requires():
    script = render_setup_script(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                                 controller_url="http://controller.test:8766",
                                 node_id="win1", profile="minimal")
    # sshd: Automatic, key-only
    assert "Set-Service -Name sshd -StartupType Automatic" in script
    assert "PasswordAuthentication no" in script
    # rescue + heartbeat both survive reboot, both start before login
    assert script.count("New-ScheduledTaskTrigger -AtStartup") == 2
    assert script.count("New-ScheduledTaskPrincipal -UserId 'SYSTEM'") == 2
    # reverse tunnel hardening
    assert "ExitOnForwardFailure=yes" in script
    assert "'-R', ('127.0.0.1:{0}:127.0.0.1:22' -f $bootstrap.rescue.reverse_port)" in script
    assert "StrictHostKeyChecking=no" not in script
    # admin + repair + uninstall surfaces
    assert "IsInRole(" in script and "-Verb RunAs" in script
    assert "$Repair" in script and "$Uninstall" in script
    # honest exit codes
    assert "exit 1" in script and "exit 2" in script and "exit 3" in script


def test_generic_script_refuses_to_run_without_a_real_code():
    script = render_setup_script(enrollment_code=GENERIC_CODE, controller_url="http://c.test:8766",
                                 node_id="pending", profile="minimal")
    assert "This is the generic setup script -- it needs an enrollment code." in script
    assert GENERIC_CODE in script


# ---------------------------------------------------------------------------
# transport resolver
# ---------------------------------------------------------------------------

def _seed_transports(store: TransportStore, node_id="win1"):
    store.upsert(node_id, KIND_TAILSCALE, endpoint="ssh://100.100.0.5:22", host="100.100.0.5", port=22)
    store.upsert(node_id, KIND_LAN, endpoint="ssh://192.168.1.50:22", host="192.168.1.50", port=22)
    store.upsert(node_id, KIND_REVERSE_SSH, endpoint="ssh://gw.example.net:22#127.0.0.1:22001",
                 host="gw.example.net", port=22001)


def test_resolver_prefers_primary_when_it_answers(tmp_path):
    store = TransportStore(tmp_path / "t.db")
    _seed_transports(store)
    resolution = TransportResolver(store).resolve("win1", probe=lambda t: (True, None))
    assert resolution.reachable and resolution.kind == KIND_TAILSCALE
    assert len(resolution.attempts) == 1, "a healthy primary must not be followed by needless probes"
    assert store.get("win1", KIND_TAILSCALE).health == HEALTH_HEALTHY


def test_resolver_falls_back_to_rescue_and_records_why(tmp_path):
    store = TransportStore(tmp_path / "t.db")
    _seed_transports(store)

    def probe(transport):
        if transport.kind == KIND_REVERSE_SSH:
            return True, None
        return False, f"{transport.kind} is down"

    resolution = TransportResolver(store).resolve("win1", probe=probe)
    assert resolution.reachable and resolution.kind == KIND_REVERSE_SSH
    assert [a.kind for a in resolution.attempts] == [KIND_TAILSCALE, KIND_LAN, KIND_REVERSE_SSH]
    # Failure is never silent: the reason is on the row, not only in a log.
    assert store.get("win1", KIND_TAILSCALE).health == HEALTH_FAILING
    assert store.get("win1", KIND_TAILSCALE).last_error == "tailscale is down"
    assert store.get("win1", KIND_REVERSE_SSH).health == HEALTH_HEALTHY


def test_resolver_reports_unreachable_with_every_reason(tmp_path):
    store = TransportStore(tmp_path / "t.db")
    _seed_transports(store)
    resolution = TransportResolver(store).resolve("win1", probe=lambda t: (False, f"{t.kind} refused"))
    assert not resolution.reachable
    assert resolution.kind is None and resolution.endpoint is None
    assert resolution.reason == REASON_ALL_FAILED
    assert {a.error for a in resolution.attempts} == {
        "tailscale refused", "lan refused", "reverse_ssh refused"}


def test_resolver_never_uses_a_disabled_transport(tmp_path):
    """"Không chuyển sang route không trust": an operator-disabled path is
    not probed and not selected, even when every other path is dead."""
    store = TransportStore(tmp_path / "t.db")
    _seed_transports(store)
    store.set_health("win1", KIND_REVERSE_SSH, HEALTH_DISABLED)
    probed = []

    def probe(transport):
        probed.append(transport.kind)
        return False, "down"

    resolution = TransportResolver(store).resolve("win1", probe=probe)
    assert KIND_REVERSE_SSH not in probed
    assert not resolution.reachable


def test_resolver_on_a_node_with_no_transports(tmp_path):
    store = TransportStore(tmp_path / "t.db")
    resolution = TransportResolver(store).resolve("ghost", probe=lambda t: (True, None))
    assert not resolution.reachable and resolution.reason == REASON_NO_TRANSPORTS and resolution.attempts == ()


def test_resolver_treats_a_raising_prober_as_a_failed_probe(tmp_path):
    store = TransportStore(tmp_path / "t.db")
    _seed_transports(store)

    def probe(transport):
        if transport.kind == KIND_TAILSCALE:
            raise OSError("network is unreachable")
        return True, None

    resolution = TransportResolver(store).resolve("win1", probe=probe)
    assert resolution.reachable and resolution.kind == KIND_LAN
    assert "OSError" in resolution.attempts[0].error


def test_reverse_tunnel_probe_classifies_gateway_output():
    gateway = rescue_gateway.describe(RescueTunnelConfig(
        enabled=True, gateway_host="gw.example.net", gateway_user="tunnel",
        gateway_host_key=GATEWAY_HOST_KEY))

    def runner_with(stderr: str, code: int = 255):
        return lambda argv, **kwargs: subprocess.CompletedProcess(argv, code, "", stderr)

    # Reached a real sshd through the tunnel -> the tunnel is UP, even
    # though authentication (correctly) failed.
    ok, error = probe_reverse_tunnel(gateway, 22001, runner=runner_with("Permission denied (publickey)."))
    assert ok and error is None
    # The gateway let us in but nothing is listening -> tunnel DOWN, and
    # the message says which of the two problems it is.
    ok, error = probe_reverse_tunnel(
        gateway, 22001, runner=runner_with("channel 0: open failed: connect failed: Connection refused"))
    assert not ok and "tunnel is down" in error
    # The gateway itself is the problem -> a different message.
    ok, error = probe_reverse_tunnel(gateway, 22001, runner=runner_with("ssh: Could not resolve hostname gw"))
    assert not ok and "gateway unreachable" in error
    # Not configured at all -> the config reason, not a fake probe result.
    unconfigured = rescue_gateway.describe(RescueTunnelConfig(enabled=False))
    ok, error = probe_reverse_tunnel(unconfigured, 22001, runner=runner_with(""))
    assert not ok and error == rescue_gateway.REASON_DISABLED


# ---------------------------------------------------------------------------
# service: create -> consume -> describe -> remove
# ---------------------------------------------------------------------------

def test_connectivity_plan_degrades_instead_of_promising(tmp_path):
    _t, _c, _cs, with_tailnet = _service(tmp_path / "a", tailscale_available=True)
    plan = with_tailnet.plan_connectivity({})
    assert plan.tailscale is True and plan.tailscale_reason is None
    # Rescue is off by default -> reported as such, with a reason.
    assert plan.rescue is False and plan.rescue_reason == rescue_gateway.REASON_DISABLED

    _t, _c, _cs, without = _service(tmp_path / "b", tailscale_available=False)
    plan = without.plan_connectivity({})
    assert plan.tailscale is False and plan.tailscale_reason == "tailscale_not_installed"

    # The Advanced toggles can only ever turn something OFF.
    plan = with_tailnet.plan_connectivity({"tailscale": False})
    assert plan.tailscale is False and plan.tailscale_reason == "declined_by_operator"


def test_full_enrollment_flow_registers_the_node(tmp_path, rescue_keys_dir, monkeypatch):
    config = _config(tmp_path, rescue=True)
    _terminal, controller, connection_store, onboarding = _service(tmp_path, config=config)

    created = onboarding.create_enrollment(node_id="win1", profile="developer",
                                           connectivity={}, created_by="op@example.com")
    assert controller.node_status("win1") is None, "creating a code must not create a node"

    result = onboarding.consume_enrollment(
        created["code"], hostname="WIN1-PC", source_ip="100.100.0.5",
        addresses={"tailscale_ip": "100.100.0.5", "lan_ip": "192.168.1.50"},
        rescue_public_key=NODE_PUBLIC_KEY)

    assert result["node_id"] == "win1"
    assert len(result["node_token"]) == 64
    assert result["ssh"]["authorized_key"] == CONTROLLER_PUBLIC_KEY
    assert result["ssh"]["password_authentication"] is False
    assert result["rescue"]["configured"] is True
    assert 22000 <= result["rescue"]["reverse_port"] <= 22004
    assert result["rescue"]["host_key"] == GATEWAY_HOST_KEY

    # The node is now real: registered, with credentials and transports.
    node = controller.node_status("win1")
    assert node is not None and node.endpoint == "http://100.100.0.5:8790"
    assert connection_store.get("win1").token_file
    kinds = {t.kind for t in onboarding.transports.list_for("win1")}
    assert kinds == {KIND_TAILSCALE, KIND_LAN, KIND_REVERSE_SSH}

    # The gateway authorized_keys line was staged for the admin to sync.
    staged = (rescue_keys_dir / "win1.pub").read_text()
    assert staged.startswith('restrict,port-forwarding,permitlisten=')
    assert (rescue_keys_dir / "authorized_keys").read_text().strip() == staged.strip()


def test_enrollment_refuses_a_duplicate_node_id(tmp_path):
    _t, controller, _cs, onboarding = _service(tmp_path)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    onboarding.consume_enrollment(created["code"], hostname="WIN1", addresses={"lan_ip": "192.168.1.50"})
    with pytest.raises(OnboardingError) as excinfo:
        onboarding.create_enrollment(node_id="win1", profile="minimal")
    assert excinfo.value.code == "NODE_ALREADY_EXISTS" and excinfo.value.status == 409


def test_a_lying_node_cannot_get_its_address_trusted_as_a_tailnet_path(tmp_path):
    """A node claiming a tailnet IP that is not in 100.64.0.0/10 gets it
    filed as LAN. The overlay path is a trust boundary, not a label the
    node picks for itself."""
    _t, _c, _cs, onboarding = _service(tmp_path)
    created = onboarding.create_enrollment(node_id="liar", profile="minimal")
    onboarding.consume_enrollment(created["code"], hostname="LIAR",
                                  addresses={"tailscale_ip": "10.0.0.5", "lan_ip": "10.0.0.5"})
    kinds = {t.kind for t in onboarding.transports.list_for("liar")}
    assert KIND_TAILSCALE not in kinds
    assert KIND_LAN in kinds


def test_rescue_is_skipped_with_a_reason_when_no_gateway_is_configured(tmp_path):
    _t, _c, _cs, onboarding = _service(tmp_path)  # rescue disabled (the default)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    result = onboarding.consume_enrollment(created["code"], hostname="WIN1",
                                           addresses={"lan_ip": "192.168.1.50"},
                                           rescue_public_key=NODE_PUBLIC_KEY)
    assert result["rescue"]["configured"] is False
    assert result["rescue"]["reason"] == rescue_gateway.REASON_DISABLED
    assert "host_key" not in result["rescue"], "an unconfigured gateway must not ship a host key"
    assert onboarding.ports.get("win1") is None, "no port is burned for a node that cannot use one"


def test_rescue_needs_a_real_public_key_from_the_node(tmp_path, rescue_keys_dir):
    config = _config(tmp_path, rescue=True)
    _t, _c, _cs, onboarding = _service(tmp_path, config=config)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    result = onboarding.consume_enrollment(
        created["code"], hostname="WIN1", addresses={"lan_ip": "192.168.1.50"},
        rescue_public_key="-----BEGIN OPENSSH PRIVATE KEY-----")
    assert result["rescue"]["configured"] is False
    assert result["rescue"]["reason"] == "node_rescue_public_key_missing_or_malformed"


def test_tailscale_auth_key_comes_from_the_environment_and_only_at_consume(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_TS_AUTH_KEY", "tskey-auth-EXAMPLE-not-a-real-key")
    _t, _c, _cs, onboarding = _service(tmp_path)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    # The create response -- which is what the browser sees and what the
    # script is rendered from -- must not carry it.
    assert "tskey-" not in json.dumps(created["enrollment"])
    assert "tskey-" not in created["script"] if "script" in created else True
    result = onboarding.consume_enrollment(created["code"], hostname="WIN1",
                                           addresses={"lan_ip": "192.168.1.50"})
    assert result["tailscale"]["auth_key"] == "tskey-auth-EXAMPLE-not-a-real-key"


def test_no_tailscale_auth_key_is_a_documented_manual_step_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("TEST_TS_AUTH_KEY", raising=False)
    _t, _c, _cs, onboarding = _service(tmp_path)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    result = onboarding.consume_enrollment(created["code"], hostname="WIN1",
                                           addresses={"lan_ip": "192.168.1.50"})
    assert result["tailscale"]["enabled"] is True
    assert result["tailscale"]["auth_key"] is None
    assert result["tailscale"]["reason"] == "auth_key_not_configured_interactive_login_required"


def test_describe_node_leaks_nothing(tmp_path, rescue_keys_dir):
    config = _config(tmp_path, rescue=True)
    _t, _c, _cs, onboarding = _service(tmp_path, config=config)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    result = onboarding.consume_enrollment(created["code"], hostname="WIN1",
                                           addresses={"lan_ip": "192.168.1.50"},
                                           rescue_public_key=NODE_PUBLIC_KEY)
    described = json.dumps(onboarding.describe_node("win1"))
    assert result["node_token"] not in described
    assert created["code"] not in described
    assert NODE_PUBLIC_KEY.split()[1] not in described, "even a public key stays out of the status payload"
    assert "has_credentials" in described


def test_remove_node_revokes_everything_it_owns(tmp_path, rescue_keys_dir, monkeypatch):
    config = _config(tmp_path, rescue=True)
    _t, controller, connection_store, onboarding = _service(tmp_path, config=config)
    created = onboarding.create_enrollment(node_id="win1", profile="minimal")
    onboarding.consume_enrollment(created["code"], hostname="WIN1",
                                  addresses={"lan_ip": "192.168.1.50"},
                                  rescue_public_key=NODE_PUBLIC_KEY)
    # A second, still-pending code for the same node must die with it.
    onboarding.create_enrollment(node_id="win1", profile="minimal") if False else None
    pending, _code = onboarding.enrollments.create(node_id="win1")

    result = onboarding.remove_node("win1", by="op@example.com")
    assert result["deregistered"] is True
    assert result["rescue_port_released"] is True
    assert result["revoked_enrollments"] == 1
    assert result["transports_removed"] == 2  # lan + reverse_ssh (no tailnet address reported)

    assert controller.node_status("win1") is None
    assert connection_store.get("win1") is None
    assert onboarding.ports.get("win1") is None
    assert onboarding.transports.list_for("win1") == []
    assert onboarding.enrollments.get(pending.id).status == enrollment_mod.STATUS_REVOKED
    # The gateway's combined authorized_keys no longer carries this node.
    assert not (rescue_keys_dir / "win1.pub").exists()
    assert "win1" not in (rescue_keys_dir / "authorized_keys").read_text()


def test_controller_public_key_reader_refuses_a_private_key(tmp_path):
    config = _onboarding_config(tmp_path)
    key, reason = read_controller_ssh_public_key(config)
    assert key == CONTROLLER_PUBLIC_KEY and reason is None

    bad = tmp_path / "bad.pub"
    bad.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----\n")
    config = OnboardingConfig(controller_ssh_public_key_file=str(bad))
    key, reason = read_controller_ssh_public_key(config)
    assert key is None and reason == "controller_ssh_public_key_malformed"

    config = OnboardingConfig(controller_ssh_public_key_file=str(tmp_path / "nope.pub"))
    key, reason = read_controller_ssh_public_key(config)
    assert key is None and reason == "controller_ssh_public_key_not_configured"


# ---------------------------------------------------------------------------
# dashboard routes
# ---------------------------------------------------------------------------

def test_profiles_route_describes_what_this_controller_can_actually_do(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.get("/dashboard/api/nodes/onboard/profiles")
    assert response.status_code == 200
    body = response.json()
    assert [p["id"] for p in body["profiles"]] == ["minimal", "developer", "ai_coding"]
    assert body["connectivity"]["tailscale"] is True
    assert body["connectivity"]["rescue"] is False
    assert body["gateway"]["reason"] == rescue_gateway.REASON_DISABLED
    assert body["controller_ssh_key_configured"] is True
    assert body["script_version"]


def test_create_enrollment_returns_the_script_once(tmp_path, monkeypatch):
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.post("/dashboard/api/nodes/onboard/enrollments",
                           json={"node_id": "win1", "profile": "developer"})
    assert response.status_code == 200
    body = response.json()
    assert body["code"].startswith("TMCP-")
    assert body["code"] in body["script"]
    assert body["script_sha256"] == script_fingerprint(body["script"])
    assert body["filename"] == "terminal-mcp-setup-win1.ps1"
    assert controller.node_status("win1") is None

    # The listing that comes back later shows the code only as a stub.
    listing = client.get("/dashboard/api/nodes/onboard/enrollments").json()["enrollments"]
    assert len(listing) == 1
    assert body["code"] not in json.dumps(listing)
    assert listing[0]["code_display"].startswith("TMCP-") and listing[0]["code_display"].endswith("…")
    assert listing[0]["status"] == "pending"


def test_enrollment_routes_require_the_dashboard_mutation_guard(tmp_path, monkeypatch):
    """A page on another origin cannot create, revoke or remove a node --
    the same CSRF/Origin boundary every other dashboard mutation has."""
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    for method, path, payload in (
        ("post", "/dashboard/api/nodes/onboard/enrollments", {"node_id": "evil"}),
        ("post", "/dashboard/api/nodes/onboard/enrollments/whatever/revoke", {}),
        ("post", "/dashboard/api/nodes/win1/remove", {}),
        ("post", "/dashboard/api/nodes/win1/test-transport", {"transport": "all"}),
    ):
        response = getattr(client, method)(path, json=payload, headers={"Origin": "https://evil.example"})
        assert response.status_code == 403, path
        assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_enrollment_is_refused_when_onboarding_is_disabled(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch, enabled=False)
    response = client.post("/dashboard/api/nodes/onboard/enrollments", json={"node_id": "win1"})
    assert response.status_code == 403 and response.json()["error"] == "ONBOARDING_DISABLED"
    # ...and the generic script download is refused too, rather than
    # serving an installer that would 403 at the enrollment step.
    assert client.get("/enroll/windows-setup.ps1").status_code == 403


def test_consume_route_is_single_use_and_replay_is_refused(tmp_path, monkeypatch):
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "win1", "profile": "minimal"}).json()["code"]

    first = client.post("/dashboard/api/enroll/consume",
                        json={"code": code, "hostname": "WIN1-PC", "platform": "windows",
                              "addresses": {"lan_ip": "192.168.1.50"}})
    assert first.status_code == 200
    assert first.json()["node_id"] == "win1"
    assert controller.node_status("win1") is not None

    replay = client.post("/dashboard/api/enroll/consume",
                         json={"code": code, "hostname": "ATTACKER"})
    assert replay.status_code == 401
    assert replay.json()["error"] == "ENROLLMENT_ALREADY_USED"


def test_consume_route_is_rate_limited(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    codes = [401] * 12
    for _ in codes:
        client.post("/dashboard/api/enroll/consume", json={"code": generate_code(), "hostname": "x"})
    limited = client.post("/dashboard/api/enroll/consume", json={"code": generate_code(), "hostname": "x"})
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"


def test_enrolled_node_can_heartbeat_immediately(tmp_path, monkeypatch):
    """The whole point of onboarding: the node shows up Ready without any
    controller restart or manual env-var export."""
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "win1", "profile": "minimal"}).json()["code"]
    bootstrap = client.post("/dashboard/api/enroll/consume",
                            json={"code": code, "hostname": "WIN1-PC",
                                  "addresses": {"lan_ip": "192.168.1.50"}}).json()

    beat = client.post(f"/dashboard/api/nodes/win1/heartbeat",
                       headers={"Authorization": f"Bearer {bootstrap['node_token']}"},
                       json={"metrics": {"cpu_percent": 12.0, "ram_percent": 40.0},
                             "tmux_session_count": 0, "agent_counts": {}, "agent_types": ["shell"],
                             "platform": "windows", "session_backend": "windows_pty",
                             "shell_capabilities": ["powershell"], "labels": ["windows", "onboarded"]})
    assert beat.status_code == 200, beat.text
    node = controller.node_status("win1")
    assert node.status == "online" and node.platform == "windows"
    assert node.session_backend == "windows_pty"

    # A wrong token is refused, so an enrolled node id is not a free pass.
    assert client.post("/dashboard/api/nodes/win1/heartbeat",
                       headers={"Authorization": "Bearer wrong"}, json={}).status_code == 401


def test_node_onboarding_status_and_transport_test_routes(tmp_path, monkeypatch):
    client, _controller, onboarding = _client(tmp_path, monkeypatch)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "win1", "profile": "minimal"}).json()["code"]
    client.post("/dashboard/api/enroll/consume",
                json={"code": code, "hostname": "WIN1", "addresses": {"lan_ip": "192.168.1.50"}})

    status = client.get("/dashboard/api/nodes/win1/onboarding")
    assert status.status_code == 200
    body = status.json()
    assert body["registered"] is True and body["has_credentials"] is True
    assert {t["kind"] for t in body["transports"]} == {KIND_LAN}
    assert body["enrollments"][0]["status"] == "consumed"

    # Test Primary against an address that is not listening: a real,
    # honest failure with a reason, never a fabricated green tick.
    result = client.post("/dashboard/api/nodes/win1/test-transport", json={"transport": "primary"})
    assert result.status_code == 200
    payload = result.json()
    assert payload["reachable"] is False
    assert payload["reason"] == REASON_ALL_FAILED
    assert payload["attempts"][0]["error"]
    # Test Rescue on a node with no rescue transport: a distinct answer.
    rescue = client.post("/dashboard/api/nodes/win1/test-transport", json={"transport": "rescue"}).json()
    assert rescue["reachable"] is False and rescue["reason"] == REASON_NO_TRANSPORTS


def test_test_transport_rejects_an_unknown_transport_name(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.post("/dashboard/api/nodes/win1/test-transport", json={"transport": "carrier-pigeon"})
    assert response.status_code == 400


def test_remove_route_refuses_the_local_node(tmp_path, monkeypatch):
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.post(f"/dashboard/api/nodes/{controller.local_node_id}/remove", json={})
    assert response.status_code == 400
    assert response.json()["error"] == "CANNOT_REMOVE_LOCAL_NODE"
    assert controller.node_status(controller.local_node_id) is not None


def test_node_can_deregister_itself_with_its_own_token_only(tmp_path, monkeypatch):
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "win1", "profile": "minimal"}).json()["code"]
    token = client.post("/dashboard/api/enroll/consume",
                        json={"code": code, "hostname": "WIN1",
                              "addresses": {"lan_ip": "192.168.1.50"}}).json()["node_token"]

    assert client.post("/dashboard/api/nodes/win1/deregister",
                       headers={"Authorization": "Bearer nope"}).status_code == 401
    assert controller.node_status("win1") is not None

    response = client.post("/dashboard/api/nodes/win1/deregister",
                           headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and response.json()["deregistered"] is True
    assert controller.node_status("win1") is None


def test_gateway_route_exposes_only_public_material(tmp_path, monkeypatch):
    config = _config(tmp_path, rescue=True)
    client, _controller, _onboarding = _client(tmp_path, monkeypatch, config=config)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "win1", "profile": "minimal"}).json()["code"]
    client.post("/dashboard/api/enroll/consume",
                json={"code": code, "hostname": "WIN1", "addresses": {"lan_ip": "192.168.1.50"},
                      "rescue_public_key": NODE_PUBLIC_KEY})

    body = client.get("/dashboard/api/nodes/onboard/gateway").json()
    assert body["gateway"]["configured"] is True
    assert body["gateway"]["host_key"] == GATEWAY_HOST_KEY  # public
    assert 'permitlisten="127.0.0.1:' in body["authorized_keys"]
    assert body["sync_command"].startswith("scp ")
    assert "PRIVATE KEY" not in json.dumps(body)
    assert body["allocations"][0]["node_id"] == "win1"
    assert "public_key" not in body["allocations"][0]


def test_generic_setup_script_is_served_without_a_secret(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.get("/enroll/windows-setup.ps1")
    assert response.status_code == 200
    assert response.headers["X-Terminal-Mcp-Setup-Version"]
    assert response.headers["X-Terminal-Mcp-Setup-Sha256"] == script_fingerprint(response.text)
    assert GENERIC_CODE in response.text
    assert not re.search(r"\b[0-9a-f]{64}\b", response.text)
    # Version pinning: a version this controller does not serve is a 404,
    # not a silently-different script.
    assert client.get("/enroll/windows-setup.ps1?v=0.0.1").status_code == 404


def test_revoked_code_cannot_be_consumed_through_the_route(tmp_path, monkeypatch):
    client, controller, _onboarding = _client(tmp_path, monkeypatch)
    created = client.post("/dashboard/api/nodes/onboard/enrollments",
                          json={"node_id": "win1", "profile": "minimal"}).json()
    revoked = client.post(
        f"/dashboard/api/nodes/onboard/enrollments/{created['enrollment']['id']}/revoke", json={})
    assert revoked.status_code == 200 and revoked.json()["revoked"] is True

    response = client.post("/dashboard/api/enroll/consume",
                           json={"code": created["code"], "hostname": "WIN1"})
    assert response.status_code == 401 and response.json()["error"] == "ENROLLMENT_REVOKED"
    assert controller.node_status("win1") is None


# ---------------------------------------------------------------------------
# UI acceptance -- the Nodes page markup/JS actually carries the flow
# ---------------------------------------------------------------------------

def _nodes_page() -> str:
    import terminal_mcp.dashboard as dashboard_module
    return dashboard_module.NODES_ADMIN_HTML


def test_add_node_wizard_is_present_and_is_the_primary_action():
    page = _nodes_page()
    # One obvious entry point, styled as the primary action.
    assert 'id="addNodeWizardBtn"' in page and '+ Add Node' in page
    assert 'class="icon-btn an-primary" id="addNodeWizardBtn"' in page
    # ...and it sits before the two advanced flows it is meant to replace
    # for a first-time user.
    assert page.index('id="addNodeWizardBtn"') < page.index('id="connectNodeBtn"')


def test_wizard_asks_for_the_minimum_and_nothing_more():
    page = _nodes_page()
    assert 'id="anStepOs"' in page and 'id="anStepForm"' in page and 'id="anStepDone"' in page
    assert 'data-os="windows"' in page
    # Node name is the ONLY free-text field on the first screen.
    form = page[page.index('id="anStepForm"'):page.index('id="anStepDone"')]
    text_inputs = re.findall(r'<input type="text" id="(\w+)"', form)
    assert text_inputs == ["anNodeId"], text_inputs
    # Connectivity is behind Advanced, not on the first screen.
    assert '<details id="anAdvanced"' in page
    assert form.index('id="anProfiles"') < form.index('id="anAdvanced"')


def test_wizard_downloads_via_blob_never_a_url_carrying_the_code():
    """A code in a query string lands in browser history, proxy logs and
    the controller's own access log. The download is a Blob built from the
    response already in memory."""
    page = _nodes_page()
    assert "new Blob([anGenerated.script]" in page
    assert "URL.createObjectURL" in page and "URL.revokeObjectURL" in page
    assert "?code=" not in page and "&code=" not in page


def test_node_detail_offers_test_primary_test_rescue_and_remove():
    page = _nodes_page()
    assert "Test Primary" in page and "Test Rescue" in page and "Remove Node" in page
    assert "renderOnboardingSection" in page
    assert 'id="nodeTransports"' in page
    # Removal is confirmed, and says what it does NOT touch.
    assert "window.confirm(" in page and "KHÔNG gỡ phần mềm nào trên máy đó" in page


def test_wizard_explains_why_a_connectivity_option_is_unavailable():
    page = _nodes_page()
    for reason in ("rescue_disabled", "tailscale_not_installed", "gateway_host_not_configured"):
        assert reason in page, f"{reason} has no operator-facing explanation"
    # An unavailable path cannot be switched ON from the form.
    assert "box.disabled = !available;" in page


def test_nodes_page_stays_responsive():
    page = _nodes_page()
    assert ".an-os-grid, .an-profiles { grid-template-columns:1fr }" in page
    assert "minmax(150px,1fr)" in page and "minmax(200px,1fr)" in page


# ---------------------------------------------------------------------------
# The generated PowerShell is parsed by a real PowerShell engine when one
# is available. This is the difference between "the string contains the
# words we expect" and "this file will actually run on the machine".
# ---------------------------------------------------------------------------

def _find_pwsh():
    import shutil as _shutil
    for name in ("pwsh", "powershell"):
        found = _shutil.which(name)
        if found:
            return found
    for candidate in Path("/tmp").glob("**/pwsh/pwsh"):
        if candidate.is_file():
            return str(candidate)
    return None


_PARSE_PROBE = r"""
param([string] $Path)
$errors = $null
$tokens = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
if ($errors) {
    $errors | ForEach-Object { "{0}:{1} {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message }
    exit 1
}
"OK"
"""


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_generated_script_parses_in_a_real_powershell(tmp_path, profile):
    pwsh = _find_pwsh()
    if pwsh is None:
        pytest.skip("no PowerShell available on this host")
    script = tmp_path / "windows-setup.ps1"
    script.write_text(render_setup_script(
        enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE", controller_url="http://controller.test:8766",
        node_id="win1", profile=profile), encoding="utf-8")
    probe = tmp_path / "parse.ps1"
    probe.write_text(_PARSE_PROBE, encoding="utf-8")
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(probe), "-Path", str(script)],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"{profile} script does not parse:\n{result.stdout}\n{result.stderr}"


def test_inner_generated_scripts_parse_too(tmp_path):
    """The installer WRITES two more PowerShell files (the rescue tunnel
    loop and the heartbeat loop) from here-strings. A syntax error in one
    of those would not show up until the machine rebooted and the
    Scheduled Task silently failed -- so they are expanded and parsed
    here, not merely eyeballed."""
    pwsh = _find_pwsh()
    if pwsh is None:
        pytest.skip("no PowerShell available on this host")
    source = render_setup_script(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                                 controller_url="http://controller.test:8766",
                                 node_id="win1", profile="minimal")
    templates = {}
    for variable in ("runner", "beat"):
        match = re.search(r"\$" + variable + r' = @"\n(.*?)\n"@\n', source, re.S)
        assert match, f"could not find the ${variable} here-string in the generated script"
        templates[variable] = match.group(1)

    expander = tmp_path / "expand.ps1"
    expander.write_text(r"""
param([string] $Template, [string] $Out)
# The variables the outer script has in scope where the here-string is
# expanded. Values are placeholders -- only the SHAPE matters here.
$LogDir = 'C:\ProgramData\TerminalMCP\logs'
$argLiteral = "'-N', '-T', '-i', 'C:\k\id'"
$RescueKey = 'C:\k\id'
$RescueKnown = 'C:\k\known_hosts'
$NodeConfig = 'C:\ProgramData\TerminalMCP\node.json'
$TokenFile = 'C:\ProgramData\TerminalMCP\node.token'
$NodeId = 'win1'
$TaskRescue = 'TerminalMCP-Rescue-win1'
$ScriptVersion = '1.0.0'
$ProfileName = 'minimal'
$interval = 30
$retry = 15
$expanded = $ExecutionContext.InvokeCommand.ExpandString((Get-Content -Raw $Template))
Set-Content -Path $Out -Value $expanded -Encoding utf8
$errors = $null; $tokens = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($Out, [ref]$tokens, [ref]$errors)
if ($errors) { $errors | ForEach-Object { "{0}:{1} {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message }; exit 1 }
"OK"
""", encoding="utf-8")

    for variable, body in templates.items():
        template_file = tmp_path / f"{variable}.tpl"
        template_file.write_text(body, encoding="utf-8")
        out_file = tmp_path / f"{variable}.ps1"
        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(expander), "-Template", str(template_file), "-Out", str(out_file)],
            capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, f"generated ${variable} script does not parse:\n{result.stdout}{result.stderr}"
        expanded = out_file.read_text(encoding="utf-8")
        if variable == "runner":
            # The rescue loop must actually loop, and must keep the
            # loopback bind it was handed.
            assert "while ($true)" in expanded and "Start-Sleep" in expanded
            assert "ssh.exe @sshArgs" in expanded
        else:
            assert "while ($true)" in expanded
            assert "/dashboard/api/nodes/$nodeId/heartbeat" in expanded
            assert "Bearer" in expanded and "$token" in expanded


def test_consume_records_the_SSH_port_not_the_agent_port(tmp_path):
    """Regression, found on staging: transports were recorded at the
    node-agent's HTTP port (8790) while Test Primary probes them with an
    SSH banner read. On a Minimal-profile node nothing listens on 8790, so
    Test Primary failed on every healthy node.

    The unit tests missed it because they seeded transports by hand with
    the right port; only a run through consume_enrollment could catch it.
    So this asserts the REAL path, and asserts the two fields that answer
    two different questions stay different: the transport is where we
    probe (sshd), the registry endpoint is where a node-agent would be."""
    _t, controller, _cs, onboarding = _service(tmp_path)
    created = onboarding.create_enrollment(node_id="ports", profile="minimal")
    onboarding.consume_enrollment(created["code"], hostname="PORTS",
                                  addresses={"tailscale_ip": "100.90.1.2", "lan_ip": "192.168.44.9"})

    by_kind = {t.kind: t for t in onboarding.transports.list_for("ports")}
    assert set(by_kind) == {KIND_TAILSCALE, KIND_LAN}
    for kind, host in ((KIND_TAILSCALE, "100.90.1.2"), (KIND_LAN, "192.168.44.9")):
        assert by_kind[kind].port == 22, f"{kind} must be probed at sshd, not the agent port"
        assert by_kind[kind].endpoint == f"ssh://{host}:22"
        assert "8790" not in by_kind[kind].endpoint

    # ...while the registry still points at the agent, for the day one is
    # installed. Losing this distinction is the other half of the bug.
    node = controller.node_status("ports")
    assert node.endpoint == "http://100.90.1.2:8790"


def test_test_primary_probes_the_port_the_installer_actually_opens(tmp_path, monkeypatch):
    """The end the operator sees: Test Primary must dial 22, because that
    is the port windows-setup.ps1 starts sshd on and firewalls."""
    client, _controller, onboarding = _client(tmp_path, monkeypatch)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "probed", "profile": "minimal"}).json()["code"]
    client.post("/dashboard/api/enroll/consume",
                json={"code": code, "hostname": "PROBED", "addresses": {"lan_ip": "192.168.44.9"}})

    dialled: list[tuple[str, int]] = []

    def _fake_connection(address, timeout=None):
        dialled.append((address[0], address[1]))
        raise OSError("refused")

    monkeypatch.setattr("terminal_mcp.node_transport.socket.create_connection", _fake_connection)
    result = client.post("/dashboard/api/nodes/probed/test-transport", json={"transport": "primary"})
    assert result.status_code == 200 and result.json()["reachable"] is False
    assert dialled == [("192.168.44.9", 22)], dialled


# ---------------------------------------------------------------------------
# Quick install: the one-liner a user pastes into Win+R
# ---------------------------------------------------------------------------

def test_quick_install_command_shape_and_safety():
    from terminal_mcp.windows_onboarding import build_quick_install_command
    code = generate_code()
    cmd = build_quick_install_command(controller_url="http://100.100.0.1:8766", enrollment_code=code)

    # Runs via the Windows Run dialog: must start with the interpreter
    # every Windows has, and must not require a shell to be open already.
    assert cmd.startswith("powershell -NoP -Command ")
    # Download to a FILE then execute it -- never iex/irm straight into the
    # pipeline (task requirement, and it keeps the artefact on disk).
    assert "-OutFile" in cmd
    assert "iex" not in cmd and "Invoke-Expression" not in cmd
    # UAC, and process-scope policy ONLY -- never the machine policy.
    assert "-Verb RunAs" in cmd
    assert "-Ex Bypass" in cmd
    assert "Set-ExecutionPolicy" not in cmd
    # 5.1 needs -UseBasicParsing or IWR wants the IE engine.
    assert "-UseB" in cmd
    # The code travels as an ARGUMENT, never a URL query parameter.
    assert f"-EnrollmentCode {code}" in cmd
    assert "?code=" not in cmd and "&code=" not in cmd
    assert f"={code}" not in cmd.split("-EnrollmentCode")[0]


def test_quick_install_quotes_paths_for_usernames_with_spaces():
    """%TEMP% for "John Doe" contains a space, and Start-Process joins an
    argument array WITHOUT quoting. The command must build the quote
    itself -- via [char]34, because a literal quote would terminate the
    outer -Command string."""
    from terminal_mcp.windows_onboarding import build_quick_install_command
    cmd = build_quick_install_command(controller_url="http://100.100.0.1:8766",
                                      enrollment_code=generate_code())
    assert "[char]34+$f+[char]34" in cmd
    # Exactly two double quotes in the whole line: the pair delimiting
    # -Command. Any other would end the string early.
    assert cmd.count('"') == 2


def test_quick_install_fits_the_run_dialog():
    """Win+R truncates at ~259 characters. A command that does not fit is
    worse than useless -- it pastes as a half-command that may still
    execute."""
    from terminal_mcp.windows_onboarding import (RUN_DIALOG_MAX_CHARS, build_quick_install_command,
                                                 quick_install_fits_run_dialog)
    for base in ("http://100.100.0.1:8766", "https://terminal-dashboard.example.net"):
        cmd = build_quick_install_command(controller_url=base, enrollment_code=generate_code())
        assert len(cmd) <= RUN_DIALOG_MAX_CHARS, f"{base}: {len(cmd)} chars"
        assert quick_install_fits_run_dialog(cmd) is True
    assert quick_install_fits_run_dialog("x" * (RUN_DIALOG_MAX_CHARS + 1)) is False


def test_quick_install_rejects_unsafe_inputs():
    from terminal_mcp.windows_onboarding import build_quick_install_command
    ok = dict(controller_url="http://100.100.0.1:8766", enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE")
    for bad in ({"enrollment_code": "'; Stop-Computer; #"}, {"enrollment_code": "TMCP-SHORT"},
                {"controller_url": "file:///etc/passwd"}, {"controller_url": "http://h/\n$(evil)"}):
        with pytest.raises(ValueError):
            build_quick_install_command(**{**ok, **bad})


def test_quick_install_command_is_returned_by_the_create_route(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    body = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "quick", "profile": "minimal"}).json()
    cmd = body["quick_install_command"]
    assert body["quick_install_fits_run_dialog"] is True
    assert body["code"] in cmd
    assert cmd.startswith("powershell -NoP -Command ")
    # It points at the SHORT alias, which is the only reason it fits.
    assert "/w'" in cmd


def test_short_alias_serves_the_same_script_as_the_long_path(tmp_path, monkeypatch):
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    short = client.get("/w")
    long = client.get("/enroll/windows-setup.ps1")
    assert short.status_code == 200 and long.status_code == 200
    assert short.text == long.text, "the alias must serve byte-identical content"
    assert short.headers["X-Terminal-Mcp-Setup-Sha256"] == long.headers["X-Terminal-Mcp-Setup-Sha256"]
    assert not re.search(r"\b[0-9a-f]{64}\b", short.text)


# ---------------------------------------------------------------------------
# UI acceptance for the quick-install flow
# ---------------------------------------------------------------------------

def test_quick_install_is_the_primary_action_and_download_is_secondary():
    page = _nodes_page()
    done = page[page.index('id="anStepDone"'):page.index('id="enrollStrip"')]
    assert 'id="anCopyCmdBtn"' in done and 'Copy lệnh cài đặt' in done
    assert 'an-primary an-big-btn' in done, "the copy button must be styled as the primary action"
    # Download still exists, but demoted behind "Cách khác / thủ công".
    assert 'id="anDownloadBtn"' in done
    assert done.index('id="anCopyCmdBtn"') < done.index('id="anDownloadBtn"')
    assert 'Cách khác / thủ công' in done
    manual = done[done.index('<details class="an-manual">'):]
    assert 'id="anDownloadBtn"' in manual, "Download belongs inside the manual section"


def test_three_steps_never_ask_the_user_to_type_a_command():
    page = _nodes_page()
    done = page[page.index('id="anStepDone"'):page.index('id="enrollStrip"')]
    assert done.count("<li>") == 3
    assert "Win + R" in done and "Ctrl + V" in done and "Enter" in done
    assert "Yes" in done and "Administrator" in done
    # None of the old manual instructions survive in the primary path.
    quick = done[done.index('<div class="an-quick">'):done.index('<details class="an-manual">')]
    for banned in ("Set-ExecutionPolicy", "cd ", "Run with PowerShell", "Chuột phải"):
        assert banned not in quick, f"{banned!r} must not be in the no-typing path"


def test_command_box_is_readonly_and_copy_has_a_fallback():
    page = _nodes_page()
    assert 'id="anQuickCmd"' in page and "readonly" in page
    # navigator.clipboard is undefined on plain http:// over the LAN, which
    # is how this dashboard is actually reached -- the fallback is what
    # keeps the primary button working there.
    assert "navigator.clipboard" in page and "window.isSecureContext" in page
    assert "document.execCommand('copy')" in page
    assert "Đã copy ✓" in page
    assert "anFlash(" in page


def test_expiry_countdown_and_regenerate_exist():
    page = _nodes_page()
    assert "Lệnh có hiệu lực khoảng 15 phút" in page
    assert 'id="anRegenBtn"' in page and "Tạo lại" in page
    assert "Lệnh đã hết hạn" in page
    assert "anExpiryTimer" in page and "clearInterval" in page


def test_non_windows_browser_gets_a_note_but_can_still_copy():
    page = _nodes_page()
    assert "Chạy lệnh này trên máy Windows cần thêm." in page
    assert "navigator.platform" in page
    # The note must not disable the copy button.
    assert "copyBtn.disabled = true" not in page.split("navigator.platform")[1][:400]


def test_troubleshooting_commands_each_have_a_copy_button():
    page = _nodes_page()
    for command in ("Get-Service sshd", "tailscale status", "TerminalMCP-*"):
        assert command in page
    assert "an-trouble-row" in page
    assert 'id="anTrouble"' in page


# ---------------------------------------------------------------------------
# Installer progress: stage reporting, stall visibility, no secret leakage
# ---------------------------------------------------------------------------

def test_progress_route_records_a_stage_without_consuming_the_code(tmp_path, monkeypatch):
    """Progress arrives BEFORE the exchange (OpenSSH can take minutes) and
    after it (winget can take longer). Reporting it must not burn the
    single-use code."""
    client, _controller, onboarding = _client(tmp_path, monkeypatch)
    created = client.post("/dashboard/api/nodes/onboard/enrollments",
                          json={"node_id": "prog", "profile": "minimal"}).json()
    code = created["code"]

    response = client.post("/dashboard/api/enroll/progress",
                           json={"code": code, "stage": "installing_openssh", "elapsed_seconds": 92})
    assert response.status_code == 202 and response.json()["accepted"] is True

    row = client.get("/dashboard/api/nodes/onboard/enrollments").json()["enrollments"][0]
    assert row["progress_stage"] == "installing_openssh"
    assert row["progress_elapsed_seconds"] == 92
    assert row["progress_label"] == "Đang cài OpenSSH"
    assert row["status"] == "pending", "reporting progress must not consume the code"

    # ...and the code still works afterwards.
    assert client.post("/dashboard/api/enroll/consume",
                       json={"code": code, "hostname": "PROG-PC",
                             "addresses": {"lan_ip": "192.168.1.9"}}).status_code == 200
    # Post-consume stages keep landing on the same row.
    client.post("/dashboard/api/enroll/progress", json={"code": code, "stage": "ready", "elapsed_seconds": 240})
    row = client.get("/dashboard/api/nodes/onboard/enrollments").json()["enrollments"][0]
    assert row["progress_stage"] == "ready" and row["status"] == "consumed"


def test_progress_route_refuses_unknown_stages_and_never_leaks_the_code(tmp_path, monkeypatch, caplog):
    import logging
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    code = client.post("/dashboard/api/nodes/onboard/enrollments",
                       json={"node_id": "prog2", "profile": "minimal"}).json()["code"]

    bad = client.post("/dashboard/api/enroll/progress",
                      json={"code": code, "stage": "<img src=x onerror=1>"})
    assert bad.status_code == 400 and bad.json()["error"] == "INVALID_REQUEST"

    with caplog.at_level(logging.INFO):
        client.post("/dashboard/api/enroll/progress",
                    json={"code": code, "stage": "installing_openssh", "elapsed_seconds": 10})
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert code not in logged, "the enrollment code must never reach a log line"
    assert "prog2" in logged, "the node id is the identifier that may be logged"


def test_progress_for_an_unknown_code_is_indistinguishable_from_success(tmp_path, monkeypatch):
    """A distinct 404 here would answer 'is this code real?' for anyone
    holding a guess."""
    client, _controller, _onboarding = _client(tmp_path, monkeypatch)
    response = client.post("/dashboard/api/enroll/progress",
                           json={"code": generate_code(), "stage": "installing_openssh"})
    assert response.status_code == 202


def test_progress_elapsed_is_clamped(tmp_path):
    from terminal_mcp.enrollment import EnrollmentStore, STAGE_INSTALLING_TOOLS
    store = EnrollmentStore(tmp_path / "e.db")
    _rec, code = store.create(node_id="clamp")
    assert store.record_progress(code, stage=STAGE_INSTALLING_TOOLS, elapsed_seconds=10**9).progress_elapsed_seconds == 86_400
    assert store.record_progress(code, stage=STAGE_INSTALLING_TOOLS, elapsed_seconds=-5).progress_elapsed_seconds == 0


def test_installer_prints_staged_progress_with_elapsed_time():
    script = render_setup_script(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                                 controller_url="http://c.test:8766", node_id="win1", profile="minimal")
    # Stage banner carries position, wall clock and per-stage elapsed.
    assert 'Write-Host ("[{0}/{1}] {2}"' in script
    assert "Get-Date -Format 'HH:mm:ss'" in script
    assert 'tong cong {2}s' in script
    # Every stage opened is closed.
    assert script.count("\nStart-Stage ") + script.count("\n    Start-Stage ") == \
           script.count("\nEnd-Stage") + script.count("\n    End-Stage")
    # The numerator must match the number of stages actually run.
    import re as _re
    declared = int(_re.search(r"\$script:StageTotal = (\d+)", script).group(1))
    actual = len(_re.findall(r"^\s*Start-Stage ", script, _re.M))
    assert declared == actual, f"prints [n/{declared}] but runs {actual} stages"


def test_installer_never_goes_silent_on_a_slow_stage():
    script = render_setup_script(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                                 controller_url="http://c.test:8766", node_id="ai", profile="ai_coding")
    # Heartbeat at most every ~12s, and a distinct "longer than usual".
    assert "-ge 12" in script
    assert "Dang cho lau hon binh thuong" in script
    assert "Van dang chay" in script
    # Output is streamed while the job runs, not collected at the end.
    assert "Receive-Job" in script and "while ($job.State -eq 'Running')" in script
    # The three genuinely slow things are all tracked.
    assert "Invoke-Tracked" in script
    for slow in ("Add-WindowsCapability", "winget install", "npm install -g"):
        assert slow in script
    # Controlled timeout rather than an unbounded wait.
    assert "-TimeoutSeconds" in script and "Stop-Job" in script


def test_installer_reports_stages_to_the_controller_without_logging_the_code():
    script = render_setup_script(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                                 controller_url="http://c.test:8766", node_id="win1", profile="minimal")
    assert "/dashboard/api/enroll/progress" in script
    for stage in ("starting", "installing_openssh", "configuring_ssh", "registering",
                  "installing_tools", "ready", "failed"):
        assert f"Report-Stage '{stage}'" in script, stage
    # Best-effort: a controller it cannot reach must never fail the install.
    reporter = script[script.index("function Report-Stage"):]
    reporter = reporter[:reporter.index("\n}")]
    assert "try {" in reporter and "catch { }" in reporter


def test_installer_holds_the_window_open_when_there_is_something_to_read():
    script = render_setup_script(enrollment_code="TMCP-4KQ7M-2ZXWP-9BCDE",
                                 controller_url="http://c.test:8766", node_id="win1", profile="minimal")
    assert "function Wait-BeforeClosing" in script
    assert "Read-Host" in script
    # FAIL and WARN wait for the user; only the all-clear auto-closes.
    # Slice from the summary onward: "exit 1" also appears far earlier, in
    # the generic-code guard, so indexing from the start of the file finds
    # the wrong one and silently yields an empty block.
    summary = script[script.index("if ($fails.Count -gt 0) {"):]
    fail_block = summary[:summary.index("exit 1")]
    assert "Wait-BeforeClosing" in fail_block and "-Seconds" not in fail_block
    warn_block = summary[summary.index("if ($warns.Count -gt 0) {"):summary.index("exit 2")]
    assert "Wait-BeforeClosing" in warn_block and "-Seconds" not in warn_block
    assert "Wait-BeforeClosing -Seconds 20" in script
    assert "TONG KET" in script


def test_wizard_shows_live_install_progress():
    page = _nodes_page()
    assert "anPollProgress" in page and 'id="anLive"' in page
    assert "setInterval(anPollProgress, 3000)" in page, "poll every 3s while the Done screen is open"
    assert "progress_label" in page and "progress_elapsed_seconds" in page
    # Terminal states stop the poll rather than spinning forever.
    assert "clearInterval(anProgressTimer)" in page
