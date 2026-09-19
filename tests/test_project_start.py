"""project_start: Project -> PM -> specialist -> Router -> Session.

Each arrow is an existing component. These tests assert the seams: that the PM
picks by capability rather than at random, that the choice is refused rather
than faked when nobody fits, that the router still does the placing, and that
the bound skills actually reach the agent's prompt.
"""
from __future__ import annotations

import pytest

from terminal_mcp import project_analyzer as pa
from terminal_mcp.agent_registry import AGENT_DORMANT, AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.project_runtime import NEEDS_TEAM_REVIEW, ProjectRuntimeService
from terminal_mcp.queue_engine import MAX_SKILLS_INJECTED, build_dispatch_text
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.task_router import TaskRouter

from tests.test_task_router import FakeController, FakeRecord, RecordingEngine, _row

BIG = "Web app with map, camera streams, AI analysis API, backend, deploy."


@pytest.fixture
def wired(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    registry = AgentRegistryStore(tmp_path / "agents.db")
    controller = FakeController(
        sessions=[_row(f"s{index}") for index in range(4)],
        records=[FakeRecord(node_id="local", session_name=f"s{index}", agent_type="claude",
                            last_known_state="IDLE") for index in range(4)])
    agents = AgentService(registry, queue=queue, skill_roots=())
    engine = RecordingEngine(queue.store)
    router = TaskRouter(queue.store, controller=controller, queue=queue, engine=engine,
                        session_registry=controller.session_registry, config=None,
                        capacity_check=agents.capacity_block_reason)
    agents.router = router
    queue.router = router
    return ProjectRuntimeService(agents), agents, queue, engine


def test_project_start_picks_an_agent_and_the_router_binds_a_session(wired):
    """THE HEADLINE REGRESSION: a task sent to a project, with no agent and no
    session named by the caller, ends up running somewhere."""
    projects, agents, queue, engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.BUILD)

    receipt = projects.project_start("demo", "add the camera list endpoint")

    assert receipt["project_id"] == "demo"
    assert receipt["phase"] == pa.BUILD
    assert receipt["agent_id"].startswith("demo-")
    assert receipt["session"] in {f"s{index}" for index in range(4)}
    assert receipt["dispatched"] is True
    assert receipt["poll"] is False
    task = queue.store.get_task(receipt["task_id"])
    assert task.agent_id == receipt["agent_id"]
    assert task.project_id == "demo" or task.metadata.get("project_id") == "demo"
    assert task.execution_session == receipt["session"]


def test_the_pm_routes_backend_work_to_the_backend_agent(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.BUILD)

    receipt = projects.project_start("demo", "fix the REST API endpoint and the database schema")

    assert receipt["agent_role"] == "backend"
    assert "capability match" in receipt["agent_selection_reason"]


def test_the_pm_routes_ui_work_to_the_frontend_agent(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.BUILD)
    receipt = projects.project_start("demo", "the map page layout breaks on mobile")
    assert receipt["agent_role"] == "ui"


def test_the_pm_does_not_take_implementation_work_itself(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.BUILD)
    receipt = projects.project_start("demo", "implement the backend API")
    assert receipt["agent_role"] != "pm"


def test_an_approval_task_cannot_go_to_the_builder_or_the_pm(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.TEST)

    receipt = projects.project_start("demo", "verify the acceptance criteria", approval=True)

    assert receipt["agent_role"] == "qa"
    rejected = {row["agent_id"]: row.get("rejected") for row in receipt["agent_candidates"]
                if not row["eligible"]}
    assert any("separation of duties" in (reason or "") for reason in rejected.values())


def test_self_approval_is_allowed_only_when_the_project_says_so(wired):
    """With the independent reviewer unavailable, an approval must stop rather
    than fall back to whoever is around -- unless the project said otherwise."""
    projects, agents, _queue, _engine = wired
    projects.bootstrap("tiny", description="A small CLI that renames files.")
    projects.advance("tiny", to_phase=pa.REVIEW)
    # Take the only assurance agent out, leaving the builder and the PM.
    agents.store.update_agent("tiny-qa", state=AGENT_DORMANT)
    agents.store.update_agent("tiny-core", state="ACTIVE")

    blocked = projects.project_start("tiny", "approve this change", approval=True)
    assert blocked["status"] == NEEDS_TEAM_REVIEW

    agents.store.update_project("tiny", policy={"allow_self_approval": True})
    allowed = projects.project_start("tiny", "approve this change", approval=True)
    assert allowed.get("agent_id") == "tiny-core"


def test_no_suitable_agent_returns_needs_team_review_and_picks_no_session(wired):
    projects, agents, queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    for agent in agents.store.list_agents(project_id="demo"):
        agents.store.update_agent(agent.id, state=AGENT_DORMANT)

    receipt = projects.project_start("demo", "do something")

    assert receipt["status"] == NEEDS_TEAM_REVIEW
    assert receipt["agent_id"] is None and receipt["session"] is None
    assert receipt["needs_human"] is True
    assert receipt["candidates"]
    assert "no agent" in receipt["routing_reason"]


def test_an_agent_from_another_project_is_never_chosen(wired):
    projects, agents, _queue, _engine = wired
    projects.bootstrap("alpha", description=BIG)
    projects.bootstrap("beta", description=BIG)

    receipt = projects.project_start("alpha", "do the intake")

    assert receipt["agent_id"].startswith("alpha-")
    assert all(row["agent_id"].startswith("alpha-") for row in receipt["agent_candidates"])


