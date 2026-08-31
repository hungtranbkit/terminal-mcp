from __future__ import annotations

from .config import load_config
from .core import TerminalService
from .dashboard import register_dashboard
from .mcp_app import build_mcp


HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8766
HTTP_PATH = "/mcp"


def main() -> None:
    # The bind address is intentionally not configurable: remote access must go
    # through an authenticated HTTPS tunnel terminating on this loopback port.
    terminal = TerminalService(load_config())
    server = build_mcp(terminal)
    register_dashboard(server, terminal)
    server.run(
        transport="streamable-http",
        host=HTTP_HOST,
        port=HTTP_PORT,
        streamable_http_path=HTTP_PATH,
        json_response=True,
    )


if __name__ == "__main__":
    main()
