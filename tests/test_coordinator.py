"""CoordinatorGate -- the Phase 2 gate review (task: "Supervisor Queue
v2 Phase 2 -- Coordinator Agent"). Pure in-process tests: a fake
evidence_collector/scope_reasoner where useful, a REAL git repo (via
tmp_path) for the git-evidence-collection tests specifically (item C's
own "uncommitted/test fail giả lập trong disposable repo" -- a fake
collector alone wouldn't prove the real `git status --porcelain`
parsing is correct).

SAFETY: every session/cwd here is a disposable tmp_path fixture --
never `window`/`window2` or a real OfflinePOS checkout."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.coordinator import (
    BLOCKED, NEEDS_HUMAN, NEEDS_REWORK, READY, CoordinatorGate, OtherLaneSnapshot, RepoEvidenceError,
    SessionSnapshot, git_repo_evidence,
)
from terminal_mcp.queue_store import DISPATCHING, QueueStore, RUNNING, VERIFYING


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _make_task(store, session="lane-a", **overrides):
    task = {"prompt": "please implement the widget exporter carefully"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    store.claim_next_task(session, claimed_by="engine-1")
    return store.get_task(task_id)


def _ok_session(**overrides):
    base = {"node_id": "local", "cwd": "/tmp/does-not-matter", "current_command": "claude"}
    base.update(overrides)
    return SessionSnapshot(**base)


def _fake_collector_factory(*, clean=True, status_lines=()):
    from terminal_mcp.coordinator import RepoEvidence

    def collector(cwd):
        return RepoEvidence(branch="main", head="abc123", clean=clean, status_lines=tuple(status_lines))
    return collector


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------

def test_all_checks_pass_returns_ready(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory(clean=True))
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Fail-closed on unreadable evidence (item 7).
# ---------------------------------------------------------------------------

def test_session_error_is_fail_closed_needs_human(store):
    task = _make_task(store)
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(error="SESSION_NOT_FOUND"))
    assert decision.status == NEEDS_HUMAN
    assert "SESSION_NOT_FOUND" in decision.reason


def test_repo_evidence_collector_raising_is_fail_closed_needs_human(store):
    task = _make_task(store)

    def broken_collector(cwd):
        raise RepoEvidenceError("git not installed")

    gate = CoordinatorGate(evidence_collector=broken_collector)
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == NEEDS_HUMAN
    assert "git" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Sensitive/destructive prompt screen.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("prompt", [
    "please run rm -rf /tmp/build and start over",
    "force-push this branch to origin main",
    "drop table users then reseed",
    "sudo apt install the missing dependency",
])
def test_sensitive_destructive_prompt_needs_human(store, prompt):
    task = _make_task(store, prompt=prompt)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == NEEDS_HUMAN
    assert "pattern" in decision.reason.lower()


# ---------------------------------------------------------------------------
# Scope/clarity heuristic.
# ---------------------------------------------------------------------------

def test_very_short_prompt_needs_human(store):
    task = _make_task(store, prompt="fix it")
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == NEEDS_HUMAN
    assert "vague" in decision.reason.lower() or "short" in decision.reason.lower()


def test_custom_scope_reasoner_is_used_when_provided(store):
    task = _make_task(store, prompt="a perfectly reasonable prompt")
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory(),
                          scope_reasoner=lambda prompt: "custom reasoner says no")
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == NEEDS_HUMAN
    assert decision.reason == "custom reasoner says no"


# ---------------------------------------------------------------------------
# Review-attempt budget -- no infinite loop.
# ---------------------------------------------------------------------------

def test_exceeding_max_review_attempts_forces_needs_human(store):
    task = _make_task(store)
    for _ in range(3):
        store.record_coordinator_decision(task.id, status="NEEDS_REWORK", reason="not ready yet")
        store.claim_next_task(task.session, claimed_by="engine-1")
    task = store.get_task(task.id)
    assert task.coordinator_attempts == 3
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory(), max_review_attempts=3)
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == NEEDS_HUMAN
    assert "max coordinator review attempts" in decision.reason


# ---------------------------------------------------------------------------
# Previous-task evidence check (item 3's first bullet / item 11).
# ---------------------------------------------------------------------------

def test_previous_task_with_no_verification_evidence_needs_rework(store):
    ids = store.set_tasks("lane-a", [{"prompt": "first task here please"}, {"prompt": "second task here please"}])
    # First task marked COMPLETED the "legacy" way -- no evidence attached.
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")
    store.transition_task(ids[0], RUNNING, event_type="TEST")
    store.transition_task(ids[0], VERIFYING, event_type="TEST")
    store.transition_task(ids[0], "COMPLETED", event_type="TEST")

    store.claim_next_task("lane-a", claimed_by="engine-1")
    second = store.get_task(ids[1])
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(second, store=store, session=_ok_session())
    assert decision.status == NEEDS_REWORK
    assert "no recorded completion evidence" in decision.reason


def test_previous_task_with_verification_evidence_passes(store):
    ids = store.set_tasks("lane-a", [{"prompt": "first task here please"}, {"prompt": "second task here please"}])
    store.transition_task(ids[0], DISPATCHING, event_type="TEST")
    store.transition_task(ids[0], RUNNING, event_type="TEST")
    store.transition_task(ids[0], VERIFYING, event_type="TEST")
    store.mark_completed_with_evidence(ids[0], evidence={"marker": "abc"})

    store.claim_next_task("lane-a", claimed_by="engine-1")
    second = store.get_task(ids[1])
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory(clean=True))
    decision = gate.review(second, store=store, session=_ok_session())
    assert decision.status == READY


def test_first_task_in_a_lane_has_no_previous_task_to_check(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Session identity/cwd/node_id check -- the P0-lesson check.
# ---------------------------------------------------------------------------

def test_cwd_mismatch_needs_human(store):
    task = _make_task(store, metadata={"expected_cwd": "C:\\Dev\\OfflinePOS-wt-explain-stock"})
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store,
                          session=_ok_session(cwd="C:\\Dev\\OfflinePOS"))  # wrong worktree
    assert decision.status == NEEDS_HUMAN
    assert "does not match" in decision.reason


def test_node_id_mismatch_needs_human(store):
    task = _make_task(store, metadata={"expected_node_id": "dell-5530"})
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(node_id="m910"))
    assert decision.status == NEEDS_HUMAN
    assert "dell-5530" in decision.reason


def test_matching_cwd_and_node_id_passes(store):
    task = _make_task(store, metadata={"expected_cwd": "/repo/a", "expected_node_id": "local"})
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(cwd="/repo/a", node_id="local"))
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Cross-lane conflict.
# ---------------------------------------------------------------------------

def test_another_active_session_in_the_same_cwd_needs_human(store):
    task = _make_task(store, session="lane-a")
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(
        task, store=store, session=_ok_session(cwd="/shared/repo"),
        other_active=(OtherLaneSnapshot(session="lane-b", node_id="local", cwd="/shared/repo"),),
    )
    assert decision.status == NEEDS_HUMAN
    assert "lane-b" in decision.reason


def test_another_active_session_in_a_different_cwd_is_fine(store):
    task = _make_task(store, session="lane-a")
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(
        task, store=store, session=_ok_session(cwd="/repo/a"),
        other_active=(OtherLaneSnapshot(session="lane-b", node_id="local", cwd="/repo/b"),),
    )
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Repo evidence: uncommitted changes -> NEEDS_REWORK (item C, real repo).
# ---------------------------------------------------------------------------

def _init_real_git_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)


def test_real_git_repo_clean_is_ready(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    task = _make_task(store)
    gate = CoordinatorGate()  # real git_repo_evidence collector
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == READY


def test_real_git_repo_with_uncommitted_changes_needs_rework(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    (repo / "README.md").write_text("modified, not committed\n")
    task = _make_task(store)
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == NEEDS_REWORK
    assert decision.blockers  # the real `git status --porcelain` line(s)
    assert "uncommitted" in decision.reason


def test_real_non_git_directory_is_fail_closed_needs_human(store, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    task = _make_task(store)
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(not_a_repo)))
    assert decision.status == NEEDS_HUMAN


def test_dirty_repo_allowed_when_task_metadata_opts_in(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    (repo / "README.md").write_text("modified, not committed, but this task says it's ok\n")
    task = _make_task(store, metadata={"allow_dirty_repo": True})
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == READY


def test_git_repo_evidence_raises_on_a_nonexistent_directory():
    with pytest.raises(RepoEvidenceError):
        git_repo_evidence("/no/such/directory/anywhere")


# ---------------------------------------------------------------------------
# Operator-declared artificial blocker (item B's own smoke-test requirement:
# "task có artificial blocker bị BLOCKED và không làm dừng queue khác").
# ---------------------------------------------------------------------------

def test_artificial_blocker_metadata_forces_blocked(store):
    task = _make_task(store, metadata={"artificial_blocker": "simulated CI outage"})
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == BLOCKED
    assert "simulated CI outage" in decision.reason


def test_artificial_blocker_true_without_a_string_reason_still_blocks(store):
    task = _make_task(store, metadata={"artificial_blocker": True})
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session())
    assert decision.status == BLOCKED


# ---------------------------------------------------------------------------
# Production-readiness pass: session state (WAITING_INPUT/stale stream).
# ---------------------------------------------------------------------------

def test_session_waiting_input_needs_human(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(state="WAITING_INPUT", input_required=True))
    assert decision.status == NEEDS_HUMAN
    assert "waiting on input" in decision.reason


def test_session_input_required_true_needs_human_even_if_state_looks_ok(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(state="UNKNOWN", input_required=True))
    assert decision.status == NEEDS_HUMAN


def test_session_reader_not_alive_needs_human(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(reader_alive=False))
    assert decision.status == NEEDS_HUMAN
    assert "stale stream" in decision.reason


def test_session_reader_alive_none_is_fine_tmux_has_no_such_concept(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(reader_alive=None))
    assert decision.status == READY


def test_session_running_state_is_fine(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(state="RUNNING", input_required=False))
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Conversation-continuity follow-up (2026-09-07): a session mid-recovery
# (RESTORING) or whose last recovery attempt failed (RECOVERY_FAILED) must
# never receive a fresh dispatch on top -- task item 5's own explicit
# "Coordinator xác minh agent thực sự tiếp tục đúng task trước khi state
# trở lại RUNNING".
# ---------------------------------------------------------------------------

def test_session_restoring_needs_human(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(recovery_state="RESTORING"))
    assert decision.status == NEEDS_HUMAN
    assert "RESTORING" in decision.reason


def test_session_recovery_failed_needs_human(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(recovery_state="RECOVERY_FAILED"))
    assert decision.status == NEEDS_HUMAN
    assert "RECOVERY_FAILED" in decision.reason


def test_session_recovery_state_none_is_fine(store):
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(recovery_state=None))
    assert decision.status == READY


def test_session_recovery_state_resumed_ok_is_fine(store):
    # A resolved-successful recovery is not a blocker -- only the two
    # unresolved/failed states are.
    task = _make_task(store)
    gate = CoordinatorGate(evidence_collector=_fake_collector_factory())
    decision = gate.review(task, store=store, session=_ok_session(recovery_state="RESUMED_OK"))
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Production-readiness pass: git diverged/ahead/behind (real repos, real
# remote-tracking branches -- never a fake RepoEvidence for this one, since
# the actual `git rev-list --left-right --count`/`@{upstream}` parsing is
# exactly what needs proving).
# ---------------------------------------------------------------------------

def _init_bare_remote_and_clone(tmp_path):
    """A real bare 'origin' + a real clone tracking it -- the minimal setup
    that gives the clone a real @{upstream} to diverge from/behind/ahead of."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(["git", "init", "-q", "--bare"], cwd=bare, check=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(clone)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=clone, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=clone, check=True)
    (clone / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=clone, check=True)
    return bare, clone


