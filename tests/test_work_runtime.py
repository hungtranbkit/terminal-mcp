"""Work Runtime V1 -- the binding layer over the queue that already exists.

The two properties worth defending, and the reason this feature is opt-in:

  A session without the `-work` suffix is NEVER driven automatically. Not
  when it is idle, not when it has a queue lane, not when every other
  component would permit it.

  A run is not complete because a worker said so. The outcome contract reads
  the QUEUE's own record of every required task.
"""
from __future__ import annotations

import pytest

from terminal_mcp import work_store as ws
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.work_eligibility import (ELIGIBLE, INPUT_NOT_PERMITTED,
                                           NODE_METADATA_STALE, NODE_UNREACHABLE,
                                           NOT_WORK_SESSION, SESSION_DEAD, SESSION_MISSING,
                                           evaluate, is_work_session, work_base_name)
from terminal_mcp.work_service import WorkService
from terminal_mcp.work_store import WorkStore


@pytest.fixture
def work(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    store = WorkStore(tmp_path / "work.db")
    return WorkService(store, queue=queue), store, queue


# -- the isolation rule ----------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("mesflow-work", True),
    ("terminal-mcp-work", True),
    ("offlinepos-work", True),
    ("hp-linux/mesflow-work", True),
    ("m1", False),
    ("terminal-mcp-main", False),
    ("work", False),
    ("-work", False),          # a suffix with no project is not a work session
    ("work-thing", False),
    ("mesflow-WORK", False),   # case matters: the opt-in must be exact
    ("mesflow-work-2", False),
    ("", False),
    (None, False),
])
def test_only_an_exact_work_suffix_opts_a_session_in(name, expected):
    """Guessing generously here would be guessing about which live terminals
    an autonomous agent may type into."""
    assert is_work_session(name) is expected


def test_the_project_behind_a_work_session_is_recoverable():
    assert work_base_name("mesflow-work") == "mesflow"
    assert work_base_name("hp-linux/offlinepos-work") == "offlinepos"
    assert work_base_name("m1") is None


HEALTHY = {"status": {"exists": True},
           "node": {"node_id": "local", "status": "online", "metadata_stale": False},
           "input_allowed": True}


def test_a_healthy_work_session_is_eligible():
    verdict = evaluate("mesflow-work", **HEALTHY)
    assert verdict.eligible is True and verdict.reason == ELIGIBLE


def test_an_ordinary_session_is_never_eligible_however_healthy_it_looks():
    """The headline guarantee. Perfect health, permissions granted, node
    fresh -- and still not claimable, because it did not opt in."""
    verdict = evaluate("terminal-mcp-main", **HEALTHY)
    assert verdict.eligible is False
    assert verdict.reason == NOT_WORK_SESSION
    assert "does not end in '-work'" in verdict.detail


@pytest.mark.parametrize("overrides,reason", [
    ({"status": None}, SESSION_MISSING),
    ({"status": {"error": "SESSION_NOT_FOUND"}}, SESSION_MISSING),
    ({"status": {"exists": False}}, SESSION_MISSING),
    ({"status": {"exists": True, "pane_dead": True}}, SESSION_DEAD),
    ({"node": {"node_id": "hp", "status": "offline"}}, NODE_UNREACHABLE),
    ({"node": {"node_id": "hp", "status": "online", "metadata_stale": True,
               "metadata_age_seconds": 9000}}, NODE_METADATA_STALE),
    ({"input_allowed": False}, INPUT_NOT_PERMITTED),
])
def test_every_unhealthy_condition_refuses_with_a_named_reason(overrides, reason):
    """"Not eligible" with no explanation is how an operator concludes the
    runtime is broken when it is in fact protecting them."""
    verdict = evaluate("mesflow-work", **{**HEALTHY, **overrides})
    assert verdict.eligible is False
    assert verdict.reason == reason
    assert verdict.detail


def test_unknown_evidence_is_not_treated_as_permission():
    """A gap in what we happened to look up must not decide an autonomous
    dispatch."""
    assert evaluate("mesflow-work", status=None).eligible is False


def test_stale_node_metadata_blocks_dispatch():
    """Dispatching on a record nobody refreshed is how a prompt reaches a
    machine that stopped answering an hour ago."""
    verdict = evaluate("mesflow-work", status={"exists": True},
                       node={"node_id": "hp", "status": "online",
                             "metadata_stale": True, "metadata_age_seconds": 7200})
    assert verdict.reason == NODE_METADATA_STALE
    assert "7200s old" in verdict.detail


