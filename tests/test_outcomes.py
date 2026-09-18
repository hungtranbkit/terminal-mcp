"""Orchestration V1 -- the OUTCOME layer.

The behaviour under test that matters most, and the reason this layer is not
just a `parent_task_id` column: **an outcome is NOT done because its children
are done.** Every real project has the failure where five tasks pass, the
tests are green, and the screen is still broken -- because nobody wrote the
task that would have caught it. Rolling an outcome to DONE off its children
would encode that mistake as a feature.
"""
from __future__ import annotations

import pytest

from terminal_mcp import queue_store as qs
from terminal_mcp.outcomes import (
    OUTCOME_AWAITING_ACCEPTANCE,
    OUTCOME_BLOCKED,
    OUTCOME_DONE,
    OUTCOME_IN_PROGRESS,
    OUTCOME_OPEN,
    OutcomeError,
    OutcomeStore,
)
from terminal_mcp.queue_store import QueueStore

PROJECT = "git:github.com/acme/widget"
CRITERIA = ["overview screen renders offline", "no data loss on reconnect"]
GOOD = {"command": "pytest -q", "exit_code": 0, "test_results": "12 passed"}
GOOD2 = {"command": "playwright test", "exit_code": 0, "artifact": "trace.zip"}


@pytest.fixture
def store(tmp_path) -> QueueStore:
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def outcomes(store) -> OutcomeStore:
    return OutcomeStore(store)


def finished_task(store: QueueStore, *, session="lane-a", title="t") -> str:
    (task_id,) = store.set_tasks(session, [{"title": title, "prompt": "p"}],
                                 replace_pending=False)
    for target, event in ((qs.DISPATCHING, "D"), (qs.RUNNING, "R"), (qs.VERIFYING, "V")):
        store.transition_task(task_id, target, event_type=event)
    store.mark_completed_with_evidence(task_id, evidence={"exit_code": 0})
    return task_id


def open_task(store: QueueStore, *, session="lane-a", title="t") -> str:
    (task_id,) = store.set_tasks(session, [{"title": title, "prompt": "p"}],
                                 replace_pending=False)
    return task_id


# -- 1. The central rule -------------------------------------------------

def test_an_outcome_is_not_done_because_its_children_are_done(store, outcomes):
    """THE rule. All children complete moves the outcome to
    AWAITING_ACCEPTANCE -- the state a naive implementation would have
    called DONE -- and completion still refuses without acceptance evidence."""
    outcome = outcomes.create(PROJECT, "Overview ships", acceptance_criteria=CRITERIA)
    for _ in range(3):
        outcomes.attach_task(outcome.id, finished_task(store))

    rolled = outcomes.refresh_status(outcome.id)
    assert rolled.status == OUTCOME_AWAITING_ACCEPTANCE
    assert outcomes.progress(outcome.id)["percent"] == 100.0

    refused = outcomes.complete(outcome.id, evidence={})
    assert refused["ok"] is False
    assert refused["error"] == "ACCEPTANCE_EVIDENCE_REQUIRED"
    assert sorted(refused["missing_criteria"]) == sorted(CRITERIA)
    assert outcomes.get(outcome.id).status == OUTCOME_AWAITING_ACCEPTANCE


def test_children_done_is_necessary_before_acceptance(store, outcomes):
    """The other half: necessary, even though not sufficient."""
    outcome = outcomes.create(PROJECT, "Overview ships", acceptance_criteria=CRITERIA)
    outcomes.attach_task(outcome.id, finished_task(store))
    stuck = open_task(store, title="not done")
    outcomes.attach_task(outcome.id, stuck)

    refused = outcomes.complete(outcome.id, evidence={c: GOOD for c in CRITERIA})
    assert refused["error"] == "TASKS_STILL_OPEN"
    assert refused["open_tasks"] == [stuck]


def test_completion_requires_evidence_for_every_criterion(store, outcomes):
    outcome = outcomes.create(PROJECT, "Overview ships", acceptance_criteria=CRITERIA)
    outcomes.attach_task(outcome.id, finished_task(store))
    outcomes.refresh_status(outcome.id)

    partial = outcomes.complete(outcome.id, evidence={CRITERIA[0]: GOOD})
    assert partial["error"] == "ACCEPTANCE_EVIDENCE_REQUIRED"
    assert partial["missing_criteria"] == [CRITERIA[1]]


