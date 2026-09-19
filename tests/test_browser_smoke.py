"""Real-browser smoke for the gateway -- opt-in (`pytest -m browser_smoke`).

Not run by default, for the same reason test_adapters_real_cli.py is not:
it starts a real Chrome and drives it. Everything security-relevant is
already covered deterministically in test_browser_gateway.py; what this
file proves is the other half -- that the declarative plan really reaches
a real page, at the exact contract viewport, and that an assertion here
fails when the page is wrong rather than passing vacuously.

The primary case serves its OWN page on loopback, so it needs no internet
and cannot be broken by someone else's site changing.
"""
from __future__ import annotations

import threading
from pathlib import Path
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from terminal_mcp.browser_gateway import BrowserGateway
from terminal_mcp.config import BrowserGatewayConfig

pytestmark = pytest.mark.browser_smoke

VIEWPORT = {"width": 1348, "height": 768}

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>TMCP Browser Gateway Smoke</title></head>
<body>
  <h1 id="title">Gateway Smoke</h1>
  <p id="viewport"></p>
  <input id="qty" value="1">
  <button id="apply" onclick="document.getElementById('total').textContent =
    (parseFloat(document.getElementById('qty').value) * 2).toFixed(2)">Apply</button>
  <p id="total">0.00</p>
  <script>
    document.getElementById('viewport').textContent =
      window.innerWidth + 'x' + window.innerHeight;
  </script>
</body></html>
"""


@pytest.fixture(scope="module")
def static_site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "index.html").write_text(PAGE, encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/index.html"
    server.shutdown()
    server.server_close()


@pytest.fixture(scope="module")
def gateway(tmp_path_factory):
    return BrowserGateway(BrowserGatewayConfig(
        enabled=True, allow_loopback=True, screenshots_enabled=True,
        artifact_dir=str(tmp_path_factory.mktemp("artifacts")),
        viewport_width=1348, viewport_height=768))


def test_local_page_viewport_and_screenshot(gateway, static_site):
    result = gateway.verify(static_site, ["status is: 200",
        "selector text is: #viewport :: 1348x768", "title is: TMCP Browser Gateway Smoke"],
        screenshot=True)
    assert result["status"] == "PASS", result
    shot = Path(result["screenshot"])
    assert shot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert shot.stat().st_size > 1000


def test_a_wrong_assertion_really_fails(gateway, static_site):
    result = gateway.verify(static_site, ["title is: Not The Title"])
    assert result["status"] == "FAIL", result


def test_decimal_quantity_survives_the_round_trip(gateway, static_site):
    result = gateway.run_task("fill #qty with 0.25; click #apply; "
        "assert selector text is: #total :: 0.50", url=static_site)
    assert result["status"] == "OK", result
    fresh = gateway.verify(static_site, ["selector text is: #total :: 0.00"])
    assert fresh["status"] == "PASS", fresh
