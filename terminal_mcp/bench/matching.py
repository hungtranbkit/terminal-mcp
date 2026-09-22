"""Cohort matching -- the part that decides which comparison is even
allowed to be made.

The naive version of this benchmark ("sum legacy tokens, sum new-
pipeline tokens, divide") is wrong in a way that is invisible in the
output: if the new pipeline happened to run the easy tasks, it wins by
50% while being no better at all. So nothing is compared across cohorts
until it has been put in the SAME stratum -- same task profile/risk,
same coarse complexity bucket, same project, same agent/model -- and
each stratum is then TRUNCATED to equal counts in both arms. After
that, the two arms have an identical mix by construction, and the
remaining difference is at least about the pipelines rather than about
the workload.

The price is paid honestly: `MatchResult.dropped` says exactly how many
tasks fell out of the comparison and why, and a run that drops most of
its data says INSUFFICIENT_DATA rather than quietly comparing whatever
survived.

An unknown control value ("this task has no recorded complexity") is
its OWN stratum -- never folded in with a known one. Two tasks whose
complexity nobody recorded are comparable to each other and to nothing
else; pretending unknown means "medium" would be exactly the missing-
is-zero mistake that `model.py` exists to prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .model import COHORT_LEGACY, COHORT_NEW, TaskRecord

# Control dimensions, in the order they appear in a stratum key.
ALL_CONTROLS = ("profile", "complexity", "project", "model", "agent")
DEFAULT_CONTROLS = ("profile", "complexity", "project")

UNKNOWN = "unknown"

# Share of matched tasks in either cohort whose complexity was never
# recorded, above which the complexity control is not really doing its
# job and the report must say so.
MIXED_COMPLEXITY_UNKNOWN_SHARE = 0.20
# Total-variation distance between the two cohorts' complexity mixes,
# above which they are materially different workloads.
MIXED_COMPLEXITY_TVD = 0.15


def control_value(record: TaskRecord, control: str) -> str:
    value = getattr(record, control, None)
    if value is None:
        return UNKNOWN
    text = str(value).strip()
    return text or UNKNOWN


def stratum_key(record: TaskRecord, controls: Sequence[str]) -> tuple[str, ...]:
    return tuple(control_value(record, control) for control in controls)


@dataclass
class Stratum:
    key: tuple[str, ...]
    controls: tuple[str, ...]
    legacy: list[TaskRecord] = field(default_factory=list)
    new: list[TaskRecord] = field(default_factory=list)

    @property
    def matched_size(self) -> int:
        return min(len(self.legacy), len(self.new))

    def balanced(self) -> tuple[list[TaskRecord], list[TaskRecord]]:
        """Equal counts from both arms, chosen deterministically by
        task_id so a rerun over the same data gives the same report."""
        size = self.matched_size
        if size == 0:
            return ([], [])
        legacy = sorted(self.legacy, key=lambda r: r.task_id)[:size]
        new = sorted(self.new, key=lambda r: r.task_id)[:size]
        return (legacy, new)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": dict(zip(self.controls, self.key)),
            "legacy_available": len(self.legacy),
            "new_available": len(self.new),
            "matched_pairs": self.matched_size,
        }


@dataclass(frozen=True)
class MatchResult:
    controls: tuple[str, ...]
    legacy: tuple[TaskRecord, ...]
    new: tuple[TaskRecord, ...]
    strata: tuple[Stratum, ...]
    dropped: dict[str, int]
    warnings: tuple[str, ...]
    complexity_mix: dict[str, dict[str, int]]

    @property
    def matched_pairs(self) -> int:
        return len(self.legacy)

    def as_dict(self) -> dict[str, Any]:
        return {
            "controls": list(self.controls),
            "matched_pairs": self.matched_pairs,
            "legacy_matched": len(self.legacy),
            "new_matched": len(self.new),
            "strata_total": len(self.strata),
            "strata_matched": sum(1 for s in self.strata if s.matched_size > 0),
            "dropped": dict(self.dropped),
            "complexity_mix": self.complexity_mix,
            "warnings": list(self.warnings),
        }


def _complexity_mix(records: Iterable[TaskRecord]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for record in records:
        key = record.complexity or UNKNOWN
        mix[key] = mix.get(key, 0) + 1
    return mix


def _total_variation_distance(left: dict[str, int], right: dict[str, int]) -> float:
    left_total = sum(left.values())
    right_total = sum(right.values())
    if left_total == 0 or right_total == 0:
        return 0.0
    keys = set(left) | set(right)
    return 0.5 * sum(
        abs(left.get(k, 0) / left_total - right.get(k, 0) / right_total) for k in keys
    )


def _unknown_share(records: Sequence[TaskRecord]) -> float:
    if not records:
        return 0.0
    unknown = sum(1 for r in records if not r.complexity)
    return unknown / len(records)


def match_cohorts(
    records: Iterable[TaskRecord],
    *,
    controls: Sequence[str] = DEFAULT_CONTROLS,
) -> MatchResult:
    """Stratify, then truncate each stratum to equal counts."""
    unknown_controls = [c for c in controls if c not in ALL_CONTROLS]
    if unknown_controls:
        raise ValueError(f"unknown control dimension(s): {', '.join(unknown_controls)}")
    controls = tuple(controls)

    strata: dict[tuple[str, ...], Stratum] = {}
    dropped = {"unknown_cohort": 0, "unmatched_stratum": 0, "surplus_in_stratum": 0}

    for record in records:
        if record.cohort not in (COHORT_LEGACY, COHORT_NEW):
            dropped["unknown_cohort"] += 1
            continue
        key = stratum_key(record, controls)
        stratum = strata.setdefault(key, Stratum(key=key, controls=controls))
        if record.cohort == COHORT_LEGACY:
            stratum.legacy.append(record)
        else:
            stratum.new.append(record)

    matched_legacy: list[TaskRecord] = []
    matched_new: list[TaskRecord] = []
    for stratum in sorted(strata.values(), key=lambda s: s.key):
        if stratum.matched_size == 0:
            dropped["unmatched_stratum"] += len(stratum.legacy) + len(stratum.new)
            continue
        legacy, new = stratum.balanced()
        dropped["surplus_in_stratum"] += (len(stratum.legacy) - len(legacy)) + (
            len(stratum.new) - len(new)
        )
        matched_legacy.extend(legacy)
        matched_new.extend(new)

    legacy_mix = _complexity_mix(matched_legacy)
    new_mix = _complexity_mix(matched_new)
    warnings: list[str] = []

    unknown_legacy = _unknown_share(matched_legacy)
    unknown_new = _unknown_share(matched_new)
    if max(unknown_legacy, unknown_new) > MIXED_COMPLEXITY_UNKNOWN_SHARE:
        warnings.append(
            "MIXED_COMPLEXITY: complexity is unrecorded for "
            f"{unknown_legacy:.0%} of matched legacy and {unknown_new:.0%} of matched "
            "new-pipeline tasks, so the complexity control could not be enforced for them"
        )
    if "complexity" not in controls:
        tvd = _total_variation_distance(legacy_mix, new_mix)
        if tvd > MIXED_COMPLEXITY_TVD:
            warnings.append(
                "MIXED_COMPLEXITY: complexity was not used as a matching control and the "
                f"two cohorts' complexity mixes differ (total-variation distance {tvd:.2f}); "
                "the difference below may be a workload difference, not a pipeline difference"
            )

    return MatchResult(
        controls=controls,
        legacy=tuple(matched_legacy),
        new=tuple(matched_new),
        strata=tuple(sorted(strata.values(), key=lambda s: s.key)),
        dropped=dropped,
        warnings=tuple(warnings),
        complexity_mix={"legacy": legacy_mix, "new_pipeline": new_mix},
    )
