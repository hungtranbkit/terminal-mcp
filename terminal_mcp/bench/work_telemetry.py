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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import pricing
from .model import (
    KNOWN_REENTRY_REASONS,
    FLAG_FPS_DISAGREEMENT,
    FLAG_MIXED_MODEL,
    FLAG_UNKNOWN_MODEL_PRICE,
    Reentry,
    SourceStatus,
    TaskRecord,
    TaskUsage,
)
from .sources import (
    BenchSource,
    probe_check_values,
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

# The CHECK constraint on telemetry_reentries.reason, as of migration
# v3. Anything outside this set is rejected at write time and lands in
# OTHER, so a reason the harness knows about but the store cannot record
# renders as UNAVAILABLE rather than as a zero row.
#
# STALE_CONTEXT arrived in v3 and the set is now complete with respect
# to what this harness knows, so nothing renders UNAVAILABLE against a
# current store. The mechanism stays because the next reason will have
# the same problem, and because this adapter may be pointed at an older
# database whose constraint predates v3.
STORE_REENTRY_REASONS = frozenset(
    {
        "TEST_FAILURE",
        "CONTRACT_GAP",
        "IMPLEMENTATION_BUG",
        "ENVIRONMENT_FAILURE",
        "USER_CHANGED_REQUIREMENT",
        "DELIVERY_FAILURE",
        "MERGE_CONFLICT",
        "STALE_CONTEXT",
        "OTHER",
    }
)

# The same constraint before migration v3. Kept named rather than
# deleted: this adapter can be pointed at an older database, and the
# UNAVAILABLE path is only exercisable against a vocabulary that is
# genuinely missing something.
STORE_REENTRY_REASONS_V2 = STORE_REENTRY_REASONS - {"STALE_CONTEXT"}

# PRAGMA user_version -> the vocabulary that schema level admits. Used
# only when the constraint itself cannot be probed. It is a DECLARED
# level rather than an observed constraint, so it drifts the moment a
# migration adds a reason without this table being updated -- which is
# precisely why it is the fallback and not the primary.
#
# It matters at all because this reader NEVER MIGRATES. A database
# opened through the store's own code migrates itself on open; this one
# is opened read-only, so it can legitimately encounter a v2 file that
# no producer has yet opened with current code. For that file
# STALE_CONTEXT genuinely is UNAVAILABLE rather than zero.
REASONS_BY_USER_VERSION: tuple[tuple[int, frozenset[str]], ...] = (
    (3, STORE_REENTRY_REASONS),
    (1, STORE_REENTRY_REASONS_V2),
)

# Samples whose model was never recorded. They are real usage whose
# price is genuinely unknown -- never dropped, never folded into a
# neighbouring model, and never priced under a silent default.
MODEL_UNKNOWN = "UNKNOWN"

FLAG_COUNTER_RESET = "COUNTER_RESET"
# At least one sample carries no model_id, so part of this task's usage
# cannot be priced at all.
FLAG_MODEL_UNATTRIBUTED = "MODEL_UNATTRIBUTED"
# The collapsed cache-write total and the 5m/1h split disagree. The
# store refuses such a write, so this can only come from an older or
# foreign database -- but silently preferring one over the other would
# be picking a winner between two contradictory measurements.
FLAG_CACHE_WRITE_INCONSISTENT = "CACHE_WRITE_INCONSISTENT"
FLAG_PARTIAL_SAMPLES = "PARTIAL_SAMPLES"

# Migration 2 of the store added the cache-write TTL split and
# model_id, so the collapsed total is no longer the only thing
# available. Reading only `cache_write_tokens` would silently discard
# the coefficient that makes cost computable at all (5m bills 1.25x
# base input, 1h bills 2x) and report every task as unpriceable under
# the default split_required policy.
_TOKEN_COLUMNS = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("cache_read_tokens", "cache_read_tokens"),
    ("cache_write_5m_tokens", "cache_write_5m_tokens"),
    ("cache_write_1h_tokens", "cache_write_1h_tokens"),
    ("cache_write_tokens", "cache_write_total_tokens"),
)


