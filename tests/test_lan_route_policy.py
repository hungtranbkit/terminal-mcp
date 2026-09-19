"""P0 (2026-09-19): source-IP membership of ALLOWED_NODE_CIDRS is not a
grant of authority.

The live exposure these tests pin, reproduced against the hp-linux
controller (bound 100.67.53.117, ALLOWED_NODE_CIDRS=100.64.0.0/10):

    POST http://100.67.53.117:8766/mcp   Host: 127.0.0.1:8766
      -> 200, a usable MCP session, tools/list returning all 293 tools
         including terminal_create_session / terminal_kill_session /
         supervisor2_execute_send.

No token, no Access assertion, no cookie. The two properties that must
hold forever after are the two halves of this file:

  * a client INSIDE the allowed CIDR still cannot reach /mcp (or any
    other operator surface) on the LAN socket, and
  * the node heartbeat -- the only reason that socket is open -- still
    works for exactly that same client.

Both are asserted through the real middleware with a real ASGI send, not
against the policy table alone: the bug was never in the table, it was
that nothing consulted one.
"""
from __future__ import annotations

import ipaddress

import pytest

from terminal_mcp import lan_route_policy
from terminal_mcp.network_middleware import LanCidrGuardMiddleware

TS_BIND = "100.67.53.117"
TS_CIDR = ipaddress.ip_network("100.64.0.0/10")
NODE_IP = "100.81.85.120"        # a real tailnet peer: inside the allowlist
OUTSIDE_IP = "203.0.113.7"       # public, outside it


