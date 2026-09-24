"""A send that goes round the queue must still be visible on the task.

Before this, a raw terminal_send_text against a session with an active queue
task recorded an event and nothing else. Every API and every UI reads the
TASK, so the task looked untouched while its prompt had in fact been
delivered by hand -- the queue said one thing, the worker was doing another,
and the screen showed neither. That discrepancy is what made the Work UI look
frozen while a worker was demonstrably running.
"""

from __future__ import annotations

import pytest

from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


def _task(store, session="lane-a"):
    (task_id,) = store.set_tasks(session, [{"prompt": "do the thing", "title": "t"}])
    return task_id


def test_bypass_is_recorded_on_the_task(store):
    task_id = _task(store)
    store.record_manual_dispatch(task_id, detail={
        "at": "2026-09-13T15:00:00Z", "via": "terminal_send_text",
        "sent": True, "submit_status": "SUBMIT_UNCONFIRMED"})
    metadata = store.get_task(task_id).metadata
    assert metadata["manual_dispatch"]["via"] == "terminal_send_text"
    assert metadata["manual_dispatch"]["submit_status"] == "SUBMIT_UNCONFIRMED"


def test_bypass_does_not_move_the_task_status(store):
    task_id = _task(store)
    before = store.get_task(task_id).status
    store.record_manual_dispatch(task_id, detail={"at": "now", "sent": True})
    # The queue genuinely did not dispatch this. Claiming RUNNING would make
    # the state machine lie about its own behaviour; the bypass is recorded
    # as what it is instead.
    assert store.get_task(task_id).status == before


def test_repeated_bypasses_keep_a_bounded_history(store):
    task_id = _task(store)
    for index in range(14):
        store.record_manual_dispatch(task_id, detail={"at": f"t{index}", "sent": True})
    history = store.get_task(task_id).metadata["manual_dispatch_history"]
    assert len(history) == 10                 # bounded, not unbounded growth
    assert history[-1]["at"] == "t13"         # most recent kept
    assert store.get_task(task_id).metadata["manual_dispatch"]["at"] == "t13"


def test_bypass_preserves_existing_task_metadata(store):
    (task_id,) = store.set_tasks("lane-a", [{"prompt": "p", "metadata": {"expected_cwd": "/repo"}}])
    store.record_manual_dispatch(task_id, detail={"at": "now", "sent": True})
    metadata = store.get_task(task_id).metadata
    assert metadata["expected_cwd"] == "/repo"     # never clobbered
    assert "manual_dispatch" in metadata


def test_recording_against_an_unknown_task_is_a_no_op(store):
    assert store.record_manual_dispatch("no-such-task", detail={"at": "now"}) is None


def test_the_send_path_reconciles_and_never_breaks_the_send():
    """The reconciliation is wrapped so a store failure cannot fail a real send."""
    import inspect

    from terminal_mcp import mcp_app

    body = inspect.getsource(mcp_app).split("RAW_SEND_DURING_ACTIVE_QUEUE_TASK", 1)[1][:1400]
    assert "record_manual_dispatch" in body
    assert "except Exception" in body          # a send must survive a bookkeeping failure
    assert "if not dry_run" in body            # a dry run dispatches nothing, so records nothing


# -- idempotent creation (item 17) -------------------------------------------

def test_request_key_is_exposed_on_the_mcp_creation_tools():
    """The idempotency key has to reach the callers that actually retry.

    It existed in the store and service but not on the tool surface, so a
    ChatGPT/dashboard retry after a timeout still produced a second task --
    the exact duplicate the key was built to prevent.
    """
    import inspect

    from terminal_mcp import mcp_app

    source = inspect.getsource(mcp_app)
    for tool in ("terminal_enqueue_task", "terminal_task_create"):
        signature = source.split(f"def {tool}(", 1)[1].split(") -> dict", 1)[0]
        assert "request_key" in signature, f"{tool} does not accept request_key"
        body = source.split(f"def {tool}(", 1)[1][:2600]
        assert "request_key=request_key" in body, f"{tool} does not pass request_key on"


def test_a_retry_with_the_same_key_returns_the_same_task(tmp_path):
    from terminal_mcp.queue_service import QueueService

    store = QueueStore(tmp_path / "queue.db")
    service = QueueService(store)
    service._validate_session = lambda session: None

    first = service.enqueue("demo-work", "do it", title="T", request_key="req-1")
    second = service.enqueue("demo-work", "do it", title="T", request_key="req-1")
    assert first["task_id"] == second["task_id"]
    assert first["deduplicated"] is False and second["deduplicated"] is True
    assert len(store.lane_status("demo-work")["tasks"]) == 1


def test_keyless_creation_is_unchanged(tmp_path):
    from terminal_mcp.queue_service import QueueService

    store = QueueStore(tmp_path / "queue.db")
    service = QueueService(store)
    service._validate_session = lambda session: None
    first = service.enqueue("demo-work", "a", title="A")
    second = service.enqueue("demo-work", "a", title="A")
    # No key means no dedupe -- existing callers behave exactly as before.
    assert first["task_id"] != second["task_id"]


def test_the_unassigned_path_is_idempotent_too(tmp_path):
    from terminal_mcp.queue_service import QueueService

    store = QueueStore(tmp_path / "queue.db")
    service = QueueService(store)
    first = service.create_task("Global", "prompt", request_key="req-9")
    second = service.create_task("Global", "prompt", request_key="req-9")
    assert first["task_id"] == second["task_id"]
    assert second["deduplicated"] is True


