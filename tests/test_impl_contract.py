"""Implementation Contract v1 / Analysis Gate -- impl_contract.py.
Pure unit tests, no I/O.

The five behaviors this suite exists to pin, in the task's own terms:
high-impact unresolved blocks READY; LOW detail does not block; a
missing source-of-truth on a stateful task blocks; FAST_FIX is a minimal
gate; and an agent that meets unspecified high-impact behavior returns
NEED_ANALYSIS instead of guessing (the marker protocol that carries
that answer back)."""
from __future__ import annotations

from terminal_mcp.impl_contract import (
    ADVISORY, CONTRACT_PROTOCOL, CRITIC_FAIL, CRITIC_PASS, CRITIC_PASS_WITH_FINDINGS,
    CRITIC_VERDICT_FAIL, ENFORCING, FAST_FIX, HIGH, HIGH_IMPACT_LOW_CONFIDENCE_ASSUMPTION,
    HIGH_IMPACT_UNGUARDED_ASSUMPTION, HIGH_RISK, LOW, LOW_DECISION_DELEGATED, MALFORMED_ENTRY,
    MEDIUM, MEDIUM_DECISION_WITHOUT_DEFAULT, MEDIUM_DECISION_WITHOUT_GUARDRAIL,
    MISSING_CRITIC_RESULT, MISSING_REQUIRED_FIELD, MISSING_SOURCE_OF_TRUTH, MISSING_STATE_MODEL,
    NEED_ANALYSIS, OPEN, READY, RESOLVED, SKIPPED, STANDARD, UNKNOWN_FIELD, UNKNOWN_PROFILE,
    UNRESOLVED_HIGH_DECISION, UNSUPPORTED_VERSION, build_contract_prompt,
    build_need_analysis_marker, check_decision_budget, contract_digest, contract_summary,
    evaluate_contract, extract_contract, parse_need_analysis_marker,
)


def _codes(result):
    return {f["code"] for f in result["findings"]}


def _blocking_codes(result):
    return {f["code"] for f in result["findings"] if f["blocking"]}


def _standard(**overrides):
    """A complete, passing STANDARD contract -- every test below is a
    single deliberate deviation from this one, so a failure names the
    rule that changed rather than a pile of unrelated missing fields."""
    contract = {
        "protocol": CONTRACT_PROTOCOL,
        "profile": STANDARD,
        "enforcement": ENFORCING,
        "goal": "Kiosk scan must not double-count a part",
        "current_behavior": "A second scan within the debounce window creates a second row.",
        "expected_behavior": "A second scan inside 3s is rejected with a visible message.",
        "invariants": ["one scan event per (device, part, second)"],
        "edge_cases": ["clock skew between kiosk and server"],
        "dangerous_failure_modes": ["silently dropping a legitimate second scan of a different part"],
        "acceptance": ["duplicate scan returns 409 and no new row"],
        "live_verify": "scan the same badge twice on kiosk1 and read the DB",
        "out_of_scope": ["the offline queue"],
        "context_pack": [{"ref": "terminal_mcp/queue_store.py", "kind": "file", "why": "claim path"}],
        "decision_budget": [],
        "assumptions": [],
    }
    contract.update(overrides)
    return contract


# --------------------------------------------------------------------------
# Legacy compatibility -- rollout layer 1
# --------------------------------------------------------------------------
def test_no_contract_is_skipped_and_never_blocks():
    result = evaluate_contract(None)
    assert result["verdict"] == SKIPPED
    assert result["blocks_ready"] is False
    assert result["findings"] == []


def test_empty_contract_is_skipped_not_a_pile_of_missing_fields():
    assert evaluate_contract({})["verdict"] == SKIPPED


def test_extract_contract_from_a_legacy_task_returns_none():
    assert extract_contract({"title": "t", "metadata": {}}) is None
    assert extract_contract({"title": "t"}) is None
    assert extract_contract(object()) is None


def test_extract_contract_reads_task_metadata():
    task = {"metadata": {"impl_contract": _standard()}}
    assert extract_contract(task)["goal"] == _standard()["goal"]


