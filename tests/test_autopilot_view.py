"""P2 Control Tower read model + safe controls (issue #106).

The snapshot is the ONE object served to the dashboard and to a read-only
ChatGPT report: no fabricated percentage, CODE_READY / DEVICE_QA /
RELEASE_READY kept separate and UNKNOWN without evidence, in-flight work shown
UNKNOWN when nobody has observed it recently, and pause/resume reusing
ProjectService so a foreign pause is never undone.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp import autopilot as ap
from terminal_mcp import autopilot_view as av
from terminal_mcp.event_bus import EventBus
from terminal_mcp.event_wiring import build_queue_event_sink, build_verify_event_sink
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.outcomes import OutcomeStore
from terminal_mcp.project_service import ProjectService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.verify_queue import VerifyQueue

from .test_autopilot import FakeDispatcher

P1 = "synthetic/brandagent-staging"
P2 = "synthetic/second-project"


class _Queue:
    def __init__(self, store):
        self.store = store


@pytest.fixture
def tower(tmp_path):
    bus = EventBus(tmp_path / "events.db")
    store = QueueStore(tmp_path / "queue.db", event_sink=build_queue_event_sink(bus))
    verify = VerifyQueue(store, event_sink=build_verify_event_sink(bus))
    outcomes = OutcomeStore(store)
    projects = ProjectService(queue=_Queue(store), events=bus, verify=verify, outcomes=outcomes)
    policy = ap.AutopilotPolicy(enabled=True, projects=(
        ap.AutopilotProjectPolicy(project_id=P1, dispatch=True, approved_by="owner@test",
                                  approval_ref="https://github.com/hungtranbkit/terminal-mcp/issues/106",
                                  lanes=("p1-work",)),
        ap.AutopilotProjectPolicy(project_id=P2, lanes=("p2-work",)),
    ))
    dispatcher = FakeDispatcher()
    autopilot = ap.ProjectAutopilot(store=store, verify=verify, outcomes=outcomes, bus=bus,
                                    locks=ResourceLockStore(tmp_path / "locks.db"),
                                    dispatcher=dispatcher, policy=policy, instance_id="hp-1")
    clock = {"now": 1_000_000.0}
    loop = ap.AutopilotLoop(autopilot, clock=lambda: clock["now"])
    t = av.ControlTower(store=store, verify=verify, outcomes=outcomes, bus=bus, projects=projects,
                        policy=policy, loop=loop, clock=lambda: clock["now"])
    t.test = {"store": store, "verify": verify, "outcomes": outcomes, "bus": bus,
              "dispatcher": dispatcher, "loop": loop, "clock": clock}
    return t


def _add(t, lane, title, project=P1, **kw):
    [task_id] = t.test["store"].append_tasks(lane, [{
        "title": title, "prompt": f"Implement {title}.", "project_id": project, **kw}])
    return task_id


def _verify_pass(t, task_id, evidence=None):
    job = t.test["verify"].claim_next(verifier="qa-1", project_id=P1)
    assert job.task_id == task_id
    assert t.test["verify"].complete(job.id, job.claim_token, evidence=evidence or {
        "command": "pytest -q", "exit_code": 0, "tests_passed": 3, "tests_failed": 0,
        "commit_sha": "c0ffee1"})["ok"]


def test_snapshot_contract_and_no_fabricated_percent(tower):
    outcome = tower.test["outcomes"].create(P1, "Brief generator", acceptance_criteria=["renders"])
    a = _add(tower, "p1-work", "A")
    tower.test["outcomes"].attach_task(outcome.id, a)
    snap = tower.snapshot(P1)
    assert snap["schema"] == av.SNAPSHOT_SCHEMA and snap["project_id"] == P1
    assert snap["autopilot"]["mode"] == ap.MODE_DISPATCH
    assert snap["autopilot"]["approval"]["approval_ref"].endswith("/issues/106")
    assert snap["outcomes"][0]["acceptance_criteria"] == ["renders"]
    assert snap["outcomes"][0]["progress"] == {"completed": 0, "total": 1}
    assert [t["id"] for t in snap["tasks"]] == [a]
    assert "percent" not in json.dumps(snap).lower()
    assert "Implement A" not in json.dumps(snap)  # prompts are not in the read model


def test_readiness_gates_are_separate_and_unknown_without_evidence(tower):
    assert tower.snapshot(P1)["readiness"] == {
        "CODE_READY": "UNKNOWN", "DEVICE_QA": "UNKNOWN", "RELEASE_READY": "UNKNOWN"}
    a = _add(tower, "p1-work", "A")
    assert tower.snapshot(P1)["readiness"]["CODE_READY"] == "NOT_READY"
    loop = tower.test["loop"]
    loop.run_once()
    tower.test["dispatcher"].set_state("p1-work", ap.DONE)
    loop.run_once()
    _verify_pass(tower, a)
    readiness = tower.snapshot(P1)["readiness"]
    # Verified code is not device QA, and neither is a release.
    assert readiness == {"CODE_READY": "READY", "DEVICE_QA": "UNKNOWN", "RELEASE_READY": "UNKNOWN"}


def test_device_qa_reads_only_explicit_device_evidence(tower):
    a = _add(tower, "p1-work", "A")
    tower.test["loop"].run_once()
    tower.test["dispatcher"].set_state("p1-work", ap.DONE)
    tower.test["loop"].run_once()
    _verify_pass(tower, a, evidence={"command": "maestro test", "exit_code": 0, "tests_passed": 4,
                                     "tests_failed": 0, "device_qa": {"passed": True,
                                                                      "device": "synthetic-emulator"}})
    readiness = tower.snapshot(P1)["readiness"]
    assert readiness["DEVICE_QA"] == "READY"
    assert readiness["RELEASE_READY"] == "UNKNOWN"  # outcomes not accepted, no owner approval


def test_in_flight_work_without_recent_observation_is_unknown(tower):
    a = _add(tower, "p1-work", "A")
    tower.test["loop"].run_once()
    tower.test["dispatcher"].set_state("p1-work", ap.RUNNING)
    tower.test["loop"].run_once()
    fresh = {t["id"]: t for t in tower.snapshot(P1)["tasks"]}[a]
    assert fresh["status"] == "RUNNING" and fresh["display_status"] == "RUNNING"
    tower.test["clock"]["now"] += 10_000  # observer has not ticked for hours
    stale = {t["id"]: t for t in tower.snapshot(P1)["tasks"]}[a]
    assert stale["stale"] is True and stale["display_status"] == "UNKNOWN"


def test_unreachable_node_is_shown_unknown_not_running(tower):
    a = _add(tower, "p1-work", "A")
    tower.test["loop"].run_once()
    tower.test["dispatcher"].set_state("p1-work", ap.RUNNING)
    tower.test["loop"].run_once()
    tower.test["dispatcher"].offline = True
    tower.test["loop"].run_once()
    task = {t["id"]: t for t in tower.snapshot(P1)["tasks"]}[a]
    assert task["display_status"] == "UNKNOWN"


def test_needs_owner_lists_blocked_work(tower):
    b = _add(tower, "p1-work", "B")
    tower.test["loop"].run_once()
    tower.test["dispatcher"].set_state("p1-work", ap.BLOCKED, "needs a signing key from owner")
    tower.test["loop"].run_once()
    needs = tower.snapshot(P1)["needs_owner"]
    assert needs and needs[0]["task_id"] == b


def test_overview_covers_multiple_projects(tower):
    _add(tower, "p1-work", "A")
    _add(tower, "p2-work", "Z", project=P2)
    overview = tower.overview()
    assert {p["project_id"] for p in overview["projects"]} == {P1, P2}
    p2 = next(p for p in overview["projects"] if p["project_id"] == P2)
    assert p2["mode"] == ap.MODE_OBSERVE_ONLY


def test_pause_and_resume_reuse_project_service_and_respect_foreign_pause(tower):
    store = tower.test["store"]
    _add(tower, "p1-work", "A")
    _add(tower, "p1-other", "B")
    store.pause_lane("p1-other", reason="operator investigating", origin="user")
    paused = tower.pause(P1, actor="owner@test", reason="owner review", idempotency_key="k1")
    assert paused["paused"] == ["p1-work"]
    assert tower.snapshot(P1)["autopilot"]["paused"] is True
    assert tower.resume(P1, actor="anon", verified_identity=None)["error"] == "OWNER_APPROVAL_REQUIRED"
    assert tower.resume(P1, actor="x@test", verified_identity="x@test")["error"] == "OWNER_APPROVAL_REQUIRED"
    assert store.lane_status("p1-work")["paused"] is True
    resumed = tower.resume(P1, actor="owner@test", verified_identity="owner@test",
                           idempotency_key="k2")
    assert resumed["resumed"] == ["p1-work"]
    assert store.lane_status("p1-other")["paused"] is True  # foreign pause untouched
    audit = [e for e in tower.test["bus"].list_events(project_id=P1)
             if e["type"].startswith("CONTROL_TOWER_")]
    assert {e["type"] for e in audit} == {"CONTROL_TOWER_PAUSE", "CONTROL_TOWER_RESUME",
                                          "CONTROL_TOWER_RESUME_REFUSED"}
    assert {e["actor"] for e in audit if e["type"] != "CONTROL_TOWER_RESUME_REFUSED"} == {"owner@test"}


def test_pause_stops_dispatch(tower):
    a = _add(tower, "p1-work", "A")
    tower.pause(P1, actor="owner@test", reason="hold")
    tower.test["loop"].run_once()
    assert tower.test["store"].get_task(a).status == "QUEUED"
    assert tower.test["dispatcher"].sends == []


def test_pause_of_unknown_project_is_refused(tower):
    assert tower.pause("synthetic/not-configured", actor="x", reason="y")["error"] == "NOT_OPTED_IN"


def test_notifications_are_meaningful_deltas_only(tower):
    a = _add(tower, "p1-work", "A")
    loop = tower.test["loop"]
    loop.run_once()
    tower.test["dispatcher"].set_state("p1-work", ap.RUNNING)
    loop.run_once()
    first = tower.notifications(P1, since_seq=0)
    assert first["notifications"] == []  # dispatch/running is not news
    tower.test["dispatcher"].set_state("p1-work", ap.DONE)
    loop.run_once()
    _verify_pass(tower, a)
    second = tower.notifications(P1, since_seq=first["next_seq"])
    kinds = [n["kind"] for n in second["notifications"]]
    assert kinds == ["MILESTONE_VERIFIED"]
    again = tower.notifications(P1, since_seq=second["next_seq"])
    assert again["notifications"] == []


def test_snapshot_is_project_scoped(tower):
    _add(tower, "p2-work", "Z-secret-title", project=P2)
    assert "Z-secret-title" not in json.dumps(tower.snapshot(P1))
