"""TMCP-TASK-ROUTER-001 -- profile, matcher, router, rescue.

THE TEST THAT MATTERS MOST is test_queued_task_beside_idle_session_is_rescued:
it reproduces the exact production failure (durable task
f807b3fb3d31438281d2cb4c8c6aea5a, cancelled after sitting QUEUED while a
compatible session was IDLE the whole time) and asserts that the reconcile
now dispatches it. Everything else in this file exists to make sure that fix
did not buy its correctness by dispatching into sessions it should refuse.

Pure in-process: fake controller/registry objects, a tmp_path queue db, no
real tmux session anywhere -- same split queue_store.py's own tests use, and
for the same reason.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

import pytest

from terminal_mcp import session_matcher as sm
from terminal_mcp import task_profile
from terminal_mcp.compact_tools import MAX_START_TICKS, START_UNDERWAY_STATUSES
from terminal_mcp.config import RouterConfig
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import (
    BOUND, COMPLETED, QUEUED, RUNNING, SPAWNED, UNASSIGNED_LANE, UNROUTED,
    WAITING_RUNTIME, QueueStore,
)
from terminal_mcp.session_matcher import SessionCandidate
from terminal_mcp.task_router import (
    ALREADY_BOUND, DEFERRED, NOT_ROUTABLE, ROUTED, SPAWNED_RUNTIME, TaskRouter,
)


# ---------------------------------------------------------------------------
# Fakes. Deliberately tiny: each one implements only the handful of methods
# the router actually calls, so a test failure points at the router rather
# than at a mock framework.
# ---------------------------------------------------------------------------

@dataclass
class FakeNode:
    id: str
    display_name: str = "node"
    status: str = "online"
    draining: bool = False
    capacity_status: str = "healthy"
    agent_types: tuple = ("claude", "codex", "shell")


@dataclass
class FakeRecord:
    node_id: str
    session_name: str
    agent_type: str | None = "claude"
    cwd: str | None = None
    repo_root: str | None = None
    git_remote: str | None = None
    git_branch: str | None = None
    worktree_path: str | None = None
    last_known_state: str | None = "IDLE"
    status: str = "ACTIVE"
    tags: tuple = ()
    binding_names: tuple = ()


class FakeRegistry:
    def __init__(self, records):
        self._records = list(records)

    def list(self):
        return list(self._records)


class FakeController:
    """Just enough controller to route against."""

    def __init__(self, *, sessions, nodes=None, records=None, statuses=None,
                 local_node_id="local"):
        self._sessions = sessions
        self._nodes = nodes or [FakeNode(id=local_node_id)]
        self.local_node_id = local_node_id
        self.session_registry = FakeRegistry(records or [])
        self._statuses = statuses or {}
        self.created: list[tuple] = []
        self.create_result: dict | None = None

    def terminal_list_sessions(self):
        return {"sessions": list(self._sessions), "unreachable_nodes": []}

    def list_nodes(self):
        return list(self._nodes)

    def terminal_status(self, session):
        return self._statuses.get(session, {"session": session, "state": "IDLE"})

    def terminal_create_session(self, name, agent_type="shell", cwd=None, **kwargs):
        self.created.append((name, agent_type, cwd))
        if self.create_result is not None:
            return self.create_result
        self._sessions.append({"name": name, "node_id": self.local_node_id,
                               "effective_input": True})
        return {"session": name, "node_id": self.local_node_id}


class _Result:
    def __init__(self, action, detail=None):
        self.action = action
        self.detail = detail


class RecordingEngine:
    """Stands in for QueueEngine, INCLUDING its one-transition-per-tick rule.

    The first cut of this fake just recorded the lane and returned, which let
    the live BOUND-but-QUEUED bug through the whole suite: the router called
    tick() once, nothing moved, and every test still passed because the fake
    never had a state machine to fail to advance. It now walks the real edges,
    one per tick, so "did the router actually get this task running" is a
    question these tests can answer.
    """

    def __init__(self, store=None):
        self.store = store
        self.ticks: list[str] = []
        self._path = ["PRECHECK", "READY", "DISPATCHING", "RUNNING"]

    def tick(self, session):
        self.ticks.append(session)
        if self.store is None:
            return _Result("NO_STORE")
        lane = self.store.lane_status(session)
        pending = [row for row in lane["tasks"]
                   if row["status"] in ("QUEUED", *self._path[:-1])]
        if not pending:
            return _Result("IDLE")
        task = pending[0]
        nxt = self._path[0] if task["status"] == "QUEUED" else \
            self._path[self._path.index(task["status"]) + 1]
        self.store.transition_task(task["id"], nxt, event_type="TEST_TICK")
        return _Result(nxt, detail=None)


class StubbornEngine:
    """An engine that accepts the tick and refuses to move the task.

    The real one does this whenever the admission governor declines, a lane is
    paused, or a dependency is unmet -- and that is the state the live failure
    was reported in."""

    def __init__(self, detail="admission refused: claude concurrency limit reached"):
        self.ticks: list[str] = []
        self.detail = detail

    def tick(self, session):
        self.ticks.append(session)
        return _Result("QUEUED", detail=self.detail)


def _row(name, *, node_id="local", input_ok=True, stale=False):
    return {"name": name, "node_id": node_id, "node_name": node_id,
            "effective_input": input_ok, "stale_identity_pin": stale}


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def queue(store):
    return QueueService(store)


def _router(store, queue, controller, *, engine=None, config=None):
    return TaskRouter(store, controller=controller, queue=queue,
                      engine=engine if engine is not None else RecordingEngine(store),
                      session_registry=controller.session_registry,
                      config=config or _config())


def _config(**overrides):
    return type("Cfg", (), {"router": RouterConfig(**overrides)})()


def _repo_worktree(tmp_path, name):
    path = tmp_path / name
    path.mkdir()
    return str(path)


# ---------------------------------------------------------------------------
# TaskProfile -- metadata first, prompt heuristics only as a fallback.
# ---------------------------------------------------------------------------

def test_profile_prefers_explicit_metadata_over_prompt_heuristics():
    profile = task_profile.analyze(
        prompt="fix the broken login using claude on branch feat/other",
        metadata={"project": "novaretail", "repo": "git@github.com:acme/web.git",
                  "task_type": "feature", "runtime": "codex", "branch": "feat/real",
                  "required_skills": ["python", "sql"]})
    assert profile.project == "novaretail"
    assert profile.task_type == "feature"          # metadata wins over "fix"
    assert profile.preferred_runtime == "codex"    # metadata wins over "claude"
    assert profile.branch == "feat/real"
    assert profile.required_skills == ("python", "sql")
    assert profile.runtime_required is True
    assert profile.evidence["task_type"] == "metadata"


def test_profile_falls_back_to_prompt_when_metadata_is_silent():
    profile = task_profile.analyze(prompt="please fix the crash in the parser")
    assert profile.task_type == "bugfix"
    assert profile.evidence["task_type"] == "prompt"
    # A prompt hint is never a hard constraint.
    assert profile.runtime_required is False


def test_profile_never_invents_a_workspace_from_a_nonexistent_path():
    profile = task_profile.analyze(prompt="update /definitely/not/a/real/path/here now")
    assert profile.workspace is None


def test_profile_reads_a_real_path_out_of_the_prompt(tmp_path):
    real = _repo_worktree(tmp_path, "somewhere")
    profile = task_profile.analyze(prompt=f"run the tests in {real} please")
    assert profile.workspace == real
    assert profile.evidence["workspace"] == "prompt"


def test_profile_delegates_risk_to_the_classifier_and_does_not_redefine_it():
    profile = task_profile.analyze(prompt="rotate the api_key and update the auth check")
    assert "credentials/secrets" in profile.risk_flags
    assert profile.requires_approval is True


def test_profile_normalises_a_single_string_skill_into_one_item_not_characters():
    profile = task_profile.analyze(prompt="x", metadata={"required_skills": "lint"})
    assert profile.required_skills == ("lint",)


# ---------------------------------------------------------------------------
# SessionMatcher -- hard rejects.
# ---------------------------------------------------------------------------

def _profile(**kwargs):
    kwargs.setdefault("task_id", "t1")
    return task_profile.TaskProfile(**kwargs)


def test_offline_node_is_rejected_whatever_else_is_true():
    candidate = SessionCandidate(session="s", node_online=False, state="IDLE")
    assert sm.hard_reject(_profile(), candidate) == sm.NODE_OFFLINE


def test_waiting_input_session_is_never_chosen():
    candidate = SessionCandidate(session="s", state="WAITING_INPUT")
    assert sm.hard_reject(_profile(), candidate) == sm.WAITING_INPUT


def test_deleted_worktree_is_rejected():
    candidate = SessionCandidate(session="s", state="IDLE", worktree_exists=False,
                                 cwd="/gone")
    assert sm.hard_reject(_profile(), candidate) == sm.WORKTREE_MISSING


def test_unknown_worktree_is_not_a_rejection():
    """Unknown is not No. A remote session's cwd cannot be checked from here,
    and rejecting on that would make every remote session ineligible."""
    candidate = SessionCandidate(session="s", state="IDLE", worktree_exists=None)
    assert sm.hard_reject(_profile(), candidate) is None


def test_known_different_repo_is_rejected_but_unknown_repo_is_only_penalised():
    profile = _profile(repo="/repos/alpha")
    wrong = SessionCandidate(session="s", state="IDLE", repo="/repos/beta")
    assert sm.hard_reject(profile, wrong) == sm.REPO_MISMATCH

    unknown = SessionCandidate(session="s", state="IDLE", repo=None)
    assert sm.hard_reject(profile, unknown) is None
    score, reasons = sm.score(profile, unknown)
    assert score == sm.PENALTY_REPO_UNKNOWN + sm.SCORE_IDLE
    assert any("repo is unknown" in reason for reason in reasons)


def test_busy_session_is_rejected_and_a_claimed_one_too():
    assert sm.hard_reject(_profile(), SessionCandidate(session="s", state="RUNNING")) == sm.SESSION_BUSY
    assert sm.hard_reject(_profile(), SessionCandidate(session="s", state="IDLE",
                                                       active_tasks=1)) == sm.SESSION_BUSY
    claimed = SessionCandidate(session="s", state="IDLE", claimed_by_task="other")
    assert sm.hard_reject(_profile(), claimed) == sm.SESSION_CLAIMED


def test_runtime_mismatch_rejects_only_when_the_runtime_was_a_constraint():
    hard = _profile(preferred_runtime="codex", runtime_required=True)
    soft = _profile(preferred_runtime="codex", runtime_required=False)
    candidate = SessionCandidate(session="s", state="IDLE", runtime="claude")
    assert sm.hard_reject(hard, candidate) == sm.RUNTIME_MISMATCH
    assert sm.hard_reject(soft, candidate) is None


def test_input_denied_and_stale_identity_pin_are_rejections():
    assert sm.hard_reject(_profile(), SessionCandidate(
        session="s", state="IDLE", input_allowed=False)) == sm.INPUT_NOT_PERMITTED
    assert sm.hard_reject(_profile(), SessionCandidate(
        session="s", state="IDLE", stale_identity_pin=True)) == sm.STALE_IDENTITY


# ---------------------------------------------------------------------------
# SessionMatcher -- scoring.
# ---------------------------------------------------------------------------

def test_context_at_or_above_85_percent_is_penalised_and_below_70_rewarded():
    profile = _profile()
    tight, _ = sm.score(profile, SessionCandidate(session="s", state="IDLE", context_percent=85.0))
    roomy, _ = sm.score(profile, SessionCandidate(session="s", state="IDLE", context_percent=40.0))
    assert tight == sm.SCORE_IDLE + sm.PENALTY_CONTEXT_TIGHT
    assert roomy == sm.SCORE_IDLE + sm.SCORE_CONTEXT_ROOMY
    assert roomy > tight


def test_agent_binding_and_repo_affinity_outrank_a_bare_idle_session():
    profile = _profile(repo="/repos/alpha", agent_id="agent-7", project="novaretail")
    best = SessionCandidate(session="best", state="IDLE", repo="/repos/alpha",
                            agent_id="agent-7", project="novaretail")
    plain = SessionCandidate(session="plain", state="IDLE")
    result = sm.rank(profile, [plain, best])
    assert result.chosen.candidate.session == "best"
    assert result.chosen.score > sm.evaluate(profile, plain).score


def test_dirty_work_on_a_different_branch_is_penalised():
    profile = _profile(branch="feat/target")
    candidate = SessionCandidate(session="s", state="IDLE", branch="feat/other", dirty=True)
    score, reasons = sm.score(profile, candidate)
    assert score == sm.SCORE_IDLE + sm.PENALTY_DIRTY_BRANCH
    assert any("uncommitted work" in reason for reason in reasons)


def test_rank_reports_why_every_rejected_candidate_lost():
    profile = _profile(repo="/repos/alpha")
    result = sm.rank(profile, [
        SessionCandidate(session="offline", node_online=False),
        SessionCandidate(session="waiting", state="WAITING_INPUT"),
        SessionCandidate(session="wrongrepo", state="IDLE", repo="/repos/beta"),
    ])
    assert result.chosen is None
    reasons = {row["session"]: row["rejected"] for row in result.rejections()}
    assert reasons == {"offline": sm.NODE_OFFLINE, "waiting": sm.WAITING_INPUT,
                       "wrongrepo": sm.REPO_MISMATCH}
    assert all(row["rejected_detail"] for row in result.rejections())


def test_the_live_probe_overrides_stale_registry_state():
    """A session the registry still thinks is idle, which has since started
    waiting for input, must not be chosen."""
    profile = _profile()
    stale = SessionCandidate(session="s", state="IDLE")

    def probe(candidate):
        return replace(candidate, state="WAITING_INPUT", state_probed=True)

    result = sm.rank(profile, [stale], probe=probe)
    assert result.chosen is None
    assert result.rejections()[0]["rejected"] == sm.WAITING_INPUT


# ---------------------------------------------------------------------------
# Router -- dispatch semantics.
# ---------------------------------------------------------------------------

def test_route_start_without_a_target_binds_an_idle_compatible_session(store, queue, tmp_path):
    worktree = _repo_worktree(tmp_path, "alpha")
    controller = FakeController(
        sessions=[_row("agent-a")],
        records=[FakeRecord(node_id="local", session_name="agent-a", cwd=worktree,
                            repo_root=worktree, git_remote=None)])
    engine = RecordingEngine(store)
    router = _router(store, queue, controller, engine=engine)

    receipt = router.route_start("do the work", metadata={"repo": worktree})

    assert receipt["status"] == "TASK_STARTED"
    assert receipt["session"] == "agent-a"
    assert receipt["routing_state"] == BOUND
    assert receipt["dispatched"] is True
    assert receipt["poll"] is False
    assert "same repo" in receipt["routing_reason"]
    assert set(engine.ticks) == {"agent-a"}

    task = store.get_task(receipt["task_id"])
    assert task.execution_session == "agent-a"
    assert task.routing_state == BOUND
    assert task.routing_evidence["reason"] == receipt["routing_reason"]
    # Actually under way, not merely bound -- see the live BOUND+QUEUED failure.
    assert task.status not in (QUEUED, "PRECHECK")
    assert receipt["task_state"] == task.status


def test_route_start_with_an_explicit_target_is_hard_affinity(store, queue, tmp_path):
    """A named session is never silently rerouted, even when the router would
    plainly prefer somewhere else."""
    controller = FakeController(
        sessions=[_row("preferred"), _row("named")],
        records=[FakeRecord(node_id="local", session_name="preferred"),
                 FakeRecord(node_id="local", session_name="named",
                            last_known_state="IDLE")])
    engine = RecordingEngine(store)
    router = _router(store, queue, controller, engine=engine)

    receipt = router.route_start("work", target="named")

    assert receipt["session"] == "named"
    assert receipt["explicit_target"] is True
    assert set(engine.ticks) == {"named"}
    task = store.get_task(receipt["task_id"])
    assert task.execution_session == "named"
    assert task.metadata["pinned_session"] == "named"

    # And a later route attempt must leave it exactly where the caller put it.
    outcome = router.route_task(receipt["task_id"])
    assert outcome.outcome == ALREADY_BOUND
    assert store.get_task(receipt["task_id"]).execution_session == "named"


def test_a_pinned_task_is_never_rerouted_even_after_its_binding_is_released(store, queue):
    controller = FakeController(sessions=[_row("elsewhere")],
                                records=[FakeRecord(node_id="local", session_name="elsewhere")])
    router = _router(store, queue, controller)
    receipt = router.route_start("work", target="named")
    store.release_execution_binding(receipt["task_id"], reason="test")

    outcome = router.route_task(receipt["task_id"])

    assert outcome.outcome == NOT_ROUTABLE
    assert "pinned" in outcome.reason


def test_no_eligible_session_defers_with_a_per_candidate_explanation(store, queue):
    controller = FakeController(
        sessions=[_row("busy"), _row("waiting"), _row("offline", node_id="other")],
        nodes=[FakeNode(id="local"), FakeNode(id="other", status="offline")],
        records=[FakeRecord(node_id="local", session_name="busy", last_known_state="RUNNING"),
                 FakeRecord(node_id="local", session_name="waiting", last_known_state="WAITING_INPUT"),
                 FakeRecord(node_id="other", session_name="offline")])
    router = _router(store, queue, controller)

    receipt = router.route_start("work")

    assert receipt["status"] == "TASK_ACCEPTED"
    assert receipt["routing_state"] == WAITING_RUNTIME
    assert receipt["session"] is None
    assert receipt["poll"] is False
    assert "no eligible session among 3 candidates" in receipt["routing_reason"]
    rejected = {row["session"]: row["rejected"] for row in receipt["rejected_candidates"]}
    assert rejected == {"busy": sm.SESSION_BUSY, "waiting": sm.WAITING_INPUT,
                        "offline": sm.NODE_OFFLINE}

    task = store.get_task(receipt["task_id"])
    assert task.routing_state == WAITING_RUNTIME
    assert task.status == QUEUED       # durable, not failed
    assert task.routing_evidence["rejected"]


def test_a_deleted_worktree_session_is_refused_even_though_it_is_idle(store, queue, tmp_path):
    gone = str(tmp_path / "deleted-worktree")   # never created
    controller = FakeController(
        sessions=[_row("stale")],
        records=[FakeRecord(node_id="local", session_name="stale", cwd=gone,
                            worktree_path=gone)])
    router = _router(store, queue, controller)

    receipt = router.route_start("work")

    assert receipt["routing_state"] == WAITING_RUNTIME
    assert receipt["rejected_candidates"][0]["rejected"] == sm.WORKTREE_MISSING


def test_a_wrong_repo_session_is_refused_and_a_right_repo_one_chosen(store, queue, tmp_path):
    alpha = _repo_worktree(tmp_path, "alpha")
    beta = _repo_worktree(tmp_path, "beta")
    controller = FakeController(
        sessions=[_row("on-beta"), _row("on-alpha")],
        records=[FakeRecord(node_id="local", session_name="on-beta", cwd=beta, repo_root=beta),
                 FakeRecord(node_id="local", session_name="on-alpha", cwd=alpha, repo_root=alpha)])
    router = _router(store, queue, controller)

    receipt = router.route_start("work", metadata={"repo": alpha})

    assert receipt["session"] == "on-alpha"


def test_a_busy_session_makes_the_router_choose_the_other_one(store, queue):
    controller = FakeController(
        sessions=[_row("busy"), _row("free")],
        records=[FakeRecord(node_id="local", session_name="busy", last_known_state="RUNNING"),
                 FakeRecord(node_id="local", session_name="free", last_known_state="IDLE")])
    router = _router(store, queue, controller)
    assert router.route_start("work")["session"] == "free"


# ---------------------------------------------------------------------------
# Concurrency.
# ---------------------------------------------------------------------------

def test_two_tasks_cannot_both_claim_the_one_idle_session(store, queue):
    controller = FakeController(sessions=[_row("only")],
                                records=[FakeRecord(node_id="local", session_name="only")])
    router = _router(store, queue, controller)

    first = router.route_start("task one")
    second = router.route_start("task two")

    assert first["session"] == "only"
    assert second["session"] is None
    assert second["routing_state"] == WAITING_RUNTIME
    holders = store.tasks_bound_to_session("only")
    assert [task.id for task in holders] == [first["task_id"]]


def test_binding_the_same_task_to_the_same_session_twice_is_idempotent(store, queue):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "p"}])
    first = store.bind_task_to_session(task_id, "lane-a", evidence={"reason": "r"})
    second = store.bind_task_to_session(task_id, "lane-a", evidence={"reason": "r"})
    assert first["rebound"] is True
    assert second["rebound"] is False
    assert "error" not in second


def test_a_second_session_cannot_be_bound_to_an_already_bound_task(store):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "p"}])
    store.bind_task_to_session(task_id, "lane-a", evidence={})
    refused = store.bind_task_to_session(task_id, "lane-b", evidence={})
    assert refused["error"] == "TASK_ALREADY_BOUND"


def test_a_completed_task_releases_its_session_for_the_next_one(store, queue):
    controller = FakeController(sessions=[_row("only")],
                                records=[FakeRecord(node_id="local", session_name="only")])
    router = _router(store, queue, controller)
    first = router.route_start("one")
    for status in ("RUNNING", "VERIFYING", COMPLETED):
        store.transition_task(first["task_id"], status, event_type="TEST")

    second = router.route_start("two")

    assert second["session"] == "only"


def test_concurrent_routes_of_the_same_task_produce_exactly_one_binding(store, queue):
    controller = FakeController(sessions=[_row("only")],
                                records=[FakeRecord(node_id="local", session_name="only")])
    router = _router(store, queue, controller)
    created = queue.create_task("t", "p")
    task_id = created["task_id"]

    outcomes = [router.route_task(task_id) for _ in range(3)]

    assert outcomes[0].outcome == ROUTED
    assert all(outcome.outcome == ALREADY_BOUND for outcome in outcomes[1:])
    assert len(store.tasks_bound_to_session("only")) == 1


# ---------------------------------------------------------------------------
# QUEUE RESCUE -- the production bug, reproduced and fixed.
# ---------------------------------------------------------------------------

def test_queued_task_beside_idle_session_is_rescued(store, queue):
    """THE REGRESSION TEST FOR THE REPORTED PRODUCTION FAILURE.

    A durable task is created with no session (the unassigned backlog) while a
    compatible session sits IDLE. Before this feature that task stayed QUEUED
    forever -- no lane opted into auto-dispatch, so nothing ever looked at it,
    and it was eventually cancelled by hand. The reconcile must now find it,
    bind it and dispatch it with no operator action at all.
    """
    controller = FakeController(sessions=[_row("idle-agent")],
                                records=[FakeRecord(node_id="local", session_name="idle-agent",
                                                    last_known_state="IDLE")])
    engine = RecordingEngine(store)
    router = _router(store, queue, controller, engine=engine)

    created = queue.create_task("stuck", "work that went nowhere")
    task_id = created["task_id"]
    assert store.get_task(task_id).session == UNASSIGNED_LANE
    assert store.get_task(task_id).status == QUEUED
    assert store.get_task(task_id).routing_state is None      # never routed: the bug

    report = router.rescue_once()

    assert report["routed"] == 1
    task = store.get_task(task_id)
    assert task.execution_session == "idle-agent"
    assert task.routing_state == BOUND
    assert set(engine.ticks) == {"idle-agent"}
    # The bug was "bound but still QUEUED". Binding is not the assertion.
    assert task.status not in (QUEUED, "PRECHECK")


def test_rescue_retries_a_deferred_task_once_a_session_frees_up(store, queue):
    controller = FakeController(
        sessions=[_row("s1")],
        records=[FakeRecord(node_id="local", session_name="s1", last_known_state="RUNNING")])
    router = _router(store, queue, controller)
    receipt = router.route_start("work")
    assert receipt["routing_state"] == WAITING_RUNTIME

    controller.session_registry._records[0].last_known_state = "IDLE"
    report = router.on_session_idle("s1")

    assert report["routed"] == 1
    assert store.get_task(receipt["task_id"]).execution_session == "s1"


def test_rescue_routes_only_the_head_of_each_serial_lane(store, queue):
    """Two tasks stacked in one lane are not two tasks waiting for a runtime:
    the second is waiting for the first. Routing both would double-book."""
    controller = FakeController(sessions=[_row("a"), _row("b")],
                                records=[FakeRecord(node_id="local", session_name="a"),
                                         FakeRecord(node_id="local", session_name="b")])
    router = _router(store, queue, controller)
    owned = {store.ROUTER_OWNED_METADATA_KEY: True}
    store.set_tasks("lane-x", [{"prompt": "first", "metadata": owned},
                               {"prompt": "second", "metadata": owned}])

    report = router.rescue_once()

    assert report["scanned"] == 1
    assert report["routed"] == 1


def test_rescue_is_restart_safe_and_reaches_the_same_answer_from_disk(store, queue, tmp_path):
    controller = FakeController(sessions=[_row("idle-agent")],
                                records=[FakeRecord(node_id="local", session_name="idle-agent")])
    queue.create_task("stuck", "work")
    # A brand-new store over the SAME file is exactly what a restart produces.
    reopened = QueueStore(store.path)
    router = TaskRouter(reopened, controller=controller, queue=QueueService(reopened),
                        engine=RecordingEngine(reopened),
                        session_registry=controller.session_registry, config=_config())

    assert router.rescue_once()["routed"] == 1


def test_rescue_does_nothing_when_disabled_by_policy(store, queue):
    controller = FakeController(sessions=[_row("idle")],
                                records=[FakeRecord(node_id="local", session_name="idle")])
    router = _router(store, queue, controller, config=_config(rescue_enabled=False))
    queue.create_task("t", "p")
    report = router.rescue_once()
    assert report == {"enabled": False, "routed": 0, "deferred": 0, "results": []}


def test_one_bad_task_does_not_wedge_the_whole_sweep(store, queue, monkeypatch):
    controller = FakeController(sessions=[_row("s1"), _row("s2")],
                                records=[FakeRecord(node_id="local", session_name="s1"),
                                         FakeRecord(node_id="local", session_name="s2")])
    router = _router(store, queue, controller)
    first = queue.create_task("bad", "p")["task_id"]
    queue.create_task("good", "p")

    original = router.route_task

    def explode(task_id, **kwargs):
        if task_id == first:
            raise RuntimeError("boom")
        return original(task_id, **kwargs)

    monkeypatch.setattr(router, "route_task", explode)
    report = router.rescue_once()

    assert report["routed"] == 1
    assert any(row["outcome"] == "ERROR" for row in report["results"])


# ---------------------------------------------------------------------------
# Spawning.
# ---------------------------------------------------------------------------

def test_spawn_is_off_by_default_and_the_task_defers_instead(store, queue):
    controller = FakeController(sessions=[], records=[])
    router = _router(store, queue, controller)
    receipt = router.route_start("work")
    assert receipt["routing_state"] == WAITING_RUNTIME
    assert "spawning" in receipt["routing_reason"]
    assert controller.created == []


def test_spawn_creates_one_compatible_runtime_when_enabled(store, queue):
    controller = FakeController(sessions=[], records=[])
    router = _router(store, queue, controller, config=_config(spawn_enabled=True))

    receipt = router.route_start("work", metadata={"runtime": "codex"})

    assert receipt["routing_state"] == SPAWNED
    assert len(controller.created) == 1
    name, agent_type, _cwd = controller.created[0]
    assert agent_type == "codex"
    assert store.get_task(receipt["task_id"]).execution_session == name


def test_spawn_never_creates_a_second_session_for_the_same_task(store, queue):
    controller = FakeController(sessions=[], records=[])
    router = _router(store, queue, controller, config=_config(spawn_enabled=True))
    created = queue.create_task("t", "p")

    router.route_task(created["task_id"])
    store.release_execution_binding(created["task_id"], reason="test")
    controller.create_result = {"error": "SESSION_ALREADY_EXISTS", "node_id": "local"}
    router.route_task(created["task_id"])

    names = {name for name, _type, _cwd in controller.created}
    assert len(names) == 1


def test_spawn_respects_the_configured_ceiling(store, queue):
    existing = [_row(f"tmcp-router-{index:012d}") for index in range(4)]
    controller = FakeController(
        sessions=existing,
        records=[FakeRecord(node_id="local", session_name=row["name"],
                            last_known_state="RUNNING") for row in existing])
    router = _router(store, queue, controller,
                     config=_config(spawn_enabled=True, max_spawned_sessions=4))

    receipt = router.route_start("work")

    assert receipt["routing_state"] == WAITING_RUNTIME
    assert controller.created == []


# ---------------------------------------------------------------------------
# Stale-session cleanup -- report only.
# ---------------------------------------------------------------------------

def test_cleanup_report_proposes_only_idle_taskless_sessions_with_real_evidence(store, queue, tmp_path):
    from terminal_mcp.stale_sessions import cleanup_candidates

    gone = str(tmp_path / "gone")
    alive = _repo_worktree(tmp_path, "alive")
    controller = FakeController(
        sessions=[_row("dead"), _row("alive"), _row("busy-dead"), _row("waiting-dead")],
        records=[
            FakeRecord(node_id="local", session_name="dead", cwd=gone, worktree_path=gone),
            FakeRecord(node_id="local", session_name="alive", cwd=alive, worktree_path=alive),
            FakeRecord(node_id="local", session_name="busy-dead", cwd=gone,
                       worktree_path=gone, last_known_state="RUNNING"),
            FakeRecord(node_id="local", session_name="waiting-dead", cwd=gone,
                       worktree_path=gone, last_known_state="WAITING_INPUT"),
        ])
    router = _router(store, queue, controller)

    report = cleanup_candidates(router, config=None)

    assert [row["session"] for row in report["candidates"]] == ["dead"]
    assert report["report_only"] is True
    excluded = {row["session"]: row["reason"] for row in report["excluded"]}
    assert excluded["busy-dead"] == "BUSY"
    assert excluded["waiting-dead"] == "WAITING_INPUT"


def test_cleanup_report_never_proposes_a_session_holding_a_task(store, queue, tmp_path):
    from terminal_mcp.stale_sessions import cleanup_candidates

    gone = str(tmp_path / "gone")
    controller = FakeController(
        sessions=[_row("dead-but-working")],
        records=[FakeRecord(node_id="local", session_name="dead-but-working",
                            cwd=gone, worktree_path=gone)])
    store.set_tasks("dead-but-working", [{"prompt": "still queued here"}])
    router = _router(store, queue, controller)

    report = cleanup_candidates(router, config=None)

    assert report["candidates"] == []
    assert report["excluded"][0]["reason"] == "HAS_TASKS"


def test_cleanup_report_never_proposes_a_protected_admin_session(store, queue, tmp_path):
    from terminal_mcp.stale_sessions import cleanup_candidates

    gone = str(tmp_path / "gone")
    controller = FakeController(
        sessions=[_row("window")],
        records=[FakeRecord(node_id="local", session_name="window", cwd=gone,
                            worktree_path=gone)])
    router = _router(store, queue, controller)

    report = cleanup_candidates(router, config=None)

    assert report["candidates"] == []
    assert report["excluded"][0]["reason"] == "PROTECTED"


# ---------------------------------------------------------------------------
# terminal_turn mapping and the queue-loop rescue step.
# ---------------------------------------------------------------------------

def test_turn_route_start_maps_to_the_router_and_needs_no_target():
    from terminal_mcp.compact_tools import CompactTerminalTools

    calls: list[dict] = []

    def route_start(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return {"status": "TASK_STARTED", "task_id": "t1", "session": "agent-a",
                "node_id": "local", "routing_state": BOUND, "routing_outcome": ROUTED,
                "routing_reason": "score 60: +20 session is IDLE", "score": 60,
                "task_state": "RUNNING", "dispatched": True, "poll": False}

    tools = CompactTerminalTools(terminal=None, controller=None,
                                 handlers={"route_start": route_start})

    result = tools.turn(action="route_start", text="do the work")

    assert result["status"] == "TASK_STARTED"
    assert result["task_id"] == "t1"
    assert result["session"] == "agent-a"
    assert result["routing_state"] == BOUND
    assert result["routing_reason"].startswith("score 60")
    assert result["poll"] is False
    assert calls[0]["target"] is None


@pytest.mark.parametrize("alias", ["start_auto", "auto", "route"])
def test_turn_route_start_aliases_resolve(alias):
    from terminal_mcp.compact_tools import CompactTerminalTools

    tools = CompactTerminalTools(terminal=None, controller=None,
                                 handlers={"route_start": lambda prompt, **kw: {"status": "OK"}})
    assert tools.turn(action=alias, text="x")["action"] == "route_start"


def test_turn_route_start_requires_text_but_not_target():
    from terminal_mcp.compact_tools import CompactTerminalTools

    tools = CompactTerminalTools(terminal=None, controller=None,
                                 handlers={"route_start": lambda prompt, **kw: {"status": "OK"}})
    assert tools.turn(action="route_start")["error"] == "TEXT_REQUIRED"


def test_turn_route_start_forwards_an_explicit_target_unchanged():
    from terminal_mcp.compact_tools import CompactTerminalTools

    seen: dict = {}

    def route_start(prompt, **kwargs):
        seen.update(kwargs)
        return {"status": "TASK_STARTED"}

    tools = CompactTerminalTools(terminal=None, controller=None,
                                 handlers={"route_start": route_start})
    tools.turn(action="route_start", text="x", target="named")
    assert seen["target"] == "named"


def test_turn_start_still_requires_a_target_and_never_routes():
    """The legacy action must keep its exact contract: naming no session is an
    error, not an invitation to pick one."""
    from terminal_mcp.compact_tools import CompactTerminalTools

    tools = CompactTerminalTools(terminal=None, controller=None, handlers={})
    assert tools.turn(action="start", text="x")["error"] == "TARGET_REQUIRED"


def test_queue_loop_runs_the_rescue_sweep_as_a_step_of_its_cycle(store):
    from terminal_mcp.queue_loop import QueueLoop

    class Router:
        def __init__(self):
            self.calls = 0

        def rescue_once(self):
            self.calls += 1
            return {"enabled": True, "routed": self.calls, "deferred": 0, "results": []}

    class Engine:
        def __init__(self, store):
            self.store = store

        def tick(self, session):  # pragma: no cover -- no opted-in lane here
            raise AssertionError("no lane should be ticked in this test")

    router = Router()
    loop = QueueLoop(Engine(store), task_router=router, rescue_interval_seconds=3600)

    loop.run_one_cycle()
    loop.run_one_cycle()      # inside the interval: must not sweep again

    assert router.calls == 1
    assert loop.status()["last_rescue"]["routed"] == 1

    loop._rescue_now = True   # a lane reported IDLE
    loop.run_one_cycle()
    assert router.calls == 2


def test_queue_loop_survives_a_failing_rescue(store):
    from terminal_mcp.queue_loop import QueueLoop

    class Engine:
        def __init__(self, store):
            self.store = store

    class Broken:
        def rescue_once(self):
            raise RuntimeError("boom")

    loop = QueueLoop(Engine(store), task_router=Broken())
    loop.run_one_cycle()
    assert "boom" in loop.status()["last_rescue"]["error"]


def test_rescue_never_overrides_an_operators_per_lane_auto_dispatch_opt_out(store, queue):
    """The safety gate this project built on purpose must survive the router.

    A task a caller deliberately put in a named lane whose auto-dispatch is OFF
    stays exactly where it was put. Re-homing it would walk straight through
    the opt-out: the operator said "nothing autonomous in this lane" and the
    router would answer "fine, a different lane then"."""
    controller = FakeController(sessions=[_row("free")],
                                records=[FakeRecord(node_id="local", session_name="free")])
    router = _router(store, queue, controller)
    (task_id,) = store.set_tasks("hands-off-lane", [{"prompt": "operator placed this here"}])

    report = router.rescue_once()

    assert report["scanned"] == 0
    assert report["routed"] == 0
    task = store.get_task(task_id)
    assert task.session == "hands-off-lane"
    assert task.execution_session is None


def test_a_task_whose_session_vanished_is_rescued_even_from_a_named_lane(store, queue):
    """WAITING_SESSION is the one case where honouring the original choice is
    impossible -- the session is gone -- so re-routing takes nothing away."""
    from terminal_mcp.queue_store import WAITING_SESSION

    controller = FakeController(sessions=[_row("survivor")],
                                records=[FakeRecord(node_id="local", session_name="survivor")])
    router = _router(store, queue, controller)
    (task_id,) = store.set_tasks("dead-lane", [{"prompt": "its session is gone"}])
    store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    store.mark_waiting_session(task_id, reason="session vanished")

    report = router.rescue_once()

    assert report["routed"] == 1
    rehomed = store.get_task(task_id)
    assert rehomed.execution_session == "survivor"
    # WAITING_SESSION's only outgoing edge is QUEUED, and the rebind has to
    # take it or the task is bound to a live runtime that can never claim it.
    assert rehomed.status not in (WAITING_SESSION, QUEUED)


# ---------------------------------------------------------------------------
# LIVE FAILURE, hp-linux @ 5cecd87 -- BOUND + QUEUED + dispatched=true.
#
# route_start chose terminal-mcp-claude-audit (score 55) and returned
# dispatched=true / routing_state=BOUND, while the task stayed status=QUEUED
# at queue_position=2 with started_at=null and the session stayed IDLE. Three
# separate defects lined up to produce it, and each gets its own test.
# ---------------------------------------------------------------------------

def test_a_session_whose_lane_already_has_queued_work_is_rejected(store, queue):
    """DEFECT 1. This was a -5 score penalty, so a backlogged lane still won.
    The task was appended BEHIND the existing queue and could not start."""
    controller = FakeController(
        sessions=[_row("terminal-mcp-claude-audit")],
        records=[FakeRecord(node_id="local", session_name="terminal-mcp-claude-audit",
                            last_known_state="IDLE")])
    store.set_tasks("terminal-mcp-claude-audit", [{"prompt": "already waiting here"}])
    router = _router(store, queue, controller)

    receipt = router.route_start("ROUTER_SMOKE: verify and reply")

    assert receipt["session"] is None
    assert receipt["routing_state"] == WAITING_RUNTIME
    assert receipt["rejected_candidates"][0]["rejected"] == sm.SESSION_BACKLOGGED
    assert receipt["dispatched"] is False


def test_dispatched_is_false_when_the_engine_declines_to_start_the_task(store, queue):
    """DEFECT 2. `dispatched` came from "tick() did not raise", so a task the
    admission governor had refused came back as started."""
    controller = FakeController(sessions=[_row("idle-one")],
                                records=[FakeRecord(node_id="local", session_name="idle-one")])
    engine = StubbornEngine()
    router = _router(store, queue, controller, engine=engine)

    receipt = router.route_start("work the engine will not claim")

    assert receipt["dispatched"] is False
    assert receipt["status"] == "TASK_ACCEPTED"
    assert engine.ticks, "the router must at least have tried"
    assert "admission refused" in receipt["routing_reason"]


def test_a_task_the_engine_will_not_start_is_released_not_left_bound_and_queued(store, queue):
    """DEFECT 3. Leaving it BOUND parks the task in a lane nothing will advance
    while holding a session other tasks could use -- the original bug with a
    routing decision attached to it."""
    controller = FakeController(sessions=[_row("idle-one")],
                                records=[FakeRecord(node_id="local", session_name="idle-one")])
    router = _router(store, queue, controller, engine=StubbornEngine())

    receipt = router.route_start("work the engine will not claim")
    task = store.get_task(receipt["task_id"])

    assert task.routing_state == WAITING_RUNTIME
    assert task.execution_session is None          # the session was handed back
    assert task.status == QUEUED                   # durable, never failed
    assert "released" in task.routing_evidence["reason"]
    assert task.routing_evidence["undispatchable_session"] == "idle-one"
    # And the freed session is offered to the next task rather than held.
    assert store.tasks_bound_to_session("idle-one") == []


def test_route_start_drives_the_lane_past_a_single_transition(store, queue):
    """The engine makes ONE transition per tick, so binding plus one tick can
    never reach a running task -- the router has to drive the sequence."""
    controller = FakeController(sessions=[_row("idle-one")],
                                records=[FakeRecord(node_id="local", session_name="idle-one")])
    engine = RecordingEngine(store)
    router = _router(store, queue, controller, engine=engine)

    receipt = router.route_start("real work")

    assert len(engine.ticks) > 1
    assert receipt["dispatch_ticks"] > 1
    assert receipt["task_state"] in START_UNDERWAY_STATUSES
    assert receipt["dispatched"] is True


def test_the_exact_live_scenario_now_reaches_a_truthful_outcome(store, queue, tmp_path):
    """END TO END, with the live fleet's shape: two sessions in the SAME repo,
    the first with work already queued in its lane and the second genuinely
    free. The first is what the live router picked, and picking it is what
    produced BOUND + QUEUED."""
    repo = _repo_worktree(tmp_path, "terminal-mcp")
    controller = FakeController(
        sessions=[_row("terminal-mcp-claude-audit"), _row("free-runner")],
        records=[
            FakeRecord(node_id="local", session_name="terminal-mcp-claude-audit",
                       cwd=repo, repo_root=repo, last_known_state="IDLE"),
            FakeRecord(node_id="local", session_name="free-runner", cwd=repo,
                       repo_root=repo, last_known_state="IDLE"),
        ])
    store.set_tasks("terminal-mcp-claude-audit", [{"prompt": "work already queued there"}])
    engine = RecordingEngine(store)
    router = _router(store, queue, controller, engine=engine)

    receipt = router.route_start(
        "ROUTER_SMOKE_001: verify the current Terminal MCP checkout is readable",
        metadata={"repo": repo})

    # The higher-scoring session is refused for a stated reason, and the task
    # goes somewhere it can actually run -- never bound-and-stuck.
    assert receipt["session"] == "free-runner"
    assert receipt["dispatched"] is True
    task = store.get_task(receipt["task_id"])
    assert task.status in START_UNDERWAY_STATUSES
    assert task.execution_session == "free-runner"
    rejected = {row["session"]: row["rejected"] for row in
                task.routing_evidence["rejected"]}
    assert rejected["terminal-mcp-claude-audit"] == sm.SESSION_BACKLOGGED


def test_a_read_only_smoke_prompt_is_not_classified_as_a_payment_change():
    """The live smoke prompt came back risk_flags=['payment'] because the
    payment exclusion matched the bare token `checkout` in "the current
    Terminal MCP checkout" -- a git checkout, not a shopping one."""
    profile = task_profile.analyze(
        prompt="ROUTER_SMOKE_001: verify the current Terminal MCP checkout is readable "
               "and reply exactly ROUTER_SMOKE_OK. Do not modify files, do not commit.")
    assert profile.risk_flags == ()
    assert profile.requires_approval is False


