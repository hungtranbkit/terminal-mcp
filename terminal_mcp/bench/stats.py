"""Robust, dependency-free statistics for the benchmark.

Three deliberate choices, each of which a reviewer is entitled to
challenge and each of which is isolated to this module:

* MEDIAN, NEVER MEAN. Agent task cost is heavy-tailed -- one task that
  re-read a huge file dominates any mean at these sample sizes. Every
  reported figure is a median with a p25/p75 spread, which is why a
  single extreme task cannot move the headline number. Outliers are
  still detected and COUNTED (`outliers` below) so the report can say
  they exist; they are not deleted, because deleting them is a second,
  unaudited judgement call on top of the robust estimator that already
  handles them.
* NUMPY IS NOT A DEPENDENCY. `percentile` is the standard linear-
  interpolation ("type 7") definition, matching numpy's default, in
  twelve lines. This package must be runnable from a bare checkout on
  any node.
* THE BOOTSTRAP IS SEEDED. `bootstrap_median_diff_bounds` uses an
  explicit `random.Random(seed)`, so two runs over the same data
  produce byte-identical reports. A benchmark whose number wobbles
  between runs cannot be used to argue about a 5% difference.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Sequence

DEFAULT_BOOTSTRAP_ITERATIONS = 2000
DEFAULT_BOOTSTRAP_SEED = 20260914
DEFAULT_CONFIDENCE = 0.90
IQR_OUTLIER_FACTOR = 1.5


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolation percentile, q in [0, 1]. None for empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)


def median(values: Sequence[float]) -> float | None:
    return percentile(values, 0.5)


@dataclass(frozen=True)
class Summary:
    """The distribution of one metric inside one cohort."""

    n: int
    p25: float | None
    median: float | None
    p75: float | None
    minimum: float | None
    maximum: float | None
    outliers: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "p25": self.p25,
            "median": self.median,
            "p75": self.p75,
            "min": self.minimum,
            "max": self.maximum,
            "outliers": self.outliers,
        }


def outlier_indices(values: Sequence[float]) -> tuple[int, ...]:
    """Tukey fence (1.5 x IQR). Needs at least 4 points to mean
    anything -- below that every point is its own quartile and the
    fence degenerates, so nothing is flagged."""
    if len(values) < 4:
        return ()
    q1 = percentile(values, 0.25)
    q3 = percentile(values, 0.75)
    if q1 is None or q3 is None:
        return ()
    iqr = q3 - q1
    if iqr <= 0:
        # A degenerate quartile range (p25 == p75) makes the Tukey fence
        # zero-width, and the textbook formula then flags nothing --
        # including a single task 10,000x the rest of a constant
        # distribution, which is exactly the case an outlier check is
        # for. So the fence collapses to the constant itself: anything
        # not equal to it is outside.
        return tuple(i for i, value in enumerate(values) if value < q1 or value > q3)
    low = q1 - IQR_OUTLIER_FACTOR * iqr
    high = q3 + IQR_OUTLIER_FACTOR * iqr
    return tuple(i for i, value in enumerate(values) if value < low or value > high)


def summarize(values: Sequence[float]) -> Summary:
    cleaned = [float(v) for v in values]
    if not cleaned:
        return Summary(0, None, None, None, None, None, 0)
    return Summary(
        n=len(cleaned),
        p25=percentile(cleaned, 0.25),
        median=percentile(cleaned, 0.5),
        p75=percentile(cleaned, 0.75),
        minimum=min(cleaned),
        maximum=max(cleaned),
        outliers=len(outlier_indices(cleaned)),
    )


def bootstrap_median_diff_bounds(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = DEFAULT_CONFIDENCE,
) -> tuple[float, float] | None:
    """Percentile-bootstrap interval for (median(baseline) -
    median(candidate)), i.e. the tokens SAVED. Positive = candidate is
    cheaper. None when either arm is empty."""
    if not baseline or not candidate:
        return None
    rng = random.Random(seed)
    base = list(baseline)
    cand = list(candidate)
    diffs: list[float] = []
    for _ in range(iterations):
        b = median([base[rng.randrange(len(base))] for _ in range(len(base))])
        c = median([cand[rng.randrange(len(cand))] for _ in range(len(cand))])
        if b is None or c is None:
            continue
        diffs.append(b - c)
    if not diffs:
        return None
    tail = (1.0 - confidence) / 2.0
    low = percentile(diffs, tail)
    high = percentile(diffs, 1.0 - tail)
    if low is None or high is None:
        return None
    return (low, high)


@dataclass(frozen=True)
class SavingsEstimate:
    """A saving, stated conservatively and labelled as observational.

    `point_percent` is the plain median-vs-median difference.
    `lower_bound_percent` is the bootstrap lower bound expressed as a
    percentage of the baseline median. `reported_percent` -- the ONLY
    number the summary line is allowed to quote -- is the smaller of
    those two, floored at 0. If the interval spans zero, the reported
    saving is 0 and `significant` is False: the harness would rather
    say "no measurable difference" than claim a win it cannot defend."""

    baseline_median: float | None
    candidate_median: float | None
    point_percent: float | None
    lower_bound_percent: float | None
    upper_bound_percent: float | None
    reported_percent: float | None
    significant: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseline_median": self.baseline_median,
            "candidate_median": self.candidate_median,
            "point_percent": self.point_percent,
            "lower_bound_percent": self.lower_bound_percent,
            "upper_bound_percent": self.upper_bound_percent,
            "reported_percent": self.reported_percent,
            "significant": self.significant,
        }


EMPTY_SAVINGS = SavingsEstimate(None, None, None, None, None, None, False)


def savings(
    baseline: Sequence[float],
    candidate: Sequence[float],
    *,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    confidence: float = DEFAULT_CONFIDENCE,
) -> SavingsEstimate:
    base_median = median(baseline)
    cand_median = median(candidate)
    if base_median is None or cand_median is None or base_median == 0:
        return EMPTY_SAVINGS
    point = (base_median - cand_median) / base_median * 100.0
    bounds = bootstrap_median_diff_bounds(
        baseline, candidate, iterations=iterations, seed=seed, confidence=confidence
    )
    if bounds is None:
        return SavingsEstimate(base_median, cand_median, point, None, None, None, False)
    low_pct = bounds[0] / base_median * 100.0
    high_pct = bounds[1] / base_median * 100.0
    significant = low_pct > 0.0
    reported = max(0.0, min(point, low_pct))
    return SavingsEstimate(
        baseline_median=base_median,
        candidate_median=cand_median,
        point_percent=point,
        lower_bound_percent=low_pct,
        upper_bound_percent=high_pct,
        reported_percent=reported,
        significant=significant,
    )


def rate(successes: int, total: int) -> float | None:
    """Proportion as a percentage, or None when the denominator is
    zero -- an unknown rate must not render as 0%."""
    if total <= 0:
        return None
    return successes / total * 100.0


# ---------------------------------------------------------------------------
# partial identification under missing outcomes
# ---------------------------------------------------------------------------


def identification_interval(rate: float | None, coverage: float | None) -> tuple[float, float] | None:
    """The range a cohort's TRUE rate could occupy, given that only some
    of its tasks were scoreable at all.

    If a fraction `c` of tasks were scored and they succeeded at rate
    `r`, the unscored remainder is unknown -- at the extremes it is all
    failures or all successes -- so the true rate lies in
    `[r*c, r*c + (1-c)]`.

    The thing to read off that expression is the WIDTH: `1 - c`. It is
    governed by how much is missing, NOT by how much more is missing in
    one arm than the other. That distinction matters because the
    intuitive rule -- "worry when coverage differs between arms" --
    misses the case where BOTH arms are equally and moderately covered:
    two arms at 50% coverage, one observing 100% success and the other
    0%, have a coverage gap of zero and an observed difference of a
    hundred points, and yet both true rates could be exactly 0.5. Equal
    coverage is not safety; poor coverage is the problem, and it can be
    poor symmetrically.

    Rates are fractions in [0, 1], not percentages."""
    if rate is None or coverage is None:
        return None
    point = rate * coverage
    return (point, point + (1.0 - coverage))


def intervals_overlap(left: tuple[float, float] | None, right: tuple[float, float] | None) -> bool:
    """Closed-interval overlap -- touching endpoints count, because a
    single shared value is exactly the case where the two arms' true
    rates could be equal."""
    if left is None or right is None:
        return False
    return left[0] <= right[1] and right[0] <= left[1]


def explained_by_missingness(
    left_rate: float | None,
    left_coverage: float | None,
    right_rate: float | None,
    right_coverage: float | None,
) -> bool:
    """True when equal true rates are consistent with what was
    observed, i.e. the difference between the arms could be entirely an
    artefact of which tasks were scoreable.

    This is EXACT rather than a bound, and it needs no thresholds: a
    zero coverage gap and a zero observed difference both fall out of it
    correctly on their own. It subsumes the cruder "gap >= observed
    difference" heuristic and any fixed gap cutoff.

    Note this is a PARTIAL IDENTIFICATION question, not a precision
    one. A larger sample does not shrink these intervals -- only
    recording the missing outcomes does. So an overlap verdict sitting
    next to a tight confidence interval is not a contradiction: the
    confidence interval describes sampling noise around a quantity that
    is not identified in the first place."""
    return intervals_overlap(
        identification_interval(left_rate, left_coverage),
        identification_interval(right_rate, right_coverage),
    )
