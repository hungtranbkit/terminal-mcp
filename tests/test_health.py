"""P1 hardening item #6: /health/live, /health/ready, /version."""
from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from terminal_mcp import __version__
from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.health import register_health
from terminal_mcp.mcp_app import build_mcp


def _client(tmp_path: Path) -> TestClient:
    config = AppConfig(
        PermissionsConfig(True, False), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))
    server = build_mcp(service)
    register_health(server, service)
    return TestClient(server.streamable_http_app())


def test_health_live_always_ok(tmp_path):
    client = _client(tmp_path)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_ready_reports_tmux_and_audit_db_checks(tmp_path):
    # tmux is genuinely present in this test environment and audit.db is a
    # real, freshly created file -- both checks should pass.
    client = _client(tmp_path)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["tmux"]["ok"] is True
    assert body["checks"]["audit_db"]["ok"] is True


def test_health_ready_reports_503_when_a_dependency_is_broken(tmp_path, monkeypatch):
    client = _client(tmp_path)

    import terminal_mcp.health as health_module

    monkeypatch.setattr(health_module, "_check_tmux", lambda terminal: (False, "simulated tmux outage"))
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["tmux"]["ok"] is False
    assert body["checks"]["tmux"]["detail"] == "simulated tmux outage"


def test_version_endpoint_reports_the_real_package_version(tmp_path):
    client = _client(tmp_path)
    response = client.get("/version")
    assert response.status_code == 200
    assert response.json() == {"version": __version__}


def test_health_and_version_routes_are_never_cached(tmp_path):
    client = _client(tmp_path)
    for path in ("/health/live", "/health/ready", "/version"):
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-store"
