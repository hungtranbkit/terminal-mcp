"""Structured JSON logging + HTTP request correlation IDs -- P1 hardening
item #7.

Every log record this process emits (from the dashboard, health checks,
the supervisor poll loop, tmux/core, everything -- this configures the
ROOT logger) comes out as one JSON object per line: timestamp, level,
logger name, message, and a request_id whenever the log statement happens
during an HTTP request (attached automatically via a logging.Filter
reading a contextvar -- no call site has to thread it through by hand).
watch_key/action_id-style correlation for the supervisor's own background
work is handled the same way: pass them via logging's `extra=` kwarg at
the call site (e.g. `_LOGGER.info("...", extra={"watch_key": key})`) and
they show up as top-level JSON fields, same mechanism as request_id.
"""
from __future__ import annotations

import contextvars
import json
import logging
import uuid
from typing import Any

_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

# Every attribute a plain logging.LogRecord already carries -- anything
# else found on a record (request_id set by the filter below, or whatever
# a call site passed via extra=) is "extra" and gets promoted to a
# top-level JSON field.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def current_request_id() -> str | None:
    return _request_id_var.get()


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_request_id(request_id: str) -> contextvars.Token:
    return _request_id_var.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _request_id_var.reset(token)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()  # type: ignore[attr-defined]
        return True


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRS and key != "request_id":
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # default=str: a caller's extra={} may include something not
        # natively JSON-serializable (a dataclass, an exception object) --
        # never let a logging call itself raise over that, just stringify it.
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Call once, at process startup. Replaces the root logger's handlers
    with a single structured JSON stream handler -- every module in this
    project (and any library that logs through the stdlib) inherits it."""
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(_RequestIdFilter())
    root.handlers[:] = [handler]


class RequestIdMiddleware:
    """ASGI middleware: forwards an inbound X-Request-Id or mints a fresh
    one, binds it to a contextvar for the lifetime of the request (so
    every log statement anywhere in this request's call stack picks it up
    automatically via _RequestIdFilter, no explicit threading required),
    and echoes it back as a response header for client-side correlation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        inbound = headers.get(b"x-request-id")
        request_id = inbound.decode("latin-1", errors="replace")[:128] if inbound else new_request_id()
        token = bind_request_id(request_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), (b"x-request-id", request_id.encode("latin-1"))]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)


class SecurityHeadersMiddleware:
    """ASGI middleware -- P1 hardening item #11: CSP, X-Content-Type-
    Options, Referrer-Policy and related safe headers on every response.

    CSP here allows 'unsafe-inline' for script-src/style-src: the
    dashboard is served as one static, pre-rendered HTML/CSS/JS document
    (DASHBOARD_HTML) with inline <script>/<style>, not per-request
    templated -- eliminating 'unsafe-inline' would need a nonce/hash
    threaded through that generation, a real but separately-scoped rework
    of how the dashboard is built, not a header-only change. Everything
    else here (default-src 'self', object/base/frame-ancestors locked
    down, nosniff, no-referrer, X-Frame-Options already set per-route by
    dashboard.py) is real hardening with zero UX cost."""

    _CSP = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'; "
        "form-action 'self'"
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                extra = [
                    (b"content-security-policy", self._CSP.encode("latin-1")),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
                ]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_wrapper)
