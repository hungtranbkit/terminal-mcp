"""Opt-in trusted-overlay ranges for NODE CONNECTIVITY (P0, 2026-09-09
Internet-connectivity audit).

Real gap this closes, found live during that audit: Tailscale is already
installed and running on this deployment's own controller host, and is
the only NAT-traversal path available today that needs no inbound port
on the node -- but a Tailscale address (100.64.0.0/10, CGNAT) is NOT
`is_private` in Python's own ipaddress module (verified live: 100.81.85.
120 -> is_private False), so `is_lan_scannable` refused it in all THREE
independent node-connectivity gates at once: the controller's LAN bind,
the LAN CIDR guard's allowlist, and the dashboard manual-add SSRF check.

The default (env unset) must stay byte-for-byte today's behavior -- that
is what the first test in each group pins.
"""
from __future__ import annotations

import ipaddress

import pytest

from terminal_mcp import network_bind, remote_connect
from terminal_mcp.lan_discovery import (
    TrustedCidrError,
    is_lan_scannable,
    is_trusted_node_address,
    trusted_vpn_cidrs,
)

TAILSCALE = "100.64.0.0/10"
TAILSCALE_IP = "100.81.85.120"


@pytest.fixture
def tailnet(monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", TAILSCALE)


@pytest.fixture(autouse=True)
def _no_ambient_override(monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_TRUSTED_VPN_CIDRS", raising=False)


# --- parsing -------------------------------------------------------------

def test_unset_env_means_no_extra_ranges():
    assert trusted_vpn_cidrs() == ()
    assert trusted_vpn_cidrs("") == ()
    assert trusted_vpn_cidrs("   ") == ()


def test_parses_declared_ranges(tailnet):
    assert trusted_vpn_cidrs() == (ipaddress.IPv4Network(TAILSCALE),)
    assert trusted_vpn_cidrs("10.8.0.0/24, 100.64.0.0/10") == (
        ipaddress.IPv4Network("10.8.0.0/24"), ipaddress.IPv4Network(TAILSCALE),
    )


@pytest.mark.parametrize("bad", ["8.8.8.0/24", "1.1.1.1/32", "0.0.0.0/0"])
def test_refuses_globally_routable_range(bad):
    """The whole point of the gate is 'never a public address' -- an
    operator override may widen it to an overlay, never to the internet."""
    with pytest.raises(TrustedCidrError):
        trusted_vpn_cidrs(bad)


@pytest.mark.parametrize("bad", ["not-a-cidr", "192.168.1.0/33", ""])
def test_refuses_malformed_entry(bad):
    if bad == "":
        assert trusted_vpn_cidrs(bad) == ()
        return
    with pytest.raises(TrustedCidrError):
        trusted_vpn_cidrs(bad)


def test_malformed_entry_raises_rather_than_being_silently_skipped():
    """Fail loudly: silently dropping one entry would leave the operator
    believing a range is trusted while the real gate stayed closed."""
    with pytest.raises(TrustedCidrError):
        trusted_vpn_cidrs("192.168.9.0/24,garbage")


# --- the predicate itself ------------------------------------------------

def test_scanner_predicate_is_untouched_by_the_override(tailnet):
    """is_lan_scannable governs ACTIVE SUBNET SCANNING and must NOT be
    widened -- a /10 shared with every other tailnet peer is exactly what
    this project must never port-sweep."""
    assert is_lan_scannable(ipaddress.IPv4Address(TAILSCALE_IP)) is False


def test_overlay_address_trusted_only_when_declared():
    addr = ipaddress.IPv4Address(TAILSCALE_IP)
    assert is_trusted_node_address(addr) is False


def test_overlay_address_trusted_once_declared(tailnet):
    assert is_trusted_node_address(ipaddress.IPv4Address(TAILSCALE_IP)) is True


@pytest.mark.parametrize("ip", ["192.168.1.5", "10.0.0.3", "172.16.4.9", "169.254.1.1"])
def test_lan_addresses_stay_trusted_with_no_override(ip):
    assert is_trusted_node_address(ipaddress.IPv4Address(ip)) is True


@pytest.mark.parametrize("ip", ["8.8.8.8", "127.0.0.1", "224.0.0.1"])
def test_public_loopback_multicast_never_trusted(ip, tailnet):
    assert is_trusted_node_address(ipaddress.IPv4Address(ip)) is False


# --- gate 1: controller LAN bind ----------------------------------------

def test_bind_refuses_overlay_address_by_default():
    with pytest.raises(network_bind.NetworkBindError):
        network_bind.resolve_lan_bind(TAILSCALE_IP)


def test_bind_accepts_overlay_address_once_declared(tailnet):
    assert network_bind.resolve_lan_bind(TAILSCALE_IP) == TAILSCALE_IP


def test_bind_still_refuses_public_address_even_with_override(tailnet):
    with pytest.raises(network_bind.NetworkBindError):
        network_bind.resolve_lan_bind("8.8.8.8")


def test_bind_unchanged_for_lan_address():
    assert network_bind.resolve_lan_bind("192.168.1.132") == "192.168.1.132"


# --- gate 2: LAN CIDR guard allowlist -----------------------------------

def test_allowlist_refuses_overlay_range_by_default():
    with pytest.raises(network_bind.NetworkBindError):
        network_bind.resolve_allowed_cidrs(TAILSCALE, "192.168.1.132")


def test_allowlist_accepts_overlay_range_once_declared(tailnet):
    assert network_bind.resolve_allowed_cidrs(TAILSCALE, TAILSCALE_IP) == (
        ipaddress.IPv4Network(TAILSCALE),
    )


def test_allowlist_auto_derives_the_declared_overlay_range(tailnet):
    """An overlay bind address is never in local_ipv4_subnets(), so the
    conventional-/24 fallback would derive 100.81.85.0/24 -- far too
    narrow, since tailnet peers are scattered across the whole /10."""
    assert network_bind.resolve_allowed_cidrs(None, TAILSCALE_IP) == (
        ipaddress.IPv4Network(TAILSCALE),
    )


def test_allowlist_still_refuses_public_range_with_override(tailnet):
    with pytest.raises(network_bind.NetworkBindError):
        network_bind.resolve_allowed_cidrs("8.8.8.0/24", TAILSCALE_IP)


def test_allowlist_empty_when_no_lan_bind():
    assert network_bind.resolve_allowed_cidrs(None, None) == ()


# --- gate 3: dashboard manual-add SSRF check ----------------------------

def test_manual_add_refuses_overlay_host_by_default():
    with pytest.raises(remote_connect.ValidationError):
        remote_connect.validate_hostname_or_ip(TAILSCALE_IP, allow_public=False)


def test_manual_add_accepts_overlay_host_once_declared(tailnet):
    assert remote_connect.validate_hostname_or_ip(TAILSCALE_IP, allow_public=False) == TAILSCALE_IP


def test_manual_add_still_refuses_public_host_with_override(tailnet):
    with pytest.raises(remote_connect.ValidationError):
        remote_connect.validate_hostname_or_ip("8.8.8.8", allow_public=False)


def test_manual_add_unchanged_for_lan_host():
    assert remote_connect.validate_hostname_or_ip("192.168.1.250", allow_public=False) == "192.168.1.250"
