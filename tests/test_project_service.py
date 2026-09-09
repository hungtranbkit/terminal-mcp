"""P0.7 Project APIs (project_service.ProjectService).

Two properties matter most here and are tested hardest:

  1. NO NEW STATE. This layer is composition over P0.1-P0.6 plus the
     backlog; a project view that cached anything would be a second
     source of truth. Asserted by reading through it and then changing
     the underlying store directly.
  2. PAUSE AND RESUME ARE NOT SYMMETRIC. A project resume must never
     silently undo a pause somebody else put there deliberately.
"""
from __future__ import annotations

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.node_models import NODE_ONLINE, Node
from terminal_mcp.project_service import PROJECT_PAUSE_MARKER, ProjectService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

PROJECT = "git:github.com/acme/widget"
OTHER = "git:github.com/acme/gadget"


@pytest.fixture
def queue(tmp_path) -> QueueService:
    return QueueService(QueueStore(tmp_path / "queue.db"))


@pytest.fixture
def projects(queue, tmp_path) -> ProjectService:
    return ProjectService(queue=queue, verify=queue.verify_queue,
                          locks=ResourceLockStore(tmp_path / "leases.db"),
                          registry=_FakeRegistry([
                              _node("linux-a", capabilities=["python", "playwright"]),
                              _node("win-a", capabilities=[], platform="windows"),
                          ]))


def _node(node_id, *, capabilities=(), platform="linux", status=NODE_ONLINE) -> Node:
    return Node(id=node_id, display_name=node_id, hostname=node_id, endpoint="local",
                status=status, draining=False, last_heartbeat_at=None, latency_ms=None,
                cpu_percent=None, cpu_percent_smoothed=None, load1=None, load5=None, load15=None,
                cpu_count=None, ram_total_bytes=None, ram_used_bytes=None, ram_percent=None,
                ram_percent_smoothed=None, swap_total_bytes=None, swap_used_bytes=None,
                swap_percent=None, swap_percent_smoothed=None, disk_total_bytes=None,
                disk_used_bytes=None, disk_free_bytes=None, disk_percent=None,
                tmux_session_count=None, capabilities=tuple(capabilities), platform=platform)


class _FakeRegistry:
    def __init__(self, nodes):
        self._nodes = nodes

    def list(self):
        return list(self._nodes)


def scoped_task(queue: QueueService, *, session: str, project_id: str = PROJECT,
                title: str = "t") -> str:
    (task_id,) = queue.store.set_tasks(session, [{"title": title, "prompt": "do it"}],
                                       replace_pending=False)
    queue.store.set_task_project(task_id, project_id)
    return task_id


# -- 1. Lane discovery ----------------------------------------------------

def test_lanes_are_discovered_from_both_sources(queue, projects):
    """A lane counts if its own project column names the project OR it
    holds a task scoped to it -- the two were populated at different
    times (v4 vs P0.1) and a real deployment has each."""
    scoped_task(queue, session="by-task")
    queue.store.set_lane_project("by-lane", PROJECT)
    scoped_task(queue, session="other-project", project_id=OTHER)

    status = projects.status(PROJECT)
    assert {row["session"] for row in status["lanes"]} == {"by-task", "by-lane"}


# -- 2. Status ------------------------------------------------------------

def test_status_reports_workers_blockers_and_counts(queue, projects):
    running = scoped_task(queue, session="lane-a", title="running one")
    queue.store.transition_task(running, qs.DISPATCHING, event_type="D")
    queue.store.transition_task(running, qs.RUNNING, event_type="R")
    blocked = scoped_task(queue, session="lane-a", title="blocked one")
    queue.store.transition_task(blocked, qs.DISPATCHING, event_type="D")
    queue.store.transition_task(blocked, qs.BLOCKED, event_type="B", reason="needs a human")

    status = projects.status(PROJECT)
    assert status["task_counts"][qs.RUNNING] == 1
    assert status["task_counts"][qs.BLOCKED] == 1
    assert [b["reason"] for b in status["blockers"]] == ["needs a human"]
    assert status["open_tasks"] == 2


def test_status_lists_the_worker_holding_each_active_task(queue, projects):
    queue.store.set_tasks("lane-a", [{"title": "t", "prompt": "p"}])
    claimed = queue.store.claim_next_task("lane-a", claimed_by="worker-A")
    queue.store.set_task_project(claimed.id, PROJECT)

    workers = projects.status(PROJECT)["workers"]
    assert len(workers) == 1
    assert workers[0]["worker"] == "worker-A"
    assert workers[0]["lease_expires_at"], "a worker entry without its lease expiry is not actionable"


