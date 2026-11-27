"""Issue #118: idle-guarded remote dispatch across a controller/node version skew.

Live evidence (2026-11-17): the production controller 5d4ee0a hard-fails every
guarded start to a remote node with IDLE_GUARD_UNSUPPORTED (client-side, no
request). 56ccb14 adds /send-guarded, but a node agent older than that route
(Dell runs 5bd6745) answers 404. That must stay a specific, fail-closed
capability error -- never NODE_UNREACHABLE, never a fallback to the unguarded
/send, never a resend -- so monitors stop retrying and name the real owner
action (upgrade the node agent).
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from terminal_mcp.node_client import RemoteNodeClient
from tests.test_codex_direct_dispatch import rig, start  # noqa: F401 -- fixture reuse


class _Node(BaseHTTPRequestHandler):
    has_guarded_route = False
    seen: list[str] = []

    def log_message(self, *args):  # keep test output quiet
        pass

    def do_POST(self):  # noqa: N802 -- http.server API
        type(self).seen.append(self.path)
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.endswith("/send-guarded") and not type(self).has_guarded_route:
            status, body = 404, {"detail": "Not Found"}   # Starlette unknown-route answer
        else:
            status, body = 200, {"sent": True, "enter_sent": True,
                                 "delivery_state": "SUBMIT_CONFIRMED"}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def node():
    handler = type("NodeAgentStub", (_Node,), {"seen": [], "has_guarded_route": False})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, RemoteNodeClient(f"http://127.0.0.1:{server.server_port}", "inert-test-token")
    finally:
        server.shutdown()
        server.server_close()


def test_old_node_without_guarded_route_is_a_specific_unsupported_error(node):
    handler, client = node
    result = client.send_text("codex", "inert", True, require_idle=True, idempotency_key="k1")
    assert result["error"] == "IDLE_GUARD_UNSUPPORTED"
    assert result["sent"] is False and result["enter_sent"] is False
    assert result["upgrade_required"] == "node_agent"
    # Fail closed: exactly one guarded attempt, never the unguarded /send.
    assert handler.seen == ["/v1/sessions/codex/send-guarded"]


def test_new_node_with_guarded_route_is_used(node):
    handler, client = node
    handler.has_guarded_route = True
    result = client.send_text("codex", "inert", True, require_idle=True, idempotency_key="k2")
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    assert handler.seen == ["/v1/sessions/codex/send-guarded"]


def test_unguarded_send_is_unchanged_on_old_nodes(node):
    handler, client = node
    result = client.send_text("codex", "inert", True, require_idle=False)
    assert result["sent"] is True
    assert handler.seen == ["/v1/sessions/codex/send"]


def test_other_http_failures_still_raise_transport_errors(node, monkeypatch):
    from terminal_mcp.node_client import NodeClientError
    _, client = node

    def boom(*a, **kw):
        raise NodeClientError("POST x -> HTTP 401: UNAUTHORIZED", http_status=401)

    monkeypatch.setattr(client, "_request", boom)
    with pytest.raises(NodeClientError):
        client.send_text("codex", "inert", True, require_idle=True)


def test_guarded_start_reports_blocked_reason_and_owner_action_without_resend(rig):  # noqa: F811
    tools, _, controller = rig
    controller.send_result = {"error": "IDLE_GUARD_UNSUPPORTED", "sent": False,
                              "enter_sent": False, "upgrade_required": "node_agent"}
    result = start(tools)
    assert result["status"] == "FAILED"
    assert result["dispatched"] is False
    assert result["error"] == "IDLE_GUARD_UNSUPPORTED"
    assert "supervise" in result["next_action"] and "node agent" in result["next_action"]
    assert result.get("task_id") is None            # never fake a durable task
    assert len(controller.send_calls) == 1
    # Same request again: idempotent replay, still no second send.
    assert start(tools) == result
    assert len(controller.send_calls) == 1
