"""The one normalized shape every benchmark source must produce.

This module is the whole reason the harness can be built BEFORE the
runtime telemetry it measures exists. Nothing downstream of here
(matching, statistics, the report) ever touches a SQLite row, a column
name or a JSON blob -- they only ever see `TaskRecord`, so a source
whose real schema turns out different from today's guess is a change to
`sources.py` alone, never to the analysis.

Two rules are enforced *in the types*, not in a convention a later
reader has to remember:

1. MISSING IS NOT ZERO. Every numeric field is `int | None` / `float |
   None`. A task whose token telemetry was never recorded has
   `input_tokens=None`, which propagates to `primary_cost_tokens=None`
   and makes the task invisible to the token statistics instead of
   silently dragging a median toward zero. There is no `or 0` anywhere
   in this package, and a test pins that.
2. RE-ENTRY REASONS ARE NOT ALL THE SAME. `USER_CHANGED_REQUIREMENT`
   and `ENVIRONMENT_FAILURE` describe the world changing under a task,
   not the pipeline doing worse work, so they are separated out of
   every "did this task need re-entry" count and reported on their own
   line. A task whose ONLY re-entries carry those reasons is flagged
   (`EXCLUDED_REASON_ONLY`), never dropped -- dropping it would quietly
   bias the sample toward whichever cohort happens to run in a more
   stable environment.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Cohort labels. "unknown" is a real, first-class value: a source that
# cannot tell which pipeline ran a task must say so rather than guess,
# and unknown-cohort tasks are counted and reported but never matched.
COHORT_LEGACY = "legacy"
COHORT_NEW = "new_pipeline"
COHORT_UNKNOWN = "unknown"
COHORTS = (COHORT_LEGACY, COHORT_NEW)

# Re-entry reasons that are excluded from the comparison's own counts
# and reported separately (see this module's docstring, rule 2).
USER_CHANGED_REQUIREMENT = "USER_CHANGED_REQUIREMENT"
ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
EXCLUDED_REENTRY_REASONS = frozenset({USER_CHANGED_REQUIREMENT, ENVIRONMENT_FAILURE})

# Re-entry reasons this harness recognises by name. STALE_CONTEXT was
# added on the critic lane's argument: a warm cache can make an agent
# reason about a repo state that no longer holds, and that rework looks
# exactly like CONTRACT_GAP from the outside -- so it would be charged
# to whichever arm carries more context, which is precisely the arm
# under test. It is COUNTED (it is real rework), but naming it keeps it
# separable in the by-reason table instead of hiding inside a generic
# bucket.
STALE_CONTEXT = "STALE_CONTEXT"
CONTRACT_GAP = "CONTRACT_GAP"
KNOWN_REENTRY_REASONS = frozenset(
    {USER_CHANGED_REQUIREMENT, ENVIRONMENT_FAILURE, STALE_CONTEXT, CONTRACT_GAP}
)

# Risk classes. The critic lane's requirement, adopted: these are never
# pooled into a single comparison, because the effect can genuinely run
# in opposite directions across them and pooling cancels a real result
# into a null.
RISK_HIGH = "HIGH_RISK"
RISK_STANDARD = "STANDARD"
RISK_FAST_FIX = "FAST_FIX"
RISK_UNKNOWN = "UNKNOWN_RISK"
RISK_CLASSES = (RISK_HIGH, RISK_STANDARD, RISK_FAST_FIX, RISK_UNKNOWN)

_RISK_ALIASES = {
    "high": RISK_HIGH,
    "high_risk": RISK_HIGH,
    "highrisk": RISK_HIGH,
    "critical": RISK_HIGH,
    "p0": RISK_HIGH,
    "standard": RISK_STANDARD,
    "normal": RISK_STANDARD,
    "medium": RISK_STANDARD,
    "default": RISK_STANDARD,
    "fast_fix": RISK_FAST_FIX,
    "fastfix": RISK_FAST_FIX,
    "fast": RISK_FAST_FIX,
    "low": RISK_FAST_FIX,
    "trivial": RISK_FAST_FIX,
}


def risk_class(profile: str | None) -> str:
    """Map a source's own profile/risk spelling onto the three classes.
    Anything unrecognised becomes UNKNOWN_RISK and is reported in its
    own section -- never quietly filed under STANDARD."""
    if not profile:
        return RISK_UNKNOWN
    return _RISK_ALIASES.get(str(profile).strip().lower().replace("-", "_"), RISK_UNKNOWN)


# How a task whose cache-write TTL split is unknown should be priced.
TTL_SPLIT_REQUIRED = "split_required"
TTL_ASSUME_5M = "assume_5m"
TTL_ASSUME_1H = "assume_1h"

# Flags attached to a record by the loader/analysis rather than by a
# source. They travel with the record so the report can explain every
# task it treated specially instead of silently dropping it.
FLAG_EXCLUDED_REASON_ONLY = "EXCLUDED_REASON_ONLY"
FLAG_INCOMPLETE_USAGE = "INCOMPLETE_USAGE"
FLAG_NO_USAGE = "NO_USAGE"
FLAG_OUTLIER = "OUTLIER"
FLAG_UNKNOWN_MODEL_PRICE = "UNKNOWN_MODEL_PRICE"
FLAG_CACHE_TTL_UNSPLIT = "CACHE_TTL_UNSPLIT"
# The measured party wrote an outcome that disagrees with the one
# recomputed from its own turn/re-entry rows. Cheapest anti-gaming
# signal available without an upstream change.
FLAG_FPS_DISAGREEMENT = "FPS_DISAGREEMENT"


@dataclass(frozen=True)
class Reentry:
    """One re-entry / rework event on a task, with the reason code the
    runtime recorded. `reason=None` means the runtime recorded a
    re-entry but no reason -- that is NOT the same as an excluded
    reason, so it counts toward the comparison."""

    reason: str | None = None
    at: str | None = None

    @property
    def is_excluded(self) -> bool:
        return self.reason in EXCLUDED_REENTRY_REASONS


@dataclass(frozen=True)
class TaskUsage:
    """Token counts for one task, already de-duplicated by whichever
    source produced it (see `sources.UsageAggregator` -- the
    double-counting of cumulative rows is prevented there, once, rather
    than in every consumer).

    Cache writes are carried as the 5m/1h SPLIT the API actually
    reports, because the two bill at different multipliers (1.25x and
    2x base input). `cache_write_total_tokens` exists for a source that
    only records the collapsed figure; it is enough to report the raw
    count and never enough to price it, which is why `cost_units`
    refuses to guess a TTL unless the caller says which assumption to
    make and the report then discloses that assumption."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    cache_write_total_tokens: int | None = None

    @property
    def cache_write_tokens(self) -> int | None:
        """Total cache-write tokens however the source recorded them."""
        if self.cache_write_total_tokens is not None:
            return self.cache_write_total_tokens
        if self.cache_write_5m_tokens is None and self.cache_write_1h_tokens is None:
            return None
        return (self.cache_write_5m_tokens or 0) + (self.cache_write_1h_tokens or 0)

    @property
    def has_ttl_split(self) -> bool:
        return self.cache_write_5m_tokens is not None and self.cache_write_1h_tokens is not None

    @property
    def total_prompt_tokens(self) -> int | None:
        """input + cache_write + cache_read.

        `input_tokens` alone is the UNCACHED REMAINDER, not the prompt.
        Reporting it as prompt size understates a cached agent loop by
        an order of magnitude, so the real prompt size gets its own
        name and nothing calls the remainder "prompt"."""
        parts = (self.input_tokens, self.cache_write_tokens, self.cache_read_tokens)
        if any(part is None for part in parts):
            return None
        return sum(parts)  # type: ignore[arg-type]

    @property
    def primary_cost_tokens(self) -> int | None:
        """input + cache_write, as named in the task brief.

        REPORTED, NOT DECIDED ON. It adds tokens of different unit cost
        at parity and omits output entirely, which biases it toward a
        pipeline that spends its budget on up-front output -- see
        `pricing.py`'s docstring for the full argument. `cost_units` is
        the metric the comparison is decided on; this one is kept so a
        reader holding the original brief can find the number it asked
        for, next to the reason it is not the headline."""
        if self.input_tokens is None or self.cache_write_tokens is None:
            return None
        return self.input_tokens + self.cache_write_tokens

    def cost_units(self, model: str | None, *, ttl_policy: str = TTL_SPLIT_REQUIRED) -> float | None:
        """Cost in base-input-equivalents. None whenever it cannot be
        computed exactly under the stated policy."""
        from . import pricing

        five_minute = self.cache_write_5m_tokens
        one_hour = self.cache_write_1h_tokens
        if not self.has_ttl_split:
            total = self.cache_write_tokens
            if total is None:
                return None
            if ttl_policy == TTL_ASSUME_5M:
                five_minute, one_hour = total, 0
            elif ttl_policy == TTL_ASSUME_1H:
                five_minute, one_hour = 0, total
            else:
                return None
        return pricing.cost_units(
            model=model,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_5m_tokens=five_minute,
            cache_write_1h_tokens=one_hour,
        )

    @property
    def is_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_tokens,
            )
        )

    @property
    def is_empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_tokens,
                self.cache_write_5m_tokens,
                self.cache_write_1h_tokens,
                self.cache_write_total_tokens,
            )
        )

    def as_dict(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_write_5m_tokens": self.cache_write_5m_tokens,
            "cache_write_1h_tokens": self.cache_write_1h_tokens,
            "total_prompt_tokens": self.total_prompt_tokens,
            "primary_cost_tokens": self.primary_cost_tokens,
        }


