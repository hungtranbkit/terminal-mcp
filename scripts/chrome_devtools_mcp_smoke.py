#!/usr/bin/env python3
"""Smoke-test Chrome DevTools MCP against only a new example.com tab."""
import json
import os
import re
import select
import subprocess
import time

CMD = ["npx", "-y", os.environ.get("CHROME_DEVTOOLS_MCP_PACKAGE", "chrome-devtools-mcp@1.10.1"),
       "--browser-url=" + os.environ.get("CHROME_DEVTOOLS_MCP_URL", "http://127.0.0.1:9222"),
       "--no-usage-statistics", "--no-performance-crux"]
p = subprocess.Popen(CMD, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     stderr=subprocess.DEVNULL, bufsize=0)
buffer = b""
next_id = 0

def send(obj):
    p.stdin.write((json.dumps(obj, separators=(",", ":")) + "\n").encode())
    p.stdin.flush()

def request(method, params=None, timeout=35):
    global next_id, buffer
    next_id += 1
    wanted = next_id
    send({"jsonrpc": "2.0", "id": wanted, "method": method, "params": params or {}})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            reply = json.loads(raw)
            if reply.get("id") == wanted:
                if "error" in reply:
                    raise RuntimeError(str(reply["error"])[:250])
                return reply["result"]
        wait = max(0, deadline - time.monotonic())
        readable, _, _ = select.select([p.stdout.fileno()], [], [], wait)
        if readable:
            chunk = os.read(p.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Server closed stdout")
            buffer += chunk
    raise TimeoutError(method + " timed out")

def tool(name, args=None, timeout=35):
    result = request("tools/call", {"name": name, "arguments": args or {}}, timeout)
    if result.get("isError"):
        raise RuntimeError(name + ": " + str(result.get("content", ""))[:250])
    return "\n".join(x.get("text", "") for x in result.get("content", []) if x.get("type") == "text")

try:
    info = request("initialize", {"protocolVersion": "2025-06-18",
        "capabilities": {}, "clientInfo": {"name": "codex-harness-qa-smoke", "version": "1.0"}}, 30)
    send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    names = [t["name"] for t in request("tools/list", timeout=30)["tools"]]
    expected = ["new_page", "take_snapshot", "list_console_messages",
                "list_network_requests", "evaluate_script", "close_page"]
    missing = [x for x in expected if x not in names]
    print("PROTOCOL_PASS", info.get("protocolVersion"), "TOOL_COUNT", len(names), flush=True)
    if missing:
        raise RuntimeError("MISSING_TOOLS " + ",".join(missing))
    created = tool("new_page", {"url": "https://example.com"}, 50)
    found = re.search(r"(?m)^[ ]*([0-9]+):.*example[.]com", created)
    if not found:
        raise RuntimeError("NEW_PAGE_ID_NOT_FOUND")
    page_args = {"pageId": int(found.group(1))}
    snap = tool("take_snapshot", page_args, timeout=30)
    heading = tool("evaluate_script",
                   {"pageId": page_args["pageId"], "function": "() => document.title"}, 30)
    console = tool("list_console_messages", page_args, timeout=30)
    network = tool("list_network_requests", page_args, timeout=30)
    print("NEW_PAGE_PASS", "https://example.com" in created, flush=True)
    print("DOM_PASS", "Example Domain" in snap and "Example Domain" in heading, flush=True)
    screenshot = request("tools/call", {"name": "take_screenshot", "arguments": page_args}, 35)
    image_ok = any(c.get("type") == "image" for c in screenshot.get("content", [])) and not screenshot.get("isError")
    print("SCREENSHOT_PASS", image_ok, flush=True)
    print("CONSOLE_PASS", isinstance(console, str), flush=True)
    print("NETWORK_PASS", "example.com" in network, flush=True)
    pages = tool("list_pages")
    lines = [line for line in pages.splitlines() if "example.com" in line and "[selected]" in line]
    if len(lines) == 1 and (match := re.search(r"^\s*(\d+):", lines[0])):
        tool("close_page", {"pageId": int(match.group(1))})
        print("CLEANUP_PASS", flush=True)
    else:
        raise RuntimeError("CLEANUP_UNCONFIRMED")
    if "Example Domain" not in snap or "Example Domain" not in heading or "example.com" not in network or not image_ok:
        raise RuntimeError("REQUIRED_ASSERTION_FAILED")
    print("MCP_SMOKE_PASS", flush=True)
finally:
    p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
