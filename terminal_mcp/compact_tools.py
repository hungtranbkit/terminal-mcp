"""Compact, high-level terminal operations built from existing guarded APIs."""
from __future__ import annotations

import math
import os
import re
import time
import threading
from concurrent.futures import Future, TimeoutError as FutureTimeout
import uuid
from collections.abc import Callable
from typing import Any

from .redaction import redact_text


MAX_TARGETS = 25
MAX_TAIL_LINES = 20
MAX_TAIL_CHARS_PER_TARGET = 1_000
MAX_TOTAL_TAIL_CHARS = 16_000
MAX_REASON_CHARS = 500
SYNC_WAIT_BUDGET_SECONDS = min(15, max(1, int(os.environ.get("MCP_LONG_CALL_MAX_SEC", "10"))))
DEFAULT_WAIT_SECONDS = 20
MAX_SEND_WAIT_SECONDS = 20
# Compatibility field retained for existing clients. New clients should use
# retry_after_ms below, which deliberately reduces polling pressure.
NEXT_POLL_MIN_MS = 1_000
RECOMMENDED_RETRY_AFTER_MS = 5_000
_RESUME_TOKEN = re.compile(r"^wait_[0-9a-f]{32}$")
_MAX_TARGET_CHARS = 512

# --------------------------------------------------------------------------
# `turn`'s action vocabulary.
#
# WHY IT IS THIS WIDE. The compact ChatGPT surface advertises ONE tool, so
# every normal orchestration step has to be reachable as an action here --
# otherwise the connector needs a second advertised tool and the user is back
# to a wall of "Called tool" rows. The five pane actions below are implemented
# in this module; the rest are routed to handlers injected by mcp_app (see
# CompactTerminalTools.handlers), because re-implementing session lifecycle or
# durable queueing here would be a second source of truth for authorization
# and idempotency. This table is the single place the vocabulary is defined --
# the MCP tool layer validates nothing of its own.
# --------------------------------------------------------------------------
TURN_PANE_ACTIONS = ("inspect", "send", "send_wait", "wait", "resume", "start")
# How many QueueEngine ticks one `start` call may spend driving the lane from
# QUEUED to actually dispatched. The engine makes at most ONE transition per
# tick on purpose (claim -> coordinator gate -> dispatch), so a handful is the
# whole start sequence with room for one reconcile; it is a bound on the
# CALLER'S wait, not on the task, which is durable from the first step and is
# carried the rest of the way server-side.
MAX_START_TICKS = 6

# A tick count is not a bound on time, and MAX_START_TICKS above claims to be
# one ("a bound on the CALLER'S wait"). It only bounds TRANSITIONS: when the
# lane is quiet six ticks are a couple of seconds, and when it is busy the same
# six become minutes. Measured on hp-linux 2026-09-21 over 12 real calls:
# `turn action=start` ran p50 92s, p90 179s, max 244s, while every other turn
# action stayed under 10s (send_wait p50 2.5s) and the durable queue path
# answered in 2ms.
#
# The cost is not the waiting itself -- the task is durable from step 1 and the
# follower carries it either way -- it is that a four-minute MCP call cannot
# share a client turn with anything else, which is why one conversation
# accumulated hundreds of separate tool blocks.
#
# So the caller's wait gets a real clock. This is a budget, not a hard ceiling:
# a tick already in flight still finishes, so the worst case is the budget plus
# one tick. Bounding it exactly would mean interrupting a transition mid-way,
# which is precisely what persist-before-dispatch exists to avoid.
# 0 = do not drive the lane on the caller's thread at all: enqueue, hand to
# the follower, return the task_id. The receipt already tells the truth in
# that case (durably queued, execution not yet confirmed),
# because the task is durable from step 1 and the follower carries it whether
# or not the caller stays -- driving ticks here only ever bought an earlier
# `dispatched: True`, and 2026-09-21 measurement priced that convenience at a
# p50 of 92 seconds and a worst case of 244.
#
# The client already works this way: terminal_task_status answers in 2ms and
# ChatGPT calls it 55 times an hour. It does not need this call to block to
# learn the outcome; it needs this call to return so the turn can hold more
# than one thing.
#
# Raise it to re-enable in-call driving (the loop below still honours both
# this budget and MAX_START_TICKS); the fast path is preserved, not deleted.
START_WAIT_BUDGET_SECONDS = 0.0
#: The task is genuinely UNDER WAY -- the only states that may be reported as
#: `dispatched: True`. Found live (hp-linux, 2026-09-19): a single "settled"
#: set conflated these with the refusal states below, so a task the coordinator
#: had just BLOCKED would have come back `dispatched: True`.
START_UNDERWAY_STATUSES = frozenset({
    "DISPATCHING", "RUNNING", "VERIFYING", "COMPLETED", "SKIPPED",
})
#: Stopped, and only a human can move it. `start` must NOT tell the client to
#: stop watching as though work were progressing -- also found live: the
#: coordinator gate paused the lane ("session X is already actively working in
#: the same repo/worktree") and the receipt still read "work is started and
#: tracked server-side", which is how an orchestrator silently drops a task.
START_NEEDS_HUMAN_STATUSES = frozenset({
    "PAUSED", "BLOCKED", "NEEDS_HUMAN", "NEEDS_REWORK", "FAILED", "CANCELLED",
})
#: Waiting on something the server itself will retry -- neither under way nor
#: a human's problem yet.
START_SERVER_PENDING_STATUSES = frozenset({"WAITING_SESSION", "DISPATCH_UNCERTAIN"})
#: `start` stops ticking at any of these: one more tick in this call would tell
#: the caller nothing the server will not handle (or nothing a human has not
#: already been asked for).
START_SETTLED_STATUSES = (START_UNDERWAY_STATUSES | START_NEEDS_HUMAN_STATUSES
                          | START_SERVER_PENDING_STATUSES)
# action -> handler key in CompactTerminalTools.handlers
# `compact=True` is terminal_turn's DEFAULT, but until 2026-09-21 only
# batch_inspect honoured it -- list_nodes and list_sessions were dispatched as
# `lambda: handler()`, dropping the flag entirely. Measured that day on hp:
# list_nodes returned 6 nodes x 57 fields = 13,655 characters and
# list_sessions 54 sessions x 21 fields = 34,104, almost all of it
# disk byte counts, probe timestamps and contract capability lists. A caller
# that cannot find `status` in that wall asks again, which is precisely the
# "many Called tool rows, no information" complaint. Projected here rather
# than upstream so the FULL endpoint keeps returning everything, and
# compact=False remains a complete escape hatch on this surface too.
_NODE_COMPACT_FIELDS = (
    "id", "status", "platform", "capacity_status", "draining",
    "tmux_session_count", "cpu_percent", "ram_percent",
    "claude_available", "codex_available", "last_error",
)
_SESSION_COMPACT_FIELDS = (
    "name", "node_id", "attached", "activity", "allowed",
    "effective_read", "effective_input",
)


