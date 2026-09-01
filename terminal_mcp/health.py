"""Health/version endpoints -- P1 hardening item #6.

Deliberately NOT under /dashboard: the public tunnel's ingress only routes
^/dashboard(?:/.*)?$ to this service (see deploy/secure-tunnel), so these
stay loopback-only by default, exactly like the rest of this project's
"remote access goes through an authenticated tunnel" posture -- nothing
here needs to be internet-reachable, it's for local monitoring (systemd,
a script, a future container orchestrator's own health probe).

Both endpoints and the checks they run are read-only and reveal nothing
session/pane-content-shaped -- no whitelist/redaction concerns apply.
"""
from __future__ import annotations

import sqlite3
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .core import TerminalService
from .tmux import TmuxError


def _check_tmux(terminal: TerminalService) -> tuple[bool, str]:
    try:
        terminal.tmux.list_sessions()
        return True, "ok"
    except TmuxError as exc:
        return False, f"tmux unreachable: {exc}"
    except Exception as exc:  # pragma: no cover -- defensive, see module docstring
        return False, f"tmux check failed: {type(exc).__name__}"


def _check_sqlite(path) -> tuple[bool, str]:
    try:
        connection = sqlite3.connect(path, timeout=2)
        try:
            connection.execute("SELECT 1")
        finally:
            connection.close()
        return True, "ok"
    except Exception as exc:
        return False, f"sqlite unreachable ({path}): {type(exc).__name__}"


def register_health(server: MCPServer, terminal: TerminalService) -> None:
    @server.custom_route("/health/live", methods=["GET"], include_in_schema=False)
    async def live(_: Request) -> JSONResponse:
        # No dependency checks at all -- this only proves the process is
        # up and its event loop is responsive enough to answer. A process
        # that is alive but whose tmux/sqlite dependencies are broken
        # still answers here (that distinction is exactly what /ready is
        # for) -- conflating the two would make a transient tmux hiccup
        # look like the whole process needs restarting.
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/health/ready", methods=["GET"], include_in_schema=False)
    async def ready(_: Request) -> JSONResponse:
        tmux_ok, tmux_detail = await anyio.to_thread.run_sync(_check_tmux, terminal)
        audit_ok, audit_detail = await anyio.to_thread.run_sync(_check_sqlite, terminal.audit.path)
        checks: dict[str, Any] = {
            "tmux": {"ok": tmux_ok, "detail": tmux_detail},
            "audit_db": {"ok": audit_ok, "detail": audit_detail},
        }
        ready_ = tmux_ok and audit_ok
        body = {"status": "ready" if ready_ else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if ready_ else 503, headers={"Cache-Control": "no-store"})

    @server.custom_route("/version", methods=["GET"], include_in_schema=False)
    async def version(_: Request) -> JSONResponse:
        return JSONResponse({"version": __version__}, headers={"Cache-Control": "no-store"})
