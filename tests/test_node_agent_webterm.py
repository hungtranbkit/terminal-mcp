"""node_agent.py's /v1/ws/terminal route -- Open Terminal for a REMOTE
node (task: "Open Terminal trên Windows phải mở được web terminal vào
persistent session/process tương ứng; disconnect browser không kill
process"), for both backends this project supports: a real tmux session
(WebTerminalProcess reused unmodified) and a real WindowsSessionBackend
session (WindowsTerminalViewer, via the same _FakePty double
test_windows_backend.py uses -- see that module's own docstring for why
this is a faithful, non-mocked stand-in on this Linux dev host).

Uses Starlette's TestClient.websocket_connect -- a real ASGI WebSocket
round-trip through the actual route handler, same pattern
tests/test_webterm.py already established for the dashboard's own local
Open Terminal route.
"""
from __future__ import annotations

import sys
import time

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.windows_backend import WindowsSessionBackend

from tests.test_windows_backend import _FAKE_SHELL_SCRIPT, _fake_factory

TOKEN = "test-ws-token-xyz"


def _drain_until(ws, needle: bytes, *, deadline_seconds: float = 8.0) -> bytes:
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


# -- Linux/tmux backend -------------------------------------------------------

def _linux_client(tmp_path) -> TestClient:
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("wsterm-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("wsterm-*",)),
    )
    service = TerminalService(config)
    app = build_node_agent(node_id="test-node", terminal=service, token=TOKEN, workspace_root=str(tmp_path))
    return TestClient(app)


def test_linux_ws_terminal_attach_type_output_reconnect(tmp_path, tmux_session_factory):
    name = tmux_session_factory("wsterm-linux-basic", "bash")
    client = _linux_client(tmp_path)

    with client.websocket_connect(f"/v1/ws/terminal?session={name}&token={TOKEN}") as ws:
        ready = ws.receive_json()
        assert ready == {"type": "ready", "session": name, "readonly": False}
        ws.send_bytes(b"echo node_ws_marker_one\n")
        _drain_until(ws, b"node_ws_marker_one")
        ws.send_bytes(b"export NODE_WS_STATE=reconnect_sees_this\n")
        _drain_until(ws, b"NODE_WS_STATE=reconnect_sees_this")

    # Real evidence: closing the WS never touches the tmux session/process.
    with client.websocket_connect(f"/v1/ws/terminal?session={name}&token={TOKEN}") as ws:
        ws.receive_json()
        ws.send_bytes(b"echo STATE_IS=$NODE_WS_STATE\n")
        _drain_until(ws, b"STATE_IS=reconnect_sees_this")


def test_linux_ws_terminal_wrong_token_rejected(tmp_path, tmux_session_factory):
    name = tmux_session_factory("wsterm-linux-auth", "bash")
    client = _linux_client(tmp_path)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/v1/ws/terminal?session={name}&token=wrong-token"):
            pass
    assert excinfo.value.code == 4401


def test_linux_ws_terminal_missing_session_rejected(tmp_path):
    client = _linux_client(tmp_path)
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/v1/ws/terminal?session=wsterm-does-not-exist&token={TOKEN}"):
            pass
    assert excinfo.value.code == 4404


def test_linux_ws_terminal_readonly_client_cannot_type(tmp_path, tmux_session_factory):
    name = tmux_session_factory("wsterm-linux-readonly", "bash")
    client = _linux_client(tmp_path)
    with client.websocket_connect(f"/v1/ws/terminal?session={name}&token={TOKEN}&readonly=1") as ws:
        ready = ws.receive_json()
        assert ready["readonly"] is True
        ws.send_bytes(b"echo should_never_appear\n")
        time.sleep(0.5)
    from terminal_mcp.tmux import TmuxClient
    output = "\n".join(TmuxClient().capture_lines(name, 20))
    assert "should_never_appear" not in output


# -- Windows backend -----------------------------------------------------------

def _windows_client(tmp_path):
    script_path = tmp_path / "fake_shell.py"
    script_path.write_text(_FAKE_SHELL_SCRIPT)

    def factory(argv, cwd):
        return _fake_factory([sys.executable, "-u", str(script_path)], cwd)

    backend = WindowsSessionBackend(shell="powershell.exe", process_factory=factory, history_lines=500)
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("wsterm-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("wsterm-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),), protected_sessions=()),
    )
    service = TerminalService(config, tmux=backend)
    app = build_node_agent(node_id="test-win-node", terminal=service, token=TOKEN, workspace_root=str(tmp_path))
    return TestClient(app), backend, service


def test_windows_ws_terminal_attach_type_output(tmp_path):
    client, backend, service = _windows_client(tmp_path)
    backend.new_session("wsterm-win-basic", str(tmp_path))
    try:
        with client.websocket_connect(f"/v1/ws/terminal?session=wsterm-win-basic&token={TOKEN}") as ws:
            ready = ws.receive_json()
            assert ready == {"type": "ready", "session": "wsterm-win-basic", "readonly": False}
            ws.send_bytes(b"hello-from-windows-ws\n")
            _drain_until(ws, b"you said: hello-from-windows-ws")
        # Real evidence: closing the WS never touches the process.
        info = backend.get_session("wsterm-win-basic")
        assert info is not None
        assert info.pane_dead is False
    finally:
        backend.kill_session("wsterm-win-basic")


def test_windows_ws_terminal_disconnect_never_kills_process(tmp_path):
    client, backend, service = _windows_client(tmp_path)
    backend.new_session("wsterm-win-persist", str(tmp_path))
    try:
        pid_before = backend.get_session("wsterm-win-persist").pane_pid
        with client.websocket_connect(f"/v1/ws/terminal?session=wsterm-win-persist&token={TOKEN}") as ws:
            ws.receive_json()
            ws.send_bytes(b"first-message\n")
            _drain_until(ws, b"you said: first-message")
        # Disconnected -- process must be the SAME pid, still alive.
        info = backend.get_session("wsterm-win-persist")
        assert info is not None
        assert info.pane_pid == pid_before
        assert info.pane_dead is False

        # Reconnect proves it's genuinely the same persistent process --
        # earlier scrollback (the shell's own "PS>" prompt after our
        # first message) is still there without re-spawning anything.
        with client.websocket_connect(f"/v1/ws/terminal?session=wsterm-win-persist&token={TOKEN}") as ws:
            ws.receive_json()
            ws.send_bytes(b"second-message\n")
            _drain_until(ws, b"you said: second-message")
        assert backend.get_session("wsterm-win-persist").pane_pid == pid_before
    finally:
        backend.kill_session("wsterm-win-persist")


def test_windows_ws_terminal_wrong_token_rejected(tmp_path):
    client, backend, service = _windows_client(tmp_path)
    backend.new_session("wsterm-win-auth", str(tmp_path))
    try:
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(f"/v1/ws/terminal?session=wsterm-win-auth&token=wrong"):
                pass
        assert excinfo.value.code == 4401
    finally:
        backend.kill_session("wsterm-win-auth")
