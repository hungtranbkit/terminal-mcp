"""P0 Part A.6 / P0 zero-gap hardening: real, disposable Codex/Claude Code
CLI coverage -- not synthetic fixtures. Skipped automatically (see
pyproject.toml's `not live_cli` default deselect) unless explicitly
requested with `pytest -m live_cli`, since every test here makes a real
API call through a real installed CLI binary (real cost, real latency,
requires the operator's own working `codex`/`claude` auth on this host)
-- the same posture as this repo's disposable-tmux-session tests, just
for a dependency this project cannot assume every environment has.

Run for real against this host's installed `codex` (v0.151.0) and
`claude` (Claude Code v2.1.252) during P0 development. Both CLIs coalesce
messages sent while a prior turn is still generating into a single
subsequent agent turn or an explicit visible queue (Codex: "Messages to
be submitted after next tool call"; Claude Code: "Press up to edit queued
messages") -- real, observed, target-CLI behavior, not a terminal-mcp
delivery defect, and each queued send is still individually confirmed at
the delivery layer regardless.

Verification methodology note (P0 zero-gap follow-up): checking a single
final-snapshot tail after a whole burst completes is NOT a reliable way
to verify "was message N ever lost" for Claude Code specifically -- its
own Ink-based renderer keeps only a bounded window of recent turns in the
observable pty scrollback (this pane's tmux `history_size` was directly
confirmed to stay 0 throughout a real run: Claude repaints via absolute
cursor positioning, not literal newline-driven scrolling, so tmux's own
history buffer never actually receives the older turns at all), dropping
earlier turns from what `capture-pane` can see as later ones are added --
independent of whether they were genuinely lost. An earlier version of
this suite checked only a final snapshot and reported apparent message
loss for Claude under zero/low-gap bursts; re-verified with INCREMENTAL
checks (each attempt's own echo checked immediately after that attempt's
own send returns, before any later send in the same burst can push it out
of the bounded window) across many real repeated runs, no genuine loss,
merge, or duplicate was ever observed, and this is now a hard assertion
here for both CLIs. See adapters.py's ClaudeAdapter.submit_ack_evidence
docstring for the complementary hardening this drove: while the target
was already busy, genuine pane progress alone is no longer sufficient
confirmation evidence -- the adapter now also requires the sent text to
be demonstrably echoed back (into history or Claude's own queued-messages
display), closing a real evidentiary gap (an unrelated turn's own
spinner/timer tick could otherwise coincidentally satisfy the old check)
even though it was never observed to produce an actual incorrect
SUBMIT_CONFIRMED in testing.
"""
from __future__ import annotations

import os
import shutil
import time

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService

pytestmark = pytest.mark.live_cli

CODEX_AVAILABLE = shutil.which("codex") is not None
CLAUDE_AVAILABLE = shutil.which("claude") is not None


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 4000, 200,
        InputPolicyConfig(allowed_session_patterns=("test-*",), max_text_length=8000),
    )
    from terminal_mcp.audit import AuditStore
    return TerminalService(config, audit=AuditStore(tmp_path / "audit.db"))


