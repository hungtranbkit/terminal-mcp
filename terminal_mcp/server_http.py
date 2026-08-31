from __future__ import annotations

import atexit

from .config import load_config
from .core import TerminalService
from .dashboard import register_dashboard
from .mcp_app import build_mcp
from .supervisor import SupervisorLoop, SupervisorService, SupervisorStore


HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8766
HTTP_PATH = "/mcp"


def main() -> None:
    # The bind address is intentionally not configurable: remote access must go
    # through an authenticated HTTPS tunnel terminating on this loopback port.
    config = load_config()
    terminal = TerminalService(config)
    supervisor = SupervisorService(terminal, SupervisorStore())
    server = build_mcp(terminal, supervisor)
    register_dashboard(server, terminal, supervisor)

    # Supervisor tools (watch/status/events/run_once) are always available —
    # only the *automatic* background poll thread is gated here. server.run()
    # below is a blocking, framework-owned call (no asyncio/lifespan hook is
    # exposed to this module), so a plain daemon thread with an interruptible
    # stop Event is the simplest correct way to add a background poller
    # without touching that machinery. One process -> at most one loop
    # instance; atexit.register gives it a best-effort clean stop on normal
    # interpreter exit (systemd's SIGTERM still just ends the process, same
    # as before this feature — the loop is a daemon thread either way).
    if config.supervisor.enabled:
        loop = SupervisorLoop(supervisor)
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
