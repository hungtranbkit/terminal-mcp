"""Operator-invoked REAL Codex acceptance; writes only inside the given lab.

Run with repository PYTHONPATH and production config. Preserves all existing
sessions. Saves metadata, never credentials or raw agent output.
"""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

from terminal_mcp.config import load_config
from terminal_mcp.core import TerminalService
from terminal_mcp.headless import HeadlessManager

parser = argparse.ArgumentParser()
parser.add_argument('--lab', required=True)
parser.add_argument('--count', type=int, default=10)
args = parser.parse_args()
lab = Path(args.lab).resolve()
lab.mkdir(parents=True, exist_ok=True, mode=0o700)
workspace = lab / 'work'
workspace.mkdir(exist_ok=True)
subprocess.run(['git', 'init', '-q', str(workspace)], check=True)
terminal = TerminalService(load_config())
manager = HeadlessManager(terminal, lab / 'manager', enabled=True)
results = []
for i in range(1, args.count + 1):
    nonce = uuid.uuid4().hex
    name = f'tmcp-acceptance-{i:02d}'
    file = workspace / f'acceptance-{i:02d}.txt'
    prompt = f'Create the file {file.name} in the current working directory containing exactly {nonce} followed by one newline. Use a tool to write the file. Do not modify any other files. Then reply WROTE.'
    started = time.monotonic()
    response = manager.start(name, prompt, str(workspace), nonce, agent='codex')
    if 'task' not in response:
        print(json.dumps(response), flush=True)
        raise SystemExit(1)
    tid = response['task']['task_id']
    # Reconstruct controller every cycle: state/ownership must survive restart.
    restored = HeadlessManager(terminal, lab / 'manager', enabled=True)
    duplicate = restored.start(name, prompt, str(workspace), nonce, agent='codex')
    assert duplicate['task']['task_id'] == tid
    while time.monotonic() - started < 180:
        row = restored.status(tid)['task']
        if row['state'] in ('COMPLETED', 'BLOCKED'):
            break
        time.sleep(.5)
    correct = file.exists() and file.read_text() == nonce + '\n'
    record = {k: row.get(k) for k in ('task_id', 'session_id', 'state', 'delivery_ack_at', 'execution_ack_at', 'exit_code', 'turn_completed', 'reason')}
    record.update(cycle=i, file_verified=correct, seconds=round(time.monotonic() - started, 2), idempotent=True)
    if row['state'] == 'COMPLETED' and correct:
        record['checkpoint_sha256'] = restored.store.checkpoint(tid)['checkpoint_sha256']
        record['cleanup_state'] = restored.store.cleanup(tid)['state']
    results.append(record)
    (lab / 'acceptance.json').write_text(json.dumps(results, indent=2))
    print(json.dumps(record), flush=True)
    if row['state'] != 'COMPLETED' or not correct:
        raise SystemExit(1)
    manager.reconcile()
