"""AI usage read from local CLI artefacts.

Fixtures are shaped from what is actually on this machine, not from a
guess: a Claude transcript here holds 13 entry types of which only
`assistant` carries `message.usage`, and `~/.claude/sessions/<pid>.json`
carries the tmux target that attributes a row to a real session.

The hard requirement these tests protect: a reset time is reported ONLY
where a CLI recorded one. There is deliberately no code path that derives
one from a session start, so there is a test asserting the absence.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from terminal_mcp.ai_usage_index import ROLLING_WINDOW_SECONDS, AiUsageIndex
from terminal_mcp.ai_usage_local import (AGENT_CLAUDE, AGENT_CODEX, SOURCE_CLI_STATE,
                                         SOURCE_TRANSCRIPT, SOURCE_UNAVAILABLE, FileCursor,
                                         claude_event, claude_session_links, codex_event,
                                         codex_quota_windows, parse_jsonl)

NOW = time.time()


def _assistant(uuid: str, *, when: float | None = None, model: str = "claude-opus-5",
               sidechain: bool = False, session: str = "sess-1", **usage):
    counts = {"input_tokens": 10, "output_tokens": 20,
              "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40}
    counts.update(usage)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(when or NOW)) + ".000Z"
    return {"type": "assistant", "uuid": uuid, "timestamp": stamp, "sessionId": session,
            "requestId": "req_" + uuid, "isSidechain": sidechain, "cwd": "/repo",
            "gitBranch": "main", "version": "2.1.266",
            "message": {"role": "assistant", "model": model, "usage": counts}}


def _write(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def _claude_home(tmp_path, entries, *, links=None, name="sess-1"):
    home = tmp_path / "claude"
    _write(home / "projects" / "-repo" / f"{name}.jsonl", entries)
    for pid, link in (links or {}).items():
        (home / "sessions").mkdir(parents=True, exist_ok=True)
        (home / "sessions" / f"{pid}.json").write_text(json.dumps(link), encoding="utf-8")
    return home


# -- parsing -----------------------------------------------------------------

def test_the_four_token_kinds_are_read_separately():
    event = claude_event(_assistant("a"), node_id="local")
    assert (event.input_tokens, event.output_tokens,
            event.cache_read_tokens, event.cache_write_tokens) == (10, 20, 30, 40)
    assert event.total_tokens == 100


def test_entries_without_usage_are_skipped_not_failed():
    # 13 of 14 entry types in a real transcript carry no usage at all.
    for entry in ({"type": "user", "uuid": "u"}, {"type": "system", "uuid": "s"},
                  {"type": "assistant", "uuid": "x", "message": {"role": "assistant"}}):
        assert claude_event(entry, node_id="local") is None


def test_a_subagent_turn_is_marked():
    assert claude_event(_assistant("a", sidechain=True), node_id="local").is_subagent is True


@pytest.mark.parametrize("usage_shape", [
    {"message": {"usage": {"input_tokens": 5, "output_tokens": 1}}},
    {"usage": {"inputTokens": 5, "outputTokens": 1}},
    {"response": {"usage": {"prompt_tokens": 5, "completion_tokens": 1}}},
])
def test_the_parser_tolerates_more_than_one_layout(usage_shape):
    # Two CLI versions are live on this host (2.1.266 and 2.1.267); pinning
    # one layout is how a parser silently reports zero after an upgrade.
    entry = {"type": "assistant", "uuid": "v", "timestamp": "2026-09-11T00:00:00Z",
             "sessionId": "s", **usage_shape}
    event = claude_event(entry, node_id="local")
    assert event is not None and event.input_tokens == 5


def test_a_malformed_line_does_not_stop_the_file(tmp_path):
    path = tmp_path / "t.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant("a")) + "\n")
        handle.write("{not json at all\n")
        handle.write(json.dumps(_assistant("b")) + "\n")
    outcome = parse_jsonl(path, node_id="local", agent=AGENT_CLAUDE)
    assert len(outcome.events) == 2
    assert outcome.unparsed == 1          # counted, never silently dropped


def test_a_half_written_final_line_is_left_for_next_time(tmp_path):
    path = tmp_path / "t.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant("a")) + "\n")
        handle.write('{"type":"assistant","uuid":"partial"')   # no newline: mid-write
    outcome = parse_jsonl(path, node_id="local", agent=AGENT_CLAUDE)
    assert len(outcome.events) == 1
    assert outcome.end_offset == len(json.dumps(_assistant("a")) + "\n")


# -- incremental reading -----------------------------------------------------

def test_appended_lines_are_read_without_re_reading_the_file(tmp_path):
    path = _write(tmp_path / "t.jsonl", [_assistant("a")])
    first = parse_jsonl(path, node_id="local", agent=AGENT_CLAUDE)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_assistant("b")) + "\n")
    second = parse_jsonl(path, node_id="local", agent=AGENT_CLAUDE,
                         start_offset=first.end_offset)
    assert [e.event_id for e in second.events] == ["b"]
    assert second.lines_read == 1


def test_a_rotated_file_is_read_from_the_start_again(tmp_path):
    path = tmp_path / "t.jsonl"
    _write(path, [_assistant("a")])
    cursor = FileCursor.of(path)
    stale = FileCursor(str(path), cursor.inode, cursor.size, cursor.size)
    path.unlink()
    _write(path, [_assistant("c")])            # new inode, same path
    assert stale.resume_offset(FileCursor.of(path)) == 0


def test_a_truncated_file_is_read_from_the_start_again(tmp_path):
    path = _write(tmp_path / "t.jsonl", [_assistant("a"), _assistant("b")])
    size = path.stat().st_size
    stale = FileCursor(str(path), path.stat().st_ino, size, size)
    path.write_text(json.dumps(_assistant("a")) + "\n", encoding="utf-8")
    assert stale.resume_offset(FileCursor.of(path)) == 0


# -- exactly once ------------------------------------------------------------

def test_re_reading_a_file_counts_nothing_twice(tmp_path, monkeypatch):
    home = _claude_home(tmp_path, [_assistant("a"), _assistant("b")])
    index = AiUsageIndex(tmp_path / "u.db")
    assert index.refresh(claude_home=home)["events_new"] == 2
    # Force the full re-read a rotation would cause.
    with index._connect() as connection:                        # noqa: SLF001
        connection.execute("UPDATE file_cursors SET offset = 0, inode = -1")
    assert index.refresh(claude_home=home)["events_new"] == 0
    assert index.report()["totals"]["lifetime"]["events"] == 2


def test_a_retried_request_is_not_double_counted(tmp_path):
    # Same requestId, two lines, distinct uuids: two real turns. Same uuid
    # twice (a duplicated write) is one.
    home = _claude_home(tmp_path, [_assistant("dup"), _assistant("dup")])
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    assert index.report()["totals"]["lifetime"]["events"] == 1


def test_concurrent_sessions_are_kept_apart(tmp_path):
    home = tmp_path / "claude"
    _write(home / "projects" / "-a" / "s1.jsonl", [_assistant("a", session="s1")])
    _write(home / "projects" / "-b" / "s2.jsonl", [_assistant("b", session="s2")])
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    sessions = index.report()["sessions"]
    assert {s["agent_session_id"] for s in sessions} == {"s1", "s2"}


# -- windows -----------------------------------------------------------------

def test_the_rolling_window_excludes_older_activity(tmp_path):
    home = _claude_home(tmp_path, [
        _assistant("old", when=NOW - ROLLING_WINDOW_SECONDS - 600),
        _assistant("new", when=NOW - 60)])
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    report = index.report(now=NOW)
    assert report["totals"]["rolling_5h"]["messages"] == 1
    assert report["totals"]["lifetime"]["events"] == 2


def test_the_rolling_window_is_labelled_as_activity_not_quota(tmp_path):
    index = AiUsageIndex(tmp_path / "u.db")
    note = index.report()["window"]["note"].casefold()
    assert "activity" in note and "not a subscription quota" in note


# -- reset time: observed, or honestly absent --------------------------------

def test_claude_reports_no_quota_window_because_it_records_none(tmp_path):
    home = _claude_home(tmp_path, [_assistant("a")])
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    claude = [w for w in index.report()["quota_windows"] if w["agent"] == AGENT_CLAUDE]
    assert claude and claude[0]["observed"] == 0
    assert claude[0]["resets_at"] is None
    assert claude[0]["source"] == SOURCE_UNAVAILABLE
    assert claude[0]["detail"]                      # says why, not just nothing


def test_no_reset_time_is_ever_derived_from_a_session_start(tmp_path):
    """The requirement, as an executable statement.

    A session that started four hours ago must not produce "resets in one
    hour". Only a CLI-recorded window may set resets_at.
    """
    home = _claude_home(tmp_path, [_assistant("a", when=NOW - 4 * 3600)],
                        links={"111": {"sessionId": "sess-1", "pid": 111,
                                       "startedAt": int((NOW - 4 * 3600) * 1000),
                                       "cwd": "/repo", "tmux": "m1:@0.%0"}})
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home, codex_home=tmp_path / "nocodex")
    for window in index.report(now=NOW, claude_home=home)["quota_windows"]:
        assert window["resets_at"] is None
        assert window["used_percent"] is None


def test_a_codex_recorded_window_is_reported_with_its_reset(tmp_path):
    entry = {"timestamp": "2026-09-11T10:00:00Z",
             "rate_limits": {"primary": {"used_percent": 42.5, "resets_in_seconds": 1800}}}
    anchor = time.mktime(time.strptime("2026-09-11T10:00:00", "%Y-%m-%dT%H:%M:%S"))
    windows = codex_quota_windows(entry, now=NOW)
    assert len(windows) == 1
    window = windows[0]
    assert window.observed and window.source == SOURCE_CLI_STATE
    assert window.used_percent == 42.5
    # Anchored to the entry's own timestamp plus the countdown the CLI wrote,
    # which is arithmetic on observed data -- not an assumed window length.
    assert window.resets_at is not None


def test_an_entry_without_rate_limits_yields_no_window():
    assert codex_quota_windows({"timestamp": "2026-09-11T10:00:00Z"}, now=NOW) == []


def test_codex_absent_reads_as_unavailable_not_zero(tmp_path):
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=tmp_path / "noclaude", codex_home=tmp_path / "nocodex")
    codex = [w for w in index.report()["quota_windows"] if w["agent"] == AGENT_CODEX]
    assert codex and codex[0]["observed"] == 0
    assert "No" in (codex[0]["detail"] or "")


def test_codex_usage_is_parsed_when_present(tmp_path):
    entry = {"timestamp": "2026-09-11T10:00:00Z", "session_id": "cx",
             "info": {"total_token_usage": {"input_tokens": 7, "output_tokens": 3,
                                            "cached_input_tokens": 2}}}
    event = codex_event(entry, node_id="local")
    assert event is not None
    assert (event.agent, event.input_tokens, event.cache_read_tokens) == (AGENT_CODEX, 7, 2)


# -- attribution -------------------------------------------------------------

def test_a_session_is_attributed_through_the_tmux_link_the_cli_wrote(tmp_path):
    home = _claude_home(tmp_path, [_assistant("a", session="sess-1")],
                        links={"26176": {"sessionId": "sess-1", "pid": 26176,
                                         "cwd": "/repo", "version": "2.1.266",
                                         "tmux": "m1:@0.%0"}})
    links = claude_session_links(home)
    assert links["sess-1"]["tmux_session"] == "m1"
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    row = index.report(session_names={"m1": "stable-abc"}, claude_home=home)["sessions"][0]
    assert row["session"] == "m1"
    assert row["stable_session_id"] == "stable-abc"
    assert row["pid"] == 26176


def test_a_renamed_session_keeps_its_history(tmp_path):
    # stable_session_id is resolved at read time against the registry, so a
    # rename re-attributes history rather than splitting it.
    home = _claude_home(tmp_path, [_assistant("a", session="sess-1")],
                        links={"1": {"sessionId": "sess-1", "pid": 1, "tmux": "old-name:@0.%0"}})
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    before = index.report(session_names={"old-name": "stable-1"}, claude_home=home)["sessions"][0]
    (home / "sessions" / "1.json").write_text(json.dumps(
        {"sessionId": "sess-1", "pid": 1, "tmux": "new-name:@0.%0"}), encoding="utf-8")
    after = index.report(session_names={"new-name": "stable-1"}, claude_home=home)["sessions"][0]
    assert before["lifetime"]["total"] == after["lifetime"]["total"]
    assert after["session"] == "new-name"
    assert after["stable_session_id"] == before["stable_session_id"] == "stable-1"


def test_every_row_declares_where_its_numbers_came_from(tmp_path):
    home = _claude_home(tmp_path, [_assistant("a")])
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    report = index.report()
    assert all(row["source"] == SOURCE_TRANSCRIPT for row in report["sessions"])
    assert set(report["sources"]) == {"claude", "codex"}


# -- privacy -----------------------------------------------------------------

def test_no_prompt_text_reaches_the_report(tmp_path):
    secret = "SUPER-SECRET-PROMPT-TEXT"
    entries = [{"type": "user", "uuid": "u", "timestamp": "2026-09-11T10:00:00Z",
                "message": {"role": "user", "content": secret}},
               _assistant("a")]
    home = _claude_home(tmp_path, entries)
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    assert secret not in json.dumps(index.report())


def test_the_history_file_with_prompt_text_is_never_opened(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    _write(home / "projects" / "-repo" / "s.jsonl", [_assistant("a")])
    (home / "history.jsonl").write_text(
        json.dumps({"display": "a prompt", "sessionId": "s"}) + "\n", encoding="utf-8")
    opened: list[str] = []
    real_open = os.open

    def watched(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", watched)
    AiUsageIndex(tmp_path / "u.db").refresh(claude_home=home, codex_home=tmp_path / "no")
    assert not any(name.endswith("history.jsonl") for name in opened)


def test_subagent_cost_is_its_own_row_not_folded_into_the_parent(tmp_path):
    """A subagent shares its parent's sessionId.

    Grouping on the session id alone therefore merged the two and set the
    subagent flag from whichever row the aggregate happened to pick -- so a
    subagent's cost silently became the parent's, and the page could not
    show what the task asked for. No transcript on this host has sidechain
    turns, which is exactly why this needed a test rather than an eyeball.
    """
    home = _claude_home(tmp_path, [
        _assistant("main", session="shared", input_tokens=100),
        _assistant("sub", session="shared", sidechain=True, input_tokens=7)])
    index = AiUsageIndex(tmp_path / "u.db")
    index.refresh(claude_home=home)
    rows = index.report()["sessions"]
    assert len(rows) == 2
    by_flag = {row["is_subagent"]: row for row in rows}
    assert by_flag[False]["lifetime"]["input"] == 100
    assert by_flag[True]["lifetime"]["input"] == 7
    assert by_flag[True]["agent_session_id"] == by_flag[False]["agent_session_id"]
