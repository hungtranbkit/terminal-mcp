"""Trusted QA proof in the Control Tower snapshot (issue #106).

CODE_READY may only rest on an INDEPENDENT VERIFIED_PASS for the task's
current attempt, at an exact commit that every record agrees on, with the
task's stored evidence identical to what the verifier submitted. Everything
else is shown for what it is:

  * a worker's own completion (a terminal receipt), or a verify job the
    reconciler closed with no verifier, is TERMINAL_RECEIPT -> UNKNOWN;
  * missing commit provenance -> UNKNOWN;
  * a self-verified job, mismatched commits or evidence that differs from the
    verifier's -> NOT_READY (the proof exists and fails);
  * DEVICE_QA reads only a trusted proof that names its device; RELEASE_READY
    stays UNKNOWN unless both are READY and every outcome was accepted.
"""
from __future__ import annotations

import json

from terminal_mcp import autopilot as ap
from terminal_mcp import autopilot_view as av
from terminal_mcp import queue_store as qs
from terminal_mcp.event_bus import EventBus
from terminal_mcp.event_wiring import build_queue_event_sink, build_verify_event_sink
from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.outcomes import OutcomeStore
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.verify_queue import VerifyQueue

from .test_autopilot_qa_dispatch import LANE, QA, SHA_A, SHA_B, Dispatcher, Verifier
from .test_autopilot_qa_dispatch import World as QAWorld

PROJECT = "synthetic/brandagent-staging"
OTHER = "synthetic/second-project"
GOOD = {"command": "pytest -q", "exit_code": 0, "tests_passed": 4, "tests_failed": 0}


class World(QAWorld):
    def __init__(self, tmp_path, **kw):
        super().__init__(tmp_path, **kw)
        # Event-wired like the controller, so notifications can be read.
        self.bus = EventBus(tmp_path / "wired-events.db")
        self.store = QueueStore(tmp_path / "wired.db", event_sink=build_queue_event_sink(self.bus))
        self.verify = VerifyQueue(self.store, event_sink=build_verify_event_sink(self.bus))
        self.outcomes = OutcomeStore(self.store)
        self.locks = ResourceLockStore(tmp_path / "wired-locks.db")
        self.dispatcher = Dispatcher()
        self.qa = Verifier(self)
        self.clock = {"now": 1_000_000.0}
        self.loop = ap.AutopilotLoop(self.autopilot(), clock=lambda: self.clock["now"])
        self.tower = av.ControlTower(store=self.store, verify=self.verify, outcomes=self.outcomes,
                                     bus=self.bus, projects=None, policy=self.policy,
                                     loop=self.loop, clock=lambda: self.clock["now"])

    def passed(self, title="A", **evidence):
        task_id = self.add(title)
        self.worker_done(task_id)
        _, text, key = self.dispatcher.qa_sends()[-1]
        job = self.qa.claim(text)
        result = self.verify.complete(job.id, job.claim_token,
                                      evidence={**GOOD, "commit_sha": SHA_A, **evidence})
        assert result["ok"], result
        self.dispatcher.set_key(key, ap.DONE)
        return task_id, job

    def running(self, title, lane=LANE, project=PROJECT):
        """A task RUNNING on `lane`, reached only through real transitions."""
        [task_id] = self.store.append_tasks(lane, [{"title": title, "prompt": "Implement it.",
                                                    "project_id": project}])
        task = self.store.claim_next_task(lane, claimed_by=lane, lease_seconds=300)
        assert task.id == task_id
        self.store.transition_task(task_id, qs.READY, event_type="READY")
        self.store.transition_task(task_id, qs.DISPATCHING, event_type="DISPATCHING")
        return self.store.mark_running_with_evidence(task_id, evidence={
            "accepted": True, "signal": "explicit_running_signal"})

    def self_completed(self, title="R", **evidence):
        """The worker completes its own task in-session: a terminal receipt."""
        task = self.running(title)
        self.store.transition_task(task.id, qs.VERIFYING, event_type="VERIFY_REQUESTED")
        self.store.mark_completed_with_evidence(task.id, evidence={**GOOD, "commit_sha": SHA_A,
                                                                   **evidence})
        return task.id

    def task_view(self, task_id):
        return {t["id"]: t for t in self.tower.snapshot(PROJECT)["tasks"]}[task_id]

    def readiness(self):
        return self.tower.snapshot(PROJECT)["readiness"]

    def tamper_task_evidence(self, task_id, evidence):
        with self.store._connection() as connection:
            connection.execute("UPDATE queue_tasks SET verification_evidence = ? WHERE id = ?",
                               (json.dumps(evidence), task_id))


