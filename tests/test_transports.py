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
    env = os.environ.copy()
    env["TERMINAL_MCP_CONFIG"] = str(
        __import__("pathlib").Path(__file__).parents[1] / "config.yaml"
    )
    env["TERMINAL_MCP_BINDINGS_DB"] = str(tmp_path_factory.mktemp("http-bindings") / "bindings.db")
    process = subprocess.Popen(
        [sys.executable, "-m", "terminal_mcp.server_http"],
        cwd=__import__("pathlib").Path(__file__).parents[1],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_port(HTTP_HOST, HTTP_PORT)
        yield process
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
    names = {tool.name for tool in tools.tools}
    assert len(names) == 13
    assert {"terminal_tail", "terminal_send_keys", "terminal_bind", "terminal_tail_bound"} <= names


@pytest.mark.anyio
async def test_http_real_handshake_tools_and_security(http_server, tmux_session_factory):
    tmux_session_factory(
        "test-http-secure",
        "bash -lc 'printf \"OPENAI_API_KEY=sk-live-secret\\n\"; "
        "printf \"Do you want to continue? [y/N] \"; read answer; sleep 10'",
    )
    tmux_session_factory("private-http", "bash -lc 'echo forbidden; sleep 10'")
    url = f"http://{HTTP_HOST}:{HTTP_PORT}{HTTP_PATH}"
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
    assert "test-http-secure" in {row["name"] for row in listed["sessions"]}
    assert "sk-live-secret" not in tail["output"]
    assert "<REDACTED>" in tail["output"]
    assert status["state"] == "WAITING_INPUT"
    assert denied["error"] == "ACCESS_DENIED"
    assert text_disabled["error"] == "INPUT_DISABLED"
    assert keys_disabled["error"] == "INPUT_DISABLED"
    assert len(tools.tools) == 13


def test_http_bind_is_fixed_loopback():
    assert HTTP_HOST == "127.0.0.1"
    assert HTTP_PATH == "/mcp"


@pytest.fixture
def anyio_backend():
    return "asyncio"
