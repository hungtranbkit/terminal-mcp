import json
import os
import socket
import urllib.error
from pathlib import Path

import pytest

from terminal_mcp.camofox_provider import (CamofoxBrowser, CamofoxClient, CamofoxConfig,
                                           CamofoxError, build_from_env, read_access_key,
                                           register_camofox_tools)

KEY = "k" * 40


class FakeWorker:
    """Records requests; answers from a route table of (method, path-prefix) -> fn."""

    def __init__(self):
        self.calls = []
        self.tabs = {}
        self.routes = {}
        self.fail = []  # queue of exceptions / (status, body) to return first

    def __call__(self, method, url, body, headers, timeout):
        self.calls.append({"method": method, "url": url, "body": json.loads(body) if body else None,
                           "auth": headers.get("Authorization")})
        if self.fail:
            item = self.fail.pop(0)
            if isinstance(item, Exception):
                raise item
            return item[0], json.dumps(item[1]).encode(), "application/json"
        path = url.split("127.0.0.1:9377", 1)[1]
        for (m, prefix), fn in sorted(self.routes.items(), key=lambda kv: -len(kv[0][1])):
            if m == method and path.startswith(prefix):
                status, payload = fn(path, json.loads(body) if body else None)
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return status, raw, "application/json"
        return 404, b'{"error":"no route"}', "application/json"


def make(worker=None, **overrides):
    worker = worker or FakeWorker()
    config = CamofoxConfig(enabled=True, **overrides)
    client = CamofoxClient(config, transport=worker, key_loader=lambda _p: KEY, sleep=lambda _s: None)
    return CamofoxBrowser(config, client=client, resolve_dns=False), worker


def standard_routes(worker, landed_url="https://example.com/"):
    worker.routes[("GET", "/tabs?")] = lambda p, b: (200, {"tabs": list(worker.tabs.values())})
    def create(p, b):
        worker.tabs["t1"] = {"tabId": "t1", "url": landed_url, "title": "Example Domain"}
        return 200, {"tabId": "t1", "url": landed_url, "httpStatus": 200, "navigationOk": True}
    worker.routes[("POST", "/tabs")] = create
    worker.routes[("GET", "/tabs/t1/snapshot")] = lambda p, b: (200, {
        "url": landed_url, "snapshot": "- paragraph: Example Domain token=abc123secretvalue",
        "refsCount": 1, "truncated": False, "totalChars": 50})
    worker.routes[("DELETE", "/tabs/t1")] = lambda p, b: (worker.tabs.pop("t1", None) and 200 or 200, {"ok": True})
    worker.routes[("POST", "/tabs/t1/click")] = lambda p, b: (200, {"ok": True, "url": landed_url})
    worker.routes[("POST", "/tabs/t1/type")] = lambda p, b: (200, {"ok": True})
    worker.routes[("POST", "/tabs/t1/scroll")] = lambda p, b: (200, {"ok": True})
    worker.routes[("POST", "/tabs/t1/evaluate")] = lambda p, b: (200, {"ok": True, "result": {
        "bx": 0, "by": 0, "x": 0, "y": 500, "maxX": 0, "maxY": 2000}})
    worker.routes[("GET", "/tabs/t1/screenshot")] = lambda p, b: (200, b"\x89PNG\r\n\x1a\n" + b"0" * 10)
    worker.routes[("DELETE", "/sessions/")] = lambda p, b: (200, {"ok": True})
    worker.routes[("GET", "/health")] = lambda p, b: (200, {"ok": True, "browserRunning": True,
                                                          "activeTabs": 0, "memory": {"rssMb": 10}})


def test_disabled_by_default_and_not_registered():
    assert CamofoxConfig().enabled is False
    assert build_from_env({}) is None
    provider = CamofoxBrowser(CamofoxConfig(), client=CamofoxClient(CamofoxConfig(), transport=FakeWorker()))
    result = provider.run("status")
    assert result["error"] == "CAMOFOX_DISABLED"
    assert "browser_verify" in result["fallback"]


def test_invalid_env_disables_instead_of_crashing():
    assert build_from_env({"TERMINAL_MCP_CAMOFOX_ENABLED": "1",
                           "TERMINAL_MCP_CAMOFOX_BASE_URL": "http://8.8.8.8:9377"}) is None
    assert build_from_env({"TERMINAL_MCP_CAMOFOX_ENABLED": "maybe"}) is None


