"""Local Playwright browser tools shared by full and compact surfaces."""
from typing import Any
from .browser_gateway import BrowserGateway

BROWSER_TOOL_NAMES = ("browser_verify", "browser_run_task", "browser_status", "browser_screenshot")

def register_browser_tools(server: Any, gateway: BrowserGateway) -> dict[str, Any]:
    @server.tool()
    def browser_verify(url: str, assertions: list[str] | None = None,
                       viewport_width: int | None = None, viewport_height: int | None = None,
                       timeout_seconds: float | None = None, screenshot: bool = False,
                       wait_for: str | None = None) -> dict:
        """Verify a URL with local Chromium and deterministic assertions; fresh context."""
        return gateway.verify(url, assertions, viewport_width=viewport_width,
            viewport_height=viewport_height, timeout_seconds=timeout_seconds,
            screenshot=screenshot, wait_for=wait_for)

    @server.tool()
    def browser_run_task(task: str, url: str | None = None,
                         viewport_width: int | None = None, viewport_height: int | None = None,
                         timeout_seconds: float | None = None, screenshot: bool = False,
                         session_id: str | None = None) -> dict:
        """Run deterministic steps only; assertions judge final page, no session persistence."""
        return gateway.run_task(task, url=url, viewport_width=viewport_width,
            viewport_height=viewport_height, timeout_seconds=timeout_seconds,
            screenshot=screenshot, session_id=session_id)

    @server.tool()
    def browser_status(probe: bool = False) -> dict:
        """Report local browser configuration; probe launches and closes Chromium."""
        return gateway.status(probe=probe)

    @server.tool()
    def browser_screenshot(url: str, viewport_width: int | None = None,
                           viewport_height: int | None = None, full_page: bool = False,
                           timeout_seconds: float | None = None) -> dict:
        """Return a local PNG path. Operator opt-in required; pixels are not redacted."""
        return gateway.screenshot(url, viewport_width=viewport_width,
            viewport_height=viewport_height, full_page=full_page,
            timeout_seconds=timeout_seconds)

    return {
        "browser_verify": browser_verify,
        "browser_run_task": browser_run_task,
        "browser_status": browser_status,
        "browser_screenshot": browser_screenshot,
    }
