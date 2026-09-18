"""P0.5 Verify Queue -- MCP tool surface. Exercises the exact call path a
real ChatGPT/Claude Code client uses (server.call_tool), for the same
reason test_queue_mcp_tools.py does: driving VerifyQueue directly in
Python cannot catch a tool wrapper that forgot to expose a parameter, or
one that leaks a claim_token it should not.

SAFETY: every session name here is a disposable fixture string."""
from __future__ import annotations

import json

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

GOOD_EVIDENCE = {"command": "pytest -q", "exit_code": 0, "test_results": "42 passed"}


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def rig(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    return build_mcp(queue=queue), queue


def running_task(queue: QueueService, *, session: str = "vq-lane") -> str:
    (task_id,) = queue.store.set_tasks(session, [{"title": "impl", "prompt": "do the work"}])
    queue.store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHING")
    queue.store.transition_task(task_id, qs.RUNNING, event_type="RUNNING")
    return task_id


@pytest.mark.anyio
async def test_full_verify_lifecycle_through_the_real_mcp_tools(rig):
    server, queue = rig
    task_id = running_task(queue)

    requested = await _call(server, "terminal_verify_request", task_id=task_id,
                            required_capabilities=["python"], backlog_id="BL-7",
                            branch="feat/x", commit_sha="cafe1234")
    job_id = requested["job"]["id"]
    assert requested["job"]["status"] == "VERIFY_PENDING"
    assert queue.store.get_task(task_id).status == qs.VERIFYING

    listed = await _call(server, "terminal_verify_list", status="VERIFY_PENDING")
    assert listed["count"] == 1
    # A pending job explains itself -- requirement: a coordinator must be
    # able to read WHY something is not moving.
    assert "routability" in listed["jobs"][0]
    assert listed["stats"]["VERIFY_PENDING"] == 1

    nothing = await _call(server, "terminal_verify_claim", verifier="v1", capabilities=["go"])
    assert nothing["claimed"] is False and "capabilit" in nothing["reason"]

    claimed = await _call(server, "terminal_verify_claim", verifier="v1",
                          capabilities=["python", "git"], verifier_node_id="m910")
    assert claimed["claimed"] is True
    token = claimed["job"]["claim_token"]

    assert (await _call(server, "terminal_verify_start", job_id=job_id,
                        claim_token=token))["started"] is True
    assert (await _call(server, "terminal_verify_renew", job_id=job_id,
                        claim_token=token, lease_seconds=900))["renewed"] is True

    done = await _call(server, "terminal_verify_complete", job_id=job_id,
                       claim_token=token, evidence=GOOD_EVIDENCE)
    assert done["ok"] is True and done["task_status"] == "COMPLETED"
    assert queue.store.get_task(task_id).status == qs.COMPLETED

    trace = await _call(server, "terminal_verify_trace", task_id=task_id)
    assert trace["backlog_id"] == "BL-7" and trace["commit_sha"] == "cafe1234"
    assert trace["verify_jobs"][0]["verifier"] == "v1"


@pytest.mark.anyio
async def test_evidence_gate_is_enforced_at_the_tool_boundary(rig):
    server, queue = rig
    task_id = running_task(queue)
    job_id = (await _call(server, "terminal_verify_request", task_id=task_id))["job"]["id"]
    claimed = await _call(server, "terminal_verify_claim", verifier="v1")
    token = claimed["job"]["claim_token"]

    refused = await _call(server, "terminal_verify_complete", job_id=job_id, claim_token=token,
                          evidence={"summary": "I checked it, looks good"})
    assert refused["ok"] is False and refused["error"] == "EVIDENCE_REJECTED"
    assert queue.store.get_task(task_id).status == qs.VERIFYING

    contradicted = await _call(server, "terminal_verify_complete", job_id=job_id, claim_token=token,
                               evidence={"command": "pytest", "exit_code": 1})
    assert contradicted["error"] == "EVIDENCE_REJECTED" and "exit_code=1" in contradicted["reason"]
    assert queue.store.get_task(task_id).status == qs.VERIFYING


@pytest.mark.anyio
async def test_fail_and_requeue_through_the_tools(rig):
    server, queue = rig
    task_id = running_task(queue)
    job_id = (await _call(server, "terminal_verify_request", task_id=task_id))["job"]["id"]
    token = (await _call(server, "terminal_verify_claim", verifier="v1"))["job"]["claim_token"]

    missing = await _call(server, "terminal_verify_fail", job_id=job_id, claim_token=token,
                          result="VERIFIED_FAIL", failure_summary={})
    assert missing["error"] == "FAILURE_SUMMARY_REQUIRED"

    blocked = await _call(server, "terminal_verify_fail", job_id=job_id, claim_token=token,
                          result="VERIFY_BLOCKED",
                          failure_summary={"headline": "no windows node online"})
    assert blocked["ok"] is True
    assert queue.store.get_task(task_id).status == qs.BLOCKED

    requeued = await _call(server, "terminal_verify_requeue", job_id=job_id, reason="node back")
    assert requeued["requeued"] is True and requeued["job"]["status"] == "VERIFY_PENDING"


@pytest.mark.anyio
async def test_handoff_through_the_tools_invalidates_the_old_token(rig):
    server, queue = rig
    task_id = running_task(queue)
    job_id = (await _call(server, "terminal_verify_request", task_id=task_id))["job"]["id"]
    first = (await _call(server, "terminal_verify_claim", verifier="v1"))["job"]["claim_token"]

    handed = await _call(server, "terminal_verify_handoff", job_id=job_id, claim_token=first,
                         to_verifier="v2", reason="v2 owns the Windows box")
    assert handed["handed_off"] is True
    second = handed["job"]["claim_token"]
    assert second != first

    stale = await _call(server, "terminal_verify_complete", job_id=job_id, claim_token=first,
                        evidence=GOOD_EVIDENCE)
    assert stale["error"] == "NOT_LEASE_HOLDER"
    assert queue.store.get_task(task_id).status == qs.VERIFYING

    ok = await _call(server, "terminal_verify_complete", job_id=job_id, claim_token=second,
                     evidence=GOOD_EVIDENCE)
    assert ok["ok"] is True


@pytest.mark.anyio
async def test_reads_never_expose_a_claim_token(rig):
    server, queue = rig
    task_id = running_task(queue)
    await _call(server, "terminal_verify_request", task_id=task_id)
    await _call(server, "terminal_verify_claim", verifier="v1")

    listed = await _call(server, "terminal_verify_list")
    assert "claim_token" not in listed["jobs"][0]
    trace = await _call(server, "terminal_verify_trace", task_id=task_id)
    assert "claim_token" not in trace["verify_jobs"][0]


@pytest.mark.anyio
async def test_request_is_idempotent_and_refuses_an_ineligible_task(rig):
    server, queue = rig
    task_id = running_task(queue)
    first = await _call(server, "terminal_verify_request", task_id=task_id)
    second = await _call(server, "terminal_verify_request", task_id=task_id)
    assert first["job"]["id"] == second["job"]["id"]

    (queued_id,) = queue.store.set_tasks("vq-other", [{"title": "t", "prompt": "p"}])
    refused = await _call(server, "terminal_verify_request", task_id=queued_id)
    assert refused["error"] == "VERIFY_REQUEST_REFUSED"

    assert (await _call(server, "terminal_verify_request",
                        task_id="no-such-task"))["error"] == "TASK_NOT_FOUND"


@pytest.mark.anyio
async def test_reconcile_through_the_tool_recovers_a_dead_verifier(rig):
    server, queue = rig
    task_id = running_task(queue)
    job_id = (await _call(server, "terminal_verify_request", task_id=task_id))["job"]["id"]
    await _call(server, "terminal_verify_claim", verifier="crashed", lease_seconds=-5)

    report = await _call(server, "terminal_verify_reconcile")
    assert report["leases_expired"] == [job_id]
    reclaimed = await _call(server, "terminal_verify_claim", verifier="fresh")
    assert reclaimed["claimed"] is True and reclaimed["job"]["verifier"] == "fresh"


@pytest.fixture
def anyio_backend():
    return "asyncio"
