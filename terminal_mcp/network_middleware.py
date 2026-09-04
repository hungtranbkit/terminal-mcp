"""LanCidrGuardMiddleware -- the application-layer half of network_bind.py's
two-layer LAN-socket protection (see that module's own docstring for the
full rationale). Runs BEFORE routing, on every request, for every
socket -- a no-op for anything that arrived on the loopback socket
(scope["server"][0] == "127.0.0.1"); for the LAN socket specifically,
rejects (403) any request whose CLIENT address isn't inside the
configured/derived private CIDR allowlist.

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

_log = logging.getLogger(__name__)


class LanCidrGuardMiddleware:
    def __init__(self, app, *, lan_bind_ip: str | None, allowed_cidrs: tuple[ipaddress.IPv4Network, ...]) -> None:
        self.app = app
        self.lan_bind_ip = lan_bind_ip
        self.allowed_cidrs = allowed_cidrs

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or self.lan_bind_ip is None:
            await self.app(scope, receive, send)
            return
        server = scope.get("server")
        local_ip = server[0] if server else None
        if local_ip != self.lan_bind_ip:
            # Arrived on the loopback socket (or anything else) -- this
            # guard only ever governs the LAN socket specifically.
            await self.app(scope, receive, send)
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
