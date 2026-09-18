"""Multi-address controller bind (P0, 2026-09-09): LAN + overlay VPN at
the same time.

Why this exists: with a single bind address, adopting an overlay VPN is
an all-or-nothing migration -- repointing TERMINAL_MCP_LAN_BIND at the
tailnet address cuts the heartbeat path out from under every node
already on the LAN (they push to the LAN address). Binding both lets
LAN nodes and off-LAN nodes coexist during and after the move.

The singular API is retained and must keep behaving EXACTLY as before --
that is what the compat tests here pin.
"""
from __future__ import annotations

import ipaddress

import pytest

from terminal_mcp import network_bind
from terminal_mcp.network_middleware import LanCidrGuardMiddleware

LAN = "192.168.1.132"
TS = "100.81.85.120"
TS_CIDR = "100.64.0.0/10"


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", raising=False)


@pytest.fixture
def tailnet(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", TS_CIDR)


# --- parsing -------------------------------------------------------------

def test_unset_means_loopback_only():
    assert network_bind.resolve_lan_binds(None) == ()
    assert network_bind.resolve_lan_binds("") == ()
    assert network_bind.resolve_lan_binds("  ") == ()


def test_single_address_unchanged():
    assert network_bind.resolve_lan_binds(LAN) == (LAN,)


def test_both_addresses(tailnet):
    assert network_bind.resolve_lan_binds(f"{LAN},{TS}") == (LAN, TS)


def test_whitespace_and_duplicates_collapsed(tailnet):
    assert network_bind.resolve_lan_binds(f" {LAN} , {TS} , {LAN} ") == (LAN, TS)


def test_every_address_is_validated(tailnet):
    """A public address anywhere in the list is refused -- not just the
    first one, which is the mistake a first-only implementation makes."""
    with pytest.raises(network_bind.NetworkBindError):
        network_bind.resolve_lan_binds(f"{LAN},8.8.8.8")


def test_overlay_address_still_needs_the_declared_range():
    with pytest.raises(network_bind.NetworkBindError):
        network_bind.resolve_lan_binds(f"{LAN},{TS}")


def test_singular_api_returns_first_and_is_unchanged(tailnet):
    assert network_bind.resolve_lan_bind(LAN) == LAN
    assert network_bind.resolve_lan_bind(None) is None
    assert network_bind.resolve_lan_bind(f"{LAN},{TS}") == LAN


# --- allowed CIDR derivation --------------------------------------------

def test_explicit_cidrs_cover_both(tailnet):
    result = network_bind.resolve_allowed_cidrs(f"192.168.1.0/24,{TS_CIDR}", (LAN, TS))
    assert result == (ipaddress.IPv4Network("192.168.1.0/24"), ipaddress.IPv4Network(TS_CIDR))


def test_auto_derivation_unions_across_binds(tailnet):
    """Each socket's own peers must be represented -- the guard is
    fail-closed, so a bind whose range is missing rejects everyone."""
    result = network_bind.resolve_allowed_cidrs(None, (LAN, TS))
    assert ipaddress.IPv4Network(TS_CIDR) in result
    assert any(ipaddress.IPv4Address(LAN) in net for net in result)


def test_auto_derivation_single_bind_unchanged():
    result = network_bind.resolve_allowed_cidrs(None, LAN)
    assert len(result) == 1
    assert ipaddress.IPv4Address(LAN) in result[0]


def test_no_binds_means_empty_allowlist():
    assert network_bind.resolve_allowed_cidrs(None, None) == ()
    assert network_bind.resolve_allowed_cidrs(None, ()) == ()


# --- sockets -------------------------------------------------------------

def test_loopback_always_present_and_one_socket_per_address():
    sockets = network_bind.build_listen_sockets(0, None)
    try:
        assert [s.getsockname()[0] for s in sockets] == ["127.0.0.1"]
    finally:
        for s in sockets:
            s.close()


def test_builds_a_socket_per_bind_address():
    # 127.0.0.2/127.0.0.3 stand in for two real NICs -- portable, and the
    # function under test only cares that it gets several addresses.
    sockets = network_bind.build_listen_sockets(0, ["127.0.0.2", "127.0.0.3"])
    try:
        assert [s.getsockname()[0] for s in sockets] == ["127.0.0.1", "127.0.0.2", "127.0.0.3"]
    finally:
        for s in sockets:
            s.close()


# --- the CIDR guard across several sockets ------------------------------

async def _call(mw, *, local_ip, client_ip):
    seen = {}

    async def inner(scope, receive, send):
        seen["passed"] = True

    mw.app = inner
    sent = []

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "server": (local_ip, 8766), "client": (client_ip, 5000),
             "headers": [], "method": "GET", "path": "/"}
    await mw(scope, None, send)
    return seen.get("passed", False), sent


@pytest.mark.anyio
async def test_guard_covers_every_bound_address():
    """The bug a single-address guard has: traffic arriving on the SECOND
    bound socket is treated as 'not the LAN socket' and waved through
    entirely unchecked."""
    mw = LanCidrGuardMiddleware(None, lan_bind_ip=[LAN, TS],
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    passed, sent = await _call(mw, local_ip=TS, client_ip="203.0.113.9")
    assert passed is False
    assert sent[0]["status"] == 403


@pytest.mark.anyio
async def test_allows_peer_on_its_own_socket():
    mw = LanCidrGuardMiddleware(None, lan_bind_ip=[LAN, TS],
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),
                                               ipaddress.IPv4Network(TS_CIDR)))
    for local_ip, client_ip in [(LAN, "192.168.1.250"), (TS, "100.90.1.2")]:
        passed, _ = await _call(mw, local_ip=local_ip, client_ip=client_ip)
        assert passed is True, f"{client_ip} on {local_ip} should be allowed"


@pytest.mark.anyio
async def test_loopback_still_bypasses_the_guard():
    mw = LanCidrGuardMiddleware(None, lan_bind_ip=[LAN, TS], allowed_cidrs=())
    passed, _ = await _call(mw, local_ip="127.0.0.1", client_ip="127.0.0.1")
    assert passed is True


@pytest.mark.anyio
async def test_singular_string_still_accepted_by_the_guard():
    mw = LanCidrGuardMiddleware(None, lan_bind_ip=LAN,
                                allowed_cidrs=(ipaddress.IPv4Network("192.168.1.0/24"),))
    passed, _ = await _call(mw, local_ip=LAN, client_ip="192.168.1.250")
    assert passed is True
    passed, sent = await _call(mw, local_ip=LAN, client_ip="10.1.1.1")
    assert passed is False and sent[0]["status"] == 403


# --- doctor / dashboard reporting ---------------------------------------

def test_describe_endpoints_lists_every_address(tailnet):
    out = network_bind.describe_endpoints(port=8766, lan_bind_env=f"{LAN},{TS}",
                                          cidrs_env=f"192.168.1.0/24,{TS_CIDR}")
    assert out["lans"] == [f"http://{LAN}:8766", f"http://{TS}:8766"]
    assert out["lan"] == f"http://{LAN}:8766"  # compat for existing readers


def test_describe_endpoints_single_unchanged():
    out = network_bind.describe_endpoints(port=8766, lan_bind_env=LAN, cidrs_env=None)
    assert out["lan"] == f"http://{LAN}:8766"
    assert out["lans"] == [f"http://{LAN}:8766"]
