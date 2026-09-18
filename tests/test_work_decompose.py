"""A feature's DAG has to reach the queue as a DAG, or not at all.

The silent failure this guards: `_apply_split` links children by
`depends_on_indices` -- positions in the list as it is being created. A spec
names dependencies by subtask id, in whatever order a planner wrote them.
Hand that list over unsorted and a child depends on a position that does not
exist yet, which resolves to nothing: the dependency is DROPPED, the subtask
runs immediately with none of its prerequisites done, and there is no error
anywhere. A DAG quietly becomes a flat list.

So the sort is load-bearing, and so is refusing before anything is created:
half a DAG in the queue is worse than none.

SAFETY: every session name here is a disposable fixture string.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_decompose as wd
from terminal_mcp import work_spec as ws
from terminal_mcp.planner_service import PlannerService
from terminal_mcp.planner_store import PlannerStore
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def planner(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    return PlannerService(PlannerStore(tmp_path / "planner.db"), queue)


def _spec(dag) -> ws.WorkSpec:
    spec = ws.WorkSpec(spec_id="spec_feature1", title="CSV export",
                       task_type=ws.FEATURE_NEW)
    spec.subtask_dag = tuple(dag)
    return spec


def _node(node_id, *, depends_on=(), acceptance=("it works",), **kw):
    return {"id": node_id, "title": f"subtask {node_id}",
            "prompt": f"build {node_id}", "depends_on": list(depends_on),
            "acceptance_criteria": list(acceptance), **kw}


# -- the sort is what makes the indices correct ------------------------------------

def test_dependencies_are_ordered_before_their_dependents():
    spec = _spec([_node("route", depends_on=["serialiser"]),
                  _node("serialiser"),
                  _node("button", depends_on=["route"])])

    order = [node["id"] for node in wd.topological_order(spec)]

    assert order.index("serialiser") < order.index("route")
    assert order.index("route") < order.index("button")


def test_every_dependency_index_points_backwards():
    """The property that makes _apply_split's index resolution correct: a
    forward reference resolves to nothing and silently drops the edge."""
    spec = _spec([_node("c", depends_on=["a", "b"]), _node("b", depends_on=["a"]),
                  _node("a")])

    children = wd.prepare(spec)

    for position, child in enumerate(children):
        assert all(index < position for index in child["depends_on_indices"]), \
            f"{child['title']} depends on a position that does not exist yet"


def test_independent_subtasks_keep_the_planners_own_order():
    """Deterministic ties: a decomposition that reorders itself between runs
    would defeat the derived request keys."""
    spec = _spec([_node("a"), _node("b"), _node("c")])
    assert [n["id"] for n in wd.topological_order(spec)] == ["a", "b", "c"]


# -- refused before anything is created ---------------------------------------------

def test_a_cycle_is_refused_and_names_the_subtasks_involved():
    spec = _spec([_node("a", depends_on=["b"]), _node("b", depends_on=["a"])])

    with pytest.raises(wd.DecompositionError) as caught:
        wd.topological_order(spec)

    assert caught.value.reason == wd.CYCLE
    assert set(caught.value.subtasks) == {"a", "b"}


def test_a_self_dependency_is_a_cycle():
    spec = _spec([_node("a", depends_on=["a"])])
    with pytest.raises(wd.DecompositionError) as caught:
        wd.topological_order(spec)
    assert caught.value.reason == wd.CYCLE


def test_an_unknown_dependency_is_refused_rather_than_dropped():
    """The whole point: an edge that cannot be resolved must be an error, not
    a silently missing prerequisite."""
    spec = _spec([_node("a", depends_on=["nope"])])

    with pytest.raises(wd.DecompositionError) as caught:
        wd.topological_order(spec)

    assert caught.value.reason == wd.UNKNOWN_DEPENDENCY
    assert "nope" in caught.value.subtasks


def test_a_duplicate_subtask_id_is_refused():
    spec = _spec([_node("a"), _node("a")])
    with pytest.raises(wd.DecompositionError) as caught:
        wd.topological_order(spec)
    assert caught.value.reason == wd.DUPLICATE_ID


def test_a_subtask_without_an_id_is_refused():
    spec = _spec([{"title": "nameless", "prompt": "do it"}])
    with pytest.raises(wd.DecompositionError) as caught:
        wd.topological_order(spec)
    assert caught.value.reason == wd.MISSING_ID


def test_an_empty_dag_is_refused():
    with pytest.raises(wd.DecompositionError) as caught:
        wd.topological_order(_spec([]))
    assert caught.value.reason == wd.EMPTY


def test_a_subtask_with_no_acceptance_criteria_stops_the_whole_decomposition():
    """PlannerService would refuse it too -- but only AFTER creating the
    earlier children, leaving half a DAG behind."""
    spec = _spec([_node("a"), _node("b", acceptance=())])

    with pytest.raises(wd.DecompositionError) as caught:
        wd.prepare(spec)

    assert caught.value.reason == wd.MISSING_ACCEPTANCE
    assert "b" in caught.value.subtasks


def test_nothing_reaches_the_queue_when_the_dag_is_invalid(planner):
    parent = planner.queue.create_task("Big feature", "build it", session=None)
    spec = _spec([_node("a"), _node("b", acceptance=())])

    with pytest.raises(wd.DecompositionError):
        wd.decompose(spec, planner=planner, parent_task_id=parent["task_id"])

    board = planner.queue.board()
    assert board["counts"]["backlog"] == 1, "only the parent -- no half DAG left behind"


# -- idempotency -------------------------------------------------------------------

def test_the_request_key_is_derived_from_the_spec_and_subtask():
    """Derived, never random: a random key regenerated on retry is the same as
    no key at all."""
    spec = _spec([_node("a")])
    first = wd.prepare(spec)[0]["request_key"]
    second = wd.prepare(spec)[0]["request_key"]

    assert first == second == wd.request_key_for(spec, "a")
    assert spec.spec_id in first and "a" in first


def test_two_specs_do_not_share_a_request_key():
    left = _spec([_node("a")])
    right = _spec([_node("a")])
    right.spec_id = "spec_feature2"

    assert wd.prepare(left)[0]["request_key"] != wd.prepare(right)[0]["request_key"]


def test_re_decomposing_returns_the_same_children_not_a_second_dag(planner):
    """A restart, a retry or a resumed planning round must not double the DAG."""
    parent = planner.queue.create_task("Big feature", "build it", session=None)
    spec = _spec([_node("serialiser"), _node("route", depends_on=["serialiser"])])

    first = wd.decompose(spec, planner=planner, parent_task_id=parent["task_id"])
    assert len(first["child_task_ids"]) == 2

    second_parent = planner.queue.create_task("Big feature again", "build it",
                                              session=None)
    second = wd.decompose(spec, planner=planner,
                          parent_task_id=second_parent["task_id"])

    assert second["child_task_ids"] == first["child_task_ids"], \
        "the same spec and subtask ids must resolve to the existing rows"


# -- the real queue rows -------------------------------------------------------------

def test_the_children_land_in_the_queue_with_their_dependency_edges(planner):
    parent = planner.queue.create_task("Big feature", "build it", session=None)
    spec = _spec([_node("route", depends_on=["serialiser"]), _node("serialiser")])

    result = wd.decompose(spec, planner=planner, parent_task_id=parent["task_id"])

    assert result["order"] == ["subtask serialiser", "subtask route"]
    serialiser_id, route_id = result["child_task_ids"]
    route_row = planner.queue.store.get_task(route_id)
    assert serialiser_id in route_row.depends_on, \
        "the edge must survive into the durable row, not just the plan"


def test_each_child_carries_its_spec_and_subtask_identity(planner):
    parent = planner.queue.create_task("Big feature", "build it", session=None)
    spec = _spec([_node("serialiser")])

    result = wd.decompose(spec, planner=planner, parent_task_id=parent["task_id"])
    row = planner.queue.store.get_task(result["child_task_ids"][0])

    assert row.metadata["work_spec_id"] == spec.spec_id
    assert row.metadata["subtask_id"] == "serialiser"
    assert row.metadata["task_type"] == ws.FEATURE_NEW
    assert row.metadata["parent_task_id"] == parent["task_id"]


def test_the_parent_is_parked_rather_than_left_dispatchable(planner):
    """The parent has no work of its own once it is split; leaving it QUEUED
    would dispatch the whole feature as one prompt beside its own children."""
    parent = planner.queue.create_task("Big feature", "build it", session=None)
    spec = _spec([_node("a"), _node("b")])

    wd.decompose(spec, planner=planner, parent_task_id=parent["task_id"])

    assert planner.queue.store.get_task(parent["task_id"]).status == "BLOCKED"
