"""The Requirement Contract as wired into QueueStore.

`test_requirement_contract.py` proves the rules. This file proves the wiring:
that the gate actually stands between an unfinished task and COMPLETED, that a
legacy task is untouched, and that deploying is not completing.
"""
from __future__ import annotations

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp import requirement_contract as rc


@pytest.fixture()
def store(tmp_path):
    return qs.QueueStore(tmp_path / "queue.db")


def _task(store, prompt="Build the Excel mismatch report"):
    task_id, = store.append_tasks("demo", [{"title": "t", "prompt": prompt}])
    return store.get_task(task_id)


def _to_verifying(store, task_id):
    for target in (qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING, qs.VERIFYING):
        store.transition_task(task_id, target, event_type="TEST")


def _r1_r2_r3(store, task_id):
    return store.set_requirement_contract(task_id, requirements=[
        {"id": "R1", "text": "show the mismatch total"},
        {"id": "R2", "text": "list each mismatching row"},
        {"id": "R3", "text": "every mismatch shows Sheet + row/cell + formula"},
    ], actor="user")


def _cover(store, task_id, ids, version):
    store.set_evidence_matrix(task_id, rc.EvidenceMatrix(
        entries=tuple(rc.EvidenceEntry(i, rc.COVERED, evidence=("commit abc123",))
                      for i in ids),
        reconciled_contract_version=version))


# -- backward compatibility --------------------------------------------------

def test_a_task_with_no_contract_still_completes(store):
    """Every task in every existing database has no contract. None may break."""
    task = _task(store)
    _to_verifying(store, task.id)
    done = store.mark_completed_with_evidence(task.id, evidence={"completion_marker": "X"})
    assert done.status == qs.COMPLETED


def test_git_required_task_cannot_bypass_evidence_gate_with_marker_only(store):
    task_id = store.append_tasks("demo", [{
        "prompt": "implement and commit the change",
        "metadata": {"requires_git_evidence": True},
    }])[0]
    store.claim_next_task("demo", claimed_by="test")
    store.record_coordinator_decision(
        task_id, status="READY", reason="ready",
        evidence={"cwd": "/repo", "node_id": "local", "branch": "main", "head": "base"})
    store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHED")
    store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    store.transition_task(task_id, qs.VERIFYING, event_type="VERIFYING")

    with pytest.raises(qs.CompletionEvidenceError, match="GIT_EVIDENCE_REQUIRED"):
        store.mark_completed_with_evidence(task_id, evidence={"completion_marker": "candidate"})

    assert store.get_task(task_id).status == qs.VERIFYING


def test_git_evidence_from_a_different_node_is_not_the_same_repository(store):
    task_id = store.append_tasks("demo", [{
        "prompt": "implement and commit", "metadata": {"requires_git_evidence": True},
    }])[0]
    store.claim_next_task("demo", claimed_by="test")
    store.record_coordinator_decision(
        task_id, status="READY", reason="ready",
        evidence={"cwd": "/repo", "node_id": "node-a", "branch": "main", "head": "base"})
    store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHED")
    store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    store.transition_task(task_id, qs.VERIFYING, event_type="VERIFYING")

    with pytest.raises(qs.CompletionEvidenceError, match="GIT_EVIDENCE_WRONG_REPO"):
        store.mark_completed_with_evidence(task_id, evidence={
            "completion_marker": "candidate",
            "git_evidence": {"cwd": "/repo", "node_id": "node-b", "branch": "work",
                             "head": "changed", "status_lines": []},
        })


