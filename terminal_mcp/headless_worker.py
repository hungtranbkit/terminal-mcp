"""Long-running pull worker for durable headless agent tasks (issue #106).

Runs as a systemd user service. It owns no new queue: it reads the existing
HeadlessStore (headless.sqlite3), where `headless_start` persisted each task
as READY before anything ran. Each tick it

1. holds lost work: a claimed task whose worker flock is free is BLOCKED with
   WORKER_LOST_NO_REPLAY and never started again;
2. launches queued READY tasks, oldest first, up to `max_parallel` active.
   Each task runs `claude -p` / `codex exec` in its own transient user unit,
   so restarting this service never kills in-flight agents. No tmux, no
   keypresses.

Launching a READY task twice is harmless: `HeadlessStore.claim` moves
READY -> DISPATCH atomically before the CLI starts, so at most one worker
ever executes a task. A launch is retried after `relaunch_after` seconds
only while the row is still READY, at most `max_launch_attempts` times.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import threading
import time

from .headless import ACTIVE_STATES, HeadlessStore, launch_unit, recover_lost


def default_root() -> Path:
    state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state')))
    return Path(os.environ.get('TERMINAL_MCP_HEADLESS_STATE_DIR', str(state / 'terminal-mcp/headless')))


class WorkerService:
    def __init__(self, root, *, max_parallel=2, launch=None, relaunch_after=60.0,
                 max_launch_attempts=3, stale_seconds=15.0):
        self.store = HeadlessStore(Path(root))
        self.max_parallel = max(1, int(max_parallel))
        self.launch = launch or launch_unit
        self.relaunch_after = relaunch_after
        self.max_launch_attempts = max_launch_attempts
        self.stale_seconds = stale_seconds

    def tick(self):
        held = recover_lost(self.store, stale_seconds=self.stale_seconds)
        now = time.time()
        rows = self.store.rows()
        active = [r for r in rows if r['state'] in ACTIVE_STATES]
        queued = sorted((r for r in rows if r['state'] == 'READY' and r.get('dispatch') == 'service'),
                        key=lambda r: r['created_at'])
        # Launched but not yet claimed: counts against capacity, not relaunched yet.
        pending = [r for r in queued if r.get('launched_at') and now - r['launched_at'] < self.relaunch_after]
        slots = self.max_parallel - len(active) - len(pending)
        launched, failed = [], []
        for row in queued:
            if slots <= 0:
                break
            if row in pending:
                continue
            if row.get('cancel_requested'):
                continue
            attempts = int(row.get('launch_attempts') or 0)
            if attempts >= self.max_launch_attempts:
                self.store.update(row['task_id'], 'LAUNCH_BUDGET_EXHAUSTED', expected_states=('READY',),
                                  state='BLOCKED', reason='WORKER_UNIT_START_FAILED', finished_at=now)
                failed.append(row['task_id'])
                continue
            # Persist the launch intent first so a restart counts it.
            after = self.store.update(row['task_id'], 'WORKER_LAUNCH', expected_states=('READY',),
                                      launch_attempts=attempts + 1, launched_at=now)
            if after['state'] != 'READY':
                continue
            self.launch(self.store.root, row['task_id'])
            launched.append(row['task_id'])
            slots -= 1
        summary = {'at': now, 'pid': os.getpid(), 'active': len(active), 'queued': len(queued) - len(launched),
                   'launched': launched, 'held_lost': held, 'launch_failed': failed,
                   'max_parallel': self.max_parallel}
        self._write_health(summary)
        return summary

    def _write_health(self, summary):
        path = self.store.root / 'worker-service.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(summary))
        os.replace(temp, path)

    def serve(self, *, poll=2.0, stop=None):
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 -- keep serving; state is in SQLite
                self._write_health({'at': time.time(), 'pid': os.getpid(), 'error': type(exc).__name__})
            stop.wait(poll)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--root', type=Path, default=None)
    parser.add_argument('--max-parallel', type=int,
                        default=int(os.environ.get('TERMINAL_MCP_HEADLESS_MAX_PARALLEL', '2')))
    parser.add_argument('--poll', type=float, default=2.0)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    service = WorkerService(args.root or default_root(), max_parallel=args.max_parallel)
    if args.once:
        print(json.dumps(service.tick()))
        return 0
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    service.serve(poll=args.poll, stop=stop)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