def _slim(row: Any, fields: tuple[str, ...]) -> Any:
    """`row` reduced to `fields`, dropping keys it does not have and any
    null-valued extra. Percentages are rounded: `cpu_percent:
    19.801976426529997` spends 16 characters saying nothing a caller acts on."""
    if not isinstance(row, dict):
        return row
    out: dict[str, Any] = {}
    for key in fields:
        if key not in row:
            continue
        value = row[key]
        if isinstance(value, float):
            value = round(value, 1)
        out[key] = value
    # Keep a refusal reason only when there IS one -- it changes what the
    # caller may do next, so dropping it would force another call.
    reason = row.get("input_denied_reason")
    if reason:
        out["input_denied_reason"] = reason
    # A whitelist that matched NOTHING means this is a shape it does not
    # recognise -- a different projection upstream, a synthetic row, a future
    # field rename. Returning {} there would destroy the caller's data to save
    # bytes, which is strictly worse than not projecting at all, so hand the
    # row back untouched and let the caller see what is really there.
    if not out:
        return row
    return out


def _project_nodes(result: Any, compact: bool) -> Any:
    if not compact or not isinstance(result, list):
        return result
    return [_slim(node, _NODE_COMPACT_FIELDS) for node in result]


def _project_sessions(result: Any, compact: bool) -> Any:
    if not compact or not isinstance(result, dict):
        return result
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        return result
    projected = dict(result)
    projected["sessions"] = [_slim(row, _SESSION_COMPACT_FIELDS) for row in sessions]
    return projected


TURN_HANDLER_ACTIONS: dict[str, str] = {
    "put_file": "put_file",
    "list_sessions": "list_sessions",
    "list_nodes": "list_nodes",
    "create_session": "create_session",
    "delete_session": "delete_session",
    "enqueue_task": "enqueue_task",
    # TMCP-TASK-ROUTER-001. `start` needs a target; this one finds it. The
    # two are deliberately separate actions rather than one action with an
    # optional target: `start(target=...)` is HARD AFFINITY and must never
    # silently reroute, so a caller that omitted a target by mistake should
    # get TARGET_REQUIRED, not a different session than it expected.
    "route_start": "route_start",
    # TMCP-AGENT-RUNTIME-001 Phase B. The Agent/Skill runtime has to be
    # reachable from the ONE advertised tool, or a connector needs a second
    # control plane to use it -- the exact thing this surface exists to avoid.
    "agent_start": "agent_start",
    "list_agents": "list_agents",
    "get_agent": "get_agent",
    "create_agent": "create_agent",
    "update_agent": "update_agent",
    "list_skills": "list_skills",
    "register_skill": "register_skill",
    "bind_agent_skill": "bind_agent_skill",
    # Read-only: proposes, never deletes. See stale_sessions.py.
    "cleanup_candidates": "cleanup_candidates",
    # TMCP-PROJECT-BOOTSTRAP-001. One logical operation, one turn call.
    "project_plan": "project_plan",
    "project_bootstrap": "project_bootstrap",
    "project_list": "project_list",
    "project_get": "project_get",
    "project_update": "project_update",
    "project_archive": "project_archive",
    "project_phase_status": "project_phase_status",
    "project_advance": "project_advance",
    "project_reconcile_team": "project_reconcile_team",
    "project_start": "project_start",
    "project_recover": "project_recover",
    "task_status": "task_status",
    "task_batch_status": "task_batch_status",
    "task_checkpoint": "task_checkpoint",
    # TMCP-HARNESS-001. The harness is reachable ONLY from here. There is
    # deliberately no standalone harness_* tool: a second surface would be a
    # second place for "may this write the task status" to be decided, and
    # the entire feature is about that question having one answer. Every
    # value below is the same HarnessService method the dashboard routes
    # call, so the one-tool path is never weaker than any other.
    "harness_start": "harness_start",
    "harness_status": "harness_status",
    "harness_resume": "harness_resume",
    "harness_cancel": "harness_cancel",
    "harness_review": "harness_review",
    # Browser gateway (TMCP-BROWSER-GATEWAY-001). The browser has to be
    # reachable from the ONE-tool ChatGPT surface, or ChatGPT would need a
    # second control plane to use it -- which is the exact thing this
    # feature exists to avoid. Same rule as every other routed action: the
    # value is the SAME function the standalone tool is registered from.
    "browser_status": "browser_status",
    "browser_verify": "browser_verify",
    "browser_screenshot": "browser_screenshot",
    "browser_run_task": "browser_run_task",
    "browser_stop": "browser_stop",
}
#: Handler keys `start` composes. They are injected exactly like the ones in
#: TURN_HANDLER_ACTIONS (mcp_app wires them to the very same implementations
#: the standalone tools use) but they are not actions of their own -- a caller
#: never asks for "tick"; it asks for `start` and the server decides how many
#: steps that takes.
START_HANDLER_KEYS = ("enqueue_task", "task_status", "dispatch_tick", "follow_task")
# Short spellings a caller reaches for first. Resolved before dispatch so the
# canonical name is the only thing the routing below has to know about.
TURN_ACTION_ALIASES: dict[str, str] = {
    "list": "list_sessions",
    "sessions": "list_sessions",
    "nodes": "list_nodes",
    "create": "create_session",
    "delete": "delete_session",
    "kill": "delete_session",
    "enqueue": "enqueue_task",
    "task": "task_status",
    "tasks": "task_batch_status",
    "browser": "browser_status",
    "verify": "browser_verify",
    "screenshot": "browser_screenshot",
    "start_auto": "route_start",
    "agent": "get_agent",
    "agents": "list_agents",
    "skills": "list_skills",
    "skill": "register_skill",
    "bind": "bind_agent_skill",
    "bind_skill": "bind_agent_skill",
    "run_agent": "agent_start",
    "cleanup": "cleanup_candidates",
    "projects": "project_list",
    "project": "project_get",
    "plan": "project_plan",
    "bootstrap": "project_bootstrap",
    "phase": "project_phase_status",
    "advance": "project_advance",
    "reconcile": "project_reconcile_team",
    "recover": "project_recover",
    "pm_recover": "project_recover",
    "new_project": "project_bootstrap",
    "stale_sessions": "cleanup_candidates",
    "auto": "route_start",
    "route": "route_start",
    # Short spellings for the harness. `harness` alone reads the status,
    # because asking about a run is overwhelmingly the common case and a
    # bare noun should never be the one that STARTS something.
    "harness": "harness_status",
    "harness_runs": "harness_status",
    "review": "harness_review",
    "start_task": "start",
    "dispatch": "start",
    "run": "start",
}
TURN_ACTIONS = (*TURN_PANE_ACTIONS, *TURN_HANDLER_ACTIONS)