def test_unknown_protocol_is_refused_loudly_not_validated_against_v1():
    result = evaluate_contract(_standard(protocol="terminal-mcp-impl-contract/v9"))
    assert result["verdict"] == UNSUPPORTED_VERSION
    # Not partially validated: no v1 field findings are invented for it.
    assert _codes(result) == {MALFORMED_ENTRY}


def test_missing_protocol_is_v0_legacy_and_refused_not_assumed_compatible():
    contract = _standard()
    contract.pop("protocol")
    assert evaluate_contract(contract)["verdict"] == UNSUPPORTED_VERSION


# --------------------------------------------------------------------------
# Rollout layers 2 and 3 -- advisory vs enforcing
# --------------------------------------------------------------------------
def test_advisory_mode_reports_the_real_verdict_but_never_blocks():
    contract = _standard(enforcement=ADVISORY, decision_budget=[
        {"id": "D1", "question": "which store is authoritative", "impact": HIGH, "status": OPEN}])
    result = evaluate_contract(contract)
    assert result["verdict"] == NEED_ANALYSIS
    assert result["blocks_ready"] is False
    assert UNRESOLVED_HIGH_DECISION in _blocking_codes(result)


def test_advisory_is_the_default_when_the_contract_declares_nothing():
    contract = _standard(decision_budget=[{"id": "D1", "impact": HIGH, "status": OPEN}])
    contract.pop("enforcement")
    result = evaluate_contract(contract)
    assert result["enforcement"] == ADVISORY
    assert result["blocks_ready"] is False


def test_caller_can_enforce_a_contract_that_declares_itself_advisory():
    contract = _standard(enforcement=ADVISORY,
                         decision_budget=[{"id": "D1", "impact": HIGH, "status": OPEN}])
    assert evaluate_contract(contract, enforcement=ENFORCING)["blocks_ready"] is True


def test_an_unreadable_enforcement_value_falls_back_to_advisory_not_enforcing():
    contract = _standard(enforcement="ENFORCE!!", decision_budget=[{"id": "D1", "impact": HIGH}])
    result = evaluate_contract(contract)
    assert result["enforcement"] == ADVISORY
    assert result["blocks_ready"] is False


# --------------------------------------------------------------------------
# Decision budget -- HIGH blocks, MEDIUM needs default+guardrail, LOW never blocks
# --------------------------------------------------------------------------
def test_complete_standard_contract_is_ready():
    result = evaluate_contract(_standard())
    assert result["verdict"] == READY
    assert result["blocks_ready"] is False


def test_unresolved_high_decision_blocks_ready():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D1", "question": "hard-delete or soft-delete", "impact": HIGH, "status": OPEN}]))
    assert result["verdict"] == NEED_ANALYSIS
    assert result["blocks_ready"] is True
    assert UNRESOLVED_HIGH_DECISION in _blocking_codes(result)


def test_resolved_high_decision_does_not_block():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D1", "question": "hard-delete or soft-delete", "impact": HIGH,
         "status": RESOLVED, "decision": "soft-delete, deleted_at column"}]))
    assert result["verdict"] == READY


def test_resolved_with_no_decision_recorded_is_the_same_guess_and_blocks():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D1", "impact": HIGH, "status": RESOLVED}]))
    assert result["blocks_ready"] is True
    assert MALFORMED_ENTRY in _blocking_codes(result)


def test_open_medium_decision_with_default_and_guardrail_is_ready():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D2", "question": "retry count", "impact": MEDIUM, "status": OPEN,
         "default": "3 retries", "guardrail": "log every exhausted retry; alert above 5/min"}]))
    assert result["verdict"] == READY


def test_open_medium_decision_without_default_blocks():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D2", "question": "retry count", "impact": MEDIUM, "status": OPEN,
         "guardrail": "log every exhausted retry"}]))
    assert result["blocks_ready"] is True
    assert MEDIUM_DECISION_WITHOUT_DEFAULT in _blocking_codes(result)


def test_open_medium_decision_without_guardrail_blocks():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D2", "question": "retry count", "impact": MEDIUM, "status": OPEN,
         "default": "3 retries"}]))
    assert result["blocks_ready"] is True
    assert MEDIUM_DECISION_WITHOUT_GUARDRAIL in _blocking_codes(result)


