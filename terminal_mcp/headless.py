"""Durable noninteractive agent runs. Never replay an uncertain launch.

Workers own a flock for their entire lifetime, independently of the MCP HTTP
process. SQLite records intent before spawn and a transactional claim before
executing the CLI. Checkpoint/cleanup never remove the working directory.
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from .session_admission import serialized_admission


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class HeadlessStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.db = self.root / 'headless.sqlite3'
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, request_key TEXT UNIQUE, name TEXT, state TEXT, data TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, task_id TEXT, at REAL, event TEXT)')
        os.chmod(self.db, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, task_id):
        with self.connect() as conn:
            row = conn.execute('SELECT data FROM runs WHERE id=?', (task_id,)).fetchone()
        return json.loads(row['data']) if row else None

    def rows(self):
        with self.connect() as conn:
            return [json.loads(r['data']) for r in conn.execute('SELECT data FROM runs')]

    def update(self, task_id, event, *, expected_states=None, **changes):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT data FROM runs WHERE id=?', (task_id,)).fetchone()
            data = json.loads(row['data'])
            if expected_states is not None and data['state'] not in expected_states:
                return data
            data.update(changes, updated_at=time.time())
            conn.execute('UPDATE runs SET state=?, data=? WHERE id=?', (data['state'], json.dumps(data), task_id))
            conn.execute('INSERT INTO events(task_id,at,event) VALUES (?,?,?)', (task_id, time.time(), event))
        return data

    def prepare(self, name, cwd, agent, prompt, key, *, limit=0, external_count=0,
                dispatch='unit', sandbox=None):
        fingerprint = digest(json.dumps([name, cwd, agent, prompt]))
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT data FROM runs WHERE request_key=?', (key,)).fetchone()
            if old:
                data = json.loads(old['data'])
                return dict(data, created=False) if data['fingerprint'] == fingerprint else {'error': 'IDEMPOTENCY_CONFLICT'}
            if conn.execute("SELECT 1 FROM runs WHERE name=? AND state!='RELEASED'", (name,)).fetchone():
                return {'error': 'SESSION_HAS_UNFINISHED_WORK'}
            count = conn.execute("SELECT count(*) FROM runs WHERE state!='RELEASED'").fetchone()[0]
            if limit and count + external_count >= limit:
                return {'error': 'NODE_AT_SESSION_CAPACITY'}
            task_id = 'hd_' + uuid.uuid4().hex
            run_dir = self.root / task_id
            run_dir.mkdir(mode=0o700)
            (run_dir / 'prompt').write_text(prompt + '\n\nReport outcome=completed only if the requested work actually succeeded; otherwise outcome=blocked. Put evidence or the blocker in summary. A normal CLI exit is not task completion.')
            os.chmod(run_dir / 'prompt', 0o600)
            data = dict(task_id=task_id, session_id=task_id, target=name, cwd=cwd,
                        agent_type=agent, state='READY', request_key=key,
                        fingerprint=fingerprint, created_at=time.time(), updated_at=time.time(),
                        delivery_ack_at=None, execution_ack_at=None, exit_code=None,
                        turn_completed=False, reason=None, checkpoint_sha256=None,
                        # 'service': the headless_worker service pulls READY rows;
                        # 'unit': the caller launched a transient unit itself.
                        dispatch=dispatch, sandbox=sandbox, heartbeat_at=None,
                        worker_pid=None, cli_pid=None)
            conn.execute('INSERT INTO runs VALUES (?,?,?,?,?)', (task_id, key, name, 'READY', json.dumps(data)))
            conn.execute('INSERT INTO events(task_id,at,event) VALUES (?,?,?)', (task_id, time.time(), 'PREFLIGHT_READY'))
        return dict(data, created=True)

    def claim(self, task_id):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT data FROM runs WHERE id=? AND state=?', (task_id, 'READY')).fetchone()
            if row is None:
                return False
            data = json.loads(row['data'])
            data.update(state='DISPATCH', worker_pid=os.getpid(), updated_at=time.time())
            conn.execute('UPDATE runs SET state=?, data=? WHERE id=?', ('DISPATCH', json.dumps(data), task_id))
            conn.execute('INSERT INTO events(task_id,at,event) VALUES (?,?,?)', (task_id, time.time(), 'WORKER_CLAIM'))
        return True

    def cancel(self, task_id):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            saved = conn.execute('SELECT data FROM runs WHERE id=?', (task_id,)).fetchone()
            if saved is None:
                return {'error': 'TASK_NOT_FOUND'}
            row = json.loads(saved['data'])
            if row['state'] in ('COMPLETED', 'RELEASED', 'CANCELLED'):
                return row
            row.update(cancel_requested=True, updated_at=time.time())
            if row['state'] == 'READY' or (row['state'] == 'BLOCKED' and row['exit_code'] is not None):
                row.update(state='CANCELLED', finished_at=time.time(), reason='OPERATOR_CANCELLED')
            conn.execute('UPDATE runs SET state=?, data=? WHERE id=?', (row['state'], json.dumps(row), task_id))
            conn.execute('INSERT INTO events(task_id,at,event) VALUES (?,?,?)', (task_id, time.time(), 'CANCEL_REQUESTED'))
        return row

    def checkpoint(self, task_id):
        row = self.get(task_id)
        if not row:
            return {'error': 'TASK_NOT_FOUND'}
        if row['state'] not in ('COMPLETED', 'BLOCKED', 'CANCELLED'):
            return {'error': 'TASK_STILL_RUNNING'}
        payload = json.dumps(row, sort_keys=True)
        path = self.root / task_id / 'checkpoint.json'
        temp = path.with_suffix('.tmp')
        with temp.open('w') as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        return self.update(task_id, 'CHECKPOINT', checkpoint_sha256=digest(payload))

    def cleanup(self, task_id):
        row = self.get(task_id)
        if not row:
            return {'error': 'TASK_NOT_FOUND'}
        if row['state'] == 'RELEASED':
            return row
        if row['state'] not in ('COMPLETED', 'CANCELLED'):
            return {'error': 'UNFINISHED_WORK_PROTECTED'}
        if not row['checkpoint_sha256']:
            return {'error': 'CHECKPOINT_REQUIRED'}
        path = self.root / task_id / 'checkpoint.json'
        if not path.exists() or digest(path.read_text()) != row['checkpoint_sha256']:
            return {'error': 'CHECKPOINT_INVALID'}
        return self.update(task_id, 'CLEANUP', state='RELEASED', released_at=time.time())


OUTCOME_SCHEMA = {'type': 'object', 'properties': {'outcome': {'type': 'string', 'enum': ['completed', 'blocked']}, 'summary': {'type': 'string'}}, 'required': ['outcome', 'summary'], 'additionalProperties': False}


def codex_argv(cwd):
    return ['codex', 'exec', '--disable', 'hooks', '--json', '--sandbox', 'workspace-write',
            '-c', 'approval_policy="never"', '-C', cwd,
            '--output-schema', str(Path(__file__).with_name('headless_outcome.schema.json')), '-']


def claude_argv(cwd, *, sandboxed=False):
    # dontAsk denies every tool that is not pre-approved. Inside a disposable
    # per-task git worktree, file edits under cwd are accepted; shell and
    # network tools stay denied either way.
    mode = 'acceptEdits' if sandboxed else 'dontAsk'
    return ['claude', '-p', '--output-format', 'stream-json', '--verbose',
            '--permission-mode', mode, '--settings', '{"disableAllHooks":true}',
            '--json-schema', json.dumps(OUTCOME_SCHEMA)]


ACTIVE_STATES = ('DISPATCH', 'ACKNOWLEDGED', 'RUNNING')
HEARTBEAT_SECONDS = 5.0


def _git(*args, timeout=60):
    return subprocess.run(['git', *args], capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def sandbox_source(cwd):
    """Pin a git checkout for an isolated per-task worktree, or None."""
    top = _git('-C', str(cwd), 'rev-parse', '--show-toplevel', timeout=15)
    head = _git('-C', str(cwd), 'rev-parse', '--verify', 'HEAD^{commit}', timeout=15)
    if top.returncode or head.returncode:
        return None
    return {'repo': top.stdout.strip(), 'base_sha': head.stdout.strip()}


def create_sandbox(store, row):
    """Create the task's own worktree on a new branch at the pinned base.

    Runs once, after the atomic claim, so a crash afterwards holds the task
    (no replay) and the worktree stays for inspection. Never removed here.
    """
    sandbox = row['sandbox']
    path = store.root / 'worktrees' / row['task_id']
    branch = 'headless/' + row['task_id']
    path.parent.mkdir(mode=0o700, exist_ok=True)
    made = _git('-C', sandbox['repo'], 'worktree', 'add', '-b', branch, str(path), sandbox['base_sha'])
    if made.returncode:
        return None
    return {'worktree': str(path), 'branch': branch}


def sandbox_evidence(path):
    head = _git('-C', path, 'rev-parse', 'HEAD', timeout=15)
    status = _git('-C', path, 'status', '--porcelain', '--untracked-files=all', timeout=30)
    changed = [line[3:] for line in status.stdout.splitlines()][:200] if not status.returncode else None
    return {'head_sha': head.stdout.strip() or None, 'changed_files': changed}


def lock_held(store, task_id):
    """True while a live worker owns the task (the flock dies with it)."""
    path = store.root / task_id / 'worker.lock'
    if not path.exists():
        return False
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def liveness(store, row, *, now=None):
    """RUNNING_VERIFIED needs execution evidence AND a live, beating worker."""
    now = time.time() if now is None else now
    held = row['state'] in ACTIVE_STATES and lock_held(store, row['task_id'])
    beat = row.get('heartbeat_at')
    fresh = bool(beat) and now - beat < 6 * HEARTBEAT_SECONDS
    return {'worker_lock_held': held, 'heartbeat_age_seconds': round(now - beat, 1) if beat else None,
            'running_verified': bool(held and fresh and row['state'] == 'RUNNING'
                                     and row.get('execution_ack_at') and row.get('cli_pid'))}


def recover_lost(store, *, stale_seconds=15):
    """Hold work whose worker died. Never replays a claimed task.

    A queued READY row owned by the worker service is NOT lost: the claim is
    atomic and happens before the CLI starts, so it is still safe to run.
    """
    held = []
    for row in store.rows():
        if row['state'] not in ('READY',) + ACTIVE_STATES:
            continue
        if row['state'] == 'READY' and row.get('dispatch') == 'service':
            continue
        if time.time() - row['updated_at'] < stale_seconds:
            continue
        lock = (store.root / row['task_id'] / 'worker.lock').open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            continue
        try:
            fresh = store.get(row['task_id'])
            if time.time() - fresh['updated_at'] >= stale_seconds:
                after = store.update(row['task_id'], 'RESTART_RECOVERY_HOLD',
                                     expected_states=('READY',) + ACTIVE_STATES,
                                     state='BLOCKED', reason='WORKER_LOST_NO_REPLAY')
                if after['state'] == 'BLOCKED':
                    held.append(row['task_id'])
        finally:
            lock.close()
    return held


def launch_unit(root, task_id):
    """Run one task in its own transient user unit.

    The unit outlives the MCP process and the worker service, so restarting
    either never kills in-flight work. A dedicated unit also avoids inheriting
    a controller's nested namespace/seccomp restrictions; no sandbox policy is
    weakened (Codex still enforces workspace-write and never approvals).
    Returns the systemd-run exit code, or None when the outcome is unknown.
    """
    try:
        return subprocess.run([
            'systemd-run', '--user', '--quiet', '--collect', '--unit=tmcp-' + task_id,
            '--property=MemoryMax=2G', '--property=CPUQuota=200%',
            '--property=NoNewPrivileges=yes', '--property=UMask=0077',
            '--setenv=PYTHONPATH=' + str(Path(__file__).resolve().parent.parent),
            '--setenv=PATH=' + os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin'),
            sys.executable, '-m', 'terminal_mcp.headless', str(root), task_id],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, close_fds=True).returncode
    except (OSError, subprocess.TimeoutExpired):
        # A timeout may still have created the unit. Reconcile, never replay.
        return None


def run_worker(root, task_id, *, argv=None, startup_timeout=60, progress_timeout=300, total_timeout=3600,
               heartbeat_seconds=HEARTBEAT_SECONDS):
    store = HeadlessStore(Path(root))
    row = store.get(task_id)
    if row is None:
        return
    lock = (store.root / task_id / 'worker.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return
    if not store.claim(task_id):
        lock.close()
        return
    proc = None
    reason = None
    final_text = None
    started = time.monotonic()
    last_progress = started
    last_beat = 0.0
    buffer = b''
    workdir = row['cwd']
    try:
        if row.get('sandbox'):
            made = create_sandbox(store, row)
            if made is None:
                store.update(task_id, 'SANDBOX_FAILED', state='BLOCKED', reason='SANDBOX_SETUP_FAILED',
                             finished_at=time.time())
                return
            row = store.update(task_id, 'SANDBOX_READY', **made)
            workdir = made['worktree']
        if argv is None:
            argv = (claude_argv(workdir, sandboxed=bool(row.get('sandbox'))) if row['agent_type'] == 'claude'
                    else codex_argv(workdir))
        prompt_file = store.root / task_id / 'prompt'
        with prompt_file.open('rb') as prompt:
            proc = subprocess.Popen(argv, cwd=workdir, stdin=prompt,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    start_new_session=True, close_fds=True)
        prompt_file.unlink(missing_ok=True)
        store.update(task_id, 'CLI_SPAWNED', cli_pid=proc.pid, worker_pid=os.getpid(),
                     heartbeat_at=time.time())
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        eof = False
        while not eof:
            now = time.monotonic()
            current = store.get(task_id)
            if current.get('cancel_requested'):
                reason = 'OPERATOR_CANCELLED'
            elif now - started > total_timeout:
                reason = 'TOTAL_TIMEOUT'
            elif not current['delivery_ack_at'] and now - started > startup_timeout:
                reason = 'DELIVERY_ACK_TIMEOUT'
            elif now - last_progress > progress_timeout:
                reason = 'EXECUTION_ACK_TIMEOUT' if not current['execution_ack_at'] else 'NO_PROGRESS_TIMEOUT'
            if reason:
                break
            if now - last_beat >= heartbeat_seconds:
                # Liveness independent of agent output: PIDs + time.
                last_beat = now
                store.update(task_id, 'HEARTBEAT', heartbeat_at=time.time(),
                             worker_pid=os.getpid(), cli_pid=proc.pid)
            for key, _ in selector.select(.1):
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    eof = True
                    break
                buffer += chunk
                if len(buffer) > 2 * 1024 * 1024:
                    reason = 'EVENT_SIZE_LIMIT'
                    break
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    # Claude's documented stream has a different vocabulary.
                    if row['agent_type'] == 'claude':
                        if event.get('type') == 'system' and event.get('subtype') == 'init':
                            event = {'type': 'thread.started'}
                        elif event.get('type') == 'assistant':
                            store.update(task_id, 'EXECUTION_ACK', state='RUNNING', execution_ack_at=time.time())
                            event = {'type': 'item.updated'}
                        elif event.get('type') == 'result':
                            if event.get('is_error') or event.get('permission_denials'):
                                reason = 'PERMISSION_DENIED' if event.get('permission_denials') else 'AGENT_REPORTED_FAILURE'
                                break
                            final_text = json.dumps(event['structured_output']) if isinstance(event.get('structured_output'), dict) else event.get('result')
                            event = {'type': 'turn.completed'}
                    kind = event.get('type')
                    if kind not in {'thread.started', 'turn.started', 'item.started', 'item.updated', 'item.completed', 'turn.completed', 'turn.failed', 'error'}:
                        continue
                    last_progress = time.monotonic()
                    if kind == 'thread.started':
                        store.update(task_id, 'DELIVERY_ACK', state='ACKNOWLEDGED', delivery_ack_at=time.time())
                    elif kind == 'turn.started':
                        store.update(task_id, 'EXECUTION_ACK', state='RUNNING', execution_ack_at=time.time())
                    elif kind == 'turn.completed':
                        store.update(task_id, 'TURN_COMPLETED', turn_completed=True)
                    elif kind in ('turn.failed', 'error'):
                        reason = 'AGENT_REPORTED_FAILURE'
                    elif kind == 'item.completed' and isinstance(event.get('item'), dict):
                        item = event['item']
                        if item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
                            final_text = item['text'][:1024 * 1024]
                    store.update(task_id, 'PROGRESS', heartbeat_at=time.time())
                if reason:
                    break
            if reason:
                break
        selector.close()
        if reason and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            exit_code = proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            exit_code = proc.wait()
        current = store.get(task_id)
        try:
            outcome = json.loads(final_text) if final_text else {}
        except (ValueError, TypeError):
            outcome = {}
        if not isinstance(outcome, dict):
            outcome = {}
        if outcome.get('outcome') == 'blocked':
            reason = reason or 'AGENT_REPORTED_BLOCKER'
        success = (outcome.get('outcome') == 'completed' and isinstance(outcome.get('summary'), str)
                   and not reason and exit_code == 0 and current['delivery_ack_at'] and
                   current['execution_ack_at'] and current['turn_completed'] and final_text is not None)
        evidence = sandbox_evidence(workdir) if row.get('sandbox') else {}
        store.update(task_id, 'PROCESS_EXIT', state='COMPLETED' if success else ('CANCELLED' if reason == 'OPERATOR_CANCELLED' else 'BLOCKED'),
                     exit_code=exit_code, result=final_text, heartbeat_at=time.time(), **evidence,
                     reason=None if success else reason or 'MISSING_STRUCTURED_COMPLETION',
                     finished_at=time.time())
    except Exception:
        # Raw CLI diagnostics can contain prompts/keys. Never expose them.
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        store.update(task_id, 'WORKER_FAILURE', state='BLOCKED', reason='WORKER_FAILURE', finished_at=time.time())
    finally:
        (store.root / task_id / 'prompt').unlink(missing_ok=True)
        lock.close()


if __name__ == '__main__':
    run_worker(Path(sys.argv[1]), sys.argv[2])


class HeadlessManager:
    """Local opt-in transport using the deployment's existing permission floors.

    tmux grants are pinned to tmux identities and cannot authorize these new
    process identities. Initially requires the operator's default read+input
    policy; explicit denials still win. Never silently moves remote work here.
    """
    def __init__(self, terminal, root, *, direct=None, enabled=False, dispatch='unit', sandbox=False):
        self.terminal = terminal
        self.store = HeadlessStore(Path(root))
        self.direct = direct
        self.enabled = enabled
        # 'service': only persist READY; terminal_mcp.headless_worker runs it.
        self.dispatch = dispatch if dispatch in ('unit', 'service') else 'unit'
        self.sandbox = sandbox
        self.children = []
        if terminal is not None:
            terminal.headless_store = self.store

    def guard(self, name, *, write=False):
        from .permissions import require_read, require_input, require_session_lifecycle
        terminal = self.terminal
        error = terminal.lifecycle.validate_create(name, 'codex')
        if error:
            return error
        for check in (require_read, require_input, require_session_lifecycle) if write else (require_read,):
            error = check(terminal.config)
            if error:
                return {'error': error}
        grant = terminal.grants.get(name)
        if not terminal.config.session_access.default_read or not terminal._read_authorized_with_grant(name, grant):
            return {'error': 'ACCESS_DENIED'}
        if write:
            # Existing input grants cannot be inherited by a new identity.
            allowed, specific = terminal._input_authorized_with_grant(name, None)
            if not allowed or (grant is not None and (not grant.read_enabled or not grant.input_enabled)):
                return {'error': specific or 'HEADLESS_DEFAULT_INPUT_REQUIRED'}
        return None

    @serialized_admission
    def start(self, name, prompt, cwd, key, *, agent='codex'):
        if not self.enabled:
            return {'error': 'HEADLESS_DISABLED'}
        if not isinstance(name, str) or not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 65536:
            return {'error': 'INVALID_HEADLESS_REQUEST'}
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            return {'error': 'IDEMPOTENCY_KEY_REQUIRED'}
        if agent not in ('codex', 'claude'):
            return {'error': 'HEADLESS_AGENT_NOT_SUPPORTED', 'agent_type': agent}
        if error := self.guard(name, write=True):
            return error
        from .lifecycle import resolve_cwd
        resolved, error = resolve_cwd(cwd, self.terminal.config)
        if error:
            return error
        # Audit real inventory before admission; failure is never an empty node.
        try:
            sessions = self.terminal.tmux.list_sessions()
            if any(s.name == name for s in sessions):
                return {'error': 'SESSION_ALREADY_EXISTS'}
            if self.direct and self.direct.active_for(name):
                return {'error': 'TARGET_HAS_ACTIVE_SUPERVISED_TASK'}
            limit, _ = self.terminal._session_capacity()
        except Exception:
            return {'error': 'SESSION_AUDIT_UNAVAILABLE'}
        # Idempotent retries never launch again, even after controller restart.
        old = next((r for r in self.store.rows() if r['request_key'] == key), None)
        if old:
            expected = digest(json.dumps([name, str(resolved), agent, prompt]))
            return self.status(old['task_id']) if old['fingerprint'] == expected else {'error': 'IDEMPOTENCY_CONFLICT'}
        try:
            if agent == 'codex':
                help_result = subprocess.run(['codex', 'exec', '--help'], capture_output=True, text=True, timeout=10)
                features = subprocess.run(['codex', '--disable', 'hooks', 'features', 'list'], capture_output=True, text=True, timeout=10)
                supported = (help_result.returncode == features.returncode == 0 and '--json' in help_result.stdout and any(
                    line.startswith('hooks') and line.rstrip().endswith('false') for line in features.stdout.splitlines()))
            else:
                help_result = subprocess.run(['claude', '--help'], capture_output=True, text=True, timeout=10)
                supported = help_result.returncode == 0 and all(flag in help_result.stdout for flag in ('--settings', 'dontAsk', 'stream-json'))
            if not supported:
                return {'error': 'HEADLESS_HOOK_ISOLATION_UNSUPPORTED'}
        except (OSError, subprocess.TimeoutExpired):
            return {'error': 'HEADLESS_CLI_PREFLIGHT_FAILED'}
        sandbox = None
        if self.sandbox:
            sandbox = sandbox_source(resolved)
            if sandbox is None:
                return {'error': 'HEADLESS_SANDBOX_REQUIRES_GIT_CHECKOUT', 'cwd': str(resolved)}
        row = self.store.prepare(name, str(resolved), agent, prompt, key, limit=limit, external_count=len(sessions),
                                 dispatch=self.dispatch, sandbox=sandbox)
        if 'error' in row:
            return row
        if not row.get('created') or self.dispatch == 'service':
            return self.status(row['task_id'])
        # Transactional worker claim also protects simultaneous identical calls.
        if launch_unit(self.store.root, row['task_id']):
            self.store.update(row['task_id'], 'SPAWN_FAILED', expected_states=('READY',), state='BLOCKED', reason='WORKER_UNIT_START_FAILED')
        return self.status(row['task_id'])

    def reconcile(self):
        self.children = [child for child in self.children if child.poll() is None]
        recover_lost(self.store)

    def status(self, task_id):
        row = self.store.get(task_id)
        if not row:
            return {'error': 'TASK_NOT_FOUND'}
        if error := self.guard(row['target']):
            return error
        return {'status': 'OK', 'task': row, 'liveness': liveness(self.store, row)}

    def turn(self, action, *, target=None, text=None, cwd=None, key=None, task_id=None, agent='codex'):
        if action == 'headless_start':
            return self.start(target, text, cwd, key, agent=agent)
        if action == 'headless_list':
            return {'status': 'OK', 'enabled': self.enabled, 'sessions': [
                {key: row.get(key) for key in ('task_id', 'session_id', 'target', 'agent_type', 'state', 'reason', 'checkpoint_sha256')}
                for row in self.store.rows() if not self.guard(row['target'])]}
        row = self.store.get(task_id)
        if not row:
            return {'error': 'TASK_NOT_FOUND'}
        if error := self.guard(row['target'], write=action != 'headless_status'):
            return error
        if action == 'headless_status':
            return self.status(task_id)
        if action == 'headless_checkpoint':
            result = self.store.checkpoint(task_id)
        elif action == 'headless_cancel':
            result = self.store.cancel(task_id)
        elif action == 'headless_cleanup':
            result = self.store.cleanup(task_id)
        else:
            return {'error': 'INVALID_ACTION'}
        return result if 'error' in result else {'status': 'OK', 'task': result}
