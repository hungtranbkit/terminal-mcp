"""The analytics layer: schema, migration, aggregation, attribution, quota.

Every number this screen shows is derived here, so the defects worth
catching are arithmetic and attribution ones -- a table that silently
answers "no data" because a query failed looks identical to one with
nothing to show, which is exactly how the ambiguous-column bug reached a
rendered page.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from terminal_mcp.ai_usage_index import ROLLING_WINDOW_SECONDS, AiUsageIndex
from terminal_mcp.ai_usage_local import claude_cost, claude_prompt, parse_claude_transcript

NOW = time.time()


def _turn(uuid, *, when, prompt_after=None, session="s1", project="/repo",
          model="claude-opus-5", i=10, o=20, cr=30, cw=40):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + ".000Z"
    return {"type": "assistant", "uuid": uuid, "timestamp": stamp, "sessionId": session,
            "cwd": project, "gitBranch": "main", "version": "2.1.266",
            "message": {"role": "assistant", "model": model,
                        "usage": {"input_tokens": i, "output_tokens": o,
                                  "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw}}}


def _prompt(prompt_id, *, when, text="build the thing", session="s1", project="/repo"):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when)) + ".000Z"
    return {"type": "user", "promptId": prompt_id, "promptSource": "typed",
            "timestamp": stamp, "sessionId": session, "cwd": project, "gitBranch": "main",
            "message": {"role": "user", "content": text}}


def _write(home, entries, name="s1"):
    project = home / "projects" / "-repo"
    project.mkdir(parents=True, exist_ok=True)
    with (project / f"{name}.jsonl").open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return home


@pytest.fixture
def index(tmp_path):
    return AiUsageIndex(tmp_path / "usage.db")


# -- schema and migration ----------------------------------------------------

def test_a_fresh_database_is_created_at_the_current_version(tmp_path):
    AiUsageIndex(tmp_path / "fresh.db")
    connection = sqlite3.connect(tmp_path / "fresh.db")
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    columns = [row[1] for row in connection.execute("PRAGMA table_info(usage_events)")]
    for column in ("prompt_id", "collector_version", "parsing_confidence"):
        assert column in columns


def test_a_v1_database_is_upgraded_without_losing_events(tmp_path):
    path = tmp_path / "v1.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE usage_events (event_id TEXT PRIMARY KEY, agent TEXT NOT NULL,
            agent_session_id TEXT NOT NULL, node_id TEXT NOT NULL, ts REAL NOT NULL,
            model TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0, project TEXT, git_branch TEXT,
            is_subagent INTEGER NOT NULL DEFAULT 0, cli_version TEXT, source TEXT NOT NULL);
        CREATE TABLE file_cursors (path TEXT PRIMARY KEY, inode INTEGER, size INTEGER,
            offset INTEGER, updated_at REAL);
        CREATE TABLE quota_windows (agent TEXT, label TEXT, observed INTEGER, source TEXT,
            used_percent REAL, resets_at REAL, detail TEXT, updated_at REAL,
            PRIMARY KEY(agent,label));
    """)
    connection.execute("INSERT INTO usage_events VALUES "
                       "('e1','claude','s','local',1.0,'m',1,2,3,4,'/p','b',0,'v','session_transcript')")
    connection.commit(); connection.close()

    AiUsageIndex(path)
    AiUsageIndex(path)          # idempotent: running it twice is normal at startup

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
    columns = [row[1] for row in connection.execute("PRAGMA table_info(usage_events)")]
    assert "prompt_id" in columns


# -- prompt attribution ------------------------------------------------------

def test_turns_are_attributed_to_the_prompt_that_caused_them(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 600), _turn("a", when=NOW - 590),
                  _turn("b", when=NOW - 580),
                  _prompt("p2", when=NOW - 500), _turn("c", when=NOW - 490)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    by_prompt = {row["prompt_id"]: row for row in index.top_prompts()["items"]}
    assert by_prompt["p1"]["requests"] == 2
    assert by_prompt["p2"]["requests"] == 1
    assert by_prompt["p1"]["total_tokens"] == 2 * 100


