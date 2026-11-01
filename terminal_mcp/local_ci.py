"""Local CI runner: a versioned repo script at an exact commit -> evidence (#106).

The local replacement for a GitHub Actions job. It does one thing: run the
script the repository itself committed AT the SHA under test, in a fresh
detached worktree at exactly that SHA, and print evidence that verify_queue's
gate can check. It schedules nothing and dispatches nothing -- the existing
queue/autopilot decides when it runs, and independent QA decides what the
evidence means.

Fail-closed rules:
  * the SHA must be a full 40-hex commit (no HEAD/branch names) that exists;
  * the script must be a safe relative path, a .sh or .py file, and tracked
    in that very commit (not merely present in the working copy);
  * the run gets a minimal environment, so no controller or user secret is
    inherited; output goes to a local 0600 log, and only its hash, size and
    path go into the evidence;
  * a timeout or any setup failure sets `passed: false` and no exit code, so
    it can never read as a pass;
  * only the worktree this run created is removed; the caller's checkout,
    including uncommitted work, is never touched.

`TMCP_CI_RESULT {"tests_passed": N, "tests_failed": M}` printed by the script
is copied into the evidence when both values are plain integers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA = "local_ci_evidence/v1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SCRIPT_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_RESULT_RE = re.compile(r"^TMCP_CI_RESULT (\{.*\})\s*$", re.MULTILINE)
_INTERPRETERS = {".sh": "bash", ".py": "python3"}
_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "TZ")


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=check, timeout=120)


def default_log_dir() -> Path:
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "local-ci"


def _refusal(sha: str, script: str, error: str, detail: str) -> dict[str, Any]:
    return {"schema": SCHEMA, "commit_sha": sha, "script": script, "passed": False,
            "exit_code": None, "error": error, "detail": detail[:300]}


def run_local_ci(repo: str | Path, sha: str, script: str, *, timeout_seconds: float = 1800,
                 log_dir: str | Path | None = None) -> dict[str, Any]:
    repo = Path(repo).resolve()
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        return _refusal(str(sha), script, "BAD_SHA", "a full 40-hex lowercase commit is required")
    if (not _SCRIPT_RE.match(script or "") or script.startswith("/")
            or ".." in script.split("/")):
        return _refusal(sha, script, "BAD_SCRIPT", "script must be a relative path inside the repo")
    interpreter = _INTERPRETERS.get(Path(script).suffix)
    if interpreter is None:
        return _refusal(sha, script, "UNSUPPORTED_SCRIPT", "only .sh and .py scripts are run")
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        return _refusal(sha, script, "UNKNOWN_COMMIT", f"{sha[:12]} is not a commit in {repo.name}")
    listed = _git(repo, "ls-tree", "--name-only", sha, "--", script, check=False).stdout.split()
    if script not in listed:
        return _refusal(sha, script, "SCRIPT_NOT_IN_COMMIT",
                        f"{script} is not tracked in commit {sha[:12]}")

    logs = Path(log_dir) if log_dir is not None else default_log_dir()
    logs.mkdir(parents=True, exist_ok=True)
    os.chmod(logs, 0o700)
    started = time.time()
    log_path = logs / f"{sha[:12]}-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(started))}-{os.getpid()}.log"
    workdir = Path(tempfile.mkdtemp(prefix="tmcp-ci-"))
    worktree = workdir / "wt"
    try:
        added = _git(repo, "worktree", "add", "--detach", str(worktree), sha, check=False)
        if added.returncode != 0:
            return _refusal(sha, script, "WORKTREE_FAILED", added.stderr.strip())
        head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
        if head != sha:
            return _refusal(sha, script, "HEAD_MISMATCH", f"worktree HEAD {head[:12]}")
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        env.update({"CI": "1", "TMCP_CI_SHA": sha})
        timed_out, exit_code = False, None
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as log:
            # Own process group, so a timeout kills the script's children too.
            proc = subprocess.Popen([interpreter, script], cwd=worktree, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                exit_code = proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        finished = time.time()
        data = log_path.read_bytes()
        evidence: dict[str, Any] = {
            "schema": SCHEMA, "commit_sha": sha, "script": script,
            "command": f"{interpreter} {script}", "exit_code": exit_code,
            "passed": exit_code == 0 and not timed_out, "timed_out": timed_out,
            "duration_seconds": round(finished - started, 3),
            "started_at": _iso(started), "finished_at": _iso(finished),
            "log_path": str(log_path), "log_bytes": len(data),
            "log_sha256": hashlib.sha256(data).hexdigest(), "host": socket.gethostname(),
        }
        matches = _RESULT_RE.findall(data.decode("utf-8", "replace"))
        if matches:
            try:
                result = json.loads(matches[-1])
            except ValueError:
                result = {}
            counts = {k: result.get(k) for k in ("tests_passed", "tests_failed")}
            if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                   for v in counts.values()):
                evidence.update(counts)
                if counts["tests_failed"] > 0:
                    evidence["passed"] = False
        return evidence
    finally:
        if worktree.exists():
            _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        _git(repo, "worktree", "prune", check=False)
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a repo's versioned CI script at an exact commit.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--log-dir")
    args = parser.parse_args(argv)
    evidence = run_local_ci(args.repo, args.sha, args.script, timeout_seconds=args.timeout,
                            log_dir=args.log_dir)
    print(json.dumps(evidence, indent=1))
    if evidence.get("error"):
        return 2
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
