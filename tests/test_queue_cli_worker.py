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


def _clean_repo(path):
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "switch", "-c", "staging/synthetic")
    _git(path, "config", "user.email", "fixture@example.invalid")
    _git(path, "config", "user.name", "Fixture")
    (path / "fixture.txt").write_text("baseline\n")
    _git(path, "add", "fixture.txt")
    _git(path, "commit", "-qm", "baseline")
    return _git(path, "rev-parse", "HEAD")


def _file_writer(path, code):
    path.write_text("#!/usr/bin/env python3\nimport re,sys\nfrom pathlib import Path\n"
                    "p=sys.stdin.read(); m=re.search(r'task id ([0-9a-f]{32})', p)\n" + code +
                    "\nprint('TMCP-DONE: '+m.group(1))\n")
    path.chmod(0o700)


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
    assert worker._argv()[1:] == ["exec", "--ephemeral", "--cd", str(repo),
                                  "--sandbox", "workspace-write", "-"]
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


def test_worker_commit_mode_commits_only_allowlisted_file_after_tests(tmp_path):
    repo = tmp_path / "repo"
    baseline = _clean_repo(repo)
    cli = tmp_path / "file-writer"
    _file_writer(cli, "Path('A.txt').write_text('accepted\\n')")
    store = QueueStore(tmp_path / "queue.db")
    task_id = _task(store, "synthetic-lane", "key-worker", "Write A.txt")
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "claude", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "commit_mode": "worker", "allowed_paths": ["A.txt"],
        "test_command": [sys.executable, "-c", "from pathlib import Path; assert Path('A.txt').read_text() == 'accepted\\n'"],
    }, worker_id="trusted-worker")
    assert worker._argv()[1:] == ["-p", "--permission-mode", "acceptEdits",
                                  "--allowedTools", "Read", "Write", "Edit"]
    sent = worker.dispatch(target="synthetic-lane", text="Write A.txt", idempotency_key="key-worker")
    assert sent.state == ap.PENDING
    deadline = time.monotonic() + 10
    observation = worker.observe(idempotency_key="key-worker")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-worker")
    assert observation.state == ap.DONE, store.get_task(task_id).metadata["cli_worker"]
    receipt = store.get_task(task_id).metadata["cli_worker"]
    assert receipt["baseline_commit_sha"] == baseline
    assert receipt["commit_sha"] != baseline and receipt["exit_code"] == 0
    assert re.fullmatch(r"[0-9a-f]{64}", receipt["test_sha"])
    assert _git(repo, "diff", "HEAD", "--name-only") == ""
    assert _git(repo, "log", "-1", "--format=%an <%ae>") == "Terminal MCP Worker <terminal-mcp-worker@localhost>"


def test_worker_commit_mode_duplicate_dispatch_does_not_spawn_twice(tmp_path):
    repo = tmp_path / "repo"
    _clean_repo(repo)
    cli = tmp_path / "slow-writer"
    _file_writer(cli, "import time; time.sleep(.2); Path('A.txt').write_text('once\\n')")
    store = QueueStore(tmp_path / "queue.db")
    task_id = _task(store, "synthetic-lane", "key-once", "Write A.txt")
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "claude", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "commit_mode": "worker", "allowed_paths": ["A.txt"],
        "test_command": [sys.executable, "-c", "from pathlib import Path; assert Path('A.txt').exists()"],
        "heartbeat_seconds": 1,
    }, worker_id="exactly-once-worker")
    first = worker.dispatch(target="synthetic-lane", text="Write A.txt", idempotency_key="key-once")
    second = worker.dispatch(target="synthetic-lane", text="Write A.txt", idempotency_key="key-once")
    assert first.state == ap.PENDING and second.state == ap.PENDING
    assert first.ref == second.ref
    deadline = time.monotonic() + 15
    observation = worker.observe(idempotency_key="key-once")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-once")
    assert observation.state == ap.DONE
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"
    assert store.get_task(task_id).metadata["cli_worker"]["pid"] == int(first.ref.removeprefix("pid:"))


