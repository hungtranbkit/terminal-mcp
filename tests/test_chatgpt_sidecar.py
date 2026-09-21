"""The compact ChatGPT connector surface.

The point of `terminal_mcp/chatgpt_sidecar.py` is that its catalog is small,
exact and deterministic -- that is the whole reason a second server identity
exists. So the catalog is asserted LITERALLY here: adding or removing a tool
on that surface has to be a deliberate edit to this file too, never a side
effect of something registered upstream.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import logging

import mcp.types as types
import pytest

from terminal_mcp import chatgpt_sidecar as sidecar
from terminal_mcp.chatgpt_sidecar import (BACKEND_UNAVAILABLE, CATALOG, SERVER_NAME,
                                          Backend, BackendUnavailable, build_app,
                                          build_sidecar, backend_url, sidecar_port)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool(name: str, description: str = "upstream description") -> types.Tool:
    return types.Tool(name=name, description=description,
                      inputSchema={"type": "object", "properties": {}})


class FakeBackend:
    """A controller stand-in. No socket, no real MCP conversation."""

    def __init__(self, tools=None, *, result=None, raises=None,
                 timeout_seconds: float = 30.0) -> None:
        self._tools = list(tools) if tools is not None else [_tool(n) for n in CATALOG]
        self._result = result
        self._raises = raises
        self.timeout_seconds = timeout_seconds
        self.calls: list[tuple[str, dict]] = []
        self.list_calls = 0

    async def list_tools(self):
        self.list_calls += 1
        if self._raises is not None:
            raise self._raises
        return list(self._tools)

    async def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if self._raises is not None:
            raise self._raises
        return self._result or types.CallToolResult(
            content=[types.TextContent(type="text", text=f"ok:{name}")])


def _list_tools(server) -> list[types.Tool]:
    handler = server.get_request_handler("tools/list").handler
    return list(asyncio.run(handler(None, None)).tools)


def _call_tool(server, name: str, arguments: dict | None = None) -> types.CallToolResult:
    handler = server.get_request_handler("tools/call").handler
    params = types.CallToolRequestParams(name=name, arguments=arguments or {})
    return asyncio.run(handler(None, params))


def _text(result: types.CallToolResult) -> str:
    return "\n".join(block.text for block in result.content
                     if isinstance(block, types.TextContent))


# ---------------------------------------------------------------------------
# The catalog IS the contract
# ---------------------------------------------------------------------------

def test_catalog_is_exactly_one_tool():
    """THE regression for the "many Called tool rows" complaint: eleven
    advertised tools meant a model picked a different one per step, so one
    logical orchestration step became five or six rows in the conversation.
    One advertised tool makes that one row by construction."""
    assert CATALOG == ("terminal_turn",)
    assert len(CATALOG) == 1
    assert len(set(CATALOG)) == len(CATALOG), "no duplicates"


#: What v1.x advertised. Kept as a named constant so the shrink is asserted
#: against the real previous surface rather than a paraphrase of it.
V1_CATALOG = (
    "terminal_turn", "terminal_batch_inspect", "terminal_enqueue_task",
    "terminal_task_status", "terminal_task_batch_status", "terminal_wait_for_state",
    "terminal_resume_wait", "terminal_list_sessions", "terminal_create_session",
    "terminal_delete_session", "terminal_list_nodes",
)


#: Cached legacy call -> (arguments it would send, the terminal_turn action it
#: must become). Every one of these is a real v1/legacy schema, so the mapping
#: is what a stale conversation actually emits, not a paraphrase.
CACHED_CALLS = (
    ("terminal_batch_inspect", {"targets": ["s1", "s2"], "tail_lines": 3, "compact": True},
     "inspect"),
    ("terminal_status", {"session": "s1"}, "inspect"),
    ("terminal_list_sessions", {}, "list_sessions"),
    ("terminal_list_nodes", {}, "list_nodes"),
    ("terminal_create_session", {"name": "agent-new", "agent_type": "claude"},
     "create_session"),
    ("terminal_delete_session", {"name": "agent-old"}, "delete_session"),
    ("terminal_send_text", {"session": "s1", "text": "go"}, "send"),
    ("terminal_wait_for_state", {"target": "s1", "desired_states": ["IDLE"]}, "wait"),
    ("terminal_resume_wait", {"resume_token": "wait_" + "0" * 32}, "resume"),
    ("terminal_enqueue_task", {"session": "s1", "prompt": "run it"}, "enqueue_task"),
    ("terminal_task_status", {"task_id": "t1"}, "task_status"),
    ("terminal_task_batch_status", {"task_ids": ["t1"]}, "task_batch_status"),
)


@pytest.mark.parametrize("name,arguments,action", CACHED_CALLS)
def test_a_cached_legacy_call_is_translated_to_the_turn_action(name, arguments, action):
    """THE compatibility contract. A cached name must not merely be tolerated:
    it must execute the CANONICAL operation, because terminal_turn is where the
    guarded send path and the server-side wait/task-following live. Forwarding
    verbatim would silently buy a stale workflow with a stale catalog."""
    assert name not in CATALOG, f"{name} must never be advertised"
    assert name in sidecar.CALL_COMPAT, f"{name} must stay callable"
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), name, arguments)
    assert result.is_error in (False, None), name
    assert len(backend.calls) == 1
    called_name, called_args = backend.calls[0]
    assert called_name == "terminal_turn", f"{name} must be translated, not forwarded"
    assert called_args["action"] == action


def test_the_cached_batch_inspect_translation_carries_its_arguments():
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), "terminal_batch_inspect",
                        {"targets": ["s1", "s2"], "tail_lines": 3, "compact": False})
    assert result.is_error in (False, None)
    assert backend.calls == [("terminal_turn", {
        "action": "inspect", "targets": ["s1", "s2"], "tail_lines": 3, "compact": False,
    })]


def test_every_retired_v1_tool_is_still_callable_but_never_advertised():
    """Shrinking the catalog must not break a conversation that is already
    open: a connector caches the catalog, so a client attached before v2.0.0
    keeps calling the v1 names until its cache turns over."""
    translated = {name for name, _args, _action in CACHED_CALLS}
    for name in V1_CATALOG:
        if name == "terminal_turn":
            continue
        assert name not in CATALOG, f"{name} must not be advertised"
        assert name in sidecar.CALL_COMPAT, f"{name} must stay callable"
        assert name in translated, f"{name} needs a translation assertion"


@pytest.mark.parametrize("name,arguments", [
    ("terminal_batch_inspect", {}),
    ("terminal_batch_inspect", {"targets": []}),
    ("terminal_status", {"session": "   "}),
    ("terminal_create_session", {"agent_type": "claude"}),
    ("terminal_send_text", {"session": "s1"}),
    ("terminal_enqueue_task", {"session": "s1"}),
    ("terminal_task_status", {}),
    ("terminal_wait_for_state", {"target": "s1"}),
])
def test_a_cached_call_missing_its_arguments_is_refused_not_guessed(name, arguments):
    """Guessing a target or a prompt is how a compatibility shim sends the
    wrong thing to the wrong pane."""
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), name, arguments)
    assert result.is_error is True
    assert "INVALID_ARGUMENT" in _text(result)
    assert backend.calls == [], "a refused call must not reach the controller"


def test_raw_keystroke_injection_is_never_reopened():
    """terminal_send_text IS accepted, but only by translation to the guarded
    action -- raw key injection has no guarded equivalent and stays refused,
    along with every granted/admin variant."""
    for forbidden in ("terminal_send_keys", "terminal_send_text_granted",
                      "terminal_kill_session", "terminal_send_bound"):
        assert forbidden not in sidecar.CALL_COMPAT
        assert forbidden not in CATALOG
        backend = FakeBackend()
        result = _call_tool(build_sidecar(backend), forbidden, {"session": "s1"})
        assert result.is_error is True
        assert "TOOL_NOT_ON_THIS_SURFACE" in _text(result)
        assert backend.calls == []


def test_a_cached_send_text_becomes_the_guarded_send_action():
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), "terminal_send_text",
                        {"session": "claude-1", "text": "go", "press_enter": True})
    assert result.is_error in (False, None)
    name, args = backend.calls[0]
    assert name == "terminal_turn"
    assert args == {"action": "send", "target": "claude-1", "text": "go"}
    # press_enter is not forwarded: the guarded action owns submission, and a
    # cached client's key-level flag must not steer it.
    assert "press_enter" not in args


def test_every_retired_tool_is_reachable_as_a_turn_action():
    """The catalog is narrower; the CAPABILITY is not. Each retired tool has a
    terminal_turn action that routes to the same backend implementation."""
    from terminal_mcp import compact_tools as ct
    for name, action in (
            ("terminal_batch_inspect", "inspect"),
            ("terminal_wait_for_state", "wait"),
            ("terminal_resume_wait", "resume"),
            ("terminal_list_sessions", "list_sessions"),
            ("terminal_list_nodes", "list_nodes"),
            ("terminal_create_session", "create_session"),
            ("terminal_delete_session", "delete_session"),
            ("terminal_enqueue_task", "enqueue_task"),
            ("terminal_task_status", "task_status"),
            ("terminal_task_batch_status", "task_batch_status"),
    ):
        assert action in ct.TURN_ACTIONS, f"{name} has no turn action ({action})"


def test_raw_keystroke_tools_are_not_on_this_surface():
    """terminal_turn's guarded send path is what replaces these; publishing
    them here would re-offer the legacy shape this surface exists to retire."""
    for forbidden in ("terminal_send_text", "terminal_send_keys",
                      "terminal_send_text_granted", "terminal_send_bound"):
        assert forbidden not in CATALOG


def test_streamable_http_transport_supports_current_and_legacy_tuple_shapes():
    assert sidecar._transport_streams(("read", "write")) == ("read", "write")
    assert sidecar._transport_streams(("read", "write", "session-id")) == ("read", "write")


def test_every_catalog_tool_really_exists_on_the_full_surface():
    """A catalog naming a tool the controller does not publish would serve a
    silently short list -- the exact failure this surface exists to end."""
    from terminal_mcp.mcp_app import build_mcp
    published = {tool.name for tool in asyncio.run(build_mcp().list_tools())}
    missing = [name for name in CATALOG if name not in published]
    assert missing == [], f"catalog names tools the controller does not publish: {missing}"


def test_the_server_identity_is_new_and_carries_the_shared_policy():
    """A connector that has never existed before cannot have a cached
    catalog -- the identity is the fix, so it must not collide.

    The instructions EMBED the shared policy rather than equalling it: the
    workflow must not diverge between the two endpoints, but the shared text
    names terminal_status/terminal_tail/terminal_send_text as low-level
    fallbacks and none of those exist here, so a surface-specific preamble
    supersedes that paragraph and carries the discovery signal.
    """
    from terminal_mcp import orchestration_policy
    assert SERVER_NAME == "terminal-mcp-chatgpt-v1"
    server = build_sidecar(FakeBackend())
    assert server.name == SERVER_NAME
    assert orchestration_policy.server_instructions() in server.instructions, \
        "the shared policy must be reused verbatim, not paraphrased"
    assert server.instructions.startswith("SURFACE."), \
        "the surface-specific preamble must come first"


def test_the_instructions_carry_a_machine_readable_discovery_signal():
    """The diagnostic gap that let this bug recur: from inside a chat, a
    stale legacy-six connector and a healthy one looked identical."""
    text = sidecar.compact_instructions()
    assert SERVER_NAME in text
    assert sidecar.SURFACE_VERSION in text
    assert f"EXACTLY {len(CATALOG)} tool" in text
    for name in CATALOG:
        assert name in text, f"{name} must be named in the discovery signal"
    assert "STALE CACHED CATALOG" in text
    assert "terminal_status" in text
    assert "USE terminal_turn FOR EVERYTHING" in text
    assert "NO terminal_send_text/terminal_send_keys" in text, \
        "the compatibility bridge must not advertise raw send in tools/list"
    # Every action has to be named, or a model cannot find the capability that
    # used to be its own tool.
    for action in ("inspect", "send", "send_wait", "wait", "resume",
                   "list_sessions", "list_nodes", "create_session",
                   "delete_session", "enqueue_task", "task_status",
                   "task_batch_status"):
        assert action in text, f"action {action} must be named in the instructions"


# ---------------------------------------------------------------------------
# list_tools: filter, never re-declare
# ---------------------------------------------------------------------------

def test_list_tools_filters_to_the_catalog_in_catalog_order():
    upstream = [_tool("terminal_admin_thing"), *[_tool(n) for n in reversed(CATALOG)]]
    server = build_sidecar(FakeBackend(upstream))
    assert [tool.name for tool in _list_tools(server)] == list(CATALOG)


def test_list_tools_serves_the_backends_own_schemas_not_copies():
    """Schemas are not re-declared in the sidecar, so an upstream argument
    change propagates automatically and cannot drift."""
    upstream = [_tool(n, description=f"real {n}") for n in CATALOG]
    upstream[0].input_schema = {"type": "object", "properties": {"action": {"type": "string"}}}
    served = _list_tools(build_sidecar(FakeBackend(upstream)))
    assert served[0].description == "real terminal_turn"
    assert served[0].input_schema["properties"] == {"action": {"type": "string"}}


def test_list_tools_before_the_backend_is_ever_reachable_is_empty_not_guessed():
    """An empty list is honest and self-correcting on the next poll; a
    hardcoded fallback would be a second source of truth."""
    server = build_sidecar(FakeBackend(raises=BackendUnavailable("down")))
    assert _list_tools(server) == []


def test_list_tools_keeps_serving_the_cache_across_a_controller_restart():
    """A deploy restarts terminal-mcp-http routinely; the connector's tool
    list must not blank mid-chat because of it."""
    backend = FakeBackend()
    server = build_sidecar(backend)
    assert [t.name for t in _list_tools(server)] == list(CATALOG)
    backend._raises = BackendUnavailable("restarting")
    assert [t.name for t in _list_tools(server)] == list(CATALOG)


def test_a_catalog_tool_missing_upstream_is_loud_but_not_fatal(caplog):
    """A silently shrinking catalog is the failure mode this module exists to
    prevent, so it has to be visible."""
    upstream = [_tool("terminal_admin_thing")]  # the catalog tool is absent
    server = build_sidecar(FakeBackend(upstream))
    with caplog.at_level(logging.ERROR):
        served = [tool.name for tool in _list_tools(server)]
    assert "terminal_turn" not in served
    assert len(served) == len(CATALOG) - 1 == 0
    assert any("terminal_turn" in record.getMessage()
               for record in caplog.records), "missing tool must be logged by name"


# ---------------------------------------------------------------------------
# call_tool: forward verbatim, or refuse
# ---------------------------------------------------------------------------

def test_call_tool_forwards_arguments_verbatim():
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), "terminal_turn",
                        {"action": "inspect", "target": "codex1"})
    assert backend.calls == [("terminal_turn", {"action": "inspect", "target": "codex1"})]
    assert result.is_error in (False, None)
    assert "ok:terminal_turn" in _text(result)


def test_stale_terminal_status_is_read_only_compat_alias_to_compact_inspect():
    """A cached legacy catalog can still inspect safely without re-exposing
    terminal_status in tools/list. The backend must execute terminal_turn,
    because compact inspection is the canonical implementation."""
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), "terminal_status",
                        {"session": "nova-claude-long-2"})
    assert backend.calls == [("terminal_turn", {
        "action": "inspect",
        "target": "nova-claude-long-2",
        "tail_lines": 1,
        "compact": True,
    })]
    assert result.is_error in (False, None)
    assert "ok:terminal_turn" in _text(result)
    assert "terminal_status" not in CATALOG


def test_stale_terminal_status_requires_a_real_session_name():
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), "terminal_status", {"session": "   "})
    assert result.is_error is True
    assert "INVALID_ARGUMENT" in _text(result)
    assert backend.calls == []


def test_a_tool_not_on_this_surface_is_refused_and_never_forwarded():
    """The compact endpoint's whole value is that it cannot be used to reach
    the rest of the surface."""
    backend = FakeBackend()
    result = _call_tool(build_sidecar(backend), "terminal_send_keys", {"keys": ["Enter"]})
    assert result.is_error is True
    assert "TOOL_NOT_ON_THIS_SURFACE" in _text(result)
    assert backend.calls == [], "a refused tool must not reach the controller"


def test_an_unreachable_backend_is_a_clean_error_not_a_stack_trace():
    backend = FakeBackend(raises=BackendUnavailable(
        "the terminal-mcp controller at http://127.0.0.1:8766/mcp is not reachable"))
    result = _call_tool(build_sidecar(backend), "terminal_list_sessions")
    assert result.is_error is True
    body = _text(result)
    assert body.startswith(f"{BACKEND_UNAVAILABLE}:")
    assert "Traceback" not in body and "  File \"" not in body


def test_a_backend_timeout_is_a_clean_error_naming_the_tool():
    backend = FakeBackend(raises=TimeoutError(), timeout_seconds=30.0)
    result = _call_tool(build_sidecar(backend), "terminal_wait_for_state",
                        {"target": "s1", "desired_states": ["IDLE"]})
    assert result.is_error is True
    body = _text(result)
    assert body.startswith(f"{BACKEND_UNAVAILABLE}:")
    assert "terminal_wait_for_state" in body


def test_a_non_final_backend_result_is_refused_rather_than_half_forwarded():
    """An elicitation/InputRequiredResult cannot be represented on a stateless
    proxy hop; passing a partial result through would strand the caller."""
    class Weird:
        pass

    async def run():
        backend = Backend(url="http://127.0.0.1:1/mcp")

        class FakeSession:
            async def call_tool(self, name, arguments):
                return Weird()

        import contextlib

        @contextlib.asynccontextmanager
        async def fake_session():
            yield FakeSession()

        backend._session = fake_session
        with pytest.raises(BackendUnavailable, match="non-final result"):
            await backend.call_tool("terminal_turn", {})

    asyncio.run(run())




def test_backend_preserves_successful_call_result_when_teardown_fails():
    """A cleanup-only transport error must never convert an executed mutation
    into BACKEND_UNAVAILABLE, because that can provoke a duplicate retry."""
    import contextlib

    async def run():
        backend = Backend("http://127.0.0.1:9/mcp", timeout_seconds=1)
        expected = types.CallToolResult(
            content=[types.TextContent(type="text", text="done")],
            is_error=False)

        class FakeSession:
            async def call_tool(self, name, arguments):
                return expected

        @contextlib.asynccontextmanager
        async def fake_session():
            yield FakeSession()
            raise BackendUnavailable("teardown failed after response")

        backend._session = fake_session
        result = await backend.call_tool("terminal_turn", {"action": "start"})
        assert result is expected

    asyncio.run(run())


def test_backend_preserves_successful_tools_list_when_teardown_fails():
    import contextlib

    async def run():
        backend = Backend("http://127.0.0.1:9/mcp", timeout_seconds=1)

        class ListResult:
            tools = [_tool("terminal_turn")]

        class FakeSession:
            async def list_tools(self):
                return ListResult()

        @contextlib.asynccontextmanager
        async def fake_session():
            yield FakeSession()
            raise BackendUnavailable("teardown failed after response")

        backend._session = fake_session
        result = await backend.list_tools()
        assert [tool.name for tool in result] == ["terminal_turn"]

    asyncio.run(run())


def test_backend_still_reports_failure_before_any_result():
    import contextlib

    async def run():
        backend = Backend("http://127.0.0.1:9/mcp", timeout_seconds=1)

        @contextlib.asynccontextmanager
        async def fake_session():
            raise BackendUnavailable("connect failed")
            yield  # pragma: no cover

        backend._session = fake_session
        with pytest.raises(BackendUnavailable, match="connect failed"):
            await backend.call_tool("terminal_turn", {})

    asyncio.run(run())

# ---------------------------------------------------------------------------
# Network posture and configuration
# ---------------------------------------------------------------------------

def test_the_sidecar_binds_loopback_only():
    """It is reached through the same authenticated tunnel the full endpoint
    is; there must be no LAN socket here to reach at all."""
    assert sidecar.BIND_HOST == "127.0.0.1"
    app = build_app(build_sidecar(FakeBackend()))
    assert app is not None


def test_backend_url_defaults_to_loopback_and_honours_the_env(monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_CHATGPT_BACKEND", raising=False)
    assert backend_url() == "http://127.0.0.1:8766/mcp"
    monkeypatch.setenv("TERMINAL_MCP_CHATGPT_BACKEND", "  http://127.0.0.1:9999/mcp  ")
    assert backend_url() == "http://127.0.0.1:9999/mcp"
    monkeypatch.setenv("TERMINAL_MCP_CHATGPT_BACKEND", "   ")
    assert backend_url() == "http://127.0.0.1:8766/mcp"


@pytest.mark.parametrize("raw,expected", [
    (None, 8768), ("", 8768), ("  ", 8768),
    ("9100", 9100), ("not-a-port", 8768), ("0", 8768), ("70000", 8768), ("-1", 8768),
])
def test_sidecar_port_never_crashes_on_a_bad_env(monkeypatch, raw, expected):
    """A malformed port must fall back to the default, not take the process
    down on startup."""
    if raw is None:
        monkeypatch.delenv("TERMINAL_MCP_CHATGPT_PORT", raising=False)
    else:
        monkeypatch.setenv("TERMINAL_MCP_CHATGPT_PORT", raw)
    assert sidecar_port() == expected


def test_the_sidecar_port_is_not_the_controller_port():
    """Binding the controller's own port would crash-loop on EADDRINUSE and
    take the compact surface down with it."""
    assert sidecar.DEFAULT_PORT != 8766


# ---------------------------------------------------------------------------
# The incident itself
# ---------------------------------------------------------------------------

#: Exactly what the broken ChatGPT connector kept offering while the live
#: endpoint answered `tools/list` with all 293 (2026-09-19). Named so the
#: regression has a name in the test output instead of an opaque set.
LEGACY_SIX = frozenset({
    "terminal_list_sessions",
    "terminal_tail",
    "terminal_capture",
    "terminal_status",
    "terminal_send_text",
    "terminal_send_keys",
})


def test_the_connector_catalog_can_never_be_the_legacy_six():
    """THE regression test for this incident.

    Note terminal_list_sessions is in BOTH sets -- that is why this asserts
    the legacy set is not a SUBSET, rather than merely that the two differ.
    """
    catalog = set(CATALOG)
    assert catalog != LEGACY_SIX
    assert not LEGACY_SIX.issubset(catalog), "the legacy shape must not be re-offered"
    assert catalog == {"terminal_turn"}
    assert not (catalog & LEGACY_SIX), "no legacy name may be advertised at all"


def test_list_tools_cannot_return_the_legacy_six_even_if_the_backend_offers_them():
    """The backend really does publish all six. The FILTER is what stops
    them, so the fake has to offer them for this to mean anything."""
    backend = FakeBackend([_tool(n) for n in sorted(LEGACY_SIX) + list(CATALOG)])
    server = build_sidecar(backend)

    names = [tool.name for tool in _list_tools(server)]

    assert names == list(CATALOG)
    assert set(names) != LEGACY_SIX
    assert not LEGACY_SIX.issubset(set(names))
    for raw in ("terminal_tail", "terminal_capture", "terminal_status",
                "terminal_send_text", "terminal_send_keys"):
        assert raw not in names


def test_the_catalog_survives_repeated_lists_and_a_process_restart():
    """Reconnect and restart are precisely when the stale catalog came back,
    so stability across both is asserted rather than assumed."""
    backend = FakeBackend()
    server = build_sidecar(backend)

    runs = [[t.name for t in _list_tools(server)] for _ in range(3)]
    assert runs[0] == runs[1] == runs[2] == list(CATALOG)
    assert backend.list_calls == 3, "each list must really re-ask the backend"

    # A restart is a brand-new process: new Backend, new Server, no state.
    for _ in range(2):
        restarted = build_sidecar(FakeBackend())
        assert [t.name for t in _list_tools(restarted)] == list(CATALOG)


# ---------------------------------------------------------------------------
# Network posture (requirement 9)
# ---------------------------------------------------------------------------

def test_no_mcp_path_is_ever_served_on_the_lan_socket():
    """The P0 lan_route_policy closed was the FULL /mcp being reachable,
    unauthenticated, on the overlay socket. This surface must not re-open
    that under a new path -- including the versioned one."""
    from terminal_mcp import lan_route_policy

    for path in ("/mcp", "/mcp/chatgpt-v1", "/mcp/chatgpt-v1/"):
        for method in ("POST", "GET", "DELETE", "HEAD"):
            assert not lan_route_policy.is_allowed_on_lan(path, method), \
                f"{method} {path} must never be served on a LAN/overlay socket"


# ---------------------------------------------------------------------------
# The full surface is untouched
# ---------------------------------------------------------------------------

def test_the_full_surface_keeps_every_tool_including_the_ones_absent_here():
    """This feature is purely additive. The compact catalog narrows what a
    CONNECTOR sees; it must never narrow what the controller publishes."""
    from terminal_mcp.mcp_app import build_mcp

    names = {tool.name for tool in asyncio.run(build_mcp().list_tools())}

    assert len(names) >= 293, f"the full surface must stay ~293, got {len(names)}"
    assert LEGACY_SIX.issubset(names), "the full surface keeps the low-level tools"
    assert set(CATALOG).issubset(names), "the proxy forwards by name -- all must exist"
    assert "terminal_kill_session" in names, "admin tools stay on the full surface"


# ---------------------------------------------------------------------------
# Inline browser screenshot content
# ---------------------------------------------------------------------------

def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"tmcp-screenshot"


def _screenshot_result(path: Path, *, status: str = "OK") -> types.CallToolResult:
    payload = {
        "status": "OK",
        "action": "browser_screenshot",
        "result": {
            "status": status,
            "screenshot": str(path),
            "bytes": path.stat().st_size if path.exists() else None,
        },
    }
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        is_error=False,
    )


def test_explicit_browser_screenshot_appends_inline_png(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    shot = root / "shot.png"
    shot.write_bytes(_png_bytes())
    backend = FakeBackend(result=_screenshot_result(shot))
    result = _call_tool(
        build_sidecar(backend, browser_artifact_root=root),
        "terminal_turn",
        {"action": "browser_screenshot", "url": "http://127.0.0.1/"},
    )
    images = [block for block in result.content if isinstance(block, types.ImageContent)]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert base64.b64decode(images[0].data) == _png_bytes()
    assert images[0].data not in _text(result)
    assert CATALOG == ("terminal_turn",)


def test_non_screenshot_call_is_unchanged(tmp_path):
    original = types.CallToolResult(
        content=[types.TextContent(type="text", text='{"status":"OK"}')])
    result = sidecar._attach_browser_screenshot_image(
        original, "terminal_turn", {"action": "inspect"}, artifact_root=tmp_path)
    assert result is original
    assert not any(isinstance(block, types.ImageContent) for block in result.content)


def test_failed_screenshot_response_has_no_image(tmp_path):
    original = types.CallToolResult(
        content=[types.TextContent(
            type="text",
            text='{"status":"FAILED","action":"browser_screenshot","result":{"status":"ERROR","error":"SCREENSHOT_DISABLED"}}'
        )],
        is_error=False,
    )
    result = sidecar._attach_browser_screenshot_image(
        original, "terminal_turn", {"action": "browser_screenshot"},
        artifact_root=tmp_path)
    assert result is original
    assert not any(isinstance(block, types.ImageContent) for block in result.content)


@pytest.mark.parametrize("case", ["outside", "missing", "non_png", "oversized", "symlink"])
def test_unsafe_screenshot_artifacts_are_rejected(tmp_path, case):
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png_bytes())
    if case == "outside":
        shot = outside
    elif case == "missing":
        shot = root / "missing.png"
    elif case == "non_png":
        shot = root / "bad.png"
        shot.write_bytes(b"not-a-png")
    elif case == "oversized":
        shot = root / "huge.png"
        shot.write_bytes(_png_bytes() + b"x" * sidecar.INLINE_SCREENSHOT_MAX_BYTES)
    else:
        shot = root / "link.png"
        shot.symlink_to(outside)

    original = types.CallToolResult(
        content=[types.TextContent(
            type="text",
            text=json.dumps({
                "status": "OK",
                "action": "browser_screenshot",
                "result": {"status": "OK", "screenshot": str(shot)},
            }),
        )],
    )
    result = sidecar._attach_browser_screenshot_image(
        original, "terminal_turn", {"action": "browser_screenshot"},
        artifact_root=root)
    assert result.is_error is True
    assert "SCREENSHOT_ARTIFACT_REJECTED" in _text(result)
    assert not any(isinstance(block, types.ImageContent) for block in result.content)


# ---------------------------------------------------------------------------
# A queued controller is not a missing one. anyio's fail_after raises
# TimeoutError from inside a task group, so it arrives wrapped in an
# ExceptionGroup; the catch-all used to call that "not reachable", which made
# the caller retry -- and each retry enqueued another task behind the very
# provider limit that caused the wait. Measured on hp 2026-09-21:
# claude_max_concurrency 1, governor queue window 900s, this client's timeout
# 30s, 729 queued events in a day.
# ---------------------------------------------------------------------------

from terminal_mcp.chatgpt_sidecar import _looks_like_timeout


def test_a_bare_timeout_is_recognised():
    assert _looks_like_timeout(TimeoutError("slow")) is True


def test_a_timeout_wrapped_in_an_exception_group_is_recognised():
    # This is the shape anyio actually delivers.
    assert _looks_like_timeout(ExceptionGroup("tg", [TimeoutError()])) is True


def test_a_timeout_nested_two_groups_deep_is_recognised():
    inner = ExceptionGroup("inner", [TimeoutError()])
    assert _looks_like_timeout(ExceptionGroup("outer", [inner])) is True


def test_a_timeout_reached_through_cause_is_recognised():
    exc = RuntimeError("wrapper")
    exc.__cause__ = TimeoutError()
    assert _looks_like_timeout(exc) is True


def test_a_real_transport_failure_is_not_called_a_timeout():
    # Must stay false, or a genuinely unreachable controller would be reported
    # as merely busy and the caller would wait forever on nothing.
    assert _looks_like_timeout(ConnectionRefusedError()) is False
    assert _looks_like_timeout(ExceptionGroup("tg", [OSError("no route")])) is False


def test_recursion_is_bounded_on_a_self_referential_cause():
    exc = RuntimeError("loop")
    exc.__cause__ = exc
    assert _looks_like_timeout(exc) is False