# -- closing a task the engine never dispatched ------------------------------
#
# The other half of the same discrepancy. record_manual_dispatch above notes
# that a send went round the queue, but deliberately does not move the status
# -- so a task whose work was driven in by hand stays QUEUED forever: no
# attempt, no nonce, no completion marker, nothing the engine can ever act on.
# Every board and dashboard then reports "pending" about work that has shipped.
# QueueService.verify is the supported way to close it, with evidence.

def _service(tmp_path):
    from terminal_mcp.queue_service import QueueService

    return QueueService(QueueStore(tmp_path / "queue.db"))


def _undispatched_task(service, session="lane-a"):
    (task_id,) = service.store.set_tasks(session, [{"prompt": "ship it", "title": "t"}])
    task = service.store.get_task(task_id)
    assert task.status == "QUEUED" and task.attempt_count == 0 and task.started_at is None
    return task_id


def test_a_task_executed_by_direct_sends_can_be_closed_with_evidence(tmp_path):
    """The exact stale-QUEUED scenario: work done by hand in the session,
    durable task never dispatched, and before this fix nothing could ever
    move it out of QUEUED."""
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    service.store.record_manual_dispatch(task_id, detail={
        "at": "2026-09-19T09:40:00Z", "via": "terminal_send_text", "sent": True})

    result = service.verify("lane-a", task_id, {
        "commits": ["d277be5"], "tests": "225 passed", "live_verify": "PASS"})

    assert "error" not in result, result
    assert result["task"]["status"] == "COMPLETED"
    assert result["task"]["completed_at"]


def test_reconciled_completion_is_labelled_as_such_in_the_event_log(tmp_path):
    """A human vouching for a task must not be indistinguishable from the
    engine having verified a completion marker."""
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    service.verify("lane-a", task_id, {"live_verify": "PASS"})

    kinds = [e["event_type"] for e in service.store.list_events("lane-a")
             if e.get("task_id") == task_id]
    assert "RECONCILED" in kinds
    assert "VERIFIED" not in kinds


def test_reconciling_still_demands_evidence(tmp_path):
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    assert service.verify("lane-a", task_id, {})["error"] == "EVIDENCE_REQUIRED"
    assert service.store.get_task(task_id).status == "QUEUED"


def test_manual_verify_reports_git_evidence_refusal_without_crashing(tmp_path):
    service = _service(tmp_path)
    (task_id,) = service.store.set_tasks("lane-a", [{
        "prompt": "implement and commit", "title": "t",
        "metadata": {"requires_git_evidence": True},
    }])

    result = service.verify("lane-a", task_id, {"completion_marker": "candidate"})

    assert result["error"] == "COMPLETION_EVIDENCE_REJECTED"
    assert "GIT_EVIDENCE_REQUIRED" in result["reason"]
    assert service.store.get_task(task_id).status == "QUEUED"


def test_a_task_the_engine_is_working_is_never_closed_from_the_side(tmp_path):
    """Only a task with no attempt, no start and no claim qualifies -- closing
    an in-flight one would race the engine's own transition."""
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    service.store.transition_task(task_id, "DISPATCHING", event_type="DISPATCHING")

    result = service.verify("lane-a", task_id, {"live_verify": "PASS"})
    assert result["error"] == "INVALID_TRANSITION"
    assert service.store.get_task(task_id).status == "DISPATCHING"


def test_a_closed_task_is_no_longer_pending_anywhere(tmp_path):
    """The point of the fix: the board must stop advertising it."""
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    service.verify("lane-a", task_id, {"live_verify": "PASS"})

    assert service.pending_counts().get("lane-a", 0) == 0
    statuses = {t["id"]: t["status"] for t in service.status("lane-a")["tasks"]}
    assert statuses[task_id] == "COMPLETED"


def test_a_contract_refusal_comes_back_as_an_answer_not_a_crash(tmp_path):
    """This tool is what an operator reaches for when a task is ALREADY stuck,
    so a contract refusal here is an expected verdict, not an exception.

    It used to raise straight through, which turned the one call someone makes
    to diagnose a stranded task into an opaque tool error -- on the incident
    task (4b5ecb09...) the operator was left editing the database by hand.
    """
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    service.store.set_requirement_contract(
        task_id, requirements=[{"id": "R1", "text": "an unmet criterion"}])

    result = service.verify("lane-a", task_id, {"live_verify": "PASS"})

    assert result["error"] == "REQUIREMENTS_NOT_COVERED"
    assert result["decision"]["missing_requirements"] == ["R1"]
    assert [row["requirement_id"] for row in result["decision"]["checklist"]] == ["R1"]
    assert service.store.get_task(task_id).status == "QUEUED", \
        "a refused verification must leave the task where it was"


def test_a_vacuous_amendment_no_longer_blocks_an_operator_reconciliation(tmp_path):
    """The incident shape, through the operator's own path."""
    service = _service(tmp_path)
    task_id = _undispatched_task(service)
    service.store.set_requirement_contract(task_id, requirements=[])
    service.store.amend_requirement_contract(
        task_id, prompt="a follow-up asking nothing new", requirements=[])

    result = service.verify("lane-a", task_id, {"live_verify": "PASS"})

    assert "error" not in result, result
    assert result["task"]["status"] == "COMPLETED"