@pytest.mark.parametrize("base", ["http://8.8.8.8:9377", "http://camofox.example.com",
                                  "http://u:p@127.0.0.1:9377", "ftp://127.0.0.1",
                                  "http://169.254.1.1:9377"])
def test_base_url_must_be_loopback_or_private(base):
    with pytest.raises(ValueError):
        CamofoxConfig(base_url=base).validate()


@pytest.mark.parametrize("base", ["http://127.0.0.1:9377", "http://localhost:19377",
                                  "http://100.81.85.120:9377", "http://10.0.0.5:9377"])
def test_base_url_accepts_private_endpoints(base):
    CamofoxConfig(base_url=base).validate()


def test_env_parsing():
    cfg = CamofoxConfig.from_env({"TERMINAL_MCP_CAMOFOX_ENABLED": "true",
                                  "TERMINAL_MCP_CAMOFOX_ALLOWED_DOMAINS": "Example.com, iana.org.",
                                  "TERMINAL_MCP_CAMOFOX_RETRIES": "2"})
    assert cfg.enabled and cfg.allowed_domains == ("example.com", "iana.org") and cfg.retries == 2
    with pytest.raises(ValueError):
        CamofoxConfig.from_env({"TERMINAL_MCP_CAMOFOX_MAX_TABS_PER_AGENT": "50"})


def test_access_key_file_permissions(tmp_path):
    key = tmp_path / "access.key"
    key.write_text(KEY)
    key.chmod(0o644)
    with pytest.raises(CamofoxError) as exc:
        read_access_key(str(key))
    assert exc.value.code == "CAMOFOX_KEY_UNAVAILABLE"
    key.chmod(0o600)
    assert read_access_key(str(key)) == KEY
    with pytest.raises(CamofoxError):
        read_access_key(str(tmp_path / "missing"))


@pytest.mark.parametrize("url,code", [
    ("file:///etc/passwd", "URL_SCHEME_BLOCKED"),
    ("javascript:alert(1)", "URL_SCHEME_BLOCKED"),
    ("http://169.254.169.254/latest", "URL_METADATA_BLOCKED"),
    ("http://127.0.0.1:9377/health", "URL_LOOPBACK_BLOCKED"),
    ("http://10.0.0.1/", "URL_PRIVATE_BLOCKED"),
    ("https://evil-example.com/", "CAMOFOX_DOMAIN_NOT_ALLOWED"),
    ("https://example.com.evil.net/", "CAMOFOX_DOMAIN_NOT_ALLOWED"),
    ("https://user:pw@example.com/", "URL_INVALID"),
])
def test_ssrf_and_allowlist_refused_before_any_request(url, code):
    provider, worker = make()
    result = provider.run("create_tab", agent_id="a1", url=url)
    assert result["status"] == "ERROR" and result["error"] == code
    assert worker.calls == []


def test_subdomain_of_allowed_domain_passes():
    provider, _ = make()
    assert provider.check_url("https://www.example.com/x") == "https://www.example.com/x"


def test_full_tab_lifecycle_with_isolation_and_redaction():
    provider, worker = make()
    standard_routes(worker)
    created = provider.run("create_tab", agent_id="agent-7", url="https://example.com/")
    assert created["status"] == "OK" and created["tab_id"] == "t1"
    post = [c for c in worker.calls if c["method"] == "POST"][0]
    assert post["body"]["userId"] == "tmcp-agent-7"
    assert post["auth"] == "Bearer " + KEY
    snap = provider.run("snapshot", agent_id="agent-7", tab_id="t1")
    assert "Example Domain" in snap["snapshot"] and "abc123secretvalue" not in snap["snapshot"]
    assert provider.run("scroll", agent_id="agent-7", tab_id="t1")["status"] == "OK"
    listed = provider.run("list_tabs", agent_id="agent-7")
    assert listed["count"] == 1 and listed["tabs"][0]["title"] == "Example Domain"
    assert provider.run("close_tab", agent_id="agent-7", tab_id="t1")["closed"] is True
    assert provider.run("close_all", agent_id="agent-7")["closed_all"] is True
    for result in (created, snap, listed):
        assert KEY not in json.dumps(result)