def test_low_impact_detail_never_blocks_and_is_reported_as_delegated():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D3", "question": "log message wording", "impact": LOW, "status": OPEN},
        {"id": "D4", "question": "helper function name", "impact": LOW, "status": OPEN},
        {"id": "D5", "question": "where the constant lives", "impact": LOW},
    ]))
    assert result["verdict"] == READY
    assert result["blocks_ready"] is False
    assert _blocking_codes(result) == set()
    assert [f["code"] for f in result["findings"]] == [LOW_DECISION_DELEGATED] * 3


def test_a_decision_with_no_impact_declared_is_treated_as_high_never_as_low():
    result = evaluate_contract(_standard(decision_budget=[
        {"id": "D1", "question": "which store wins", "status": OPEN}]))
    assert result["blocks_ready"] is True
    finding = next(f for f in result["findings"] if f["blocking"])
    assert "treated as HIGH" in finding["detail"]


def test_decision_budget_can_be_checked_on_its_own():
    findings = check_decision_budget([{"id": "D1", "impact": HIGH, "status": OPEN}])
    assert [f["code"] for f in findings] == [UNRESOLVED_HIGH_DECISION]
    assert check_decision_budget(None) == []


def test_a_non_list_decision_budget_is_a_finding_not_an_exception():
    result = evaluate_contract(_standard(decision_budget="we decided everything, trust me"))
    assert MALFORMED_ENTRY in _blocking_codes(result)


# --------------------------------------------------------------------------
# Assumptions -- budgeted by impact, not by how confident the prose sounds
# --------------------------------------------------------------------------
def test_high_impact_low_confidence_assumption_blocks():
    result = evaluate_contract(_standard(assumptions=[
        {"statement": "no other writer touches this table", "impact": HIGH, "confidence": LOW}]))
    assert result["blocks_ready"] is True
    assert HIGH_IMPACT_LOW_CONFIDENCE_ASSUMPTION in _blocking_codes(result)


def test_high_impact_medium_confidence_assumption_needs_a_guardrail():
    unguarded = evaluate_contract(_standard(assumptions=[
        {"statement": "the queue is single-consumer", "impact": HIGH, "confidence": MEDIUM}]))
    assert HIGH_IMPACT_UNGUARDED_ASSUMPTION in _blocking_codes(unguarded)

    guarded = evaluate_contract(_standard(assumptions=[
        {"statement": "the queue is single-consumer", "impact": HIGH, "confidence": MEDIUM,
         "guardrail": "claim is BEGIN IMMEDIATE; a second consumer shows up as a claim conflict"}]))
    assert guarded["verdict"] == READY


def test_high_confidence_high_impact_assumption_is_fine():
    result = evaluate_contract(_standard(assumptions=[
        {"statement": "sqlite WAL is on", "impact": HIGH, "confidence": HIGH}]))
    assert result["verdict"] == READY


def test_low_impact_assumptions_never_block_however_unsure():
    result = evaluate_contract(_standard(assumptions=[
        {"statement": "nobody reads this log", "impact": LOW, "confidence": LOW},
        {"statement": "the label fits on one line", "impact": MEDIUM, "confidence": LOW}]))
    assert result["verdict"] == READY


# --------------------------------------------------------------------------
# Stateful work -- source of truth
# --------------------------------------------------------------------------
def test_stateful_task_without_source_of_truth_blocks():
    result = evaluate_contract(_standard(stateful=True, state_model={"states": ["NEW", "DONE"]}))
    assert result["blocks_ready"] is True
    assert MISSING_SOURCE_OF_TRUTH in _blocking_codes(result)


def test_stateful_task_with_source_of_truth_and_state_model_is_ready():
    result = evaluate_contract(_standard(
        stateful=True, source_of_truth="queue_tasks table (sqlite); every view derives from it",
        state_model={"states": ["UNASSIGNED", "READY", "RUNNING"], "writer": "queue_engine"}))
    assert result["verdict"] == READY


def test_stateful_standard_task_without_state_model_blocks():
    result = evaluate_contract(_standard(stateful=True, source_of_truth="queue_tasks"))
    assert MISSING_STATE_MODEL in _blocking_codes(result)


