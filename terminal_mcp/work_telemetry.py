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

Everything else here is counted, not guessed: files read, search calls,
runbook hits and misses, redefine count, and the wall-clock to first preview.

WHO FILLS THESE ROWS. Not a worker's memory of its own task, which is the
weakest possible source. `work_telemetry_runtime.py` opens a row when the
QUEUE dispatches a task and closes it on the queue's own terminal
transition, so the lifecycle numbers are the runtime's record rather than a
report about it. A worker may still add what only it can see (its provider's
usage counters), and those arrive through the same provenance rules.

BASELINE. A "saving" means nothing without something to compare against, and
an invented comparison is worse than none. So a baseline exists here only if
it was MEASURED from recorded rows or its DEFINITION was stated by whoever
supplied it; otherwise the baseline -- and every saving derived from it --
is reported UNAVAILABLE and no number is shown.
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

from .bug_spec import (PLAN_ADJUSTED, PLAN_CONFIRMED, PLAN_MISMATCH,
                       PLAN_PENDING)
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

# The plan-verification vocabulary is IMPORTED, never restated. A worker
# answers one of these on the spec and on its telemetry row, and the two can
# only be compared if they are literally the same strings.
PLAN_STATUSES = (PLAN_CONFIRMED, PLAN_ADJUSTED, PLAN_MISMATCH, PLAN_PENDING)

OUTCOME_COMPLETED = "COMPLETED"

# The counters a signal may add to, and the name each is reported under. The
# stored field for search calls keeps its original name; everything that reads
# telemetry sees `search_calls`, which is the name the efficiency contract
# uses -- one number, never two that can drift apart.
COUNTER_FIELDS = ("files_read", "search_rounds", "runbook_hits", "runbook_misses",
                  "knowledge_hits", "context_pack_hits", "similar_bug_hits",
                  "cache_hits", "redefine_count", "assist_requests")
REPORTED_COUNTER_NAMES = {"search_rounds": "search_calls"}
# Signal names a caller may use, mapped to the field they add to. `search_calls`
# is accepted as well as `search_rounds` so an instrumented call site can use
# the contract's name without knowing the storage history.
SIGNAL_ALIASES = {name: name for name in COUNTER_FIELDS}
SIGNAL_ALIASES["search_calls"] = "search_rounds"


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
    def derived(cls, value: int, *, method: str) -> "TokenCount":
        """A figure computed from reported ones -- a sum, a difference, a rate.

        The arithmetic is exact; the FIGURE is still not something a provider
        said, so it carries the estimate label wherever it is read. That is
        the whole point: a reader must never have to work out for themselves
        whether a number was measured or assembled.
        """
        return cls(value=int(value), source=ESTIMATED, method=f"derived: {method}")

    @classmethod
    def unavailable(cls, why: str = "runtime did not report token usage") -> "TokenCount":
        return cls(value=None, source=UNAVAILABLE, method=why)

    @classmethod
    def from_dict(cls, raw: Any) -> "TokenCount":
        """Read one back out of a stored payload, tolerating an older shape."""
        if isinstance(raw, TokenCount):
            return raw
        if not isinstance(raw, dict):
            return cls.unavailable("no token usage in the stored record")
        value = raw.get("value")
        return cls(value=None if value is None else int(value),
                   source=str(raw.get("source") or UNAVAILABLE),
                   method=str(raw.get("method") or ""))

    def as_dict(self) -> dict[str, Any]:
        return {"value": self.value, "source": self.source, "method": self.method}

    def display(self) -> str:
        """How it should appear anywhere a human reads it."""
        if self.value is None:
            return f"unavailable ({self.method})" if self.method else "unavailable"
        if self.source == ESTIMATED:
            return f"~{self.value:,} (estimated)"
        return f"{self.value:,}"


# The provider counters this system records. A runtime that reports something
# under a name nobody can map with certainty keeps that name in `ignored`
# rather than being folded into one of these by guesswork.
USAGE_COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "total_tokens")

# Spellings real provider runtimes use for the same counter. Only exact,
# unambiguous synonyms belong here -- a key whose meaning is a judgement call
# is left unmapped, because a mis-mapped counter is an invented number wearing
# a measurement's label.
USAGE_ALIASES = {
    "input_tokens": "input_tokens",
    "prompt_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "completion_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
    "total_tokens": "total_tokens",
}