def test_real_git_repo_with_no_upstream_at_all_is_ready(store, tmp_path):
    """A fresh local-only branch (init, no remote at all) is a real, common
    state -- has_upstream=False must never be treated as an evidence
    failure or as "diverged"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    task = _make_task(store)
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == READY
    assert decision.evidence["has_upstream"] is False
    assert decision.evidence["ahead"] == 0 and decision.evidence["behind"] == 0


def test_real_git_repo_ahead_only_is_still_ready(store, tmp_path):
    """Unpushed local commits alone (ahead > 0, behind == 0) is routine
    mid-task state -- must never block on its own."""
    _bare, clone = _init_bare_remote_and_clone(tmp_path)
    (clone / "new_file.txt").write_text("a new unpushed commit\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "unpushed work"], cwd=clone, check=True)
    task = _make_task(store)
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(clone)))
    assert decision.status == READY
    assert decision.evidence["ahead"] == 1 and decision.evidence["behind"] == 0


def test_real_git_repo_diverged_needs_human(store, tmp_path):
    """A REAL divergence: one commit pushed to origin from a second clone,
    one different, unpushed local commit in the first clone -- ahead AND
    behind both nonzero, exactly the case that needs a human merge/rebase
    call."""
    bare, clone = _init_bare_remote_and_clone(tmp_path)
    # A second clone pushes a diverging commit to the same remote.
    other_clone = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(other_clone)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=other_clone, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=other_clone, check=True)
    (other_clone / "from_other_clone.txt").write_text("a remote-side commit\n")
    subprocess.run(["git", "add", "."], cwd=other_clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remote-side work"], cwd=other_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=other_clone, check=True)
    # The first clone makes its OWN, different, unpushed local commit.
    (clone / "from_first_clone.txt").write_text("a local-side commit\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "local-side work"], cwd=clone, check=True)
    subprocess.run(["git", "fetch", "-q"], cwd=clone, check=True)  # sees the remote-side commit, doesn't merge it

    task = _make_task(store)
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(clone)))
    assert decision.status == NEEDS_HUMAN
    assert decision.evidence["ahead"] == 1 and decision.evidence["behind"] == 1
    assert "diverged" in decision.reason


def test_real_git_repo_diverged_allowed_when_task_metadata_opts_in(store, tmp_path):
    bare, clone = _init_bare_remote_and_clone(tmp_path)
    other_clone = tmp_path / "other-clone"
    subprocess.run(["git", "clone", "-q", str(bare), str(other_clone)], check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=other_clone, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=other_clone, check=True)
    (other_clone / "from_other_clone.txt").write_text("a remote-side commit\n")
    subprocess.run(["git", "add", "."], cwd=other_clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "remote-side work"], cwd=other_clone, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=other_clone, check=True)
    (clone / "from_first_clone.txt").write_text("a local-side commit\n")
    subprocess.run(["git", "add", "."], cwd=clone, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "local-side work"], cwd=clone, check=True)
    subprocess.run(["git", "fetch", "-q"], cwd=clone, check=True)

    task = _make_task(store, metadata={"allow_diverged_branch": True})
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(clone)))
    assert decision.status == READY


# ---------------------------------------------------------------------------
# Production-readiness pass: opt-in smoke test (task item 2).
# ---------------------------------------------------------------------------

def test_smoke_test_not_declared_is_unaffected(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    task = _make_task(store)  # no require_smoke_test_command in metadata
    calls = []
    def runner(command, cwd, timeout_seconds):
        calls.append(command)
        raise AssertionError("must never be called when not declared")
    gate = CoordinatorGate(smoke_test_runner=runner)
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == READY
    assert calls == []


def test_smoke_test_passing_is_ready(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    task = _make_task(store, metadata={"require_smoke_test_command": ["true"]})
    gate = CoordinatorGate()  # the REAL run_smoke_test_command subprocess runner
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == READY


def test_smoke_test_failing_needs_rework_with_real_evidence(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    task = _make_task(store, metadata={
        "require_smoke_test_command": ["python3", "-c", "print('assertion failed: widget count'); exit(1)"],
    })
    gate = CoordinatorGate()  # the REAL run_smoke_test_command subprocess runner
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == NEEDS_REWORK
    assert decision.evidence["returncode"] == 1
    assert "widget count" in decision.evidence["output_tail"]


def test_smoke_test_timeout_is_treated_as_a_failure(store, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_real_git_repo(repo)
    task = _make_task(store, metadata={
        "require_smoke_test_command": ["python3", "-c", "import time; time.sleep(5)"],
        "smoke_test_timeout_seconds": 0.2,
    })
    gate = CoordinatorGate()
    decision = gate.review(task, store=store, session=_ok_session(cwd=str(repo)))
    assert decision.status == NEEDS_REWORK
