"""Unit coverage for composer.py -- the ONE reader of "what is sitting in the
agent's composer right now".

The anchor case is a real pane, captured 2026-09-15 from the live `mcp-work`
Claude Code session while it was reporting the false negative this module
exists to fix. Both previous readers returned the FOOTER for it.
"""
from __future__ import annotations

import pytest

from terminal_mcp import composer
from terminal_mcp.core import _extract_composer_text
from terminal_mcp.submit_flow import extract_composer_text

# The real pane, verbatim. The character between ❯ and the prompt is U+00A0,
# which is the entire bug -- keep it a real NBSP here, never an ASCII space.
REAL_CLAUDE_PANE = [
    "  All three acceptance criteria are now demonstrated on production, not only in",
    "  tests and staging:",
    "",
    "  - AC1 — rotated with no file edited on either side ✔",
    "  - AC2 — no restart on either side; agent PID unchanged ✔",
    "",
    "✻ Brewed for 3m 13s · done 6:39 AM",
    "                                        ✔ Update installed · Restart to update",
    "─" * 78,
    "❯ Đóng backlog item, còn lại tách rollout item riêng",
    "─" * 78,
    "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents",
]

PROMPT = "Đóng backlog item, còn lại tách rollout item riêng"


def test_reads_the_prompt_from_the_real_captured_pane():
    assert composer.extract(REAL_CLAUDE_PANE) == PROMPT


def test_both_historical_readers_now_agree_on_the_real_pane():
    # The two readers disagreeing -- and both being wrong -- is what made the
    # STALLED classifier compare one wrong answer against another.
    assert _extract_composer_text(REAL_CLAUDE_PANE) == PROMPT
    assert extract_composer_text(REAL_CLAUDE_PANE) == PROMPT


def test_the_footer_is_never_mistaken_for_the_composer():
    footer = "⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"
    assert composer.extract(REAL_CLAUDE_PANE) != footer
    assert composer.is_chrome(footer) is True


@pytest.mark.parametrize("separator", [" ", " ", " ", " ", " "])
def test_every_unicode_space_after_the_marker_reads_the_same(separator):
    # Matching on the Zs category rather than a hand-listed set is what stops
    # the next unlisted space character from reopening this bug.
    assert composer.extract([f"❯{separator}hello world"]) == "hello world"


@pytest.mark.parametrize("marker", ["❯", "›", "»", ">"])
def test_every_known_composer_marker_is_recognised(marker):
    # First line uses the NBSP separator a real Claude pane emits, second
    # the ASCII one fixtures use. They look identical; they are not.
    assert composer.extract([f"{marker} some prompt"]) == "some prompt"
    assert composer.extract([f"{marker} some prompt"]) == "some prompt"


def test_a_bare_marker_is_an_empty_composer_not_a_missing_one():
    read = composer.read(["earlier output", "❯"])
    assert read.text == ""
    assert read.found_marker is True
    assert read.is_empty is True


def test_a_pane_with_no_marker_reports_no_marker_found():
    read = composer.read(["just text, no marker"])
    assert read.text == "just text, no marker"
    assert read.found_marker is False


def test_chrome_is_skipped_when_falling_back_without_a_marker():
    # No composer marker anywhere: the fallback must not hand back the box
    # rule or the footer, which is precisely what the old readers did.
    snapshot = ["the real last content line", "─" * 40,
                "  ⏵⏵ auto mode on (shift+tab to cycle)"]
    assert composer.extract(snapshot) == "the real last content line"


def test_holds_survives_rewrapping_and_padding():
    assert composer.holds(["❯ do   the    thing"], "do the thing") is True
    assert composer.holds(["❯ do the thing"], "something else") is False
    # An empty staged text never "holds" -- it would match trivially and
    # silently defeat every caller that asks "is my prompt still pending?".
    assert composer.holds(["❯ anything"], "") is False


def test_empty_and_none_snapshots_never_raise():
    assert composer.extract([]) == ""
    assert composer.extract(None) == ""
    assert composer.read(None).found_marker is False


# -- behaviour the previous readers had, which must not regress ---------------

def test_legacy_ascii_marker_shape_still_works():
    assert _extract_composer_text(["some earlier line", "> hello world"]) == "hello world"


def test_legacy_real_reported_window2_shape_still_works():
    snapshot = ["new task? /clear to save 891k tokens",
                "> Làm Role/Permission step 2 custom role web đi"]
    assert _extract_composer_text(snapshot) == "Làm Role/Permission step 2 custom role web đi"


def test_legacy_no_marker_uses_whole_line():
    assert _extract_composer_text(["just text, no marker"]) == "just text, no marker"


def test_legacy_skips_trailing_blank_lines():
    assert _extract_composer_text(["> real text", "", "  "]) == "real text"


def test_legacy_empty_snapshot_returns_empty():
    assert _extract_composer_text([]) == ""


# -- the downstream consequences the wrong answer caused ---------------------

def test_submission_id_differs_per_prompt_on_real_panes():
    # With the footer as composer_text, every session sitting in the same UI
    # state hashed the SAME string, so unrelated submissions collided.
    from terminal_mcp.submit_flow import submission_id

    def pane_with(prompt: str) -> list[str]:
        return REAL_CLAUDE_PANE[:-3] + ["─" * 78, f"❯ {prompt}", "─" * 78,
                                        "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents"]

    first = submission_id(pane_identity="win1",
                          composer_text=composer.extract(pane_with("do thing A")))
    second = submission_id(pane_identity="win1",
                           composer_text=composer.extract(pane_with("do thing B")))
    assert first != second


def test_plan_submit_sees_the_prompt_not_the_footer():
    from terminal_mcp.adapters import ClaudeAdapter
    from terminal_mcp.submit_flow import SUBMIT_ONCE, plan_submit

    plan = plan_submit(snapshot_a=REAL_CLAUDE_PANE, snapshot_b=REAL_CLAUDE_PANE,
                       adapter=ClaudeAdapter(), pane_identity="mcp-work")
    assert plan.composer_text == PROMPT
    assert plan.stage == SUBMIT_ONCE
    assert plan.send_enter is True
    assert plan.send_activation is True
