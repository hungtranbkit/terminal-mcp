"""terminal-mcp-chatgpt-v1 -- the compact ChatGPT connector surface.

WHY THIS EXISTS
---------------
The full controller at 127.0.0.1:8766/mcp publishes 293 tools. That surface
is correct and stays exactly as it is -- Claude Code, the dashboard and every
admin path depend on it. But a ChatGPT connector pointed at it has repeatedly
come back with a STALE CATALOG: the live endpoint answers `tools/list` with
the batch/turn/queue tools, while the conversation still offers the original
legacy six. Re-pointing the same connector URL has not reliably cleared it,
because the cached catalog is keyed to the connector identity, not to what
the endpoint currently answers.

So this is not a second copy of the tool surface. It is a NEW SERVER IDENTITY
on a NEW URL, publishing a deliberately small, deterministic catalog. Moving
the connector to it once is what escapes the stale cache permanently: a
connector that has never existed before cannot have a cached catalog.

WHY A SIDECAR PROCESS AND NOT A SECOND MOUNT
--------------------------------------------
Mounting a second MCP server inside terminal-mcp-http would put two
StreamableHTTPSessionManagers under one Starlette lifespan, which has to be
chained by hand -- and a mistake there breaks the FULL surface, not just the
compact one. A separate process cannot do that. terminal-mcp-http is not
edited, not restarted, and not risked by anything here; if this sidecar
crashes, the full controller does not notice.

IT OWNS NOTHING
---------------
No database, no config store, no queue/supervisor/controller/integration
loop, no tmux access. Every tool call is forwarded verbatim over MCP to the
real controller on loopback, so all authorization, routing, node selection,
idempotency and durable state stay in exactly one place -- the backend. This
process is a catalog filter and a proxy, and holds no state that could drift
from the controller's own.

The schemas are not re-declared here either: `list_tools` serves the
BACKEND's own `types.Tool` objects, filtered to CATALOG. A tool whose
arguments change upstream changes here automatically, and cannot drift.

NETWORK POSTURE
---------------
Loopback only, always (see `main`). The compact endpoint is reached the same
way the full one is: over the authenticated tunnel that terminates on
loopback. It is never bound to the LAN/overlay address, so the P0 that
lan_route_policy.py closed cannot reappear on this port -- there is no LAN
socket here to reach at all.

FAIL CLOSED
-----------
A backend that is down, unreachable or mid-restart yields a concise
BACKEND_UNAVAILABLE tool error, never a fabricated success and never a stack
trace. `list_tools` before the backend has ever been reached is an empty
catalog rather than a guessed one: an empty list is honest and self-
correcting on the next poll, while a hardcoded fallback would be a second
source of truth -- exactly the drift this module exists to avoid.
"""
from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server

from . import __version__, orchestration_policy

_log = logging.getLogger(__name__)

SERVER_NAME = "terminal-mcp-chatgpt-v1"

# The compact catalog. ORDERED and EXACT -- this tuple is the contract the
# connector sees, and tests/test_chatgpt_sidecar.py asserts it literally so
# that adding a tool here is a deliberate, reviewed act rather than a
# side effect of something registered upstream.
#
# Chosen so one logical ChatGPT turn needs one call:
#   terminal_turn           the default surface: inspect/send/wait/resume
#   terminal_batch_inspect  many targets in one call, instead of N tails
#   terminal_enqueue_task   durable submission (idempotent, request_key'd)
#   terminal_task_status    one task by id
#   terminal_task_batch_status  up to 100 task states in one call
#   terminal_wait_for_state durable wait
#   terminal_resume_wait    resume a wait that returned PENDING
#   terminal_list_sessions  discovery
#   terminal_create_session lifecycle
#   terminal_delete_session lifecycle
#   terminal_list_nodes     fleet visibility (which node a session is on)
#
# Deliberately ABSENT: terminal_send_text / terminal_send_keys. Raw keystroke
# injection is what terminal_turn's guarded send path exists to replace, and
# publishing it here would re-offer the legacy shape this surface is meant to
# retire. Everything else on the 293-tool surface remains available on the
# full endpoint for admin/Claude Code use.
CATALOG: tuple[str, ...] = (
    "terminal_turn",
    "terminal_batch_inspect",
    "terminal_enqueue_task",
    "terminal_task_status",
    "terminal_task_batch_status",
    "terminal_wait_for_state",
    "terminal_resume_wait",
    "terminal_list_sessions",
    "terminal_create_session",
    "terminal_delete_session",
    "terminal_list_nodes",
)

