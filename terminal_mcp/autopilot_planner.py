"""ProjectFlow Autopilot P1 -- a bounded, advisory AI Project Coordinator (#106).

THE MODEL PROPOSES; DETERMINISTIC CODE DISPOSES. A provider returns strict,
versioned JSON (`projectflow.plan/v1`). `validate_plan` rejects everything the
runtime cannot safely accept; `apply_plan` can only APPEND validated tasks
through `QueueStore.append_tasks`, idempotently (request_key per plan+key). A
plan cannot set a status, a lease, a completion, a lane outside the project's
allowlist, or anything in another project. Applying is an explicit operator
call -- `ProjectPlanner.propose` never writes.

CONTEXT IS BOUNDED AND SCRUBBED. Only this project's outcome titles/AC and
task id/title/status/deps go out -- never prompt bodies -- every string passes
`redaction.redact_text`, and the whole payload is hard-capped in characters.

NOTHING STOPS ON THE MODEL. A missing, unauthorized (`BLOCKED_PROVIDER`),
failing or over-budget provider yields the deterministic plan (no new tasks):
the autopilot keeps dispatching existing ready work by dependency and priority.
An unchanged context is never re-sent, and calls are capped per hour.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .autopilot import screen_prompt
from .autopilot_policy import PlannerProviderConfig
from .redaction import redact_text

PLAN_SCHEMA = "projectflow.plan/v1"
CONTEXT_SCHEMA = "projectflow.plan_context/v1"

PROPOSED = "PROPOSED"
APPLIED = "APPLIED"
REJECTED = "REJECTED"
FALLBACK_DETERMINISTIC = "FALLBACK_DETERMINISTIC"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
BUSY = "BUSY"
BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
PROVIDER_ERROR = "PROVIDER_ERROR"

_TOP_FIELDS = {"schema", "project_id", "tasks", "summary"}
_TASK_FIELDS = {"key", "title", "prompt", "depends_on", "priority", "risk", "capabilities",
                "rationale", "lane"}
_RISKS = ("low", "medium", "high")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_LIMITS = {"title": 200, "prompt": 4000, "rationale": 500}
DEFAULT_CONTEXT_CHARS = 12_000

PLANNER_INSTRUCTIONS = (
    "Propose at most the allowed number of next tasks for THIS project only, as JSON matching "
    f"{PLAN_SCHEMA}. Each task needs key, title, prompt, depends_on (keys or existing task ids "
    "from the context), priority 0-100, risk low|medium|high, capabilities, and a one-line "
    "rationale explaining its priority. Never include credentials, deploy/merge/force-push/"
    "destructive steps, or work for another project. Your output is advisory and validated."
)


class ProviderBlocked(RuntimeError):
    """The provider is not authorized/configured. Not an error to retry."""


# -- context ----------------------------------------------------------------

def _scrub(value: Any, limit: int = 200) -> str:
    return redact_text(str(value or ""))[:limit]


def build_context(store: Any, outcomes: Any, project_id: str, *,
                  max_chars: int = DEFAULT_CONTEXT_CHARS) -> dict[str, Any]:
    outcome_rows = [{
        "id": o.id, "title": _scrub(o.title), "status": o.status,
        "acceptance_criteria": [_scrub(c) for c in o.acceptance_criteria][:20],
    } for o in outcomes.list_outcomes(project_id=project_id)][:20]
    task_rows = [{
        "id": t.id, "title": _scrub(t.title), "status": t.status,
        "depends_on": list(t.depends_on), "priority": t.priority, "outcome_id": t.outcome_id,
    } for t in store.list_tasks_for_project(project_id, limit=500)]
    context = {"schema": CONTEXT_SCHEMA, "project_id": project_id, "outcomes": outcome_rows,
               "tasks": task_rows, "truncated": False}
    while len(json.dumps(context)) > max_chars and (context["tasks"] or context["outcomes"]):
        context["truncated"] = True
        if context["tasks"]:
            context["tasks"].pop()
        else:
            context["outcomes"].pop()
    return context


def context_hash(context: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:16]


# -- validation ----------------------------------------------------------------

@dataclass
class Validation:
    ok: bool
    errors: list[dict[str, str]] = field(default_factory=list)


def validate_plan(raw: Any, *, store: Any, project_id: str, lanes: Sequence[str], max_tasks: int,
                  approve_high_risk: bool = False) -> Validation:
    errors: list[dict[str, str]] = []

    def err(code: str, detail: str = "") -> None:
        errors.append({"code": code, "detail": detail[:200]})

    if not isinstance(raw, dict):
        return Validation(False, [{"code": "NOT_AN_OBJECT", "detail": type(raw).__name__}])
    for name in sorted(set(raw) - _TOP_FIELDS):
        err("UNKNOWN_FIELD", name)
    if raw.get("schema") != PLAN_SCHEMA:
        err("SCHEMA_VERSION", str(raw.get("schema")))
    if raw.get("project_id") != project_id:
        err("PROJECT_MISMATCH", str(raw.get("project_id")))
    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        err("TASKS_NOT_A_LIST")
        return Validation(False, errors)
    if len(tasks) > max_tasks:
        err("TOO_MANY_TASKS", f"{len(tasks)} > {max_tasks}")

    keys: list[str] = []
    graph: dict[str, list[str]] = {}
    for index, item in enumerate(tasks):
        where = f"tasks[{index}]"
        if not isinstance(item, dict):
            err("TASK_NOT_AN_OBJECT", where)
            continue
        for name in sorted(set(item) - _TASK_FIELDS):
            err("UNKNOWN_FIELD", f"{where}.{name}")
        key = item.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            err("INVALID_KEY", where)
            continue
        if key in keys:
            err("DUPLICATE_KEY", key)
        keys.append(key)
        for name, limit in _LIMITS.items():
            value = item.get(name)
            if not isinstance(value, str) or not value.strip():
                err("RATIONALE_REQUIRED" if name == "rationale" else "FIELD_REQUIRED", f"{key}.{name}")
                continue
            if len(value) > limit:
                err("FIELD_TOO_LONG", f"{key}.{name}")
            if redact_text(value) != value:
                err("SECRET_LIKE_CONTENT", f"{key}.{name}")
        hit = screen_prompt(f"{item.get('title') or ''}\n{item.get('prompt') or ''}")
        if hit is not None:
            err("SENSITIVE_PROMPT", f"{key}: {hit[1]}")
        priority = item.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
            err("PRIORITY_RANGE", key)
        risk = item.get("risk")
        if risk not in _RISKS:
            err("INVALID_RISK", key)
        elif risk == "high" and not approve_high_risk:
            err("HIGH_RISK_NEEDS_APPROVAL", key)
        caps = item.get("capabilities", [])
        if not isinstance(caps, list) or len(caps) > 8 or not all(isinstance(c, str) for c in caps):
            err("INVALID_CAPABILITIES", key)
        lane = item.get("lane")
        if lane is not None and lane not in lanes:
            err("LANE_NOT_ALLOWED", f"{key}: {lane}")
        deps = item.get("depends_on", [])
        if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
            err("INVALID_DEPENDS_ON", key)
            deps = []
        graph[key] = deps

    for key, deps in graph.items():
        for dep in deps:
            if dep in graph:
                continue
            existing = store.get_task(dep)
            if existing is None:
                err("UNKNOWN_DEPENDENCY", f"{key} -> {dep}")
            elif existing.project_id != project_id:
                err("CROSS_PROJECT_DEPENDENCY", f"{key} -> {dep}")
    cycle = _cycle(graph)
    if cycle:
        err("DEPENDENCY_CYCLE", " -> ".join(cycle))
    return Validation(not errors, errors)


def _cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> list[str] | None:
        state[node] = 1
        for dep in graph.get(node, ()):
            if dep not in graph:
                continue
            if state.get(dep) == 1:
                return [*path, dep]
            if state.get(dep) is None:
                found = visit(dep, [*path, dep])
                if found:
                    return found
        state[node] = 2
        return None

    for node in graph:
        if state.get(node) is None:
            found = visit(node, [node])
            if found:
                return found
    return None


def _topological(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {t["key"]: t for t in tasks}
    ordered: list[dict[str, Any]] = []
    done: set[str] = set()

    def place(task: dict[str, Any]) -> None:
        if task["key"] in done:
            return
        for dep in task.get("depends_on", []):
            if dep in by_key:
                place(by_key[dep])
        done.add(task["key"])
        ordered.append(task)

    for task in tasks:
        place(task)
    return ordered


def plan_id(raw: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(raw.get("tasks"), sort_keys=True).encode()).hexdigest()[:12]


def apply_plan(raw: Any, *, store: Any, project_id: str, lanes: Sequence[str], max_tasks: int,
               approve_high_risk: bool = False, provider: str = "operator") -> dict[str, Any]:
    """Validate, then append each task once. Re-applying the same plan returns
    the same task ids and creates nothing (request_key per plan+key)."""
    validation = validate_plan(raw, store=store, project_id=project_id, lanes=lanes,
                               max_tasks=max_tasks, approve_high_risk=approve_high_risk)
    if not validation.ok:
        return {"status": REJECTED, "errors": validation.errors, "created": []}
    pid = plan_id(raw)
    ids: dict[str, str] = {}
    for item in _topological(raw["tasks"]):
        request_key = f"plan:{project_id}:{pid}:{item['key']}"
        existing = store.task_by_request_key(request_key)
        if existing is not None:
            ids[item["key"]] = existing["id"]
            continue
        depends_on = [ids.get(dep, dep) for dep in item.get("depends_on", [])]
        new_id = f"pln_{hashlib.sha256(request_key.encode()).hexdigest()[:16]}"
        store.validate_dependencies(new_id, depends_on)
        [created] = store.append_tasks(item.get("lane") or lanes[0], [{
            "id": new_id, "title": item["title"], "prompt": item["prompt"],
            "project_id": project_id, "depends_on": depends_on,
            "priority": int(item.get("priority", 0)), "request_key": request_key,
            "metadata": {"plan": {"plan_id": pid, "key": item["key"], "provider": provider,
                                  "risk": item["risk"], "rationale": item["rationale"],
                                  "capabilities": list(item.get("capabilities", []))}}}])
        ids[item["key"]] = created
    return {"status": APPLIED, "plan_id": pid, "errors": [],
            "created": [ids[t["key"]] for t in raw["tasks"]]}


# -- providers ---------------------------------------------------------------------

class DeterministicPlanner:
    """Always available, never leaves the process: proposes no new work. The
    autopilot's own dependency/priority order is the deterministic plan."""
    name = "deterministic"

    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        return {"schema": PLAN_SCHEMA, "project_id": context["project_id"], "tasks": [],
                "summary": "deterministic: no new tasks; ready work continues by dependency "
                           "then priority order"}


