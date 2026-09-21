#!/usr/bin/env python3
"""Loi goi MCP tool trong N gio qua. Chi doc, khong doi gi.

    scripts/tool_stats.py [gio] [--by-session]
"""
from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from terminal_mcp.tool_metrics import default_path  # noqa: E402


def main() -> int:
    hours = float(next((a for a in sys.argv[1:] if not a.startswith("-")), 1))
    by_session = "--by-session" in sys.argv
    db = default_path()
    if not db.exists():
        print(f"Chua co du lieu: {db}")
        return 1
    cutoff = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat()
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        total = con.execute("SELECT COUNT(*) FROM tool_calls WHERE timestamp>=?",
                            (cutoff,)).fetchone()[0]
        print(f"{total} loi goi trong {hours:g}h qua\n")
        rows = con.execute(
            "SELECT tool, COUNT(*) n, SUM(ok=0), ROUND(AVG(latency_ms),1)"
            " FROM tool_calls WHERE timestamp>=? GROUP BY tool ORDER BY n DESC",
            (cutoff,)).fetchall()
        if rows:
            print(f"  {'tool':34} {'goi':>6} {'%':>6} {'loi':>5} {'ms':>8}")
            for tool, n, err, ms in rows:
                print(f"  {tool[:34]:34} {n:6} {n*100/total:5.1f}% {err or 0:5} {ms or 0:8.1f}")
        if by_session:
            print("\n  theo session:")
            for s, n in con.execute(
                    "SELECT COALESCE(session,'(khong ro)'), COUNT(*) n FROM tool_calls"
                    " WHERE timestamp>=? GROUP BY 1 ORDER BY n DESC LIMIT 15",
                    (cutoff,)).fetchall():
                print(f"    {s[:44]:44} {n:6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
