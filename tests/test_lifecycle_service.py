"""Lifecycle Close-Loop V1 -- the three edges, and their guards.

These are the tests the codebase did not have: every stage had its own
suite, and nothing asserted a TRANSITION between two stages. Real SQLite
stores and real `git` subprocess calls throughout -- the cleanup guards in
particular are only meaningful against a real worktree, since what they
assert is what git itself reports about dirtiness and ancestry.
"""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.integration_store import IntegrationStore, MERGE_READY, REGRESSION_RUNNING
from terminal_mcp.lifecycle_service import LifecycleService
from terminal_mcp.lifecycle_store import CLEANUP_CLEANED, LifecycleStore, release_request_key
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.release_service import ReleaseService
from terminal_mcp.release_store import (
    DEPLOYED, DEPLOYING, RELEASE_CANDIDATE, ReleaseStore, VERIFIED_PROD,
)
from terminal_mcp.verify_queue import NEEDS_REWORK, VerifyQueue

SESSION = "lifecycle-lane"
PROJECT = "proj-lifecycle"
GOOD_EVIDENCE = {"command": "pytest -q", "exit_code": 0, "test_results": "12 passed"}


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


# -- fixtures ------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@e.com"], path)
    _git(["config", "user.name", "T"], path)
    (path / "README.md").write_text("trunk\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "initial"], path)
    return path


@pytest.fixture
def rig(tmp_path, repo):
    """One wired lifecycle service over real, isolated stores."""
    queue = QueueService(store=QueueStore(tmp_path / "queue.db"))
    integration = IntegrationStore(tmp_path / "integration.db")
    release = ReleaseService(ReleaseStore(tmp_path / "release.db"))
    service = LifecycleService(
        queue=queue, integration=integration, release=release,
        store=LifecycleStore(tmp_path / "lifecycle.db"),
        worktree_roots=(str(tmp_path / "trees"),),
    )
    queue.verify_queue.on_verified_pass = service.on_verified_pass
    return service


def _integration_spec(branch="task/x", commit="c" * 40, base="b" * 40):
    return {"project": PROJECT, "branch": branch, "commit_sha": commit,
            "base_sha": base, "changed_paths": ["a.py"]}


def _running_task(store: QueueStore, *, metadata=None, title="impl"):
    ids = store.set_tasks(SESSION, [{"title": title, "prompt": "work",
                                     "project_id": PROJECT, "metadata": metadata or {}}])
    store.transition_task(ids[0], qs.DISPATCHING, event_type="DISPATCHING")
    return store.transition_task(ids[0], qs.RUNNING, event_type="RUNNING")


def _pass_verification(queue: QueueService, task):
    verify: VerifyQueue = queue.verify_queue
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1")
    verify.start(claimed.id, claimed.claim_token)
    return verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)


# -- edge 1: VERIFIED_PASS -> handoff ------------------------------------

def test_verify_pass_publishes_handoff_once(rig):
    task = _running_task(rig.queue.store, metadata={"integration_required": _integration_spec()})

    result = _pass_verification(rig.queue, task)
    assert result["ok"] is True

    handoffs = rig.integration.list_handoffs(PROJECT)
    assert len(handoffs) == 1
    assert handoffs[0].task_id == task.id
    assert handoffs[0].status == "READY_FOR_INTEGRATION"

    # The hook is not the only caller -- reconcile must agree it is done.
    again = rig.ensure_handoff_for_task(task.id)
    assert again["action"] == "ALREADY_PUBLISHED"
    assert len(rig.integration.list_handoffs(PROJECT)) == 1


def test_needs_rework_does_not_publish_handoff(rig):
    task = _running_task(rig.queue.store, metadata={"integration_required": _integration_spec()})
    verify = rig.queue.verify_queue
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1")
    verify.start(claimed.id, claimed.claim_token)

    verify.fail(job.id, claimed.claim_token, result=NEEDS_REWORK,
                failure_summary={"reason": "tests fail", "detail": "3 failed"})

    assert rig.integration.list_handoffs(PROJECT) == []
    # And a reconcile pass must not invent one either.
    assert rig.reconcile()["counts"]["handoffs"] == 0
    assert rig.integration.list_handoffs(PROJECT) == []


