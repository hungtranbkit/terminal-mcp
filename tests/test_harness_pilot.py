"""The UrbanFlow pilot: definition loading, the asset nobody may invent, and
the arithmetic of the report.

These run against the REAL pilot repository when it is present, and skip
cleanly when it is not, so the suite stays portable without the assertions
becoming vague.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_scheduler as sched
from terminal_mcp import harness_state as state
from terminal_mcp.harness_pilot import (DEFAULT_REPO, PILOT_CHECK_MAP,
                                        TaskDefinition, decisive_check_for,
                                        human_input_required, load_definitions,
                                        to_nodes)
from terminal_mcp.harness_store import HarnessStore

REAL_REPO = Path(DEFAULT_REPO)
real_repo_only = pytest.mark.skipif(
    not (REAL_REPO / "TASKS.json").exists(),
    reason="the UrbanFlow pilot repository is not on this machine")


@pytest.fixture
def scaffold(tmp_path):
    """A miniature stand-in with the same shape as the real definition file."""
    root = tmp_path / "repo"
    (root / "docs" / "design" / "reference").mkdir(parents=True)
    (root / "apps").mkdir()
    (root / "TASKS.json").write_text(json.dumps({
        "schema_version": 1, "project": "demo",
        "tasks": [
            {"id": "A-1", "title": "First", "lane": "A", "priority": "P0",
             "depends_on": [], "acceptance": ["apps exists"], "checks": ["test -d apps"],
             "status": "READY"},
            {"id": "A-2", "title": "Second", "lane": "A", "priority": "P0",
             "depends_on": ["A-1"], "acceptance": ["still there"],
             "checks": ["test -d apps"], "status": "PLANNED"},
            {"id": "ASSET-1", "title": "Sync the board", "lane": "D", "priority": "P1",
             "depends_on": [], "acceptance": ["docs/design/reference/board.png exists"],
             "checks": ["sha256sum docs/design/reference/board.png"], "status": "READY"},
        ]}))
    return root


# ---------------------------------------------------------------------------
# the definition file is read, never written
# ---------------------------------------------------------------------------

def test_definitions_load_with_their_declared_status_carried_but_unused(scaffold):
    definitions = load_definitions(scaffold / "TASKS.json")
    assert set(definitions) == {"A-1", "A-2", "ASSET-1"}
    assert definitions["A-1"].declared_status == "READY"
    assert definitions["A-2"].depends_on == ("A-1",)


def test_running_the_pilot_never_writes_the_definition_file(scaffold):
    """A definition file records what was planned. The moment it starts
    recording what happened there are two sources of truth again."""
    tasks_path = scaffold / "TASKS.json"
    before = tasks_path.read_bytes()
    definitions = load_definitions(tasks_path)
    to_nodes(definitions, repo_root=scaffold)
    assert tasks_path.read_bytes() == before


def test_readiness_ignores_the_declared_status_entirely(scaffold):
    """A-2 says PLANNED and A-1 says READY, and neither fact is consulted."""
    definitions = load_definitions(scaffold / "TASKS.json")
    nodes = to_nodes(definitions, repo_root=scaffold)
    assert sched.ready(nodes, satisfied=[], target="A-2") == ["A-1"]
    assert sched.ready(nodes, satisfied=["A-1"], target="A-2") == ["A-2"]


# ---------------------------------------------------------------------------
# the asset nobody may invent
# ---------------------------------------------------------------------------

def test_a_missing_binary_asset_makes_a_task_non_autonomous(scaffold):
    definitions = load_definitions(scaffold / "TASKS.json")
    reason = human_input_required(definitions["ASSET-1"], scaffold)
    assert reason is not None
    assert "board.png" in reason
    assert "no code change can produce" in reason


def test_an_asset_that_is_present_is_not_a_blocker(scaffold):
    (scaffold / "docs" / "design" / "reference" / "board.png").write_bytes(b"\x89PNG")
    definitions = load_definitions(scaffold / "TASKS.json")
    assert human_input_required(definitions["ASSET-1"], scaffold) is None


def test_a_source_file_that_does_not_exist_yet_is_not_a_blocker(scaffold):
    """Code can create source. Refusing to start every greenfield task would
    be the opposite failure and a far more expensive one."""
    definition = TaskDefinition(
        id="X", title="x", lane="A", priority="P0", depends_on=(),
        acceptance=("src/index.ts exists",), checks=("true",))
    assert human_input_required(definition, scaffold) is None


def test_the_blocked_asset_task_is_never_planned_as_startable(scaffold):
    definitions = load_definitions(scaffold / "TASKS.json")
    nodes = to_nodes(definitions, repo_root=scaffold)
    nodes["A-2"] = sched.TaskNode("A-2", lane="A", priority="P0",
                                  depends_on=("A-1", "ASSET-1"))
    plan = sched.plan(nodes, target="A-2")
    assert "ASSET-1" not in [a.task_id for a in plan.order]
    assert "A-2" not in [a.task_id for a in plan.order]
    assert dict(plan.deferred)["ASSET-1"]


# ---------------------------------------------------------------------------
# a check must print what it decided on
# ---------------------------------------------------------------------------

def test_a_derived_version_check_prints_the_version_it_gated_on():
    """`grep -q` alone decides the exit status and leaves no evidence of WHAT
    was decided, which is useless to an evaluator and to an audit."""
    command = decisive_check_for("node -v reports v24.x")
    assert command is not None
    assert command.startswith("node -v &&")
    assert "grep -q" in command


def test_nothing_is_invented_for_a_criterion_with_no_derivable_gate():
    assert decisive_check_for("the header looks balanced") is None


@pytest.fixture
def pilot_toolchain(declared_toolchain):
    """Planning tests model a host meeting the pilot's declared Node version."""
    node = declared_toolchain / "node"
    node.write_text("#!/bin/sh\necho v24.0.0\n")
    node.chmod(0o755)


