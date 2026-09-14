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


# -- the registry as the DEFAULT path ----------------------------------------
#
# The registry already knew how to hold a runbook. What decides whether it is
# used is whether calling it is easier than re-deriving the command, so these
# tests are about the cheap path existing at all: an operation name resolves,
# registration happens on the way through, a pass costs one line, and the
# script is handed back only when something is actually wrong with it.

@pytest.fixture()
def agent_repo(tmp_path):
    """A repo whose runbooks live where this project's convention puts them."""
    root = tmp_path / "agentrepo"
    (root / "scripts" / "agent").mkdir(parents=True)
    (root / "terminal_mcp").mkdir()
    (root / "terminal_mcp" / "__init__.py").write_text("")
    (root / "tests").mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    for name, body in (("test-gate.sh", "echo 'PASS stage=pytest 12 passed'\n"),
                       ("smoke.sh", "echo 'PASS stage=routes'\n"),
                       ("deploy-restart.sh", "echo 'PASS stage=restart'\n")):
        (root / "scripts" / "agent" / name).write_text(f"#!/usr/bin/env bash\n{body}")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return pr.ProcedureRegistry(ProjectKnowledge(root), log_dir=tmp_path / "logs")


def test_the_conventional_scripts_register_themselves(agent_repo):
    report = agent_repo.ensure_operations()
    assert report["operations"]["test"]["procedure_id"] == "test_gate"
    assert agent_repo.get("test_gate").command == ["bash", "scripts/agent/test-gate.sh"]
    # Nothing is invented for an operation this repository cannot perform.
    assert "build" in report["absent"]
    assert agent_repo.get("build") is None


def test_what_the_repository_already_runs_wins_over_the_agent_script(agent_repo):
    (agent_repo.root / "Makefile").write_text("test:\n\tpytest -q\n")
    agent_repo.ensure_operations()
    # A second way to run the tests is a way for the two to disagree.
    assert agent_repo.get("test_gate").command == ["make", "test"]


def test_a_declared_procedure_is_never_overwritten_by_discovery(agent_repo):
    agent_repo.register(pr.Procedure(id="test_gate", name="hand written",
                                     command=["bash", "scripts/agent/test-gate.sh"],
                                     depends_on=["terminal_mcp"], risk=pr.RISK_READ_ONLY))
    agent_repo.ensure_operations()
    kept = agent_repo.get("test_gate")
    # The author knew a risk level and a dependency set discovery cannot infer.
    assert kept.name == "hand written" and kept.depends_on == ("terminal_mcp",)


def test_ensuring_twice_keeps_the_green_result_of_the_first(agent_repo):
    agent_repo.ensure_operations()
    agent_repo.run_operation("test")
    before = agent_repo.get("test_gate").last_success_at
    assert agent_repo.ensure_operations()["registered"] == []
    assert agent_repo.get("test_gate").last_success_at == before


def test_the_word_test_runs_the_registered_gate(agent_repo):
    result = agent_repo.run_operation("test")
    assert result.ok is True and result.operation == "test"
    assert result.procedure_id == "test_gate"
    # Nothing had to be registered first: asking is what registers it.
    assert agent_repo.get("test_gate") is not None


@pytest.mark.parametrize("spelling", ["test", "tests", "pytest", "TEST-GATE", "regression"])
def test_the_spelling_a_caller_would_use_resolves(agent_repo, spelling):
    # A caller who gets "unknown" back composes the command by hand, which is
    # the cost this whole module exists to remove.
    assert agent_repo.run_operation(spelling).procedure_id == "test_gate"


def test_an_unknown_operation_names_the_ones_that_exist(agent_repo):
    result = agent_repo.run_operation("deploy-to-mars")
    assert result.ok is False and result.status == pr.UNREGISTERED
    for operation in ("test", "build", "deploy", "smoke", "health"):
        assert operation in result.summary


