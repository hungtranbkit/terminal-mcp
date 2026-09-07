"""IntegrationEngine -- real-git-repo tests for the merge/test
orchestration engine (task: "3-role model: Coding A/B + Integration
Agent"). Every repo here is a disposable tmp_path fixture -- never a
real OfflinePOS checkout, never `window`/`window2`.

These exercise REAL `git merge`/`git status`/subprocess test commands,
not mocks -- a conflict here is a REAL git conflict, a "failing test"
is a REAL non-zero subprocess exit."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.integration_engine import IntegrationEngine
from terminal_mcp.integration_store import (
    BLOCKED, INTEGRATED, MERGE_READY, REGRESSION_FAILED, REWORK_REQUIRED, TARGETED_TEST, IntegrationStore,
)
from terminal_mcp.queue_store import QueueStore


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "initial"], path)
    _git(["branch", "integration"], path)
    return path


def _commit_on_branch(repo, branch, filename, content, *, base="main", message="feature commit"):
    _git(["checkout", "-q", base], repo)
    _git(["checkout", "-q", "-b", branch], repo, check=False)  # may already exist
    _git(["checkout", "-q", branch], repo)
    (repo / filename).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", message], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    base_sha = _git(["rev-parse", base], repo).stdout.strip()
    _git(["checkout", "-q", "main"], repo)  # leave repo on a neutral branch
    return sha, base_sha


@pytest.fixture
def stores(tmp_path):
    return {
        "integration": IntegrationStore(tmp_path / "integration.db"),
        "queue": QueueStore(tmp_path / "queue.db"),
    }


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def _configure(store, repo_path, **overrides):
    kwargs = {"repo_path": str(repo_path), "targeted_test_command": ["true"],
             "full_regression_command": ["true"], "batch_size": 1}
    kwargs.update(overrides)
    return store.configure_pipeline("proj-a", **kwargs)


def _engine(stores):
    return IntegrationEngine(stores["integration"], stores["queue"])


def _publish_handoff(store, **kwargs):
    # docs_exempt="chore" by default -- every test in this file is
    # exercising merge/conflict/rework/regression/promote MECHANICS, not
    # the living-requirements doc gate (that gate has its own dedicated
    # tests in test_integration_reviewer.py); a disposable a.txt/shared.txt
    # commit has nothing real to document either way.
    kwargs.setdefault("artifacts", {"docs_exempt": "chore"})
    return store.publish_handoff(**kwargs)


# ---------------------------------------------------------------------------
# Clean merge -> targeted test -> INTEGRATED.
# ---------------------------------------------------------------------------

def test_clean_merge_and_passing_targeted_test_reaches_integrated(stores, repo):
    _configure(stores["integration"], repo)
    sha, base_sha = _commit_on_branch(repo, "feature/a", "a.txt", "hello from A\n")
    handoff = _publish_handoff(stores["integration"], 
        project="proj-a", task_id="t1", origin_session="lane-a", branch="feature/a",
        commit_sha=sha, base_sha=base_sha, changed_paths=["a.txt"],
    )
    engine = _engine(stores)

    claim = engine.tick("proj-a")
    assert claim.action == "CLAIMED"
    merge = engine.tick("proj-a")
    assert merge.action == "MERGED"
    assert stores["integration"].get_handoff(handoff.id).status == TARGETED_TEST
    test = engine.tick("proj-a")
    assert test.action == "INTEGRATED"
    final = stores["integration"].get_handoff(handoff.id)
    assert final.status == INTEGRATED
    assert final.merge_commit_sha is not None

    # The file really is on the integration branch now.
    _git(["checkout", "-q", "integration"], repo)
    assert (repo / "a.txt").read_text() == "hello from A\n"


# ---------------------------------------------------------------------------
# Real merge conflict -> REWORK_REQUIRED, routed to the owning session,
# never guessed at.
# ---------------------------------------------------------------------------

def test_real_merge_conflict_routes_rework_to_owning_session(stores, repo):
    _configure(stores["integration"], repo)
    sha_a, base_a = _commit_on_branch(repo, "feature/a", "shared.txt", "version A\n")
    sha_b, base_b = _commit_on_branch(repo, "feature/b", "shared.txt", "version B (conflicting)\n")

    handoff_a = _publish_handoff(stores["integration"], 
        project="proj-a", task_id="t1", origin_session="lane-a", branch="feature/a",
        commit_sha=sha_a, base_sha=base_a, changed_paths=["shared.txt"],
    )
    handoff_b = _publish_handoff(stores["integration"], 
        project="proj-a", task_id="t2", origin_session="lane-b", branch="feature/b",
        commit_sha=sha_b, base_sha=base_b, changed_paths=["shared.txt"],
    )
    engine = _engine(stores)

    # A merges cleanly first (nothing on integration branch touched shared.txt yet).
    engine.tick("proj-a")  # CLAIMED (a)
    engine.tick("proj-a")  # MERGED (a)
    engine.tick("proj-a")  # INTEGRATED (a)
    assert stores["integration"].get_handoff(handoff_a.id).status == INTEGRATED

    # B now genuinely conflicts with what's already on the integration branch.
    engine.tick("proj-a")  # CLAIMED (b)
    merge_result = engine.tick("proj-a")  # attempts merge -> REAL conflict
    assert merge_result.action == "REWORK_REQUIRED"
    handoff_b_final = stores["integration"].get_handoff(handoff_b.id)
    assert handoff_b_final.status == REWORK_REQUIRED
    assert handoff_b_final.conflict_detected is True
    assert "shared.txt" in handoff_b_final.conflict_paths

    # Rework was routed to lane-b (B's OWN origin session), not lane-a,
    # and lane-b can see it as a real, ordinary queued task -- never
    # auto-resolved by guessing content.
    rework_task = stores["queue"].get_task(handoff_b_final.rework_task_id)
    assert rework_task.session == "lane-b"
    assert rework_task.status == "QUEUED"
    assert "REWORK" in rework_task.prompt
    assert "shared.txt" in rework_task.prompt

    # The integration branch itself was never left in a conflicted state
    # -- the merge was cleanly aborted.
    _git(["checkout", "-q", "integration"], repo)
    status = _git(["status", "--porcelain"], repo).stdout
    assert status.strip() == ""  # clean, no leftover conflict markers


def test_conflict_routes_to_a_different_session_via_ownership_override(stores, repo):
    """item 9's own path-based ownership fallback: even though the
    handoff's origin_session is lane-a, a configured session_ownership
    override for this path routes rework to lane-c instead."""
    _configure(stores["integration"], repo, session_ownership={"shared.txt": "lane-c"})
    sha_a, base_a = _commit_on_branch(repo, "feature/a", "shared.txt", "version A\n")
    sha_b, base_b = _commit_on_branch(repo, "feature/b", "shared.txt", "version B\n")
    _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                                          branch="feature/a", commit_sha=sha_a, base_sha=base_a,
                                          changed_paths=["shared.txt"])
    handoff_b = _publish_handoff(stores["integration"], project="proj-a", task_id="t2", origin_session="lane-a",
                                                      branch="feature/b", commit_sha=sha_b, base_sha=base_b,
                                                      changed_paths=["shared.txt"])
    engine = _engine(stores)
    engine.tick("proj-a")
    engine.tick("proj-a")
    engine.tick("proj-a")  # a integrated
    engine.tick("proj-a")  # claim b
    engine.tick("proj-a")  # conflict
    rework_task_id = stores["integration"].get_handoff(handoff_b.id).rework_task_id
    assert stores["queue"].get_task(rework_task_id).session == "lane-c"


# ---------------------------------------------------------------------------
# Failing targeted test -> REWORK_REQUIRED with real evidence.
# ---------------------------------------------------------------------------

def test_failing_targeted_test_command_routes_rework(stores, repo):
    _configure(stores["integration"], repo, targeted_test_command=["grep", "-q", "SHOULD_NOT_EXIST", "{paths}"])
    sha, base_sha = _commit_on_branch(repo, "feature/bad", "bad.txt", "perfectly normal content\n")
    handoff = _publish_handoff(stores["integration"], 
        project="proj-a", task_id="t1", origin_session="lane-a", branch="feature/bad",
        commit_sha=sha, base_sha=base_sha, changed_paths=["bad.txt"],
    )
    engine = _engine(stores)
    engine.tick("proj-a")  # CLAIMED
    merge_result = engine.tick("proj-a")  # MERGED
    assert merge_result.action == "MERGED"
    test_result = engine.tick("proj-a")  # grep finds nothing -> exit 1 -> REWORK_REQUIRED
    assert test_result.action == "REWORK_REQUIRED"
    final = stores["integration"].get_handoff(handoff.id)
    assert final.status == REWORK_REQUIRED
    assert final.targeted_test_result["returncode"] != 0
    rework_task = stores["queue"].get_task(final.rework_task_id)
    assert rework_task.session == "lane-a"
    assert rework_task.metadata["rework_for_handoff"] == handoff.id


# ---------------------------------------------------------------------------
# Restart mid-merge -- idempotent re-merge, never a duplicate commit.
# ---------------------------------------------------------------------------

def test_restart_mid_merge_never_creates_a_duplicate_merge_commit(stores, repo, tmp_path):
    _configure(stores["integration"], repo)
    sha, base_sha = _commit_on_branch(repo, "feature/a", "a.txt", "content\n")
    handoff = _publish_handoff(stores["integration"], 
        project="proj-a", task_id="t1", origin_session="lane-a", branch="feature/a",
        commit_sha=sha, base_sha=base_sha, changed_paths=["a.txt"],
    )
    engine = _engine(stores)
    engine.tick("proj-a")  # CLAIMED
    engine.tick("proj-a")  # MERGED -> TARGETED_TEST
    merged_handoff = stores["integration"].get_handoff(handoff.id)
    assert merged_handoff.status == TARGETED_TEST
    first_merge_commit = merged_handoff.merge_commit_sha

    # Simulate "the process crashed right after the merge committed but
    # before it recorded TARGETED_TEST" -- force back to a state that
    # will be reconciled, with an expired lease.
    with stores["integration"]._connection() as connection:
        connection.execute(
            "UPDATE integration_handoffs SET status = 'MERGING', lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = ?", (handoff.id,))

    integration_store2 = IntegrationStore(tmp_path / "integration.db")
    engine2 = IntegrationEngine(integration_store2, stores["queue"])
    reconciled = integration_store2.reconcile_stale_handoff_claims("proj-a")
    assert handoff.id in reconciled

    engine2.tick("proj-a")  # CLAIMED again
    remerge_result = engine2.tick("proj-a")  # re-merge attempt -- git no-op (already an ancestor)
    assert remerge_result.action == "MERGED"
    second_handoff = integration_store2.get_handoff(handoff.id)
    assert second_handoff.merge_commit_sha == first_merge_commit  # SAME commit, no duplicate

    # And the integration branch's own history has exactly ONE merge
    # commit for this branch, not two.
    _git(["checkout", "-q", "integration"], repo)
    log = _git(["log", "--oneline", "--all"], repo).stdout
    assert log.count("integrate feature/a") == 1


# ---------------------------------------------------------------------------
# Regression batch: pass -> MERGE_READY -> promote; fail -> pipeline paused.
# ---------------------------------------------------------------------------

def test_batch_regression_pass_and_promote_to_main(stores, repo):
    _configure(stores["integration"], repo, batch_size=1)
    sha, base_sha = _commit_on_branch(repo, "feature/a", "a.txt", "content\n")
    _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                                          branch="feature/a", commit_sha=sha, base_sha=base_sha,
                                          changed_paths=["a.txt"])
    engine = _engine(stores)
    engine.tick("proj-a")  # CLAIMED
    engine.tick("proj-a")  # MERGED
    engine.tick("proj-a")  # INTEGRATED

    batch_result = engine.tick("proj-a")  # batch_size=1 reached -> BATCH_CREATED
    assert batch_result.action == "BATCH_CREATED"
    batch_id = batch_result.batch_id
    running = engine.tick("proj-a")
    assert running.action == "REGRESSION_RUNNING"
    passed = engine.tick("proj-a")
    assert passed.action == "MERGE_READY"

    promotion = engine.promote_to_main("proj-a", batch_id)
    assert promotion["action"] == "PROMOTED"
    batch = stores["integration"].get_batch(batch_id)
    assert batch.promoted_to_main is True

    _git(["checkout", "-q", "main"], repo)
    assert (repo / "a.txt").read_text() == "content\n"


def test_batch_regression_failure_pauses_the_whole_pipeline(stores, repo):
    _configure(stores["integration"], repo, batch_size=1, full_regression_command=["false"])
    sha, base_sha = _commit_on_branch(repo, "feature/a", "a.txt", "content\n")
    _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                                          branch="feature/a", commit_sha=sha, base_sha=base_sha,
                                          changed_paths=["a.txt"])
    engine = _engine(stores)
    engine.tick("proj-a")
    engine.tick("proj-a")
    engine.tick("proj-a")  # INTEGRATED
    engine.tick("proj-a")  # BATCH_CREATED
    engine.tick("proj-a")  # REGRESSION_RUNNING
    failed = engine.tick("proj-a")
    assert failed.action == "REGRESSION_FAILED"
    assert stores["integration"].get_pipeline("proj-a")["paused"] is True


def test_auto_promote_disabled_by_default_never_touches_main(stores, repo):
    _configure(stores["integration"], repo, batch_size=1)  # auto_promote_enabled defaults False
    sha, base_sha = _commit_on_branch(repo, "feature/a", "a.txt", "content\n")
    _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                                          branch="feature/a", commit_sha=sha, base_sha=base_sha,
                                          changed_paths=["a.txt"])
    engine = _engine(stores)
    for _ in range(6):
        result = engine.tick("proj-a")
        if result.action == "MERGE_READY":
            break
    assert result.action == "MERGE_READY"  # reached MERGE_READY but did NOT auto-promote
    _git(["checkout", "-q", "main"], repo)
    assert not (repo / "a.txt").exists()  # main untouched


# ---------------------------------------------------------------------------
# Git isolation + Merge Agent checkpoint (§20.4): mechanical-only
# (whitespace-only) conflict auto-resolve, opt-in per project, real git
# behavior throughout -- never a custom content-guessing heuristic.
# ---------------------------------------------------------------------------

def _commit_base_file(repo, filename, content):
    _git(["checkout", "-q", "main"], repo)
    (repo / filename).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "base content"], repo)
    _git(["branch", "-f", "integration", "main"], repo)


def test_whitespace_only_conflict_auto_resolved_when_opted_in(stores, repo):
    # base line has 2-space indentation; branch A changes it to 4-space,
    # branch B changes the SAME line to tab-indentation -- a REAL git
    # conflict (both sides touched the same line), but the two versions
    # differ from each other ONLY in whitespace.
    _commit_base_file(repo, "shared.txt", "function foo() {\n  return 1;\n}\n")
    _configure(stores["integration"], repo, allow_mechanical_conflict_resolution=True)

    sha_a, base_a = _commit_on_branch(repo, "feature/a", "shared.txt",
                                      "function foo() {\n    return 1;\n}\n")
    sha_b, base_b = _commit_on_branch(repo, "feature/b", "shared.txt",
                                      "function foo() {\n\treturn 1;\n}\n")

    handoff_a = _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                                 branch="feature/a", commit_sha=sha_a, base_sha=base_a,
                                 changed_paths=["shared.txt"])
    handoff_b = _publish_handoff(stores["integration"], project="proj-a", task_id="t2", origin_session="lane-b",
                                 branch="feature/b", commit_sha=sha_b, base_sha=base_b,
                                 changed_paths=["shared.txt"])
    engine = _engine(stores)

    engine.tick("proj-a")  # CLAIMED (a)
    engine.tick("proj-a")  # MERGED (a)
    engine.tick("proj-a")  # INTEGRATED (a)
    assert stores["integration"].get_handoff(handoff_a.id).status == INTEGRATED

    engine.tick("proj-a")  # CLAIMED (b)
    merge_result = engine.tick("proj-a")  # attempts merge -> real conflict -> mechanical retry succeeds
    assert merge_result.action == "MERGED"

    handoff_b_final = stores["integration"].get_handoff(handoff_b.id)
    assert handoff_b_final.status == TARGETED_TEST
    assert handoff_b_final.conflict_detected is False  # never recorded as an unresolved conflict
    assert handoff_b_final.artifacts["mechanical_conflict_auto_resolved"] is True
    assert "shared.txt" in handoff_b_final.artifacts["mechanical_conflict_original_paths"]

    # The integration branch is clean, no leftover conflict markers.
    _git(["checkout", "-q", "integration"], repo)
    status = _git(["status", "--porcelain"], repo).stdout
    assert status.strip() == ""
    assert "<<<<<<<" not in (repo / "shared.txt").read_text()


def test_real_content_conflict_still_routes_rework_even_when_opted_in(stores, repo):
    # Genuinely different CONTENT (not just whitespace) on the same
    # line -- the mechanical retry must also conflict, so this still
    # correctly falls through to REWORK_REQUIRED, never silently picking
    # a side.
    _commit_base_file(repo, "shared.txt", "version base\n")
    _configure(stores["integration"], repo, allow_mechanical_conflict_resolution=True)

    sha_a, base_a = _commit_on_branch(repo, "feature/a", "shared.txt", "version A\n")
    sha_b, base_b = _commit_on_branch(repo, "feature/b", "shared.txt", "version B (conflicting)\n")

    handoff_a = _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                                 branch="feature/a", commit_sha=sha_a, base_sha=base_a,
                                 changed_paths=["shared.txt"])
    handoff_b = _publish_handoff(stores["integration"], project="proj-a", task_id="t2", origin_session="lane-b",
                                 branch="feature/b", commit_sha=sha_b, base_sha=base_b,
                                 changed_paths=["shared.txt"])
    engine = _engine(stores)

    engine.tick("proj-a")  # CLAIMED (a)
    engine.tick("proj-a")  # MERGED (a)
    engine.tick("proj-a")  # INTEGRATED (a)

    engine.tick("proj-a")  # CLAIMED (b)
    merge_result = engine.tick("proj-a")
    assert merge_result.action == "REWORK_REQUIRED"
    handoff_b_final = stores["integration"].get_handoff(handoff_b.id)
    assert handoff_b_final.status == REWORK_REQUIRED
    assert handoff_b_final.conflict_detected is True
    assert "mechanical_conflict_auto_resolved" not in handoff_b_final.artifacts

    _git(["checkout", "-q", "integration"], repo)
    assert _git(["status", "--porcelain"], repo).stdout.strip() == ""


def test_mechanical_conflict_resolution_disabled_by_default(stores, repo):
    # Same whitespace-only scenario as above, but WITHOUT opting in --
    # must fall through to REWORK_REQUIRED exactly like before this
    # feature existed (never a silent behavior change for an unconfigured
    # project).
    _commit_base_file(repo, "shared.txt", "function foo() {\n  return 1;\n}\n")
    _configure(stores["integration"], repo)  # allow_mechanical_conflict_resolution defaults False

    sha_a, base_a = _commit_on_branch(repo, "feature/a", "shared.txt",
                                      "function foo() {\n    return 1;\n}\n")
    sha_b, base_b = _commit_on_branch(repo, "feature/b", "shared.txt",
                                      "function foo() {\n\treturn 1;\n}\n")
    _publish_handoff(stores["integration"], project="proj-a", task_id="t1", origin_session="lane-a",
                     branch="feature/a", commit_sha=sha_a, base_sha=base_a, changed_paths=["shared.txt"])
    handoff_b = _publish_handoff(stores["integration"], project="proj-a", task_id="t2", origin_session="lane-b",
                                 branch="feature/b", commit_sha=sha_b, base_sha=base_b,
                                 changed_paths=["shared.txt"])
    engine = _engine(stores)
    engine.tick("proj-a"); engine.tick("proj-a"); engine.tick("proj-a")  # a: claim/merge/integrate
    engine.tick("proj-a")  # CLAIMED (b)
    merge_result = engine.tick("proj-a")
    assert merge_result.action == "REWORK_REQUIRED"
    assert stores["integration"].get_handoff(handoff_b.id).status == REWORK_REQUIRED
