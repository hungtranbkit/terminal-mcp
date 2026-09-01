"""P1 hardening item #7 (structured logs + request correlation IDs) and
item #11 (CSP/nosniff/Referrer-Policy/... security response headers)."""
from __future__ import annotations

import io
import json
import logging

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from terminal_mcp.logging_setup import (
    JsonLogFormatter,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    current_request_id,
)


def _make_app():
    captured = {}

    async def endpoint(request):
        captured["request_id_seen_in_handler"] = current_request_id()
        logging.getLogger("test.logger").info("handled a request")
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/thing", endpoint)])
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    return app, captured


# ---------------------------------------------------------------------------
# RequestIdMiddleware
# ---------------------------------------------------------------------------


def test_request_id_is_minted_and_echoed_when_absent():
    app, captured = _make_app()
    client = TestClient(app)
    response = client.get("/thing")
    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id
    assert captured["request_id_seen_in_handler"] == request_id


def test_inbound_request_id_is_forwarded_not_replaced():
    app, captured = _make_app()
    client = TestClient(app)
    response = client.get("/thing", headers={"X-Request-Id": "caller-supplied-id-123"})
    assert response.headers.get("x-request-id") == "caller-supplied-id-123"
    assert captured["request_id_seen_in_handler"] == "caller-supplied-id-123"


def test_request_id_is_reset_after_request_completes():
    app, _captured = _make_app()
    client = TestClient(app)
    client.get("/thing", headers={"X-Request-Id": "leaked-if-not-reset"})
    assert current_request_id() is None


def test_two_concurrent_requests_never_share_a_request_id():
    import concurrent.futures

    app, _captured = _make_app()
    client = TestClient(app)

    def _get(rid: str) -> str:
        return client.get("/thing", headers={"X-Request-Id": rid}).headers["x-request-id"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_get, [f"req-{i}" for i in range(4)]))
    assert results == [f"req-{i}" for i in range(4)]


# ---------------------------------------------------------------------------
# SecurityHeadersMiddleware
# ---------------------------------------------------------------------------


def test_security_headers_present_on_every_response():
    app, _captured = _make_app()
    client = TestClient(app)
    response = client.get("/thing")
    assert "self'" in response.headers.get("content-security-policy", "")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("referrer-policy") == "no-referrer"
    assert "geolocation=()" in response.headers.get("permissions-policy", "")


def test_csp_locks_down_object_base_and_frame_ancestors():
    app, _captured = _make_app()
    client = TestClient(app)
    csp = client.get("/thing").headers["content-security-policy"]
    assert "object-src 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "default-src 'self'" in csp


# ---------------------------------------------------------------------------
# JsonLogFormatter
# ---------------------------------------------------------------------------


def test_json_formatter_emits_valid_json_with_expected_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test.json.formatter")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("something happened", extra={"watch_key": "session:demo", "iteration": 3})

    line = stream.getvalue().strip()
    payload = json.loads(line)  # must be valid JSON, not just JSON-ish text
    assert payload["message"] == "something happened"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.json.formatter"
    assert payload["watch_key"] == "session:demo"
    assert payload["iteration"] == 3
    assert "timestamp" in payload


def test_json_formatter_includes_request_id_and_exc_info_when_present():
    from terminal_mcp.logging_setup import bind_request_id, reset_request_id, _RequestIdFilter

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(_RequestIdFilter())
    logger = logging.getLogger("test.json.formatter.reqid")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    token = bind_request_id("rid-abc123")
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("it broke")
    finally:
        reset_request_id(token)

    payload = json.loads(stream.getvalue().strip())
    assert payload["request_id"] == "rid-abc123"
    assert "ValueError: boom" in payload["exc_info"]


def test_json_formatter_never_raises_on_unserializable_extra():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("test.json.formatter.unserializable")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    class Weird:
        def __str__(self):
            return "a-weird-object"

    logger.info("carrying something odd", extra={"thing": Weird()})
    payload = json.loads(stream.getvalue().strip())
    assert payload["thing"] == "a-weird-object"