@pytest.mark.parametrize("prompt", [
    "update the stripe checkout flow",
    "the billing page is broken",
    "issue a refund for this invoice",
    "charge the card on file twice",
])
def test_real_payment_work_is_still_excluded(prompt):
    """The false-positive fix must not blunt the exclusion it narrowed."""
    assert "payment" in task_profile.analyze(prompt=prompt).risk_flags


@pytest.mark.parametrize("prompt", [
    "git checkout main and run the tests",
    "verify the checkout at /srv/app is clean",
    "who is in charge of this module",
])
def test_git_and_ordinary_english_are_not_payment_work(prompt):
    assert "payment" not in task_profile.analyze(prompt=prompt).risk_flags


# ---------------------------------------------------------------------------
# LIVE FAILURE 2, hp-linux @ 5541539 -- an idle shell was chosen for a
# dispatch it can never accept, and the synchronous call had no time ceiling.
# ---------------------------------------------------------------------------

def test_a_plain_shell_session_is_never_chosen_for_a_queued_dispatch(store, queue):
    """The engine wraps every prompt in a multi-line completion-marker
    template, and a shell executes each line as it arrives -- so the send is
    refused. Found live: the router bound an idle same-repo shell, the engine
    answered MULTILINE_SHELL_SEND_REFUSED, and the binding had to be undone.
    That is a permanent property of the runtime, so it is rejected up front."""
    controller = FakeController(
        sessions=[_row("a-shell")],
        records=[FakeRecord(node_id="local", session_name="a-shell",
                            agent_type="shell", last_known_state="IDLE")])
    router = _router(store, queue, controller)

    receipt = router.route_start("echo hello")

    assert receipt["session"] is None
    assert receipt["rejected_candidates"][0]["rejected"] == sm.RUNTIME_CANNOT_RECEIVE_DISPATCH
    assert "multi-line" in receipt["rejected_candidates"][0]["rejected_detail"]


