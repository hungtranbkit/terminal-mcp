"""Work Efficiency Telemetry -- durable storage + schema (TASK A:
"add additive telemetry needed to measure whether Analysis Gate +
implementation contracts reduce model turns/token cost").

WHAT THIS MEASURES AND WHY IT IS ITS OWN STORE
----------------------------------------------
The question this exists to answer is an EMPIRICAL one: does making a
worker understand the problem first (`analysis_gate.py`) and handing it
an explicit implementation contract actually cost fewer model turns and
fewer tokens per delivered task than not doing so? Nobody can answer
that from the queue's own tables -- they record what a task DID, never
what it COST.

This module is deliberately a SATELLITE of the queue, in its own
database file (`work_telemetry.db`), not another set of columns on
`queue_tasks`:

  * It owns NOTHING about how work is decided, dispatched, transitioned,
    verified or delivered. It cannot block a task, cannot change a
    status, and is never on the dispatch path. Deleting this file would
    lose measurements and change no behaviour whatsoever.
  * `queue_store.QUEUE_MIGRATIONS` is a single, globally ordered list.
    Several lanes are editing it concurrently right now; a telemetry
    migration appended there would both conflict textually and race for
    a version number with whatever another lane appends. A separate file
    with its own migration list starting at 1 has neither problem.
  * Telemetry write volume (one row per usage sample) is far higher than
    queue write volume, and it must never contend for the same WAL as
    the dispatch loop.

`task_id` and `work_id` are therefore plain TEXT, with NO foreign key
into the queue database (cross-database references do not exist in
SQLite, and inventing one via triggers would couple exactly what this
module refuses to couple). `task_id` is a `queue_tasks.id`; `work_id`
is the `outcomes.id` that task rolls up into -- the "Work" noun in
this project is the OUTCOME (`outcomes.py`), the user-visible
deliverable N tasks produce. Both are recorded as given and never
validated against the queue: a task that predates this store simply has
no telemetry rows, and every query below reports that as UNKNOWN.

NULL/UNKNOWN IS NEVER ZERO (explicit acceptance requirement)
-----------------------------------------------------------
A missing measurement and a measurement of zero are different facts and
this store never conflates them. Concretely:

  * Token columns are nullable everywhere and have NO DEFAULT. A sample
    that reports input tokens but not cache tokens stores NULL for the
    cache columns, not 0.
  * The per-task token totals are NOT denormalized onto the task row at
    all -- they are derived with SUM() over `telemetry_usage_samples`,
    which returns NULL (not 0) when no sample exists. That is the whole
    reason the rollup row carries no token columns: a DEFAULT 0 column
    would make "never measured" indistinguishable from "measured, cost
    nothing" the moment anyone forgot one UPDATE.
  * `first_pass_success` is a real tri-state -- 'UNKNOWN' until a
    terminal status actually resolves it, never a boolean defaulting to
    false.

DOUBLE COUNTING: THE DELTA/IDEMPOTENCY RULE
-------------------------------------------
Agent CLIs report usage as a CUMULATIVE, monotonically-climbing counter
("this session has used 41,201 input tokens so far"), and they report it
repeatedly. Summing those snapshots is the obvious way to get a number
that is wrong by an order of magnitude. Two independent primitives
prevent it, and they solve two genuinely different problems:

  1. `idempotency_key` (UNIQUE, the same primitive `event_bus.publish`
     uses): protects against the SAME report arriving twice -- a
     producer retry, an at-least-once bus redelivery, a crash between
     "wrote the file" and "recorded it". A duplicate key is a no-op that
     returns the original row; it never re-applies a delta and never
     re-increments a counter. When a caller supplies no key, one is
     derived deterministically from the full content of the report, so
     a byte-identical replay still dedupes.
  2. `counter_id` + `telemetry_counters` (the DELTA rule): protects
     against DIFFERENT reports of the same climbing counter. A caller
     sending `kind=CUMULATIVE` must name the counter being snapshotted
     (`counter_id` -- typically the agent session/process whose usage
     file is being read). This store keeps that counter's last observed
     value and stores only `new - last` as the sample's contribution.
     Ten snapshots of a session that climbs 100 -> 1000 contribute 1000
     in total, not 5500.

  A cumulative counter that goes DOWN means the thing counting restarted
  (the agent process was killed and relaunched, which is routine here).
  The new value is then genuine new usage measured from a fresh zero, so
  it is taken in full as the delta and the sample is flagged
  `counter_reset = 1` -- visible in the data rather than silently
  smoothed away, because a reset is exactly the kind of event that
  should make a human distrust a number.

Callers that already compute their own increments pass `kind=DELTA` and
are stored verbatim; they get primitive 1 but not primitive 2.

Schema/persistence posture: identical to `queue_store.QueueStore` (0700
state dir, 0600 db file, WAL, row_factory=Row, migrations through
`schema.py`'s tracked Migration/apply_migrations on PRAGMA
user_version).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import Migration, apply_migrations

# -- Phases -------------------------------------------------------------
#
# The phase a sample is attributed to is what makes "did the Analysis
# Gate pay for itself?" answerable at all: analysis/contract tokens are
# the INVESTMENT, worker tokens are the COST it is supposed to reduce.

PHASE_ANALYSIS = "ANALYSIS"
PHASE_CONTRACT = "CONTRACT"
PHASE_IMPLEMENTATION = "IMPLEMENTATION"
PHASE_VERIFICATION = "VERIFICATION"
PHASE_DELIVERY = "DELIVERY"
PHASE_UNKNOWN = "UNKNOWN"

ALL_PHASES = (PHASE_ANALYSIS, PHASE_CONTRACT, PHASE_IMPLEMENTATION,
              PHASE_VERIFICATION, PHASE_DELIVERY, PHASE_UNKNOWN)

WORKER_PHASES = (PHASE_IMPLEMENTATION, PHASE_VERIFICATION, PHASE_DELIVERY)
"""Which phases count as `worker_*` in a summary. Deliberately NOT
"everything that isn't analysis/contract": PHASE_UNKNOWN is excluded,
because attributing unattributed usage to the worker would inflate
exactly the number the Analysis Gate is meant to reduce -- i.e. it would
bias the experiment's own outcome. Unattributed usage is reported
separately as `unattributed_tokens`."""

# -- Re-entry reasons ---------------------------------------------------

REENTRY_TEST_FAILURE = "TEST_FAILURE"
REENTRY_CONTRACT_GAP = "CONTRACT_GAP"
REENTRY_IMPLEMENTATION_BUG = "IMPLEMENTATION_BUG"
REENTRY_ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
REENTRY_USER_CHANGED_REQUIREMENT = "USER_CHANGED_REQUIREMENT"
REENTRY_DELIVERY_FAILURE = "DELIVERY_FAILURE"
REENTRY_MERGE_CONFLICT = "MERGE_CONFLICT"
REENTRY_OTHER = "OTHER"

REENTRY_REASONS = (REENTRY_TEST_FAILURE, REENTRY_CONTRACT_GAP, REENTRY_IMPLEMENTATION_BUG,
                   REENTRY_ENVIRONMENT_FAILURE, REENTRY_USER_CHANGED_REQUIREMENT,
                   REENTRY_DELIVERY_FAILURE, REENTRY_MERGE_CONFLICT, REENTRY_OTHER)

# -- Sample kinds -------------------------------------------------------

KIND_DELTA = "DELTA"
KIND_CUMULATIVE = "CUMULATIVE"
SAMPLE_KINDS = (KIND_DELTA, KIND_CUMULATIVE)

# -- first_pass_success tri-state ---------------------------------------

FIRST_PASS_UNKNOWN = "UNKNOWN"
FIRST_PASS_TRUE = "TRUE"
FIRST_PASS_FALSE = "FALSE"
FIRST_PASS_VALUES = (FIRST_PASS_UNKNOWN, FIRST_PASS_TRUE, FIRST_PASS_FALSE)

# -- Terminal statuses --------------------------------------------------

STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
TERMINAL_STATUSES = (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED)

# -- Confidence ---------------------------------------------------------
#
# Ordered strongest -> weakest. An aggregate over several samples reports
# the WEAKEST confidence any of its inputs carried: a total that mixes
# one measured number with one estimated one is an estimate, and saying
# otherwise is the kind of quiet over-claim this whole store exists to
# avoid.

CONFIDENCE_MEASURED = "MEASURED"    # read from the agent's own usage output
CONFIDENCE_DERIVED = "DERIVED"      # computed from measured values (e.g. a delta)
CONFIDENCE_ESTIMATED = "ESTIMATED"  # approximated (e.g. token count from text length)
CONFIDENCE_UNKNOWN = "UNKNOWN"      # provenance not stated
CONFIDENCE_ORDER = (CONFIDENCE_MEASURED, CONFIDENCE_DERIVED, CONFIDENCE_ESTIMATED, CONFIDENCE_UNKNOWN)

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


class TelemetryValidationError(ValueError):
    """A caller sent something this store will not guess about: an
    unknown phase/reason/kind, a negative token count, a cumulative
    sample with no counter_id. Raised rather than coerced -- silently
    mapping an unrecognised reentry reason to OTHER would destroy the
    one field the experiment is trying to read."""


def default_telemetry_db_path() -> Path:
    """Same lookup order as every other default_*_path() in this project
    (explicit env var, then XDG_STATE_HOME, then ~/.local/state) -- which
    is what lets tests/conftest.py's XDG_STATE_HOME redirect isolate this
    store automatically, exactly as it already does for every other one."""
    override = os.environ.get("TERMINAL_MCP_WORK_TELEMETRY_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "work_telemetry.db"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_v1_schema(connection: sqlite3.Connection) -> None:
    """Initial Work Efficiency Telemetry schema.

    Three tables, one job each:

      telemetry_tasks    -- the per-task rollup. Holds ONLY identity,
                            lifecycle and event COUNTS. No token columns
                            at all, on purpose (see module docstring:
                            totals are SUM()ed from the samples so that
                            "never measured" reads back NULL, not 0).
      telemetry_usage_samples -- the append-only measurement log and the
                            single source of truth for every token
                            number. UNIQUE(idempotency_key) is the
                            duplicate-report primitive.
      telemetry_counters -- last observed value per cumulative counter;
                            the delta primitive.

    Every event table carries its own UNIQUE idempotency_key rather than
    sharing one namespace, so a usage sample and a reentry can safely be
    keyed off the same upstream event id without colliding.

    EVERY statement here is IF NOT EXISTS, and that is load-bearing
    rather than decorative. `apply_migrations` runs each migration inside
    `with connection:` -- but Python's sqlite3 in its default (legacy)
    isolation mode does not open a transaction for DDL, so CREATE TABLE
    commits immediately and is NOT rolled back when the migration body
    raises afterwards. A crash partway through this function therefore
    leaves some tables on disk with `user_version` still at 0, and the
    retry re-runs the whole function. With bare CREATE TABLE that retry
    dies on "table already exists" and the database is unopenable
    forever; with IF NOT EXISTS it completes and stamps the version.
    (Found by test_a_partially_applied_migration_is_resumable, not by
    reasoning about it afterwards.)"""
    connection.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_tasks (
            task_id TEXT PRIMARY KEY,
            work_id TEXT,
            session_id TEXT,
            project_id TEXT,
            phase TEXT NOT NULL DEFAULT 'UNKNOWN',
            started_at TEXT,
            completed_at TEXT,
            terminal_status TEXT,
            reentry_count INTEGER NOT NULL DEFAULT 0,
            contract_gap_count INTEGER NOT NULL DEFAULT 0,
            first_pass_success TEXT NOT NULL DEFAULT 'UNKNOWN',
            evidence_source TEXT,
            confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK (phase IN ('ANALYSIS','CONTRACT','IMPLEMENTATION','VERIFICATION','DELIVERY','UNKNOWN')),
            CHECK (first_pass_success IN ('UNKNOWN','TRUE','FALSE')),
            CHECK (confidence IN ('MEASURED','DERIVED','ESTIMATED','UNKNOWN')),
            CHECK (terminal_status IS NULL OR terminal_status IN ('COMPLETED','FAILED','CANCELLED'))
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_tasks_work ON telemetry_tasks(work_id)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_tasks_project ON telemetry_tasks(project_id, terminal_status)")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_usage_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            work_id TEXT,
            session_id TEXT,
            phase TEXT NOT NULL,
            kind TEXT NOT NULL,
            counter_id TEXT,
            observed_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            turn_count INTEGER,
            reported_input_tokens INTEGER,
            reported_output_tokens INTEGER,
            reported_cache_read_tokens INTEGER,
            reported_cache_write_tokens INTEGER,
            reported_turn_count INTEGER,
            counter_reset INTEGER NOT NULL DEFAULT 0,
            evidence_source TEXT,
            confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
            metadata TEXT,
            CHECK (phase IN ('ANALYSIS','CONTRACT','IMPLEMENTATION','VERIFICATION','DELIVERY','UNKNOWN')),
            CHECK (kind IN ('DELTA','CUMULATIVE')),
            CHECK (confidence IN ('MEASURED','DERIVED','ESTIMATED','UNKNOWN')),
            CHECK (input_tokens IS NULL OR input_tokens >= 0),
            CHECK (output_tokens IS NULL OR output_tokens >= 0),
            CHECK (cache_read_tokens IS NULL OR cache_read_tokens >= 0),
            CHECK (cache_write_tokens IS NULL OR cache_write_tokens >= 0),
            CHECK (turn_count IS NULL OR turn_count >= 0)
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_samples_task ON telemetry_usage_samples(task_id, phase)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_samples_work ON telemetry_usage_samples(work_id)")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_reentries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            work_id TEXT,
            reason TEXT NOT NULL,
            phase TEXT,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            detail TEXT,
            evidence_source TEXT,
            confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
            CHECK (reason IN ('TEST_FAILURE','CONTRACT_GAP','IMPLEMENTATION_BUG','ENVIRONMENT_FAILURE',
                              'USER_CHANGED_REQUIREMENT','DELIVERY_FAILURE','MERGE_CONFLICT','OTHER')),
            CHECK (confidence IN ('MEASURED','DERIVED','ESTIMATED','UNKNOWN'))
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_reentries_task ON telemetry_reentries(task_id)")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_contract_gaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            idempotency_key TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL,
            work_id TEXT,
            phase TEXT,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            detail TEXT,
            evidence_source TEXT,
            confidence TEXT NOT NULL DEFAULT 'UNKNOWN',
            CHECK (confidence IN ('MEASURED','DERIVED','ESTIMATED','UNKNOWN'))
        )
    """)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_gaps_task ON telemetry_contract_gaps(task_id)")

    connection.execute("""
        CREATE TABLE IF NOT EXISTS telemetry_counters (
            counter_id TEXT PRIMARY KEY,
            last_task_id TEXT,
            last_input_tokens INTEGER,
            last_output_tokens INTEGER,
            last_cache_read_tokens INTEGER,
            last_cache_write_tokens INTEGER,
            last_turn_count INTEGER,
            updated_at TEXT NOT NULL
        )
    """)


