from __future__ import annotations

import threading
import time
from typing import Any

from .audit import AuditStore
from .bindings import Binding, BindingStore, valid_binding_name
from .config import AppConfig
from .models import SessionIdentity
from .permissions import (binding_session_allowed, input_session_allowed, require_input,
                          require_read, session_allowed)
from .redaction import redact_ansi_safe, redact_text
from .status import classify_status
from .tmux import SEND_TEXT_ENTER_SETTLE_SECONDS, TmuxClient, TmuxError, iso_timestamp


class PaneLockRegistry:
    """P0-3: one lock per canonical pane identity (falling back to session
    name if identity can't be resolved), shared by every send path -- plain
    terminal_send_text/terminal_send_bound calls and Supervisor v2's
    execute_send all go through the *same* TerminalService instance in this
    process, so a registry owned by TerminalService is automatically shared
    across all of them. Guarantees two sends targeting the same pane can
    never interleave their text/Enter keystrokes, and (combined with the
    idempotency-key claim, which happens before the lock is even needed)
    that concurrent duplicate requests never both proceed to send."""

    def __init__(self) -> None:
        self._registry_lock = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def get(self, key: str) -> threading.Lock:
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


SENSITIVE_COMMANDS = {"ssh", "mysql", "psql", "sudo", "passwd"}

# Post-send submission verification (terminal_send_text/terminal_send_bound,
# press_enter=True only). tmux.send_text already adds a fixed settle delay
# between the text and Enter keystrokes -- this is a *second*, independent
# layer on top: a best-effort check, after the send, of whether the pane
# shows evidence Enter actually submitted rather than just having been
# typed. SEND_VERIFY_LINES is how much of the tail we look at;
# SEND_VERIFY_TIMEOUT_SECONDS bounds the total time spent polling for a
# transition (never unbounded); SEND_VERIFY_POLL_INTERVAL_SECONDS is the
# gap between polls -- short enough that a fast-confirming send doesn't pay
# the full timeout, long enough not to hammer tmux.
SEND_VERIFY_LINES = 20
SEND_VERIFY_TIMEOUT_SECONDS = 0.6
SEND_VERIFY_POLL_INTERVAL_SECONDS = 0.05


