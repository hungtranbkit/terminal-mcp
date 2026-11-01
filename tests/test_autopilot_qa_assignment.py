"""A QA claim is bound to the exact destination the prompt was sent to (#106).

Before this slice, any declared verifier could claim an autopilot verify job,
even one the coordinator had sent to a DIFFERENT verifier, the same verifier
name on another node, or before anything was sent at all; and the proof did
not ask whether the claim came from the dispatch the task recorded.

Now the coordinator writes the destination (verifier session, node, dispatch
key) on the verify job itself -- after the task correlation, before the send,
under the lease fence -- and verify_queue lets only that destination claim:

  * an autopilot job is not claimable until it is ASSIGNED;
  * the latest assignment wins, so a re-dispatch (lost node, failed run)
    retires the previous destination;
  * the claim records the assignment key, and the proof requires claim,
    assignment and the task's dispatch key to agree.

MCP callers are still not authenticated, so this binds the claim to the
destination's (session, node) as recorded by the controller; it does not prove
which process typed the call. No claim token ever enters a prompt.
"""
from __future__ import annotations

import threading

import pytest

from terminal_mcp import autopilot as ap
from terminal_mcp import autopilot_view as av
from terminal_mcp.autopilot_policy import VerifierSessionPolicy
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.verify_queue import VerifyQueue

from .test_autopilot import SimulatedCrash
from .test_autopilot_proof import World as ProofWorld
from .test_autopilot_qa_dispatch import PROJECT, QA, SHA_A
from .test_verify_queue import make_running_task

NODE = "hp-qa-node"
NODE_2 = "mac-qa-node"
QA_2 = "ba-qa-2"


# -- canonical verify queue ---------------------------------------------------

@pytest.fixture
def vq(tmp_path):
    store = QueueStore(tmp_path / "q.db")
    return store, VerifyQueue(store)


def test_job_requiring_assignment_is_not_claimable_until_assigned(vq):
    store, verify = vq
    job = verify.ensure_verify_job(make_running_task(store), allowed_verifiers=("qa-1",),
                                   require_assignment=True)
    assert verify.claim_next(verifier="qa-1", capabilities=[], job_id=job.id) is None
    assert verify.assign(job.id, verifier="qa-1", verifier_node_id="n1", dispatch_key="k1",
                         actor="autopilot") is not None
    claimed = verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n1")
    assert claimed is not None and claimed.id == job.id


def test_only_the_assigned_session_on_the_assigned_node_can_claim(vq):
    store, verify = vq
    job = verify.ensure_verify_job(make_running_task(store), require_assignment=True)
    verify.assign(job.id, verifier="qa-1", verifier_node_id="n1", dispatch_key="k1", actor="ap")
    assert verify.claim_next(verifier="qa-2", capabilities=[], verifier_node_id="n1") is None
    assert verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n2") is None
    assert verify.claim_next(verifier="qa-1", capabilities=[]) is None   # node not named
    claimed = verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n1")
    assert claimed.id == job.id
    claim_entry = [h for h in claimed.history if h["event"] == "CLAIMED"][-1]
    assert claim_entry["assignment_key"] == "k1"


def test_a_new_assignment_retires_the_previous_destination(vq):
    store, verify = vq
    job = verify.ensure_verify_job(make_running_task(store), require_assignment=True)
    verify.assign(job.id, verifier="qa-1", verifier_node_id="n1", dispatch_key="k1", actor="ap")
    verify.assign(job.id, verifier="qa-1", verifier_node_id="n2", dispatch_key="k2", actor="ap")
    assert verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n1") is None
    assert verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n2").id == job.id


def test_assign_is_idempotent_and_refused_once_claimed(vq):
    store, verify = vq
    job = verify.ensure_verify_job(make_running_task(store), require_assignment=True)
    for _ in range(3):
        verify.assign(job.id, verifier="qa-1", verifier_node_id="n1", dispatch_key="k1", actor="ap")
    assert [h["event"] for h in verify.get(job.id).history].count("ASSIGNED") == 1
    verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n1")
    assert verify.assign(job.id, verifier="qa-2", verifier_node_id="n1", dispatch_key="k2",
                         actor="ap") is None
    assert verify.get(job.id).verifier == "qa-1"


def test_handoff_cannot_move_an_assigned_claim_to_another_destination(vq):
    store, verify = vq
    job = verify.ensure_verify_job(make_running_task(store), require_assignment=True)
    verify.assign(job.id, verifier="qa-1", verifier_node_id="n1", dispatch_key="k1", actor="ap")
    held = verify.claim_next(verifier="qa-1", capabilities=[], verifier_node_id="n1")
    assert verify.handoff(job.id, held.claim_token, to_verifier="qa-2", to_node_id="n1",
                          reason="spoof") is None
    assert verify.get(job.id).verifier == "qa-1"


def test_ordinary_jobs_keep_the_open_claim(vq):
    store, verify = vq
    job = verify.ensure_verify_job(make_running_task(store))
    assert verify.claim_next(verifier="anyone", capabilities=[]).id == job.id


# -- autopilot wiring -----------------------------------------------------------

class World(ProofWorld):
    """QA world whose verifier probe reports the node each session runs on."""

    def __init__(self, tmp_path, *, nodes=None, **kw):
        self.nodes = dict(nodes or {QA: NODE})
        super().__init__(tmp_path, verifier_probe=self.node_probe, **kw)

    def node_probe(self, session):
        node = self.nodes.get(session)
        return {"available": node is not None, "node_id": node,
                "reason": None if node else "offline"}

    def claim_as(self, session, node, job_id):
        return self.verify.claim_next(verifier=session, capabilities=("python",),
                                      project_id=PROJECT, verifier_node_id=node, job_id=job_id)

    def assignments(self, job_id):
        return [h for h in self.verify.get(job_id).history if h["event"] == "ASSIGNED"]

    def pass_as(self, session, node, job_id, commit=SHA_A):
        job = self.claim_as(session, node, job_id)
        assert job is not None
        result = self.verify.complete(job.id, job.claim_token, evidence={
            "command": "pytest -q", "exit_code": 0, "tests_passed": 5, "tests_failed": 0,
            "commit_sha": commit})
        assert result["ok"], result
        return job