def test_naming_an_agent_from_another_project_is_refused(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("alpha", description=BIG)
    projects.bootstrap("beta", description=BIG)
    result = projects.project_start("alpha", "x", agent_id="beta-pm")
    assert result["error"] == "AGENT_NOT_IN_PROJECT"


def test_a_dormant_agent_is_rejected_with_its_state_as_the_reason(wired):
    projects, agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    agents.store.update_agent("demo-analyst", state=AGENT_DORMANT)
    receipt = projects.project_start("demo", "intake work")
    rejected = {row["agent_id"]: row.get("rejected") for row in receipt["agent_candidates"]}
    assert "DORMANT" in (rejected.get("demo-analyst") or "")


def test_load_lowers_an_agents_own_score(wired):
    """Compared against ITSELF, not against a differently-skilled agent: a busy
    specialist can still be the right choice over an idle generalist, and the
    load term only has to move the needle, not dominate it."""
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.advance("demo", to_phase=pa.BUILD)

    def score_of(agent_id):
        rationale = projects.select_agent("demo", wanted=["backend", "api"])[1]
        return next(row["score"] for row in rationale["candidates"]
                    if row["agent_id"] == agent_id)

    idle_score = score_of("demo-backend")
    projects.project_start("demo", "implement the backend API", agent_id="demo-backend")
    busy_score = score_of("demo-backend")

    assert busy_score < idle_score


def test_an_archived_project_refuses_new_work(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    projects.archive_project("demo")
    assert projects.project_start("demo", "x")["error"] == "PROJECT_ARCHIVED"


def test_project_start_is_idempotent_under_a_request_key(wired):
    projects, _agents, _queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    first = projects.project_start("demo", "work", request_key="k1")
    second = projects.project_start("demo", "work", request_key="k1")
    assert first["task_id"] == second["task_id"]
    assert second["deduplicated"] is True


# ---------------------------------------------------------------------------
# Skill injection.
# ---------------------------------------------------------------------------

def test_bound_skill_bodies_reach_the_dispatched_prompt(wired):
    projects, agents, queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    receipt = projects.project_start("demo", "write down the scope")
    task = queue.store.get_task(receipt["task_id"])

    preamble = agents.skill_preamble(task)
    assert preamble and "Standing instructions" in preamble
    text = build_dispatch_text(task, nonce="n", skills_preamble=preamble)
    assert text.index("Standing instructions") < text.index("write down the scope"), \
        "standing instructions must be read before the request they qualify"
    assert "###TERMINAL_MCP_COMPLETION" in text


def test_only_the_pinned_versions_are_injected(wired):
    projects, agents, queue, _engine = wired
    projects.bootstrap("demo", description="A small CLI that renames files.")
    receipt = projects.project_start("demo", "do the thing")
    task = queue.store.get_task(receipt["task_id"])
    assert task.skill_ids and all("@" in label for label in task.skill_ids)

    # A later version must not retroactively change what this run loaded.
    first_label = task.skill_ids[0]
    skill_id = first_label.split("@")[0]
    agents.store.register_skill(skill_id, version="99", body="COMPLETELY DIFFERENT")
    assert "COMPLETELY DIFFERENT" not in (agents.skill_preamble(task) or "")


def test_skill_injection_is_bounded_by_count(wired):
    projects, agents, queue, _engine = wired
    projects.bootstrap("demo", description=BIG)
    receipt = projects.project_start("demo", "coordinate", agent_id="demo-pm")
    task = queue.store.get_task(receipt["task_id"])
    assert len(task.skill_ids) > MAX_SKILLS_INJECTED, "the PM has six base skills"

    preamble = agents.skill_preamble(task)
    assert preamble.count("(skill ") == MAX_SKILLS_INJECTED
    assert "not loaded for this task" in preamble


def test_a_task_with_no_agent_gets_no_preamble_and_an_unchanged_prompt(wired):
    projects, agents, queue, _engine = wired
    (task_id,) = queue.store.set_tasks("lane-a", [{"prompt": "legacy work"}])
    task = queue.store.get_task(task_id)
    assert agents.skill_preamble(task) is None
    assert build_dispatch_text(task, nonce="n") == build_dispatch_text(
        task, nonce="n", skills_preamble=None)


def test_allow_self_approval_never_promotes_the_pm_into_an_approver(wired):
    """The waiver is about a dev signing off their own work when the team is
    too small to have anyone else. The PM asked for the work to be finished,
    so its sign-off adds nothing at any project size."""
    projects, agents, _queue, _engine = wired
    projects.bootstrap("tiny", description="A small CLI that renames files.")
    projects.advance("tiny", to_phase=pa.REVIEW)
    agents.store.update_agent("tiny-qa", state=AGENT_DORMANT)
    agents.store.update_agent("tiny-core", state=AGENT_DORMANT)
    agents.store.update_project("tiny", policy={"allow_self_approval": True})

    receipt = projects.project_start("tiny", "approve this change", approval=True)

    assert receipt["status"] == NEEDS_TEAM_REVIEW
    rejected = {row["agent_id"]: row.get("rejected") for row in receipt["candidates"]}
    assert "separation of duties" in (rejected.get("tiny-pm") or "")


def test_the_task_carries_the_project_in_its_durable_column_not_only_metadata(wired):
    """LIVE, hp-linux @ a90835e: a project task came back with
    project_id=None. queue_tasks.project_id is what project_service's view,
    the Global Tasks card and every per-project query read; metadata is not."""
    projects, _agents, queue, _engine = wired
    projects.bootstrap("demo", description=BIG)

    receipt = projects.project_start("demo", "write the scope down")
    task = queue.store.get_task(receipt["task_id"])

    assert task.project_id == "demo"
    assert queue.store.list_tasks_for_project("demo")
