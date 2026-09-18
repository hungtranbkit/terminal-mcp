"""P0.5 Verify Queue -- state machine, capability routing, lease
ownership, evidence gate, duplicate prevention and restart recovery.

In-process against a real SQLite queue database: no tmux/ConPTY session
and no live node is involved, for the same reason queue_store's own tests
avoid them -- these are persistence and state-machine properties, and
they must be provable without a session to break."""
from __future__ import annotations

import json

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.node_models import NODE_ONLINE, Node
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.verify_queue import (
    ALL_VERIFY_STATUSES,
    FALLBACK_HOLD,
    NEEDS_REWORK,
    VERIFIED_FAIL,
    VERIFIED_PASS,
    VERIFY_BLOCKED,
    VERIFY_CANCELLED,
    VERIFY_CLAIMED,
    VERIFY_PENDING,
    VERIFY_RUNNING,
    InvalidVerifyTransitionError,
    VerifyQueue,
    evidence_verdict,
    match_nodes_by_capability,
    node_capability_set,
)

SESSION = "vq-lane"
GOOD_EVIDENCE = {"command": "pytest -q", "exit_code": 0, "test_results": "42 passed"}


@pytest.fixture
def store(tmp_path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def verify(store) -> VerifyQueue:
    return VerifyQueue(store)


def make_running_task(store: QueueStore, *, session: str = SESSION, title: str = "impl",
                      completion_policy: dict | None = None) -> qs.QueueTask:
    """A task in RUNNING, reached only through real transitions -- never
    by writing a status straight into the row, so the fixture itself can
    never set up a state the state machine would refuse."""
    task_ids = store.set_tasks(session, [{"title": title, "prompt": "do the work",
                                          "completion_policy": completion_policy or {}}])
    task_id = task_ids[0]
    store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHING")
    return store.transition_task(task_id, qs.RUNNING, event_type="RUNNING")


def node(node_id: str, *, capabilities=(), platform="linux", backend="tmux",
         status=NODE_ONLINE) -> Node:
    """A real Node dataclass, not a stand-in -- routing reads real
    attribute names, so a shape mismatch has to fail here."""
    return Node(id=node_id, display_name=node_id, hostname=node_id, endpoint="local",
                status=status, draining=False, last_heartbeat_at=None, latency_ms=None,
                cpu_percent=None, cpu_percent_smoothed=None, load1=None, load5=None,
                load15=None, cpu_count=None, ram_total_bytes=None, ram_used_bytes=None,
                ram_percent=None, ram_percent_smoothed=None, swap_total_bytes=None,
                swap_used_bytes=None, swap_percent=None, swap_percent_smoothed=None,
                disk_total_bytes=None, disk_used_bytes=None, disk_free_bytes=None,
                disk_percent=None, tmux_session_count=None,
                capabilities=tuple(capabilities), platform=platform, session_backend=backend)


# -- 1. State machine ----------------------------------------------------

def test_happy_path_pending_claimed_running_pass(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task, required_capabilities=["python"])
    assert job.status == VERIFY_PENDING
    # The task moved RUNNING -> VERIFYING in the SAME transaction.
    assert store.get_task(task.id).status == qs.VERIFYING

    claimed = verify.claim_next(verifier="verifier-1", capabilities=["python", "git"])
    assert claimed is not None and claimed.id == job.id
    assert claimed.status == VERIFY_CLAIMED and claimed.verifier == "verifier-1"

    running = verify.start(claimed.id, claimed.claim_token)
    assert running.status == VERIFY_RUNNING

    result = verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)
    assert result["ok"] is True
    assert result["job"]["status"] == VERIFIED_PASS
    assert store.get_task(task.id).status == qs.COMPLETED
    # The evidence landed in the task's own pre-existing column too.
    assert store.get_task(task.id).verification_evidence["exit_code"] == 0


