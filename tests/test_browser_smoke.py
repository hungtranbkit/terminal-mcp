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
import urllib.error
import urllib.request
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from terminal_mcp.browser_gateway import BrowserGateway
from terminal_mcp.browser_plan import UrlPolicy
from terminal_mcp.browser_runner import LocalBrowserRunner

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
    artifacts = tmp_path_factory.mktemp("artifacts")
    runner = LocalBrowserRunner(artifact_dir=artifacts)
    if not runner.runtime().available:
        pytest.skip("Browser Use / Browser Harness is not provisioned on this node")
    gw = BrowserGateway(
        runner=runner,
        local_node_id="local",
        # The smoke target IS a local dev server, which is exactly the case
        # the allowlist exists for -- the default policy blocks loopback.
        url_policy=UrlPolicy(allow_hosts=frozenset({"127.0.0.1", "localhost"})),
        sync_wait_seconds=45.0,
    )
    yield gw
    gw.stop()


def test_declarative_plan_drives_a_real_page_at_1348x768(gateway, static_site):
    result = gateway.verify({
        "url": static_site,
        "viewport": VIEWPORT,
        "screenshot": "always",
        "screenshot_name": "smoke",
        "allow_mutations": True,
        "steps": [
            {"op": "assert_text", "selector": "#title", "contains": "Gateway Smoke"},
            # The viewport assertion is the contract: the page must have
            # been laid out at 1348x768, not merely screenshotted at it.
            {"op": "assert_text", "selector": "#viewport", "contains": "1348x"},
            {"op": "fill", "selector": "#qty", "value": "12.5"},
            {"op": "assert_value", "selector": "#qty", "equals": "12.5"},
            {"op": "click", "selector": "#apply"},
            {"op": "assert_text", "selector": "#total", "equals": "25.00"},
            {"op": "assert_visible", "selector": "#apply", "visible": True},
            {"op": "assert_url", "contains": "index.html"},
        ],
    })
    assert result["status"] == "PASS", result
    assert result["summary"] == "8/8 checks passed"
    assert result["artifact"], "screenshot policy 'always' must produce an artifact"

    from pathlib import Path

    shot = Path(result["artifact"])
    assert shot.exists() and shot.stat().st_size > 1000


def test_a_wrong_assertion_really_fails(gateway, static_site):
    """Fail-on-wrong proof. Without this, a PASS above means nothing."""
    result = gateway.verify({
        "url": static_site,
        "viewport": VIEWPORT,
        "steps": [{"op": "assert_text", "selector": "#title", "equals": "Not The Title"}],
    })
    assert result["status"] == "FAIL", result
    assert result["checks"][0]["ok"] is False


def test_decimal_quantity_survives_the_round_trip(gateway, static_site):
    """The decimal-quantity regression shape, in a browser.

    A value like 12.5 that silently becomes 12 or 13 between the input and
    the computed total is the exact class of bug a DOM assertion catches
    and an HTTP check does not.
    """
    result = gateway.verify({
        "url": static_site,
        "viewport": VIEWPORT,
        "allow_mutations": True,
        "steps": [
            {"op": "fill", "selector": "#qty", "value": "0.25"},
            {"op": "click", "selector": "#apply"},
            {"op": "assert_value", "selector": "#qty", "equals": "0.25"},
            {"op": "assert_text", "selector": "#total", "equals": "0.50"},
        ],
    })
    assert result["status"] == "PASS", result


@pytest.mark.real_network
def test_example_com_loads_over_the_public_internet(gateway):
    result = gateway.verify({
        "url": "https://example.com/",
        "viewport": VIEWPORT,
        "steps": [
            {"op": "assert_text", "selector": "h1", "contains": "Example Domain"},
            {"op": "assert_url", "contains": "example.com"},
        ],
    })
    assert result["status"] == "PASS", result


def _discover_novaretail() -> str | None:
    """Find a NovaRetail/preview server only if one is already running and
    needs no credentials. Never starts anything, never logs in."""
    for port in (4173, 5173, 3000, 3100, 3200, 8080):
        url = f"http://127.0.0.1:{port}/"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if response.status == 200:
                    body = response.read(4096).decode("utf-8", "replace").lower()
                    if "login" in body and "password" in body:
                        continue  # credentialed -- out of scope
                    return url
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return None


def test_novaretail_preview_loads_if_one_is_running(gateway):
    url = _discover_novaretail()
    if not url:
        pytest.skip("no credential-free local preview server discovered")
    result = gateway.verify({
        "url": url,
        "viewport": VIEWPORT,
        "screenshot": "always",
        "screenshot_name": "preview",
        # Non-destructive by construction: no mutating step is even allowed.
        "steps": [{"op": "assert_visible", "selector": "body", "visible": True}],
    })
    assert result["status"] == "PASS", result
