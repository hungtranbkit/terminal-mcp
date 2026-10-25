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
from pathlib import Path
from typing import Any, Callable

from .config import AppConfig
from .adapters import (codex_folder_trust_prompt, codex_model_migration_prompt,
                       codex_rate_limit_switch_prompt, codex_sandbox_approval_prompt,
                       tool_permission_prompt)
from .codex_trust import ensure_trust
from .launcher_resolution import resolve_launcher
from .permissions import SENSITIVE_SESSION_WORDS, valid_new_session_name, session_input_denied_by_pattern
from .session_backend import SessionBackend
from .tmux import TmuxError

AGENT_TYPES = ("shell", "claude", "codex")
CREATE_POLL_INTERVAL_SECONDS = 0.2


_NO_DAEMON_SUPPORT: dict[str, bool] = {}


def _codex_supports_no_daemon(command: str) -> bool:
    """Use the installed CLI's own help as the capability check.

    This is a per-process startup choice. It avoids mutating the shared
    Codex daemon's feature settings and is never inferred from a version
    string, which may not match a wrapper or vendor build.
    """
    if command in _NO_DAEMON_SUPPORT:
        return _NO_DAEMON_SUPPORT[command]
    try:
        result = subprocess.run([command, "--help"], capture_output=True,
                                text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        # Not cached: a --help that timed out on a loaded node (seen live)
        # must not pin "no --no-daemon" for the agent's whole lifetime.
        return False
    help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
    supported = result.returncode == 0 and re.search(r"(?m)^\s*--no-daemon(?:\s|$)", help_text) is not None
    _NO_DAEMON_SUPPORT[command] = supported
    return supported


_codex_supports_no_daemon.cache_clear = _NO_DAEMON_SUPPORT.clear  # type: ignore[attr-defined]


def _codex_readiness(lines: list[str]) -> tuple[str, str | None]:
    """Classify Codex's visible startup surface without accepting prompts.

    The process name alone is not a readiness signal. Only Codex's stable
    composer footer admits task input; setup, trust, hook, and config prompts
    are returned as structured blockers and are never answered here.
    """
    if tool_permission_prompt(lines):
        return "BLOCKED_PERMISSION", "CODEX_TOOL_PERMISSION_REQUIRED"
    if codex_sandbox_approval_prompt(lines) is not None:
        return "BLOCKED_PERMISSION", "CODEX_SANDBOX_APPROVAL"
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
    if codex_model_migration_prompt(lines):
        return "BLOCKED", "CODEX_MODEL_MIGRATION"
    if codex_rate_limit_switch_prompt(lines):
        return "BLOCKED", "CODEX_RATE_LIMIT_SWITCH"
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


def _authorized_startup_step(lines: list[str]) -> tuple[str, str] | None:
    """One key toward a known non-granting option, never a shared config change."""
    if tool_permission_prompt(lines):
        return None
    recent = [line for line in lines if line.strip()][-12:]
    text = "\n".join(recent).casefold()
    if ("update now" in text and "2. skip" in text
            and "3. skip until next version" in text and "esc skip" in text):
        return "CODEX_UPDATE_MENU", "Escape"
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
              show_on_desktop: bool = False, extra_args: tuple[str, ...] = ()) -> dict[str, Any]:
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

        `extra_args` (conversation-continuity follow-up, 2026-09-07):
        passed straight through to the backend's own `new_session` --
        see its docstring. core.py's terminal_create_session is the
        only caller that ever populates this (`--session-id`/`--resume`
        for a resume-capable agent_type), never derived here."""
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
        codex_trust = None
        if agent_type == "codex" and self.config.session_lifecycle.codex_trust_roots:
            # Folder trust for a managed workspace is decided by the operator's
            # root list, so record it before launch instead of leaving the
            # session on a modal nobody may answer. Never sandbox approval.
            codex_trust = ensure_trust(resolved_cwd, self.config.session_lifecycle.codex_trust_roots)
        launch_args = extra_args
        if (agent_type == "codex" and command and "--no-daemon" not in extra_args
                and _codex_supports_no_daemon(command)):
            # Safe scoped fallback for a shared daemon with incompatible
            # feature settings. Place before resume/session subcommands.
            launch_args = ("--no-daemon", *extra_args)
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
                    if agent_type == "codex":
                        try:
                            lines = self.tmux.capture_lines(name, 80)
                        except TmuxError:
                            lines = []
                        readiness, blocked_reason = _codex_readiness(lines)
                        if readiness != "READY":
                            composer_since = None
                            composer_snapshot = None
                        step = _authorized_startup_step(lines)
                        if step:
                            reason, key = step
                            # Both are explicitly owner-approved, non-granting
                            # choices: run without the daemon once, or skip a
                            # CLI update (Escape = Skip; Update now is never
                            # chosen). Neither changes model, trust or approvals.
                            safe_daemon_fallback = (reason in ("CODEX_STARTUP_MENU", "CODEX_UPDATE_MENU")
                                and valid_new_session_name(name)
                                and not session_input_denied_by_pattern(name, self.config)
                                and not any(word in name.casefold() for word in SENSITIVE_SESSION_WORDS))
                            permitted = (safe_daemon_fallback or (self.config.permissions.terminal_input
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
                                        and _authorized_startup_step(fresh_lines) == step)
                                    still_authorized = (safe_daemon_fallback or self._startup_input_authorized(name))
                                    if same and still_authorized:
                                        self.tmux.send_keys(name, [key])
                                        startup_key_count += 1
                                        startup_actions.append({"menu": reason, "key": key})
                                        if key in ("Enter", "Escape"):
                                            # One answer per menu: a menu that
                                            # survives it is reported, not retried.
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
                                    "Owner must decide whether this exact repository is trusted, or add "
                                    "its managed workspace to session_lifecycle.codex_trust_roots and "
                                    "restart the session; never answer the dialog automatically."),
                                "CODEX_SANDBOX_APPROVAL": (
                                    "Owner must approve or deny the command in the session; this is "
                                    "sandbox approval, not folder trust, and is never auto-approved."),
                                "CODEX_UPDATE_MENU": (
                                    "Codex kept showing its update menu after Skip; the owner chooses in "
                                    "the session or sets check_for_update_on_startup = false."),
                                "CODEX_MODEL_MIGRATION": (
                                    "Owner must choose the model in the session; Terminal MCP never "
                                    "changes the model. Dispatch to another node/agent meanwhile."),
                                "CODEX_RATE_LIMIT_SWITCH": (
                                    "Owner must decide whether to switch to the cheaper model; never "
                                    "chosen automatically. Dispatch to another node/agent meanwhile."),
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
                                **({"codex_trust": codex_trust} if codex_trust else {}),
                            }
                        if readiness == "READY":
                            snapshot = tuple(line.strip() for line in lines[-12:])
                            if composer_since is None or composer_snapshot != snapshot:
                                composer_since = time.monotonic()
                                composer_snapshot = snapshot
                            elif time.monotonic() - composer_since >= settle_seconds:
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
                        **({"codex_trust": codex_trust} if codex_trust else {}),
                    }
                break
            time.sleep(CREATE_POLL_INTERVAL_SECONDS)

        result = {
            "session": name, "agent_type": agent_type, "cwd": str(resolved_cwd), "state": state,
            "session_id": info.session_id, "pane_id": info.pane_id,
            "pane_current_command": info.pane_current_command, "created_epoch": info.created_epoch,
        }
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
        if codex_trust:
            result["codex_trust"] = codex_trust
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
