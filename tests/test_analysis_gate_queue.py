"""Analysis Gate x queue/coordinator integration (§20.6 Phase F).

The unit tests in test_analysis_gate.py prove the rule. These prove the
WIRING: that a gated task really is refused at the PRECHECK -> READY
edge, that a complete contract really does let it through, that the
column round-trips, and -- the part that matters most for a queue that
is already full of real work -- that nothing pre-existing changed.

SAFETY: every store is a tmp_path fixture; no real queue.db is touched.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from terminal_mcp.analysis_gate import NEEDS_CLARIFICATION, AnalysisGatePolicy
from terminal_mcp.coordinator import BLOCKED, READY, CoordinatorGate, RepoEvidence, SessionSnapshot
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _collector(cwd):
    return RepoEvidence(branch="main", head="abc123", clean=True, status_lines=())


def _gate(**kwargs):
    return CoordinatorGate(evidence_collector=_collector, **kwargs)


def _session(**overrides):
    base = {"node_id": "local", "cwd": "/tmp/does-not-matter", "current_command": "claude"}
    base.update(overrides)
    return SessionSnapshot(**base)


def _make_task(store, session="lane-a", **overrides):
    task = {"prompt": "please implement the widget exporter carefully"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    store.claim_next_task(session, claimed_by="engine-1")
    return store.get_task(task_id)


def _full_contract(**overrides):
    contract = {
        "profile": "full",
        "problem_statement": "the widget exporter drops rows on retry",
        "user_observable_goal": "a retried export contains every row exactly once",
        "source_of_truth": "exporter.py's own dedupe index",
        "evidence": ["reproduced on a disposable lane", "read exporter.py:100-180"],
        "invariants": ["no export ever loses a row"],
        "assumptions": [],
        "acceptance_tests": ["retry an export twice, row count is stable"],
        "live_verification": "run a real export twice against the disposable lane",
    }
    contract.update(overrides)
    return contract


# -- the transition gate itself -----------------------------------------

def test_gated_task_without_a_contract_is_blocked_at_precheck(store):
    task = _make_task(store, metadata={"task_class": "feature"})
    decision = _gate().review(task, store=store, session=_session())
    assert decision.status == BLOCKED
    assert decision.evidence["analysis_gate"]["status"] == NEEDS_CLARIFICATION
    assert decision.blockers                      # machine-readable missing fields
    assert "analysis gate" in decision.reason     # human-readable


def test_gated_task_with_a_complete_contract_reaches_ready(store):
    task = _make_task(store, metadata={"task_class": "feature"}, analysis=_full_contract())
    decision = _gate().review(task, store=store, session=_session())
    assert decision.status == READY


def test_high_impact_unresolved_assumption_blocks_ready(store):
    contract = _full_contract(assumptions=[{
        "statement": "the exporter's dedupe index is unique", "confidence": "HIGH",
        "impact": "CRITICAL", "status": "OPEN"}])
    task = _make_task(store, metadata={"task_class": "feature"}, analysis=contract)
    decision = _gate().review(task, store=store, session=_session())
    assert decision.status == BLOCKED
    gate_result = decision.evidence["analysis_gate"]
    assert len(gate_result["unresolved_assumptions"]) == 1
    assert gate_result["unresolved_assumptions"][0]["impact"] == "CRITICAL"


def test_resolving_the_assumption_with_evidence_allows_ready(store):
    contract = _full_contract(assumptions=[{
        "statement": "the exporter's dedupe index is unique", "confidence": "HIGH",
        "impact": "CRITICAL", "status": "RESOLVED",
        "resolution": "checked the schema: UNIQUE(export_id, row_id) exists"}])
    task = _make_task(store, metadata={"task_class": "feature"}, analysis=contract)
    assert _gate().review(task, store=store, session=_session()).status == READY


def test_critic_required_category_blocks_until_critic_result_recorded(store):
    contract = _full_contract(categories=["data-model"])
    task = _make_task(store, metadata={"task_class": "feature"}, analysis=contract)
    decision = _gate().review(task, store=store, session=_session())
    assert decision.status == BLOCKED
    assert decision.evidence["analysis_gate"]["critic_required_categories"] == ["data-model"]

    contract["critic_result"] = "reviewed the migration: additive, nullable, no backfill"
    updated = store.set_task_analysis(task.id, contract)
    assert _gate().review(updated, store=store, session=_session()).status == READY


def test_fast_fix_profile_is_enforced_with_its_lighter_contract(store):
    task = _make_task(store, metadata={"task_class": "fast_fix"})
    assert _gate().review(task, store=store, session=_session()).status == BLOCKED

    updated = store.set_task_analysis(task.id, {
        "reproduce": "reproduced 3/3 on a disposable lane",
        "root_cause": "position compared before the lane row existed",
        "expected_behavior": "task lands at the end of the target lane",
        "invariant": "no existing task changes position",
        "regression_test": "test_move_task_to_empty_lane",
        "verify_fix": "re-ran the real move on the disposable lane",
    })
    assert _gate().review(updated, store=store, session=_session()).status == READY


def test_blocked_decision_reason_is_both_machine_and_human_readable(store):
    task = _make_task(store, metadata={"task_class": "feature"})
    decision = _gate().review(task, store=store, session=_session())
    payload = decision.to_dict()
    json.dumps(payload)                                   # must survive persistence
    gate_result = payload["evidence"]["analysis_gate"]
    assert gate_result["gate_version"] >= 1
    assert gate_result["profile"] == "full"
    assert isinstance(gate_result["missing_fields"], list) and gate_result["missing_fields"]
    assert isinstance(payload["reason"], str) and payload["reason"].strip()
    assert payload["required_actions"]


# -- backward compatibility: the hard constraint ------------------------

def test_legacy_task_with_no_metadata_still_reaches_ready(store):
    """The whole existing queue looks like this. It must not change."""
    task = _make_task(store)
    assert _gate().review(task, store=store, session=_session()).status == READY


@pytest.mark.parametrize("task_class", ["chore", "docs", "research", "incident"])
def test_non_implementation_classes_are_not_gated(store, task_class):
    task = _make_task(store, metadata={"task_class": task_class})
    assert _gate().review(task, store=store, session=_session()).status == READY


def test_default_coordinator_construction_does_not_gate_legacy_work(store):
    """CoordinatorGate() with no arguments -- exactly how queue_engine.py
    and mcp_app.py already construct it -- must not start blocking the
    tasks they already have."""
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_collector)
    assert gate.review(task, store=store, session=_session()).status == READY


def test_require_classification_policy_gates_unclassified_tasks(store):
    task = _make_task(store)
    strict = _gate(analysis_policy=AnalysisGatePolicy(require_classification=True))
    assert strict.review(task, store=store, session=_session()).status == BLOCKED
    # ...and the default policy still lets the very same task through.
    assert _gate().review(task, store=store, session=_session()).status == READY


def test_advisory_policy_reports_without_blocking(store):
    task = _make_task(store, metadata={"task_class": "feature"})
    advisory = _gate(analysis_policy=AnalysisGatePolicy(enforcement="advisory"))
    assert advisory.review(task, store=store, session=_session()).status == READY


# -- schema / migration --------------------------------------------------

def test_analysis_column_round_trips(store):
    task = _make_task(store, metadata={"task_class": "feature"}, analysis=_full_contract())
    assert store.get_task(task.id).analysis["problem_statement"]
    assert "analysis" in store.get_task(task.id).to_dict()


def test_set_task_analysis_merges_by_default(store):
    task = _make_task(store)
    store.set_task_analysis(task.id, {"problem_statement": "a"})
    store.set_task_analysis(task.id, {"root_cause": "b"})
    analysis = store.get_task(task.id).analysis
    assert analysis == {"problem_statement": "a", "root_cause": "b"}


def test_set_task_analysis_can_replace_outright(store):
    task = _make_task(store)
    store.set_task_analysis(task.id, {"problem_statement": "a"})
    store.set_task_analysis(task.id, {"root_cause": "b"}, merge=False)
    assert store.get_task(task.id).analysis == {"root_cause": "b"}


def test_set_task_analysis_on_a_missing_task_returns_none(store):
    assert store.set_task_analysis("no-such-task", {"a": 1}) is None


def test_task_with_no_analysis_reads_back_empty_not_null(store):
    task = _make_task(store)
    assert store.get_task(task.id).analysis == {}


def test_migration_is_additive_over_a_pre_analysis_database(tmp_path):
    """The real backward-compatibility proof: build a database at the
    PREVIOUS schema version with a real row in it, then let the current
    code migrate it. The old row must survive, read back cleanly, and
    still be dispatchable."""
    path = tmp_path / "legacy.db"
    legacy = QueueStore(path)
    (task_id,) = legacy.set_tasks("lane-a", [{"prompt": "an old task", "title": "legacy"}])
    del legacy

    # Simulate a database written before migration 9 existed.
    connection = sqlite3.connect(path)
    connection.execute("ALTER TABLE queue_tasks DROP COLUMN analysis")
    connection.execute("PRAGMA user_version = 8")
    connection.commit()
    connection.close()

    migrated = QueueStore(path)
    task = migrated.get_task(task_id)
    assert task is not None
    assert task.prompt == "an old task"
    assert task.analysis == {}
    migrated.claim_next_task("lane-a", claimed_by="engine-1")
    decision = _gate().review(migrated.get_task(task_id), store=migrated, session=_session())
    assert decision.status == READY, "a pre-Analysis-Gate row must still dispatch"


# -- service surface -----------------------------------------------------

@pytest.fixture
def service(store, monkeypatch):
    service = QueueService.__new__(QueueService)
    service.store = store
    return service


def test_service_check_analysis_reports_the_same_verdict(service, store):
    task = _make_task(store, metadata={"task_class": "feature"})
    result = service.check_analysis(task.id)
    assert result["status"] == NEEDS_CLARIFICATION
    assert result["task_id"] == task.id


def test_service_set_analysis_records_and_reports(service, store):
    task = _make_task(store, metadata={"task_class": "feature"})
    result = service.set_analysis(task.id, _full_contract())
    assert result["gate"]["status"] == READY
    assert store.get_task(task.id).analysis["problem_statement"]


def test_service_reports_missing_task(service):
    assert service.check_analysis("nope")["error"] == "TASK_NOT_FOUND"
    assert service.set_analysis("nope", {})["error"] == "TASK_NOT_FOUND"
