"""TMCP-HARNESS-001: the two things decided before anything is spent.

A task can be unstartable for two completely different reasons, and the
cheapest possible handling of both is to find out BEFORE a Planner writes a
contract and a Builder iterates against a bar it can never reach.

  * A HUMAN INPUT -- an approved asset, a licensed file, a real measurement.
    No amount of iteration produces it. It goes to the Human Decision Queue.
  * An INFRASTRUCTURE PREREQUISITE -- the repository is not on this machine,
    or a toolchain the task's own checks invoke is missing or the wrong
    version. Not a decision at all: the same task on a machine that has the
    toolchain is perfectly autonomous.

Conflating them is wrong in both directions. Putting an absent Node runtime
in front of a person as a "decision" asks them to approve something rather
than install it; treating a missing design asset as an infra gap implies
somebody could fix it by installing software.

Both gates cost zero model calls, which is the whole point, and the tests
below assert that as an ABSENCE: no run opened, no decision opened, nothing
sent to a runner.
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_scheduler as sched
from terminal_mcp.harness_pilot import (TaskDefinition, human_input_required,
                                        infra_prerequisite_missing,
                                        installed_version, programs_invoked,
                                        to_nodes)


def _definition(task_id="T-1", *, acceptance=(), checks=(), depends_on=(), lane="A"):
    return TaskDefinition(id=task_id, title=task_id, lane=lane, priority="P0",
                          depends_on=tuple(depends_on), acceptance=tuple(acceptance),
                          checks=tuple(checks))


# ---------------------------------------------------------------------------
# which programs a check actually needs
# ---------------------------------------------------------------------------

def test_a_quoted_regex_is_not_a_list_of_programs():
    """The bug this pins: splitting the raw text on shell operators turns a
    grep pattern's alternation branches into "programs", and the task is
    then deferred for a reason that is not true."""
    command = "node -v && node -v | grep -qE '^v(2[4-9]|[3-9][0-9])\\.'"
    assert programs_invoked(command) == ("node", "grep")


@pytest.mark.parametrize("command,expected", [
    ("npm test", ("npm",)),
    ("npm ci; npx tsc --noEmit", ("npm", "npx")),
    ("NODE_ENV=test npm run build", ("npm",)),
    ("( cd apps && pnpm test )", ("pnpm",)),
    ("pnpm i & pnpm build", ("pnpm",)),
    # Shell builtins are not programs to look for on PATH.
    ("test -d apps/mobile && echo present", ()),
    ("", ()),
])
def test_programs_invoked_reads_the_shell_line_the_way_a_shell_would(command, expected):
    assert programs_invoked(command) == expected


def test_a_malformed_line_yields_nothing_rather_than_guessing():
    assert programs_invoked("echo 'unterminated") == ()


# ---------------------------------------------------------------------------
# the infrastructure gate
# ---------------------------------------------------------------------------

def test_a_repository_that_is_not_here_is_the_first_thing_reported(tmp_path):
    missing = tmp_path / "not-cloned"
    reason = infra_prerequisite_missing(_definition(checks=["true"]), missing)
    assert reason is not None
    assert str(missing) in reason


def test_a_missing_toolchain_is_named_exactly(tmp_path):
    """Through the DECISIVE CHECK MAP -- commands somebody authored, which
    are commands whether or not this host can run them."""
    (tmp_path / "src").mkdir()
    reason = infra_prerequisite_missing(
        _definition(), tmp_path, checks=["definitely-not-a-real-program --check"])
    assert reason is not None
    assert "definitely-not-a-real-program" in reason


def test_a_declared_prose_label_is_never_reported_as_a_missing_program(tmp_path):
    """"component tests" names no executable. Reporting `component` as
    uninstalled would be a blocker that is simply false."""
    (tmp_path / "src").mkdir()
    assert infra_prerequisite_missing(
        _definition(checks=["component tests", "typecheck"]), tmp_path) is None


def test_a_real_command_in_the_definition_is_still_screened(tmp_path):
    """A shell line with operators is unambiguously a command, so its
    programs are checked even without a decisive map."""
    (tmp_path / "src").mkdir()
    reason = infra_prerequisite_missing(
        _definition(checks=["definitely-not-a-real-program --check && echo ok"]),
        tmp_path)
    assert reason is not None
    assert "definitely-not-a-real-program" in reason


def test_a_task_whose_checks_are_all_builtins_needs_no_toolchain(tmp_path):
    (tmp_path / "apps").mkdir()
    assert infra_prerequisite_missing(
        _definition(checks=["test -d apps && echo present"]), tmp_path) is None


def test_a_wrong_version_is_distinguished_from_an_absent_one(tmp_path):
    """"node is missing" and "node is v26 where the contract says v24" need
    completely different actions. A blocker that cannot tell them apart
    sends somebody to find out by hand."""
    (tmp_path / "src").mkdir()
    definition = _definition(acceptance=["node -v reports v24.x"], checks=["true"])

    reason = infra_prerequisite_missing(definition, tmp_path)

    actual = installed_version("node")
    if actual is None:
        assert "not installed" in reason
    else:
        assert reason is None or actual in reason, \
            "a version mismatch must report what is actually here"


def test_the_version_gate_passes_when_the_toolchain_matches(tmp_path, monkeypatch):
    """A shim whose `-v` prints the required major must satisfy the gate --
    otherwise the gate would defer every task on every machine."""
    (tmp_path / "src").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "faketool"
    shim.write_text("#!/bin/sh\necho v24.3.1\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

    definition = _definition(acceptance=["faketool -v reports v24.x"],
                             checks=["faketool -v"])

    assert installed_version("faketool") == "v24.3.1"
    assert infra_prerequisite_missing(definition, tmp_path) is None


def test_a_mismatched_version_reports_what_is_actually_installed(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "faketool"
    shim.write_text("#!/bin/sh\necho v26.7.0\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")

    reason = infra_prerequisite_missing(
        _definition(acceptance=["faketool -v reports v24.x"], checks=["faketool -v"]),
        tmp_path)

    assert reason is not None
    assert "v26.7.0" in reason
    assert "v24" in reason
    assert "not a defect in the work" in reason, \
        "a toolchain gap must not read as a verdict about the code"


# ---------------------------------------------------------------------------
# the two gates route differently
# ---------------------------------------------------------------------------

def test_a_human_asset_and_a_toolchain_gap_are_different_fields(tmp_path):
    (tmp_path / "src").mkdir()
    definitions = {
        "ASSET-1": _definition("ASSET-1", acceptance=["docs/board.png exists"],
                               checks=["true"]),
        "TOOL-1": _definition("TOOL-1", acceptance=["it builds"],
                              checks=["build the app"]),
        "FINE-1": _definition("FINE-1", acceptance=["it builds"], checks=["true"]),
    }
    check_map = {"TOOL-1": ["definitely-not-a-real-program build"]}

    nodes = to_nodes(definitions, repo_root=tmp_path, check_map=check_map)

    assert nodes["ASSET-1"].autonomous is False
    assert nodes["ASSET-1"].infra_prerequisite is None, \
        "a missing design asset is not something you install"

    assert nodes["TOOL-1"].autonomous is True, \
        "a toolchain gap says nothing about whether a person is needed"
    assert nodes["TOOL-1"].infra_prerequisite is not None

    assert nodes["FINE-1"].startable is True
    assert nodes["ASSET-1"].startable is False and nodes["TOOL-1"].startable is False


def test_the_plan_separates_infra_blockers_from_human_ones(tmp_path):
    (tmp_path / "src").mkdir()
    definitions = {
        "ASSET-1": _definition("ASSET-1", acceptance=["docs/board.png exists"],
                               checks=["true"]),
        "TOOL-1": _definition("TOOL-1", acceptance=["it builds"],
                              checks=["build the app"]),
        "GOAL": _definition("GOAL", acceptance=["it ships"], checks=["true"],
                            depends_on=["ASSET-1", "TOOL-1"]),
    }
    nodes = to_nodes(definitions, repo_root=tmp_path,
                     check_map={"TOOL-1": ["definitely-not-a-real-program build"]})

    plan = sched.plan(nodes, target="GOAL")

    infra = dict(plan.blocked_on_infra)
    deferred = dict(plan.deferred)
    assert "TOOL-1" in infra
    assert "ASSET-1" not in infra, "a human asset is not an infra gap"
    assert "ASSET-1" in deferred and "TOOL-1" in deferred
    assert plan.to_dict()["blocked_on_infra"]


def test_nothing_blocked_is_ever_marked_satisfied(tmp_path):
    """The one outcome these gates exist to make unreachable: a dependent
    task starting because its blocked prerequisite was counted as done."""
    (tmp_path / "src").mkdir()
    definitions = {
        "TOOL-1": _definition("TOOL-1", acceptance=["it builds"],
                              checks=["build the app"]),
        "GOAL": _definition("GOAL", acceptance=["it ships"], checks=["true"],
                            depends_on=["TOOL-1"]),
    }
    nodes = to_nodes(definitions, repo_root=tmp_path,
                     check_map={"TOOL-1": ["definitely-not-a-real-program build"]})

    plan = sched.plan(nodes, target="GOAL")

    assert [assignment.task_id for assignment in plan.order] == [], \
        "nothing may run behind an unsatisfied prerequisite"
    assert "GOAL" in dict(plan.deferred)


# ---------------------------------------------------------------------------
# and it costs nothing
# ---------------------------------------------------------------------------

def test_deciding_all_of_this_opens_no_run_and_calls_no_model(tmp_path):
    """The whole justification for the gates: the alternative is learning the
    same facts from a failed Builder iteration."""
    from terminal_mcp.harness_store import HarnessStore

    (tmp_path / "src").mkdir()
    store = HarnessStore(tmp_path / "queue.db")
    definitions = {
        "ASSET-1": _definition("ASSET-1", acceptance=["docs/board.png exists"],
                               checks=["true"]),
        "TOOL-1": _definition("TOOL-1", acceptance=["it builds"],
                              checks=["build the app"]),
    }

    nodes = to_nodes(definitions, repo_root=tmp_path,
                     check_map={"TOOL-1": ["definitely-not-a-real-program build"]})
    plan = sched.plan(nodes, target="TOOL-1")

    assert plan.blocked_on_infra
    assert store.list_runs() == [], "no run was opened to find this out"
    assert store.list_decisions(status="open") == []
    assert store.efficiency_totals([])["llm_calls"] == 0


def test_a_human_asset_reaches_the_queue_only_through_the_closed_list(tmp_path):
    """VIS-001's class. It IS a human decision -- missing_human_input -- and
    that reason is on the closed list, so it can be opened. Completion may
    never be faked in its place."""
    from terminal_mcp.harness_store import HarnessStore

    (tmp_path / "src").mkdir()
    store = HarnessStore(tmp_path / "queue.db")
    definition = _definition("VIS-001",
                             acceptance=["docs/design/reference/00-ui-board.png is approved"],
                             checks=["true"])

    reason = human_input_required(definition, tmp_path)
    assert reason is not None and "00-ui-board.png" in reason

    decision = store.open_decision(
        reason=policy.MISSING_HUMAN_INPUT, task_id="VIS-001", question=reason)
    assert decision["reason"] in policy.HUMAN_DECISION_REASONS
    assert store.list_runs() == [], "routed to a human without opening a run"