DEFAULT_BACKEND_URL = "http://127.0.0.1:8766/mcp"
DEFAULT_PORT = 8768
# Loopback, not configurable. See the module docstring's NETWORK POSTURE.
BIND_HOST = "127.0.0.1"

BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"

#: Bumped whenever CATALOG changes. Carried in the instructions below so an
#: operator can tell from the CONVERSATION -- without shell access -- which
#: surface a connector is actually attached to. That is the whole diagnostic
#: gap that made this bug recur: a stale legacy-six connector and a healthy
#: one were indistinguishable from inside a chat.
SURFACE_VERSION = "1.0.0"


def compact_instructions() -> str:
    """The shared orchestration policy, prefixed with what is specific to
    THIS surface.

    The shared policy is reused verbatim rather than re-written, so the
    workflow cannot diverge between the full and compact endpoints. But it
    names `terminal_status`, `terminal_tail` and `terminal_send_text` as
    low-level fallbacks, and NONE of those exist here -- so without this
    preamble the compact surface would be advertising tools it does not
    publish. The preamble states the catalog and supersedes that paragraph.
    """
    catalog = ", ".join(CATALOG)
    return (
        f"SURFACE. You are connected to {SERVER_NAME} (compact surface "
        f"v{SURFACE_VERSION}, server version {__version__}), which publishes "
        f"EXACTLY these {len(CATALOG)} tools: {catalog}.\n"
        "If the tool list you can see does not match that, you are attached to a "
        "STALE CACHED CATALOG and must say so instead of working around it -- in "
        "particular, a list containing terminal_send_text/terminal_send_keys or "
        "lacking terminal_turn/terminal_batch_inspect is the known legacy six-tool "
        "regression and needs the connector re-pointed at this endpoint.\n"
        "This surface deliberately has NO terminal_status/terminal_tail/"
        "terminal_send_text/terminal_send_keys: use terminal_turn (action=inspect/"
        "send/send_wait/wait/resume) and terminal_batch_inspect instead, which is "
        "what the paragraph below means by the compact tools. Admin tools live on "
        "the full endpoint and are not reachable here.\n\n"
        + orchestration_policy.server_instructions()
    )


def backend_url() -> str:
    return (os.environ.get("TERMINAL_MCP_CHATGPT_BACKEND")
            or DEFAULT_BACKEND_URL).strip() or DEFAULT_BACKEND_URL


