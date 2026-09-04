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
from pathlib import Path
from typing import Any

from .config import AppConfig
from .permissions import SENSITIVE_SESSION_WORDS, valid_new_session_name
from .session_backend import SessionBackend
from .tmux import TmuxError

AGENT_TYPES = ("shell", "claude", "codex")
CREATE_POLL_INTERVAL_SECONDS = 0.2


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
    def __init__(self, config: AppConfig, tmux: SessionBackend) -> None:
        self.config = config
        self.tmux = tmux

    def launch_command_for(self, agent_type: str) -> str | None:
        if agent_type == "shell":
            return None
        return dict(self.config.session_lifecycle.launch_commands).get(agent_type)

    def validate_create(self, name: str, agent_type: str) -> dict[str, Any] | None:
        if not valid_new_session_name(name):
            return {"error": "INVALID_SESSION_NAME", "session": name}
        if any(word in name.casefold() for word in SENSITIVE_SESSION_WORDS):
            return {"error": "SENSITIVE_SESSION_NOT_CREATABLE", "session": name}
        if agent_type not in AGENT_TYPES:
            return {"error": "INVALID_AGENT_TYPE", "agent_type": agent_type}
        return None

    def create(self, name: str, agent_type: str, cwd: str | None) -> dict[str, Any]:
        """Create one new detached session, then poll (bounded by
        config.session_lifecycle.create_ready_timeout_seconds) for
        evidence the launched process actually started. Returns a receipt
        with `state` in {"READY", "CREATED", "FAILED"}: READY means the
        pane's current command already matches the expected launcher (or,
        for agent_type="shell", the session simply exists -- a shell has
        nothing else to wait for); CREATED means the session exists but
        the expected command hasn't shown up in the pane yet by the
        timeout (still probably starting -- never torn down on a mere
        timeout, since a slow-starting real CLI is not a failure); FAILED
        means either the request was invalid/blocked before anything was
        created, or the launched process visibly exited (pane_dead, or
        the whole session vanished) -- in the pane_dead case the disposable
        session this call itself just made is cleaned up (kill-session)
        before returning; a request that fails before create ever runs
        touches no session at all, disposable or otherwise."""
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
        try:
            self.tmux.new_session(name, str(resolved_cwd), command)
        except TmuxError as exc:
            return {"error": "LAUNCH_FAILED", "session": name, "reason": str(exc), "state": "FAILED"}

        expected_command = Path(command).name.casefold() if command else None
        deadline = time.monotonic() + self.config.session_lifecycle.create_ready_timeout_seconds
        state = "CREATED"
        info = None
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
            if expected_command is None or info.pane_current_command.casefold() == expected_command:
                state = "READY"
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(CREATE_POLL_INTERVAL_SECONDS)

        return {
            "session": name, "agent_type": agent_type, "cwd": str(resolved_cwd), "state": state,
            "session_id": info.session_id, "pane_id": info.pane_id,
            "pane_current_command": info.pane_current_command, "created_epoch": info.created_epoch,
        }

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
