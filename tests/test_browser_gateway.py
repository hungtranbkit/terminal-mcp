"""Browser gateway: schema, URL policy, injection, bounds, nodes, surface.

None of these tests start a browser. That is the point of splitting
validation (browser_plan), execution (browser_runner) and orchestration
(browser_gateway): the security-relevant half is pure functions, so it is
tested exhaustively and deterministically on any host, including one with
no Chrome at all. The real-browser proof lives in test_browser_smoke.py.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from terminal_mcp.browser_exec import js_selector_expression, run_plan
from terminal_mcp.browser_gateway import (
    MAX_CHECKS_RETURNED,
    MAX_ERRORS_RETURNED,
    BrowserGateway,
)
from terminal_mcp.browser_plan import (
    MAX_STEPS,
    BrowserGatewayError,
    UrlPolicy,
    validate_plan,
    validate_screenshot_name,
    validate_url,
)
from terminal_mcp.browser_runner import BrowserRuntime, LocalBrowserRunner, parse_result
from terminal_mcp.browser_tools import BROWSER_TOOL_NAMES, register_browser_tools


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRunner:
    """Stands in for LocalBrowserRunner without a browser or a subprocess."""

    def __init__(self, result=None, *, available=True, delay=0.0, artifact_dir="/tmp/tmcp-art"):
        self.result = result or {"status": "PASS", "checks": [], "errors": [],
                                 "artifact": "", "elapsed_ms": 5}
        self.available = available
        self.delay = delay
        self.artifact_dir = artifact_dir
        self.cdp_url = "http://127.0.0.1:9444"
        self.calls: list[dict] = []
        self.stopped = False

    def runtime(self):
        if self.available:
            return BrowserRuntime(available=True, package_version="0.13.10",
                                  harness_version="0.1.13", cli="/x/browser-harness",
                                  chrome="/usr/bin/google-chrome")
        return BrowserRuntime(available=False, reason="browser-harness is not installed")

    def browser_alive(self, timeout=2.0):
        return self.available

    def execute(self, payload, *, timeout_seconds):
        self.calls.append(payload)
        if self.delay:
            time.sleep(self.delay)
        return dict(self.result)

    def stop_browser(self):
        self.stopped = True
        return {"stopped": True, "pid": 123, "alive": False}


def _gateway(tmp_path, **kwargs):
    runner = kwargs.pop("runner", None) or FakeRunner(artifact_dir=str(tmp_path))
    runner.artifact_dir = str(tmp_path)
    return BrowserGateway(runner=runner, **kwargs)


def _plan(**overrides):
    payload = {
        "url": "https://example.com",
        "steps": [{"op": "assert_text", "selector": "h1", "contains": "Example"}],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Schema / validation
# ---------------------------------------------------------------------------

def test_valid_plan_normalizes_defaults():
    plan = validate_plan(_plan())
    assert plan.viewport == (1348, 768)
    assert plan.screenshot == "on_failure"
    assert plan.steps[0]["op"] == "assert_text"


def test_plan_without_any_assertion_is_rejected():
    # A "verification" that checks nothing would report PASS forever.
    with pytest.raises(BrowserGatewayError) as exc:
        validate_plan(_plan(steps=[{"op": "navigate", "url": "https://example.com"}]))
    assert exc.value.code == "BROWSER_INVALID_PLAN"
    assert "assert" in exc.value.message


def test_unknown_op_is_rejected_by_name():
    with pytest.raises(BrowserGatewayError) as exc:
        validate_plan(_plan(steps=[{"op": "eval", "script": "alert(1)"}]))
    assert exc.value.code == "BROWSER_INVALID_PLAN"
    assert exc.value.detail["op"] == "eval"


@pytest.mark.parametrize("step", [
    {"op": "press", "key": "F13"},
    {"op": "press", "key": "rm -rf /"},
    {"op": "wait", "seconds": 999},
    {"op": "wait", "seconds": 1, "state": "load"},
    {"op": "wait"},
    {"op": "assert_text", "selector": "h1"},
    {"op": "assert_text", "selector": "h1", "equals": "a", "contains": "b"},
    {"op": "click"},
    {"op": "assert_visible", "selector": "h1", "visible": "yes"},
])
def test_malformed_steps_are_rejected(step):
    with pytest.raises(BrowserGatewayError):
        validate_plan(_plan(steps=[step, {"op": "assert_url", "contains": "x"}],
                            allow_mutations=True))


def test_step_count_is_bounded():
    steps = [{"op": "assert_url", "contains": "x"}] * (MAX_STEPS + 1)
    with pytest.raises(BrowserGatewayError) as exc:
        validate_plan(_plan(steps=steps))
    assert exc.value.detail["steps"] == MAX_STEPS + 1


@pytest.mark.parametrize("viewport", [
    {"width": 10, "height": 768}, {"width": 1348, "height": 99999},
    {"width": "wide", "height": 768}, "1348x768",
])
def test_viewport_bounds(viewport):
    with pytest.raises(BrowserGatewayError):
        validate_plan(_plan(viewport=viewport))


def test_viewport_accepts_the_1348x768_contract_size():
    assert validate_plan(_plan(viewport={"width": 1348, "height": 768})).viewport == (1348, 768)


@pytest.mark.parametrize("timeout", [0, -5, 10_000, "soon"])
def test_timeout_bounds(timeout):
    with pytest.raises(BrowserGatewayError):
        validate_plan(_plan(timeout_seconds=timeout))


def test_mutating_steps_require_explicit_authorization():
    payload = _plan(steps=[{"op": "click", "selector": "#buy"},
                           {"op": "assert_url", "contains": "/cart"}])
    with pytest.raises(BrowserGatewayError) as exc:
        validate_plan(payload)
    assert exc.value.code == "BROWSER_MUTATION_NOT_ALLOWED"
    assert exc.value.detail["mutating_steps"] == ["click"]

    plan = validate_plan({**payload, "allow_mutations": True})
    assert plan.mutating == ("click",)


def test_mutations_are_echoed_for_audit(tmp_path):
    gateway = _gateway(tmp_path)
    result = gateway.verify(_plan(
        steps=[{"op": "fill", "selector": "#q", "value": "x"},
               {"op": "assert_url", "contains": "example"}],
        allow_mutations=True))
    assert result["mutations"] == ["fill"]


# ---------------------------------------------------------------------------
# URL policy / SSRF
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "chrome://settings",
    "chrome-extension://abc/page.html",
    "javascript:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    "about:blank",
    "view-source:https://example.com",
    "blob:https://example.com/uuid",
    "devtools://devtools/bundled/inspector.html",
    "ftp://example.com/file",
    "ws://example.com/socket",
    "/relative/path",
    "example.com",
])
def test_non_http_schemes_are_rejected(url):
    with pytest.raises(BrowserGatewayError) as exc:
        validate_url(url)
    assert exc.value.code == "BROWSER_URL_REJECTED"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:3200/", "http://localhost:3200/", "https://10.0.0.5/",
    "http://192.168.1.10/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
])
def test_private_and_metadata_targets_are_blocked_by_default(url):
    with pytest.raises(BrowserGatewayError) as exc:
        validate_url(url, UrlPolicy())
    assert exc.value.code == "BROWSER_URL_REJECTED"


def test_allowlist_permits_a_configured_local_dev_target():
    policy = UrlPolicy(allow_hosts=frozenset({"127.0.0.1:3200", "localhost"}))
    assert validate_url("http://127.0.0.1:3200/menu", policy)
    assert validate_url("http://localhost:9999/any-port", policy)
    # A different port on an address allowlisted WITH a port stays blocked.
    with pytest.raises(BrowserGatewayError):
        validate_url("http://127.0.0.1:3201/", policy)


def test_metadata_endpoint_is_blocked_even_when_allowlisted():
    policy = UrlPolicy(allow_hosts=frozenset({"169.254.169.254"}), allow_private=True)
    with pytest.raises(BrowserGatewayError) as exc:
        validate_url("http://169.254.169.254/latest/meta-data/", policy)
    assert "metadata" in exc.value.message


def test_public_name_resolving_into_a_private_range_is_rejected():
    # DNS-rebinding shape: the name is public, the address is not.
    policy = UrlPolicy(resolve=lambda host: ["10.1.2.3"])
    with pytest.raises(BrowserGatewayError) as exc:
        validate_url("https://rebind.example.com/", policy)
    assert exc.value.detail["address"] == "10.1.2.3"


def test_unresolvable_host_is_not_fatal():
    def _boom(host):
        raise OSError("no DNS here")

    assert validate_url("https://example.com/", UrlPolicy(resolve=_boom))


def test_url_bounds_and_control_characters():
    with pytest.raises(BrowserGatewayError):
        validate_url("https://example.com/" + "a" * 3000)
    with pytest.raises(BrowserGatewayError):
        validate_url("https://example.com/\nSet-Cookie: x=1")


def test_step_urls_are_validated_too():
    with pytest.raises(BrowserGatewayError) as exc:
        validate_plan(_plan(steps=[{"op": "navigate", "url": "file:///etc/shadow"},
                                   {"op": "assert_url", "contains": "x"}]))
    assert exc.value.code == "BROWSER_URL_REJECTED"


def test_gateway_returns_rejection_as_a_typed_dict(tmp_path):
    result = _gateway(tmp_path).verify(_plan(url="file:///etc/passwd"))
    assert result["error"] == "BROWSER_URL_REJECTED"


# ---------------------------------------------------------------------------
# Injection safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("selector", [
    "#a\"); alert(1); //",
    "#a'); fetch('http://evil');//",
    "`${process.env}`",
    "</script><script>alert(1)</script>",
    "#a\\", "input[value='x']",
])
def test_selectors_cross_into_javascript_as_data_not_code(selector):
    expression = js_selector_expression(selector, "return 1;")
    # The selector appears exactly once, as a JSON string literal, and the
    # expression still parses as one querySelector call.
    literal = json.dumps(selector)
    assert literal in expression
    assert expression.count("document.querySelector(") == 1
    # Nothing the caller wrote escaped the literal into executable position.
    prefix, _, remainder = expression.partition(literal)
    assert "alert(" not in prefix and "alert(" not in remainder
    assert "fetch(" not in prefix and "fetch(" not in remainder


def test_plan_reaches_the_executor_as_json_never_as_script(tmp_path):
    gateway = _gateway(tmp_path)
    gateway.verify(_plan(steps=[{"op": "fill", "selector": "#q", "value": "'; DROP TABLE x; --"},
                                {"op": "assert_url", "contains": "example"}],
                         allow_mutations=True))
    payload = gateway.runner.calls[0]
    # The value survives verbatim as DATA and round-trips through JSON --
    # it is never concatenated into a script.
    assert json.loads(json.dumps(payload))["steps"][0]["value"] == "'; DROP TABLE x; --"


def test_executor_module_does_nothing_on_import(monkeypatch):
    # Importing browser_exec must not execute a plan: it runs only when the
    # harness sets TMCP_BROWSER_PLAN.
    import importlib

    import terminal_mcp.browser_exec as module

    monkeypatch.delenv("TMCP_BROWSER_PLAN", raising=False)
    importlib.reload(module)  # no exception, no execution


def test_run_plan_reports_missing_helpers_instead_of_crashing():
    # Outside the harness the helpers do not exist; the executor must turn
    # that into a structured ERROR, not a traceback on someone's transcript.
    result = run_plan({"url": "https://example.com", "steps": [], "viewport": {}})
    assert result["status"] == "ERROR"
    assert result["errors"]


# ---------------------------------------------------------------------------
# Screenshot path safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../../etc/cron.d/x", "/etc/passwd", "a/b", "..", ".hidden",
    "name with spaces", "x" * 80, "naughty;rm -rf /",
])
def test_screenshot_names_that_could_place_a_file_are_rejected(name):
    with pytest.raises(BrowserGatewayError):
        validate_screenshot_name(name)


def test_screenshot_name_optional_and_sane():
    assert validate_screenshot_name("") == ""
    assert validate_screenshot_name(None) == ""
    assert validate_screenshot_name("novaretail_preview-1") == "novaretail_preview-1"


def test_artifact_path_is_always_inside_the_artifact_dir(tmp_path):
    gateway = _gateway(tmp_path)
    path = gateway.artifact_path("shot")
    assert path.parent == tmp_path.resolve()
    assert path.name.startswith("shot-") and path.suffix == ".png"


def test_screenshot_tool_rejects_a_traversing_name(tmp_path):
    result = _gateway(tmp_path).screenshot("https://example.com", name="../escape")
    assert result["error"] == "BROWSER_INVALID_PLAN"


# ---------------------------------------------------------------------------
# Node capability selection / affinity
# ---------------------------------------------------------------------------

def _nodes(*entries):
    return lambda: list(entries)


def test_auto_selects_the_local_node_when_it_is_capable(tmp_path):
    gateway = _gateway(tmp_path, local_node_id="dell-linux")
    assert gateway.select_node() == "dell-linux"


def test_auto_selects_a_capable_remote_node_when_local_cannot(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(available=False),
                       nodes_provider=_nodes(
                           {"id": "m910", "capabilities": ["git"]},
                           {"id": "mac", "capabilities": ["git", "browser-harness"]}))
    assert gateway.select_node() == "mac"


def test_auto_without_any_capable_node_is_typed(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(available=False),
                       nodes_provider=_nodes({"id": "m910", "capabilities": ["git"]}))
    with pytest.raises(BrowserGatewayError) as exc:
        gateway.select_node()
    assert exc.value.code == "BROWSER_UNAVAILABLE"
    assert exc.value.detail["reason"] == "no_eligible_node"


def test_named_node_without_the_capability_is_typed(tmp_path):
    gateway = _gateway(tmp_path, nodes_provider=_nodes({"id": "m910", "capabilities": ["git"]}))
    with pytest.raises(BrowserGatewayError) as exc:
        gateway.select_node(node="m910")
    assert exc.value.code == "BROWSER_UNAVAILABLE"
    assert exc.value.detail["reason"] == "node_capability_missing"


def test_named_capable_remote_node_is_unsupported_in_phase_1(tmp_path):
    gateway = _gateway(tmp_path, nodes_provider=_nodes(
        {"id": "mac", "capabilities": ["browser-harness"]}))
    with pytest.raises(BrowserGatewayError) as exc:
        gateway.select_node(node="mac")
    assert exc.value.code == "BROWSER_UNAVAILABLE"
    assert exc.value.detail["reason"] == "remote_execution_unsupported"


def test_unknown_node_is_not_silently_rerouted(tmp_path):
    gateway = _gateway(tmp_path, nodes_provider=_nodes({"id": "m910", "capabilities": []}))
    with pytest.raises(BrowserGatewayError) as exc:
        gateway.select_node(node="ghost")
    assert exc.value.code == "BROWSER_UNKNOWN_NODE"


def test_session_affinity_pins_the_plan_to_its_own_node(tmp_path):
    gateway = _gateway(tmp_path, local_node_id="dell-linux",
                       session_node_resolver=lambda s: "dell-linux" if s == "mine" else None,
                       nodes_provider=_nodes({"id": "mac", "capabilities": ["browser-harness"]}))
    assert gateway.select_node(session="mine") == "dell-linux"


def test_explicit_node_beats_session_affinity(tmp_path):
    gateway = _gateway(tmp_path, local_node_id="dell-linux",
                       session_node_resolver=lambda s: "mac")
    assert gateway.select_node(node="local", session="mine") == "dell-linux"


def test_verify_reports_the_node_it_ran_on(tmp_path):
    gateway = _gateway(tmp_path, local_node_id="dell-linux")
    assert gateway.verify(_plan())["node"] == "dell-linux"


# ---------------------------------------------------------------------------
# Missing dependency -> degraded, never a crash
# ---------------------------------------------------------------------------

def test_status_is_degraded_when_browser_use_is_not_installed(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(available=False))
    status = gateway.status()
    assert status["status"] == "DEGRADED"
    assert "not installed" in status["runtime"]["reason"]


def test_status_is_ready_and_reports_resolved_versions(tmp_path):
    status = _gateway(tmp_path).status()
    assert status["status"] == "READY"
    assert status["runtime"]["browser_use_version"] == "0.13.10"
    assert status["runtime"]["harness_version"] == "0.1.13"


def test_recording_is_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_BROWSER_ALLOW_RECORDING", raising=False)
    assert _gateway(tmp_path).status()["browser"]["recording"] == "off"


def test_real_runner_degrades_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BROWSER_HOME", str(tmp_path / "nothing-here"))
    monkeypatch.setenv("PATH", str(tmp_path))  # no browser-harness anywhere
    runner = LocalBrowserRunner(home=tmp_path / "nothing-here", artifact_dir=tmp_path / "art")
    result = runner.execute({"url": "https://example.com", "steps": []}, timeout_seconds=5)
    assert result["status"] == "ERROR" and result["degraded"] is True


# ---------------------------------------------------------------------------
# Bounded sync window / PENDING / resume
# ---------------------------------------------------------------------------

def test_slow_plan_returns_pending_with_a_resume_handle(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(delay=1.5), sync_wait_seconds=0.2)
    result = gateway.verify(_plan())
    assert result["status"] == "PENDING"
    assert result["resume"]["job_id"] == result["job_id"]
    assert result["resume"]["tool"] == "terminal_browser_status"


def test_pending_job_result_is_readable_afterwards(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(
        delay=0.3, result={"status": "PASS", "checks": [{"i": 0, "op": "assert_text", "ok": True}],
                           "errors": [], "artifact": "", "elapsed_ms": 300}),
        sync_wait_seconds=0.05)
    pending = gateway.verify(_plan())
    assert pending["status"] == "PENDING"
    deadline = time.time() + 5
    while time.time() < deadline:
        later = gateway.status(pending["job_id"])
        if later["status"] != "PENDING":
            break
        time.sleep(0.05)
    assert later["status"] == "PASS"
    assert later["job_id"] == pending["job_id"]


def test_unknown_job_id_is_typed(tmp_path):
    assert _gateway(tmp_path).status("nope")["error"] == "BROWSER_UNKNOWN_JOB"


def test_sync_wait_is_capped_at_the_contract_ceiling(tmp_path):
    gateway = _gateway(tmp_path, sync_wait_seconds=600)
    assert gateway.sync_wait_seconds == 45.0


def test_executor_timeout_is_reported_as_fail(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(
        result={"status": "TIMEOUT", "checks": [], "errors": ["plan exceeded 45s"],
                "artifact": "", "elapsed_ms": 45000}))
    result = gateway.verify(_plan())
    assert result["status"] == "FAIL"
    assert "exceeded" in result["errors"][0]


def test_a_verify_never_blocks_longer_than_its_budget(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(delay=3.0), sync_wait_seconds=0.3)
    started = time.monotonic()
    gateway.verify(_plan())
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# Compact results
# ---------------------------------------------------------------------------

def test_long_results_are_truncated_compactly(tmp_path):
    checks = [{"i": i, "op": "assert_url", "ok": True, "detail": "d" * 500} for i in range(60)]
    errors = [f"error {i} " + "e" * 500 for i in range(12)]
    gateway = _gateway(tmp_path, runner=FakeRunner(
        result={"status": "FAIL", "checks": checks, "errors": errors,
                "artifact": "", "elapsed_ms": 10}))
    result = gateway.verify(_plan())
    assert len(result["checks"]) == MAX_CHECKS_RETURNED
    assert result["checks_truncated"] == 60 - MAX_CHECKS_RETURNED
    assert len(result["errors"]) == MAX_ERRORS_RETURNED
    assert result["errors_truncated"] == 12 - MAX_ERRORS_RETURNED
    assert all(len(c["detail"]) <= 160 for c in result["checks"])
    assert all(len(e) <= 240 for e in result["errors"])


def test_result_carries_the_contract_fields(tmp_path):
    gateway = _gateway(tmp_path, local_node_id="dell-linux", runner=FakeRunner(
        result={"status": "PASS", "checks": [{"i": 0, "op": "assert_text", "ok": True}],
                "errors": [], "artifact": "/art/x.png", "elapsed_ms": 42,
                "page": {"url": "https://example.com/", "title": "Example"}}))
    result = gateway.verify(_plan())
    assert result["status"] == "PASS"
    assert result["node"] == "dell-linux"
    assert result["summary"] == "1/1 checks passed"
    assert result["artifact"] == "/art/x.png"
    assert result["elapsed_ms"] == 42
    assert result["page"]["title"] == "Example"


def test_secrets_do_not_leak_into_results(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(
        result={"status": "FAIL", "checks": [],
                "errors": ["login failed with password=hunter2 token=abc123secret"],
                "artifact": "", "elapsed_ms": 1}))
    result = gateway.verify(_plan())
    blob = json.dumps(result)
    assert "hunter2" not in blob
    assert "abc123secret" not in blob


def test_filled_values_are_never_echoed_back(tmp_path):
    gateway = _gateway(tmp_path)
    result = gateway.verify(_plan(
        steps=[{"op": "fill", "selector": "#password", "value": "s3cr3t-value", "secret": True},
               {"op": "assert_url", "contains": "example"}],
        allow_mutations=True))
    assert "s3cr3t-value" not in json.dumps(result)


def test_parse_result_finds_the_sentinel_amid_harness_chatter():
    stdout = ("browser-harness: starting daemon\n"
              "WARNING: something\n"
              '__TMCP_BROWSER_RESULT__{"status": "PASS", "checks": []}\n')
    assert parse_result(stdout)["status"] == "PASS"
    assert parse_result("no result here") is None


# ---------------------------------------------------------------------------
# Tool surface regression
# ---------------------------------------------------------------------------

class _FakeServer:
    def __init__(self):
        self.registered: list[str] = []

    def tool(self):
        def decorate(fn):
            self.registered.append(fn.__name__)
            return fn
        return decorate


def test_the_browser_surface_is_exactly_four_declarative_tools(tmp_path):
    """The surface IS the security boundary.

    If this test fails because a tool was added, that is the review
    prompt: a browser tool that runs Python/shell/JS or exposes raw CDP
    hands a chat client a shell on the node and makes the declarative
    plan decorative.
    """
    server = _FakeServer()
    register_browser_tools(server, _gateway(tmp_path))
    assert tuple(server.registered) == BROWSER_TOOL_NAMES
    forbidden = ("exec", "eval", "shell", "python", "script", "cdp", "js", "command", "run")
    for name in server.registered:
        tail = name.replace("terminal_browser_", "")
        assert tail not in forbidden, f"{name} looks like a raw-execution escape hatch"


@pytest.mark.anyio
async def test_build_mcp_registers_the_browser_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from terminal_mcp.mcp_app import build_mcp

    server = build_mcp(browser=_gateway(tmp_path), default_optional_services=False)
    tools = {tool.name for tool in await server.list_tools()}
    assert set(BROWSER_TOOL_NAMES) <= tools


@pytest.mark.anyio
async def test_tool_docs_teach_the_declarative_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from terminal_mcp.mcp_app import build_mcp

    server = build_mcp(browser=_gateway(tmp_path), default_optional_services=False)
    tools = {tool.name: tool for tool in await server.list_tools()}
    verify_doc = tools["terminal_browser_verify"].description.lower()
    assert "assert" in verify_doc and "allow_mutations" in verify_doc
    assert "pending" in verify_doc and "http" in verify_doc


def test_stop_releases_only_the_managed_browser(tmp_path):
    gateway = _gateway(tmp_path)
    result = gateway.stop()
    assert result["status"] == "STOPPED"
    assert gateway.runner.stopped is True


def test_concurrent_verifies_get_distinct_jobs(tmp_path):
    gateway = _gateway(tmp_path, runner=FakeRunner(delay=0.2), sync_wait_seconds=0.05)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(gateway.verify(_plan())))
               for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len({r["job_id"] for r in results}) == 5


# ---------------------------------------------------------------------------
# The one-tool ChatGPT surface
#
# ChatGPT sees exactly one tool (chatgpt_sidecar.CATALOG == ("terminal_turn",)).
# A browser reachable only through the standalone tools would therefore be
# invisible to ChatGPT -- and reaching it would need a second control plane,
# which is the exact thing this feature exists to prevent.
# ---------------------------------------------------------------------------

def test_browser_actions_are_in_the_turn_vocabulary():
    from terminal_mcp.compact_tools import TURN_ACTION_ALIASES, TURN_ACTIONS, TURN_HANDLER_ACTIONS

    for action in ("browser_status", "browser_verify", "browser_screenshot", "browser_stop"):
        assert action in TURN_ACTIONS
        assert action in TURN_HANDLER_ACTIONS
    assert TURN_ACTION_ALIASES["verify"] == "browser_verify"
    assert TURN_ACTION_ALIASES["screenshot"] == "browser_screenshot"


def test_turn_routes_browser_verify_to_the_gateway(tmp_path):
    from terminal_mcp.compact_tools import CompactTerminalTools

    gateway = _gateway(tmp_path)
    tools = CompactTerminalTools.__new__(CompactTerminalTools)
    tools.handlers = {"browser_verify": lambda **kw: gateway.verify(
        {**kw, "steps": kw.get("steps") or []})}
    result = tools._handler_turn(
        "browser_verify", target=None, text=None, agent_type="shell",
        working_directory=None, initial_prompt=None, grant_mode="none", binding=None,
        node="auto", title=None, priority=0, metadata=None, request_key=None,
        task_id=None, task_ids=None, url="https://example.com",
        steps=[{"op": "assert_url", "contains": "example"}])
    assert result["status"] == "OK"
    assert result["result"]["status"] == "PASS"


def test_turn_browser_verify_without_a_url_is_refused(tmp_path):
    from terminal_mcp.compact_tools import CompactTerminalTools

    tools = CompactTerminalTools.__new__(CompactTerminalTools)
    tools.handlers = {"browser_verify": lambda **kw: {"status": "PASS"}}
    result = tools._handler_turn(
        "browser_verify", target=None, text=None, agent_type="shell",
        working_directory=None, initial_prompt=None, grant_mode="none", binding=None,
        node="auto", title=None, priority=0, metadata=None, request_key=None,
        task_id=None, task_ids=None, url=None)
    assert result["error"] == "URL_REQUIRED"


def test_an_unwired_browser_action_refuses_honestly():
    from terminal_mcp.compact_tools import CompactTerminalTools

    tools = CompactTerminalTools.__new__(CompactTerminalTools)
    tools.handlers = {"browser_verify": None}
    result = tools._handler_turn(
        "browser_verify", target=None, text=None, agent_type="shell",
        working_directory=None, initial_prompt=None, grant_mode="none", binding=None,
        node="auto", title=None, priority=0, metadata=None, request_key=None,
        task_id=None, task_ids=None, url="https://example.com")
    assert result["error"] == "ACTION_UNAVAILABLE"
