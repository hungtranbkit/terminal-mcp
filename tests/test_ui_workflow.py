"""The central UI workflow policy -- TMCP-UI-WORKFLOW-001.

What is worth testing here is the DECISIONS, because they are the whole
module: which profile applies, whether Image-to-Code runs at all, who wins
when two design authorities disagree, and whether an audit result is allowed
through the gate. The module executes nothing, so every test below is pure.

The one integration concern is the opposite of a feature: this must not have
widened or disturbed the compact Browser Gateway surface. That is asserted
explicitly at the bottom rather than assumed.
"""
from __future__ import annotations

import asyncio

import pytest

from terminal_mcp import ui_workflow as uw


# -- precedence: the reason the module exists --------------------------------

def test_the_precedence_table_is_the_specified_order():
    assert [name for _, name, _ in uw.PRECEDENCE] == [
        "user_instruction",
        "project_ui_rules",
        "design_system",
        "reference_mockup",
        "taste",
        "web_design_guidelines",
        "generic_ui_skill",
    ]
    assert uw.LAYER_RANK["user_instruction"] == 1
    assert uw.LAYER_RANK["generic_ui_skill"] == 7


def test_a_lower_layer_never_wins_over_a_higher_one():
    """Taste and the Guidelines are real authorities, and both lose to the
    project's own UI rules. That direction is the contract."""
    resolved = uw.resolve_precedence({
        "generic_ui_skill": "use a card grid",
        "web_design_guidelines": "increase contrast",
        "taste": "warmer palette",
        "project_ui_rules": ["UI V3 only"],
    })
    assert resolved["winner"] == "project_ui_rules"
    assert resolved["winning_rank"] == 2
    assert resolved["overridden"] == ["taste", "web_design_guidelines", "generic_ui_skill"]


def test_user_instruction_outranks_everything_including_a_mockup():
    resolved = uw.resolve_precedence({
        "reference_mockup": ["/tmp/shot.png"],
        "project_ui_rules": ["UI V3 only"],
        "user_instruction": "keep the existing header",
    })
    assert resolved["winner"] == "user_instruction"


def test_an_empty_layer_is_not_an_opinion():
    """Silence must not outrank a real contribution just by being higher."""
    resolved = uw.resolve_precedence({
        "user_instruction": "",
        "project_ui_rules": [],
        "taste": "warmer palette",
    })
    assert resolved["winner"] == "taste"
    assert [c["layer"] for c in resolved["chain"]] == ["taste"]


def test_no_contributing_layer_is_reported_honestly():
    resolved = uw.resolve_precedence({})
    assert resolved["winner"] is None
    assert resolved["chain"] == []


# -- project profiles --------------------------------------------------------

def test_novaretail_profile_carries_its_own_rules_and_viewports():
    profile = uw.select_profile(project_id="novaretail")
    assert profile.project_id == "novaretail"
    assert (1366, 768) in profile.viewports
    assert (1920, 1080) in profile.viewports
    assert (390, 844) in profile.viewports
    joined = " ".join(profile.ui_rules)
    assert "V3" in joined and "V2" in joined, "the never-revive-V2 rule must be stated"
    for check in ("forms", "modals", "tables_lists", "print"):
        assert check in profile.checks


def test_mesflow_profile_forbids_changing_application_behaviour():
    profile = uw.select_profile(project_id="mesflow")
    assert profile.allow_behaviour_change is False
    verdict = uw.allows_behaviour_change(profile, requested=True)
    assert verdict["allowed"] is False
    assert "forbids" in verdict["reason"]
    # Not requesting one is always fine.
    assert uw.allows_behaviour_change(profile, requested=False)["allowed"] is True
    for check in ("dashboard", "po_part_operation_views", "kiosk_mobile",
                  "quantity_multisession"):
        assert check in profile.checks


def test_a_profile_is_matched_from_the_repo_path():
    assert uw.select_profile(repo_root="/home/dell/workspace/novaretail-web").project_id == "novaretail"
    assert uw.select_profile(repo_root="/home/dell/workspace/mesflow/mesflow").project_id == "mesflow"


