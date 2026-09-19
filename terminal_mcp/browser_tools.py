"""MCP registration for the browser gateway -- four tools, no more.

The surface is the security boundary. There is no tool here that runs
Python, runs shell, evaluates JavaScript, or exposes a raw CDP verb,
because a chat client with any of those has a shell on the node and the
declarative plan becomes decoration. Adding one later is a security
decision, not a convenience one -- tests/test_browser_gateway.py pins this
surface so that decision cannot be made by accident.
"""
from __future__ import annotations

from typing import Any

from .browser_gateway import BrowserGateway

#: The exact tool surface. Pinned by test_browser_gateway.py.
BROWSER_TOOL_NAMES = (
    "terminal_browser_status",
    "terminal_browser_verify",
    "terminal_browser_screenshot",
    "terminal_browser_stop",
)


def register_browser_tools(server: Any, gateway: BrowserGateway) -> dict[str, Any]:
    """Register the four tools and RETURN them.

    The returned functions are what mcp_app threads into
    compact_tools.turn_handler_map, so the one-tool ChatGPT surface routes
    `terminal_turn(action="browser_verify", ...)` into the very same
    implementation the standalone tool calls -- never a copy, and never a
    weaker path.
    """
    @server.tool()
    def terminal_browser_status(job_id: str | None = None) -> dict:
        """Browser gateway health, or the result of one earlier job.

        With no argument: whether Browser Use/Browser Harness is installed
        on this node, its resolved version, whether the managed browser is
        up, whether recording is on (it is off unless an operator enabled
        it), and which fleet nodes advertise the browser capability.

        With `job_id`: the finished result of a verify/screenshot that
        returned PENDING because it outlived the synchronous window.
        """
        return gateway.status(job_id)

    @server.tool()
    def terminal_browser_verify(
        url: str,
        steps: list | None = None,
        viewport: dict | None = None,
        timeout_seconds: float = 45.0,
        screenshot: str = "on_failure",
        screenshot_name: str = "",
        allow_mutations: bool = False,
        node: str | None = None,
        session: str | None = None,
        wait_seconds: float | None = None,
    ) -> dict:
        """Verify a page declaratively in a real browser.

        `steps` is a bounded list of objects drawn from a closed
        vocabulary -- never code:

          {"op": "navigate", "url": "https://..."}
          {"op": "click",  "selector": "#submit"}
          {"op": "fill",   "selector": "#q", "value": "text", "secret": false}
          {"op": "press",  "key": "Enter"}
          {"op": "wait",   "selector": "#done"} | {"seconds": 1.5} | {"state": "load"|"network_idle"}
          {"op": "assert_text",    "selector": "h1", "contains": "Example"}
          {"op": "assert_value",   "selector": "#q", "equals": "12.5"}
          {"op": "assert_visible", "selector": "#cart", "visible": true}
          {"op": "assert_url",     "contains": "/checkout"}

        At least one assert_* step is required -- a plan that asserts
        nothing would always pass. click/fill/press change the page, so
        they require `allow_mutations=true` and are echoed back in the
        result for audit.

        Only http/https URLs are reachable; private and loopback targets
        are refused unless an operator allowlisted them for local dev.
        Returns PASS|FAIL|PENDING|ERROR compactly. PENDING means the plan
        outlived the synchronous window and is still running -- read it
        with terminal_browser_status(job_id=...).
        """
        payload = {
            "url": url,
            "steps": steps or [],
            "viewport": viewport,
            "timeout_seconds": timeout_seconds,
            "screenshot": screenshot,
            "screenshot_name": screenshot_name,
            "allow_mutations": allow_mutations,
            "node": node,
            "session": session,
        }
        return gateway.verify(payload, wait_seconds=wait_seconds)

    @server.tool()
    def terminal_browser_screenshot(
        url: str,
        name: str = "",
        viewport: dict | None = None,
        node: str | None = None,
        session: str | None = None,
        wait_seconds: float | None = None,
    ) -> dict:
        """Capture one page as evidence, at an exact viewport (default
        1348x768).

        `name` names the artifact; it may not place it -- the file is
        always written inside the gateway's own artifact directory. Use
        terminal_browser_verify when the point is to CHECK something;
        this tool only captures.
        """
        return gateway.screenshot(url, name=name, viewport=viewport, node=node,
                                  session=session, wait_seconds=wait_seconds)

    @server.tool()
    def terminal_browser_stop() -> dict:
        """Stop the managed browser this gateway started.

        Safe to call at any time: the browser is re-launched on the next
        verify/screenshot. Only the gateway's own browser is touched --
        never another Chrome running on the host.
        """
        return gateway.stop()

    return {
        "browser_status": terminal_browser_status,
        "browser_verify": terminal_browser_verify,
        "browser_screenshot": terminal_browser_screenshot,
        "browser_stop": terminal_browser_stop,
    }
