"""Idempotency of dashboard-created submissions.

One browser submission must produce exactly one task, however many times the
request reaches the server. The scenarios below are the ways it actually
reaches the server more than once: a double-click, a retry after a dropped
connection, two tabs racing.
"""
from __future__ import annotations

import concurrent.futures

import pytest

from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture()
def service(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


def _count(service, session="demo"):
    return len(service.status(session).get("tasks", []))


# -- the duplicate-submission scenarios --------------------------------------

def test_double_click_creates_one_task(service):
    """Two clicks of one button, same key, same payload."""
    first = service.create_task("t", "do the thing", session="demo", request_key="k1")
    second = service.create_task("t", "do the thing", session="demo", request_key="k1")

    assert first["task_id"] == second["task_id"]
    assert first.get("deduplicated") is False
    assert second["deduplicated"] is True
    assert _count(service) == 1


def test_network_retry_returns_the_original_task(service):
    """The first attempt succeeded server-side; the response never arrived."""
    first = service.create_task("t", "p", session="demo", request_key="k2")
    retry = service.create_task("t", "p", session="demo", request_key="k2")
    assert retry["task_id"] == first["task_id"]
    assert retry["deduplicated"] is True
    assert _count(service) == 1


def test_a_replay_reports_the_current_state_not_the_original(service):
    """A retry arriving after the task moved on must describe the task as it
    is now -- a stale echo of the first response would tell the browser the
    task is still queued when it is running."""
    first = service.create_task("t", "p", session="demo", request_key="k3")
    service.store.transition_task(first["task_id"], "PRECHECK", event_type="TEST")
    replay = service.create_task("t", "p", session="demo", request_key="k3")
    assert replay["task_status"] == "PRECHECK"


def test_a_new_submission_uses_a_new_key_and_creates_a_new_task(service):
    """Refresh after an accepted submit: the browser clears the key, so the
    next submission is a real one rather than a replay."""
    first = service.create_task("t", "first", session="demo", request_key="k4")
    second = service.create_task("t", "second", session="demo", request_key="k5")
    assert first["task_id"] != second["task_id"]
    assert _count(service) == 2


def test_concurrent_submissions_with_one_key_produce_one_task(service):
    """Two tabs, or a click racing its own retry. The loser of the insert race
    returns the winner's task, which is the correct answer to the question
    that was asked, not an error."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(
            lambda _: service.create_task("t", "p", session="demo", request_key="race"),
            range(8)))

    ids = {r["task_id"] for r in results}
    assert len(ids) == 1, f"one key produced {len(ids)} tasks"
    assert _count(service) == 1
    assert sum(1 for r in results if not r.get("deduplicated")) == 1, \
        "exactly one caller should see a fresh accept"


# -- same key, different payload --------------------------------------------

def test_same_key_conflicting_payload_still_creates_only_one_task(service):
    """Idempotency is about the KEY. A changed payload must not become a
    second task -- that is the duplicate this exists to prevent."""
    first = service.create_task("t", "original prompt", session="demo", request_key="k6")
    second = service.create_task("t", "DIFFERENT prompt", session="demo", request_key="k6")
    assert second["task_id"] == first["task_id"]
    assert _count(service) == 1


def test_same_key_conflicting_payload_tells_the_caller(service):
    """...but it must not answer a changed request with the old task in
    silence. The task is idempotent; the caller is told."""
    service.create_task("t", "original prompt", session="demo", request_key="k7")
    second = service.create_task("t", "DIFFERENT prompt", session="demo", request_key="k7")

    assert second["payload_conflict"] is True
    assert "prompt" in second["conflicting_fields"]
    assert "different" in second["conflict_detail"]


def test_identical_payload_is_not_reported_as_a_conflict(service):
    service.create_task("t", "p", session="demo", project="proj", request_key="k8")
    second = service.create_task("t", "p", session="demo", project="proj", request_key="k8")
    assert "payload_conflict" not in second


def test_a_changed_title_is_named_as_the_conflicting_field(service):
    service.create_task("first title", "p", session="demo", request_key="k9")
    second = service.create_task("second title", "p", session="demo", request_key="k9")
    assert second["conflicting_fields"] == ["title"]


# -- backward compatibility --------------------------------------------------

def test_a_caller_without_a_key_behaves_exactly_as_before(service):
    """Every existing caller passes no key. Two such calls are two tasks --
    unchanged, because there is nothing to deduplicate against."""
    first = service.create_task("t", "p", session="demo")
    second = service.create_task("t", "p", session="demo")
    assert first["task_id"] != second["task_id"]
    assert _count(service) == 2
    assert first.get("request_key") is None


def test_enqueue_without_a_key_is_unchanged(service):
    a = service.enqueue("demo", "p")
    b = service.enqueue("demo", "p")
    assert a["task_id"] != b["task_id"]


# -- the same guarantees on the enqueue path --------------------------------

def test_enqueue_is_idempotent_under_one_key(service):
    first = service.enqueue("demo", "p", request_key="e1")
    second = service.enqueue("demo", "p", request_key="e1")
    assert first["task_id"] == second["task_id"]
    assert second["deduplicated"] is True
    assert _count(service) == 1


def test_enqueue_reports_a_conflicting_prompt(service):
    service.enqueue("demo", "original", request_key="e2")
    second = service.enqueue("demo", "changed", request_key="e2")
    assert second["payload_conflict"] is True
    assert "prompt" in second["conflicting_fields"]