def test_coordinator_assigns_the_destination_before_the_send(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.worker_done(a)
    job = w.verify.open_job_for_task(a)
    meta = w.qa_meta(a)
    [assigned] = w.assignments(job.id)
    assert (assigned["verifier"], assigned["verifier_node_id"], assigned["dispatch_key"]) == \
        (QA, NODE, meta["key"])
    assert meta["verifier_node_id"] == NODE
    [(_, text, _)] = w.dispatcher.qa_sends()
    assert f'verifier_node_id="{NODE}"' in text and "claim_token" not in text


def test_foreign_and_wrong_node_claims_are_refused_and_the_destination_proves(tmp_path):
    w = World(tmp_path, verifiers=(VerifierSessionPolicy(QA, ("python",)),
                                   VerifierSessionPolicy(QA_2, ("python",))),
              nodes={QA: NODE, QA_2: NODE})
    a = w.add("A")
    w.worker_done(a)
    job = w.verify.open_job_for_task(a)
    sent_to = w.qa_meta(a)["verifier"]
    other = QA_2 if sent_to == QA else QA
    assert w.claim_as(other, NODE, job.id) is None          # declared, but not the destination
    assert w.claim_as(sent_to, NODE_2, job.id) is None      # destination name, wrong node
    assert w.status(a) == "VERIFYING"
    w.pass_as(sent_to, NODE, job.id)
    proof = w.task_view(a)["proof"]
    assert proof["trusted"] is True and proof["verifier_node_id"] == NODE


def test_restart_between_assignment_and_send_sends_once_and_assigns_once(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.dispatcher.crash_before_persist_for.add(QA)
    with pytest.raises(SimulatedCrash):
        w.worker_done(a)
    job = w.verify.open_job_for_task(a)
    assert len(w.assignments(job.id)) == 1 and w.dispatcher.qa_sends() == []
    w.dispatcher.crash_before_persist_for.clear()
    w.locks.force_release(PROJECT, ap.COORDINATOR_RESOURCE, actor="test", reason="died")
    for _ in range(3):
        w.autopilot("hp-1-restarted").tick(PROJECT)
    assert len(w.dispatcher.qa_sends()) == 1 and len(w.assignments(job.id)) == 1


def test_lost_node_redispatch_retires_the_old_destination(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.worker_done(a)
    job = w.verify.open_job_for_task(a)
    first_key = w.qa_meta(a)["key"]
    w.dispatcher.set_key(first_key, ap.FAILED, "node lost")
    w.nodes[QA] = NODE_2                         # the session came back on another node
    w.autopilot().tick(PROJECT)
    assert w.qa_meta(a)["key"] != first_key and w.qa_meta(a)["verifier_node_id"] == NODE_2
    assert w.claim_as(QA, NODE, job.id) is None   # the old destination is retired
    w.pass_as(QA, NODE_2, job.id)
    assert w.task_view(a)["proof"]["trusted"] is True


def test_verifier_with_unknown_node_is_not_sent_to(tmp_path):
    w = World(tmp_path)
    w.verifier_probe = lambda session: {"available": True, "node_id": None}
    a = w.add("A")
    report = w.worker_done(a)
    assert w.dispatcher.qa_sends() == []
    assert [i["reason"] for i in report["qa_waiting"]] == [ap.REASON_QA_VERIFIER_UNAVAILABLE]


def test_ambiguous_send_keeps_the_assignment_and_never_resends(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.worker_done(a)
    w.dispatcher.set_key(w.qa_meta(a)["key"], ap.UNCERTAIN, "delivery unconfirmed")
    job = w.verify.open_job_for_task(a)
    for _ in range(3):
        w.autopilot().tick(PROJECT)
    assert len(w.dispatcher.qa_sends()) == 1 and len(w.assignments(job.id)) == 1
    # If the prompt did arrive, the destination (and only it) can still do the job.
    assert w.claim_as(QA, NODE_2, job.id) is None
    w.pass_as(QA, NODE, job.id)
    assert w.status(a) == "COMPLETED"


def test_concurrent_coordinators_assign_once(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.autopilot("hp-0").tick(PROJECT)
    w.dispatcher.set_key(w.store.get_task(a).dispatch_idempotency_key, ap.DONE)
    w.locks.force_release(PROJECT, ap.COORDINATOR_RESOURCE, actor="test", reason="race")
    errors = []

    def run(name):
        try:
            for _ in range(6):
                w.autopilot(name).tick(PROJECT)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"hp-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    job = w.verify.open_job_for_task(a)
    assert errors == [] and len(w.dispatcher.qa_sends()) == 1 and len(w.assignments(job.id)) == 1


# -- proof ---------------------------------------------------------------------

def test_pass_claimed_under_a_different_dispatch_than_the_task_records_is_failing(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.worker_done(a)
    job = w.verify.open_job_for_task(a)
    w.pass_as(QA, NODE, job.id)
    meta = dict(w.store.get_task(a).metadata["autopilot"])
    meta["qa"] = {**meta["qa"], "key": "autopilot-qa:elsewhere:9"}
    w.store.set_internal_metadata(a, "autopilot", meta, event_type="TEST_DRIFT", reason="test")
    proof = w.task_view(a)["proof"]
    assert proof["trusted"] is False and av.PROBLEM_UNASSIGNED_CLAIM in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY
