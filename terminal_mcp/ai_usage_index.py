"""Incremental index over local AI-CLI usage, and the aggregates the report
page reads.

WHY AN INDEX AT ALL
-------------------
The transcripts on this host are 19MB and one of them is 13MB and still
growing. Re-parsing all of it on every dashboard poll would make a
read-only report the most expensive thing on the box. So each file is read
once from its last byte offset, and the events land in SQLite.

EXACTLY-ONCE
------------
`event_id` is the primary key. Claude's per-line `uuid` is stable across
re-reads, so re-ingesting a file -- which happens whenever it is rotated or
truncated and the offset is reset to 0 -- inserts nothing new. That is the
property that makes the cheap path (offsets) safe to abandon for the
correct path (full re-read) whenever identity is in doubt.

SESSION IDENTITY ACROSS RESTART AND RENAME
------------------------------------------
Usage rows carry the CLI's own session id. A tmux session that is renamed
or restarted keeps its row in the session registry, so the mapping
CLI session -> tmux name -> stable_session_id is resolved at READ time
against the registry, never frozen into the stored row. Renaming a session
therefore re-attributes its history correctly instead of splitting it.

NEVER STORED: prompt text, response text, credentials. Only counters,
identifiers, timestamps, model names.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .ai_usage_local import (AGENT_CLAUDE, AGENT_CODEX, CLAUDE_QUOTA_UNOBSERVED,
                             SOURCE_CLI_STATE, SOURCE_TRANSCRIPT, SOURCE_UNAVAILABLE,
                             FileCursor, QuotaWindow, UsageEvent, claude_session_links,
                             codex_quota_windows, discover_claude_transcripts,
                             discover_codex_logs, parse_jsonl, unobserved_window)

SCHEMA_VERSION = 1
ROLLING_WINDOW_SECONDS = 5 * 3600


def default_index_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_AI_USAGE_DB")
    if override:
        return Path(override)
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "ai_usage.db"


class AiUsageIndex:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_index_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    event_id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    agent_session_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    model TEXT,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    project TEXT,
                    git_branch TEXT,
                    is_subagent INTEGER NOT NULL DEFAULT 0,
                    cli_version TEXT,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_ts ON usage_events (ts);
                CREATE INDEX IF NOT EXISTS usage_session ON usage_events (agent_session_id);
                CREATE TABLE IF NOT EXISTS file_cursors (
                    path TEXT PRIMARY KEY,
                    inode INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    offset INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quota_windows (
                    agent TEXT NOT NULL,
                    label TEXT NOT NULL,
                    observed INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    used_percent REAL,
                    resets_at REAL,
                    detail TEXT,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (agent, label)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    # -- ingest ---------------------------------------------------------------

    def _cursor_for(self, connection: sqlite3.Connection, path: Path) -> FileCursor | None:
        row = connection.execute(
            "SELECT path, inode, size, offset FROM file_cursors WHERE path = ?",
            (str(path),)).fetchone()
        if row is None:
            return None
        return FileCursor(row["path"], row["inode"], row["size"], row["offset"])

    def _store_events(self, connection: sqlite3.Connection, events: Iterable[UsageEvent]) -> int:
        rows = [(e.event_id, e.agent, e.agent_session_id, e.node_id, e.timestamp, e.model,
                 e.input_tokens, e.output_tokens, e.cache_read_tokens, e.cache_write_tokens,
                 e.project, e.git_branch, int(e.is_subagent), e.cli_version, e.source)
                for e in events]
        if not rows:
            return 0
        before = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        connection.executemany(
            """INSERT OR IGNORE INTO usage_events
               (event_id, agent, agent_session_id, node_id, ts, model,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                project, git_branch, is_subagent, cli_version, source)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        after = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        return after - before

    def refresh(self, *, node_id: str = "local", claude_home: Path | None = None,
                codex_home: Path | None = None) -> dict[str, Any]:
        """Read whatever is new and return what happened.

        Deliberately reports `unparsed` and `files_rescanned` rather than
        swallowing them: a parser that silently drops what it cannot read is
        indistinguishable from one with nothing to read.
        """
        started = time.time()
        summary = {"files_seen": 0, "files_read": 0, "files_rescanned": 0,
                   "events_new": 0, "lines_read": 0, "unparsed": 0, "agents": {}}
        sources = [(AGENT_CLAUDE, discover_claude_transcripts(claude_home)),
                   (AGENT_CODEX, discover_codex_logs(codex_home))]
        with self._connect() as connection:
            for agent, files in sources:
                agent_new = 0
                for path in files:
                    summary["files_seen"] += 1
                    current = FileCursor.of(path)
                    if current is None:
                        continue
                    stored = self._cursor_for(connection, path)
                    start = stored.resume_offset(current) if stored else 0
                    if stored and start == 0 and stored.offset > 0:
                        summary["files_rescanned"] += 1
                    if stored and start >= current.size:
                        continue        # nothing appended since last time
                    outcome = parse_jsonl(path, node_id=node_id, agent=agent,
                                          start_offset=start,
                                          session_id=path.stem)
                    summary["files_read"] += 1
                    summary["lines_read"] += outcome.lines_read
                    summary["unparsed"] += outcome.unparsed
                    inserted = self._store_events(connection, outcome.events)
                    summary["events_new"] += inserted
                    agent_new += inserted
                    connection.execute(
                        """INSERT INTO file_cursors (path, inode, size, offset, updated_at)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(path) DO UPDATE SET
                             inode=excluded.inode, size=excluded.size,
                             offset=excluded.offset, updated_at=excluded.updated_at""",
                        (str(path), current.inode, current.size, outcome.end_offset, started))
                summary["agents"][agent] = {"files": len(files), "events_new": agent_new}
            self._refresh_quota(connection, codex_home=codex_home)
        summary["duration_seconds"] = round(time.time() - started, 3)
        return summary

    def _refresh_quota(self, connection: sqlite3.Connection,
                       *, codex_home: Path | None = None) -> None:
        now = time.time()
        windows: list[tuple[str, QuotaWindow]] = [
            (AGENT_CLAUDE, unobserved_window("subscription", CLAUDE_QUOTA_UNOBSERVED))]
        codex_files = discover_codex_logs(codex_home)
        codex_found: list[QuotaWindow] = []
        for path in reversed(codex_files[-5:]):     # newest few carry the latest state
            import json as _json
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for raw in reversed(lines):
                try:
                    entry = _json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                found = codex_quota_windows(entry, now=now)
                if found:
                    codex_found = found
                    break
            if codex_found:
                break
        if codex_found:
            windows.extend((AGENT_CODEX, w) for w in codex_found)
        else:
            detail = ("No ~/.codex on this machine." if not codex_files
                      else "Codex logs present but contain no rate-limit metadata.")
            windows.append((AGENT_CODEX, unobserved_window("subscription", detail)))
        connection.execute("DELETE FROM quota_windows")
        connection.executemany(
            """INSERT INTO quota_windows
               (agent, label, observed, source, used_percent, resets_at, detail, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [(agent, w.label, int(w.observed), w.source, w.used_percent,
              w.resets_at, w.detail, now) for agent, w in windows])

    # -- read -----------------------------------------------------------------

    def report(self, *, now: float | None = None,
               session_names: dict[str, str] | None = None,
               claude_home: Path | None = None) -> dict[str, Any]:
        """Everything the page renders, in one query pass."""
        now = now or time.time()
        rolling_from = now - ROLLING_WINDOW_SECONDS
        today_from = _local_midnight(now)
        links = claude_session_links(claude_home)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT agent, agent_session_id, node_id, model, project, git_branch,
                          is_subagent, cli_version,
                          SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cache_write_tokens) AS cache_write_tokens,
                          COUNT(*) AS messages,
                          MIN(ts) AS first_ts, MAX(ts) AS last_ts,
                          SUM(CASE WHEN ts >= ? THEN input_tokens ELSE 0 END) AS r_input,
                          SUM(CASE WHEN ts >= ? THEN output_tokens ELSE 0 END) AS r_output,
                          SUM(CASE WHEN ts >= ? THEN cache_read_tokens ELSE 0 END) AS r_cache_read,
                          SUM(CASE WHEN ts >= ? THEN cache_write_tokens ELSE 0 END) AS r_cache_write,
                          SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS r_messages,
                          SUM(CASE WHEN ts >= ? THEN input_tokens + output_tokens
                                   + cache_read_tokens + cache_write_tokens ELSE 0 END) AS d_total,
                          SUM(CASE WHEN ts >= ? THEN 1 ELSE 0 END) AS d_messages
                   FROM usage_events
                   -- is_subagent is part of the key, not an attribute of the
                   -- group: a subagent shares its parent's session id, so
                   -- grouping without it would fold subagent cost into the
                   -- parent row and leave the flag set by whichever row the
                   -- aggregate happened to pick.
                   GROUP BY agent, agent_session_id, is_subagent
                   ORDER BY last_ts DESC""",
                (rolling_from,) * 5 + (today_from, today_from)).fetchall()
            quota = [dict(r) for r in connection.execute(
                "SELECT * FROM quota_windows ORDER BY agent, label")]
            totals = connection.execute(
                """SELECT COUNT(*) AS events,
                          SUM(input_tokens) AS input_tokens,
                          SUM(output_tokens) AS output_tokens,
                          SUM(cache_read_tokens) AS cache_read_tokens,
                          SUM(cache_write_tokens) AS cache_write_tokens
                   FROM usage_events""").fetchone()

        sessions = []
        for row in rows:
            link = links.get(row["agent_session_id"], {})
            tmux_name = link.get("tmux_session")
            sessions.append({
                "agent": row["agent"],
                "agent_session_id": row["agent_session_id"],
                "session": tmux_name,
                "stable_session_id": (session_names or {}).get(tmux_name or "", None),
                "node_id": row["node_id"],
                "model": row["model"],
                "project": row["project"] or link.get("cwd"),
                "git_branch": row["git_branch"],
                "is_subagent": bool(row["is_subagent"]),
                "cli_version": row["cli_version"] or link.get("cli_version"),
                "pid": link.get("pid"),
                "rolling_5h": {
                    "input": row["r_input"] or 0, "output": row["r_output"] or 0,
                    "cache_read": row["r_cache_read"] or 0,
                    "cache_write": row["r_cache_write"] or 0,
                    "messages": row["r_messages"] or 0,
                    "total": (row["r_input"] or 0) + (row["r_output"] or 0)
                             + (row["r_cache_read"] or 0) + (row["r_cache_write"] or 0),
                },
                "today": {"total": row["d_total"] or 0, "messages": row["d_messages"] or 0},
                "lifetime": {
                    "input": row["input_tokens"] or 0, "output": row["output_tokens"] or 0,
                    "cache_read": row["cache_read_tokens"] or 0,
                    "cache_write": row["cache_write_tokens"] or 0,
                    "messages": row["messages"] or 0,
                    "total": (row["input_tokens"] or 0) + (row["output_tokens"] or 0)
                             + (row["cache_read_tokens"] or 0) + (row["cache_write_tokens"] or 0),
                },
                "first_seen": row["first_ts"], "last_activity": row["last_ts"],
                "source": SOURCE_TRANSCRIPT,
            })

        def _sum(key: str, bucket: str) -> int:
            return sum(s[bucket][key] for s in sessions)

        return {
            "generated_at": now,
            "window": {
                "rolling_seconds": ROLLING_WINDOW_SECONDS,
                "rolling_from": rolling_from,
                "today_from": today_from,
                "note": ("Rolling 5h is token ACTIVITY measured from transcript "
                         "timestamps. It is not a subscription quota window."),
            },
            "totals": {
                "rolling_5h": {k: _sum(k, "rolling_5h") for k in
                               ("input", "output", "cache_read", "cache_write", "total", "messages")},
                "today": {k: _sum(k, "today") for k in ("total", "messages")},
                "lifetime": {
                    "input": totals["input_tokens"] or 0,
                    "output": totals["output_tokens"] or 0,
                    "cache_read": totals["cache_read_tokens"] or 0,
                    "cache_write": totals["cache_write_tokens"] or 0,
                    "events": totals["events"] or 0,
                },
            },
            "sessions": sessions,
            "quota_windows": quota,
            "sources": {
                "claude": {"status": SOURCE_TRANSCRIPT if sessions else SOURCE_UNAVAILABLE,
                           "detail": "~/.claude/projects/**/*.jsonl + ~/.claude/sessions/*.json"},
                "codex": {"status": SOURCE_CLI_STATE if discover_codex_logs() else SOURCE_UNAVAILABLE,
                          "detail": "$CODEX_HOME/{sessions,logs,history}/**/*.jsonl"},
            },
        }


def _local_midnight(now: float) -> float:
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0,
                        local.tm_wday, local.tm_yday, local.tm_isdst))