def test_eligible_workers_reports_rejections_not_just_winners(work):
    service, _store, _queue = work
    rows = service.eligible_workers(
        sessions=[{"name": "mesflow-work", "node_id": "local", "input_allowed": True},
                  {"name": "m1", "node_id": "local", "input_allowed": True}],
        nodes={"local": {"node_id": "local", "status": "online", "metadata_stale": False}},
        statuses={"mesflow-work": {"exists": True}, "m1": {"exists": True}})
    by_session = {r["session"]: r for r in rows}
    assert by_session["mesflow-work"]["eligible"] is True
    assert by_session["m1"]["eligible"] is False
    assert by_session["m1"]["reason"] == NOT_WORK_SESSION


# -- creation and planning --------------------------------------------------------

def test_a_run_cannot_be_created_on_an_ordinary_session(work):
    """Refused before anything is written: a durable run bound to a normal
    session would invite every later scheduling decision to do the wrong
    thing."""
    service, store, _queue = work
    result = service.create(title="T", goal="G", lane="terminal-mcp-main")
    assert result["error"] == "LANE_NOT_A_WORK_SESSION"
    assert store.list_runs() == []


def test_creating_a_run_with_a_plan_writes_real_queue_tasks(work):
    service, store, queue = work
    result = service.create(
        title="Ship the overview screen", goal="Users can see project health",
        lane="mesflow-work", done_criteria=["tests pass", "screen renders"],
        created_by="hung",
        tasks=[{"title": "build", "prompt": "implement the screen", "weight": 2},
               {"title": "test", "prompt": "write tests"}])
    work_id = result["work"]["work_id"]
    assert result["work"]["state"] == ws.READY
    assert len(result["tasks"]) == 2

    # The plan is rows in the REAL queue, not a parallel table.
    lane = queue.status("mesflow-work")
    assert len(lane["tasks"]) == 2
    assert {t["prompt"] for t in lane["tasks"]} == {"implement the screen", "write tests"}
    # ...and each queue task carries the work binding.
    assert all(t["metadata"]["work_id"] == work_id for t in lane["tasks"])


def test_the_prompt_is_stored_verbatim(work):
    """This layer adds no wrapper and rewrites nothing -- the same rule
    queue_service already holds itself to."""
    service, _store, queue = work
    prompt = "Refactor  the   parser\n\nKeep the API stable."
    service.create(title="T", goal="G", lane="mesflow-work",
                   tasks=[{"title": "x", "prompt": prompt}])
    assert queue.status("mesflow-work")["tasks"][0]["prompt"] == prompt


def test_a_plan_needs_a_prompt(work):
    service, _store, _queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "empty"}])
    assert result["error"] == "TASK_PROMPT_REQUIRED"


def test_binding_a_second_queue_task_is_refused_rather_than_orphaning_the_first(work):
    service, store, _queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "a", "prompt": "do a"}])
    task_id = result["tasks"][0]["work_task_id"]
    store.bind_queue_task(task_id, store.get_task(task_id)["queue_task_id"])  # idempotent
    with pytest.raises(ws.WorkError, match="orphan"):
        store.bind_queue_task(task_id, "some-other-queue-task")


# -- progress and the outcome contract ---------------------------------------------

def _complete(queue, session, task_id):
    """Drive a queue task to COMPLETED through its REAL state machine.

    Deliberately not a direct UPDATE: the contract is only meaningful if the
    states it reads were reached the way a live dispatch would reach them.
    """
    # VERIFYING is not optional: the queue's own state machine refuses
    # RUNNING -> COMPLETED outright, which is the "a worker cannot declare
    # itself done" rule already enforced one level down.
    for step in ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING", "COMPLETED"):
        queue.store.transition_task(task_id, step, event_type="test", reason="test")


def test_progress_is_computed_from_weights_not_parsed_from_a_model(work):
    service, _store, queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "big", "prompt": "p1", "weight": 3},
                                   {"title": "small", "prompt": "p2", "weight": 1}])
    work_id = result["work"]["work_id"]
    assert service.progress(work_id).percent == 0

    _complete(queue, "mesflow-work", result["tasks"][0]["queue_task_id"])
    progress = service.progress(work_id)
    assert progress.percent == 75, "weights, not task count"
    assert progress.done_tasks == 1


def test_a_run_is_not_complete_just_because_tasks_look_done_to_a_worker(work):
    """The outcome contract reads the queue's own record. A model writing
    'STATUS: done' moves nothing."""
    service, _store, queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "a", "prompt": "p1"},
                                   {"title": "b", "prompt": "p2"}])
    work_id = result["work"]["work_id"]
    contract = service.evaluate_contract(work_id)
    assert contract["satisfied"] is False
    assert {u["reason"] for u in contract["unmet"]} == {"REQUIRED_TASK_NOT_COMPLETE"}

    for task in result["tasks"]:
        _complete(queue, "mesflow-work", task["queue_task_id"])
    assert service.evaluate_contract(work_id)["satisfied"] is True


