"""Work telemetry: what a task actually cost, recorded without invention.

The rule that shapes every line here: **a number we do not have is reported
as not having it.** Token counts are only as good as what the runtime hands
back, and a plausible-looking estimate presented as a measurement corrupts
every efficiency decision made from it afterwards -- including the decision
about whether this system is working at all.

So a token count always travels with its provenance: EXACT when a provider
reported it, ESTIMATED when it was derived from text (with the method named),
UNAVAILABLE when nothing reported it. Aggregates refuse to mix provenances
silently: a total over partly-estimated inputs is itself marked estimated,
and one with missing inputs says how many were missing.

Everything else here is counted, not guessed: files read, search rounds,
runbook hits and misses, redefine count, and the wall-clock to first preview.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .schema import Migration, apply_migrations

SCHEMA_VERSION = 1

EXACT = "EXACT"
ESTIMATED = "ESTIMATED"
UNAVAILABLE = "UNAVAILABLE"
# An aggregate over rows where some reported nothing. Distinct from EXACT:
# the arithmetic is exact, but the TOTAL is not the fleet's usage, and a
# reader keying on `source` must not mistake one for the other.
PARTIAL = "PARTIAL"

# Rough bytes-per-token, used ONLY when explicitly asked for an estimate and
# always labelled as one. It is a crude constant on purpose: a more elaborate
# approximation would invite the reader to trust it as a measurement.
_BYTES_PER_TOKEN = 4.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_telemetry_db_path(state_home: str | None = None) -> Path:
    override = os.environ.get("TERMINAL_MCP_TELEMETRY_DB")
    if override:
        return Path(override).expanduser()
    base = Path(state_home).expanduser() if state_home else (
        Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")))
    return Path(base) / "terminal-mcp" / "work_telemetry.db"


@dataclass(frozen=True)
class TokenCount:
    """A token number that cannot be read without also reading where it came from."""

    value: int | None = None
    source: str = UNAVAILABLE
    method: str = ""

    @classmethod
    def reported(cls, value: int, *, by: str) -> "TokenCount":
        """A count the runtime actually gave us."""
        return cls(value=int(value), source=EXACT, method=f"reported by {by}")

    @classmethod
    def estimate_from_text(cls, text: str) -> "TokenCount":
        """A crude estimate, labelled as one. Never presented as a measurement."""
        if not text:
            return cls(value=0, source=ESTIMATED, method="no text")
        return cls(value=int(len(text.encode("utf-8")) / _BYTES_PER_TOKEN),
                   source=ESTIMATED, method=f"len(utf-8)/{_BYTES_PER_TOKEN:g}")

    @classmethod
    def unavailable(cls, why: str = "runtime did not report token usage") -> "TokenCount":
        return cls(value=None, source=UNAVAILABLE, method=why)

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source, "method": self.method}

    def display(self) -> str:
        """How it should appear anywhere a human reads it."""
        if self.value is None:
            return f"unavailable ({self.method})" if self.method else "unavailable"
        if self.source == ESTIMATED:
            return f"~{self.value:,} (estimated)"
        return f"{self.value:,}"


@dataclass
class TaskTelemetry:
    """One task's cost. Counters are counted; tokens carry provenance."""

    telemetry_id: str = field(default_factory=lambda: f"tel_{uuid.uuid4().hex[:12]}")
    task_id: str | None = None
    work_id: str | None = None
    bug_id: str | None = None
    project_id: str | None = None
    module: str | None = None
    execution_mode: str | None = None
    spec_level: str | None = None
    difficulty: str | None = None

    files_read: int = 0
    search_rounds: int = 0
    runbook_hits: int = 0          # a registered procedure was reused
    runbook_misses: int = 0        # an operation was done by hand instead
    cache_hits: int = 0            # a green result reused rather than re-run
    redefine_count: int = 0
    assist_requests: int = 0
    tokens: TokenCount = field(default_factory=TokenCount.unavailable)

    started_at: str = field(default_factory=_now)
    first_preview_at: str | None = None
    finished_at: str | None = None
    outcome: str | None = None
    notes: tuple[str, ...] = ()

    # -- recording -----------------------------------------------------------

    def record_files(self, count: int = 1) -> None:
        self.files_read += count

    def record_search(self, rounds: int = 1) -> None:
        self.search_rounds += rounds

    def record_runbook(self, *, hit: bool, cached: bool = False) -> None:
        if hit:
            self.runbook_hits += 1
            if cached:
                self.cache_hits += 1
        else:
            self.runbook_misses += 1

    def record_redefine(self) -> None:
        self.redefine_count += 1

    def record_assist(self) -> None:
        self.assist_requests += 1

    def mark_preview(self, when: str | None = None) -> None:
        # First only: the interesting number is how long until something was
        # visible, not how many times it was rebuilt afterwards.
        self.first_preview_at = self.first_preview_at or (when or _now())

    def finish(self, outcome: str, *, when: str | None = None) -> None:
        self.finished_at = when or _now()
        self.outcome = outcome

    # -- derived -------------------------------------------------------------

    def seconds_to_preview(self) -> float | None:
        """None means it never previewed -- not zero, which would read as instant."""
        if not self.first_preview_at:
            return None
        return _elapsed(self.started_at, self.first_preview_at)

    def duration_seconds(self) -> float | None:
        if not self.finished_at:
            return None
        return _elapsed(self.started_at, self.finished_at)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tokens"] = self.tokens.as_dict()
        payload["notes"] = list(self.notes)
        payload["seconds_to_preview"] = self.seconds_to_preview()
        payload["duration_seconds"] = self.duration_seconds()
        return payload

    def one_line(self) -> str:
        """The compact summary, honest about what is missing."""
        preview = self.seconds_to_preview()
        parts = [
            f"files={self.files_read}",
            f"searches={self.search_rounds}",
            f"runbooks={self.runbook_hits}/{self.runbook_hits + self.runbook_misses}",
            f"redefines={self.redefine_count}",
            f"tokens={self.tokens.display()}",
            f"to_preview={'n/a' if preview is None else f'{preview:.0f}s'}",
        ]
        return f"{self.task_id or self.telemetry_id} :: " + " ".join(parts)


