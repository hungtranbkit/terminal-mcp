"""IntegrationReviewGate -- the Integration Agent's pre-merge review
(task follow-up: "review phải là thật... diff review, suspicious
changes, API/schema/migration impact... merge conflict"). Real git
repos (tmp_path), no mocks -- a diff here is a REAL `git diff`.

SAFETY: every repo/branch here is disposable."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.integration_reviewer import BLOCKED, READY, REWORK_REQUIRED, IntegrationReviewGate
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
    # artifacts defaults to docs_exempt="chore" -- every pre-existing
    # test here is exercising review mechanics OTHER than the living-
    # requirements doc gate (commit resolution, dirty tree, migration
    # flags, evidence shape); the doc-gate's OWN tests (below) override
    # this explicitly to prove the real behavior.
    base = {"id": "h1", "project": "proj-a", "task_id": "t1", "origin_session": "lane-a", "branch": "feature/x",
           "commit_sha": "x", "base_sha": "y", "changed_paths": (), "test_summary": {},
           "artifacts": {"docs_exempt": "chore"}, "status": "CLAIMED", "created_at": "now", "updated_at": "now"}
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


# ---------------------------------------------------------------------------
# Living-requirements convention: a behavior-changing diff must also touch
# docs/REQUIREMENTS.md, or be explicitly docs_exempt, or be REWORK_REQUIRED.
# ---------------------------------------------------------------------------

def test_behavior_change_without_requirements_doc_update_needs_rework(repo):
    sha, base_sha = _commit(repo, "app.py", "def handler():\n    return 42\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("app.py",), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == REWORK_REQUIRED
    assert "REQUIREMENTS.md" in decision.reason
    assert "missing_requirements_doc_update" in decision.risk_flags


def test_behavior_change_with_requirements_doc_update_is_ready(repo):
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "app.py").write_text("def handler():\n    return 42\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### handler\n- Status: Verified\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add handler + docs"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("app.py", "docs/REQUIREMENTS.md"),
                       artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY


def test_behavior_change_with_docs_exempt_metadata_is_ready(repo):
    sha, base_sha = _commit(repo, "app.py", "def handler():\n    return 42\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("app.py",),
                       artifacts={"docs_exempt": "refactor"})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY


def test_test_only_change_is_exempt_without_needing_docs_exempt(repo):
    (repo / "tests").mkdir()
    sha, base_sha = _commit(repo, "tests/test_app.py", "def test_handler():\n    assert True\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("tests/test_app.py",), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY


def test_doc_only_change_is_exempt_without_needing_docs_exempt(repo):
    (repo / "docs").mkdir()
    sha, base_sha = _commit(repo, "docs/notes.md", "some notes\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("docs/notes.md",), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY


def test_doc_gate_runs_at_basic_depth_too_not_only_deep(repo):
    sha, base_sha = _commit(repo, "app.py", "def handler():\n    return 42\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("app.py",), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo, review_depth="basic"))
    assert decision.status == REWORK_REQUIRED


# ---------------------------------------------------------------------------
# Agent-guide currency (comprehensive-docs checkpoint, 2026-09-07) --
# informational risk_flag only, never blocks (unlike the REQUIREMENTS.md
# gate above) -- see AGENT_FACING_PATH_MARKERS' own docstring for why.
# ---------------------------------------------------------------------------

def test_mcp_app_change_without_agent_guide_update_flags_but_stays_ready(repo):
    (repo / "terminal_mcp").mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### new tool\n- Status: Verified\n")
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "terminal_mcp" / "mcp_app.py").write_text("def new_tool(): return 1\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add a new mcp tool + REQUIREMENTS"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    handoff = _handoff(commit_sha=sha, base_sha=base_sha,
                       changed_paths=("terminal_mcp/mcp_app.py", "docs/REQUIREMENTS.md"), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    # Informational only -- REQUIREMENTS.md was updated so the hard gate
    # passes; this is a SEPARATE, non-blocking signal.
    assert decision.status == READY
    assert "missing_agent_guide_update" in decision.risk_flags


def test_mcp_app_change_with_agent_guide_update_has_no_flag(repo):
    (repo / "terminal_mcp").mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### new tool\n- Status: Verified\n")
    (repo / "docs" / "CHATGPT_USAGE.md").write_text("## new tool\nHow to call it.\n")
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "terminal_mcp" / "mcp_app.py").write_text("def new_tool(): return 1\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add a new mcp tool + both docs"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    handoff = _handoff(commit_sha=sha, base_sha=base_sha,
                       changed_paths=("terminal_mcp/mcp_app.py", "docs/REQUIREMENTS.md",
                                     "docs/CHATGPT_USAGE.md"), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY
    assert "missing_agent_guide_update" not in decision.risk_flags


def test_dashboard_py_change_also_triggers_agent_guide_flag(repo):
    (repo / "terminal_mcp").mkdir()
    (repo / "docs").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### new route\n- Status: Verified\n")
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "terminal_mcp" / "dashboard.py").write_text("# new route\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "add a new dashboard route + REQUIREMENTS"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    handoff = _handoff(commit_sha=sha, base_sha=base_sha,
                       changed_paths=("terminal_mcp/dashboard.py", "docs/REQUIREMENTS.md"), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert "missing_agent_guide_update" in decision.risk_flags


def test_non_agent_facing_change_never_flags_missing_agent_guide(repo):
    (repo / "docs").mkdir()
    (repo / "docs" / "REQUIREMENTS.md").write_text("### internal fix\n- Status: Verified\n")
    base_sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    (repo / "status.py").write_text("# internal-only fix, no MCP/dashboard surface change\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "internal fix + REQUIREMENTS"], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    handoff = _handoff(commit_sha=sha, base_sha=base_sha,
                       changed_paths=("status.py", "docs/REQUIREMENTS.md"), artifacts={})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY
    assert "missing_agent_guide_update" not in decision.risk_flags


def test_agent_guide_flag_is_skipped_when_docs_exempt(repo):
    (repo / "terminal_mcp").mkdir()
    sha, base_sha = _commit(repo, "terminal_mcp/mcp_app.py", "def x(): return 1\n")
    handoff = _handoff(commit_sha=sha, base_sha=base_sha, changed_paths=("terminal_mcp/mcp_app.py",),
                       artifacts={"docs_exempt": "chore"})
    gate = IntegrationReviewGate()
    decision = gate.review(handoff, _pipeline(repo))
    assert decision.status == READY
    assert "missing_agent_guide_update" not in decision.risk_flags