def test_a_stateless_task_owes_neither():
    result = evaluate_contract(_standard())
    assert MISSING_SOURCE_OF_TRUTH not in _codes(result)
    assert MISSING_STATE_MODEL not in _codes(result)


def test_source_of_truth_is_required_even_at_the_fast_fix_minimal_gate():
    result = evaluate_contract({
        "protocol": CONTRACT_PROTOCOL, "profile": FAST_FIX, "enforcement": ENFORCING,
        "stateful": True, "goal": "fix the off-by-one", "expected_behavior": "counts match",
        "acceptance": ["the count is 3, not 4"]})
    assert MISSING_SOURCE_OF_TRUTH in _blocking_codes(result)
    # ...but a FAST_FIX still does not owe a state model.
    assert MISSING_STATE_MODEL not in _codes(result)


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------
def test_fast_fix_needs_only_goal_expected_behavior_and_acceptance():
    result = evaluate_contract({
        "protocol": CONTRACT_PROTOCOL, "profile": FAST_FIX, "enforcement": ENFORCING,
        "goal": "fix the typo in the error message",
        "expected_behavior": "the message reads 'not found'",
        "acceptance": ["the string is exactly 'not found'"]})
    assert result["verdict"] == READY
    assert result["blocks_ready"] is False


def test_the_same_minimal_contract_at_standard_is_not_ready():
    result = evaluate_contract({
        "protocol": CONTRACT_PROTOCOL, "profile": STANDARD, "enforcement": ENFORCING,
        "goal": "fix the typo in the error message",
        "expected_behavior": "the message reads 'not found'",
        "acceptance": ["the string is exactly 'not found'"]})
    assert result["blocks_ready"] is True
    assert MISSING_REQUIRED_FIELD in _blocking_codes(result)


def test_fast_fix_still_blocks_on_an_unresolved_high_decision():
    """The minimal gate is minimal about DETAIL, never about judgment."""
    result = evaluate_contract({
        "protocol": CONTRACT_PROTOCOL, "profile": FAST_FIX, "enforcement": ENFORCING,
        "goal": "fix the duplicate row", "expected_behavior": "one row",
        "acceptance": ["one row"],
        "decision_budget": [{"id": "D1", "question": "delete which duplicate", "impact": HIGH,
                             "status": OPEN}]})
    assert result["blocks_ready"] is True
    assert UNRESOLVED_HIGH_DECISION in _blocking_codes(result)


def test_high_risk_requires_a_critic_result():
    result = evaluate_contract(_standard(profile=HIGH_RISK))
    assert result["blocks_ready"] is True
    assert MISSING_CRITIC_RESULT in _blocking_codes(result)


def test_high_risk_with_a_passing_critic_is_ready():
    result = evaluate_contract(_standard(
        profile=HIGH_RISK, critic_result={"verdict": CRITIC_PASS, "critic": "critic-agent-1"}))
    assert result["verdict"] == READY


def test_high_risk_critic_pass_with_findings_does_not_block():
    result = evaluate_contract(_standard(
        profile=HIGH_RISK,
        critic_result={"verdict": CRITIC_PASS_WITH_FINDINGS, "summary": "naming nits"}))
    assert result["verdict"] == READY


def test_high_risk_critic_fail_blocks():
    result = evaluate_contract(_standard(
        profile=HIGH_RISK,
        critic_result={"verdict": CRITIC_FAIL, "summary": "the migration is not reversible"}))
    assert CRITIC_VERDICT_FAIL in _blocking_codes(result)


def test_standard_and_fast_fix_never_require_a_critic():
    assert MISSING_CRITIC_RESULT not in _codes(evaluate_contract(_standard()))


def test_an_unknown_profile_is_validated_against_standard_not_the_loosest_one():
    result = evaluate_contract(_standard(profile="TRIVIAL", invariants=[], acceptance=[]))
    assert UNKNOWN_PROFILE in _blocking_codes(result)
    assert result["profile"] == STANDARD
    assert MISSING_REQUIRED_FIELD in _blocking_codes(result)