def test_worker_commit_mode_blocks_unexpected_dirty_path_without_committing(tmp_path):
    repo = tmp_path / "repo"
    _clean_repo(repo)
    cli = tmp_path / "file-writer"
    _file_writer(cli, "Path('A.txt').write_text('allowed\\n'); Path('surprise.txt').write_text('no\\n')")
    store = QueueStore(tmp_path / "queue.db")
    task_id = _task(store, "synthetic-lane", "key-worker", "Write A.txt")
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "claude", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "commit_mode": "worker", "allowed_paths": ["A.txt"],
        "test_command": [sys.executable, "-c", "pass"],
    }, worker_id="trusted-worker")
    worker.dispatch(target="synthetic-lane", text="Write A.txt", idempotency_key="key-worker")
    deadline = time.monotonic() + 15
    observation = worker.observe(idempotency_key="key-worker")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-worker")
    receipt = store.get_task(task_id).metadata["cli_worker"]
    assert observation.state == ap.BLOCKED and observation.reason == "UNAUTHORIZED_PATHS:surprise.txt"
    assert receipt["commit_sha"] is None
    assert _git(repo, "rev-parse", "HEAD") == receipt["baseline_commit_sha"]


def test_worker_commit_mode_blocks_symlink_escape_and_failed_tests(tmp_path):
    repo = tmp_path / "repo"
    baseline = _clean_repo(repo)
    outside = tmp_path / "outside.txt"
    outside.write_text("must stay outside\n")
    cli = tmp_path / "symlink-writer"
    _file_writer(cli, f"Path('A.txt').symlink_to({str(outside)!r})")
    store = QueueStore(tmp_path / "queue.db")
    task_id = _task(store, "synthetic-lane", "key-symlink", "Write A.txt")
    worker = QueueStoreCLIWorker(store, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "claude", "executable": str(cli),
        "working_directory": str(repo), "isolated_worktree": True,
        "commit_mode": "worker", "allowed_paths": ["A.txt"],
        "test_command": [sys.executable, "-c", "pass"],
    }, worker_id="trusted-worker")
    worker.dispatch(target="synthetic-lane", text="Write A.txt", idempotency_key="key-symlink")
    deadline = time.monotonic() + 15
    observation = worker.observe(idempotency_key="key-symlink")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-symlink")
    assert observation.state == ap.BLOCKED and observation.reason == "SYMLINK_PATH_BLOCKED:A.txt"
    assert _git(repo, "rev-parse", "HEAD") == baseline
    assert outside.read_text() == "must stay outside\n"

    # A failing pinned test command prevents the worker from staging/committing.
    repo2 = tmp_path / "repo-failing-test"
    baseline2 = _clean_repo(repo2)
    cli2 = tmp_path / "writer2"
    _file_writer(cli2, "Path('A.txt').write_text('not accepted\\n')")
    store2 = QueueStore(tmp_path / "queue2.db")
    task2 = _task(store2, "synthetic-lane", "key-fail", "Write A.txt")
    worker2 = QueueStoreCLIWorker(store2, project_id="synthetic", lane="synthetic-lane", config={
        "enabled": True, "cli": "claude", "executable": str(cli2),
        "working_directory": str(repo2), "isolated_worktree": True,
        "commit_mode": "worker", "allowed_paths": ["A.txt"],
        "test_command": [sys.executable, "-c", "raise SystemExit(7)"],
    }, worker_id="trusted-worker-2")
    worker2.dispatch(target="synthetic-lane", text="Write A.txt", idempotency_key="key-fail")
    deadline = time.monotonic() + 15
    observation2 = worker2.observe(idempotency_key="key-fail")
    while observation2.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation2 = worker2.observe(idempotency_key="key-fail")
    assert observation2.state == ap.BLOCKED
    assert store2.get_task(task2).metadata["cli_worker"]["blocker"] == "CONFIGURED_TEST_COMMAND_FAILED"
    assert _git(repo2, "rev-parse", "HEAD") == baseline2


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
    job_b = world.verify.claim_next(verifier="independent-synthetic-qa", project_id=PROJECT)
    assert job_b and job_b.task_id == b and job_b.commit_sha == b_receipt["commit_sha"]
    assert world.verify.complete(job_b.id, job_b.claim_token, evidence={
        "tests_passed": 1, "tests_failed": 0, "exit_code": 0,
        "command": "independent synthetic acceptance check B", "commit_sha": job_b.commit_sha})["ok"]
    autopilot.tick(PROJECT)
    assert world.status(b) == "COMPLETED"


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
    deadline = time.monotonic() + 15
    observation = worker.observe(idempotency_key="key-a")
    while observation.state == ap.PENDING and time.monotonic() < deadline:
        time.sleep(.05)
        observation = worker.observe(idempotency_key="key-a")
    assert observation.state == ap.BLOCKED
    assert observation.reason == "— owner: configure the isolated staging SDK"
