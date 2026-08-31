from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, load_config
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import DASHBOARD_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp

REPO_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def test_dashboard_routes_are_registered(read_config):
    service = TerminalService(read_config)
    server = build_mcp(service)
    register_dashboard(server, service)

    routes = {route.path: set(route.methods) for route in server._custom_starlette_routes}
    assert routes["/dashboard"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/sessions"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/session"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/session/input"] == {"POST"}


def test_dashboard_uses_safe_dom_rendering():
    assert "innerHTML" not in DASHBOARD_HTML
    assert "textContent" in DASHBOARD_HTML
    assert "Whitelisted tmux session monitor" in DASHBOARD_HTML


def test_dashboard_auto_scrolls_output_to_newest_line():
    # UX guard: the pane must land on the newest (bottom) output without the
    # viewer having to scroll manually, without reordering the rendered lines.
    assert "outputEl.scrollTop = outputEl.scrollHeight" in DASHBOARD_HTML


def test_session_detail_tail_is_bounded_recent_and_chronological(read_config, tmux_session_factory):
    # Enough lines that the visible pane + config's default_tail_lines window
    # (see conftest.py) can't possibly cover the whole thing, so a truly
    # unbounded/full-history read would be distinguishable from a tail.
    session = tmux_session_factory(
        "test-tail-order",
        "bash -lc 'for i in $(seq -w 1 100); do echo line$i; done; sleep 30'",
    )
    client, _ = _client(read_config)
    response = client.get(f"/dashboard/api/session?name={session}")
    assert response.status_code == 200
    lines = response.json()["tail"]["output"].splitlines()

    # Exact bound: read_config's default_tail_lines is 20 (see conftest.py), and
    # 100 real lines were produced, so exactly the most recent 20 come back —
    # not "at least" or "roughly" 20, and no leftover blank padding rows.
    assert lines == [f"line{i:03d}" for i in range(81, 101)]


def test_session_detail_tail_length_is_driven_by_config_not_hardcoded(tmux_session_factory):
    # Same session, two services differing only in default_tail_lines, called via
    # terminal_tail(name) with no explicit `lines` — proves the dashboard route
    # (terminal_mcp/dashboard.py) no longer hardcodes 200 and instead flows
    # through config.default_tail_lines, the project's existing single source of
    # truth for "how many recent lines" (see config.yaml).
    session = tmux_session_factory(
        "test-tail-config",
        "bash -lc 'for i in $(seq -w 1 100); do echo line$i; done; sleep 30'",
    )
    small_client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 90, 3))
    large_client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 90, 60))

    small_output = small_client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]
    large_output = large_client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]

    # Each config's configured count comes back exactly, not just "more than".
    assert len(small_output.splitlines()) == 3
    assert len(large_output.splitlines()) == 60


def test_session_detail_tail_respects_300_line_config_exactly(tmux_session_factory):
    # The actual value now shipped in config.yaml: 400 real lines produced,
    # default_tail_lines=300 configured -> exactly the most recent 300 come
    # back, oldest-first/newest-last, with no reordering or off-by-some slop.
    session = tmux_session_factory(
        "test-tail-300",
        "bash -lc 'for i in $(seq -w 1 400); do echo line$i; done; sleep 30'",
    )
    client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 500, 300))
    response = client.get(f"/dashboard/api/session?name={session}")
    assert response.status_code == 200
    lines = response.json()["tail"]["output"].splitlines()

    assert lines == [f"line{i:03d}" for i in range(101, 401)]


def test_repo_config_yaml_default_tail_lines_is_300():
    # Guards the actual deployed source of truth the dashboard route reads
    # (terminal.terminal_tail(name) with no explicit `lines`): config.yaml's
    # default_tail_lines, not a hardcoded route-level number.
    config = load_config(REPO_CONFIG_PATH)
    assert config.default_tail_lines == 300


@pytest.fixture
def input_config() -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True),
        ("test-*", "agent-*"),
        50,
        20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )


def _client(config: AppConfig) -> tuple[TestClient, TerminalService]:
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    return TestClient(server.streamable_http_app()), service


def test_session_input_blocked_when_input_permission_disabled(read_config):
    client, _ = _client(read_config)  # read_config has terminal_input=False
    response = client.post("/dashboard/api/session/input", json={"name": "test-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "INPUT_DISABLED"


def test_session_input_blocked_for_unmatched_session(input_config):
    client, _ = _client(input_config)
    response = client.post("/dashboard/api/session/input", json={"name": "agent-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "ACCESS_DENIED"


def test_session_input_rejects_malformed_body(input_config):
    client, _ = _client(input_config)
    response = client.post("/dashboard/api/session/input", json={"name": "test-x"})
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"


def test_session_input_sends_text_to_allowed_session(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-input")
    client, _ = _client(input_config)
    response = client.post(
        "/dashboard/api/session/input",
        json={"name": session, "text": "echo hi", "press_enter": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"session": session, "sent": True, "characters": len("echo hi"), "press_enter": False}


def test_session_detail_reports_input_allowed(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-detail")
    client, _ = _client(input_config)
    response = client.get(f"/dashboard/api/session?name={session}")
    assert response.status_code == 200
    assert response.json()["input_allowed"] is True