BROWSER_ARGS: dict[str, frozenset[str]] = {
    "browser_verify": frozenset({"url", "assertions", "viewport_width", "viewport_height",
                                   "timeout_seconds", "screenshot", "wait_for"}),
    "browser_run_task": frozenset({"task", "url", "viewport_width", "viewport_height",
                                     "timeout_seconds", "screenshot", "session_id",
                                     "allow_mutations"}),
    "browser_status": frozenset({"probe"}),
    "browser_screenshot": frozenset({"url", "viewport_width", "viewport_height",
                                       "timeout_seconds", "full_page"}),
    # Takes nothing: it releases whatever this gateway has in flight.
    "browser_stop": frozenset(),
}

#: Phase B action -> the argument names it accepts through `turn(args=...)`.
#: Mirrors BROWSER_ARGS exactly: one table, checked before the call, so an
#: unknown key is an error here rather than a TypeError inside a handler.
AGENT_ARGS: dict[str, frozenset[str]] = {
    "agent_start": frozenset({"agent_id", "prompt", "title", "priority", "metadata",
                                "request_key", "skill_ids", "include_task_skills", "target"}),
    "list_agents": frozenset({"project_id", "state"}),
    "get_agent": frozenset({"agent_id"}),
    "create_agent": frozenset({"agent_id", "name", "project_id", "description", "runtime",
                                 "max_sessions", "repo", "workspace", "model", "metadata"}),
    "update_agent": frozenset({"agent_id", "name", "project_id", "description", "runtime",
                                 "max_sessions", "repo", "workspace", "model", "metadata",
                                 "state"}),
    "list_skills": frozenset({"latest_only"}),
    "register_skill": frozenset({"skill_id", "version", "body", "name", "summary", "metadata"}),
    "bind_agent_skill": frozenset({"agent_id", "skill_id", "kind", "version"}),
    "cleanup_candidates": frozenset({"limit"}),
    "project_plan": frozenset({"name", "description", "repo_root", "project_id",
                                 "complexity", "runtime", "max_agents"}),
    "project_bootstrap": frozenset({"name", "description", "repo_root", "project_id",
                                      "complexity", "runtime", "max_agents", "policy",
                                      "roles", "request_key"}),
    "project_list": frozenset({"status"}),
    "project_get": frozenset({"project_id"}),
    "project_update": frozenset({"project_id", "name", "description", "policy", "complexity"}),
    "project_archive": frozenset({"project_id"}),
    "project_phase_status": frozenset({"project_id"}),
    "project_advance": frozenset({"project_id", "to_phase", "reason", "gate_evidence",
                                    "handoff"}),
    "project_reconcile_team": frozenset({"project_id"}),
    "project_start": frozenset({"project_id", "prompt", "title", "capabilities", "approval",
                                  "agent_id", "priority", "metadata", "request_key", "target"}),
    # TMCP-HARNESS-001. `task_id` is the positional for every one of these:
    # a harness action is always ABOUT a task, and a run id is the optional
    # narrower way to name the same thing.
    "harness_start": frozenset({"task_id", "prompt", "title", "project_id", "acceptance",
                                  "checks", "changed_paths", "mode", "write_authority",
                                  "node_id", "actor", "steps"}),
    "harness_status": frozenset({"run_id", "task_id", "project_id", "limit"}),
    "harness_resume": frozenset({"run_id", "task_id", "actor", "steps"}),
    "harness_cancel": frozenset({"run_id", "task_id", "reason", "actor"}),
    "harness_review": frozenset({"run_id", "task_id", "project_id", "decision_id",
                                   "resolution", "approve_merge", "actor"}),
    # No required argument: the PM sweeps the whole fleet when no project is
    # named, which is what an operator asking "is anything stuck?" means.
    "project_recover": frozenset({"project_id", "limit", "dry_run"}),
}