def test_p0_5_adds_no_task_status_and_no_task_transition_edge(store, verify):
    """The central backward-compatibility claim, asserted structurally
    rather than trusted to review: every task status a verify outcome can
    produce, and every edge it uses, existed before P0.5."""
    from terminal_mcp.verify_queue import VERIFY_RESULT_TO_TASK_STATUS
    for verify_result, task_status in VERIFY_RESULT_TO_TASK_STATUS.items():
        assert task_status in qs.ALL_STATUSES, f"{verify_result} invented task status {task_status}"
        assert qs.is_valid_transition(qs.VERIFYING, task_status), \
            f"{verify_result} needs a new VERIFYING -> {task_status} edge"
    # ... and the verify vocabulary is disjoint from the task vocabulary,
    # so nothing can be confused for a task status by a careless caller.
    assert not set(ALL_VERIFY_STATUSES) & set(qs.ALL_STATUSES)


def test_invalid_verify_transition_raises(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)
    # A passed job is terminal -- requeue/claim can never reopen it.
    assert verify.requeue(job.id, actor="op", reason="try again") is None
    assert verify.claim_next(verifier="v2", capabilities=[]) is None


def test_cannot_open_a_verify_job_from_a_non_running_task(store, verify):
    ids = store.set_tasks(SESSION, [{"title": "queued", "prompt": "p"}])
    task = store.get_task(ids[0])
    with pytest.raises(InvalidVerifyTransitionError):
        verify.ensure_verify_job(task)


# -- 2. Capability routing ----------------------------------------------

def test_capability_matching_is_AND_not_OR():
    nodes = [node("both", capabilities=["playwright", "dotnet"]),
             node("one", capabilities=["playwright"])]
    matched = match_nodes_by_capability(nodes, ["playwright", "dotnet"])
    assert [n.id for n in matched] == ["both"]


def test_platform_is_a_routing_key_so_a_pre_probe_node_is_still_reachable():
    """dell-5530 reports capabilities=[] (its agent predates P0.3) but
    DOES report platform=windows. Routing on reported platform makes it a
    valid target for `windows` work without redeploying it -- while a
    probed capability it never reported still does not match."""
    win = node("dell-5530", capabilities=[], platform="windows", backend="windows_pty")
    assert node_capability_set(win) == frozenset({"windows", "windows_pty"})
    assert [n.id for n in match_nodes_by_capability([win], ["windows"])] == ["dell-5530"]
    assert match_nodes_by_capability([win], ["dotnet"]) == []
    assert match_nodes_by_capability([win], ["windows", "dotnet"]) == []


def test_offline_nodes_are_not_candidates():
    offline = node("gone", capabilities=["python"], status="offline")
    assert match_nodes_by_capability([offline], ["python"]) == []
    assert [n.id for n in match_nodes_by_capability([offline], ["python"], online_only=False)] == ["gone"]


def test_verifier_without_every_required_capability_claims_nothing(store, verify):
    task = make_running_task(store)
    verify.ensure_verify_job(task, required_capabilities=["dotnet", "windows"])
    assert verify.claim_next(verifier="linux-box", capabilities=["dotnet", "linux"]) is None
    got = verify.claim_next(verifier="win-box", capabilities=["dotnet", "windows", "git"])
    assert got is not None and got.verifier == "win-box"


def test_no_hardcoded_application_capabilities():
    """Routing must stay data-driven: an arbitrary capability a node
    reports (via TERMINAL_MCP_CAPABILITY_PROBES) routes with no code
    change, and no application name is special-cased anywhere."""
    import pathlib as _pathlib
    source = (_pathlib.Path(__file__).parent.parent / "terminal_mcp" / "verify_queue.py").read_text().lower()
    for application_name in ("offlinepos", "novaretail", "wtest"):
        assert application_name not in source, \
            f"{application_name} is named in the routing module -- routing must stay data-driven"
    custom = node("kiosk", capabilities=["webview2", "browser"], platform="windows")
    assert [n.id for n in match_nodes_by_capability([custom], ["webview2", "windows"])] == ["kiosk"]


# -- 3. Lease ownership -------------------------------------------------

