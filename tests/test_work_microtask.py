from __future__ import annotations

import pytest

from terminal_mcp import work_decompose as wd
from terminal_mcp import work_microtask as wm
from terminal_mcp import work_planning
from terminal_mcp import work_spec as ws


def _node(node_id, *, depends_on=(), estimate=None, prompt=None):
    node = {
        "id": node_id,
        "title": f"subtask {node_id}",
        "prompt": prompt or f"build {node_id}",
        "depends_on": list(depends_on),
        "acceptance_criteria": ["it works"],
    }
    if estimate is not None:
        node["estimated_minutes"] = estimate
    return node


def _spec(nodes):
    spec = ws.WorkSpec(spec_id="spec_micro", title="bounded work",
                       task_type=ws.FEATURE_NEW)
    spec.subtask_dag = tuple(nodes)
    return spec


def test_missing_estimate_defaults_to_target():
    child = wm.prepare(_spec([_node("a")]))[0]
    assert child["metadata"]["estimated_minutes"] == 20


def test_thirty_minutes_is_accepted():
    assert wm.prepare(_spec([_node("a", estimate=30)]))[0]["metadata"][
        "estimated_minutes"] == 30


def test_thirty_one_minutes_is_rejected_with_all_offending_ids():
    class PlannerThatMustNotBeCalled:
        calls = 0

        def propose_split(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("oversized DAG reached PlannerService")

    planner = PlannerThatMustNotBeCalled()
    with pytest.raises(wm.MicrotaskError) as caught:
        work_planning.decompose_microtasks(
            _spec([_node("a", estimate=31), _node("b", estimate=45)]),
            planner=planner, parent_task_id="parent",
        )
    assert caught.value.reason == wm.MICRO_TASK_TOO_LARGE
    assert caught.value.offending_ids == ("a", "b")
    assert planner.calls == 0


@pytest.mark.parametrize("estimate", [0, -1])
def test_non_positive_estimate_is_invalid(estimate):
    with pytest.raises(wm.MicrotaskError) as caught:
        wm.prepare(_spec([_node("a", estimate=estimate)]))
    assert caught.value.reason == wm.MICRO_TASK_INVALID_ESTIMATE
    assert caught.value.offending_ids == ("a",)


def test_order_dependency_indices_request_keys_and_prompts_are_unchanged():
    prompt = "Use this exact prompt; do not append checkpoint instructions."
    spec = _spec([_node("c", depends_on=("a",), prompt=prompt), _node("a")])

    baseline = wd.prepare(spec)
    prepared = wm.prepare(spec)

    assert [c["metadata"]["subtask_id"] for c in prepared] == ["a", "c"]
    assert [c["depends_on_indices"] for c in prepared] == [[], [0]]
    assert [c["request_key"] for c in prepared] == [c["request_key"] for c in baseline]
    assert [c["prompt"] for c in prepared] == [c["prompt"] for c in baseline]
    assert prepared[1]["prompt"] == prompt


def test_policy_metadata_is_added_without_losing_existing_metadata():
    node = _node("a", estimate=25)
    node["metadata"] = {"caller": "kept"}
    metadata = work_planning.prepare_microtasks(_spec([node]))[0]["metadata"]
    assert metadata == {
        "caller": "kept",
        "work_spec_id": "spec_micro",
        "subtask_id": "a",
        "task_type": ws.FEATURE_NEW,
        "estimated_minutes": 25,
        "microtask_policy": "v1",
        "checkpoint_after_minutes": 20,
        "hard_stop_minutes": 30,
    }


def test_audit_critical_path_and_parallel_width_are_from_the_dag():
    # a(10) fans out to b(20) and c(5), then joins at d(10).
    spec = _spec([
        _node("d", depends_on=("b", "c"), estimate=10),
        _node("c", depends_on=("a",), estimate=5),
        _node("b", depends_on=("a",), estimate=20),
        _node("a", estimate=10),
    ])
    report = wm.audit(spec)
    assert report == {
        "total_slices": 4,
        "critical_path_minutes": 40,
        "oversized_ids": [],
        "max_parallel_width": 2,
    }


def test_audit_reports_oversized_without_creating_or_rewriting_anything():
    spec = _spec([_node("a", estimate=31, prompt="original")])
    assert work_planning.audit_microtasks(spec)["oversized_ids"] == ["a"]
    assert spec.subtask_dag[0]["prompt"] == "original"
