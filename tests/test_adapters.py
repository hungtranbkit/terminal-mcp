"""Unit coverage for adapters.py's pure evidence rules -- no tmux/pane
needed, every method is a pure function of pane-line snapshots handed to
it. Real disposable-CLI coverage (actual Codex/Claude processes in real
tmux panes) lives in test_send_reliability.py (Codex, via the reproduced
fixture) and test_adapters_real_cli.py (both CLIs, live)."""
from __future__ import annotations

from terminal_mcp.adapters import (DELIVERY_BLOCKED, DELIVERY_ERROR, DELIVERY_STATES, DELIVERY_SUBMIT_CONFIRMED,
                                   DELIVERY_TEXT_SENT, DELIVERY_UNKNOWN, TARGET_RUNNING, TARGET_STATES,
                                   TARGET_UNKNOWN, TARGET_WAITING, ClaudeAdapter, CodexAdapter, GenericShellAdapter,
                                   select_adapter, to_legacy_submit_status)


def test_select_adapter_dispatches_by_command_case_insensitive():
    assert isinstance(select_adapter("codex"), CodexAdapter)
    assert isinstance(select_adapter("Codex"), CodexAdapter)
    assert isinstance(select_adapter("claude"), ClaudeAdapter)
    assert isinstance(select_adapter("bash"), GenericShellAdapter)
    assert isinstance(select_adapter(""), GenericShellAdapter)
    assert isinstance(select_adapter("python3"), GenericShellAdapter)


def test_to_legacy_submit_status_maps_every_delivery_state():
    assert to_legacy_submit_status(DELIVERY_TEXT_SENT) == "TEXT_SENT"
    assert to_legacy_submit_status(DELIVERY_SUBMIT_CONFIRMED) == "SUBMIT_CONFIRMED"
    for state in (DELIVERY_UNKNOWN, DELIVERY_BLOCKED, DELIVERY_ERROR):
        assert to_legacy_submit_status(state) == "SUBMIT_UNCONFIRMED"
    assert set(DELIVERY_STATES) == {DELIVERY_TEXT_SENT, DELIVERY_SUBMIT_CONFIRMED, DELIVERY_UNKNOWN,
                                     DELIVERY_BLOCKED, DELIVERY_ERROR}


def test_generic_shell_adapter_never_recovers_and_uses_bare_diff():
    adapter = GenericShellAdapter()
    before, after_same = ["$ "], ["$ "]
    after_diff = ["$ ", "output line"]
    assert adapter.submit_ack_evidence(before, after_same, "irrelevant") is False
    assert adapter.submit_ack_evidence(before, after_diff, "irrelevant") is True
    assert adapter.stuck_composer_evidence(before, after_diff) is False
    assert adapter.safe_recovery_allowed(after_diff) is False
    assert adapter.identify_target_state(after_diff) == TARGET_UNKNOWN
    assert adapter.can_submit_now(after_diff) is True


def test_codex_adapter_requires_genuine_progress_not_bare_diff():
    adapter = CodexAdapter()
    before = ["> hello [tick 1]"]
    redraw_only = ["> hello [tick 2]"]  # same line count, different content -- in-place redraw
    genuine = ["> hello [tick 2]", "SUBMITTED[1]: hello"]  # new line -- real submission
    assert adapter.submit_ack_evidence(before, redraw_only, "hello") is False
    assert adapter.stuck_composer_evidence(before, redraw_only) is True
    assert adapter.submit_ack_evidence(before, genuine, "hello") is True
    assert adapter.stuck_composer_evidence(before, genuine) is False


def test_codex_adapter_working_evidence_blocks_recovery_and_submit():
    adapter = CodexAdapter()
    working = ["Thinking...", "esc to interrupt"]
    assert adapter.safe_recovery_allowed(working) is False
    assert adapter.can_submit_now(working) is False
    assert adapter.identify_target_state(working) == TARGET_RUNNING


def test_codex_adapter_waiting_state_detected():
    adapter = CodexAdapter()
    waiting = ["Do you want to continue? [y/N]"]
    assert adapter.identify_target_state(waiting) == TARGET_WAITING


def test_claude_adapter_never_claims_stuck_composer_evidence():
    # Never reproduced for Claude Code -- no recovery path is enabled for
    # it regardless of how strongly a redraw-without-growth pattern
    # resembles Codex's known signature (see the P0 report's NOT VERIFIED
    # list). This is a deliberate safety choice, not an oversight.
    adapter = ClaudeAdapter()
    before = ["✻ Metamorphosing… (3s · esc to interrupt)"]
    redraw_only = ["✻ Metamorphosing… (4s · esc to interrupt)"]
    assert adapter.stuck_composer_evidence(before, redraw_only) is False
    assert adapter.safe_recovery_allowed(redraw_only) is False


