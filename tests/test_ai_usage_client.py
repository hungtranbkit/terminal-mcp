"""ai_usage_client.py -- the pure, read-only HTTP client for the
separate 'AI Usage Monitor' local service. Uses a REAL disposable
http.server instance (never mocks urllib itself) so a genuine HTTP
round trip, real timeouts, and real malformed-response parsing are all
actually exercised -- same "real, not mocked" discipline as node_
client.py's own tests."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from terminal_mcp.ai_usage_client import AiUsageClientError, fetch_usage


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: A003 -- silence test-run noise
        pass

    def do_GET(self):  # noqa: N802
        body = self.server.response_body
        status = self.server.response_status
        if self.server.hang_seconds:
            time.sleep(self.server.hang_seconds)
        self.send_response(status)
        if isinstance(body, (bytes, bytearray)):
            payload = bytes(body)
        else:
            payload = json.dumps(body).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _server(body, *, status=200, hang_seconds=0.0):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.response_body = body
    server.response_status = status
    server.hang_seconds = hang_seconds
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def running_server():
    started = []

    def make(body, *, status=200, hang_seconds=0.0):
        server, thread = _server(body, status=status, hang_seconds=hang_seconds)
        started.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield make
    for server in started:
        server.shutdown()


def test_fetch_usage_real_round_trip(running_server):
    base_url = running_server({"app_version": "0.6.0", "codex": {"ok": True}})
    result = fetch_usage(base_url)
    assert result["app_version"] == "0.6.0"
    assert result["codex"]["ok"] is True


def test_fetch_usage_http_error(running_server):
    base_url = running_server({"error": "boom"}, status=500)
    with pytest.raises(AiUsageClientError):
        fetch_usage(base_url)


def test_fetch_usage_invalid_json(running_server):
    base_url = running_server(b"not json at all")
    with pytest.raises(AiUsageClientError, match="invalid JSON"):
        fetch_usage(base_url)


def test_fetch_usage_non_dict_response(running_server):
    base_url = running_server([1, 2, 3])
    with pytest.raises(AiUsageClientError, match="not a JSON object"):
        fetch_usage(base_url)


def test_fetch_usage_connection_refused():
    # A real closed port -- no server listening at all.
    with pytest.raises(AiUsageClientError):
        fetch_usage("http://127.0.0.1:1", timeout=1.0)


def test_fetch_usage_timeout(running_server):
    base_url = running_server({"ok": True}, hang_seconds=2.0)
    with pytest.raises(AiUsageClientError):
        fetch_usage(base_url, timeout=0.2)
