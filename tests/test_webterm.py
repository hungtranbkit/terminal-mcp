"""Web terminal (webterm.py + dashboard.py's /dashboard/ws/terminal): a
real browser-facing xterm.js session attached directly to an EXISTING
tmux session's own pty over a WebSocket.

Every test here uses a real, disposable tmux session (webterm-smoke-*,
via tmux_session_factory) and a real WebSocket connection through
Starlette's TestClient -- this project's other test files already insist
on live-CLI-verified coverage for tmux-facing code, and this feature had
none before this file. In particular this is what actually proves
webterm.py's own claim ("closing this process... is exactly equivalent to
any other tmux client disconnecting... never calls kill-session") against
a real tmux session, not just documents it.

Never uses the literal name of a real, live session (e.g. "terminal-mcp")
-- see conftest.py's tmux_session_factory docstring for why that is
never safe on this project's own deployment.
"""
from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, DashboardConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.lease import PaneLeaseStore
from terminal_mcp.mcp_app import build_mcp

ORIGIN_HEADERS = {"Origin": "http://testserver"}


def _service(tmp_path, *, input_enabled: bool = True) -> TerminalService:
    config = AppConfig(
        permissions=PermissionsConfig(True, input_enabled),
        allowed_session_patterns=("webterm-smoke-*",),
        max_capture_lines=200,
        default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("webterm-smoke-*",)),
        dashboard=DashboardConfig(web_terminal_enabled=True),
    )
    # Isolated, tmp_path-scoped stores -- never the real ~/.local/state/
    # terminal-mcp/*.db this process's own live deployment uses, so this
    # suite adds no test rows to production audit/grant/binding/lease data.
    return TerminalService(
        config,
        bindings=BindingStore(tmp_path / "bindings.db"),
        audit=AuditStore(tmp_path / "audit.db"),
        grants=SessionGrantStore(tmp_path / "grants.db"),
        leases=PaneLeaseStore(tmp_path / "leases.db"),
    )


def _client(service: TerminalService) -> TestClient:
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app(), headers=ORIGIN_HEADERS)


def _drain_until(ws, needle: bytes, *, deadline_seconds: float = 8.0) -> bytes:
    """Accumulates BINARY pty-output frames off `ws` until `needle` shows
    up, or fails the test -- never a silent pass on missing output. Real
    pty output arrives however tmux/the shell chooses to chunk/flush it,
    never as one frame, so this must accumulate rather than check any one
    frame in isolation."""
    deadline = time.monotonic() + deadline_seconds
    buffer = b""
    while time.monotonic() < deadline:
        message = ws.receive()
        if message.get("type") == "websocket.disconnect":
            pytest.fail(f"websocket disconnected before seeing {needle!r}; got so far: {buffer!r}")
        data = message.get("bytes")
        if data is not None:
            buffer += data
            if needle in buffer:
                return buffer
    pytest.fail(f"timed out waiting for {needle!r}; got so far: {buffer!r}")


def test_webterm_fails_closed_on_missing_session_never_auto_creates(tmp_path):
    service = _service(tmp_path)
    client = _client(service)
    missing = "webterm-smoke-does-not-exist"
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/dashboard/ws/terminal?session={missing}"):
            pass
    assert excinfo.value.code == 4404
    # The one thing this must never do on a not-found session: create it.
    assert service.tmux.get_session(missing) is None


def test_webterm_smoke_attach_type_output_detach_reconnect(tmp_path, tmux_session_factory):
    name = tmux_session_factory("webterm-smoke-basic", "bash")
    service = _service(tmp_path)
    client = _client(service)

    # 1) Open Terminal attaches to the EXISTING session -- this call never
    # creates one; `name` already exists purely via the fixture above.
    with client.websocket_connect(f"/dashboard/ws/terminal?session={name}") as ws:
        ready = ws.receive_json()
        assert ready == {"type": "ready", "session": name, "readonly": False, "attached": False}

        # 2) Type one harmless command, see its output -- a real,
        # interactive, read-WRITE attach to the actual pane, not a
        # read-only tail of already-captured text.
        ws.send_bytes(b"echo webterm_smoke_marker_one\n")
        _drain_until(ws, b"webterm_smoke_marker_one")

        # State that only survives a *reconnect* if it attaches to this
        # SAME session/process rather than a fresh one.
        ws.send_bytes(b"export SMOKE_STATE=reconnect_sees_this\n")
        _drain_until(ws, b"SMOKE_STATE=reconnect_sees_this")

    # 3) Closing the connection detaches the client only -- the tmux
    # session and its shell process are untouched.
    info = service.tmux.get_session(name)
    assert info is not None, "closing the web terminal must never kill the tmux session"
    assert info.pane_dead is False, "closing the web terminal must never kill the pane's process"
    assert info.pane_current_command.casefold() == "bash", "must never restart/replace the agent process"

    # 4) Reconnect to the SAME session and see the same history/state --
    # SMOKE_STATE set before the disconnect is still set, proving this is
    # the same shell process, not a freshly (re)created one.
    with client.websocket_connect(f"/dashboard/ws/terminal?session={name}") as ws:
        ready = ws.receive_json()
        assert ready["readonly"] is False
        ws.send_bytes(b"echo STATE_IS=$SMOKE_STATE\n")
        _drain_until(ws, b"STATE_IS=reconnect_sees_this")

    info_after = service.tmux.get_session(name)
    assert info_after is not None
    assert info_after.pane_dead is False


def test_webterm_read_only_client_cannot_type(tmp_path, tmux_session_factory):
    name = tmux_session_factory("webterm-smoke-readonly", "bash")
    # terminal_input disabled at the permissions layer -- the exact same
    # effective_input decision terminal_send_text itself uses (see
    # core.py's terminal_web_terminal_access docstring); never a separate,
    # weaker check for this surface.
    readonly_service = _service(tmp_path, input_enabled=False)

    with _client(readonly_service).websocket_connect(f"/dashboard/ws/terminal?session={name}") as ws:
        ready = ws.receive_json()
        assert ready["readonly"] is True
        # Attempted keystrokes from a read-only client are a silent no-op
        # (both tmux's own `-r` flag AND WebTerminalProcess.write's own
        # independent check) -- never forwarded to the pane.
        ws.send_bytes(b"echo should_never_run_readonly\n")

    # Prove it truly had no effect: a second, input-authorized connection
    # to the SAME real session never sees it land/execute.
    write_service = _service(tmp_path, input_enabled=True)
    with _client(write_service).websocket_connect(f"/dashboard/ws/terminal?session={name}") as ws2:
        ws2.receive_json()
        ws2.send_bytes(b"echo confirm_readonly_had_no_effect\n")
        buffer = _drain_until(ws2, b"confirm_readonly_had_no_effect")
        assert b"should_never_run_readonly" not in buffer
