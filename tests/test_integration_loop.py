"""IntegrationLoop -- the event-driven WAIT/wake background loop for the
3-role pipeline's Integration Agent (§9, task follow-up: "chế độ WAIT/
EVENT-DRIVEN cho session Integration/Test").

Fast, deterministic tests exercise run_one_cycle()/wake() directly, plus
real background threads (start()/stop()) against a REAL git repo (same
`_init_repo`/`_commit_on_branch` fixtures as test_integration_engine.py)
to prove the full autonomous lifecycle end to end -- a handoff reaching
INTEGRATED/REWORK_REQUIRED with ZERO manual run_once/tick() calls.

SAFETY: every repo/project here is a disposable tmp_path fixture --
never a real OfflinePOS checkout, never `window`/`window2`."""
from __future__ import annotations

import subprocess
import threading
import time

import pytest

from terminal_mcp.integration_engine import IntegrationEngine
from terminal_mcp.integration_loop import IntegrationLoop
from terminal_mcp.integration_store import IntegrationStore
from terminal_mcp.queue_store import QueueStore


def _git(args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=check)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / "README.md").write_text("hello\n")
    _git(["add", "."], path)
    _git(["commit", "-q", "-m", "initial"], path)
    _git(["branch", "integration"], path)
    return path


def _commit_on_branch(repo, branch, filename, content, *, base="main", message="feature commit"):
    _git(["checkout", "-q", base], repo)
    _git(["checkout", "-q", "-b", branch], repo, check=False)
    _git(["checkout", "-q", branch], repo)
    (repo / filename).write_text(content)
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", message], repo)
    sha = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    base_sha = _git(["rev-parse", base], repo).stdout.strip()
    _git(["checkout", "-q", "main"], repo)
    return sha, base_sha


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture
def store(tmp_path):
    return IntegrationStore(tmp_path / "integration.db")


@pytest.fixture
def queue_store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def engine(store, queue_store):
    return IntegrationEngine(store, queue_store)


@pytest.fixture
def repo(tmp_path):
    return _init_repo(tmp_path / "repo")


def _configure(store, repo_path, **overrides):
    kwargs = {"repo_path": str(repo_path), "targeted_test_command": ["true"],
             "full_regression_command": ["true"], "batch_size": 5}
    kwargs.update(overrides)
    return store.configure_pipeline("proj-loop", **kwargs)


def _publish(store, **kwargs):
    kwargs.setdefault("artifacts", {"docs_exempt": "chore"})
    return store.publish_handoff(**kwargs)


# ---------------------------------------------------------------------------
# run_one_cycle() -- deterministic, no real thread.
# ---------------------------------------------------------------------------

def test_run_one_cycle_only_touches_configured_non_paused_projects(store, engine, repo):
    _configure(store, repo)
    store.configure_pipeline("proj-paused", repo_path=str(repo), targeted_test_command=["true"],
                             full_regression_command=["true"])
    store.pause_pipeline("proj-paused", reason="operator maintenance")
    loop = IntegrationLoop(engine, store, fallback_poll_seconds=60)

    results = loop.run_one_cycle()

    projects_touched = {r["project"] for r in results}
    assert projects_touched == {"proj-loop"}  # proj-paused skipped entirely


def test_one_project_failure_never_stops_other_projects_in_the_same_cycle(store, engine, repo):
    _configure(store, repo)
    store.configure_pipeline("proj-broken", repo_path=str(repo), targeted_test_command=["true"],
                             full_regression_command=["true"])
    real_tick = engine.tick

    def flaky_tick(project):
        if project == "proj-broken":
            raise RuntimeError("simulated engine failure for proj-broken")
        return real_tick(project)
    engine.tick = flaky_tick

    loop = IntegrationLoop(engine, store, fallback_poll_seconds=60)
    results = loop.run_one_cycle()

    broken = next(r for r in results if r["project"] == "proj-broken")
    healthy = next(r for r in results if r["project"] == "proj-loop")
    assert broken["action"] == "ENGINE_ERROR"
    assert healthy["action"] in ("WAITING_FOR_HANDOFF", "CLAIMED")


def test_status_reports_running_and_last_cycle(store, engine, repo):
    _configure(store, repo)
    loop = IntegrationLoop(engine, store, fallback_poll_seconds=60)
    assert loop.status()["running"] is False
    assert loop.status()["last_cycle_at"] is None
    loop.run_one_cycle()
    assert loop.status()["last_cycle_at"] is not None


