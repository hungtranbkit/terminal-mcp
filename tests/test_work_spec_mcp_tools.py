"""The spec surface has to be REACHABLE, not merely present.

This file exists because of what an audit found: `bug_spec.py` (829 lines) and
`context_pack.py` (303 lines) had no production caller at all -- importable
only from their own tests, with zero references in `mcp_app.py`,
`planner_service.py`, `work_loop.py` or `coordinator.py`. Machinery nobody can
call saves nobody any tokens.

So these exercise the real MCP call path end to end, same pattern as
test_pm_mcp_tools.py, and assert the loop a planner actually walks:
create -> gate refuses with questions -> fill -> gate releases the handoff.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.mcp_app import build_mcp


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_WORK_SPEC_DB", str(tmp_path / "specs.db"))
    return build_mcp()


# -- the tools exist on the surface ------------------------------------------------

@pytest.mark.anyio
async def test_the_spec_tools_are_registered(server):
    """The audit's finding, turned into a test: a module with no caller is a
    module that saves nobody anything."""
    names = {tool.name for tool in await server.list_tools()}
    assert {"work_spec_create", "work_spec_update", "work_spec_gate",
            "work_spec_get", "work_spec_list", "work_test_selection"} <= names


# -- the planner loop --------------------------------------------------------------

@pytest.mark.anyio
async def test_a_feature_spec_is_refused_then_released_once_it_is_complete(server):
    created = await _call(server, "work_spec_create",
                          title="CSV export for the reports page",
                          requirement="An operator can download the report as CSV")
    spec_id = created["spec"]["spec_id"]

    assert created["spec"]["task_type"] == "FEATURE_NEW"
    assert created["gate"]["status"] == "NEEDS_REDEFINE"
    assert created["gate"]["handoff"] is None, \
        "an incomplete spec must hand nothing to a worker"
    assert created["gate"]["reply_to_planner"]["QUESTIONS_FOR_PLANNER"]

    filled = await _call(server, "work_spec_update", spec_id=spec_id, fields={
        "problem": "Finance exports by hand and the numbers drift",
        "user_value": "Finance stops retyping numbers into a spreadsheet",
        "expected_outcome": "An Export button downloads a CSV of the current report",
        "scope": ["the export button", "a CSV serialiser"],
        "out_of_scope": ["XLSX", "scheduled exports"],
        "arch_impact": "one route on the existing dashboard app",
        "reuse_candidates": ["redaction.redact_output", "report_service.rows()"],
        "likely_files": ["terminal_mcp/dashboard.py"],
        "api_contract": "GET /dashboard/api/reports/export.csv -> text/csv",
        "existing_patterns": ["dashboard routes register via register_dashboard()"],
        "implementation_plan": ["add the serialiser", "add the route", "wire the button"],
        "test_plan": ["unit: the serialiser quotes embedded commas"],
        "test_runbook": "test_gate",
        "acceptance_criteria": ["clicking Export downloads a CSV matching the table"],
        "risks": ["a large report could block the event loop"],
    })

    assert filled["gate"]["status"] == "SPEC_READY", filled["gate"]["missing"]
    handoff = filled["gate"]["handoff"]
    assert handoff["REUSE_FIRST"], "the worker is told what to reuse before building"
    assert handoff["OUT_OF_SCOPE"], "the boundary survives into the handoff"


@pytest.mark.anyio
async def test_the_gate_names_blocking_fields_apart_from_the_score(server):
    """A planner needs to know which gaps are unbuyable."""
    created = await _call(server, "work_spec_create", title="Ship 1.4.2 to staging",
                          task_type="DEPLOY", requirement="deploy build 1.4.2")
    gate = await _call(server, "work_spec_gate", spec_id=created["spec"]["spec_id"])

    assert gate["status"] == "NEEDS_REDEFINE"
    assert "rollback_plan" in gate["mandatory_missing"]
    assert "rollback_plan" in gate["reply_to_planner"]["BLOCKING"]


@pytest.mark.anyio
async def test_an_unknown_task_type_is_refused_rather_than_defaulted(server):
    result = await _call(server, "work_spec_create", title="x", task_type="NONSENSE")
    assert result["error"] == "SPEC_CREATE_FAILED"


@pytest.mark.anyio
async def test_a_missing_spec_is_an_error_not_an_empty_success(server):
    assert (await _call(server, "work_spec_gate", spec_id="nope"))["error"] == "SPEC_NOT_FOUND"
    assert (await _call(server, "work_spec_get", spec_id="nope"))["error"] == "SPEC_NOT_FOUND"


@pytest.mark.anyio
async def test_an_unknown_field_is_rejected_rather_than_silently_dropped(server):
    created = await _call(server, "work_spec_create", title="t", requirement="r")
    result = await _call(server, "work_spec_update",
                         spec_id=created["spec"]["spec_id"],
                         fields={"not_a_field": "x"})
    assert result["error"] == "UNKNOWN_FIELD"


@pytest.mark.anyio
async def test_specs_are_listed_with_their_readiness(server):
    await _call(server, "work_spec_create", title="one", requirement="r")
    await _call(server, "work_spec_create", title="two", task_type="BUG",
                symptom="button does nothing")

    listed = await _call(server, "work_spec_list")
    assert len(listed["specs"]) == 2
    assert all(row["ready"] is False for row in listed["specs"])

    bugs = await _call(server, "work_spec_list", task_type="BUG")
    assert [row["title"] for row in bugs["specs"]] == ["two"]


# -- test selection over the real suite ----------------------------------------------

@pytest.mark.anyio
async def test_test_selection_narrows_a_known_module(server):
    result = await _call(server, "work_test_selection",
                         changed_paths="terminal_mcp/work_spec.py")
    selection = result["selection"]

    assert selection["lane"] == "FAST_LANE", selection["full_verify_because"]
    assert "tests/test_work_spec.py" in selection["tests"]
    assert result["stages"][-1]["command"] == ["pytest", "-q"], \
        "FULL_VERIFY still runs before the change is called done"


@pytest.mark.anyio
async def test_test_selection_fails_closed_on_an_uncovered_path(server):
    result = await _call(server, "work_test_selection",
                         changed_paths="terminal_mcp/work_spec.py,scripts/whatever.sh")
    assert result["selection"]["lane"] == "FULL_VERIFY"
    assert result["selection"]["full_verify_because"]


# -- the pipeline over MCP ------------------------------------------------------------

@pytest.mark.anyio
async def test_work_plan_runs_every_stage_and_saves_the_spec(server):
    result = await _call(server, "work_plan",
                         request="Add CSV export to the reports page")

    assert [s["stage"] for s in result["stages"]] == [
        "capture", "classify", "knowledge", "similar", "delta", "reuse", "spec", "gate"]
    assert result["status"] == "NEEDS_REDEFINE"
    assert result["spec"]["spec_id"]

    stored = await _call(server, "work_spec_get", spec_id=result["spec"]["spec_id"])
    assert stored["spec"]["redefine_reason"], "the refusal reason has to survive"


@pytest.mark.anyio
async def test_work_plan_redefine_resumes_the_same_spec(server):
    planned = await _call(server, "work_plan", request="Add CSV export to reports")
    spec_id = planned["spec"]["spec_id"]

    resumed = await _call(server, "work_plan_redefine", spec_id=spec_id, fields={
        "problem": "Finance exports by hand",
        "user_value": "Finance stops retyping numbers",
        "expected_outcome": "an Export button downloads a CSV",
        "scope": ["the button", "a serialiser"],
        "out_of_scope": ["XLSX"],
        "arch_impact": "one route",
        "reuse_candidates": ["redaction.redact_output"],
        "existing_patterns": ["register_dashboard()"],
        "implementation_plan": ["serialiser", "route", "button"],
        "likely_files": ["terminal_mcp/dashboard.py"],
        "api_contract": "GET /export.csv -> text/csv",
        "test_plan": ["unit: quoting"],
        "test_runbook": "test_gate",
        "acceptance_criteria": ["clicking Export downloads a CSV"],
        "risks": ["large reports"],
    })

    assert resumed["spec"]["spec_id"] == spec_id
    assert resumed["status"] == "SPEC_READY", resumed["gate"]["missing"]
    listed = await _call(server, "work_spec_list")
    assert len(listed["specs"]) == 1, "a resume must not leave a second spec behind"


@pytest.mark.anyio
async def test_work_plan_redefine_reports_a_missing_spec(server):
    result = await _call(server, "work_plan_redefine", spec_id="nope", fields={})
    assert result["error"] == "SPEC_NOT_FOUND"