def test_mutations_require_opt_in_and_do_not_echo_text():
    provider, worker = make()
    standard_routes(worker)
    refused = provider.run("click", agent_id="a", tab_id="t1", ref="e1")
    assert refused["error"] == "CAMOFOX_MUTATION_NOT_ALLOWED" and worker.calls == []
    clicked = provider.run("click", agent_id="a", tab_id="t1", ref="e1", allow_mutations=True)
    assert clicked["status"] == "OK" and clicked["mutations"] == ["click"]
    typed = provider.run("type", agent_id="a", tab_id="t1", selector="#q", text="hunter2-password",
                         allow_mutations=True)
    assert typed["typed_chars"] == len("hunter2-password")
    assert "hunter2" not in json.dumps(typed)


@pytest.mark.parametrize("kwargs", [
    {"agent_id": "../etc", "tab_id": "t1"},
    {"agent_id": "a", "tab_id": "../../sessions/x"},
    {"agent_id": "a", "tab_id": "t1", "ref": "e1;drop"},
    {"agent_id": None, "tab_id": "t1"},
])
def test_identifier_validation(kwargs):
    provider, worker = make()
    action = "click" if "ref" in kwargs else "snapshot"
    result = provider.run(action, allow_mutations=True, **kwargs)
    assert result["error"] == "CAMOFOX_BAD_REQUEST" and worker.calls == []


def test_redirect_off_allowlist_closes_tab():
    provider, worker = make()
    standard_routes(worker, landed_url="https://evil.net/phish")
    result = provider.run("create_tab", agent_id="a", url="https://example.com/")
    assert result["error"] == "CAMOFOX_LANDED_OFF_POLICY"
    assert any(c["method"] == "DELETE" and "/tabs/t1" in c["url"] for c in worker.calls)


def test_click_navigating_off_allowlist_closes_tab():
    provider, worker = make()
    standard_routes(worker)
    worker.routes[("POST", "/tabs/t1/click")] = lambda p, b: (200, {"ok": True, "url": "http://10.1.1.1/"})
    result = provider.run("click", agent_id="a", tab_id="t1", ref="e2", allow_mutations=True)
    assert result["error"] == "CAMOFOX_LANDED_OFF_POLICY"


def test_tab_limit_per_agent():
    provider, worker = make(max_tabs_per_agent=1)
    standard_routes(worker)
    worker.tabs["t0"] = {"tabId": "t0", "url": "https://example.com/"}
    result = provider.run("create_tab", agent_id="a", url="https://example.com/")
    assert result["error"] == "CAMOFOX_TAB_LIMIT"
    assert not any(c["method"] == "POST" for c in worker.calls)


def test_screenshot_opt_in(tmp_path):
    provider, worker = make()
    standard_routes(worker)
    assert provider.run("screenshot", agent_id="a", tab_id="t1")["error"] == "SCREENSHOTS_DISABLED"
    provider, worker = make(screenshots_enabled=True, artifact_dir=str(tmp_path))
    standard_routes(worker)
    shot = provider.run("screenshot", agent_id="a", tab_id="t1")
    path = Path(shot["screenshot"])
    assert path.parent == tmp_path and path.read_bytes().startswith(b"\x89PNG")
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("failure,code", [
    ((401, {"error": "Unauthorized " + KEY}), "CAMOFOX_AUTH_FAILED"),
    ((404, {"error": "Tab not found"}), "CAMOFOX_TAB_NOT_FOUND"),
    ((410, {"error": "Tab no longer exists"}), "CAMOFOX_TAB_NOT_FOUND"),
    ((429, {"error": "too many"}), "CAMOFOX_LIMIT"),
    ((500, {"error": "boom " + KEY}), "CAMOFOX_UPSTREAM_ERROR"),
])
def test_error_classification_and_key_never_leaks(failure, code):
    provider, worker = make(retries=0)
    worker.fail.append(failure)
    result = provider.run("list_tabs", agent_id="a")
    assert result["error"] == code
    assert KEY not in json.dumps(result)


def test_idempotent_reads_retry_but_posts_do_not():
    provider, worker = make(retries=1)
    standard_routes(worker)
    worker.fail.append(urllib.error.URLError(ConnectionRefusedError()))
    assert provider.run("list_tabs", agent_id="a")["status"] == "OK"
    assert len(worker.calls) == 2
    worker.calls.clear()
    worker.fail.append((503, {"error": "busy"}))
    result = provider.run("click", agent_id="a", tab_id="t1", ref="e1", allow_mutations=True)
    assert result["status"] == "ERROR" and len(worker.calls) == 1


