"""dashboard.py's /dashboard/api/ai-usage route -- the read-only AI
Usage panel's own data source. Real Starlette TestClient (in-process
ASGI, real routing/middleware, no real network) + an injected fake
fetcher on AiUsageService -- no real dependency on the actual local AI
Usage Monitor service being up."""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.ai_usage_client import AiUsageClientError
from terminal_mcp.ai_usage_service import AiUsageService
from terminal_mcp.config import AiUsageConfig, AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp


def _config() -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )


def _client(tmp_path, *, fetcher, session_lister=None) -> TestClient:
    from terminal_mcp.grants import SessionGrantStore
    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))
    ai_usage = AiUsageService(AiUsageConfig(), fetcher=fetcher, session_lister=session_lister)
    server = build_mcp(service, ai_usage=ai_usage)
    register_dashboard(server, service, ai_usage=ai_usage)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})


def _fetcher_returning(payload):
    def fetch(base_url, timeout):
        return payload
    return fetch


def _fetcher_raising(message="boom"):
    def fetch(base_url, timeout):
        raise AiUsageClientError(message)
    return fetch


def test_route_returns_real_normalized_usage(tmp_path):
    client = _client(tmp_path, fetcher=_fetcher_returning(
        {"app_version": "0.6.0", "codex": {"ok": True, "five_hour": {"used_percent": 42.0}}}))
    response = client.get("/dashboard/api/ai-usage")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    codex = next(p for p in body["providers"] if p["provider"] == "codex")
    assert codex["windows"][0]["used_percent"] == 42.0
    assert response.headers["cache-control"] == "no-store"


def test_route_degrades_cleanly_when_ai_usage_monitor_is_down(tmp_path):
    client = _client(tmp_path, fetcher=_fetcher_raising("connection refused"))
    response = client.get("/dashboard/api/ai-usage")
    assert response.status_code == 200  # never a 5xx -- a degraded body, not a broken route
    body = response.json()
    assert body["available"] is False
    assert "connection refused" in body["error"]


def test_route_force_query_param_bypasses_cache(tmp_path):
    calls = []

    def fetch(base_url, timeout):
        calls.append(1)
        return {"codex": {"ok": True}}
    client = _client(tmp_path, fetcher=fetch)
    client.get("/dashboard/api/ai-usage")
    client.get("/dashboard/api/ai-usage?force=1")
    assert len(calls) == 2


def test_route_includes_local_session_correlation(tmp_path):
    client = _client(tmp_path, fetcher=_fetcher_returning({"claude": {"ok": True}}),
                     session_lister=lambda: [{"name": "window", "agent_type": "claude"}])
    response = client.get("/dashboard/api/ai-usage")
    body = response.json()
    assert body["sessions"] == [{"session": "window", "node_id": "local", "agent_type": "claude",
                                 "provider": "claude", "quota_available": True}]


def test_dashboard_page_html_includes_the_ai_usage_panel(tmp_path):
    client = _client(tmp_path, fetcher=_fetcher_returning({}))
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.text
    assert 'id="aiUsagePanel"' in html
    assert 'id="openAiUsageBtn"' in html
    assert "/dashboard/api/ai-usage" in html