# --------------------------------------------------------------------------
# Shape checks that must not silently swallow analysis
# --------------------------------------------------------------------------
def test_an_unknown_field_is_reported_but_does_not_block():
    result = evaluate_contract(_standard(acceptance_criteria=["typo'd field name"]))
    assert UNKNOWN_FIELD in _codes(result)
    assert result["verdict"] == READY


def test_context_pack_entry_without_a_ref_blocks():
    result = evaluate_contract(_standard(context_pack=[{"kind": "file"}]))
    assert MALFORMED_ENTRY in _blocking_codes(result)


def test_context_pack_accepts_bare_strings_and_only_warns_about_a_missing_kind():
    result = evaluate_contract(_standard(context_pack=["docs/REQUIREMENTS.md §20.6",
                                                       {"ref": "terminal_mcp/lease.py"}]))
    assert result["verdict"] == READY
    assert MALFORMED_ENTRY in _codes(result)  # non-blocking: the missing `kind`
    assert _blocking_codes(result) == set()


def test_false_and_zero_are_declared_values_not_missing_ones():
    result = evaluate_contract(_standard(stateful=False, out_of_scope=[0]))
    assert result["verdict"] == READY


def test_the_gate_never_raises_on_junk():
    for junk in ("a string", 12, [1, 2], True):
        assert evaluate_contract(junk)["verdict"] in (SKIPPED, UNSUPPORTED_VERSION)


def test_contract_digest_ignores_key_order_and_outer_whitespace_but_not_edits():
    a = {"goal": " ship it ", "profile": STANDARD}
    b = {"profile": STANDARD, "goal": "ship it"}
    c = {"profile": STANDARD, "goal": "ship it tomorrow"}
    assert contract_digest(a) == contract_digest(b)
    assert contract_digest(a) != contract_digest(c)


# --------------------------------------------------------------------------
# NEED_ANALYSIS marker -- the agent's answer instead of a guess
# --------------------------------------------------------------------------
def test_need_analysis_marker_round_trips():
    line = build_need_analysis_marker(task_id="t-1", attempt=2, nonce="abc123",
                                      impact=HIGH, decision_id="D7")
    parsed = parse_need_analysis_marker(f"I stopped because of X.\n{line}\n")
    assert parsed["task_id"] == "t-1"
    assert parsed["attempt"] == "2"
    assert parsed["nonce"] == "abc123"
    assert parsed["impact"] == HIGH
    assert parsed["decision_id"] == "D7"


def test_a_decision_id_with_spaces_cannot_break_the_marker_fields():
    line = build_need_analysis_marker(task_id="t-1", attempt=1, nonce="n",
                                      decision_id="which store wins")
    assert parse_need_analysis_marker(line)["decision_id"] == "which_store_wins"


def test_an_empty_decision_id_still_produces_a_parseable_marker():
    line = build_need_analysis_marker(task_id="t-1", attempt=1, nonce="n", decision_id="")
    assert parse_need_analysis_marker(line)["decision_id"] == "unspecified"


def test_no_marker_and_an_incomplete_marker_both_read_as_no_marker():
    assert parse_need_analysis_marker("I could not figure it out, sorry") is None
    assert parse_need_analysis_marker(
        "###TERMINAL_MCP_NEED_ANALYSIS protocol=terminal-mcp-need-analysis/v1 "
        "task_id=t-1 status=need_analysis###") is None  # no impact
    assert parse_need_analysis_marker(
        "###TERMINAL_MCP_NEED_ANALYSIS protocol=terminal-mcp-need-analysis/v1 "
        "task_id=t-1 status=done impact=HIGH###") is None  # wrong status


def test_an_unreadable_impact_is_not_guessed_at():
    assert parse_need_analysis_marker(
        "###TERMINAL_MCP_NEED_ANALYSIS protocol=terminal-mcp-need-analysis/v1 "
        "task_id=t-1 status=need_analysis impact=PROBABLY_BIG###") is None


def test_the_last_marker_wins_like_the_completion_marker():
    first = build_need_analysis_marker(task_id="t-1", attempt=1, nonce="n", decision_id="D1")
    second = build_need_analysis_marker(task_id="t-1", attempt=1, nonce="n", decision_id="D2")
    assert parse_need_analysis_marker(f"{first}\nlater...\n{second}")["decision_id"] == "D2"


