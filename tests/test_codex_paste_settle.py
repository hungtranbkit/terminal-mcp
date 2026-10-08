"""Regression: a collapsed Unicode draft must settle, then receive a separate Enter."""
import subprocess

import time

import pytest

from terminal_mcp.adapters import select_adapter
from terminal_mcp.audit import AuditStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.models import SessionIdentity, SessionInfo
from terminal_mcp.submit_watchdog import VerifiedSubmitWatchdog, WatchdogConfig
from terminal_mcp.tmux import PASTE_BUFFER_THRESHOLD, TmuxClient, TmuxError


@pytest.mark.parametrize("prompt", ["x" * 1024, "越" * 400, "越" * 5000],
                         ids=["exact-1kib", "unicode-1200-bytes", "unicode-15000-bytes"])
def test_large_paste_load_and_delete_share_selected_socket(monkeypatch, prompt):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(subprocess, 'run', run)
    TmuxClient(socket_name='test-isolated-paste').send_text('test-pane', prompt, False)
    assert len(calls) == 3
    assert all(argv[:3] == ["tmux", "-L", "test-isolated-paste"] for argv in calls)


PROMPT = "Only echo this inert test.\n" + "Bình Phước 越南 🏡\n" * 80 + "Unique end: FINISH_7081"


class ComposerTransport:
    def __init__(self, frames, *, fail_enter=False, accept_after=1):
        self.frames = iter(frames)
        self.last = []
        self.fail_enter = fail_enter
        self.accept_after = accept_after
        self.injections = []
        self.enters = []
        self.injected_at = 0.0
        self.info = SessionInfo(name="test-paste", attached=False, windows=1,
                               created_epoch=1, activity_epoch=1, pane_pid=123,
                               pane_current_command="codex", pane_dead=False,
                               session_id="$1", pane_id="%1", pane_in_mode=False,
                               pane_current_path="/tmp")

    def get_session(self, session):
        return self.info

    def send_text(self, session, prompt, press_enter):
        assert press_enter is False
        self.injections.append(prompt)
        self.injected_at = time.monotonic()

    def capture_lines(self, session, lines):
        if len(self.enters) >= self.accept_after and not self.fail_enter:
            return ["Working (esc to interrupt)"]
        self.last = next(self.frames, self.last)
        return self.last

    def send_keys(self, session, keys):
        if keys == ["Enter"]:
            self.enters.append(time.monotonic())
            if self.fail_enter:
                raise TmuxError("simulated Enter transport failure")


def submit_service(tmp_path, backend):
    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 200, 100,
                       InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=8000))
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))
    service.tmux = backend
    service.submit_watchdog = VerifiedSubmitWatchdog(service.submissions, WatchdogConfig(
        poll_interval_seconds=.01, timeout_seconds=.5, max_enter_attempts=2))
    return service


def submit(service, backend, key="one-prompt"):
    return service._verified_codex_submit_locked(
        "test-paste", PROMPT, key, SessionIdentity.from_session_info(backend.info),
        "codex", select_adapter("codex"))


def test_verified_codex_waits_for_settle_before_separate_enter(tmp_path):
    backend = ComposerTransport([[f"› [Pasted Content {len(PROMPT)} chars]"]])
    result = submit(submit_service(tmp_path, backend), backend)
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert len(backend.enters) == 1
    assert backend.enters[0] - backend.injected_at >= .075
    assert backend.injections == [PROMPT]


@pytest.mark.parametrize("draft", [
    ["› " + PROMPT[:100]],
    [f"historical [Pasted Content {len(PROMPT)} chars]", "› unrelated draft"],
])
def test_partial_or_historical_text_never_activates(tmp_path, draft):
    backend = ComposerTransport([draft])
    result = submit(submit_service(tmp_path, backend), backend)
    assert result["delivery_state"] == "TEXT_SENT"
    assert backend.enters == []
    assert backend.injections == [PROMPT]


def test_delayed_collapsed_unicode_draft_and_swallowed_enter_recover_once(tmp_path):
    backend = ComposerTransport([[], [f"› [Pasted Content {len(PROMPT)} chars]"]], accept_after=2)
    service = submit_service(tmp_path, backend)
    result = submit(service, backend)
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert len(backend.enters) == 2
    assert backend.injections == [PROMPT]
    replay = submit(service, backend)
    assert replay["submission_id"] == result["submission_id"]
    assert backend.injections == [PROMPT]
    assert len(backend.enters) == 2