def test_claude_adapter_requires_genuine_progress_for_confirmation():
    adapter = ClaudeAdapter()
    before = ["> hello"]
    redraw_only = ["✻ Thinking… (1s · esc to interrupt)"]
    assert adapter.submit_ack_evidence(before, redraw_only, "hello") is False
    # Neither snapshot shows working evidence here (a settled/final reply,
    # no "esc to interrupt"/"working"/"thinking" anywhere) -- the not-busy
    # path applies, so genuine progress alone is sufficient, exactly as
    # for Codex; the busy-requires-echo path below is a separate case.
    genuine_idle = ["hello", "some real response text, now settled"]
    assert adapter.submit_ack_evidence(before, genuine_idle, "hello") is True


# ---------------------------------------------------------------------------
# P0 zero-gap hardening: sending while the target is ALREADY busy (an
# earlier, unrelated turn still generating) requires the sent text to be
# demonstrably echoed back -- genuine progress alone is not enough, since
# an ordinary spinner/timer tick from that unrelated turn can itself
# satisfy it regardless of what this specific Enter did. See the P0
# report for the real-CLI evidence this is grounded in (Claude Code
# reliably echoes a just-landed send, either into history or its own
# "queued messages" display) and why this is scoped to the busy case only
# -- the idle-composer case above is completely unaffected.
# ---------------------------------------------------------------------------


def test_claude_adapter_busy_state_requires_sent_text_echo_not_just_progress():
    adapter = ClaudeAdapter()
    # Both ends of the window show working evidence -- an unrelated
    # earlier turn is still generating. A generic new line of unrelated
    # "progress" (e.g. streaming reply tokens) must NOT confirm our send.
    before = ["✻ Thinking… (1s · esc to interrupt)"]
    unrelated_progress = ["✻ Thinking… (2s · esc to interrupt)", "some unrelated streamed reply token"]
    assert adapter.submit_ack_evidence(before, unrelated_progress, "reply with ACK99") is False


def test_claude_adapter_busy_state_confirms_when_sent_text_is_echoed():
    adapter = ClaudeAdapter()
    before = ["✻ Thinking… (1s · esc to interrupt)"]
    # Claude's own "queued messages" display echoing our exact text back,
    # while still busy -- real, observed evidence this attempt's own
    # content specifically landed, not just generic unrelated progress.
    echoed_while_queued = ["✻ Thinking… (2s · esc to interrupt)",
                           "  ❯ Reply with exactly the token ACK99 and nothing else."]
    assert adapter.submit_ack_evidence(before, echoed_while_queued, "Reply with exactly the token ACK99 and nothing else.") is True


def test_claude_adapter_busy_state_echo_tolerates_word_wrap():
    adapter = ClaudeAdapter()
    before = ["✻ Thinking… (1s · esc to interrupt)"]
    # Realistic word-wrapped echo across multiple captured lines.
    wrapped_echo = [
        "✻ Thinking… (2s · esc to interrupt)",
        "  ❯ Reply with exactly the token ACK100 and nothing",
        "    else, no tools, no explanation whatsoever please",
    ]
    long_sent = "Reply with exactly the token ACK100 and nothing else, no tools, no explanation whatsoever please"
    assert adapter.submit_ack_evidence(before, wrapped_echo, long_sent) is True


def test_claude_adapter_busy_only_at_the_after_end_still_requires_echo():
    adapter = ClaudeAdapter()
    # Composer was idle pre-Enter, but by the time `after` was captured
    # the target had already moved on to actively working (e.g. our
    # Enter kicked off *something*, or an unrelated turn started) --
    # still the busy/stricter path, since a coincidental tick risk exists
    # at the `after` end too.
    before = ["> hello"]
    now_busy_no_echo = ["✻ Thinking… (1s · esc to interrupt)", "unrelated content"]
    assert adapter.submit_ack_evidence(before, now_busy_no_echo, "hello") is False


def test_claude_adapter_empty_sent_text_never_blocks_confirmation():
    # Defensive: submit_ack_evidence must never itself crash or spuriously
    # block on an empty/whitespace-only sent_text (shouldn't occur given
    # upstream validation, but the evidence rule stays safe either way).
    adapter = ClaudeAdapter()
    before = ["✻ Thinking… (1s · esc to interrupt)"]
    after = ["✻ Thinking… (2s · esc to interrupt)", "anything"]
    assert adapter.submit_ack_evidence(before, after, "") is True
    assert adapter.submit_ack_evidence(before, after, "   ") is True


def test_all_target_states_are_distinct_and_documented():
    assert len(set(TARGET_STATES)) == len(TARGET_STATES) == 5
