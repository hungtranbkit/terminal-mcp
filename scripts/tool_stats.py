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
        has_action = any(r[1] == "action" for r in
                         con.execute("PRAGMA table_info(tool_calls)"))
        key = "tool || COALESCE(' ' || action, '')" if has_action else "tool"
        rows = con.execute(
            f"SELECT {key} k, COUNT(*) n, SUM(ok=0) FROM tool_calls"
            " WHERE timestamp>=? GROUP BY k ORDER BY n DESC", (cutoff,)).fetchall()
        if rows:
            # Trung binh giau het moi thu dang quan tam: mot tool co p50 3s
            # va max 61s khong "trung binh 8s" theo bat ky nghia huu ich nao.
            print(f"  {'tool':40} {'goi':>5} {'%':>6} {'loi':>4} "
                  f"{'p50':>7} {'p90':>7} {'max':>7}")
            for k, n, err in rows:
                lat = sorted(r[0] or 0 for r in con.execute(
                    f"SELECT latency_ms FROM tool_calls WHERE timestamp>=? AND {key}=?",
                    (cutoff, k)))
                p50 = lat[len(lat)//2]
                p90 = lat[max(0, int(len(lat)*0.9)-1)]
                print(f"  {k[:40]:40} {n:5} {n*100/total:5.1f}% {err or 0:4} "
                      f"{p50:7.0f} {p90:7.0f} {lat[-1]:7.0f}")
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
