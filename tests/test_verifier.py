"""P0 Part C: verifier.py unit coverage -- real subprocess execution
against real, disposable git worktrees/scripts (never mocked at the
subprocess boundary, since the whole point is proving these are real
external commands, not self-reported markers)."""
from __future__ import annotations

import subprocess
import sys

from terminal_mcp.verifier import VerifierPolicy, run_verifier


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)


def test_unconfigured_policy_never_silently_passes():
    result = run_verifier(VerifierPolicy())
    assert result["overall_pass"] is False
    assert "no verifier policy configured" in result["reasons"][0]


def test_clean_worktree_passes_git_check(tmp_path):
    _init_repo(tmp_path)
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), require_git_clean=True))
    assert result["overall_pass"] is True
    assert result["git"]["dirty"] is False
    assert result["git"]["commit_sha"]


def test_dirty_worktree_fails_when_clean_required(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n")
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), require_git_clean=True))
    assert result["overall_pass"] is False
    assert result["git"]["dirty"] is True
    assert any("uncommitted" in r for r in result["reasons"])


def test_dirty_worktree_ok_when_clean_not_required(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n")
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), require_git_clean=False))
    assert result["overall_pass"] is True
    assert result["git"]["dirty"] is True


def test_commit_mismatch_fails(tmp_path):
    _init_repo(tmp_path)
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), require_commit_matches="deadbeef" * 5))
    assert result["overall_pass"] is False
    assert any("does not match" in r for r in result["reasons"])


def test_commit_match_passes(tmp_path):
    _init_repo(tmp_path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
                         text=True, check=True).stdout.strip()
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), require_commit_matches=sha))
    assert result["overall_pass"] is True


def test_passing_test_command(tmp_path):
    _init_repo(tmp_path)
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), test_command=(sys.executable, "-c", "exit(0)")))
    assert result["overall_pass"] is True
    assert result["test"]["exit_code"] == 0
    assert result["test"]["passed"] is True


def test_failing_test_command(tmp_path):
    _init_repo(tmp_path)
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path), test_command=(sys.executable, "-c", "exit(1)")))
    assert result["overall_pass"] is False
    assert result["test"]["exit_code"] == 1
    assert any("exited 1" in r for r in result["reasons"])


def test_test_command_timeout_reported_not_hung(tmp_path):
    _init_repo(tmp_path)
    result = run_verifier(VerifierPolicy(
        worktree=str(tmp_path), test_command=(sys.executable, "-c", "import time; time.sleep(5)"),
        test_timeout_seconds=0.3,
    ))
    assert result["overall_pass"] is False
    assert result["test"]["timed_out"] is True
    assert any("timed out" in r for r in result["reasons"])


def test_nonexistent_worktree_fails_never_raises(tmp_path):
    result = run_verifier(VerifierPolicy(worktree=str(tmp_path / "does-not-exist"), require_git_clean=True))
    assert result["overall_pass"] is False
    assert result["git"]["commit_sha"] is None


def test_git_and_test_both_required_both_must_pass(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("changed\n")
    result = run_verifier(VerifierPolicy(
        worktree=str(tmp_path), require_git_clean=True,
        test_command=(sys.executable, "-c", "exit(0)"),
    ))
    assert result["overall_pass"] is False  # git check alone fails the whole thing
    assert result["test"]["passed"] is True  # but the test itself genuinely passed
    assert result["git"]["dirty"] is True


def test_never_uses_shell_true_no_arbitrary_interpolation(tmp_path):
    # A test_command containing shell metacharacters is passed through
    # argv, never interpreted -- proves there is no shell=True anywhere
    # in this module by using a payload that would do something
    # observably different (write a file) if it *were* shell-interpreted.
    _init_repo(tmp_path)
    marker = tmp_path / "should-not-exist"
    result = run_verifier(VerifierPolicy(
        worktree=str(tmp_path),
        test_command=(sys.executable, "-c", f"print('touch {marker}; rm -rf /')"),
    ))
    assert result["overall_pass"] is True  # the literal string was just printed, not executed
    assert not marker.exists()
