"""Compact, high-level terminal operations built from existing guarded APIs."""
from __future__ import annotations

import math
import os
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from .redaction import redact_text


MAX_TARGETS = 25
MAX_TAIL_LINES = 20
MAX_TAIL_CHARS_PER_TARGET = 1_000
MAX_TOTAL_TAIL_CHARS = 16_000
MAX_REASON_CHARS = 500
SYNC_WAIT_BUDGET_SECONDS = min(30, max(1, int(os.environ.get("MCP_LONG_CALL_MAX_SEC", "20"))))
DEFAULT_WAIT_SECONDS = 20
MAX_SEND_WAIT_SECONDS = 20
# Compatibility field retained for existing clients. New clients should use
# retry_after_ms below, which deliberately reduces polling pressure.
NEXT_POLL_MIN_MS = 1_000
RECOMMENDED_RETRY_AFTER_MS = 5_000
_RESUME_TOKEN = re.compile(r"^wait_[0-9a-f]{32}$")
_MAX_TARGET_CHARS = 512

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
                 run_journal: Any = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.terminal = terminal
        self.controller = controller
        self.run_journal = run_journal
        self.monotonic = monotonic
        self.sleep = sleep

    def _resolve(self, target: str) -> tuple[str, str] | tuple[None, dict[str, Any]]:
        if (not isinstance(target, str) or not target.strip()
                or len(target) > _MAX_TARGET_CHARS):
            return None, {"error": "INVALID_TARGET"}
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

    def _status(self, target: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        kind, value = self._resolve(target)
        if kind is None:
            return value
        if kind == "binding":
            result = self.terminal.terminal_status_bound(value)
        elif timeout_seconds is not None and hasattr(self.controller, "terminal_status_bounded"):
            result = self.controller.terminal_status_bounded(value, timeout_seconds)
        else:
            result = self.controller.terminal_status(value)
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
        if (not isinstance(targets, list) or not targets or len(targets) > MAX_TARGETS
                or any(not isinstance(target, str) or not target.strip()
                       or len(target) > _MAX_TARGET_CHARS for target in targets)):
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
                  timeout: float = 20, idempotency_key: str | None = None) -> dict[str, Any]:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= MAX_SEND_WAIT_SECONDS:
            return {"status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 20"}
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

    @staticmethod
    def _validate_wait(timeout: float, poll_interval: float) -> dict[str, Any] | None:
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            return {"status": "FAILED", "error": "INVALID_TIMEOUT", "allowed": "timeout > 0"}
        if (isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float))
                or not math.isfinite(poll_interval) or poll_interval < 1):
            return {"status": "FAILED", "error": "INVALID_POLL_INTERVAL", "minimum": 1}
        return None

    @staticmethod
    def _validate_resume_token(resume_token: str) -> bool:
        return isinstance(resume_token, str) and bool(_RESUME_TOKEN.fullmatch(resume_token))

    @staticmethod
    def _checkpoint_id(wait: dict[str, Any]) -> str:
        return f"{wait['run_id']}:{wait['checkpoint_version']}"

    def _durable_result(self, wait: dict[str, Any], *, waited_ms: int = 0,
                        polls: int = 0, tail: str = "", tail_truncated: bool = False) -> dict[str, Any]:
        status = wait["status"]
        result = {
            # MATCHED is retained for existing clients. continuation_status is
            # the durable contract's terminal/pending vocabulary.
            "status": status,
            "continuation_status": "COMPLETE" if status == "MATCHED" else status,
            "target": wait["target"],
            "target_type": wait["target_type"],
            "run_id": wait["run_id"],
            "task_id": wait["run_id"],
            "resume_token": wait["resume_token"],
            "checkpoint_id": self._checkpoint_id(wait),
            "desired_states": wait["desired_states"],
            "last_observed_state": wait["last_observed_state"] or "UNKNOWN",
            # Compatibility aliases from the original response.
            "state": wait["last_observed_state"] or "UNKNOWN",
            "input_required": bool(wait["input_required"]),
            "reason": wait["reason"],
            "polls": polls,
            "total_polls": wait["polls"],
            "waited_ms": waited_ms,
            "elapsed_ms": wait["waited_ms"],
            "elapsed_seconds": round(wait["waited_ms"] / 1000, 3),
            "sync_wait_budget_ms": SYNC_WAIT_BUDGET_SECONDS * 1000,
            "requested_timeout_seconds": wait["requested_timeout_seconds"],
            "pending_return_count": wait["pending_return_count"],
            "tail": tail,
            "tail_truncated": tail_truncated,
            "untrusted_output": True,
            "untrusted_fields": ["tail"],
        }
        if status == "PENDING":
            result.update({
                "next_poll_after_ms": NEXT_POLL_MIN_MS,
                "retry_after_ms": RECOMMENDED_RETRY_AFTER_MS,
                "pending_reason": "SYNC_WAIT_BUDGET_EXHAUSTED",
                "next_action": "Call terminal_resume_wait with resume_token",
            })
        return result

    def _wait_slice(self, wait: dict[str, Any], *, timeout: float,
                    poll_interval: float) -> dict[str, Any]:
        # Terminal results are immutable and returned from SQLite without
        # consulting or redispatching the underlying target.
        if wait["status"] in {"MATCHED", "FAILED"}:
            return self._durable_result(wait)

        slice_seconds = min(float(timeout), float(SYNC_WAIT_BUDGET_SECONDS))
        started = self.monotonic()
        deadline = started + slice_seconds
        polls = 0
        final: dict[str, Any] = {}
        matched = False
        probe_timed_out = False
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0 and polls:
                break
            polls += 1
            final = self._status(wait["target"], timeout_seconds=max(0.001, remaining))
            if final.get("error") == "STATUS_PROBE_TIMEOUT":
                probe_timed_out = True
                final = {"state": wait.get("last_observed_state") or "UNKNOWN",
                         "reason": "status probe exhausted synchronous wait budget"}
                break
            if "error" in final:
                break
            if str(final.get("state", "UNKNOWN")).upper() in set(wait["desired_states"]):
                matched = True
                break
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            self.sleep(min(float(poll_interval), remaining))

        waited_ms = max(0, round((self.monotonic() - started) * 1000))
        state = str(final.get("state", "UNKNOWN"))
        reason = redact_text(str(final.get("reason") or final.get("error") or ""))
        status = "MATCHED" if matched else ("FAILED" if "error" in final else "PENDING")
        if probe_timed_out and status == "PENDING" and not reason:
            reason = "status probe exhausted synchronous wait budget"
        saved = self.run_journal.record_wait_observation(
            wait["resume_token"], status=status, last_observed_state=state,
            input_required=bool(final.get("input_required", False)), reason=reason,
            polls=polls, waited_ms=waited_ms,
        )
        # Preserve the useful final tail for the legacy MATCHED shape. A
        # PENDING slice deliberately does no extra remote read after its wait
        # budget expires, keeping both latency and response size predictable.
        tail: dict[str, Any] = {}
        rendered = ""
        clipped = False
        if status == "MATCHED":
            tail = self._tail(wait["target"], wait["tail_lines"])
            rendered, clipped = _bounded(
                redact_text(str(tail.get("output", ""))) if "error" not in tail else "",
                MAX_TAIL_CHARS_PER_TARGET,
            )
        return self._durable_result(
            saved, waited_ms=waited_ms, polls=polls, tail=rendered,
            tail_truncated=bool(clipped or tail.get("truncated", False)),
        )

    def wait_for_state(self, target: str, desired_states: list[str], timeout: float = DEFAULT_WAIT_SECONDS,
                       poll_interval: float = 1, tail_lines: int = 20) -> dict[str, Any]:
        if (not isinstance(target, str) or not target.strip() or len(target) > _MAX_TARGET_CHARS
                or redact_text(target) != target):
            return {"status": "FAILED", "error": "INVALID_TARGET"}
        if (not isinstance(desired_states, list) or not desired_states or len(desired_states) > 20 or
                any(not isinstance(state, str) or not state.strip() or len(state) > 64
                    or redact_text(state) != state for state in desired_states)):
            return {"status": "FAILED", "error": "INVALID_DESIRED_STATES"}
        if error := self._validate_wait(timeout, poll_interval):
            return error
        if error := self._validate_tail_lines(tail_lines):
            return {"status": "FAILED", **error}
        if self.run_journal is None:
            return {"status": "FAILED", "error": "CONTINUATION_STORE_UNAVAILABLE"}
        kind, _value = self._resolve(target)
        if kind is None:
            return {"status": "FAILED", "error": "INVALID_TARGET", "target": target}
        normalized_states = list(dict.fromkeys(state.strip().upper() for state in desired_states))
        try:
            # Persist first. No status polling/sleep occurs until this durable
            # record and its opaque resume token have committed.
            wait = self.run_journal.start_wait(
                target=target.strip(), target_type=kind,
                desired_states=normalized_states, tail_lines=tail_lines,
                requested_timeout_seconds=float(timeout),
            )
        except Exception as exc:  # noqa: BLE001 -- persistence is mandatory here
            return {"status": "FAILED", "error": "CONTINUATION_PERSIST_FAILED",
                    "reason": type(exc).__name__}
        return self._wait_slice(wait, timeout=timeout, poll_interval=poll_interval)

    def resume_wait(self, resume_token: str, timeout: float = DEFAULT_WAIT_SECONDS,
                    poll_interval: float = 1) -> dict[str, Any]:
        if not self._validate_resume_token(resume_token):
            return {"status": "FAILED", "error": "INVALID_RESUME_TOKEN"}
        if error := self._validate_wait(timeout, poll_interval):
            return error
        if self.run_journal is None:
            return {"status": "FAILED", "error": "CONTINUATION_STORE_UNAVAILABLE"}
        try:
            wait = self.run_journal.get_wait(resume_token)
        except KeyError:
            return {"status": "FAILED", "error": "UNKNOWN_RESUME_TOKEN"}
        except TimeoutError:
            return {"status": "FAILED", "error": "EXPIRED_RESUME_TOKEN"}
        return self._wait_slice(wait, timeout=timeout, poll_interval=poll_interval)