# --------------------------------------------------------------------------
# Prompt packaging -- reference the contract, do not restate it
# --------------------------------------------------------------------------
def test_the_prompt_carries_the_escape_hatch_the_agent_is_supposed_to_use():
    prompt = build_contract_prompt(_standard(), task_id="t-1", attempt=1, nonce="n")
    assert "NEED_ANALYSIS" in prompt
    assert parse_need_analysis_marker(prompt) is not None
    assert "is a CORRECT outcome" in prompt


def test_the_prompt_points_at_the_contract_instead_of_pasting_long_prose():
    long_prose = "the legacy path " * 400
    prompt = build_contract_prompt(_standard(current_behavior=long_prose),
                                   task_id="t-1", attempt=1, nonce="n",
                                   contract_ref="terminal_task_get_contract(task_id='t-1')")
    assert len(prompt) < len(long_prose)
    assert "truncated" in prompt
    assert "terminal_task_get_contract(task_id='t-1')" in prompt
    assert contract_digest(_standard(current_behavior=long_prose)) in prompt


def test_the_prompt_separates_decided_deferred_and_delegated():
    prompt = build_contract_prompt(_standard(decision_budget=[
        {"id": "D1", "question": "store", "impact": HIGH, "status": RESOLVED,
         "decision": "queue_tasks is authoritative"},
        {"id": "D2", "question": "retries", "impact": MEDIUM, "status": OPEN,
         "default": "3", "guardrail": "alert above 5/min"},
        {"id": "D3", "question": "log wording", "impact": LOW, "status": OPEN},
    ]), task_id="t-1", attempt=1, nonce="n")
    assert "do not re-litigate" in prompt
    assert "queue_tasks is authoritative" in prompt
    assert "alert above 5/min" in prompt
    assert "Yours to choose" in prompt


def test_the_prompt_lists_the_context_pack_as_refs():
    prompt = build_contract_prompt(_standard(), task_id="t-1", attempt=1, nonce="n")
    assert "CONTEXT PACK" in prompt
    assert "terminal_mcp/queue_store.py" in prompt


def test_the_prompt_names_the_source_of_truth_for_a_stateful_task():
    prompt = build_contract_prompt(
        _standard(stateful=True, source_of_truth="queue_tasks table",
                  state_model={"states": ["NEW"]}),
        task_id="t-1", attempt=1, nonce="n")
    assert "SOURCE OF TRUTH" in prompt
    assert "queue_tasks table" in prompt
    assert "do not write a second store" in prompt


def test_the_prompt_does_not_add_a_second_completion_marker():
    """queue_engine.build_dispatch_text already appends that one."""
    prompt = build_contract_prompt(_standard(), task_id="t-1", attempt=1, nonce="n")
    assert "TERMINAL_MCP_COMPLETION" not in prompt


def test_the_prompt_survives_a_contract_with_almost_nothing_in_it():
    prompt = build_contract_prompt({"protocol": CONTRACT_PROTOCOL, "profile": FAST_FIX},
                                   task_id="t-1", attempt=1, nonce="n")
    assert "GOAL: (not declared)" in prompt
    assert "no decisions recorded" in prompt


# --------------------------------------------------------------------------
# The flat shape telemetry/benchmark tooling consumes (owned elsewhere)
# --------------------------------------------------------------------------
def test_contract_summary_counts_the_budget_without_an_opinion_about_storage():
    summary = contract_summary(_standard(stateful=True, decision_budget=[
        {"id": "D1", "impact": HIGH, "status": RESOLVED, "decision": "x"},
        {"id": "D2", "impact": HIGH, "status": OPEN},
        {"id": "D3", "impact": MEDIUM, "status": OPEN, "default": "d", "guardrail": "g"},
        {"id": "D4", "impact": LOW},
    ], assumptions=[{"statement": "s", "impact": HIGH, "confidence": HIGH}]))
    assert summary["has_contract"] is True
    assert summary["decisions_total"] == 4
    assert summary["decisions_high_open"] == 1
    assert summary["decisions_high_resolved"] == 1
    assert summary["decisions_medium_open"] == 1
    assert summary["decisions_low"] == 1
    assert summary["assumptions_high_impact"] == 1
    assert summary["profile"] == STANDARD
    assert summary["context_pack_refs"] == 1
    assert summary["stateful"] is True


