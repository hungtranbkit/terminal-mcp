"""TMCP-HARNESS-001: the old paths that could destroy a harness run.

Two of them, and they are the reason this feature is not purely additive.

RETRY. "Retry" could not tell an infrastructure failure (the machinery
broke, the work is fine) from a product failure (the machinery worked, the
work is wrong), so it did the one thing that is wrong for both: sent the task
back to QUEUED to be executed again from its original prompt. Every hour of
build, every checkpoint, the contract and the whole iteration history were
discarded to re-derive what was already known. A harness run has a different,
correct answer for each case, so a generic retry aimed at a live run is
refused and pointed at the one that does what the caller meant.

DEPLOY. A production deploy of work whose run has not merged would put
unreviewed output in front of users past a gate that exists precisely to stop
that. Test deploys stay open, because that is how the work gets evaluated.

Both are deliberately NARROW. A task with no harness run -- which is every
task in every database that predates this -- behaves exactly as it always
did, and the first test in each section is what holds that.
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_state as state
from terminal_mcp import queue_store as qs
from terminal_mcp.harness_service import HarnessService
from terminal_mcp.harness_store import HarnessStore
from terminal_mcp.queue_service import QueueService

from .test_harness_core import ScriptedRunner, passing_checks

pytestmark = pytest.mark.usefixtures("declared_toolchain")


@pytest.fixture
def store(tmp_path):
    return qs.QueueStore(tmp_path / "queue.db")


@pytest.fixture
def harness(store, tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)

    class Q:
        pass

    queue = Q()
    queue.store = store
    return HarnessService(store=HarnessStore(store.path), queue=queue,
                          repo_root=str(repo), runner=ScriptedRunner(),
                          check_runner=passing_checks)


def _task(store, session="demo"):
    task_id, = store.append_tasks(session, [{"title": "t", "prompt": "build the card"}])
    return task_id


def _to_failed(store, task_id):
    for target in (qs.PRECHECK, qs.READY, qs.DISPATCHING, qs.RUNNING, qs.FAILED):
        if target == qs.RUNNING:
            store.mark_running_with_evidence(
                task_id, evidence={"accepted": True, "signal": "explicit_running_signal"})
        else:
            store.transition_task(task_id, target, event_type="TEST")


# ---------------------------------------------------------------------------
# retry: unchanged for every task the harness does not own
# ---------------------------------------------------------------------------

def test_a_task_with_no_harness_run_retries_exactly_as_it_always_did(store):
    """Every task in every existing database is this case. None may break."""
    task_id = _task(store)
    _to_failed(store, task_id)

    retried = store.retry_task(task_id)

    assert retried.status == qs.QUEUED
    assert retried.dispatch_idempotency_key is None


def test_a_finished_harness_run_does_not_block_a_retry(store, harness):
    """Only a LIVE run owns a task. A cancelled attempt releases it."""
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"])
    harness.cancel(run_id=started["run"]["id"], actor="operator")
    _to_failed(store, task_id)

    assert store.retry_task(task_id).status == qs.QUEUED


# ---------------------------------------------------------------------------
# retry: refused while a run owns the task
# ---------------------------------------------------------------------------

def test_a_generic_retry_will_not_restart_a_live_harness_run(store, harness):
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=2)
    _to_failed(store, task_id)

    with pytest.raises(qs.HarnessRunOwnsTask) as caught:
        store.retry_task(task_id)

    assert caught.value.run["id"] == started["run"]["id"]
    assert store.get_task(task_id).status == qs.FAILED, \
        "the refusal changed nothing; a half-applied retry is worse than none"


def test_the_refusal_names_the_action_that_does_what_was_meant(store, harness):
    task_id = _task(store)
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"], steps=2)
    _to_failed(store, task_id)

    with pytest.raises(qs.HarnessRunOwnsTask) as caught:
        store.retry_task(task_id)

    message = str(caught.value)
    assert "harness_resume" in message
    assert "harness_cancel" in message


def test_the_service_reports_the_refusal_rather_than_raising(store, harness):
    """A caller asked for something reasonable and there is a better action
    available; that is a result, not a 500."""
    queue = QueueService(store)
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=2)
    _to_failed(store, task_id)

    result = queue.retry("demo", task_id)

    assert result["error"] == "HARNESS_RUN_OWNS_TASK"
    assert result["run"]["id"] == started["run"]["id"]
    assert result["use_instead"] == "terminal_turn(action='harness_resume')"


def test_resume_is_what_a_retry_should_have_been(store, harness):
    """The point of the refusal: the correct action preserves the run."""
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=2)
    run_id = started["run"]["id"]
    before = harness.store.require_run(run_id)
    harness.engine().record_infra_failure(run_id, "the node went away")

    resumed = harness.resume(run_id=run_id)

    assert resumed["status"] == "OK"
    assert resumed["contract_id"] == before.contract_id, "same contract"
    assert resumed["iteration"] == before.current_iteration, "same iteration"
    assert resumed["planner_rerun"] is False


# ---------------------------------------------------------------------------
# deploy: production waits for the merge
# ---------------------------------------------------------------------------

def test_a_task_with_no_harness_run_deploys_to_production_as_before(store):
    task_id = _task(store)
    deployed = store.record_deploy(task_id, deploy_state=qs.QueueStore.DEPLOYED_PROD,
                                   actor="operator")
    assert deployed.deploy_state == qs.QueueStore.DEPLOYED_PROD


def test_a_test_deploy_is_never_gated(store, harness):
    """Deploying an in-flight branch to a test target is how the work gets
    evaluated at all. Gating it would make the harness worse at its job."""
    task_id = _task(store)
    harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"], steps=2)

    deployed = store.record_deploy(task_id, deploy_state=qs.QueueStore.DEPLOYED_TEST,
                                   actor="operator")

    assert deployed.deploy_state == qs.QueueStore.DEPLOYED_TEST


def test_production_is_refused_while_the_run_has_not_merged(store, harness):
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=24)
    run = harness.store.require_run(started["run"]["id"])
    assert run.stage == state.MERGE_READY, "reached the gate, and stopped there"

    with pytest.raises(qs.HarnessRunNotMerged) as caught:
        store.record_deploy(task_id, deploy_state=qs.QueueStore.DEPLOYED_PROD,
                            actor="operator")

    assert "not MERGED" in str(caught.value)
    assert store.get_task(task_id).deploy_state != qs.QueueStore.DEPLOYED_PROD


def test_production_opens_once_a_named_approver_has_merged(store, harness):
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            steps=24)
    run_id = started["run"]["id"]

    approved = harness.review(run_id=run_id, approve_merge=True, actor="a-human")
    assert approved["status"] == "OK"
    assert harness.store.require_run(run_id).stage == state.MERGED

    deployed = store.record_deploy(task_id, deploy_state=qs.QueueStore.DEPLOYED_PROD,
                                   actor="operator")
    assert deployed.deploy_state == qs.QueueStore.DEPLOYED_PROD


def test_supervised_cannot_merge_itself_into_a_deployable_state(store, harness):
    """The gate is a NAMED approver, not a mode. SUPERVISED reaching
    MERGE_READY on its own must not be enough to reach production."""
    task_id = _task(store)
    started = harness.start(task_id=task_id, acceptance=["a"], checks=["npm test"],
                            write_authority=policy.SUPERVISED, steps=24)
    run_id = started["run"]["id"]

    refused = harness.review(run_id=run_id, approve_merge=True, actor="engine")

    assert refused["status"] == "FAILED"
    assert refused["error"] == "MERGE_REFUSED"
    assert harness.store.require_run(run_id).stage != state.MERGED
    with pytest.raises(qs.HarnessRunNotMerged):
        store.record_deploy(task_id, deploy_state=qs.QueueStore.DEPLOYED_PROD)


# ---------------------------------------------------------------------------
# the migration map, and the claim it is NOT allowed to make yet
# ---------------------------------------------------------------------------

def test_nothing_has_been_removed():
    """The dangerous moment is six weeks from now, when somebody reads 'the
    Harness owns execution' and deletes a path that was quietly still doing
    the job. Removal needs evidence, and there is none yet."""
    from terminal_mcp import harness_migration as migration

    assert migration.by_verdict(migration.REMOVE) == ()
    assert migration.summary()[migration.REMOVE] == 0


def test_the_harness_does_not_yet_claim_to_be_authoritative():
    """It has executed no real work: no AgentRunner is wired anywhere, so
    every run stops at PLAN_READY. Parity cannot be claimed from that."""
    from terminal_mcp import harness_migration as migration

    assert migration.HARNESS_IS_AUTHORITATIVE is False
    assert migration.NOT_AUTHORITATIVE_BECAUSE.strip()


def test_every_deprecated_path_says_what_would_let_it_go():
    """A DEPRECATE with no exit criteria is a path that gets deleted on
    somebody's confidence rather than on evidence."""
    from terminal_mcp import harness_migration as migration

    for entry in migration.by_verdict(migration.DEPRECATE):
        assert entry.exit_criteria.strip(), entry.module
    # GROUP is not a path to removal -- a grouped subsystem keeps its own
    # job and shares one definition of something with the Harness. Only the
    # ones that still hold a DECISION the Harness now duplicates can ever
    # move on, so only those carry criteria.
    assert any(entry.exit_criteria.strip()
               for entry in migration.by_verdict(migration.GROUP))


def test_the_paths_marked_deprecated_still_work():
    """DEPRECATE means 'superseded and still running', never 'disabled'.
    The generic retry is the one with a behaviour change, and it is narrow:
    refused only where a live run would be destroyed."""
    from terminal_mcp import harness_migration as migration

    deprecated = {entry.module for entry in migration.by_verdict(migration.DEPRECATE)}
    assert "generic retry (queue_store.retry_task)" in deprecated
    assert hasattr(qs.QueueStore, "retry_task"), "not removed"
    assert hasattr(qs.QueueStore, "record_deploy"), "not removed"
