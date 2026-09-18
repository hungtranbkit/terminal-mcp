"""Activate-only sends must not confirm a submission that never happened.

Measured live on hp-linux (node p1repro-hp, 2026-09-15) against the deployed
build BEFORE this change:

    composer empty, agent idle
    terminal_send_text("", press_enter=True)
      -> {"delivery_state": "SUBMIT_CONFIRMED", "submit_reason": null}
    pane afterwards: byte-identical, composer still empty

and, with a turn already running, three consecutive activate-only calls each
returned SUBMIT_CONFIRMED while the pane only ever showed "Press up to edit
queued messages" -- no new turn began for any of them.

The mechanism: `_sent_text_echoed` treats an empty `sent_text` as trivially
satisfied, by design, for a caller with genuinely nothing to attribute. An
activate-only send has something to attribute -- the composer's own content --
and passing "" instead removed the busy-window guard entirely, leaving a
spinner tick sufficient to confirm.
"""
from __future__ import annotations

import pytest

from terminal_mcp import submit_flow as sf
from terminal_mcp.adapters import (
    DELIVERY_SUBMIT_CONFIRMED, DELIVERY_UNKNOWN, _sent_text_echoed, select_adapter,
)

CLAUDE = select_adapter("claude")
CODEX = select_adapter("codex")

PROMPT = "REPRO2: empty-text plus enter path"
EMPTY_COMPOSER = [
    "  ▝▝ ▝▝    ~/workspace",
    "────────────────────────────────",
    "❯",
    "────────────────────────────────",
    "  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt",
]
FULL_COMPOSER = [
    "  ▝▝ ▝▝    ~/workspace",
    "────────────────────────────────",
    f"❯ {PROMPT}",
    "────────────────────────────────",
    "  ⏵⏵ auto mode on (shift+tab to cycle)",
]


# -- the root cause, stated as an assertion --------------------------------

def test_an_empty_sent_text_is_trivially_echoed():
    """This is by design and is NOT the bug. The bug is passing "" for a send
    that had something to attribute."""
    assert _sent_text_echoed(EMPTY_COMPOSER, "") is True
    assert _sent_text_echoed(EMPTY_COMPOSER, PROMPT) is False


def test_an_activate_only_send_attributes_the_composer_not_the_empty_text():
    """What the fix substitutes: the composer's own content."""
    assert sf.extract_composer_text(FULL_COMPOSER) == PROMPT
    assert sf.extract_composer_text(EMPTY_COMPOSER) == ""


# -- the false positive ----------------------------------------------------

def test_an_empty_composer_offers_nothing_to_submit():
    """The live case: activate-only into an empty composer. There is no prompt,
    so no evidence could honestly confirm one."""
    assert sf.extract_composer_text(EMPTY_COMPOSER) == ""


def test_a_spinner_tick_cannot_confirm_an_activate_only_send():
    """With the composer's text required as the echo, a busy pane that never
    shows that text cannot confirm -- which is what three consecutive
    activate-only calls exploited."""
    busy_after = EMPTY_COMPOSER[:-1] + ["✻ Puttering… (4s · esc to interrupt)"]
    # Attributed to the composer (empty -> the guard refuses before this point).
    # Attributed to a real prompt that never appears: not confirmed.
    assert CLAUDE.submit_ack_evidence(FULL_COMPOSER, busy_after, PROMPT) is False


def test_the_same_pane_with_a_real_echo_does_confirm():
    """The fix must not turn a genuine submit into a false negative."""
    accepted_after = [
        "  ▝▝ ▝▝    ~/workspace",
        f"  {PROMPT}",
        "────────────────────────────────",
        "❯",
        "✻ Puttering… (2s · thinking)",
    ]
    assert CLAUDE.submit_ack_evidence(FULL_COMPOSER, accepted_after, PROMPT) is True


# -- states stay distinct --------------------------------------------------

def test_text_delivered_is_not_submitted():
    from terminal_mcp.adapters import DELIVERY_TEXT_SENT
    assert DELIVERY_TEXT_SENT != DELIVERY_SUBMIT_CONFIRMED
    assert DELIVERY_UNKNOWN != DELIVERY_SUBMIT_CONFIRMED
    assert sf.STALLED != DELIVERY_UNKNOWN


def test_nothing_to_submit_is_a_named_outcome():
    assert sf.NOTHING_TO_SUBMIT == "NOTHING_TO_SUBMIT"


# -- Codex semantics stay scoped -------------------------------------------

def test_only_claude_gets_the_activation_nudge():
    assert sf.ACTIVATION_ADAPTERS == frozenset({"claude"})
    assert CODEX.name not in sf.ACTIVATION_ADAPTERS


def test_multi_enter_is_never_applied_to_claude():
    """Codex's retry profile is gated on adapter.name == 'codex' in core; this
    pins the intent so a later edit cannot widen it silently."""
    import inspect
    from terminal_mcp import core
    source = inspect.getsource(core.TerminalService._send_text_and_verify_locked)
    assert 'adapter.name == "codex"' in source, \
        "the multi-enter retry must stay gated on Codex by name"


# -- the patched decision, exercised directly ------------------------------

