import os
import re
import subprocess
import sys
import time

from terminal_mcp import autopilot as ap
from terminal_mcp.queue_cli_worker import QueueStoreCLIWorker
from terminal_mcp.queue_store import DISPATCHING, QueueStore
from tests.test_autopilot import PROJECT, World


def _task(store, lane, key, text):
    task_id = store.set_tasks(lane, [{"prompt": text, "project_id": "synthetic"}])[0]
    store.transition_task(task_id, "PRECHECK", event_type="TEST_PRECHECK")
    store.transition_task(task_id, "READY", event_type="TEST_READY")
    store.transition_task(task_id, DISPATCHING, event_type="TEST_DISPATCH",
                          extra_fields={"dispatch_idempotency_key": key})
    return task_id


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_noninteractive_cli_runner_records_pid_heartbeat_exit_and_exact_shas(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "switch", "-c", "staging/synthetic")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    _git(repo, "config", "user.name", "Synthetic")
    (repo / "base.txt").write_text("synthetic fixture\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-qm", "synthetic fixture")
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/usr/bin/env python3\nimport re,subprocess,sys\nfrom pathlib import Path\n"
                   "p=sys.stdin.read(); m=re.search(r'task id ([0-9a-f]{32})', p)\n"
                   "name='B.txt' if 'synthetic B' in p else 'A.txt'\nPath(name).write_text('acceptance '+name[0]+'\\n')\n"
                   "subprocess.run(['git','add',name],check=True)\n"
                   "subprocess.run(['git','commit','-m','synthetic '+name],check=True)\n"
                   "print('TMCP-DONE:'+m.group(1))\n")
    cli.chmod(0o700)
    store = QueueStore(tmp_path / "queue.db")
    task_id = _task(store, "synthetic-lane", "key-a", "Implement synthetic A")
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "codex", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "test_command": [sys.executable, "-c", "from pathlib import Path; assert Path('A.txt').is_file() or Path('B.txt').is_file()"],
        "timeout_seconds": 60, "heartbeat_seconds": 1,
    }, worker_id="staging-worker")
    sent = worker.dispatch(target="synthetic-lane", text="Implement synthetic A",
                           idempotency_key="key-a")
    assert sent.state == ap.PENDING and sent.ref.startswith("pid:")
    deadline = time.monotonic() + 10
    observation = worker.observe(idempotency_key="key-a")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-a")
    assert observation.state == ap.DONE, (observation, store.get_task(task_id).metadata["cli_worker"])
    receipt = store.get_task(task_id).metadata["cli_worker"]
    assert receipt["pid"] > 0 and receipt["heartbeat_at"]
    assert receipt["exit_code"] == 0
    assert re.fullmatch(r"[0-9a-f]{40}", receipt["commit_sha"])
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["test_sha"])
    assert store.get_task(task_id).status == DISPATCHING  # independent QA still owns completion


def test_worker_restart_holds_claimed_dead_process_without_replay(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "switch", "-c", "staging/synthetic")
    (repo / "base.txt").write_text("fixture\n")
    _git(repo, "add", "base.txt")
    _git(repo, "-c", "user.email=synthetic@example.invalid", "-c", "user.name=Synthetic",
         "commit", "-qm", "fixture")
    store = QueueStore(tmp_path / "queue.db")
    _task(store, "synthetic-lane", "key-a", "A")
    store.claim_cli_worker(store.list_tasks_for_project("synthetic")[0].id,
                           dispatch_key="key-a", worker_id="crashed-worker", pid=2_000_000)
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "codex", "working_directory": str(repo),
        "isolated_worktree": True, "test_command": [sys.executable, "-c", "pass"],
    }, worker_id="restarted-worker")
    observation = worker.observe(idempotency_key="key-a")
    assert observation.state == ap.BLOCKED
    assert "WORKER_LOST_NO_REPLAY" in observation.reason
    assert len([e for e in store.get_task(store.list_tasks_for_project("synthetic")[0].id).metadata
                if e == "cli_worker"]) == 1


