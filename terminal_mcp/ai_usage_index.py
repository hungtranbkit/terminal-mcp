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

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .ai_context_window import context_usage
from .ai_usage_local import (AGENT_CLAUDE, AGENT_CODEX, CLAUDE_QUOTA_UNOBSERVED,
                             parse_claude_transcript,
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
            self._migrate(connection)
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
                    source TEXT NOT NULL,
                    -- v2. Listed here as well as in MIGRATIONS: a fresh
                    -- database is created current and never runs the ALTERs,
                    -- while an existing v1 one only runs them. Both end up
                    -- with the same shape, which is the point.
                    prompt_id TEXT,
                    node_name TEXT,
                    project_id TEXT,
                    conversation_id TEXT,
                    request_id TEXT,
                    duration_ms INTEGER,
                    status TEXT,
                    collector_version TEXT,
                    parsing_confidence REAL NOT NULL DEFAULT 1.0
                );
                CREATE INDEX IF NOT EXISTS usage_prompt ON usage_events (prompt_id);
                CREATE INDEX IF NOT EXISTS usage_project ON usage_events (project);
                CREATE INDEX IF NOT EXISTS usage_model ON usage_events (agent, model);
                CREATE INDEX IF NOT EXISTS usage_ts ON usage_events (ts);
                CREATE INDEX IF NOT EXISTS usage_session ON usage_events (agent_session_id);
                CREATE TABLE IF NOT EXISTS file_cursors (
                    path TEXT PRIMARY KEY,
                    inode INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    offset INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    -- Which prompt the last read ended inside, so a turn
                    -- appended later still lands on the prompt that caused it.
                    carry_prompt_id TEXT
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
                    -- Normalised to the windows a subscription is metered on
                    -- ("5h" / "1w"), so the screen can ask for one by name
                    -- rather than string-matching a provider's own wording.
                    window TEXT,
                    PRIMARY KEY (agent, label)
                );
                -- A prompt someone typed. Preview is redacted and truncated;
                -- the hash is of the ORIGINAL text so two identical prompts
                -- group even when redaction rewrites them differently.
                CREATE TABLE IF NOT EXISTS prompts (
                    prompt_id TEXT PRIMARY KEY,
                    agent_session_id TEXT NOT NULL,
                    ts REAL NOT NULL,
                    preview TEXT NOT NULL,
                    text_hash TEXT NOT NULL,
                    char_length INTEGER NOT NULL,
                    source TEXT,
                    project TEXT,
                    git_branch TEXT
                );
                CREATE INDEX IF NOT EXISTS prompt_ts ON prompts (ts);
                CREATE INDEX IF NOT EXISTS prompt_hash ON prompts (text_hash);
                -- Cost as the CLI computed it, never a price table of ours.
                CREATE TABLE IF NOT EXISTS session_costs (
                    agent_session_id TEXT PRIMARY KEY,
                    ts REAL NOT NULL,
                    total_cost_usd REAL NOT NULL,
                    per_model TEXT NOT NULL,
                    message_count INTEGER,
                    api_duration_ms INTEGER,
                    source TEXT NOT NULL
                );
                -- Quota over time, so a window, a reset and a peak can be
                -- reported later instead of only "right now".
                CREATE TABLE IF NOT EXISTS quota_snapshots (
                    taken_at REAL NOT NULL,
                    agent TEXT NOT NULL,
                    label TEXT NOT NULL,
                    state TEXT NOT NULL,
                    used_percent REAL,
                    resets_at REAL,
                    source TEXT NOT NULL,
                    window TEXT,
                    remaining_percent REAL,
                    -- What the subscription is, never who owns it: no email,
                    -- no org id, no token.
                    account_tier TEXT,
                    PRIMARY KEY (taken_at, agent, label)
                );
                CREATE INDEX IF NOT EXISTS quota_snap_ts ON quota_snapshots (taken_at);
                """
            )

    # Schema history. Each step is idempotent and additive: this database is
    # a derived index, but the events in it took real time to parse and the
    # cost rollups cannot be recomputed once a transcript rotates away, so it
    # is migrated rather than rebuilt.
    MIGRATIONS: tuple[tuple[int, str, str], ...] = (
        (2, "usage_events: prompt attribution, provider/cost/provenance columns",
         """
         ALTER TABLE usage_events ADD COLUMN prompt_id TEXT;
         ALTER TABLE usage_events ADD COLUMN node_name TEXT;
         ALTER TABLE usage_events ADD COLUMN project_id TEXT;
         ALTER TABLE usage_events ADD COLUMN conversation_id TEXT;
         ALTER TABLE usage_events ADD COLUMN request_id TEXT;
         ALTER TABLE usage_events ADD COLUMN duration_ms INTEGER;
         ALTER TABLE usage_events ADD COLUMN status TEXT;
         ALTER TABLE usage_events ADD COLUMN collector_version TEXT;
         ALTER TABLE usage_events ADD COLUMN parsing_confidence REAL NOT NULL DEFAULT 1.0;
         CREATE INDEX IF NOT EXISTS usage_prompt ON usage_events (prompt_id);
         CREATE INDEX IF NOT EXISTS usage_project ON usage_events (project);
         CREATE INDEX IF NOT EXISTS usage_model ON usage_events (agent, model);
         ALTER TABLE file_cursors ADD COLUMN carry_prompt_id TEXT;
         """),
        (3, "quota: normalised window, the remaining half, and the account tier",
         """
         ALTER TABLE quota_windows ADD COLUMN window TEXT;
         ALTER TABLE quota_snapshots ADD COLUMN window TEXT;
         ALTER TABLE quota_snapshots ADD COLUMN remaining_percent REAL;
         ALTER TABLE quota_snapshots ADD COLUMN account_tier TEXT;
         """),
    )

    COLLECTOR_VERSION = "2"

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # Either a brand-new database (the CREATE TABLEs below will make
            # it current) or a v1 one. Distinguish by whether v1's own table
            # already exists.
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_events'"
            ).fetchone()
            version = 1 if existing else max(step[0] for step in self.MIGRATIONS)
            if not existing:
                connection.execute(f"PRAGMA user_version = {version}")
                return
        for number, _label, script in self.MIGRATIONS:
            if number <= version:
                continue
            for statement in filter(None, (part.strip() for part in script.split(";"))):
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    message = str(exc)
                    # Two benign cases. Re-running an ALTER that already
                    # landed, and altering a table this version introduced --
                    # migrations run BEFORE the CREATE TABLE script, so on a
                    # v1 database quota_snapshots does not exist yet and is
                    # about to be created with these columns already in it.
                    if ("duplicate column name" not in message
                            and "no such table" not in message):
                        raise
            connection.execute(f"PRAGMA user_version = {number}")

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

    def _store_events(self, connection: sqlite3.Connection, events: Iterable[UsageEvent],
                      *, prompt_ids: list[str | None] | None = None) -> int:
        events = list(events)
        ids = prompt_ids or [None] * len(events)
        rows = [(e.event_id, e.agent, e.agent_session_id, e.node_id, e.timestamp, e.model,
                 e.input_tokens, e.output_tokens, e.cache_read_tokens, e.cache_write_tokens,
                 e.project, e.git_branch, int(e.is_subagent), e.cli_version, e.source,
                 prompt, self.COLLECTOR_VERSION)
                for e, prompt in zip(events, ids)]
        if not rows:
            return 0
        before = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        connection.executemany(
            """INSERT OR IGNORE INTO usage_events
               (event_id, agent, agent_session_id, node_id, ts, model,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                project, git_branch, is_subagent, cli_version, source,
                prompt_id, collector_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        after = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        return after - before

    def _store_prompts(self, connection: sqlite3.Connection,
                       prompts: Iterable[Any]) -> int:
        rows = [(p.prompt_id, p.agent_session_id, p.timestamp, p.preview, p.text_hash,
                 p.char_length, p.source, p.project, p.git_branch) for p in prompts]
        if not rows:
            return 0
        before = connection.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]
        connection.executemany(
            """INSERT OR IGNORE INTO prompts
               (prompt_id, agent_session_id, ts, preview, text_hash, char_length,
                source, project, git_branch) VALUES (?,?,?,?,?,?,?,?,?)""", rows)
        return connection.execute("SELECT COUNT(*) FROM prompts").fetchone()[0] - before

    def _store_costs(self, connection: sqlite3.Connection, costs: Iterable[Any]) -> int:
        import json as _json
        rows = [(c.agent_session_id, c.timestamp, c.total_cost_usd,
                 _json.dumps(c.per_model), c.message_count, c.api_duration_ms,
                 "session_transcript") for c in costs]
        if not rows:
            return 0
        # A later cost-state supersedes an earlier one for the same session:
        # it is a running total, not an increment.
        connection.executemany(
            """INSERT INTO session_costs
               (agent_session_id, ts, total_cost_usd, per_model, message_count,
                api_duration_ms, source) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(agent_session_id) DO UPDATE SET
                 ts=excluded.ts, total_cost_usd=excluded.total_cost_usd,
                 per_model=excluded.per_model, message_count=excluded.message_count,
                 api_duration_ms=excluded.api_duration_ms""", rows)
        return len(rows)

    def _carry_prompt(self, connection: sqlite3.Connection, path: Path) -> str | None:
        """Which prompt the previous incremental read ended inside.

        Without it, a turn appended after the offset would be attributed to
        no prompt at all, and the Top Prompts table would quietly lose the
        most recent -- and usually most interesting -- work.
        """
        row = connection.execute(
            "SELECT carry_prompt_id FROM file_cursors WHERE path = ?", (str(path),)).fetchone()
        return row["carry_prompt_id"] if row else None

    def _remember_carry(self, connection: sqlite3.Connection, path: Path,
                        prompt_id: str | None) -> None:
        connection.execute(
            """INSERT INTO file_cursors (path, inode, size, offset, updated_at, carry_prompt_id)
               VALUES (?, 0, 0, 0, 0, ?)
               ON CONFLICT(path) DO UPDATE SET carry_prompt_id = excluded.carry_prompt_id""",
            (str(path), prompt_id))

    def refresh(self, *, node_id: str = "local", claude_home: Path | None = None,
                codex_home: Path | None = None) -> dict[str, Any]:
        """Read whatever is new and return what happened.

        Deliberately reports `unparsed` and `files_rescanned` rather than
        swallowing them: a parser that silently drops what it cannot read is
        indistinguishable from one with nothing to read.
        """
        started = time.time()
        summary = {"files_seen": 0, "files_read": 0, "files_rescanned": 0,
                   "events_new": 0, "prompts_new": 0, "costs_new": 0,
                   "lines_read": 0, "unparsed": 0, "agents": {}}
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
                    if agent == AGENT_CLAUDE:
                        carry = self._carry_prompt(connection, path) if start else None
                        outcome = parse_claude_transcript(path, node_id=node_id,
                                                          start_offset=start,
                                                          carry_prompt_id=carry)
                        prompt_ids = outcome.event_prompt_ids
                        summary["prompts_new"] += self._store_prompts(connection, outcome.prompts)
                        summary["costs_new"] += self._store_costs(connection, outcome.costs)
                        self._remember_carry(connection, path, outcome.last_prompt_id)
                    else:
                        outcome = parse_jsonl(path, node_id=node_id, agent=agent,
                                              start_offset=start, session_id=path.stem)
                        prompt_ids = [None] * len(outcome.events)
                    summary["files_read"] += 1
                    summary["lines_read"] += outcome.lines_read
                    summary["unparsed"] += outcome.unparsed
                    inserted = self._store_events(connection, outcome.events,
                                                  prompt_ids=prompt_ids)
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
        # An index carried over from the v1 collector has events whose bytes
        # were already read, so nothing above will ever attribute them.
        filled = self.backfill(claude_home=claude_home, node_id=node_id)
        if filled.get("events_attributed"):
            summary["backfilled"] = filled
        summary["duration_seconds"] = round(time.time() - started, 3)
        return summary

    def backfill(self, *, claude_home: Path | None = None,
                 node_id: str = "local") -> dict[str, Any]:
        """Give already-indexed events the prompts and costs they predate.

        An index built by the v1 collector holds events with no prompt
        attribution and no cost, and its file cursors already sit at EOF --
        so an ordinary refresh appends nothing and those events stay
        unassigned forever. Measured on the production index before this
        existed: 96.79% of all tokens attributed to "unassigned", and no
        cost at all.

        Re-reads each transcript from the start and UPDATEs by event id
        rather than inserting, so it cannot duplicate a single event no
        matter how many times it runs. Only fills what is missing: an event
        that already has a prompt keeps it.
        """
        started = time.time()
        summary = {"files": 0, "events_attributed": 0, "prompts_new": 0, "costs_new": 0}
        with self._connect() as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM usage_events WHERE agent = ? AND prompt_id IS NULL",
                (AGENT_CLAUDE,)).fetchone()[0]
            if not pending:
                summary["skipped"] = "every event already carries its attribution"
                return summary
            for path in discover_claude_transcripts(claude_home):
                summary["files"] += 1
                outcome = parse_claude_transcript(path, node_id=node_id, start_offset=0)
                summary["prompts_new"] += self._store_prompts(connection, outcome.prompts)
                summary["costs_new"] += self._store_costs(connection, outcome.costs)
                updates = [(prompt, event.event_id)
                           for event, prompt in zip(outcome.events, outcome.event_prompt_ids)
                           if prompt]
                if updates:
                    # total_changes is cumulative for the whole connection, so
                    # using it here reported 11,961 rows updated for 3,700
                    # events. Counting the rowcount of this statement alone is
                    # what the summary is claiming to be.
                    cursor = connection.executemany(
                        "UPDATE usage_events SET prompt_id = ? "
                        "WHERE event_id = ? AND prompt_id IS NULL", updates)
                    summary["events_attributed"] += max(cursor.rowcount, 0)
                self._remember_carry(connection, path, outcome.last_prompt_id)
        summary["duration_seconds"] = round(time.time() - started, 3)
        return summary

    def _refresh_quota(self, connection: sqlite3.Connection,
                       *, codex_home: Path | None = None) -> None:
        now = time.time()
        # Both metered windows, always, for both providers. A window that
        # nothing measured is still a row: an absent one is indistinguishable
        # from one nobody looked at.
        windows: list[tuple[str, QuotaWindow]] = [
            (AGENT_CLAUDE, unobserved_window("5h", CLAUDE_QUOTA_UNOBSERVED)),
            (AGENT_CLAUDE, unobserved_window("1w", CLAUDE_QUOTA_UNOBSERVED))]
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
            windows.extend([(AGENT_CODEX, unobserved_window("5h", detail)),
                            (AGENT_CODEX, unobserved_window("1w", detail))])
        connection.execute("DELETE FROM quota_windows")
        connection.executemany(
            """INSERT OR REPLACE INTO quota_windows
               (agent, label, observed, source, used_percent, resets_at, detail,
                updated_at, window)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [(agent, w.label, int(w.observed), w.source, w.used_percent,
              w.resets_at, w.detail, now, _normalise_window(w.label))
             for agent, w in windows])

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
            quota = []
            for row in connection.execute("SELECT * FROM quota_windows ORDER BY agent, label"):
                record = dict(row)
                record.setdefault("window", None)
                record["window"] = record["window"] or _normalise_window(record["label"])
                # Providers differ on which half they state; both are carried
                # so the screen never has to do the subtraction silently.
                record["remaining_percent"] = (None if record["used_percent"] is None
                                               else 100 - record["used_percent"])
                quota.append(record)

            # The prompt size of each session's MOST RECENT request. Summing a
            # session would answer a different question (total spend) and would
            # pass any window within a few turns; what the model actually held
            # is the last request's input + cache-read + cache-write.
            latest_context: dict[tuple[str, int], int] = {}
            for row in connection.execute(
                    """SELECT u.agent_session_id, u.is_subagent,
                              u.input_tokens + u.cache_read_tokens
                              + u.cache_write_tokens AS ctx
                       FROM usage_events u
                       WHERE u.ts = (SELECT MAX(ts) FROM usage_events x
                                     WHERE x.agent_session_id = u.agent_session_id
                                       AND x.is_subagent = u.is_subagent)"""):
                latest_context[(row["agent_session_id"],
                                int(row["is_subagent"] or 0))] = int(row["ctx"] or 0)

            # `message.model` drops the variant suffix that states the window,
            # but the cost block's per-model breakdown keeps it. Read the
            # provider's own structured record rather than inferring one.
            variant_ids: dict[str, list[str]] = {}
            try:
                for row in connection.execute(
                        "SELECT agent_session_id, per_model FROM session_costs"):
                    try:
                        variant_ids[row["agent_session_id"]] = list(
                            json.loads(row["per_model"] or "{}").keys())
                    except (TypeError, ValueError):
                        continue
            except sqlite3.Error:
                # An older database without session_costs still reports usage;
                # it simply cannot resolve a window, which the page renders as
                # unavailable rather than as a failure.
                variant_ids = {}

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
                # How full the model's context is -- the one percentage that is
                # computable locally. Provider quota % stays in quota_windows,
                # where it is honestly `unavailable`.
                "context": context_usage(
                    latest_context.get((row["agent_session_id"],
                                        int(row["is_subagent"] or 0))),
                    model=row["model"],
                    variant_ids=variant_ids.get(row["agent_session_id"], ())),
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


    # -- analytics ------------------------------------------------------------
    #
    # Aggregation is computed in SQL over the raw events rather than kept in
    # rollup tables. At this fleet's volume (thousands of events) a GROUP BY
    # is instant and always consistent with the raw data; materialised
    # rollups would add a second source of truth to keep correct for no gain
    # yet. The indexes on ts/project/model are what make that hold as the
    # table grows, and a rollup table is the documented next step if it
    # stops holding.

    BUCKETS = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800}

    def _where(self, filters: dict[str, Any] | None,
               *, alias: str = "") -> tuple[str, list[Any]]:
        """Filters shared by every analytics query, so the numbers on one
        screen always describe the same slice.

        `alias` qualifies every column. The prompts query joins usage_events
        to prompts and BOTH carry `ts`, so an unqualified clause raised
        "ambiguous column name: ts" and the whole Top Prompts table came back
        empty -- with a 200, because an analytics query failing closed is
        indistinguishable from having nothing to show.
        """
        filters = filters or {}
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if filters.get("since") is not None:
            clauses.append(f"{prefix}ts >= ?"); params.append(float(filters["since"]))
        if filters.get("until") is not None:
            clauses.append(f"{prefix}ts <= ?"); params.append(float(filters["until"]))
        # range_label is presentation metadata, never a filter column.
        for column, key in (("node_id", "node_id"), ("agent", "agent"),
                            ("model", "model"), ("project", "project"),
                            ("agent_session_id", "agent_session_id")):
            if filters.get(key):
                clauses.append(f"{prefix}{column} = ?"); params.append(filters[key])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    _TOKEN_SUMS = ("SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
                   "SUM(cache_read_tokens) AS cache_read_tokens, "
                   "SUM(cache_write_tokens) AS cache_write_tokens, "
                   "SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens) "
                   "AS total_tokens, COUNT(*) AS requests")

    def cost_for(self, *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Cost of the SELECTED RANGE, not of all time.

        This is the bug the card's label was hiding: `estimated_cost_usd` was
        `SUM(total_cost_usd) FROM session_costs` -- every session, every day,
        ignoring every filter -- while the screen showed it beside token
        counts that DID respect the range. Changing the label alone would
        have made a wrong number better documented.

        A `cost-state` entry is a RUNNING total for a whole session, so a
        range narrower than the session has to apportion. The obvious method
        -- divide costUSD by the token counts in that same entry to recover a
        per-token rate -- is wrong, and measurably so: those counts are the
        totals AT THE MOMENT the entry was written, which is smaller than
        what the session went on to spend, and the resulting rate produced
        $112 for a fleet whose CLI reports $12.20.

        So the session's reported cost is split by its share of that
        session's own tokens. The full range then sums back to exactly what
        the CLI reported, which is the property that makes the number
        checkable.
        """
        where, params = self._where(filters)
        with self._connect() as connection:
            costs = {row["agent_session_id"]: row["total_cost_usd"]
                     for row in connection.execute(
                         "SELECT agent_session_id, total_cost_usd FROM session_costs")}
            in_range = dict(connection.execute(
                f"""SELECT agent_session_id,
                           SUM(input_tokens + output_tokens + cache_read_tokens
                               + cache_write_tokens) AS tokens
                    FROM usage_events{where} GROUP BY agent_session_id""", params).fetchall())
            whole = dict(connection.execute(
                """SELECT agent_session_id,
                          SUM(input_tokens + output_tokens + cache_read_tokens
                              + cache_write_tokens) AS tokens
                   FROM usage_events GROUP BY agent_session_id""").fetchall())
            lifetime = connection.execute(
                "SELECT SUM(total_cost_usd) FROM session_costs").fetchone()[0]
        total = 0.0
        priced = 0
        unpriced_tokens = 0
        for session, tokens in in_range.items():
            session_cost = costs.get(session)
            session_tokens = whole.get(session) or 0
            if session_cost is None or session_tokens <= 0:
                # A session the CLI has not written a cost for yet -- a live
                # one, usually. Counted and reported, never guessed at.
                unpriced_tokens += tokens or 0
                continue
            total += session_cost * ((tokens or 0) / session_tokens)
            priced += 1
        return {
            "usd": round(total, 4) if priced else None,
            "lifetime_usd": lifetime,
            "method": ("the CLI's own per-session cost, split by that session's share of "
                       "tokens inside the selected range"),
            "source": "session_transcript (CLI-computed)" if priced else None,
            "unpriced_tokens": unpriced_tokens,
            "priced_sessions": priced,
        }

    def summary(self, *, now: float | None = None,
                filters: dict[str, Any] | None = None) -> dict[str, Any]:
        now = now or time.time()
        cost = self.cost_for(filters=filters)
        where, params = self._where(filters)
        with self._connect() as connection:
            totals = dict(connection.execute(
                f"SELECT {self._TOKEN_SUMS} FROM usage_events{where}", params).fetchone())
            spans = {}
            for label, seconds in (("1h", 3600), ("5h", ROLLING_WINDOW_SECONDS),
                                   ("24h", 86400), ("7d", 604800), ("30d", 2592000)):
                row = connection.execute(
                    f"SELECT {self._TOKEN_SUMS} FROM usage_events WHERE ts >= ?",
                    (now - seconds,)).fetchone()
                spans[label] = {k: (row[k] or 0) for k in row.keys()}
            cost_sessions = connection.execute(
                "SELECT COUNT(*) FROM session_costs").fetchone()[0]
            active = connection.execute(
                "SELECT COUNT(DISTINCT agent_session_id) FROM usage_events WHERE ts >= ?",
                (now - 86400,)).fetchone()[0]
            peak = connection.execute(
                f"""SELECT agent_session_id, project,
                           SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) AS total
                    FROM usage_events WHERE ts >= ? GROUP BY agent_session_id
                    ORDER BY total DESC LIMIT 1""", (now - 86400,)).fetchone()
            top_project = connection.execute(
                """SELECT project,
                          SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) AS total
                   FROM usage_events WHERE ts >= ? AND project IS NOT NULL
                   GROUP BY project ORDER BY total DESC LIMIT 1""", (now - 86400,)).fetchone()
        return {
            "generated_at": now,
            "totals": {k: (totals[k] or 0) for k in totals},
            "spans": spans,
            "estimated_cost_usd": cost["usd"],
            "cost_lifetime_usd": cost["lifetime_usd"],
            "cost_source": cost["source"],
            "cost_method": cost["method"],
            "cost_unpriced_tokens": cost["unpriced_tokens"],
            "cost_priced_sessions": cost["priced_sessions"],
            "cost_sessions": cost_sessions,
            # What the numbers on this payload actually describe, so the card
            # can name its own range instead of leaving the reader to assume.
            "range": {
                "label": (filters or {}).get("range_label"),
                "since": (filters or {}).get("since"),
                "until": (filters or {}).get("until"),
            },
            "active_sessions_24h": active,
            "peak_session_24h": (dict(peak) if peak else None),
            "top_project_24h": (dict(top_project) if top_project else None),
        }

    def timeline(self, *, bucket: str = "hour", now: float | None = None,
                 filters: dict[str, Any] | None = None) -> dict[str, Any]:
        seconds = self.BUCKETS.get(bucket)
        if seconds is None:
            raise ValueError(f"unknown bucket: {bucket!r}")
        where, params = self._where(filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT CAST(ts / {seconds} AS INTEGER) * {seconds} AS bucket_start,
                           {self._TOKEN_SUMS}
                    FROM usage_events{where}
                    GROUP BY bucket_start ORDER BY bucket_start""", params).fetchall()
        return {"bucket": bucket, "bucket_seconds": seconds,
                "points": [{k: row[k] for k in row.keys()} for row in rows]}

    def top_prompts(self, *, limit: int = 25, offset: int = 0,
                    filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Which prompts cost the most.

        A prompt's cost is every assistant turn attributed to it, which is
        what makes this answerable at all -- the transcript has no field
        linking a turn to a prompt, so attribution is positional and
        recorded at ingest.
        """
        where, params = self._where(filters)
        joined_where, joined_params = self._where(filters, alias="e")
        sums = self._TOKEN_SUMS.replace("SUM(", "SUM(e.")
        with self._connect() as connection:
            grand = connection.execute(
                f"SELECT SUM(input_tokens+output_tokens+cache_read_tokens+cache_write_tokens) "
                f"FROM usage_events{where}", params).fetchone()[0] or 0
            rows = connection.execute(
                f"""SELECT e.prompt_id, {sums},
                           MIN(e.ts) AS first_ts, MAX(e.ts) AS last_ts,
                           e.agent_session_id, e.project, e.agent, e.node_id,
                           GROUP_CONCAT(DISTINCT e.model) AS models,
                           p.preview, p.text_hash, p.char_length, p.ts AS prompt_ts,
                           p.git_branch
                    FROM usage_events e LEFT JOIN prompts p ON p.prompt_id = e.prompt_id
                    {joined_where}
                    GROUP BY e.prompt_id
                    ORDER BY total_tokens DESC LIMIT ? OFFSET ?""",
                joined_params + [limit, offset]).fetchall()
        items = []
        for row in rows:
            record = {k: row[k] for k in row.keys()}
            record["share_percent"] = (round(record["total_tokens"] / grand * 100, 2)
                                       if grand else 0.0)
            record["duration_seconds"] = (record["last_ts"] - record["first_ts"]
                                          if record["first_ts"] else 0)
            if record.get("preview") is None:
                # A turn with no prompt attributed: shown as unassigned rather
                # than guessed at.
                record["preview"] = None
                record["unassigned"] = record["prompt_id"] is None
            items.append(record)
        return {"items": items, "limit": limit, "offset": offset, "grand_total": grand}

    def top_sessions(self, *, now: float | None = None, limit: int = 50,
                     filters: dict[str, Any] | None = None) -> dict[str, Any]:
        now = now or time.time()
        where, params = self._where(filters)
        spans = {"5h": ROLLING_WINDOW_SECONDS, "24h": 86400, "7d": 604800, "30d": 2592000}
        span_sql = ", ".join(
            f"SUM(CASE WHEN ts >= {now - seconds} THEN "
            f"input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) "
            f"AS tokens_{label}" for label, seconds in spans.items())
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT agent, agent_session_id, node_id, project, is_subagent,
                           GROUP_CONCAT(DISTINCT model) AS models,
                           {self._TOKEN_SUMS}, {span_sql},
                           MAX(ts) AS last_activity, MIN(ts) AS first_seen
                    FROM usage_events{where}
                    GROUP BY agent, agent_session_id, is_subagent
                    ORDER BY tokens_24h DESC, total_tokens DESC LIMIT ?""",
                params + [limit]).fetchall()
            costs: dict[str, Any] = {}
            variant_ids: dict[str, list[str]] = {}
            for r in connection.execute("SELECT * FROM session_costs"):
                costs[r["agent_session_id"]] = r["total_cost_usd"]
                try:
                    variant_ids[r["agent_session_id"]] = list(
                        json.loads(r["per_model"] or "{}").keys())
                except (TypeError, ValueError, IndexError):
                    continue
            # Context fullness is a property of the LATEST request, not of the
            # session total -- see ai_context_window for why summing answers a
            # different question.
            latest_context = {
                (r["agent_session_id"], int(r["is_subagent"] or 0)): int(r["ctx"] or 0)
                for r in connection.execute(
                    """SELECT u.agent_session_id, u.is_subagent,
                              u.input_tokens + u.cache_read_tokens
                              + u.cache_write_tokens AS ctx
                       FROM usage_events u
                       WHERE u.ts = (SELECT MAX(ts) FROM usage_events x
                                     WHERE x.agent_session_id = u.agent_session_id
                                       AND x.is_subagent = u.is_subagent)""")}
        items = []
        for row in rows:
            record = {k: row[k] for k in row.keys()}
            record["is_subagent"] = bool(record["is_subagent"])
            record["estimated_cost_usd"] = costs.get(record["agent_session_id"])
            record["avg_tokens_per_request"] = (
                round(record["total_tokens"] / record["requests"]) if record["requests"] else 0)
            # `models` here is a GROUP_CONCAT of every model the session used;
            # the window belongs to the one it is running now, which is the
            # last id in that list.
            newest_model = (record.get("models") or "").split(",")[-1].strip() or None
            record["context"] = context_usage(
                latest_context.get((record["agent_session_id"],
                                    int(row["is_subagent"] or 0))),
                model=newest_model,
                variant_ids=variant_ids.get(record["agent_session_id"], ()))
            items.append(record)
        return {"items": items, "limit": limit}

    def top_projects(self, *, now: float | None = None, limit: int = 50,
                     filters: dict[str, Any] | None = None) -> dict[str, Any]:
        now = now or time.time()
        where, params = self._where(filters)
        spans = {"24h": 86400, "7d": 604800, "30d": 2592000}
        span_sql = ", ".join(
            f"SUM(CASE WHEN ts >= {now - seconds} THEN "
            f"input_tokens+output_tokens+cache_read_tokens+cache_write_tokens ELSE 0 END) "
            f"AS tokens_{label}" for label, seconds in spans.items())
        # Previous 7d, so a trend is a comparison rather than a feeling.
        prev_sql = (f"SUM(CASE WHEN ts >= {now - 1209600} AND ts < {now - 604800} THEN "
                    f"input_tokens+output_tokens+cache_read_tokens+cache_write_tokens "
                    f"ELSE 0 END) AS tokens_prev_7d")
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT COALESCE(project, 'unassigned') AS project,
                           COUNT(DISTINCT agent_session_id) AS sessions,
                           GROUP_CONCAT(DISTINCT model) AS models,
                           {self._TOKEN_SUMS}, {span_sql}, {prev_sql},
                           MAX(ts) AS last_activity
                    FROM usage_events{where}
                    GROUP BY COALESCE(project, 'unassigned')
                    ORDER BY tokens_7d DESC, total_tokens DESC LIMIT ?""",
                params + [limit]).fetchall()
        items = []
        for row in rows:
            record = {k: row[k] for k in row.keys()}
            previous = record.pop("tokens_prev_7d") or 0
            current = record["tokens_7d"] or 0
            record["trend_percent"] = (round((current - previous) / previous * 100, 1)
                                       if previous else None)
            items.append(record)
        return {"items": items, "limit": limit}

    def model_breakdown(self, *, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        where, params = self._where(filters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT agent, COALESCE(model, 'unknown') AS model, {self._TOKEN_SUMS}
                    FROM usage_events{where}
                    GROUP BY agent, COALESCE(model, 'unknown')
                    ORDER BY total_tokens DESC""", params).fetchall()
            per_model_cost: dict[str, float] = {}
            import json as _json
            for row in connection.execute("SELECT per_model FROM session_costs"):
                try:
                    for model, usage in (_json.loads(row["per_model"]) or {}).items():
                        cost = usage.get("costUSD")
                        if not isinstance(cost, (int, float)):
                            continue
                        # cost-state names a model with its context variant
                        # ("claude-opus-5[1m]") while an assistant turn names
                        # the base model ("claude-opus-5"). Joining on the raw
                        # string silently produced no cost at all for the
                        # model doing all the work.
                        key = model.split("[", 1)[0]
                        per_model_cost[key] = per_model_cost.get(key, 0.0) + float(cost)
                except (ValueError, AttributeError):
                    continue
        grand = sum(row["total_tokens"] or 0 for row in rows) or 0
        items = []
        for row in rows:
            record = {k: row[k] for k in row.keys()}
            record["share_percent"] = (round(record["total_tokens"] / grand * 100, 2)
                                       if grand else 0.0)
            record["estimated_cost_usd"] = per_model_cost.get(record["model"])
            items.append(record)
        return {"items": items, "grand_total": grand}

    def raw_events(self, *, limit: int = 100, offset: int = 0,
                   filters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Paginated, because a raw event feed without a bound is a way to
        make a dashboard poll expensive."""
        limit = max(1, min(int(limit), 500))
        where, params = self._where(filters)
        with self._connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM usage_events{where}", params).fetchone()[0]
            rows = connection.execute(
                f"""SELECT * FROM usage_events{where}
                    ORDER BY ts DESC LIMIT ? OFFSET ?""", params + [limit, offset]).fetchall()
        return {"items": [{k: row[k] for k in row.keys()} for row in rows],
                "total": total, "limit": limit, "offset": offset,
                "has_more": offset + limit < total}

    def record_quota_snapshot(self, *, now: float | None = None) -> int:
        """Append the current windows to history.

        Without this, quota is only ever "right now"; with it, a later report
        can show when a window filled and when it reset.
        """
        now = now or time.time()
        with self._connect() as connection:
            windows = connection.execute("SELECT * FROM quota_windows").fetchall()
            tier = _account_tier()
            rows = [(now, w["agent"], w["label"],
                     "provider_reported" if w["observed"] else "unavailable",
                     w["used_percent"], w["resets_at"], w["source"],
                     _normalise_window(w["label"]),
                     (None if w["used_percent"] is None else 100 - w["used_percent"]),
                     tier) for w in windows]
            connection.executemany(
                """INSERT OR IGNORE INTO quota_snapshots
                   (taken_at, agent, label, state, used_percent, resets_at, source,
                    window, remaining_percent, account_tier)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""", rows)
        return len(rows)

    def quota_history(self, *, since: float | None = None, limit: int = 500) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM quota_snapshots WHERE taken_at >= ?
                   ORDER BY taken_at DESC LIMIT ?""",
                (since or 0.0, limit)).fetchall()
        return {"items": [{k: row[k] for k in row.keys()} for row in rows]}


def _normalise_window(label: str | None) -> str | None:
    """A provider's own wording mapped to the window it means.

    Codex says "primary"/"secondary", Claude's subscription is metered on a
    five-hour and a weekly window. The screen asks for "5h" or "1w"; this is
    the one place that translation lives.
    """
    if not label:
        return None
    text = str(label).strip().lower()
    if text in ("5h", "five_hour", "five-hour", "primary", "subscription"):
        return "5h"
    if text in ("1w", "week", "weekly", "seven_day", "secondary"):
        return "1w"
    return None


def _account_tier() -> str | None:
    """The subscription TYPE, read from the CLI's own auth status.

    Deliberately only the tier: that command also prints an email and an org
    id, and neither belongs in a usage index. Never the credential file.
    """
    import subprocess

    try:
        result = subprocess.run(["claude", "auth", "status", "--json"],
                                capture_output=True, text=True, timeout=8, check=False)
        if result.returncode != 0:
            return None
        import json as _json

        return _json.loads(result.stdout).get("subscriptionType")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _local_midnight(now: float) -> float:
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0,
                        local.tm_wday, local.tm_yday, local.tm_isdst))
