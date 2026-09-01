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
    body = response.json()
    assert body["version"] == __version__
    # This checkout is a real git repo -- source identity should resolve,
    # not silently report unavailable.
    assert body["source"]["git_available"] is True
    assert isinstance(body["source"]["commit"], str) and body["source"]["commit"]
    assert isinstance(body["source"]["dirty"], bool)


def test_health_and_version_routes_are_never_cached(tmp_path):
    client = _client(tmp_path)
    for path in ("/health/live", "/health/ready", "/version", "/health/metrics"):
        response = client.get(path)
        assert response.headers.get("cache-control") == "no-store"


def test_health_ready_reports_every_durable_store(tmp_path):
    # bindings/grants/leases use their default (non-tmp_path) locations in
    # _client -- still, each must appear in the checks dict and pass, since
    # the final audit pass requires readiness to cover every store this
    # process actually opens, not just audit.db.
    client = _client(tmp_path)
    response = client.get("/health/ready")
    body = response.json()
    for name in ("tmux", "audit_db", "bindings_db", "grants_db", "leases_db"):
        assert name in body["checks"], body["checks"]
        assert body["checks"][name]["ok"] is True
    assert "supervisor" not in body["checks"]  # no SupervisorService was registered


def test_health_ready_reports_503_when_a_secondary_store_is_broken(tmp_path, monkeypatch):
    client = _client(tmp_path)

    import terminal_mcp.health as health_module

    def _fake_check_sqlite(path):
        return (False, "simulated disk outage") if str(path).endswith("bindings.db") else (True, "ok")

    monkeypatch.setattr(health_module, "_check_sqlite", _fake_check_sqlite)
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["bindings_db"]["ok"] is False
    # A single broken secondary store must not be reported as if tmux itself
    # were the problem.
    assert body["checks"]["tmux"]["ok"] is True


def test_health_metrics_endpoint_reports_counter_snapshot(tmp_path):
    from terminal_mcp import metrics as metrics_module

    metrics_module.increment("delivery.text_sent")
    client = _client(tmp_path)
    response = client.get("/health/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["counters"]["delivery.text_sent"] >= 1
    assert "supervisor.policy_blocked" in body["counters"]
    assert "supervisor" not in body  # no SupervisorService was registered


def _supervisor_client(tmp_path: Path):
    from terminal_mcp.supervisor import SupervisorService, SupervisorStore

    config = AppConfig(
        PermissionsConfig(True, False), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))
    supervisor = SupervisorService(service, SupervisorStore(tmp_path / "supervisor.db"))
    server = build_mcp(service, supervisor)
    register_health(server, service, supervisor)
    return TestClient(server.streamable_http_app())


def test_health_ready_includes_informational_supervisor_staleness_when_registered(tmp_path):
    client = _supervisor_client(tmp_path)
    response = client.get("/health/ready")
    assert response.status_code == 200  # supervisor never gates overall readiness
    body = response.json()
    assert body["checks"]["supervisor"]["enabled"] is False  # not enabled in this AppConfig


def test_health_ready_supervisor_staleness_never_flips_overall_status(tmp_path, monkeypatch):
    client = _supervisor_client(tmp_path)

    import terminal_mcp.health as health_module

    monkeypatch.setattr(
        health_module, "_check_supervisor_staleness",
        lambda supervisor: {"enabled": True, "loop_running": True, "stale": True, "last_poll_age_seconds": 99999},
    )
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["supervisor"]["stale"] is True


def test_health_metrics_includes_supervisor_staleness_when_registered(tmp_path):
    client = _supervisor_client(tmp_path)
    response = client.get("/health/metrics")
    body = response.json()
    assert "supervisor" in body
    assert body["supervisor"]["enabled"] is False
