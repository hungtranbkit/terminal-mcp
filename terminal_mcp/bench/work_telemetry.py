"""Adapter for the Task A work-efficiency telemetry store.

Written against the schema the telemetry lane committed on
`feat/work-efficiency-telemetry` (`docs/WORK_EFFICIENCY_TELEMETRY.md`),
rather than left to the generic schema-discovery in `sources.py`. The
generic reader would find the token columns and sum them correctly, but
it would get four things wrong that only a named adapter can get right:

1. `telemetry_usage_samples` keeps BOTH already-differenced deltas and
   the raw `reported_*` values, which ARE cumulative when
   `kind='CUMULATIVE'`. Summing `reported_*` would double count exactly
   the way this harness exists to prevent. This adapter reads the delta
   columns by name and never touches `reported_*`.
2. Worker turns are `SUM(turn_count)` over the WORKER phases only.
   `ANALYSIS` and `CONTRACT` are the investment side of the experiment
   and `UNKNOWN` is unattributed -- counting either into the worker's
   turns would inflate the precise number the experiment is testing.
3. `first_pass_success` is a tri-state TEXT (`UNKNOWN`/`TRUE`/`FALSE`),
   not a boolean. `UNKNOWN` must leave the denominator of the rate, not
   be coerced to failure.
4. Token columns are nullable with no default, so NULL means never
   measured and 0 means measured as zero. `SUM` over zero rows returns
   NULL, which is already the right answer -- it is never coalesced.

The store deliberately does NOT carry the cohort label, the matching
controls, or the model id. Cohort and controls are joined from
`queue.db` (see `QueueDbSource`), which is the gate's own source of
truth rather than a second, drifting copy. The model id is not recorded
anywhere yet, which is why `cost_units` is unpriceable from this source
alone unless an operator states an assumption with `--price-model`.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .model import Reentry, SourceStatus, TaskRecord, TaskUsage
from .sources import (
    BenchSource,
    SourceError,
    SourceResult,
    _missing,
    as_int,
    as_text,
    cell,
    column_names,
    normalise_cohort,
    open_readonly,
    parse_iso,
    parse_json_object,
    table_names,
)

# Phases whose turns are the WORKER's. ANALYSIS/CONTRACT are the
# investment being measured; UNKNOWN is unattributed.
WORKER_PHASES = ("IMPLEMENTATION", "VERIFICATION", "DELIVERY")

# Weakest-wins ordering, so an aggregate never claims more confidence
# than its least confident input.
CONFIDENCE_ORDER = ("UNKNOWN", "ESTIMATED", "DERIVED", "MEASURED")

FLAG_COUNTER_RESET = "COUNTER_RESET"
FLAG_PARTIAL_SAMPLES = "PARTIAL_SAMPLES"

_TOKEN_COLUMNS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_tokens", "cache_read_tokens"),
    ("cache_write_tokens", "cache_write_total_tokens"),
)


def default_path() -> Path:
    """Same resolution order the store itself uses."""
    override = os.environ.get("TERMINAL_MCP_WORK_TELEMETRY_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "terminal-mcp" / "work_telemetry.db"


class WorkTelemetryDbSource(BenchSource):
    name = "work_telemetry.db"

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        cohort_map: dict[str, str] | None = None,
        price_model: str | None = None,
    ):
        self.path = Path(path) if path is not None else default_path()
        self.cohort_map = cohort_map or {}
        self.price_model = price_model

    def load(self) -> SourceResult:
        if not self.path.exists():
            return _missing(
                self.name,
                self.path,
                "file does not exist -- no worker has emitted telemetry on this host yet",
            )
        try:
            connection, temp = open_readonly(self.path)
        except SourceError as exc:
            return _missing(self.name, self.path, str(exc))
        try:
            tables = table_names(connection)
            if "telemetry_tasks" not in tables:
                return _missing(
                    self.name,
                    self.path,
                    f"no telemetry_tasks table (found: {sorted(tables)})",
                )
            usage, turns, sample_flags, confidence = self._read_samples(connection, tables)
            reentries = self._read_reentries(connection, tables)
            columns = column_names(connection, "telemetry_tasks")
            records: list[TaskRecord] = []
            warnings: list[str] = []
            for row in connection.execute("SELECT * FROM telemetry_tasks"):
                task_id = as_text(cell(row, "task_id"))
                if task_id is None:
                    continue
                records.append(
                    self._to_record(
                        row,
                        columns,
                        task_id,
                        usage.get(task_id, TaskUsage()),
                        turns.get(task_id),
                        tuple(reentries.get(task_id, ())),
                        sample_flags.get(task_id, ()),
                        confidence.get(task_id),
                    )
                )
            measured = sum(1 for record in records if not record.usage.is_empty)
            if records and measured < len(records):
                warnings.append(
                    f"{self.name}: {len(records) - measured} of {len(records)} task(s) have no "
                    "usage samples at all; they count toward coverage and are absent from every "
                    "token statistic, never entered as zero"
                )
            if self.price_model:
                warnings.append(
                    f"{self.name}: this store records no model id, so costs were priced under the "
                    f"stated assumption --price-model={self.price_model}. Every cost figure is "
                    "conditional on that assumption."
                )
            return SourceResult(
                status=SourceStatus(
                    name=self.name,
                    path=str(self.path),
                    available=True,
                    task_count=len(records),
                    detail=(
                        f"{len(records)} task row(s), {measured} with usage samples"
                        if records
                        else "tables present but empty -- no worker has written a row yet"
                    ),
                ),
                records=tuple(records),
                warnings=tuple(warnings),
            )
        finally:
            connection.close()
            if temp is not None:
                temp.unlink(missing_ok=True)

    # -- samples ------------------------------------------------------------

    def _read_samples(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> tuple[dict[str, TaskUsage], dict[str, int], dict[str, tuple[str, ...]], dict[str, str]]:
        if "telemetry_usage_samples" not in tables:
            return ({}, {}, {}, {})
        columns = column_names(connection, "telemetry_usage_samples")
        totals: dict[str, dict[str, int | None]] = {}
        nulls: dict[str, bool] = {}
        turns: dict[str, int] = {}
        flags: dict[str, set[str]] = {}
        confidence: dict[str, str] = {}
        for row in connection.execute("SELECT * FROM telemetry_usage_samples"):
            task_id = as_text(cell(row, "task_id"))
            if task_id is None:
                continue
            bucket = totals.setdefault(task_id, {name: None for _, name in _TOKEN_COLUMNS})
            for source_column, field in _TOKEN_COLUMNS:
                if source_column not in columns:
                    continue
                value = as_int(cell(row, source_column))
                if value is None:
                    nulls[task_id] = True
                    continue
                current = bucket[field]
                bucket[field] = value if current is None else current + value
            phase = (as_text(cell(row, "phase")) or "").upper()
            if phase in WORKER_PHASES:
                turn = as_int(cell(row, "turn_count"))
                if turn is not None:
                    turns[task_id] = turns.get(task_id, 0) + turn
            if as_int(cell(row, "counter_reset")):
                flags.setdefault(task_id, set()).add(FLAG_COUNTER_RESET)
            level = (as_text(cell(row, "confidence")) or "UNKNOWN").upper()
            if level not in CONFIDENCE_ORDER:
                level = "UNKNOWN"
            existing = confidence.get(task_id)
            if existing is None or CONFIDENCE_ORDER.index(level) < CONFIDENCE_ORDER.index(existing):
                confidence[task_id] = level

        usage: dict[str, TaskUsage] = {}
        for task_id, bucket in totals.items():
            if nulls.get(task_id) and any(value is not None for value in bucket.values()):
                flags.setdefault(task_id, set()).add(FLAG_PARTIAL_SAMPLES)
            usage[task_id] = TaskUsage(**bucket)
        return (usage, turns, {k: tuple(sorted(v)) for k, v in flags.items()}, confidence)

    def _read_reentries(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> dict[str, list[Reentry]]:
        if "telemetry_reentries" not in tables:
            return {}
        found: dict[str, list[Reentry]] = {}
        for row in connection.execute("SELECT * FROM telemetry_reentries"):
            task_id = as_text(cell(row, "task_id"))
            if task_id is None:
                continue
            reason = as_text(cell(row, "reason"))
            found.setdefault(task_id, []).append(
                Reentry(
                    reason=reason.upper() if reason else None,
                    at=as_text(cell(row, "occurred_at")),
                )
            )
        return found

    # -- rollup -------------------------------------------------------------

    def _to_record(
        self,
        row: sqlite3.Row,
        columns: set[str],
        task_id: str,
        usage: TaskUsage,
        turn_count: int | None,
        reentries: tuple[Reentry, ...],
        sample_flags: tuple[str, ...],
        confidence: str | None,
    ) -> TaskRecord:
        started = parse_iso(cell(row, "started_at")) if "started_at" in columns else None
        completed = parse_iso(cell(row, "completed_at")) if "completed_at" in columns else None
        duration = None
        if started is not None and completed is not None and completed >= started:
            duration = (completed - started).total_seconds()
        metadata = parse_json_object(cell(row, "metadata")) if "metadata" in columns else {}
        cohort = self.cohort_map.get(task_id)
        if cohort is None:
            cohort = metadata.get("cohort")
        flags = list(sample_flags)
        if confidence and confidence != "MEASURED":
            flags.append(f"CONFIDENCE_{confidence}")
        return TaskRecord(
            task_id=task_id,
            cohort=normalise_cohort(cohort),
            project=as_text(cell(row, "project_id")) if "project_id" in columns else None,
            model=self.price_model,
            usage=usage,
            worker_turn_count=turn_count,
            reentries=reentries,
            first_pass_success=_tri_state(cell(row, "first_pass_success"))
            if "first_pass_success" in columns
            else None,
            duration_seconds=duration,
            # There is no separate retry concept in this store: a retry
            # that costs a worker round trip IS a re-entry, so inventing
            # a retries number here would double count rework.
            retries=None,
            source=self.name,
            flags=tuple(dict.fromkeys(flags)),
        )


def _tri_state(value: Any) -> bool | None:
    """`UNKNOWN` -> None, so an unresolved task leaves the rate's
    denominator instead of being scored as a failure."""
    text = as_text(value)
    if text is None:
        return None
    upper = text.upper()
    if upper == "TRUE":
        return True
    if upper == "FALSE":
        return False
    return None
