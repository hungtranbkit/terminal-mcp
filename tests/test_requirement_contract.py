"""The Requirement Contract, its amendments, and the completion gate.

`test_the_reported_mesflow_failure` and `test_amendment_added_after_the_task_ran`
are the regression tests for the actual incident (task item 11). Everything else
exists to stop those two from passing for the wrong reason.
"""
from __future__ import annotations

import pytest

from terminal_mcp import requirement_contract as rc

NOW = "2026-09-14T00:00:00+00:00"
LATER = "2026-09-14T01:00:00+00:00"


def _contract_r1_r2_r3():
    return rc.create_contract(
        prompt="Build the Excel mismatch report",
        requirements=[
            {"id": "R1", "text": "show the mismatch total"},
            {"id": "R2", "text": "list each mismatching row"},
            {"id": "R3", "text": "every mismatch shows Sheet + row/cell + formula"},
        ],
        created_at=NOW, actor="user")


def _matrix(pairs, version, **kw):
    return rc.EvidenceMatrix(
        entries=tuple(rc.EvidenceEntry(requirement_id=rid, status=status,
                                       evidence=("commit abc123",), **kw)
                      for rid, status in pairs),
        reconciled_contract_version=version)


# -- the incident ------------------------------------------------------------

def test_the_reported_mesflow_failure():
    """R1/R2/R3 asked for; agent evidences R1/R2 only. Must NOT be done."""
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED)], version=1)

    decision = rc.reconcile(contract, matrix)

    assert decision.verified_done is False
    assert decision.reason == rc.REQUIREMENTS_NOT_COVERED
    assert decision.missing_requirements == ("R3",)
    assert "R3" in decision.blocking_ids()


def test_amendment_added_after_the_task_ran():
    """The amendment is the thing that got dropped. Reconciling against the
    version the agent happened to see must not be enough."""
    contract = _contract_r1_r2_r3()
    amended = rc.amend_contract(
        contract, prompt="also export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        created_at=LATER, actor="user")

    # The agent covered everything it knew about -- v1's three requirements.
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED)],
                     version=1)
    decision = rc.reconcile(amended, matrix)

    assert decision.verified_done is False
    # The refusal and the named id are the guarantee. The REASON is
    # REQUIREMENTS_NOT_COVERED rather than STALE_CONTRACT_VERSION because the
    # matrix makes no claim about R4 at all -- "you have not covered R4" is
    # the accurate and actionable answer, and it is the same answer whether
    # R4 arrived in v1 or in an amendment. STALE_CONTRACT_VERSION is now
    # reserved for evidence that CLAIMS a requirement predating it (see
    # test_evidence_claiming_a_requirement_added_after_it_is_stale).
    assert decision.reason == rc.REQUIREMENTS_NOT_COVERED
    assert decision.contract_version == 2
    assert decision.reconciled_version == 1
    assert "R4" in decision.missing_requirements
    assert "R4" in decision.blocking_ids()


def test_amendment_reconciled_and_covered_passes():
    contract = rc.amend_contract(
        _contract_r1_r2_r3(), prompt="also export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        created_at=LATER)
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED),
                      ("R3", rc.COVERED), ("R4", rc.COVERED)], version=2)

    decision = rc.reconcile(contract, matrix)
    assert decision.verified_done is True, decision.detail
    assert decision.reason == rc.VERIFIED


# -- amendments never lose anything -----------------------------------------

def test_amendment_appends_and_never_overwrites():
    contract = _contract_r1_r2_r3()
    amended = rc.amend_contract(contract, prompt="more",
                                requirements=[{"id": "R4", "text": "d"}],
                                created_at=LATER)
    assert len(amended.versions) == 2
    assert amended.versions[0] == contract.versions[0], "v1 must be untouched"
    assert {r.id for r in amended.requirements()} == {"R1", "R2", "R3", "R4"}


def test_amendment_may_restate_text_but_keeps_the_id_and_its_history():
    contract = _contract_r1_r2_r3()
    amended = rc.amend_contract(
        contract, prompt="clarify R3",
        requirements=[{"id": "R3", "text": "Sheet + row/cell + formula, per mismatch"}],
        created_at=LATER)
    r3 = {r.id: r for r in amended.requirements()}["R3"]
    assert r3.text.endswith("per mismatch")
    assert r3.added_in_version == 1, "restating must not re-date the requirement"
    assert len(amended.requirements()) == 3, "restating must not duplicate"


def test_original_prompt_is_preserved_verbatim():
    contract = _contract_r1_r2_r3()
    amended = rc.amend_contract(contract, prompt="extra", created_at=LATER)
    assert amended.original_prompt() == "Build the Excel mismatch report"