def test_unreachable_and_timeout_classified():
    provider, worker = make(retries=0)
    worker.fail.append(urllib.error.URLError(ConnectionRefusedError()))
    assert provider.run("list_tabs", agent_id="a")["error"] == "CAMOFOX_UNAVAILABLE"
    worker.fail.append(socket.timeout())
    assert provider.run("list_tabs", agent_id="a")["error"] == "CAMOFOX_TIMEOUT"


def test_status_reports_fallback_when_unavailable():
    provider, worker = make(retries=0)
    worker.fail.append(urllib.error.URLError(ConnectionRefusedError()))
    status = provider.run("status")
    assert status["status"] == "UNAVAILABLE" and status["default_browser"] == "browser_verify"
    provider, worker = make()
    standard_routes(worker)
    assert provider.run("status")["status"] == "OK"


def test_register_camofox_tools_exposes_one_tool():
    registered = {}

    class Server:
        def tool(self):
            def deco(fn):
                registered[fn.__name__] = fn
                return fn
            return deco

    provider, worker = make()
    standard_routes(worker)
    handlers = register_camofox_tools(Server(), provider)
    assert list(registered) == ["camofox_browser"] == list(handlers)
    assert handlers["camofox_browser"]("list_tabs", agent_id="a")["status"] == "OK"


@pytest.mark.skipif(os.environ.get("CAMOFOX_LIVE") != "1",
                    reason="live worker test; set CAMOFOX_LIVE=1 on the Camofox host")
def test_live_worker_example_com(tmp_path):
    config = CamofoxConfig.from_env({**os.environ, "TERMINAL_MCP_CAMOFOX_ENABLED": "1",
                                     "TERMINAL_MCP_CAMOFOX_SCREENSHOTS_ENABLED": "1",
                                     "TERMINAL_MCP_CAMOFOX_ARTIFACT_DIR": str(tmp_path)})
    provider = CamofoxBrowser(config)
    assert provider.run("status")["status"] == "OK"
    created = provider.run("create_tab", agent_id="pytest-live", url="https://example.com/")
    assert created["status"] == "OK", created
    tab = created["tab_id"]
    try:
        snap = provider.run("snapshot", agent_id="pytest-live", tab_id=tab)
        assert "This domain is for use in documentation" in snap["snapshot"]
        assert provider.run("list_tabs", agent_id="pytest-live")["tabs"][0]["title"] == "Example Domain"
        # Upstream v1.18.1 screenshots intermittently time out ("waiting for
        # fonts"); the provider must classify that, never crash or hang.
        shot = provider.run("screenshot", agent_id="pytest-live", tab_id=tab)
        assert shot["status"] == "OK" or (shot["error"] in ("CAMOFOX_UPSTREAM_ERROR", "CAMOFOX_TIMEOUT")
                                          and shot["fallback_call"]["tool"] == "browser_screenshot"), shot
        assert shot["duration_ms"] < 15_000
        # example.com fits the viewport: scroll reports the edge, fast, no wheel.
        flat = provider.run("scroll", agent_id="pytest-live", tab_id=tab)
        if flat.get("error") != "CAMOFOX_TAB_BUSY":  # busy only if the screenshot timed out
            assert flat["status"] == "OK" and flat["scrolled"] is False, flat
            assert flat["duration_ms"] < 3_000
        assert provider.run("list_tabs", agent_id="other-agent")["count"] == 0
        assert provider.run("click", agent_id="pytest-live", tab_id=tab, ref="e999",
                            allow_mutations=True)["status"] == "ERROR"
        foreign = provider.run("snapshot", agent_id="other-agent", tab_id=tab)
        assert foreign["error"] == "CAMOFOX_TAB_NOT_FOUND"
        assert provider.run("create_tab", agent_id="pytest-live",
                            url="https://www.iana.org/")["error"] == "CAMOFOX_DOMAIN_NOT_ALLOWED"
    finally:
        provider.run("close_all", agent_id="pytest-live")
    assert provider.run("list_tabs", agent_id="pytest-live")["count"] == 0


@pytest.mark.skipif(os.environ.get("CAMOFOX_LIVE") != "1",
                    reason="live worker test; set CAMOFOX_LIVE=1 on the Camofox host")