def test_an_explicit_project_id_beats_a_conflicting_path():
    """A caller naming the project is layer 1; guessing over it would invert
    the precedence order this module enforces everywhere else."""
    profile = uw.select_profile(project_id="mesflow",
                                repo_root="/home/dell/workspace/novaretail-web")
    assert profile.project_id == "mesflow"


def test_an_unknown_project_gets_the_strict_generic_profile_not_a_loose_one():
    profile = uw.select_profile(project_id="whatever", repo_root="/tmp/unrelated")
    assert profile.project_id == "generic"
    assert profile.allow_behaviour_change is False
    assert profile.viewports == uw.DEFAULT_VIEWPORTS


# -- conditional Image-to-Code ----------------------------------------------

def test_image_to_code_is_skipped_without_a_reference():
    """The condition IS the feature: with no reference the alternative is
    inventing a visual contract and implementing against the invention."""
    routing = uw.route_image_to_code()
    assert routing["route"] == "skip"
    assert routing["authority_layer"] is None
    assert "no reference" in routing["reason"]


def test_image_to_code_runs_when_a_reference_is_supplied():
    routing = uw.route_image_to_code(reference_images=["/tmp/orders.png"])
    assert routing["route"] == "image_to_code"
    assert routing["references"] == ["/tmp/orders.png"]
    assert routing["authority_layer"] == "reference_mockup"


def test_an_explicit_no_reference_beats_a_non_empty_list():
    """The caller knows; we are guessing."""
    routing = uw.route_image_to_code(reference_images=["/tmp/x.png"],
                                     reference_provided=False)
    assert routing["route"] == "skip"


def test_a_caller_may_assert_a_reference_it_holds_elsewhere():
    routing = uw.route_image_to_code(reference_provided=True)
    assert routing["route"] == "image_to_code"
    assert routing["authority_layer"] == "reference_mockup"


def test_blank_reference_paths_do_not_count_as_a_reference():
    assert uw.route_image_to_code(reference_images=["", "   "])["route"] == "skip"


# -- the Web Design Guidelines audit gate ------------------------------------

def test_one_critical_finding_fails_the_gate():
    result = uw.audit_gate(counts={"critical": 1})
    assert result["verdict"] == uw.GATE_FAIL
    assert result["blocking"] is True
    assert result["next_stage"] == "fix_blocking"


def test_a_major_finding_must_be_fixed_before_browser_verification():
    result = uw.audit_gate(counts={"major": 2})
    assert result["verdict"] == uw.GATE_FIX_REQUIRED
    assert result["blocking"] is True
    assert result["next_stage"] == "fix_blocking"


def test_critical_outranks_major_in_the_verdict():
    result = uw.audit_gate(counts={"critical": 1, "major": 5})
    assert result["verdict"] == uw.GATE_FAIL


def test_minor_findings_pass_but_are_not_silently_dropped():
    reported = uw.audit_gate(counts={"minor": 3})
    assert reported["verdict"] == uw.GATE_PASS
    assert reported["blocking"] is False
    assert reported["minor_action"] == "report_and_carry_forward"
    assert reported["next_stage"] == "browser_verify"

    low_risk = uw.audit_gate(counts={"minor": 3}, minor_fix_is_low_risk=True)
    assert low_risk["minor_action"] == "fix_now"


def test_a_clean_audit_passes_with_no_minor_action():
    result = uw.audit_gate([])
    assert result["verdict"] == uw.GATE_PASS
    assert result["minor_action"] is None
    assert result["counts"] == {"critical": 0, "major": 0, "minor": 0}


def test_the_gate_counts_raw_findings_when_no_counts_are_given():
    findings = [{"severity": "major", "id": "a"}, {"severity": "MINOR", "id": "b"},
                {"severity": "minor", "id": "c"}, {"id": "no-severity"}]
    result = uw.audit_gate(findings)
    assert result["counts"] == {"critical": 0, "major": 1, "minor": 2}
    assert result["verdict"] == uw.GATE_FIX_REQUIRED


def test_precomputed_counts_win_over_raw_findings():
    result = uw.audit_gate([{"severity": "critical"}], counts={"critical": 0, "major": 0})
    assert result["verdict"] == uw.GATE_PASS