def test_attribution_survives_an_incremental_read(tmp_path, index):
    """A turn appended after the offset still belongs to its prompt.

    Without carrying the open prompt across reads, the most recent -- and
    usually most interesting -- work would land as unassigned.
    """
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 600), _turn("a", when=NOW - 590)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    _write(home, [_turn("b", when=NOW - 100)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    rows = {row["prompt_id"]: row for row in index.top_prompts()["items"]}
    assert rows["p1"]["requests"] == 2


def test_a_turn_with_no_preceding_prompt_is_unassigned_not_invented(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_turn("a", when=NOW - 100)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    row = index.top_prompts()["items"][0]
    assert row["prompt_id"] is None
    assert row["preview"] is None


def test_a_tool_result_is_not_treated_as_a_prompt():
    # Its content is a list of blocks, not typed text; flattening it would
    # put file contents into a preview.
    entry = {"type": "user", "promptId": "p", "timestamp": "2026-09-11T00:00:00Z",
             "sessionId": "s", "message": {"role": "user",
                                           "content": [{"type": "tool_result", "content": "x"}]}}
    assert claude_prompt(entry) is None


def test_a_prompt_preview_is_redacted_truncated_and_hashed():
    long_text = "x" * 500
    record = claude_prompt(_prompt("p", when=NOW, text=long_text))
    assert len(record.preview) <= 181          # PROMPT_PREVIEW_CHARS + ellipsis
    assert record.preview.endswith("…")
    assert record.char_length == 500
    assert len(record.text_hash) == 32
    # Identical prompts group; different ones do not.
    assert claude_prompt(_prompt("q", when=NOW, text=long_text)).text_hash == record.text_hash
    assert claude_prompt(_prompt("r", when=NOW, text="other")).text_hash != record.text_hash


# -- aggregation -------------------------------------------------------------

def test_the_windows_add_up_to_what_was_ingested(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_prompt("p", when=NOW - 100), _turn("a", when=NOW - 100),
                  _turn("b", when=NOW - 86400 - 100),       # yesterday
                  _turn("c", when=NOW - 604800 - 100)])     # 8 days ago
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    summary = index.summary(now=NOW)
    assert summary["spans"]["5h"]["total_tokens"] == 100
    assert summary["spans"]["24h"]["total_tokens"] == 100
    assert summary["spans"]["7d"]["total_tokens"] == 200
    assert summary["spans"]["30d"]["total_tokens"] == 300
    assert summary["totals"]["total_tokens"] == 300


@pytest.mark.parametrize("bucket,expected", [("hour", 2), ("day", 1)])
def test_the_timeline_buckets_by_the_requested_grain(tmp_path, index, bucket, expected):
    home = tmp_path / "claude"
    base = NOW - (NOW % 86400) + 3600          # a fixed hour inside today
    _write(home, [_turn("a", when=base + 60), _turn("b", when=base + 120),
                  _turn("c", when=base + 3700)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    points = index.timeline(bucket=bucket)["points"]
    assert len(points) == expected
    assert sum(p["total_tokens"] for p in points) == 300


def test_an_unknown_bucket_is_refused(index):
    with pytest.raises(ValueError):
        index.timeline(bucket="fortnight")


def test_a_time_filter_narrows_every_query_the_same_way(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_prompt("old", when=NOW - 86400 * 3), _turn("a", when=NOW - 86400 * 3),
                  _prompt("new", when=NOW - 60), _turn("b", when=NOW - 60)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    recent = {"since": NOW - 3600}
    assert len(index.top_prompts(filters=recent)["items"]) == 1
    assert index.summary(now=NOW, filters=recent)["totals"]["total_tokens"] == 100
    assert index.top_sessions(now=NOW, filters=recent)["items"][0]["requests"] == 1


def test_top_prompts_works_with_a_time_filter(tmp_path, index):
    """Regression: the prompts query joins usage_events to prompts and both
    carry `ts`, so an unqualified filter raised "ambiguous column name: ts"
    -- and the route turned that into an empty table with a 200, which on
    screen is indistinguishable from having no prompts at all."""
    home = tmp_path / "claude"
    _write(home, [_prompt("p", when=NOW - 60), _turn("a", when=NOW - 60)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    items = index.top_prompts(filters={"since": NOW - 3600})["items"]
    assert len(items) == 1 and items[0]["prompt_id"] == "p"


def test_share_percentages_are_of_the_filtered_slice(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 100), _turn("a", when=NOW - 100),
                  _prompt("p2", when=NOW - 90), _turn("b", when=NOW - 90),
                  _turn("c", when=NOW - 89)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    rows = {r["prompt_id"]: r for r in index.top_prompts()["items"]}
    assert rows["p1"]["share_percent"] == pytest.approx(33.33, abs=0.02)
    assert rows["p2"]["share_percent"] == pytest.approx(66.67, abs=0.02)


# -- project mapping ---------------------------------------------------------

def test_a_project_comes_from_recorded_cwd_not_from_a_session_name(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_turn("a", when=NOW - 10, project="/home/me/repo-a")],
           name="misleading-session-name")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    assert index.top_projects()["items"][0]["project"] == "/home/me/repo-a"


def test_events_without_a_project_are_unassigned(tmp_path, index):
    home = tmp_path / "claude"
    entry = _turn("a", when=NOW - 10)
    del entry["cwd"]
    _write(home, [entry])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    assert index.top_projects()["items"][0]["project"] == "unassigned"


def test_a_project_trend_compares_against_the_previous_week(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_turn("a", when=NOW - 86400),                  # this week: 100
                  _turn("b", when=NOW - 604800 - 86400),         # last week: 200
                  _turn("c", when=NOW - 604800 - 86500)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    row = index.top_projects(now=NOW)["items"][0]
    assert row["trend_percent"] == pytest.approx(-50.0)


# -- cost --------------------------------------------------------------------

def test_cost_comes_from_the_cli_not_from_a_price_table():
    record = claude_cost({"type": "cost-state", "sessionId": "s", "totalCostUSD": 1.77,
                          "timestamp": "2026-09-11T00:00:00Z",
                          "modelUsage": {"claude-opus-5[1m]": {"costUSD": 1.76}}})
    assert record.total_cost_usd == 1.77
    assert "claude-opus-5[1m]" in record.per_model


def test_model_cost_joins_across_the_context_variant_suffix(tmp_path, index):
    """cost-state names "claude-opus-5[1m]" while a turn names
    "claude-opus-5"; joining on the raw string reported no cost at all for
    the model doing all the work."""
    home = tmp_path / "claude"
    _write(home, [_turn("a", when=NOW - 10, model="claude-opus-5"),
                  {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 2.5,
                   "timestamp": "2026-09-11T00:00:00Z",
                   "modelUsage": {"claude-opus-5[1m]": {"costUSD": 2.5}}}])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    row = index.model_breakdown()["items"][0]
    assert row["model"] == "claude-opus-5"
    assert row["estimated_cost_usd"] == pytest.approx(2.5)


def test_a_later_cost_state_supersedes_an_earlier_one(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [{"type": "cost-state", "sessionId": "s1", "totalCostUSD": 1.0,
                   "timestamp": "2026-09-11T00:00:00Z", "modelUsage": {}}])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    _write(home, [{"type": "cost-state", "sessionId": "s1", "totalCostUSD": 3.0,
                   "timestamp": "2026-09-11T01:00:00Z", "modelUsage": {}}])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    # It is a running total, not an increment: 3.0, never 4.0.
    assert index.summary()["estimated_cost_usd"] == pytest.approx(3.0)


# -- quota classification ----------------------------------------------------

def test_quota_snapshots_record_the_three_states(tmp_path, index):
    index.refresh(claude_home=tmp_path / "noclaude", codex_home=tmp_path / "nocodex")
    assert index.record_quota_snapshot(now=NOW) >= 1
    items = index.quota_history()["items"]
    assert items
    assert all(item["state"] in ("provider_reported", "locally_estimated", "unavailable")
               for item in items)
    # Nothing is reported on this host, and nothing pretends to be.
    assert all(item["state"] == "unavailable" for item in items)
    assert all(item["used_percent"] is None and item["resets_at"] is None for item in items)


def test_a_reported_window_is_stored_as_reported(tmp_path, index):
    codex = tmp_path / "codex" / "sessions"
    codex.mkdir(parents=True)
    (codex / "rollout.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-11T10:00:00Z",
        "rate_limits": {"primary": {"used_percent": 40.0, "resets_in_seconds": 900}}}) + "\n",
        encoding="utf-8")
    index.refresh(claude_home=tmp_path / "noclaude", codex_home=tmp_path / "codex")
    index.record_quota_snapshot(now=NOW)
    states = {item["agent"]: item["state"] for item in index.quota_history()["items"]}
    assert states["codex"] == "provider_reported"
    assert states["claude"] == "unavailable"


# -- pagination and export ---------------------------------------------------

def test_raw_events_are_paginated(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_turn(f"e{i}", when=NOW - i) for i in range(30)])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    first = index.raw_events(limit=10)
    assert len(first["items"]) == 10 and first["total"] == 30 and first["has_more"]
    last = index.raw_events(limit=10, offset=20)
    assert len(last["items"]) == 10 and not last["has_more"]


def test_the_page_size_is_bounded(tmp_path, index):
    assert index.raw_events(limit=100000)["limit"] == 500
