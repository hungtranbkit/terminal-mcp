"""Analysis Gate MCP tool surface (§20.6 Phase F) -- exercises the real
MCP call path, same pattern as test_dor_gate_mcp_tools.py.

Also pins the agent bootstrap (part B): the MCP server's `instructions`
field is the one agent-instruction channel this project has, and it must
keep pointing at docs/AI_ANALYSIS_GATE.md without growing into a copy of
it.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.mcp_app import ANALYSIS_GATE_BOOTSTRAP, build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def server(tmp_path):
    return build_mcp(queue=QueueService(QueueStore(tmp_path / "queue.db")))


def _full_contract(**overrides):
    contract = {
        "profile": "full",
        "problem_statement": "exports drop rows on retry",
        "user_observable_goal": "a retried export contains every row exactly once",
        "source_of_truth": "exporter.py's dedupe index",
        "evidence": ["reproduced on a disposable lane"],
        "invariants": ["no export ever loses a row"],
        "assumptions": [],
        "acceptance_tests": ["retry twice, row count stable"],
        "live_verification": "run a real export twice",
    }
    contract.update(overrides)
    return contract


@pytest.mark.anyio
async def test_check_analysis_is_ready_for_an_unclassified_task(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p")
    result = await _call(server, "terminal_task_check_analysis", task_id=created["task_id"])
    assert result["status"] == "READY"
    assert result["profile"] == "none"


@pytest.mark.anyio
async def test_check_analysis_reports_missing_fields_for_a_feature_task(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p",
                          metadata={"task_class": "feature"})
    result = await _call(server, "terminal_task_check_analysis", task_id=created["task_id"])
    assert result["status"] == "NEEDS_CLARIFICATION"
    assert "problem_statement" in result["missing_fields"]
    assert result["reason"]


@pytest.mark.anyio
async def test_set_analysis_records_the_contract_and_reports_ready(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p",
                          metadata={"task_class": "feature"})
    result = await _call(server, "terminal_task_set_analysis",
                         task_id=created["task_id"], analysis=_full_contract())
    assert result["gate"]["status"] == "READY"
    assert result["analysis"]["problem_statement"]

    again = await _call(server, "terminal_task_check_analysis", task_id=created["task_id"])
    assert again["status"] == "READY"


@pytest.mark.anyio
async def test_set_analysis_merges_so_one_question_can_be_answered_at_a_time(server):
    created = await _call(server, "terminal_task_create", title="t", prompt="p",
                          metadata={"task_class": "fast_fix"})
    partial = {"reproduce": "3/3", "root_cause": "off-by-one", "expected_behavior": "no dupes"}
    first = await _call(server, "terminal_task_set_analysis",
                        task_id=created["task_id"], analysis=partial)
    assert first["gate"]["status"] == "NEEDS_CLARIFICATION"

    second = await _call(server, "terminal_task_set_analysis", task_id=created["task_id"],
                         analysis={"invariant": "no row moves", "regression_test": "test_x",
                                   "verify_fix": "re-ran on the disposable lane"})
    assert second["gate"]["status"] == "READY"
    assert second["analysis"]["reproduce"] == "3/3", "merge must not drop earlier answers"


@pytest.mark.anyio
async def test_tools_report_a_missing_task(server):
    assert (await _call(server, "terminal_task_check_analysis", task_id="nope"))["error"] == "TASK_NOT_FOUND"
    assert (await _call(server, "terminal_task_set_analysis",
                        task_id="nope", analysis={}))["error"] == "TASK_NOT_FOUND"


# -- part B: the agent bootstrap ----------------------------------------

def test_bootstrap_points_at_the_doc_without_duplicating_it():
    assert "docs/AI_ANALYSIS_GATE.md" in ANALYSIS_GATE_BOOTSTRAP
    # Short on purpose: a bootstrap that grows into a copy of the doc is
    # how the two drift apart.
    assert len(ANALYSIS_GATE_BOOTSTRAP) < 1200, "bootstrap is turning into a copy of the doc"


def test_bootstrap_states_the_invariants_an_agent_must_not_violate():
    text = ANALYSIS_GATE_BOOTSTRAP.lower()
    assert "mcp" in text and "before asking" in text        # evidence first
    assert "unknown" in text                                 # UNKNOWN > guess
    assert "high/critical" in text or "high-impact" in text  # blocking assumptions
    assert "needs_clarification" in text                     # the escape hatch that isn't a guess


def test_bootstrap_keeps_the_pre_existing_session_policy_line():
    """The instructions field already carried a real access-policy
    sentence -- adding to it must never drop it."""
    assert "Only access explicitly allowed tmux sessions" in ANALYSIS_GATE_BOOTSTRAP
    assert "Input is disabled by default" in ANALYSIS_GATE_BOOTSTRAP


def test_server_actually_publishes_the_bootstrap(tmp_path):
    server = build_mcp(queue=QueueService(QueueStore(tmp_path / "queue.db")))
    assert server.instructions == ANALYSIS_GATE_BOOTSTRAP
