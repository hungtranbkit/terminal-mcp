"""The canonical Work Policy: bootstrap, subset loading, overrides, bindings.

These tests encode the acceptance criteria for the policy itself -- above all
that a fresh session knows the rules WITHOUT being told them, which is the
entire reason the policy is a file rather than a habit.
"""

from __future__ import annotations

import subprocess

import pytest

from terminal_mcp import work_policy as wp


@pytest.fixture()
def project(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return str(tmp_path)


def test_bootstrap_writes_canonical_policy(project):
    result = wp.ensure_policy_file(project)
    assert result["written"] is True
    assert result["version"] == wp.WORK_POLICY_VERSION
    text = (wp.policy_dir(project) / wp.POLICY_FILENAME).read_text()
    assert wp.parse_version(text) == wp.WORK_POLICY_VERSION


def test_bootstrap_never_clobbers_an_edited_policy(project):
    wp.ensure_policy_file(project)
    path = wp.policy_dir(project) / wp.POLICY_FILENAME
    path.write_text(path.read_text() + "\n## Local Rule\n\nDeploy only on weekdays.\n")
    again = wp.ensure_policy_file(project)
    assert again["written"] is False
    # The deliberate edit survives: silently restoring the shipped text would
    # delete a decision someone made on purpose.
    assert "Deploy only on weekdays." in path.read_text()
    assert "local_rule" in wp.load_policy(project).sections


def test_every_required_rule_has_a_section(project):
    wp.ensure_policy_file(project)
    policy = wp.load_policy(project)
    for key in wp.SECTION_KEYS:
        assert key in policy.sections, f"policy is missing the {key!r} section"
        assert policy.sections[key].strip()


def test_policy_states_precedence_and_keeps_guards_above_instruction(project):
    wp.ensure_policy_file(project)
    precedence = wp.load_policy(project).section("precedence")
    assert precedence.index("Current code") < precedence.index("user instruction")
    assert precedence.index("user instruction") < precedence.index("WORK_POLICY")
    assert precedence.index("WORK_POLICY") < precedence.index("Knowledge Map")
    assert precedence.index("Knowledge Map") < precedence.index("Historical memory")
    # An instruction does not dissolve a release guard.
    assert "does not remove the guard" in precedence


def test_subset_load_is_much_smaller_than_the_whole_policy(project):
    wp.ensure_policy_file(project)
    policy = wp.load_policy(project)
    subset = policy.load(["modes", "file_budget"])
    assert "FAST_FIX" in subset and "search rounds" in subset
    assert "Telemetry" not in subset
    # The point of subset loading is the saving; assert it actually saves.
    assert len(subset) < len(policy.text) / 3


def test_subset_load_names_sections_it_could_not_find(project):
    wp.ensure_policy_file(project)
    subset = wp.load_policy(project).load(["modes", "no_such_section"])
    assert "no_such_section" in subset  # never silently omitted


def test_new_task_binds_the_new_version_after_a_bump(project):
    wp.ensure_policy_file(project)
    first = wp.load_policy(project).binding()
    path = wp.policy_dir(project) / wp.POLICY_FILENAME
    path.write_text(path.read_text().replace(
        f"WORK_POLICY_VERSION: {wp.WORK_POLICY_VERSION}", "WORK_POLICY_VERSION: 1.1.0", 1))
    second = wp.load_policy(project).binding()
    assert first.policy_version == wp.WORK_POLICY_VERSION
    assert second.policy_version == "1.1.0"
    assert first.policy_hash != second.policy_hash


def test_a_running_task_is_not_silently_reruled_midflight(project):
    wp.ensure_policy_file(project)
    binding = wp.load_policy(project).binding(sections_loaded=["modes"])
    assert wp.binding_is_stale(binding, wp.load_policy(project)) == {
        "stale": False, "action": "continue"}
    path = wp.policy_dir(project) / wp.POLICY_FILENAME
    path.write_text(path.read_text().replace("WORK_POLICY_VERSION: 1.0.0",
                                             "WORK_POLICY_VERSION: 2.0.0", 1))
    verdict = wp.binding_is_stale(binding, wp.load_policy(project))
    assert verdict["stale"] is True
    # Reported, not applied: the worker planned against the bound version.
    assert verdict["action"] == "continue_under_bound_version"
    assert verdict["bound_version"] == "1.0.0"
    assert verdict["current_version"] == "2.0.0"


def test_project_override_applies_only_its_delta(project):
    wp.ensure_policy_file(project)
    (wp.policy_dir(project) / wp.PROJECT_POLICY_FILENAME).write_text(
        "<!-- WORK_POLICY_VERSION: 1.0.0-proj.1 -->\n"
        "## Release Levels\n\nPRODUCTION also requires a fresh database backup.\n")
    policy = wp.load_policy(project)
    assert policy.override_present is True
    assert policy.override_version == "1.0.0-proj.1"   # suffix preserved
    assert policy.override_sections == ("release",)
    assert "fresh database backup" in policy.section("release")
    # Everything not named by the delta is inherited, not lost.
    assert "FAST_FIX" in policy.section("modes")
    assert len(policy.sections) >= len(wp.SECTION_KEYS)


def test_override_that_duplicates_canonical_text_is_called_out(project):
    wp.ensure_policy_file(project)
    canonical = wp.load_policy(project).section("modes")
    (wp.policy_dir(project) / wp.PROJECT_POLICY_FILENAME).write_text(
        "<!-- WORK_POLICY_VERSION: 1.0.0-dup -->\n" + canonical)
    drift = wp.load_policy(project).drift
    assert any("duplicates canonical text" in d for d in drift)


def test_ordinary_sessions_are_not_governed_by_work_policy(project):
    wp.ensure_policy_file(project)
    result = wp.policy_for_task(project, session="m1")
    assert result["applies"] is False
    assert result["reason"] == "NOT_WORK_SESSION"
    assert "binding" not in result and "text" not in result


def test_work_sessions_auto_load_without_being_told_the_rules(project):
    wp.ensure_policy_file(project)
    result = wp.policy_for_task(project, session="uidemo-work",
                                sections=["modes", "needs_redefine"])
    assert result["applies"] is True
    # A brand-new session learns these from the file, not from chat history.
    assert "FAST_FIX" in result["text"] and "NEEDS_REDEFINE" in result["text"]
    assert result["binding"]["policy_version"] == wp.WORK_POLICY_VERSION
    assert result["binding"]["sections_loaded"] == ["modes", "needs_redefine"]


def test_a_project_without_a_policy_file_still_gets_rules(tmp_path):
    policy = wp.load_policy(str(tmp_path))
    assert policy.source == "built-in"
    assert policy.version == wp.WORK_POLICY_VERSION
    assert "FAST_FIX" in policy.section("modes")
    # The fallback is reported rather than disguised as a project policy.
    assert any("built-in policy" in d for d in policy.drift)


def test_policy_outside_a_repository_degrades_instead_of_raising(tmp_path):
    assert wp.policy_dir(str(tmp_path)) is None
    assert wp.ensure_policy_file(str(tmp_path))["reason"] == "NOT_A_GIT_REPOSITORY"
    assert wp.load_policy("/nonexistent-path-xyz").source == "built-in"


def test_version_marker_keeps_prerelease_and_build_suffixes():
    assert wp.parse_version("WORK_POLICY_VERSION: 2.3.4") == "2.3.4"
    assert wp.parse_version("WORK_POLICY_VERSION: 1.0.0-offlinepos.1") == "1.0.0-offlinepos.1"
    assert wp.parse_version("WORK_POLICY_VERSION: 1.0.0+build7") == "1.0.0+build7"
    assert wp.parse_version("nothing here") is None


def test_policy_carries_no_secrets_only_env_var_names(project):
    wp.ensure_policy_file(project)
    text = wp.load_policy(project).text
    lowered = text.lower()
    for forbidden in ("-----begin", "password:", "bearer ", "api_key=", "token="):
        assert forbidden not in lowered
    assert "environment variable names only" in lowered


def test_policy_text_survives_the_knowledge_secret_scrubber():
    from terminal_mcp.project_knowledge import scrub_knowledge
    # The scrubber REFUSES rather than strips, so this also proves the
    # shipped policy contains nothing that looks like a credential.
    assert scrub_knowledge(wp.CANONICAL_POLICY, where="WORK_POLICY.md")