#: Arguments without which the action cannot mean anything. Checked here so
#: the refusal names the missing field instead of surfacing as a TypeError.
AGENT_REQUIRED: dict[str, tuple[str, ...]] = {
    "agent_start": ("agent_id", "prompt"),
    "get_agent": ("agent_id",),
    "create_agent": ("agent_id",),
    "update_agent": ("agent_id",),
    "register_skill": ("skill_id",),
    "bind_agent_skill": ("agent_id", "skill_id"),
    "project_plan": ("name",),
    "project_bootstrap": ("name",),
    "project_get": ("project_id",),
    "project_update": ("project_id",),
    "project_archive": ("project_id",),
    "project_phase_status": ("project_id",),
    "project_advance": ("project_id",),
    "project_reconcile_team": ("project_id",),
    "project_start": ("project_id", "prompt"),
    # Only harness_start is required to name a task. status/resume/cancel/
    # review all accept EITHER a run_id or a task_id, which a fixed
    # positional list cannot express -- HarnessService refuses the empty
    # case itself, with a message that names both ways to fix it.
    "harness_start": ("task_id",),
}

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
                 handlers: dict[str, Callable[..., Any]] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.terminal = terminal
        self.controller = controller
        self.run_journal = run_journal
        # Late-bound implementations for the discovery/lifecycle/queue actions
        # `turn` routes but does not own (see TURN_HANDLER_ACTIONS). They are
        # the very same functions the individual MCP tools are registered
        # from, injected by mcp_app once those are defined -- so a single-tool
        # surface reuses one implementation instead of growing a second one
        # here, and the wrapper-layer side effects those tools already carry
        # (heartbeat refresh, supervisor-watch cleanup on delete) still run.
        self.handlers: dict[str, Callable[..., Any]] = dict(handlers or {})
        from .context_pack import RepoContextCache
        self._repo_context_cache = RepoContextCache()
        self.monotonic = monotonic
        self.sleep = sleep
        self._read_lock = threading.Lock()
        self._reads: dict[tuple, Future] = {}

    def _bounded_read(self, key: tuple, read: Callable, seconds: float) -> dict[str, Any]:
        """Bound read-only I/O without cancelling or duplicating an in-flight probe.

        A stalled node may ignore its transport timeout. Keep at most 32 daemon
        readers and one per operation/target; never run a mutation here. A result
        left over from an earlier timed-out call is discarded before a fresh
        probe, since a stale IDLE observation must not complete new work.
        """
        if seconds <= 0:
            return {"error": "STATUS_PROBE_TIMEOUT"}
        with self._read_lock:
            self._reads = {k: f for k, f in self._reads.items() if not f.done()}
            future = self._reads.get(key)
            if future is not None:
                # Do not reuse an observation begun before this request.
                return {"error": "STATUS_PROBE_TIMEOUT"}
            if future is None:
                if len(self._reads) >= 32:
                    return {"error": "STATUS_PROBE_TIMEOUT"}
                future = Future()
                self._reads[key] = future
                def run():
                    try:
                        future.set_result(read())
                    except Exception as exc:
                        future.set_exception(exc)
                threading.Thread(target=run, daemon=True, name="terminal-bounded-read").start()
        try:
            return future.result(timeout=seconds)
        except FutureTimeout:
            return {"error": "STATUS_PROBE_TIMEOUT"}

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
                      compact: bool = True,

                      mode: str | None = None) -> dict[str, Any]:
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
            }
            # TMCP-SESSION-HEALTH-001: pass the canonical status payload's
            # own `resource` block straight through, in BOTH compact and
            # full mode. It is the field a dispatcher needs precisely when
            # it is asking about many sessions at once, and it is small
            # (fixed keys, numbers and short enum strings -- no pane text),
            # so it does not compete with the tail budget. Absent whenever
            # the status payload had none, so no existing response changes.
            if isinstance(status.get("resource"), dict):
                row["resource"] = status["resource"]
            if not compact:
                row["reason"] = reason
                row["tail"] = rendered
                row["tail_truncated"] = bool(clipped or tail.get("truncated", False))
                row["exists"] = status.get("exists")
                row["cwd"] = status.get("cwd")
            else:
                # Compact inspect v2 is a decision surface. Keep the fields
                # that help route or act. Preserve established keys so older
                # clients can keep using the same row shape.
                row["reason"] = reason or ""
                row["tail"] = rendered
                row["tail_truncated"] = bool(clipped or tail.get("truncated", False))
                for key, value in (
                    ("node", status.get("node_id")),
                    ("cwd", status.get("cwd")),
                    ("last_activity_s", status.get("last_activity_s")),
                ):
                    if value is not None and value != "":
                        row[key] = value
                resource = status.get("resource")
                git = resource.get("git") if isinstance(resource, dict) else None
                if isinstance(git, dict):
                    if git.get("branch") not in (None, ""):
                        row["branch"] = git["branch"]
                    if git.get("dirty") is not None:
                        row["dirty"] = git["dirty"]
                if status.get("state") == "UNKNOWN" and reason:
                    row["reason"] = reason
            if "error" in tail:
                row["tail_error"] = tail["error"]
            rows.append(row)
            response_truncated |= bool(row.get("tail_truncated")) or reason_clipped
        return {
            "version": 2 if compact else 1,
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
        contradictory_confirm = (
            delivery == "SUBMIT_CONFIRMED"
            and result.get("press_enter") is True
            and result.get("enter_sent") is False
        )
        if contradictory_confirm:
            status = "FAILED"
        elif delivery == "SUBMIT_CONFIRMED":
            status = "SUBMIT_CONFIRMED"
        elif delivery == "BLOCKED" or result.get("error") in _BLOCKED_ERRORS:
            status = "BLOCKED"
        else:
            status = "FAILED"
        raw_reason = result.get("submit_reason") or result.get("reason") or result.get("error")
        if contradictory_confirm:
            raw_reason = (
                "receipt invariant violation: SUBMIT_CONFIRMED cannot be trusted because "
                "press_enter=True but enter_sent=False"
            )
        reason, clipped = _bounded(raw_reason, MAX_REASON_CHARS)
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
                "next_action": "Call terminal_turn action=resume with resume_token; do not resend the command",
                "command_cancelled": False,
            })
        return result

    def _wait_slice(self, wait: dict[str, Any], *, timeout: float,
                    poll_interval: float, deadline: float | None = None) -> dict[str, Any]:
        # Terminal results are immutable and returned from SQLite without
        # consulting or redispatching the underlying target.
        if wait["status"] in {"MATCHED", "FAILED"}:
            return self._durable_result(wait)

        slice_seconds = min(float(timeout), float(SYNC_WAIT_BUDGET_SECONDS))
        started = self.monotonic()
        deadline = min(started + slice_seconds, deadline) if deadline is not None else started + slice_seconds
        polls = 0
        final: dict[str, Any] = {}
        matched = False
        probe_timed_out = False
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                break
            polls += 1
            final = self._bounded_read(
                ("status", wait["target"]),
                lambda: self._status(wait["target"], timeout_seconds=max(0.001, remaining)),
                remaining)
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
            tail = self._bounded_read(
                ("tail", wait["target"], wait["tail_lines"]),
                lambda: self._tail(wait["target"], wait["tail_lines"]),
                deadline - self.monotonic())
            rendered, clipped = _bounded(
                redact_text(str(tail.get("output", ""))) if "error" not in tail else "",
                MAX_TAIL_CHARS_PER_TARGET,
            )
        result = self._durable_result(
            saved, waited_ms=waited_ms, polls=polls, tail=rendered,
            tail_truncated=bool(clipped or tail.get("truncated", False)),
        )
        result["tail_unavailable"] = bool(tail.get("error"))
        return result

    def wait_for_state(self, target: str, desired_states: list[str], timeout: float = DEFAULT_WAIT_SECONDS,
                       poll_interval: float = 1, tail_lines: int = 20,
                       _deadline: float | None = None) -> dict[str, Any]:
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
        return self._wait_slice(wait, timeout=timeout, poll_interval=poll_interval, deadline=_deadline)

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


    def turn(self, *, action: str, target: str | None = None,
             targets: list[str] | None = None, text: str | None = None,
             desired_states: list[str] | None = None, resume_token: str | None = None,
             timeout: float = DEFAULT_WAIT_SECONDS, poll_interval: float = 1,
             tail_lines: int = 20, compact: bool = True,
             path: str | None = None, content_b64: str | None = None,
             overwrite: bool = False, mode: str | None = None,
             idempotency_key: str | None = None,
             agent_type: str = "shell", working_directory: str | None = None,
             initial_prompt: str | None = None, grant_mode: str = "none",
             binding: str | None = None, node: str = "auto",
             title: str | None = None, priority: int = 0,
             metadata: dict[str, Any] | None = None, request_key: str | None = None,
             task_id: str | None = None,
             task_ids: list[str] | None = None,
             long_task: bool = False,
             url: str | None = None, args: dict | None = None) -> dict[str, Any]:
        """One MCP-call surface for one logical terminal turn.

        Pane actions, implemented here:
        - inspect: status+tail for one target or many targets (`targets`), each
          row carrying the `resource` context/quota health block.
        - send: guarded/idempotent task submission. Set ``long_task=True``
          to persist the prompt in the durable queue and return its receipt;
          dispatch and progress observation stay server-side.
        - send_wait: submit, then create one durable bounded wait in the same call.
        - wait: create one durable bounded wait.
        - resume: resume a previously PENDING wait.

        Discovery / lifecycle / durable-queue actions, routed to the same
        implementations the individual tools use (see self.handlers):
        - list_sessions (list, sessions), list_nodes (nodes)
        - create_session (create): `target` is the new name; agent_type,
          working_directory, initial_prompt, grant_mode, binding, node apply.
        - delete_session (delete, kill): `target` is the name.
        - enqueue_task (enqueue): `target` is the session, `text` the prompt;
          title, priority, metadata, request_key apply.
        - task_status (task): `task_id`. task_batch_status (tasks): `task_ids`.

        Parameters are reused across actions on purpose -- `target` is the
        session for every action that names one, and `text` is the prompt for
        both send and enqueue -- so a single-tool surface stays learnable
        instead of growing a parallel name per verb.

        This deliberately composes the existing methods and handlers rather
        than creating a second authorization, send, wait, lifecycle or
        idempotency implementation.
        """
        normalized = str(action or "").strip().lower().replace("-", "_")
        normalized = TURN_ACTION_ALIASES.get(normalized, normalized)
        if normalized not in TURN_ACTIONS:
            return {"status": "FAILED", "error": "INVALID_ACTION",
                    "allowed": list(TURN_ACTIONS),
                    "aliases": dict(TURN_ACTION_ALIASES)}

        if normalized in TURN_HANDLER_ACTIONS:
            return self._handler_turn(
                normalized, target=target, text=text, agent_type=agent_type,
                working_directory=working_directory, initial_prompt=initial_prompt,
                grant_mode=grant_mode, binding=binding, node=node, title=title,
                priority=priority, metadata=metadata, request_key=request_key,
                task_id=task_id, task_ids=task_ids, url=url, args=args,
                compact=compact, path=path, content_b64=content_b64,
                overwrite=overwrite, mode=mode)

        if normalized == "inspect":
            resolved_targets = list(targets or ([] if target is None else [target]))
            if not resolved_targets:
                return {"status": "FAILED", "error": "TARGET_REQUIRED"}
            result = self.batch_inspect(resolved_targets, tail_lines=tail_lines, compact=compact)
            return {"status": "OK" if "error" not in result else "FAILED",
                    "action": normalized, "result": result}

        if normalized == "resume":
            if not resume_token:
                return {"status": "FAILED", "error": "RESUME_TOKEN_REQUIRED"}
            result = self.resume_wait(resume_token, timeout=timeout,
                                      poll_interval=poll_interval)
            return {"status": result.get("status", "FAILED"),
                    "action": normalized, "result": result}

        if not target:
            return {"status": "FAILED", "error": "TARGET_REQUIRED"}

        if normalized == "start":
            if not text:
                return {"status": "FAILED", "error": "TEXT_REQUIRED"}
            return self._start_turn(target, text, title=title, priority=priority,
                                    metadata=metadata, request_key=request_key)

        if normalized == "send":
            if not text:
                return {"status": "FAILED", "error": "TEXT_REQUIRED"}
            if long_task:
                # Long work must cross the durable persist-before-dispatch
                # boundary in this same MCP call. Do not send first and then
                # make the client inspect/wait: the queue loop/watcher owns
                # dispatch and progress after the receipt is returned.
                queued = self._handler_turn(
                    "enqueue_task", target=target, text=text,
                    agent_type=agent_type, working_directory=working_directory,
                    initial_prompt=initial_prompt, grant_mode=grant_mode,
                    binding=binding, node=node, title=title, priority=priority,
                    metadata={**(metadata or {}), "long_task": True},
                    request_key=request_key or idempotency_key,
                    task_id=task_id, task_ids=task_ids)
                accepted = queued.get("result") if isinstance(queued, dict) else None
                if queued.get("status") != "OK" or not isinstance(accepted, dict):
                    return {"status": "FAILED", "action": normalized,
                            "mode": "durable_queue", "result": queued}
                # Persisting is only half of "one call". The comment above
                # says the queue loop owns dispatch from here -- but that loop
                # is OFF by default behind two gates (config.queue.enabled and
                # the per-lane auto_dispatch_enabled, see queue_loop.py), so on
                # an ordinary deployment the task would sit QUEUED and nothing
                # would ever start it. Drive the same bounded start sequence
                # `action="start"` uses, and hand it to the same server-side
                # follower, so the durable receipt this returns describes work
                # that is actually under way. Every key this path returned
                # before is unchanged; the start fields are added beside them.
                task_id_started = accepted.get("task_id")
                started: dict[str, Any] = {}
                if task_id_started:
                    ticks, task_state, blocked_reason = self._drive_start(target, task_id_started)
                    started = {
                        "task_id": task_id_started,
                        "session": target,
                        "task_state": task_state,
                        "dispatched": task_state in START_UNDERWAY_STATUSES,
                        "dispatch_ticks": ticks,
                        "server_side_progress": self._follow(target, task_id_started, task_state),
                        **self._start_next_step(task_state, blocked_reason),
                    }
                return {
                    # Unchanged: a long_task send still reports what the
                    # DURABLE QUEUE said, so an existing caller reading
                    # status/mode/receipt/result/client_polling sees exactly
                    # what it saw before.
                    "status": accepted.get("status", "TASK_ACCEPTED"),
                    "action": normalized,
                    "mode": "durable_queue",
                    "receipt": accepted,
                    "result": accepted,
                    "client_polling": False,
                    **started,
                }
            result = self.send_task(target, text, wait_for_accept=True,
                                    timeout=min(float(timeout), MAX_SEND_WAIT_SECONDS),
                                    idempotency_key=idempotency_key)
            return {"status": result.get("status", "FAILED"),
                    "action": normalized, "result": result}

        states = desired_states or ["IDLE", "WAITING_INPUT"]
        if normalized == "wait":
            result = self.wait_for_state(target, states, timeout=timeout,
                                         poll_interval=poll_interval,
                                         tail_lines=tail_lines)
            return {"status": result.get("status", "FAILED"),
                    "action": normalized, "result": result}

        # send_wait: send first, and only wait if the guarded submit was
        # positively confirmed. A blocked/failed send never creates a wait.
        if not text:
            return {"status": "FAILED", "error": "TEXT_REQUIRED"}
        if error := self._validate_wait(timeout, poll_interval):
            return error
        deadline = self.monotonic() + min(float(timeout), SYNC_WAIT_BUDGET_SECONDS)
        sent = self.send_task(target, text, wait_for_accept=True,
                              timeout=min(float(timeout), MAX_SEND_WAIT_SECONDS),
                              idempotency_key=idempotency_key)
        if sent.get("status") != "SUBMIT_CONFIRMED":
            return {"status": sent.get("status", "FAILED"),
                    "action": normalized, "send": sent, "wait": None}
        waited = self.wait_for_state(target, states, timeout=timeout,
                                     poll_interval=poll_interval,
                                     tail_lines=tail_lines, _deadline=deadline)
        return {"status": waited.get("status", "FAILED"),
                "action": normalized, "send": sent, "wait": waited}


    def _start_turn(self, target: str, text: str, *, title: str | None,
                    priority: int, metadata: dict[str, Any] | None,
                    request_key: str | None) -> dict[str, Any]:
        """`action="start"` -- the ONE call a normal request needs.

        Persist first, dispatch second, hand back a durable id, and leave
        the rest to the server. Concretely:

          1. enqueue_task, the existing durable, restart-safe, request_key-
             deduplicated path -- so the prompt is recorded BEFORE anything
             is sent and survives a crash between these steps;
          2. drive the lane through its start sequence here, bounded by
             MAX_START_TICKS, because QueueEngine.tick makes one transition
             per call and a client should never have to spend one MCP call
             per transition (that is this task's whole point);
          3. hand the task to the server-side follower so it keeps
             advancing after this call returns.

        The receipt says `poll: False` and `next_action: "none"` explicitly.
        A caller that has a task_id and a server that is still working has
        nothing to learn by asking again -- `task_status` is for when the
        USER asks to check, not for a loop.

        Every step reuses an injected handler, so authorization, allowed-cwd,
        protected-session and idempotency rules are the same ones the
        standalone tools enforce; nothing about them is re-decided here.
        """
        enqueue = self.handlers.get("enqueue_task")
        if enqueue is None:
            return {"status": "FAILED", "error": "ACTION_UNAVAILABLE", "action": "start",
                    "detail": "start is not wired on this server"}
        durable_metadata = {**(metadata or {}), "fast_agent_mode": True}
        repo_context = self._repo_context_for_target(target)
        if repo_context is not None:
            durable_metadata.setdefault("repo_context", repo_context)
        accepted = enqueue(target, text, title=title, priority=priority,
                           metadata=durable_metadata, request_key=request_key)
        if not isinstance(accepted, dict) or accepted.get("error") or not accepted.get("task_id"):
            return {"status": "FAILED", "action": "start", "result": accepted}
        task_id = accepted["task_id"]

        checkpoint_handler = self.handlers.get("task_checkpoint")
        if checkpoint_handler is not None:
            try:
                checkpoint_handler(task_id, {
                    "task_id": task_id, "goal": text, "findings": [], "files_read": [],
                    "changed_files": [], "tests": [], "failures": [],
                    "next_action": "inspect the repository and reproduce the requested behavior",
                    "repo": ({key: repo_context[key] for key in ("path", "branch", "commit")
                              if repo_context.get(key) is not None} if repo_context else {}),
                })
            except Exception:  # noqa: BLE001 -- task enqueue remains durable if advisory checkpoint fails
                pass

        ticks, task_state, blocked_reason = self._drive_start(target, task_id)
        follow = self._follow(target, task_id, task_state)
        dispatched = task_state in START_UNDERWAY_STATUSES
        return {
            "status": "TASK_STARTED" if dispatched else "TASK_ACCEPTED",
            "action": "start", "task_id": task_id, "session": target,
            "task_state": task_state, "dispatched": dispatched,
            "queue_position": accepted.get("queue_position"),
            "deduplicated": bool(accepted.get("deduplicated")),
            "request_key": accepted.get("request_key"),
            "dispatch_ticks": ticks,
            "server_side_progress": follow,
            "repo_context_cached": repo_context is not None,
            **self._start_next_step(task_state, blocked_reason),
        }

    def _repo_context_for_target(self, target: str) -> dict[str, Any] | None:
        """Inspect only a repository on this process's own node."""
        local_node = getattr(self.controller, "local_node_id", None)
        status_reader = getattr(self.controller, "terminal_status", None)
        if not local_node or status_reader is None:
            return None
        try:
            status = status_reader(target)
            if (not isinstance(status, dict) or status.get("error")
                    or status.get("node_id") != local_node or not status.get("cwd")):
                return None
            from pathlib import Path
            if not Path(status["cwd"]).is_dir():
                return None
            return self._repo_context_cache.get(status["cwd"])
        except Exception:  # noqa: BLE001 -- advisory context must not block durable enqueue
            return None

    @staticmethod
    def _start_next_step(task_state: str, blocked_reason: str | None) -> dict[str, Any]:
        """The anti-polling contract, stated in the receipt itself rather than
        only in the tool description -- and stated HONESTLY.

        `poll: False` is right in every case here (asking again changes
        nothing), but "do not watch this" must never be mistaken for "this is
        fine". A task the coordinator gate refused is stopped until a person
        decides something, and a receipt that reads "work is started and
        tracked server-side" over that outcome is how an orchestrator silently
        drops a task -- observed live on hp-linux before this was split out.
        """
        if task_state in START_NEEDS_HUMAN_STATUSES:
            return {
                "poll": False,
                "needs_human": True,
                "next_action": "resolve",
                "blocked_reason": blocked_reason,
                "guidance": (
                    f"this task is NOT running: it is {task_state}"
                    + (f" -- {blocked_reason}" if blocked_reason else "")
                    + ". Polling will not change that. Tell the user and resolve the blocker "
                      "(the coordinator gate, a paused lane, or a failed attempt); do not "
                      "re-send the prompt, the task is already durably queued under this task_id"),
            }
        if task_state in START_SERVER_PENDING_STATUSES:
            return {
                "poll": False,
                "needs_human": False,
                "next_action": "none",
                "guidance": ("the target was not reachable yet, so the server will retry this "
                             "task itself under this task_id -- do not poll and do not re-send"),
            }
        if task_state not in START_UNDERWAY_STATUSES:
            return {
                "poll": False, "needs_human": False, "next_action": "none",
                "guidance": ("task is durably queued under this task_id; execution has not yet "
                             "been confirmed. Progress is tracked server-side -- do not poll "
                             "or re-send; use action=task only if the user asks to check"),
            }
        return {
            "poll": False,
            "needs_human": False,
            "next_action": "none",
            "guidance": ("work is started and tracked server-side under this task_id -- do not "
                         "call wait/resume/inspect to watch it; use action=task with this "
                         "task_id only if the user explicitly asks to check"),
        }

    def _drive_start(self, target: str, task_id: str) -> tuple[int, str, str | None]:
        """Tick the lane until this task is under way or has stopped.

        Bounded by MAX_START_TICKS -- a bound on how long the CALLER waits,
        never on the task, which the follower carries from here. Stops at the
        FIRST settled state rather than spending the rest of the budget: a
        lane the coordinator just paused will answer PAUSED to every further
        tick, and five more of those cost the caller latency to learn nothing
        (observed live: dispatch_ticks=6 against an already-paused lane).
        """
        tick = self.handlers.get("dispatch_tick")
        state, reason = self._task_snapshot(task_id)
        if tick is None:
            return 0, state, reason
        ticks = 0
        deadline = time.monotonic() + START_WAIT_BUDGET_SECONDS
        for _ in range(MAX_START_TICKS):
            if state in START_SETTLED_STATUSES:
                break
            if time.monotonic() >= deadline:
                # Out of the caller's budget. The task keeps advancing --
                # the follower owns it from here -- so this returns
                # TASK_ACCEPTED rather than TASK_STARTED and says nothing
                # untrue about where the work got to.
                break
            try:
                tick(target)
            except Exception:  # noqa: BLE001 -- the task is durable; report its real state
                break
            ticks += 1
            state, reason = self._task_snapshot(task_id)
        return ticks, state, reason

    def _task_snapshot(self, task_id: str) -> tuple[str, str | None]:
        """This task's durable status and, when it is stopped, WHY.

        The reason comes from the record the gate itself wrote (the
        coordinator decision, or the task's last error) -- never invented
        here, and never guessed from the status alone."""
        status = self.handlers.get("task_status")
        if status is None:
            return "UNKNOWN", None
        try:
            result = status(task_id)
        except Exception:  # noqa: BLE001
            return "UNKNOWN", None
        if not isinstance(result, dict):
            return "UNKNOWN", None
        task = result.get("task")
        if not isinstance(task, dict):
            return (str(result["status"]) if result.get("status") else "UNKNOWN"), None
        state = str(task.get("status") or "UNKNOWN")
        reason = None
        decision = task.get("coordinator_decision")
        if isinstance(decision, dict) and decision.get("reason"):
            reason = str(decision["reason"])
            # QueueStore intentionally maps NEEDS_REWORK back to QUEUED so the
            # background loop can re-review after the blocker is fixed.  A
            # one-call start must not synchronously repeat that unchanged
            # deterministic refusal until the sidecar times out.
            if state == "QUEUED" and str(decision.get("status") or "") == "NEEDS_REWORK":
                state = "NEEDS_REWORK"
        elif task.get("last_error"):
            reason = str(task["last_error"])
        return state, reason

    def _follow(self, target: str, task_id: str, task_state: str) -> dict[str, Any]:
        """Hand the started task to the server-side follower.

        Skipped when the task already settled inside this call -- there is
        nothing left to follow, and starting a thread to discover that would
        be the same wasted work in a different place."""
        if task_state in START_NEEDS_HUMAN_STATUSES or task_state in {"COMPLETED", "SKIPPED"}:
            # Nothing left to follow: either done, or stopped on something only
            # a person can move. Starting a thread to rediscover that would be
            # the same wasted work in a different place.
            return {"following": False, "reason": "ALREADY_SETTLED"}
        follow = self.handlers.get("follow_task")
        if follow is None:
            return {"following": False, "reason": "FOLLOWER_UNAVAILABLE"}
        try:
            result = follow(target, task_id)
        except Exception:  # noqa: BLE001 -- never fail a started task over its follower
            return {"following": False, "reason": "FOLLOWER_ERROR"}
        return result if isinstance(result, dict) else {"following": bool(result)}

    def _handler_turn(self, action: str, *, target: str | None, text: str | None,
                      agent_type: str, working_directory: str | None,
                      initial_prompt: str | None, grant_mode: str,
                      binding: str | None, node: str, title: str | None,
                      priority: int, metadata: dict[str, Any] | None,
                      request_key: str | None, task_id: str | None,
                      task_ids: list[str] | None, url: str | None = None,
                      args: dict | None = None,
                      # Keyword-only with a default so the OTHER call site (the
                      # long_task enqueue path) needs no change. Only the list
                      # actions read it. Passing it is what makes terminal_turn's
                      # documented `compact` flag real for list_nodes/
                      # list_sessions -- before this it was silently dropped.
                      compact: bool = True,
                      path: str | None = None, content_b64: str | None = None,
                      overwrite: bool = False,
                      mode: str | None = None) -> dict[str, Any]:
        """Route one non-pane action to its injected implementation.

        Argument shaping only. Every authorization, allowed-cwd, protected-
        session, idempotency and durability rule stays in the handler -- which
        is the same function the standalone tool calls -- so routing a verb
        through `turn` can never be a weaker path than calling it directly.
        """
        handler = self.handlers.get(TURN_HANDLER_ACTIONS[action])
        if handler is None:
            # An honest refusal, not a silent no-op: this deployment did not
            # wire the action (a bare CompactTerminalTools in a test, or a
            # build without the queue).
            return {"status": "FAILED", "error": "ACTION_UNAVAILABLE", "action": action,
                    "detail": f"{action} is not wired on this server"}

        if action in {"create_session", "delete_session", "enqueue_task"} and (
                not isinstance(target, str) or not target.strip()):
            return {"status": "FAILED", "error": "TARGET_REQUIRED", "action": action}
        if action in {"enqueue_task", "route_start"} and (
                not isinstance(text, str) or not text.strip()):
            return {"status": "FAILED", "error": "TEXT_REQUIRED", "action": action}
        if action == "task_checkpoint":
            if not isinstance(task_id, str) or not task_id.strip():
                return {"status": "FAILED", "error": "TASK_ID_REQUIRED", "action": action}
            if not isinstance(args, dict) or set(args) != {"checkpoint"}:
                return {"status": "FAILED", "error": "INVALID_ARGS", "action": action,
                        "allowed": ["checkpoint"]}
            try:
                result = handler(task_id.strip(), args["checkpoint"])
            except (KeyError, ValueError) as exc:
                return {"status": "FAILED", "error": "INVALID_CHECKPOINT",
                        "action": action, "detail": str(exc)[:300]}
            return {"status": "OK", "action": action, "result": result}
        if action in AGENT_ARGS:
            # Shaped from `args` plus the two conventional positionals this
            # surface already uses everywhere else: `target` is the agent or
            # skill id, `text` is the prompt. Unknown keys are refused by name
            # rather than dropped -- a typo that appears to succeed is how a
            # caller comes to believe it set something it did not.
            allowed = AGENT_ARGS[action]
            if args is not None and not isinstance(args, dict):
                return {"status": "FAILED", "error": "INVALID_ARGS", "action": action,
                        "allowed": sorted(allowed)}
            extra = dict(args or {})
            if unknown := sorted(key for key in extra if key not in allowed):
                return {"status": "FAILED", "error": "UNKNOWN_ARGS", "action": action,
                        "unknown": unknown, "allowed": sorted(allowed)}
            # `target` fills the action's FIRST required argument and `text`
            # its second. One rule instead of a chain of special cases: the
            # required list already says what each action's positionals mean,
            # so target/text cannot drift from it.
            required = AGENT_REQUIRED.get(action, ())
            if target and required and required[0] in allowed:
                extra.setdefault(required[0], target)
            elif target and not required and "project_id" in allowed:
                # An action with no REQUIRED argument still has a conventional
                # positional. `project_recover` sweeps the whole fleet when
                # nothing is named and one project when something is, so a
                # bare `target` must not be silently dropped -- that would
                # turn "recover this project" into "recover everything".
                extra.setdefault("project_id", target)
            if text and len(required) > 1 and required[1] in allowed:
                extra.setdefault(required[1], text)
            elif text and "prompt" in allowed:
                extra.setdefault("prompt", text)
            elif text and "description" in allowed:
                # A project's positional text is its description.
                extra.setdefault("description", text)
            if title and "title" in allowed:
                extra.setdefault("title", title)
            if metadata and "metadata" in allowed:
                extra.setdefault("metadata", metadata)
            if request_key and "request_key" in allowed:
                extra.setdefault("request_key", request_key)
            # `task_id` is a turn-level parameter already (task_status uses
            # it), so an action that takes one reads it from there rather
            # than making the caller repeat it inside `args`. Same rule as
            # title/metadata/request_key above: forwarded only where the
            # action actually declares it.
            if task_id and "task_id" in allowed:
                extra.setdefault("task_id", task_id)
            missing = sorted(key for key in AGENT_REQUIRED.get(action, ()) if not extra.get(key))
            if missing:
                return {"status": "FAILED", "error": "MISSING_ARGS", "action": action,
                        "missing": missing}
            result = handler(**extra)
            status = "FAILED" if isinstance(result, dict) and "error" in result else "OK"
            return {"status": status, "action": action, "result": result,
                    **{key: result[key] for key in
                       ("task_id", "session", "node_id", "routing_state", "routing_outcome",
                        "routing_reason", "score", "task_state", "dispatched", "poll",
                        "agent_id", "skills", "project_id", "phase", "agent_role",
                        "pm_agent_id", "agent_selection_reason",
                        # TMCP-HARNESS-001: the few scalars a caller acts on
                        # without reading the whole report. `created` is the
                        # exactly-once answer and `resumed_to` is the proof a
                        # resume continued rather than restarted.
                        "run_id", "stage", "created", "resumed_from", "resumed_to")
                       if isinstance(result, dict) and key in result}}

        if action == "route_start":
            # No target: that is the point. `target`, when a caller does pass
            # one, is forwarded and honoured as hard affinity by the router --
            # never re-decided here.
            result = handler(text, title=title, priority=priority, metadata=metadata,
                             request_key=request_key, target=target)
            status = result.get("status", "OK") if isinstance(result, dict) else "OK"
            return {"status": status, "action": action, "result": result,
                    **{key: result[key] for key in
                       ("task_id", "session", "node_id", "routing_state", "routing_outcome",
                        "routing_reason", "score", "task_state", "dispatched", "poll")
                       if isinstance(result, dict) and key in result}}
        if action == "task_status" and (not isinstance(task_id, str) or not task_id.strip()):
            return {"status": "FAILED", "error": "TASK_ID_REQUIRED", "action": action}
        if action == "task_batch_status" and not isinstance(task_ids, list):
            return {"status": "FAILED", "error": "TASK_IDS_REQUIRED", "action": action}
        if action.startswith("browser_"):
            allowed = BROWSER_ARGS[action]
            if args is not None and not isinstance(args, dict):
                return {"status": "FAILED", "error": "INVALID_ARGS", "action": action,
                        "allowed": sorted(allowed)}
            extra = dict(args or {})
            if unknown := sorted(key for key in extra if key not in allowed):
                return {"status": "FAILED", "error": "UNKNOWN_ARGS", "action": action,
                        "unknown": unknown, "allowed": sorted(allowed)}
            if action in {"browser_verify", "browser_screenshot"}:
                extra.setdefault("url", url if url is not None else target)
            elif action == "browser_run_task":
                extra.setdefault("task", text)
                if url is not None or target is not None:
                    extra.setdefault("url", url if url is not None else target)
            result = handler(**extra)
            status = result.get("status", "OK") if isinstance(result, dict) else "OK"
            if isinstance(result, dict) and "error" in result:
                status = "FAILED"
            return {"status": status, "action": action, "result": result}

        if action == "delete_session" and args is not None:
            if not isinstance(args, dict) or set(args) - {"confirm"} or type(args.get("confirm", False)) is not bool:
                return {"status": "FAILED", "error": "INVALID_ARGUMENT", "action": action,
                        "detail": "delete args must contain only boolean confirm"}

        calls: dict[str, Callable[[], Any]] = {
            # Not routed through `args`: the payload is large and the path is
            # security-relevant, so both are first-class arguments that the
            # schema documents rather than free-form keys in a dict.
            # node defaults to "auto" on terminal_turn (it means auto-PLACEMENT
            # for create_session), but a file has no sensible auto placement --
            # it is written so something on a specific machine can read it. Map
            # it to None so the controller resolves the LOCAL node instead of
            # looking up a node literally named "auto" and returning
            # NODE_NOT_FOUND.
            "put_file": lambda: handler(path or "", content_b64 or "",
                                        overwrite=overwrite, mode=mode,
                                        node=(None if node in (None, "auto") else node)),
            "list_sessions": lambda: _project_sessions(handler(), compact),
            "list_nodes": lambda: _project_nodes(handler(), compact),
            "create_session": lambda: handler(
                target.strip(), agent_type=agent_type, working_directory=working_directory,
                initial_prompt=initial_prompt, grant_mode=grant_mode, binding=binding,
                node=node),
            "delete_session": lambda: (handler(target.strip(), confirm=args["confirm"])
                                        if args and "confirm" in args else handler(target.strip())),
            "enqueue_task": lambda: handler(
                target.strip(), text, title=title, priority=priority,
                metadata=metadata, request_key=request_key),
            "task_status": lambda: handler(task_id.strip()),
            "task_batch_status": lambda: handler(task_ids),
        }
        result = calls[action]()
        failed = isinstance(result, dict) and ("error" in result or result.get("status") == "FAILED")
        return {"status": "FAILED" if failed else "OK", "action": action, "result": result}
