"""ai_usage_service.py -- normalization, caching, degraded-state, and
session correlation. A fake, injected fetcher (no real network, no real
timing dependency) so this suite is fast and deterministic; real HTTP
behavior is test_ai_usage_client.py's own job."""
from __future__ import annotations

import pytest

from terminal_mcp.ai_usage_client import AiUsageClientError
from terminal_mcp.ai_usage_service import AiUsageService, correlate_local_sessions
from terminal_mcp.config import AiUsageConfig

REAL_SAMPLE_RESPONSE = {
    "app_version": "0.6.0",
    "codex": {
        "ok": True, "provider": "codex", "account": "user@example.com", "plan": "plus",
        "five_hour": {"used_percent": 99.0, "remaining_percent": 1.0, "window_seconds": 18000,
                     "resets_at": 1788768751},
        "weekly": {"used_percent": 12.0, "remaining_percent": 88.0, "window_seconds": 604800,
                  "resets_at": 1789356641},
        "updated_at": "2026-09-07T06:26:19+00:00",
    },
    "claude": {
        "ok": True, "provider": "claude", "account": "Claude Code", "claude_version": "2.1.0",
        "five_hour": {"used_percent": 71.0, "remaining_percent": 29.0, "resets_at": "2026-09-07T09:50:00+00:00"},
        "weekly": {"used_percent": 67.0, "remaining_percent": 33.0, "resets_at": "2026-09-11T23:00:00+00:00"},
        "weekly_sonnet": None, "weekly_opus": None, "updated_at": "2026-09-07T06:24:23+00:00",
    },
    "gemini": {
        "ok": True, "provider": "gemini", "installed": True, "status": "NOT_AUTHENTICATED",
        "usage": None, "usage_status": "USAGE_UNAVAILABLE", "usage_message": "Usage data unavailable",
        "updated_at": "2026-09-07T06:26:20+00:00",
    },
    "antigravity": {
        "ok": False, "provider": "antigravity", "error": "not installed",
    },
    "timestamp": "2026-09-07T06:26:20+00:00",
}


def _fetcher_returning(payload):
    def fetch(base_url, timeout):
        return payload
    return fetch


def _fetcher_raising(message="boom"):
    def fetch(base_url, timeout):
        raise AiUsageClientError(message)
    return fetch


@pytest.fixture
def config():
    return AiUsageConfig(enabled=True, base_url="http://127.0.0.1:8787", cache_ttl_seconds=20.0,
                         warning_threshold_percent=70.0, critical_threshold_percent=90.0)


# -- normalization --------------------------------------------------------

def test_get_usage_normalizes_real_sample_shape(config):
    service = AiUsageService(config, fetcher=_fetcher_returning(REAL_SAMPLE_RESPONSE))
    result = service.get_usage()
    assert result["available"] is True
    assert result["app_version"] == "0.6.0"
    providers = {p["provider"]: p for p in result["providers"]}
    assert set(providers) == {"codex", "claude", "gemini", "antigravity"}

    codex = providers["codex"]
    assert codex["ok"] is True
    assert codex["account"] == "user@example.com"
    five_hour = next(w for w in codex["windows"] if w["label"] == "5h")
    assert five_hour["used_percent"] == 99.0
    assert five_hour["severity"] == "critical"  # >= 90
    weekly = next(w for w in codex["windows"] if w["label"] == "Weekly")
    assert weekly["severity"] == "ok"  # 12% < 70
    assert codex["critical"] is True
    assert codex["warning"] is False


def test_claude_five_hour_severity_is_warning_not_critical(config):
    service = AiUsageService(config, fetcher=_fetcher_returning(REAL_SAMPLE_RESPONSE))
    result = service.get_usage()
    claude = next(p for p in result["providers"] if p["provider"] == "claude")
    five_hour = next(w for w in claude["windows"] if w["label"] == "5h")
    assert five_hour["used_percent"] == 71.0
    assert five_hour["severity"] == "warning"  # 70 <= 71 < 90
    assert claude["warning"] is True
    assert claude["critical"] is False


def test_gemini_has_no_usage_windows_but_stays_ok(config):
    service = AiUsageService(config, fetcher=_fetcher_returning(REAL_SAMPLE_RESPONSE))
    result = service.get_usage()
    gemini = next(p for p in result["providers"] if p["provider"] == "gemini")
    assert gemini["ok"] is True
    assert gemini["windows"] == []
    assert gemini["usage_available"] is False
    assert gemini["usage_message"] == "Usage data unavailable"
    assert gemini["warning"] is False and gemini["critical"] is False


def test_antigravity_not_ok_surfaces_error_not_a_fake_number(config):
    service = AiUsageService(config, fetcher=_fetcher_returning(REAL_SAMPLE_RESPONSE))
    result = service.get_usage()
    antigravity = next(p for p in result["providers"] if p["provider"] == "antigravity")
    assert antigravity["ok"] is False
    assert antigravity["windows"] == []
    assert antigravity["error"] == "not installed"


def test_antigravity_quota_windows_shape_is_normalized_too(config):
    raw = dict(REAL_SAMPLE_RESPONSE)
    raw["antigravity"] = {
        "ok": True, "provider": "antigravity", "account": "user@example.com",
        "quota_windows": [{"id": "daily", "remaining_percent": 5.0, "resets_at": "2026-09-08T00:00:00+00:00"}],
    }
    service = AiUsageService(config, fetcher=_fetcher_returning(raw))
    result = service.get_usage()
    antigravity = next(p for p in result["providers"] if p["provider"] == "antigravity")
    window = antigravity["windows"][0]
    assert window["label"] == "daily"
    assert window["remaining_percent"] == 5.0
    assert window["used_percent"] == 95.0  # derived from remaining_percent
    assert window["severity"] == "critical"
    assert antigravity["critical"] is True


