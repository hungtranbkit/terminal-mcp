"""Browser contract for the Nodes shortcut, using the real dashboard pages and fixture transports."""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from terminal_mcp.dashboard import NODES_ADMIN_HTML, WEBTERM_HTML, _nav_page


@pytest.mark.browser_smoke
def test_nodes_ssh_web_opens_selected_node_or_explains_why_it_cannot_connect():
    playwright = pytest.importorskip("playwright.sync_api")
    assets = Path(__file__).resolve().parents[1] / "terminal_mcp" / "vendor" / "xterm"
    nodes = [
        {"id": name, "display_name": name, "status": "online", "endpoint": endpoint}
        for name, endpoint in [("local", "local"), ("worker", "http://worker"), ("other", "http://other")]
    ]
    status_requests = []
    sockets = []
    detail_requests = []

    def serve(route):
        path = urlsplit(route.request.url).path
        if path == "/dashboard/nodes":
            route.fulfill(content_type="text/html", body=_nav_page(NODES_ADMIN_HTML, "nodes"))
        elif path == "/dashboard/terminal":
            route.fulfill(content_type="text/html", body=_nav_page(WEBTERM_HTML, "terminal"))
        elif path.startswith("/dashboard/assets/"):
            route.fulfill(path=assets / Path(path).name)
        elif path == "/dashboard/api/nodes":
            route.fulfill(json={"nodes": nodes})
        elif path.endswith("/ssh"):
            status_requests.append((route.request.method, path))
            ready = path == "/dashboard/api/nodes/worker/ssh"
            route.fulfill(json={"verified": ready, "status": "verified" if ready else "route_collision"})
        elif path == "/dashboard/api/node":
            detail_requests.append(route.request.url)
            route.fulfill(json={"node": nodes[0], "sessions": []})
        else:
            route.fulfill(json={})

    def open_socket(socket):
        sockets.append(socket.url)
        socket.send('{"type":"ready","readonly":false}')

    with playwright.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"), headless=True)
        context = browser.new_context()
        context.route("**/*", serve)
        context.route_web_socket("**/dashboard/ws/terminal*", open_socket)
        page = context.new_page()
        page.goto("http://localhost/dashboard/nodes")
        playwright.expect(page.get_by_role("link", name="SSH Web")).to_have_count(2)
        assert status_requests == []
        detail_requests.clear()

        for node_id in ("worker", "other"):
            card = page.locator(f'.node-card[data-node-id="{node_id}"]')
            with page.expect_popup() as popup:
                card.get_by_role("link", name="SSH Web").click()
            terminal = popup.value
            playwright.expect(terminal.locator("#sessName")).to_have_text("SSH " + node_id)
            if node_id == "worker":
                playwright.expect(terminal.locator("#connPill")).to_contain_text("LIVE")
            else:
                playwright.expect(terminal.locator("#banner")).to_contain_text("dịch vụ SSH khác")
            terminal.close()

        assert detail_requests == []
        assert status_requests == [("GET", f"/dashboard/api/nodes/{name}/ssh") for name in ("worker", "other")]
        assert sockets == ["ws://localhost/dashboard/ws/terminal?transport=ssh&node_id=worker"]
        browser.close()
