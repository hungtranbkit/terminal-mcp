#!/usr/bin/env python3
"""Poll allowlisted read-only sources into one isolated Kanzo projection."""

from __future__ import annotations

import argparse
import fcntl
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from terminal_mcp.kanzo_reconcile import reconcile_once
from terminal_mcp.kanzo_source import (
    GhCliReadSource,
    HttpMcpReadSource,
    load_live_projects,
    snapshot_from_terminal_mcp,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", default="http://127.0.0.1:8766/mcp")
    parser.add_argument("--projects", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-backoff", type=int, default=300)
    parser.add_argument("--max-cycles", type=int, default=0)
    args = parser.parse_args()
    if not 10 <= args.interval <= 3600 or not args.interval <= args.max_backoff <= 3600:
        parser.error("interval/backoff must satisfy 10 <= interval <= max-backoff <= 3600")

    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (home / "tmcp-reconcile-worker.lock").open("a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another reconcile worker already owns this home", file=sys.stderr)
        return 3

    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    projects = load_live_projects(args.projects)
    failures = 0
    cycles = 0
    while not stopped and (args.max_cycles == 0 or cycles < args.max_cycles):
        synced_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        def build():
            return snapshot_from_terminal_mcp(
                HttpMcpReadSource(args.source_url), projects, github=GhCliReadSource()
            )

        status = reconcile_once(build, home, synced_at=synced_at)
        print(json.dumps(status, sort_keys=True), flush=True)
        cycles += 1
        failures = 0 if status["ok"] else failures + 1
        delay = min(args.max_backoff, args.interval * (2 ** min(failures, 4)))
        deadline = time.monotonic() + delay
        while not stopped and time.monotonic() < deadline and (
            args.max_cycles == 0 or cycles < args.max_cycles
        ):
            time.sleep(min(1.0, deadline - time.monotonic()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
