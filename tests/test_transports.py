from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from terminal_mcp import __version__
from terminal_mcp.server_http import HTTP_HOST, HTTP_PATH, HTTP_PORT


def result_json(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def wait_for_port(host: str, port: int, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.1)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"HTTP MCP did not listen on {host}:{port}")


@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    with socket.socket() as probe:
        probe.bind((HTTP_HOST, 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env["TERMINAL_MCP_CONFIG"] = str(
        __import__("pathlib").Path(__file__).parents[1] / "config.yaml"
    )
    env["TERMINAL_MCP_BINDINGS_DB"] = str(tmp_path_factory.mktemp("http-bindings") / "bindings.db")
    env["TERMINAL_MCP_AUDIT_DB"] = str(tmp_path_factory.mktemp("http-audit") / "audit.db")
    launch = (
        "from terminal_mcp.mcp_app import build_mcp; "
        f"build_mcp().run(transport='streamable-http', host='{HTTP_HOST}', port={port}, "
        f"streamable_http_path='{HTTP_PATH}', json_response=True)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", launch],
        cwd=__import__("pathlib").Path(__file__).parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_port(HTTP_HOST, port)
        yield process, port
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.mark.anyio
async def test_stdio_real_handshake_and_tools(tmp_path):
    root = __import__("pathlib").Path(__file__).parents[1]
    params = StdioServerParameters(
        command=str(root / ".venv/bin/terminal-mcp"),
        cwd=str(root),
        env={"TERMINAL_MCP_CONFIG": str(root / "config.yaml"),
             "TERMINAL_MCP_BINDINGS_DB": str(tmp_path / "bindings.db")},
    )
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
    assert initialized.server_info.name == "terminal-mcp"
    assert initialized.server_info.version == __version__
    names = {tool.name for tool in tools.tools}
    assert len(names) == 33
    assert {"terminal_tail", "terminal_send_keys", "terminal_exit_copy_mode",
            "terminal_bind", "terminal_tail_bound"} <= names


@pytest.mark.anyio
async def test_http_real_handshake_tools_and_security(http_server, tmux_session_factory):
    tmux_session_factory(
        "test-http-secure",
        "bash -lc 'printf \"OPENAI_API_KEY=sk-live-secret\\n\"; "
        "printf \"Do you want to continue? [y/N] \"; read answer; sleep 10'",
    )
    tmux_session_factory("private-http", "bash -lc 'echo forbidden; sleep 10'")
    _, port = http_server
    url = f"http://{HTTP_HOST}:{port}{HTTP_PATH}"
    async with streamable_http_client(url) as streams:
        async with ClientSession(*streams) as session:
            initialized = await session.initialize()
            listed = result_json(await session.call_tool("terminal_list_sessions", {}))
            tail = result_json(
                await session.call_tool("terminal_tail", {"session": "test-http-secure", "lines": 20})
            )
            status = result_json(
                await session.call_tool("terminal_status", {"session": "test-http-secure"})
            )
            denied = result_json(
                await session.call_tool("terminal_tail", {"session": "private-http", "lines": 20})
            )
            text_disabled = result_json(
                await session.call_tool(
                    "terminal_send_text",
                    {"session": "test-http-secure", "text": "yes", "press_enter": True},
                )
            )
            keys_disabled = result_json(
                await session.call_tool(
                    "terminal_send_keys", {"session": "test-http-secure", "keys": ["Enter"]}
                )
            )
            tools = await session.list_tools()

    assert initialized.server_info.name == "terminal-mcp"
    assert initialized.server_info.version == __version__
    assert "test-http-secure" in {row["name"] for row in listed["sessions"]}
    assert "sk-live-secret" not in tail["output"]
    assert "<REDACTED>" in tail["output"]
    assert status["state"] == "WAITING_INPUT"
    assert denied["error"] == "ACCESS_DENIED"
    # config.yaml now enables permissions.terminal_input globally (for ChatGPT input),
    # so "test-http-secure" is denied via input_policy pattern matching rather than the
    # global INPUT_DISABLED gate; INPUT_DISABLED itself stays covered in test_permissions.py.
    assert text_disabled["error"] == "ACCESS_DENIED"
    assert keys_disabled["error"] == "ACCESS_DENIED"
    assert len(tools.tools) == 33


def test_http_bind_is_fixed_loopback():
    assert HTTP_HOST == "127.0.0.1"
    assert HTTP_PATH == "/mcp"


def test_real_server_http_main_wires_request_id_and_security_headers(tmp_path_factory):
    # Unlike the http_server fixture above (which calls build_mcp().run()
    # directly, bypassing server_http.main() entirely), this spawns the
    # REAL production entrypoint -- the only path that actually adds
    # RequestIdMiddleware/SecurityHeadersMiddleware (P1 items #7/#11),
    # since server.run() itself exposes no middleware hook. HTTP_PORT is
    # patched in-process (before main() is called, in the SAME subprocess)
    # rather than hardcoded, so this can never collide with the real
    # production service's fixed port 8766.
    with socket.socket() as probe:
        probe.bind((HTTP_HOST, 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    root = __import__("pathlib").Path(__file__).parents[1]
    env["TERMINAL_MCP_CONFIG"] = str(root / "config.yaml")
    env["TERMINAL_MCP_BINDINGS_DB"] = str(tmp_path_factory.mktemp("mainpath-bindings") / "bindings.db")
    env["TERMINAL_MCP_AUDIT_DB"] = str(tmp_path_factory.mktemp("mainpath-audit") / "audit.db")
    env["TERMINAL_MCP_SUPERVISOR_DB"] = str(tmp_path_factory.mktemp("mainpath-supervisor") / "supervisor.db")
    launch = f"import terminal_mcp.server_http as m; m.HTTP_PORT = {port}; m.main()"
    process = subprocess.Popen(
        [sys.executable, "-c", launch], cwd=root, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        import urllib.request

        wait_for_port(HTTP_HOST, port)
        with urllib.request.urlopen(f"http://{HTTP_HOST}:{port}/health/live", timeout=5) as resp:
            assert resp.status == 200
            assert json.loads(resp.read()) == {"status": "ok"}
            # resp.headers (email.message.Message) is case-insensitive on
            # .get() -- the middleware sends lowercase header names, an
            # HTTP client is free to send/report any case, so look up via
            # the object itself rather than a plain dict (which would lose
            # that case-insensitivity).
            request_id = resp.headers.get("X-Request-Id")
            csp = resp.headers.get("Content-Security-Policy", "")
            nosniff = resp.headers.get("X-Content-Type-Options")
            referrer_policy = resp.headers.get("Referrer-Policy")
        assert request_id and len(request_id) >= 8
        assert "default-src 'self'" in csp
        assert nosniff == "nosniff"
        assert referrer_policy == "no-referrer"

        # A caller-supplied request id is forwarded, not replaced.
        req = urllib.request.Request(f"http://{HTTP_HOST}:{port}/version", headers={"X-Request-Id": "e2e-fixed-id"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert resp.headers.get("X-Request-Id") == "e2e-fixed-id"

        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def anyio_backend():
    return "asyncio"