def _elapsed(start: str, end: str) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None


def summarise(records: Sequence[TaskTelemetry | dict[str, Any]]) -> dict[str, Any]:
    """Aggregate without laundering provenance.

    A total built partly from estimates is an estimate, and one built from
    incomplete data says so. The alternative -- a clean-looking number over
    whatever happened to be present -- is how a fleet convinces itself it is
    more efficient than it is.
    """
    rows = [r if isinstance(r, dict) else r.as_dict() for r in records]
    if not rows:
        return {"tasks": 0, "note": "no telemetry recorded yet"}

    def total(field_name: str) -> int:
        return sum(int(row.get(field_name) or 0) for row in rows)

    token_rows = [row.get("tokens") or {} for row in rows]
    counted = [t for t in token_rows if t.get("value") is not None]
    missing = len(token_rows) - len(counted)
    any_estimated = any(t.get("source") == ESTIMATED for t in counted)

    if not counted:
        tokens: dict[str, Any] = {"value": None, "source": UNAVAILABLE,
                                  "method": f"none of {len(rows)} tasks reported usage"}
    else:
        tokens = {
            "value": sum(int(t["value"]) for t in counted),
            "source": (ESTIMATED if any_estimated else PARTIAL if missing else EXACT),
            "complete": missing == 0,
            "method": ("sum of reported counts"
                       + (", some estimated" if any_estimated else "")
                       + (f"; {missing} of {len(rows)} tasks reported nothing"
                          if missing else "")),
            "covered_tasks": len(counted),
            "missing_tasks": missing,
        }

    previews = [row["seconds_to_preview"] for row in rows
                if row.get("seconds_to_preview") is not None]
    attempted = total("runbook_hits") + total("runbook_misses")
    return {
        "tasks": len(rows),
        "files_read": total("files_read"),
        "search_rounds": total("search_rounds"),
        "runbook_hits": total("runbook_hits"),
        "runbook_misses": total("runbook_misses"),
        "runbook_hit_rate": (round(total("runbook_hits") / attempted, 3)
                             if attempted else None),
        "cache_hits": total("cache_hits"),
        "redefine_count": total("redefine_count"),
        "assist_requests": total("assist_requests"),
        "tokens": tokens,
        "median_seconds_to_preview": (sorted(previews)[len(previews) // 2]
                                      if previews else None),
        "tasks_without_preview": len(rows) - len(previews),
    }


# Targets, stated as intent rather than measurement. They are compared
# against real aggregates; they never stand in for one.
EFFICIENCY_TARGETS = {
    "runbook_hit_rate": 0.80,
    "redefines_per_task": 0.20,
    "files_read_per_fast_fix": 5,
}


def against_targets(summary: dict[str, Any]) -> dict[str, Any]:
    """Compare an aggregate to intent, reporting UNKNOWN where data is absent."""
    tasks = summary.get("tasks") or 0
    out: dict[str, Any] = {}
    hit_rate = summary.get("runbook_hit_rate")
    out["runbook_hit_rate"] = (
        {"status": "UNKNOWN", "reason": "no runbook attempts recorded"}
        if hit_rate is None else
        {"status": "MEETS" if hit_rate >= EFFICIENCY_TARGETS["runbook_hit_rate"] else "BELOW",
         "actual": hit_rate, "target": EFFICIENCY_TARGETS["runbook_hit_rate"]})
    if tasks:
        per_task = round((summary.get("redefine_count") or 0) / tasks, 3)
        out["redefines_per_task"] = {
            "status": "MEETS" if per_task <= EFFICIENCY_TARGETS["redefines_per_task"] else "ABOVE",
            "actual": per_task, "target": EFFICIENCY_TARGETS["redefines_per_task"]}
    else:
        out["redefines_per_task"] = {"status": "UNKNOWN", "reason": "no tasks recorded"}
    token_summary = summary.get("tokens") or {}
    if token_summary.get("value") is None:
        out["tokens"] = {"status": "UNKNOWN",
                         "reason": token_summary.get("method")
                                   or "no token usage reported"}
    elif token_summary.get("source") in (PARTIAL, ESTIMATED):
        # Reported, but not a number to draw a conclusion from.
        out["tokens"] = {"status": "PARTIAL", "source": token_summary["source"],
                         "reason": token_summary.get("method", ""),
                         "covered_tasks": token_summary.get("covered_tasks"),
                         "missing_tasks": token_summary.get("missing_tasks")}
    return out


TELEMETRY_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: work_telemetry", lambda connection: None),
]


