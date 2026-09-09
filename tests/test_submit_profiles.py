from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SubmitConfig, SubmitProfile
from terminal_mcp.core import TerminalService


FIXTURE = Path(__file__).parent / "fixtures" / "codex_composer.py"


def _service(tmp_path: Path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=2000),
        submit=SubmitConfig(codex=SubmitProfile(max_enter_attempts=3, enter_interval_ms=120)),
    )
    return TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))


@pytest.mark.parametrize("required", [1, 2, 3])
def test_codex_profile_stops_at_first_verified_enter(tmux_session_factory, tmp_path, required):
    name = f"test-codex-profile-{required}"
    command = f"bash -lc 'CODEX_FIXTURE_MODE=submit_after_n_enters CODEX_REQUIRED_ENTERS={required} exec -a codex python3 -u {FIXTURE}'"
    tmux_session_factory(name, command)
    time.sleep(0.3)
    result = _service(tmp_path).terminal_send_text(name, "profile-prompt", press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    assert result["enter_count"] == required
    assert result["attempts"] == required
    assert result["submit_latency_ms"] >= 0


def test_codex_profile_stops_at_max_without_blind_fourth_enter(tmux_session_factory, tmp_path):
    name = "test-codex-profile-max"
    command = f"bash -lc 'CODEX_FIXTURE_MODE=submit_after_n_enters CODEX_REQUIRED_ENTERS=4 exec -a codex python3 -u {FIXTURE}'"
    tmux_session_factory(name, command)
    time.sleep(0.3)
    result = _service(tmp_path).terminal_send_text(name, "profile-prompt", press_enter=True)
    assert result["delivery_state"] == "DELIVERY_UNKNOWN"
    assert result["enter_count"] == 3
    assert result["attempts"] == 3
    assert "SUBMITTED[" not in _service(tmp_path).terminal_tail(name, 20)["output"]