@dataclass(frozen=True)
class SampleRollup:
    """Everything `telemetry_usage_samples` yields, per task."""

    usage: dict[str, TaskUsage]
    turns: dict[str, int]
    flags: dict[str, tuple[str, ...]]
    confidence: dict[str, str]
    models: dict[str, tuple[str, ...]]
    costs: dict[str, float | None]


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
            samples = self._read_samples(connection, tables)
            reentries = self._read_reentries(connection, tables)
            columns = column_names(connection, "telemetry_tasks")
            records: list[TaskRecord] = []
            warnings: list[str] = []
            vocabulary, vocabulary_source = self._reason_vocabulary(connection, tables)
            if vocabulary_source != "probed":
                warnings.append(
                    f"{self.name}: the re-entry reason constraint could not be probed directly; "
                    f"the emittable set was taken from {vocabulary_source}, which can drift "
                    "behind the store"
                )
            for row in connection.execute("SELECT * FROM telemetry_tasks"):
                task_id = as_text(cell(row, "task_id"))
                if task_id is None:
                    continue
                records.append(
                    self._to_record(row, columns, task_id, samples, reentries)
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
                reason_vocabulary=vocabulary,
            )
        finally:
            connection.close()
            if temp is not None:
                temp.unlink(missing_ok=True)

    def _reason_vocabulary(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> tuple[frozenset[str], str]:
        """Which re-entry reasons this FILE can actually hold.

        Three sources, most authoritative first, because they fail in
        different ways:

        1. Probe the constraint (`sources.probe_check_values`). Exact,
           observed from the file, and immune both to the DDL-comment
           trap -- where a comment naming a value makes a text match
           report it as admitted -- and to drift when the store adds a
           reason. It replays the DDL into a fresh in-memory database,
           so it stays compatible with this package's read-only
           guarantee; a SAVEPOINT probe against the real file would
           fail on `query_only`, and would report EVERY reason as
           rejected rather than erroring, which is a silent
           fail-closed.
        2. `PRAGMA user_version`, the declared migration level. Also
           read from the file, but a version-to-vocabulary table here
           drifts the moment a migration adds a reason.
        3. The documented constant, which is this code's idea of the
           vocabulary rather than the file's.

        A `warnings` line names which one was used whenever it is not
        the probe, so a reader is never told a vocabulary is observed
        when it was assumed."""
        if "telemetry_reentries" in tables:
            probed = probe_check_values(
                connection,
                "telemetry_reentries",
                "reason",
                sorted(KNOWN_REENTRY_REASONS | STORE_REENTRY_REASONS),
            )
            if probed:
                return (probed, "probed")
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        except (sqlite3.Error, TypeError, IndexError):
            version = None
        if isinstance(version, int) and version > 0:
            for minimum, reasons in REASONS_BY_USER_VERSION:
                if version >= minimum:
                    return (reasons, f"PRAGMA user_version = {version}")
        return (STORE_REENTRY_REASONS, "this adapter's documented constant")

    # -- samples ------------------------------------------------------------

    def _read_samples(
        self, connection: sqlite3.Connection, tables: set[str]
    ) -> "SampleRollup":
        if "telemetry_usage_samples" not in tables:
            return SampleRollup({}, {}, {}, {}, {}, {})
        columns = column_names(connection, "telemetry_usage_samples")
        has_split = "cache_write_5m_tokens" in columns and "cache_write_1h_tokens" in columns
        models: dict[str, set[str]] = {}
        costs: dict[str, float | None] = {}
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
            # Per-sample pricing, using THIS sample's model. Summing
            # exact per-sample costs is the only correct answer for a
            # task whose turns ran on more than one model, and it is
            # never worse than pricing the task total once.
            recorded_model = as_text(cell(row, "model_id")) if "model_id" in columns else None
            # An unattributed sample groups under MODEL_UNKNOWN rather
            # than inheriting a neighbouring sample's model. Only an
            # explicit operator assumption (--price-model) may stand in
            # for it, and the report discloses that it did.
            sample_model = recorded_model or self.price_model
            models.setdefault(task_id, set()).add(recorded_model or MODEL_UNKNOWN)
            already_unpriceable = task_id in costs and costs[task_id] is None
            if has_split and not already_unpriceable:
                sample_cost = pricing.cost_units(
                    model=sample_model,
                    input_tokens=as_int(cell(row, "input_tokens")),
                    output_tokens=as_int(cell(row, "output_tokens")),
                    cache_read_tokens=as_int(cell(row, "cache_read_tokens")),
                    cache_write_5m_tokens=as_int(cell(row, "cache_write_5m_tokens")),
                    cache_write_1h_tokens=as_int(cell(row, "cache_write_1h_tokens")),
                )
                if sample_cost is None:
                    # One unpriceable sample makes the whole task
                    # unpriceable -- a partial cost is a wrong cost.
                    costs[task_id] = None
                else:
                    running = costs.get(task_id)
                    costs[task_id] = sample_cost if running is None else running + sample_cost
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
            # The 5m/1h columns are a BREAKDOWN of cache_write_tokens,
            # never an addition to it -- summing all three would count
            # every cache write twice. Where all three are present they
            # must agree; the store refuses a write that disagrees, so a
            # mismatch here means an older or foreign database, and
            # preferring one over the other would be picking a winner
            # between two contradictory measurements.
            total = bucket["cache_write_total_tokens"]
            five, hour = bucket["cache_write_5m_tokens"], bucket["cache_write_1h_tokens"]
            if total is not None and five is not None and hour is not None and five + hour != total:
                flags.setdefault(task_id, set()).add(FLAG_CACHE_WRITE_INCONSISTENT)
            usage[task_id] = TaskUsage(**bucket)
        for task_id, seen in models.items():
            attributed = {model for model in seen if model != MODEL_UNKNOWN}
            if MODEL_UNKNOWN in seen:
                flags.setdefault(task_id, set()).add(FLAG_MODEL_UNATTRIBUTED)
            if len(attributed) > 1:
                flags.setdefault(task_id, set()).add(FLAG_MIXED_MODEL)
            if any(pricing.lookup(model) is None for model in attributed):
                flags.setdefault(task_id, set()).add(FLAG_UNKNOWN_MODEL_PRICE)
        for task_id, task_flags in flags.items():
            if FLAG_CACHE_WRITE_INCONSISTENT in task_flags:
                costs[task_id] = None
        return SampleRollup(
            usage=usage,
            turns=turns,
            flags={k: tuple(sorted(v)) for k, v in flags.items()},
            confidence=confidence,
            models={k: tuple(sorted(v)) for k, v in models.items()},
            costs=costs,
        )

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
        samples: SampleRollup,
        all_reentries: dict[str, list[Reentry]],
    ) -> TaskRecord:
        usage = samples.usage.get(task_id, TaskUsage())
        turn_count = samples.turns.get(task_id)
        reentries = tuple(all_reentries.get(task_id, ()))
        sample_flags = samples.flags.get(task_id, ())
        confidence = samples.confidence.get(task_id)
        models = samples.models.get(task_id, ())
        attributed_models = tuple(model for model in models if model != MODEL_UNKNOWN)
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
        stored_first_pass = (
            _tri_state(cell(row, "first_pass_success")) if "first_pass_success" in columns else None
        )
        terminal_status = (
            as_text(cell(row, "terminal_status")) if "terminal_status" in columns else None
        )
        record = TaskRecord(
            task_id=task_id,
            cohort=normalise_cohort(cohort),
            project=as_text(cell(row, "project_id")) if "project_id" in columns else None,
            model=attributed_models[0] if len(attributed_models) == 1 else self.price_model,
            cost_units_override=samples.costs.get(task_id),
            cost_units_unavailable=task_id in samples.costs
            and samples.costs[task_id] is None,
            usage=usage,
            worker_turn_count=turn_count,
            reentries=reentries,
            first_pass_success=stored_first_pass,
            terminal_status=terminal_status,
            duration_seconds=duration,
            # There is no separate retry concept in this store: a retry
            # that costs a worker round trip IS a re-entry, so inventing
            # a retries number here would double count rework.
            retries=None,
            source=self.name,
            flags=tuple(dict.fromkeys(flags)),
        )
        # Recomputed from this task's own re-entry rows, then compared
        # with the scalar the reporter wrote about itself.
        if record.first_pass_success_disagrees:
            record = record.with_flags(FLAG_FPS_DISAGREEMENT)
        return record


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