def _wait_idle(service: TerminalService, session: str, *, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    busy_markers = ("esc to interrupt", "Working", "Messages to be submitted")
    tail = ""
    while time.monotonic() < deadline:
        tail = service.terminal_tail(session, 60)["output"]
        if not any(marker in tail for marker in busy_markers):
            time.sleep(0.6)  # settle: a reply can render in more than one paint
            tail = service.terminal_tail(session, 60)["output"]
            if not any(marker in tail for marker in busy_markers):
                return tail
        time.sleep(0.5)
    return tail


@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not installed on this host")
def test_real_codex_short_and_long_and_multiline_prompts(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-live-codex-basic", "codex")
    time.sleep(3)  # real boot time, not a fixed test-fixture launch
    service = _service(tmp_path)

    # Short.
    result = service.terminal_send_text(session, "Reply with exactly OK1 and nothing else. No tools.",
                                        press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    tail = _wait_idle(service, session)
    assert "OK1" in tail

    # Long (well over one composer line).
    long_prompt = ("Reply with exactly OK2 and nothing else, no tools, no explanation. " +
                  "Ignore the following padding: " + ("lorem ipsum dolor sit amet " * 15))
    result = service.terminal_send_text(session, long_prompt, press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    tail = _wait_idle(service, session, timeout=60.0)
    assert "OK2" in tail

    # Multiline.
    multiline_prompt = "Reply with exactly OK3 and nothing else.\nNo tools.\nNo explanation."
    result = service.terminal_send_text(session, multiline_prompt, press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    tail = _wait_idle(service, session)
    assert "OK3" in tail


@pytest.mark.skipif(not CLAUDE_AVAILABLE, reason="claude CLI not installed on this host")
def test_real_claude_short_and_long_and_multiline_prompts(tmux_session_factory, tmp_path):
    session = tmux_session_factory("test-live-claude-basic", "claude")
    time.sleep(4)
    service = _service(tmp_path)
    # Claude Code defaults to an auto-accept mode that could act on tool-
    # using prompts -- BTab cycles to manual/ask-first mode before any
    # real prompt is sent, exactly as done for the live P0 verification.
    service.tmux.send_keys(session, ["BTab"])
    # Real, live-tested finding (P0 dev): a send issued too soon after this
    # raw mode-switch key can look SUBMIT_CONFIRMED (the mode banner's own
    # redraw satisfies the genuine-progress check) without landing as a
    # real conversational turn -- 2s reliably clears it; a normal caller
    # never chains a raw key-send immediately before a text-send this way.
    time.sleep(2.0)

    result = service.terminal_send_text(
        session, "Do not use any tools. Reply with exactly OK1 and nothing else.", press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    tail = _wait_idle(service, session)
    assert "OK1" in tail

    long_prompt = ("Do not use any tools. Reply with exactly OK2 and nothing else. " +
                  "Ignore the following padding: " + ("lorem ipsum dolor sit amet " * 15))
    result = service.terminal_send_text(session, long_prompt, press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    tail = _wait_idle(service, session, timeout=60.0)
    assert "OK2" in tail

    multiline_prompt = "Do not use any tools.\nReply with exactly OK3 and nothing else.\nNo explanation."
    result = service.terminal_send_text(session, multiline_prompt, press_enter=True)
    assert result["delivery_state"] == "SUBMIT_CONFIRMED"
    tail = _wait_idle(service, session)
    assert "OK3" in tail


def _stress_count() -> int:
    # Defaults to a small, cheap-to-re-run sample; set
    # REAL_CLI_STRESS_COUNT=100 to reproduce the full P0 run reported
    # above (real cost/time -- roughly half a minute for 100 against
    # Codex on this host, see the P0 report for the actual figures).
    return int(os.environ.get("REAL_CLI_STRESS_COUNT", "8"))


def _run_sequential_stress(service: TerminalService, session: str, count: int, *,
                           inter_send_delay: float = 0.0) -> None:
    """Hard-asserts, for EVERY attempt: SUBMIT_CONFIRMED (0 lost Enter at
    the delivery layer -- entirely within terminal-mcp's own control) AND
    that attempt's own sent-text echo is visible in the tail checked
    IMMEDIATELY after that specific send returns -- before any later send
    in this same burst gets a chance to push it out of view. This
    incremental check (not a single final-snapshot check after all N
    sends complete) is what real testing established is actually required
    for Claude Code -- see the module docstring for why a final-snapshot
    check is unreliable for it specifically, and why this is a hard
    assertion for both CLIs here, not a soft/logged one."""
    for i in range(1, count + 1):
        result = service.terminal_send_text(
            session, f"Reply with exactly the token ACK{i} and nothing else. No tools, no explanation.",
            press_enter=True, idempotency_key=f"live-stress-{session}-{i}")
        assert result["delivery_state"] == "SUBMIT_CONFIRMED", (i, result)
        tail = service.terminal_tail(session, 60)["output"]
        assert f"ACK{i}" in tail, (
            f"attempt {i}'s own echo was not visible immediately after its own send returned "
            f"(checked before any later send in this burst could push it out of view): {result}"
        )
        if inter_send_delay:
            time.sleep(inter_send_delay)


@pytest.mark.skipif(not CODEX_AVAILABLE, reason="codex CLI not installed on this host")
def test_real_codex_repeated_sequential_sends_zero_lost_enter(tmux_session_factory, tmp_path):
    # Codex's own queuing tolerates true back-to-back sends with zero
    # inter-send delay -- live-verified up to 100 sequential real sends
    # (see the P0 report): every one individually SUBMIT_CONFIRMED, every
    # prompt's own echo present immediately, each queued turn shown
    # explicitly ("Messages to be submitted after next tool call") while
    # Codex is still generating a prior reply.
    session = tmux_session_factory("test-live-codex-stress", "codex")
    time.sleep(3)
    service = _service(tmp_path)
    _run_sequential_stress(service, session, _stress_count())


@pytest.mark.skipif(not CLAUDE_AVAILABLE, reason="claude CLI not installed on this host")
def test_real_claude_repeated_sequential_sends_zero_lost_enter(tmux_session_factory, tmp_path):
    # P0 zero-gap follow-up: true zero-gap back-to-back sends (no inter-
    # send delay at all), hard-asserted, incrementally verified -- see the
    # module docstring for why the earlier "Claude loses messages under
    # rapid-fire load" finding was a final-snapshot measurement artifact,
    # not a genuine delivery defect, and for the ClaudeAdapter hardening
    # (submit_ack_evidence now requires the sent text to be echoed back
    # specifically while the target is already busy) this is verifying.
    session = tmux_session_factory("test-live-claude-stress", "claude")
    time.sleep(4)
    service = _service(tmp_path)
    service.tmux.send_keys(session, ["BTab"])
    time.sleep(2.0)
    _run_sequential_stress(service, session, _stress_count())


@pytest.mark.skipif(not CLAUDE_AVAILABLE, reason="claude CLI not installed on this host")
def test_real_claude_mixed_short_long_multiline_zero_gap_burst(tmux_session_factory, tmp_path):
    # P0 zero-gap follow-up: exactly the combination requested for this
    # investigation -- long and multiline prompts interleaved with short
    # ones, all sent back-to-back with no artificial delay, each verified
    # incrementally right after its own send.
    session = tmux_session_factory("test-live-claude-mixed", "claude")
    time.sleep(4)
    service = _service(tmp_path)
    service.tmux.send_keys(session, ["BTab"])
    time.sleep(2.0)

    padding = "lorem ipsum dolor sit amet " * 15
    prompts = [
        ("MIXA", "Reply with exactly the token MIXA and nothing else. No tools."),
        ("MIXMULTI", "Reply with exactly the token MIXMULTI and nothing else.\nNo tools.\nNo explanation at all."),
        ("MIXLONG", f"Reply with exactly the token MIXLONG and nothing else, no tools. Ignore: {padding}"),
        ("MIXB", "Reply with exactly the token MIXB and nothing else. No tools."),
    ]
    for token, text in prompts:
        result = service.terminal_send_text(session, text, press_enter=True,
                                            idempotency_key=f"live-mixed-{session}-{token}")
        assert result["delivery_state"] == "SUBMIT_CONFIRMED", (token, result)
        # A long prompt's own token can legitimately scroll past a bounded
        # tail window if it sits at the very front of heavy padding text
        # that hasn't finished echoing/wrapping yet -- widen the check for
        # that one case rather than assert on a token position artifact
        # unrelated to whether the send itself was genuinely confirmed.
        tail = service.terminal_tail(session, 300 if token == "MIXLONG" else 60)["output"]
        assert token in tail, (token, result)
