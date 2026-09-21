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

import base64
import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

import anyio
import mcp.types as types
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server

from . import __version__, orchestration_policy

_log = logging.getLogger(__name__)

SERVER_NAME = "terminal-mcp-chatgpt-v1"

# THE COMPACT CATALOG: exactly one tool.
#
# The previous eleven were all genuinely useful and all genuinely reachable --
# and that was the problem. A model handed eleven plausible tools picks a
# different one per step, so a single logical orchestration step ("look at
# these four sessions, send to the idle one, wait for it") became four, five,
# six separate "Called tool" rows in the conversation. The user's complaint was
# never that a tool was missing; it was the wall of rows.
#
# One advertised tool makes one orchestration step one row, by construction
# rather than by asking the model nicely. Nothing was dropped: terminal_turn's
# action vocabulary covers every one of the retired ten --
#
#   inspect (one target or many, i.e. batch inspect) | send | send_wait |
#   wait | resume | list_sessions | list_nodes | create_session |
#   delete_session | enqueue_task | task_status | task_batch_status
#
# -- routing each to the very same backend implementation the standalone tool
# used (see compact_tools.TURN_HANDLER_ACTIONS), so this is a narrower
# CATALOG, not a narrower capability.
#
# Deliberately ABSENT, and never to be added: terminal_send_text /
# terminal_send_keys. Raw keystroke injection is what terminal_turn's guarded
# send path exists to replace. Everything else on the 293-tool surface remains
# available on the full endpoint for admin/Claude Code use.
CATALOG: tuple[str, ...] = (
    "terminal_turn",
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
SURFACE_VERSION = "2.1.0"

# CACHED-CALL TRANSLATION: every legacy name this surface used to advertise is
# accepted at CALL time and rewritten into the equivalent terminal_turn action.
#
# WHY TRANSLATE RATHER THAN FORWARD. These names are all still real tools on the
# full controller, so forwarding them verbatim "works" -- which is exactly the
# trap. terminal_turn is where the current behaviour lives: the guarded send
# path, the server-side wait/poll/task-following that TMCP-CALLED-TOOL-SPAM-002
# moved off the client, the resource-health block. A cached connector that calls
# terminal_batch_inspect or terminal_wait_for_state DIRECTLY silently gets the
# older, thinner semantics and starts polling again -- a stale catalog quietly
# buying a stale workflow. Translating means a cached conversation executes the
# same canonical operation a new one does, and improvements to terminal_turn
# reach both without the client ever updating.
#
# This is a CALL-time allowance and never a listing: nothing here can put a row
# back in tools/list (CATALOG remains the only public contract), and every
# translation lands on an action terminal_turn already validates, so the backend
# stays the only authority on authorization and idempotency.
#
# NOT translated, and deliberately still refused: terminal_send_keys (raw
# keystroke injection has no guarded equivalent -- terminal_turn(action=send) is
# a composed, verified submission, not a key sequence) and every admin tool on
# the 293-tool surface. terminal_send_text IS translated, because routing a
# cached text send through the guarded action is strictly safer than either
# refusing it (a cached conversation loses the ability to send at all) or
# forwarding it raw.
#
# Each entry: the target action, how the legacy argument names map onto
# terminal_turn's, which of those are required, and constants to add.
_LEGACY_TRANSLATIONS: dict[str, dict[str, Any]] = {
    "terminal_status": {
        "action": "inspect", "rename": {"session": "target"}, "required": ("target",),
        # One bounded line is enough: resource health is carried by the status
        # payload, not inferred from the returned tail.
        "constants": {"tail_lines": 1, "compact": True},
    },
    "terminal_batch_inspect": {
        "action": "inspect", "keep": ("targets", "tail_lines", "compact"),
        "required": ("targets",),
    },
    "terminal_list_sessions": {"action": "list_sessions"},
    "terminal_list_nodes": {"action": "list_nodes"},
    "terminal_create_session": {
        "action": "create_session", "rename": {"name": "target"},
        "keep": ("agent_type", "working_directory", "initial_prompt", "grant_mode",
                 "binding", "node"),
        "required": ("target",),
    },
    "terminal_delete_session": {
        "action": "delete_session", "rename": {"name": "target"}, "required": ("target",),
    },
    "terminal_send_text": {
        "action": "send", "rename": {"session": "target"}, "keep": ("text",),
        "required": ("target", "text"),
    },
    "terminal_wait_for_state": {
        "action": "wait",
        "keep": ("target", "desired_states", "timeout", "poll_interval", "tail_lines"),
        "required": ("target", "desired_states"),
    },
    "terminal_resume_wait": {
        "action": "resume", "keep": ("resume_token", "timeout", "poll_interval"),
        "required": ("resume_token",),
    },
    "terminal_enqueue_task": {
        "action": "enqueue_task", "rename": {"session": "target", "prompt": "text"},
        "keep": ("title", "priority", "metadata", "request_key"),
        "required": ("target", "text"),
    },
    "terminal_task_status": {
        "action": "task_status", "keep": ("task_id",), "required": ("task_id",),
    },
    "terminal_task_batch_status": {
        "action": "task_batch_status", "keep": ("task_ids",), "required": ("task_ids",),
    },
}

#: The accepted cached names, as a set. Kept as its own public name because it
#: is what a caller/test asks "is this still callable here?" with.
CALL_COMPAT = frozenset(_LEGACY_TRANSLATIONS)
#: Retained for compatibility with the first bridge, which only handled
#: terminal_status. Now simply the read-only subset of the table above.
LEGACY_READ_COMPAT = frozenset({"terminal_status"})


class _UntranslatableCall(ValueError):
    """A cached call naming a translatable tool but missing a required field."""


def translate_legacy_call(name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
    """terminal_turn arguments for a cached legacy call, or None if `name` is
    not one.

    Raises _UntranslatableCall when the legacy arguments cannot produce a valid
    action -- refusing is right there, because guessing a target or a prompt is
    how a compatibility shim ends up sending the wrong thing to the wrong pane.
    """
    spec = _LEGACY_TRANSLATIONS.get(name)
    if spec is None:
        return None
    translated: dict[str, Any] = {"action": spec["action"]}
    for legacy_key, turn_key in (spec.get("rename") or {}).items():
        if legacy_key in arguments:
            translated[turn_key] = arguments[legacy_key]
    for key in spec.get("keep") or ():
        if key in arguments:
            translated[key] = arguments[key]
    translated.update(spec.get("constants") or {})
    for key in spec.get("required") or ():
        value = translated.get(key)
        # A blank string or an empty list is the same as absent: both are a
        # client that lost its arguments, not a client asking for nothing.
        if value is None or (isinstance(value, (str, list, tuple)) and not
                             (value.strip() if isinstance(value, str) else value)):
            raise _UntranslatableCall(
                f"{name} compatibility requires {key!r}")
        if isinstance(value, str):
            translated[key] = value.strip()
    return translated


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
        f"EXACTLY {len(CATALOG)} tool: {catalog}.\n"
        "USE terminal_turn FOR EVERYTHING. One logical orchestration step is one "
        "terminal_turn call. Its `action` covers inspect (one `target` or many "
        "`targets` -- this is batch inspect), send, send_wait, wait, resume, "
        "list_sessions, list_nodes, create_session, delete_session, enqueue_task, "
        "task_status and task_batch_status. `target` is the session for every "
        "action that names one; `text` is the prompt for both send and "
        "enqueue_task. Do not look for a separate tool per verb -- there is "
        "none, and calling terminal_turn repeatedly for one step is the thing "
        "this surface exists to avoid.\n"
        "If the tool list you can see offers more than that one tool, you are "
        "attached to a STALE CACHED CATALOG. Those older names (terminal_batch_"
        "inspect, terminal_enqueue_task, terminal_wait_for_state, "
        "terminal_list_sessions, terminal_status, terminal_send_text and the "
        "other v1 names) still work: this surface TRANSLATES each one into the "
        "equivalent terminal_turn action, so you get the current behaviour "
        "either way. Prefer terminal_turn; re-point/reconnect obtains the "
        "canonical catalog.\n"
        "This surface deliberately has NO terminal_send_text/terminal_send_keys: "
        "terminal_turn(action=send) is the guarded replacement. Admin tools live "
        "on the full endpoint and are not reachable here.\n"
        # Collapsing the CATALOG to one tool never collapsed the number of
        # CALLS: every terminal_turn call is still its own "Called tool" row in
        # the client, so a fanned-out turn reads exactly like the multi-tool
        # surface this was meant to replace. The plural arguments already exist
        # (`targets`, `task_ids`, `desired_states`); what was missing was an
        # instruction to reach for them, stated in terms of the concrete
        # mistake rather than as a general preference.
        "BATCH, DO NOT FAN OUT. Answer the whole question in ONE call wherever "
        "the action supports it. Inspecting several sessions is ONE call with "
        "`targets` (a list) -- never one call per session with `target`. "
        "Waiting is ONE call with action=wait plus `desired_states` and "
        "`timeout` -- never a poll loop of inspects. Checking several queued "
        "tasks is task_batch_status with `task_ids` -- never task_status "
        "repeated. Do not call list_sessions and then inspect each result one "
        "at a time: pass those names straight to `targets`. Before making a "
        "SECOND terminal_turn call in the same turn, check whether a plural "
        "argument or `desired_states` would have answered both at once; if it "
        "would, make that one call instead.\n"
        "Results are already compact by default -- every list action returns "
        "only the fields an orchestrator acts on. Use compact=false ONLY after "
        "reading a compact result that genuinely lacked a field you need, "
        "never speculatively and never as a first call.\n\n"
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
        # A Streamable HTTP teardown can fail after the controller response has
        # already been decoded (for example when the best-effort session DELETE
        # races an already-completed ASGI response).  Do not throw away a
        # successful tools/list result just because closing that one-shot
        # transport failed afterwards.
        result = None
        try:
            async with self._session() as session:
                result = await session.list_tools()
        except BackendUnavailable as exc:
            if result is None:
                raise
            _log.warning(
                "chatgpt-v1: backend teardown failed after successful tools/list; "
                "preserving the received result: %s", exc)
        return list(result.tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        # Same rule for mutations: once the final CallToolResult was decoded,
        # a teardown-only failure must not turn it into BACKEND_UNAVAILABLE.
        # Reporting UNKNOWN here is especially dangerous because a caller may
        # retry a mutation.  We never retry the tool call ourselves.
        result = None
        try:
            async with self._session() as session:
                with anyio.fail_after(self.timeout_seconds):
                    result = await session.call_tool(name, arguments)
        except BackendUnavailable as exc:
            if result is None:
                raise
            _log.warning(
                "chatgpt-v1: backend teardown failed after successful call %s; "
                "preserving the received result and not replaying it: %s",
                name, exc)
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


INLINE_SCREENSHOT_MAX_BYTES = 8 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SCREENSHOT_ARTIFACT_ERROR = "SCREENSHOT_ARTIFACT_REJECTED"


def _screenshot_artifact_root(override: str | Path | None = None) -> Path:
    value = override or os.environ.get("TERMINAL_MCP_BROWSER_ARTIFACT_DIR")
    if value:
        return Path(value).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "browser-artifacts"


def _is_browser_screenshot_call(call_name: str, call_args: dict[str, Any]) -> bool:
    if call_name != "terminal_turn":
        return False
    action = str(call_args.get("action") or "").strip().lower().replace("-", "_")
    return action in {"browser_screenshot", "screenshot"}


def _screenshot_artifact_error(detail: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(
            type="text", text=f"{_SCREENSHOT_ARTIFACT_ERROR}: {detail}")],
        is_error=True,
    )


def _attach_browser_screenshot_image(
    result: types.CallToolResult,
    call_name: str,
    call_args: dict[str, Any],
    *,
    artifact_root: str | Path | None = None,
) -> types.CallToolResult:
    if not _is_browser_screenshot_call(call_name, call_args) or result.is_error:
        return result

    text_blocks = [block for block in result.content
                   if isinstance(block, types.TextContent)]
    if not text_blocks:
        return _screenshot_artifact_error("controller returned no text metadata")

    try:
        envelope = json.loads(text_blocks[0].text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _screenshot_artifact_error("controller returned invalid JSON metadata")
    if not isinstance(envelope, dict):
        return _screenshot_artifact_error("controller returned invalid screenshot metadata")
    payload = envelope.get("result")
    if not isinstance(payload, dict):
        return result
    if str(payload.get("status") or "").upper() != "OK":
        return result
    raw_path = payload.get("screenshot")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _screenshot_artifact_error("successful screenshot response has no artifact path")

    try:
        root = _screenshot_artifact_root(artifact_root).resolve(strict=True)
        candidate = Path(raw_path).expanduser()
        if candidate.is_symlink():
            return _screenshot_artifact_error("screenshot artifact may not be a symlink")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        stat_result = resolved.stat()
        if not resolved.is_file():
            return _screenshot_artifact_error("screenshot artifact is not a regular file")
        if stat_result.st_size <= 0:
            return _screenshot_artifact_error("screenshot artifact is empty")
        if stat_result.st_size > INLINE_SCREENSHOT_MAX_BYTES:
            return _screenshot_artifact_error(
                f"screenshot artifact exceeds {INLINE_SCREENSHOT_MAX_BYTES} bytes")
        data = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return _screenshot_artifact_error(
            "screenshot artifact is missing or outside the configured artifact directory")

    if len(data) > INLINE_SCREENSHOT_MAX_BYTES:
        return _screenshot_artifact_error(
            f"screenshot artifact exceeds {INLINE_SCREENSHOT_MAX_BYTES} bytes")
    if not data.startswith(_PNG_SIGNATURE):
        return _screenshot_artifact_error("screenshot artifact is not a PNG")

    image = types.ImageContent(
        type="image",
        data=base64.b64encode(data).decode("ascii"),
        mime_type="image/png",
    )
    return result.model_copy(update={"content": [*result.content, image]})


# Human-readable display name. MCP clients that honour `title`/
# `annotations.title` (spec 2025-06-18 onward) render it instead of the raw
# tool name, so a call shows as "Terminal MCP" rather than
# `codex_apps.terminal_mcp.terminal_turn`. Purely cosmetic and best-effort:
# a client is free to keep showing the name, and NOTHING here depends on it.
# Set only when the backend left the field empty, so an upstream title always
# wins.
DISPLAY_TITLES = {"terminal_turn": "Terminal MCP"}


def _with_display_title(tool: types.Tool) -> types.Tool:
    title = DISPLAY_TITLES.get(tool.name)
    if not title:
        return tool
    annotations = tool.annotations or types.ToolAnnotations()
    return tool.model_copy(update={
        "title": tool.title or title,
        "annotations": annotations.model_copy(update={"title": annotations.title or title}),
    })


def build_sidecar(backend: Backend | None = None, *,
                  catalog: tuple[str, ...] = CATALOG,
                  browser_artifact_root: str | Path | None = None) -> Server:
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
        tools = [_with_display_title(by_name[name]) for name in catalog if name in by_name]
        cached["tools"] = tools
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(_ctx, params: types.CallToolRequestParams):
        call_name = params.name
        call_args = dict(params.arguments or {})

        # Cached-catalog compatibility. A stale ChatGPT conversation can only
        # emit the schema it cached, even while tools/list on this endpoint
        # correctly serves CATALOG. Every legacy name is REWRITTEN into the
        # equivalent terminal_turn action rather than forwarded, so a cached
        # conversation executes the same canonical operation a new one does --
        # see _LEGACY_TRANSLATIONS for why forwarding verbatim is the trap.
        if call_name not in catalog:
            try:
                translated = translate_legacy_call(call_name, call_args)
            except _UntranslatableCall as exc:
                return types.CallToolResult(
                    content=[types.TextContent(type="text",
                                               text=f"INVALID_ARGUMENT: {exc}")],
                    is_error=True)
            if translated is None:
                # Genuinely not on this surface. Refused here rather than
                # forwarded: the compact endpoint's whole value is that it
                # cannot be used to reach the rest of the 293-tool surface.
                return types.CallToolResult(
                    content=[types.TextContent(
                        type="text",
                        text=(f"TOOL_NOT_ON_THIS_SURFACE: {params.name!r} is not part of "
                              f"{SERVER_NAME}. Use the full /mcp endpoint for admin tools."))],
                    is_error=True)
            # WARNING, not info: a legacy name reaching this endpoint means some
            # client is still driving the pre-compact, multi-tool catalog it
            # cached earlier -- exactly the "many called-tool lines" symptom
            # this surface was built to end. The call still succeeds (it is
            # rewritten below), but the stale client is worth seeing, because
            # a silent rewrite makes a regression here invisible. Audited
            # 2026-09-21: the last such call was 2026-09-19T22:21, i.e. the
            # fleet is clean and any NEW occurrence is a real signal.
            _log.warning(
                "chatgpt-v1: STALE_CLIENT_CATALOG -- legacy tool %s called; rewritten to "
                "terminal_turn(action=%s). A client is still using a cached multi-tool "
                "catalog; if this repeats, re-point or reconnect that connector.",
                call_name, translated["action"])
            call_name = "terminal_turn"
            call_args = translated
        try:
            result = await client.call_tool(call_name, call_args)
            return _attach_browser_screenshot_image(
                result, call_name, call_args, artifact_root=browser_artifact_root)
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
