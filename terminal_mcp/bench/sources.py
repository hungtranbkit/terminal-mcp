"""Read-only adapters from whatever telemetry exists on this host to
`model.TaskRecord`.

Everything here is READ-ONLY and says so twice: every connection is
opened through SQLite's `mode=ro` URI *and* immediately issued `PRAGMA
query_only = 1`. This package owns no schema, creates no table, and
must stay safe to point at a live production database while the runtime
is writing to it.

Schema tolerance is the other half of the job. At the time this harness
was written the runtime telemetry it is meant to measure did not exist
yet on this host: `queue.db` has the right tables and zero rows,
`work.db` and `ai_usage.db` have not been created at all. Rather than
guess a schema and be silently wrong when the real one lands, every
adapter DISCOVERS what it is looking at -- it asks the database for its
tables and columns, picks the first recognised spelling from a list of
candidates, and reports through `SourceStatus` exactly what it found
and what it could not find. A column that is absent yields `None`, and
`None` means missing all the way to the report. Nothing here ever
substitutes zero for a number nobody recorded.

THE DOUBLE-COUNTING TRAP. Usage telemetry is written per turn, but
several real emitters (the Claude Code transcript format among them)
repeat the SAME usage block on several rows of the same turn, and some
emitters write a running CUMULATIVE total per turn instead of a delta.
Summing either shape inflates a task's cost -- measured on a real
transcript in this workspace, naive summation reported 1,034,241 output
tokens where the true figure was 542,382, a 1.9x overstatement. So no
adapter ever sums raw rows: they all fold through `UsageAggregator`,
which de-duplicates by turn identity in delta mode and takes the last
row (never a sum) in cumulative mode.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .model import (
    COHORT_LEGACY,
    COHORT_NEW,
    COHORT_UNKNOWN,
    Reentry,
    SourceStatus,
    TaskRecord,
    TaskUsage,
)

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "terminal-mcp"

# Usage folding modes -- see the module docstring's double-counting note.
USAGE_MODE_DELTA = "delta"
USAGE_MODE_CUMULATIVE = "cumulative"
USAGE_MODE_AUTO = "auto"

# Column-name spellings each adapter will accept, most specific first.
TASK_ID_COLUMNS = ("task_id", "id", "work_id", "job_id", "correlation_id")
INPUT_COLUMNS = ("input_tokens", "input", "prompt_tokens", "tokens_input")
OUTPUT_COLUMNS = ("output_tokens", "output", "completion_tokens", "tokens_output")
CACHE_READ_COLUMNS = (
    "cache_read_tokens",
    "cache_read_input_tokens",
    "cache_read",
    "cached_tokens",
)
CACHE_WRITE_5M_COLUMNS = (
    "cache_write_5m_tokens",
    "ephemeral_5m_input_tokens",
    "cache_creation_5m_tokens",
    "cache_write_5m",
)
CACHE_WRITE_1H_COLUMNS = (
    "cache_write_1h_tokens",
    "ephemeral_1h_input_tokens",
    "cache_creation_1h_tokens",
    "cache_write_1h",
)
CACHE_WRITE_COLUMNS = (
    "cache_write_tokens",
    "cache_creation_input_tokens",
    "cache_creation_tokens",
    "cache_write",
)
IDEMPOTENCY_COLUMNS = ("dispatch_idempotency_key", "idempotency_key", "dispatch_key")
TURN_ID_COLUMNS = ("turn_id", "request_id", "message_id", "event_id", "id")
SEQUENCE_COLUMNS = ("turn_index", "seq", "sequence", "turn", "created_at", "timestamp")
CUMULATIVE_MARKER_COLUMNS = ("is_cumulative", "cumulative", "running_total")
COHORT_COLUMNS = ("cohort", "pipeline", "pipeline_version", "arm", "variant")
PROFILE_COLUMNS = ("profile", "task_profile", "task_class", "risk", "risk_tier", "risk_profile")
COMPLEXITY_COLUMNS = ("complexity", "complexity_bucket", "size", "t_shirt_size")
PROJECT_COLUMNS = ("project", "project_id")
AGENT_COLUMNS = ("agent", "agent_type", "worker", "worker_type")
MODEL_COLUMNS = ("model", "model_id", "llm_model")
TURN_COUNT_COLUMNS = ("worker_turn_count", "turn_count", "turns", "assistant_turns")
RETRY_COLUMNS = ("retries", "retry_count", "attempt_count", "attempts")
DURATION_COLUMNS = ("duration_seconds", "duration_ms", "elapsed_seconds", "elapsed_ms")
FIRST_PASS_COLUMNS = ("first_pass_success", "first_pass", "passed_first_time")
REASON_COLUMNS = ("reason", "reason_code", "reentry_reason", "outcome_reason")

# Cohort spellings a source may use, normalised to the two model labels.
_LEGACY_ALIASES = {"legacy", "before", "baseline", "old", "control", "v0", "pre"}
_NEW_ALIASES = {
    "new",
    "new_pipeline",
    "newpipeline",
    "after",
    "candidate",
    "treatment",
    "v1",
    "post",
}


class SourceError(Exception):
    """A source exists but could not be read at all."""


# ---------------------------------------------------------------------------
# read-only sqlite plumbing
# ---------------------------------------------------------------------------


def open_readonly(path: str | os.PathLike[str]) -> tuple[sqlite3.Connection, Path | None]:
    """Open `path` strictly read-only.

    Returns the connection plus the temporary copy that had to be made,
    if any. A database mid-WAL-write can refuse a `mode=ro` open when
    the reader cannot create the -shm file; rather than fall back to a
    writable open (which would break the read-only guarantee this whole
    package rests on), we copy the file aside and read the copy."""
    uri = f"file:{Path(path).resolve()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only = 1")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        connection.row_factory = sqlite3.Row
        return connection, None
    except sqlite3.Error:
        pass
    handle, temp_name = tempfile.mkstemp(prefix="bench-ro-", suffix=".db")
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(Path(path), temp_path)
        connection = sqlite3.connect(f"file:{temp_path}?mode=ro", uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only = 1")
        connection.row_factory = sqlite3.Row
        return connection, temp_path
    except (OSError, sqlite3.Error) as exc:
        temp_path.unlink(missing_ok=True)
        raise SourceError(f"cannot open {path} read-only: {exc}") from exc


def table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    ).fetchall()
    return {row[0] for row in rows}


def column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return set()
    return {row[1] for row in rows}


def pick(available: set[str], candidates: Sequence[str]) -> str | None:
    """First recognised spelling, or None. `None` is the whole point:
    an absent column is a missing measurement, not a zero."""
    for candidate in candidates:
        if candidate in available:
            return candidate
    return None


def cell(row: sqlite3.Row | dict[str, Any], column: str | None) -> Any:
    if column is None:
        return None
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None if value is None else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "ok", "pass", "passed"}:
        return True
    if text in {"0", "false", "no", "n", "fail", "failed"}:
        return False
    return None


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalise_cohort(value: Any) -> str:
    text = as_text(value)
    if text is None:
        return COHORT_UNKNOWN
    key = text.lower().replace("-", "_").replace(" ", "_")
    if key in _LEGACY_ALIASES:
        return COHORT_LEGACY
    if key in _NEW_ALIASES:
        return COHORT_NEW
    if key in (COHORT_LEGACY, COHORT_NEW):
        return key
    return COHORT_UNKNOWN


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    text = as_text(value)
    if text is None:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_iso(value: Any) -> datetime | None:
    text = as_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# usage folding -- the double-count guard
# ---------------------------------------------------------------------------


@dataclass
class UsageAggregator:
    """Folds many usage rows for one task into one `TaskUsage`.

    `delta` mode sums rows, but only ONCE per turn identity: a row whose
    turn id has already been folded is skipped entirely. This is what
    stops the "same usage block repeated on four transcript lines"
    shape from inflating a task 1.9x.

    `cumulative` mode never sums at all -- it keeps the row with the
    highest sequence value, because in that shape the last row already
    IS the task total.

    A field stays `None` until at least one row actually carries it, so
    a task whose rows never mention `cache_read` reports missing cache
    reads rather than zero cache reads."""

    mode: str = USAGE_MODE_DELTA
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    cache_write_total_tokens: int | None = None
    rows_seen: int = 0
    rows_folded: int = 0
    duplicates_skipped: int = 0
    _seen_turns: set[str] = field(default_factory=set)
    _best_sequence: Any = None

    _FIELDS = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_5m_tokens",
        "cache_write_1h_tokens",
        "cache_write_total_tokens",
    )

    def add(
        self,
        values: dict[str, int | None],
        *,
        turn_id: str | None = None,
        sequence: Any = None,
    ) -> None:
        self.rows_seen += 1
        if self.mode == USAGE_MODE_CUMULATIVE:
            key = sequence if sequence is not None else self.rows_seen
            if self._best_sequence is not None:
                try:
                    if not _sequence_gt(key, self._best_sequence):
                        return
                except TypeError:
                    return
            self._best_sequence = key
            self.rows_folded = 1
            for name in self._FIELDS:
                setattr(self, name, values.get(name))
            return

        if turn_id is not None:
            if turn_id in self._seen_turns:
                self.duplicates_skipped += 1
                return
            self._seen_turns.add(turn_id)
        self.rows_folded += 1
        for name in self._FIELDS:
            incoming = values.get(name)
            if incoming is None:
                continue
            current = getattr(self, name)
            setattr(self, name, incoming if current is None else current + incoming)

    def result(self) -> TaskUsage:
        return TaskUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_5m_tokens=self.cache_write_5m_tokens,
            cache_write_1h_tokens=self.cache_write_1h_tokens,
            cache_write_total_tokens=self.cache_write_total_tokens,
        )


def _sequence_gt(left: Any, right: Any) -> bool:
    try:
        return bool(left > right)
    except TypeError:
        return str(left) > str(right)


def detect_usage_mode(columns: set[str], table: str, requested: str) -> str:
    """Resolve `auto` against whatever the schema admits about itself.

    Only an EXPLICIT self-description counts -- a marker column, or a
    column/table name that literally says cumulative/total. Guessing
    cumulative-ness from the data (are the numbers monotonic?) would be
    a silent, unfalsifiable decision about someone else's schema, so
    when nothing says otherwise the safe default is delta-with-turn-
    de-duplication, which is correct for delta rows and merely
    conservative for repeated ones."""
    if requested in (USAGE_MODE_DELTA, USAGE_MODE_CUMULATIVE):
        return requested
    lowered = {name.lower() for name in columns}
    if any(marker in lowered for marker in CUMULATIVE_MARKER_COLUMNS):
        return USAGE_MODE_CUMULATIVE
    haystack = " ".join(lowered) + " " + table.lower()
    if "cumulative" in haystack or "running_total" in haystack:
        return USAGE_MODE_CUMULATIVE
    return USAGE_MODE_DELTA


def read_usage_values(row: sqlite3.Row | dict[str, Any], mapping: dict[str, str | None]) -> dict[str, int | None]:
    return {
        "input_tokens": as_int(cell(row, mapping.get("input"))),
        "output_tokens": as_int(cell(row, mapping.get("output"))),
        "cache_read_tokens": as_int(cell(row, mapping.get("cache_read"))),
        "cache_write_5m_tokens": as_int(cell(row, mapping.get("cache_write_5m"))),
        "cache_write_1h_tokens": as_int(cell(row, mapping.get("cache_write_1h"))),
        "cache_write_total_tokens": as_int(cell(row, mapping.get("cache_write"))),
    }


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------


@dataclass
class SourceResult:
    status: SourceStatus
    records: tuple[TaskRecord, ...] = ()
    usage_by_task: dict[str, TaskUsage] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    # The closed set of re-entry reasons this source can physically
    # emit, where it has one. It matters because a reason the store
    # cannot record renders as a zero row, and a zero row reads as
    # evidence of absence rather than absence of evidence.
    reason_vocabulary: frozenset[str] | None = None


class BenchSource:
    name = "source"

    def load(self) -> SourceResult:  # pragma: no cover - interface
        raise NotImplementedError


def _missing(name: str, path: Path | None, detail: str) -> SourceResult:
    return SourceResult(
        status=SourceStatus(
            name=name,
            path=str(path) if path else None,
            available=False,
            task_count=0,
            detail=detail,
        )
    )


class QueueDbSource(BenchSource):
    """`queue.db` -- the orchestration queue. Supplies the task spine:
    identity, project, timing, retries, and (from `queue_events`) the
    re-entry stream with its reason codes. Supplies no tokens; those
    come from `ai_usage.db`/`work.db` and are joined on task id."""

    name = "queue.db"

    def __init__(self, path: str | os.PathLike[str], *, cohort_map: dict[str, str] | None = None):
        self.path = Path(path)
        self.cohort_map = cohort_map or {}

    def load(self) -> SourceResult:
        if not self.path.exists():
            return _missing(self.name, self.path, "file does not exist")
        try:
            connection, temp = open_readonly(self.path)
        except SourceError as exc:
            return _missing(self.name, self.path, str(exc))
        try:
            tables = table_names(connection)
            if "queue_tasks" not in tables:
                return _missing(
                    self.name, self.path, f"no queue_tasks table (found: {sorted(tables)})"
                )
            task_columns = column_names(connection, "queue_tasks")
            reentries = self._load_reentries(connection, tables)
            dispatches = self._load_dispatches(connection, tables)
            records: list[TaskRecord] = []
            for row in connection.execute("SELECT * FROM queue_tasks"):
                record = self._to_record(row, task_columns, reentries, dispatches)
                if record is not None:
                    records.append(record)
            detail = (
                f"read {len(records)} task rows"
                if records
                else "table present but empty -- no tasks have been queued on this host yet"
            )
            return SourceResult(
                status=SourceStatus(
                    name=self.name,
                    path=str(self.path),
                    available=True,
                    task_count=len(records),
                    detail=detail,
                ),
                records=tuple(records),
            )
        finally:
            connection.close()
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _load_reentries(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> dict[str, list[Reentry]]:
        if "queue_events" not in tables:
            return {}
        columns = column_names(connection, "queue_events")
        if "task_id" not in columns:
            return {}
        reason_column = pick(columns, REASON_COLUMNS)
        type_column = pick(columns, ("event_type", "type", "kind"))
        time_column = pick(columns, ("timestamp", "created_at", "at"))
        found: dict[str, list[Reentry]] = {}
        for row in connection.execute("SELECT * FROM queue_events"):
            event_type = (as_text(cell(row, type_column)) or "").upper()
            if not _is_reentry_event(event_type):
                continue
            task_id = as_text(cell(row, "task_id"))
            if task_id is None:
                continue
            reason = as_text(cell(row, reason_column))
            found.setdefault(task_id, []).append(
                Reentry(reason=reason.upper() if reason else None, at=as_text(cell(row, time_column)))
            )
        return found

    def _load_dispatches(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> dict[str, set[str]]:
        """Distinct dispatch identities per task -- the ONLY honest way
        to count worker turns here.

        `queue_tasks.attempt_count` must never be used for this. It is
        bumped by reconcile-and-reclaim as well as by real rework, so a
        metric derived from it counts a flaky node as a lane with bad
        contracts -- a node with N crash-reclaim cycles would score
        identically to a lane that genuinely had to redo the work N
        times. `dispatch_idempotency_key` is the field the runtime
        itself already uses to tell those two apart: a reclaim reuses
        the key, a genuine new dispatch mints a new one. So N reclaim
        cycles collapse to ONE worker turn, which is the truth."""
        if "queue_events" not in tables:
            return {}
        columns = column_names(connection, "queue_events")
        if "task_id" not in columns:
            return {}
        type_column = pick(columns, ("event_type", "type", "kind"))
        key_column = pick(columns, IDEMPOTENCY_COLUMNS)
        found: dict[str, set[str]] = {}
        for row in connection.execute("SELECT * FROM queue_events"):
            event_type = (as_text(cell(row, type_column)) or "").upper()
            if "DISPATCH" not in event_type:
                continue
            task_id = as_text(cell(row, "task_id"))
            if task_id is None:
                continue
            metadata = parse_json_object(cell(row, "metadata" if "metadata" in columns else None))
            key = as_text(cell(row, key_column)) or as_text(_dig(metadata, IDEMPOTENCY_COLUMNS))
            if key is None:
                # A dispatch event with no idempotency key cannot be
                # distinguished from a reclaim of the previous one, so
                # it is not counted rather than guessed at.
                continue
            found.setdefault(task_id, set()).add(key)
        return found

    def _to_record(
        self,
        row: sqlite3.Row,
        columns: set[str],
        reentries: dict[str, list[Reentry]],
        dispatches: dict[str, set[str]],
    ) -> TaskRecord | None:
        task_id = as_text(cell(row, pick(columns, TASK_ID_COLUMNS)))
        if task_id is None:
            return None
        metadata = parse_json_object(cell(row, "metadata" if "metadata" in columns else None))
        # The Feature Contract the Analysis Gate writes. Its presence IS
        # the cohort signal -- the gate owns that fact, so it is read
        # from the gate's own column rather than copied into a second,
        # drifting label. It also carries the matching controls.
        analysis = parse_json_object(cell(row, "analysis" if "analysis" in columns else None))
        has_analysis = "analysis" in columns and as_text(cell(row, "analysis")) is not None
        cohort = self.cohort_map.get(task_id)
        if cohort is None:
            cohort = _first_present(
                normalise_cohort(cell(row, pick(columns, COHORT_COLUMNS))),
                normalise_cohort(_dig(metadata, COHORT_COLUMNS)),
                normalise_cohort(_dig(analysis, COHORT_COLUMNS)),
            )
            if cohort == COHORT_UNKNOWN and "analysis" in columns:
                cohort = COHORT_NEW if has_analysis else COHORT_LEGACY
        else:
            cohort = normalise_cohort(cohort)
        started = parse_iso(cell(row, "started_at" if "started_at" in columns else None))
        completed = parse_iso(cell(row, "completed_at" if "completed_at" in columns else None))
        duration = None
        if started is not None and completed is not None and completed >= started:
            duration = (completed - started).total_seconds()
        # NOT derived from attempt_count -- see _load_dispatches.
        dispatch_keys = dispatches.get(task_id)
        if dispatch_keys is None and "dispatch_idempotency_key" in columns:
            single = as_text(cell(row, "dispatch_idempotency_key"))
            dispatch_keys = {single} if single else None
        worker_turn_count = as_int(_dig(metadata, TURN_COUNT_COLUMNS))
        if worker_turn_count is None and dispatch_keys is not None:
            worker_turn_count = len(dispatch_keys)
        retries = None if worker_turn_count is None else max(0, worker_turn_count - 1)
        profile = as_text(_dig(analysis, PROFILE_COLUMNS))
        profile_source = "queue_tasks.analysis" if profile else None
        if profile is None:
            profile = as_text(_dig(metadata, PROFILE_COLUMNS))
            profile_source = "queue_tasks.metadata" if profile else None
        if profile is None:
            profile = as_text(cell(row, pick(columns, PROFILE_COLUMNS)))
            profile_source = "queue_tasks column" if profile else None
        task_reentries = tuple(reentries.get(task_id, ()))
        first_pass = as_bool(_dig(metadata, FIRST_PASS_COLUMNS))
        if first_pass is None:
            # First-pass success is only derived where the runtime left
            # real verification evidence. Without it, "no recorded
            # rework" is indistinguishable from "nobody checked", and
            # scoring the latter as a success would reward a vague
            # contract -- the less a task promises, the easier it is to
            # satisfy. Unverified stays None, and None stays out of the
            # rate's denominator.
            evidence = (
                as_text(cell(row, "verification_evidence"))
                if "verification_evidence" in columns
                else None
            )
            if evidence and worker_turn_count is not None:
                counted = [entry for entry in task_reentries if not entry.is_excluded]
                first_pass = worker_turn_count == 1 and not counted
        has_evidence = (
            as_text(cell(row, "verification_evidence")) is not None
            if "verification_evidence" in columns
            else None
        )
        return TaskRecord(
            task_id=task_id,
            cohort=cohort,
            project=as_text(cell(row, pick(columns, PROJECT_COLUMNS)))
            or as_text(_dig(metadata, PROJECT_COLUMNS)),
            profile=profile,
            profile_source=profile_source,
            complexity=as_text(_dig(analysis, COMPLEXITY_COLUMNS))
            or as_text(_dig(metadata, COMPLEXITY_COLUMNS))
            or as_text(cell(row, pick(columns, COMPLEXITY_COLUMNS))),
            agent=as_text(_dig(metadata, AGENT_COLUMNS))
            or as_text(cell(row, pick(columns, AGENT_COLUMNS))),
            model=as_text(_dig(metadata, MODEL_COLUMNS))
            or as_text(cell(row, pick(columns, MODEL_COLUMNS))),
            worker_turn_count=worker_turn_count,
            reentries=task_reentries,
            first_pass_success=first_pass,
            duration_seconds=duration,
            retries=retries,
            source=self.name,
            verification_evidence=has_evidence,
            terminal_status=as_text(cell(row, "status")) if "status" in columns else None,
        )


def _is_reentry_event(event_type: str) -> bool:
    return any(
        marker in event_type
        for marker in ("REENTR", "RE_ENTR", "REWORK", "REOPEN", "RETRY", "REJECT", "BOUNCE")
    )


def _first_present(*values: str) -> str:
    for value in values:
        if value and value != COHORT_UNKNOWN:
            return value
    return COHORT_UNKNOWN


def _dig(metadata: dict[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in metadata:
            return metadata[key]
    return None


class UsageDbSource(BenchSource):
    """`ai_usage.db` / `work.db` -- per-task (or per-turn) token rows.

    The same adapter serves both because the only thing that differs is
    the file name and which tables happen to be inside; the table and
    column discovery is identical. It emits token totals keyed by task
    id, plus -- for `work.db`, which is expected to carry the worker
    turn/re-entry side of the story -- whatever task-level fields it
    finds, so that a deployment where `work.db` is the only task store
    still produces a usable comparison."""

    name = "usage.db"

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        name: str | None = None,
        usage_mode: str = USAGE_MODE_AUTO,
        cohort_map: dict[str, str] | None = None,
    ):
        self.path = Path(path)
        if name:
            self.name = name
        self.usage_mode = usage_mode
        self.cohort_map = cohort_map or {}

    def load(self) -> SourceResult:
        if not self.path.exists():
            return _missing(
                self.name,
                self.path,
                "file does not exist -- this telemetry is not produced on this host yet",
            )
        try:
            connection, temp = open_readonly(self.path)
        except SourceError as exc:
            return _missing(self.name, self.path, str(exc))
        try:
            table = self._find_usage_table(connection)
            if table is None:
                return _missing(
                    self.name,
                    self.path,
                    "no table carrying both a task identifier and token columns "
                    f"(tables: {sorted(table_names(connection))})",
                )
            return self._read_table(connection, table)
        finally:
            connection.close()
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _find_usage_table(self, connection: sqlite3.Connection) -> str | None:
        best: tuple[int, str] | None = None
        for table in sorted(table_names(connection)):
            if table.startswith("sqlite_"):
                continue
            columns = column_names(connection, table)
            if pick(columns, TASK_ID_COLUMNS) is None:
                continue
            token_columns = [
                pick(columns, INPUT_COLUMNS),
                pick(columns, OUTPUT_COLUMNS),
                pick(columns, CACHE_READ_COLUMNS),
                pick(columns, CACHE_WRITE_COLUMNS),
                pick(columns, CACHE_WRITE_5M_COLUMNS),
                pick(columns, CACHE_WRITE_1H_COLUMNS),
            ]
            score = sum(1 for column in token_columns if column is not None)
            if score == 0:
                continue
            if best is None or score > best[0]:
                best = (score, table)
        return best[1] if best else None

    def _read_table(self, connection: sqlite3.Connection, table: str) -> SourceResult:
        columns = column_names(connection, table)
        mapping = {
            "task": pick(columns, TASK_ID_COLUMNS),
            "input": pick(columns, INPUT_COLUMNS),
            "output": pick(columns, OUTPUT_COLUMNS),
            "cache_read": pick(columns, CACHE_READ_COLUMNS),
            "cache_write": pick(columns, CACHE_WRITE_COLUMNS),
            "cache_write_5m": pick(columns, CACHE_WRITE_5M_COLUMNS),
            "cache_write_1h": pick(columns, CACHE_WRITE_1H_COLUMNS),
            "turn": pick(columns, TURN_ID_COLUMNS),
            "sequence": pick(columns, SEQUENCE_COLUMNS),
        }
        mode = detect_usage_mode(columns, table, self.usage_mode)
        aggregators: dict[str, UsageAggregator] = {}
        extras: dict[str, dict[str, Any]] = {}
        for row in connection.execute(f'SELECT * FROM "{table}"'):
            task_id = as_text(cell(row, mapping["task"]))
            if task_id is None:
                continue
            aggregator = aggregators.setdefault(task_id, UsageAggregator(mode=mode))
            turn_id = as_text(cell(row, mapping["turn"]))
            aggregator.add(
                read_usage_values(row, mapping),
                turn_id=turn_id,
                sequence=cell(row, mapping["sequence"]),
            )
            extras.setdefault(task_id, {}).update(self._extras(row, columns))
        usage_by_task = {task: agg.result() for task, agg in aggregators.items()}
        records = tuple(
            self._to_record(task_id, usage_by_task[task_id], extras.get(task_id, {}))
            for task_id in sorted(usage_by_task)
        )
        duplicates = sum(agg.duplicates_skipped for agg in aggregators.values())
        warnings: list[str] = []
        if duplicates:
            warnings.append(
                f"{self.name}: skipped {duplicates} repeated usage row(s) that shared a turn "
                "identity with a row already counted (double-count guard)"
            )
        missing = [key for key in ("input", "output", "cache_read", "cache_write") if mapping[key] is None]
        if missing:
            warnings.append(
                f"{self.name}: table '{table}' has no column for {', '.join(missing)}; those "
                "measurements are recorded as missing, not as zero"
            )
        return SourceResult(
            status=SourceStatus(
                name=self.name,
                path=str(self.path),
                available=True,
                task_count=len(usage_by_task),
                detail=f"table '{table}', usage folded in {mode} mode",
            ),
            records=records,
            usage_by_task=usage_by_task,
            warnings=tuple(warnings),
        )

    def _extras(self, row: sqlite3.Row, columns: set[str]) -> dict[str, Any]:
        metadata = parse_json_object(cell(row, "metadata" if "metadata" in columns else None))
        found = {
            "cohort": cell(row, pick(columns, COHORT_COLUMNS)) or _dig(metadata, COHORT_COLUMNS),
            "project": cell(row, pick(columns, PROJECT_COLUMNS)) or _dig(metadata, PROJECT_COLUMNS),
            "profile": cell(row, pick(columns, PROFILE_COLUMNS)) or _dig(metadata, PROFILE_COLUMNS),
            "complexity": cell(row, pick(columns, COMPLEXITY_COLUMNS))
            or _dig(metadata, COMPLEXITY_COLUMNS),
            "agent": cell(row, pick(columns, AGENT_COLUMNS)) or _dig(metadata, AGENT_COLUMNS),
            "model": cell(row, pick(columns, MODEL_COLUMNS)) or _dig(metadata, MODEL_COLUMNS),
            "worker_turn_count": cell(row, pick(columns, TURN_COUNT_COLUMNS))
            or _dig(metadata, TURN_COUNT_COLUMNS),
            "retries": cell(row, pick(columns, RETRY_COLUMNS)) or _dig(metadata, RETRY_COLUMNS),
            "first_pass_success": cell(row, pick(columns, FIRST_PASS_COLUMNS)),
            "duration": cell(row, pick(columns, DURATION_COLUMNS)),
            "duration_column": pick(columns, DURATION_COLUMNS),
        }
        return {key: value for key, value in found.items() if value is not None}

    def _to_record(self, task_id: str, usage: TaskUsage, extras: dict[str, Any]) -> TaskRecord:
        cohort = self.cohort_map.get(task_id)
        duration = as_float(extras.get("duration"))
        if duration is not None and str(extras.get("duration_column", "")).endswith("_ms"):
            duration = duration / 1000.0
        return TaskRecord(
            task_id=task_id,
            cohort=normalise_cohort(cohort if cohort is not None else extras.get("cohort")),
            project=as_text(extras.get("project")),
            profile=as_text(extras.get("profile")),
            complexity=as_text(extras.get("complexity")),
            agent=as_text(extras.get("agent")),
            model=as_text(extras.get("model")),
            usage=usage,
            worker_turn_count=as_int(extras.get("worker_turn_count")),
            first_pass_success=as_bool(extras.get("first_pass_success")),
            duration_seconds=duration,
            retries=as_int(extras.get("retries")),
            source=self.name,
        )


class JsonlFixtureSource(BenchSource):
    """A JSONL file of already-normalised task records.

    Not a toy: it is how the harness is exercised in tests, how a
    reviewer reproduces a disputed number without access to the live
    databases, and how an operator can run the comparison over an
    export from another host. One JSON object per line, keys matching
    `TaskRecord`; `reentries` is a list of `{"reason": ..., "at": ...}`
    or bare reason strings. Any key that is absent stays missing."""

    name = "jsonl"

    def __init__(self, path: str | os.PathLike[str], *, name: str | None = None):
        self.path = Path(path)
        if name:
            self.name = name

    def load(self) -> SourceResult:
        if not self.path.exists():
            return _missing(self.name, self.path, "file does not exist")
        records: list[TaskRecord] = []
        warnings: list[str] = []
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                payload = json.loads(stripped)
            except ValueError as exc:
                warnings.append(f"{self.path.name}:{number}: not valid JSON ({exc})")
                continue
            if not isinstance(payload, dict):
                warnings.append(f"{self.path.name}:{number}: not a JSON object")
                continue
            record = record_from_dict(payload, source=self.name)
            if record is None:
                warnings.append(f"{self.path.name}:{number}: no task_id")
                continue
            records.append(record)
        return SourceResult(
            status=SourceStatus(
                name=self.name,
                path=str(self.path),
                available=True,
                task_count=len(records),
                detail=f"read {len(records)} record(s)",
            ),
            records=tuple(records),
            warnings=tuple(warnings),
        )


def record_from_dict(payload: dict[str, Any], *, source: str = "dict") -> TaskRecord | None:
    task_id = as_text(payload.get("task_id") or payload.get("id"))
    if task_id is None:
        return None
    usage = TaskUsage(
        input_tokens=as_int(payload.get("input_tokens")),
        output_tokens=as_int(payload.get("output_tokens")),
        cache_read_tokens=as_int(payload.get("cache_read_tokens")),
        cache_write_5m_tokens=as_int(payload.get("cache_write_5m_tokens")),
        cache_write_1h_tokens=as_int(payload.get("cache_write_1h_tokens")),
        cache_write_total_tokens=as_int(payload.get("cache_write_tokens")),
    )
    reentries: list[Reentry] = []
    for entry in payload.get("reentries") or ():
        if isinstance(entry, str):
            reentries.append(Reentry(reason=entry.upper()))
        elif isinstance(entry, dict):
            reason = as_text(entry.get("reason"))
            reentries.append(
                Reentry(reason=reason.upper() if reason else None, at=as_text(entry.get("at")))
            )
    return TaskRecord(
        task_id=task_id,
        cohort=normalise_cohort(payload.get("cohort")),
        project=as_text(payload.get("project")),
        profile=as_text(payload.get("profile")),
        complexity=as_text(payload.get("complexity")),
        agent=as_text(payload.get("agent")),
        model=as_text(payload.get("model")),
        usage=usage,
        worker_turn_count=as_int(payload.get("worker_turn_count")),
        reentries=tuple(reentries),
        first_pass_success=as_bool(payload.get("first_pass_success")),
        duration_seconds=as_float(payload.get("duration_seconds")),
        retries=as_int(payload.get("retries")),
        source=as_text(payload.get("source")) or source,
    )


# ---------------------------------------------------------------------------
# merging
# ---------------------------------------------------------------------------


def merge_records(groups: Iterable[Sequence[TaskRecord]]) -> tuple[TaskRecord, ...]:
    """Join the per-source views of the same task id into one record.

    First non-missing value wins, in source order, which is why the
    caller passes the task spine (`queue.db`) before the token sources:
    a field the spine knows is authoritative, and a field only the
    usage store knows is still picked up. Nothing is overwritten with
    `None`, so a later source can only ever ADD information."""
    merged: dict[str, TaskRecord] = {}
    order: list[str] = []
    for group in groups:
        for record in group:
            existing = merged.get(record.task_id)
            if existing is None:
                merged[record.task_id] = record
                order.append(record.task_id)
                continue
            merged[record.task_id] = _combine(existing, record)
    return tuple(merged[task_id] for task_id in order)


def _combine(base: TaskRecord, extra: TaskRecord) -> TaskRecord:
    usage = TaskUsage(
        input_tokens=_prefer(base.usage.input_tokens, extra.usage.input_tokens),
        output_tokens=_prefer(base.usage.output_tokens, extra.usage.output_tokens),
        cache_read_tokens=_prefer(base.usage.cache_read_tokens, extra.usage.cache_read_tokens),
        cache_write_5m_tokens=_prefer(
            base.usage.cache_write_5m_tokens, extra.usage.cache_write_5m_tokens
        ),
        cache_write_1h_tokens=_prefer(
            base.usage.cache_write_1h_tokens, extra.usage.cache_write_1h_tokens
        ),
        cache_write_total_tokens=_prefer(
            base.usage.cache_write_total_tokens, extra.usage.cache_write_total_tokens
        ),
    )
    cohort = base.cohort if base.cohort != COHORT_UNKNOWN else extra.cohort
    sources = base.source if base.source == extra.source else f"{base.source}+{extra.source}"
    return TaskRecord(
        task_id=base.task_id,
        cohort=cohort,
        project=_prefer(base.project, extra.project),
        profile=_prefer(base.profile, extra.profile),
        complexity=_prefer(base.complexity, extra.complexity),
        agent=_prefer(base.agent, extra.agent),
        model=_prefer(base.model, extra.model),
        usage=usage,
        worker_turn_count=_prefer(base.worker_turn_count, extra.worker_turn_count),
        reentries=base.reentries or extra.reentries,
        first_pass_success=_prefer(base.first_pass_success, extra.first_pass_success),
        duration_seconds=_prefer(base.duration_seconds, extra.duration_seconds),
        retries=_prefer(base.retries, extra.retries),
        source=sources,
        flags=tuple(dict.fromkeys(base.flags + extra.flags)),
        verification_evidence=_prefer(base.verification_evidence, extra.verification_evidence),
        terminal_status=_prefer(base.terminal_status, extra.terminal_status),
        profile_source=_prefer(base.profile_source, extra.profile_source),
        cost_units_override=_prefer(base.cost_units_override, extra.cost_units_override),
        cost_units_unavailable=base.cost_units_unavailable or extra.cost_units_unavailable,
    )


def _prefer(base: Any, extra: Any) -> Any:
    return base if base is not None else extra


def load_cohort_map(path: str | os.PathLike[str] | None) -> dict[str, str]:
    """`{"task-id": "legacy"}` -- the escape hatch for telemetry that
    does not label its own cohort. Explicit, file-based and auditable,
    rather than a heuristic buried in an adapter."""
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SourceError("cohort map must be a JSON object of task_id -> cohort")
    return {str(key): str(value) for key, value in payload.items()}


def default_source_paths(state_dir: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    root = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
    return {
        "queue.db": root / "queue.db",
        "ai_usage.db": root / "ai_usage.db",
        "work.db": root / "work.db",
    }
