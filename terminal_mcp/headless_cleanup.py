"""Conservative periodic cleanup: only proven completed headless work.

Never kills tmux panes, never removes a workspace, never releases a blocked
or unknown task. Checkpoint failure stops cleanup for that run.
"""
import json
import os
from pathlib import Path
import time
from .headless import HeadlessStore


def sweep(store, *, grace_seconds=60):
    released = 0
    for row in store.rows():
        if row['state'] != 'COMPLETED' or time.time() - row.get('finished_at', time.time()) < grace_seconds:
            continue
        checkpoint = store.checkpoint(row['task_id'])
        if checkpoint.get('checkpoint_sha256') and store.cleanup(row['task_id']).get('state') == 'RELEASED':
            released += 1
    return {'released': released, 'tmux_deleted': 0}


if __name__ == '__main__':
    root = Path(os.environ.get('TERMINAL_MCP_HEADLESS_STATE_DIR', str(Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'terminal-mcp/headless')))
    print(json.dumps(sweep(HeadlessStore(root))))
