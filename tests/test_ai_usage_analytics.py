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
    # It is a running total, not an increment: 3.0, never 4.0. Checked on the
    # lifetime figure, because the range-scoped one apportions by tokens and
    # this fixture has no usage events to apportion across.
    assert index.cost_for()["lifetime_usd"] == pytest.approx(3.0)


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


# -- backfill ----------------------------------------------------------------

def test_events_indexed_before_prompts_existed_get_attributed(tmp_path):
    """The v1 index problem, reproduced.

    Events whose bytes were already read sit at EOF, so no ordinary refresh
    will ever touch them again -- measured on the production index, 96.79%
    of all tokens were attributed to "unassigned" and no cost was recorded.
    """
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 300), _turn("a", when=NOW - 290),
                  _turn("b", when=NOW - 280),
                  {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 4.2,
                   "timestamp": "2026-09-11T00:00:00Z", "modelUsage": {}}])
    index = AiUsageIndex(tmp_path / "usage.db")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    # Simulate what the older collector left behind.
    with index._connect() as connection:                        # noqa: SLF001
        connection.execute("UPDATE usage_events SET prompt_id = NULL")
        connection.execute("DELETE FROM prompts")
        connection.execute("DELETE FROM session_costs")

    filled = index.backfill(claude_home=home)
    assert filled["events_attributed"] == 2
    assert filled["prompts_new"] == 1
    assert filled["costs_new"] == 1
    assert index.top_prompts()["items"][0]["prompt_id"] == "p1"
    assert index.summary()["estimated_cost_usd"] == pytest.approx(4.2)


def test_backfill_is_idempotent_and_never_duplicates(tmp_path):
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 300), _turn("a", when=NOW - 290)])
    index = AiUsageIndex(tmp_path / "usage.db")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    before = index.summary()["totals"]
    for _ in range(3):
        index.backfill(claude_home=home)
    assert index.summary()["totals"] == before
    assert index.raw_events()["total"] == 1


def test_backfill_reports_only_the_rows_it_changed(tmp_path):
    # sqlite3's total_changes is cumulative for the connection; using it made
    # the summary claim three times more work than it did.
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 300)] +
           [_turn(f"t{i}", when=NOW - 290 + i) for i in range(5)])
    index = AiUsageIndex(tmp_path / "usage.db")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    with index._connect() as connection:                        # noqa: SLF001
        connection.execute("UPDATE usage_events SET prompt_id = NULL")
    assert index.backfill(claude_home=home)["events_attributed"] == 5


def test_backfill_leaves_an_already_attributed_event_alone(tmp_path):
    home = tmp_path / "claude"
    _write(home, [_prompt("p1", when=NOW - 300), _turn("a", when=NOW - 290)])
    index = AiUsageIndex(tmp_path / "usage.db")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    assert index.backfill(claude_home=home)["events_attributed"] == 0


# -- cost is of the SELECTED RANGE -------------------------------------------

def test_cost_used_to_ignore_the_range_and_now_does_not(tmp_path, index):
    """The defect the card's label was hiding.

    `estimated_cost_usd` was SUM(total_cost_usd) over every session ever,
    shown beside token counts that DID respect the filter. A narrower range
    must cost less, and the widest must equal what the CLI reported.
    """
    home = tmp_path / "claude"
    _write(home, [_turn("old", when=NOW - 86400 * 5), _turn("new", when=NOW - 60),
                  {"type": "cost-state", "sessionId": "s1", "totalCostUSD": 10.0,
                   "timestamp": "2026-09-11T00:00:00Z", "modelUsage": {}}])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")

    everything = index.cost_for()
    recent = index.cost_for(filters={"since": NOW - 3600})
    assert everything["usd"] == pytest.approx(10.0)          # sums back exactly
    assert recent["usd"] == pytest.approx(5.0)               # half the tokens, half the cost
    assert recent["usd"] < everything["usd"]


def test_a_per_token_rate_from_cost_state_would_have_been_wrong(tmp_path, index):
    """Why apportionment is by token SHARE, not by a recovered rate.

    cost-state's token counts are the totals at the moment it was written,
    which is less than the session went on to spend. Dividing costUSD by them
    produced $112 against a CLI-reported $12.20 on this fleet. Splitting the
    reported cost by share cannot exceed it, which this pins down.
    """
    home = tmp_path / "claude"
    _write(home, [_turn(f"t{i}", when=NOW - 100 + i) for i in range(10)] +
           [{"type": "cost-state", "sessionId": "s1", "totalCostUSD": 2.0,
             "timestamp": "2026-09-11T00:00:00Z",
             # Counts far smaller than the events, as a real snapshot is.
             "modelUsage": {"claude-opus-5[1m]": {"inputTokens": 1, "outputTokens": 1,
                                                  "cacheReadInputTokens": 1,
                                                  "cacheCreationInputTokens": 1,
                                                  "costUSD": 2.0}}}])
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    assert index.cost_for()["usd"] == pytest.approx(2.0)


