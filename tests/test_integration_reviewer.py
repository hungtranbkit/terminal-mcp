"""IntegrationReviewGate -- the Integration Agent's pre-merge review
(task follow-up: "review phải là thật... diff review, suspicious
changes, API/schema/migration impact... merge conflict"). Real git
repos (tmp_path), no mocks -- a diff here is a REAL `git diff`.

SAFETY: every repo/branch here is disposable."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.integration_reviewer import BLOCKED, READY, IntegrationReviewGate
from terminal_mcp.integration_store import Handoff


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
    return path


def _commit(repo, filename, content, message="feature commit"):
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / filename).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", message], repo)
    commit_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    return commit_sha, base_sha


def _handoff(**overrides):
    base = {"id": "h1", "project": "proj-a", "task_id": "t1", "origin_session": "lane-a", "branch": "feature/x",
           "commit_sha": "x", "base_sha": "y", "changed_paths": (), "test_summary": {}, "artifacts": {},
           "status": "CLAIMED", "created_at": "now", "updated_at": "now"}
    base.update(overrides)
    return Handoff(**base)


def _pipeline(repo, **overrides):
    base = {"repo_path": str(repo), "integration_branch": "integration", "review_depth": "basic"}
    base.update(overrides)
    return base


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def test_clean_commit_passes_basic_review(repo):
    sha, base_sha = _commit(repo, "a.txt", "hello\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("a.txt",))
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY


def test_unresolvable_commit_sha_is_fail_closed_blocked(repo):
    handoff = _handoff(commit_sha="0" * 40, base_sha="1" * 40)
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == BLOCKED


def test_dirty_working_tree_is_fail_closed_blocked(repo):
    sha, base_sha = _commit(repo, "a.txt", "hello\n")
    (repo / "uncommitted.txt").write_text("oops\n")  # dirty the tree
    handoff = _handoff(commit_sha=sha, base_sha=base_sha)
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == BLOCKED
    assert "clean" in decision.reason.lower()


def test_sensitive_content_in_the_real_diff_blocks(repo):
    sha, base_sha = _commit(repo, "config.py", 'API_KEY = "sk-super-secret-value"\n')
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("config.py",))
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == BLOCKED
    assert "pattern" in decision.reason.lower()


def test_deep_review_flags_migration_paths(repo):
    (repo / "migrations").mkdir()
    sha, base_sha = _commit(repo, "migrations/0001_init.sql", "CREATE TABLE x (id INT);\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("migrations/0001_init.sql",))
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo, review_depth="deep"))
    assert decision.status == READY  # informational only, never blocks by itself
    assert "schema_or_migration_change" in decision.risk_flags


def test_basic_review_does_not_compute_migration_flags(repo):
    (repo / "migrations").mkdir()
    sha, base_sha = _commit(repo, "migrations/0001_init.sql", "CREATE TABLE x (id INT);\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("migrations/0001_init.sql",))
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo, review_depth="basic"))
    assert decision.status == READY
    assert decision.risk_flags == ()  # basic depth never computes this


def test_evidence_includes_resolved_commit(repo):
    sha, base_sha = _commit(repo, "a.txt", "hello\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha)
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.evidence["resolved_commit"] == sha
