"""network_bind.py -- controller LAN bind resolution (TERMINAL_MCP_LAN_BIND/
TERMINAL_MCP_ALLOWED_NODE_CIDRS), socket construction, firewall script
generation. network_middleware.py's LanCidrGuardMiddleware is tested
alongside it (same feature, same file, matching this project's own
convention of colocating a feature's config-resolution and enforcement
tests)."""
from __future__ import annotations

import ipaddress
import socket

import pytest

from terminal_mcp import network_bind
from terminal_mcp.network_bind import NetworkBindError
from terminal_mcp.network_middleware import LanCidrGuardMiddleware

# ---------------------------------------------------------------------------
# resolve_lan_bind
# ---------------------------------------------------------------------------


def test_unset_or_empty_stays_loopback_only():
    assert network_bind.resolve_lan_bind(None) is None
    assert network_bind.resolve_lan_bind("") is None
    assert network_bind.resolve_lan_bind("   ") is None


def test_explicit_private_ip_accepted():
    assert network_bind.resolve_lan_bind("192.168.1.132") == "192.168.1.132"


def test_explicit_public_ip_rejected():
    with pytest.raises(NetworkBindError, match="not a private/link-local address"):
        network_bind.resolve_lan_bind("8.8.8.8")


def test_invalid_ip_format_rejected():
    with pytest.raises(NetworkBindError, match="not a valid IPv4 address"):
        network_bind.resolve_lan_bind("not-an-ip")


def test_auto_uses_local_ipv4_subnets(monkeypatch):
    from terminal_mcp.lan_discovery import LocalSubnet
    fake_subnet = LocalSubnet(interface="eth0", network=ipaddress.IPv4Network("192.168.1.0/24"),
                              local_ip=ipaddress.IPv4Address("192.168.1.132"))
    monkeypatch.setattr(network_bind, "local_ipv4_subnets", lambda: [fake_subnet])
    assert network_bind.resolve_lan_bind("auto") == "192.168.1.132"


def test_auto_with_no_subnets_raises(monkeypatch):
    monkeypatch.setattr(network_bind, "local_ipv4_subnets", lambda: [])
    with pytest.raises(NetworkBindError, match="no private/link-local IPv4 subnet"):
        network_bind.resolve_lan_bind("auto")


# ---------------------------------------------------------------------------
# resolve_allowed_cidrs
# ---------------------------------------------------------------------------


def test_no_lan_bind_and_no_explicit_cidrs_means_no_allowed_cidrs():
    # Auto-derivation has nothing to derive FROM without a lan_bind_ip --
    # returns empty. (An explicitly-configured CIDR list, by contrast, is
    # still parsed/validated and returned even with no LAN bind -- honors
    # explicit operator config exactly; LanCidrGuardMiddleware itself is
    # what actually no-ops the whole feature when lan_bind_ip is None, not
    # this resolver silently discarding a value the operator set.)
    assert network_bind.resolve_allowed_cidrs(None, None) == ()


def test_explicit_cidrs_parsed_and_validated_private():
    result = network_bind.resolve_allowed_cidrs("192.168.1.0/24, 10.0.0.0/8", "192.168.1.132")
    assert result == (ipaddress.IPv4Network("192.168.1.0/24"), ipaddress.IPv4Network("10.0.0.0/8"))


def test_explicit_public_cidr_rejected():
    with pytest.raises(NetworkBindError, match="not a private/link-local range"):
        network_bind.resolve_allowed_cidrs("8.8.8.0/24", "192.168.1.132")


def test_explicit_malformed_cidr_rejected():
    with pytest.raises(NetworkBindError, match="not a valid CIDR"):
        network_bind.resolve_allowed_cidrs("not-a-cidr", "192.168.1.132")


def test_auto_derives_from_matching_subnet(monkeypatch):
    from terminal_mcp.lan_discovery import LocalSubnet
    fake_subnet = LocalSubnet(interface="eth0", network=ipaddress.IPv4Network("192.168.1.0/24"),
                              local_ip=ipaddress.IPv4Address("192.168.1.132"))
    monkeypatch.setattr(network_bind, "local_ipv4_subnets", lambda: [fake_subnet])
    result = network_bind.resolve_allowed_cidrs(None, "192.168.1.132")
    assert result == (ipaddress.IPv4Network("192.168.1.0/24"),)


def test_auto_falls_back_to_slash24_when_no_nic_matches(monkeypatch):
    monkeypatch.setattr(network_bind, "local_ipv4_subnets", lambda: [])
    result = network_bind.resolve_allowed_cidrs(None, "192.168.1.132")
    assert result == (ipaddress.IPv4Network("192.168.1.0/24"),)


# ---------------------------------------------------------------------------
# build_listen_sockets
# ---------------------------------------------------------------------------