def test_every_mutating_verb_requires_the_current_token(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    wrong = "not-the-token"
    assert verify.renew(job.id, wrong) is None
    assert verify.release(job.id, wrong) is None
    assert verify.start(job.id, wrong) is None
    assert verify.handoff(job.id, wrong, to_verifier="v2", reason="x") is None
    assert verify.complete(job.id, wrong, evidence=GOOD_EVIDENCE)["error"] == "NOT_LEASE_HOLDER"
    assert verify.fail(job.id, wrong, result=VERIFIED_FAIL,
                       failure_summary={"headline": "x"})["error"] == "NOT_LEASE_HOLDER"
    # The real holder is unaffected by all that noise.
    assert verify.renew(job.id, claimed.claim_token) is not None


def test_release_returns_the_job_to_the_pool_in_the_same_shape_reconcile_does(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    released = verify.release(job.id, claimed.claim_token, reason="stepping away")
    assert released.status == VERIFY_PENDING
    assert (released.verifier, released.claim_token, released.lease_expires_at) == (None, None, None)
    assert released.claim_count == 1  # not reset -- churn stays visible
    again = verify.claim_next(verifier="v2", capabilities=[])
    assert again.id == job.id and again.verifier == "v2"


def test_expired_lease_is_reconciled_back_to_pending_and_never_lost(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="crashed", capabilities=[], lease_seconds=-5)
    assert claimed.status == VERIFY_CLAIMED
    report = verify.reconcile()
    assert report["leases_expired"] == [job.id]
    recovered = verify.get(job.id)
    assert recovered.status == VERIFY_PENDING and recovered.verifier is None
    assert store.get_task(task.id).status == qs.VERIFYING  # task never lost
    # The dead verifier's token is worthless now.
    assert verify.complete(job.id, claimed.claim_token,
                           evidence=GOOD_EVIDENCE)["error"] == "NOT_LEASE_HOLDER"
    assert verify.claim_next(verifier="fresh", capabilities=[]) is not None


def test_reconcile_is_idempotent(store, verify):
    task = make_running_task(store)
    verify.ensure_verify_job(task)
    verify.claim_next(verifier="crashed", capabilities=[], lease_seconds=-5)
    first = verify.reconcile()
    second = verify.reconcile()
    assert first["expired_count"] == 1
    assert second["expired_count"] == 0 and second["closed_count"] == 0


# -- 4. Handoff ----------------------------------------------------------

def test_handoff_rotates_the_token_so_the_old_verifier_cannot_mutate_the_result(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task, required_capabilities=["python"])
    first = verify.claim_next(verifier="v1", capabilities=["python"])
    handed = verify.handoff(job.id, first.claim_token, to_verifier="v2",
                            reason="v2 has the Windows box")
    assert handed.verifier == "v2" and handed.claim_token != first.claim_token
    assert handed.status == VERIFY_CLAIMED  # still claimed -- never back to the pool

    stale = verify.complete(job.id, first.claim_token, evidence=GOOD_EVIDENCE)
    assert stale["error"] == "NOT_LEASE_HOLDER"
    assert store.get_task(task.id).status == qs.VERIFYING  # the stale write changed nothing

    ok = verify.complete(job.id, handed.claim_token, evidence=GOOD_EVIDENCE)
    assert ok["ok"] is True and ok["job"]["verifier"] == "v2"
    # Audit trail records the handoff, with actor and reason.
    events = [entry["event"] for entry in verify.get(job.id).history]
    assert "HANDOFF" in events


def test_implementer_cannot_verify_its_own_work_when_independence_is_required(store, verify):
    store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}])
    claimed_task = store.claim_next_task(SESSION, claimed_by="worker-A")
    store.transition_task(claimed_task.id, qs.READY, event_type="READY")
    store.transition_task(claimed_task.id, qs.DISPATCHING, event_type="DISPATCHING")
    task = store.transition_task(claimed_task.id, qs.RUNNING, event_type="RUNNING")

    job = verify.ensure_verify_job(task, require_independent=True)
    assert job.implementer == "worker-A"
    assert verify.claim_next(verifier="worker-A", capabilities=[]) is None
    assert verify.claim_next(verifier="worker-B", capabilities=[]) is not None


def test_independence_can_be_waived_explicitly(store, verify):
    store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}])
    claimed_task = store.claim_next_task(SESSION, claimed_by="solo")
    store.transition_task(claimed_task.id, qs.READY, event_type="READY")
    store.transition_task(claimed_task.id, qs.DISPATCHING, event_type="DISPATCHING")
    task = store.transition_task(claimed_task.id, qs.RUNNING, event_type="RUNNING")
    verify.ensure_verify_job(task, require_independent=False)
    assert verify.claim_next(verifier="solo", capabilities=[]) is not None


# -- 5. Evidence gate ----------------------------------------------------

