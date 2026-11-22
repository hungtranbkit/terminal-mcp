"""Public AI Usage contract: finite projections only, no accounts or secrets."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from starlette.testclient import TestClient

from terminal_mcp.ai_usage_public import PublicUsageService, safe_public_usage
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.node_agent import build_node_agent


NOW = datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc).isoformat()


def _raw():
    return {
        "timestamp": NOW,
        "codex": {
            "ok": True, "plan": "private-plan", "account": {
                "email": "private-person@example.com", "access_token": "sk-secret"},
            "five_hour": {"used_percent": 54.23, "remaining_percent": 45.77,
                          "resets_at": 1795320000},
            "weekly": {"used_percent": 12.0, "remaining_percent": 88.0,
                       "resets_at": 1795751440},
            "other_windows": [{"secret": "do-not-publish"}],
            "updated_at": NOW,
        },
        "claude": {"ok": False, "error": "private account and token", "updated_at": NOW},
        "gemini": {"ok": True, "account": "private-account", "usage_status": "USAGE_UNAVAILABLE"},
        "antigravity": {"ok": True, "account": "secret",
                        "quota_windows": [{"id": "user-model-id", "remaining_percent": 90}]},
    }


def test_projection_is_fixed_and_drops_identifiers():
    result = safe_public_usage(_raw(), sampled_at=NOW)
    assert result["kind"] == "terminal-mcp-public-ai-usage"
    assert result["schema_version"] == 1
    assert [p["provider"] for p in result["providers"]] == [
        "codex", "claude", "gemini", "antigravity"]
    codex = result["providers"][0]
    assert codex["windows"]["five_hour"]["used_percent"] == 54.2
    assert codex["windows"]["weekly"]["remaining_percent"] == 88.0
    assert codex["windows"]["weekly"]["reset_at"] == 1795751440
    assert result["providers"][1]["state"] == "unavailable"
    assert result["providers"][1]["windows"] == {}
    assert result["providers"][2]["state"] == "not_observed"
    encoded = json.dumps(result)
    for secret in ("private-person", "access_token", "sk-secret", "email", "plan",
                   "user-model-id", "account", "private account", "other_windows"):
        assert secret not in encoded


def test_absent_or_invalid_quota_is_unknown_not_zero():
    raw = {"codex": {"ok": True, "five_hour": {"used_percent": -5},
                     "weekly": {"used_percent": 101}},
           "claude": {"ok": True, "five_hour": {"used_percent": float("nan")}}}
    result = safe_public_usage(raw, sampled_at=NOW)
    assert result["providers"][0]["windows"] == {}
    assert result["providers"][0]["state"] == "not_observed"
    assert result["providers"][1]["state"] == "not_observed"
    value = safe_public_usage({"codex": {"ok": True, "weekly": {"remaining_percent": 72.8}}},
                              sampled_at=NOW)["providers"][0]["windows"]["weekly"]
    assert value["used_percent"] == 27.2


def test_caching_is_read_only_and_source_failure_is_explicit():
    tick = [0.0]
    calls = []
    def fetch(url, timeout):
        calls.append((url, timeout))
        if len(calls) > 1:
            raise PermissionError("private account path never disclosed")
        return _raw()
    service = PublicUsageService(fetcher=fetch, clock=lambda: tick[0],
                                 now=lambda: datetime(2026, 11, 22, 3, tzinfo=timezone.utc),
                                 ttl_seconds=60)
    first = service.get()
    first["providers"][0]["windows"]["weekly"]["used_percent"] = 999
    assert service.get()["providers"][0]["windows"]["weekly"]["used_percent"] == 12.0
    assert len(calls) == 1
    tick[0] = 61.0
    failure = service.get()
    assert failure["source_status"] == "unavailable"
    assert all(p["windows"] == {} for p in failure["providers"])
    assert "PermissionError" not in json.dumps(failure)
    assert calls == [("http://127.0.0.1:8787", 2.0)] * 2


def test_node_route_requires_bearer_and_only_allows_get(tmp_path):
    cfg = AppConfig(PermissionsConfig(True, True), ("*",), 50, 20,
                    InputPolicyConfig(allowed_session_patterns=("*",)))
    terminal = TerminalService(cfg)
    class Stub:
        def get(self):
            return safe_public_usage(_raw(), sampled_at=NOW)
    api = TestClient(build_node_agent(node_id="dell-linux", terminal=terminal,
                                      token="fake-test-token", ai_usage_service=Stub()))
    assert api.get("/v1/ai-usage/summary").status_code == 401
    assert api.get("/v1/ai-usage/summary",
                   headers={"Authorization": "Bearer wrong"}).status_code == 401
    res = api.get("/v1/ai-usage/summary",
                  headers={"Authorization": "Bearer fake-test-token"})
    assert res.status_code == 200
    assert res.json()["providers"][0]["windows"]["weekly"]["used_percent"] == 12.0
    assert "private-person" not in res.text
    assert api.post("/v1/ai-usage/summary",
                    headers={"Authorization": "Bearer fake-test-token"}).status_code == 405