# ---------------------------------------------------------------------------
# wake() -- the real event-driven signal.
# ---------------------------------------------------------------------------

def test_wake_is_safe_to_call_with_no_loop_running(store, engine):
    loop = IntegrationLoop(engine, store, fallback_poll_seconds=60)
    loop.wake("proj-loop")  # must not raise


def test_publish_handoff_wakes_a_running_loop_almost_immediately(store, engine, repo):
    """THE core event-driven proof at the wiring level: publish_handoff
    itself (via the store's own on_handoff_published hook) wakes the
    loop well before its own long fallback interval would have."""
    _configure(store, repo)
    loop = IntegrationLoop(engine, store, fallback_poll_seconds=30)  # deliberately long
    store.on_handoff_published = loop.wake
    loop.start()
    try:
        sha, base_sha = _commit_on_branch(repo, "feature/x", "x.txt", "content\n")
        started_at = time.monotonic()
        _publish(store, project="proj-loop", task_id="t1", origin_session="role-a", branch="feature/x",
                commit_sha=sha, base_sha=base_sha, changed_paths=["x.txt"])
        assert _wait_until(lambda: loop.status()["last_cycle_at"] is not None, timeout=3.0)
        elapsed = time.monotonic() - started_at
        assert elapsed < 5.0, f"loop took {elapsed:.1f}s to react -- looks like it waited out the 30s fallback poll"
    finally:
        loop.stop()


# ---------------------------------------------------------------------------
# Real background thread, full lifecycle, ZERO manual run_once/tick calls.
# ---------------------------------------------------------------------------

def test_real_loop_drives_a_clean_handoff_to_integrated_with_zero_manual_ticks(store, engine, repo):
    _configure(store, repo)
    sha, base_sha = _commit_on_branch(repo, "feature/x", "x.txt", "content\n")

    loop = IntegrationLoop(engine, store, fallback_poll_seconds=20)  # long -- proves the wake path, not the poll
    store.on_handoff_published = loop.wake
    loop.start()
    try:
        started_at = time.monotonic()
        handoff = _publish(store, project="proj-loop", task_id="t1", origin_session="role-a", branch="feature/x",
                           commit_sha=sha, base_sha=base_sha, changed_paths=["x.txt"])
        assert _wait_until(lambda: store.get_handoff(handoff.id).status == "INTEGRATED", timeout=10.0)
        elapsed = time.monotonic() - started_at
        assert elapsed < 20.0, "reached INTEGRATED only after the fallback poll -- back-to-back progress not working"
    finally:
        loop.stop()

    _git(["checkout", "-q", "integration"], repo)
    assert (repo / "x.txt").read_text() == "content\n"


def test_real_loop_routes_a_real_conflict_to_rework_required_with_zero_manual_ticks(store, engine, queue_store, repo):
    _configure(store, repo)
    sha_a, base_a = _commit_on_branch(repo, "feature/a", "shared.txt", "A's content\n")
    sha_b, base_b = _commit_on_branch(repo, "feature/b", "shared.txt", "B's conflicting content\n")

    loop = IntegrationLoop(engine, store, fallback_poll_seconds=20)
    store.on_handoff_published = loop.wake
    loop.start()
    try:
        handoff_a = _publish(store, project="proj-loop", task_id="ta", origin_session="role-a", branch="feature/a",
                             commit_sha=sha_a, base_sha=base_a, changed_paths=["shared.txt"])
        assert _wait_until(lambda: store.get_handoff(handoff_a.id).status == "INTEGRATED", timeout=10.0)

        handoff_b = _publish(store, project="proj-loop", task_id="tb", origin_session="role-b", branch="feature/b",
                             commit_sha=sha_b, base_sha=base_b, changed_paths=["shared.txt"])
        assert _wait_until(lambda: store.get_handoff(handoff_b.id).status == "REWORK_REQUIRED", timeout=10.0)
    finally:
        loop.stop()

    refreshed_b = store.get_handoff(handoff_b.id)
    assert refreshed_b.conflict_detected is True
    rework = queue_store.get_task(refreshed_b.rework_task_id)
    assert rework.session == "role-b"


# ---------------------------------------------------------------------------
# Race/duplicate: two loop instances (simulating two processes) racing on
# the exact same handoff must never double-merge.
# ---------------------------------------------------------------------------