@pytest.mark.parametrize("evidence,expected", [
    ({}, False),
    ("done", False),
    ({"summary": "I verified it and it works"}, False),          # pure self-report
    ({"message": "ok", "note": "all good"}, False),              # still pure self-report
    ({"command": ""}, False),                                     # substantive key, empty value
    ({"command": "pytest", "exit_code": 3}, False),               # contradicted by itself
    ({"test_results": "1 failed", "passed": False}, False),
    ({"command": "pytest", "tests_failed": 2}, False),
    ({"command": "pytest", "exit_code": True}, False),            # bool is not an exit code
    ({"command": "pytest -q", "exit_code": 0}, True),
    ({"completion_marker": "TASK-DONE nonce"}, True),
    ({"commit_sha": "abc123", "summary": "self report ALONGSIDE real evidence"}, True),
])
def test_evidence_gate(evidence, expected):
    accepted, reason = evidence_verdict(evidence)
    assert accepted is expected, reason


def test_pass_is_refused_without_real_evidence_and_the_task_stays_verifying(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    result = verify.complete(job.id, claimed.claim_token,
                             evidence={"summary": "trust me, it works"})
    assert result["ok"] is False and result["error"] == "EVIDENCE_REJECTED"
    assert store.get_task(task.id).status == qs.VERIFYING
    assert verify.get(job.id).status == VERIFY_CLAIMED  # still claimed, still retryable
    # The same verifier can then supply real evidence and pass.
    assert verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)["ok"] is True


