"""Session lifecycle: create/detach/delete real tmux sessions.

Deliberately a small, standalone abstraction (composed by TerminalService
in core.py the same way it already composes SessionGrantStore/
BindingStore) rather than shell commands scattered across dashboard.py's
route handlers -- both the dashboard's "Tạo session"/"Tách"/"Xóa session"
UI and the terminal_create_session/_detach_session/_delete_session MCP
tools call through TerminalService's thin wrapper methods into the exact
same SessionLifecycleService instance, so there is exactly one
implementation of "what counts as a valid new session" and "what tmux
calls are ever issued" no matter which surface a request came from.

Every method here is a pure decision over tmux state + config -- no
permission/audit/grant logic lives in this file at all (that stays in
core.py, exactly like every other TerminalService capability), so this
class is trivially unit-testable in isolation.
"""
from __future__ import annotations

import time
import re
import subprocess
import shlex
import os
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig
from .agent_launch_policy import (LaunchPolicyError, build_permission_args,
                                  register_claude_project_trust,
                                  register_codex_project_trust,
                                  validate_project_directory)
from .adapters import codex_folder_trust_prompt, tool_permission_prompt
from .launcher_resolution import resolve_launcher
from .permissions import SENSITIVE_SESSION_WORDS, valid_new_session_name, session_input_denied_by_pattern
from .session_backend import SessionBackend
from .tmux import TmuxError

AGENT_TYPES = ("shell", "claude", "codex")
CREATE_POLL_INTERVAL_SECONDS = 0.2


def _process_argv(pid: int) -> list[str] | None:
    """Read only a spawned CLI's argv; never expose or inspect its environment."""
    if not pid:
        return None
    if os.name == "nt":
        try:
            from .windows_backend import _win32_process_command_line
            command_line = _win32_process_command_line(pid)
        except Exception:  # noqa: BLE001 - verification is best-effort per backend
            return None
        return shlex.split(command_line, posix=False) if command_line else None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _process_tree_has_flag(pid: int, flag: str) -> bool:
    """Verify a launcher flag in the pane process or its CLI wrapper child."""
    if not pid:
        return False
    pending = [(int(pid), 0)]
    seen = set()
    while pending and len(seen) < 32:
        current, depth = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        argv = _process_argv(current)
        if argv and flag in argv:
            return True
        if depth >= 3 or os.name == "nt":
            continue
        try:
            for item in Path("/proc").iterdir():
                if not item.name.isdigit():
                    continue
                try:
                    fields = (item / "stat").read_text().rsplit(")", 1)[1].split()
                    if int(fields[1]) == current:
                        pending.append((int(item.name), depth + 1))
                except (OSError, IndexError, ValueError):
                    continue
        except OSError:
            break
    return False