def test_unwired_subsystems_report_null_not_zero(queue):
    """"not wired" and "wired and empty" are different answers; a status
    API that conflated them would be lying quietly."""
    bare = ProjectService(queue=queue)
    scoped_task(queue, session="lane-a")
    status = bare.status(PROJECT)
    assert status["verification"] is None
    assert status["resource_locks"] is None
    assert status["events"] is None
    assert status["backlog"] is None
    # ...while a wired-but-empty one reports a real, empty structure.
    wired = ProjectService(queue=queue, verify=queue.verify_queue)
    assert wired.status(PROJECT)["verification"]["stats"]["VERIFY_PENDING"] == 0


def test_status_surfaces_pending_verification_with_its_block_reason(queue, projects):
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    task = queue.store.transition_task(task_id, qs.RUNNING, event_type="R")
    queue.verify_queue.ensure_verify_job(task, required_capabilities=["dotnet", "windows"])

    verification = projects.status(PROJECT)["verification"]
    assert verification["stats"]["VERIFY_PENDING"] == 1
    pending = verification["pending"][0]
    assert pending["required_capabilities"] == ["dotnet", "windows"]
    assert pending["routability"]["routable"] is False
    assert "dotnet" in pending["routability"]["reason"]


def test_routability_uses_this_services_registry_not_only_the_verify_queues(queue, tmp_path):
    """Found by a failing test, not by review: ProjectService holds the
    node registry, but VerifyQueue (built by QueueService) does not. Before
    this, a project's pending verification always reported
    "routability unknown" -- the least useful possible answer, from a
    caller that had the data to answer it."""
    assert queue.verify_queue.registry is None
    service = ProjectService(queue=queue, verify=queue.verify_queue,
                             registry=_FakeRegistry([_node("linux-a", capabilities=["python"])]))
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    task = queue.store.transition_task(task_id, qs.RUNNING, event_type="R")
    queue.verify_queue.ensure_verify_job(task, required_capabilities=["python"])

    routing = service.status(PROJECT)["verification"]["pending"][0]["routability"]
    assert routing["routable"] is True
    assert routing["candidates"] == ["linux-a"]


def test_status_includes_held_resource_locks(queue, projects, tmp_path):
    scoped_task(queue, session="lane-a")
    projects.locks.acquire(PROJECT, "src/app.py", "worker-A", reason="editing")
    projects.locks.acquire(OTHER, "src/app.py", "worker-B")

    locks = projects.status(PROJECT)["resource_locks"]
    assert [row["resource_key"] for row in locks] == ["src/app.py"]
    assert locks[0]["owner_id"] == "worker-A"


def test_status_holds_no_cached_copy(queue, projects):
    """Read, then change the underlying store directly -- the next read
    must reflect it, proving nothing is cached in this layer."""
    task_id = scoped_task(queue, session="lane-a")
    assert projects.status(PROJECT)["task_counts"].get(qs.QUEUED) == 1
    queue.store.cancel_task(task_id)
    counts = projects.status(PROJECT)["task_counts"]
    assert counts.get(qs.QUEUED, 0) == 0 and counts[qs.CANCELLED] == 1


def test_status_requires_a_project_id(projects):
    assert projects.status("")["error"] == "INVALID_REQUEST"


# -- 3. Pause / resume asymmetry -- the dangerous part -------------------

def test_pause_pauses_every_lane_the_project_owns(queue, projects):
    scoped_task(queue, session="lane-a")
    scoped_task(queue, session="lane-b")
    scoped_task(queue, session="lane-other", project_id=OTHER)

    result = projects.pause(PROJECT, reason="release freeze")
    assert sorted(result["paused"]) == ["lane-a", "lane-b"]
    assert queue.store.lane_status("lane-a")["paused"] is True
    assert queue.store.lane_status("lane-other")["paused"] is False, "another project was touched"
    assert PROJECT_PAUSE_MARKER in queue.store.lane_status("lane-a")["paused_reason"]
    assert "release freeze" in queue.store.lane_status("lane-a")["paused_reason"]


def test_pause_leaves_an_already_paused_lane_and_its_reason_alone(queue, projects):
    scoped_task(queue, session="lane-a")
    queue.store.pause_lane("lane-a", reason="operator: investigating a hang")

    result = projects.pause(PROJECT, reason="release freeze")
    assert result["paused"] == []
    assert result["already_paused"][0]["paused_reason"] == "operator: investigating a hang"
    assert queue.store.lane_status("lane-a")["paused_reason"] == "operator: investigating a hang"


