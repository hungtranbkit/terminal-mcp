"""Headless lifecycle acceptance: durable intent, structured evidence, no replay."""
import sys
import time
from pathlib import Path

from terminal_mcp.headless import HeadlessStore, run_worker


def task(tmp_path, script):
    store = HeadlessStore(tmp_path)
    row = store.prepare('lab', str(tmp_path), 'codex', 'hello', 'key-1')
    launcher = tmp_path / 'fake.py'
    launcher.write_text(script)
    return store, row, [sys.executable, str(launcher)]


def test_structured_completion_and_idempotent_restart(tmp_path):
    store, row, argv = task(tmp_path, '''import json,sys
sys.stdin.read()
for event in [{'type':'thread.started'}, {'type':'turn.started'}, {'type':'item.completed','item':{'type':'agent_message','text':'{\"outcome\":\"completed\",\"summary\":\"ok\"}'}}, {'type':'turn.completed'}]:
 print(json.dumps(event), flush=True)
''')
    run_worker(store.root, row['task_id'], argv=argv)
    first = store.get(row['task_id'])
    assert first['state'] == 'COMPLETED'
    assert first['delivery_ack_at'] and first['execution_ack_at']
    assert first['exit_code'] == 0 and first['turn_completed']
    run_worker(store.root, row['task_id'], argv=argv)
    assert store.get(row['task_id']) == first
    restored = HeadlessStore(tmp_path)
    assert restored.prepare('lab', str(tmp_path), 'codex', 'hello', 'key-1')['task_id'] == row['task_id']
    assert restored.prepare('lab', str(tmp_path), 'codex', 'different', 'key-1')['error'] == 'IDEMPOTENCY_CONFLICT'
    assert restored.cleanup(row['task_id'])['error'] == 'CHECKPOINT_REQUIRED'
    assert restored.checkpoint(row['task_id'])['checkpoint_sha256']
    assert restored.cleanup(row['task_id'])['state'] == 'RELEASED'


def test_marker_alone_is_not_completion(tmp_path):
    store, row, argv = task(tmp_path, "print('TMCP-DONE:hd_fake')")
    run_worker(store.root, row['task_id'], argv=argv)
    assert store.get(row['task_id'])['state'] == 'BLOCKED'
    assert store.get(row['task_id'])['reason'] == 'MISSING_STRUCTURED_COMPLETION'


def test_nonresponsive_agent_times_out_without_replay(tmp_path):
    store, row, argv = task(tmp_path, 'import time; time.sleep(30)')
    before = time.monotonic()
    run_worker(store.root, row['task_id'], argv=argv, startup_timeout=.15, progress_timeout=.2)
    assert time.monotonic() - before < 5
    assert store.get(row['task_id'])['reason'] == 'DELIVERY_ACK_TIMEOUT'
    assert store.cleanup(row['task_id'])['error'] == 'UNFINISHED_WORK_PROTECTED'


def test_active_name_cannot_be_replaced(tmp_path):
    store, row, _ = task(tmp_path, '')
    assert store.prepare('lab', str(tmp_path), 'codex', 'second', 'key-2')['error'] == 'SESSION_HAS_UNFINISHED_WORK'


def test_restart_lost_worker_holds_work_and_never_replays(tmp_path):
    from terminal_mcp.headless import HeadlessManager
    store, row, _ = task(tmp_path, '')
    assert store.claim(row['task_id'])
    with store.connect() as conn:
        import json
        data = store.get(row['task_id'])
        data['updated_at'] = 0
        conn.execute('UPDATE runs SET data=? WHERE id=?', (json.dumps(data), row['task_id']))
    manager = HeadlessManager(None, tmp_path)
    manager.reconcile()
    assert store.get(row['task_id'])['reason'] == 'WORKER_LOST_NO_REPLAY'
    assert not store.claim(row['task_id'])


def test_hook_isolation_and_permissions_are_explicit():
    from terminal_mcp.headless import codex_argv
    argv = codex_argv('/tmp/test')
    assert argv[argv.index('--disable') + 1] == 'hooks'
    assert 'approval_policy="never"' in argv
    assert '--dangerously-bypass-approvals-and-sandbox' not in argv