def test_live_worker_type_scroll_on_local_fixture():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            rows = b"".join(b"<p>row %d</p>" % i for i in range(300))
            self.wfile.write(b'<html><body><input id="q" aria-label="query">' + rows + b"</body></html>")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = CamofoxConfig.from_env({**os.environ, "TERMINAL_MCP_CAMOFOX_ENABLED": "1",
                                     "TERMINAL_MCP_CAMOFOX_ALLOW_LOOPBACK_TARGETS": "1",
                                     "TERMINAL_MCP_CAMOFOX_ALLOWED_DOMAINS": "127.0.0.1"})
    provider = CamofoxBrowser(config)
    try:
        created = provider.run("create_tab", agent_id="pytest-fixture",
                               url=f"http://127.0.0.1:{server.server_port}/form")
        assert created["status"] == "OK", created
        tab = created["tab_id"]
        scrolled = provider.run("scroll", agent_id="pytest-fixture", tab_id=tab)
        assert scrolled["status"] == "OK" and scrolled["scrolled"] is True, scrolled
        typed = provider.run("type", agent_id="pytest-fixture", tab_id=tab, selector="#q",
                             text="hello camofox", allow_mutations=True)
        assert typed["status"] == "OK", typed
        # The input now has focus (upstream's wheel always hung here); the
        # scrollBy path must still scroll, fast, without typing into the field.
        after = provider.run("scroll", agent_id="pytest-fixture", tab_id=tab)
        assert after["status"] == "OK" and after["scrolled"] is True, after
        assert after["duration_ms"] < 3_000
        assert "row 1" in provider.run("snapshot", agent_id="pytest-fixture", tab_id=tab)["snapshot"]
    finally:
        provider.run("close_all", agent_id="pytest-fixture")
        server.shutdown()


def test_build_mcp_registers_camofox_only_when_enabled(monkeypatch):
    from terminal_mcp.mcp_app import build_mcp

    monkeypatch.delenv("TERMINAL_MCP_CAMOFOX_ENABLED", raising=False)
    default_names = set(build_mcp()._tool_manager._tools)
    assert "camofox_browser" not in default_names
    assert {"browser_verify", "browser_screenshot"} <= default_names
    monkeypatch.setenv("TERMINAL_MCP_CAMOFOX_ENABLED", "1")
    enabled_names = set(build_mcp()._tool_manager._tools)
    assert enabled_names - default_names == {"camofox_browser"}


# -- upstream hang mitigation (constant scrollBy, busy cooldown, screenshot fast-fail) --

def scroll_route(worker, moved=True, maxY=2000):
    def handler(p, b):
        y = 500 if moved else 0
        return 200, {"ok": True, "result": {"bx": 0, "by": 0, "x": 0, "y": y, "maxX": 0, "maxY": maxY}}
    worker.routes[("POST", "/tabs/t1/evaluate")] = handler


def test_scroll_uses_constant_scrollby_template_not_the_wheel():
    from terminal_mcp import camofox_provider as cp
    provider, worker = make()
    standard_routes(worker)
    scroll_route(worker)
    result = provider.run("scroll", agent_id="a", tab_id="t1", amount=700)
    assert result["status"] == "OK" and result["scrolled"] is True
    assert result["position"] == {"x": 0, "y": 500, "max_y": 2000}
    assert not any(c["url"].split("?")[0].endswith("/scroll") for c in worker.calls)
    sent = [c["body"]["expression"] for c in worker.calls if "/evaluate" in c["url"]]
    assert sent == [cp._SCROLL_SCRIPT.format(dx=0, dy=700)]
    assert "evaluate" not in cp.ACTIONS


@pytest.mark.parametrize("direction,dx,dy", [("down", 0, 500), ("up", 0, -500),
                                             ("right", 500, 0), ("left", -500, 0)])
def test_scroll_direction_mapping_and_integer_only_inputs(direction, dx, dy):
    from terminal_mcp import camofox_provider as cp
    provider, worker = make()
    standard_routes(worker)
    scroll_route(worker)
    provider.run("scroll", agent_id="a", tab_id="t1", direction=direction)
    sent = [c["body"]["expression"] for c in worker.calls if "/evaluate" in c["url"]]
    assert sent == [cp._SCROLL_SCRIPT.format(dx=dx, dy=dy)]
    bad = provider.run("scroll", agent_id="a", tab_id="t1", amount="1); fetch('x')")
    assert bad["error"] == "CAMOFOX_BAD_REQUEST"
    assert provider.run("scroll", agent_id="a", tab_id="t1", direction="sideways")["error"] == "CAMOFOX_BAD_REQUEST"


