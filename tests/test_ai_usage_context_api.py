"""The context percentage has to survive the trip from transcript to payload.

Unit-testing the resolver proves the arithmetic. These prove the wiring: that
the API reads the LATEST request rather than the session total, that it finds
the variant suffix in the cost block where the transcript dropped it, and that
an unresolvable window reaches the page as a null rather than as a number.
"""
from __future__ import annotations

import json
import time

import pytest

from terminal_mcp import ai_context_window as cw
from terminal_mcp.ai_usage_index import AiUsageIndex

NOW = time.time()


def _turn(uuid, *, when, session="s1", model="claude-opus-5",
          i=10, o=20, cr=30, cwr=40, project="/repo"):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + ".000Z"
    return {"type": "assistant", "uuid": uuid, "timestamp": stamp, "sessionId": session,
            "cwd": project, "gitBranch": "main", "version": "2.1.266",
            "message": {"role": "assistant", "model": model,
                        "usage": {"input_tokens": i, "output_tokens": o,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cwr}}}


def _cost(session, model_key, *, when=None):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when or NOW)) + ".000Z"
    return {"type": "cost-state", "sessionId": session, "totalCostUSD": 1.0,
            "timestamp": stamp, "modelUsage": {model_key: {"costUSD": 1.0}}}


def _write(home, entries, name="s1", project="-repo"):
    folder = home / "projects" / project
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{name}.jsonl").open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return home


@pytest.fixture
def index(tmp_path):
    return AiUsageIndex(tmp_path / "usage.db")


def _ingest(index, tmp_path, entries):
    home = _write(tmp_path / "claude", entries)
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")


def _session(report, session_id="s1"):
    return next(s for s in report["sessions"] if s["agent_session_id"] == session_id)


# -- the latest request, not the running total ---------------------------------------

def test_context_is_the_latest_request_not_the_session_sum(index, tmp_path):
    """Summing a session answers "what did this cost", not "how full is the
    context" -- and it would pass any window within a few turns."""
    _ingest(index, tmp_path, [
        _turn("a", when=NOW - 300, i=1, cr=100_000, cwr=0),
        _turn("b", when=NOW - 200, i=1, cr=150_000, cwr=0),
        _turn("c", when=NOW - 100, i=2, cr=50_000, cwr=8),
        _cost("s1", "claude-opus-5[1m]"),
    ])
    context = _session(index.report())["context"]

    assert context["used"] == 50_010, "the most recent request, not 300k of history"
    assert context["used_percent"] == pytest.approx(5.0, abs=0.1)


def test_the_variant_in_the_cost_block_sizes_the_window(index, tmp_path):
    """The transcript says claude-opus-5; only the cost block says [1m]."""
    _ingest(index, tmp_path, [
        _turn("a", when=NOW - 10, i=0, cr=400_000, cwr=0),
        _cost("s1", "claude-opus-5[1m]"),
    ])
    context = _session(index.report())["context"]

    assert context["window"] == 1_000_000
    assert context["source"] == cw.VARIANT_SUFFIX
    assert context["used_percent"] == pytest.approx(40.0, abs=0.1)


def test_without_a_cost_block_a_small_session_still_reports_a_percentage(index, tmp_path):
    """Falls back to the model table, which is right whenever the session is
    genuinely on the base window."""
    _ingest(index, tmp_path, [_turn("a", when=NOW - 10, i=0, cr=20_000, cwr=0)])
    context = _session(index.report())["context"]

    assert context["window"] == 200_000
    assert context["source"] == cw.MODEL_TABLE
    assert context["used_percent"] == pytest.approx(10.0, abs=0.1)


def test_a_large_session_without_a_cost_block_reports_na_not_four_hundred_percent(
        index, tmp_path):
    """Exactly the shape seen in production: 795k measured, no variant
    recorded, base window 200k. The measurement disproves the window."""
    _ingest(index, tmp_path, [_turn("a", when=NOW - 10, i=0, cr=795_496, cwr=0)])
    context = _session(index.report())["context"]

    assert context["used_percent"] is None
    assert context["source"] == cw.CONTRADICTED
    assert context["used"] == 795_496


# -- the payload shape the page depends on --------------------------------------------

def test_every_session_carries_a_context_block(index, tmp_path):
    _ingest(index, tmp_path, [
        _turn("a", when=NOW - 10),
        _turn("b", when=NOW - 5, session="s2"),
    ])
    for session in index.report()["sessions"]:
        assert set(session["context"]) == {"used", "window", "used_percent",
                                           "source", "detail"}


def test_the_sessions_api_carries_the_same_context_block(index, tmp_path):
    """The page's session table reads top_sessions, not the local report --
    a fix that reached only one of them would look done and render nothing."""
    _ingest(index, tmp_path, [
        _turn("a", when=NOW - 10, i=0, cr=100_000, cwr=0),
        _cost("s1", "claude-opus-5[1m]"),
    ])
    item = index.top_sessions()["items"][0]

    assert item["context"]["window"] == 1_000_000
    assert item["context"]["used_percent"] == pytest.approx(10.0, abs=0.1)


def test_quota_is_left_untouched_and_still_honestly_unavailable(index, tmp_path):
    """Context fullness must not be quietly presented as subscription quota:
    they answer different questions and only one of them is knowable here."""
    _ingest(index, tmp_path, [_turn("a", when=NOW - 10)])
    report = index.report()

    for window in report["quota_windows"]:
        assert window["used_percent"] is None
        assert window["source"] == "unavailable"


def test_a_subagent_gets_its_own_context_not_the_parents(index, tmp_path):
    """A subagent shares its parent's session id; folding them together would
    report one context bar sized from the other's prompt."""
    parent = _turn("a", when=NOW - 20, i=0, cr=10_000, cwr=0)
    child = _turn("b", when=NOW - 10, i=0, cr=90_000, cwr=0)
    child["isSidechain"] = True
    _ingest(index, tmp_path, [parent, child])

    contexts = {bool(s["is_subagent"]): s["context"]["used"]
                for s in index.report()["sessions"]}
    assert contexts.get(False) == 10_000
    assert contexts.get(True) == 90_000
