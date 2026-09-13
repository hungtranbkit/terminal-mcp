"""Procedural memory: the runbook registry, its cache and its output contract.

The behaviour that matters is token economy with honesty. A settled operation
should cost one line of context when it passes, and only a failing run should
be allowed to spend more.
"""

from __future__ import annotations

import subprocess

import pytest

from terminal_mcp import procedures as pr
from terminal_mcp.project_knowledge import ProjectKnowledge


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture()
def registry(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    (root / "src.py").write_text("x = 1\n")
    (root / "scripts" / "gate.sh").write_text("#!/usr/bin/env bash\necho 'PASS gate'\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return pr.ProcedureRegistry(ProjectKnowledge(root), log_dir=tmp_path / "logs")


def _gate(**kwargs):
    base = dict(id="test_gate", name="test gate",
                command=["bash", "scripts/gate.sh"], depends_on=["src.py"],
                risk=pr.RISK_READ_ONLY)
    base.update(kwargs)
    return pr.Procedure(**base)


# -- reuse before creation ---------------------------------------------------

def test_discovery_finds_what_a_repo_already_has(tmp_path):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "Makefile").write_text("test:\n\tpytest\n")
    (root / "scripts" / "smoke.sh").write_text("#!/bin/sh\n")
    found = {item["id"]: item for item in pr.discover_existing(root)}
    # Reusing what humans already run beats adding a second path that drifts.
    assert found["test_gate"]["command"] == ["make", "test"]
    assert found["smoke"]["via"] == "scripts/smoke.sh"


def test_discovery_does_not_claim_a_make_target_that_is_absent(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Makefile").write_text("build:\n\tgo build\n")
    ids = {item["id"] for item in pr.discover_existing(root)}
    assert "build" in ids and "test_gate" not in ids


def test_discovery_of_an_empty_repo_finds_nothing(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    assert pr.discover_existing(root) == []


# -- registration ------------------------------------------------------------

def test_register_and_look_up(registry):
    registry.register(_gate())
    loaded = registry.get("test_gate")
    assert loaded.command == ["bash", "scripts/gate.sh"]
    assert loaded.depends_on == ("src.py",)


def test_registering_again_keeps_an_existing_green_result(registry):
    registry.register(_gate())
    registry.run("test_gate")
    before = registry.get("test_gate")
    assert before.last_success_at
    registry.register(_gate())      # same metadata, re-registered
    after = registry.get("test_gate")
    # Losing this would send the next caller off to re-run a passing gate.
    assert after.last_success_at == before.last_success_at


def test_the_registry_stores_metadata_not_script_bodies(registry):
    registry.register(_gate())
    stored = registry.state_path.read_text()
    assert "scripts/gate.sh" in stored
    assert "#!/usr/bin/env bash" not in stored


def test_a_command_carrying_a_secret_is_refused(registry):
    with pytest.raises(Exception) as excinfo:
        registry.register(_gate(id="leaky", command=["bash", "deploy.sh",
                                                     "--token=ghp_" + "a" * 36]))
    assert "environment" in str(excinfo.value).lower()


# -- output contract ---------------------------------------------------------

def test_a_passing_run_costs_one_line_and_no_excerpt(registry):
    registry.register(_gate())
    result = registry.run("test_gate")
    assert result.ok is True
    assert result.error_excerpt == ""     # success needs no log in context
    line = result.one_line()
    assert line.startswith("PASS test_gate")
    assert "\n" not in line


def test_a_failing_run_returns_the_failing_region_only(registry, tmp_path):
    script = registry.root / "scripts" / "boom.sh"
    noise = "\n".join(f"echo line {i}" for i in range(80))
    script.write_text(f"#!/usr/bin/env bash\n{noise}\necho 'ERROR: schema mismatch'\nexit 1\n")
    registry.register(_gate(id="boom", command=["bash", "scripts/boom.sh"]))
    result = registry.run("boom")
    assert result.ok is False
    assert "schema mismatch" in result.summary
    assert "schema mismatch" in result.error_excerpt
    # The failing window, not the whole transcript.
    assert result.error_excerpt.count("\n") < 40
    assert "line 0" not in result.error_excerpt
    assert result.log_path        # the rest stays on disk, addressable


def test_the_one_line_marks_failure_plainly(registry):
    registry.register(_gate(id="boom", command=["bash", "scripts/missing.sh"]))
    assert registry.run("boom").one_line().startswith("FAIL boom")


# -- caching -----------------------------------------------------------------

def test_a_green_result_is_reused_at_the_same_fingerprint(registry):
    registry.register(_gate())
    first = registry.run("test_gate")
    second = registry.run("test_gate")
    assert first.from_cache is False
    # It did not become false because somebody asked again.
    assert second.from_cache is True and second.ok is True


def test_editing_a_dependency_invalidates_the_cache(registry):
    registry.register(_gate())
    registry.run("test_gate")
    (registry.root / "src.py").write_text("x = 2\n")
    assert registry.run("test_gate").from_cache is False


def test_editing_an_unrelated_file_does_not_invalidate_the_cache(registry):
    registry.register(_gate())
    registry.run("test_gate")
    (registry.root / "unrelated.md").write_text("notes\n")
    assert registry.run("test_gate").from_cache is True


def test_extra_args_are_a_different_question_and_bypass_the_cache(registry):
    registry.register(_gate())
    registry.run("test_gate")
    assert registry.run("test_gate", extra_args=["-k", "x"]).from_cache is False


def test_a_failure_is_never_cached_as_green(registry):
    (registry.root / "scripts" / "boom.sh").write_text("#!/usr/bin/env bash\nexit 3\n")
    registry.register(_gate(id="boom", command=["bash", "scripts/boom.sh"],
                            depends_on=["src.py"]))
    assert registry.run("boom").ok is False
    assert registry.run("boom").from_cache is False


# -- risk --------------------------------------------------------------------

def test_a_production_procedure_is_never_invoked_automatically(registry):
    registry.register(_gate(id="deploy_prod", risk=pr.RISK_PRODUCTION))
    result = registry.run("deploy_prod")
    assert result.ok is False
    assert result.stage == "policy"
    assert "approval" in result.summary


def test_an_explicit_approval_lets_a_risky_procedure_run(registry):
    registry.register(_gate(id="deploy_prod", risk=pr.RISK_PRODUCTION))
    assert registry.run("deploy_prod", allow_risky=True).ok is True


def test_preview_is_auto_invokable_but_staging_and_production_are_not():
    assert pr.RISK_PREVIEW in pr.AUTO_INVOKABLE_RISK
    assert pr.RISK_STAGING not in pr.AUTO_INVOKABLE_RISK
    assert pr.RISK_PRODUCTION not in pr.AUTO_INVOKABLE_RISK


# -- honesty about staleness -------------------------------------------------

def test_a_missing_script_fails_lookup_instead_of_pretending(registry):
    registry.register(_gate(id="ghost", command=["bash", "scripts/ghost.sh"]))
    result = registry.run("ghost")
    assert result.ok is False and result.stage == "lookup"
    assert "missing" in result.summary


def test_an_unregistered_procedure_says_so(registry):
    result = registry.run("never_registered")
    assert result.ok is False and "no procedure registered" in result.summary


def test_listing_reports_current_status_from_disk(registry):
    registry.register(_gate())
    registry.register(_gate(id="ghost", command=["bash", "scripts/ghost.sh"]))
    listed = {item["id"]: item for item in registry.list()}
    assert listed["test_gate"]["script_exists"] is True
    assert listed["ghost"]["script_exists"] is False


# -- shared definitions vs local evidence ------------------------------------

def test_definitions_are_shareable_but_run_evidence_is_not(registry):
    registry.register(_gate())
    registry.run("test_gate")
    shared = registry.state_path.read_text()
    # What belongs in a repository: what the procedure IS.
    assert "scripts/gate.sh" in shared
    # What must never travel with it: where and when it happened to pass.
    for local_only in ("last_success_at", "last_verified_commit", "fingerprint",
                       "log_path", str(registry.log_dir)):
        assert local_only not in shared


def test_run_evidence_lands_in_machine_local_state(registry):
    registry.register(_gate())
    registry.run("test_gate")
    assert registry.runs_path.exists()
    evidence = registry.runs_path.read_text()
    assert "last_success_at" in evidence and "fingerprint" in evidence
    # Not inside the repository.
    assert str(registry.root) not in str(registry.runs_path)


def test_two_clones_on_one_host_do_not_share_results(tmp_path):
    import subprocess

    from terminal_mcp.project_knowledge import ProjectKnowledge

    paths = []
    for name in ("clone_a", "clone_b"):
        root = tmp_path / name
        (root / "scripts").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "scripts" / "gate.sh").write_text("#!/usr/bin/env bash\necho ok\n")
        paths.append(pr.ProcedureRegistry(ProjectKnowledge(root),
                                          log_dir=tmp_path / "logs").runs_path)
    # A green result in one clone is not evidence about the other.
    assert paths[0] != paths[1]


def test_a_clone_without_local_evidence_reports_unverified(registry):
    registry.register(_gate())
    registry.run("test_gate")
    assert registry.get("test_gate").last_success_at
    # Simulate a fresh machine: definitions present, no local run history.
    registry.runs_path.unlink()
    assert registry.get("test_gate").last_success_at is None
    assert registry.get("test_gate").command == ["bash", "scripts/gate.sh"]
    # And nothing is reused from a result this machine never produced.
    assert registry.run("test_gate").from_cache is False


def test_corrupt_local_evidence_means_nothing_has_run_here(registry):
    registry.register(_gate())
    registry.run("test_gate")
    registry.runs_path.write_text("{not json")
    # Regenerable, so it degrades rather than taking the registry down.
    assert registry.get("test_gate").last_success_at is None
    assert registry.list()[0]["script_exists"] is True