# -- the deterministic flow --------------------------------------------------

def test_the_flow_skips_image_to_code_when_there_is_no_reference():
    """A skipped conditional stage must not linger as pending work."""
    assert uw.next_stage(completed=["profile", "taste"], image_to_code=False) == "implementation"
    assert uw.next_stage(completed=["profile", "taste"], image_to_code=True) == "image_to_code"


def test_the_flow_only_offers_a_fix_stage_when_something_demands_it():
    done = ["profile", "taste", "implementation", "guidelines_audit"]
    assert uw.next_stage(completed=done, gate_blocking=False) == "browser_verify"
    assert uw.next_stage(completed=done, gate_blocking=True) == "fix_blocking"


def test_a_failed_verification_routes_to_reverify_not_to_commit():
    done = ["profile", "taste", "implementation", "guidelines_audit", "browser_verify"]
    assert uw.next_stage(completed=done, verification_failed=True) == "fix_reverify"
    assert uw.next_stage(completed=done, verification_failed=False) == "commit"


def test_the_flow_terminates():
    assert uw.next_stage(completed=uw.STAGES) is None


def test_the_five_specified_checkpoints_exist_and_map_to_stages():
    assert [n for n, _, _ in uw.CHECKPOINTS] == [1, 2, 3, 4, 5]
    assert [name for _, name, _ in uw.CHECKPOINTS] == [
        "visual_contract", "implementation_complete", "audit_complete",
        "verification_complete", "deployed_and_live",
    ]
    for _, _, stage in uw.CHECKPOINTS:
        assert stage in uw.STAGES, f"{stage} must be a real stage"
    assert uw.checkpoint_for("guidelines_audit")["checkpoint"] == 3
    assert uw.checkpoint_for("implementation")["name"] == "implementation_complete"
    assert uw.checkpoint_for("commit") is None


# -- the pinned skill catalog ------------------------------------------------

def test_the_catalog_never_fetches_and_reports_absence_honestly():
    catalog = uw.skill_catalog(installed=[])
    assert "never" in catalog["runtime_fetch"]
    assert "taste" in catalog["missing"]
    assert "web-design-guidelines" in catalog["missing"]


def test_an_installed_skill_is_marked_available():
    catalog = uw.skill_catalog(installed=["taste", "web-design-guidelines"])
    by_name = {s["name"]: s for s in catalog["skills"]}
    assert by_name["taste"]["available"] is True
    assert by_name["web-design-guidelines"]["available"] is True
    assert "taste" not in catalog["missing"]


def test_the_awesome_catalog_is_index_only_and_never_counted_missing():
    """Bulk-loading a collection at runtime is the opposite of a
    deterministic flow, so it is catalog-only by construction."""
    catalog = uw.skill_catalog(installed=[])
    entry = next(s for s in catalog["skills"] if s["name"] == "awesome-design-agent-skills")
    assert entry["catalog_only"] is True
    assert "awesome-design-agent-skills" not in catalog["missing"]


def test_ui_ux_pro_max_is_kept_at_the_lowest_precedence():
    entry = next(s for s in uw.SKILL_CATALOG if s.name == "ui-ux-pro-max")
    assert entry.precedence_layer == "generic_ui_skill"
    assert uw.LAYER_RANK[entry.precedence_layer] == 7


def test_image_to_code_is_catalogued_under_the_reference_layer():
    entry = next(s for s in uw.SKILL_CATALOG if s.name == "taste:image-to-code")
    assert entry.precedence_layer == "reference_mockup"


# -- the one-call plan -------------------------------------------------------

def test_the_plan_is_deterministic():
    kwargs = dict(task="fix the orders table", repo_root="/home/dell/workspace/novaretail-web")
    assert uw.plan(**kwargs) == uw.plan(**kwargs)


def test_the_plan_wires_the_profile_rules_into_the_precedence_chain():
    result = uw.plan(task="polish", project_id="novaretail")
    assert result["precedence"]["winner"] == "project_ui_rules"
    assert result["profile"]["project_id"] == "novaretail"


