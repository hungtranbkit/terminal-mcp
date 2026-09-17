from __future__ import annotations

import json

import pytest

from terminal_mcp import work_store as ws
from terminal_mcp.work_recovery import WorkRecoveryService


class FakeQueue:
    def __init__(self, statuses=None, *, error: Exception | None = None):
        self.statuses = statuses or {}
        self.error = error

    def status(self, lane):
        if self.error:
            raise self.error
        return {"tasks": [
            {"id": task_id, "status": status, "session": lane,
             "prompt": "SECRET FULL PROMPT", "transcript": "SECRET TRANSCRIPT"}
            for task_id, status in self.statuses.get(lane, {}).items()
        ]}


@pytest.fixture
def store(tmp_path):
    value = ws.WorkStore(tmp_path / "work.db")
    yield value
    value.close()


def make_run(store, *, state=ws.RUNNING, project="p1", queue_id="q1"):
    run = store.create_run(title="Build", goal="ship it", lane="alpha-work",
                           project_id=project, done_criteria=["tests pass"], state=state)
    task = store.add_task(run.work_id, title="Implement", queue_task_id=queue_id,
                          lane="alpha-work")
    return run, task


def test_restart_safe_active_discovery_and_project_filter(tmp_path):
    path = tmp_path / "work.db"
    first = ws.WorkStore(path)
    active, _ = make_run(first, project="wanted")
    make_run(first, state=ws.COMPLETE, project="wanted", queue_id="done-q")
    make_run(first, project="other", queue_id="other-q")
    first.close()

    reopened = ws.WorkStore(path)
    try:
        result = WorkRecoveryService(
            reopened, queue=FakeQueue({"alpha-work": {"q1": "RUNNING"}})
        ).recover_active(project_id="wanted")
        assert [item["work_id"] for item in result["works"]] == [active.work_id]
        assert result["works"][0]["progress"]["total"] == 1
        assert result["works"][0]["running_tasks"][0]["status"] == "RUNNING"
        assert result["works"][0]["last_event_id"] > 0
        assert "SECRET" not in json.dumps(result)
    finally:
        reopened.close()


def test_event_cursor_replay_is_ordered_idempotent_and_has_no_duplicates(store):
    run, _ = make_run(store)
    store.record_event(run.work_id, kind="checkpoint", summary="one", detail="full transcript")
    store.record_event(run.work_id, kind="checkpoint", summary="two")

    service = WorkRecoveryService(store)
    first = service.events_since(run.work_id, after_event_id=0, limit=2)
    replay = service.events_since(run.work_id, after_event_id=0, limit=2)
    assert first == replay
    assert [event["id"] for event in first["events"]] == sorted(
        event["id"] for event in first["events"])
    second = service.events_since(run.work_id, first["next_cursor"], limit=50)
    assert not ({event["id"] for event in first["events"]}
                & {event["id"] for event in second["events"]})
    assert all("detail" not in event for event in first["events"] + second["events"])


def test_snapshot_and_attach_include_approval_and_bounded_evidence(store):
    run, task = make_run(store)
    approval = store.request_approval(
        run.work_id, kind="deploy", summary="approve release", requested_by="agent",
        work_task_id=task["work_task_id"], detail="do not expose this detail")
    store.add_artifact(run.work_id, kind="commit", reference="abc123", summary="implementation")
    service = WorkRecoveryService(
        store, queue=FakeQueue({"alpha-work": {"q1": "READY"}}))

    snapshot = service.snapshot(run.work_id)
    assert snapshot["pending_approvals"][0]["approval_id"] == approval["approval_id"]
    assert snapshot["artifacts"][0]["reference"] == "abc123"
    attached = service.attach(run.work_id, after_event_id=0)
    assert attached["snapshot"]["work_id"] == run.work_id
    assert attached["delta"]["events"]
    assert service.attach(run.work_id)["delta"]["events"] == []


@pytest.mark.parametrize(
    ("run_state", "queue_status", "approval", "expected"),
    [
        (ws.RUNNING, "RUNNING", False, "WAIT"),
        (ws.READY, "READY", False, "CONTINUE"),
        (ws.RUNNING, "BLOCKED", False, "NEEDS_HUMAN"),
        (ws.RUNNING, "READY", True, "NEEDS_HUMAN"),
        (ws.COMPLETE, "COMPLETED", False, "DONE"),
    ],
)
def test_resume_recommendations(store, run_state, queue_status, approval, expected):
    run, task = make_run(store, state=run_state, queue_id=f"q-{expected}-{approval}")
    if approval:
        store.request_approval(run.work_id, kind="custom", summary="choose",
                               requested_by="agent", work_task_id=task["work_task_id"])
    queue = FakeQueue({"alpha-work": {task["queue_task_id"]: queue_status}})
    result = WorkRecoveryService(store, queue=queue).recommend_resume(run.work_id)
    assert result["recommendation"] == expected


def test_queue_unavailable_is_unknown_and_does_not_change_durable_state(store):
    run, task = make_run(store, state=ws.RUNNING)
    service = WorkRecoveryService(store, queue=FakeQueue(error=OSError("offline")))

    snapshot = service.snapshot(run.work_id)
    assert snapshot["tasks"][0]["status"] == "UNKNOWN"
    assert snapshot["queue_failures"][0]["status"] == "UNKNOWN"
    recommendation = service.recommend_resume(run.work_id)
    assert recommendation["recommendation"] == "UNKNOWN"
    assert "QUEUE_UNAVAILABLE" in recommendation["reason"]
    assert store.get_run(run.work_id).state == ws.RUNNING
    assert store.get_task(task["work_task_id"])["queue_task_id"] == "q1"