def test_task_without_integration_opt_in_is_skipped(rig):
    task = _running_task(rig.queue.store, metadata={})
    _pass_verification(rig.queue, task)
    assert rig.integration.list_handoffs(PROJECT) == []
    assert rig.ensure_handoff_for_task(task.id)["reason"] == "SKIPPED_NOT_OPTED_IN"


def test_reconcile_publishes_a_handoff_the_hook_missed(tmp_path, rig):
    """The repair path: a PASS recorded while no hook was wired at all,
    exactly what a restart or an unconfigured deployment produces."""
    rig.queue.verify_queue.on_verified_pass = None
    task = _running_task(rig.queue.store, metadata={"integration_required": _integration_spec()})
    _pass_verification(rig.queue, task)
    assert rig.integration.list_handoffs(PROJECT) == []

    summary = rig.reconcile()
    assert summary["counts"]["handoffs"] == 1
    assert summary["handoffs"][0]["action"] == "PUBLISHED"
    assert len(rig.integration.list_handoffs(PROJECT)) == 1

    # Second pass is a pure no-op, not a second handoff.
    assert rig.reconcile()["counts"]["handoffs"] == 0
    assert len(rig.integration.list_handoffs(PROJECT)) == 1


def test_unverified_completion_records_a_bypass_event(tmp_path, rig):
    """Legacy path stays working but must announce itself."""
    from terminal_mcp.event_bus import EventBus

    rig.events = EventBus(tmp_path / "events.db")
    task = _running_task(rig.queue.store, metadata={"integration_required": _integration_spec()})

    outcome = rig.on_task_completed(task)
    assert outcome["action"] == "PUBLISHED"
    types = [event["type"] for event in rig.events.list_events(limit=50)]
    assert "VERIFICATION_BYPASSED" in types


def test_unverified_completion_can_be_refused_outright(rig):
    rig.allow_unverified_integration = False
    task = _running_task(rig.queue.store, metadata={"integration_required": _integration_spec()})
    outcome = rig.on_task_completed(task)
    assert outcome["reason"] == "UNVERIFIED_INTEGRATION_DISABLED"
    assert rig.integration.list_handoffs(PROJECT) == []


def test_completion_defers_to_the_verdict_when_a_verify_job_exists(rig):
    task = _running_task(rig.queue.store, metadata={"integration_required": _integration_spec()})
    rig.queue.verify_queue.ensure_verify_job(task)
    outcome = rig.on_task_completed(task)
    assert outcome["action"] == "DEFERRED_TO_VERIFY"
    assert rig.integration.list_handoffs(PROJECT) == []


# -- edge 2: promoted -> release -----------------------------------------

def _promoted_batch(rig, *, task, main_sha, promote=True):
    handoff = rig.integration.publish_handoff(
        project=PROJECT, task_id=task.id, origin_session=SESSION, branch="task/x",
        commit_sha="c" * 40, base_sha="b" * 40)
    batch = rig.integration.create_batch(PROJECT, [handoff.id])
    rig.integration.transition_batch(batch.id, REGRESSION_RUNNING, event_type="REGRESSION_STARTED")
    rig.integration.transition_batch(batch.id, MERGE_READY, event_type="REGRESSION_PASSED")
    if promote:
        rig.integration.promote_batch(batch.id, main_commit_sha=main_sha)
    return handoff, batch


def test_promote_creates_release_once(rig):
    task = _running_task(rig.queue.store)
    main_sha = "a" * 40
    _handoff, batch = _promoted_batch(rig, task=task, main_sha=main_sha)

    first = rig.on_promoted(PROJECT, batch.id, main_sha)
    assert first["releases"][0]["action"] == "CREATED"
    releases = rig.release.store.list_releases(project=PROJECT)
    assert len(releases) == 1
    assert releases[0].task_id == task.id
    assert releases[0].artifact_ref == main_sha
    assert releases[0].status == "MERGED"

    second = rig.on_promoted(PROJECT, batch.id, main_sha)
    assert second["releases"][0]["action"] == "ALREADY_EXISTS"
    assert len(rig.release.store.list_releases(project=PROJECT)) == 1