def test_fail_requires_a_structured_summary_and_redacts_it(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    assert verify.fail(job.id, claimed.claim_token, result=VERIFIED_FAIL,
                       failure_summary={})["error"] == "FAILURE_SUMMARY_REQUIRED"
    result = verify.fail(job.id, claimed.claim_token, result=VERIFIED_FAIL, failure_summary={
        "headline": "build failed",
        "log": "curl -H 'Authorization: Bearer ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' failed",
        "checks": [{"name": "unit", "status": "fail"}],
    })
    assert result["ok"] is True
    stored = json.dumps(verify.get(job.id).failure_summary)
    assert "ghp_" not in stored and "<REDACTED>" in stored
    assert stored.count("unit") == 1  # structure preserved, not flattened


def test_fail_rejects_an_invalid_result_value(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    for bad in (VERIFIED_PASS, "COMPLETED", "nonsense"):
        assert verify.fail(job.id, claimed.claim_token, result=bad,
                           failure_summary={"headline": "x"})["error"] == "INVALID_RESULT"


def test_needs_rework_lands_in_failed_and_the_existing_retry_path_still_works(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    result = verify.fail(job.id, claimed.claim_token, result=NEEDS_REWORK,
                         failure_summary={"headline": "missing error handling",
                                          "required_changes": ["wrap the parse in try/except"]})
    assert result["ok"] is True
    assert verify.get(job.id).status == NEEDS_REWORK       # distinction kept on the JOB
    assert store.get_task(task.id).status == qs.FAILED     # mapped onto an EXISTING task status
    assert store.retry_task(task.id).status == qs.QUEUED   # pre-existing rework path


def test_verify_blocked_is_recoverable_not_terminal(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    verify.fail(job.id, claimed.claim_token, result=VERIFY_BLOCKED,
                failure_summary={"headline": "no Windows node online"})
    blocked = verify.get(job.id)
    assert blocked.status == VERIFY_BLOCKED and blocked.block_reason == "no Windows node online"
    assert store.get_task(task.id).status == qs.BLOCKED
    assert verify.requeue(job.id, actor="op", reason="node is back").status == VERIFY_PENDING


# -- 6. Duplicate prevention & restart ----------------------------------

def test_ensure_verify_job_is_idempotent_for_the_same_attempt(store, verify):
    task = make_running_task(store)
    first = verify.ensure_verify_job(task, required_capabilities=["python"])
    second = verify.ensure_verify_job(task, required_capabilities=["dotnet"])
    assert first.id == second.id
    assert second.required_capabilities == ("python",)  # the original job, not a re-spec
    assert len(verify.list_jobs(task_id=task.id)) == 1


def test_a_genuine_retry_gets_its_own_job(store, verify):
    task = make_running_task(store)
    job1 = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    verify.fail(job1.id, claimed.claim_token, result=NEEDS_REWORK,
                failure_summary={"headline": "not right yet"})
    store.retry_task(task.id)
    store.transition_task(task.id, qs.DISPATCHING, event_type="DISPATCHING")
    retried = store.transition_task(task.id, qs.RUNNING, event_type="RUNNING")
    assert retried.attempt_count == 2

    job2 = verify.ensure_verify_job(retried)
    assert job2.id != job1.id and job2.attempt == 2
    # The first attempt's verdict and its reasons survive intact.
    assert verify.get(job1.id).status == NEEDS_REWORK
    assert verify.get(job1.id).failure_summary["headline"] == "not right yet"


def test_duplicate_prevention_survives_a_restart(store, verify, tmp_path):
    """The guarantee is a UNIQUE constraint in the file, not an in-memory
    set -- so a fresh process reaches the same conclusion."""
    task = make_running_task(store)
    first = verify.ensure_verify_job(task, required_capabilities=["python"])
    reopened = VerifyQueue(QueueStore(tmp_path / "queue.db"))
    again = reopened.ensure_verify_job(reopened.store.get_task(task.id),
                                       required_capabilities=["python"])
    assert again.id == first.id
    assert len(reopened.list_jobs(task_id=task.id)) == 1


def test_a_claimed_job_survives_a_restart_with_its_lease_intact(store, verify, tmp_path):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    reopened = VerifyQueue(QueueStore(tmp_path / "queue.db"))
    survived = reopened.get(job.id)
    assert survived.status == VERIFY_CLAIMED and survived.verifier == "v1"
    # And the original token still works across the restart.
    assert reopened.renew(job.id, claimed.claim_token) is not None


# -- 7. Fallback when nothing can verify --------------------------------

def test_no_capable_verifier_holds_the_job_visibly_and_never_passes_it(store):
    registry = _FakeRegistry([node("linux-a", capabilities=["python"])])
    verify = VerifyQueue(store, registry=registry)
    task = make_running_task(store)
    job = verify.ensure_verify_job(task, required_capabilities=["dotnet", "windows"],
                                   fallback=FALLBACK_HOLD)
    routing = verify.routability(job)
    assert routing["routable"] is False
    assert "dotnet" in routing["reason"] and routing["candidates"] == []
    # Held, not passed, not dropped.
    assert verify.get(job.id).status == VERIFY_PENDING
    assert store.get_task(task.id).status == qs.VERIFYING
    assert verify.reconcile()["closed_count"] == 0


def test_routability_reports_the_lone_implementer_case(store):
    registry = _FakeRegistry([node("only", capabilities=["python"])])
    verify = VerifyQueue(store, registry=registry)
    store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}])
    claimed_task = store.claim_next_task(SESSION, claimed_by="only")
    store.transition_task(claimed_task.id, qs.READY, event_type="READY")
    store.transition_task(claimed_task.id, qs.DISPATCHING, event_type="DISPATCHING")
    task = store.transition_task(claimed_task.id, qs.RUNNING, event_type="RUNNING")
    job = verify.ensure_verify_job(task, required_capabilities=["python"])
    routing = verify.routability(job)
    assert routing["routable"] is False and "implementer" in routing["reason"]


def test_routability_is_unknown_rather_than_guessed_without_a_registry(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task, required_capabilities=["dotnet"])
    assert verify.routability(job)["routable"] is None


def test_in_session_completion_closes_the_job_with_the_real_evidence(store, verify):
    """The default fallback: the existing in-session marker path completes
    the task while the job is still pending. reconcile() must close the
    job from the evidence the task actually recorded -- not mark it passed
    by fiat, and not leave it orphaned."""
    task = make_running_task(store)
    job = verify.ensure_verify_job(task, required_capabilities=["python"])
    store.mark_completed_with_evidence(task.id, evidence={"completion_marker": "MARKER-ok"})

    report = verify.reconcile()
    assert report["closed_count"] == 1
    closed = verify.get(job.id)
    assert closed.status == VERIFIED_PASS
    assert closed.evidence == {"completion_marker": "MARKER-ok"}
    assert any(entry["event"] == "VERIFIED_PASS" and "in-session" in entry.get("reason", "")
               for entry in closed.history)


def test_a_cancelled_task_cancels_its_open_verify_job(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    store.cancel_task(task.id)
    verify.reconcile()
    assert verify.get(job.id).status == VERIFY_CANCELLED


# -- 8. Traceability & reads --------------------------------------------

def test_trace_links_backlog_task_implementer_branch_verifier_and_evidence(store, verify):
    store.set_tasks(SESSION, [{"title": "t", "prompt": "p"}])
    claimed_task = store.claim_next_task(SESSION, claimed_by="worker-A")
    store.set_task_project(claimed_task.id, "git:github.com/acme/widget")
    store.transition_task(claimed_task.id, qs.READY, event_type="READY")
    store.transition_task(claimed_task.id, qs.DISPATCHING, event_type="DISPATCHING")
    task = store.transition_task(claimed_task.id, qs.RUNNING, event_type="RUNNING")

    job = verify.ensure_verify_job(task, backlog_id="BL-42", branch="feat/x",
                                   commit_sha="deadbeef", required_capabilities=["python"])
    claimed = verify.claim_next(verifier="verifier-1", capabilities=["python"],
                                verifier_node_id="m910")
    verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)

    trace = verify.trace(task.id)
    assert trace["backlog_id"] == "BL-42"
    assert trace["project_id"] == "git:github.com/acme/widget"
    assert trace["implementer"] == "worker-A"
    assert (trace["branch"], trace["commit_sha"]) == ("feat/x", "deadbeef")
    assert trace["task_status"] == qs.COMPLETED
    entry = trace["verify_jobs"][0]
    assert entry["verifier"] == "verifier-1" and entry["verifier_node_id"] == "m910"
    assert entry["status"] == VERIFIED_PASS and entry["evidence"]["exit_code"] == 0
    # Audit: every state change carries actor + time.
    assert all("at" in item and "actor" in item for item in entry["history"])


def test_claim_token_is_never_exposed_by_a_read(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    assert claimed.claim_token  # the claimer gets it...
    assert "claim_token" not in claimed.to_dict()  # ...but a serialised read never does
    assert "claim_token" not in verify.trace(task.id)["verify_jobs"][0]
    assert verify.list_jobs()[0].to_dict(include_token=True)["claim_token"] == claimed.claim_token


def test_stats_and_project_scoped_listing(store, verify):
    task_a = make_running_task(store, title="a")
    store.set_task_project(task_a.id, "proj-1")
    verify.ensure_verify_job(store.get_task(task_a.id))
    task_b = make_running_task(store, session="other-lane", title="b")
    store.set_task_project(task_b.id, "proj-2")
    verify.ensure_verify_job(store.get_task(task_b.id))

    assert verify.stats()[VERIFY_PENDING] == 2
    assert verify.stats(project_id="proj-1")[VERIFY_PENDING] == 1
    assert [j.task_id for j in verify.list_jobs(project_id="proj-2")] == [task_b.id]
    got = verify.claim_next(verifier="v1", capabilities=[], project_id="proj-2")
    assert got.task_id == task_b.id


def test_open_job_for_task_returns_only_an_unanswered_job(store, verify):
    task = make_running_task(store)
    job = verify.ensure_verify_job(task)
    assert verify.open_job_for_task(task.id).id == job.id
    claimed = verify.claim_next(verifier="v1", capabilities=[])
    verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)
    assert verify.open_job_for_task(task.id) is None


# -- 9. Per-task opt-in --------------------------------------------------

def test_verify_policy_is_absent_on_every_ordinary_task(store, verify):
    task = make_running_task(store)
    assert VerifyQueue.verify_policy_for(task) is None
    with_policy = make_running_task(
        store, title="opted-in",
        completion_policy={"verify": {"required_capabilities": ["dotnet"]}})
    assert VerifyQueue.verify_policy_for(with_policy) == {"required_capabilities": ["dotnet"]}


# -- 11. Migration v7 against the real production database --------------

def test_v7_is_additive_on_a_copy_of_the_REAL_production_database(tmp_path):
    """The migration must be safe on the database that actually exists.

    Written as an INVARIANT, not a snapshot: it captures whatever the real
    rows hold and asserts the migration preserves them and invents
    nothing. (The P0.1 version of this test asserted a specific value that
    real work later changed -- the same mistake is not repeated here.)"""
    import shutil
    import sqlite3
    from pathlib import Path
    prod = Path.home() / ".local" / "state" / "terminal-mcp" / "queue.db"
    if not prod.exists():
        pytest.skip("no real queue.db on this host")
    copy = tmp_path / "prod.db"
    shutil.copy(prod, copy)

    before = sqlite3.connect(copy)
    rows_before = {r[0]: r[1:] for r in before.execute(
        "SELECT id, status, session, attempt_count, project_id FROM queue_tasks")}
    events_before = before.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0]
    before.close()

    QueueStore(copy)

    after = sqlite3.connect(copy)
    rows_after = {r[0]: r[1:] for r in after.execute(
        "SELECT id, status, session, attempt_count, project_id FROM queue_tasks")}
    assert rows_after == rows_before, "v7 altered an existing task row"
    assert after.execute("SELECT COUNT(*) FROM queue_events").fetchone()[0] == events_before
    # A migration must never conjure verification work for existing tasks.
    assert after.execute("SELECT COUNT(*) FROM verify_jobs").fetchone()[0] == 0
    # The duplicate-prevention primitive is really in the file, not just
    # in the Python that writes to it.
    schema = after.execute("SELECT sql FROM sqlite_master WHERE name = 'verify_jobs'").fetchone()[0]
    assert "UNIQUE (task_id, attempt)" in schema
    after.close()

    QueueStore(copy)  # re-applying is a no-op
    again = sqlite3.connect(copy)
    assert again.execute("SELECT COUNT(*) FROM queue_tasks").fetchone()[0] == len(rows_before)
    again.close()