def _is_count(value: Any) -> bool:
    """A usable counter: a real, non-negative integer. `True` is not one."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class ProviderUsage:
    """What the PROVIDER said this task cost -- and strictly nothing else.

    A counter is present only because a runtime reported it. Whatever was not
    reported is UNAVAILABLE, never zero: zero is itself a measurement, and it
    says "this cost nothing", which is a different and almost always false
    claim. Nothing here is ever back-filled from a text length, a duration, a
    price list or another task.
    """

    provider: str | None = None
    reported_at: str | None = None
    counters: dict[str, TokenCount] = field(default_factory=dict)
    ignored: tuple[str, ...] = ()
    note: str = "runtime did not report provider usage"

    @classmethod
    def unavailable(cls, why: str = "runtime did not report provider usage"
                    ) -> "ProviderUsage":
        return cls(note=why)

    @classmethod
    def from_report(cls, payload: Any, *, provider: str | None = None,
                    reported_at: str | None = None) -> "ProviderUsage":
        """Build from whatever a runtime actually handed back.

        Keys that are absent stay absent. Keys that are present but are not a
        plain non-negative integer are IGNORED and named in `ignored`, because
        a counter we could not read is a gap in the record, not a zero.
        """
        if not isinstance(payload, dict) or not payload:
            return cls.unavailable("runtime reported no usage payload")
        counters: dict[str, TokenCount] = {}
        ignored: list[str] = []
        by = provider or "runtime"
        for key, value in payload.items():
            name = USAGE_ALIASES.get(str(key))
            if name is None:
                ignored.append(str(key))
                continue
            if not _is_count(value):
                ignored.append(str(key))
                continue
            counters[name] = TokenCount.reported(int(value), by=by)
        if not counters:
            return cls(provider=provider, reported_at=reported_at,
                       ignored=tuple(sorted(ignored)),
                       note="runtime reported usage, but no counter in it was readable")
        missing = [n for n in USAGE_COUNTERS if n not in counters]
        note = f"{len(counters)} counter(s) reported by {by}"
        if missing:
            note += f"; unreported: {', '.join(missing)}"
        return cls(provider=provider, reported_at=reported_at, counters=counters,
                   ignored=tuple(sorted(ignored)), note=note)

    @classmethod
    def from_dict(cls, raw: Any) -> "ProviderUsage":
        if isinstance(raw, ProviderUsage):
            return raw
        if not isinstance(raw, dict):
            return cls.unavailable()
        counters = {name: TokenCount.from_dict(value)
                    for name, value in (raw.get("counters") or {}).items()
                    if isinstance(name, str)}
        return cls(provider=raw.get("provider"), reported_at=raw.get("reported_at"),
                   counters={n: c for n, c in counters.items() if c.value is not None},
                   ignored=tuple(raw.get("ignored") or ()),
                   note=str(raw.get("note") or cls.unavailable().note))

    # -- reading --------------------------------------------------------------

    def available(self) -> bool:
        return bool(self.counters)

    def unreported(self) -> tuple[str, ...]:
        return tuple(n for n in USAGE_COUNTERS if n not in self.counters)

    def get(self, name: str) -> TokenCount:
        """Any counter, reported or not -- an absent one says so itself."""
        return self.counters.get(name) or TokenCount.unavailable(
            f"{name} was not reported for this task")

    def total(self) -> TokenCount:
        """The task's total, reported if the provider gave one.

        Otherwise input+output, which is arithmetic on measurements but is
        NOT a figure the provider stated, so it is labelled an estimate. When
        neither exists the answer is UNAVAILABLE -- never the sum of whatever
        happened to be present, which would silently mean something else.
        """
        if "total_tokens" in self.counters:
            return self.counters["total_tokens"]
        parts = [n for n in ("input_tokens", "output_tokens") if n in self.counters]
        if len(parts) < 2:
            return TokenCount.unavailable(
                "no total reported, and input/output were not both reported")
        return TokenCount.derived(
            sum(int(self.counters[n].value or 0) for n in parts),
            method="input_tokens + output_tokens as reported by "
                   f"{self.provider or 'the runtime'}")

    def as_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "reported_at": self.reported_at,
                "available": self.available(),
                "counters": {n: c.as_dict() for n, c in self.counters.items()},
                "unreported": list(self.unreported()),
                "ignored": list(self.ignored),
                "total": self.total().as_dict(),
                "note": self.note}


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
    # Kept under its original name because it is persisted and reported under
    # it; `search_calls` in every payload is this same number, which is the
    # name the efficiency contract uses.
    search_rounds: int = 0
    runbook_hits: int = 0          # a registered procedure was reused
    runbook_misses: int = 0        # an operation was done by hand instead
    knowledge_hits: int = 0        # the knowledge map answered instead of a search
    context_pack_hits: int = 0     # a prepared module briefing was served
    similar_bug_hits: int = 0      # a past spec was offered as a starting point
    cache_hits: int = 0            # a green result reused rather than re-run
    redefine_count: int = 0
    assist_requests: int = 0
    # The worker's plan-verification verdict. None is NOT a fourth verdict --
    # it means nobody answered, which is a different fact from "the plan was
    # wrong" and is counted separately wherever these are summarised.
    plan_status: str | None = None
    plan_note: str = ""
    # Times this task was allowed past its file/search budget, each with a
    # stated reason. An unrecorded overrun cannot be represented here: the
    # counter only moves through record_budget_escalation(), which is what
    # makes "exceeded" distinguishable from "exceeded, and here is why".
    budget_escalations: int = 0
    tokens: TokenCount = field(default_factory=TokenCount.unavailable)
    # Provider counters, separate from `tokens` on purpose: `tokens` is one
    # summary figure whoever filled the row chose, while this is the runtime's
    # own per-counter report with each counter's provenance intact.
    usage: ProviderUsage = field(default_factory=ProviderUsage.unavailable)

    # -- what the runtime itself observed ------------------------------------
    # Filled by `work_telemetry_runtime` from the queue's own transitions, so
    # they are a record of what happened rather than a report about it.
    lane: str | None = None
    dispatch_count: int = 0        # DISPATCHING transitions seen for this task
    reopened: int = 0              # dispatched again after having finished
    failed_excursions: int = 0     # FAILED/BLOCKED transitions seen
    running_at: str | None = None  # the agent actually started
    preview_basis: str = ""        # WHICH transition counted as the preview
    first_pass_success: bool | None = None
    first_pass_basis: str = "not finished yet"
    # Which instrumented sources ever reported into this row. An empty list is
    # why a zero counter must not be read as "it never happened".
    signal_sources: tuple[str, ...] = ()

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

    def record_plan_outcome(self, status: str, *, note: str = "") -> None:
        """PLAN_CONFIRMED / PLAN_ADJUSTED / PLAN_MISMATCH, as the worker answered.

        The spec store records the same verdict on the spec. Both are kept
        because they answer different questions: the spec says what the next
        planner for that module should know, and this row says what this run
        cost and how it went. A MISMATCH rate is the number that says whether
        specs are worth trusting at all, and it is only computable if the
        verdict lives somewhere aggregable.
        """
        if status not in PLAN_STATUSES:
            raise ValueError(f"unknown plan status {status!r}")
        self.plan_status = status
        if note:
            self.plan_note = note

    def record_budget_escalation(self, reason: str) -> None:
        """A worker was let past its file/search budget, and why.

        Recorded rather than merely permitted. A budget nobody can audit is
        advice, not a budget: without the reason attached, an overrun and a
        justified overrun are the same row, and the only honest thing to do
        with such a fleet-wide number is ignore it.
        """
        if not (reason or "").strip():
            raise ValueError("a budget escalation without a reason is not one")
        self.budget_escalations += 1
        self.note(f"budget escalation: {reason.strip()}"[:200])

    def record_redefine(self, count: int = 1) -> None:
        self.redefine_count += count

    def record_assist(self) -> None:
        self.assist_requests += 1

    def record_knowledge(self, count: int = 1) -> None:
        self.knowledge_hits += count

    def record_context_pack(self, count: int = 1) -> None:
        self.context_pack_hits += count

    def record_similar_bug(self, count: int = 1) -> None:
        self.similar_bug_hits += count

    def record_signal(self, kind: str, count: int = 1, *, source: str = "") -> bool:
        """Add to one counter by name. Unknown names are refused, not invented.

        Returning False rather than raising: a mis-named signal must not take
        down the runtime path that emitted it, and a silently-created counter
        nobody aggregates is worse than a rejected one.
        """
        if kind not in COUNTER_FIELDS or count <= 0:
            return False
        setattr(self, kind, int(getattr(self, kind) or 0) + int(count))
        if source and source not in self.signal_sources:
            self.signal_sources = (*self.signal_sources, source)
        return True

    def record_provider_usage(self, payload: Any, *, provider: str | None = None,
                              reported_at: str | None = None) -> ProviderUsage:
        """Record what a runtime reported. Nothing reported, nothing recorded."""
        usage = ProviderUsage.from_report(payload, provider=provider,
                                          reported_at=reported_at or _now())
        if not usage.available():
            # Keep any earlier real report rather than overwriting it with a
            # blank: a later empty report is silence, not a correction.
            if self.usage.available():
                return self.usage
            self.usage = usage
            return usage
        self.usage = usage
        total = usage.total()
        # `tokens` stays the single headline figure. It is only replaced by a
        # provider-reported one -- never by a derived total, which would put
        # an assembled number where a measured one is expected.
        if total.source == EXACT:
            self.tokens = total
        elif self.tokens.source == UNAVAILABLE:
            self.tokens = total
        return usage

    # -- lifecycle, driven by the runtime's own transitions -------------------

    def mark_dispatched(self, when: str | None = None, *, lane: str | None = None) -> None:
        """A real DISPATCHING transition. Counted, because a second one is
        exactly what distinguishes a first-pass success from a retry."""
        self.dispatch_count += 1
        if lane:
            self.lane = lane
        if self.dispatch_count > 1 and self.finished_at:
            # Dispatched again after finishing: the same task, still open.
            self.reopened += 1
            self.finished_at = None
            self.outcome = None
        self._evaluate_first_pass()

    def mark_running(self, when: str | None = None) -> None:
        self.running_at = self.running_at or (when or _now())

    def mark_preview(self, when: str | None = None, *, basis: str = "") -> None:
        # First only: the interesting number is how long until something was
        # visible, not how many times it was rebuilt afterwards.
        if self.first_preview_at:
            return
        self.first_preview_at = when or _now()
        self.preview_basis = basis or self.preview_basis

    def record_excursion(self, status: str) -> None:
        """A FAILED/BLOCKED transition. The evidence that a later COMPLETED
        was not a first-pass success."""
        self.failed_excursions += 1
        self.note(f"excursion: {status}")
        self._evaluate_first_pass()

    def note(self, text: str) -> None:
        if text and text not in self.notes:
            self.notes = (*self.notes, text)

    def finish(self, outcome: str, *, when: str | None = None) -> None:
        self.finished_at = when or _now()
        self.outcome = outcome
        self._evaluate_first_pass()

    def _evaluate_first_pass(self) -> None:
        """Did this task land on its first attempt? Unknown stays unknown.

        A row whose dispatches were never observed (a worker-reported row, for
        instance) cannot answer this, and answering it anyway would turn a gap
        in instrumentation into a success statistic.
        """
        if not self.finished_at:
            self.first_pass_success, self.first_pass_basis = None, "not finished yet"
            return
        if self.outcome != OUTCOME_COMPLETED:
            self.first_pass_success = False
            self.first_pass_basis = f"finished as {self.outcome or 'UNKNOWN'}"
            return
        if self.dispatch_count == 0:
            self.first_pass_success = None
            self.first_pass_basis = ("no dispatch was observed for this task, so "
                                     "first-pass success is not knowable")
            return
        against = []
        if self.dispatch_count > 1:
            against.append(f"{self.dispatch_count} dispatches")
        if self.failed_excursions:
            against.append(f"{self.failed_excursions} failed/blocked excursion(s)")
        if self.redefine_count:
            against.append(f"{self.redefine_count} redefine(s)")
        if against:
            self.first_pass_success = False
            self.first_pass_basis = "completed, but " + ", ".join(against)
        else:
            self.first_pass_success = True
            self.first_pass_basis = "completed on one dispatch, no redefine, no excursion"

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
        payload["usage"] = self.usage.as_dict()
        payload["notes"] = list(self.notes)
        payload["signal_sources"] = list(self.signal_sources)
        # The contract's name for the same stored number, so no reader has to
        # know which of two spellings a given row was written with.
        payload["search_calls"] = self.search_rounds
        payload["seconds_to_preview"] = self.seconds_to_preview()
        payload["time_to_preview_seconds"] = self.seconds_to_preview()
        payload["duration_seconds"] = self.duration_seconds()
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskTelemetry":
        """Read a stored row back into a record that can be updated further.

        This is what makes the runtime recorder survive a restart: the row a
        previous process opened is reopened and continued, instead of a second
        row being started for the same task.
        """
        known = set(cls.__dataclass_fields__)  # noqa: F821
        data: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            if key not in known or key in ("tokens", "usage"):
                continue
            if key in ("notes", "signal_sources"):
                data[key] = tuple(value or ())
            else:
                data[key] = value
        record = cls(**data)
        record.tokens = TokenCount.from_dict((raw or {}).get("tokens"))
        record.usage = ProviderUsage.from_dict((raw or {}).get("usage"))
        return record

    def one_line(self) -> str:
        """The compact summary, honest about what is missing."""
        preview = self.seconds_to_preview()
        parts = [
            f"files={self.files_read}",
            f"searches={self.search_rounds}",
            f"runbooks={self.runbook_hits}/{self.runbook_hits + self.runbook_misses}",
            f"reuse={self.context_pack_hits + self.knowledge_hits + self.similar_bug_hits}",
            f"redefines={self.redefine_count}",
            f"tokens={self.tokens.display()}",
            f"to_preview={'n/a' if preview is None else f'{preview:.0f}s'}",
            f"first_pass={'unknown' if self.first_pass_success is None else self.first_pass_success}",
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
    # first-pass success is a three-valued fact: yes, no, and not knowable.
    # The third is kept separate rather than folded into "no", which would
    # make an un-instrumented task look like a failure.
    first_pass = [row.get("first_pass_success") for row in rows
                  if row.get("first_pass_success") is not None]
    judged = len(first_pass)
    searches = total("search_rounds")
    return {
        "tasks": len(rows),
        "files_read": total("files_read"),
        # Both names, one number. `search_rounds` stays for readers written
        # against the original shape.
        "search_calls": searches,
        "search_rounds": searches,
        "runbook_hits": total("runbook_hits"),
        "runbook_misses": total("runbook_misses"),
        "runbook_hit_rate": (round(total("runbook_hits") / attempted, 3)
                             if attempted else None),
        "knowledge_hits": total("knowledge_hits"),
        "context_pack_hits": total("context_pack_hits"),
        "similar_bug_hits": total("similar_bug_hits"),
        "cache_hits": total("cache_hits"),
        "redefine_count": total("redefine_count"),
        "assist_requests": total("assist_requests"),
        # Three verdicts plus "nobody answered", kept apart on purpose: an
        # un-answered plan check must never be counted as a confirmed one, and
        # the MISMATCH count is the only direct evidence the fleet has about
        # whether its specs describe the code.
        "plan_outcomes": _summarise_plan_outcomes(rows),
        "budget_escalations": total("budget_escalations"),
        "dispatches": total("dispatch_count"),
        "tokens": tokens,
        "provider_usage": _summarise_usage(rows),
        "first_pass_success": sum(1 for value in first_pass if value),
        "first_pass_judged": judged,
        "first_pass_unknown": len(rows) - judged,
        "first_pass_rate": (round(sum(1 for v in first_pass if v) / judged, 3)
                            if judged else None),
        "median_time_to_preview_seconds": (sorted(previews)[len(previews) // 2]
                                           if previews else None),
        "median_seconds_to_preview": (sorted(previews)[len(previews) // 2]
                                      if previews else None),
        "tasks_without_preview": len(rows) - len(previews),
        "tasks_with_signals": sum(1 for row in rows if row.get("signal_sources")),
    }


def _summarise_plan_outcomes(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """How the plan-verification handshake actually went across these tasks."""
    counts = {name: sum(1 for row in rows if row.get("plan_status") == name)
              for name in PLAN_STATUSES}
    counts["unreported"] = sum(1 for row in rows if not row.get("plan_status"))
    answered = sum(counts[name] for name in (PLAN_CONFIRMED, PLAN_ADJUSTED, PLAN_MISMATCH))
    counts["answered"] = answered
    # Left as None rather than 0 when nothing answered: a mismatch rate over
    # zero verdicts is not a rate, and printing 0% would read as "our specs
    # are always right" when it means "nobody checked".
    counts["mismatch_rate"] = (round(counts[PLAN_MISMATCH] / answered, 3)
                               if answered else None)
    return counts


def _summarise_usage(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per-counter provider totals, each carrying how complete it is.

    A counter is summed over the tasks that REPORTED it, and the count of
    tasks that did not is part of the answer. Reporting a bare total over an
    unknown subset is how a fleet ends up quoting a third of its real usage
    as if it were all of it.
    """
    usages = [ProviderUsage.from_dict(row.get("usage")) for row in rows]
    reporting = [u for u in usages if u.available()]
    if not reporting:
        return {"available": False, "reporting_tasks": 0, "tasks": len(rows),
                "counters": {},
                "note": f"none of {len(rows)} task(s) had provider usage reported"}
    counters: dict[str, Any] = {}
    for name in USAGE_COUNTERS:
        values = [u.counters[name] for u in usages if name in u.counters]
        if not values:
            counters[name] = TokenCount.unavailable(
                f"no task reported {name}").as_dict()
            continue
        missing = len(rows) - len(values)
        counters[name] = {
            "value": sum(int(v.value or 0) for v in values),
            "source": PARTIAL if missing else EXACT,
            "complete": missing == 0,
            "reporting_tasks": len(values),
            "missing_tasks": missing,
            "method": (f"sum over the {len(values)} task(s) that reported {name}"
                       + (f"; {missing} did not" if missing else "")),
        }
    providers = sorted({u.provider for u in reporting if u.provider})
    return {"available": True, "reporting_tasks": len(reporting), "tasks": len(rows),
            "providers": providers, "counters": counters,
            "note": (f"{len(reporting)} of {len(rows)} task(s) had provider usage "
                     f"reported" + ("" if len(reporting) == len(rows) else
                                    "; the rest reported nothing and are not zeros"))}