def test_lineage_is_recorded_per_version():
    contract = rc.amend_contract(_contract_r1_r2_r3(), prompt="extra",
                                 created_at=LATER, actor="operator")
    v1, v2 = sorted(contract.versions, key=lambda v: v.version)
    assert (v1.source, v1.created_at) == (rc.SOURCE_ORIGINAL, NOW)
    assert (v2.source, v2.created_at, v2.actor) == (rc.SOURCE_AMENDMENT, LATER, "operator")


def test_duplicate_ids_in_one_version_are_refused():
    with pytest.raises(rc.ContractError):
        rc.create_contract(prompt="p", created_at=NOW, requirements=[
            {"id": "R1", "text": "a"}, {"id": "R1", "text": "b"}])


def test_requirement_needs_id_and_text():
    with pytest.raises(rc.ContractError):
        rc.create_contract(prompt="p", created_at=NOW, requirements=[{"text": "no id"}])
    with pytest.raises(rc.ContractError):
        rc.create_contract(prompt="p", created_at=NOW, requirements=[{"id": "R1"}])


# -- backward compatibility --------------------------------------------------

def test_a_task_with_no_contract_is_not_blocked():
    """Every task that exists today has no contract. None of them may break."""
    decision = rc.reconcile(None, None)
    assert decision.verified_done is True
    assert decision.reason == rc.NO_CONTRACT


def test_a_contract_with_no_requirements_has_nothing_to_block_on():
    contract = rc.create_contract(prompt="just do the thing", created_at=NOW)
    decision = rc.reconcile(contract, rc.EvidenceMatrix(reconciled_contract_version=1))
    assert decision.verified_done is True


# -- fail-closed -------------------------------------------------------------

def test_a_requirement_with_no_evidence_entry_is_missing_not_assumed_done():
    contract = _contract_r1_r2_r3()
    decision = rc.reconcile(contract, rc.EvidenceMatrix(reconciled_contract_version=1))
    assert decision.verified_done is False
    assert set(decision.missing_requirements) == {"R1", "R2", "R3"}


def test_partial_blocks_just_like_missing():
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.PARTIAL)],
                     version=1)
    decision = rc.reconcile(contract, matrix)
    assert decision.verified_done is False
    assert decision.partial_requirements == ("R3",)


def test_unrecognised_status_fails_closed():
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", "probably_fine")],
                     version=1)
    decision = rc.reconcile(contract, matrix)
    assert decision.verified_done is False
    assert decision.reason == rc.UNKNOWN_STATUS


def test_optional_requirement_does_not_block():
    contract = rc.create_contract(prompt="p", created_at=NOW, requirements=[
        {"id": "R1", "text": "a"}, {"id": "R2", "text": "nice to have", "required": False}])
    matrix = _matrix([("R1", rc.COVERED)], version=1)
    assert rc.reconcile(contract, matrix).verified_done is True


def test_evidence_for_an_unknown_requirement_is_surfaced():
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED),
                      ("R9", rc.COVERED)], version=1)
    decision = rc.reconcile(contract, matrix)
    assert decision.unknown_requirements == ("R9",)


# -- waivers -----------------------------------------------------------------

def test_waiver_needs_an_actor_and_a_reason():
    contract = _contract_r1_r2_r3()
    matrix = rc.EvidenceMatrix(
        entries=(rc.EvidenceEntry("R1", rc.COVERED, evidence=("c",)),
                 rc.EvidenceEntry("R2", rc.COVERED, evidence=("c",)),
                 rc.EvidenceEntry("R3", rc.WAIVED)),
        reconciled_contract_version=1)
    decision = rc.reconcile(contract, matrix)
    assert decision.verified_done is False
    assert decision.reason == rc.INVALID_WAIVER
    assert decision.invalid_waivers == ("R3",)


def test_a_properly_waived_requirement_passes_and_stays_on_the_record():
    contract = _contract_r1_r2_r3()
    matrix = rc.EvidenceMatrix(
        entries=(rc.EvidenceEntry("R1", rc.COVERED, evidence=("c",)),
                 rc.EvidenceEntry("R2", rc.COVERED, evidence=("c",)),
                 rc.EvidenceEntry("R3", rc.WAIVED, waived_by="user",
                                  waived_reason="formula export deferred to v2")),
        reconciled_contract_version=1)
    decision = rc.reconcile(contract, matrix)
    assert decision.verified_done is True, decision.detail
    row = {c["requirement_id"]: c for c in decision.checklist}["R3"]
    assert row["status"] == rc.WAIVED
    assert row["waived_by"] == "user" and row["waived_reason"]


# -- detectors can only add --------------------------------------------------

