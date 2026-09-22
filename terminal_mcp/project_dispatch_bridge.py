"""Safe bridge from Terminal MCP queue events to a project's own planner.

The queue remains the only dispatcher. This bridge never writes terminal input and never
claims project tasks itself. On a configured TASK_COMPLETED event it executes one fixed,
repo-local Python planner, parses its JSON envelope, and appends a bounded continuation
prompt to the SAME opted-in queue lane using QueueService.enqueue().

No shell is involved. The configured planner must resolve inside the configured repo root,
the session must match an explicit fnmatch pattern, and the queue's normal per-lane
auto-dispatch/permission/idempotency gates still decide whether the prompt reaches tmux.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SUPPORTED_ACTIONS = frozenset({"RESUME", "CLAIM", "RECONCILE", "BLOCKED_REVIEW"})
DEFAULT_EVENT_TYPES = frozenset({"TASK_COMPLETED"})


@dataclass(frozen=True)
class ProjectDispatchRule:
    name: str
    session_patterns: tuple[str, ...]
    repo_root: str | None = None
    planner_path: str = "tools/orchestration/continuous_dispatch.py"
    event_types: tuple[str, ...] = ("TASK_COMPLETED",)
    timeout_seconds: float = 10.0


class ProjectDispatchBridge:
    def __init__(self, queue: Any, rules: tuple[ProjectDispatchRule, ...]) -> None:
        self.queue = queue
        self.rules = rules

    def handle(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type") or "")
        session = self._session_of(event)
        if not session:
            return {"action": "NO_SESSION"}
        rule = self._match_rule(session, event_type)
        if rule is None:
            return {"action": "NO_RULE", "session": session, "event_type": event_type}

        repo_root = self._repo_root_for_event(rule, event)
        try:
            envelope = self._run_planner(rule, event, repo_root)
        except Exception as exc:  # noqa: BLE001 - caller decides retry/dead-letter policy
            _LOGGER.exception("project-dispatch: planner failed for %s", rule.name)
            raise RuntimeError(f"project planner failed: {type(exc).__name__}: {exc}") from exc

        action = str(envelope.get("action") or "")
        if action not in SUPPORTED_ACTIONS:
            return {
                "action": "PLANNER_NO_DISPATCH",
                "session": session,
                "project": rule.name,
                "planner_action": action or None,
                "task_id": envelope.get("taskId"),
            }

        task_id = envelope.get("taskId")
        event_id = str(event.get("id") or event.get("event_id") or "")
        request_key = f"project-dispatch:{rule.name}:{event_id}:{action}:{task_id or '-'}"
        prompt = self._continuation_prompt(rule, envelope)
        accepted = self.queue.enqueue(
            session,
            prompt,
            title=f"{rule.name} continuous dispatch: {action} {task_id or ''}".strip(),
            priority=100,
            metadata={
                "source": "project_dispatch_bridge",
                "project": rule.name,
                "event_id": event_id,
                "planner_action": action,
                "planner_task_id": task_id,
            },
            request_key=request_key,
        )
        if accepted.get("error"):
            raise RuntimeError(f"queue enqueue failed: {accepted['error']}")
        return {
            "action": "CONTINUATION_ENQUEUED",
            "session": session,
            "project": rule.name,
            "planner_action": action,
            "planner_task_id": task_id,
            "queue_task_id": accepted.get("task_id"),
            "deduplicated": accepted.get("deduplicated", False),
            "request_key": request_key,
        }

    def _match_rule(self, session: str, event_type: str) -> ProjectDispatchRule | None:
        for rule in self.rules:
            if event_type not in set(rule.event_types):
                continue
            if any(fnmatch.fnmatchcase(session, pattern) for pattern in rule.session_patterns):
                return rule
        return None

    @staticmethod
    def _session_of(event: dict[str, Any]) -> str | None:
        payload = event.get("payload")
        if isinstance(payload, dict) and payload.get("session"):
            return str(payload["session"])
        if event.get("session"):
            return str(event["session"])
        return None

    def _repo_root_for_event(self, rule: ProjectDispatchRule, event: dict[str, Any]) -> str:
        entity_id = event.get("entity_id")
        if entity_id:
            try:
                task = self.queue.store.get_task(str(entity_id))
            except Exception:  # noqa: BLE001 -- fall back to explicit config
                task = None
            metadata = getattr(task, "metadata", None)
            if isinstance(metadata, dict):
                for key in ("repo_root", "worktree", "cwd"):
                    value = metadata.get(key)
                    if isinstance(value, str) and value.strip():
                        return value
        if rule.repo_root:
            return rule.repo_root
        raise ValueError("no repo root in completed task metadata or project dispatch rule")

    @staticmethod
    def _safe_planner(rule: ProjectDispatchRule, repo_root: str) -> tuple[Path, Path]:
        root = Path(repo_root).expanduser().resolve()
        planner = (root / rule.planner_path).resolve()
        try:
            planner.relative_to(root)
        except ValueError as exc:
            raise ValueError("planner_path escapes repo_root") from exc
        if not root.is_dir():
            raise FileNotFoundError(f"repo_root does not exist: {root}")
        if not planner.is_file():
            raise FileNotFoundError(f"planner does not exist: {planner}")
        return root, planner

    def _run_planner(self, rule: ProjectDispatchRule, event: dict[str, Any],
                     repo_root: str) -> dict[str, Any]:
        root, planner = self._safe_planner(rule, repo_root)
        entity_id = event.get("entity_id")
        event_type = str(event.get("type") or "")
        project_event = "TASK_DONE" if event_type == "TASK_COMPLETED" else "TASK_BLOCKED"
        argv = [sys.executable, str(planner), "event", project_event]
        if entity_id:
            argv.extend(["--task-id", str(entity_id)])
        completed = subprocess.run(
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=rule.timeout_seconds,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home())},
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[-1000:]
            raise RuntimeError(f"planner exit {completed.returncode}: {stderr}")
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("planner did not return one JSON object") from exc
        if not isinstance(envelope, dict):
            raise RuntimeError("planner result must be one JSON object")
        return envelope

    @staticmethod
    def _continuation_prompt(rule: ProjectDispatchRule, envelope: dict[str, Any]) -> str:
        payload = json.dumps(
            {
                "action": envelope.get("action"),
                "taskId": envelope.get("taskId"),
                "reason": envelope.get("reason"),
                "dependencies": envelope.get("dependencies"),
                "requiredChecks": envelope.get("requiredChecks"),
                "classificationRequired": envelope.get("classificationRequired"),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            "CONTINUOUS_DISPATCH_EVENT\n"
            f"Project: {rule.name}\n"
            f"Planner envelope: {payload}\n\n"
            "Read the repository AGENTS.md continuous-dispatch contract and the project runbook. "
            "Execute exactly the planner action against canonical project state, then continue the "
            "next production-critical task in this same work lane. Persist TASKS/checkpoint/evidence "
            "before relying on session memory. Do not bypass dependencies, approvals, credentials, "
            "destructive actions, production writes, or release/security gates. If the envelope is "
            "stale, recompute canonical state once and record why; never duplicate a claim."
        )