def test_contract_summary_on_a_legacy_task_says_so_and_nothing_else():
    assert contract_summary(None) == {"has_contract": False}


def test_gate_result_is_json_shaped_and_exposes_blocking_findings():
    result = evaluate_contract(_standard(decision_budget=[{"id": "D1", "impact": HIGH}]))
    payload = result.to_dict()
    assert payload["protocol"] == CONTRACT_PROTOCOL
    assert set(payload) >= {"verdict", "blocks_ready", "enforcement", "profile", "findings",
                            "reason", "contract_digest"}
    assert [f["code"] for f in result.blocking_findings()] == [UNRESOLVED_HIGH_DECISION]


# --------------------------------------------------------------------------
# The worked example in the docs must stay valid
# --------------------------------------------------------------------------
def test_the_documented_example_contract_is_ready():
    """docs/examples/impl-contract.example.json is what an analysis agent is
    shown. A doc example that no longer passes its own gate teaches the
    wrong format to every reader."""
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "docs/examples/impl-contract.example.json"
    contract = json.loads(path.read_text())
    result = evaluate_contract(contract, enforcement=ENFORCING)
    assert result["verdict"] == READY, result["reason"]
    assert result["blocks_ready"] is False
    # It demonstrates the non-blocking codes on purpose: its `_comment` key
    # is not a v1 field, and it hands two LOW decisions to the agent.
    assert _codes(result) == {UNKNOWN_FIELD, LOW_DECISION_DELEGATED}


def test_the_documented_example_builds_a_prompt_that_fits_in_a_terminal():
    import json
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "docs/examples/impl-contract.example.json"
    contract = json.loads(path.read_text())
    prompt = build_contract_prompt(contract, task_id="t-1", attempt=1, nonce="n",
                                   contract_ref="terminal_task_get_contract('t-1')")
    assert len(prompt) < 6000, len(prompt)
    assert "queue_tasks table" in prompt          # the source of truth is named
    assert "do not re-litigate" in prompt          # D1 is decided
    assert "O(expired)" in prompt                  # D2's guardrail travels with its default
    assert "Yours to choose" in prompt             # D3/D4 are delegated
    assert "NEED_ANALYSIS" in prompt


# --------------------------------------------------------------------------
# Prompt rendering of a contract that dispatched ANYWAY (advisory mode)
# --------------------------------------------------------------------------
def test_an_unresolved_high_decision_is_rendered_as_unresolved_not_as_default_none():
    """In advisory mode a contract with an open HIGH decision still
    dispatches. The prompt must say nobody answered it -- rendering
    `default: None` would read as an instruction to use None."""
    prompt = build_contract_prompt(_standard(decision_budget=[
        {"id": "D1", "question": "hard-delete or soft-delete", "impact": HIGH, "status": OPEN}]),
        task_id="t-1", attempt=1, nonce="n")
    assert "UNRESOLVED" in prompt
    assert "default: None" not in prompt
    assert "return NEED_ANALYSIS" in prompt


def test_a_medium_default_with_no_guardrail_says_none_declared_not_none():
    prompt = build_contract_prompt(_standard(decision_budget=[
        {"id": "D2", "question": "retries", "impact": MEDIUM, "status": OPEN, "default": "3"}]),
        task_id="t-1", attempt=1, nonce="n")
    assert "guardrail: (none declared)" in prompt
    assert "guardrail: None" not in prompt


def test_one_enormous_list_item_is_truncated_not_pasted_whole():
    """A single 4KB invariant must not defeat 'reference, don't restate'."""
    huge = "the lease must not expire while the worker is alive " * 200
    prompt = build_contract_prompt(_standard(invariants=[huge]),
                                   task_id="t-1", attempt=1, nonce="n")
    assert len(prompt) < len(huge)
    assert "truncated" in prompt
