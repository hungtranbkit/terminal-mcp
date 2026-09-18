"""Turn a feature's subtask DAG into real, durable, dependency-ordered queue rows.

WHAT THIS IS NOT

It is not a second planner, a second queue, or a second dependency mechanism.
`PlannerService` already owns decomposition infrastructure -- validation that
refuses a child with no acceptance criteria, parent/child linking, a real
`depends_on` DAG over the ONE queue engine, and the parent-completion rule that
finishes a parent only once every child has. `queue_service.create_task`
already owns `request_key` idempotency and cycle validation.

So this module is an ADAPTER, and a deliberately thin one: it translates a
`WorkSpec.subtask_dag` into the `children` shape `PlannerService.propose_split`
already accepts, and refuses the decompositions that shape cannot express.
Everything durable happens in the modules that already had it.

WHY THE TRANSLATION IS NOT TRIVIAL

`_apply_split` links children by `depends_on_indices` -- positions in the list
being created, resolved as it goes. A spec's DAG names dependencies by subtask
ID, in whatever order a planner wrote them. Handing that list over unsorted
produces a child that depends on a position that does not exist yet, which
resolves to nothing: the dependency is silently DROPPED and the subtask runs
immediately, out of order, with none of its prerequisites done.

That failure is invisible -- there is no error, just a DAG that quietly became
a flat list. So the translation topologically sorts first, and any DAG that
cannot be sorted is refused before a single row is created.

REFUSED BEFORE ANYTHING IS CREATED

A cycle, a dependency on an unknown id, a duplicate id, or a subtask with no
acceptance criteria all stop the whole decomposition. Half a DAG in the queue
is worse than none: the created half starts running against prerequisites that
will never exist, and nothing records that the rest was meant to follow.

IDEMPOTENT BY CONSTRUCTION

Each child carries a `request_key` derived from the spec id and the subtask id,
so re-running a decomposition -- after a restart, a retry, or a resumed
planning round -- returns the EXISTING rows rather than a second copy of the
DAG. The key is derived, never random, because a random key regenerated on
retry is the same as no key at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .work_spec import WorkSpec

# Reasons, as stable strings: they reach the planner, the UI and the MCP
# surface, and a caller may branch on them.
CYCLE = "SUBTASK_CYCLE"
UNKNOWN_DEPENDENCY = "UNKNOWN_DEPENDENCY"
DUPLICATE_ID = "DUPLICATE_SUBTASK_ID"
MISSING_ACCEPTANCE = "SUBTASK_MISSING_ACCEPTANCE"
MISSING_ID = "SUBTASK_MISSING_ID"
EMPTY = "EMPTY_DAG"


class DecompositionError(ValueError):
    """A DAG that cannot be expressed as ordered queue rows.

    Carries the reason code and the subtasks involved, because "invalid DAG"
    with no pointer is a message a planner cannot act on.
    """

    def __init__(self, reason: str, detail: str, *, subtasks: Sequence[str] = ()) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail
        self.subtasks = tuple(subtasks)

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.reason, "detail": self.detail,
                "subtasks": list(self.subtasks)}


@dataclass
class PreparedChild:
    """One subtask, in the shape PlannerService already accepts."""

    subtask_id: str
    payload: dict[str, Any] = field(default_factory=dict)


def request_key_for(spec: WorkSpec, subtask_id: str) -> str:
    """Derived, never random.

    A random key regenerated on retry is the same as no key at all -- the
    second run would not recognise the first run's rows.
    """
    return f"workspec:{spec.spec_id}:{subtask_id}"


def _normalise(spec: WorkSpec) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(spec.subtask_dag or ()):
        node = dict(raw or {})
        subtask_id = str(node.get("id") or "").strip()
        if not subtask_id:
            raise DecompositionError(
                MISSING_ID, f"subtask at position {index} has no id; "
                            f"dependencies are named by id, so every node needs one")
        if subtask_id in seen:
            raise DecompositionError(
                DUPLICATE_ID, f"subtask id {subtask_id!r} appears more than once",
                subtasks=[subtask_id])
        seen.add(subtask_id)
        node["id"] = subtask_id
        node["depends_on"] = [str(d).strip() for d in (node.get("depends_on") or []) if str(d).strip()]
        nodes.append(node)

    for node in nodes:
        unknown = [d for d in node["depends_on"] if d not in seen]
        if unknown:
            raise DecompositionError(
                UNKNOWN_DEPENDENCY,
                f"subtask {node['id']!r} depends on unknown id(s): {', '.join(unknown)}",
                subtasks=[node["id"], *unknown])
        if node["id"] in node["depends_on"]:
            raise DecompositionError(
                CYCLE, f"subtask {node['id']!r} depends on itself",
                subtasks=[node["id"]])
    return nodes


def topological_order(spec: WorkSpec) -> list[dict[str, Any]]:
    """Subtasks in an order where every dependency precedes its dependent.

    Kahn's algorithm, with ties broken by the planner's own ordering so the
    result is deterministic -- a decomposition that reorders itself between
    runs would defeat the derived request keys.
    """
    nodes = _normalise(spec)
    if not nodes:
        raise DecompositionError(EMPTY, "the spec carries no subtask_dag")

    by_id = {node["id"]: node for node in nodes}
    remaining = {node["id"]: set(node["depends_on"]) for node in nodes}
    order: list[dict[str, Any]] = []

    while remaining:
        ready = [node["id"] for node in nodes
                 if node["id"] in remaining and not remaining[node["id"]]]
        if not ready:
            raise DecompositionError(
                CYCLE,
                "these subtasks depend on each other in a cycle: "
                + ", ".join(sorted(remaining)),
                subtasks=sorted(remaining))
        for subtask_id in ready:
            order.append(by_id[subtask_id])
            del remaining[subtask_id]
        for pending in remaining.values():
            pending.difference_update(ready)
    return order


def prepare(spec: WorkSpec, *, session: str | None = None,
            project: str | None = None) -> list[dict[str, Any]]:
    """The `children` list `PlannerService.propose_split` accepts.

    Dependencies arrive as ids and leave as `depends_on_indices`, because that
    is what `_apply_split` resolves. The sort above is what makes those indices
    correct: an unsorted list produces indices that do not exist yet, which
    resolve to nothing and silently flatten the DAG.
    """
    ordered = topological_order(spec)
    position = {node["id"]: index for index, node in enumerate(ordered)}

    children: list[dict[str, Any]] = []
    for node in ordered:
        acceptance = node.get("acceptance_criteria") or []
        if not acceptance:
            # PlannerService would refuse this too, but it would refuse it
            # AFTER earlier children were created. Refusing here keeps the
            # all-or-nothing promise.
            raise DecompositionError(
                MISSING_ACCEPTANCE,
                f"subtask {node['id']!r} has no acceptance_criteria; a child "
                f"nobody can check is a child nobody can finish",
                subtasks=[node["id"]])

        prompt = node.get("prompt") or node.get("title") or node["id"]
        children.append({
            "title": node.get("title") or node["id"],
            "prompt": prompt,
            "acceptance_criteria": list(acceptance),
            "depends_on_indices": sorted(position[d] for d in node["depends_on"]),
            "session": node.get("session") or session,
            "project": node.get("project") or project,
            "priority": int(node.get("priority") or 0),
            # Derived from the spec and the subtask, so a re-run returns the
            # existing rows instead of a second DAG.
            "request_key": request_key_for(spec, node["id"]),
            "metadata": {
                **(node.get("metadata") or {}),
                "work_spec_id": spec.spec_id,
                "subtask_id": node["id"],
                "task_type": spec.task_type,
            },
        })
    return children


def decompose(spec: WorkSpec, *, planner: Any, parent_task_id: str,
              session: str | None = None,
              project: str | None = None) -> dict[str, Any]:
    """Create the DAG as durable queue rows, through the planner that owns it.

    AUTO mode: the decomposition was already approved when the spec passed its
    completeness gate, and asking for a second approval of the same decision is
    process for its own sake. A caller wanting the two-step review calls
    `planner.propose_split` with `prepare(spec)` instead.
    """
    from .pm_service import VALID_MODES  # noqa: F401 -- vocabulary lives there

    children = prepare(spec, session=session, project=project)
    result = planner.propose_split(parent_task_id, children, mode="AUTO")
    if "error" in result:
        return result

    child_ids = result.get("child_task_ids") or []
    return {
        "parent_task_id": parent_task_id,
        "spec_id": spec.spec_id,
        "child_task_ids": child_ids,
        "subtask_ids": [node["metadata"]["subtask_id"] for node in children],
        "order": [child["title"] for child in children],
        # What a caller needs to verify idempotency without re-reading the DB.
        "request_keys": [child["request_key"] for child in children],
    }
