"""Project -> phase -> team -> task. The pipeline, made durable.

WHAT THIS ADDS TO PHASE B

Phase B gave agents a durable identity. It did not say where agents come from,
and in practice they came from a human typing `create_agent` eight times. This
module derives them: a project is analysed, a phase-scoped team is proposed,
and only the CURRENT phase's agents are materialised.

WHY PHASE-SCOPED AND NOT ALWAYS-ON

A project is a pipeline, not a standing organisation. Creating all eight agents
at bootstrap means a QA identity exists before anything is written and the
build agents are in the room while planning should still own the decisions.
Worse, every one of them accumulates nothing until its phase arrives, so the
"team" is seven idle rows and one working agent.

So agents are materialised on arrival at the phase that needs them, and go
DORMANT -- not deleted -- when the project moves on. Dormant matters because
phases go backwards: a failed review sends BUILD back to work, and the agent
that did the building still has its history.

SEPARATION OF DUTIES IS ENFORCED, NOT SUGGESTED

The agent that wrote the code is refused as the approval in REVIEW, TEST and
RELEASE unless the project explicitly sets `policy.allow_self_approval`. See
project_analyzer.SEPARATION_OF_DUTIES for why that is the one collapse the
role tables will not perform even on a one-person project.

NO PARALLEL STORE

Projects live in the Phase B agent registry beside the agents they own, so the
`project_id` on an agent is a real foreign key. `queue_tasks.project_id` is
untouched and project_service.py's composition view over it still works
whether or not a Project row exists -- registering a project adds a team and a
phase; it gates nothing that worked before.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from . import project_analyzer as pa
from .agent_registry import (
    AGENT_ACTIVE, AGENT_DORMANT, AGENT_RETIRED, AGENT_STARTABLE, SKILL_BASE,
    AgentRegistryError,
)
from .project_analyzer import (
    APPROVAL_PHASES, BUILD_ROLES, PHASE_GATE, PHASE_ORDER, PHASES, TeamPlan,
)
from .queue_store import ROUTING_BOUND_STATES, TERMINAL_STATUSES

_LOGGER = logging.getLogger(__name__)

BOOTSTRAP_PENDING = "PENDING"
BOOTSTRAP_DONE = "BOOTSTRAPPED"

#: Returned instead of silently picking a random session when a project has no
#: agent able to take the work. The task is still durably created.
NEEDS_TEAM_REVIEW = "NEEDS_TEAM_REVIEW"


def _score_agent(agent: Any, *, wanted: Sequence[str], phase: str,
                 load: Mapping[str, tuple[int, int]]) -> tuple[int, list[str]]:
    """How well this agent fits the work. Deterministic, same as the session
    matcher: a project routing decision has to be reproducible too."""
    score = 0
    reasons: list[str] = []
    capabilities = set((agent.metadata or {}).get("capabilities") or ())
    if agent.role:
        capabilities.update(pa.ROLE_CATALOGUE.get(agent.role, {}).get("capabilities", ()))

    overlap = sorted(capabilities & set(wanted))
    if overlap:
        score += 30 * len(overlap)
        reasons.append(f"+{30 * len(overlap)} capability match ({', '.join(overlap)})")
    if phase in (agent.phases or ()):
        score += 40
        reasons.append(f"+40 owns the current phase ({phase})")
    elif not agent.phases:
        score += 5
        reasons.append("+5 not phase-scoped, so available in any phase")
    active, queued = load.get(agent.id, (0, 0))
    if active == 0 and queued == 0:
        score += 20
        reasons.append("+20 idle")
    else:
        penalty = 10 * min(active + queued, 4)
        score -= penalty
        reasons.append(f"-{penalty} {active} active / {queued} queued task(s)")
    return score, reasons


class ProjectRuntimeService:
    """Project CRUD, analysis, phase-scoped bootstrap, and project_start."""

    def __init__(self, agents: Any) -> None:
        # The Phase B AgentService -- and through it the one registry store,
        # the queue and the router. This service owns no store of its own.
        self.agents = agents

    @property
    def store(self) -> Any:
        return self.agents.store

    @property
    def queue(self) -> Any:
        return self.agents.queue

    # -- analysis ------------------------------------------------------------

    def plan(self, name: str, *, description: str = "", repo_root: str | None = None,
             project_id: str | None = None, complexity: str | None = None,
             runtime: str | None = None, max_agents: int | None = None,
             phase: str = pa.INTAKE, stack: Sequence[str] = (),
             modules: Sequence[str] = (), capabilities: Sequence[str] = ()) -> dict[str, Any]:
        """Analyse and propose. Writes nothing -- this is the wizard's step 2/3."""
        if repo_root and not self._cwd_allowed(repo_root):
            return {"error": "REPO_NOT_ALLOWED", "repo_root": repo_root,
                    "detail": "repo_root is outside the configured allowed cwd roots"}
        profile = pa.analyze(
            name, description=description, repo_root=repo_root, project_id=project_id,
            complexity=complexity, stack=stack, modules=modules, capabilities=capabilities)
        team = pa.plan_team(profile, runtime=runtime, max_agents=max_agents, phase=phase)
        return {"plan": team.to_dict(),
                "separation_of_duties": pa.SEPARATION_OF_DUTIES}

    def _cwd_allowed(self, path: str) -> bool:
        """Reuse the deployment's existing allowed-cwd policy rather than
        inventing a second one. No policy configured means no restriction,
        which is the behaviour every other path in this project already has."""
        config = getattr(getattr(self.agents, "queue", None), "config", None) or \
            getattr(self.agents, "config", None)
        lifecycle = getattr(config, "session_lifecycle", None)
        roots = tuple(getattr(lifecycle, "allowed_cwd_roots", ()) or ())
        if not roots:
            return True
        import os
        try:
            resolved = os.path.realpath(path)
        except OSError:
            return False
        return any(resolved == os.path.realpath(root)
                   or resolved.startswith(os.path.realpath(root).rstrip("/") + "/")
                   for root in roots)

    # -- project CRUD ---------------------------------------------------------

    def create_project(self, project_id: str, *, name: str | None = None, description: str = "",
                       repo_root: str | None = None, **fields: Any) -> dict[str, Any]:
        if repo_root and not self._cwd_allowed(repo_root):
            return {"error": "REPO_NOT_ALLOWED", "repo_root": repo_root}
        try:
            project = self.store.create_project(
                project_id, name=name, description=description, repo_root=repo_root, **fields)
        except AgentRegistryError as exc:
            return {"error": "INVALID_PROJECT", "detail": str(exc)}
        return {"project": project.to_dict()}

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any]:
        try:
            return {"project": self.store.update_project(project_id, **fields).to_dict()}
        except AgentRegistryError as exc:
            return {"error": "INVALID_PROJECT", "detail": str(exc)}

    def archive_project(self, project_id: str) -> dict[str, Any]:
        """Archive the project and retire its team in one step -- an archived
        project whose agents are still startable is a contradiction that would
        let work continue on something nobody is running any more."""
        try:
            project = self.store.archive_project(project_id)
        except AgentRegistryError as exc:
            return {"error": "PROJECT_NOT_FOUND", "detail": str(exc)}
        retired = []
        for agent in self.store.list_agents(project_id=project_id):
            if agent.state != AGENT_RETIRED:
                self.store.update_agent(agent.id, state=AGENT_RETIRED,
                                        phase_state_reason="project archived")
                retired.append(agent.id)
        return {"project": project.to_dict(), "retired_agents": retired}

    def list_projects(self, *, status: str | None = None) -> dict[str, Any]:
        projects = self.store.list_projects(status=status)
        return {"projects": [self._project_summary(project) for project in projects],
                "count": len(projects)}

    def get_project(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        if project is None:
            return {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        view = self._project_summary(project, detailed=True)
        return {"project": view}

    # -- views -----------------------------------------------------------------

    def _lane_load(self) -> dict[str, tuple[int, int]]:
        """agent_id -> (active, queued), from ONE bulk queue read.

        Bulk on purpose: a projects page with twelve agents must not perform
        twelve task queries, and must never probe a session per card."""
        load: dict[str, tuple[int, int]] = {}
        if self.queue is None:
            return load
        try:
            rows = [task for lane in self.queue.store.list_all_lanes()
                    for task in lane["tasks"]]
        except Exception:  # noqa: BLE001
            _LOGGER.exception("project-runtime: bulk task read failed")
            return load
        for task in rows:
            agent_id = task.get("agent_id")
            if not agent_id or task["status"] in TERMINAL_STATUSES:
                continue
            active, queued = load.get(agent_id, (0, 0))
            if task.get("routing_state") in ROUTING_BOUND_STATES:
                load[agent_id] = (active + 1, queued)
            else:
                load[agent_id] = (active, queued + 1)
        return load

    def _project_summary(self, project: Any, *, detailed: bool = False) -> dict[str, Any]:
        row = project.to_dict()
        agents = self.store.list_agents(project_id=project.id)
        load = self._lane_load()
        running = queued = 0
        team = []
        for agent in agents:
            active_count, queued_count = load.get(agent.id, (0, 0))
            running += active_count
            queued += queued_count
            card = {
                "id": agent.id, "name": agent.name, "role": agent.role,
                "state": agent.state, "startable": agent.state in AGENT_STARTABLE,
                "phases": list(agent.phases), "runtime": agent.runtime,
                "cross_phase": agent.cross_phase,
                "can_approve": agent.role in pa.ASSURANCE_ROLES,
                "max_sessions": agent.max_sessions,
                "active_tasks": active_count, "queued_tasks": queued_count,
                "in_current_phase": project.phase in (agent.phases or ()),
                "phase_state_reason": agent.phase_state_reason,
                "skills": [binding.to_dict() for binding in self.store.agent_skills(agent.id)],
            }
            if detailed:
                card["run_counts"] = self.store.run_counts(agent.id)
                card["recent_runs"] = self.store.recent_runs(agent.id, limit=5)
            team.append(card)
        row["agents"] = team
        row["agent_count"] = len(team)
        row["active_agent_count"] = sum(1 for card in team if card["startable"])
        row["running_tasks"] = running
        row["queued_tasks"] = queued
        row["phase_gate"] = PHASE_GATE.get(project.phase)
        row["next_phase"] = pa.next_phase(project.phase)
        row["phases"] = list(PHASES)
        if detailed:
            row["phase_history"] = self.store.phase_history(project.id, limit=20)
            row["upcoming"] = self._upcoming(project)
        return row

    def _upcoming(self, project: Any) -> dict[str, list[dict[str, Any]]]:
        profile = self._profile_of(project)
        existing = {agent.role for agent in self.store.list_agents(project_id=project.id)}
        upcoming: dict[str, list[dict[str, Any]]] = {}
        for phase in PHASES:
            if PHASE_ORDER[phase] <= PHASE_ORDER.get(project.phase, 0):
                continue
            upcoming[phase] = [
                {**spec.to_dict(), "already_exists": spec.role in existing}
                for spec in pa.plan_phase_team(profile, phase)]
        return upcoming

    def _profile_of(self, project: Any) -> pa.ProjectProfile:
        """The stored profile, rebuilt. Stored rather than re-derived so a
        team stays stable when a repository changes underneath it; re-analysis
        is an explicit action, not a side effect of opening a page."""
        stored = project.profile or {}
        return pa.ProjectProfile(
            project_id=project.id, name=project.name, description=project.description,
            repo_root=project.repo_root,
            domain=project.domain, stack=tuple(project.stack), modules=tuple(project.modules),
            capabilities=tuple(project.capabilities), complexity=project.complexity,
            signals=stored.get("signals", {}) if isinstance(stored, dict) else {})

    def phase_status(self, project_id: str) -> dict[str, Any]:
        project = self.store.get_project(project_id)
        if project is None:
            return {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        agents = self.store.list_agents(project_id=project_id)
        return {
            "project_id": project_id, "phase": project.phase,
            "phase_entered_at": project.phase_entered_at,
            "gate": PHASE_GATE.get(project.phase),
            "next_phase": pa.next_phase(project.phase),
            "phases": list(PHASES),
            "active_agents": [agent.id for agent in agents
                              if agent.state in AGENT_STARTABLE
                              and project.phase in (agent.phases or ())],
            "dormant_agents": [agent.id for agent in agents if agent.state == AGENT_DORMANT],
            "upcoming": self._upcoming(project),
            "history": self.store.phase_history(project_id, limit=20),
        }

    # -- bootstrap -------------------------------------------------------------

    def _ensure_skills(self, skill_ids: Sequence[str]) -> tuple[list[str], list[str]]:
        """Register any built-in skill the plan needs. (resolved, unresolved).

        Built-in templates only. A skill id the analyzer proposed that has no
        template and is not already registered comes back UNRESOLVED rather
        than being invented -- writing prompt text for a capability nobody
        defined would be the system making up an agent's instructions."""
        resolved: list[str] = []
        unresolved: list[str] = []
        for skill_id in skill_ids:
            if self.store.get_skill(skill_id) is not None:
                resolved.append(skill_id)
                continue
            template = pa.BUILTIN_SKILLS.get(skill_id)
            if template is None:
                unresolved.append(skill_id)
                continue
            self.store.register_skill(
                skill_id, version="1", body=template["body"], name=template["name"],
                summary=template["summary"], source="builtin")
            resolved.append(skill_id)
        return resolved, unresolved

    def _materialise(self, project: Any, specs: Sequence[Any], *,
                     reason: str) -> tuple[list[dict[str, Any]], list[str]]:
        """Create or wake the agents for a phase. Idempotent by agent id.

        Re-running bootstrap, or returning to a phase, must never produce a
        second `<project>-qa`. An agent that already exists is updated and
        woken; only a genuinely new role is created."""
        created: list[dict[str, Any]] = []
        unresolved: list[str] = []
        for spec in specs:
            skills, missing = self._ensure_skills(spec.base_skills)
            unresolved.extend(missing)
            existing = self.store.get_agent(spec.agent_id)
            if existing is None:
                agent = self.store.create_agent(
                    spec.agent_id, name=spec.name, project_id=project.id,
                    description=spec.description, runtime=spec.runtime or project.policy.get(
                        "runtime_preferred"),
                    max_sessions=spec.max_sessions, repo=project.repo_root,
                    workspace=project.workspace, role=spec.role, phases=spec.phases,
                    cross_phase=spec.role in pa.CROSS_PHASE_ROLES,
                    metadata={"capabilities": list(spec.capabilities), "reason": spec.reason})
                action = "created"
            else:
                merged_phases = tuple(dict.fromkeys([*existing.phases, *spec.phases]))
                agent = self.store.update_agent(
                    spec.agent_id, state=AGENT_ACTIVE, phases=list(merged_phases),
                    role=spec.role, phase_state_reason=reason)
                action = "reactivated" if existing.state != AGENT_ACTIVE else "kept"
            for skill_id in skills:
                self.store.bind_skill(agent.id, skill_id, kind=SKILL_BASE)
            created.append({"agent_id": agent.id, "role": agent.role, "action": action,
                            "skills": skills, "reason": spec.reason,
                            "cross_phase": agent.cross_phase})
        return created, sorted(set(unresolved))

    def bootstrap(self, name: str, *, description: str = "", repo_root: str | None = None,
                  project_id: str | None = None, complexity: str | None = None,
                  runtime: str | None = None, max_agents: int | None = None,
                  policy: dict[str, Any] | None = None,
                  roles: Sequence[str] | None = None,
                  request_key: str | None = None) -> dict[str, Any]:
        """Create the project and materialise ONLY its first phase's team.

        Idempotent by project id: re-running reconciles instead of duplicating,
        which is what makes this safe to call from a wizard that the user may
        submit twice.

        `roles`, when given, filters the proposed team -- the wizard's
        "uncheck the ones you disagree with". The PM is exempt: disabling it is
        `policy.disable_pm`, a deliberate project-level decision rather than an
        unchecked box."""
        plan_result = self.plan(
            name, description=description, repo_root=repo_root, project_id=project_id,
            complexity=complexity, runtime=runtime, max_agents=max_agents)
        if "error" in plan_result:
            return plan_result
        plan = plan_result["plan"]
        resolved_id = plan["profile"]["project_id"]
        merged_policy = {"runtime_preferred": runtime, **(policy or {})}

        project = self.store.get_project(resolved_id)
        if project is None:
            created = self.create_project(
                resolved_id, name=name, description=description, repo_root=repo_root,
                stack=plan["profile"]["stack"], modules=plan["profile"]["modules"],
                capabilities=plan["profile"]["capabilities"],
                complexity=plan["profile"]["complexity"], policy=merged_policy,
                profile=plan["profile"])
            if "error" in created:
                return created
            project = self.store.get_project(resolved_id)
        else:
            project = self.store.update_project(
                resolved_id, description=description or project.description,
                stack=plan["profile"]["stack"], modules=plan["profile"]["modules"],
                capabilities=plan["profile"]["capabilities"],
                complexity=plan["profile"]["complexity"], profile=plan["profile"],
                policy={**project.policy, **merged_policy})

        profile = self._profile_of(project)
        specs = list(pa.plan_phase_team(profile, project.phase, runtime=runtime,
                                        max_agents=max_agents))
        if roles is not None:
            wanted = set(roles)
            specs = [spec for spec in specs
                     if spec.role in wanted or spec.role in pa.CROSS_PHASE_ROLES]
        if project.policy.get("disable_pm"):
            specs = [spec for spec in specs if spec.role not in pa.CROSS_PHASE_ROLES]

        created, unresolved = self._materialise(
            project, specs, reason=f"bootstrap into {project.phase}")

        pm_id = next((row["agent_id"] for row in created if row["cross_phase"]), None)
        if pm_id and project.pm_agent_id != pm_id:
            project = self.store.update_project(resolved_id, pm_agent_id=pm_id)
        project = self.store.update_project(resolved_id, bootstrap_state=BOOTSTRAP_DONE)
        self.store.set_phase(
            project.id, project.phase, reason="team bootstrapped",
            active_agent_ids=[row["agent_id"] for row in created],
            gate_evidence={"request_key": request_key} if request_key else None,
            actor="project_bootstrap")
        return {
            "project": self._project_summary(self.store.get_project(resolved_id), detailed=True),
            "plan": plan, "agents": created, "unresolved_skills": unresolved,
            "pm_agent_id": pm_id,
            "request_key": request_key,
        }

    # -- phase transitions -------------------------------------------------------

    def reconcile_team(self, project_id: str, *, reason: str = "reconcile") -> dict[str, Any]:
        """Make the live team match the current phase.

        Wakes or creates what this phase needs; makes dormant what it does not.
        The PM is never touched -- that is the whole point of cross_phase. A
        dormant agent keeps its identity, skills and history, so returning to
        a phase wakes the original rather than creating a second one."""
        project = self.store.get_project(project_id)
        if project is None:
            return {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        profile = self._profile_of(project)
        specs = pa.plan_phase_team(profile, project.phase,
                                   runtime=project.policy.get("runtime_preferred"))
        if project.policy.get("disable_pm"):
            specs = tuple(spec for spec in specs if spec.role not in pa.CROSS_PHASE_ROLES)
        activated, unresolved = self._materialise(project, specs, reason=reason)
        wanted_ids = {row["agent_id"] for row in activated}

        slept: list[str] = []
        for agent in self.store.list_agents(project_id=project_id):
            if agent.id in wanted_ids or agent.cross_phase:
                continue
            if agent.state in (AGENT_DORMANT, AGENT_RETIRED):
                continue
            self.store.update_agent(
                agent.id, state=AGENT_DORMANT,
                phase_state_reason=f"{project.phase} does not need a {agent.role}")
            slept.append(agent.id)
        return {"project_id": project_id, "phase": project.phase,
                "activated": activated, "dormant": slept, "unresolved_skills": unresolved}

    def advance(self, project_id: str, *, to_phase: str | None = None, reason: str = "",
                gate_evidence: dict[str, Any] | None = None,
                handoff: dict[str, Any] | None = None, actor: str = "mcp") -> dict[str, Any]:
        """Move to the next phase (or a named one) and reconcile the team.

        Backwards is legal and deliberate: a failed review returns to BUILD,
        and the agents that did the building wake up with their history rather
        than being recreated empty."""
        project = self.store.get_project(project_id)
        if project is None:
            return {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        target = to_phase or pa.next_phase(project.phase)
        if target is None:
            return {"error": "NO_NEXT_PHASE", "phase": project.phase,
                    "detail": f"{project.phase} is the final phase"}
        if target not in PHASES:
            return {"error": "UNKNOWN_PHASE", "phase": target, "allowed": list(PHASES)}

        outgoing = [agent.id for agent in self.store.list_agents(project_id=project_id)
                    if agent.state in AGENT_STARTABLE]
        self.store.set_phase(
            project_id, target,
            reason=reason or f"advanced from {project.phase} to {target}",
            gate_evidence={**(gate_evidence or {}),
                           "previous_gate": PHASE_GATE.get(project.phase)},
            active_agent_ids=outgoing, handoff=handoff, actor=actor)
        reconciled = self.reconcile_team(project_id, reason=f"entered {target}")
        return {"project_id": project_id, "from_phase": project.phase, "phase": target,
                "gate": PHASE_GATE.get(target), "handoff": handoff or {},
                "next_phase": pa.next_phase(target), **reconciled}

    # -- project_start: PM orchestration -> specialist -> router -> session ------

    def select_agent(self, project_id: str, *, wanted: Sequence[str] = (),
                     phase: str | None = None,
                     approval: bool = False) -> tuple[Any, dict[str, Any]]:
        """PM orchestration: which specialist should take this work?

        Returns (agent, rationale). The rationale is persisted on the task, so
        a surprising assignment is traceable to the scoring rather than to the
        router's reputation. Hard rejects come first, for the same reason they
        do in the session matcher: they are safety facts, not preferences."""
        project = self.store.get_project(project_id)
        if project is None:
            return None, {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        current_phase = phase or project.phase
        load = self._lane_load()
        considered: list[dict[str, Any]] = []
        best: tuple[int, Any, list[str]] | None = None

        for agent in self.store.list_agents(project_id=project_id):
            rejected = None
            if agent.state not in AGENT_STARTABLE:
                rejected = f"agent is {agent.state}"
            elif agent.project_id != project_id:
                rejected = "agent belongs to a different project"
            elif approval and not pa.can_approve(agent.role, current_phase):
                # The separation-of-duties gate. A build role wrote the thing
                # and the PM asked for it; neither is an independent look.
                rejected = (f"role {agent.role!r} may not supply the {current_phase} approval "
                            f"(separation of duties)")
                if project.allow_self_approval and agent.role not in pa.CROSS_PHASE_ROLES:
                    # `allow_self_approval` waives the DEV self-approval rule:
                    # a team that small has nobody else. It never promotes the
                    # PM into an approver -- the coordinator that asked for the
                    # work to be finished is the one party whose sign-off adds
                    # nothing at any project size.
                    rejected = None
            if rejected is None and agent.role in pa.CROSS_PHASE_ROLES and not approval:
                # The PM coordinates; it does not implement. It is still
                # selectable for coordination work, just ranked last, so it
                # never takes a build task away from a specialist.
                pass
            if rejected:
                considered.append({"agent_id": agent.id, "role": agent.role,
                                   "eligible": False, "rejected": rejected})
                continue
            score, reasons = _score_agent(agent, wanted=wanted, phase=current_phase, load=load)
            if agent.role in pa.CROSS_PHASE_ROLES and not approval and wanted:
                overlap = set(pa.ROLE_CATALOGUE["pm"]["capabilities"]) & set(wanted)
                if not overlap:
                    score -= 50
                    reasons.append("-50 the project manager coordinates rather than implements")
            considered.append({"agent_id": agent.id, "role": agent.role, "eligible": True,
                               "score": score, "reasons": reasons})
            if best is None or score > best[0] or (score == best[0] and agent.id < best[1].id):
                best = (score, agent, reasons)

        considered.sort(key=lambda row: (-(row.get("score") or -999), row["agent_id"]))
        rationale = {
            "project_id": project_id, "phase": current_phase,
            "wanted_capabilities": list(wanted), "approval": approval,
            "pm_agent_id": project.pm_agent_id,
            "candidates": considered[:8],
        }
        if best is None:
            rationale["reason"] = (
                f"no agent in project {project_id!r} can take this work in {current_phase}"
                + (" as an independent approval" if approval else ""))
            return None, rationale
        rationale["chosen"] = best[1].id
        rationale["score"] = best[0]
        rationale["reason"] = f"score {best[0]}: " + "; ".join(best[2])
        return best[1], rationale

    def project_start(self, project_id: str, prompt: str, *, title: str | None = None,
                      capabilities: Sequence[str] = (), approval: bool = False,
                      agent_id: str | None = None, priority: int = 0,
                      metadata: dict[str, Any] | None = None,
                      request_key: str | None = None,
                      target: str | None = None) -> dict[str, Any]:
        """Send work to a PROJECT. No agent, no session -- the pipeline decides.

        Project -> PM orchestration -> specialist Agent -> Router -> Session.
        Each arrow is an existing component: this method only chooses the
        agent and then calls Phase B's agent_start, which calls Phase A's
        router. Nothing here places a session.

        When no agent can take the work the task is still created durably and
        the answer is NEEDS_TEAM_REVIEW with the per-candidate reasons --
        never a silently-picked random session."""
        project = self.store.get_project(project_id)
        if project is None:
            return {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        if not project.active:
            return {"error": "PROJECT_ARCHIVED", "project_id": project_id}

        wanted = list(capabilities) or self._infer_capabilities(prompt, project.phase)
        if agent_id:
            agent = self.store.get_agent(agent_id)
            if agent is None or agent.project_id != project_id:
                return {"error": "AGENT_NOT_IN_PROJECT", "agent_id": agent_id,
                        "project_id": project_id}
            rationale = {"chosen": agent_id, "reason": "caller named the agent explicitly",
                         "explicit_agent": True, "pm_agent_id": project.pm_agent_id}
        else:
            agent, rationale = self.select_agent(
                project_id, wanted=wanted, approval=approval)

        if agent is None:
            return {"status": NEEDS_TEAM_REVIEW, "project_id": project_id,
                    "phase": project.phase, "agent_id": None, "session": None,
                    "poll": False, "needs_human": True, "next_action": "review the team",
                    "routing_reason": rationale.get("reason"),
                    "candidates": rationale.get("candidates", []),
                    "guidance": (
                        f"project {project_id!r} has no agent able to take this work in "
                        f"{project.phase}. Advance the phase, reconcile the team, or create "
                        f"the missing role -- the request was not started and no session was "
                        f"picked at random")}

        receipt = self.agents.agent_start(
            agent.id, prompt, title=title, priority=priority,
            metadata={**(metadata or {}), "project_id": project_id,
                      "project_phase": project.phase,
                      "pm_rationale": rationale.get("reason"),
                      "pm_agent_id": project.pm_agent_id},
            request_key=request_key, target=target)
        task_id = receipt.get("task_id") if isinstance(receipt, dict) else None
        if task_id:
            # The DURABLE column, not just metadata. queue_tasks.project_id is
            # what project_service.py's view, the Global Tasks card and every
            # per-project query read; leaving it null put the project in the
            # metadata blob only, where none of them look. Found live: a
            # project task came back with project_id=None.
            try:
                self.queue.store.set_task_project(task_id, project_id)
            except Exception:  # noqa: BLE001 -- never fail a started task over its label
                _LOGGER.exception("project-runtime: could not stamp project on task %s", task_id)
        return {**receipt, "project_id": project_id, "phase": project.phase,
                "agent_role": agent.role, "pm_agent_id": project.pm_agent_id,
                "agent_selection_reason": rationale.get("reason"),
                "agent_candidates": rationale.get("candidates", [])}

    # -- PM recovery ---------------------------------------------------------

    def recover_stalled(self, project_id: str | None = None, *, limit: int = 25,
                        dry_run: bool = False) -> dict[str, Any]:
        """The PM's own recovery pass: stalled runtimes back to the router.

        This is the PM acting, not reporting. A project whose execution
        session died has tasks that look perfectly assigned and are going
        nowhere, and nothing else in the system picks them up -- see
        pm_recovery.py for why routable_tasks and bound_unstarted_tasks both
        miss exactly this case.

        Durable ownership (project, agent, skills, evidence) is preserved
        throughout; only the runtime binding is released and re-decided.
        """
        from .pm_recovery import ProjectPMRecovery

        if project_id:
            project = self.store.get_project(project_id)
            if project is None:
                return {"error": "PROJECT_NOT_FOUND", "project_id": project_id}
        router = getattr(self.queue, "router", None)
        controller = getattr(router, "controller", None)
        recovery = ProjectPMRecovery(self.queue.store, router=router, controller=controller)
        result = recovery.sweep(project_id=project_id, limit=limit, dry_run=dry_run)
        if project_id:
            project = self.store.get_project(project_id)
            result["pm_agent_id"] = getattr(project, "pm_agent_id", None)
            result["phase"] = getattr(project, "phase", None)
        return result

    @staticmethod
    def _infer_capabilities(prompt: str, phase: str) -> list[str]:
        """What this task needs, from its own words plus the current phase.

        Deliberately the same shape as task_profile's heuristics: metadata
        would be better, and when the caller supplies `capabilities` this is
        not consulted at all."""
        wanted: list[str] = []
        for module, pattern in pa.MODULE_PATTERNS:
            if pattern.search(prompt or ""):
                role = pa.MODULE_ROLE.get(module)
                if role:
                    wanted.extend(pa.ROLE_CATALOGUE[role]["capabilities"])
        if wanted:
            # The prompt named a module, so THAT is the signal. Adding the
            # phase's generic capabilities here as well would give the
            # phase's default role the same match count as the specialist and
            # decide every task by alphabetical order of agent id -- observed:
            # "the map page layout breaks" routed to core rather than ui.
            return list(dict.fromkeys(wanted))
        # Nothing module-specific was said, so fall back to whatever this
        # phase is for; generic work belongs to the phase's own role.
        for role in pa.PHASE_ROLES.get(phase, ()):
            wanted.extend(pa.ROLE_CATALOGUE[role]["capabilities"])
        return list(dict.fromkeys(wanted))
