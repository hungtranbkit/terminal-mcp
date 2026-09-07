"""Definition of Ready -- dor_gate.py (docs/REQUIREMENTS.md §20.6 Phase
A). Pure unit tests, no I/O."""
from __future__ import annotations

from terminal_mcp.dor_gate import NEEDS_CLARIFICATION, READY, check_definition_of_ready


def _task(title="A real title", **metadata):
    return {"title": title, "metadata": metadata}


def test_dor_not_required_is_always_ready():
    result = check_definition_of_ready(_task())  # no dor_required flag at all
    assert result["status"] == READY


def test_dor_required_with_all_fields_present_is_ready():
    result = check_definition_of_ready(_task(
        dor_required=True, acceptance_criteria="it works", project="OfflinePOS", risk_level="LOW",
    ))
    assert result["status"] == READY


def test_dor_required_missing_acceptance_criteria():
    result = check_definition_of_ready(_task(dor_required=True, project="P", risk_level="LOW"))
    assert result["status"] == NEEDS_CLARIFICATION
    assert "acceptance_criteria" in result["missing_fields"]


def test_dor_required_missing_project():
    result = check_definition_of_ready(_task(dor_required=True, acceptance_criteria="x", risk_level="LOW"))
    assert result["status"] == NEEDS_CLARIFICATION
    assert "project" in result["missing_fields"]


def test_dor_required_missing_risk_level():
    result = check_definition_of_ready(_task(dor_required=True, acceptance_criteria="x", project="P"))
    assert result["status"] == NEEDS_CLARIFICATION
    assert "risk_level" in result["missing_fields"]


def test_dor_required_invalid_risk_level():
    result = check_definition_of_ready(_task(dor_required=True, acceptance_criteria="x", project="P",
                                              risk_level="SUPER_HIGH"))
    assert result["status"] == NEEDS_CLARIFICATION
    assert any("risk_level" in field for field in result["missing_fields"])


def test_dor_required_missing_title():
    result = check_definition_of_ready(_task(title="", dor_required=True, acceptance_criteria="x", project="P",
                                              risk_level="LOW"))
    assert result["status"] == NEEDS_CLARIFICATION
    assert "title" in result["missing_fields"]


def test_dor_required_reports_every_missing_field_at_once():
    result = check_definition_of_ready(_task(title="", dor_required=True))
    assert result["status"] == NEEDS_CLARIFICATION
    assert set(result["missing_fields"]) == {"title", "acceptance_criteria", "project", "risk_level"}


def test_dor_never_requires_required_os_or_capabilities_or_dependencies():
    # Deliberate scope cut, disclosed in dor_gate.py's own docstring --
    # a task with no OS/capability/dependency requirement declared is
    # still READY as long as the core 4 fields are present.
    result = check_definition_of_ready(_task(dor_required=True, acceptance_criteria="x", project="P",
                                              risk_level="LOW"))
    assert result["status"] == READY