def test_proven_false_git_completion_can_be_invalidated_without_redispatch(store):
    task_id = store.append_tasks("demo", [{
        "prompt": "implement and commit the change",
        "metadata": {"requires_git_evidence": True},
    }])[0]
    store.claim_next_task("demo", claimed_by="test")
    store.record_coordinator_decision(
        task_id, status="READY", reason="ready",
        evidence={"cwd": "/repo", "node_id": "local", "branch": "main",
                  "head": "base", "status_lines": []})
    store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHED")
    store.mark_running_with_evidence(task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
    store.transition_task(task_id, qs.VERIFYING, event_type="VERIFYING")
    store.transition_task(task_id, qs.COMPLETED, event_type="LEGACY_FALSE_VERIFIED",
                          extra_fields={"verification_evidence": '{"completion_marker":"candidate"}'})

    invalidated = store.invalidate_false_completion(
        task_id, reason="post-deploy audit found no implementation delta",
        git_evidence={"cwd": "/repo", "node_id": "local", "branch": "main",
                      "head": "base", "status_lines": []})

    assert invalidated.status == qs.CANCELLED
    assert invalidated.claim_token is None
    assert invalidated.lease_expires_at is None
    assert invalidated.completed_at is None
    assert "GIT_EVIDENCE_UNCHANGED" in invalidated.last_error
    event = store.list_events("demo", limit=1)[0]
    assert event["event_type"] == "COMPLETION_INVALIDATED"
    assert event["from_status"] == qs.COMPLETED
    assert event["to_status"] == qs.CANCELLED


def test_legacy_columns_default_to_empty(store):
    task = _task(store)
    assert task.requirement_contract == {}
    assert task.evidence_matrix == {}
    assert task.deploy_state is None


# -- the incident, through the real store -----------------------------------

def test_completion_is_refused_when_a_required_criterion_is_missing(store):
    task = _task(store)
    _r1_r2_r3(store, task.id)
    _cover(store, task.id, ("R1", "R2"), version=1)
    _to_verifying(store, task.id)

    with pytest.raises(qs.RequirementsNotCoveredError) as excinfo:
        store.mark_completed_with_evidence(task.id, evidence={"completion_marker": "X"})

    assert excinfo.value.decision.missing_requirements == ("R3",)
    assert store.get_task(task.id).status == qs.VERIFYING, \
        "a refused completion must leave the task where it was"


def test_the_refusal_is_recorded_with_the_missing_id(store):
    task = _task(store)
    _r1_r2_r3(store, task.id)
    _cover(store, task.id, ("R1", "R2"), version=1)
    _to_verifying(store, task.id)
    with pytest.raises(qs.RequirementsNotCoveredError):
        store.mark_completed_with_evidence(task.id, evidence={"m": "x"})

    events = store.list_events(session="demo")
    refusals = [e for e in events if e["event_type"] == "COMPLETION_REFUSED_REQUIREMENTS"]
    assert refusals and "R3" in (refusals[0]["reason"] or "")


def test_completion_succeeds_once_every_criterion_is_covered(store):
    task = _task(store)
    _r1_r2_r3(store, task.id)
    _cover(store, task.id, ("R1", "R2", "R3"), version=1)
    _to_verifying(store, task.id)
    done = store.mark_completed_with_evidence(task.id, evidence={"commit": "abc123"})
    assert done.status == qs.COMPLETED


# -- amendment ---------------------------------------------------------------

def test_amendment_appends_and_blocks_completion_until_reconciled(store):
    task = _task(store)
    _r1_r2_r3(store, task.id)
    _cover(store, task.id, ("R1", "R2", "R3"), version=1)

    amended = store.amend_requirement_contract(
        task.id, prompt="also export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        actor="user")
    assert amended.contract_version == 2

    _to_verifying(store, task.id)
    with pytest.raises(qs.RequirementsNotCoveredError) as excinfo:
        store.mark_completed_with_evidence(task.id, evidence={"m": "x"})

    decision = excinfo.value.decision
    # See test_requirement_contract.test_amendment_added_after_the_task_ran:
    # an un-claimed new requirement is named by the ordinary coverage
    # comparison, which is the accurate reason for it.
    assert decision.reason == rc.REQUIREMENTS_NOT_COVERED
    assert "R4" in decision.missing_requirements


def test_a_contract_is_never_overwritten(store):
    task = _task(store)
    _r1_r2_r3(store, task.id)
    with pytest.raises(rc.ContractError):
        store.set_requirement_contract(task.id, requirements=[{"id": "R9", "text": "x"}])


def test_amending_a_task_that_had_no_contract_preserves_its_prompt(store):
    task = _task(store, prompt="the original wording")
    amended = store.amend_requirement_contract(
        task.id, prompt="and also this", requirements=[{"id": "R1", "text": "a"}])
    assert amended.contract_version == 2
    assert amended.original_prompt() == "the original wording"


def test_contract_survives_a_reload_from_disk(tmp_path):
    store = qs.QueueStore(tmp_path / "queue.db")
    task = _task(store, prompt="p")
    _r1_r2_r3(store, task.id)
    reopened = qs.QueueStore(tmp_path / "queue.db")
    contract = reopened.get_requirement_contract(task.id)
    assert {r.id for r in contract.requirements()} == {"R1", "R2", "R3"}


# -- deployment is not completion (RC6) -------------------------------------

def test_recording_a_deploy_does_not_change_status(store):
    task = _task(store)
    _to_verifying(store, task.id)
    updated = store.record_deploy(task.id, deploy_state=qs.QueueStore.DEPLOYED_TEST,
                                  reference="build-42", actor="ci")
    assert updated.deploy_state == qs.QueueStore.DEPLOYED_TEST
    assert updated.status == qs.VERIFYING, "deploying is not completing"


def test_a_deployed_task_with_a_missing_criterion_still_cannot_complete(store):
    """The exact conflation RC6 describes: deployed to TEST, reported done."""
    task = _task(store)
    _r1_r2_r3(store, task.id)
    _cover(store, task.id, ("R1", "R2"), version=1)
    _to_verifying(store, task.id)
    store.record_deploy(task.id, deploy_state=qs.QueueStore.DEPLOYED_TEST)

    with pytest.raises(qs.RequirementsNotCoveredError):
        store.mark_completed_with_evidence(task.id, evidence={"deployed": "test"})
    assert store.get_task(task.id).deploy_state == qs.QueueStore.DEPLOYED_TEST


def test_deploy_state_is_not_a_task_status(store):
    assert qs.QueueStore.DEPLOYED_TEST not in qs.ALL_STATUSES
    assert qs.QueueStore.DEPLOYED_PROD not in qs.ALL_STATUSES


def test_unknown_deploy_state_is_refused(store):
    task = _task(store)
    with pytest.raises(ValueError):
        store.record_deploy(task.id, deploy_state="SHIPPED_IT")


# -- the reader-facing decision, without attempting completion --------------

def test_completion_decision_is_readable_without_completing(store):
    task = _task(store)
    _r1_r2_r3(store, task.id)
    _cover(store, task.id, ("R1", "R2"), version=1)

    payload = store.completion_decision(task.id).to_dict()
    assert payload["missing_requirements"] == ["R3"]
    assert [row["requirement_id"] for row in payload["checklist"]] == ["R1", "R2", "R3"]
    assert store.get_task(task.id).status == qs.QUEUED, "reading must not transition"


def test_self_reported_marker_alone_does_not_satisfy_the_gate(store):
    """RC2, end to end: the evidence shape that let the real failure through."""
    task = _task(store)
    _r1_r2_r3(store, task.id)
    store.set_evidence_matrix(task.id, rc.EvidenceMatrix(
        entries=tuple(rc.EvidenceEntry(i, rc.COVERED, evidence=("completion_marker:DONE",))
                      for i in ("R1", "R2", "R3")),
        reconciled_contract_version=1))
    _to_verifying(store, task.id)

    with pytest.raises(qs.RequirementsNotCoveredError) as excinfo:
        store.mark_completed_with_evidence(task.id, evidence={"completion_marker": "DONE"})
    assert excinfo.value.decision.reason == rc.DETECTOR_FINDING
