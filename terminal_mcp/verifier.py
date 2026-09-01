"""P0 Part C: independent completion verification -- runs OUTSIDE the
target pane, entirely separate from anything the watched agent printed.
Terminal output (prose "done" claims, ###TERMINAL_MCP_COMPLETION/
###TERMINAL_MCP_EVIDENCE markers) is untrusted self-report, handled
elsewhere (status.py/supervisor.py's existing marker system) -- this
module is the one place in this codebase that actually executes anything,
and it is scoped narrowly on purpose:

  - Every command is a fixed argument list, never a shell string --
    subprocess.run(..., shell=False) always, no exceptions. There is no
    code path from pane content to a command this module runs: the
    worktree path and test command come from an operator-configured
    VerifierPolicy (set once, at watch-creation time, by whoever calls
    SupervisorService.watch -- never parsed out of anything the target
    process printed).
  - Only two kinds of command ever run: a small, fixed set of read-only
    `git` invocations (rev-parse HEAD, status --porcelain, diff --stat)
    against the configured worktree, and -- only if the operator
    explicitly configured one -- a single fixed test command, run as-is
    with no interpolation.
  - Bounded: every subprocess call has an explicit timeout; a command that
    hangs is treated as a verification failure, never left to block
    forever.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class VerifierPolicy:
    """Operator-configured, opt-in -- set once via SupervisorService.watch's
    verifier_* parameters, never inferred or updated from pane content.
    A watch with no worktree and no test_command has an "empty" policy
    (is_configured is False) -- see run_verifier for what that means for
    an autonomous watch."""
    worktree: str | None = None
    require_git_clean: bool = False
    require_commit_matches: str | None = None  # a specific pinned SHA the worktree's HEAD must match, if set
    test_command: tuple[str, ...] = ()
    test_timeout_seconds: float = 120.0
    checklist: tuple[str, ...] = ()  # names cross-checked against a "checklist" evidence marker, see supervisor.py

    @property
    def is_configured(self) -> bool:
        return bool(self.worktree or self.test_command)


def _run_git(worktree: str, *args: str, timeout: float = 15.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, *args],
            capture_output=True, text=True, timeout=timeout, shell=False, check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"


def run_verifier(policy: VerifierPolicy) -> dict[str, Any]:
    """Executes `policy` right now and returns a structured, durable-
    evidence-shaped result -- never raises (a command failing to even
    start, e.g. a bad worktree path, is captured as a failing check, not
    an exception the caller has to handle specially). `overall_pass` is
    True only if every check this policy actually configured passed; a
    policy with is_configured False (nothing to check) is `overall_pass:
    False` with an explicit reason -- silently passing "nothing was
    configured" would defeat the entire point of requiring independent
    verification before autonomous VERIFIED_DONE (see supervisor.py's
    _handle_completion_candidate, which treats this as the BLOCKED case,
    not a pass)."""
    checked_at = datetime.now(timezone.utc).isoformat()
    reasons: list[str] = []
    git_result: dict[str, Any] | None = None
    test_result: dict[str, Any] | None = None
    overall_pass = True

    if not policy.is_configured:
        return {
            "checked_at": checked_at, "overall_pass": False,
            "reasons": ["no verifier policy configured (neither worktree nor test_command)"],
            "git": None, "test": None,
        }

    if policy.worktree:
        rc, sha_out, sha_err = _run_git(policy.worktree, "rev-parse", "HEAD")
        commit_sha = sha_out.strip() if rc == 0 else None
        if rc != 0:
            overall_pass = False
            reasons.append(f"git rev-parse HEAD failed in {policy.worktree!r}: {sha_err.strip() or 'unknown error'}")
        status_rc, status_out, status_err = _run_git(policy.worktree, "status", "--porcelain")
        dirty = bool(status_out.strip()) if status_rc == 0 else None
        if status_rc != 0:
            overall_pass = False
            reasons.append(f"git status --porcelain failed in {policy.worktree!r}: {status_err.strip() or 'unknown error'}")
        elif policy.require_git_clean and dirty:
            overall_pass = False
            reasons.append("worktree has uncommitted changes (git status --porcelain non-empty) "
                          "but require_git_clean is set")
        diff_rc, diff_out, _ = _run_git(policy.worktree, "diff", "--stat")
        if policy.require_commit_matches is not None and commit_sha != policy.require_commit_matches:
            overall_pass = False
            reasons.append(f"HEAD {commit_sha!r} does not match the pinned commit "
                          f"{policy.require_commit_matches!r} this attempt is tied to")
        git_result = {
            "worktree": policy.worktree, "commit_sha": commit_sha, "dirty": dirty,
            "diff_stat": diff_out.strip() if diff_rc == 0 else None,
        }

    if policy.test_command:
        try:
            proc = subprocess.run(
                list(policy.test_command), cwd=policy.worktree, capture_output=True, text=True,
                timeout=policy.test_timeout_seconds, shell=False, check=False,
            )
            exit_code = proc.returncode
            stdout_tail, stderr_tail = proc.stdout[-2000:], proc.stderr[-2000:]
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            exit_code = None
            stdout_tail = (exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else ""
            stderr_tail = (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else ""
            timed_out = True
        except OSError as exc:
            exit_code = None
            stdout_tail, stderr_tail = "", f"{type(exc).__name__}: {exc}"
            timed_out = False
        passed = exit_code == 0
        if not passed:
            overall_pass = False
            reasons.append(
                f"test command timed out after {policy.test_timeout_seconds}s" if timed_out
                else f"test command exited {exit_code}"
            )
        test_result = {
            "command": list(policy.test_command), "exit_code": exit_code, "passed": passed,
            "timed_out": timed_out, "stdout_tail": stdout_tail, "stderr_tail": stderr_tail,
        }

    return {"checked_at": checked_at, "overall_pass": overall_pass, "reasons": reasons,
            "git": git_result, "test": test_result}
