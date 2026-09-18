"""Test selection has exactly one dangerous failure: selecting nothing and
looking green.

So most of these assert the fail-closed direction rather than the happy path.
A selection that misses the one test that mattered is worse than no selection
at all, because it is reported with the same confidence.
"""
from __future__ import annotations

import pytest

from terminal_mcp import test_selection as sel


@pytest.fixture
def tests_dir(tmp_path):
    """A miniature suite, so the assertions are about the algorithm rather
    than about whichever real tests happen to import a module today."""
    root = tmp_path / "tests"
    root.mkdir()
    (root / "test_alpha.py").write_text(
        "from terminal_mcp.alpha import thing\n", encoding="utf-8")
    (root / "test_beta.py").write_text(
        "from terminal_mcp import beta as b\nimport terminal_mcp.gamma\n", encoding="utf-8")
    (root / "test_both.py").write_text(
        "from terminal_mcp.alpha import x\nfrom terminal_mcp.beta import y\n", encoding="utf-8")
    (root / "test_patched.py").write_text(
        'from unittest.mock import patch\npatch("terminal_mcp.delta.run")\n', encoding="utf-8")
    return root


# -- the index is derived from the tests themselves -------------------------------

def test_the_index_finds_every_import_form(tests_dir):
    """A hand-maintained table drifts on the first rename; this reads the
    files. All four import spellings used in this suite must be found."""
    index = sel.build_index(tests_dir)

    assert "tests/test_alpha.py" in index["alpha"]      # from terminal_mcp.alpha import
    assert "tests/test_beta.py" in index["beta"]        # from terminal_mcp import beta as b
    assert "tests/test_beta.py" in index["gamma"]       # import terminal_mcp.gamma
    assert "tests/test_patched.py" in index["delta"]    # patch("terminal_mcp.delta...")


def test_a_module_imported_by_several_tests_selects_all_of_them(tests_dir):
    selection = sel.select(["terminal_mcp/alpha.py"], tests_dir=tests_dir)

    assert selection.lane == sel.FAST_LANE
    assert selection.tests == ["tests/test_alpha.py", "tests/test_both.py"]
    assert selection.reasons["tests/test_both.py"] == ["imports alpha"]


# -- fail closed ------------------------------------------------------------------

def test_no_changed_paths_is_full_verify_not_an_empty_lane():
    selection = sel.select([])
    assert selection.lane == sel.FULL_VERIFY
    assert selection.tests == []
    assert selection.full_verify_because


def test_a_module_no_test_imports_forces_full_verify(tests_dir):
    """The quiet-green case: a new module nothing covers must not report a
    narrow pass."""
    selection = sel.select(["terminal_mcp/brand_new.py"], tests_dir=tests_dir)

    assert selection.lane == sel.FULL_VERIFY
    assert "terminal_mcp/brand_new.py" in selection.unmapped_paths
    assert any("no test imports brand_new" in why for why in selection.full_verify_because)


def test_a_path_outside_the_package_forces_full_verify(tests_dir):
    selection = sel.select(["scripts/agent/deploy.sh"], tests_dir=tests_dir)

    assert selection.lane == sel.FULL_VERIFY
    assert any("blast radius unknown" in why for why in selection.full_verify_because)


@pytest.mark.parametrize("path", ["pyproject.toml", "tests/conftest.py",
                                  "deploy/node-agent.service"])
def test_infrastructure_changes_force_full_verify(path, tests_dir):
    selection = sel.select([path], tests_dir=tests_dir)
    assert selection.lane == sel.FULL_VERIFY


def test_a_high_fan_in_module_forces_full_verify(tests_dir):
    """Selecting for `config` pretends to narrow while narrowing nothing."""
    selection = sel.select(["terminal_mcp/config.py"], tests_dir=tests_dir)

    assert selection.lane == sel.FULL_VERIFY
    assert any("high fan-in" in why for why in selection.full_verify_because)


def test_one_unnarrowable_path_in_a_batch_forces_full_verify_for_the_batch(tests_dir):
    """Mixing a known module with an unknown one must not quietly run only the
    known one's tests."""
    selection = sel.select(["terminal_mcp/alpha.py", "terminal_mcp/brand_new.py"],
                           tests_dir=tests_dir)

    assert selection.lane == sel.FULL_VERIFY
    assert any("brand_new" in why for why in selection.full_verify_because)


def test_full_verify_never_inherits_a_narrowed_command(tests_dir):
    """The command must name no paths, or a narrowed selection leaks into what
    was supposed to be the whole suite."""
    selection = sel.select(["terminal_mcp/alpha.py", "terminal_mcp/brand_new.py"],
                           tests_dir=tests_dir)
    assert selection.command() == ["pytest", "-q"]


# -- changed tests run themselves --------------------------------------------------

def test_a_source_module_named_test_something_is_not_treated_as_a_test(tests_dir):
    """Caught for real: `terminal_mcp/test_selection.py` matched the
    "changed test runs itself" branch on its basename, so the fast lane ran a
    source file and skipped the tests that actually cover it."""
    selection = sel.select(["terminal_mcp/test_selection.py"], tests_dir=tests_dir)

    assert "terminal_mcp/test_selection.py" not in selection.tests


def test_a_changed_test_file_runs_itself(tests_dir):
    selection = sel.select(["tests/test_alpha.py"], tests_dir=tests_dir)

    assert selection.lane == sel.FAST_LANE
    assert selection.tests == ["tests/test_alpha.py"]
    assert selection.reasons["tests/test_alpha.py"] == ["the changed test itself"]


# -- the two-stage plan ------------------------------------------------------------

def test_full_verify_is_always_the_second_stage_even_after_a_fast_lane(tests_dir):
    """The narrowing is for the author's loop; it never becomes the only
    evidence that the change is done."""
    plan = sel.plan(["terminal_mcp/alpha.py"], tests_dir=tests_dir)

    assert plan["selection"]["lane"] == sel.FAST_LANE
    stages = plan["stages"]
    assert [s["stage"] for s in stages] == ["fast", "full"]
    assert stages[-1]["command"] == ["pytest", "-q"]
    assert stages[-1]["lane"] == sel.FULL_VERIFY


def test_the_fast_command_runs_only_the_selected_tests(tests_dir):
    selection = sel.select(["terminal_mcp/beta.py"], tests_dir=tests_dir)
    assert selection.command() == ["pytest", "-q", "tests/test_beta.py", "tests/test_both.py"]


# -- against the real suite ---------------------------------------------------------

def test_the_real_suite_narrows_a_change_to_this_very_module():
    """An end-to-end check against the tests actually on disk: changing
    `test_selection.py` must select this file, and must not select everything.
    """
    selection = sel.select(["terminal_mcp/test_selection.py"], tests_dir="tests")

    assert selection.lane == sel.FAST_LANE, selection.full_verify_because
    assert "tests/test_test_selection.py" in selection.tests
    assert len(selection.tests) < 20, "a narrow change must not select the whole suite"