# -- aggregation: per task, per module, per time period ----------------------

PERIOD_DAY = "day"
PERIOD_WEEK = "week"
PERIOD_MONTH = "month"
GROUP_KEYS = ("task", "module", "work", "project", "lane", "execution_mode",
              "difficulty", "spec_level", PERIOD_DAY, PERIOD_WEEK, PERIOD_MONTH)

_GROUP_FIELDS = {"task": "task_id", "module": "module", "work": "work_id",
                 "project": "project_id", "lane": "lane",
                 "execution_mode": "execution_mode", "difficulty": "difficulty",
                 "spec_level": "spec_level"}


def period_key(timestamp: str | None, granularity: str) -> str | None:
    """The bucket a timestamp falls in, or None when it cannot be read.

    None rather than a guess: a row whose clock cannot be parsed belongs in
    no period, and putting it in today's would quietly move work between
    reporting windows.
    """
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(str(timestamp))
    except ValueError:
        return None
    if granularity == PERIOD_DAY:
        return moment.strftime("%Y-%m-%d")
    if granularity == PERIOD_WEEK:
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"
    if granularity == PERIOD_MONTH:
        return moment.strftime("%Y-%m")
    return None


def group_key(row: dict[str, Any], by: str) -> str | None:
    if by in _GROUP_FIELDS:
        value = row.get(_GROUP_FIELDS[by])
        return str(value) if value else None
    if by in (PERIOD_DAY, PERIOD_WEEK, PERIOD_MONTH):
        return period_key(row.get("started_at"), by)
    return None


