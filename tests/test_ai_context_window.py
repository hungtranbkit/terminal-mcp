"""A percentage needs a denominator somebody verified.

The bug this covers: the page offered one percentage -- subscription quota --
which no local artefact reports, so it was permanently N/A, while the one
percentage that IS computable from provider-reported numbers (how full the
context window is) did not exist at all.

The trap on the way to fixing it: every transcript on this machine reports
`message.model` as `claude-opus-5` while the cost block reports
`claude-opus-5[1m]`. Sessions were measured holding 795k tokens. Against an
assumed 200k window that is 397% -- so a default window would not have been
slightly wrong, it would have been nonsense stated confidently.
"""
from __future__ import annotations

import pytest

from terminal_mcp import ai_context_window as cw


# -- reading a window the provider states outright ---------------------------------

@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5[1m]", 1_000_000),
    ("claude-opus-5[200k]", 200_000),
    ("some-model[128K]", 128_000),
    ("claude-opus-5", None),
    ("", None),
    (None, None),
])
def test_a_variant_suffix_states_its_own_window(model, expected):
    assert cw.parse_variant_window(model) == expected


def test_the_cost_blocks_variant_resolves_a_window_the_transcript_dropped():
    """`message.model` loses the suffix; the per-model cost breakdown keeps
    it. That is the structured source, not an inference."""
    window, source, _ = cw.resolve_window(
        "claude-opus-5", variant_ids=["claude-opus-5[1m]"])

    assert window == 1_000_000
    assert source == cw.VARIANT_SUFFIX


def test_a_different_model_in_the_same_session_does_not_lend_its_window():
    """A haiku subagent inside an opus session says nothing about the opus
    window, and borrowing it would size the bar from the wrong model."""
    window, source, _ = cw.resolve_window(
        "claude-opus-5", variant_ids=["claude-haiku-4-5-20251001[1m]"])

    assert source == cw.MODEL_TABLE
    assert window == cw.CONTEXT_WINDOWS["claude-opus-5"]


def test_an_unknown_model_is_unavailable_rather_than_defaulted():
    window, source, detail = cw.resolve_window("some-model-nobody-tabulated")

    assert window is None
    assert source == cw.UNKNOWN_MODEL
    assert "rather than assumed" in detail


# -- the percentage ------------------------------------------------------------------

def test_a_known_window_yields_a_real_percentage():
    usage = cw.context_usage(500_000, model="claude-opus-5",
                             variant_ids=["claude-opus-5[1m]"])

    assert usage["window"] == 1_000_000
    assert usage["used_percent"] == 50.0
    assert usage["source"] == cw.VARIANT_SUFFIX


def test_an_unknown_window_reports_the_tokens_but_no_percentage():
    """The count is measured and worth showing; only the ratio is unknown."""
    usage = cw.context_usage(123_456, model="mystery-model")

    assert usage["used"] == 123_456
    assert usage["used_percent"] is None
    assert usage["window"] is None


def test_a_measurement_larger_than_the_window_refuses_to_report_a_percentage():
    """The exact case seen on this machine: 795k measured against a resolved
    200k window. 397% would dress a resolution failure up as a reading."""
    usage = cw.context_usage(795_496, model="claude-opus-5")

    assert usage["used_percent"] is None
    assert usage["source"] == cw.CONTRADICTED
    assert "exceeds" in usage["detail"]
    assert usage["used"] == 795_496


def test_a_percentage_never_exceeds_one_hundred():
    for used in (1, 199_999, 200_000, 200_001, 10_000_000):
        usage = cw.context_usage(used, model="claude-opus-5")
        assert usage["used_percent"] is None or usage["used_percent"] <= 100.0


def test_exactly_full_is_one_hundred_percent_not_a_contradiction():
    usage = cw.context_usage(200_000, model="claude-opus-5")
    assert usage["used_percent"] == 100.0
    assert usage["source"] == cw.MODEL_TABLE


def test_a_session_with_no_activity_is_not_zero_percent_full():
    """Nothing recorded is not the same as an empty context, and showing 0%
    would claim a measurement that was never taken."""
    usage = cw.context_usage(0, model="claude-opus-5")

    assert usage["used_percent"] is None
    assert usage["source"] == cw.NO_ACTIVITY


def test_none_usage_is_handled_like_no_activity():
    assert cw.context_usage(None, model="claude-opus-5")["source"] == cw.NO_ACTIVITY


# -- the three percentages stay separate ----------------------------------------------

def test_context_usage_never_claims_to_be_a_quota_reading():
    """Subscription quota and context fullness answer different questions.
    Nothing here may present itself as the former."""
    usage = cw.context_usage(100_000, model="claude-opus-5")
    assert "quota" not in str(usage).lower()
    assert set(usage) == {"used", "window", "used_percent", "source", "detail"}