def test_loopback_only_when_lan_bind_is_none():
    sockets = network_bind.build_listen_sockets(0, None)  # port 0 -- OS picks a free ephemeral port
    try:
        assert len(sockets) == 1
        assert sockets[0].getsockname()[0] == "127.0.0.1"
    finally:
        for s in sockets:
            s.close()


def test_loopback_plus_lan_when_lan_bind_given():
    sockets = network_bind.build_listen_sockets(0, "127.0.0.1")  # use loopback as a stand-in LAN ip for a portable test
    try:
        assert len(sockets) == 2
        hosts = {s.getsockname()[0] for s in sockets}
        assert hosts == {"127.0.0.1"}  # both bound to the same test address on different ports, still 2 real sockets
        ports = [s.getsockname()[1] for s in sockets]
        assert len(set(ports)) == 2  # OS-assigned distinct ports (port=0) -- confirms 2 independent sockets, not 1
    finally:
        for s in sockets:
            s.close()


def test_sockets_are_actually_listening_and_reusable():
    sockets = network_bind.build_listen_sockets(0, None)
    try:
        sock = sockets[0]
        # A real client can connect -- proves listen() was actually called,
        # not just bind().
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.settimeout(2)
        client.connect(sock.getsockname())
        client.close()
    finally:
        for s in sockets:
            s.close()


# ---------------------------------------------------------------------------
# firewall_script
# ---------------------------------------------------------------------------


def test_firewall_script_covers_every_allowed_cidr_and_ends_with_default_deny():
    script = network_bind.firewall_script(
        lan_bind_ip="192.168.1.132", port=8766,
        allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"), ipaddress.IPv4Network("10.0.0.0/8")),
    )
    assert "sudo ufw allow proto tcp from 192.168.1.0/24 to 192.168.1.132 port 8766" in script
    assert "sudo ufw allow proto tcp from 10.0.0.0/8 to 192.168.1.132 port 8766" in script
    lines = [line for line in script.splitlines() if line.startswith("sudo ufw")]
    assert lines[-1].startswith("sudo ufw deny")  # default-deny always comes last


# ---------------------------------------------------------------------------
# LanCidrGuardMiddleware
# ---------------------------------------------------------------------------


class _RecordingApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _scope(server_ip: str, client_ip: str | None) -> dict:
    return {
        "type": "http", "server": (server_ip, 8766),
        "client": (client_ip, 12345) if client_ip else None,
        "headers": [], "path": "/", "method": "GET",
    }


async def _run(app, scope):
    sent = []
    async def receive():
        return {"type": "http.disconnect"}
    async def send(message):
        sent.append(message)
    await app(scope, receive, send)
    return sent


@pytest.mark.anyio
async def test_disabled_when_lan_bind_ip_is_none():
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip=None, allowed_cidrs=())
    await _run(mw, _scope("127.0.0.1", "203.0.113.5"))
    assert inner.called is True  # never guards anything when the feature is off


@pytest.mark.anyio
async def test_loopback_socket_requests_pass_through_untouched():
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip="192.168.1.132",
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    # arrived on the LOOPBACK socket even though a LAN bind is configured --
    # this guard only ever governs the LAN socket specifically.
    await _run(mw, _scope("127.0.0.1", "8.8.8.8"))
    assert inner.called is True


@pytest.mark.anyio
async def test_lan_socket_request_from_allowed_cidr_passes():
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip="192.168.1.132",
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    await _run(mw, _scope("192.168.1.132", "192.168.1.250"))
    assert inner.called is True


@pytest.mark.anyio
async def test_lan_socket_request_from_outside_cidr_rejected_403():
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip="192.168.1.132",
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    sent = await _run(mw, _scope("192.168.1.132", "8.8.8.8"))
    assert inner.called is False
    assert sent[0]["status"] == 403


@pytest.mark.anyio
async def test_lan_socket_empty_allowlist_fails_closed_never_open():
    # A detection failure (network_bind.resolve_allowed_cidrs returning
    # ()) must never become "allow everyone" -- fail closed.
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip="192.168.1.132", allowed_cidrs=())
    sent = await _run(mw, _scope("192.168.1.132", "192.168.1.250"))
    assert inner.called is False
    assert sent[0]["status"] == 403


@pytest.mark.anyio
async def test_missing_client_address_rejected():
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip="192.168.1.132",
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    sent = await _run(mw, _scope("192.168.1.132", None))
    assert inner.called is False
    assert sent[0]["status"] == 403


@pytest.mark.anyio
async def test_non_http_scope_passes_through():
    inner = _RecordingApp()
    mw = LanCidrGuardMiddleware(inner, lan_bind_ip="192.168.1.132",
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    sent = []
    async def receive():
        return {}
    async def send(message):
        sent.append(message)
    await mw({"type": "lifespan"}, receive, send)
    assert inner.called is True


@pytest.fixture
def anyio_backend():
    return "asyncio"
