"""Dem loi goi tung MCP tool -- diem mu cuoi cung cua audit.

`input_audit` chi ghi cac hanh dong GHI (send_text/create_session/...), va no
ghi ten hanh dong LOI chu khong phai ten tool: `terminal_send_task` va
`terminal_send_text` deu hien ra la "send_text". Cac loi goi DOC
(`terminal_tail`, `terminal_status`, `terminal_batch_inspect`) khong duoc ghi
o dau ca -- ma do gan nhu chac chan la nhom dong nhat.

Hau qua do duoc 2026-09-21: mot hoi thoai ChatGPT lai terminal truc tiep tich
tu 377 khoi "Called tool", trong khi mot hoi thoai giao viec cho session Claude
chi co 7. Khong the biet 377 do la tail-poll, la churn session, hay gi khac,
nen moi toi uu deu la doan. Module nay ghi dung mot dong moi loi goi tool.

Nguyen tac: KHONG BAO GIO lam hong mot loi goi tool. Moi loi ghi deu nuot.
"""
from __future__ import annotations

import functools
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCAL = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    session     TEXT,
    ok          INTEGER NOT NULL,
    latency_ms  REAL
);
CREATE INDEX IF NOT EXISTS tool_calls_ts   ON tool_calls(timestamp);
CREATE INDEX IF NOT EXISTS tool_calls_tool ON tool_calls(tool);
"""

# Doi so nao dang ra ten session, theo thu tu uu tien.
_SESSION_KEYS = ("session", "target", "name", "binding", "session_name")


def default_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_TOOL_METRICS_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "tool_calls.db"


class ToolMetricsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def record(self, tool: str, session: str | None, ok: bool,
               latency_ms: float) -> None:
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO tool_calls (timestamp, tool, session, ok, latency_ms)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(), tool, session,
                     int(ok), round(latency_ms, 2)))
                connection.commit()
        except Exception:
            pass          # do luong khong bao gio duoc lam hong viec that

    def prune(self, keep_rows: int = 200_000) -> int:
        try:
            with closing(self._connect()) as connection:
                cur = connection.execute(
                    "DELETE FROM tool_calls WHERE id NOT IN "
                    "(SELECT id FROM tool_calls ORDER BY id DESC LIMIT ?)",
                    (keep_rows,))
                connection.commit()
                return cur.rowcount
        except Exception:
            return 0

    def summary(self, since_sec: float = 3600) -> list[dict[str, Any]]:
        cutoff = datetime.fromtimestamp(time.time() - since_sec, timezone.utc).isoformat()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT tool, COUNT(*) n, SUM(ok=0) loi, ROUND(AVG(latency_ms),1) ms"
                    " FROM tool_calls WHERE timestamp >= ?"
                    " GROUP BY tool ORDER BY n DESC", (cutoff,)).fetchall()
            return [{"tool": r[0], "calls": r[1], "errors": r[2], "avg_ms": r[3]}
                    for r in rows]
        except Exception:
            return []


def _session_of(kwargs: dict[str, Any]) -> str | None:
    for key in _SESSION_KEYS:
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            return value[:120]
    return None


def instrument(server: Any, store: ToolMetricsStore) -> None:
    """Boc `server.tool` sao cho MOI tool dang ky sau day deu duoc dem.

    Boc mot lan o day thay vi sua 280 cho dang ky. functools.wraps giu
    __name__/__doc__/__annotations__ va dat __wrapped__, nen inspect.signature
    ma FastMCP dung de dung schema van nhin thay ham goc.
    """
    original = server.tool

    def counting_tool(*t_args: Any, **t_kwargs: Any) -> Callable:
        decorate = original(*t_args, **t_kwargs)

        def wrap(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def inner(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                ok = True
                try:
                    return fn(*args, **kwargs)
                except Exception:
                    ok = False
                    raise
                finally:
                    # Bat o CA day, khong chi trong store.record: mot store
                    # hong (dia day, quyen sai, bi thay the) khong duoc phep
                    # lam hong mot loi goi tool that.
                    try:
                        store.record(fn.__name__, _session_of(kwargs), ok,
                                     (time.perf_counter() - started) * 1000)
                    except Exception:
                        pass
            return decorate(inner)

        return wrap

    server.tool = counting_tool
