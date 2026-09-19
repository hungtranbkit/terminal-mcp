"""Which paths a LAN/overlay socket may serve at all (P0, 2026-09-19).

THE HOLE THIS CLOSES
--------------------
`TERMINAL_MCP_LAN_BIND` opens a SECOND socket on this host's LAN/overlay
address so a worker node's own agent can push its heartbeat directly --
that, and only that, is the documented reason it exists (see
network_bind.py and docs/multi-node.md's "Controller LAN bind").

But the socket served the WHOLE application. `LanCidrGuardMiddleware`
checked that the client's source IP was inside
`TERMINAL_MCP_ALLOWED_NODE_CIDRS` and then passed the request to the
ordinary router -- including `/mcp`, the full MCP tool surface.

Reproduced live on the hp-linux controller (2026-09-19), which binds
`100.67.53.117` with `ALLOWED_NODE_CIDRS=100.64.0.0/10`:

    POST http://100.67.53.117:8766/mcp   Host: 127.0.0.1:8766
      -> 200, a working MCP session, `tools/list` returning all 293 tools
         including terminal_create_session, terminal_kill_session,
         terminal_delete_session and supervisor2_execute_send.

No bearer token, no Cloudflare Access assertion, no webauth cookie --
membership of a /10 was the entire authorization story for full remote
terminal control. The `Host` header check that superficially appeared to
block this (421 "Invalid Host header") is DNS-rebinding protection, not
authentication: a client that sets the header itself walks straight
through it, which is what the probe above did.

The CIDR is also far wider than the operator's actual fleet:
100.64.0.0/10 is the whole RFC 6598 CGNAT range Tailscale allocates
from, not "my four nodes".

THE RULE
--------
A LAN/overlay socket is DEFAULT-DENY and serves only the machine-facing
routes it exists for. Every path on this list carries its OWN credential
check, so the CIDR is defense in depth and never the only thing standing
between a caller and the action:

  nodes/{id}/heartbeat        per-node bearer token (_verify_node_token)
  nodes/{id}/agent-bundle     same per-node bearer token
  nodes/{id}/token/refresh    same per-node bearer token
  nodes/{id}/deregister       same per-node bearer token
  enroll/consume|redeem       single-use enrollment code, rate limited
  enroll/progress             the enrollment code the installer holds

That list is deliberately NOT "everything machine-ish". The operator/
browser surface -- `/dashboard*` pages and their read APIs, the helper
artifact downloads, and `/mcp` above all -- stays off the LAN socket
entirely and keeps reaching this controller the way it always has: over
loopback, through the authenticated tunnel. Nothing about the loopback
socket changes, so the tunnel path and local clients are untouched.

WHY A PATH ALLOWLIST AND NOT "REQUIRE AUTH ON /mcp"
---------------------------------------------------
Bolting an auth check onto `/mcp` would mean inventing a second
credential system for the MCP surface and teaching every existing local
client to present it -- a breaking change to the one path that is
already safe, to fix exposure on a path that should never have been
served here at all. Removing the route from the socket is both smaller
and strictly safer: it cannot be misconfigured into openness, and a
caller who should reach `/mcp` still can, over the socket that was
always intended for it.

Matching is on the ROUTED path and method, with `{node_id}` matched as a
single non-empty segment -- never a prefix/`startswith` test, which
`/dashboard/api/nodes/x/heartbeat/../../..` style inputs make unsafe.
"""
from __future__ import annotations

import logging
import os
import re

_log = logging.getLogger(__name__)

# One segment: no slash, non-empty. `{node_id}` is operator-chosen and can
# contain dots/hyphens, but never a path separator.
_SEGMENT = r"[^/]+"


def _compile(pattern: str) -> re.Pattern[str]:
    """`/a/{x}/b` -> an anchored regex matching exactly one segment for {x}."""
    parts = re.split(r"(\{[^}]*\})", pattern)
    out = []
    for part in parts:
        out.append(_SEGMENT if part.startswith("{") and part.endswith("}")
                   else re.escape(part))
    return re.compile("^" + "".join(out) + "$")


# (path pattern, allowed methods). HEAD rides with GET because Starlette
# answers a HEAD on a GET route and the agent-bundle client uses it to
# check for a new bundle without downloading one.
_ALLOWED: tuple[tuple[str, frozenset[str]], ...] = (
    ("/dashboard/api/nodes/{node_id}/heartbeat", frozenset({"POST"})),
    ("/dashboard/api/nodes/{node_id}/agent-bundle", frozenset({"GET", "HEAD"})),
    ("/dashboard/api/nodes/{node_id}/token/refresh", frozenset({"POST"})),
    ("/dashboard/api/nodes/{node_id}/deregister", frozenset({"POST"})),
    ("/dashboard/api/enroll/consume", frozenset({"POST"})),
    ("/dashboard/api/enroll/redeem", frozenset({"POST"})),
    ("/dashboard/api/enroll/progress", frozenset({"GET", "HEAD", "POST"})),
)

_COMPILED: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = tuple(
    (_compile(path), methods) for path, methods in _ALLOWED)

# Escape hatch for a deployment with a machine-facing route this list does
# not know about. Comma-separated paths, same `{seg}` syntax. It can only
# ADD paths, never remove one, and a malformed entry is dropped with a
# warning rather than widening the policy -- fail closed, like
# network_bind.resolve_allowed_cidrs.
EXTRA_PATHS_ENV = "TERMINAL_MCP_LAN_EXTRA_PATHS"


def _extra_patterns() -> tuple[tuple[re.Pattern[str], frozenset[str]], ...]:
    raw = (os.environ.get(EXTRA_PATHS_ENV) or "").strip()
    if not raw:
        return ()
    out = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        if not candidate.startswith("/"):
            _log.warning("%s: ignoring %r -- a path must start with '/'",
                         EXTRA_PATHS_ENV, candidate)
            continue
        try:
            out.append((_compile(candidate), frozenset({"GET", "HEAD", "POST"})))
        except re.error:
            _log.warning("%s: ignoring %r -- not a usable path pattern",
                         EXTRA_PATHS_ENV, candidate)
    return tuple(out)


def describe_policy() -> dict:
    """What the LAN socket serves, for the doctor output and the docs, so
    the rule is readable from one place rather than inferred."""
    return {
        "posture": "default-deny: a LAN/overlay socket serves only the "
                   "machine-facing node routes below",
        "allowed": [{"path": path, "methods": sorted(methods)}
                    for path, methods in _ALLOWED],
        "extra_paths_env": EXTRA_PATHS_ENV,
        "every_allowed_route_also_requires": "its own per-node bearer token "
                                             "or single-use enrollment code",
        "loopback": "unaffected -- serves the full application, including /mcp",
    }


def is_allowed_on_lan(path: str, method: str) -> bool:
    """May a LAN/overlay socket serve this request at all?

    Answers only the socket question. The route's own credential check
    still runs afterwards and is what actually authorizes the caller.
    """
    if not path:
        return False
    # Starlette hands us the already-normalised path; strip a trailing
    # slash so `/heartbeat/` and `/heartbeat` are the same route, which is
    # how the router itself treats them.
    candidate = path.rstrip("/") or "/"
    verb = (method or "").upper()
    for pattern, methods in _COMPILED + _extra_patterns():
        if verb in methods and pattern.match(candidate):
            return True
    return False
