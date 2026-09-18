"""Skill provider resolution: gstack as an option, never a dependency.

The integration's one hard rule is that nothing breaks when gstack is
absent -- it is not installed on this fleet today, and a project that
cannot run without a third-party skill pack has not integrated it, it has
married it. So the absent/unsupported/drifted paths get as much attention
here as the happy one.

The second rule: a skill reports, ProjectFlow decides. Evidence carries
what the skill said; `decides_done` is false, always.
"""
from __future__ import annotations

import pytest

from terminal_mcp.skill_provider import (GSTACK_STAGE_SKILLS, HOSTS, STAGES, STATUS_DRIFT,
                                         STATUS_MISSING, STATUS_READY, STATUS_UNSUPPORTED,
                                         GstackProvider, Resolution, SkillRef, WorkflowPolicy,
                                         evidence_record, resolve_stage)


def _install(root, skills, version="1.84.1.0", *, codex=False):
    """A gstack tree shaped the way its own installer lays one out."""
    root.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        directory = root / (f"gstack-{skill}" if codex else skill)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "SKILL.md").write_text(f"---\nname: {skill}\n---\n", encoding="utf-8")
    if version is not None:
        target = root.parent if codex else root
        (target / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


# -- provider status ---------------------------------------------------------

def test_a_missing_install_reports_missing_not_an_error(tmp_path):
    provider = GstackProvider(claude_root=tmp_path / "nope")
    status = provider.status("claude")
    assert status.status == STATUS_MISSING
    assert not status.usable


def test_a_real_install_reports_ready_with_its_version_and_skills(tmp_path):
    root = _install(tmp_path / "gstack", ["review", "qa", "ship"])
    status = GstackProvider(claude_root=root).status("claude")
    assert status.status == STATUS_READY
    assert status.version == "1.84.1.0"
    assert status.skills == ("qa", "review", "ship")


def test_an_install_older_than_the_pin_is_drift_not_ready(tmp_path):
    # A skill whose behaviour the project has not seen is a different
    # skill; running it anyway puts unattributable findings in evidence.
    root = _install(tmp_path / "gstack", ["review"], version="1.70.0.0")
    status = GstackProvider(minimum_version="1.84.1.0", claude_root=root).status("claude")
    assert status.status == STATUS_DRIFT
    assert "1.70.0.0" in status.detail and "1.84.1.0" in status.detail


def test_a_newer_install_than_the_pin_is_fine(tmp_path):
    root = _install(tmp_path / "gstack", ["review"], version="1.90.0.0")
    assert GstackProvider(minimum_version="1.84.1.0", claude_root=root).status("claude").status == STATUS_READY


def test_an_empty_directory_is_missing_not_ready(tmp_path):
    root = tmp_path / "gstack"
    root.mkdir()
    assert GstackProvider(claude_root=root).status("claude").status == STATUS_MISSING


def test_codex_installs_are_found_under_their_own_prefixed_layout(tmp_path):
    # gstack installs to $CODEX_HOME/skills/gstack-<name>/ for Codex, not
    # the flat layout it uses for Claude.
    root = _install(tmp_path / "skills", ["review", "qa"], codex=True)
    status = GstackProvider(codex_root=root).status("codex")
    assert status.status == STATUS_READY
    assert status.skills == ("qa", "review")


def test_an_unknown_host_is_unsupported(tmp_path):
    assert GstackProvider().status("emacs").status == STATUS_UNSUPPORTED


# -- stage resolution --------------------------------------------------------

def _providers(tmp_path, skills=("plan-eng-review", "review", "qa", "ship", "retro"), **kwargs):
    root = _install(tmp_path / "gstack", list(skills))
    return {"gstack": GstackProvider(claude_root=root, **kwargs)}


def test_a_disabled_stage_resolves_to_nothing(tmp_path):
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"pre_merge_review"}))
    result = resolve_stage(policy, "release", "claude", _providers(tmp_path))
    assert result.skill is None
    assert "not enabled" in result.reason
    assert result.fallback is False      # nothing failed; the project said no


def test_an_enabled_stage_resolves_to_a_real_skill(tmp_path):
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"pre_merge_review"}))
    result = resolve_stage(policy, "pre_merge_review", "claude", _providers(tmp_path))
    assert result.skill == SkillRef(provider="gstack", skill="review", host="claude",
                                    invocation="/review", version="1.84.1.0")


