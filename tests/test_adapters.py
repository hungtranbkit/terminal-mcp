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
    assert adapter.submit_ack_evidence(before, after_same) is False
    assert adapter.submit_ack_evidence(before, after_diff) is True
    assert adapter.stuck_composer_evidence(before, after_diff) is False
    assert adapter.safe_recovery_allowed(after_diff) is False
    assert adapter.identify_target_state(after_diff) == TARGET_UNKNOWN
    assert adapter.can_submit_now(after_diff) is True


def test_codex_adapter_requires_genuine_progress_not_bare_diff():
    adapter = CodexAdapter()
    before = ["> hello [tick 1]"]
    redraw_only = ["> hello [tick 2]"]  # same line count, different content -- in-place redraw
    genuine = ["> hello [tick 2]", "SUBMITTED[1]: hello"]  # new line -- real submission
    assert adapter.submit_ack_evidence(before, redraw_only) is False
    assert adapter.stuck_composer_evidence(before, redraw_only) is True
    assert adapter.submit_ack_evidence(before, genuine) is True
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
    genuine = ["✻ Thinking… (1s · esc to interrupt)", "some real response text"]
    assert adapter.submit_ack_evidence(before, redraw_only) is False
    assert adapter.submit_ack_evidence(before, genuine) is True


def test_all_target_states_are_distinct_and_documented():
    assert len(set(TARGET_STATES)) == len(TARGET_STATES) == 5
