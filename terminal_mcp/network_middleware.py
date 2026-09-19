"""LanCidrGuardMiddleware -- the application-layer half of network_bind.py's
two-layer LAN-socket protection (see that module's own docstring for the
full rationale). Runs BEFORE routing, on every request, for every
socket -- a no-op for anything that arrived on the loopback socket
(scope["server"][0] == "127.0.0.1"); for the LAN socket specifically,
rejects (403) any request that is not one of the machine-facing node
routes that socket exists to serve (lan_route_policy.py), and then any
request whose CLIENT address isn't inside the configured/derived
private CIDR allowlist.

The route gate is the P0 half (2026-09-19). Source-IP membership of
ALLOWED_NODE_CIDRS is a statement about the network a caller sits on,
not a grant of authority, but this middleware previously passed every
CIDR-matching request straight to the router -- so `/mcp`, the whole
MCP tool surface including session create/kill/send-input, was
reachable with no credential at all from anywhere inside the allowed
range. lan_route_policy.py has the live reproduction; the fix is that
a LAN/overlay socket now serves node heartbeat/enrollment only, each
of which carries its own bearer token or single-use enrollment code.

This is deliberately NOT a replacement for the OS firewall
(network_bind.firewall_script) -- it protects THIS process even when no
firewall rule exists yet (a real, disclosed possibility on a host this
project has no sudo on), but a genuinely hostile actor already on the
LAN segment itself is exactly what a real firewall (blocking the port
entirely except from known node IPs) protects against and this
application-level check alone does not fully replace. Both together is
the intended posture.
"""
from __future__ import annotations

import ipaddress
import logging

from starlette.responses import PlainTextResponse

from . import lan_route_policy

_log = logging.getLogger(__name__)


class LanCidrGuardMiddleware:
    def __init__(self, app, *, lan_bind_ip, allowed_cidrs: tuple[ipaddress.IPv4Network, ...]) -> None:
        self.app = app
        # Accepts the singular `str | None` shape it has always accepted,
        # or a sequence -- the controller can bind its LAN address and its
        # overlay-VPN address at the same time, and EVERY such socket must
        # be guarded, not just the first one.
        if lan_bind_ip is None:
            self.lan_bind_ips: frozenset[str] = frozenset()
        elif isinstance(lan_bind_ip, str):
            self.lan_bind_ips = frozenset({lan_bind_ip})
        else:
            self.lan_bind_ips = frozenset(lan_bind_ip)
        self.lan_bind_ip = lan_bind_ip
        self.allowed_cidrs = allowed_cidrs

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.lan_bind_ips:
            await self.app(scope, receive, send)
            return
        server = scope.get("server")
        local_ip = server[0] if server else None
        if local_ip not in self.lan_bind_ips:
            # Arrived on the loopback socket (or anything else) -- this
            # guard only ever governs the LAN/overlay sockets specifically.
            await self.app(scope, receive, send)
            return
        # ROUTE GATE FIRST (P0, 2026-09-19). The CIDR check below answers
        # "is this caller on my network"; it was never an answer to "may
        # this caller do this", and treating it as one is what left the
        # entire /mcp tool surface -- session create/kill/send-input --
        # open to anything inside ALLOWED_NODE_CIDRS. See
        # lan_route_policy.py for the reproduction and the rule. Checked
        # before the CIDR so a refusal here is logged as what it is (a
        # path that does not belong on this socket) rather than as a
        # rejected source address.
        path = scope.get("path") or ""
        method = scope.get("method") or ""
        if not lan_route_policy.is_allowed_on_lan(path, method):
            _log.warning("network_middleware: refused %s %s on LAN socket %s from %s -- "
                         "this socket serves node heartbeat/enrollment routes only",
                         method, path, local_ip,
                         (scope.get("client") or ("?",))[0])
            response = PlainTextResponse(
                "Forbidden: this address serves node heartbeat and enrollment routes only. "
                "Everything else must arrive over loopback (the authenticated tunnel).",
                status_code=403)
            await response(scope, receive, send)
            return
        client = scope.get("client")
        client_ip = client[0] if client else None
        allowed = False
        if client_ip is not None:
            try:
                allowed = any(ipaddress.IPv4Address(client_ip) in network for network in self.allowed_cidrs)
            except ValueError:
                allowed = False
        if not allowed:
            # Fail closed -- an empty allowlist (network_bind.
            # resolve_allowed_cidrs's own documented fail-closed
            # behavior) or an unparseable client address both land here,
            # never an implicit allow.
            _log.warning("network_middleware: rejected LAN request from %s (not in allowed CIDRs %s)",
                        client_ip, [str(n) for n in self.allowed_cidrs])
            response = PlainTextResponse("Forbidden: source address not in the allowed LAN range", status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
