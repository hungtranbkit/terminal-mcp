"""3-role model end-to-end smoke test (task: "3-role model: Coding A/B
+ Integration Agent" -- Coordinator/coding sessions never wait on merge/
test; a dedicated Integration Agent pipeline does the merge/test/
promote work on its own per-project queue). Combines the two already-
separately-proven halves (test_queue_engine_smoke.py's real-tmux coding
dispatch, test_integration_engine.py's real-git merge/test/promote) into
ONE real, end-to-end chain: a real tmux coding session completes a real
task -> QueueEngine's on_completed hook auto-publishes a real Handoff ->
IntegrationEngine merges/tests/integrates/batches/promotes it -- all
through the real MCP tool surface.

SAFETY: every session/project/repo here is disposable. NEVER
`window`/`window2`, NEVER a real OfflinePOS checkout, NEVER auto-
promote enabled without an explicit test assertion checking for it.

NOT RUN BY DEFAULT (`pytest.mark.queue_smoke` -- reused, not a new
marker, since this has the identical real-wall-clock-wait
characteristics documented on test_queue_engine_smoke.py's own
docstring). Run explicitly: `pytest -m queue_smoke tests/test_three_role_smoke.py -v`.
"""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.controller import build_default_controller
from terminal_mcp.core import TerminalService
from terminal_mcp.integration_service import IntegrationService
from terminal_mcp.integration_store import IntegrationStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

pytestmark = pytest.mark.queue_smoke


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


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _service(tmp_path) -> TerminalService:
    config = AppConfig(
        PermissionsConfig(True, True), ("role3-*",), 200, 100,
        InputPolicyConfig(allowed_session_patterns=("role3-*",), max_text_length=4000),
    )
    return TerminalService(config, bindings=BindingStore(tmp_path / "bindings.db"),
                          audit=AuditStore(tmp_path / "audit.db"))


@pytest.fixture
def rig(tmp_path, tmux_session_factory):
    service = _service(tmp_path)
    controller = build_default_controller(service)
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)

    integration = IntegrationService(IntegrationStore(tmp_path / "integration.db"))
    from terminal_mcp.integration_store import publish_handoff_for_completed_task

    def _on_completed(task):
        publish_handoff_for_completed_task(task, integration.store)

    queue = QueueService(QueueStore(tmp_path / "queue.db"), on_completed=_on_completed)
    mcp_server = build_mcp(service, controller=controller, queue=queue, integration=integration)

    def make_worker(name: str, repo) -> None:
        tmux_session_factory(
            name, f"bash -c 'cd {repo} && IFS= read -r first_line && sleep 0.15 && echo \"$first_line\" && exec cat'")

    return {"controller": controller, "queue": queue, "integration": integration, "server": mcp_server,
           "make_worker": make_worker}


def _reheartbeat(controller):
    controller.refresh_local_heartbeat(tmux_session_count=0, agent_counts={}, agent_types=(), agent_version=None)