def test_two_concurrent_loop_instances_never_double_merge_the_same_handoff(tmp_path, repo):
    db_path = tmp_path / "integration.db"
    queue_path = tmp_path / "queue.db"
    store_a = IntegrationStore(db_path)
    store_b = IntegrationStore(db_path)  # a SEPARATE connection/instance -- simulates a second process
    queue_store_a = QueueStore(queue_path)
    queue_store_b = QueueStore(queue_path)
    _configure(store_a, repo)
    engine_a = IntegrationEngine(store_a, queue_store_a)
    engine_b = IntegrationEngine(store_b, queue_store_b)
    loop_a = IntegrationLoop(engine_a, store_a, fallback_poll_seconds=0.05)
    loop_b = IntegrationLoop(engine_b, store_b, fallback_poll_seconds=0.05)

    sha, base_sha = _commit_on_branch(repo, "feature/race", "race.txt", "race content\n")
    handoff = _publish(store_a, project="proj-loop", task_id="t-race", origin_session="role-a",
                       branch="feature/race", commit_sha=sha, base_sha=base_sha, changed_paths=["race.txt"])

    # Both loops start already racing for the SAME single handoff --
    # no wake() needed, the short fallback_poll_seconds alone is enough
    # to make them race hard against each other within a few cycles.
    loop_a.start()
    loop_b.start()
    try:
        assert _wait_until(lambda: store_a.get_handoff(handoff.id).status == "INTEGRATED", timeout=10.0)
        time.sleep(0.5)  # let any in-flight racing cycle settle before asserting
    finally:
        loop_a.stop()
        loop_b.stop()

    _git(["checkout", "-q", "integration"], repo)
    log = _git(["log", "--oneline", "--all"], repo).stdout
    assert log.count("integrate feature/race") == 1  # never double-merged
    assert store_a.get_handoff(handoff.id).status == "INTEGRATED"


# ---------------------------------------------------------------------------
# Restart safety: a loop instance dies mid-pipeline (never stopped
# gracefully); a BRAND NEW loop instance, wired to the same store, must
# still find and complete the abandoned handoff on its own -- no manual
# tick() call, proving the LOOP itself (not just the engine) survives a
# restart.
# ---------------------------------------------------------------------------

def test_a_fresh_loop_instance_recovers_a_handoff_abandoned_by_a_dead_loop(tmp_path, repo):
    db_path = tmp_path / "integration.db"
    queue_path = tmp_path / "queue.db"
    store1 = IntegrationStore(db_path)
    queue_store1 = QueueStore(queue_path)
    _configure(store1, repo)
    sha, base_sha = _commit_on_branch(repo, "feature/restart", "restart.txt", "restart content\n")
    handoff = _publish(store1, project="proj-loop", task_id="t-restart", origin_session="role-a",
                       branch="feature/restart", commit_sha=sha, base_sha=base_sha,
                       changed_paths=["restart.txt"])

    engine1 = IntegrationEngine(store1, queue_store1)
    engine1.tick("proj-loop")  # CLAIMED
    merged = engine1.tick("proj-loop")  # MERGED -> TARGETED_TEST
    assert merged.action == "MERGED"
    first_merge_commit = store1.get_handoff(handoff.id).merge_commit_sha
    # The "process" that owned this claim is gone -- simulate an abandoned
    # lease (same real mechanism a real crash/restart leaves behind) --
    # never call loop1.stop()/anything graceful, matching a real crash.

    with store1._connection() as connection:
        connection.execute(
            "UPDATE integration_handoffs SET status = 'MERGING', lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = ?", (handoff.id,))

    # A BRAND NEW store/engine/loop -- same db file, nothing carried over
    # in memory, exactly what a real process restart looks like.
    store2 = IntegrationStore(db_path)
    queue_store2 = QueueStore(queue_path)
    engine2 = IntegrationEngine(store2, queue_store2)
    loop2 = IntegrationLoop(engine2, store2, fallback_poll_seconds=0.05)
    loop2.start()
    try:
        assert _wait_until(lambda: store2.get_handoff(handoff.id).status == "INTEGRATED", timeout=10.0)
    finally:
        loop2.stop()

    assert store2.get_handoff(handoff.id).merge_commit_sha == first_merge_commit  # git no-op re-merge, no duplicate

    _git(["checkout", "-q", "integration"], repo)
    log = _git(["log", "--oneline", "--all"], repo).stdout
    assert log.count("integrate feature/restart") == 1
