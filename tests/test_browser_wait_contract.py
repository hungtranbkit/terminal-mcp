"""Read-only cached browser waiting and fail-closed wait receipts."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from terminal_mcp.browser_compat import normalize_cached_browser_args
from terminal_mcp.browser_gateway import BrowserGateway
from terminal_mcp.config import BrowserGatewayConfig


def test_cached_wait_and_assertion_forwarding():
    result = normalize_cached_browser_args("browser_verify", None, steps=[
        {"action": "wait_for", "selector": " #fixture-done "},
        {"action": "assert_text", "text": "Done"},
        {"action": "assert_status", "status": 200},
    ], viewport={"width": 390, "height": 844})
    assert result == {"wait_for": "#fixture-done", "assertions": [
        "text contains: Done", "status is: 200"],
        "viewport_width": 390, "viewport_height": 844}


@pytest.mark.parametrize("steps", [
    [{"action": "wait_for", "selector": ""}],
    [{"action": "wait_for", "selector": "x" * 301}],
    [{"action": "wait_for", "selector": "#x\n#y"}],
    [{"action": "wait_for", "selector": "#x", "timeout": 1000}],
    [{"action": "wait_for", "selector": "#x"}, {"action": "wait_for", "selector": "#y"}],
    [{"action": "assert_text", "text": "Done"}, {"action": "wait_for", "selector": "#x"}],
    [{"action": "click", "selector": "#x"}],
])
def test_cached_invalid_or_mutating_steps_remain_denied(steps):
    with pytest.raises(ValueError):
        normalize_cached_browser_args("browser_verify", None, steps=steps)


def test_wait_cannot_override_canonical_or_mutation_guard():
    step = [{"action": "wait_for", "selector": "#done"}]
    with pytest.raises(ValueError):
        normalize_cached_browser_args("browser_verify", {"wait_for": "#other"}, steps=step)
    with pytest.raises(ValueError):
        normalize_cached_browser_args("browser_verify", None, steps=step, allow_mutations=True)


@pytest.mark.parametrize("receipts,verdict,reason", [
    ([], "ERROR", "WAIT_RECEIPT_MISSING"),
    (None, "ERROR", "WAIT_RECEIPT_MISSING"),
    ([{"action":"wait_for","target":"#other","status":"OK"}], "ERROR", "WAIT_RECEIPT_MISSING"),
    ([{"action":"wait_for","target":"#done","status":"FAILED","detail":"token=synthetic"}], "FAIL", "WAIT_FAILED"),
    ([{"action":"wait_for","target":"#done","status":"ERROR"}], "ERROR", "WAIT_STEP_ERROR"),
])
def test_wait_failure_or_missing_receipt_never_passes(receipts, verdict, reason):
    gateway = BrowserGateway(BrowserGatewayConfig(enabled=True), runner=lambda *_: {
        "ok":True,"steps":receipts,"observation":{"status":200,"text":"Already ready"}})
    result = gateway.verify("https://8.8.8.8", ["text contains: Already ready"], wait_for="#done")
    assert result["status"] == verdict
    assert result["verification_scope"] == "incomplete"
    assert result["wait"]["reason"] == reason
    assert reason in result["failures"]
    assert "synthetic" not in str(result)


def test_successful_wait_precedes_assertions():
    jobs = []
    gateway = BrowserGateway(BrowserGatewayConfig(enabled=True), runner=lambda job,*_: jobs.append(job) or {
        "ok":True, "steps":[{"action":"wait_for","target":"#done","status":"OK"}],
        "observation":{"status":200,"text":"Done"}})
    result = gateway.verify("https://8.8.8.8", ["text contains: Done"], wait_for="#done")
    assert jobs[0]["steps"] == [{"action":"wait_for","target":"#done"}]
    assert result["status"] == "PASS" and result["verification_scope"] == "assertions"
    assert result["wait"] == {"status":"PASS"}


@pytest.mark.parametrize("selector", ["", "x"*301, "#a\n#b", 42])
def test_invalid_native_wait_rejected_before_worker(selector):
    def forbidden(*_):
        pytest.fail("invalid selector must not launch worker")
    gateway = BrowserGateway(BrowserGatewayConfig(enabled=True), runner=forbidden)
    result = gateway.verify("https://8.8.8.8", wait_for=selector)
    assert result["status"] == "ERROR"
    assert result["verification_scope"] == "incomplete"


@pytest.mark.browser_smoke
def test_real_delayed_fixture_and_missing_selector(tmp_path):
    class Fixture(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_GET(self):
            body = b"""<!doctype html><html><body><p>Starting</p><script>
            setTimeout(()=>{const e=document.createElement('p');e.id='done';e.textContent='FINISHED';document.body.append(e)},800);
            </script></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type","text/html")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server=ThreadingHTTPServer(("127.0.0.1",0),Fixture)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        gateway=BrowserGateway(BrowserGatewayConfig(enabled=True,allow_loopback=True,
            navigation_timeout_seconds=4,hard_timeout_seconds=12,artifact_dir=str(tmp_path)))
        url="http://127.0.0.1:%d/" % server.server_address[1]
        ok=gateway.verify(url,["text contains: FINISHED"],wait_for="#done",
                          viewport_width=390,viewport_height=844)
        assert ok["status"]=="PASS", ok
        assert ok["wait"]=={"status":"PASS"}
        bad=gateway.verify(url,["status is: 200"],wait_for="#absent")
        assert bad["status"]!="PASS", bad
        assert bad["verification_scope"]=="incomplete"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
