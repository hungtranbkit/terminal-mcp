"""Characterization/contract tests for the AI Usage read path, pinned
against a REAL (redacted) /api/usage response captured from the live AI
Usage Monitor on this host (tests/fixtures/ai_usage_live_sample.json,
app_version 0.6.0).

Why a separate file: the existing tests/test_ai_usage_service.py builds
its own hand-written minimal payloads, so it never sees the fields the
real service actually emits. These tests pin the SHAPE OF THE REAL
UPSTREAM CONTRACT -- specifically the three different "percent" concepts
that look alike and are not:

  1. quota/account %   -- codex/claude `five_hour`/`weekly`, and
                          antigravity `quota_windows[]`. Keys are
                          `used_percent` / `remaining_percent`.
  2. context-window %  -- antigravity `context_window`. Keys are
                          `used_percentage` / `remaining_percentage`
                          (DIFFERENT SUFFIX) plus token counts. Not
                          read by this project at all today.
  3. token/task usage  -- only ever present inside (2)'s token counts;
                          there is no per-session token accounting
                          anywhere in this read path.

Tests named `test_gap_*` document a CURRENT LIMITATION on purpose. They
are not asserting that the limitation is correct -- they exist so that
when someone closes the gap, the test fails loudly and gets updated
alongside the fix, rather than the contract drifting silently.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from terminal_mcp.ai_usage_service import _normalize, _provider_windows
from terminal_mcp.config import AiUsageConfig

FIXTURE = Path(__file__).parent / "fixtures" / "ai_usage_live_sample.json"


@pytest.fixture
def raw() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def config() -> AiUsageConfig:
    return AiUsageConfig()


# --- 1. quota/account % : the path that actually works today ----------

def test_quota_windows_normalize_from_real_payload(raw, config):
    """codex + claude both surface real, non-null quota percentages."""
    norm = _normalize(raw, config)
    by_provider = {p["provider"]: p for p in norm["providers"]}
    for key in ("codex", "claude"):
        provider = by_provider[key]
        assert provider["ok"] is True
        assert provider["usage_available"] is True
        labels = [w["label"] for w in provider["windows"]]
        assert labels == ["5h", "Weekly"], f"{key} windows drifted: {labels}"
        for window in provider["windows"]:
            assert window["used_percent"] is not None
            assert window["remaining_percent"] is not None
            assert window["severity"] in ("ok", "warning", "critical")


def test_resets_at_is_polymorphic_and_passed_through_unchanged(raw, config):
    """codex reports resets_at as an INT epoch, claude as an ISO STRING.
    _normalize passes both through untouched, so every consumer must
    handle both types. The dashboard's auFmtTime() branches on
    `typeof iso === 'number'` precisely for this -- any new consumer
    (MCP tool output, a node/CLI renderer) has to do the same or it
    renders "Invalid Date"."""
    norm = _normalize(raw, config)
    by_provider = {p["provider"]: p for p in norm["providers"]}
    assert isinstance(by_provider["codex"]["windows"][0]["resets_at"], int)
    assert isinstance(by_provider["claude"]["windows"][0]["resets_at"], str)


def test_null_subwindows_are_skipped_not_rendered_as_zero(raw, config):
    """claude's weekly_sonnet/weekly_opus are literal JSON null here.
    They must be DROPPED, never emitted as a 0%/unavailable window --
    a fabricated 0% reads as 'plenty of quota left', the exact opposite
    of 'no data'."""
    assert raw["claude"]["weekly_sonnet"] is None
    assert raw["claude"]["weekly_opus"] is None
    norm = _normalize(raw, config)
    claude = next(p for p in norm["providers"] if p["provider"] == "claude")
    assert [w["label"] for w in claude["windows"]] == ["5h", "Weekly"]


def test_no_provider_gets_an_invented_percentage(raw, config):
    """Providers with no usage data must have ZERO windows -- never a
    zero-filled bar standing in for real data."""
    norm = _normalize(raw, config)
    for provider in norm["providers"]:
        if not provider["usage_available"]:
            assert provider["windows"] == []
            assert provider["usage_message"], (
                f"{provider['provider']} has no windows and no explanatory message"
            )


# --- 2. context-window % : the field-name trap ------------------------

def test_context_window_uses_a_different_percent_key_than_quota_windows():
    """THE TRAP. Quota windows use `used_percent`; the antigravity
    context window uses `used_percentage`. Feeding a context_window dict
    through _provider_windows' quota-window reader therefore yields
    used_percent=None -- i.e. a permanent "unavailable" -- rather than a
    loud failure. Anyone wiring context-window % into the dashboard must
    map the keys explicitly; this test is the guard rail."""
    context_window = {
        "used_percentage": 62.5,
        "remaining_percentage": 37.5,
        "total_input_tokens": 125_000,
        "total_output_tokens": 8_400,
    }
    assert context_window.get("used_percent") is None
    assert context_window.get("remaining_percent") is None
    windows = _provider_windows({"quota_windows": [context_window]}, AiUsageConfig())
    assert len(windows) == 1
    assert windows[0]["used_percent"] is None, (
        "a context_window read through the quota-window path must NOT silently "
        "produce a percentage -- the key names genuinely differ"
    )
    assert windows[0]["severity"] is None


def test_gap_context_window_is_dropped_entirely(raw, config):
    """CURRENT LIMITATION. The upstream antigravity entry carries a
    `context_window` key (the AI Usage Monitor's own UI renders it as a
    'Context window' bar plus input/output token counts). _normalize
    never reads it, so context-window % and token counts are invisible
    in Terminal MCP. Update this test when that is wired up."""
    assert "context_window" in raw["antigravity"]
    populated = dict(raw["antigravity"])
    populated["context_window"] = {
        "used_percentage": 62.5, "remaining_percentage": 37.5,
        "total_input_tokens": 125_000, "total_output_tokens": 8_400,
    }
    norm = _normalize({"antigravity": populated}, config)
    antigravity = next(p for p in norm["providers"] if p["provider"] == "antigravity")
    assert antigravity["windows"] == [], "context_window is now consumed -- update this test"
    serialized = json.dumps(norm)
    assert "used_percentage" not in serialized
    assert "total_input_tokens" not in serialized


def test_gap_no_token_or_task_accounting_anywhere(raw, config):
    """CURRENT LIMITATION. There is no token/task usage concept in this
    read path at all: the only token numbers upstream live inside
    antigravity's context_window (dropped, above), and codex's
    `reset_credits` counter is dropped too. `sessions[]` correlates a
    session to a provider but carries no per-session consumption."""
    norm = _normalize(raw, config)
    serialized = json.dumps(norm)
    for token_field in ("token", "reset_credits", "other_windows"):
        assert token_field not in serialized


# --- 3. provider health vs usage availability -------------------------

def test_gap_ok_true_does_not_mean_the_provider_is_usable(raw, config):
    """CURRENT LIMITATION / most user-visible one. Upstream `ok` means
    'the probe ran without error', NOT 'this provider is usable':
    gemini reports ok=true with status=NOT_AUTHENTICATED, antigravity
    ok=true with status=NOT_INSTALLED. _normalize drops `status` and
    `usage_status`, and warning/critical stay False when there are no
    windows -- so the dashboard renders a green 'OK' badge for a
    provider that is not installed at all. Update this test when
    usage_status/status is carried through."""
    assert raw["gemini"]["ok"] is True
    assert raw["gemini"]["status"] == "NOT_AUTHENTICATED"
    assert raw["antigravity"]["ok"] is True
    assert raw["antigravity"]["status"] == "NOT_INSTALLED"

    norm = _normalize(raw, config)
    for key in ("gemini", "antigravity"):
        provider = next(p for p in norm["providers"] if p["provider"] == key)
        assert provider["ok"] is True
        assert provider["usage_available"] is False
        # Nothing in the payload lets a renderer distinguish
        # "healthy and idle" from "not installed":
        assert provider["warning"] is False
        assert provider["critical"] is False
        assert "status" not in provider
        assert "usage_status" not in provider


def test_gap_upstream_per_provider_cache_age_is_dropped(raw, config):
    """CURRENT LIMITATION. Upstream stamps every provider with `_cached`
    /`_cache_age`; in this capture gemini and antigravity were ~1897s
    (31 min) old while codex was fresh. _normalize drops both, and the
    service's own `cache_age_seconds`/`stale` describe only ITS OWN
    fetch -- so a caller sees cache_age_seconds=0.0, stale=False over
    half-hour-old provider data with no way to tell."""
    assert raw["gemini"]["_cache_age"] > 60
    norm = _normalize(raw, config)
    assert "_cache_age" not in json.dumps(norm)
    for provider in norm["providers"]:
        assert "cache_age_seconds" not in provider
        assert "stale" not in provider
