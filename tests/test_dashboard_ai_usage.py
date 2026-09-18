"""dashboard.py's /dashboard/api/ai-usage route.

It used to proxy a SEPARATE local service (the AI Usage Monitor, an
external project on 127.0.0.1:8787). That service is not installed on this
fleet, so the route answered `available:false, "Connection refused"` and
every client rendered an empty panel -- including a dashboard tab opened
before the report page existed, which keeps polling this endpoint and never
reloads. That was the whole of "AI Usage shows nothing".

The route is served from the LOCAL collector now (ai_usage_index.py), so
there is no runtime dependency on any external project and a stale tab
recovers without being reloaded. These tests were rewritten to that
contract; the ones that pinned the proxy's cache/degradation behaviour are
gone because the behaviour they described is gone.

Real Starlette TestClient: in-process ASGI, real routing and middleware,
no network."""
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


def _local_payload(tmp_path, monkeypatch, events=2):
    """Point the collector at a disposable index and a fixture transcript."""
    import json
    import time

    home = tmp_path / "claude"
    project = home / "projects" / "-repo"
    project.mkdir(parents=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"
    with (project / "sess.jsonl").open("w", encoding="utf-8") as handle:
        for index in range(events):
            handle.write(json.dumps({
                "type": "assistant", "uuid": f"u{index}", "timestamp": stamp,
                "sessionId": "sess", "cwd": "/repo", "version": "2.1.266",
                "message": {"role": "assistant", "model": "claude-opus-5",
                            "usage": {"input_tokens": 1, "output_tokens": 2,
                                      "cache_read_input_tokens": 3,
                                      "cache_creation_input_tokens": 4}}}) + "\n")
    monkeypatch.setenv("TERMINAL_MCP_AI_USAGE_DB", str(tmp_path / "ai_usage.db"))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nocodex"))


def test_the_route_answers_from_local_data_not_an_external_service(tmp_path, monkeypatch):
    _local_payload(tmp_path, monkeypatch)
    client = _client(tmp_path, fetcher=_fetcher_raising("must not be called"))
    body = client.get("/dashboard/api/ai-usage?force=1").json()
    # The injected fetcher raises; the route must never touch it.
    assert body["available"] is True
    assert body["error"] is None
    assert "127.0.0.1:8787" not in str(body.get("source"))
    assert body["report_url"] == "/dashboard/ai-usage"


def test_a_dead_external_service_no_longer_empties_the_panel(tmp_path, monkeypatch):
    # The reproduction of the reported bug, as a test.
    _local_payload(tmp_path, monkeypatch)
    client = _client(tmp_path, fetcher=_fetcher_raising("Connection refused"))
    body = client.get("/dashboard/api/ai-usage?force=1").json()
    claude = next(p for p in body["providers"] if p["provider"] == "claude")
    assert claude["ok"] is True
    assert claude["windows"], "the panel must have something real to render"


def test_the_activity_window_never_invents_a_percentage(tmp_path, monkeypatch):
    _local_payload(tmp_path, monkeypatch)
    client = _client(tmp_path, fetcher=_fetcher_returning({}))
    body = client.get("/dashboard/api/ai-usage?force=1").json()
    window = next(p for p in body["providers"] if p["provider"] == "claude")["windows"][0]
    assert window["used_percent"] is None      # a percent needs a limit; none is stated
    # so the figure travels in the label, which the older renderer still shows
    assert "tokens" in window["label"]


def test_codex_without_local_data_does_not_break_the_claude_report(tmp_path, monkeypatch):
    _local_payload(tmp_path, monkeypatch)
    client = _client(tmp_path, fetcher=_fetcher_returning({}))
    body = client.get("/dashboard/api/ai-usage?force=1").json()
    codex = next(p for p in body["providers"] if p["provider"] == "codex")
    claude = next(p for p in body["providers"] if p["provider"] == "claude")
    assert codex["ok"] is False and "No" in (codex["usage_message"] or "")
    assert claude["ok"] is True


def test_the_payload_carries_per_session_rows_and_totals(tmp_path, monkeypatch):
    _local_payload(tmp_path, monkeypatch, events=3)
    client = _client(tmp_path, fetcher=_fetcher_returning({}))
    body = client.get("/dashboard/api/ai-usage?force=1").json()
    assert len(body["sessions"]) == 1
    assert body["totals"]["lifetime"]["events"] == 3
    assert body["totals"]["rolling_5h"]["total"] == 3 * (1 + 2 + 3 + 4)


def test_the_menu_links_to_the_local_ai_usage_report(tmp_path):
    # The menu used to open an in-page panel backed by a SEPARATE local
    # service (ai_usage_client.py, 127.0.0.1:8787). It now links to
    # /dashboard/ai-usage, which reads the CLIs' own local artefacts and
    # therefore works whether or not that service is installed -- it is not
    # installed on this fleet.
    client = _client(tmp_path, fetcher=_fetcher_returning({}))
    response = client.get("/dashboard")
    assert response.status_code == 200
    html = response.text
    assert 'href="/dashboard/ai-usage"' in html
    assert 'id="aiUsageLink"' in html
    assert 'id="openAiUsageBtn"' not in html
    assert "/dashboard/api/ai-usage" in html