class _FakeRegistry:
    def __init__(self, nodes):
        self._nodes = nodes

    def list(self):
        return list(self._nodes)


# -- 10. Engine integration ---------------------------------------------
#
# The opt-in is per TASK, so the property that matters most is a negative
# one: wiring a VerifyQueue into the engine must change NOTHING for a task
# that never asked for external verification.

from terminal_mcp.coordinator import CoordinatorGate, RepoEvidence  # noqa: E402
from terminal_mcp.queue_engine import QueueEngine  # noqa: E402
from tests.test_queue_engine import FakeOps  # noqa: E402


def _engine(store, ops, verify=None) -> QueueEngine:
    gate = CoordinatorGate(evidence_collector=lambda cwd: RepoEvidence(
        branch="main", head="x", clean=True, status_lines=()))
    return QueueEngine(store, ops, coordinator=gate, verify_queue=verify)


def _run_to_running(engine, ops, store, session, *, completion_policy=None):
    (task_id,) = store.set_tasks(session, [{"prompt": "please do the real work carefully",
                                            "completion_policy": completion_policy or {}}])
    ops.set_status(session, {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    for _ in range(6):
        engine.tick(session)
        if store.get_task(task_id).status == qs.RUNNING:
            break
    assert store.get_task(task_id).status == qs.RUNNING
    return task_id


def test_a_task_without_a_verify_policy_is_completely_unaffected(store, verify):
    """The backward-compatibility guarantee, end to end: same engine, a
    VerifyQueue wired in, and a task that never opted in still completes
    through the ordinary in-session marker path with no job created."""
    ops = FakeOps()
    engine = _engine(store, ops, verify)
    task_id = _run_to_running(engine, ops, store, "lane-plain")
    task = store.get_task(task_id)
    nonce = store.ensure_verification_nonce(task_id)

    ops.set_status("lane-plain", {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"})
    engine.tick("lane-plain")
    assert store.get_task(task_id).status == qs.VERIFYING
    assert verify.list_jobs() == []  # nothing was created

    ops.set_capture("lane-plain", {"output": _marker(task_id, task.attempt_count, nonce)})
    engine.tick("lane-plain")
    assert store.get_task(task_id).status == qs.COMPLETED
    assert verify.list_jobs() == []


def test_engine_creates_a_verify_job_when_the_task_policy_asks_for_one(store, verify):
    ops = FakeOps()
    engine = _engine(store, ops, verify)
    task_id = _run_to_running(engine, ops, store, "lane-opt-in", completion_policy={
        "verify": {"required_capabilities": ["dotnet", "windows"], "fallback": "hold"}})

    result = engine.tick("lane-opt-in")
    assert result.action == "VERIFY_REQUESTED"
    job = verify.open_job_for_task(task_id)
    assert job is not None
    assert job.required_capabilities == ("dotnet", "windows")
    assert store.get_task(task_id).status == qs.VERIFYING


def test_hold_fallback_refuses_in_session_completion_even_with_a_valid_marker(store, verify):
    """The point of demanding an independent verifier is not to accept the
    implementer's own word when none is available."""
    ops = FakeOps()
    engine = _engine(store, ops, verify)
    task_id = _run_to_running(engine, ops, store, "lane-hold", completion_policy={
        "verify": {"required_capabilities": ["dotnet"], "fallback": "hold"}})
    task = store.get_task(task_id)
    nonce = store.ensure_verification_nonce(task_id)
    engine.tick("lane-hold")

    ops.set_capture("lane-hold", {"output": _marker(task_id, task.attempt_count, nonce)})
    result = engine.tick("lane-hold")
    assert result.action == "AWAITING_EXTERNAL_VERIFICATION"
    assert store.get_task(task_id).status == qs.VERIFYING  # held, not passed

    # An independent verifier with the right capability finishes the job.
    job = verify.open_job_for_task(task_id)
    claimed = verify.claim_next(verifier="win-verifier", capabilities=["dotnet", "windows"])
    assert claimed.id == job.id
    assert verify.complete(job.id, claimed.claim_token, evidence=GOOD_EVIDENCE)["ok"] is True
    assert store.get_task(task_id).status == qs.COMPLETED


def test_in_session_fallback_still_completes_while_a_job_is_pending(store, verify):
    """fallback="in_session" is the DEFAULT and keeps today's behaviour:
    the marker path completes the task, and reconcile then closes the job
    from that same real evidence rather than orphaning it."""
    ops = FakeOps()
    engine = _engine(store, ops, verify)
    task_id = _run_to_running(engine, ops, store, "lane-soft", completion_policy={
        "verify": {"required_capabilities": ["dotnet"], "fallback": "in_session"}})
    task = store.get_task(task_id)
    nonce = store.ensure_verification_nonce(task_id)
    engine.tick("lane-soft")
    job = verify.open_job_for_task(task_id)
    assert job is not None

    ops.set_capture("lane-soft", {"output": _marker(task_id, task.attempt_count, nonce)})
    engine.tick("lane-soft")
    assert store.get_task(task_id).status == qs.COMPLETED

    verify.reconcile()
    closed = verify.get(job.id)
    assert closed.status == VERIFIED_PASS
    assert "completion_marker" in closed.evidence


def test_repeated_ticks_never_create_a_second_verify_job(store, verify):
    ops = FakeOps()
    engine = _engine(store, ops, verify)
    task_id = _run_to_running(engine, ops, store, "lane-dup", completion_policy={
        "verify": {"required_capabilities": [], "fallback": "hold"}})
    for _ in range(5):
        engine.tick("lane-dup")
    assert len(verify.list_jobs(task_id=task_id)) == 1


def test_an_engine_without_a_verify_queue_ignores_the_policy_entirely(store, verify):
    """A node running an older engine must not strand an opted-in task:
    with no VerifyQueue wired, the task takes the ordinary path."""
    ops = FakeOps()
    engine = _engine(store, ops, verify=None)
    task_id = _run_to_running(engine, ops, store, "lane-noverify", completion_policy={
        "verify": {"required_capabilities": ["dotnet"], "fallback": "hold"}})
    task = store.get_task(task_id)
    nonce = store.ensure_verification_nonce(task_id)
    engine.tick("lane-noverify")
    assert store.get_task(task_id).status == qs.VERIFYING
    ops.set_capture("lane-noverify", {"output": _marker(task_id, task.attempt_count, nonce)})
    engine.tick("lane-noverify")
    assert store.get_task(task_id).status == qs.COMPLETED


def _marker(task_id: str, attempt: int, nonce: str) -> str:
    """The same structured completion marker queue_engine already
    verifies -- the literal shape test_queue_engine.py uses, so these
    tests exercise the REAL in-session evidence path rather than a
    convenient stand-in for it."""
    return (f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 task_id={task_id} "
            f"attempt={attempt} nonce={nonce} status=completion_candidate "
            f"summary_sha256=deadbeef###")
