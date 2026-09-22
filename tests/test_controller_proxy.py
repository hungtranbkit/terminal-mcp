from __future__ import annotations

import asyncio

import httpx

from terminal_mcp.controller_affinity import AffinityError
from terminal_mcp.controller_proxy import ControllerProxy


class FakeRouter:
    def __init__(self, endpoint="http://127.0.0.1:9911"):
        self.endpoint = endpoint
        self.selections = []
        self.error = None

    def select_backend(self, session_id):
        self.selections.append(session_id)
        if self.error is not None:
            raise self.error
        return {"endpoint": self.endpoint}


def client_factory(transport):
    return lambda **kwargs: httpx.AsyncClient(transport=transport, **kwargs)


async def check_legacy_session_id_is_passed_exactly_and_selected_endpoint_is_used():
    router = FakeRouter("http://127.0.0.1:8123")

    async def backend(request):
        assert str(request.url) == "http://127.0.0.1:8123/tools/call?x=1&x=2"
        return httpx.Response(201, content=b"selected")

    app = ControllerProxy(
        router, client_factory=client_factory(httpx.MockTransport(backend))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.get(
            "/tools/call?x=1&x=2", headers={"Mcp-Session-Id": "legacy-ABC_123"}
        )

    assert router.selections == ["legacy-ABC_123"]
    assert response.status_code == 201
    assert response.content == b"selected"


async def check_request_without_session_id_passes_none():
    router = FakeRouter()
    app = ControllerProxy(
        router,
        client_factory=client_factory(
            httpx.MockTransport(lambda request: httpx.Response(204))
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 204
    assert router.selections == [None]


async def check_method_headers_body_path_and_query_are_preserved_and_unsafe_headers_stripped():
    router = FakeRouter()

    async def backend(request):
        assert request.method == "PATCH"
        assert request.url.raw_path.split(b"?", 1)[0] == b"/a%2Fb/items"
        assert request.url.query == b"order=newest%20first"
        assert request.content == b"payload"
        assert request.headers["x-safe"] == "yes"
        assert request.headers["mcp-session-id"] == "sticky"
        assert request.headers["host"] == "127.0.0.1:9911"
        assert request.headers.get("connection") != "x-remove"
        assert "x-remove" not in request.headers
        return httpx.Response(
            202,
            headers={"x-backend": "ok", "connection": "close"},
            content=b"accepted",
        )

    app = ControllerProxy(
        router, client_factory=client_factory(httpx.MockTransport(backend))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.patch(
            "/a%2Fb/items?order=newest%20first",
            headers={
                "Mcp-Session-Id": "sticky",
                "X-Safe": "yes",
                "Connection": "x-remove",
                "X-Remove": "no",
            },
            content=b"payload",
        )

    assert response.status_code == 202
    assert response.headers["x-backend"] == "ok"
    assert "connection" not in response.headers
    assert response.content == b"accepted"


async def check_affinity_error_returns_503_with_code():
    router = FakeRouter()
    router.error = AffinityError("NO_ACTIVE_BACKEND")
    app = ControllerProxy(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 503
    assert response.json() == {"error": "NO_ACTIVE_BACKEND"}


async def check_backend_io_failure_returns_502():
    router = FakeRouter()

    async def backend(request):
        raise httpx.ConnectError("offline", request=request)

    app = ControllerProxy(
        router, client_factory=client_factory(httpx.MockTransport(backend))
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 502
    assert response.text == "Bad Gateway"


def test_legacy_session_id_is_passed_exactly_and_selected_endpoint_is_used():
    asyncio.run(check_legacy_session_id_is_passed_exactly_and_selected_endpoint_is_used())


def test_request_without_session_id_passes_none():
    asyncio.run(check_request_without_session_id_passes_none())


def test_method_headers_body_path_and_query_are_preserved_and_unsafe_headers_stripped():
    asyncio.run(
        check_method_headers_body_path_and_query_are_preserved_and_unsafe_headers_stripped()
    )


def test_affinity_error_returns_503_with_code():
    asyncio.run(check_affinity_error_returns_503_with_code())


def test_backend_io_failure_returns_502():
    asyncio.run(check_backend_io_failure_returns_502())


async def check_new_session_sticks_to_issuer_across_cutover(tmp_path):
    from terminal_mcp.controller_affinity import ControllerAffinityStore, ControllerRouter, ACTIVE
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    store.upsert_backend("old", "http://127.0.0.1:8001", 1, ACTIVE)
    store.upsert_backend("new", "http://127.0.0.1:8002", 2)
    router = ControllerRouter(store)
    seen = []
    def backend(request):
        seen.append(request.url.port)
        return httpx.Response(200, headers={"Mcp-Session-Id": "issued-on-old"})
    app = ControllerProxy(router, client_factory=client_factory(httpx.MockTransport(backend)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        response = await client.post("/mcp", json={"method": "initialize"})
        router.begin_cutover("old", "new")
        await client.post("/mcp", headers={"Mcp-Session-Id": response.headers["mcp-session-id"]})
    assert seen == [8001, 8001]
    assert router.drain_status("old")["active_sessions"] == 1


def test_new_session_sticks_to_issuer_across_cutover(tmp_path):
    asyncio.run(check_new_session_sticks_to_issuer_across_cutover(tmp_path))


async def check_successful_session_delete_releases_affinity(tmp_path):
    from terminal_mcp.controller_affinity import ControllerAffinityStore, ControllerRouter, ACTIVE
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    store.upsert_backend("old", "http://127.0.0.1:8001", 1, ACTIVE)
    router = ControllerRouter(store)
    router.select_backend("session-to-close")
    app = ControllerProxy(router, client_factory=client_factory(httpx.MockTransport(
        lambda request: httpx.Response(204))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        await client.delete("/mcp", headers={"Mcp-Session-Id": "session-to-close"})
    assert store.sessions_for_backend("old") == []


def test_successful_session_delete_releases_affinity(tmp_path):
    asyncio.run(check_successful_session_delete_releases_affinity(tmp_path))


def test_proxy_streams_before_backend_finishes():
    async def check():
        first_sent = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"data: first\n\n"
                await asyncio.wait_for(first_sent.wait(), timeout=1)
                yield b"data: second\n\n"
        def backend(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())
        app = ControllerProxy(FakeRouter(), client_factory=client_factory(httpx.MockTransport(backend)))
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
            if message.get("body") == b"data: first\n\n":
                first_sent.set()
        await app({"type": "http", "method": "GET", "path": "/mcp", "headers": []}, receive, send)
        assert first_sent.is_set()
        assert b"".join(message.get("body", b"") for message in sent) == b"data: first\n\ndata: second\n\n"
    asyncio.run(check())


def test_disconnect_during_body_does_not_forward_partial_request():
    async def check():
        sent = []
        def backend(request):
            raise AssertionError("partial request must not reach backend")
        app = ControllerProxy(FakeRouter(), client_factory=client_factory(httpx.MockTransport(backend)))
        messages = iter([{"type": "http.request", "body": b"partial", "more_body": True},
                         {"type": "http.disconnect"}])
        async def receive(): return next(messages)
        async def send(message): sent.append(message)
        await app({"type": "http", "method": "POST", "path": "/mcp", "headers": []}, receive, send)
        assert sent == []
    asyncio.run(check())