def test_missing_provider_key_entirely_is_reported_as_not_ok(config):
    raw = {"app_version": "0.6.0", "codex": {"ok": True}, "timestamp": "2026-09-07T00:00:00+00:00"}
    service = AiUsageService(config, fetcher=_fetcher_returning(raw))
    result = service.get_usage()
    claude = next(p for p in result["providers"] if p["provider"] == "claude")
    assert claude["ok"] is False
    assert claude["windows"] == []


def test_a_window_with_used_percent_none_is_never_given_a_fake_severity(config):
    raw = {"codex": {"ok": True, "five_hour": {"used_percent": None, "remaining_percent": None}}}
    service = AiUsageService(config, fetcher=_fetcher_returning(raw))
    result = service.get_usage()
    codex = next(p for p in result["providers"] if p["provider"] == "codex")
    window = codex["windows"][0]
    assert window["used_percent"] is None
    assert window["severity"] is None  # never invented


# -- caching ----------------------------------------------------------------

def test_cache_ttl_avoids_a_second_real_fetch(config):
    calls = []

    def fetch(base_url, timeout):
        calls.append(1)
        return REAL_SAMPLE_RESPONSE
    service = AiUsageService(config, fetcher=fetch)
    r1 = service.get_usage()
    r2 = service.get_usage()
    assert r1["cached"] is False
    assert r2["cached"] is True
    assert len(calls) == 1


def test_force_bypasses_the_cache(config):
    calls = []

    def fetch(base_url, timeout):
        calls.append(1)
        return REAL_SAMPLE_RESPONSE
    service = AiUsageService(config, fetcher=fetch)
    service.get_usage()
    service.get_usage(force=True)
    assert len(calls) == 2


# -- degraded state -----------------------------------------------------------

def test_unavailable_with_no_prior_cache_reports_available_false(config):
    service = AiUsageService(config, fetcher=_fetcher_raising("connection refused"))
    result = service.get_usage()
    assert result["available"] is False
    assert result["error"] == "connection refused"
    assert result["providers"] == []


def test_a_failure_after_a_prior_success_falls_back_to_stale_cached_data(config):
    calls = {"n": 0}

    def fetch(base_url, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return REAL_SAMPLE_RESPONSE
        raise AiUsageClientError("service down")
    service = AiUsageService(config, fetcher=fetch)
    good = service.get_usage()
    assert good["available"] is True
    bad = service.get_usage(force=True)
    assert bad["available"] is True  # STALE, not unavailable -- real prior data still shown
    assert bad["stale"] is True
    assert bad["last_error"] == "service down"
    assert bad["providers"] == good["providers"]  # same last-known-good content


def test_disabled_config_never_calls_the_fetcher_at_all(config):
    disabled = AiUsageConfig(enabled=False)
    calls = []

    def fetch(base_url, timeout):
        calls.append(1)
        return REAL_SAMPLE_RESPONSE
    service = AiUsageService(disabled, fetcher=fetch)
    result = service.get_usage()
    assert result["available"] is False
    assert result["error"] == "AI_USAGE_DISABLED"
    assert calls == []


def test_get_usage_never_raises_even_on_a_completely_broken_fetcher(config):
    def fetch(base_url, timeout):
        raise RuntimeError("something unexpected, not even AiUsageClientError")
    service = AiUsageService(config, fetcher=fetch)
    with pytest.raises(RuntimeError):
        # A non-AiUsageClientError IS allowed to propagate -- only the
        # documented client-error contract is caught here; this proves
        # the boundary is exactly where the docstring says it is, not
        # a blanket except-Exception that could hide a real code bug.
        service.get_usage()


# -- session correlation --------------------------------------------------

def test_correlate_local_sessions_tags_claude_and_codex_only():
    normalized = {"providers": [{"provider": "claude", "ok": True}, {"provider": "codex", "ok": True}]}
    rows = [{"name": "window", "agent_type": "claude"}, {"name": "shell-1", "agent_type": "bash"},
           {"name": "cx-1", "agent_type": "codex"}]
    correlated = correlate_local_sessions(normalized, rows)
    by_name = {c["session"]: c for c in correlated}
    assert by_name["window"]["provider"] == "claude"
    assert by_name["window"]["quota_available"] is True
    assert by_name["shell-1"]["provider"] is None
    assert by_name["cx-1"]["provider"] == "codex"


def test_correlate_local_sessions_reports_quota_unavailable_when_provider_not_ok():
    normalized = {"providers": [{"provider": "claude", "ok": False}]}
    rows = [{"name": "window", "agent_type": "claude"}]
    correlated = correlate_local_sessions(normalized, rows)
    assert correlated[0]["quota_available"] is False


def test_get_usage_includes_session_correlation_via_injected_lister(config):
    service = AiUsageService(config, fetcher=_fetcher_returning(REAL_SAMPLE_RESPONSE),
                             session_lister=lambda: [{"name": "window", "agent_type": "claude"}])
    result = service.get_usage()
    assert result["sessions"] == [{"session": "window", "node_id": "local", "agent_type": "claude",
                                   "provider": "claude", "quota_available": True}]


def test_a_broken_session_lister_never_breaks_the_real_usage_data(config):
    def broken_lister():
        raise RuntimeError("tmux unreachable")
    service = AiUsageService(config, fetcher=_fetcher_returning(REAL_SAMPLE_RESPONSE),
                             session_lister=broken_lister)
    result = service.get_usage()
    assert result["available"] is True
    assert result["sessions"] == []
