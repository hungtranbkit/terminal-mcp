"""Only a DECLARED verifier can verify a project's work (#106 P0 hardening).

`claim_next` without `project_id` claims across projects, and verifier names
are self-declared on the MCP surface. Before this slice, an autopilot QA job
could be claimed by any capable verifier on the box (another project's QA
session, a polling verifier) and its PASS completed the task, unblocked the
dependants and read as a TRUSTED proof, because the proof only checked that
the verifier was not the implementer.

Now the job records the project's verifier allowlist at creation (same place
as `require_commit_evidence`), claim and handoff honour it, and the proof
treats an undeclared verifier or an implementer lane as a failing proof.
"""
from __future__ import annotations

import dataclasses

from terminal_mcp import autopilot as ap
from terminal_mcp import autopilot_view as av
from terminal_mcp.verify_queue import VerifyQueue

from .test_autopilot_proof import GOOD, PROJECT, World
from .test_autopilot_qa_dispatch import LANE, QA, SHA_A
from .test_verify_queue import make_running_task

OTHER_QA = "other-project-qa"
LANE_2 = "ba-work-2"


# -- canonical verify queue ---------------------------------------------------

def test_job_with_allowed_verifiers_is_never_claimed_by_another_verifier(tmp_path):
    from terminal_mcp.queue_store import QueueStore
    store = QueueStore(tmp_path / "q.db")
    verify = VerifyQueue(store)
    job = verify.ensure_verify_job(make_running_task(store), allowed_verifiers=("qa-1",))
    assert verify.claim_next(verifier="intruder", capabilities=[]) is None
    assert verify.claim_next(verifier="intruder", capabilities=[], job_id=job.id) is None
    claimed = verify.claim_next(verifier="qa-1", capabilities=[])
    assert claimed is not None and claimed.id == job.id


def test_handoff_to_an_undeclared_verifier_is_refused_and_the_holder_keeps_it(tmp_path):
    from terminal_mcp.queue_store import QueueStore
    store = QueueStore(tmp_path / "q.db")
    verify = VerifyQueue(store)
    job = verify.ensure_verify_job(make_running_task(store), allowed_verifiers=("qa-1", "qa-2"))
    held = verify.claim_next(verifier="qa-1", capabilities=[])
    assert verify.handoff(job.id, held.claim_token, to_verifier="intruder",
                          reason="spoof") is None
    assert verify.get(job.id).verifier == "qa-1"
    moved = verify.handoff(job.id, held.claim_token, to_verifier="qa-2", reason="rebalance")
    assert moved is not None and moved.verifier == "qa-2"


def test_job_without_an_allowlist_keeps_the_old_open_claim(tmp_path):
    from terminal_mcp.queue_store import QueueStore
    store = QueueStore(tmp_path / "q.db")
    verify = VerifyQueue(store)
    job = verify.ensure_verify_job(make_running_task(store))
    assert verify.claim_next(verifier="anyone", capabilities=[]).id == job.id


# -- autopilot wiring -----------------------------------------------------------

def test_autopilot_qa_job_cannot_be_claimed_by_another_projects_verifier(tmp_path):
    w = World(tmp_path)
    a = w.add("A")
    w.worker_done(a)
    job = w.verify.open_job_for_task(a)
    created = job.history[0]
    assert created["allowed_verifiers"] == [QA]
    # A capable verifier from elsewhere polling without a project filter.
    assert w.verify.claim_next(verifier=OTHER_QA, capabilities=("python",)) is None
    assert w.verify.claim_next(verifier=OTHER_QA, capabilities=("python",),
                               job_id=job.id) is None
    assert w.status(a) == "VERIFYING"
    w.qa.act(passed=True)
    assert w.task_view(a)["proof"]["trusted"] is True


# -- proof ---------------------------------------------------------------------

def _passed_by(w, verifier):
    """A project task verified outside the autopilot's routing (a manually
    opened job with no allowlist), so the proof itself must judge it."""
    task = w.running("M")
    job = w.verify.ensure_verify_job(task, commit_sha=SHA_A)
    held = w.verify.claim_next(verifier=verifier, capabilities=[], job_id=job.id)
    assert held is not None
    assert w.verify.complete(job.id, held.claim_token,
                             evidence={**GOOD, "commit_sha": SHA_A})["ok"]
    return task.id


def test_pass_from_an_undeclared_verifier_is_a_failing_proof(tmp_path):
    w = World(tmp_path)
    m = _passed_by(w, OTHER_QA)
    proof = w.task_view(m)["proof"]
    assert proof["kind"] == av.PROOF_VERIFIED_PASS and proof["trusted"] is False
    assert av.PROBLEM_UNDECLARED_VERIFIER in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY
    assert m in w.tower.snapshot(PROJECT)["readiness_detail"]["CODE_READY"]["failed"]


def test_pass_from_another_implementer_lane_is_a_failing_proof(tmp_path):
    w = World(tmp_path)
    [project] = w.policy.projects
    w.policy = ap.AutopilotPolicy(enabled=True, projects=(
        dataclasses.replace(project, lanes=(LANE, LANE_2)),))
    w.tower.policy = w.policy
    m = _passed_by(w, LANE_2)
    proof = w.task_view(m)["proof"]
    assert proof["trusted"] is False
    assert av.PROBLEM_VERIFIER_IS_LANE in proof["problems"]
    assert w.readiness()["CODE_READY"] == av.NOT_READY


def test_pass_from_the_declared_verifier_outside_autopilot_routing_is_trusted(tmp_path):
    w = World(tmp_path)
    m = _passed_by(w, QA)
    assert w.task_view(m)["proof"]["trusted"] is True