def test_tokens_from_a_session_with_no_cost_yet_are_reported_not_priced(tmp_path, index):
    home = tmp_path / "claude"
    _write(home, [_turn("a", when=NOW - 60)])                # a live session: no cost-state
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    cost = index.cost_for()
    assert cost["usd"] is None
    assert cost["unpriced_tokens"] == 100
    assert cost["priced_sessions"] == 0


def test_the_summary_states_which_range_its_numbers_describe(tmp_path, index):
    summary = index.summary(now=NOW, filters={"since": NOW - 86400, "range_label": "Hôm nay"})
    assert summary["range"]["label"] == "Hôm nay"
    assert summary["range"]["since"] == pytest.approx(NOW - 86400)
    assert summary["cost_method"]


def test_a_presentation_label_is_never_used_as_a_sql_filter(index):
    # range_label rides along on the filter dict; treating it as a column
    # would raise rather than be ignored.
    assert index.summary(filters={"range_label": "Hôm nay"})["totals"] is not None


# -- quota windows -----------------------------------------------------------

@pytest.mark.parametrize("label,expected", [
    ("5h", "5h"), ("five_hour", "5h"), ("primary", "5h"), ("subscription", "5h"),
    ("1w", "1w"), ("weekly", "1w"), ("seven_day", "1w"), ("secondary", "1w"),
    ("something-else", None), (None, None),
])
def test_provider_wording_maps_to_the_window_it_means(label, expected):
    from terminal_mcp.ai_usage_index import _normalise_window

    assert _normalise_window(label) == expected


def test_both_windows_exist_for_both_providers_even_with_no_data(tmp_path, index):
    # An absent bar is indistinguishable from one nobody looked at.
    index.refresh(claude_home=tmp_path / "noclaude", codex_home=tmp_path / "nocodex")
    windows = index.report()["quota_windows"]
    pairs = {(w["agent"], w["window"]) for w in windows}
    assert pairs == {("claude", "5h"), ("claude", "1w"), ("codex", "5h"), ("codex", "1w")}
    assert all(w["observed"] == 0 and w["used_percent"] is None for w in windows)
    assert all(w["detail"] for w in windows)          # every N/A says why


def test_a_reported_window_carries_both_halves(tmp_path, index):
    codex = tmp_path / "codex" / "sessions"
    codex.mkdir(parents=True)
    (codex / "r.jsonl").write_text(json.dumps({
        "timestamp": "2026-09-11T10:00:00Z",
        "rate_limits": {"primary": {"used_percent": 30.0, "resets_in_seconds": 600}}}) + "\n",
        encoding="utf-8")
    index.refresh(claude_home=tmp_path / "noclaude", codex_home=tmp_path / "codex")
    window = next(w for w in index.report()["quota_windows"]
                  if w["agent"] == "codex" and w["window"] == "5h" and w["observed"])
    assert window["used_percent"] == pytest.approx(30.0)
    # The screen never has to do the subtraction silently.
    assert window["remaining_percent"] == pytest.approx(70.0)
    assert window["resets_at"] is not None


def test_a_snapshot_records_the_window_and_the_tier_but_no_identity(tmp_path, index, monkeypatch):
    monkeypatch.setattr("terminal_mcp.ai_usage_index._account_tier", lambda: "max")
    index.refresh(claude_home=tmp_path / "noclaude", codex_home=tmp_path / "nocodex")
    index.record_quota_snapshot(now=NOW)
    items = index.quota_history()["items"]
    assert {item["window"] for item in items} == {"5h", "1w"}
    assert all(item["account_tier"] == "max" for item in items)
    blob = json.dumps(items)
    for secret in ("@", "orgId", "token", "sk-"):
        assert secret not in blob


def test_the_account_tier_probe_never_reaches_a_credential_file(monkeypatch):
    """It shells out to `claude auth status --json`, which also prints an
    email and an org id. Only the tier is kept, and .credentials.json is
    never opened."""
    import subprocess

    from terminal_mcp.ai_usage_index import _account_tier

    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"subscriptionType": "max",
                                        "email": "someone@example.com",
                                        "orgId": "secret-org"}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _account_tier() == "max"
    assert calls == [["claude", "auth", "status", "--json"]]


def test_a_broken_auth_probe_degrades_to_none(monkeypatch):
    import subprocess

    from terminal_mcp.ai_usage_index import _account_tier

    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="x"))
    assert _account_tier() is None
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no claude")))
    assert _account_tier() is None