TELEMETRY_MIGRATIONS = [
    Migration(1, "Work Efficiency Telemetry v1: telemetry_tasks/telemetry_usage_samples/"
                 "telemetry_reentries/telemetry_contract_gaps/telemetry_counters",
              _create_v1_schema),
]


def _validate_choice(value: str, allowed: Iterable[str], field: str) -> str:
    if value not in allowed:
        raise TelemetryValidationError(
            f"{field}={value!r} is not one of {tuple(allowed)!r} -- this store never guesses a "
            f"value it was not given (see TelemetryValidationError)")
    return value


def _validate_count(value: Any, field: str) -> int | None:
    """None stays None -- an unmeasured counter is NOT zero. A present
    value must be a non-negative int; a float/str is rejected rather than
    coerced, because a silently truncated token count is worse than a
    loud one."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TelemetryValidationError(f"{field} must be an int or None, got {type(value).__name__}")
    if value < 0:
        raise TelemetryValidationError(f"{field} must be >= 0, got {value}")
    return value


def _weakest_confidence(*values: str | None) -> str:
    """The weakest (most cautious) of the given confidences; UNKNOWN for
    anything unrecognised or absent."""
    worst = 0
    for value in values:
        if value is None:
            continue
        worst = max(worst, CONFIDENCE_ORDER.index(value) if value in CONFIDENCE_ORDER
                    else CONFIDENCE_ORDER.index(CONFIDENCE_UNKNOWN))
    return CONFIDENCE_ORDER[worst]


def _derive_idempotency_key(parts: dict[str, Any]) -> str:
    """A deterministic key over the FULL content of a report, used only
    when a caller supplies none. Two byte-identical reports therefore
    dedupe to one -- the conservative choice for a cumulative snapshot
    (replaying it must never add usage twice). A caller that genuinely
    needs two identical-valued records (e.g. two real, separate 500-token
    DELTA increments in the same second) must supply its own distinct
    key; that is exactly why this is a fallback and not the only mode."""
    canonical = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return "auto:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class WorkTelemetryStore:
    """SQLite persistence for work-efficiency telemetry -- same posture
    as queue_store.QueueStore (0700 state dir, 0600 db file, WAL,
    row_factory=Row, tracked migrations via schema.py).

    Every public method is idempotent by key and safe to call from a
    producer that may crash and retry at any point."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_telemetry_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, TELEMETRY_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- task rollup ----------------------------------------------------

    def _upsert_task(self, connection: sqlite3.Connection, task_id: str, *,
                     work_id: str | None = None, session_id: str | None = None,
                     project_id: str | None = None, phase: str | None = None,
                     started_at: str | None = None, evidence_source: str | None = None,
                     confidence: str | None = None) -> None:
        """Creates the rollup row if absent, otherwise fills in identity
        fields that were previously unknown.

        NEVER overwrites a known value with NULL and never resets
        `started_at`: producers legitimately learn the work_id/session_id
        later than the first sample (a worker can start burning tokens
        before an outcome is attached), and a late-arriving report with
        fewer fields filled in must not erase what an earlier one already
        established. `phase` is the one field that DOES move forward --
        it is the task's current phase by definition."""
        now = _iso_now()
        row = connection.execute(
            "SELECT task_id FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO telemetry_tasks (task_id, work_id, session_id, project_id, phase, "
                "started_at, evidence_source, confidence, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, work_id, session_id, project_id, phase or PHASE_UNKNOWN,
                 started_at or now, evidence_source, confidence or CONFIDENCE_UNKNOWN, now, now))
            return
        connection.execute(
            "UPDATE telemetry_tasks SET "
            "work_id = COALESCE(work_id, ?), session_id = COALESCE(session_id, ?), "
            "project_id = COALESCE(project_id, ?), started_at = COALESCE(started_at, ?), "
            "evidence_source = COALESCE(evidence_source, ?), "
            "phase = COALESCE(?, phase), "
            "confidence = ?, updated_at = ? WHERE task_id = ?",
            (work_id, session_id, project_id, started_at or now, evidence_source, phase,
             _weakest_confidence(
                 connection.execute("SELECT confidence FROM telemetry_tasks WHERE task_id = ?",
                                    (task_id,)).fetchone()[0], confidence),
             now, task_id))

    def start_task(self, task_id: str, *, work_id: str | None = None, session_id: str | None = None,
                   project_id: str | None = None, phase: str = PHASE_ANALYSIS,
                   started_at: str | None = None, evidence_source: str | None = None,
                   confidence: str = CONFIDENCE_UNKNOWN) -> dict[str, Any]:
        """Opens (or enriches) a task's telemetry rollup. Idempotent:
        calling it again after a producer restart keeps the ORIGINAL
        started_at, so a restart never makes a task look faster or newer
        than it was."""
        _validate_choice(phase, ALL_PHASES, "phase")
        _validate_choice(confidence, CONFIDENCE_ORDER, "confidence")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_task(connection, task_id, work_id=work_id, session_id=session_id,
                              project_id=project_id, phase=phase, started_at=started_at,
                              evidence_source=evidence_source, confidence=confidence)
            return _row_to_dict(connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone())

    def set_phase(self, task_id: str, phase: str) -> dict[str, Any]:
        """Moves the task's CURRENT phase marker. Purely descriptive --
        it gates nothing and is not a state machine; per-sample `phase`
        is what attributes cost, not this."""
        _validate_choice(phase, ALL_PHASES, "phase")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_task(connection, task_id, phase=phase)
            return _row_to_dict(connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone())

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return _row_to_dict(connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone())

    # -- usage samples --------------------------------------------------

    def record_usage(self, task_id: str, *, phase: str, kind: str = KIND_DELTA,
                     counter_id: str | None = None, idempotency_key: str | None = None,
                     input_tokens: int | None = None, output_tokens: int | None = None,
                     cache_read_tokens: int | None = None, cache_write_tokens: int | None = None,
                     turn_count: int | None = None, work_id: str | None = None,
                     session_id: str | None = None, project_id: str | None = None,
                     observed_at: str | None = None, evidence_source: str | None = None,
                     confidence: str = CONFIDENCE_UNKNOWN,
                     metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Records one usage report and returns
        `{"applied", "duplicate", "sample", "counter_reset"}`.

        `kind=DELTA`     -- the values ARE the increment; stored verbatim.
        `kind=CUMULATIVE`-- the values are a snapshot of the counter named
                            by the REQUIRED `counter_id`; only the rise
                            since that counter's last observation is
                            stored (see module docstring).

        A repeat of an already-seen `idempotency_key` returns
        `applied=False, duplicate=True` with the ORIGINAL sample and
        changes nothing -- the counter baseline included. This is the
        crash/retry path: a producer that dies after committing and
        retries on restart re-sends the same key and adds nothing.

        Unmeasured fields must be passed as None and are stored as NULL.
        Passing 0 asserts a real measurement of zero and is kept as 0."""
        _validate_choice(phase, ALL_PHASES, "phase")
        _validate_choice(kind, SAMPLE_KINDS, "kind")
        _validate_choice(confidence, CONFIDENCE_ORDER, "confidence")
        if kind == KIND_CUMULATIVE and not counter_id:
            raise TelemetryValidationError(
                "kind=CUMULATIVE requires counter_id -- without the identity of the counter being "
                "snapshotted there is no way to tell a rise from a repeat, which is precisely the "
                "double-counting this store exists to prevent")
        reported = {
            "input_tokens": _validate_count(input_tokens, "input_tokens"),
            "output_tokens": _validate_count(output_tokens, "output_tokens"),
            "cache_read_tokens": _validate_count(cache_read_tokens, "cache_read_tokens"),
            "cache_write_tokens": _validate_count(cache_write_tokens, "cache_write_tokens"),
        }
        reported_turn = _validate_count(turn_count, "turn_count")
        observed = observed_at or _iso_now()
        key = idempotency_key or _derive_idempotency_key({
            "task_id": task_id, "phase": phase, "kind": kind, "counter_id": counter_id,
            "observed_at": observed, "turn_count": reported_turn, **reported,
        })

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM telemetry_usage_samples WHERE idempotency_key = ?", (key,)).fetchone()
            if existing is not None:
                return {"applied": False, "duplicate": True, "sample": _row_to_dict(existing),
                        "counter_reset": bool(existing["counter_reset"])}

            self._upsert_task(connection, task_id, work_id=work_id, session_id=session_id,
                              project_id=project_id, evidence_source=evidence_source,
                              confidence=confidence)

            deltas = dict(reported)
            delta_turn = reported_turn
            counter_reset = False
            if kind == KIND_CUMULATIVE:
                deltas, delta_turn, counter_reset = self._apply_counter(
                    connection, counter_id, task_id, reported, reported_turn, observed)

            connection.execute(
                "INSERT INTO telemetry_usage_samples ("
                "idempotency_key, task_id, work_id, session_id, phase, kind, counter_id, "
                "observed_at, recorded_at, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, turn_count, reported_input_tokens, reported_output_tokens, "
                "reported_cache_read_tokens, reported_cache_write_tokens, reported_turn_count, "
                "counter_reset, evidence_source, confidence, metadata) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, task_id, work_id, session_id, phase, kind, counter_id, observed, _iso_now(),
                 deltas["input_tokens"], deltas["output_tokens"], deltas["cache_read_tokens"],
                 deltas["cache_write_tokens"], delta_turn,
                 reported["input_tokens"], reported["output_tokens"], reported["cache_read_tokens"],
                 reported["cache_write_tokens"], reported_turn, int(counter_reset),
                 evidence_source, confidence,
                 json.dumps(metadata, sort_keys=True) if metadata else None))
            sample = connection.execute(
                "SELECT * FROM telemetry_usage_samples WHERE idempotency_key = ?", (key,)).fetchone()
            return {"applied": True, "duplicate": False, "sample": _row_to_dict(sample),
                    "counter_reset": counter_reset}

    def _apply_counter(self, connection: sqlite3.Connection, counter_id: str, task_id: str,
                       reported: dict[str, int | None], reported_turn: int | None,
                       observed_at: str) -> tuple[dict[str, int | None], int | None, bool]:
        """Turns a cumulative snapshot into the increment since this
        counter's last observation, and advances the baseline.

        Per-field, not per-row: a producer that reports input tokens but
        has no cache numbers must not have its cache baseline clobbered
        to NULL by that report -- an absent field leaves that field's
        baseline exactly as it was and contributes NULL (unknown), never
        0.

        A field that moved BACKWARDS means the counter restarted, so the
        whole new value is the increment (it was counted from zero
        again). That flips `counter_reset` for the row -- surfaced in the
        data rather than hidden, because a reset is a real reason to
        distrust a total."""
        row = connection.execute(
            "SELECT * FROM telemetry_counters WHERE counter_id = ?", (counter_id,)).fetchone()
        deltas: dict[str, int | None] = {}
        counter_reset = False
        for field in _TOKEN_FIELDS:
            value = reported[field]
            if value is None:
                deltas[field] = None
                continue
            previous = row[f"last_{field}"] if row is not None else None
            if previous is None:
                deltas[field] = value
            elif value < previous:
                deltas[field] = value
                counter_reset = True
            else:
                deltas[field] = value - previous
        if reported_turn is None:
            delta_turn = None
        else:
            previous_turn = row["last_turn_count"] if row is not None else None
            if previous_turn is None:
                delta_turn = reported_turn
            elif reported_turn < previous_turn:
                delta_turn = reported_turn
                counter_reset = True
            else:
                delta_turn = reported_turn - previous_turn

        if row is None:
            connection.execute(
                "INSERT INTO telemetry_counters (counter_id, last_task_id, last_input_tokens, "
                "last_output_tokens, last_cache_read_tokens, last_cache_write_tokens, "
                "last_turn_count, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (counter_id, task_id, reported["input_tokens"], reported["output_tokens"],
                 reported["cache_read_tokens"], reported["cache_write_tokens"], reported_turn,
                 observed_at))
        else:
            connection.execute(
                "UPDATE telemetry_counters SET last_task_id = ?, "
                "last_input_tokens = COALESCE(?, last_input_tokens), "
                "last_output_tokens = COALESCE(?, last_output_tokens), "
                "last_cache_read_tokens = COALESCE(?, last_cache_read_tokens), "
                "last_cache_write_tokens = COALESCE(?, last_cache_write_tokens), "
                "last_turn_count = COALESCE(?, last_turn_count), updated_at = ? "
                "WHERE counter_id = ?",
                (task_id, reported["input_tokens"], reported["output_tokens"],
                 reported["cache_read_tokens"], reported["cache_write_tokens"], reported_turn,
                 observed_at, counter_id))
        return deltas, delta_turn, counter_reset

    # -- re-entry / contract gaps ---------------------------------------

    def record_reentry(self, task_id: str, *, reason: str, idempotency_key: str | None = None,
                       phase: str | None = None, work_id: str | None = None,
                       occurred_at: str | None = None, detail: str | None = None,
                       evidence_source: str | None = None,
                       confidence: str = CONFIDENCE_UNKNOWN) -> dict[str, Any]:
        """Records that the task had to be handed back to a worker again,
        and increments `reentry_count` EXACTLY ONCE per distinct
        idempotency_key -- the increment happens in the same transaction
        as the insert that claims the key, so a crash cannot produce a
        row without its increment or an increment without its row, and a
        retry with the same key adds neither.

        A CONTRACT_GAP re-entry also increments `contract_gap_count`: the
        contract having been incomplete is the fact under test, and a gap
        serious enough to force a re-entry is the strongest evidence of
        it there is. Gaps found WITHOUT a re-entry go through
        `record_contract_gap` and are counted there only."""
        _validate_choice(reason, REENTRY_REASONS, "reason")
        _validate_choice(confidence, CONFIDENCE_ORDER, "confidence")
        if phase is not None:
            _validate_choice(phase, ALL_PHASES, "phase")
        occurred = occurred_at or _iso_now()
        key = idempotency_key or _derive_idempotency_key(
            {"kind": "reentry", "task_id": task_id, "reason": reason, "occurred_at": occurred,
             "detail": detail})
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM telemetry_reentries WHERE idempotency_key = ?", (key,)).fetchone()
            if existing is not None:
                task = connection.execute(
                    "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone()
                return {"applied": False, "duplicate": True, "reentry": _row_to_dict(existing),
                        "task": _row_to_dict(task)}
            self._upsert_task(connection, task_id, work_id=work_id,
                              evidence_source=evidence_source, confidence=confidence)
            connection.execute(
                "INSERT INTO telemetry_reentries (idempotency_key, task_id, work_id, reason, phase, "
                "occurred_at, recorded_at, detail, evidence_source, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, task_id, work_id, reason, phase, occurred, _iso_now(), detail,
                 evidence_source, confidence))
            gap_increment = 1 if reason == REENTRY_CONTRACT_GAP else 0
            connection.execute(
                "UPDATE telemetry_tasks SET reentry_count = reentry_count + 1, "
                "contract_gap_count = contract_gap_count + ?, updated_at = ? WHERE task_id = ?",
                (gap_increment, _iso_now(), task_id))
            reentry = connection.execute(
                "SELECT * FROM telemetry_reentries WHERE idempotency_key = ?", (key,)).fetchone()
            task = connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone()
            return {"applied": True, "duplicate": False, "reentry": _row_to_dict(reentry),
                    "task": _row_to_dict(task)}

    def record_contract_gap(self, task_id: str, *, idempotency_key: str | None = None,
                            phase: str | None = None, work_id: str | None = None,
                            occurred_at: str | None = None, detail: str | None = None,
                            evidence_source: str | None = None,
                            confidence: str = CONFIDENCE_UNKNOWN) -> dict[str, Any]:
        """Records a contract gap that did NOT (or has not yet) forced a
        re-entry -- something the implementation contract failed to
        specify, noticed and worked around in place. Same exactly-once
        semantics as `record_reentry`."""
        _validate_choice(confidence, CONFIDENCE_ORDER, "confidence")
        if phase is not None:
            _validate_choice(phase, ALL_PHASES, "phase")
        occurred = occurred_at or _iso_now()
        key = idempotency_key or _derive_idempotency_key(
            {"kind": "contract_gap", "task_id": task_id, "occurred_at": occurred, "detail": detail})
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM telemetry_contract_gaps WHERE idempotency_key = ?", (key,)).fetchone()
            if existing is not None:
                task = connection.execute(
                    "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone()
                return {"applied": False, "duplicate": True, "gap": _row_to_dict(existing),
                        "task": _row_to_dict(task)}
            self._upsert_task(connection, task_id, work_id=work_id,
                              evidence_source=evidence_source, confidence=confidence)
            connection.execute(
                "INSERT INTO telemetry_contract_gaps (idempotency_key, task_id, work_id, phase, "
                "occurred_at, recorded_at, detail, evidence_source, confidence) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (key, task_id, work_id, phase, occurred, _iso_now(), detail, evidence_source,
                 confidence))
            connection.execute(
                "UPDATE telemetry_tasks SET contract_gap_count = contract_gap_count + 1, "
                "updated_at = ? WHERE task_id = ?", (_iso_now(), task_id))
            gap = connection.execute(
                "SELECT * FROM telemetry_contract_gaps WHERE idempotency_key = ?", (key,)).fetchone()
            task = connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone()
            return {"applied": True, "duplicate": False, "gap": _row_to_dict(gap),
                    "task": _row_to_dict(task)}

    # -- completion -----------------------------------------------------

    def complete_task(self, task_id: str, *, status: str = STATUS_COMPLETED,
                      completed_at: str | None = None, work_id: str | None = None,
                      evidence_source: str | None = None,
                      confidence: str = CONFIDENCE_UNKNOWN) -> dict[str, Any]:
        """Closes the task's telemetry and DERIVES `first_pass_success`.

        The rule, stated once and implemented nowhere else:

            COMPLETED with reentry_count == 0  -> TRUE
            COMPLETED with reentry_count  > 0  -> FALSE
            FAILED                             -> FALSE
            CANCELLED                          -> stays UNKNOWN

        Why CANCELLED stays UNKNOWN: a cancelled task never got the
        chance to succeed or fail on its first pass, so both TRUE and
        FALSE would be a claim about something that did not happen. The
        tri-state exists precisely so this case has somewhere honest to
        live, and folding it into FALSE would quietly bias the
        Analysis-Gate comparison against whichever arm happened to have
        more cancellations.

        Why contract gaps do NOT falsify it: a gap that was absorbed
        without a re-entry cost no extra worker round-trip, which is the
        thing `first_pass_success` measures. `contract_gap_count` stays
        available alongside as the explanatory variable.

        Idempotent: re-completing an already-completed task is a no-op
        that keeps the first `completed_at` and the verdict derived then.
        A re-entry recorded AFTER completion (a delivery failure that
        reopens the task) does not retroactively rewrite the verdict --
        call `complete_task` again after the task genuinely re-completes
        via `reopen_task`, which is the only thing that clears it."""
        _validate_choice(status, TERMINAL_STATUSES, "status")
        _validate_choice(confidence, CONFIDENCE_ORDER, "confidence")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._upsert_task(connection, task_id, work_id=work_id,
                              evidence_source=evidence_source, confidence=confidence)
            row = connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if row["completed_at"] is not None:
                return _row_to_dict(row)
            if status == STATUS_COMPLETED:
                verdict = FIRST_PASS_TRUE if row["reentry_count"] == 0 else FIRST_PASS_FALSE
            elif status == STATUS_FAILED:
                verdict = FIRST_PASS_FALSE
            else:
                verdict = FIRST_PASS_UNKNOWN
            now = _iso_now()
            connection.execute(
                "UPDATE telemetry_tasks SET completed_at = ?, terminal_status = ?, "
                "first_pass_success = ?, updated_at = ? WHERE task_id = ?",
                (completed_at or now, status, verdict, now, task_id))
            return _row_to_dict(connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone())

    def reopen_task(self, task_id: str) -> dict[str, Any] | None:
        """Clears the terminal fields so a re-completed task can be
        re-derived. Deliberately explicit rather than letting
        `record_reentry` silently un-complete a task: a re-entry after
        delivery is a real, rare event whose handling should be a
        decision the caller makes, not a side effect."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE telemetry_tasks SET completed_at = NULL, terminal_status = NULL, "
                "first_pass_success = 'UNKNOWN', updated_at = ? WHERE task_id = ?",
                (_iso_now(), task_id))
            return _row_to_dict(connection.execute(
                "SELECT * FROM telemetry_tasks WHERE task_id = ?", (task_id,)).fetchone())

    # -- reads ----------------------------------------------------------

    def list_samples(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM telemetry_usage_samples WHERE task_id = ? ORDER BY id", (task_id,))]

    def list_reentries(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM telemetry_reentries WHERE task_id = ? ORDER BY id", (task_id,))]

    def list_contract_gaps(self, task_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM telemetry_contract_gaps WHERE task_id = ? ORDER BY id", (task_id,))]

    def get_counter(self, counter_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return _row_to_dict(connection.execute(
                "SELECT * FROM telemetry_counters WHERE counter_id = ?", (counter_id,)).fetchone())

    def phase_totals(self, task_id: str) -> dict[str, dict[str, Any]]:
        """Per-phase SUM()s for one task. SUM over no rows is NULL in
        SQLite, which is exactly the wanted answer for "this phase was
        never measured" -- so this deliberately does NOT wrap it in
        COALESCE(...,0)."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT phase, SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
                "SUM(cache_read_tokens) AS cache_read_tokens, SUM(cache_write_tokens) AS cache_write_tokens, "
                "SUM(turn_count) AS turn_count, COUNT(*) AS sample_count, "
                "MAX(counter_reset) AS counter_reset, "
                "GROUP_CONCAT(DISTINCT evidence_source) AS evidence_sources, "
                "GROUP_CONCAT(DISTINCT confidence) AS confidences "
                "FROM telemetry_usage_samples WHERE task_id = ? GROUP BY phase", (task_id,)).fetchall()
        return {row["phase"]: dict(row) for row in rows}

    def list_tasks(self, *, project_id: str | None = None, work_id: str | None = None,
                   terminal_status: str | None = None, since: str | None = None,
                   limit: int | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if work_id is not None:
            clauses.append("work_id = ?")
            params.append(work_id)
        if terminal_status is not None:
            clauses.append("terminal_status = ?")
            params.append(terminal_status)
        if since is not None:
            clauses.append("COALESCE(completed_at, started_at, created_at) >= ?")
            params.append(since)
        sql = "SELECT * FROM telemetry_tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, task_id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(sql, params)]