def test_resume_never_undoes_a_pause_somebody_else_put_there(queue, projects):
    """The single most dangerous thing this API could do. A lane paused by
    an operator must survive a project-level resume."""
    scoped_task(queue, session="ours")
    scoped_task(queue, session="theirs")
    projects.pause(PROJECT, reason="release freeze")
    queue.store.resume_lane("theirs")
    queue.store.pause_lane("theirs", reason="operator: DO NOT RESUME, disk full")

    result = projects.resume(PROJECT)
    assert result["resumed"] == ["ours"]
    assert queue.store.lane_status("ours")["paused"] is False
    assert queue.store.lane_status("theirs")["paused"] is True
    skipped = result["skipped"][0]
    assert skipped["session"] == "theirs"
    assert "DO NOT RESUME" in skipped["paused_reason"]


def test_force_resume_overrides_and_says_so(queue, projects):
    scoped_task(queue, session="lane-a")
    queue.store.pause_lane("lane-a", reason="operator: investigating")

    result = projects.resume(PROJECT, force=True)
    assert result["forced"] is True
    assert result["resumed"] == ["lane-a"]
    assert queue.store.lane_status("lane-a")["paused"] is False


def test_pause_then_resume_restores_a_running_task(queue, projects):
    """The lane mechanism already saves paused_from_status; a project
    pause must inherit that rather than dropping tasks to QUEUED."""
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    queue.store.transition_task(task_id, qs.RUNNING, event_type="R")

    projects.pause(PROJECT, reason="freeze")
    assert queue.store.get_task(task_id).status == qs.PAUSED
    projects.resume(PROJECT)
    assert queue.store.get_task(task_id).status == qs.RUNNING


def test_resume_reports_lanes_that_were_never_paused(queue, projects):
    scoped_task(queue, session="lane-a")
    result = projects.resume(PROJECT)
    assert result["resumed"] == [] and result["already_running"] == ["lane-a"]


# -- 4. Goals -------------------------------------------------------------

def test_submit_goal_records_intent_and_never_dispatches(queue, tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_BACKLOG_DB", str(tmp_path / "backlog.db"))
    from terminal_mcp.backlog_service import BacklogService
    from tests.test_backlog import make_config
    backlog = BacklogService(make_config(tmp_path), queue=queue)
    service = ProjectService(queue=queue, backlog=backlog)

    result = service.submit_goal(PROJECT, "Ship the offline sync", priority="P1",
                                 acceptance_criteria=["works offline", "no data loss"])
    assert result["submitted"] is True
    assert result["item"]["title"] == "Ship the offline sync"
    assert result["item"]["priority"] == "P1"
    # The crucial negative: NO queue task was created.
    assert queue.store.project_task_counts(PROJECT) == {}
    assert result["item"]["queue_task_id"] is None
    assert "never dispatches" in result["next_step"]


def test_submit_goal_needs_a_backlog_and_a_non_empty_goal(queue):
    service = ProjectService(queue=queue)
    assert service.submit_goal(PROJECT, "x")["error"] == "BACKLOG_UNAVAILABLE"
    assert service.submit_goal(PROJECT, "  ")["error"] == "INVALID_REQUEST"


# -- 5. Events / report ---------------------------------------------------

def test_events_keeps_the_two_streams_apart(queue, projects):
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHING")

    result = projects.project_events(PROJECT)
    assert result["bus"] is None, "no event bus wired in this rig"
    assert any(e["event_type"] == "DISPATCHING" for e in result["queue"])
    # Not merged into one list -- different id spaces.
    assert set(result) >= {"bus", "queue"}


def test_queue_events_follow_a_task_across_lanes(queue, projects):
    """The task_id arm of the derivation matters on its own: a task moved
    between lanes keeps its events attributed to the project."""
    task_id = scoped_task(queue, session="lane-a")
    queue.store.reassign_task(task_id, "lane-b", reason="rebalance", actor="test")
    events = projects.project_events(PROJECT)["queue"]
    assert any(e["task_id"] == task_id for e in events)


def test_report_counts_transitions_not_a_snapshot(queue, projects):
    """A task that completed and was then retried is still a completion
    that happened; a snapshot of current status would have lost it."""
    task_id = scoped_task(queue, session="lane-a")
    for target, event in ((qs.DISPATCHING, "D"), (qs.RUNNING, "R"), (qs.VERIFYING, "V")):
        queue.store.transition_task(task_id, target, event_type=event)
    queue.store.mark_completed_with_evidence(task_id, evidence={"exit_code": 0})

    report = projects.report(PROJECT, window_hours=1)
    assert report["throughput"]["completed"] == 1
    assert report["throughput"]["dispatched"] == 1
    assert report["current"][qs.COMPLETED] == 1


def test_report_window_excludes_older_activity(queue, projects):
    """Backdate one event directly, rather than asking for a zero-length
    window: queue_events timestamps are SECOND-granularity, so "now" and
    an event from this same second are indistinguishable and a 0-hour
    window would prove nothing about the filter."""
    import sqlite3
    task_id = scoped_task(queue, session="lane-a")
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="D")
    assert projects.report(PROJECT, window_hours=1)["throughput"]["dispatched"] == 1

    connection = sqlite3.connect(queue.store.path)
    connection.execute("UPDATE queue_events SET timestamp = '2020-01-01T00:00:00Z'")
    connection.commit()
    connection.close()

    assert projects.report(PROJECT, window_hours=1)["throughput"]["dispatched"] == 0
    assert projects.report(PROJECT, window_hours=24 * 365 * 20)["throughput"]["dispatched"] == 1
    # ...and `current` is a snapshot, so it is unaffected by the window.
    assert projects.report(PROJECT, window_hours=1)["current"][qs.DISPATCHING] == 1


