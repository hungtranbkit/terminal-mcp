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
  1. The bind address itself is a single, specific private IPv4 address
     (validated via lan_discovery.is_lan_scannable) -- never a wildcard.
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

from .lan_discovery import is_lan_scannable, local_ipv4_subnets

_log = logging.getLogger(__name__)

LOOPBACK = "127.0.0.1"


class NetworkBindError(ValueError):
    pass


def resolve_lan_bind(raw: str | None) -> str | None:
    """`raw` is TERMINAL_MCP_LAN_BIND's raw value: unset/empty -> None
    (today's exact, unchanged loopback-only behavior); "auto" -> this
    host's own first UP/private-subnet NIC address (lan_discovery.
    local_ipv4_subnets -- the SAME real subnet detection LAN discovery
    itself uses, so "the LAN" always means the same thing across this
    project); an explicit dotted-quad -- validated private/link-local,
    never a public address (this is a BIND address, not a scan target,
    but the safety bar is the same: never let this process listen on
    something reachable from outside this LAN)."""
    if not raw or not raw.strip():
        return None
    value = raw.strip()
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
    if not is_lan_scannable(addr):
        raise NetworkBindError(
            f"TERMINAL_MCP_LAN_BIND={value!r} is not a private/link-local address -- refusing to bind a "
            "controller HTTP socket to it (this must be a LAN address, never a public one)"
        )
    return value


def resolve_allowed_cidrs(raw: str | None, lan_bind_ip: str | None) -> tuple[ipaddress.IPv4Network, ...]:
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
            if not is_lan_scannable(network.network_address):
                raise NetworkBindError(
                    f"TERMINAL_MCP_ALLOWED_NODE_CIDRS entry {part!r} is not a private/link-local range -- "
                    "refusing (this allowlist must only ever cover LAN addresses)"
                )
            networks.append(network)
        return tuple(networks)
    if lan_bind_ip is None:
        return ()
    for subnet in local_ipv4_subnets():
        if str(subnet.local_ip) == lan_bind_ip:
            return (subnet.network,)
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
    return (network,)


def build_listen_sockets(port: int, lan_bind_ip: str | None) -> list[socket.socket]:
    """Loopback ALWAYS included -- this must never regress existing
    loopback-only behavior (tunnels, local tools, tests all depend on
    it). lan_bind_ip, when given, is a SECOND, additional socket -- never
    a replacement for the loopback one."""
    sockets = []
    for host in ([LOOPBACK] if lan_bind_ip is None else [LOOPBACK, lan_bind_ip]):
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
        lan_bind_ip = resolve_lan_bind(lan_bind_env)
    except NetworkBindError as exc:
        return {"loopback": f"http://{LOOPBACK}:{port}", "lan": None, "lan_error": str(exc), "tunnel": tunnel_note}
    if lan_bind_ip is None:
        return {"loopback": f"http://{LOOPBACK}:{port}", "lan": None, "tunnel": tunnel_note}
    try:
        allowed = resolve_allowed_cidrs(cidrs_env, lan_bind_ip)
    except NetworkBindError as exc:
        return {"loopback": f"http://{LOOPBACK}:{port}", "lan": f"http://{lan_bind_ip}:{port}",
               "lan_error": str(exc), "tunnel": tunnel_note}
    return {
        "loopback": f"http://{LOOPBACK}:{port}", "lan": f"http://{lan_bind_ip}:{port}",
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