def _urllib_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> bytes:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 -- https/loopback enforced by config
        return response.read(1_000_000)


class GatewayPlanner:
    """AI Gateway provider, server-side only. The token is read from a 0600
    file at call time and placed only in the Authorization header; it is never
    stored on the instance, logged, or returned. Unconfigured/unreadable ->
    ProviderBlocked (reported as BLOCKED_PROVIDER, never retried in a loop)."""
    name = "ai-gateway"

    def __init__(self, config: PlannerProviderConfig, *,
                 http_post: Callable[[str, bytes, dict[str, str], float], bytes] | None = None) -> None:
        self.config = config
        self._post = http_post or _urllib_post

    def _token(self) -> str:
        if self.config.kind != "gateway" or not self.config.token_file:
            raise ProviderBlocked("gateway planner is not configured")
        path = Path(self.config.token_file)
        try:
            info = path.stat()
        except OSError:
            raise ProviderBlocked("token_file is not readable") from None
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ProviderBlocked("token_file must not be group/world readable (chmod 600)")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ProviderBlocked("token_file must be owned by the service user")
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ProviderBlocked("token_file is empty")
        return token

    def propose(self, context: dict[str, Any]) -> dict[str, Any]:
        token = self._token()
        body = json.dumps({"response_schema": PLAN_SCHEMA, "model": self.config.model,
                           "max_tasks": self.config.max_tasks,
                           "instructions": PLANNER_INSTRUCTIONS, "context": context}).encode()
        raw = self._post(str(self.config.endpoint), body,
                         {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         float(self.config.timeout_seconds))
        parsed = json.loads(raw)
        return parsed.get("plan", parsed) if isinstance(parsed, dict) else parsed


# -- coordinator -------------------------------------------------------------------

class ProjectPlanner:
    def __init__(self, *, store: Any, outcomes: Any, provider: Any,
                 config: PlannerProviderConfig, lanes: Sequence[str],
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.outcomes = outcomes
        self.provider = provider
        self.config = config
        self.lanes = tuple(lanes)
        self.clock = clock
        self._lock = threading.Lock()
        self._calls: dict[str, list[float]] = {}
        self._cache: dict[str, tuple[str, dict[str, Any]]] = {}
        self._fallback = DeterministicPlanner()

    def _deterministic(self, context: dict[str, Any], status: str, **extra: Any) -> dict[str, Any]:
        return {"status": status, "provider": self._fallback.name,
                "plan": self._fallback.propose(context), **extra}

    def propose(self, project_id: str, *, approve_high_risk: bool = False) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"status": BUSY, "detail": "a planner call is already in flight"}
        try:
            context = build_context(self.store, self.outcomes, project_id)
            digest = context_hash(context)
            cached = self._cache.get(project_id)
            if cached and cached[0] == digest:
                return {**cached[1], "cached": True}
            now = self.clock()
            recent = [t for t in self._calls.get(project_id, []) if now - t < 3600]
            if len(recent) >= self.config.max_calls_per_hour:
                return self._deterministic(context, BUDGET_EXHAUSTED, context_hash=digest)
            recent.append(now)
            self._calls[project_id] = recent
            try:
                raw = self.provider.propose(context)
            except ProviderBlocked as exc:
                result = self._deterministic(context, FALLBACK_DETERMINISTIC,
                                             provider_status=BLOCKED_PROVIDER, detail=str(exc))
            except Exception as exc:  # noqa: BLE001 -- the model never stops the project
                result = self._deterministic(context, FALLBACK_DETERMINISTIC,
                                             provider_status=PROVIDER_ERROR,
                                             detail=redact_text(f"{type(exc).__name__}: {exc}")[:200])
            else:
                validation = validate_plan(raw, store=self.store, project_id=project_id,
                                           lanes=self.lanes, max_tasks=self.config.max_tasks,
                                           approve_high_risk=approve_high_risk)
                name = getattr(self.provider, "name", "provider")
                result = ({"status": PROPOSED, "provider": name, "plan": raw}
                          if validation.ok else
                          {"status": REJECTED, "provider": name, "errors": validation.errors})
            result["context_hash"] = digest
            self._cache[project_id] = (digest, result)
            return result
        finally:
            self._lock.release()

    def apply(self, project_id: str, plan: dict[str, Any], *,
              approve_high_risk: bool = False) -> dict[str, Any]:
        return apply_plan(plan, store=self.store, project_id=project_id, lanes=self.lanes,
                          max_tasks=self.config.max_tasks, approve_high_risk=approve_high_risk,
                          provider=getattr(self.provider, "name", "operator"))