def aggregate(records: Sequence[TaskTelemetry | dict[str, Any]], *,
              by: str = "module") -> dict[str, Any]:
    """Aggregate the same rows along one axis: task, module or time period.

    Rows that cannot be placed on the chosen axis -- no module recorded, an
    unreadable timestamp -- are counted as `ungrouped` and named, never
    dropped into an "other" bucket that then reads as a real group.
    """
    if by not in GROUP_KEYS:
        return {"error": "UNKNOWN_GROUPING", "group_by": by, "allowed": list(GROUP_KEYS)}
    rows = [r if isinstance(r, dict) else r.as_dict() for r in records]
    buckets: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[str] = []
    for row in rows:
        key = group_key(row, by)
        if key is None:
            ungrouped.append(str(row.get("task_id") or row.get("telemetry_id") or "?"))
            continue
        buckets.setdefault(key, []).append(row)
    groups = [{"key": key, "summary": summarise(bucket)}
              for key, bucket in sorted(buckets.items())]
    return {
        "group_by": by,
        "tasks": len(rows),
        "groups": groups,
        "ungrouped_tasks": len(ungrouped),
        "ungrouped_reason": (f"{len(ungrouped)} row(s) carry no {by} and are excluded "
                             f"rather than bucketed") if ungrouped else "",
        "ungrouped_ids": ungrouped[:20],
        "overall": summarise(rows),
    }