def test_a_mockup_in_the_plan_outranks_taste_but_not_the_project():
    result = uw.plan(project_id="novaretail", reference_images=["/tmp/a.png"],
                     layers={"taste": "warmer"})
    chain = [c["layer"] for c in result["precedence"]["chain"]]
    assert chain.index("project_ui_rules") < chain.index("reference_mockup")
    assert chain.index("reference_mockup") < chain.index("taste")


def test_the_plan_omits_image_to_code_from_the_stage_list_when_skipped():
    without = uw.plan(project_id="mesflow")
    assert "image_to_code" not in without["stages"]
    assert without["image_to_code"]["route"] == "skip"
    with_ref = uw.plan(project_id="mesflow", reference_images=["/tmp/a.png"])
    assert "image_to_code" in with_ref["stages"]


def test_the_plan_names_the_existing_browser_surface_and_does_not_invent_one():
    result = uw.plan(project_id="novaretail")
    assert result["verification"]["actions"] == [
        "browser_verify", "browser_status", "browser_screenshot", "browser_stop",
    ]
    assert len(result["verification"]["viewports"]) == 3


def test_the_generated_document_cannot_disagree_with_the_code():
    doc = uw.policy_document()
    for _, name, _ in uw.PRECEDENCE:
        assert f"`{name}`" in doc
    for profile in uw.PROFILES:
        assert profile.display_name in doc
    assert uw.GATE_FAIL in doc and uw.GATE_FIX_REQUIRED in doc
    assert uw.UI_WORKFLOW_POLICY_VERSION in doc


# -- config ------------------------------------------------------------------

def test_the_config_section_defaults_on_because_it_decides_nothing_dangerous():
    from terminal_mcp.config import UiWorkflowConfig
    config = UiWorkflowConfig()
    assert config.enabled is True
    assert config.installed_skills == ()


def test_a_malformed_skill_list_is_an_error_not_a_silent_empty():
    """A typo there would report every skill missing, which reads as
    'nothing installed' -- the quiet wrong answer this validation prevents."""
    from terminal_mcp.config import _load_ui_workflow_config
    with pytest.raises(ValueError, match="installed_skills"):
        _load_ui_workflow_config({"installed_skills": "taste"})
    with pytest.raises(ValueError, match="installed_skills"):
        _load_ui_workflow_config({"installed_skills": ["taste", ""]})
    with pytest.raises(ValueError, match="enabled"):
        _load_ui_workflow_config({"enabled": "yes"})
    loaded = _load_ui_workflow_config({"installed_skills": [" taste "],
                                       "default_project": " novaretail "})
    assert loaded.installed_skills == ("taste",)
    assert loaded.default_project == "novaretail"


# -- MCP surface -------------------------------------------------------------

def _tool_names():
    from terminal_mcp.mcp_app import build_mcp
    return {t.name for t in asyncio.run(build_mcp().list_tools())}


def test_the_policy_tools_are_registered():
    names = _tool_names()
    assert {"terminal_ui_workflow_plan", "terminal_ui_workflow_audit_gate",
            "terminal_ui_workflow_policy"} <= names


def test_this_feature_did_not_widen_the_compact_browser_surface():
    """The Browser Gateway's public contract is four compact actions and zero
    standalone terminal_browser_* tools. A policy feature must not change
    that, so it is asserted rather than assumed."""
    from terminal_mcp.compact_tools import TURN_HANDLER_ACTIONS

    names = _tool_names()
    assert [n for n in names if n.startswith("terminal_browser_")] == []
    for action in ("browser_verify", "browser_status", "browser_screenshot", "browser_stop"):
        assert action in TURN_HANDLER_ACTIONS or action in str(TURN_HANDLER_ACTIONS), \
            f"{action} must remain on the compact surface"


def test_the_policy_tools_are_not_terminal_turn_actions():
    """Deliberate: terminal_turn is the compact EXECUTION surface. A
    read-only policy query belongs beside it, not inside it."""
    from terminal_mcp.compact_tools import TURN_HANDLER_ACTIONS

    actions = set(TURN_HANDLER_ACTIONS) if not isinstance(TURN_HANDLER_ACTIONS, dict) \
        else set(TURN_HANDLER_ACTIONS)
    assert not any("ui_workflow" in str(a) for a in actions)