def test_a_detector_can_block_an_otherwise_passing_gate():
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED)],
                     version=1)

    def always(_c, _m):
        return [("R3", "screenshot does not show a formula")]

    decision = rc.reconcile(contract, matrix, detectors=[always])
    assert decision.verified_done is False
    assert decision.reason == rc.DETECTOR_FINDING


def test_a_silent_detector_cannot_rescue_a_failing_gate():
    """The hard gate must not depend on a detector answering."""
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED)], version=1)
    decision = rc.reconcile(contract, matrix, detectors=[lambda _c, _m: []])
    assert decision.verified_done is False
    assert decision.missing_requirements == ("R3",)


def test_a_crashing_detector_does_not_pass_the_gate():
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED)],
                     version=1)

    def boom(_c, _m):
        raise RuntimeError("llm unavailable")

    decision = rc.reconcile(contract, matrix, detectors=[boom])
    assert decision.verified_done is False
    assert "llm unavailable" in decision.detail


def test_self_reported_completion_marker_is_detected():
    """RC2: the evidence that let the real failure through."""
    contract = _contract_r1_r2_r3()
    matrix = rc.EvidenceMatrix(
        entries=tuple(rc.EvidenceEntry(r, rc.COVERED, evidence=("completion_marker:DONE",))
                      for r in ("R1", "R2", "R3")),
        reconciled_contract_version=1)
    decision = rc.reconcile(
        contract, matrix, detectors=[rc.detector_evidence_must_not_be_self_reported])
    assert decision.verified_done is False
    assert decision.reason == rc.DETECTOR_FINDING


def test_covered_with_no_evidence_reference_is_detected():
    contract = _contract_r1_r2_r3()
    matrix = rc.EvidenceMatrix(
        entries=tuple(rc.EvidenceEntry(r, rc.COVERED) for r in ("R1", "R2", "R3")),
        reconciled_contract_version=1)
    decision = rc.reconcile(
        contract, matrix, detectors=[rc.detector_evidence_must_not_be_self_reported])
    assert decision.verified_done is False


# -- the reader-facing output (task item 12's data shape) --------------------

def test_checklist_has_one_row_per_requirement_with_status_and_evidence():
    contract = _contract_r1_r2_r3()
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED)], version=1)
    payload = rc.reconcile(contract, matrix).to_dict()

    assert [row["requirement_id"] for row in payload["checklist"]] == ["R1", "R2", "R3"]
    assert payload["missing_requirements"] == ["R3"]
    assert payload["blocking_requirements"] == ["R3"]
    r3 = payload["checklist"][2]
    assert (r3["status"], r3["text"]) == (rc.MISSING, contract.requirements()[2].text)


def test_decision_is_json_serialisable():
    import json
    json.dumps(rc.reconcile(_contract_r1_r2_r3(), _matrix([("R1", rc.COVERED)], 1)).to_dict())


# -- round trip --------------------------------------------------------------

def test_contract_round_trips_through_dict():
    contract = rc.amend_contract(_contract_r1_r2_r3(), prompt="extra",
                                 requirements=[{"id": "R4", "text": "d"}],
                                 created_at=LATER)
    restored = rc.RequirementContract.from_dict(contract.to_dict())
    assert restored.contract_version == 2
    assert {r.id for r in restored.requirements()} == {"R1", "R2", "R3", "R4"}


def test_matrix_round_trips_through_dict():
    matrix = _matrix([("R1", rc.COVERED)], version=3)
    assert rc.EvidenceMatrix.from_dict(matrix.to_dict()).reconciled_contract_version == 3


# -- the VERIFYING deadlock (queue task 4b5ecb09..., facebook-property-claude)


def test_a_version_bump_that_adds_nothing_does_not_block_completion():
    """The incident, at its source.

    A finished task was pinned in VERIFYING for 28 minutes because its
    contract had been amended with ZERO new requirements ("v2: +0
    requirement(s)"): the version counter moved, so the gate answered
    STALE_CONTRACT_VERSION, but it could name no requirement to fix. Nothing
    the worker, the engine or an operator could do would ever clear it, and
    the task held its session's lane the whole time.
    """
    contract = rc.amend_contract(
        _contract_r1_r2_r3(), prompt="a follow-up instruction that asks nothing new",
        requirements=[], created_at=LATER, actor="user")
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED)],
                     version=1)

    decision = rc.reconcile(contract, matrix)

    assert contract.contract_version == 2, "the amendment really did bump the version"
    assert decision.verified_done is True
    assert decision.reason == rc.VERIFIED


def test_a_contract_whose_matrix_was_never_written_does_not_block_completion():
    """The shape EVERY real task has.

    Nothing in the package writes an evidence matrix outside tests, so
    `reconciled_contract_version` is 0 in production. Against a bare version
    comparison that made the gate unsatisfiable for every contracted task
    whose requirement list is empty -- which is what a contract built from an
    unstructured prompt is.
    """
    contract = rc.create_contract(prompt="Do the thing", requirements=[],
                                  created_at=NOW, actor="user")

    decision = rc.reconcile(contract, None)

    assert decision.verified_done is True
    assert decision.reason == rc.VERIFIED