# -- baseline and savings ----------------------------------------------------

BASELINE_MEASURED = "MEASURED"
BASELINE_STATED = "STATED"
BASELINE_UNAVAILABLE = "UNAVAILABLE"

# The per-task metrics a saving can be expressed in. Each is a rate or a
# median over real rows -- never a total, which would only say that one
# window contained more work than the other.
BASELINE_METRICS = ("files_read_per_task", "search_calls_per_task",
                    "redefines_per_task", "median_time_to_preview_seconds",
                    "runbook_hit_rate", "first_pass_rate", "tokens_per_task")


def per_task_metrics(summary: dict[str, Any]) -> dict[str, float | None]:
    """The shape a baseline and a current window are both expressed in."""
    tasks = int(summary.get("tasks") or 0)
    if not tasks:
        return {name: None for name in BASELINE_METRICS}
    def per(field_name: str) -> float:
        return round(float(summary.get(field_name) or 0) / tasks, 3)
    token_summary = summary.get("tokens") or {}
    token_value = token_summary.get("value")
    # Tokens per task only from the tasks that actually reported one -- an
    # average over rows that reported nothing would be arithmetic on silence.
    covered = int(token_summary.get("covered_tasks") or 0)
    return {
        "files_read_per_task": per("files_read"),
        "search_calls_per_task": per("search_calls"),
        "redefines_per_task": per("redefine_count"),
        "median_time_to_preview_seconds": summary.get("median_time_to_preview_seconds"),
        "runbook_hit_rate": summary.get("runbook_hit_rate"),
        "first_pass_rate": summary.get("first_pass_rate"),
        "tokens_per_task": (round(float(token_value) / covered, 1)
                            if token_value is not None and covered else None),
    }