def test_policy_floor_applies_to_headless(tmp_path):
    from dataclasses import replace
    from terminal_mcp.headless import HeadlessManager
    from terminal_mcp.config import load_config, SessionAccessConfig
    from terminal_mcp.core import TerminalService
    terminal = TerminalService(replace(load_config(), session_access=SessionAccessConfig(default_read=True, default_input=False)))
    manager = HeadlessManager(terminal, tmp_path, enabled=True)
    assert manager.start('root-repair', 'hello', str(tmp_path), 'k')['error'] == 'SENSITIVE_SESSION_NOT_CREATABLE'
    assert manager.start('lab', 'hello', str(tmp_path), 'k')['error'] == 'HEADLESS_DEFAULT_INPUT_REQUIRED'


def test_checkpoint_tamper_never_releases(tmp_path):
    store, row, _ = task(tmp_path, '')
    store.update(row['task_id'], 'test', state='COMPLETED')
    store.checkpoint(row['task_id'])
    (tmp_path / row['task_id'] / 'checkpoint.json').write_text('altered')
    assert store.cleanup(row['task_id'])['error'] == 'CHECKPOINT_INVALID'


def test_successful_cli_exit_with_business_blocker_is_not_complete(tmp_path):
    store, row, argv = task(tmp_path, '''import json
for event in [{'type':'thread.started'}, {'type':'turn.started'}, {'type':'item.completed','item':{'type':'agent_message','text':'{"outcome":"blocked","summary":"sandbox unavailable"}'}}, {'type':'turn.completed'}]:
 print(json.dumps(event), flush=True)
''')
    run_worker(store.root, row['task_id'], argv=argv)
    assert store.get(row['task_id'])['exit_code'] == 0
    assert store.get(row['task_id'])['reason'] == 'AGENT_REPORTED_BLOCKER'
    assert store.cleanup(row['task_id'])['error'] == 'UNFINISHED_WORK_PROTECTED'


def test_cleanup_never_removes_worktree(tmp_path):
    store, row, _ = task(tmp_path, '')
    work = tmp_path / 'unsaved.txt'
    work.write_text('precious uncommitted work')
    store.update(row['task_id'], 'test', state='COMPLETED')
    store.checkpoint(row['task_id'])
    store.cleanup(row['task_id'])
    assert work.read_text() == 'precious uncommitted work'


def test_cancel_before_claim_prevents_execution_and_allows_checkpoint(tmp_path):
    store, row, argv = task(tmp_path, "raise Exception('must never launch')")
    assert store.cancel(row['task_id'])['state'] == 'CANCELLED'
    run_worker(store.root, row['task_id'], argv=argv)
    assert store.get(row['task_id'])['state'] == 'CANCELLED'
    store.checkpoint(row['task_id'])
    assert store.cleanup(row['task_id'])['state'] == 'RELEASED'


def test_only_one_caller_owns_launch_under_concurrent_retry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = HeadlessStore(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda _: store.prepare('lab', str(tmp_path), 'codex', 'hello', 'same'), range(8)))
    assert len({r['task_id'] for r in rows}) == 1
    assert sum(bool(r['created']) for r in rows) == 1


def test_recovery_cannot_overwrite_completed_state(tmp_path):
    store, row, _ = task(tmp_path, '')
    store.update(row['task_id'], 'test', state='COMPLETED')
    result = store.update(row['task_id'], 'RESTART_RECOVERY_HOLD', expected_states=('RUNNING',), state='BLOCKED')
    assert result['state'] == 'COMPLETED'


def test_tmux_create_refuses_headless_owned_name(tmp_path):
    from terminal_mcp.config import load_config
    from terminal_mcp.core import TerminalService
    from terminal_mcp.headless import HeadlessManager
    terminal = TerminalService(load_config())
    manager = HeadlessManager(terminal, tmp_path)
    manager.store.prepare('owned', str(tmp_path), 'codex', 'hello', 'key')
    assert terminal.terminal_create_session('owned')['error'] == 'SESSION_HAS_HEADLESS_WORK'


def test_periodic_cleanup_preserves_blocked_work(tmp_path):
    from terminal_mcp.headless_cleanup import sweep
    store, row, _ = task(tmp_path, '')
    store.update(row['task_id'], 'test', state='BLOCKED', finished_at=0)
    assert sweep(store, grace_seconds=0)['released'] == 0
    assert store.get(row['task_id'])['state'] == 'BLOCKED'
