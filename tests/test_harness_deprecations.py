"""The superseded-path inventory, checked against the code it describes.

An inventory nobody verifies is fiction within two refactors, and the whole
value of this one is that it answers "what does Harness make redundant, and
can it go yet" without reading six modules.
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_deprecations as dep


def test_every_superseded_path_still_exists():
    """A missing symbol means somebody already removed the old path -- good
    news, but the inventory has to be told."""
    verified = dep.verify()
    assert verified["ok"], f"no longer present: {verified['missing']}"
    assert verified["checked"] == len(dep.SUPERSEDED)


def test_nothing_is_disabled_on_this_branch():
    """Harness runs in SHADOW/SUPERVISED and the old paths stay authoritative.
    A feature that disables its predecessor before proving itself has no way
    back, and the way back is the entire reason for running in shadow."""
    assert {entry.status for entry in dep.SUPERSEDED} == {dep.PARALLEL}


def test_every_entry_names_a_replacement_and_a_retirement_gate():
    for entry in dep.SUPERSEDED:
        assert entry.decides.strip(), entry.dotted
        assert entry.superseded_by.strip(), entry.dotted
        assert entry.retire_gate.strip(), f"{entry.dotted} has no gate to retire behind"
        assert entry.status in dep.STATUSES


def test_the_blocked_sweep_is_named_because_harness_removes_its_reason():
    """That sweep exists only to undo a decision the old pipeline should not
    have taken. If it is not listed here, the argument has a hole in it."""
    listed = {entry.symbol for entry in dep.SUPERSEDED}
    assert "QueueStore.reevaluate_ai_owned_blocked" in listed


def test_the_report_groups_by_status():
    report = dep.report()
    assert set(report["by_status"]) == set(dep.STATUSES)
    assert report["by_status"][dep.PARALLEL]
    assert report["by_status"][dep.FROZEN] == []
    assert "authoritative" in report["note"]
