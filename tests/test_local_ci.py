"""Local CI runner: versioned script, exact commit, checkable evidence (#106).

Replaces a GitHub Actions job with a deterministic local run: the script must
be committed AT the SHA under test, it runs in a fresh detached worktree at
exactly that SHA with a scrubbed environment, and the evidence it prints is
what independent QA hands to verify_queue. A timeout or a setup failure is
never evidence of a pass.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from terminal_mcp import local_ci
from terminal_mcp.verify_queue import evidence_verdict

PASSING = """#!/usr/bin/env bash
set -euo pipefail
echo "head=$(git rev-parse HEAD)"
echo "secret=${TMCP_TEST_SECRET:-absent}"
echo 'TMCP_CI_RESULT {"tests_passed": 4, "tests_failed": 0}'
"""
FAILING = """#!/usr/bin/env bash
echo 'TMCP_CI_RESULT {"tests_passed": 3, "tests_failed": 1}'
exit 1
"""
SLOW = "#!/usr/bin/env bash\nsleep 30\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def commit_script(repo, body, message="ci"):
    path = repo / "scripts" / "ci" / "local-ci.sh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "ci@example.test")
    git(repo, "config", "user.name", "ci")
    (repo / "README.md").write_text("synthetic\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def run(repo, sha, tmp_path, **kw):
    return local_ci.run_local_ci(repo, sha, "scripts/ci/local-ci.sh",
                                 log_dir=tmp_path / "logs", **kw)


def test_passing_script_at_the_exact_sha_yields_accepted_evidence(repo, tmp_path, monkeypatch):
    monkeypatch.setenv("TMCP_TEST_SECRET", "hunter2")
    sha = commit_script(repo, PASSING)
    commit_script(repo, FAILING, message="later commit")   # HEAD moves on; we test `sha`
    evidence = run(repo, sha, tmp_path)
    assert evidence["schema"] == local_ci.SCHEMA and evidence["commit_sha"] == sha
    assert evidence["exit_code"] == 0 and evidence["passed"] is True
    assert evidence["tests_passed"] == 4 and evidence["tests_failed"] == 0
    assert evidence["command"] == "bash scripts/ci/local-ci.sh"
    log = Path(evidence["log_path"]).read_text()
    assert f"head={sha}" in log                     # ran at exactly the requested commit
    assert "secret=absent" in log and "hunter2" not in log   # environment scrubbed
    assert oct(os.stat(evidence["log_path"]).st_mode & 0o777) == "0o600"
    assert evidence_verdict(evidence) == (True, "accepted")


def test_failing_script_is_never_a_pass(repo, tmp_path):
    sha = commit_script(repo, FAILING)
    evidence = run(repo, sha, tmp_path)
    assert evidence["exit_code"] == 1 and evidence["passed"] is False
    assert evidence["tests_failed"] == 1 and evidence_verdict(evidence)[0] is False


def test_timeout_is_never_a_pass(repo, tmp_path):
    sha = commit_script(repo, SLOW)
    evidence = run(repo, sha, tmp_path, timeout_seconds=1)
    assert evidence["timed_out"] is True and evidence["passed"] is False
    assert evidence["exit_code"] is None and evidence_verdict(evidence)[0] is False


def test_script_not_committed_at_that_sha_is_refused(repo, tmp_path):
    sha = git(repo, "rev-parse", "HEAD")         # before the script exists
    commit_script(repo, PASSING)
    evidence = run(repo, sha, tmp_path)
    assert evidence["error"] == "SCRIPT_NOT_IN_COMMIT" and evidence["passed"] is False
    assert evidence_verdict(evidence)[0] is False


def test_unknown_commit_is_refused(repo, tmp_path):
    evidence = run(repo, "e" * 40, tmp_path)
    assert evidence["error"] == "UNKNOWN_COMMIT" and evidence["passed"] is False


@pytest.mark.parametrize("script", ["../outside.sh", "/etc/passwd", "scripts/ci/x.sh;rm -rf /",
                                    "scripts/ci/tool.exe"])
def test_unsafe_or_unsupported_script_path_is_refused(repo, tmp_path, script):
    sha = commit_script(repo, PASSING)
    evidence = local_ci.run_local_ci(repo, sha, script, log_dir=tmp_path / "logs")
    assert evidence["error"] in ("BAD_SCRIPT", "UNSUPPORTED_SCRIPT") and evidence["passed"] is False


@pytest.mark.parametrize("sha", ["abc", "z" * 40, "HEAD", "main"])
def test_symbolic_or_malformed_sha_is_refused(repo, tmp_path, sha):
    commit_script(repo, PASSING)
    evidence = run(repo, sha, tmp_path)
    assert evidence["error"] == "BAD_SHA" and evidence["passed"] is False


def test_the_temporary_worktree_is_removed_and_the_checkout_untouched(repo, tmp_path):
    sha = commit_script(repo, PASSING)
    (repo / "dirty.txt").write_text("user work in progress\n")
    before = git(repo, "worktree", "list", "--porcelain")
    run(repo, sha, tmp_path)
    assert git(repo, "worktree", "list", "--porcelain") == before
    assert (repo / "dirty.txt").read_text() == "user work in progress\n"
    assert git(repo, "status", "--porcelain") == "?? dirty.txt"


def test_forged_result_line_with_non_integers_is_ignored(repo, tmp_path):
    sha = commit_script(repo, "#!/usr/bin/env bash\n"
                              "echo 'TMCP_CI_RESULT {\"tests_passed\": \"all\", \"tests_failed\": true}'\n")
    evidence = run(repo, sha, tmp_path)
    assert "tests_passed" not in evidence and "tests_failed" not in evidence
    assert evidence["exit_code"] == 0


def test_cli_prints_evidence_and_exit_code_follows_the_script(repo, tmp_path):
    sha = commit_script(repo, FAILING)
    proc = subprocess.run(
        [sys.executable, "-m", "terminal_mcp.local_ci", "--repo", str(repo),
         "--sha", sha, "--script", "scripts/ci/local-ci.sh", "--log-dir", str(tmp_path / "logs")],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(Path.cwd())})
    assert proc.returncode == 1
    assert json.loads(proc.stdout)["commit_sha"] == sha
