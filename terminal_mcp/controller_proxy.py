"""Opt-in ASGI proxy over the existing persistent ControllerRouter.

Nothing imports or mounts this app automatically. An operator must explicitly
construct it with an affinity router and provide the deployment's existing
HTTP authentication boundary. Backend endpoints remain loopback-only under
ControllerAffinityStore's validation; requests are never failed over/replayed.
Requires the optional `httpx` package when this module is imported.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from terminal_mcp.controller_affinity import AffinityError, ControllerRouter


_HOP_BY_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_REQUEST_STRIP_HEADERS = _HOP_BY_HOP_HEADERS | {b"host", b"content-length"}
_RESPONSE_STRIP_HEADERS = _HOP_BY_HOP_HEADERS | {b"content-length"}


def _filtered_headers(
    headers: list[tuple[bytes, bytes]], strip: set[bytes]
) -> list[tuple[bytes, bytes]]:
    connection_tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            connection_tokens.update(
                token.strip().lower() for token in value.split(b",") if token.strip()
            )
    blocked = strip | connection_tokens
    return [(name, value) for name, value in headers if name.lower() not in blocked]


class _ClientDisconnected(Exception):
    pass


async def _read_body(receive: Callable[..., Any]) -> bytes:
    chunks: list[bytes] = []
    more_body = True
    while more_body:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _ClientDisconnected
        if message["type"] != "http.request":
            continue
        chunks.append(message.get("body", b""))
        more_body = message.get("more_body", False)
    return b"".join(chunks)


class ControllerProxy:
    """An ASGI app that proxies HTTP requests to a router-selected backend."""

    def __init__(
        self,
        router: ControllerRouter,
        *,
        client_factory: Callable[..., Any] = httpx.AsyncClient,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self.router = router
        self.client_factory = client_factory
        self.timeout = timeout or httpx.Timeout(
            connect=5.0, read=None, write=30.0, pool=5.0
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            raise RuntimeError("ControllerProxy only supports HTTP ASGI scopes")

        headers = list(scope.get("headers", []))
        session_id = next(
            (
                value.decode("latin-1")
                for name, value in headers
                if name.lower() == b"mcp-session-id"
            ),
            None,
        )

        try:
            backend = self.router.select_backend(session_id)
        except AffinityError as exc:
            code = str(exc.code)
            body = json.dumps({"error": code}).encode("utf-8")
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        try:
            body = await _read_body(receive)
        except _ClientDisconnected:
            return
        endpoint = str(backend["endpoint"]).rstrip("/")
        path = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
        query = scope.get("query_string", b"")
        target = endpoint.encode("ascii") + path
        if query:
            target += b"?" + query

        response_started = False
        try:
            # Never route local controller traffic through environment proxies
            # or follow a redirect carrying the caller's authorization headers.
            async with self.client_factory(timeout=self.timeout, trust_env=False,
                                           follow_redirects=False) as client:
                async with client.stream(
                    scope["method"], target.decode("ascii"),
                    headers=_filtered_headers(headers, _REQUEST_STRIP_HEADERS), content=body,
                ) as response:
                    if 200 <= response.status_code < 300:
                        issued_session = response.headers.get("mcp-session-id")
                        if issued_session:
                            # Initialization has no inbound session id. Bind the
                            # id returned by the issuer BEFORE exposing it, so a
                            # cutover cannot send its next request to a new node.
                            self.router.store.bind_session(issued_session, backend["backend_id"])
                        if scope["method"] == "DELETE" and session_id is not None:
                            self.router.store.release_session(session_id)
                    response_headers = list(response.headers.raw)
                    if response.is_stream_consumed:
                        # In-memory transports may supply an already-decoded
                        # body. The network path below streams original bytes.
                        response_headers = _filtered_headers(response_headers, {b"content-encoding"})
                    await send({"type": "http.response.start", "status": response.status_code,
                                "headers": _filtered_headers(response_headers, _RESPONSE_STRIP_HEADERS)})
                    response_started = True
                    if response.is_stream_consumed:
                        await send({"type": "http.response.body", "body": response.content,
                                    "more_body": True})
                    else:
                        async for chunk in response.aiter_raw():
                            await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        except AffinityError as exc:
            if response_started:
                raise
            await send({"type": "http.response.start", "status": 503,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({"error": exc.code}).encode()})
        except httpx.HTTPError:
            if response_started:
                # A started stream cannot be replaced by another response.
                # Propagate so the ASGI server terminates the incomplete stream.
                raise
            await send({"type": "http.response.start", "status": 502,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"Bad Gateway"})


def create_controller_proxy(
    router: ControllerRouter,
    **kwargs: Any,
) -> ControllerProxy:
    """Create a ControllerProxy ASGI application."""

    return ControllerProxy(router, **kwargs)


__all__ = ["ControllerProxy", "create_controller_proxy"]
