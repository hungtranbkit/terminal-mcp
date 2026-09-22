#!/usr/bin/env python3
"""Bounded Terminal MCP round-trip benchmark (stdlib only).

Measures transport-inclusive latency and response size for safe read calls.
Pass --target to include status/tail/batch inspect for one explicit session.
The script never sends input or mutates server state.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from typing import Any, Callable


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, int(len(ordered) * fraction) - 1))]


class Client:
    def __init__(self, base_url: str, *, host_header: str | None, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Accept": "application/json, text/event-stream"}
        if host_header:
            self.headers["Host"] = host_header
        _, _, response_headers = self.request(
            "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "terminal-mcp-roundtrip-benchmark", "version": "1"},
            }})
        self.headers["Mcp-Session-Id"] = response_headers.get("Mcp-Session-Id")

    def request(self, path: str, payload: dict[str, Any] | None = None):
        headers = dict(self.headers)
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers)
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            content = response.read()
            response_headers = response.headers
        return (time.perf_counter() - started) * 1000.0, content, response_headers

    def mcp(self, method: str, params: dict[str, Any] | None = None):
        return self.request("/mcp", {"jsonrpc": "2.0", "id": 2, "method": method,
                                     "params": params or {}})

    def tool(self, name: str, arguments: dict[str, Any] | None = None):
        return self.mcp("tools/call", {"name": name, "arguments": arguments or {}})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--host-header")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--target", help="Prefer node/session to avoid measuring a fleet cache miss")
    args = parser.parse_args()
    if not 1 <= args.samples <= 100:
        parser.error("--samples must be between 1 and 100")

    client = Client(args.base_url, host_header=args.host_header, timeout=args.timeout)
    checks: list[tuple[str, Callable[[], tuple[float, bytes, Any]]]] = [
        ("health_ready", lambda: client.request("/health/ready")),
        ("version", lambda: client.request("/version")),
        ("tools_list", lambda: client.mcp("tools/list")),
        ("terminal_list_nodes", lambda: client.tool("terminal_list_nodes")),
        ("terminal_list_sessions", lambda: client.tool("terminal_list_sessions")),
    ]
    if args.target:
        checks.extend([
            ("terminal_status", lambda: client.tool("terminal_status", {"session": args.target})),
            ("terminal_tail", lambda: client.tool("terminal_tail", {"session": args.target, "lines": 20})),
            ("terminal_batch_inspect", lambda: client.tool(
                "terminal_batch_inspect", {"targets": [args.target], "tail_lines": 20, "compact": True})),
        ])

    results = []
    for name, operation in checks:
        timings: list[float] = []
        sizes: list[int] = []
        errors = 0
        for _ in range(args.samples):
            try:
                elapsed, body, _ = operation()
                timings.append(elapsed)
                sizes.append(len(body))
            except Exception:  # one failure is counted; the bounded run continues
                errors += 1
        results.append({
            "operation": name,
            "samples": len(timings),
            "errors": errors,
            "error_rate": round(errors / args.samples, 4),
            "p50_ms": round(statistics.median(timings), 2) if timings else None,
            "p95_ms": round(percentile(timings, 0.95), 2) if timings else None,
            "max_ms": round(max(timings), 2) if timings else None,
            "median_bytes": int(statistics.median(sizes)) if sizes else 0,
        })
    print(json.dumps({"base_url": args.base_url, "samples_per_operation": args.samples,
                      "results": results}, indent=2))


if __name__ == "__main__":
    main()