def test_scroll_reports_edge_without_error():
    provider, worker = make()
    standard_routes(worker)
    scroll_route(worker, moved=False, maxY=0)
    result = provider.run("scroll", agent_id="a", tab_id="t1")
    assert result["status"] == "OK" and result["scrolled"] is False
    assert result["reason"] == "at_edge_or_not_scrollable"


def test_scroll_bad_response_classified():
    provider, worker = make()
    standard_routes(worker)
    worker.routes[("POST", "/tabs/t1/evaluate")] = lambda p, b: (200, {"ok": True, "result": "nope"})
    assert provider.run("scroll", agent_id="a", tab_id="t1")["error"] == "CAMOFOX_BAD_RESPONSE"


def test_scroll_timeout_marks_tab_busy_then_fails_fast_until_cooldown():
    now = {"t": 100.0}
    provider, worker = make(retries=0)
    provider.clock = lambda: now["t"]
    standard_routes(worker)
    worker.routes[("POST", "/tabs/t1/evaluate")] = lambda p, b: (500, {"error": "Internal server error"})
    first = provider.run("scroll", agent_id="a", tab_id="t1")
    assert first["error"] == "CAMOFOX_UPSTREAM_ERROR" and first["retry_after_seconds"] == 32
    calls_before = len(worker.calls)
    busy = provider.run("click", agent_id="a", tab_id="t1", ref="e1", allow_mutations=True)
    assert busy["error"] == "CAMOFOX_TAB_BUSY" and busy["duration_ms"] < 1000
    assert provider.run("snapshot", agent_id="a", tab_id="t1")["error"] == "CAMOFOX_TAB_BUSY"
    assert len(worker.calls) == calls_before  # nothing queued behind the upstream lock
    # another agent / tab is unaffected; list and close still work
    assert provider.run("list_tabs", agent_id="a")["status"] == "OK"
    now["t"] += 33
    assert provider.run("snapshot", agent_id="a", tab_id="t1")["status"] == "OK"


def test_close_clears_busy_state():
    provider, worker = make(retries=0)
    standard_routes(worker)
    worker.routes[("POST", "/tabs/t1/evaluate")] = lambda p, b: (500, {"error": "x"})
    provider.run("scroll", agent_id="a", tab_id="t1")
    assert provider.run("close_tab", agent_id="a", tab_id="t1")["status"] == "OK"
    assert provider._busy_until == {}


def test_screenshot_timeout_fails_fast_with_default_engine_fallback(tmp_path):
    provider, worker = make(retries=0, screenshots_enabled=True, artifact_dir=str(tmp_path))
    standard_routes(worker)
    worker.routes[("GET", "/tabs/t1/screenshot")] = lambda p, b: (500, {"error": "Internal server error"})
    created = provider.run("create_tab", agent_id="a", url="https://example.com/")
    assert created["status"] == "OK"
    shot = provider.run("screenshot", agent_id="a", tab_id="t1")
    assert shot["error"] == "CAMOFOX_UPSTREAM_ERROR" and shot["retry_after_seconds"] == 32
    assert shot["fallback_call"] == {"tool": "browser_screenshot", "url": "https://example.com/"}
    assert provider.run("screenshot", agent_id="a", tab_id="t1")["error"] == "CAMOFOX_TAB_BUSY"


def test_screenshot_and_scroll_use_short_client_timeouts():
    seen = {}

    def transport(method, url, body, headers, timeout):
        seen[url.split("?")[0].rsplit("/", 1)[-1]] = timeout
        if url.endswith("/evaluate"):
            return 200, b'{"result": {"bx": 0, "by": 0, "x": 0, "y": 500, "maxX": 0, "maxY": 900}}', ""
        if "/screenshot" in url:
            return 200, b"\x89PNG\r\n\x1a\n0", "image/png"
        return 200, b"{}", ""

    config = CamofoxConfig(enabled=True, screenshots_enabled=True, artifact_dir="/tmp")
    client = CamofoxClient(config, transport=transport, key_loader=lambda _p: KEY, sleep=lambda _s: None)
    provider = CamofoxBrowser(config, client=client, resolve_dns=False)
    provider.run("scroll", agent_id="a", tab_id="t1")
    provider.run("screenshot", agent_id="a", tab_id="t1")
    assert seen["evaluate"] <= 10 and seen["screenshot"] <= 12 and "scroll" not in seen
