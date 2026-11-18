"""Issue #106: noninteractive CLI worker service on the existing HeadlessStore.

The CLI is a fake Codex `exec --json` stream; it never touches tmux. Real
`claude -p` / `codex exec` runs are covered by the staging canary.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from terminal_mcp.headless import (HeadlessStore, claude_argv, liveness, recover_lost, run_worker,
                                   sandbox_source)
from terminal_mcp.headless_worker import WorkerService

ROOT = Path(__file__).resolve().parent.parent

FAKE = '''import json, os, sys, time
sys.stdin.read()
delay = float(os.environ.get("FAKE_DELAY", "0"))
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(os.environ.get("FAKE_TAG", "") + " " + os.getcwd() + "\\n")
print(json.dumps({"type": "thread.started"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
time.sleep(delay)
open("out.txt", "w").write("done")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
      "text": json.dumps({"outcome": "completed", "summary": "wrote out.txt"})}}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
'''


def fake(tmp_path, delay=0.0):
    script = tmp_path / 'fake_cli.py'
    script.write_text(FAKE)
    os.environ['FAKE_LOG'] = str(tmp_path / 'runs.log')
    os.environ['FAKE_DELAY'] = str(delay)
    return [sys.executable, str(script)]


def git_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for args in (['init', '-q'], ['config', 'user.email', 't@example.invalid'], ['config', 'user.name', 't']):
        subprocess.run(['git', '-C', str(repo), *args], check=True)
    (repo / 'README').write_text('base\n')
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'base'], check=True)
    return repo


def thread_launcher(argv, running, **kw):
    def launch(root, task_id):
        def body():
            running.append(task_id)
            try:
                run_worker(root, task_id, argv=argv, heartbeat_seconds=.05, **kw)
            finally:
                running.remove(task_id)
        threading.Thread(target=body, daemon=True).start()
    return launch


def drain(service, store, ids, timeout=30, peak=None, running=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        service.tick()
        if peak is not None:
            peak.append(len(running))
        if all(store.get(i)['state'] in ('COMPLETED', 'BLOCKED', 'CANCELLED') for i in ids):
            return
        time.sleep(.05)
    raise AssertionError([store.get(i)['state'] for i in ids])


def test_ten_tasks_run_exactly_once_within_parallel_limit_and_replay_dedups(tmp_path):
    argv = fake(tmp_path, delay=.2)
    store = HeadlessStore(tmp_path / 'state')
    work = tmp_path / 'work'
    work.mkdir()
    rows = [store.prepare(f'hd-canary-{n}', str(work), 'codex', f'task {n}', f'key-{n}', dispatch='service')
            for n in range(10)]
    ids = [r['task_id'] for r in rows]
    running, peak = [], []
    service = WorkerService(store.root, max_parallel=3, launch=thread_launcher(argv, running))
    drain(service, store, ids, peak=peak, running=running)
    done = [store.get(i) for i in ids]
    assert {r['state'] for r in done} == {'COMPLETED'}
    assert all(r['exit_code'] == 0 and r['cli_pid'] and r['worker_pid'] and r['heartbeat_at'] for r in done)
    assert max(peak) <= 3
    assert len((tmp_path / 'runs.log').read_text().splitlines()) == 10
    # Replay after completion: same ids, nothing new queued or launched.
    replay = [store.prepare(f'hd-canary-{n}', str(work), 'codex', f'task {n}', f'key-{n}', dispatch='service')
              for n in range(10)]
    assert [r['task_id'] for r in replay] == ids and not any(r['created'] for r in replay)
    assert service.tick()['launched'] == []
    assert len((tmp_path / 'runs.log').read_text().splitlines()) == 10


def test_service_queue_is_not_mistaken_for_lost_work(tmp_path):
    store = HeadlessStore(tmp_path)
    queued = store.prepare('a', str(tmp_path), 'codex', 'p', 'k1', dispatch='service')
    direct = store.prepare('b', str(tmp_path), 'codex', 'p', 'k2')
    assert recover_lost(store, stale_seconds=0) == [direct['task_id']]
    assert store.get(queued['task_id'])['state'] == 'READY'


def test_worktree_sandbox_isolates_changes_and_records_git_evidence(tmp_path):
    argv = fake(tmp_path)
    repo = git_repo(tmp_path)
    sandbox = sandbox_source(repo)
    store = HeadlessStore(tmp_path / 'state')
    row = store.prepare('hd-sbx', str(repo), 'codex', 'write out.txt', 'sbx', dispatch='service', sandbox=sandbox)
    run_worker(store.root, row['task_id'], argv=argv)
    done = store.get(row['task_id'])
    assert done['state'] == 'COMPLETED', done
    worktree = Path(done['worktree'])
    assert (worktree / 'out.txt').read_text() == 'done'
    assert not (repo / 'out.txt').exists()
    assert done['branch'] == 'headless/' + row['task_id']
    assert done['head_sha'] == sandbox['base_sha'] and done['changed_files'] == ['out.txt']
    assert subprocess.run(['git', '-C', str(repo), 'status', '--porcelain'], capture_output=True,
                          text=True).stdout == ''
    assert (tmp_path / 'runs.log').read_text().split()[-1] == str(worktree)
    # Checkpoint + release never deletes the worktree or its uncommitted work.
    store.checkpoint(row['task_id'])
    assert store.cleanup(row['task_id'])['state'] == 'RELEASED'
    assert (worktree / 'out.txt').exists()


def test_sandbox_setup_failure_blocks_without_running_the_cli(tmp_path):
    argv = fake(tmp_path)
    store = HeadlessStore(tmp_path / 'state')
    row = store.prepare('hd-bad', str(tmp_path), 'codex', 'p', 'bad', dispatch='service',
                        sandbox={'repo': str(tmp_path / 'missing'), 'base_sha': '0' * 40})
    run_worker(store.root, row['task_id'], argv=argv)
    assert store.get(row['task_id'])['reason'] == 'SANDBOX_SETUP_FAILED'
    assert not (tmp_path / 'runs.log').exists()
    assert sandbox_source(tmp_path / 'state') is None


def test_heartbeat_pid_and_running_verified_while_alive(tmp_path):
    argv = fake(tmp_path, delay=1.5)
    store = HeadlessStore(tmp_path / 'state')
    row = store.prepare('hd-beat', str(tmp_path), 'codex', 'p', 'beat', dispatch='service')
    worker = threading.Thread(target=run_worker, args=(store.root, row['task_id']),
                              kwargs={'argv': argv, 'heartbeat_seconds': .05})
    worker.start()
    deadline = time.monotonic() + 10
    while store.get(row['task_id'])['state'] != 'RUNNING' and time.monotonic() < deadline:
        time.sleep(.02)
    time.sleep(.3)
    live = store.get(row['task_id'])
    first_beat = live['heartbeat_at']
    assert live['worker_pid'] == os.getpid()
    os.kill(live['cli_pid'], 0)                       # the CLI process really exists
    assert liveness(store, live)['running_verified'] is True
    time.sleep(.3)
    assert store.get(row['task_id'])['heartbeat_at'] > first_beat
    worker.join(10)
    done = store.get(row['task_id'])
    assert done['state'] == 'COMPLETED' and done['exit_code'] == 0
    assert liveness(store, done)['running_verified'] is False


def test_killed_worker_is_held_never_replayed_and_queue_survives_restart(tmp_path):
    argv = fake(tmp_path, delay=30)
    store = HeadlessStore(tmp_path / 'state')
    lost = store.prepare('hd-lost', str(tmp_path), 'codex', 'p', 'lost', dispatch='service')
    queued = store.prepare('hd-next', str(tmp_path), 'codex', 'p2', 'next', dispatch='service')
    code = ('import sys; from terminal_mcp.headless import run_worker; '
            'run_worker(sys.argv[1], sys.argv[2], argv=sys.argv[3:], heartbeat_seconds=.05)')
    proc = subprocess.Popen([sys.executable, '-c', code, str(store.root), lost['task_id'], *argv],
                            env=dict(os.environ, PYTHONPATH=str(ROOT)))
    deadline = time.monotonic() + 15
    while store.get(lost['task_id'])['state'] != 'RUNNING' and time.monotonic() < deadline:
        time.sleep(.05)
    cli_pid = store.get(lost['task_id'])['cli_pid']
    proc.send_signal(signal.SIGKILL)                  # worker crashes mid-run
    proc.wait()
    os.killpg(cli_pid, signal.SIGKILL)
    # A brand-new service process: only SQLite survived.
    launches = []
    service = WorkerService(store.root, max_parallel=2, stale_seconds=0,
                            launch=lambda root, task_id: launches.append(task_id))
    first = service.tick()
    assert first['held_lost'] == [lost['task_id']]
    held = store.get(lost['task_id'])
    assert held['state'] == 'BLOCKED' and held['reason'] == 'WORKER_LOST_NO_REPLAY'
    assert launches == [queued['task_id']]
    assert not store.claim(lost['task_id'])
    # Restart again before the queued task was claimed: not launched twice.
    assert WorkerService(store.root, max_parallel=2, stale_seconds=0,
                         launch=lambda root, task_id: launches.append(task_id)).tick()['launched'] == []
    assert len((tmp_path / 'runs.log').read_text().splitlines()) == 1


def test_unclaimed_launches_are_bounded_then_blocked(tmp_path):
    store = HeadlessStore(tmp_path)
    row = store.prepare('hd-stuck', str(tmp_path), 'codex', 'p', 'stuck', dispatch='service')
    calls = []
    service = WorkerService(tmp_path, relaunch_after=0, max_launch_attempts=3,
                            launch=lambda root, task_id: calls.append(task_id))
    for _ in range(5):
        service.tick()
    assert len(calls) == 3
    blocked = store.get(row['task_id'])
    assert blocked['state'] == 'BLOCKED' and blocked['reason'] == 'WORKER_UNIT_START_FAILED'


def test_cancelled_queue_entry_is_never_launched(tmp_path):
    store = HeadlessStore(tmp_path)
    row = store.prepare('hd-c', str(tmp_path), 'codex', 'p', 'c', dispatch='service')
    store.cancel(row['task_id'])
    calls = []
    WorkerService(tmp_path, launch=lambda root, task_id: calls.append(task_id)).tick()
    assert calls == [] and store.get(row['task_id'])['state'] == 'CANCELLED'


def test_claude_may_edit_only_inside_a_sandbox_worktree():
    plain, sandboxed = claude_argv('/x'), claude_argv('/x', sandboxed=True)
    assert plain[plain.index('--permission-mode') + 1] == 'dontAsk'
    assert sandboxed[sandboxed.index('--permission-mode') + 1] == 'acceptEdits'
    assert '--dangerously-skip-permissions' not in sandboxed
    assert json.loads(sandboxed[sandboxed.index('--settings') + 1]) == {'disableAllHooks': True}


def test_service_health_file_reports_queue(tmp_path):
    store = HeadlessStore(tmp_path)
    store.prepare('hd-h', str(tmp_path), 'codex', 'p', 'h', dispatch='service')
    WorkerService(tmp_path, launch=lambda *a: None).tick()
    health = json.loads((tmp_path / 'worker-service.json').read_text())
    assert health['pid'] == os.getpid() and health['launched'] and health['max_parallel'] == 2
