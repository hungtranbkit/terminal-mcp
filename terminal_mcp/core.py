from __future__ import annotations

from typing import Any

from .audit import AuditStore
from .bindings import Binding, BindingStore, valid_binding_name
from .config import AppConfig
from .permissions import (binding_session_allowed, input_session_allowed, require_input,
                          require_read, session_allowed)
from .redaction import redact_text
from .status import classify_status
from .tmux import TmuxClient, TmuxError, iso_timestamp


SENSITIVE_COMMANDS = {"ssh", "mysql", "psql", "sudo", "passwd"}


class TerminalService:
    def __init__(self, config: AppConfig, tmux: TmuxClient | None = None,
                 bindings: BindingStore | None = None,
                 audit: AuditStore | None = None) -> None:
        self.config = config
        self.tmux = tmux or TmuxClient()
        self.bindings = bindings or BindingStore()
        self.audit = audit or AuditStore()

    def _audit_result(self, response: dict[str, Any], *, action: str,
                      session: str | None, binding: str | None = None,
                      text: str | None = None, keys: list[str] | None = None,
                      press_enter: bool = False) -> dict[str, Any]:
        error = response.get("error")
        self.audit.record(action=action, binding=binding, session=session, text=text,
                          keys=keys, press_enter=press_enter,
                          result="BLOCKED" if error else ("DRY_RUN" if response.get("dry_run") else "SENT"),
                          reason=error or response.get("reason"))
        return response

    def _input_guard(self, session: str) -> dict[str, Any] | None:
        if (error := require_input(self.config)) is not None:
            return {"error": error, "session": session}
        if not input_session_allowed(session, self.config):
            return {"error": "ACCESS_DENIED", "session": session}
        try:
            info = self.tmux.get_session(session)
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        if info is None:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        command = info.pane_current_command.casefold()
        allowed = {item.casefold() for item in self.config.input_policy.allowed_sensitive_commands}
        if command in SENSITIVE_COMMANDS and command not in allowed:
            return {"error": "SENSITIVE_TARGET", "session": session, "current_command": command}
        return None

    def _guard(self, session: str, *, input_action: bool = False) -> dict[str, Any] | None:
        permission_error = require_input(self.config) if input_action else require_read(self.config)
        if permission_error:
            return {"error": permission_error, "session": session}
        if not session_allowed(session, self.config):
            return {"error": "ACCESS_DENIED", "session": session}
        return None

    def terminal_list_sessions(self) -> dict[str, Any]:
        if (error := require_read(self.config)) is not None:
            return {"error": error, "sessions": []}
        try:
            sessions = [
                {
                    "name": item.name,
                    "allowed": True,
                    "attached": item.attached,
                    "windows": item.windows,
                    "created": iso_timestamp(item.created_epoch),
                    "activity": iso_timestamp(item.activity_epoch),
                }
                for item in self.tmux.list_sessions()
                if session_allowed(item.name, self.config)
            ]
            return {"sessions": sessions}
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "reason": str(exc), "sessions": []}

    def terminal_tail(self, session: str, lines: int | None = None) -> dict[str, Any]:
        if error := self._guard(session):
            return error
        requested = self.config.default_tail_lines if lines is None else lines
        if requested < 1:
            return {"error": "INVALID_LINES", "session": session}
        effective = min(requested, self.config.max_capture_lines)
        try:
            output_lines = self.tmux.capture_lines(session, effective)
            return {
                "session": session,
                "lines_requested": requested,
                "output": redact_text("\n".join(output_lines)),
                "truncated": requested > self.config.max_capture_lines,
            }
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}

    def terminal_capture(self, session: str, start_line: int | None = None) -> dict[str, Any]:
        if error := self._guard(session):
            return error
        if start_line is not None and start_line < 0:
            return {"error": "INVALID_START_LINE", "session": session}
        try:
            captured = self.tmux.capture_lines(session, self.config.max_capture_lines)
            start = start_line or 0
            sliced = captured[start : start + self.config.max_capture_lines]
            truncated = start + len(sliced) < len(captured)
            return {
                "session": session,
                "start_line": start_line,
                "output": redact_text("\n".join(sliced)),
                "lines_returned": len(sliced),
                "truncated": truncated,
                "max_capture_lines": self.config.max_capture_lines,
            }
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}

    def terminal_status(self, session: str) -> dict[str, Any]:
        if error := self._guard(session):
            return error
        try:
            info = self.tmux.get_session(session)
            if info is None:
                return {"session": session, "exists": False, "allowed": True, "state": "UNKNOWN", "input_required": False, "reason": "session does not exist", "last_output": ""}
            output = "\n".join(self.tmux.capture_lines(session, 80))
            state, input_required, reason = classify_status(info, output)
            last_output = "\n".join(output.splitlines()[-20:])
            return {
                "session": session,
                "exists": True,
                "allowed": True,
                "state": state,
                "input_required": input_required,
                "reason": reason,
                "last_output": redact_text(last_output),
            }
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "session": session, "reason": str(exc)}

    def terminal_send_text(self, session: str, text: str, press_enter: bool = False,
                           dry_run: bool = False) -> dict[str, Any]:
        action = "send_text"
        if error := self._input_guard(session):
            return self._audit_result(error, action=action, session=session, text=text, press_enter=press_enter)
        if not self.config.input_policy.allow_send_text:
            response = {"error": "ACTION_NOT_ALLOWED", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if "\x00" in text:
            response = {"error": "INVALID_TEXT", "session": session}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if len(text) > self.config.input_policy.max_text_length:
            response = {"error": "INPUT_TOO_LARGE", "session": session, "max_text_length": self.config.input_policy.max_text_length}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        if dry_run:
            response = {"session": session, "would_send": True, "dry_run": True,
                        "characters": len(text), "press_enter": press_enter}
            return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)
        try:
            self.tmux.send_text(session, text, press_enter)
            response = {"session": session, "sent": True, "characters": len(text), "press_enter": press_enter}
        except TmuxError as exc:
            response = {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        return self._audit_result(response, action=action, session=session, text=text, press_enter=press_enter)

    def terminal_send_keys(self, session: str, keys: list[str],
                           confirm_sensitive: bool = False) -> dict[str, Any]:
        action = "send_keys"
        if error := self._input_guard(session):
            return self._audit_result(error, action=action, session=session, keys=keys)
        allowed = set(self.config.input_policy.allow_keys)
        sensitive = set(self.config.input_policy.sensitive_keys_require_confirmation)
        invalid = [key for key in keys if key not in allowed and key not in sensitive]
        if not keys or invalid:
            response = {"error": "KEY_NOT_ALLOWED", "session": session, "invalid_keys": invalid,
                        "allowed_keys": sorted(allowed | sensitive)}
            return self._audit_result(response, action=action, session=session, keys=keys)
        requested_sensitive = [key for key in keys if key in sensitive]
        if requested_sensitive and not confirm_sensitive:
            response = {"error": "CONFIRMATION_REQUIRED", "session": session,
                        "sensitive_keys": requested_sensitive}
            return self._audit_result(response, action=action, session=session, keys=keys)
        if len(keys) > 100:
            response = {"error": "TOO_MANY_KEYS", "session": session}
            return self._audit_result(response, action=action, session=session, keys=keys)
        try:
            self.tmux.send_keys(session, keys)
            response = {"session": session, "sent": True, "keys": keys}
        except TmuxError as exc:
            response = {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        return self._audit_result(response, action=action, session=session, keys=keys)

    def _binding_result(self, binding: Binding) -> dict[str, Any]:
        allowed = binding_session_allowed(binding.session, self.config)
        try:
            exists = self.tmux.get_session(binding.session) is not None
        except TmuxError:
            exists = False
        return {
            "binding": binding.name, "session": binding.session,
            "session_exists": exists, "allowed": allowed,
            "read_enabled": binding.read_enabled, "input_enabled": binding.input_enabled,
            "effective_input": (self.config.permissions.terminal_input and binding.input_enabled
                                and input_session_allowed(binding.session, self.config)),
            "created_at": binding.created_at, "updated_at": binding.updated_at,
        }

    def terminal_bind(self, binding: str, session: str, replace: bool = False,
                      read_enabled: bool = True, input_enabled: bool = False) -> dict[str, Any]:
        if not valid_binding_name(binding):
            return {"error": "INVALID_BINDING", "binding": binding}
        if not binding_session_allowed(session, self.config):
            return {"error": "ACCESS_DENIED", "binding": binding, "session": session}
        try:
            if self.tmux.get_session(session) is None:
                return {"error": "SESSION_NOT_FOUND", "binding": binding, "session": session}
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "binding": binding, "session": session, "reason": str(exc)}
        stored, changed = self.bindings.put(
            binding, session, read_enabled=read_enabled,
            input_enabled=input_enabled, replace=replace,
        )
        if not changed:
            return {"error": "BINDING_EXISTS", "binding": binding, "session": stored.session}
        result = self._binding_result(stored)
        result["replaced"] = replace
        return result

    def terminal_get_binding(self, binding: str) -> dict[str, Any]:
        if not valid_binding_name(binding):
            return {"error": "INVALID_BINDING", "binding": binding}
        stored = self.bindings.get(binding)
        if stored is None:
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return self._binding_result(stored)

    def terminal_list_bindings(self) -> list[dict[str, Any]]:
        results = []
        for binding in self.bindings.list():
            row = self._binding_result(binding)
            row["state"] = "RUNNING" if row["session_exists"] else "MISSING"
            results.append(row)
        return results

    def terminal_unbind(self, binding: str) -> dict[str, Any]:
        if not valid_binding_name(binding):
            return {"error": "INVALID_BINDING", "binding": binding}
        if not self.bindings.delete(binding):
            return {"error": "BINDING_NOT_FOUND", "binding": binding}
        return {"binding": binding, "unbound": True}

    def _resolve_binding(self, binding: str) -> tuple[Binding | None, dict[str, Any] | None]:
        if not valid_binding_name(binding):
            return None, {"error": "INVALID_BINDING", "binding": binding}
        stored = self.bindings.get(binding)
        if stored is None:
            return None, {"error": "BINDING_NOT_FOUND", "binding": binding}
        if not binding_session_allowed(stored.session, self.config):
            return None, {"error": "ACCESS_DENIED", "binding": binding, "session": stored.session}
        return stored, None

    def terminal_tail_bound(self, binding: str, lines: int = 200) -> dict[str, Any]:
        stored, error = self._resolve_binding(binding)
        if error:
            return error
        if not stored.read_enabled:
            return {"error": "READ_DISABLED", "binding": binding, "session": stored.session}
        return {"binding": binding, **self.terminal_tail(stored.session, lines)}

    def terminal_status_bound(self, binding: str) -> dict[str, Any]:
        stored, error = self._resolve_binding(binding)
        if error:
            return error
        if not stored.read_enabled:
            return {"error": "READ_DISABLED", "binding": binding, "session": stored.session}
        if (permission_error := require_read(self.config)) is not None:
            return {"error": permission_error, "binding": binding, "session": stored.session}
        try:
            if self.tmux.get_session(stored.session) is None:
                return {"binding": binding, "session": stored.session, "exists": False,
                        "allowed": True, "state": "MISSING", "input_required": False,
                        "reason": "session does not exist", "last_output": ""}
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "binding": binding, "session": stored.session, "reason": str(exc)}
        return {"binding": binding, **self.terminal_status(stored.session)}

    def terminal_send_bound(self, binding: str, text: str,
                            press_enter: bool = False, dry_run: bool = False) -> dict[str, Any]:
        action = "send_bound"
        stored, error = self._resolve_binding(binding)
        if error:
            return self._audit_result(error, action=action, session=error.get("session"), binding=binding,
                                      text=text, press_enter=press_enter)
        if not self.config.permissions.terminal_input:
            response = {"error": "INPUT_DISABLED", "binding": binding, "session": stored.session}
            return self._audit_result(response, action=action, session=stored.session, binding=binding,
                                      text=text, press_enter=press_enter)
        if not stored.input_enabled:
            response = {"error": "BINDING_INPUT_DISABLED", "binding": binding, "session": stored.session}
            return self._audit_result(response, action=action, session=stored.session, binding=binding,
                                      text=text, press_enter=press_enter)
        # Apply action/session/target validation here while recording exactly one bound event.
        if error := self._input_guard(stored.session):
            response = {"binding": binding, **error}
        elif not self.config.input_policy.allow_send_text:
            response = {"error": "ACTION_NOT_ALLOWED", "binding": binding, "session": stored.session}
        elif "\x00" in text:
            response = {"error": "INVALID_TEXT", "binding": binding, "session": stored.session}
        elif len(text) > self.config.input_policy.max_text_length:
            response = {"error": "INPUT_TOO_LARGE", "binding": binding, "session": stored.session,
                        "max_text_length": self.config.input_policy.max_text_length}
        elif dry_run:
            response = {"binding": binding, "session": stored.session, "would_send": True,
                        "dry_run": True, "characters": len(text), "press_enter": press_enter}
        else:
            try:
                self.tmux.send_text(stored.session, text, press_enter)
                response = {"binding": binding, "session": stored.session, "sent": True,
                            "characters": len(text), "press_enter": press_enter}
            except TmuxError as exc:
                response = {"error": "SESSION_NOT_FOUND", "binding": binding,
                            "session": stored.session, "reason": str(exc)}
        return self._audit_result(response, action=action, session=stored.session, binding=binding,
                                  text=text, press_enter=press_enter)

    def terminal_list_input_audit(self, limit: int = 50, binding: str | None = None,
                                  session: str | None = None) -> dict[str, Any]:
        if not 1 <= limit <= 500:
            return {"error": "INVALID_LIMIT", "events": []}
        return {"events": self.audit.list(limit, binding, session)}

    def terminal_input_context(self, session: str | None = None,
                               binding: str | None = None) -> dict[str, Any]:
        if (session is None) == (binding is None):
            return {"error": "EXACTLY_ONE_TARGET_REQUIRED"}
        stored = None
        if binding is not None:
            stored, error = self._resolve_binding(binding)
            if error:
                return error
            session = stored.session
        if error := self._guard(session):
            return error
        try:
            info = self.tmux.get_session(session)
            if info is None:
                return {"error": "SESSION_NOT_FOUND", "session": session}
            lines = self.tmux.capture_lines(session, 20)
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "session": session, "reason": str(exc)}
        effective = (self.config.permissions.terminal_input and input_session_allowed(session, self.config)
                     and (stored is None or stored.input_enabled)
                     and info.pane_current_command.casefold() not in SENSITIVE_COMMANDS)
        return {"binding": binding, "session": session, "current_command": info.pane_current_command,
                "status": "RUNNING" if not info.pane_dead else "DEAD",
                "last_output": redact_text("\n".join(lines)), "effective_input": effective}