def test_a_self_report_is_not_acceptance_evidence(store, outcomes):
    """Reuses verify_queue.evidence_verdict rather than inventing a fourth
    notion of what evidence is."""
    outcome = outcomes.create(PROJECT, "Overview ships", acceptance_criteria=CRITERIA)
    outcomes.attach_task(outcome.id, finished_task(store))
    outcomes.refresh_status(outcome.id)

    result = outcomes.complete(outcome.id, evidence={
        CRITERIA[0]: {"summary": "I checked it and it looks right"},
        CRITERIA[1]: {"command": "pytest", "exit_code": 1},
    })
    assert result["error"] == "ACCEPTANCE_EVIDENCE_REQUIRED"
    assert set(result["rejected_criteria"]) == set(CRITERIA)
    assert "self-report" in result["rejected_criteria"][CRITERIA[0]]
    assert "exit_code=1" in result["rejected_criteria"][CRITERIA[1]]


def test_a_fully_evidenced_outcome_completes(store, outcomes):
    outcome = outcomes.create(PROJECT, "Overview ships", acceptance_criteria=CRITERIA)
    outcomes.attach_task(outcome.id, finished_task(store))
    outcomes.refresh_status(outcome.id)

    result = outcomes.complete(outcome.id, evidence={CRITERIA[0]: GOOD, CRITERIA[1]: GOOD2},
                               actor="verifier-1")
    assert result["ok"] is True
    done = outcomes.get(outcome.id)
    assert done.status == OUTCOME_DONE and done.completed_at
    assert set(done.evidence) == set(CRITERIA)
    assert any(e["event"] == OUTCOME_DONE and e["actor"] == "verifier-1" for e in done.history)


def test_acceptance_evidence_is_redacted(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["works"])
    outcomes.attach_task(outcome.id, finished_task(store))
    outcomes.refresh_status(outcome.id)
    outcomes.complete(outcome.id, evidence={"works": {
        "command": "curl -H 'Authorization: Bearer ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' x",
        "exit_code": 0}})
    stored = str(outcomes.get(outcome.id).evidence)
    assert "ghp_" not in stored and "<REDACTED>" in stored


# -- 2. Creation invariants ----------------------------------------------

def test_an_outcome_requires_at_least_one_acceptance_criterion(outcomes):
    """Without criteria, complete() would have nothing to gate on and an
    empty evidence payload would pass -- the exact hole this layer closes."""
    with pytest.raises(OutcomeError, match="acceptance criterion"):
        outcomes.create(PROJECT, "Vague thing", acceptance_criteria=[])
    with pytest.raises(OutcomeError, match="acceptance criterion"):
        outcomes.create(PROJECT, "Vague thing", acceptance_criteria=["   "])


def test_project_and_title_are_required(outcomes):
    with pytest.raises(OutcomeError, match="project_id"):
        outcomes.create("", "t", acceptance_criteria=["c"])
    with pytest.raises(OutcomeError, match="title"):
        outcomes.create(PROJECT, "  ", acceptance_criteria=["c"])


# -- 3. One outcome, MANY tasks (the thing the old model could not do) ---

def test_one_outcome_spans_many_tasks(store, outcomes):
    """The previous model was welded 1:1 -- backlog_service.dispatch created
    one task per item and then refused forever (ALREADY_DISPATCHED)."""
    outcome = outcomes.create(PROJECT, "Overview ships", acceptance_criteria=CRITERIA,
                              backlog_id="BL-1")
    ids = [finished_task(store, title=f"t{i}") for i in range(5)]
    for task_id in ids:
        outcomes.attach_task(outcome.id, task_id)
    assert {t.id for t in outcomes.tasks_for(outcome.id)} == set(ids)
    assert outcomes.progress(outcome.id)["total"] == 5