class TerminalService:
    def __init__(self, config: AppConfig, tmux: TmuxClient | None = None,
                 bindings: BindingStore | None = None,
                 audit: AuditStore | None = None) -> None:
        self.config = config
        self.tmux = tmux or TmuxClient()
        self.bindings = bindings or BindingStore()
        self.audit = audit or AuditStore()
        self._pane_locks = PaneLockRegistry()

    def resolve_identity(self, session: str) -> SessionIdentity | None:
        try:
            info = self.tmux.get_session(session)
        except TmuxError:
            return None
        return None if info is None else SessionIdentity.from_session_info(info)

    def _audit_result(self, response: dict[str, Any], *, action: str,
                      session: str | None, binding: str | None = None,
                      text: str | None = None, keys: list[str] | None = None,
                      press_enter: bool = False) -> dict[str, Any]:
        error = response.get("error")
        if error:
            result = "BLOCKED"
        elif response.get("dry_run"):
            result = "DRY_RUN"
        elif response.get("submit_status") == "SUBMIT_UNCONFIRMED":
            # Distinct from plain "SENT": text delivery itself succeeded,
            # but the Enter's submission could not be confirmed within the
            # verification window -- see _send_text_and_verify. Keeping this
            # as its own audit category (not folded into "SENT" or an
            # error) is what makes exactly this class of intermittent bug
            # discoverable from the audit log instead of only from a user
            # report.
            result = "SENT_UNCONFIRMED"
        else:
            result = "SENT"
        # submit_reason (only set on the unconfirmed path) carries no
        # prompt content -- it is a fixed short phrase describing *why*
        # verification was inconclusive, same redaction posture as every
        # other reason string already passed through here.
        self.audit.record(action=action, binding=binding, session=session, text=text,
                          keys=keys, press_enter=press_enter, result=result,
                          reason=error or response.get("reason") or response.get("submit_reason"))
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

    def terminal_tail(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]:
        """Return sanitized recent output. `ansi` is keyword-only, defaults to
        False, and is never set by the MCP tool wrapper — only the dashboard's
        terminal-style renderer opts in to get colour/style escape sequences
        back (still redaction-safe; see redact_ansi_safe). Every other caller
        keeps today's exact plain-text behavior unchanged."""
        if error := self._guard(session):
            return error
        requested = self.config.default_tail_lines if lines is None else lines
        if requested < 1:
            return {"error": "INVALID_LINES", "session": session}
        effective = min(requested, self.config.max_capture_lines)
        try:
            output_lines = self.tmux.capture_lines(session, effective, ansi=ansi)
            redact = redact_ansi_safe if ansi else redact_text
            return {
                "session": session,
                "lines_requested": requested,
                "output": redact("\n".join(output_lines)),
                "truncated": requested > self.config.max_capture_lines,
                # P0-9: `output` is text the *watched program* printed, not
                # an instruction from this tool or from terminal-mcp itself
                # -- a caller (human or an external model driving these
                # tools) must treat it as untrusted evidence to read, never
                # as directives to follow. untrusted_fields names exactly
                # which key(s) in this response carry that content.
                "untrusted_output": True, "untrusted_fields": ["output"], "content_source": "session",
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
                "untrusted_output": True, "untrusted_fields": ["output"], "content_source": "session",
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
                "untrusted_output": True, "untrusted_fields": ["last_output"], "content_source": "session",
            }
        except TmuxError as exc:
            return {"error": "TMUX_ERROR", "session": session, "reason": str(exc)}

    def _send_text_and_verify(self, session: str, text: str, press_enter: bool, *,
                              idempotency_key: str | None = None) -> dict[str, Any]:
        """P0-3/P0-4 wrapper around _send_text_and_verify_locked: claims an
        idempotency key (if given) before anything else, serializes on the
        target pane's own lock so no two sends to the same pane can ever
        interleave, and persists the final result under the claimed key.

        idempotency_key semantics: the *first* caller to successfully claim
        a given key is the only one that ever actually sends -- a repeat
        call with the same key (a retry, a duplicate request, or a call
        made again after a process restart, since the claim is durable on
        disk) returns the original stored result instead of sending again.
        A concurrent caller that loses the claim race while the winner is
        still mid-send gets an honest DUPLICATE_IN_PROGRESS rather than a
        second send or a fabricated result.
        """
        if idempotency_key is not None:
            if not self.audit.claim_idempotency_key(idempotency_key):
                existing = self.audit.get_idempotent_result(idempotency_key)
                if existing is not None:
                    return existing
                return {"session": session, "error": "DUPLICATE_IN_PROGRESS", "idempotency_key": idempotency_key}
        identity = self.resolve_identity(session)
        lock_key = f"{identity.session_id}:{identity.pane_id}" if identity is not None else f"name:{session}"
        with self._pane_locks.get(lock_key):
            result = self._send_text_and_verify_locked(session, text, press_enter)
        if idempotency_key is not None:
            self.audit.store_idempotent_result(idempotency_key, result)
        return result

    def _send_text_and_verify_locked(self, session: str, text: str, press_enter: bool) -> dict[str, Any]:
        """Send `text` (and, if requested, Enter) through the tmux layer,
        then make a bounded, best-effort attempt to confirm Enter actually
        *submitted* rather than merely having been typed. This is the fix
        for the intermittent "text fully typed but does not execute until
        a human presses Enter" bug: `sent: True` alone was never proof of
        submission (tmux send-keys succeeding only proves the bytes were
        written to the pty, not that the receiving program acted on them),
        so callers must not treat it as one.

        Returns `sent` (the tmux-level send itself succeeded -- unchanged
        meaning from before this fix) plus a new `submit_status`:
          - "TEXT_SENT": press_enter was False. No submission was
            attempted, so there is nothing to confirm -- unchanged
            behavior, just a name for it.
          - "SUBMIT_CONFIRMED": press_enter was True and the pane shows
            evidence consistent with the Enter having been processed
            (the previously-typed text is no longer sitting as unconsumed
            trailing content, or the tail of the pane changed some other
            way in the verification window).
          - "SUBMIT_UNCONFIRMED": press_enter was True but verification
            could not confirm submission within SEND_VERIFY_TIMEOUT_SECONDS
            (the typed text still visibly sits unconsumed, or the pane's
            tail never changed at all, or a post-send capture itself
            failed). This is a deliberately conservative default: an
            inconclusive result is reported as unconfirmed, never silently
            upgraded to success.

        Never auto-retries Enter -- a second Enter risks a genuine double
        submission (e.g. accepting a destructive confirmation prompt
        twice), which is strictly worse than an honest UNCONFIRMED status
        that a caller (a human, or Supervisor v2 -- see supervisor2.py's
        execute_send) can act on deliberately.

        Verification method: capture the pane's tail exactly as it looks
        right after the text lands but *before* Enter is sent (the
        unambiguous "typed, not yet submitted" reference for this specific
        text), then poll after Enter until that exact snapshot changes.
        Deliberately NOT a substring/marker match against the sent text --
        a real target's own confirmation output can legitimately echo the
        submitted text back (e.g. "SUBMITTED: <text>"), which would falsely
        look like "still pending" under a marker-suffix check. Comparing
        against the precise pre-Enter snapshot has no such false positive:
        it only reports CONFIRMED once the pane has genuinely moved on
        from exactly what "typed but not submitted" looked like.
        """
        self.tmux.send_text(session, text, press_enter=False)
        result: dict[str, Any] = {"sent": True, "characters": len(text), "press_enter": press_enter}
        if not press_enter:
            result["submit_status"] = "TEXT_SENT"
            return result
        try:
            typed_snapshot = self.tmux.capture_lines(session, SEND_VERIFY_LINES)
        except TmuxError:
            typed_snapshot = None
        # Same fixed settle window tmux.send_text itself uses for a
        # press_enter=True call -- imported, not duplicated, so there is
        # exactly one place that value is decided.
        time.sleep(SEND_TEXT_ENTER_SETTLE_SECONDS)
        self.tmux.send_keys(session, ["Enter"])
        if typed_snapshot is None:
            # No reliable pre-Enter baseline to diff against -- verification
            # itself is compromised (a capture failure right after a
            # successful send is unusual but possible, e.g. a fast-closing
            # session). Report unconfirmed rather than guess either way.
            result["submit_status"] = "SUBMIT_UNCONFIRMED"
            result["submit_reason"] = "could not capture a pre-submit baseline to verify against"
            return result
        deadline = time.monotonic() + SEND_VERIFY_TIMEOUT_SECONDS
        after: list[str] | None = None
        while True:
            try:
                after = self.tmux.capture_lines(session, SEND_VERIFY_LINES)
            except TmuxError:
                after = None
                break
            if after != typed_snapshot:
                result["submit_status"] = "SUBMIT_CONFIRMED"
                return result
            if time.monotonic() >= deadline:
                break
            time.sleep(SEND_VERIFY_POLL_INTERVAL_SECONDS)
        result["submit_status"] = "SUBMIT_UNCONFIRMED"
        result["submit_reason"] = (
            "post-send capture failed" if after is None
            else "the pane looked identical to its pre-Enter state throughout the verification window"
        )
        return result

    def terminal_send_text(self, session: str, text: str, press_enter: bool = False,
                           dry_run: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
        """idempotency_key (P0-4, optional): if provided, a repeat call
        with the same key never sends twice -- it returns the original
        stored result instead, durable across a process restart. Manual/
        dashboard callers can generate one (e.g. a UUID) for this
        guarantee; omitted entirely, behavior is unchanged from before."""
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
            response = {"session": session,
                        **self._send_text_and_verify(session, text, press_enter, idempotency_key=idempotency_key)}
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
            info = self.tmux.get_session(session)
            if info is None:
                return {"error": "SESSION_NOT_FOUND", "binding": binding, "session": session}
        except TmuxError as exc:
            return {"error": "SESSION_NOT_FOUND", "binding": binding, "session": session, "reason": str(exc)}
        # P0-2: pin identity at bind (and rebind) time -- a bind/rebind is
        # the explicit "pin to whatever this session is right now" action.
        stored, changed = self.bindings.put(
            binding, session, read_enabled=read_enabled,
            input_enabled=input_enabled, replace=replace,
            pinned_session_id=info.session_id, pinned_pane_id=info.pane_id,
            pinned_created_epoch=info.created_epoch,
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

    def _check_binding_identity(self, binding: str, stored: Binding) -> dict[str, Any] | None:
        """P0-2: refuse to send through a binding whose pinned identity no
        longer matches what currently answers to its session name -- the
        name may have been recycled onto an unrelated tmux session, or its
        pane replaced. A binding pinned before this feature existed (or
        whose identity couldn't be resolved at bind time) has no pin yet;
        its first successful send after upgrade lazily adopts whatever
        identity is live *then* rather than being blocked outright, so
        this upgrade never silently breaks every pre-existing binding.
        Returns None if the send may proceed."""
        current = self.resolve_identity(stored.session)
        if stored.pinned_session_id is None:
            if current is not None:
                self.bindings.adopt_pin(binding, pinned_session_id=current.session_id,
                                        pinned_pane_id=current.pane_id,
                                        pinned_created_epoch=current.created_epoch)
            return None
        pinned = SessionIdentity(name=stored.session, session_id=stored.pinned_session_id,
                                 pane_id=stored.pinned_pane_id or "",
                                 created_epoch=stored.pinned_created_epoch or 0)
        if current is None or not pinned.matches(current):
            return {
                "error": "IDENTITY_MISMATCH", "binding": binding, "session": stored.session,
                "reason": "the session/pane this binding was created for no longer matches "
                          "what currently answers to that session name -- rebind explicitly "
                          "to accept the new target",
            }
        return None

    def terminal_send_bound(self, binding: str, text: str, press_enter: bool = False,
                            dry_run: bool = False, idempotency_key: str | None = None) -> dict[str, Any]:
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
        elif (identity_error := self._check_binding_identity(binding, stored)) is not None:
            response = identity_error
        else:
            try:
                response = {"binding": binding, "session": stored.session,
                            **self._send_text_and_verify(stored.session, text, press_enter,
                                                         idempotency_key=idempotency_key)}
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
