"""AI usage read from what the CLIs already wrote on this machine.

NO PROVIDER API IS CALLED, EVER. Not for tokens, not for quota, not for a
header. Every number here comes from a file a coding CLI wrote for its own
purposes, which means this page costs nothing and works offline.

WHAT THE DATA ACTUALLY LOOKS LIKE (measured on this host, not assumed)
---------------------------------------------------------------------
Claude Code writes one JSONL transcript per session under
`~/.claude/projects/<slug>/<sessionId>.jsonl`. Every assistant turn carries
real usage metadata:

    {"type":"assistant","uuid":"...","timestamp":"2026-09-09T00:13:02.337Z",
     "sessionId":"...","requestId":"req_...","isSidechain":false,
     "cwd":"...","gitBranch":"...","version":"2.1.266",
     "message":{"model":"claude-opus-5","usage":{
        "input_tokens":2,"output_tokens":314,
        "cache_read_input_tokens":26448,"cache_creation_input_tokens":9118}}}

and `~/.claude/sessions/<pid>.json` maps an OS pid to that sessionId, the
cwd, the CLI version and -- the useful part for this fleet -- the tmux
session it is running in (`"tmux": "m1:@0.%0"`). That is how a usage row
gets attributed to a real session without guessing.

RESET TIME: WHAT IS AND IS NOT HERE
-----------------------------------
Searched every JSONL and every JSON state file under ~/.claude for any key
matching reset / rate.?limit / quota / window / remaining / utilization.
Result: **nothing**, except `claudeAiOauth.rateLimitTier` inside
`.credentials.json`, which is a credential file this code never opens and
which names a tier, not a reset instant.

So for Claude this build reports the subscription window as **not
observed**. It does NOT invent "five hours from session start" -- that is a
guess dressed as data, and a wrong reset time is worse than an absent one
because it gets trusted. The rolling 5h figure this page does show is a
different thing and is labelled as such: token activity in the last five
hours, computed from transcript timestamps.

Codex writes rate-limit metadata into its own rollout logs on hosts where
it runs. The adapter below reads it when present. This host has no
`~/.codex` at all, so Codex reports as unavailable rather than empty --
"no data source" and "a source that says zero" are different answers.

VERSION TOLERANCE
-----------------
Formats move. Every extractor here probes several shapes and takes the
first that yields numbers, rather than asserting one layout; an entry it
cannot read is counted as `unparsed` and surfaced, never silently dropped.

PRIVACY
-------
Prompt and response text are never read into memory beyond the JSON parse,
never stored, and never returned. `history.jsonl` (which does contain
prompt text) is not opened at all. Only counters, identifiers, timestamps
and model names leave this module.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"

# Where a metric came from, carried per row so a reader can tell measured
# from missing without reading this docstring.
SOURCE_TRANSCRIPT = "session_transcript"
SOURCE_CLI_STATE = "local_cli_state"
SOURCE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class UsageEvent:
    """One assistant turn's token cost, attributed as far as the local data
    allows. `event_id` is what makes re-reading a file idempotent."""

    event_id: str
    agent: str
    agent_session_id: str
    node_id: str
    timestamp: float
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    project: str | None = None
    git_branch: str | None = None
    is_subagent: bool = False
    cli_version: str | None = None
    source: str = SOURCE_TRANSCRIPT

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_read_tokens + self.cache_write_tokens)


@dataclass
class ParseOutcome:
    events: list[UsageEvent] = field(default_factory=list)
    lines_read: int = 0
    unparsed: int = 0
    end_offset: int = 0


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _iso_to_epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Milliseconds if it is far too large to be seconds.
        return float(value) / 1000.0 if value > 1e11 else float(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# -- Claude -------------------------------------------------------------------

# Probed in order; the first shape that yields a dict of counters wins.
_CLAUDE_USAGE_PATHS: tuple[tuple[str, ...], ...] = (
    ("message", "usage"),
    ("usage",),
    ("response", "usage"),
)

_CLAUDE_FIELDS = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens"),
    "cache_read_tokens": ("cache_read_input_tokens", "cacheReadInputTokens",
                          "cache_read_tokens"),
    "cache_write_tokens": ("cache_creation_input_tokens", "cacheCreationInputTokens",
                           "cache_write_tokens"),
}


def _dig(entry: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = entry
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _pick(source: dict[str, Any], names: tuple[str, ...]) -> int:
    for name in names:
        if name in source:
            return _as_int(source[name])
    return 0


def claude_event(entry: dict[str, Any], *, node_id: str) -> UsageEvent | None:
    """One transcript line -> one UsageEvent, or None if it carries no usage.

    Most lines carry none: a 959-line transcript on this host held 334
    assistant turns among 13 other entry types. Returning None for the rest
    is the normal path, not a parse failure.
    """
    if entry.get("type") not in (None, "assistant"):
        return None
    usage = None
    for path in _CLAUDE_USAGE_PATHS:
        candidate = _dig(entry, path)
        if isinstance(candidate, dict):
            usage = candidate
            break
    if usage is None:
        return None
    counts = {field: _pick(usage, names) for field, names in _CLAUDE_FIELDS.items()}
    if not any(counts.values()):
        return None
    when = _iso_to_epoch(entry.get("timestamp"))
    if when is None:
        return None
    # uuid is per-line and stable across re-reads; requestId can repeat across
    # retries of one request, so it is the weaker key and only a fallback.
    event_id = entry.get("uuid") or entry.get("requestId")
    if not event_id:
        return None
    message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
    return UsageEvent(
        event_id=str(event_id),
        agent=AGENT_CLAUDE,
        agent_session_id=str(entry.get("sessionId") or entry.get("session_id") or ""),
        node_id=node_id,
        timestamp=when,
        model=message.get("model") or entry.get("model"),
        project=entry.get("cwd"),
        git_branch=entry.get("gitBranch"),
        is_subagent=bool(entry.get("isSidechain")),
        cli_version=entry.get("version"),
        **counts,
    )


# -- Codex --------------------------------------------------------------------

# Codex rollout logs are not present on this host, so these shapes are
# probed rather than asserted, and an unreadable entry is counted as
# `unparsed` and reported. The adapter exists so a host that DOES run Codex
# is read correctly without a code change; it is covered by fixtures.
_CODEX_USAGE_PATHS: tuple[tuple[str, ...], ...] = (
    ("info", "total_token_usage"),
    ("info", "last_token_usage"),
    ("payload", "info", "total_token_usage"),
    ("token_usage",),
    ("usage",),
)

_CODEX_FIELDS = {
    "input_tokens": ("input_tokens", "prompt_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "completion_tokens", "outputTokens"),
    "cache_read_tokens": ("cached_input_tokens", "cache_read_input_tokens",
                          "cache_read_tokens", "cachedInputTokens"),
    "cache_write_tokens": ("cache_creation_input_tokens", "cache_write_tokens"),
}


def codex_event(entry: dict[str, Any], *, node_id: str,
                session_id: str = "") -> UsageEvent | None:
    usage = None
    for path in _CODEX_USAGE_PATHS:
        candidate = _dig(entry, path)
        if isinstance(candidate, dict):
            usage = candidate
            break
    if usage is None:
        return None
    counts = {field: _pick(usage, names) for field, names in _CODEX_FIELDS.items()}
    if not any(counts.values()):
        return None
    when = _iso_to_epoch(entry.get("timestamp") or entry.get("ts") or entry.get("time"))
    if when is None:
        return None
    event_id = (entry.get("id") or entry.get("event_id") or entry.get("uuid")
                or f"{session_id}:{when}:{counts['output_tokens']}")
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
    return UsageEvent(
        event_id=str(event_id),
        agent=AGENT_CODEX,
        agent_session_id=str(entry.get("session_id") or session_id or ""),
        node_id=node_id,
        timestamp=when,
        model=entry.get("model") or payload.get("model"),
        project=entry.get("cwd") or payload.get("cwd"),
        cli_version=entry.get("version"),
        **counts,
    )


@dataclass(frozen=True)
class QuotaWindow:
    """A subscription window the CLI itself recorded. Never computed here.

    `observed` False means exactly that -- no local artefact reported one --
    and every field is None. It is deliberately impossible to construct a
    reset time from a session start in this module.
    """

    label: str
    observed: bool
    source: str
    used_percent: float | None = None
    resets_at: float | None = None
    seconds_remaining: float | None = None
    detail: str | None = None


_RATE_LIMIT_PATHS: tuple[tuple[str, ...], ...] = (
    ("rate_limits",),
    ("info", "rate_limits"),
    ("payload", "rate_limits"),
)


def codex_quota_windows(entry: dict[str, Any], *, now: float) -> list[QuotaWindow]:
    """Rate-limit windows Codex recorded, if this entry carries any."""
    limits = None
    for path in _RATE_LIMIT_PATHS:
        candidate = _dig(entry, path)
        if isinstance(candidate, dict):
            limits = candidate
            break
    if not limits:
        return []
    windows: list[QuotaWindow] = []
    for label, payload in limits.items():
        if not isinstance(payload, dict):
            continue
        used = payload.get("used_percent", payload.get("usedPercent"))
        resets_in = payload.get("resets_in_seconds", payload.get("resetsInSeconds"))
        resets_at = _iso_to_epoch(payload.get("resets_at") or payload.get("resetsAt"))
        if resets_at is None and isinstance(resets_in, (int, float)):
            # The CLI recorded a countdown, not an instant; anchoring it to
            # the entry's own timestamp is arithmetic on observed data, not
            # an assumption about window length.
            anchor = _iso_to_epoch(entry.get("timestamp") or entry.get("ts")) or now
            resets_at = anchor + float(resets_in)
        windows.append(QuotaWindow(
            label=str(label),
            observed=True,
            source=SOURCE_CLI_STATE,
            used_percent=float(used) if isinstance(used, (int, float)) else None,
            resets_at=resets_at,
            seconds_remaining=(resets_at - now) if resets_at is not None else None,
        ))
    return windows


def unobserved_window(label: str, detail: str) -> QuotaWindow:
    return QuotaWindow(label=label, observed=False, source=SOURCE_UNAVAILABLE, detail=detail)


CLAUDE_QUOTA_UNOBSERVED = (
    "Claude Code records no rate-limit or reset metadata in its local "
    "transcripts or state files on this machine. Reported as not observed "
    "rather than estimated."
)


# -- incremental reading ------------------------------------------------------

def parse_jsonl(path: Path, *, node_id: str, agent: str, start_offset: int = 0,
                session_id: str = "") -> ParseOutcome:
    """Read a transcript from `start_offset` to EOF.

    Byte offsets, not line numbers: these files are appended to constantly,
    and re-reading 13MB on every refresh to recount what has not changed is
    the thing this exists to avoid. A truncated or rotated file is handled
    by the caller (see `FileCursor.resume_offset`), not here.
    """
    outcome = ParseOutcome(end_offset=start_offset)
    make = claude_event if agent == AGENT_CLAUDE else codex_event
    try:
        with path.open("rb") as handle:
            handle.seek(start_offset)
            for raw in handle:
                if not raw.endswith(b"\n"):
                    # A partial final line: the CLI is mid-write. Stop before
                    # it and leave the offset where the complete data ended,
                    # so the rest is read once it is whole.
                    break
                outcome.end_offset += len(raw)
                text = raw.strip()
                if not text:
                    continue
                outcome.lines_read += 1
                try:
                    entry = json.loads(text)
                except (ValueError, UnicodeDecodeError):
                    outcome.unparsed += 1
                    continue
                if not isinstance(entry, dict):
                    outcome.unparsed += 1
                    continue
                try:
                    event = (make(entry, node_id=node_id, session_id=session_id)
                             if agent == AGENT_CODEX else make(entry, node_id=node_id))
                except Exception:  # noqa: BLE001 -- one bad line never stops a file
                    outcome.unparsed += 1
                    continue
                if event is not None:
                    outcome.events.append(event)
    except OSError:
        return outcome
    return outcome


@dataclass(frozen=True)
class FileCursor:
    """Where a file was last read to, and enough identity to know whether it
    is still the same file."""

    path: str
    inode: int
    size: int
    offset: int

    @staticmethod
    def of(path: Path) -> "FileCursor | None":
        try:
            stat = path.stat()
        except OSError:
            return None
        return FileCursor(str(path), stat.st_ino, stat.st_size, 0)

    def resume_offset(self, current: "FileCursor") -> int:
        """0 when the file is not the one we read before.

        Two cases, both real: a rotated file reuses the path with a new
        inode, and a truncated file keeps the inode but shrinks below the
        offset. Either way the old offset points at the wrong bytes, so the
        file is read from the start and event ids deduplicate the overlap.
        """
        if current.inode != self.inode or current.size < self.offset:
            return 0
        return self.offset


# -- discovery ----------------------------------------------------------------

def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def discover_claude_transcripts(home: Path | None = None) -> list[Path]:
    root = (home or claude_home()) / "projects"
    if not root.is_dir():
        return []
    return sorted(root.rglob("*.jsonl"))


def discover_codex_logs(home: Path | None = None) -> list[Path]:
    root = home or codex_home()
    if not root.is_dir():
        return []
    found: list[Path] = []
    for sub in ("sessions", "logs", "history"):
        directory = root / sub
        if directory.is_dir():
            found.extend(sorted(directory.rglob("*.jsonl")))
    return found


def claude_session_links(home: Path | None = None) -> dict[str, dict[str, Any]]:
    """agent session id -> what the CLI recorded about where it runs.

    `~/.claude/sessions/<pid>.json` is written by Claude Code itself and
    carries the pid, the cwd, the CLI version and the tmux target
    (`"m1:@0.%0"`). That last field is what lets a usage row name the tmux
    session it belongs to instead of guessing from a path.
    """
    root = (home or claude_home()) / "sessions"
    links: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return links
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        session_id = data.get("sessionId")
        if not session_id:
            continue
        tmux = data.get("tmux")
        links[str(session_id)] = {
            "pid": data.get("pid"),
            "cwd": data.get("cwd"),
            "cli_version": data.get("version"),
            "tmux_target": tmux,
            # "m1:@0.%0" -> "m1"
            "tmux_session": tmux.split(":", 1)[0] if isinstance(tmux, str) and tmux else None,
            "started_at": _iso_to_epoch(data.get("startedAt")),
            "kind": data.get("kind"),
        }
    return links