def test_an_attached_task_inherits_the_project(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    task_id = open_task(store)
    assert store.get_task(task_id).project_id is None
    outcomes.attach_task(outcome.id, task_id)
    assert store.get_task(task_id).project_id == PROJECT
    assert store.get_task(task_id).outcome_id == outcome.id


def test_cannot_attach_to_a_finished_outcome(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    outcomes.attach_task(outcome.id, finished_task(store))
    outcomes.refresh_status(outcome.id)
    outcomes.complete(outcome.id, evidence={"c": GOOD})
    with pytest.raises(OutcomeError, match="DONE"):
        outcomes.attach_task(outcome.id, finished_task(store))


def test_attaching_an_unknown_task_or_outcome_is_refused(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    with pytest.raises(OutcomeError, match="no such task"):
        outcomes.attach_task(outcome.id, "nope")
    with pytest.raises(OutcomeError, match="no such outcome"):
        outcomes.attach_task("nope", finished_task(store))


# -- 4. Rollup + lifecycle ----------------------------------------------

def test_rollup_tracks_children_but_can_never_reach_done(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    assert outcomes.refresh_status(outcome.id).status == OUTCOME_OPEN

    outcomes.attach_task(outcome.id, open_task(store, title="a"))
    assert outcomes.refresh_status(outcome.id).status == OUTCOME_IN_PROGRESS

    for task in outcomes.tasks_for(outcome.id):
        store.cancel_task(task.id)
    assert outcomes.refresh_status(outcome.id).status == OUTCOME_AWAITING_ACCEPTANCE
    assert outcomes.refresh_status(outcome.id).status != OUTCOME_DONE


def test_block_and_unblock(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    outcomes.attach_task(outcome.id, open_task(store))
    blocked = outcomes.block(outcome.id, reason="waiting on a design decision")
    assert blocked.status == OUTCOME_BLOCKED
    assert blocked.blocked_reason == "waiting on a design decision"
    # A blocked outcome does not get silently rolled forward.
    assert outcomes.refresh_status(outcome.id).status == OUTCOME_BLOCKED
    assert outcomes.unblock(outcome.id).status == OUTCOME_IN_PROGRESS


def test_invalid_transitions_are_refused(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    outcomes.cancel(outcome.id, reason="dropped")
    with pytest.raises(OutcomeError, match="not a valid outcome transition"):
        outcomes.block(outcome.id, reason="x")
    result = outcomes.complete(outcome.id, evidence={"c": GOOD})
    assert result["error"] == "OUTCOME_CANCELLED"


def test_completing_twice_is_refused(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"])
    outcomes.attach_task(outcome.id, finished_task(store))
    outcomes.refresh_status(outcome.id)
    assert outcomes.complete(outcome.id, evidence={"c": GOOD})["ok"] is True
    assert outcomes.complete(outcome.id, evidence={"c": GOOD})["error"] == "ALREADY_DONE"


# -- 5. Traceability -----------------------------------------------------

def test_trace_walks_backlog_to_outcome_to_tasks(store, outcomes):
    outcome = outcomes.create(PROJECT, "Ship", acceptance_criteria=["c"], backlog_id="BL-42")
    (task_id,) = store.set_tasks("lane-a", [{"title": "impl", "prompt": "p",
                                             "metadata": {"branch": "feat/x",
                                                          "commit_sha": "cafe"}}])
    outcomes.attach_task(outcome.id, task_id)

    trace = outcomes.trace(outcome.id)
    assert trace["outcome"]["backlog_id"] == "BL-42"
    assert trace["outcome"]["project_id"] == PROJECT
    entry = trace["tasks"][0]
    assert entry["task_id"] == task_id
    assert entry["branch"] == "feat/x" and entry["commit_sha"] == "cafe"
    assert trace["progress"]["total"] == 1


def test_listing_is_project_and_status_scoped(store, outcomes):
    a = outcomes.create(PROJECT, "A", acceptance_criteria=["c"], backlog_id="BL-1")
    outcomes.create("other-project", "B", acceptance_criteria=["c"])
    outcomes.block(a.id, reason="x")
    assert {o.title for o in outcomes.list_outcomes(project_id=PROJECT)} == {"A"}
    assert {o.title for o in outcomes.list_outcomes(status=OUTCOME_BLOCKED)} == {"A"}
    assert {o.title for o in outcomes.list_outcomes(backlog_id="BL-1")} == {"A"}


# -- 6. Backward compatibility ------------------------------------------

def test_tasks_without_an_outcome_are_completely_unaffected(store):
    """outcome_id is nullable and additive: every pre-existing task and
    query behaves exactly as before."""
    (task_id,) = store.set_tasks("lane-a", [{"title": "t", "prompt": "p"}])
    task = store.get_task(task_id)
    assert task.outcome_id is None
    assert task.to_dict()["outcome_id"] is None
    assert store.next_dispatchable_task("lane-a").id == task_id


def test_migration_v8_is_additive_on_a_real_production_copy(tmp_path):
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
        "SELECT id, status, session, project_id FROM queue_tasks")}
    lanes_before = before.execute("SELECT COUNT(*) FROM queue_lanes").fetchone()[0]
    before.close()

    QueueStore(copy)

    after = sqlite3.connect(copy)
    rows_after = {r[0]: r[1:] for r in after.execute(
        "SELECT id, status, session, project_id FROM queue_tasks")}
    assert rows_after == rows_before
    assert after.execute("SELECT COUNT(*) FROM queue_lanes").fetchone()[0] == lanes_before
    assert after.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 0
    assert after.execute("SELECT COUNT(*) FROM queue_tasks WHERE outcome_id IS NOT NULL").fetchone()[0] == 0
    after.close()