# ---------------------------------------------------------------------------
# against the real repository
# ---------------------------------------------------------------------------

@real_repo_only
def test_exactly_one_urbanflow_task_needs_a_human_asset():
    definitions = load_definitions(REAL_REPO / "TASKS.json")
    blocked = {task_id: human_input_required(definition, REAL_REPO)
               for task_id, definition in definitions.items()}
    needing = {k: v for k, v in blocked.items() if v}
    assert list(needing) == ["VIS-001"], needing
    assert "00-ui-board.png" in needing["VIS-001"]


@real_repo_only
def test_the_real_mob_011_chain_is_nine_tasks_deep(pilot_toolchain):
    definitions = load_definitions(REAL_REPO / "TASKS.json")
    nodes = to_nodes(definitions, repo_root=REAL_REPO, critical_tasks=["MOB-011"])
    order = [a.task_id for a in sched.plan(nodes, target="MOB-011").order]
    assert order[0] == "ENV-001" and order[-1] == "MOB-011"
    assert len(order) == 9
    assert set(order) == {"ENV-001", "ENV-006", "MOB-001", "MOB-002", "MOB-003",
                          "CT-001", "CT-004", "CT-005", "MOB-011"}


@real_repo_only
def test_only_the_milestone_is_escalated_to_critical():
    definitions = load_definitions(REAL_REPO / "TASKS.json")
    nodes = to_nodes(definitions, repo_root=REAL_REPO, critical_tasks=["MOB-011"])
    escalated = [t for t in PILOT_CHECK_MAP if nodes[t].requested_mode == policy.CRITICAL]
    assert escalated == ["MOB-011"]


@real_repo_only
def test_the_cost_first_plan_opens_two_sessions_for_nine_tasks(pilot_toolchain):
    definitions = load_definitions(REAL_REPO / "TASKS.json")
    nodes = to_nodes(definitions, repo_root=REAL_REPO, critical_tasks=["MOB-011"])
    plan = sched.plan(nodes, target="MOB-011", mode=sched.COST_FIRST)
    fresh = [a for a in plan.order if not a.inherits_session]
    assert len(plan.order) == 9
    assert len(fresh) == 2, "one session per lane, not one per task"
    assert plan.session_reuses == 7


@real_repo_only
def test_every_mapped_pilot_check_is_a_runnable_command():
    from terminal_mcp.harness_engine import partition_checks
    for task_id, commands in PILOT_CHECK_MAP.items():
        runnable, prose = partition_checks(commands)
        assert runnable == tuple(commands), f"{task_id} declares a non-command"
        assert prose == ()
