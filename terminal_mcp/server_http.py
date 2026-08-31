from __future__ import annotations

import atexit

from .config import load_config
from .core import TerminalService
from .dashboard import register_dashboard
from .mcp_app import build_mcp
from .supervisor import SupervisorLoop, SupervisorService, SupervisorStore
from .supervisor2 import build_supervisor_v2


HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8766
HTTP_PATH = "/mcp"


def main() -> None:
    # The bind address is intentionally not configurable: remote access must go
    # through an authenticated HTTPS tunnel terminating on this loopback port.
    config = load_config()
    terminal = TerminalService(config)
    supervisor = SupervisorService(terminal, SupervisorStore())
    supervisor_v2 = build_supervisor_v2(supervisor)
    server = build_mcp(terminal, supervisor, supervisor_v2)
    register_dashboard(server, terminal, supervisor, supervisor_v2)

    # Supervisor tools (watch/status/events/run_once, and the v2 policy/
    # claim/decide/approve/send tools) are always available — only the
    # *automatic* background poll thread is gated here. server.run() below
    # is a blocking, framework-owned call (no asyncio/lifespan hook is
    # exposed to this module), so a plain daemon thread with an interruptible
    # stop Event is the simplest correct way to add a background poller
    # without touching that machinery. One process -> at most one loop
    # instance; atexit.register gives it a best-effort clean stop on normal
    # interpreter exit (systemd's SIGTERM still just ends the process, same
    # as before this feature — the loop is a daemon thread either way).
    # The loop drives supervisor_v2.run_once() (a strict superset of
    # supervisor.run_once() — v1's own poll plus v2's reconciliation pass),
    # so an approved_auto_continue chain progresses automatically once
    # enabled, with no separate v2 loop/thread needed.
    if config.supervisor.enabled:
        loop = SupervisorLoop(supervisor_v2)
        loop.start()
        atexit.register(loop.stop)

    server.run(
        transport="streamable-http",
        host=HTTP_HOST,
        port=HTTP_PORT,
        streamable_http_path=HTTP_PATH,
        json_response=True,
    )


if __name__ == "__main__":
    main()