def test_an_optional_task_does_not_hold_the_contract_open(work):
    service, _store, queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "a", "prompt": "p1"},
                                   {"title": "nice-to-have", "prompt": "p2",
                                    "required": False}])
    _complete(queue, "mesflow-work", result["tasks"][0]["queue_task_id"])
    assert service.evaluate_contract(result["work"]["work_id"])["satisfied"] is True


def test_a_blocked_task_holds_the_contract_open_and_says_so(work):
    service, _store, queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "a", "prompt": "p1"}])
    task_id = result["tasks"][0]["queue_task_id"]
    queue.store.transition_task(task_id, "PRECHECK", event_type="test", reason="t")
    queue.store.transition_task(task_id, "BLOCKED", event_type="test", reason="t")
    contract = service.evaluate_contract(result["work"]["work_id"])
    assert contract["satisfied"] is False
    assert "TASK_BLOCKED" in {u["reason"] for u in contract["unmet"]}


def test_a_run_with_no_plan_is_not_silently_complete(work):
    service, _store, _queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work")
    contract = service.evaluate_contract(result["work"]["work_id"])
    assert contract["satisfied"] is False
    assert "NO_PLAN" in {u["reason"] for u in contract["unmet"]}


# -- approvals ---------------------------------------------------------------------

def test_a_pending_approval_holds_the_contract_open(work):
    service, store, queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "a", "prompt": "p1"}])
    work_id = result["work"]["work_id"]
    _complete(queue, "mesflow-work", result["tasks"][0]["queue_task_id"])
    assert service.evaluate_contract(work_id)["satisfied"] is True

    service.request_approval(work_id, kind="production_deploy",
                             summary="restart the controller", requested_by="agent:worker")
    contract = service.evaluate_contract(work_id)
    assert contract["satisfied"] is False
    assert "APPROVAL_PENDING" in {u["reason"] for u in contract["unmet"]}


def test_an_agent_cannot_approve_its_own_gate(work):
    """The entire value of a gate: an agent that can approve what it asked
    for has not been gated at all."""
    service, _store, _queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work")
    work_id = result["work"]["work_id"]
    approval = service.request_approval(work_id, kind="production_deploy",
                                        summary="deploy", requested_by="agent:worker")
    refused = service.decide_approval(approval["approval"]["approval_id"],
                                      decision=ws.APPROVAL_APPROVED,
                                      decided_by="agent:worker")
    assert refused["error"] == "APPROVAL_REFUSED"
    assert "may not also decide it" in refused["detail"]

    allowed = service.decide_approval(approval["approval"]["approval_id"],
                                      decision=ws.APPROVAL_APPROVED, decided_by="human:hung")
    assert allowed["approval"]["decision"] == ws.APPROVAL_APPROVED


def test_asking_for_the_same_gate_twice_does_not_stack_gates(work):
    service, _store, _queue = work
    work_id = service.create(title="T", goal="G", lane="mesflow-work")["work"]["work_id"]
    first = service.request_approval(work_id, kind="deploy", summary="s",
                                     requested_by="agent")["approval"]
    second = service.request_approval(work_id, kind="deploy", summary="s",
                                      requested_by="agent")["approval"]
    assert first["approval_id"] == second["approval_id"]


def test_an_approval_must_name_its_decider(work):
    service, store, _queue = work
    work_id = service.create(title="T", goal="G", lane="mesflow-work")["work"]["work_id"]
    approval = store.request_approval(work_id, kind="k", summary="s", requested_by="a")
    with pytest.raises(ws.WorkError, match="must name a decider"):
        store.decide_approval(approval["approval_id"], decision=ws.APPROVAL_APPROVED,
                              decided_by="")


def test_a_decided_approval_cannot_be_decided_again(work):
    service, store, _queue = work
    work_id = service.create(title="T", goal="G", lane="mesflow-work")["work"]["work_id"]
    approval = store.request_approval(work_id, kind="k", summary="s", requested_by="a")
    store.decide_approval(approval["approval_id"], decision=ws.APPROVAL_APPROVED,
                          decided_by="human")
    with pytest.raises(ws.WorkError, match="already"):
        store.decide_approval(approval["approval_id"], decision=ws.APPROVAL_REJECTED,
                              decided_by="human2")


# -- state machine ------------------------------------------------------------------

def test_an_invalid_run_transition_is_refused_centrally(tmp_path):
    store = WorkStore(tmp_path / "w.db")
    run = store.create_run(title="T", goal="G")
    with pytest.raises(ws.WorkError, match="not a valid transition"):
        store.transition_run(run.work_id, ws.COMPLETE)


