"""TMCP-PROJECT-BOOTSTRAP-001 -- project, phases, PM, and the generated team.

Three claims are under test, and everything here is one of them:

  1. The team is RIGHT-SIZED and PHASE-SCOPED. A small project does not get an
     eight-agent organisation, and no project gets agents for phases it has
     not reached.
  2. The PM is DURABLE and SINGULAR. One per project, surviving every
     transition and every re-bootstrap, while the specialists around it change.
  3. SEPARATION OF DUTIES holds. The agent that built it, and the PM that
     asked for it, are both refused as the independent approval.
"""
from __future__ import annotations

import pytest

from terminal_mcp import project_analyzer as pa
from terminal_mcp.agent_registry import (
    AGENT_ACTIVE, AGENT_DORMANT, AGENT_RETIRED, AgentRegistryError, AgentRegistryStore,
)
from terminal_mcp.agent_service import AgentService
from terminal_mcp.project_analyzer import (
    APPROVAL_PHASES, ASSURANCE_ROLES, BUILD_ROLES, CROSS_PHASE_ROLES, PHASES, TEAM_SIZE,
    analyze, can_approve, collapse_role, plan_phase_team, plan_team,
)
from terminal_mcp.project_runtime import NEEDS_TEAM_REVIEW, ProjectRuntimeService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.task_router import TaskRouter

from tests.test_task_router import FakeController, FakeRecord, RecordingEngine, _row


BIG = "Web app with map, camera streams, AI analysis API, backend, deploy."
SMALL_DESC = "A small CLI that renames files."


@pytest.fixture
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


@pytest.fixture
def registry(tmp_path):
    return AgentRegistryStore(tmp_path / "agents.db")


def _fleet(*names):
    return FakeController(
        sessions=[_row(name) for name in names],
        records=[FakeRecord(node_id="local", session_name=name, agent_type="claude",
                            last_known_state="IDLE") for name in names])


@pytest.fixture
def wired(registry, queue):
    """Exactly what build_mcp composes: registry -> agents -> router -> projects."""
    controller = _fleet("s1", "s2", "s3")
    agents = AgentService(registry, queue=queue, skill_roots=())
    router = TaskRouter(queue.store, controller=controller, queue=queue,
                        engine=RecordingEngine(queue.store),
                        session_registry=controller.session_registry,
                        config=None, capacity_check=agents.capacity_block_reason)
    agents.router = router
    queue.router = router
    projects = ProjectRuntimeService(agents)
    return projects, agents, router, controller


# ---------------------------------------------------------------------------
# Analysis and right-sizing.
# ---------------------------------------------------------------------------

def test_stack_and_modules_are_detected_from_a_real_repository(tmp_path):
    repo = tmp_path / "app"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "package.json").write_text("{}", encoding="utf-8")
    (repo / "pyproject.toml").write_text("", encoding="utf-8")
    (repo / "tests").mkdir()

    profile = analyze("app", description="", repo_root=str(repo))

    assert {"node", "python", "github-actions", "tests"} <= set(profile.stack)
    assert {"frontend", "backend", "deploy", "qa"} <= set(profile.modules)
    assert profile.signals["repo_scanned"] is True
    assert profile.signals["repo_evidence"]


def test_a_missing_repo_still_produces_a_plan_rather_than_failing(tmp_path):
    profile = analyze("app", description=BIG, repo_root=str(tmp_path / "nope"))
    assert profile.modules
    assert profile.signals["repo_scanned"] is False


@pytest.mark.parametrize("description,expected", [
    (SMALL_DESC, pa.SMALL),
    ("A web app with a backend API and tests.", pa.MEDIUM),
    (BIG, pa.LARGE),
])
def test_complexity_bands_follow_the_number_of_modules(description, expected):
    assert analyze("p", description=description).complexity == expected


@pytest.mark.parametrize("description", [SMALL_DESC, "web app with backend api and tests", BIG])
def test_every_phase_team_stays_inside_its_size_band(description):
    """The failure this prevents is proposing the same big organisation for
    every project."""
    profile = analyze("p", description=description)
    low, high = TEAM_SIZE[profile.complexity]
    for phase in PHASES:
        team = plan_phase_team(profile, phase)
        assert 1 <= len(team) <= high, (phase, [a.role for a in team])


def test_a_small_project_collapses_specialists_but_never_the_pm_or_qa():
    profile = analyze("tiny", description=SMALL_DESC)
    build = {agent.role for agent in plan_phase_team(profile, pa.BUILD)}
    test = {agent.role for agent in plan_phase_team(profile, pa.TEST)}
    assert build == {"pm", "core"}
    assert test == {"pm", "qa"}          # QA is never merged into the builder


def test_a_large_project_uses_specialists_per_module():
    profile = analyze("big", description=BIG)
    build = [agent.role for agent in plan_phase_team(profile, pa.BUILD)]
    assert "pm" in build and "core" in build
    assert {"ui", "backend", "ai"} <= set(build)


def test_no_phase_team_contains_the_same_role_twice():
    for description in (SMALL_DESC, BIG):
        profile = analyze("p", description=description)
        for phase in PHASES:
            roles = [agent.role for agent in plan_phase_team(profile, phase)]
            assert len(roles) == len(set(roles)), (phase, roles)


def test_the_plan_shows_later_phases_without_creating_them():
    plan = plan_team(analyze("big", description=BIG))
    assert plan.phase == pa.INTAKE
    assert {agent.role for agent in plan.agents} == {"pm", "analyst"}
    assert pa.BUILD in plan.upcoming
    assert any(row["role"] == "ui" for row in plan.upcoming[pa.BUILD])
    # The PM appears in every later phase but is flagged as already existing.
    assert all(row["already_exists"] for phase in plan.upcoming.values()
               for row in phase if row["role"] == "pm")


# ---------------------------------------------------------------------------
# Separation of duties.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase", sorted(APPROVAL_PHASES))
@pytest.mark.parametrize("role", sorted(BUILD_ROLES | {"pm"}))
def test_neither_a_builder_nor_the_pm_may_approve(role, phase):
    assert can_approve(role, phase) is False


@pytest.mark.parametrize("role", sorted(ASSURANCE_ROLES))
def test_assurance_roles_may_approve(role):
    for phase in APPROVAL_PHASES:
        assert can_approve(role, phase) is True


def test_outside_an_approval_phase_every_role_may_work():
    for role in ("core", "pm", "qa", "analyst"):
        assert can_approve(role, pa.BUILD) is True


def test_the_collapse_tables_cannot_merge_across_the_duty_boundary(monkeypatch):
    monkeypatch.setitem(pa.ROLE_COLLAPSE, pa.SMALL, {"qa": "core"})
    with pytest.raises(ValueError, match="separation of duties"):
        collapse_role("qa", pa.SMALL)


def test_the_collapse_tables_cannot_merge_the_pm_away(monkeypatch):
    monkeypatch.setitem(pa.ROLE_COLLAPSE, pa.SMALL, {"pm": "core"})
    with pytest.raises(ValueError, match="cross-phase"):
        collapse_role("pm", pa.SMALL)


# ---------------------------------------------------------------------------
# Project CRUD + bootstrap.
# ---------------------------------------------------------------------------

def test_project_crud_persists_and_is_idempotent(registry):
    project = registry.create_project("demo", name="Demo", complexity="medium")
    assert project.phase == pa.INTAKE
    assert project.bootstrap_state == "PENDING"
    assert registry.get_project("demo").name == "Demo"
    with pytest.raises(AgentRegistryError, match="already exists"):
        registry.create_project("demo")
    assert registry.update_project("demo", name="Demo 2").name == "Demo 2"
    archived = registry.archive_project("demo")
    assert archived.status == "ARCHIVED" and archived.archived_at
    assert registry.get_project("demo") is not None      # archived, never deleted


def test_bootstrap_creates_only_the_first_phase_team_plus_the_pm(wired):
    projects, _agents, _router, _controller = wired
    result = projects.bootstrap("traffic-camera-ai", description=BIG)

    roles = {row["role"] for row in result["agents"]}
    assert roles == {"pm", "analyst"}, "later phases must not be materialised yet"
    assert result["pm_agent_id"] == "traffic-camera-ai-pm"
    project = result["project"]
    assert project["phase"] == pa.INTAKE
    assert project["bootstrap_state"] == "BOOTSTRAPPED"
    assert project["complexity"] == pa.LARGE
    # And the plan still shows where it is going.
    assert pa.BUILD in project["upcoming"]


def test_bootstrap_binds_the_builtin_skills_for_each_created_agent(wired):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=SMALL_DESC)

    pm_skills = {binding.skill_id for binding in agents.store.agent_skills("demo-pm")}
    assert {"project-planning", "task-decomposition", "phase-gates"} <= pm_skills
    for skill_id in pm_skills:
        skill = agents.store.get_skill(skill_id)
        assert skill.source == "builtin" and skill.body


def test_rerunning_bootstrap_never_duplicates_agents(wired):
    projects, agents, _router, _controller = wired
    first = projects.bootstrap("demo", description=BIG)
    second = projects.bootstrap("demo", description=BIG)

    ids = [agent.id for agent in agents.store.list_agents(project_id="demo")]
    assert len(ids) == len(set(ids))
    assert {row["agent_id"] for row in first["agents"]} == {row["agent_id"] for row in second["agents"]}
    assert all(row["action"] in ("kept", "reactivated") for row in second["agents"])
    assert second["project"]["pm_agent_id"] == first["pm_agent_id"]


def test_a_project_may_opt_out_of_the_pm(wired):
    projects, agents, _router, _controller = wired
    result = projects.bootstrap("nopm", description=SMALL_DESC, policy={"disable_pm": True})
    assert result["pm_agent_id"] is None
    assert all(row["role"] != "pm" for row in result["agents"])