def _expected_echo(text: str, typed_snapshot: list[str]) -> str | None:
    """Mirrors the branch added to _send_text_and_verify_locked."""
    if text:
        return text
    composer = sf.extract_composer_text(typed_snapshot)
    return composer or None


def test_activate_only_with_a_full_composer_attributes_it():
    assert _expected_echo("", FULL_COMPOSER) == PROMPT


def test_activate_only_with_an_empty_composer_has_no_echo_to_require():
    assert _expected_echo("", EMPTY_COMPOSER) is None


def test_a_normal_send_still_attributes_its_own_text():
    assert _expected_echo("hello world", FULL_COMPOSER) == "hello world"


# -- item 5: the fields a caller needs to debug without prompt content -----

def test_nothing_to_submit_reports_zero_activation_attempts():
    """Enter was withheld, so the attempt count must say so."""
    import inspect
    from terminal_mcp import core
    src = inspect.getsource(core.TerminalService._send_text_and_verify_locked)
    gate = src.split("VERIFY_TEXT, and it belongs HERE")[1].split("self.tmux.send_keys")[0]
    assert '"activation_attempts"] = 0' in gate
    assert '"acceptance_evidence"] = []' in gate
    assert '"stage"] = "VERIFY_TEXT"' in gate


def test_the_gate_runs_before_the_enter_is_sent():
    """VERIFY_TEXT before ACTIVATE, not after -- the ordering IS the fix."""
    import inspect
    from terminal_mcp import core
    src = inspect.getsource(core.TerminalService._send_text_and_verify_locked)
    gate_at = src.index("VERIFY_TEXT, and it belongs HERE")
    enter_at = src.index('self.tmux.send_keys(session, ["Enter"])')
    assert gate_at < enter_at, "the empty-composer gate must precede the Enter"


# -- item 6: remote failure classes stay apart ----------------------------

@pytest.mark.parametrize("detail,expected,retryable", [
    (TimeoutError("timed out"),            sf.REMOTE_TIMEOUT,           True),
    ({"error": "SESSION_NOT_FOUND"},       sf.REMOTE_SESSION_GONE,      False),
    ({"error": "INPUT_NOT_PERMITTED"},     sf.REMOTE_INPUT_DENIED,      False),
    ({"error": "SEND_KEYS_DISABLED"},      sf.REMOTE_INPUT_DENIED,      False),
    ("URLError: Connection refused",       sf.REMOTE_NODE_UNREACHABLE,  True),
    ("HTTP 502: bad gateway",              sf.REMOTE_TRANSPORT_FAILED,  True),
])
def test_remote_failures_are_classified_apart(detail, expected, retryable):
    verdict = sf.classify_remote_failure(detail, node_id="hp-linux", session="s1")
    assert verdict["remote_state"] == expected
    assert verdict["retryable"] is retryable


def test_an_unrecognised_remote_failure_is_not_marked_retryable():
    """Not knowing what happened is not evidence that retrying is safe."""
    verdict = sf.classify_remote_failure("")
    assert verdict["remote_state"] == sf.REMOTE_UNKNOWN
    assert verdict["retryable"] is False


def test_a_denied_send_is_never_retryable():
    for detail in ({"error": "ACCESS_DENIED"}, {"error": "READ_RESTRICTED"}):
        assert sf.classify_remote_failure(detail)["retryable"] is False


def test_the_verdict_carries_node_and_session():
    v = sf.classify_remote_failure({"error": "SESSION_NOT_FOUND"},
                                   node_id="hp-linux", session="p1repro-hp")
    assert (v["node_id"], v["session"]) == ("hp-linux", "p1repro-hp")


def test_classification_never_raises_on_any_shape():
    for junk in (None, 0, [], {}, object(), ValueError("x")):
        assert "remote_state" in sf.classify_remote_failure(junk)


def test_no_prompt_content_can_reach_the_classifier_output():
    """It reads error/reason/detail only -- never a prompt field."""
    v = sf.classify_remote_failure({"error": "SESSION_NOT_FOUND",
                                    "prompt": "SECRET PROMPT TEXT"})
    import json
    assert "SECRET" not in json.dumps(v)


def test_core_repairs_confirmed_receipt_when_enter_was_not_sent():
    from terminal_mcp.core import TerminalService
    service = object.__new__(TerminalService)
    receipt = {
        "sent": True,
        "press_enter": True,
        "enter_sent": False,
        "delivery_state": DELIVERY_SUBMIT_CONFIRMED,
        "submit_status": "SUBMIT_CONFIRMED",
        "correlation_id": "corr-invariant",
    }
    repaired = TerminalService._enrich_receipt(service, receipt)
    assert repaired["delivery_state"] == DELIVERY_UNKNOWN
    assert repaired["submit_status"] != "SUBMIT_CONFIRMED"
    assert repaired["receipt_invariant_repaired"] is True
    assert repaired["evidence"] == []


def test_codex_ack_paths_require_an_enter_before_running_or_adapter_ack():
    import inspect
    from terminal_mcp import core
    src = inspect.getsource(core.TerminalService._verified_codex_submit_locked)
    assert 'if has_submitted_enter and adapter.identify_target_state(lines) == "running"' in src
    assert 'if (has_submitted_enter and baseline is not None' in src
    assert 'and enter_count > 0' in src