@pytest.fixture
def anyio_backend():
    """asyncio only -- this middleware has no trio-specific behaviour and
    the rest of the suite pins the same single backend."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _no_ambient_extra(monkeypatch):
    monkeypatch.delenv(lan_route_policy.EXTRA_PATHS_ENV, raising=False)


class _Recorder:
    """Minimal ASGI app + send pair: records whether the request reached
    the application at all, and what status the middleware sent if not."""

    def __init__(self) -> None:
        self.reached = False
        self.status: int | None = None

    async def app(self, scope, receive, send):
        self.reached = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]


async def _call(path, method, *, client_ip=NODE_IP, local_ip=TS_BIND):
    recorder = _Recorder()
    middleware = LanCidrGuardMiddleware(
        recorder.app, lan_bind_ip=(TS_BIND,), allowed_cidrs=(TS_CIDR,))
    scope = {"type": "http", "path": path, "method": method,
             "server": (local_ip, 8766), "client": (client_ip, 51234)}
    await middleware(scope, None, recorder.send)
    return recorder


# --- the exposure itself -------------------------------------------------

@pytest.mark.anyio
async def test_mcp_is_refused_on_lan_socket_from_an_allowed_cidr_client():
    """THE regression. The client is inside ALLOWED_NODE_CIDRS -- the exact
    situation the old code treated as authorization -- and must still be
    refused, without the request ever reaching the MCP app."""
    recorder = await _call("/mcp", "POST")
    assert recorder.status == 403
    assert not recorder.reached, "/mcp must never be routed from a LAN socket"


@pytest.mark.anyio
async def test_mcp_get_and_delete_also_refused():
    """The MCP streamable transport uses GET (SSE) and DELETE (session
    teardown) as well as POST -- refusing only POST would leave the
    session channel open."""
    for method in ("GET", "DELETE", "POST"):
        recorder = await _call("/mcp", method)
        assert recorder.status == 403, method
        assert not recorder.reached, method


@pytest.mark.anyio
@pytest.mark.parametrize("path", [
    "/dashboard",
    "/dashboard/sessions",
    "/dashboard/nodes",
    "/dashboard/api/sessions",
    "/dashboard/api/nodes",
    "/dashboard/api/audit",
    "/dashboard/api/nodes/onboard/helper/windows-x64",
    "/dashboard/api/nodes/hp-linux/token/rotate",
    "/dashboard/api/nodes/hp-linux/token/revoke",
])
async def test_operator_surface_is_not_on_the_lan_socket(path):
    """Everything a human or the tunnel reaches stays loopback-only. The
    token rotate/revoke pair matters most: those mint and destroy the very
    credentials the heartbeat route checks."""
    recorder = await _call(path, "GET")
    assert recorder.status == 403
    assert not recorder.reached


# --- what the socket exists for, still working ---------------------------

@pytest.mark.anyio
async def test_node_heartbeat_still_reaches_the_app():
    """The whole point of the LAN bind. Refusing this would silently take
    every remote node offline, which is the failure mode the fix must not
    trade for the one it closes."""
    recorder = await _call("/dashboard/api/nodes/dell-linux/heartbeat", "POST")
    assert recorder.reached
    assert recorder.status == 200


@pytest.mark.anyio
@pytest.mark.parametrize("path,method", [
    ("/dashboard/api/nodes/dell-5530/heartbeat", "POST"),
    ("/dashboard/api/nodes/m910/agent-bundle", "GET"),
    ("/dashboard/api/nodes/m910/agent-bundle", "HEAD"),
    ("/dashboard/api/nodes/dell-linux/token/refresh", "POST"),
    ("/dashboard/api/nodes/dell-linux/deregister", "POST"),
    ("/dashboard/api/enroll/consume", "POST"),
    ("/dashboard/api/enroll/redeem", "POST"),
    ("/dashboard/api/enroll/progress", "GET"),
])
async def test_machine_facing_node_routes_pass(path, method):
    recorder = await _call(path, method)
    assert recorder.reached, f"{method} {path} must stay reachable for nodes"


@pytest.mark.anyio
async def test_heartbeat_on_a_wrong_method_is_refused():
    """A GET on the heartbeat path is not the heartbeat; the allowlist is
    method-aware so it cannot be used as a generic way onto the socket."""
    recorder = await _call("/dashboard/api/nodes/dell-linux/heartbeat", "GET")
    assert recorder.status == 403
    assert not recorder.reached


# --- the CIDR check is still there, underneath ---------------------------

@pytest.mark.anyio
async def test_cidr_check_still_applies_to_an_allowed_route():
    """The route gate ADDS to the CIDR check rather than replacing it: an
    off-tailnet client hitting the heartbeat route is still refused."""
    recorder = await _call("/dashboard/api/nodes/dell-linux/heartbeat", "POST",
                           client_ip=OUTSIDE_IP)
    assert recorder.status == 403
    assert not recorder.reached


@pytest.mark.anyio
async def test_loopback_socket_is_completely_unaffected():
    """/mcp over loopback is the intended path (the tunnel terminates
    there) and must behave exactly as before this change."""
    recorder = await _call("/mcp", "POST", client_ip="127.0.0.1",
                           local_ip="127.0.0.1")
    assert recorder.reached
    assert recorder.status == 200


@pytest.mark.anyio
async def test_no_lan_bind_means_the_middleware_does_nothing():
    """Single-node deployments never set TERMINAL_MCP_LAN_BIND; they must
    not acquire a route policy they never asked for."""
    recorder = _Recorder()
    middleware = LanCidrGuardMiddleware(recorder.app, lan_bind_ip=None,
                                        allowed_cidrs=())
    scope = {"type": "http", "path": "/mcp", "method": "POST",
             "server": ("127.0.0.1", 8766), "client": ("127.0.0.1", 5000)}
    await middleware(scope, None, recorder.send)
    assert recorder.reached


# --- path matching cannot be walked around ------------------------------

@pytest.mark.parametrize("path", [
    "/dashboard/api/nodes/a/heartbeat/../../../../mcp",
    "/dashboard/api/nodes/a/b/heartbeat",
    "/mcp/../dashboard/api/nodes/a/heartbeat",
    "/dashboard/api/nodes//heartbeat",
    "/dashboard/api/nodes/a/heartbeatX",
    "/xdashboard/api/nodes/a/heartbeat",
])
def test_lookalike_paths_do_not_match(path):
    """`{node_id}` is one non-empty segment and the match is anchored, so
    neither a traversal string nor a prefix/suffix lookalike slips in."""
    assert not lan_route_policy.is_allowed_on_lan(path, "POST")


def test_trailing_slash_is_the_same_route():
    assert lan_route_policy.is_allowed_on_lan(
        "/dashboard/api/nodes/dell-linux/heartbeat/", "POST")


def test_node_ids_with_dots_and_hyphens_work():
    for node_id in ("dell-linux", "hp.linux.local", "m910_2", "DELL-5530"):
        assert lan_route_policy.is_allowed_on_lan(
            f"/dashboard/api/nodes/{node_id}/heartbeat", "POST"), node_id


def test_empty_path_is_refused():
    assert not lan_route_policy.is_allowed_on_lan("", "POST")


# --- the operator escape hatch ------------------------------------------

def test_extra_paths_env_can_only_add(monkeypatch):
    monkeypatch.setenv(lan_route_policy.EXTRA_PATHS_ENV,
                       "/dashboard/api/custom/{node_id}/ping")
    assert lan_route_policy.is_allowed_on_lan(
        "/dashboard/api/custom/m910/ping", "POST")
    # and cannot be used to re-open /mcp by omission
    assert not lan_route_policy.is_allowed_on_lan("/mcp", "POST")


def test_malformed_extra_path_is_dropped_not_widened(monkeypatch):
    monkeypatch.setenv(lan_route_policy.EXTRA_PATHS_ENV, "mcp, ,/ok/path")
    assert not lan_route_policy.is_allowed_on_lan("/mcp", "POST")
    assert not lan_route_policy.is_allowed_on_lan("mcp", "POST")
    assert lan_route_policy.is_allowed_on_lan("/ok/path", "POST")


def test_describe_policy_lists_every_allowed_route():
    described = lan_route_policy.describe_policy()
    paths = {entry["path"] for entry in described["allowed"]}
    assert "/dashboard/api/nodes/{node_id}/heartbeat" in paths
    assert not any("/mcp" == p for p in paths)
