#!/usr/bin/env python3
"""Build a disposable Kanzo view from Repo Kanban or live read-only TMCP."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from terminal_mcp.kanzo_projection import write_projection
from terminal_mcp.kanzo_source import (
    HttpMcpReadSource,
    SourceError,
    load_live_projects,
    snapshot_from_terminal_mcp,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    project = subparsers.add_parser(
        "project", help="replace a dedicated Kanzo home with a one-way projection"
    )
    project.add_argument(
        "--snapshot",
        required=True,
        type=Path,
        help="sanitized Repo Kanban JSON snapshot",
    )
    project.add_argument(
        "--home", required=True, type=Path, help="dedicated Kanzo pilot home"
    )
    project.add_argument(
        "--synced-at", required=True, help="UTC ISO-8601 source snapshot time"
    )
    live = subparsers.add_parser(
        "live", help="read explicitly identified sessions through loopback Terminal MCP"
    )
    live.add_argument(
        "--source-url", default="http://127.0.0.1:8766/mcp",
        help="literal loopback Terminal MCP HTTP endpoint",
    )
    live.add_argument(
        "--projects", required=True, type=Path,
        help="JSON allowlist binding project ids to node/session identities",
    )
    live.add_argument(
        "--home", required=True, type=Path, help="dedicated Kanzo pilot home"
    )
    live.add_argument(
        "--synced-at", required=True, help="UTC ISO-8601 source snapshot time"
    )
    args = parser.parse_args()
    try:
        if args.command == "project":
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        else:
            source = HttpMcpReadSource(args.source_url)
            snapshot = snapshot_from_terminal_mcp(
                source, load_live_projects(args.projects)
            )
        result = write_projection(snapshot, args.home, synced_at=args.synced_at)
    except (OSError, SourceError, ValueError) as exc:
        print(f"kanzo pilot projection refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "database": str(result.database),
                "digest": result.digest,
                "duplicates": result.duplicates,
                "projects": result.projects,
                "tasks": result.tasks,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