@dataclass(frozen=True)
class Baseline:
    """What "before" was -- and where that claim comes from.

    There is no third option. Either these numbers were measured from real
    recorded rows, or a human stated the definition they came from. A
    baseline with neither is UNAVAILABLE and carries no numbers at all.
    """

    source: str = BASELINE_UNAVAILABLE
    definition: str = ""
    metrics: dict[str, float | None] = field(default_factory=dict)
    tasks: int = 0
    window: tuple[str | None, str | None] = (None, None)
    note: str = ""

    @classmethod
    def unavailable(cls, why: str) -> "Baseline":
        return cls(source=BASELINE_UNAVAILABLE, note=why)

    def available(self) -> bool:
        return self.source in (BASELINE_MEASURED, BASELINE_STATED) and bool(self.metrics)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"source": self.source, "available": self.available(),
                                   "note": self.note}
        if not self.available():
            return payload
        payload.update({"definition": self.definition, "metrics": dict(self.metrics),
                        "tasks": self.tasks,
                        "window": {"since": self.window[0], "until": self.window[1]}})
        return payload


def measure_baseline(records: Sequence[TaskTelemetry | dict[str, Any]], *,
                     definition: str, window: tuple[str | None, str | None] = (None, None),
                     min_tasks: int = 1) -> Baseline:
    """A baseline computed from rows that were actually recorded.

    `min_tasks` exists because an average over one task is not a baseline; a
    caller that wants a stricter floor raises it, and below the floor the
    answer is UNAVAILABLE rather than a number with a caveat attached.
    """
    rows = [r if isinstance(r, dict) else r.as_dict() for r in records]
    if len(rows) < max(1, min_tasks):
        return Baseline.unavailable(
            f"baseline needs at least {max(1, min_tasks)} recorded task(s); "
            f"{len(rows)} available")
    summary = summarise(rows)
    return Baseline(source=BASELINE_MEASURED, definition=definition,
                    metrics=per_task_metrics(summary), tasks=len(rows), window=window,
                    note=f"measured from {len(rows)} recorded task(s)")


