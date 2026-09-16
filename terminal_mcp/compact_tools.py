"""Compact, high-level terminal operations built from existing guarded APIs."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any


MAX_TARGETS = 25
MAX_TAIL_LINES = 20
MAX_TAIL_CHARS_PER_TARGET = 1_000
MAX_TOTAL_TAIL_CHARS = 16_000
MAX_REASON_CHARS = 500
MAX_WAIT_SECONDS = 900
MAX_SEND_WAIT_SECONDS = 30

_BLOCKED_ERRORS = {
    "ACCESS_DENIED", "ACTION_NOT_ALLOWED", "BINDING_INPUT_DISABLED",
    "BINDING_NOT_PINNED", "GRANT_REQUIRED", "IDENTITY_MISMATCH",
    "INPUT_DISABLED", "PANE_IN_COPY_MODE", "TARGET_AWAITING_APPROVAL",
}


def _bounded(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    if limit <= 0:
        return "", True
    return text[:max(0, limit - 1)] + "…", True


class CompactTerminalTools:
    """Composition layer; all authorization and submission stays downstream."""

    def __init__(self, terminal: Any, controller: Any, *,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.terminal = terminal
        self.controller = controller
        self.monotonic = monotonic
        self.sleep = sleep

    def _resolve(self, target: str) -> tuple[str, str] | tuple[None, dict[str, Any]]:
        if not isinstance(target, str) or not target.strip():
            return None, {"error": "INVALID_TARGET", "target": target}
        target = target.strip()
        if target.startswith("binding:"):
            name = target.removeprefix("binding:")
            if not name:
                return None, {"error": "INVALID_TARGET", "target": target}
            return "binding", name
        if target.startswith("session:"):
            name = target.removeprefix("session:")
            if not name:
                return None, {"error": "INVALID_TARGET", "target": target}
            return "session", name
        binding = self.terminal.terminal_get_binding(target)
        if isinstance(binding, dict) and "error" not in binding:
            return "binding", target
        return "session", target

    def _status(self, target: str) -> dict[str, Any]:
        kind, value = self._resolve(target)
        if kind is None:
            return value
        result = (self.terminal.terminal_status_bound(value) if kind == "binding"
                  else self.controller.terminal_status(value))
        return {"target": target, "target_type": kind, **result}

    def _tail(self, target: str, lines: int) -> dict[str, Any]:
        kind, value = self._resolve(target)
        if kind is None:
            return value
        result = (self.terminal.terminal_tail_bound(value, lines) if kind == "binding"
                  else self.controller.terminal_tail(value, lines))
        return {"target": target, "target_type": kind, **result}

    @staticmethod
    def _validate_tail_lines(tail_lines: int) -> dict[str, Any] | None:
        if isinstance(tail_lines, bool) or not isinstance(tail_lines, int) or not 1 <= tail_lines <= MAX_TAIL_LINES:
            return {"error": "INVALID_TAIL_LINES", "allowed": "1..20"}
        return None

    def batch_inspect(self, targets: list[str], tail_lines: int = 20,
                      compact: bool = True) -> dict[str, Any]:
        if not isinstance(targets, list) or not targets or len(targets) > MAX_TARGETS:
            return {"error": "INVALID_TARGETS", "max_targets": MAX_TARGETS}
        if error := self._validate_tail_lines(tail_lines):
            return error
        remaining = MAX_TOTAL_TAIL_CHARS
        rows: list[dict[str, Any]] = []
        response_truncated = False
        for target in targets:
            status = self._status(target)
            if "error" in status:
                reason, clipped = _bounded(status.get("reason") or status["error"], MAX_REASON_CHARS)
                rows.append({"target": target, "error": status["error"], "reason": reason})
                response_truncated |= clipped
                continue
            tail = self._tail(target, tail_lines)
            raw_tail = tail.get("output", "") if "error" not in tail else ""
            allowed = min(MAX_TAIL_CHARS_PER_TARGET, max(0, remaining))
            rendered, clipped = _bounded(raw_tail, allowed)
            remaining -= len(rendered)
            reason, reason_clipped = _bounded(status.get("reason"), MAX_REASON_CHARS)
            row = {
                "target": target,
                "target_type": status.get("target_type"),
                "session": status.get("session"),
                "state": status.get("state", "UNKNOWN"),
                "input_required": bool(status.get("input_required", False)),
                "reason": reason,
                "tail": rendered,
                "tail_truncated": bool(clipped or tail.get("truncated", False)),
            }
            if not compact:
                row["exists"] = status.get("exists")
                row["cwd"] = status.get("cwd")
            if "error" in tail:
                row["tail_error"] = tail["error"]
            rows.append(row)
            response_truncated |= row["tail_truncated"] or reason_clipped
        return {
            "targets": rows,
            "count": len(rows),
            "response_truncated": response_truncated,
            "limits": {"max_targets": MAX_TARGETS, "tail_lines": MAX_TAIL_LINES,
                       "per_target_chars": MAX_TAIL_CHARS_PER_TARGET,
                       "total_tail_chars": MAX_TOTAL_TAIL_CHARS},
            "untrusted_output": True,
            "untrusted_fields": ["targets[].tail"],
        }

    def send_task(self, target: str, text: str, wait_for_accept: bool = True,
                  timeout: float = 30, idempotency_key: str | None = None) -> dict[str, Any]:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_SEND_WAIT_SECONDS:
            return {"status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 30"}
        kind, value = self._resolve(target)
        if kind is None:
            return {"status": "FAILED", **value}
        # One guarded send call owns WRITE + ACTIVATE + PROVE_ACCEPTED.
        # It sends Enter at most once (or uses the existing Codex watchdog),
        # applies menu/identity guards, and persists the idempotent receipt.
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or
                                            not idempotency_key or len(idempotency_key) > 200 or
                                            "\x00" in idempotency_key):
            return {"status": "FAILED", "error": "INVALID_IDEMPOTENCY_KEY"}
        effective_key = idempotency_key or f"compact-task:{uuid.uuid4()}"
        result = (self.terminal.terminal_send_bound(
                    value, text, True, False, effective_key)
                  if kind == "binding" else
                  self.controller.terminal_send_text(
                    value, text, True, False, idempotency_key=effective_key))
        delivery = result.get("delivery_state") or result.get("submit_status")
        if delivery == "SUBMIT_CONFIRMED":
            status = "SUBMIT_CONFIRMED"
        elif delivery == "BLOCKED" or result.get("error") in _BLOCKED_ERRORS:
            status = "BLOCKED"
        else:
            status = "FAILED"
        reason, clipped = _bounded(result.get("submit_reason") or result.get("reason") or result.get("error"),
                                   MAX_REASON_CHARS)
        evidence = {
            key: result[key] for key in
            ("delivery_state", "submit_status", "agent_type", "enter_count", "attempts", "enter_sent")
            if key in result
        }
        return {
            "status": status,
            "target": target,
            "target_type": kind,
            "session": result.get("session"),
            "correlation_id": result.get("correlation_id"),
            "submission_id": result.get("submission_id") or result.get("correlation_id"),
            "reason": reason,
            "evidence": evidence,
            "evidence_truncated": clipped,
            "wait_for_accept": bool(wait_for_accept),
            "timeout": timeout,
        }

    def wait_for_state(self, target: str, desired_states: list[str], timeout: float = 900,
                       poll_interval: float = 1, tail_lines: int = 20) -> dict[str, Any]:
        if (not isinstance(desired_states, list) or not desired_states or len(desired_states) > 20 or
                any(not isinstance(state, str) or not state.strip() or len(state) > 64 for state in desired_states)):
            return {"status": "FAILED", "error": "INVALID_DESIRED_STATES"}
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_WAIT_SECONDS:
            return {"status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 900"}
        if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)) or poll_interval < 1:
            return {"status": "FAILED", "error": "INVALID_POLL_INTERVAL", "minimum": 1}
        if error := self._validate_tail_lines(tail_lines):
            return {"status": "FAILED", **error}
        desired = {state.strip().upper() for state in desired_states}
        deadline = self.monotonic() + timeout
        polls = 0
        final: dict[str, Any] = {}
        matched = False
        while True:
            polls += 1
            final = self._status(target)
            if "error" in final:
                break
            if str(final.get("state", "UNKNOWN")).upper() in desired:
                matched = True
                break
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(float(poll_interval), remaining))
        tail = self._tail(target, tail_lines)
        rendered, clipped = _bounded(tail.get("output", "") if "error" not in tail else "",
                                     MAX_TAIL_CHARS_PER_TARGET)
        reason, reason_clipped = _bounded(final.get("reason") or final.get("error"), MAX_REASON_CHARS)
        return {
            "status": "MATCHED" if matched else ("FAILED" if "error" in final else "TIMEOUT"),
            "target": target,
            "state": final.get("state", "UNKNOWN"),
            "input_required": bool(final.get("input_required", False)),
            "reason": reason,
            "polls": polls,
            "elapsed_seconds": round(timeout - max(0.0, deadline - self.monotonic()), 3),
            "tail": rendered,
            "tail_truncated": bool(clipped or tail.get("truncated", False) or reason_clipped),
            "untrusted_output": True,
            "untrusted_fields": ["tail"],
        }