class TelemetryStore:
    """Durable telemetry, one row per task."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path is not None else default_telemetry_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS work_telemetry (
                    telemetry_id TEXT PRIMARY KEY,
                    task_id TEXT,
                    work_id TEXT,
                    bug_id TEXT,
                    project_id TEXT,
                    module TEXT,
                    payload TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                )""")
            for column in ("task_id", "work_id", "project_id"):
                self._connection.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_telemetry_{column} "
                    f"ON work_telemetry({column})")
        apply_migrations(self._connection, TELEMETRY_MIGRATIONS)

    def save(self, record: TaskTelemetry) -> TaskTelemetry:
        with self._connection:
            self._connection.execute(
                "INSERT INTO work_telemetry (telemetry_id, task_id, work_id, bug_id, "
                "project_id, module, payload, started_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(telemetry_id) DO UPDATE SET payload=excluded.payload, "
                "task_id=excluded.task_id, work_id=excluded.work_id, "
                "bug_id=excluded.bug_id, project_id=excluded.project_id, "
                "module=excluded.module, finished_at=excluded.finished_at",
                (record.telemetry_id, record.task_id, record.work_id, record.bug_id,
                 record.project_id, record.module,
                 json.dumps(record.as_dict(), ensure_ascii=False),
                 record.started_at, record.finished_at))
        return record

    def get(self, telemetry_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM work_telemetry WHERE telemetry_id=?",
            (telemetry_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def for_work(self, work_id: str) -> list[dict[str, Any]]:
        return [json.loads(row["payload"]) for row in self._connection.execute(
            "SELECT payload FROM work_telemetry WHERE work_id=? ORDER BY started_at",
            (work_id,))]

    def recent(self, *, limit: int = 100, project_id: str | None = None
               ) -> list[dict[str, Any]]:
        sql = "SELECT payload FROM work_telemetry"
        args: list[Any] = []
        if project_id:
            sql += " WHERE project_id=?"
            args.append(project_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        return [json.loads(row["payload"]) for row in self._connection.execute(sql, args)]

    def summary(self, *, limit: int = 200, project_id: str | None = None
                ) -> dict[str, Any]:
        rows = self.recent(limit=limit, project_id=project_id)
        report = summarise(rows)
        report["targets"] = against_targets(report)
        return report

    def close(self) -> None:
        self._connection.close()
