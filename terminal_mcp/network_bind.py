"""Controller HTTP server binding -- loopback ALWAYS, plus an optional,
explicit LAN address so a worker node's own node-agent (Windows or
Linux) can reach this controller's heartbeat-receiving route directly on
the local network. `server_http.py`'s own module docstring/HTTP_HOST
comment already documents WHY loopback-only was the original, deliberate
default ("remote access must go through an authenticated HTTPS tunnel");
this module is the explicit, config-driven, narrowly-scoped exception to
that for multi-node LAN worker nodes specifically -- never 0.0.0.0,
never enabled unless an operator opts in.

Two independent layers of protection for the LAN socket, so this stays
safe even on a host where this project has no permission to touch the OS
firewall (a real, disclosed possibility -- see firewall_script's own
docstring):
  1. The bind address itself is a single, specific non-public IPv4
     address (validated via lan_discovery.is_trusted_node_address: a
     private/link-local one, or one inside an operator-declared
     TERMINAL_MCP_TRUSTED_VPN_CIDRS overlay range) -- never a wildcard,
     never a globally-routable address.
  2. LanCidrGuardMiddleware (network_middleware.py) rejects, at the
     application layer, any connection that arrived on that LAN socket
     from an IP outside the configured/derived private CIDR allowlist --
     enforced by this process itself, independent of whatever the OS
     firewall does or doesn't do.

Every existing Cloudflare-Access-gated dashboard route
(_mutation_guard/_read_guard) is COMPLETELY UNCHANGED by any of this --
that check happens at the application layer regardless of which socket a
request arrived on, so opening the LAN socket does not weaken it at all.
What newly becomes LAN-reachable is exactly what was already meant to be
network-reachable for multi-node to work (node_heartbeat, itself already
bearer-token authenticated) plus the already-unauthenticated health/
version endpoints (low-sensitivity liveness info) -- nothing that was
previously protected by network topology alone loses that protection.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Sequence

from .lan_discovery import is_trusted_node_address, local_ipv4_subnets, trusted_vpn_cidrs

_log = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"


class NetworkBindError(ValueError):
    pass


def _resolve_one_bind(value: str) -> str:
    """Validate ONE bind address (the body resolve_lan_bind always had,
    lifted out unchanged so the plural entry point below and the
    singular one can never drift apart in what they accept)."""
    if value.lower() == "auto":
        subnets = local_ipv4_subnets()
        if not subnets:
            raise NetworkBindError(
                "TERMINAL_MCP_LAN_BIND=auto but no private/link-local IPv4 subnet was found on any UP NIC -- "
                "set an explicit IP instead, or unset this to stay loopback-only"
            )
        return str(subnets[0].local_ip)
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise NetworkBindError(f"TERMINAL_MCP_LAN_BIND={value!r} is not a valid IPv4 address") from exc
    if not is_trusted_node_address(addr):
        raise NetworkBindError(
            f"TERMINAL_MCP_LAN_BIND={value!r} is not a private/link-local address, and is not covered by "
            "TERMINAL_MCP_TRUSTED_VPN_CIDRS -- refusing to bind a controller HTTP socket to it (this must "
            "be a LAN or declared-overlay address, never a public one). To bind this controller's own "
            "overlay-VPN address (e.g. a Tailscale 100.64.0.0/10 one) so remote nodes can reach it without "
            "any inbound port-forward, declare that range in TERMINAL_MCP_TRUSTED_VPN_CIDRS first."
        )
    return value


def resolve_lan_binds(raw: str | None) -> tuple[str, ...]:
    """TERMINAL_MCP_LAN_BIND, plural form: a COMMA-SEPARATED list, so this
    controller can be reachable on more than one network at once -- the
    real case this exists for is "LAN + overlay VPN simultaneously"
    (192.168.1.132 for the nodes already on this LAN, plus this host's
    own Tailscale 100.x address for nodes on other internet connections),
    which otherwise forces an all-or-nothing migration: repointing the
    single bind at the tailnet would cut the heartbeat path out from
    under every existing LAN node at once.

    Loopback is NEVER listed here -- build_listen_sockets always adds it,
    unchanged. Duplicates are collapsed, order preserved. Unset/empty ->
    () (today's exact loopback-only behaviour)."""
    if not raw or not raw.strip():
        return ()
    binds: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        resolved = _resolve_one_bind(part)
        if resolved not in binds:
            binds.append(resolved)
    return tuple(binds)


def resolve_lan_bind(raw: str | None) -> str | None:

    """Singular view of resolve_lan_binds: the FIRST configured bind, or
    None. Retained because `describe_endpoints`/doctor/dashboard and this
    module's own long-standing tests all speak in terms of "the" LAN bind;
    anything that actually opens sockets or builds the CIDR guard must use
    the plural form, or it will silently ignore every address after the
    first. Same validation either way -- see _resolve_one_bind."""
    binds = resolve_lan_binds(raw)
    return binds[0] if binds else None


def _as_bind_tuple(lan_bind_ip: str | None | Sequence[str]) -> tuple[str, ...]:
    """Every public entry point here accepts EITHER the singular
    `str | None` shape it has always accepted, OR a sequence of bind
    addresses -- so adding multi-bind support changed no existing caller
    or test, and a caller that was passing one string keeps working
    byte-for-byte."""
    if lan_bind_ip is None:
        return ()
    if isinstance(lan_bind_ip, str):
        return (lan_bind_ip,)
    return tuple(lan_bind_ip)


def _derive_cidr_for_bind(lan_bind_ip: str) -> ipaddress.IPv4Network:
    """Auto-derivation for ONE bind address -- its own NIC subnet, else
    the declared overlay range containing it, else its conventional /24."""
    for subnet in local_ipv4_subnets():
        if str(subnet.local_ip) == lan_bind_ip:
            return subnet.network
    # An overlay-VPN bind address is never one of local_ipv4_subnets()'s
    # own results (that helper deliberately filters to LAN-scannable
    # ranges only), so the /24 fallback below would derive a range far
    # too narrow to contain the tailnet's other peers -- a Tailscale
    # peer is anywhere in 100.64.0.0/10, not in the bind address's own
    # /24. Derive the operator's own declared range instead, which is
    # exactly the set of peers they said they trust.
    bind_addr = ipaddress.IPv4Address(lan_bind_ip)
    for network in trusted_vpn_cidrs():
        if bind_addr in network:
            _log.info("network_bind: %s is inside declared trusted overlay range %s -- using it as the "
                     "allowed-source CIDR list", lan_bind_ip, network)
            return network
    # lan_bind_ip was given explicitly (not "auto") and doesn't match any
    # currently-UP NIC's own subnet (e.g. configured ahead of the NIC
    # coming up, or a static/manually-assigned address ip addr wouldn't
    # necessarily surface the same way) -- fall back to this address's
    # own conventional /24, still verified private by resolve_lan_bind
    # already having accepted it. Never wider than /24 by inference alone.
    network = ipaddress.IPv4Network(f"{lan_bind_ip}/24", strict=False)
    _log.warning("network_bind: could not find %s among this host's own UP NIC subnets -- "
                "derived allowlist %s from its conventional /24 instead; set "
                "TERMINAL_MCP_ALLOWED_NODE_CIDRS explicitly if this is wrong", lan_bind_ip, network)
    return network


def resolve_allowed_cidrs(raw: str | None,
                          lan_bind_ip: str | None | Sequence[str]) -> tuple[ipaddress.IPv4Network, ...]:
    """`raw` is TERMINAL_MCP_ALLOWED_NODE_CIDRS's raw value (comma-
    separated CIDRs). Explicit value: every entry must itself be a
    private/link-local range (rejects a typo'd public CIDR outright,
    same posture as everything else in this module) -- returned as-is.
    Empty/unset: auto-derived as the single subnet lan_bind_ip itself
    belongs to (so the common case -- "just let nodes on my own LAN
    segment in" -- needs zero extra configuration beyond LAN_BIND=auto).
    Returns an EMPTY tuple only when lan_bind_ip is None (nothing to
    guard) -- LanCidrGuardMiddleware treats a configured LAN bind with an
    empty allowlist as fail-closed (reject everything), never fail-open,
    so a detection failure here can never silently become "allow any
    source"."""
    if raw and raw.strip():
        networks = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                network = ipaddress.IPv4Network(part, strict=False)
            except ValueError as exc:
                raise NetworkBindError(f"TERMINAL_MCP_ALLOWED_NODE_CIDRS entry {part!r} is not a valid CIDR") from exc
            if not is_trusted_node_address(network.network_address):
                raise NetworkBindError(
                    f"TERMINAL_MCP_ALLOWED_NODE_CIDRS entry {part!r} is not a private/link-local range and "
                    "is not covered by TERMINAL_MCP_TRUSTED_VPN_CIDRS -- refusing (this allowlist must only "
                    "ever cover LAN or declared-overlay addresses)"
                )
            networks.append(network)
        return tuple(networks)
    binds = _as_bind_tuple(lan_bind_ip)
    if not binds:
        return ()
    # UNION across every bind address -- with LAN + overlay bound at once,
    # each socket's own peers must be represented or that socket is
    # fail-closed for everyone (LanCidrGuardMiddleware rejects an empty/
    # non-matching allowlist rather than falling open).
    derived: list[ipaddress.IPv4Network] = []
    for bind in binds:
        network = _derive_cidr_for_bind(bind)
        if network not in derived:
            derived.append(network)
    return tuple(derived)


def build_listen_sockets(port: int, lan_bind_ip: str | None | Sequence[str]) -> list[socket.socket]:
    """Loopback ALWAYS included -- this must never regress existing
    loopback-only behavior (tunnels, local tools, tests all depend on
    it). lan_bind_ip, when given, adds one ADDITIONAL socket per address
    -- never a replacement for the loopback one. Accepts a single address
    (unchanged) or several, so the controller can serve its LAN and its
    overlay-VPN address at the same time."""
    sockets = []
    for host in [LOOPBACK, *_as_bind_tuple(lan_bind_ip)]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(2048)
        sock.setblocking(False)
        sockets.append(sock)
    return sockets


def describe_endpoints(*, port: int, lan_bind_env: str | None, cidrs_env: str | None,
                       tunnel_note: str = "OpenAI Secure MCP Tunnel + Cloudflare Access dashboard tunnel "
                                          "(see terminal-mcp-doctor connection)") -> dict:
    """One place both `terminal-mcp-doctor connection` and the dashboard's
    own /dashboard/api/connection-health route pull this from (task item
    7: "Doctor/dashboard phải hiển thị rõ controller endpoints") -- reads
    the SAME two env vars server_http.py itself reads at startup, so this
    always reflects what the RUNNING process actually resolved, never a
    second, independently-drifting guess. `firewall_verified` is always
    False: this process has no way to introspect the real OS firewall
    state (querying `ufw status` itself needs root, same as applying a
    rule does) -- reported as an honest, standing reminder while a LAN
    bind is active, never a false "verified" claim."""
    try:
        lan_binds = resolve_lan_binds(lan_bind_env)
    except NetworkBindError as exc:
        return {"loopback": f"http://{LOOPBACK}:{port}", "lan": None, "lan_error": str(exc), "tunnel": tunnel_note}
    if not lan_binds:
        return {"loopback": f"http://{LOOPBACK}:{port}", "lan": None, "tunnel": tunnel_note}
    lan_urls = [f"http://{ip}:{port}" for ip in lan_binds]
    try:
        allowed = resolve_allowed_cidrs(cidrs_env, lan_binds)
    except NetworkBindError as exc:
        return {"loopback": f"http://{LOOPBACK}:{port}", "lan": lan_urls[0], "lans": lan_urls,
               "lan_error": str(exc), "tunnel": tunnel_note}
    return {
        # "lan" stays a single string for every existing reader; "lans"
        # is the full list once more than one address is bound.
        "loopback": f"http://{LOOPBACK}:{port}", "lan": lan_urls[0], "lans": lan_urls,
        "allowed_cidrs": [str(c) for c in allowed], "firewall_verified": False,
        "firewall_reminder": "This process enforces the allowed_cidrs list itself (LanCidrGuardMiddleware), "
                             "but has no way to confirm an OS firewall rule also restricts this port -- run "
                             "network_bind.firewall_script's output yourself if you haven't already.",
        "tunnel": tunnel_note,
    }


def firewall_script(*, lan_bind_ip: str, port: int, allowed_cidrs: tuple[ipaddress.IPv4Network, ...]) -> str:
    """A ufw script covering exactly the LAN bind above -- this project
    has no permission to run this itself on a real deployment (no
    passwordless sudo, confirmed live rather than assumed -- see this
    feature's own task report for that check), so it is generated for
    the operator to review and run manually, never applied silently.
    `terminal-mcp-doctor connection` prints this same guidance whenever
    a LAN bind is configured -- see doctor.py."""
    lines = [
        "#!/bin/sh",
        "# Generated by terminal-mcp (network_bind.firewall_script) -- review before running.",
        "# Restricts inbound access to the controller's LAN-bound port to the",
        "# private CIDR ranges this deployment's own node agents are expected on.",
        "# Application-level LanCidrGuardMiddleware already enforces the SAME",
        "# allowlist independent of this script -- this is defense in depth",
        "# (a second, OS-level layer), not the only thing standing between an",
        "# open LAN port and this controller.",
        "set -e",
    ]
    for cidr in allowed_cidrs:
        lines.append(f"sudo ufw allow proto tcp from {cidr} to {lan_bind_ip} port {port} comment 'terminal-mcp LAN node'")
    lines.append(f"sudo ufw deny proto tcp to {lan_bind_ip} port {port} comment 'terminal-mcp LAN node (default deny)'")
    return "\n".join(lines) + "\n"