@pytest.mark.anyio
async def test_worker_continues_to_next_task_without_waiting_for_integration(rig, tmp_path):
    """The KEY throughput proof (item: "coding worker vẫn có thể tiếp
    tục feature kế tiếp"): a real coding session, dispatched/completed
    through the real MCP tool surface + real tmux, is free to start its
    SECOND task immediately after its first COMPLETED -- with zero
    integration work having happened yet for the first one (no
    Integration Agent tick has even run)."""
    repo = _init_repo(tmp_path / "repo")
    server = rig["server"]
    controller = rig["controller"]

    # Targeted test PASSES when the file contains OK_MARKER -- used
    # consistently across this whole test module.
    await _call(server, "terminal_integration_configure", project="proj-3role", repo_path=str(repo),
               targeted_test_command=["grep", "-q", "OK_MARKER", "{paths}"],
               full_regression_command=["true"], batch_size=2)
    rig["make_worker"]("role3-a", repo)

    sha_a1, base_a1 = _commit_on_branch(repo, "feature/a1", "a1.txt", "OK_MARKER present\n")
    sha_a2, base_a2 = _commit_on_branch(repo, "feature/a2", "a2.txt", "OK_MARKER present too\n")

    result_a = await _call(server, "terminal_queue_set", session="role3-a", tasks=[
        {"prompt": "print marker A1 please", "metadata": {"integration_required": {
            "project": "proj-3role", "branch": "feature/a1", "commit_sha": sha_a1, "base_sha": base_a1,
            "changed_paths": ["a1.txt"]}}},
        {"prompt": "print marker A2 please", "metadata": {"integration_required": {
            "project": "proj-3role", "branch": "feature/a2", "commit_sha": sha_a2, "base_sha": base_a2,
            "changed_paths": ["a2.txt"]}}},
    ])

    deadline = time.monotonic() + 90
    a1_done = False
    while time.monotonic() < deadline:
        _reheartbeat(controller)
        await _call(server, "terminal_queue_run_once", session="role3-a")
        status_a = await _call(server, "terminal_queue_status", session="role3-a")
        if status_a["tasks"][0]["status"] == "COMPLETED":
            a1_done = True
            break
        time.sleep(0.5)
    assert a1_done, "worker A1 never completed in time"

    # Zero Integration Agent ticks have run -- A1's handoff is still
    # sitting untouched in READY_FOR_INTEGRATION.
    handoffs = await _call(server, "terminal_integration_list_handoffs", project="proj-3role")
    assert len(handoffs["handoffs"]) == 1
    assert handoffs["handoffs"][0]["status"] == "READY_FOR_INTEGRATION"

    # A2 is claimable/dispatchable RIGHT NOW -- the coding session was
    # never blocked waiting on merge/test.
    status_a = await _call(server, "terminal_queue_status", session="role3-a")
    assert status_a["tasks"][1]["status"] == "QUEUED"
    claim_a2 = await _call(server, "terminal_queue_run_once", session="role3-a")
    assert claim_a2["action"] == "CLAIMED"


@pytest.mark.anyio
async def test_conflict_and_failing_test_route_rework_to_correct_owner_end_to_end(rig, tmp_path):
    """A smaller, more direct version of the full chain focused
    specifically on item: 'cố ý tạo 1 conflict và 1 failing test để
    verify route rework đúng worker' -- commits are created directly
    (standing in for 'the worker already completed this feature'),
    then driven purely through the real Integration Agent MCP tools
    (the coding-dispatch half is already proven separately above and in
    test_queue_engine_smoke.py)."""
    repo = _init_repo(tmp_path / "repo")
    server = rig["server"]
    integration = rig["integration"]
    queue = rig["queue"]

    # Targeted test PASSES when the file contains "OK_MARKER".
    await _call(server, "terminal_integration_configure", project="proj-3role", repo_path=str(repo),
               targeted_test_command=["grep", "-q", "OK_MARKER", "{paths}"],
               full_regression_command=["true"], batch_size=1)

    sha_a1, base_a1 = _commit_on_branch(repo, "feature/a1", "shared.txt", "OK_MARKER present\n")
    sha_b1, base_b1 = _commit_on_branch(repo, "feature/b1", "shared.txt", "CONFLICT: no marker here\n")
    sha_a2, base_a2 = _commit_on_branch(repo, "feature/a2", "a2.txt", "no marker at all -- will fail test\n")

    integration.store.publish_handoff(project="proj-3role", task_id="task-a1", origin_session="role3-a",
                                      branch="feature/a1", commit_sha=sha_a1, base_sha=base_a1,
                                      changed_paths=["shared.txt"])
    integration.store.publish_handoff(project="proj-3role", task_id="task-b1", origin_session="role3-b",
                                      branch="feature/b1", commit_sha=sha_b1, base_sha=base_b1,
                                      changed_paths=["shared.txt"])
    integration.store.publish_handoff(project="proj-3role", task_id="task-a2", origin_session="role3-a",
                                      branch="feature/a2", commit_sha=sha_a2, base_sha=base_a2,
                                      changed_paths=["a2.txt"])

    engine = integration.engine
    # A1: claim -> review -> merge -> targeted test PASS -> INTEGRATED.
    r1 = engine.tick("proj-3role")
    assert r1.action == "CLAIMED"
    r2 = engine.tick("proj-3role")
    assert r2.action == "MERGED"
    r3 = engine.tick("proj-3role")
    assert r3.action == "INTEGRATED"

    # B1: claim -> review -> REAL merge conflict -> REWORK_REQUIRED, routed to role3-b.
    r4 = engine.tick("proj-3role")
    assert r4.action == "CLAIMED"
    r5 = engine.tick("proj-3role")
    assert r5.action == "REWORK_REQUIRED"
    b1_handoffs = integration.store.list_handoffs("proj-3role", status="REWORK_REQUIRED")
    b1_handoff = next(h for h in b1_handoffs if h.branch == "feature/b1")
    assert b1_handoff.conflict_detected is True
    rework_b = queue.store.get_task(b1_handoff.rework_task_id)
    assert rework_b.session == "role3-b"

    # A2: claim -> review -> merge (clean, no conflict) -> targeted test
    # FAILS (no OK_MARKER) -> REWORK_REQUIRED, routed to role3-a.
    r6 = engine.tick("proj-3role")
    assert r6.action == "CLAIMED"
    r7 = engine.tick("proj-3role")
    assert r7.action == "MERGED"
    r8 = engine.tick("proj-3role")
    assert r8.action == "REWORK_REQUIRED"
    a2_handoffs = integration.store.list_handoffs("proj-3role", status="REWORK_REQUIRED")
    a2_handoff = next(h for h in a2_handoffs if h.branch == "feature/a2")
    rework_a = queue.store.get_task(a2_handoff.rework_task_id)
    assert rework_a.session == "role3-a"
    assert rework_a.status == "QUEUED"  # role3-a's own queue can proceed independently

    # Batch regression + promote for the ONE successfully integrated handoff.
    batch_result = engine.tick("proj-3role")
    assert batch_result.action == "BATCH_CREATED"
    running = engine.tick("proj-3role")
    assert running.action == "REGRESSION_RUNNING"
    passed = engine.tick("proj-3role")
    assert passed.action == "MERGE_READY"
    promotion = await _call(server, "terminal_integration_promote", project="proj-3role",
                            batch_id=batch_result.batch_id)
    assert promotion["action"] == "PROMOTED"

    _git(["checkout", "-q", "main"], repo)
    assert (repo / "shared.txt").read_text() == "OK_MARKER present\n"
    assert not (repo / "a2.txt").exists()  # A2's failed work never reached main