def test_autopilot_cli_a_qa_b_runs_without_manual_redispatch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "switch", "-c", "staging/synthetic")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    _git(repo, "config", "user.name", "Synthetic")
    (repo / "fixture.txt").write_text("synthetic\n")
    _git(repo, "add", "fixture.txt")
    _git(repo, "commit", "-qm", "fixture")
    cli = tmp_path / "synthetic-cli"
    cli.write_text("#!/usr/bin/env python3\nimport re,subprocess,sys\nfrom pathlib import Path\n"
                   "p=sys.stdin.read(); m=re.search(r'task id ([0-9a-f]{32})', p)\n"
                   "name='B.txt' if 'B in the synthetic repo' in p else 'A.txt'\nPath(name).write_text('acceptance '+name[0]+'\\n')\n"
                   "subprocess.run(['git','add',name],check=True)\n"
                   "subprocess.run(['git','commit','-m','synthetic '+name],check=True)\n"
                   "print('TMCP-DONE:'+m.group(1))\n")
    cli.chmod(0o700)
    world = World(tmp_path / "world", lanes=("synthetic-lane",))
    lane = "synthetic-lane"
    a = world.add(lane, "A")
    b = world.add(lane, "B", depends_on=[a])
    worker = QueueStoreCLIWorker(world.store, project_id=PROJECT, lane=lane, config={
        "enabled": True, "cli": "codex", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "test_command": [sys.executable, "-c", "from pathlib import Path; assert Path('A.txt').is_file() or Path('B.txt').is_file()"],
        "timeout_seconds": 60, "heartbeat_seconds": 1,
    }, worker_id="synthetic-autopilot-worker")
    dispatcher = ap.ProjectDispatcher(world.dispatcher, {lane: worker})
    autopilot = world.autopilot(dispatcher=dispatcher)
    autopilot.tick(PROJECT)
    deadline = time.monotonic() + 10
    while world.status(a) != "VERIFYING" and time.monotonic() < deadline:
        time.sleep(.05)
        autopilot.tick(PROJECT)
    assert world.status(a) == "VERIFYING", (world.store.get_task(a).last_error,
                                               world.store.get_task(a).metadata)
    a_receipt = world.store.get_task(a).metadata["cli_worker"]
    job = world.verify.claim_next(verifier="independent-synthetic-qa", project_id=PROJECT)
    assert job and job.task_id == a and job.commit_sha == a_receipt["commit_sha"]
    assert world.verify.complete(job.id, job.claim_token, evidence={
        "tests_passed": 1, "tests_failed": 0, "exit_code": 0,
        "command": "synthetic independent QA", "commit_sha": job.commit_sha})["ok"]
    autopilot.tick(PROJECT)  # controller continues without user resubmission
    assert world.status(b) == DISPATCHING
    deadline = time.monotonic() + 10
    while world.status(b) != "VERIFYING" and time.monotonic() < deadline:
        time.sleep(.05)
        autopilot.tick(PROJECT)
    assert world.status(b) == "VERIFYING"
    b_receipt = world.store.get_task(b).metadata["cli_worker"]
    assert a_receipt["pid"] != b_receipt["pid"]
    assert a_receipt["commit_sha"] != b_receipt["commit_sha"]
    assert a_receipt["test_sha"] and b_receipt["test_sha"]


def test_explicit_cli_blocker_is_persisted_as_owner_action(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "switch", "-c", "staging/synthetic")
    _git(repo, "config", "user.email", "synthetic@example.invalid")
    _git(repo, "config", "user.name", "Synthetic")
    (repo / "fixture.txt").write_text("synthetic\n")
    _git(repo, "add", "fixture.txt")
    _git(repo, "commit", "-qm", "fixture")
    cli = tmp_path / "blocked-cli"
    cli.write_text("#!/usr/bin/env python3\nimport re,sys\np=sys.stdin.read()\n"
                   "m=re.search(r'task id ([0-9a-f]{32})',p)\n"
                   "print('TMCP-BLOCKED: '+m.group(1)+' — owner: configure the isolated staging SDK')\n")
    cli.chmod(0o700)
    store = QueueStore(tmp_path / "queue.db")
    _task(store, "synthetic-lane", "key-a", "Synthetic blocked task")
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "codex", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "test_command": [sys.executable, "-c", "pass"],
    }, worker_id="blocked-worker")
    worker.dispatch(target="synthetic-lane", text="Synthetic blocked task", idempotency_key="key-a")
    deadline = time.monotonic() + 5
    observation = worker.observe(idempotency_key="key-a")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-a")
    assert observation.state == ap.BLOCKED
    assert observation.reason == "— owner: configure the isolated staging SDK"