def test_a_passing_operation_hands_back_one_line_and_no_script(agent_repo):
    payload = agent_repo.run_operation("test").as_context()
    assert payload["line"].startswith("PASS test_gate")
    assert "\n" not in payload["line"]
    # The rule expressed as data: on a pass there is nothing to read.
    assert "inspect" not in payload and "error_excerpt" not in payload
    assert payload["log_path"]


def test_a_failing_operation_points_at_the_failing_region_and_the_script(agent_repo):
    (agent_repo.root / "scripts" / "agent" / "test-gate.sh").write_text(
        "#!/usr/bin/env bash\necho 'FAILED tests/test_x.py::test_y'\nexit 1\n")
    payload = agent_repo.run_operation("test").as_context()
    assert payload["line"].startswith("FAIL test_gate")
    assert "test_x" in payload["error_excerpt"]
    assert payload["inspect"]["script"] == "scripts/agent/test-gate.sh"
    assert payload["inspect"]["log_path"] == payload["log_path"]


def test_a_stale_operation_is_re_run_rather_than_read(agent_repo):
    agent_repo.run_operation("test")
    (agent_repo.root / "terminal_mcp" / "core.py").write_text("x = 1\n")
    result = agent_repo.run_operation("test")
    # Its dependencies moved, so the cached PASS is not reused -- but nobody
    # needs the script to find that out.
    assert result.status == pr.STALE
    assert result.from_cache is False and result.ok is True
    assert result.inspect is None


def test_a_verified_operation_is_reused_not_re_run(agent_repo):
    agent_repo.run_operation("test")
    second = agent_repo.run_operation("test")
    assert second.status == pr.VERIFIED and second.from_cache is True
    assert second.one_line().startswith("PASS test_gate")


def test_a_broken_operation_says_which_script_vanished(agent_repo):
    agent_repo.ensure_operations()
    (agent_repo.root / "scripts" / "agent" / "test-gate.sh").unlink()
    result = agent_repo.run_operation("test")
    assert result.ok is False and result.stage == "lookup"
    assert result.status == pr.BROKEN
    assert "scripts/agent/test-gate.sh" in result.summary


def test_deploy_goes_through_the_registry_but_is_still_not_automatic(agent_repo):
    result = agent_repo.run_operation("deploy")
    # Routed and resolved -- then refused, because a deploy is a decision.
    assert result.procedure_id == "deploy_restart"
    assert result.ok is False and result.stage == "policy"
    assert agent_repo.run_operation("deploy", allow_risky=True).ok is True


def test_a_registered_id_outside_the_operation_table_still_runs(agent_repo):
    agent_repo.register(_gate(id="tunnel_check",
                              command=["bash", "scripts/agent/smoke.sh"],
                              depends_on=[]))
    result = agent_repo.run_operation("tunnel_check")
    assert result.ok is True and result.operation is None


def test_an_undeclared_dependency_set_does_not_cache_across_an_edit(agent_repo):
    # `smoke` is about a running system, so it declares no paths. That must
    # mean "cannot claim freshness", never "always fresh".
    assert agent_repo.run_operation("smoke").ok is True
    assert agent_repo.run_operation("smoke").from_cache is True
    (agent_repo.root / "terminal_mcp" / "routes.py").write_text("ROUTES = []\n")
    assert agent_repo.run_operation("smoke").from_cache is False


def test_a_script_is_looked_for_in_the_REPOSITORY_not_the_callers_directory(registry):
    # This path exists in the repo the test process happens to run from, and
    # nowhere in the repo under test. Resolving it against the caller's own
    # working directory would report a vanished script as present -- the one
    # mistake a BROKEN status exists to catch.
    registry.register(_gate(id="elsewhere",
                            command=["bash", "scripts/agent/test-gate.sh"]))
    assert registry.list()[0]["script_exists"] is False
    assert registry.status_of(registry.get("elsewhere")) == pr.BROKEN