def test_a_refusal_always_names_something_to_fix():
    """The property that makes a refusal survivable.

    A gate that refuses without naming a blocker cannot be satisfied, and on a
    lane-holding status that is a deadlock rather than a safety property. Any
    refusal must point at a requirement id.
    """
    contract = rc.amend_contract(
        _contract_r1_r2_r3(), prompt="and export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        created_at=LATER, actor="user")

    optional = rc.create_contract(
        prompt="p", requirements=[{"id": "R1", "text": "optional", "required": False}],
        created_at=NOW)

    cases = [
        (contract, None),
        (contract, _matrix([], version=0)),
        (contract, _matrix([("R1", rc.COVERED)], version=1)),
        (contract, _matrix([("R1", rc.COVERED), ("R2", rc.COVERED),
                            ("R3", rc.COVERED)], version=1)),
        # Stale claim on a requirement added later.
        (contract, _matrix([("R1", rc.COVERED), ("R2", rc.COVERED),
                            ("R3", rc.COVERED), ("R4", rc.COVERED)], version=1)),
        # A waiver missing its actor/reason.
        (contract, _matrix([("R1", rc.WAIVED), ("R2", rc.COVERED),
                            ("R3", rc.COVERED), ("R4", rc.COVERED)], version=2)),
        # An unreadable status -- on an OPTIONAL requirement, so nothing else
        # records the id.
        (optional, rc.EvidenceMatrix(
            entries=(rc.EvidenceEntry("R1", "probably-fine", evidence=("x",)),),
            reconciled_contract_version=1)),
    ]
    for contract_under_test, matrix in cases:
        decision = rc.reconcile(contract_under_test, matrix)
        assert decision.verified_done is False
        assert decision.blocking_ids(), \
            f"refused with nothing to fix: {decision.reason}: {decision.detail}"


def test_evidence_claiming_a_requirement_added_after_it_is_stale():
    """What STALE_CONTRACT_VERSION is actually for, and it still holds.

    Evidence reconciled at v1 cannot honestly claim R4, which only exists
    from v2. That claim is refused BY NAME -- unlike the version-counter
    check it replaces, this refusal is always actionable.
    """
    contract = rc.amend_contract(
        _contract_r1_r2_r3(), prompt="and export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        created_at=LATER, actor="user")
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED),
                      ("R4", rc.COVERED)], version=1)

    decision = rc.reconcile(contract, matrix)

    assert decision.verified_done is False
    assert decision.reason == rc.STALE_CONTRACT_VERSION
    assert decision.missing_requirements == ("R4",)
    assert "R4" in decision.detail


def test_a_stale_partial_claim_is_also_refused():
    """`partial` and `waived` are claims too -- only `missing` is not."""
    contract = rc.amend_contract(
        _contract_r1_r2_r3(), prompt="and export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        created_at=LATER, actor="user")
    matrix = rc.EvidenceMatrix(
        entries=(rc.EvidenceEntry("R1", rc.COVERED, evidence=("commit abc",)),
                 rc.EvidenceEntry("R2", rc.COVERED, evidence=("commit abc",)),
                 rc.EvidenceEntry("R3", rc.COVERED, evidence=("commit abc",)),
                 rc.EvidenceEntry("R4", rc.PARTIAL, evidence=("commit abc",))),
        reconciled_contract_version=1)

    decision = rc.reconcile(contract, matrix)

    assert decision.verified_done is False
    assert decision.reason == rc.STALE_CONTRACT_VERSION
    assert "R4" in decision.missing_requirements


def test_an_explicitly_missing_claim_on_a_new_requirement_is_not_stale():
    """Saying "I did not do R4" is honest, not stale evidence.

    It still blocks -- through the coverage comparison, which names R4 -- but
    calling it stale would send a reader looking for a re-reconciliation that
    would change nothing.
    """
    contract = rc.amend_contract(
        _contract_r1_r2_r3(), prompt="and export the audit trail",
        requirements=[{"id": "R4", "text": "export the audit trail as CSV"}],
        created_at=LATER, actor="user")
    matrix = _matrix([("R1", rc.COVERED), ("R2", rc.COVERED), ("R3", rc.COVERED),
                      ("R4", rc.MISSING)], version=1)

    decision = rc.reconcile(contract, matrix)

    assert decision.verified_done is False
    assert decision.reason == rc.REQUIREMENTS_NOT_COVERED
    assert decision.missing_requirements == ("R4",)