def test_the_wizard_can_deselect_proposed_roles_but_not_the_pm(wired):
    projects, _agents, _router, _controller = wired
    result = projects.bootstrap("demo", description=BIG, roles=[])
    assert {row["role"] for row in result["agents"]} == {"pm"}


def test_a_repo_outside_the_allowed_roots_is_refused(wired, monkeypatch, tmp_path):
    projects, agents, _router, _controller = wired
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    agents.config = type("Cfg", (), {
        "session_lifecycle": type("L", (), {"allowed_cwd_roots": (str(allowed),)})()})()
    result = projects.bootstrap("demo", description=SMALL_DESC, repo_root="/etc")
    assert result["error"] == "REPO_NOT_ALLOWED"


# ---------------------------------------------------------------------------
# Phases: the team changes, the PM does not.
# ---------------------------------------------------------------------------

def test_advancing_a_phase_swaps_the_team_and_keeps_the_pm(wired):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=BIG)

    projects.advance("demo", reason="intake done")          # -> ANALYSIS
    after = {a.id: a for a in agents.store.list_agents(project_id="demo")}
    assert after["demo-pm"].state == AGENT_ACTIVE, "the PM is never made dormant"
    assert after["demo-analyst"].state == AGENT_DORMANT
    assert after["demo-architect"].state == AGENT_ACTIVE

    status = projects.phase_status("demo")
    assert status["phase"] == pa.ANALYSIS
    assert "demo-pm" in status["active_agents"]
    assert "demo-analyst" in status["dormant_agents"]


def test_the_pm_survives_the_whole_pipeline_while_specialists_rotate(wired):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=BIG)
    seen_roles = {a.role for a in agents.store.list_agents(project_id="demo")
                  if a.state == AGENT_ACTIVE}
    for _ in range(len(PHASES) - 1):
        projects.advance("demo")
        live = {a.role for a in agents.store.list_agents(project_id="demo")
                if a.state == AGENT_ACTIVE}
        assert "pm" in live, "the coordinator must persist across every phase"
        seen_roles |= live
    assert {"analyst", "architect", "planner", "qa", "release"} <= seen_roles
    assert agents.store.get_project("demo").pm_agent_id == "demo-pm"


def test_returning_to_an_earlier_phase_wakes_the_original_agent(wired):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.BUILD)
    build_ids = {a.id for a in agents.store.list_agents(project_id="demo")
                 if a.state == AGENT_ACTIVE}
    projects.advance("demo", to_phase=pa.REVIEW)
    projects.advance("demo", to_phase=pa.BUILD, reason="review found a defect")

    woken = {a.id for a in agents.store.list_agents(project_id="demo")
             if a.state == AGENT_ACTIVE}
    assert build_ids <= woken
    all_ids = [a.id for a in agents.store.list_agents(project_id="demo")]
    assert len(all_ids) == len(set(all_ids)), "waking must not create a second agent"


def test_every_transition_records_reason_gate_and_handoff(wired):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", reason="scope agreed",
                     gate_evidence={"doc": "scope.md"},
                     handoff={"artifacts": ["scope.md"], "open_questions": ["which map SDK"]})

    history = agents.store.phase_history("demo")
    latest = history[0]
    assert latest["from_phase"] == pa.INTAKE and latest["to_phase"] == pa.ANALYSIS
    assert latest["reason"] == "scope agreed"
    assert latest["gate_evidence"]["doc"] == "scope.md"
    assert latest["handoff"]["open_questions"] == ["which map SDK"]
    assert latest["active_agent_ids"]


def test_phase_transitions_survive_a_restart_without_duplicating(wired, tmp_path):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo")
    reopened = AgentRegistryStore(agents.store.path)
    fresh = ProjectRuntimeService(AgentService(reopened, queue=projects.queue, skill_roots=()))

    fresh.reconcile_team("demo")

    ids = [a.id for a in reopened.list_agents(project_id="demo")]
    assert len(ids) == len(set(ids))
    assert reopened.get_project("demo").phase == pa.ANALYSIS
    assert reopened.get_project("demo").pm_agent_id == "demo-pm"


def test_archiving_a_project_retires_its_whole_team(wired):
    projects, agents, _router, _controller = wired
    projects.bootstrap("demo", description=BIG)
    result = projects.archive_project("demo")
    assert result["project"]["status"] == "ARCHIVED"
    assert all(a.state == AGENT_RETIRED for a in agents.store.list_agents(project_id="demo"))


def test_advancing_past_the_last_phase_is_refused_clearly(wired):
    projects, _agents, _router, _controller = wired
    projects.bootstrap("demo", description=SMALL_DESC)
    projects.advance("demo", to_phase=pa.OPERATE)
    assert projects.advance("demo")["error"] == "NO_NEXT_PHASE"