# -- the trusted path -------------------------------------------------------

def test_independent_pass_at_the_observed_commit_is_trusted(tmp_path):
    w = World(tmp_path)
    a, job = w.passed()
    proof = w.task_view(a)["proof"]
    assert proof == {**proof, "kind": av.PROOF_VERIFIED_PASS, "trusted": True, "problems": [],
                     "job_id": job.id, "attempt": 1, "verifier": QA, "implementer": LANE,
                     "commit_sha": SHA_A}
    assert proof["verified_at"]
    assert w.readiness()["CODE_READY"] == av.READY


# -- terminal receipts are not verification ------------------------------------

def test_worker_self_completion_is_a_terminal_receipt_not_code_ready(tmp_path):
    w = World(tmp_path)
    r = w.self_completed()
    proof = w.task_view(r)["proof"]
    assert proof["kind"] == av.PROOF_TERMINAL_RECEIPT and proof["trusted"] is False
    assert av.PROBLEM_NO_INDEPENDENT_PASS in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.UNKNOWN


def test_reconciler_closed_job_without_a_verifier_is_not_trusted(tmp_path):
    w = World(tmp_path)
    task = w.running("R")
    job = w.verify.ensure_verify_job(task, commit_sha=SHA_A)
    w.store.mark_completed_with_evidence(task.id, evidence={**GOOD, "commit_sha": SHA_A})
    w.verify.reconcile(project_id=PROJECT)
    assert w.verify.get(job.id).status == "VERIFIED_PASS" and w.verify.get(job.id).verifier is None
    proof = w.task_view(task.id)["proof"]
    assert proof["kind"] == av.PROOF_TERMINAL_RECEIPT
    assert av.PROBLEM_NO_VERIFIER in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.UNKNOWN


def test_completion_receipt_is_not_announced_as_a_verified_milestone(tmp_path):
    w = World(tmp_path)
    w.self_completed()
    assert w.tower.notifications(PROJECT, since_seq=0)["notifications"] == []
    a, job = w.passed()
    kinds = [(n["kind"], n["task_id"]) for n in w.tower.notifications(PROJECT)["notifications"]]
    assert kinds == [("MILESTONE_VERIFIED", a)]


# -- failing proofs ----------------------------------------------------------

def test_self_verified_job_fails_independence(tmp_path):
    w = World(tmp_path)
    task = w.running("S")
    job = w.verify.ensure_verify_job(task, commit_sha=SHA_A, require_independent=False)
    claimed = w.verify.claim_next(verifier=LANE, job_id=job.id)
    assert claimed is not None  # the queue allows it when independence was waived
    assert w.verify.complete(job.id, claimed.claim_token,
                             evidence={**GOOD, "commit_sha": SHA_A})["ok"]
    proof = w.task_view(task.id)["proof"]
    assert av.PROBLEM_SELF_VERIFIED in proof["problems"] and proof["trusted"] is False
    assert w.readiness()["CODE_READY"] == av.NOT_READY


def test_tampered_task_evidence_cannot_make_code_ready(tmp_path):
    w = World(tmp_path)
    a, _ = w.passed()
    w.tamper_task_evidence(a, {**GOOD, "commit_sha": SHA_A, "tests_passed": 999})
    proof = w.task_view(a)["proof"]
    assert av.PROBLEM_EVIDENCE_MISMATCH in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY


def test_evidence_naming_another_commit_is_a_mismatch(tmp_path):
    w = World(tmp_path)
    a, _ = w.passed()
    tampered = {**GOOD, "commit_sha": SHA_B}
    w.tamper_task_evidence(a, tampered)
    with w.store._connection() as connection:  # both copies rewritten consistently
        connection.execute("UPDATE verify_jobs SET evidence = ? WHERE task_id = ?",
                           (json.dumps(tampered), a))
    proof = w.task_view(a)["proof"]
    assert av.PROBLEM_COMMIT_MISMATCH in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY


def test_job_commit_that_differs_from_the_observed_task_head_is_a_mismatch(tmp_path):
    w = World(tmp_path)
    a, _ = w.passed()
    meta = dict(w.store.get_task(a).metadata["autopilot"])
    meta["qa"] = {**meta["qa"], "commit_sha": SHA_B}
    w.store.set_internal_metadata(a, "autopilot", meta, event_type="TEST")
    assert av.PROBLEM_COMMIT_MISMATCH in w.task_view(a)["proof"]["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY


def test_incomplete_evidence_is_rejected(tmp_path):
    w = World(tmp_path)
    a, _ = w.passed()
    w.tamper_task_evidence(a, {"summary": "done"})
    with w.store._connection() as connection:
        connection.execute("UPDATE verify_jobs SET evidence = ? WHERE task_id = ?",
                           (json.dumps({"summary": "done"}), a))
    assert av.PROBLEM_EVIDENCE_REJECTED in w.task_view(a)["proof"]["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY


def test_pass_without_any_commit_provenance_is_unknown(tmp_path):
    w = World(tmp_path)
    task = w.running("N")
    job = w.verify.ensure_verify_job(task)  # opened with no commit
    claimed = w.verify.claim_next(verifier="human-qa", job_id=job.id)
    assert w.verify.complete(job.id, claimed.claim_token, evidence=GOOD)["ok"]
    proof = w.task_view(task.id)["proof"]
    assert av.PROBLEM_NO_COMMIT in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.UNKNOWN


def test_unfinished_work_is_not_ready_whatever_else_passed(tmp_path):
    w = World(tmp_path)
    w.passed()
    w.add("still queued")
    assert w.readiness()["CODE_READY"] == av.NOT_READY


# -- device and release --------------------------------------------------------

def test_device_qa_needs_a_trusted_proof_naming_the_device(tmp_path):
    w = World(tmp_path)
    w.passed(device_qa={"passed": True, "device": "synthetic-emulator-34"})
    readiness = w.readiness()
    assert readiness["CODE_READY"] == av.READY and readiness["DEVICE_QA"] == av.READY
    assert readiness["RELEASE_READY"] == av.UNKNOWN  # no accepted outcome, no owner approval


def test_device_claim_without_a_device_id_is_unknown(tmp_path):
    w = World(tmp_path)
    w.passed(device_qa={"passed": True})
    assert w.readiness()["DEVICE_QA"] == av.UNKNOWN


def test_device_claim_on_a_terminal_receipt_is_unknown(tmp_path):
    w = World(tmp_path)
    w.self_completed(device_qa={"passed": True, "device": "emulator"})
    readiness = w.readiness()
    assert readiness["DEVICE_QA"] == av.UNKNOWN and readiness["RELEASE_READY"] == av.UNKNOWN


def test_failed_device_qa_is_not_ready(tmp_path):
    w = World(tmp_path)
    w.passed(device_qa={"passed": False, "device": "emulator"})
    readiness = w.readiness()
    assert readiness["DEVICE_QA"] == av.NOT_READY and readiness["RELEASE_READY"] == av.NOT_READY


def test_readiness_explains_itself(tmp_path):
    w = World(tmp_path)
    w.passed()
    r = w.self_completed()
    detail = w.tower.snapshot(PROJECT)["readiness_detail"]
    assert detail["CODE_READY"]["unproven"] == [r]
    assert detail["CODE_READY"]["failed"] == [] and detail["CODE_READY"]["open"] == []


# -- isolation and leakage --------------------------------------------------------

def test_proof_and_snapshot_never_leak_tokens_or_other_projects(tmp_path):
    w = World(tmp_path)
    a, job = w.passed()
    foreign = w.running("foreign", lane="other-lane", project=OTHER)
    other_job = w.verify.ensure_verify_job(foreign, commit_sha=SHA_B)
    held = w.verify.claim_next(verifier="qa-x", job_id=other_job.id)
    text = json.dumps(w.tower.snapshot(PROJECT))
    assert held.claim_token not in text and other_job.id not in text and SHA_B not in text
    assert "claim_token" not in text