def test_release_is_not_created_for_an_unpromoted_batch(rig):
    task = _running_task(rig.queue.store)
    _handoff, batch = _promoted_batch(rig, task=task, main_sha="a" * 40, promote=False)

    outcome = rig.ensure_releases_for_batch(PROJECT, batch.id, main_commit_sha="a" * 40)
    assert outcome["reason"] == "BATCH_NOT_PROMOTED"
    assert rig.release.store.list_releases(project=PROJECT) == []
    assert rig.reconcile()["counts"]["releases"] == 0


def test_restart_reconciles_promoted_without_release_without_double_promote(rig):
    """The crash window: git moved and promote_batch committed, but the
    process died before the release INSERT. Reconcile must create exactly
    the missing release and must not re-promote anything."""
    task = _running_task(rig.queue.store)
    main_sha = "a" * 40
    _handoff, batch = _promoted_batch(rig, task=task, main_sha=main_sha)
    assert rig.release.store.list_releases(project=PROJECT) == []

    promoted_at = rig.integration.get_batch(batch.id).promoted_at
    summary = rig.reconcile()

    assert summary["counts"]["releases"] == 1
    releases = rig.release.store.list_releases(project=PROJECT)
    assert len(releases) == 1
    assert releases[0].request_key == release_request_key(task.id, main_sha)
    # Nothing re-promoted: the batch's own promotion record is untouched.
    after = rig.integration.get_batch(batch.id)
    assert after.promoted_at == promoted_at
    assert after.main_commit_sha == main_sha

    # A second reconcile creates nothing further.
    rig.reconcile()
    assert len(rig.release.store.list_releases(project=PROJECT)) == 1


def test_lifecycle_request_key_retry_is_noop(rig):
    """A replayed request key returns the first outcome and performs no
    second side effect -- proven at the store level, where the guard is."""
    task = _running_task(rig.queue.store)
    main_sha = "a" * 40
    _handoff, batch = _promoted_batch(rig, task=task, main_sha=main_sha)
    rig.ensure_releases_for_batch(PROJECT, batch.id, main_commit_sha=main_sha)

    key = release_request_key(task.id, main_sha)
    assert rig.store.claim(key, kind="release") is False, "a settled key must not be re-claimable"
    assert rig.store.result_for(key)["action"] == "CREATED"

    # Even a caller that bypasses the service entirely cannot duplicate:
    # the UNIQUE index returns the existing row instead of inserting.
    duplicate = rig.release.store.create_release(
        project=PROJECT, task_id=task.id, environment="dev", artifact_ref=main_sha, request_key=key)
    assert duplicate.id == rig.release.store.find_by_request_key(key).id
    assert len(rig.release.store.list_releases(project=PROJECT)) == 1


# -- edge 3: VERIFIED_PROD -> cleanup ------------------------------------

def _isolated_task_at_verified_prod(rig, tmp_path, repo, *, branch="task/reapme",
                                    merge_to_main=True, dirty=False):
    """A task with a REAL worktree whose branch really landed on main, and
    a release driven through the real state machine to VERIFIED_PROD."""
    from terminal_mcp import git_worktree

    worktree_path = tmp_path / "trees" / branch.replace("/", "-")
    created = git_worktree.create_worktree(str(repo), branch, None, str(worktree_path))
    assert "error" not in created, created

    (worktree_path / "feature.txt").write_text("feature\n")
    _git(["add", "."], worktree_path)
    _git(["commit", "-q", "-m", "feature work"], worktree_path)

    if merge_to_main:
        _git(["merge", "-q", "--no-ff", "-m", "land it", branch], repo)
    if dirty:
        (worktree_path / "scratch.txt").write_text("uncommitted\n")

    task = _running_task(rig.queue.store, metadata={"git_isolation": {
        "repo_path": str(repo), "branch": branch, "base_sha": created["base_sha"],
        "worktree_path": str(worktree_path), "main_branch": "main"}})

    release = rig.release.store.create_release(
        project=PROJECT, task_id=task.id, environment="dev", artifact_ref="a" * 40)
    for status in (RELEASE_CANDIDATE, DEPLOYING, DEPLOYED, VERIFIED_PROD):
        rig.release.store.transition_release(release.id, status)
    return task, release, worktree_path


def test_verified_prod_reaps_clean_worktree_and_local_branch(rig, tmp_path, repo):
    task, release, worktree_path = _isolated_task_at_verified_prod(rig, tmp_path, repo)

    result = rig.cleanup_release(release.id)

    assert result["action"] == "CLEANED", result
    assert not worktree_path.exists()
    assert _git(["rev-parse", "--verify", "refs/heads/task/reapme"], repo,
                check=False).returncode != 0
    assert rig.store.get_cleanup(task.id)["outcome"] == CLEANUP_CLEANED

    # Idempotent, and it must not DOWNGRADE its own provenance: a second
    # call reports ALREADY_CLEANED rather than re-running the guards (which
    # would now say WORKTREE_MISSING) and overwriting the CLEANED row.
    again = rig.cleanup_release(release.id)
    assert again["action"] == "ALREADY_CLEANED"
    assert rig.store.get_cleanup(task.id)["outcome"] == CLEANUP_CLEANED


def test_cleanup_rejects_dirty_worktree(rig, tmp_path, repo):
    _task, release, worktree_path = _isolated_task_at_verified_prod(
        rig, tmp_path, repo, branch="task/dirty", dirty=True)

    result = rig.cleanup_release(release.id)

    assert result["action"] == "BLOCKED"
    assert result["reason"] == "WORKTREE_DIRTY"
    assert worktree_path.exists(), "a dirty worktree must survive"
    assert (worktree_path / "scratch.txt").exists()


def test_cleanup_rejects_unmerged_branch(rig, tmp_path, repo):
    _task, release, worktree_path = _isolated_task_at_verified_prod(
        rig, tmp_path, repo, branch="task/unmerged", merge_to_main=False)

    result = rig.cleanup_release(release.id)

    assert result["action"] == "BLOCKED"
    assert result["reason"] == "BRANCH_NOT_MERGED"
    assert worktree_path.exists()
    assert _git(["rev-parse", "--verify", "refs/heads/task/unmerged"], repo,
                check=False).returncode == 0


def test_cleanup_rejects_active_owner(rig, tmp_path, repo):
    """A live session sitting inside the worktree is ownership evidence,
    even when everything else says the work is finished."""
    task, release, worktree_path = _isolated_task_at_verified_prod(
        rig, tmp_path, repo, branch="task/owned")

    class _Registry:
        def list(self):
            return [type("R", (), {"session_name": "hp3-work", "cwd": str(worktree_path)})()]

    rig.session_registry = _Registry()
    result = rig.cleanup_release(release.id)

    assert result["action"] == "BLOCKED"
    assert result["reason"] == "ACTIVE_OWNER"
    assert "hp3-work" in result["detail"]
    assert worktree_path.exists()


def test_cleanup_rejects_a_path_outside_the_configured_roots(rig, tmp_path, repo):
    task, release, worktree_path = _isolated_task_at_verified_prod(rig, tmp_path, repo)
    rig.worktree_roots = (str(tmp_path / "somewhere-else"),)

    result = rig.cleanup_release(release.id)

    assert result["action"] == "BLOCKED"
    assert result["reason"] == "PATH_MISMATCH"
    assert worktree_path.exists()


def test_cleanup_refuses_a_release_that_is_not_verified_prod(rig, tmp_path, repo):
    task, release, worktree_path = _isolated_task_at_verified_prod(rig, tmp_path, repo,
                                                                   branch="task/early")
    rig.release.store.transition_release(release.id, "ROLLED_BACK", reason="incident")

    result = rig.cleanup_release(release.id)

    assert result["reason"] == "NOT_VERIFIED_PROD"
    assert worktree_path.exists()


def test_cleanup_is_reached_by_the_reconcile_pass(rig, tmp_path, repo):
    task, _release, worktree_path = _isolated_task_at_verified_prod(rig, tmp_path, repo,
                                                                    branch="task/viareconcile")
    summary = rig.reconcile()

    assert summary["counts"]["cleanups"] == 1
    assert summary["cleanups"][0]["action"] == "CLEANED"
    assert not worktree_path.exists()
    # Already-cleaned tasks are skipped entirely on the next pass.
    assert rig.reconcile()["counts"]["cleanups"] == 0


def test_reconcile_is_bounded(rig):
    assert rig.reconcile(limit=5)["limit"] == 5
    assert rig.reconcile(limit=10_000)["limit"] == 1000
    assert rig.reconcile(limit=0)["limit"] == 1


def test_handoff_dedup_is_scoped_to_the_commit_not_the_task(rig):
    """Dedup must key on the commit the merge queue actually consumes.

    Task metadata is immutable in this project (queue_store's own
    "state machine on top of an otherwise-frozen record" discipline), so a
    rework arrives as a NEW task -- but both tasks legitimately target the
    same project, and each must get its own handoff. Keying dedup on the
    task alone would be right here by accident; keying it on the commit is
    right for the case that actually breaks, which is the same task's PASS
    being delivered twice."""
    first = _running_task(rig.queue.store,
                          metadata={"integration_required": _integration_spec(commit="c" * 40)})
    _pass_verification(rig.queue, first)
    # A duplicate delivery of the SAME verdict publishes nothing further.
    assert rig.ensure_handoff_for_task(first.id)["action"] == "ALREADY_PUBLISHED"
    assert len(rig.integration.list_handoffs(PROJECT)) == 1

    rework = _running_task(rig.queue.store, title="rework",
                           metadata={"integration_required": _integration_spec(commit="d" * 40)})
    _pass_verification(rig.queue, rework)

    handoffs = rig.integration.list_handoffs(PROJECT)
    assert len(handoffs) == 2
    assert {h.commit_sha for h in handoffs} == {"c" * 40, "d" * 40}


def test_find_handoff_for_task_is_commit_scoped(rig):
    task = _running_task(rig.queue.store)
    rig.integration.publish_handoff(
        project=PROJECT, task_id=task.id, origin_session=SESSION, branch="task/x",
        commit_sha="c" * 40, base_sha="b" * 40)

    assert rig.integration.find_handoff_for_task(task.id, commit_sha="c" * 40) is not None
    assert rig.integration.find_handoff_for_task(task.id, commit_sha="d" * 40) is None
    # Unscoped still answers "any handoff at all", for other callers.
    assert rig.integration.find_handoff_for_task(task.id) is not None


def test_incomplete_integration_spec_is_skipped_without_stranding_the_key(rig):
    """A spec missing its commit publishes nothing and, crucially, leaves
    no claimed key behind -- a corrected task must still be publishable."""
    task = _running_task(rig.queue.store, metadata={"integration_required": {"project": PROJECT}})
    outcome = rig.ensure_handoff_for_task(task.id)
    assert outcome["reason"] == "INCOMPLETE_INTEGRATION_SPEC"
    assert rig.integration.list_handoffs(PROJECT) == []


def test_partial_cleanup_can_be_finished_on_a_later_pass(rig, tmp_path, repo):
    """The worktree removed but the branch refused is a real half-state.
    A later pass must be able to finish it -- and must report the BRANCH
    reason, not bury it under WORKTREE_MISSING."""
    task, release, worktree_path = _isolated_task_at_verified_prod(
        rig, tmp_path, repo, branch="task/partial")

    # Force the half-state: an extra unmerged commit on the branch makes
    # `git branch -d` refuse after the tree has already been removed.
    _git(["checkout", "-q", "task/partial"], worktree_path)
    (worktree_path / "extra.txt").write_text("not on main\n")
    _git(["add", "."], worktree_path)
    _git(["commit", "-q", "-m", "extra work"], worktree_path)
    _git(["checkout", "-q", "--detach"], worktree_path)

    first = rig.cleanup_release(release.id)
    assert first["action"] in ("PARTIAL", "BLOCKED"), first

    if first["action"] == "PARTIAL":
        assert not worktree_path.exists()
        assert rig.store.get_cleanup(task.id)["reason"] == "BRANCH_NOT_MERGED"
        # A later pass still reports the BRANCH problem, not the tree's.
        second = rig.cleanup_release(release.id)
        assert second["reason"] == "BRANCH_NOT_MERGED"
        # Once the work really lands, the same pass finishes the job.
        _git(["merge", "-q", "--no-ff", "-m", "land the rest", "task/partial"], repo)
        third = rig.cleanup_release(release.id)
        assert third["action"] == "CLEANED"
        assert _git(["rev-parse", "--verify", "refs/heads/task/partial"], repo,
                    check=False).returncode != 0
