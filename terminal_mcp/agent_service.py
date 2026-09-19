"""Starting work as an AGENT, on top of the Phase A router.

WHY THERE IS NO SECOND SCHEDULER HERE

The obvious way to build `agent_start` is to give the agent runtime its own
placement logic: it knows the agent's repo, its runtime preference and its
skills, so it could pick a session itself. That would be the second scheduler,
and within a week the two would disagree about which sessions are eligible --
one of them would have the deleted-worktree check and the other would not.

So this composes. `agent_start` resolves the agent, turns its durable identity
into the metadata a TaskProfile already understands (project, repo, workspace,
preferred runtime), resolves its skills to pinned `id@version` labels, and
hands all of it to `TaskRouter.route_start`. Every eligibility rule, the
atomic claim, the bounded dispatch, the truthful receipt and Queue Rescue come
from there unchanged. This module adds identity, capacity and history --
nothing about placement.

WHAT max_sessions ACTUALLY PROTECTS

An agent is one identity with one train of thought. Two sessions running its
tasks at once means two runtimes in the same worktree, which is the collision
the coordinator gate already refuses at dispatch time -- but refusing there
means a task gets bound, ticked, gated and paused before anyone notices. The
capacity check is the same rule applied one step earlier, where the honest
answer is simply "this agent is busy; the task is queued and will start when
it frees up".

It is installed as a ROUTER HOOK rather than an if-statement in `agent_start`,
because Queue Rescue re-routes tasks nobody re-submits. A capacity rule that
only ran on the submission path would be silently bypassed ten seconds later
by the reconcile.

OWNERSHIP IS STABLE; EXECUTION IS NOT

`queue_tasks.agent_id` is written once, when the task is created, and nothing
here ever changes it. `execution_session` moves freely -- released when a
session turns out to be unusable, re-bound by the next rescue. That split is
the whole Phase B thesis: durable Agent, disposable Session.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

from .agent_registry import (
    AGENT_DISABLED, LATEST, SKILL_BASE, SKILL_TASK, Agent, AgentRegistryError,
    AgentRegistryStore,
)
from .queue_store import ROUTING_BOUND_STATES, TERMINAL_STATUSES
from .skill_packages import SkillPackageError, default_skill_roots, discover, load_package

_LOGGER = logging.getLogger(__name__)

RUN_STARTED = "STARTED"
RUN_QUEUED = "QUEUED"
RUN_FAILED = "FAILED"


class AgentService:
    """Agent/Skill CRUD, plus `agent_start` over the existing router."""

    def __init__(self, store: AgentRegistryStore, *, router: Any = None, queue: Any = None,
                 skill_roots: Sequence[str] | None = None) -> None:
        self.store = store
        self.router = router
        self.queue = queue
        self._skill_roots = tuple(skill_roots) if skill_roots else None

    @property
    def skill_roots(self) -> tuple[str, ...]:
        return self._skill_roots if self._skill_roots is not None else default_skill_roots()

    # -- agents ------------------------------------------------------------

    def create_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        try:
            return self.store.create_agent(agent_id, **fields).to_dict()
        except AgentRegistryError as exc:
            return {"error": "INVALID_AGENT", "detail": str(exc)}
        except TypeError as exc:
            return {"error": "INVALID_AGENT_FIELD", "detail": str(exc)}

    def update_agent(self, agent_id: str, **fields: Any) -> dict[str, Any]:
        try:
            return self.store.update_agent(agent_id, **fields).to_dict()
        except AgentRegistryError as exc:
            return {"error": "INVALID_AGENT", "detail": str(exc)}

    def disable_agent(self, agent_id: str) -> dict[str, Any]:
        try:
            return self.store.disable_agent(agent_id).to_dict()
        except AgentRegistryError as exc:
            return {"error": "AGENT_NOT_FOUND", "detail": str(exc)}

    def enable_agent(self, agent_id: str) -> dict[str, Any]:
        try:
            return self.store.enable_agent(agent_id).to_dict()
        except AgentRegistryError as exc:
            return {"error": "AGENT_NOT_FOUND", "detail": str(exc)}

    def list_agents(self, *, project_id: str | None = None, state: str | None = None,
                    limit: int = 200) -> dict[str, Any]:
        agents = self.store.list_agents(project_id=project_id, state=state, limit=limit)
        return {"agents": [self._agent_view(agent) for agent in agents], "count": len(agents)}

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        agent = self.store.get_agent(agent_id)
        if agent is None:
            return {"error": "AGENT_NOT_FOUND", "agent_id": agent_id}
        view = self._agent_view(agent, detailed=True)
        return {"agent": view}

    def _agent_view(self, agent: Agent, *, detailed: bool = False) -> dict[str, Any]:
        """One agent, with the live facts the dashboard asks for.

        Assembled at read time from the queue and the run history rather than
        cached on the agent row -- a cached "current task" is wrong the moment
        the task moves, and this is the same composition posture
        worker_registry already uses for its own view."""
        row = agent.to_dict()
        row["skills"] = [binding.to_dict() for binding in self.store.agent_skills(agent.id)]
        tasks = self._agent_tasks(agent.id)
        active = [task for task in tasks if task["status"] not in TERMINAL_STATUSES]
        running = [task for task in active if task.get("routing_state") in ROUTING_BOUND_STATES]
        row["active_tasks"] = len(active)
        row["queued_tasks"] = len(active) - len(running)
        row["max_sessions"] = agent.max_sessions
        row["at_capacity"] = len(running) >= agent.max_sessions
        current = running[0] if running else None
        row["current_task"] = (
            {"task_id": current["id"], "title": current["title"], "status": current["status"],
             "session": current.get("execution_session"),
             "node_id": current.get("execution_node_id")}
            if current else None)
        row["run_counts"] = self.store.run_counts(agent.id)
        if detailed:
            row["recent_runs"] = self.store.recent_runs(agent.id, limit=10)
            row["recent_failures"] = [run for run in self.store.recent_runs(agent.id, limit=50)
                                      if run.get("status") in ("FAILED", "BLOCKED")][:5]
            row["tasks"] = active[:20]
        return row

    def _agent_tasks(self, agent_id: str) -> list[dict[str, Any]]:
        if self.queue is None:
            return []
        try:
            return self.queue.store.tasks_for_agent(agent_id)
        except Exception:  # noqa: BLE001 -- a dashboard view must not fail on a queue read
            _LOGGER.exception("agent-service: task lookup failed for agent %s", agent_id)
            return []

    # -- capacity (installed on the router) ---------------------------------

    def capacity_block_reason(self, task: Any) -> str | None:
        """Router hook: may this task be placed right now, given its owner?

        Returns a human-readable reason to DEFER, or None to proceed. A task
        with no agent, or an agent this registry has never heard of, is never
        blocked -- Phase A behaviour must be untouched for everything that
        predates agents."""
        agent_id = getattr(task, "agent_id", None)
        if not agent_id:
            return None
        agent = self.store.get_agent(agent_id)
        if agent is None:
            return None
        if agent.state == AGENT_DISABLED:
            return (f"agent {agent.id!r} is disabled; re-enable it to let its queued work run")
        holding = [row for row in self._agent_tasks(agent.id)
                   if row["id"] != getattr(task, "id", None)
                   and row["status"] not in TERMINAL_STATUSES
                   and row.get("routing_state") in ROUTING_BOUND_STATES]
        if len(holding) >= agent.max_sessions:
            return (f"agent {agent.id!r} is at its max_sessions limit "
                    f"({agent.max_sessions}); {len(holding)} task(s) already hold a runtime")
        return None

    # -- skills --------------------------------------------------------------

    def register_skill(self, skill_id: str, *, version: str | None = None, body: str | None = None,
                       name: str = "", summary: str = "",
                       metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        """Register a skill from disk, or inline when `body` is supplied.

        The filesystem path is the normal one and is the one that is rooted and
        bounded; `body` exists for a skill composed by a caller that has no
        package on disk, and never touches the filesystem at all."""
        try:
            if body is not None:
                skill = self.store.register_skill(
                    skill_id, version=version or "1", body=body, name=name or skill_id,
                    summary=summary, source="inline", metadata=metadata)
                return {"skill": skill.to_dict()}
            package = load_package(skill_id, self.skill_roots, version=version)
            skill = self.store.register_skill(
                package.skill_id, version=package.version, body=package.body,
                name=name or package.name, summary=summary or package.summary,
                source="filesystem", package_path=package.path,
                content_sha=package.content_sha, metadata={**package.metadata, **(metadata or {})})
            return {"skill": skill.to_dict(), "loaded_from": package.path}
        except SkillPackageError as exc:
            return {"error": "SKILL_PACKAGE_REJECTED", "detail": str(exc)}
        except AgentRegistryError as exc:
            return {"error": "INVALID_SKILL", "detail": str(exc)}

    def list_skills(self, *, latest_only: bool = True, limit: int = 200) -> dict[str, Any]:
        skills = self.store.list_skills(latest_only=latest_only, limit=limit)
        return {"skills": [skill.to_dict() for skill in skills], "count": len(skills),
                "skill_roots": list(self.skill_roots)}

    def get_skill(self, skill_id: str, *, version: str | None = None,
                  include_body: bool = False) -> dict[str, Any]:
        skill = self.store.get_skill(skill_id, version=version)
        if skill is None:
            return {"error": "SKILL_NOT_FOUND", "skill_id": skill_id, "version": version}
        return {"skill": skill.to_dict(include_body=include_body),
                "versions": self.store.skill_versions(skill_id)}

    def discover_skills(self) -> dict[str, Any]:
        return {"packages": discover(self.skill_roots), "skill_roots": list(self.skill_roots)}

    def bind_agent_skill(self, agent_id: str, skill_id: str, *, kind: str = SKILL_BASE,
                         version: str | None = None) -> dict[str, Any]:
        try:
            binding = self.store.bind_skill(agent_id, skill_id, kind=kind, version=version)
        except AgentRegistryError as exc:
            return {"error": "BIND_REFUSED", "detail": str(exc)}
        return {"binding": binding.to_dict()}

    def unbind_agent_skill(self, agent_id: str, skill_id: str,
                           kind: str | None = None) -> dict[str, Any]:
        removed = self.store.unbind_skill(agent_id, skill_id, kind=kind)
        return {"agent_id": agent_id, "skill_id": skill_id, "removed": removed}

    # -- agent_start ----------------------------------------------------------

    def agent_start(self, agent_id: str, prompt: str, *, title: str | None = None,
                    priority: int = 0, metadata: dict[str, Any] | None = None,
                    request_key: str | None = None, skill_ids: Sequence[str] | None = None,
                    include_task_skills: bool = False,
                    target: str | None = None) -> dict[str, Any]:
        """Start work AS an agent. One call, durable, routed, no target needed.

        Everything about placement is the router's; everything about identity
        is this method's. `target`, if given, is forwarded and stays hard
        affinity exactly as it is for `route_start` -- naming a session means
        that session, whoever asked."""
        if self.router is None:
            return {"error": "ROUTER_UNAVAILABLE",
                    "detail": "agent_start needs the task router, which is not wired here"}
        agent = self.store.get_agent(agent_id)
        if agent is None:
            return {"error": "AGENT_NOT_FOUND", "agent_id": agent_id}
        if not agent.enabled:
            # Refused up front rather than queued: a disabled agent is a
            # decision someone made, and quietly accepting work for it would
            # leave a task nobody is coming to run.
            return {"error": "AGENT_DISABLED", "agent_id": agent_id,
                    "detail": f"agent {agent_id!r} is disabled; enable it before starting work"}
        if not prompt or not str(prompt).strip():
            return {"error": "TEXT_REQUIRED"}

        resolved_skills = self.store.resolve_skills(
            agent.id, extra=skill_ids or (), include_task_skills=include_task_skills)

        # The agent's durable identity, expressed as the metadata TaskProfile
        # already reads. setdefault throughout: an explicit per-call value
        # always wins over the agent's standing default.
        full_metadata = dict(metadata or {})
        full_metadata.setdefault("agent_id", agent.id)
        if agent.project_id:
            full_metadata.setdefault("project_id", agent.project_id)
            full_metadata.setdefault("project", agent.project_id)
        if agent.repo:
            full_metadata.setdefault("repo", agent.repo)
        if agent.workspace:
            full_metadata.setdefault("workspace", agent.workspace)
        if agent.runtime:
            full_metadata.setdefault("runtime", agent.runtime)
        if agent.model:
            full_metadata.setdefault("model", agent.model)

        receipt = self.router.route_start(
            prompt, title=title or f"{agent.name}: work", priority=priority,
            metadata=full_metadata, request_key=request_key,
            project=agent.project_id, agent_id=agent.id, skill_ids=resolved_skills,
            target=target)

        task_id = receipt.get("task_id") if isinstance(receipt, dict) else None
        if task_id:
            dispatched = bool(receipt.get("dispatched"))
            self.store.record_run(
                agent.id, task_id=task_id, session=receipt.get("session"),
                node_id=receipt.get("node_id"),
                status=RUN_STARTED if dispatched else RUN_QUEUED,
                detail=receipt.get("routing_reason"))
        return {**receipt, "agent_id": agent.id, "agent_name": agent.name,
                "skills": resolved_skills}