@pytest.mark.anyio
async def test_restart_mid_pipeline_never_double_merges(rig, tmp_path):
    """item 9/10: 'restart controller giữa chừng để verify không
    double-dispatch/double-merge' for the INTEGRATION side specifically
    (the coding side's own equivalent is test_queue_engine_smoke.py's
    own restart test)."""
    repo = _init_repo(tmp_path / "repo")
    integration = rig["integration"]
    queue = rig["queue"]
    integration.store.configure_pipeline("proj-3role", repo_path=str(repo), targeted_test_command=["true"],
                                         full_regression_command=["true"], batch_size=1)
    sha, base_sha = _commit_on_branch(repo, "feature/x", "x.txt", "content\n")
    handoff = integration.store.publish_handoff(project="proj-3role", task_id="t1", origin_session="role3-a",
                                                branch="feature/x", commit_sha=sha, base_sha=base_sha,
                                                changed_paths=["x.txt"])
    engine = integration.engine
    engine.tick("proj-3role")  # CLAIMED
    merged = engine.tick("proj-3role")  # MERGED -> TARGETED_TEST
    assert merged.action == "MERGED"
    first_merge_commit = integration.store.get_handoff(handoff.id).merge_commit_sha

    with integration.store._connection() as connection:
        connection.execute(
            "UPDATE integration_handoffs SET status = 'MERGING', lease_expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = ?", (handoff.id,))

    integration_store2 = IntegrationStore(tmp_path / "integration.db")
    from terminal_mcp.integration_engine import IntegrationEngine
    engine2 = IntegrationEngine(integration_store2, queue.store)
    reconciled = integration_store2.reconcile_stale_handoff_claims("proj-3role")
    assert handoff.id in reconciled
    engine2.tick("proj-3role")  # CLAIMED again
    remerge = engine2.tick("proj-3role")  # re-merge -- git no-op
    assert remerge.action == "MERGED"
    assert integration_store2.get_handoff(handoff.id).merge_commit_sha == first_merge_commit

    _git(["checkout", "-q", "integration"], repo)
    log = _git(["log", "--oneline", "--all"], repo).stdout
    assert log.count("integrate feature/x") == 1