def test_an_enabled_stage_falls_back_cleanly_when_gstack_is_absent(tmp_path):
    # The case that is true on this fleet today.
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"pre_merge_review"}))
    providers = {"gstack": GstackProvider(claude_root=tmp_path / "absent")}
    result = resolve_stage(policy, "pre_merge_review", "claude", providers)
    assert result.skill is None
    assert result.fallback is True
    assert STATUS_MISSING in result.reason


def test_drift_also_falls_back_rather_than_running_an_unpinned_skill(tmp_path):
    root = _install(tmp_path / "gstack", ["review"], version="1.0.0.0")
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"pre_merge_review"}))
    result = resolve_stage(policy, "pre_merge_review", "claude",
                           {"gstack": GstackProvider(minimum_version="1.84.1.0", claude_root=root)})
    assert result.skill is None and result.fallback is True
    assert STATUS_DRIFT in result.reason


def test_a_stage_with_no_gstack_equivalent_falls_back(tmp_path):
    # implementation is deliberately not mapped: the worker does the work.
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"implementation"}))
    result = resolve_stage(policy, "implementation", "claude", _providers(tmp_path))
    assert result.skill is None and result.fallback is True


def test_an_unregistered_provider_falls_back_instead_of_raising(tmp_path):
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"release"}), provider="nope")
    result = resolve_stage(policy, "release", "claude", _providers(tmp_path))
    assert result.skill is None and result.fallback is True


def test_an_unknown_stage_is_a_programming_error(tmp_path):
    with pytest.raises(ValueError):
        resolve_stage(WorkflowPolicy(project="p"), "deploy-to-prod", "claude", {})


def test_a_policy_cannot_enable_a_stage_that_does_not_exist():
    with pytest.raises(ValueError):
        WorkflowPolicy(project="p", enabled_stages=frozenset({"vibes"}))


def test_no_stage_is_enabled_by_default():
    # An integration that every project pays for by default is a tax.
    policy = WorkflowPolicy(project="p")
    assert all(not policy.enabled(stage) for stage in STAGES)


# -- the boundary this integration exists to hold ----------------------------

def test_every_mapped_stage_is_a_real_stage():
    assert set(GSTACK_STAGE_SKILLS) <= set(STAGES)


def test_implementation_is_never_delegated_to_a_skill_pack():
    assert "implementation" not in GSTACK_STAGE_SKILLS


def test_evidence_records_what_the_skill_said_and_never_that_it_is_done(tmp_path):
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"verification"}))
    result = resolve_stage(policy, "verification", "claude", _providers(tmp_path))
    record = evidence_record(result, task_id="T-42", project="pilot",
                             branch="feat/x", commit="deadbeef", outcome="3 findings")
    assert record["skill"] == "qa"
    assert record["task_id"] == "T-42"
    assert record["branch"] == "feat/x" and record["commit"] == "deadbeef"
    assert record["reported_outcome"] == "3 findings"
    # ProjectFlow's verification decides DONE, not the skill.
    assert record["decides_done"] is False


def test_evidence_still_records_a_stage_that_fell_back(tmp_path):
    # A stage that ran without gstack must leave a trace saying so, or the
    # trail silently looks the same as one that never ran.
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"verification"}))
    providers = {"gstack": GstackProvider(claude_root=tmp_path / "absent")}
    result = resolve_stage(policy, "verification", "claude", providers)
    record = evidence_record(result, task_id="T-43", project="pilot")
    assert record["provider"] is None and record["fallback"] is True
    assert record["stage"] == "verification"
    assert record["reason"]


@pytest.mark.parametrize("host", HOSTS)
def test_resolution_is_host_specific(tmp_path, host):
    # Installed for Claude only: Codex must not inherit the answer.
    root = _install(tmp_path / "gstack", ["review"])
    providers = {"gstack": GstackProvider(claude_root=root, codex_root=tmp_path / "no-codex")}
    policy = WorkflowPolicy(project="pilot", enabled_stages=frozenset({"pre_merge_review"}))
    result = resolve_stage(policy, "pre_merge_review", host, providers)
    assert (result.skill is not None) == (host == "claude")