@dataclass(frozen=True)
class TaskRecord:
    """One task, from one cohort, as the analysis sees it."""

    task_id: str
    cohort: str = COHORT_UNKNOWN
    project: str | None = None
    profile: str | None = None
    complexity: str | None = None
    agent: str | None = None
    model: str | None = None
    usage: TaskUsage = field(default_factory=TaskUsage)
    worker_turn_count: int | None = None
    reentries: tuple[Reentry, ...] = ()
    first_pass_success: bool | None = None
    duration_seconds: float | None = None
    retries: int | None = None
    source: str = "unknown"
    flags: tuple[str, ...] = ()
    verification_evidence: bool | None = None
    terminal_status: str | None = None
    # Where the stratification key came from. A key recomputed at
    # report time can be recomputed after seeing the outcome, so its
    # provenance is carried per task and printed, never assumed.
    profile_source: str | None = None

    @property
    def risk_class(self) -> str:
        """The never-pooled stratum this task belongs to."""
        return risk_class(self.profile)

    def cost_units(self, *, ttl_policy: str = TTL_SPLIT_REQUIRED) -> float | None:
        return self.usage.cost_units(self.model, ttl_policy=ttl_policy)

    # -- re-entry accounting -------------------------------------------------
    @property
    def counted_reentries(self) -> tuple[Reentry, ...]:
        return tuple(r for r in self.reentries if not r.is_excluded)

    @property
    def excluded_reentries(self) -> tuple[Reentry, ...]:
        return tuple(r for r in self.reentries if r.is_excluded)

    @property
    def counted_reentry_count(self) -> int:
        return len(self.counted_reentries)

    def reentry_counts_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for reentry in self.reentries:
            key = reentry.reason or "UNSPECIFIED"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def counted_reentry_count_excluding(self, reasons: frozenset[str]) -> int:
        """Counted re-entries with a further set of reasons removed.

        Used to render the comparison both with and WITHOUT
        `STALE_CONTEXT`: staleness caused by a longer analysis prefix is
        a genuine downstream cost of the treatment (a mediator), so it
        belongs in the headline -- but the causal reading differs
        between the two views and the gap between them is itself
        informative, so both are shown."""
        return sum(1 for r in self.counted_reentries if r.reason not in reasons)

    @property
    def first_pass_success_recomputed(self) -> bool | None:
        """First-pass success derived HERE from the task's own turn and
        re-entry rows, rather than read from whatever scalar the
        measured party wrote about itself.

        `telemetry_tasks.first_pass_success` is a STORED tri-state
        written by the reporter, not derived -- so the party being
        measured writes its own outcome. Recomputing it from the
        underlying rows and flagging disagreement is the cheapest
        anti-gaming fix available without an upstream change.

        NOTE: this is the THREE-condition definition. The fourth
        condition of the full contract -- that the task raised no
        clarification -- cannot be evaluated at all today, because
        there is no clarification concept anywhere in the telemetry
        store (no table, no event, no column; the Question Ledger is
        unbuilt). The report states that rather than letting the
        definition drift silently."""
        if self.verification_evidence is False:
            return None
        status = (self.terminal_status or "").upper()
        if not status:
            return None
        if status == "CANCELLED":
            return None
        if status == "COMPLETED":
            return self.counted_reentry_count == 0
        if status == "FAILED":
            return False
        return None

    @property
    def first_pass_success_disagrees(self) -> bool:
        """The stored outcome and the recomputed one differ."""
        recomputed = self.first_pass_success_recomputed
        if recomputed is None or self.first_pass_success is None:
            return False
        return recomputed != self.first_pass_success

    @property
    def first_pass_success_adjusted(self) -> bool | None:
        """The value the report actually uses.

        Prefers the recomputed outcome over the stored one. Falls back
        to the stored value adjusted for excluded reasons: a task the
        runtime marked NOT first-pass whose every re-entry carries an
        excluded reason counts as a success AND gets the
        `EXCLUDED_REASON_ONLY` flag, so the adjustment is visible
        rather than assumed.

        Stays None when nothing recorded first-pass success at all --
        a missing flag is not a failure."""
        recomputed = self.first_pass_success_recomputed
        if recomputed is not None:
            return recomputed
        if self.first_pass_success is None:
            return None
        if self.first_pass_success:
            return True
        if self.reentries and not self.counted_reentries:
            return True
        return False

    @property
    def has_excluded_reason_only(self) -> bool:
        return bool(self.reentries) and not self.counted_reentries

    def with_flags(self, *flags: str) -> "TaskRecord":
        merged = tuple(dict.fromkeys(self.flags + flags))
        return TaskRecord(**{**self.__dict__, "flags": merged})

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "cohort": self.cohort,
            "project": self.project,
            "profile": self.profile,
            "risk_class": self.risk_class,
            "complexity": self.complexity,
            "agent": self.agent,
            "model": self.model,
            **self.usage.as_dict(),
            "worker_turn_count": self.worker_turn_count,
            "reentries_total": len(self.reentries),
            "reentries_counted": self.counted_reentry_count,
            "reentries_excluded": len(self.excluded_reentries),
            "reentries_by_reason": self.reentry_counts_by_reason(),
            "first_pass_success": self.first_pass_success,
            "first_pass_success_recomputed": self.first_pass_success_recomputed,
            "first_pass_success_adjusted": self.first_pass_success_adjusted,
            "first_pass_success_disagrees": self.first_pass_success_disagrees,
            "verification_evidence": self.verification_evidence,
            "terminal_status": self.terminal_status,
            "profile_source": self.profile_source,
            "duration_seconds": self.duration_seconds,
            "retries": self.retries,
            "source": self.source,
            "flags": list(self.flags),
        }


@dataclass(frozen=True)
class SourceStatus:
    """What one source had to say for itself -- reported verbatim in
    every run so an empty report always explains WHY it is empty."""

    name: str
    path: str | None
    available: bool
    task_count: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "available": self.available,
            "task_count": self.task_count,
            "detail": self.detail,
        }