def test_a_terminal_run_never_transitions_again(tmp_path):
    store = WorkStore(tmp_path / "w.db")
    run = store.create_run(title="T", goal="G")
    store.transition_run(run.work_id, ws.CANCELLED)
    with pytest.raises(ws.WorkError):
        store.transition_run(run.work_id, ws.READY)


def test_every_state_in_the_template_vocabulary_exists():
    for state in ("DRAFT", "PLANNING", "READY", "RUNNING", "VERIFYING",
                  "WAITING_APPROVAL", "BLOCKED", "PAUSED", "FAILED", "CANCELLED",
                  "COMPLETE"):
        assert state in ws.WORK_STATES


def test_pause_stops_new_dispatch_without_killing_the_worker(work):
    service, _store, queue = work
    result = service.create(title="T", goal="G", lane="mesflow-work",
                            tasks=[{"title": "a", "prompt": "p1"}])
    work_id = result["work"]["work_id"]
    service.store.transition_run(work_id, ws.RUNNING)
    paused = service.control(work_id, "pause", actor="hung", reason="lunch")
    assert paused["work"]["state"] == ws.PAUSED
    assert queue.status("mesflow-work").get("paused") is True
    # The queued task is still there -- pausing is not cancelling.
    assert len(queue.status("mesflow-work")["tasks"]) == 1


def test_resume_puts_the_lane_back(work):
    service, _store, queue = work
    work_id = service.create(title="T", goal="G", lane="mesflow-work",
                             tasks=[{"title": "a", "prompt": "p"}])["work"]["work_id"]
    service.store.transition_run(work_id, ws.RUNNING)
    service.control(work_id, "pause")
    service.control(work_id, "resume")
    assert queue.status("mesflow-work").get("paused") is not True


def test_an_unknown_control_action_is_named_not_guessed(work):
    service, _store, _queue = work
    work_id = service.create(title="T", goal="G", lane="mesflow-work")["work"]["work_id"]
    result = service.control(work_id, "explode")
    assert result["error"] == "UNKNOWN_ACTION"
    assert "cancel" in result["known"]


# -- durability ----------------------------------------------------------------------

def test_everything_survives_a_reopen(tmp_path):
    """Durable across restart, which is the difference between a work runtime
    and a dashboard."""
    queue = QueueService(QueueStore(tmp_path / "q.db"))
    store = WorkStore(tmp_path / "w.db")
    service = WorkService(store, queue=queue)
    result = service.create(title="Persisted", goal="survive", lane="mesflow-work",
                            done_criteria=["still here"], created_by="hung",
                            tasks=[{"title": "a", "prompt": "p1", "weight": 2}])
    work_id = result["work"]["work_id"]
    service.request_approval(work_id, kind="deploy", summary="s", requested_by="agent")
    store.add_artifact(work_id, kind="commit", reference="abc1234", summary="the change")
    store.close()

    # A whole new process's worth of objects, same files.
    reopened = WorkService(QueueService(QueueStore(tmp_path / "q.db")),
                           queue=QueueService(QueueStore(tmp_path / "q.db")))
    reopened = WorkService(WorkStore(tmp_path / "w.db"),
                           queue=QueueService(QueueStore(tmp_path / "q.db")))
    status = reopened.status(work_id)
    assert status["work"]["title"] == "Persisted"
    assert status["work"]["done_criteria"] == ["still here"]
    assert len(status["tasks"]) == 1
    assert status["tasks"][0]["queue_status"] == "QUEUED", "the queue row survived too"
    assert len(status["pending_approvals"]) == 1
    assert status["artifacts"][0]["reference"] == "abc1234"


def test_events_are_append_only_and_explain_the_run(tmp_path):
    store = WorkStore(tmp_path / "w.db")
    run = store.create_run(title="T", goal="G")
    store.transition_run(run.work_id, ws.PLANNING)
    store.transition_run(run.work_id, ws.READY)
    kinds = [e["kind"] for e in store.events_for(run.work_id)]
    assert kinds.count("run_state") == 2
    assert "run_created" in kinds


def test_the_queue_itself_refuses_a_worker_declaring_completion():
    """Worth pinning where Work can see it: the task state machine has no
    RUNNING -> COMPLETED edge at all. Verification is structural, one level
    below the outcome contract, and the contract is the second line of
    defence rather than the only one."""
    from terminal_mcp.queue_store import VALID_TRANSITIONS

    assert "COMPLETED" not in VALID_TRANSITIONS["RUNNING"]
    assert "COMPLETED" in VALID_TRANSITIONS["VERIFYING"]