# -- 6. Assignment --------------------------------------------------------

def test_assign_to_an_explicit_session_moves_the_task(queue, projects):
    task_id = scoped_task(queue, session=qs.UNASSIGNED_LANE)
    result = projects.assign(PROJECT, task_id, session="lane-target")
    assert "error" not in result["result"], result
    assert queue.store.get_task(task_id).session == "lane-target"


def test_assign_refuses_another_projects_task(queue, projects):
    task_id = scoped_task(queue, session="lane-a", project_id=OTHER)
    result = projects.assign(PROJECT, task_id, session="lane-b")
    assert result["error"] == "TASK_NOT_IN_PROJECT"
    assert queue.store.get_task(task_id).session == "lane-a"


def test_assign_by_capability_resolves_without_moving_the_task(queue, projects):
    """Picking a lane on a remote node is a decision this facade must not
    make silently -- it reports candidates and stops."""
    task_id = scoped_task(queue, session="lane-a")
    result = projects.assign(PROJECT, task_id, capabilities=["playwright"])
    assert result["assigned"] is False
    assert [c["node_id"] for c in result["candidates"]] == ["linux-a"]
    assert queue.store.get_task(task_id).session == "lane-a"


def test_assign_by_capability_uses_the_same_matcher_as_verifier_routing(queue, projects):
    """AND semantics over probed tools PLUS platform -- so a node with an
    empty probed list is still reachable by `windows`."""
    task_id = scoped_task(queue, session="lane-a")
    windows = projects.assign(PROJECT, task_id, capabilities=["windows"])
    assert [c["node_id"] for c in windows["candidates"]] == ["win-a"]
    impossible = projects.assign(PROJECT, task_id, capabilities=["windows", "playwright"])
    assert impossible["candidates"] == []


def test_assign_reports_a_missing_task_and_a_missing_registry(queue):
    scoped = ProjectService(queue=queue)
    assert scoped.assign(PROJECT, "no-such-task")["error"] == "TASK_NOT_FOUND"
    task_id = scoped_task(queue, session="lane-a")
    assert scoped.assign(PROJECT, task_id, capabilities=["x"])["error"] == "REGISTRY_UNAVAILABLE"


# -- 7. No new state ------------------------------------------------------

def test_project_service_adds_no_table_and_no_migration(queue, tmp_path):
    """The design constraint, asserted structurally: P0.7 is composition.
    A project view with its own table would be a second source of truth."""
    import sqlite3
    before = sqlite3.connect(queue.store.path)
    tables_before = {r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    version_before = before.execute("PRAGMA user_version").fetchone()[0]
    before.close()

    service = ProjectService(queue=queue, verify=queue.verify_queue)
    scoped_task(queue, session="lane-a")
    service.status(PROJECT)
    service.report(PROJECT)
    service.project_events(PROJECT)

    after = sqlite3.connect(queue.store.path)
    tables_after = {r[0] for r in after.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables_after == tables_before
    assert after.execute("PRAGMA user_version").fetchone()[0] == version_before
    after.close()

    source = (__import__("pathlib").Path(__file__).parent.parent
              / "terminal_mcp" / "project_service.py").read_text()
    assert "CREATE TABLE" not in source
    assert "Migration(" not in source
