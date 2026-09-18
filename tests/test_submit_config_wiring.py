"""Regression: `submit` and `submit_watchdog` are DIFFERENT config objects.

Real bug this pins (found 2026-09-09 while verifying macbook's submit
policy): config.py built a SubmitConfig, then later rebound the SAME
local name `submit_config` to a SubmitWatchdogConfig, so AppConfig got
the watchdog object for BOTH fields. Consequences, all silent:

  - `config.submit.codex/claude/default` did not exist at all;
  - the whole `submit:` block in config.yaml was dead config;
  - every TERMINAL_MCP_CODEX_SUBMIT_* env override was dead;
  - core.py used the watchdog config as a "profile", which has no
    enter_interval_ms/verify_after_each_enter/fixed_enter_count -- so the
    legacy Codex retry path would AttributeError the moment it ran (it
    only survived because submit_watchdog.enabled short-circuits it).
"""
from __future__ import annotations

import textwrap

import pytest

from terminal_mcp.config import SubmitConfig, SubmitWatchdogConfig, load_config
from terminal_mcp.core import _submit_profile_for

BASE = """
permissions: {allow_send_text: true, allow_keys: true}
allowed_session_patterns: ["*"]
"""


def _write(tmp_path, extra: str = ""):
    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(BASE) + textwrap.dedent(extra))
    return path


def test_submit_and_watchdog_are_distinct_objects(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path)))
    config = load_config()
    assert isinstance(config.submit, SubmitConfig)
    assert isinstance(config.submit_watchdog, SubmitWatchdogConfig)
    assert config.submit is not config.submit_watchdog


def test_submit_block_is_actually_applied(tmp_path, monkeypatch):
    """The `submit:` block was silently ignored before this fix."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path, """
        submit:
          codex:
            max_enter_attempts: 5
            enter_interval_ms: 250
          claude:
            max_enter_attempts: 1
    """)))
    config = load_config()
    assert config.submit.codex.max_enter_attempts == 5
    assert config.submit.codex.enter_interval_ms == 250
    assert config.submit.claude.max_enter_attempts == 1


def test_env_override_for_codex_is_applied(tmp_path, monkeypatch):
    """These overrides were dead too -- they were parsed into the object
    that got clobbered."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path)))
    monkeypatch.setenv("TERMINAL_MCP_CODEX_SUBMIT_ENTER_MAX_ATTEMPTS", "4")
    config = load_config()
    assert config.submit.codex.max_enter_attempts == 4


def test_watchdog_block_still_applies(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path, """
        submit_watchdog:
          enabled: false
          max_enter_attempts: 2
          retry_agent_types: [codex]
    """)))
    config = load_config()
    assert config.submit_watchdog.enabled is False
    assert config.submit_watchdog.max_enter_attempts == 2


def test_watchdog_validation_still_fires(tmp_path, monkeypatch):
    """The validation block referenced the clobbered name too -- it must
    still reject a bad watchdog value."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path, """
        submit_watchdog:
          poll_interval_seconds: 9.0
    """)))
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        load_config()


# ------------------------------------------------------- profile selection
def test_profile_selection_defaults_are_single_submit(tmp_path, monkeypatch):
    """A bare config keeps the historical single-Enter behaviour for EVERY
    agent, Codex included -- production opts Codex into retries explicitly
    (see SubmitConfig's own comment). Pinned so a future default change is
    a deliberate act, not a silent one."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path)))
    config = load_config()
    for agent in ("codex", "claude", "shell", "totally-unknown"):
        assert _submit_profile_for(config, agent).max_enter_attempts == 1, agent
    assert _submit_profile_for(config, "shell") is config.submit.default


def test_profile_selection_per_agent_when_configured(tmp_path, monkeypatch):
    """The production shape: Codex opted into retries, Claude explicitly
    single-submit, unknown agents falling back to `default` -- never to
    Codex's retry profile."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path, """
        submit:
          default:
            max_enter_attempts: 1
          codex:
            max_enter_attempts: 3
          claude:
            max_enter_attempts: 1
    """)))
    config = load_config()
    assert _submit_profile_for(config, "codex").max_enter_attempts == 3
    assert _submit_profile_for(config, "claude").max_enter_attempts == 1
    assert _submit_profile_for(config, "shell") is config.submit.default
    assert _submit_profile_for(config, "totally-unknown").max_enter_attempts == 1


def test_profile_has_every_field_the_retry_path_reads(tmp_path, monkeypatch):
    """core.py's legacy Codex retry path reads all four. The watchdog
    config only ever had one of them, which is why this regressed."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path)))
    profile = _submit_profile_for(load_config(), "codex")
    for field in ("max_enter_attempts", "enter_interval_ms",
                  "verify_after_each_enter", "fixed_enter_count"):
        assert hasattr(profile, field), field


def test_watchdog_config_would_NOT_satisfy_the_retry_path(tmp_path, monkeypatch):
    """Pins the actual defect shape: had the two stayed conflated, the
    retry path would raise AttributeError rather than misbehave quietly."""
    monkeypatch.setenv("TERMINAL_MCP_CONFIG", str(_write(tmp_path)))
    watchdog = load_config().submit_watchdog
    for field in ("enter_interval_ms", "verify_after_each_enter", "fixed_enter_count"):
        assert not hasattr(watchdog, field), field
