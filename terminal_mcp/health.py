"""Health/version/metrics endpoints -- P1 hardening item #6, extended by
the final audit pass (items I/12: readiness must cover every durable
store, not just audit.db; an internal, in-process metrics surface for the
counters this project's own safety model cares about).

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

import functools
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, metrics
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


def _check_supervisor_staleness(supervisor) -> dict[str, Any]:
    """Informational only -- never gates /health/ready's overall status/
    HTTP code (see register_health below for why): a stale or stopped
    Supervisor Loop v1 is a real degraded-state signal worth surfacing,
    but it is not itself a reason to fail the process's core readiness
    (tmux + every durable store) the way a systemd/orchestrator restart
    probe should react to -- 0 watches configured (this deployment's
    current, deliberate state) still updates last_poll_at every cycle, so
    "stale" here means the loop genuinely stopped ticking, not merely
    "idle"."""
    status = supervisor.status()
    if not status["config_enabled"]:
        return {"enabled": False, "detail": "disabled in config"}
    if not status["loop_running"]:
        return {"enabled": True, "loop_running": False, "detail": "enabled but the background loop is not running"}
    last_poll_at = status.get("last_poll_at")
    if last_poll_at is None:
        return {"enabled": True, "loop_running": True, "detail": "no poll cycle has completed yet"}
    try:
        age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(last_poll_at)).total_seconds()
    except ValueError:
        return {"enabled": True, "loop_running": True, "detail": "last_poll_at is not a valid timestamp"}
    stale_after = max(60, status["poll_interval_seconds"] * 3)
    return {
        "enabled": True, "loop_running": True,
        "last_poll_age_seconds": round(age_seconds, 1),
        "stale_after_seconds": stale_after,
        "stale": age_seconds >= stale_after,
        "last_poll_error": status.get("last_poll_error"),
    }


@functools.lru_cache(maxsize=1)
def _source_identity() -> dict[str, Any]:
    """Best-effort, computed once (git state doesn't change while this
    process runs) -- P0 audit item #10 (immutable/version-consistent
    runtime): lets an operator confirm the exact commit and working-tree
    cleanliness the *currently running* process was started from, rather
    than trusting that a `git pull` + restart actually happened. Never
    raises -- a repo-less deployment (e.g. installed from a built package
    with no .git directory) just reports git info as unavailable, which is
    itself useful signal, not a failure."""
    repo_dir = Path(__file__).resolve().parent.parent
    if not shutil.which("git") or not (repo_dir / ".git").exists():
        return {"git_available": False}
    try:
        def _git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True,
                                  text=True, timeout=5).stdout.strip()

        commit = _git("rev-parse", "HEAD")
        dirty = bool(_git("status", "--porcelain"))
        return {"git_available": True, "commit": commit, "dirty": dirty}
    except (subprocess.SubprocessError, OSError):
        return {"git_available": False}


def register_health(server: MCPServer, terminal: TerminalService, supervisor=None) -> None:
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
        # Every durable SQLite store this process actually opens -- not
        # just audit.db (the original P1 check). A store is only checked
        # if this TerminalService actually has one configured (all do by
        # default -- see TerminalService.__init__), so this never breaks
        # a caller that injected something unusual in a test.
        store_paths = {
            "audit_db": getattr(terminal.audit, "path", None),
            "bindings_db": getattr(terminal.bindings, "path", None),
            "grants_db": getattr(terminal.grants, "path", None),
            "leases_db": getattr(terminal.leases, "path", None),
        }
        checks: dict[str, Any] = {"tmux": {"ok": tmux_ok, "detail": tmux_detail}}
        all_ok = tmux_ok
        for name, path in store_paths.items():
            if path is None:
                continue
            ok, detail = await anyio.to_thread.run_sync(_check_sqlite, path)
            checks[name] = {"ok": ok, "detail": detail}
            all_ok = all_ok and ok
        # Supervisor loop staleness is informational only -- see
        # _check_supervisor_staleness's own docstring for why it never
        # flips the overall ready/not_ready status or HTTP code.
        if supervisor is not None:
            checks["supervisor"] = await anyio.to_thread.run_sync(_check_supervisor_staleness, supervisor)
        body = {"status": "ready" if all_ok else "not_ready", "checks": checks}
        return JSONResponse(body, status_code=200 if all_ok else 503, headers={"Cache-Control": "no-store"})

    @server.custom_route("/version", methods=["GET"], include_in_schema=False)
    async def version(_: Request) -> JSONResponse:
        return JSONResponse({"version": __version__, "source": _source_identity()},
                            headers={"Cache-Control": "no-store"})

    @server.custom_route("/health/metrics", methods=["GET"], include_in_schema=False)
    async def health_metrics(_: Request) -> JSONResponse:
        # P0 audit item #12: internal counters only (metrics.py) -- no
        # external metrics backend is configured for this deployment, and
        # none is invented here; this is what a real one would scrape or
        # forward later. Per-process (HTTP and STDIO each report only
        # their own counters, reset on restart) -- see metrics.py's module
        # docstring for the full scope/rationale.
        body: dict[str, Any] = {"counters": metrics.snapshot()}
        if supervisor is not None:
            body["supervisor"] = await anyio.to_thread.run_sync(_check_supervisor_staleness, supervisor)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})