def sidecar_port() -> int:
    raw = (os.environ.get("TERMINAL_MCP_CHATGPT_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        _log.warning("TERMINAL_MCP_CHATGPT_PORT=%r is not a number -- using %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT
    if not (1 <= port <= 65535):
        _log.warning("TERMINAL_MCP_CHATGPT_PORT=%r is out of range -- using %d", raw, DEFAULT_PORT)
        return DEFAULT_PORT
    return port


class BackendUnavailable(RuntimeError):
    """The full controller could not be reached or did not answer.

    Carries no upstream stack trace on purpose: what reaches the connector is
    one short sentence naming the backend URL, never internals.
    """


def _transport_streams(transport: tuple[Any, ...]) -> tuple[Any, Any]:
    """Extract read/write streams from supported MCP client tuple shapes.

    ``streamable_http_client`` used to yield ``(read, write, session_id)``.
    Current MCP releases yield ``(read, write)``.  The sidecar has never used
    the optional session-id callback, so accepting both shapes keeps it a
    transparent proxy across that dependency upgrade.
    """
    if len(transport) < 2:
        raise RuntimeError("MCP streamable HTTP transport returned no streams")
    return transport[0], transport[1]


class Backend:
    """One MCP conversation with the full controller, per call.

    A fresh session per call rather than a long-lived one. The backend is on
    loopback, so connect+initialize costs ~milliseconds, and the alternative
    -- a cached session -- has to survive controller restarts, session
    expiry and half-open sockets correctly or it silently serves errors
    until something notices. A restart of terminal-mcp-http is a routine
    event here (every deploy does one); per-call connect means this process
    simply works again on the next call with no reconnect logic to get
    wrong.
    """

    def __init__(self, url: str | None = None, *, timeout_seconds: float = 30.0) -> None:
        self.url = url or backend_url()
        self.timeout_seconds = timeout_seconds

    @contextlib.asynccontextmanager
    async def _session(self):
        try:
            async with streamable_http_client(self.url) as transport:
                read, write = _transport_streams(transport)
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        except BackendUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 -- every transport failure is the same answer
            raise BackendUnavailable(
                f"the terminal-mcp controller at {self.url} is not reachable "
                f"({type(exc).__name__}); it may be restarting -- retry shortly") from exc

    async def list_tools(self) -> list[types.Tool]:
        async with self._session() as session:
            result = await session.list_tools()
            return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        async with self._session() as session:
            with anyio.fail_after(self.timeout_seconds):
                result = await session.call_tool(name, arguments)
        if not isinstance(result, types.CallToolResult):
            # An InputRequiredResult/elicitation cannot be represented on a
            # stateless proxy hop -- surfacing it as an error is honest,
            # where passing a partial result through would strand the caller.
            raise BackendUnavailable(
                f"tool {name!r} returned a non-final result this connector cannot forward")
        return result


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{BACKEND_UNAVAILABLE}: {message}")],
        is_error=True)


def build_sidecar(backend: Backend | None = None, *,
                  catalog: tuple[str, ...] = CATALOG) -> Server:
    """The compact server. `backend` is injectable so tests never need a
    real controller listening."""
    client = backend or Backend()
    # Last catalog successfully read from the backend, so a momentary
    # controller restart does not blank the connector's tool list mid-chat.
    cached: dict[str, list[types.Tool]] = {"tools": []}

    async def on_list_tools(_ctx, _params) -> types.ListToolsResult:
        try:
            upstream = await client.list_tools()
        except BackendUnavailable as exc:
            _log.warning("chatgpt-v1: catalog refresh failed (%s) -- serving %d cached tool(s)",
                         exc, len(cached["tools"]))
            return types.ListToolsResult(tools=list(cached["tools"]))
        by_name = {tool.name: tool for tool in upstream}
        missing = [name for name in catalog if name not in by_name]
        if missing:
            # Loud, and not fatal: the connector still gets every tool that
            # does exist. A silently shrinking catalog is the failure mode
            # this whole module exists to prevent, so it must be visible.
            _log.error("chatgpt-v1: backend is missing catalog tool(s) %s -- serving %d of %d",
                       missing, len(catalog) - len(missing), len(catalog))
        tools = [by_name[name] for name in catalog if name in by_name]
        cached["tools"] = tools
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx, params: types.CallToolRequestParams):
        if params.name not in catalog:
            # Not on this surface. Refused here rather than forwarded: the
            # compact endpoint's whole value is that it cannot be used to
            # reach the other 282 tools.
            return types.CallToolResult(
                content=[types.TextContent(
                    type="text",
                    text=(f"TOOL_NOT_ON_THIS_SURFACE: {params.name!r} is not part of "
                          f"{SERVER_NAME}. Use the full /mcp endpoint for admin tools."))],
                is_error=True)
        try:
            return await client.call_tool(params.name, dict(params.arguments or {}))
        except BackendUnavailable as exc:
            _log.warning("chatgpt-v1: call %s failed: %s", params.name, exc)
            return _error_result(str(exc))
        except TimeoutError:
            _log.warning("chatgpt-v1: call %s timed out", params.name)
            return _error_result(
                f"the controller did not answer {params.name!r} within "
                f"{client.timeout_seconds:.0f}s")

    return Server(
        SERVER_NAME,
        version=__version__,
        instructions=compact_instructions(),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def build_app(server: Server | None = None, *, path: str = "/mcp"):
    """The Starlette app for this sidecar. One MCP server, one lifespan --
    which is the entire reason this is its own process."""
    target = server or build_sidecar()
    return target.streamable_http_app(streamable_http_path=path, json_response=True,
                                      host=BIND_HOST)


def main() -> None:
    import uvicorn

    from .logging_setup import configure_logging

    configure_logging()
    port = sidecar_port()
    _log.info("%s starting on %s:%d -> backend %s (catalog: %d tools)",
              SERVER_NAME, BIND_HOST, port, backend_url(), len(CATALOG))
    app = build_app()
    # host=BIND_HOST is not a default to be overridden: this surface is
    # reached through the tunnel that terminates on loopback, exactly like
    # the full controller.
    uvicorn.run(app, host=BIND_HOST, port=port, log_config=None)


if __name__ == "__main__":
    main()
