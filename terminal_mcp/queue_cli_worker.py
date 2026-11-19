"""Noninteractive CLI execution backed only by QueueStore task rows.

The CLI process is an executor, not a task store. A fenced receipt and every
heartbeat/result are written to the canonical queue task metadata. Claimed
work is never replayed after a worker/process crash.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import PurePosixPath
from typing import Any

from . import autopilot as ap
from . import queue_store as qs
from .redaction import redact_text

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TEST_SHA = re.compile(r"^[0-9a-f]{64}$")


class WorkerBlocked(RuntimeError):
    """A deterministic owner-facing fail-closed worker condition."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class QueueStoreCLIWorker:
    def __init__(self, store: qs.QueueStore, *, project_id: str, lane: str,
                 config: dict[str, Any], worker_id: str | None = None,
                 popen=subprocess.Popen, clock=time.time) -> None:
        self.store, self.project_id, self.lane = store, project_id, lane
        self.config = dict(config)
        self.worker_id = worker_id or f"cli-worker:{os.getpid()}"
        self.popen, self.clock = popen, clock
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self.cwd = Path(self.config["working_directory"]).resolve(strict=True)
        if not self.cwd.is_dir() or self.config.get("isolated_worktree") is not True:
            raise ValueError("CLI worker requires an existing isolated worktree")
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.cwd,
                                capture_output=True, text=True, timeout=10, shell=False)
        clean = subprocess.run(["git", "status", "--porcelain"], cwd=self.cwd,
                               capture_output=True, text=True, timeout=10, shell=False)
        if branch.returncode or branch.stdout.strip() in ("", "main", "master") or clean.returncode or clean.stdout.strip():
            raise ValueError("CLI worker requires a clean, named non-main git worktree")
        command = self.config.get("test_command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise ValueError("CLI worker requires test_command as an argv list")
        self.test_command = command
        self.commit_mode = self.config.get("commit_mode", "agent")
        if self.commit_mode not in ("agent", "worker"):
            raise ValueError("commit_mode must be agent or worker")
        self.allowed_paths = tuple(self.config.get("allowed_paths") or ())
        if self.commit_mode == "worker":
            if not self.allowed_paths:
                raise ValueError("worker commit_mode requires allowed_paths")
            for value in self.allowed_paths:
                path = PurePosixPath(value) if isinstance(value, str) else PurePosixPath("/")
                if (not isinstance(value, str) or not value or "\\" in value or path.is_absolute()
                        or path.as_posix() != value or any(part in ("", ".", "..") for part in path.parts)
                        or path.parts[0] == ".git"):
                    raise ValueError(f"unsafe worker allowlist path: {value!r}")
            if len(self.allowed_paths) != len(set(self.allowed_paths)):
                raise ValueError("worker allowlist paths must be unique")
        self.timeout = max(30, min(int(self.config.get("timeout_seconds", 1800)), 7200))
        self.heartbeat_seconds = max(1, min(int(self.config.get("heartbeat_seconds", 5)), 30))

    def _task_for_key(self, key: str):
        return next((task for task in self.store.list_tasks_for_project(self.project_id, limit=500)
                     if task.dispatch_idempotency_key == key), None)

    def dispatch(self, *, target: str, text: str, idempotency_key: str) -> ap.Observation:
        if target != self.lane:
            return ap.Observation(ap.REFUSED, reason="CLI worker lane is not allowlisted")
        task = self._task_for_key(idempotency_key)
        if task is None or task.status != qs.DISPATCHING:
            return ap.Observation(ap.REFUSED, reason="no matching DISPATCHING QueueStore task")
        existing = (task.metadata or {}).get("cli_worker")
        if existing:
            return self.observe(idempotency_key=idempotency_key)
        try:
            self._require_clean_feature_worktree()
        except WorkerBlocked as exc:
            return ap.Observation(ap.REFUSED, reason=exc.reason)
        # Persist intent before spawn. A crash after this point is held and
        # blocked; it is never automatically replayed.
        baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.cwd,
                                  capture_output=True, text=True, timeout=10, shell=False)
        if baseline.returncode or not _SHA.fullmatch(baseline.stdout.strip()):
            return ap.Observation(ap.REFUSED, reason="baseline commit SHA unavailable")
        claim = self.store.claim_cli_worker(task.id, dispatch_key=idempotency_key,
                                            worker_id=self.worker_id, pid=0, lease_seconds=30,
                                            baseline_commit_sha=baseline.stdout.strip())
        if not claim:
            return ap.Observation(ap.REFUSED, reason="QueueStore worker claim lost")
        if self.commit_mode == "worker":
            mode_text = ("You are a file editor only. Do not call shell/Bash or git. Write only these "
                         f"files: {', '.join(self.allowed_paths)}. Do not create any other files. "
                         "The trusted worker will run tests and commit after validation. Print "
                         "TMCP-DONE only after the requested edits are written.")
        else:
            mode_text = "Continue without asking for a user turn. Verify the task and commit the completed change."
        completion_clause = ("After the requested edits are written, print a final line whose first token is "
                            "TMCP-DONE followed by a colon and task id " + task.id + ".")
        if self.commit_mode != "worker":
            completion_clause = ("Only after all acceptance criteria pass, print a final line whose first token is "
                                "TMCP-DONE followed by a colon and task id " + task.id + ".")
        prompt = (text + "\n\n" + mode_text + " " + completion_clause + " "
                  "If blocked, print a final line whose first token is TMCP-BLOCKED followed by "
                  f"a colon, task id {task.id}, and a concrete owner action. A normal CLI exit or IDLE is not done.")
        try:
            argv = self._argv()
            log = tempfile.NamedTemporaryFile(prefix="tmcp-cli-", suffix=".log", delete=False)
            log_path = Path(log.name)
            proc = self.popen(argv, cwd=self.cwd, stdin=subprocess.PIPE, stdout=log,
                              stderr=subprocess.STDOUT, shell=False, text=True,
                              start_new_session=True)
            self.store.update_cli_worker(task.id, worker_id=self.worker_id,
                                         changes={"state": "RUNNING", "pid": proc.pid,
                                                  "heartbeat_at": self.clock(),
                                                  "log_path": str(log_path)})
            with self._guard:
                thread = threading.Thread(target=self._finish, args=(task.id, proc, log, prompt),
                                          name=f"queue-cli-{task.id}", daemon=True)
                self._threads[task.id] = thread
                thread.start()
            return ap.Observation(ap.PENDING, reason="noninteractive CLI process started",
                                  ref=f"pid:{proc.pid}")
        except Exception as exc:  # no resend after a persisted claim
            self.store.update_cli_worker(task.id, worker_id=self.worker_id,
                                         changes={"state": "BLOCKED", "blocker":
                                                  f"CLI_START_FAILED:{type(exc).__name__}"})
            return ap.Observation(ap.BLOCKED, reason="CLI start failed; owner must inspect worker receipt")

    def _argv(self) -> list[str]:
        cli = self.config["cli"]
        if cli == "codex":
            return [str(self.config.get("executable") or "codex"), "exec", "--ephemeral",
                    "--cd", str(self.cwd), "--sandbox", "workspace-write", "-"]
        if cli == "claude":
            argv = [str(self.config.get("executable") or "claude"), "-p",
                    "--permission-mode", "acceptEdits"]
            if self.commit_mode == "worker":
                argv.extend(["--allowedTools", "Read", "Write", "Edit"])
            return argv
        raise ValueError("unsupported CLI")

    def _finish(self, task_id: str, proc: Any, log: Any, prompt: str) -> None:
        receipt = self.store.get_task(task_id).metadata["cli_worker"]
        log_path = Path(receipt["log_path"])
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
            deadline = self.clock() + self.timeout
            while proc.poll() is None:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    break
                try:
                    proc.wait(timeout=min(float(self.heartbeat_seconds), remaining))
                except subprocess.TimeoutExpired:
                    now = self.clock()
                    self.store.update_cli_worker(task_id, worker_id=self.worker_id,
                        changes={"heartbeat_at": now, "lease_expires_at": now + 60})
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)
                self._block(task_id, "CLI_TIMEOUT_OWNER_ACTION_REQUIRED")
                return
            log.flush()
            output = log_path.read_text(errors="replace")[-200_000:]
            exit_code = int(proc.returncode)
            blocked = re.search(rf"(?m)^\s*TMCP-BLOCKED:\s*{re.escape(task_id)}\s+(.+?)\s*$", output)
            if blocked:
                self.store.update_cli_worker(task_id, worker_id=self.worker_id, changes={
                    "state": "BLOCKED", "exit_code": exit_code,
                    "blocker": redact_text(blocked.group(1))[:500] or "AGENT_REPORTED_BLOCKER"})
                return
            done = re.search(rf"(?m)^\s*TMCP-DONE:\s*{re.escape(task_id)}\s*$", output)
            if exit_code != 0 or not done:
                self.store.update_cli_worker(task_id, worker_id=self.worker_id, changes={
                    "state": "BLOCKED", "exit_code": exit_code,
                    "blocker": "CLI_EXIT_OR_EXPLICIT_DONE_MARKER_MISSING"})
                return
            if self.commit_mode == "worker":
                self._validate_worker_changes()
            test = subprocess.run(self.test_command, cwd=self.cwd, capture_output=True,
                                  text=True, timeout=self.timeout, shell=False)
            test_material = ("\0".join(self.test_command) + "\0" + str(test.returncode)
                             + "\0" + test.stdout + test.stderr)
            test_sha = hashlib.sha256(test_material.encode()).hexdigest()
            if test.returncode != 0:
                self.store.update_cli_worker(task_id, worker_id=self.worker_id, changes={
                    "state": "BLOCKED", "exit_code": exit_code, "test_sha": test_sha,
                    "blocker": "CONFIGURED_TEST_COMMAND_FAILED"})
                return
            if self.commit_mode == "worker":
                self._validate_worker_changes()
                changed = self._changed_paths()
                if not changed:
                    self._block(task_id, "NO_ALLOWLISTED_CHANGES")
                    return
                self._git_checked(["add", "--", *changed])
                staged = self._staged_paths()
                if not staged or set(staged) != set(changed) or not set(staged).issubset(self.allowed_paths):
                    self._block(task_id, "STAGED_PATH_VALIDATION_FAILED")
                    return
                commit_result = subprocess.run([
                    "git", "-c", "user.name=Terminal MCP Worker", "-c",
                    "user.email=terminal-mcp-worker@localhost", "commit", "-m",
                    f"autopilot: {task_id}"], cwd=self.cwd, capture_output=True,
                    text=True, timeout=30, shell=False)
                if commit_result.returncode != 0:
                    self._block(task_id, "WORKER_COMMIT_FAILED_OWNER_ACTION_REQUIRED")
                    return
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.cwd,
                                    capture_output=True, text=True, timeout=10, shell=False)
            commit_sha = commit.stdout.strip()
            if commit.returncode != 0 or not _SHA.fullmatch(commit_sha):
                self._block(task_id, "COMMIT_SHA_UNAVAILABLE")
                return
            if commit_sha == receipt.get("baseline_commit_sha"):
                self._block(task_id, "NO_NEW_COMMIT_OWNER_ACTION_REQUIRED")
                return
            self.store.update_cli_worker(task_id, worker_id=self.worker_id, changes={
                "state": "DONE", "exit_code": exit_code, "commit_sha": commit_sha,
                "test_sha": test_sha, "heartbeat_at": self.clock()})
        except WorkerBlocked as exc:
            self._block(task_id, exc.reason)
        except Exception as exc:  # fail closed; claimed work is never replayed
            self._block(task_id, f"WORKER_EXCEPTION:{type(exc).__name__}")
        finally:
            try:
                log.close()
            except Exception:
                pass

    def _block(self, task_id: str, reason: str) -> None:
        self.store.update_cli_worker(task_id, worker_id=self.worker_id,
                                     changes={"state": "BLOCKED", "blocker": reason})

    def _require_clean_feature_worktree(self) -> None:
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.cwd,
                                capture_output=True, text=True, timeout=10, shell=False)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.cwd,
                                capture_output=True, text=True, timeout=10, shell=False)
        if (branch.returncode != 0 or branch.stdout.strip() in ("", "main", "master")
                or status.returncode != 0 or status.stdout.strip()):
            raise WorkerBlocked("WORKTREE_NOT_CLEAN_FEATURE_BRANCH")

    def _git_checked(self, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(["git", *args], cwd=self.cwd, capture_output=True,
                                text=True, timeout=30, shell=False)
        if result.returncode:
            raise WorkerBlocked("WORKER_GIT_COMMAND_FAILED")
        return result

    def _status_paths(self) -> set[str]:
        result = subprocess.run(["git", "status", "--porcelain=v1", "-z",
                                 "--untracked-files=all"], cwd=self.cwd,
                                capture_output=True, timeout=10, shell=False)
        if result.returncode:
            raise WorkerBlocked("WORKTREE_STATUS_UNAVAILABLE")
        paths: set[str] = set()
        for item in result.stdout.split(b"\0"):
            if not item:
                continue
            if len(item) < 4:
                raise WorkerBlocked("WORKTREE_STATUS_PARSE_FAILED")
            code, raw_path = item[:2], item[3:]
            if b"R" in code or b"C" in code:
                raise WorkerBlocked("RENAMES_NOT_ALLOWED_IN_WORKER_MODE")
            paths.add(os.fsdecode(raw_path))
        return paths

    def _validate_worker_changes(self) -> None:
        changed = self._status_paths()
        unexpected = changed - set(self.allowed_paths)
        if unexpected:
            raise WorkerBlocked("UNAUTHORIZED_PATHS:" + ",".join(sorted(unexpected))[:300])
        for value in changed:
            candidate = self.cwd / value
            cursor = self.cwd
            for part in PurePosixPath(value).parts:
                cursor = cursor / part
                try:
                    mode = cursor.lstat().st_mode
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(mode):
                    raise WorkerBlocked("SYMLINK_PATH_BLOCKED:" + value[:200])
                if cursor == candidate and not stat.S_ISREG(mode):
                    raise WorkerBlocked("NONREGULAR_PATH_BLOCKED:" + value[:200])
            try:
                candidate.resolve(strict=False).relative_to(self.cwd)
            except ValueError as exc:
                raise WorkerBlocked("PATH_ESCAPE_BLOCKED:" + value[:200]) from exc

    def _changed_paths(self) -> list[str]:
        return sorted(self._status_paths())

    def _staged_paths(self) -> list[str]:
        result = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"],
                                cwd=self.cwd, capture_output=True, timeout=10, shell=False)
        if result.returncode:
            raise WorkerBlocked("STAGED_PATHS_UNAVAILABLE")
        return sorted(os.fsdecode(path) for path in result.stdout.split(b"\0") if path)

    def observe(self, *, idempotency_key: str) -> ap.Observation:
        task = self._task_for_key(idempotency_key)
        receipt = (task.metadata or {}).get("cli_worker") if task else None
        if not isinstance(receipt, dict):
            return ap.Observation(ap.NOT_FOUND)
        state = receipt.get("state")
        if state == "DONE" and receipt.get("exit_code") == 0 and _SHA.fullmatch(
                str(receipt.get("commit_sha") or "")) and _TEST_SHA.fullmatch(
                    str(receipt.get("test_sha") or "")):
            return ap.Observation(ap.DONE, reason="CLI exit, test and commit evidence recorded",
                                  ref=task.id)
        if state == "BLOCKED":
            return ap.Observation(ap.BLOCKED, reason=receipt.get("blocker") or "worker blocked", ref=task.id)
        if state in ("STARTING", "RUNNING"):
            with self._guard:
                thread = self._threads.get(task.id)
            if thread is not None and thread.is_alive():
                return ap.Observation(ap.PENDING, reason="CLI worker is finalizing process evidence",
                                      ref=f"pid:{receipt.get('pid') or 0}")
            pid = int(receipt.get("pid") or 0)
            if pid and self._pid_alive(pid):
                return ap.Observation(ap.PENDING, reason="CLI process heartbeat active", ref=f"pid:{pid}")
            if not pid and float(receipt.get("lease_expires_at") or 0) > self.clock():
                return ap.Observation(ap.PENDING, reason="CLI launch intent is fenced", ref=task.id)
            blocked = self.store.update_cli_worker(task.id, worker_id=receipt["worker_id"], changes={
                "state": "BLOCKED", "blocker": "WORKER_LOST_NO_REPLAY"})
            return ap.Observation(ap.BLOCKED, reason=(blocked or {}).get("blocker"), ref=task.id)
        return ap.Observation(ap.UNCERTAIN, reason="worker receipt state is unknown", ref=task.id)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