@pytest.mark.parametrize("runtime,chooseable", [("claude", True), ("codex", True),
                                                ("shell", False), ("bash", False)])
def test_only_runtimes_that_buffer_a_multiline_prompt_are_dispatchable(runtime, chooseable):
    candidate = SessionCandidate(session="s", state="IDLE", runtime=runtime)
    rejected = sm.hard_reject(_profile(), candidate)
    assert (rejected is None) is chooseable


def test_route_start_stops_driving_when_its_wall_clock_budget_runs_out(store, queue):
    """A submission must not become a long poll. Each tick is remote node I/O,
    so the drive is bounded by wall clock as well as by tick count."""
    now = {"t": 0.0}

    class SlowEngine:
        def __init__(self):
            self.ticks = 0

        def tick(self, session):
            self.ticks += 1
            now["t"] += 3.0        # each tick costs three seconds
            return _Result("SLOW")

    engine = SlowEngine()
    controller = FakeController(sessions=[_row("idle-one")],
                                records=[FakeRecord(node_id="local", session_name="idle-one")])
    router = TaskRouter(store, controller=controller, queue=queue, engine=engine,
                        session_registry=controller.session_registry,
                        config=_config(dispatch_budget_seconds=5.0),
                        clock=lambda: now["t"])

    receipt = router.route_start("work")

    assert receipt["dispatched"] is False
    assert receipt["budget_exhausted"] is True
    assert "budget of 5s exhausted" in receipt["dispatch_detail"]
    # Stopped by the clock, well before the tick ceiling.
    assert engine.ticks == 2
    assert engine.ticks < MAX_START_TICKS


def test_a_budget_timeout_keeps_the_binding_because_the_engine_is_still_working(store, queue):
    """Releasing here would be wrong. The engine may be mid-dispatch; only the
    CALLER gave up waiting, so the task stays bound and the server carries on.
    This is the opposite of the engine-refuses case, which does release."""
    now = {"t": 0.0}

    class SlowEngine:
        def tick(self, session):
            now["t"] += 10.0
            return _Result("SLOW")

    controller = FakeController(sessions=[_row("idle-one")],
                                records=[FakeRecord(node_id="local", session_name="idle-one")])
    router = TaskRouter(store, controller=controller, queue=queue, engine=SlowEngine(),
                        session_registry=controller.session_registry,
                        config=_config(dispatch_budget_seconds=5.0),
                        clock=lambda: now["t"])

    receipt = router.route_start("work")
    task = store.get_task(receipt["task_id"])

    assert receipt["budget_exhausted"] is True
    assert task.routing_state == BOUND
    assert task.execution_session == "idle-one"
    assert receipt["poll"] is False
    assert "still driving it" in receipt["guidance"]