def stated_baseline(metrics: dict[str, float | None], *, definition: str) -> Baseline:
    """A baseline someone supplied, admissible only with its definition.

    Without a stated definition the numbers are unattributable, so they are
    refused outright rather than shown with an empty provenance.
    """
    if not definition.strip():
        return Baseline.unavailable(
            "a supplied baseline must state how it was arrived at; none was given")
    usable = {name: value for name, value in (metrics or {}).items()
              if name in BASELINE_METRICS and value is not None}
    if not usable:
        return Baseline.unavailable(
            "no usable metric supplied; expected one of " + ", ".join(BASELINE_METRICS))
    return Baseline(source=BASELINE_STATED, definition=definition.strip(),
                    metrics=usable, note="supplied with a stated definition")


# Lower is better for these; for the rest a higher number is the improvement.
_LOWER_IS_BETTER = frozenset({"files_read_per_task", "search_calls_per_task",
                              "redefines_per_task", "median_time_to_preview_seconds",
                              "tokens_per_task"})


def savings(summary: dict[str, Any], baseline: Baseline) -> dict[str, Any]:
    """Current against baseline, per metric -- or nothing at all.

    Every figure here is DERIVED, so every figure is labelled an estimate,
    however exact the subtraction was. And with no admissible baseline there
    is no comparison: the answer is that it is unavailable and why, not a
    zero and not a silently-omitted section.
    """
    if not baseline.available():
        return {"available": False, "reason": baseline.note or "no baseline",
                "baseline": baseline.as_dict()}
    current = per_task_metrics(summary)
    deltas: dict[str, Any] = {}
    for name in BASELINE_METRICS:
        before, after = baseline.metrics.get(name), current.get(name)
        if before is None or after is None:
            deltas[name] = {"status": "UNKNOWN",
                            "reason": ("no baseline value for this metric"
                                       if before is None else
                                       "not measured in the current window")}
            continue
        change = round(after - before, 3)
        improved = (change < 0) if name in _LOWER_IS_BETTER else (change > 0)
        deltas[name] = {
            "status": "IMPROVED" if change and improved else
                      ("WORSE" if change else "UNCHANGED"),
            "baseline": before, "current": after, "change": change,
            "percent_change": (round((change / before) * 100, 1) if before else None),
            "source": ESTIMATED,
            "method": f"derived: current {name} minus baseline {name}",
        }
    return {"available": True, "baseline": baseline.as_dict(),
            "current": current, "tasks": summary.get("tasks", 0), "metrics": deltas,
            "note": "every figure here is derived from the two windows, not measured"}


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


