"""Integration Agent -- MCP tool surface (task: "3-role model: Coding
A/B + Integration Agent"). Exercises the real MCP call path
(server.call_tool), same pattern as test_queue_mcp_tools.py.

SAFETY: `project`/`repo_path` here are always disposable tmp_path
fixtures -- never a real OfflinePOS checkout."""
from __future__ import annotations

import json
import subprocess

import pytest

from terminal_mcp.integration_service import IntegrationService
from terminal_mcp.integration_store import IntegrationStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_store import QueueStore


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=path, check=True)
    subprocess.run(["git", "branch", "integration"], cwd=path, check=True)
    return path


@pytest.fixture
def server(tmp_path):
    from terminal_mcp.integration_store import publish_handoff_for_completed_task
    from terminal_mcp.queue_service import QueueService

    integration = IntegrationService(IntegrationStore(tmp_path / "integration.db"))

    def _on_completed(task):
        publish_handoff_for_completed_task(task, integration.store)

    # Same wiring build_mcp does internally for its own DEFAULT-
    # constructed queue -- reproduced explicitly here because this test
    # needs a handle to queue.store for direct state-machine stepping
    # (terminal_queue_* intentionally exposes no raw-transition tool).
    queue = QueueService(QueueStore(tmp_path / "queue.db"), on_completed=_on_completed)
    return build_mcp(queue=queue, integration=integration), integration, queue


@pytest.mark.anyio
async def test_configure_then_status_round_trips(server, tmp_path):
    mcp_server, integration, queue = server
    repo = _init_repo(tmp_path / "repo")
    result = await _call(mcp_server, "terminal_integration_configure", project="proj-a", repo_path=str(repo))
    assert result["repo_path"] == str(repo)
    status = await _call(mcp_server, "terminal_integration_status", project="proj-a")
    assert status["pipeline"]["paused"] is False
    assert status["current_handoff"] is None


@pytest.mark.anyio
async def test_pause_and_resume_through_mcp(server, tmp_path):
    mcp_server, integration, queue = server
    repo = _init_repo(tmp_path / "repo")
    await _call(mcp_server, "terminal_integration_configure", project="proj-a", repo_path=str(repo))
    paused = await _call(mcp_server, "terminal_integration_pause", project="proj-a", reason="testing")
    assert paused["pipeline"]["paused"] is True
    resumed = await _call(mcp_server, "terminal_integration_resume", project="proj-a")
    assert resumed["pipeline"]["paused"] is False


@pytest.mark.anyio
async def test_run_once_reports_waiting_for_handoff_on_an_empty_queue(server, tmp_path):
    mcp_server, integration, queue = server
    repo = _init_repo(tmp_path / "repo")
    await _call(mcp_server, "terminal_integration_configure", project="proj-a", repo_path=str(repo))
    result = await _call(mcp_server, "terminal_integration_run_once", project="proj-a")
    assert result["action"] == "WAITING_FOR_HANDOFF"


@pytest.mark.anyio
async def test_integration_loop_status_reports_not_running_by_default(server, tmp_path):
    mcp_server, integration, queue = server
    status = await _call(mcp_server, "terminal_integration_loop_status")
    assert status["running"] is False  # build_mcp constructs it but server_http.py never started it here
    assert status["last_cycle_at"] is None


@pytest.mark.anyio
async def test_integration_loop_run_once_through_mcp_drives_one_real_cycle(server, tmp_path):
    mcp_server, integration, queue = server
    repo = _init_repo(tmp_path / "repo")
    await _call(mcp_server, "terminal_integration_configure", project="proj-a", repo_path=str(repo),
               targeted_test_command=["true"], full_regression_command=["true"])
    subprocess.run(["git", "checkout", "-q", "-b", "feature/x"], cwd=repo, check=True)
    (repo / "x.txt").write_text("content\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature commit"], cwd=repo, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                         check=True).stdout.strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    base_sha = subprocess.run(["git", "rev-parse", "main"], cwd=repo, capture_output=True, text=True,
                              check=True).stdout.strip()
    integration.store.publish_handoff(project="proj-a", task_id="t1", origin_session="role-a", branch="feature/x",
                                      commit_sha=sha, base_sha=base_sha, changed_paths=["x.txt"],
                                      artifacts={"docs_exempt": "chore"})

    result = await _call(mcp_server, "terminal_integration_loop_run_once")
    assert result["results"][0]["project"] == "proj-a"
    assert result["results"][0]["action"] == "CLAIMED"  # exactly ONE tick -- same one-step-per-call posture as tick()

    status = await _call(mcp_server, "terminal_integration_loop_status")
    assert status["last_cycle_at"] is not None
    assert status["running"] is False  # run_one_cycle never starts the background thread itself


@pytest.mark.anyio
async def test_a_completed_queue_task_with_integration_required_publishes_a_real_handoff_via_mcp(server, tmp_path):
    mcp_server, integration, queue = server
    repo = _init_repo(tmp_path / "repo")
    await _call(mcp_server, "terminal_integration_configure", project="proj-a", repo_path=str(repo))

    result = await _call(mcp_server, "terminal_queue_set", session="lane-a", tasks=[
        {"prompt": "implement the thing", "metadata": {
            "integration_required": {"project": "proj-a", "branch": "feature/x", "commit_sha": "abc123",
                                    "base_sha": "base000", "changed_paths": ["a.py"]},
        }},
    ])
    task_id = result["task_ids"][0]
    verify_result = await _call(mcp_server, "terminal_queue_verify", session="lane-a", task_id=task_id,
                                evidence={"manually_confirmed": True})
    assert "error" in verify_result  # not VERIFYING yet -- but this proves the tool path itself works end to end

    # Drive it properly through the state machine to VERIFYING first.
    queue.store.transition_task(task_id, "PRECHECK", event_type="TEST")
    queue.store.transition_task(task_id, "READY", event_type="TEST")
    queue.store.transition_task(task_id, "DISPATCHING", event_type="TEST")
    queue.store.transition_task(task_id, "RUNNING", event_type="TEST")
    queue.store.transition_task(task_id, "VERIFYING", event_type="TEST")

    verify_result = await _call(mcp_server, "terminal_queue_verify", session="lane-a", task_id=task_id,
                                evidence={"manually_confirmed": True})
    assert verify_result["task"]["status"] == "COMPLETED"

    handoffs = await _call(mcp_server, "terminal_integration_list_handoffs", project="proj-a")
    assert len(handoffs["handoffs"]) == 1
    assert handoffs["handoffs"][0]["branch"] == "feature/x"
    assert handoffs["handoffs"][0]["origin_session"] == "lane-a"


@pytest.mark.anyio
async def test_force_regression_with_nothing_pending_is_a_clean_no_op(server, tmp_path):
    mcp_server, integration, queue = server
    repo = _init_repo(tmp_path / "repo")
    await _call(mcp_server, "terminal_integration_configure", project="proj-a", repo_path=str(repo))
    result = await _call(mcp_server, "terminal_integration_force_regression", project="proj-a")
    assert result["error"] == "NOTHING_PENDING"
