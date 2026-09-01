from __future__ import annotations

import atexit

import anyio
import uvicorn

from .config import load_config
from .core import TerminalService
from .dashboard import register_dashboard
from .health import register_health
from .logging_setup import RequestIdMiddleware, SecurityHeadersMiddleware, configure_logging
from .maintenance import MaintenanceLoop
from .mcp_app import build_mcp
from .supervisor import SupervisorLoop, SupervisorService, SupervisorStore
from .supervisor2 import build_supervisor_v2


HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8766
HTTP_PATH = "/mcp"


async def _serve(server) -> None:
    """Builds and runs the same Starlette app server.run(transport=
    "streamable-http", ...) would (streamable_http_app() + uvicorn), but
    driven directly rather than through that convenience wrapper -- it
    exposes no hook to add ASGI middleware, and P1 items #7/#11 (request-
    id correlation, security response headers) need one. log_config=None
    is deliberate: uvicorn's own default logging setup would otherwise
    call logging.config.dictConfig() and silently replace the root
    handler configure_logging() (called before this) just installed."""
    starlette_app = server.streamable_http_app(
        streamable_http_path=HTTP_PATH, json_response=True, host=HTTP_HOST,
    )
    starlette_app.add_middleware(SecurityHeadersMiddleware)
    starlette_app.add_middleware(RequestIdMiddleware)
    config = uvicorn.Config(starlette_app, host=HTTP_HOST, port=HTTP_PORT, log_level="info", log_config=None)
    await uvicorn.Server(config).serve()


def main() -> None:
    configure_logging()
    # The bind address is intentionally not configurable: remote access must go
    # through an authenticated HTTPS tunnel terminating on this loopback port.
    config = load_config()
    terminal = TerminalService(config)
    supervisor = SupervisorService(terminal, SupervisorStore())
    supervisor_v2 = build_supervisor_v2(supervisor)
    server = build_mcp(terminal, supervisor, supervisor_v2)
    register_dashboard(server, terminal, supervisor, supervisor_v2)
    register_health(server, terminal)

    # Supervisor tools (watch/status/events/run_once, and the v2 policy/
    # claim/decide/approve/send tools) are always available — only the
    # *automatic* background poll thread is gated here. A plain daemon
    # thread with an interruptible stop Event is the simplest correct way
    # to add a background poller without needing an asyncio/lifespan hook.
    # One process -> at most one loop instance; atexit.register gives it a
    # best-effort clean stop on normal interpreter exit (systemd's SIGTERM
    # still just ends the process, same as before this feature — the loop
    # is a daemon thread either way).
    # The loop drives supervisor_v2.run_once() (a strict superset of
    # supervisor.run_once() — v1's own poll plus v2's reconciliation pass),
    # so an approved_auto_continue chain progresses automatically once
    # enabled, with no separate v2 loop/thread needed.
    if config.supervisor.enabled:
        loop = SupervisorLoop(supervisor_v2)
        loop.start()
        atexit.register(loop.stop)

    # P1 hardening item #9: unconditional, unlike the supervisor loop
    # above -- audit.db accumulates from any terminal_send_text/_keys call
    # regardless of whether Supervisor Loop v1 is enabled, so its
    # retention/WAL maintenance is baseline hygiene, not gated behind that
    # unrelated opt-in.
    maintenance_loop = MaintenanceLoop(
        audit=terminal.audit, supervisor2_store=supervisor_v2.store,
        bindings_path=terminal.bindings.path, config=config.maintenance,
        leases=terminal.leases,
    )
    maintenance_loop.start()
    atexit.register(maintenance_loop.stop)

    anyio.run(lambda: _serve(server))


if __name__ == "__main__":
    main()