def _index_module_and_time(connection: sqlite3.Connection) -> None:
    """Indexes for the two axes the aggregation API filters on.

    Additive and idempotent, like every other migration here: an older build
    reading this database sees the same rows, just without the index.
    """
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_module "
                       "ON work_telemetry(module)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_started "
                       "ON work_telemetry(started_at)")


TELEMETRY_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: work_telemetry", lambda connection: None),
    Migration(2, "aggregation: index module and started_at so per-module and "
                 "per-period reads do not scan the table", _index_module_and_time),
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

    def for_task(self, task_id: str) -> dict[str, Any] | None:
        """The row for a queue task, which is what the runtime keys on.

        One row per task by construction: the recorder reopens this row on a
        re-dispatch instead of starting a second one, so a task's cost stays
        one number rather than becoming a sum a reader has to assemble.
        """
        row = self._connection.execute(
            "SELECT payload FROM work_telemetry WHERE task_id=? "
            "ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def query(self, *, task_id: str | None = None, work_id: str | None = None,
              project_id: str | None = None, module: str | None = None,
              since: str | None = None, until: str | None = None,
              limit: int = 500) -> list[dict[str, Any]]:
        """Rows on the three axes savings are read along: task, module, period.

        `since`/`until` are ISO timestamps compared against `started_at`. They
        are half-open (`since <= started_at < until`) so two adjacent windows
        can never both contain the same task and double-count it.
        """
        clauses: list[str] = []
        args: list[Any] = []
        for column, value in (("task_id", task_id), ("work_id", work_id),
                              ("project_id", project_id), ("module", module)):
            if value:
                clauses.append(f"{column}=?")
                args.append(value)
        if since:
            clauses.append("started_at >= ?")
            args.append(since)
        if until:
            clauses.append("started_at < ?")
            args.append(until)
        sql = "SELECT payload FROM work_telemetry"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY started_at DESC LIMIT ?"
        args.append(max(1, int(limit)))
        return [json.loads(row["payload"])
                for row in self._connection.execute(sql, args)]

    def summary(self, *, limit: int = 200, project_id: str | None = None
                ) -> dict[str, Any]:
        rows = self.recent(limit=limit, project_id=project_id)
        report = summarise(rows)
        report["targets"] = against_targets(report)
        return report

    def aggregate(self, *, by: str = "module", limit: int = 500, **filters: Any
                  ) -> dict[str, Any]:
        """Grouped totals over the rows a filter selects."""
        return aggregate(self.query(limit=limit, **filters), by=by)

    def report(self, *, by: str = "module", limit: int = 500,
               baseline: Baseline | None = None,
               baseline_window: tuple[str | None, str | None] | None = None,
               baseline_definition: str = "", baseline_min_tasks: int = 3,
               **filters: Any) -> dict[str, Any]:
        """One read: what the window cost, grouped, and what it saved.

        The baseline is whichever the CALLER can justify -- one supplied with
        its definition, or one measured from an earlier window of real rows.
        Given neither, the report still returns the window's own numbers and
        says plainly that there is nothing to compare them against. A savings
        figure is never manufactured to fill the space.
        """
        rows = self.query(limit=limit, **filters)
        summary = summarise(rows)
        summary["targets"] = against_targets(summary)
        if baseline is None and baseline_window:
            window_filters = {k: v for k, v in filters.items()
                              if k not in ("since", "until")}
            earlier = self.query(limit=limit, since=baseline_window[0],
                                 until=baseline_window[1], **window_filters)
            baseline = measure_baseline(
                earlier,
                definition=(baseline_definition
                            or f"recorded tasks started in "
                               f"[{baseline_window[0]}, {baseline_window[1]})"),
                window=baseline_window, min_tasks=baseline_min_tasks)
        if baseline is None:
            baseline = Baseline.unavailable(
                "no baseline was measured or stated for this report")
        return {"filters": {k: v for k, v in filters.items() if v},
                "tasks": len(rows), "summary": summary,
                "grouped": aggregate(rows, by=by),
                "savings": savings(summary, baseline)}

    def close(self) -> None:
        self._connection.close()
