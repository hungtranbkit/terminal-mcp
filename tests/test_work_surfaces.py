"""The knowledge / runbook / policy / telemetry read surfaces.

Two things matter here: they are gated like every other dashboard read, and
they are READS. A panel that could mutate the map or run a production
procedure would turn a status screen into a control surface.
"""

from __future__ import annotations

import inspect

import pytest

from terminal_mcp import dashboard, mcp_app

NEW_ROUTES = ("/dashboard/api/knowledge", "/dashboard/api/procedures",
              "/dashboard/api/policy", "/dashboard/api/telemetry")


@pytest.fixture(scope="module")
def dashboard_source() -> str:
    return inspect.getsource(dashboard)


@pytest.mark.parametrize("route", NEW_ROUTES)
def test_route_is_registered_once(route, dashboard_source):
    assert dashboard_source.count(f'"{route}"') == 1


@pytest.mark.parametrize("route", NEW_ROUTES)
def test_route_is_get_only(route, dashboard_source):
    declaration = dashboard_source.split(f'"{route}"', 1)[1][:120]
    assert 'methods=["GET"]' in declaration


@pytest.mark.parametrize("route", NEW_ROUTES)
def test_route_is_behind_the_read_guard(route, dashboard_source):
    body = dashboard_source.split(f'"{route}"', 1)[1][:1200]
    assert "_read_guard(request)" in body
    assert "if blocked is not None" in body


@pytest.mark.parametrize("route", NEW_ROUTES)
def test_route_never_5xxs_a_status_screen(route, dashboard_source):
    body = dashboard_source.split(f'"{route}"', 1)[1][:2400]
    # A dashboard panel reports its own failure; it does not take the page down.
    assert "except Exception" in body
    assert "JSONResponse" in body


def test_reads_do_not_mutate_the_knowledge_map(dashboard_source):
    body = dashboard_source.split('"/dashboard/api/knowledge"', 1)[1][:2400]
    for forbidden in ("record_module", "write_document", "mark_indexed"):
        assert forbidden not in body


def test_the_procedures_panel_lists_but_never_runs(dashboard_source):
    body = dashboard_source.split('"/dashboard/api/procedures"', 1)[1][:2400]
    assert "registry.list()" in body
    assert "registry.run(" not in body


# -- the Work page ------------------------------------------------------------

def test_the_work_page_has_the_new_panels():
    for marker in ("loadKnowledge", "loadProcedures", "loadPolicy", "loadTelemetry",
                   'id="knowledge"', 'id="procedures"', 'id="telemetry"',
                   'id="policyBadge"'):
        assert marker in dashboard.WORK_HTML


@pytest.mark.parametrize("template", ["TERMINAL_WALL_HTML", "FLEET_HTML", "AUDIT_HTML"])
def test_the_new_panels_did_not_leak_into_other_templates(template):
    # Learned the hard way: these templates share handler names, so an edit
    # anchored on a non-unique string lands in the wrong page.
    html = getattr(dashboard, template)
    for marker in ("loadKnowledge", "loadTelemetry", "policyBadge"):
        assert marker not in html


def test_the_panels_build_dom_rather_than_interpolating_markup():
    block = dashboard.WORK_HTML.split("// -- knowledge, runbooks", 1)[1]
    block = block.split("$('#liveBtn')", 1)[0]
    # Module summaries, runbook commands and git reasons are text read off
    # disk; they are set with textContent, never spliced into HTML.
    assert "innerHTML" not in block
    assert "replaceChildren" in block


def test_the_context_panels_are_not_on_the_six_second_poll():
    # They move at the speed of commits. Re-fetching them every six seconds
    # would spend real work to re-render an unchanged panel.
    tail = dashboard.WORK_HTML.split("setInterval(", 1)[1][:200]
    assert "loadContext" not in tail
    assert "load()" in tail


def test_token_provenance_is_rendered_not_dropped():
    block = dashboard.WORK_HTML.split("function tokenText", 1)[1][:800]
    for marker in ("ESTIMATED", "PARTIAL", "không có số liệu"):
        assert marker in block


def test_missing_data_is_not_shown_as_zero():
    block = dashboard.WORK_HTML.split("async function loadTelemetry", 1)[1][:1800]
    # "no data" and "a rate of zero" are different facts.
    assert "chưa đủ dữ liệu" in block
    assert "chưa có preview nào" in block


# -- MCP tools ----------------------------------------------------------------

@pytest.mark.parametrize("tool", ["work_knowledge", "work_knowledge_record",
                                  "work_procedures", "work_policy", "work_telemetry"])
def test_the_mcp_tool_exists(tool):
    source = inspect.getsource(mcp_app)
    assert f"def {tool}(" in source


def test_risky_procedures_are_not_auto_invokable_from_mcp():
    source = inspect.getsource(mcp_app)
    body = source.split("def work_procedures(", 1)[1][:2000]
    # The default must be False: a production runbook is a human decision.
    assert "allow_risky: bool = False" in source.split("def work_procedures(", 1)[1][:200]
    assert "allow_risky=allow_risky" in body


def test_the_policy_tool_supports_subset_loading():
    body = inspect.getsource(mcp_app).split("def work_policy(", 1)[1][:2400]
    assert "sections" in body
    assert "policy_for_task" in body


def test_the_worker_telemetry_tool_exists_and_defaults_to_unreported():
    source = inspect.getsource(mcp_app)
    assert "def work_telemetry_report(" in source
    signature = source.split("def work_telemetry_report(", 1)[1].split(") -> dict", 1)[0]
    # -1 means "not available" and is recorded as exactly that. A default of 0
    # would record every unreported task as having cost nothing.
    assert "tokens_used: int = -1" in signature
    body = source.split("def work_telemetry_report(", 1)[1][:3000]
    assert "int(tokens_used) >= 0" in body


def test_an_unrecognised_token_source_is_treated_as_an_estimate():
    body = inspect.getsource(mcp_app).split("def work_telemetry_report(", 1)[1][:4200]
    # A count of unknown origin is at best an estimate; calling it EXACT
    # would launder it into a measurement.
    assert 'tokens_source.upper() == EXACT' in body
    assert 'source="ESTIMATED"' in body


def test_re_reporting_a_task_updates_its_row(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_TELEMETRY_DB", str(tmp_path / "t.db"))
    from terminal_mcp.work_telemetry import TaskTelemetry, TelemetryStore

    store = TelemetryStore()
    first = TaskTelemetry(task_id="tsk_1", work_id="w1", files_read=3)
    store.save(first)
    again = TaskTelemetry(task_id="tsk_1", work_id="w1", files_read=7,
                          started_at=first.started_at)
    again.telemetry_id = first.telemetry_id
    store.save(again)
    rows = store.for_work("w1")
    assert len(rows) == 1 and rows[0]["files_read"] == 7
    store.close()