@lru_cache(maxsize=64)
def _codex_supports_no_daemon(command: str) -> bool:
    """Use the installed CLI's own help as the capability check.

    This is a per-process startup choice. It avoids mutating the shared
    Codex daemon's feature settings and is never inferred from a version
    string, which may not match a wrapper or vendor build.
    """
    try:
        result = subprocess.run([command, "--help"], capture_output=True,
                                text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
    return result.returncode == 0 and re.search(r"(?m)^\s*--no-daemon(?:\s|$)", help_text) is not None


def _codex_readiness(lines: list[str]) -> tuple[str, str | None]:
    """Classify Codex's visible startup surface without accepting prompts.

    The process name alone is not a readiness signal. Only Codex's stable
    composer footer admits task input; setup, trust, hook, and config prompts
    are returned as structured blockers and are never answered here.
    """
    if tool_permission_prompt(lines):
        return "BLOCKED_PERMISSION", "CODEX_TOOL_PERMISSION_REQUIRED"
    if codex_folder_trust_prompt(lines):
        return "BLOCKED", "CODEX_TRUST_PROMPT"
    # Pane capture includes scrollback. Classify only the current bottom
    # surface so an already-dismissed startup menu cannot override the
    # present composer or receive a stale navigation key.
    recent = [line for line in lines if line.strip()][-12:]
    text = "\n".join(recent).casefold()
    if ("update now" in text and "2. skip" in text
            and "3. skip until next version" in text and "esc skip" in text):
        return "BLOCKED", "CODEX_UPDATE_MENU"
    if ("run without daemon this time" in text
            and "restart with these settings" in text
            and "cancel" in text):
        return "BLOCKED", "CODEX_STARTUP_MENU"
    if ("trust this folder" in text or "do you trust" in text
            or "workspace trust" in text):
        return "BLOCKED", "CODEX_TRUST_PROMPT"
    if any(marker in text for marker in (
            "sign in to codex", "log in to codex", "authentication required",
            "api key required", "could not authenticate")):
        return "BLOCKED", "CODEX_AUTH_REQUIRED"
    if "hook" in text and any(word in text for word in ("allow", "approve", "trust", "run this")):
        return "BLOCKED", "CODEX_HOOK_PROMPT"
    if ("permission" in text and any(marker in text for marker in
            ("enter to select", "press enter", "tab/arrow keys to navigate"))):
        return "BLOCKED", "CODEX_PERMISSION_PROMPT"
    if "warning" in text and any(word in text for word in ("config", "configuration", "hook")):
        return "BLOCKED", "CODEX_CONFIG_WARNING"
    if "ask codex to do anything" in text and any(marker in text for marker in ("›", ">")):
        return "READY", None
    return "STARTUP_CHECK", None


def _authorized_startup_step(lines: list[str], agent_type: str = "codex") -> tuple[str, str] | None:
    """One key toward a known non-granting option, never a shared config change."""
    if tool_permission_prompt(lines):
        return None
    recent = [line for line in lines if line.strip()][-12:]
    text = "\n".join(recent).casefold()
    if ("update now" in text and "2. skip" in text
            and "3. skip until next version" in text and "esc skip" in text):
        return "CODEX_UPDATE_MENU", "Escape"
    if agent_type == "claude" and any(marker in text for marker in (
            "teach auto mode", "save auto-mode settings", "save auto mode")):
        options = {}
        selected = []
        for line in recent:
            match = re.match(r"^\s*([›❯>])?\s*([1-4])\.\s+(.+?)\s*$", line)
            if not match:
                continue
            mark, number, label = match.groups()
            number = int(number)
            if number in options:
                return None
            options[number] = label.casefold()
            if mark:
                selected.append(number)
        if len(options) < 2 or len(selected) != 1:
            return None
        if "teach auto mode" in text:
            safe = [number for number, label in options.items()
                    if label in {"not now", "skip", "no thanks", "don't teach me"}]
        else:
            safe = [number for number, label in options.items()
                    if label in {"don't save", "do not save", "not now", "no thanks"}]
        if len(safe) != 1:
            return None
        current = selected[0]
        return "CLAUDE_OPTIONAL_NOTICE_MENU", (
            "Enter" if current == safe[0] else "Up" if current > safe[0] else "Down")
    options = {}
    selected = []
    for line in recent:
        match = re.match(r"^\s*([›❯>])?\s*([1-3])\.\s+(.+?)\s*$", line)
        if not match:
            continue
        mark, number, label = match.groups()
        number = int(number)
        if number in options:
            return None
        options[number] = label.casefold()
        if mark:
            selected.append(number)
    if set(options) != {1, 2, 3} or len(selected) != 1:
        return None
    if options == {1: "run without daemon this time", 2: "restart with these settings", 3: "cancel"}:
        safe, reason = 1, "CODEX_STARTUP_MENU"
    elif ("hooks need review" in text and options[1] == "review hooks"
          and options[2] == "trust all and continue"
          and re.fullmatch(r"continue without trusting(?: \(hooks won.t run\))?", options[3])):
        safe, reason = 3, "CODEX_HOOK_REVIEW_MENU"
    else:
        return None
    current = selected[0]
    return reason, ("Enter" if current == safe else "Up" if current > safe else "Down")

def _authorized_startup_choice(lines: list[str]) -> str | None:
    step = _authorized_startup_step(lines)
    return step[0] if step and step[1] == "Enter" else None


def _startup_prompt_fingerprint(lines: list[str]) -> str:
    visible = "\n".join(" ".join(line.split()) for line in lines[-12:] if line.strip())
    return hashlib.sha256(visible.encode("utf-8", errors="replace")).hexdigest()


def resolve_cwd(requested: str | None, config: AppConfig) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve an optional caller-supplied working directory against
    config.session_lifecycle.allowed_cwd_roots (falling back to the
    server's own home directory when the operator hasn't configured any
    roots -- never to "/" or any other unbounded default). Symlinks are
    followed (`Path.resolve()`) BEFORE the containment check, so a
    symlink inside an allowed root that points outside of it is caught,
    not silently trusted. Returns (resolved_path, None) on success, or
    (None, error_dict) otherwise -- never partially resolved."""
    configured_roots = config.session_lifecycle.allowed_cwd_roots or (str(Path.home()),)
    resolved_roots: list[Path] = []
    for root in configured_roots:
        try:
            resolved_roots.append(Path(root).expanduser().resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    if not resolved_roots:
        return None, {"error": "NO_ALLOWED_CWD_ROOTS"}
    if not requested:
        return resolved_roots[0], None
    try:
        candidate = Path(requested).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None, {"error": "INVALID_CWD", "cwd": requested}
    if not candidate.is_dir():
        return None, {"error": "CWD_NOT_FOUND", "cwd": requested}
    if not any(candidate == root or root in candidate.parents for root in resolved_roots):
        return None, {"error": "CWD_NOT_ALLOWED", "cwd": requested,
                      "allowed_roots": [str(root) for root in resolved_roots]}
    return candidate, None


class SessionLifecycleService:
    def __init__(self, config: AppConfig, tmux: SessionBackend, *,
                 input_authorized: Callable[[str], bool] | None = None) -> None:
        self.config = config
        self.tmux = tmux
        self.input_authorized = input_authorized

    def _startup_input_authorized(self, name: str) -> bool:
        # The hard deny floor applies even to injected grant checks.
        if (not valid_new_session_name(name)
                or session_input_denied_by_pattern(name, self.config)
                or any(word in name.casefold() for word in SENSITIVE_SESSION_WORDS)):
            return False
        if self.input_authorized is not None:
            return self.input_authorized(name)
        return bool(self.config.session_access.default_input)

    def launch_command_for(self, agent_type: str) -> str | None:
        if agent_type == "shell":
            return None
        configured = dict(self.config.session_lifecycle.launch_commands).get(agent_type)
        if not configured:
            return None
        # Resolve at create time, not only at node-agent startup.  The
        # returned absolute path is what the backend actually spawns, so a
        # Windows node-agent with a stale inherited PATH still launches the
        # same binary that capability detection verified.
        return resolve_launcher(configured)

    def validate_create(self, name: str, agent_type: str) -> dict[str, Any] | None:
        if not valid_new_session_name(name):
            return {"error": "INVALID_SESSION_NAME", "session": name}
        if any(word in name.casefold() for word in SENSITIVE_SESSION_WORDS):
            return {"error": "SENSITIVE_SESSION_NOT_CREATABLE", "session": name}
        if agent_type not in AGENT_TYPES:
            return {"error": "INVALID_AGENT_TYPE", "agent_type": agent_type}
        return None

    def create(self, name: str, agent_type: str, cwd: str | None, *,
              show_on_desktop: bool = False, extra_args: tuple[str, ...] = (),
              permission_mode: str | None = None) -> dict[str, Any]:
        """Create one new detached session, then poll (bounded by
        config.session_lifecycle.create_ready_timeout_seconds) for
        evidence the launched process actually started. Returns a receipt
        with separate process/composer and tool-readiness evidence. Codex
        returns READY only after its composer is visible and stable;
        TOOL_PREFLIGHT_REQUIRED means that signal has not arrived by the
        bounded timeout. This is task-input readiness, not proof of
        external tool/OS permission. READY is retained for a
        shell after its own first output (normally its prompt), rather than
        READY the instant the tmux session merely EXISTS, before the
        shell has actually attached its line editor, used to let
        terminal_create_session's initial_prompt send race the shell's
        own startup); CREATED means the session exists but the expected
        readiness signal hasn't shown up in the pane yet by the
        timeout (still probably starting -- never torn down on a mere
        timeout, since a slow-starting real CLI is not a failure); FAILED
        means either the request was invalid/blocked before anything was
        created, or the launched process visibly exited (pane_dead, or
        the whole session vanished) -- in the pane_dead case the disposable
        session this call itself just made is cleaned up (kill-session)
        before returning; a request that fails before create ever runs
        touches no session at all, disposable or otherwise.

        `extra_args` are literal server-selected conversation tokens. The
        owner permission mode is resolved here, after exact project/trust
        validation, and is verified against the spawned process tree; a
        native-mode session is an explicit per-session opt-out."""
        if (error := self.validate_create(name, agent_type)) is not None:
            return {**error, "state": "FAILED"}
        try:
            if self.tmux.get_session(name) is not None:
                return {"error": "SESSION_ALREADY_EXISTS", "session": name, "state": "FAILED"}
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc), "state": "FAILED"}
        resolved_cwd, error = resolve_cwd(cwd, self.config)
        if error is not None:
            return {**error, "state": "FAILED"}
        command = self.launch_command_for(agent_type)
        if agent_type != "shell" and not command:
            return {"error": "LAUNCHER_NOT_CONFIGURED", "agent_type": agent_type, "state": "FAILED"}
        launch_args = extra_args
        permission_mode = permission_mode or self.config.session_lifecycle.default_permission_mode
        if permission_mode not in {"owner", "native"}:
            return {"error": "INVALID_PERMISSION_MODE", "state": "FAILED"}
        launch_evidence: dict[str, Any] = {"permission_mode": permission_mode, "flags": []}
        if agent_type in {"claude", "codex"}:
            if not cwd:
                return {"error": "WORKING_DIRECTORY_REQUIRED", "state": "FAILED",
                        "detail": "Pass the exact existing project/worktree path; no home/workspace fallback is used."}
            try:
                project = validate_project_directory(
                    str(resolved_cwd),
                    allowed_roots=self.config.session_lifecycle.allowed_cwd_roots,
                    trusted_project_roots=self.config.session_lifecycle.trusted_project_roots,
                )
                if project.path != project.repo_root:
                    return {"error": "CWD_NOT_PROJECT_ROOT", "state": "FAILED",
                            "cwd": str(project.path), "repo_root": str(project.repo_root)}
                trust = (register_claude_project_trust(project.path) if agent_type == "claude"
                         else register_codex_project_trust(project.path))
                launch_evidence["project_trust"] = trust
                policy_args = build_permission_args(agent_type, permission_mode, command)
                launch_args = (*policy_args, *extra_args)
                launch_evidence["flags"] = list(launch_args)
            except LaunchPolicyError as exc:
                return {"error": exc.code, "state": "FAILED", "detail": exc.detail,
                        "cwd": str(resolved_cwd)}
        if (agent_type == "codex" and command and "--no-daemon" not in extra_args
                and _codex_supports_no_daemon(command)):
            # Safe scoped fallback for a shared daemon with incompatible
            # feature settings. Place before resume/session subcommands.
            launch_args = ("--no-daemon", *launch_args)
            launch_evidence["flags"] = list(launch_args)
        try:
            spawn_result = self.tmux.new_session(name, str(resolved_cwd), command,
                                                 show_on_desktop=show_on_desktop, extra_args=launch_args)
        except TmuxError as exc:
            return {"error": "LAUNCH_FAILED", "session": name, "reason": str(exc), "state": "FAILED"}
        visible, visible_reason = spawn_result if isinstance(spawn_result, tuple) else (False, None)

        expected_command = Path(command).name.casefold() if command else None
        deadline = time.monotonic() + self.config.session_lifecycle.create_ready_timeout_seconds
        state = "CREATED"
        info = None
        process_args_verified: bool | None = None
        startup_choices: set[str] = set()
        startup_key_count = 0
        composer_since = None
        composer_snapshot = None
        settle_seconds = min(2.0, max(0.05, self.config.session_lifecycle.create_ready_timeout_seconds / 4.0))
        startup_actions: list[dict[str, str]] = []
        while True:
            try:
                info = self.tmux.get_session(name)
            except TmuxError:
                info = None
            if info is None:
                # The process exited immediately and tmux tore the session
                # down with it (no remain-on-exit) -- nothing left to
                # clean up, it's already gone.
                return {"error": "LAUNCH_FAILED", "session": name, "state": "FAILED",
                        "reason": "session exited immediately after launch"}
            if info.pane_dead:
                try:
                    self.tmux.kill_session(name)
                except TmuxError:
                    pass
                return {"error": "LAUNCH_FAILED", "session": name, "state": "FAILED",
                        "reason": "launched process exited"}
            if expected_command is not None:
                if info.pane_current_command.casefold() == expected_command:
                    if permission_mode == "owner" and agent_type in {"claude", "codex"}:
                        pane_pid = getattr(info, "pane_pid", 0)
                        expected_flag = "--dangerously-skip-permissions" if agent_type == "claude" else "--yolo"
                        process_args_verified = _process_tree_has_flag(pane_pid, expected_flag)
                        if not process_args_verified:
                            try:
                                self.tmux.kill_session(name)
                            except TmuxError:
                                pass
                            return {"error": "LAUNCH_POLICY_ARGV_UNVERIFIED", "state": "FAILED",
                                    "session": name, "agent_type": agent_type,
                                    "required_flag": expected_flag,
                                    "detail": "The spawned process argv did not prove the requested permission flag."}
                        launch_evidence["process_argv_verified"] = True
                    if agent_type == "codex":
                        try:
                            lines = self.tmux.capture_lines(name, 80)
                        except TmuxError:
                            lines = []
                        readiness, blocked_reason = _codex_readiness(lines)
                        if readiness != "READY":
                            composer_since = None
                            composer_snapshot = None
                        step = _authorized_startup_step(lines, "codex")
                        if step:
                            reason, key = step
                            prompt_fingerprint = _startup_prompt_fingerprint(lines)
                            safe_daemon_fallback = (reason == "CODEX_STARTUP_MENU"
                                and valid_new_session_name(name)
                                and not session_input_denied_by_pattern(name, self.config)
                                and not any(word in name.casefold() for word in SENSITIVE_SESSION_WORDS))
                            safe_owner_choice = (permission_mode == "owner" and reason in {
                                "CODEX_UPDATE_MENU", "CODEX_STARTUP_MENU", "CODEX_HOOK_REVIEW_MENU",
                                "CLAUDE_OPTIONAL_NOTICE_MENU",
                            })
                            permitted = (safe_daemon_fallback or safe_owner_choice or (self.config.permissions.terminal_input
                                and self.config.permissions.allow_send_keys
                                and self._startup_input_authorized(name)
                                and key in self.config.input_policy.allow_keys
                                and key not in self.config.input_policy.sensitive_keys_require_confirmation))
                            if (permitted and reason not in startup_choices
                                    and startup_key_count < 8 and time.monotonic() < deadline):
                                try:
                                    fresh = self.tmux.get_session(name)
                                    fresh_lines = self.tmux.capture_lines(name, 80)
                                    same = (fresh is not None and not fresh.pane_dead
                                        and fresh.session_id == info.session_id
                                        and fresh.pane_id == info.pane_id
                                        and fresh.created_epoch == info.created_epoch
                                        and fresh.pane_current_command.casefold() == expected_command
                                        and _authorized_startup_step(fresh_lines, "codex") == step
                                        and _startup_prompt_fingerprint(fresh_lines) == prompt_fingerprint)
                                    still_authorized = (safe_daemon_fallback or safe_owner_choice
                                                        or self._startup_input_authorized(name))
                                    if same and still_authorized:
                                        self.tmux.send_keys(name, [key])
                                        startup_key_count += 1
                                        startup_actions.append({
                                            "menu": reason, "choice_id": reason.lower(), "key": key,
                                            "prompt_fingerprint": prompt_fingerprint,
                                            "session_generation": {
                                                "session_id": info.session_id, "pane_id": info.pane_id,
                                                "created_epoch": info.created_epoch},
                                            "idempotency_key": f"launch:{name}:{info.created_epoch}:{reason}:{prompt_fingerprint[:16]}",
                                        })
                                        if key == "Enter":
                                            startup_choices.add(reason)
                                        time.sleep(CREATE_POLL_INTERVAL_SECONDS)
                                        continue
                                except TmuxError:
                                    pass
                        if readiness in {"BLOCKED", "BLOCKED_PERMISSION"}:
                            next_action = {
                                "CODEX_STARTUP_MENU": (
                                    "Use Codex --no-daemon or choose only option 1, 'Run without daemon this time'; "
                                    "do not restart shared settings."),
                                "CODEX_TRUST_PROMPT": (
                                    "Owner must decide whether this exact workspace is trusted; "
                                    "do not auto-trust it."),
                                "CODEX_HOOK_PROMPT": (
                                    "Owner must review the hook change; never auto-approve it."),
                                "CODEX_HOOK_REVIEW_MENU": (
                                    "Owner must review hooks or explicitly continue without trusting; "
                                    "never choose Trust all."),
                                "CODEX_TOOL_PERMISSION_REQUIRED": (
                                    "Owner must review the exact tool and OS permission; never select Always allow."),
                            }.get(blocked_reason, "Owner must resolve the exact startup blocker before dispatch.")
                            return {
                                "session": name, "agent_type": agent_type,
                                "cwd": str(resolved_cwd), "state": readiness,
                                "input_required": True, "tool_ready": False,
                                "next_action": next_action,
                                "readiness_stage": "STARTUP_CHECK",
                                "blocked_reason": blocked_reason,
                                "startup_prompt_id": ("codex.daemon_feature_mismatch.v1"
                                    if blocked_reason == "CODEX_STARTUP_MENU" else None),
                                "session_id": info.session_id, "pane_id": info.pane_id,
                                "pane_current_command": info.pane_current_command,
                            }
                        if readiness == "READY":
                            snapshot = tuple(line.strip() for line in lines[-12:])
                            if composer_since is None or composer_snapshot != snapshot:
                                composer_since = time.monotonic()
                                composer_snapshot = snapshot
                            elif time.monotonic() - composer_since >= settle_seconds:
                                state = "READY"
                                break
                    elif agent_type == "claude":
                        try:
                            lines = self.tmux.capture_lines(name, 80)
                        except TmuxError:
                            lines = []
                        step = _authorized_startup_step(lines, "claude")
                        if step and permission_mode == "owner" and startup_key_count < 8:
                            reason, key = step
                            fingerprint = _startup_prompt_fingerprint(lines)
                            try:
                                fresh = self.tmux.get_session(name)
                                fresh_lines = self.tmux.capture_lines(name, 80)
                                same = (fresh is not None and not fresh.pane_dead
                                    and fresh.session_id == info.session_id
                                    and fresh.pane_id == info.pane_id
                                    and fresh.created_epoch == info.created_epoch
                                    and fresh.pane_current_command.casefold() == expected_command
                                    and _authorized_startup_step(fresh_lines, "claude") == step
                                    and _startup_prompt_fingerprint(fresh_lines) == fingerprint)
                                if same:
                                    self.tmux.send_keys(name, [key])
                                    startup_key_count += 1
                                    startup_actions.append({
                                        "menu": reason,
                                        "choice_id": "skip_optional_notice_without_saving",
                                        "key": key, "prompt_fingerprint": fingerprint,
                                        "session_generation": {
                                            "session_id": info.session_id, "pane_id": info.pane_id,
                                            "created_epoch": info.created_epoch},
                                        "idempotency_key": f"launch:{name}:{info.created_epoch}:{reason}:{fingerprint[:16]}",
                                    })
                                    time.sleep(CREATE_POLL_INTERVAL_SECONDS)
                                    continue
                            except TmuxError:
                                pass
                        state = "READY"
                        break
                    else:
                        state = "READY"
                        break
            else:
                # agent_type == "shell": no specific launcher command to
                # poll for, but the pane's own shell (rc-file sourcing,
                # PS1 draw, readline attaching to the tty) can genuinely
                # still be mid-startup the INSTANT tmux reports the
                # session created -- this used to fall straight through
                # to READY on this loop's very first iteration, zero
                # wait, zero readiness signal at all. Real, reproducing
                # bug this fixes (Backlog item 14, "initial_prompt can
                # visibly echo twice for a brand-new plain-shell
                # session"): terminal_create_session sends initial_prompt
                # the instant this returns READY -- racing that shell
                # startup meant the kernel tty's own raw echo of the
                # just-written text (drawn with no prompt yet) was
                # followed by the shell's OWN readline redraw of that
                # same still-buffered line once it finished attaching,
                # visibly duplicating it. Waiting for a real, cheap
                # readiness signal instead -- the pane has drawn
                # SOMETHING (its own prompt, at minimum) -- closes that
                # race in the overwhelming majority of real cases (a
                # shell's first prompt draws in low milliseconds), same
                # bounded deadline as every other agent_type, so a
                # genuinely silent/empty-PS1 shell (rare, untested edge
                # case) just falls back to the pre-existing CREATED-
                # after-timeout behavior -- never a regression, only a
                # strictly better readiness check.
                try:
                    has_output = bool("".join(self.tmux.capture_lines(name, 5)).strip())
                except TmuxError:
                    has_output = False
                if has_output:
                    state = "READY"
                    break
            if time.monotonic() >= deadline:
                if agent_type == "codex" and expected_command is not None:
                    return {
                        "session": name, "agent_type": agent_type,
                        "cwd": str(resolved_cwd), "state": "BLOCKED",
                        "readiness_stage": "STARTUP_CHECK",
                        "blocked_reason": "CODEX_READINESS_TIMEOUT",
                        "session_id": info.session_id, "pane_id": info.pane_id,
                        "pane_current_command": info.pane_current_command,
                    }
                break
            time.sleep(CREATE_POLL_INTERVAL_SECONDS)

        result = {
            "session": name, "agent_type": agent_type, "cwd": str(resolved_cwd), "state": state,
            "session_id": info.session_id, "pane_id": info.pane_id,
            "pane_current_command": info.pane_current_command, "created_epoch": info.created_epoch,
        }
        result["launch_policy"] = {**launch_evidence, "cwd": str(resolved_cwd),
                                    "process_argv_verified": process_args_verified}
        if agent_type in {"codex", "claude"}:
            task_ready = agent_type == "codex" and state == "READY"
            result.update(
                state="READY" if task_ready else "CREATED", agent_process_ready=state == "READY",
                composer_ready=task_ready,
                tool_ready=False,
                readiness_stage="TASK_READY" if task_ready else "TOOL_PREFLIGHT_REQUIRED",
                readiness_status="READY" if task_ready else "WAITING_INPUT",
                next_action=("Session can receive a task; tool and OS permissions remain governed by their existing approval policy." if task_ready else "Wait for startup to settle or resolve the reported startup blocker."),
            )
        if startup_actions:
            result["startup_actions"] = startup_actions
        # Only surfaced when actually requested -- never a new, always-
        # present field an existing (tmux-only, or headless-by-default
        # Windows) caller would need to start ignoring.
        if show_on_desktop:
            result["visible_window"] = visible
            if visible_reason:
                result["visible_window_reason"] = visible_reason
        return result

    def detach(self, name: str) -> dict[str, Any]:
        """Detach any attached client -- never kills the session/process,
        never loses output. Idempotent: a session that is already not
        attached returns its current state (attached=False) rather than
        an error."""
        try:
            info = self.tmux.get_session(name)
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc)}
        if info is None:
            return {"error": "SESSION_NOT_FOUND", "session": name}
        if not info.attached:
            return {"session": name, "attached": False, "action": "already_detached"}
        try:
            self.tmux.detach_session(name)
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc)}
        return {"session": name, "attached": False, "action": "detached"}

    def delete(self, name: str, *, protected_sessions: tuple[str, ...]) -> dict[str, Any]:
        """Terminate and remove exactly one session (`kill-session`,
        never `kill-server`). Idempotent: a session already gone returns
        a success-shaped result (deleted=False, action=already_gone), not
        an error -- a caller retrying a delete that already succeeded
        must never see a failure. Protected names are refused outright,
        checked before any tmux call at all."""
        if name in protected_sessions:
            return {"error": "SESSION_PROTECTED", "session": name}
        try:
            info = self.tmux.get_session(name)
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc)}
        if info is None:
            return {"session": name, "deleted": False, "action": "already_gone"}
        try:
            self.tmux.kill_session(name)
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc)}
        return {"session": name, "deleted": True, "action": "deleted"}
