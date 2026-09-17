"""Deterministic, bounded microtasks over the existing work DAG.

This module is policy, not another planner.  The subtask graph remains
``WorkSpec.subtask_dag`` and :mod:`work_decompose` remains responsible for its
topological order, dependency indices, and deterministic request keys.  The
only extra decision made here is whether every already-planned child is small
enough to execute as one bounded slice.
"""
from __future__ import annotations

import math
from numbers import Real
from typing import Any, Sequence

from . import work_decompose
from .work_spec import WorkSpec

POLICY_VERSION = "v1"
TARGET_MINUTES = 20
SOFT_LIMIT_MINUTES = 25
HARD_STOP_MINUTES = 30
CHECKPOINT_AFTER_MINUTES = 20

MICRO_TASK_INVALID_ESTIMATE = "MICRO_TASK_INVALID_ESTIMATE"
MICRO_TASK_TOO_LARGE = "MICRO_TASK_TOO_LARGE"


class MicrotaskError(ValueError):
    """A subtask DAG that violates the deterministic sizing policy."""

    def __init__(self, reason: str, detail: str, *, offending_ids: Sequence[str]) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.offending_ids = tuple(offending_ids)
        # Match DecompositionError's useful vocabulary for callers which
        # already handle errors raised while preparing a WorkSpec DAG.
        self.subtasks = self.offending_ids

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.reason, "detail": self.detail,
                "offending_ids": list(self.offending_ids)}


def _estimate(node: dict[str, Any]) -> Real:
    """Return the declared estimate, or the v1 compatibility default."""
    value = node.get("estimated_minutes")
    if value is None:
        return TARGET_MINUTES
    # bool is a Real in Python, but accepting True as one minute would be an
    # accidental JSON/Python coercion rather than an estimate.
    if (isinstance(value, bool) or not isinstance(value, Real)
            or not math.isfinite(value) or value <= 0):
        raise MicrotaskError(
            MICRO_TASK_INVALID_ESTIMATE,
            f"subtask {node['id']!r} has invalid estimated_minutes {value!r}; "
            "the estimate must be greater than zero",
            offending_ids=[node["id"]],
        )
    return value


def _ordered_with_estimates(spec: WorkSpec) -> tuple[list[dict[str, Any]], dict[str, Real]]:
    ordered = work_decompose.topological_order(spec)
    invalid: list[str] = []
    estimates: dict[str, Real] = {}
    for node in ordered:
        try:
            estimates[node["id"]] = _estimate(node)
        except MicrotaskError:
            invalid.append(node["id"])
    if invalid:
        raise MicrotaskError(
            MICRO_TASK_INVALID_ESTIMATE,
            "estimated_minutes must be greater than zero for: " + ", ".join(invalid),
            offending_ids=invalid,
        )
    return ordered, estimates


def _maximum_antichain_width(ordered: list[dict[str, Any]]) -> int:
    """Exact DAG width via a minimum path cover in its transitive closure."""
    if not ordered:
        return 0

    ids = [node["id"] for node in ordered]
    ancestors: dict[str, set[str]] = {}
    for node in ordered:
        reached: set[str] = set(node["depends_on"])
        for dependency in node["depends_on"]:
            reached.update(ancestors[dependency])
        ancestors[node["id"]] = reached

    # In the bipartite comparability graph, n - maximum matching is the
    # maximum antichain (Dilworth).  That is the largest set the DAG permits
    # to be in flight in parallel, independent of how Kahn-sort ties break.
    edges = {left: [right for right in ids if left in ancestors[right]]
             for left in ids}
    matched_left_by_right: dict[str, str] = {}

    def augment(left: str, seen: set[str]) -> bool:
        for right in edges[left]:
            if right in seen:
                continue
            seen.add(right)
            previous = matched_left_by_right.get(right)
            if previous is None or augment(previous, seen):
                matched_left_by_right[right] = left
                return True
        return False

    matching = sum(1 for left in ids if augment(left, set()))
    return len(ids) - matching


def audit(spec: WorkSpec) -> dict[str, Any]:
    """Return sizing and concurrency facts derived from the spec DAG only.

    Oversized estimates are reported rather than rejected here so callers can
    inspect a plan.  ``prepare`` is the enforcement boundary.
    """
    ordered, estimates = _ordered_with_estimates(spec)
    longest_to: dict[str, Real] = {}
    for node in ordered:
        prefix = max((longest_to[item] for item in node["depends_on"]), default=0)
        longest_to[node["id"]] = prefix + estimates[node["id"]]

    return {
        "total_slices": len(ordered),
        "critical_path_minutes": max(longest_to.values(), default=0),
        "oversized_ids": [node["id"] for node in ordered
                          if estimates[node["id"]] > HARD_STOP_MINUTES],
        "max_parallel_width": _maximum_antichain_width(ordered),
    }


def prepare(spec: WorkSpec, *, session: str | None = None,
            project: str | None = None) -> list[dict[str, Any]]:
    """Prepare existing DAG nodes as bounded PlannerService children.

    Validation of *all* estimates happens before delegating, so an oversized
    child cannot follow already-created siblings.  Prompts, order, dependency
    indices, and request keys come unchanged from the existing adapter.
    """
    ordered, estimates = _ordered_with_estimates(spec)
    oversized = [node["id"] for node in ordered
                 if estimates[node["id"]] > HARD_STOP_MINUTES]
    if oversized:
        raise MicrotaskError(
            MICRO_TASK_TOO_LARGE,
            f"estimated_minutes exceeds the {HARD_STOP_MINUTES}-minute hard stop for: "
            + ", ".join(oversized),
            offending_ids=oversized,
        )

    children = work_decompose.prepare(spec, session=session, project=project)
    for child in children:
        subtask_id = child["metadata"]["subtask_id"]
        child["metadata"].update({
            "estimated_minutes": estimates[subtask_id],
            "microtask_policy": POLICY_VERSION,
            "checkpoint_after_minutes": CHECKPOINT_AFTER_MINUTES,
            "hard_stop_minutes": HARD_STOP_MINUTES,
        })
    return children


def decompose(spec: WorkSpec, *, planner: Any, parent_task_id: str,
              session: str | None = None,
              project: str | None = None) -> dict[str, Any]:
    """Create policy-compliant children through the existing PlannerService.

    ``prepare`` completes validation for the entire DAG before
    ``propose_split`` is called.  Consequently PlannerService cannot create a
    prefix of children and only then discover an oversized later node.
    """
    children = prepare(spec, session=session, project=project)
    result = planner.propose_split(parent_task_id, children, mode="AUTO")
    if "error" in result:
        return result

    return {
        "parent_task_id": parent_task_id,
        "spec_id": spec.spec_id,
        "child_task_ids": result.get("child_task_ids") or [],
        "subtask_ids": [child["metadata"]["subtask_id"] for child in children],
        "order": [child["title"] for child in children],
        "request_keys": [child["request_key"] for child in children],
    }