def test_failed_enter_is_unconfirmed_and_replay_does_not_retype(tmp_path):
    backend = ComposerTransport([[f"› [Pasted Content {len(PROMPT)} chars]"]], fail_enter=True)
    service = submit_service(tmp_path, backend)
    result = submit(service, backend)
    assert result["submit_status"] != "SUBMIT_CONFIRMED"
    assert result["enter_sent"] is False
    submit(service, backend)
    assert backend.injections == [PROMPT]
    assert len(backend.enters) == 1


def test_real_tmux_preserves_long_unicode_multiline_paste_and_replay(
    tmux_session_factory, tmp_path,
):
    import hashlib
    import shlex
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "codex_bracketed_paste.py"
    command = f"exec -a codex python3 -u {shlex.quote(str(fixture))}"
    session = tmux_session_factory("test-codex-paste-bytes", f"bash -c {shlex.quote(command)}")
    service = submit_service(tmp_path, TmuxClient())
    deadline = time.monotonic() + 3
    while "codex fixture" not in "\n".join(service.tmux.capture_lines(session, 30)):
        assert time.monotonic() < deadline, "disposable fixture failed to initialize"
        time.sleep(.02)
    prompt = PROMPT * 3
    assert len(prompt.encode("utf-8")) > PASTE_BUFFER_THRESHOLD
    result = service.terminal_send_text(session, prompt, press_enter=True, idempotency_key="unicode-once")
    assert result["submit_status"] == "SUBMIT_CONFIRMED", result
    replay = service.terminal_send_text(session, prompt, press_enter=True, idempotency_key="unicode-once")
    assert replay["submission_id"] == result["submission_id"]
    pane = "\n".join(service.tmux.capture_lines(session, 30))
    assert "SUBMITTED[1]" in pane
    assert hashlib.sha256(prompt.encode("utf-8")).hexdigest() in pane
    assert "SUBMITTED[2]" not in pane


def test_initial_approval_with_complete_draft_never_receives_enter(tmp_path):
    backend = ComposerTransport([[f"› [Pasted Content {len(PROMPT)} chars]", "approve or deny? [y/n]"]])
    result = submit(submit_service(tmp_path, backend), backend)
    assert result["submit_status"] != "SUBMIT_CONFIRMED"
    assert backend.enters == []


@pytest.mark.parametrize("settle_ms,expected", [(-10, .001), (1, .001), (350, .350), (99999, 2.0)])
def test_verified_codex_configured_settle_is_bounded(tmp_path, monkeypatch, settle_ms, expected):
    from dataclasses import replace
    from terminal_mcp import core

    backend = ComposerTransport([[f"› [Pasted Content {len(PROMPT)} chars]"]])
    service = submit_service(tmp_path, backend)
    service.config = replace(service.config, submit=replace(
        service.config.submit, codex=replace(service.config.submit.codex, settle_ms=settle_ms)))
    sleeps = []
    monkeypatch.setattr(core.time, "sleep", sleeps.append)
    result = submit(service, backend)
    assert sleeps[0] == expected
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    assert len(backend.enters) == 1


def test_explicit_menu_answer_never_uses_fixed_enter_fallback(tmp_path, monkeypatch):
    from dataclasses import replace

    backend = ComposerTransport([["approve or deny? [y/n]"]], accept_after=99)
    service = submit_service(tmp_path, backend)
    service.config = replace(service.config, submit=replace(
        service.config.submit, codex=replace(service.config.submit.codex,
            fixed_enter_count=3, verify_after_each_enter=False, enter_interval_ms=1)))
    monkeypatch.setattr(service, "_poll_for_submission",
                        lambda *a, **kw: (False, ["approve or deny? [y/n]"], "unchanged"))
    result = service.terminal_send_text("test-paste", "2", press_enter=True,
                                        prompt_response=True, idempotency_key="answer-once")
    assert result["enter_sent"] is True, result
    assert result["submit_status"] != "SUBMIT_CONFIRMED"
    assert len(backend.enters) == 1
    assert backend.injections == ["2"]
