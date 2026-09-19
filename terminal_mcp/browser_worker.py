"""The child process that actually drives a browser. `python -m terminal_mcp.browser_worker`.

WHY A SUBPROCESS AND NOT AN IN-PROCESS CALL
-------------------------------------------
Three reasons, and each one alone would be enough.

1. A HARD TIMEOUT THAT IS REAL. A wedged page, a hung CDP handshake or a
   browser that will not exit cannot be cancelled from inside the calling
   thread -- Playwright's own per-operation timeouts help, but they do not
   cover "the driver never answered". The parent spawns this in its own
   process GROUP and kills the group when the clock runs out, which is
   the only cleanup that is guaranteed to work.
2. NO EVENT-LOOP COLLISION. The MCP server runs inside asyncio. Playwright's
   sync API refuses to start inside a running loop, and its async API would
   put a second driver's lifetime under the server's own lifespan -- the
   same class of coupling `chatgpt_sidecar` exists to avoid. A child process
   simply cannot interfere with the server's loop.
3. CRASH ISOLATION. Chromium segfaults. When it does, it takes this process
   down and the parent reports BROWSER_CRASHED -- it does not take the
   controller with it.

THE CONTRACT
------------
One JSON job on stdin, exactly one JSON object on stdout, then exit. Never
partial output, never a stack trace on stdout: an exception becomes
`{"ok": false, "error": ..., "detail": ...}` so the parent always has
something structured to forward. Diagnostics go to stderr, which the parent
captures separately and never ships to a chat verbatim.

WHAT IT DOES NOT DECIDE
-----------------------
It does not judge assertions and it does not validate URLs. It reports what
it SAW -- final url, status, title, visible text, console/network errors,
and a count+text probe for every selector it was asked about -- and
`browser_script.evaluate` turns that into PASS/FAIL in the parent, where it
is testable without a browser. Keeping the verdict out of the child is what
makes the verdict trustworthy.
"""
from __future__ import annotations

import json
import sys
from typing import Any
from dataclasses import replace
from .browser_safety import UrlPolicy, UrlRejected, validate_url, scrub
from .browser_network import GuardedProxy

#: Mirrors of the parent's caps. Applied HERE too, so a huge page is cut
#: before it is serialised through a pipe rather than after.
MAX_TEXT_CHARS = 8_000
MAX_MESSAGE_CHARS = 600
MAX_MESSAGES = 25
MAX_SELECTORS = 50
MAX_SELECTOR_TEXT = 400

#: Never negotiable. A remote debugging port turns the browser into an
#: unauthenticated RPC endpoint for anything that can reach the port, which
#: is precisely the posture the rest of this project spends its effort
#: closing. Stripped from operator-supplied args rather than trusted.
_FORBIDDEN_ARG_PREFIXES = (
    "--remote-debugging-port", "--remote-debugging-address",
    "--remote-allow-origins", "--load-extension", "--disable-web-security",
)


def _fail(error: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "detail": detail}


def _clip(text: Any, limit: int) -> str:
    return scrub(text, limit)


def _safe_args(args: Any) -> list[str]:
    # Operator arguments cannot override the network boundary or load profiles.
    allowed = {"--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"}
    return [arg for arg in list(args or [])[:20] if arg in allowed]


def _probe_selectors(page: Any, selectors: Any) -> dict[str, dict[str, Any]]:
    """count + first element's text for each selector, never raising.

    A bad selector is reported as an `error` on that probe rather than
    aborting the run: one malformed assertion must not cost the caller the
    verdict on the other nine.
    """
    probes: dict[str, dict[str, Any]] = {}
    for selector in list(selectors or [])[:MAX_SELECTORS]:
        if not isinstance(selector, str) or not selector.strip():
            continue
        try:
            locator = page.locator(selector)
            count = locator.count()
            text = ""
            if count:
                try:
                    text = locator.first.inner_text(timeout=2_000)[:MAX_SELECTOR_TEXT]
                except Exception:  # noqa: BLE001 -- hidden/detached element
                    try:
                        text = _clip(locator.first.text_content(timeout=2_000) or "",
                                     MAX_SELECTOR_TEXT)
                    except Exception:  # noqa: BLE001
                        text = ""
            probes[selector] = {"count": int(count), "text": text}
        except Exception as exc:  # noqa: BLE001 -- invalid selector syntax
            probes[selector] = {"error": f"{type(exc).__name__}: {exc}"[:200]}
    return probes


def _run_step(page: Any, step: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    action = step.get("action")
    target = step.get("target") or ""
    value = step.get("value") or ""
    try:
        if action == "goto":
            response = page.goto(target, wait_until="load", timeout=timeout_ms)
            return {"action": action, "target": target, "status": "OK",
                    "http_status": response.status if response else None}
        if action == "click":
            page.click(target, timeout=timeout_ms)
        elif action == "fill":
            page.fill(target, value, timeout=timeout_ms)
        elif action == "press":
            page.keyboard.press(value)
        elif action == "wait_for":
            page.wait_for_selector(target, timeout=timeout_ms)
        elif action == "wait_ms":
            page.wait_for_timeout(min(int(value or 0), timeout_ms))
        elif action == "scroll_bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif action in ("assert", "screenshot"):
            # Judged / captured by the caller after the run completes.
            return {"action": action, "target": target, "status": "OK"}
        else:
            return {"action": action, "target": target, "status": "ERROR",
                    "detail": f"unsupported action {action!r}"}
        return {"action": action, "target": target, "status": "OK"}
    except Exception as exc:  # noqa: BLE001 -- one step failing is a result, not a crash
        return {"action": action, "target": target, "status": "FAILED",
                "detail": f"{type(exc).__name__}: {exc}"[:300]}


def run_job(job: dict[str, Any]) -> dict[str, Any]:
    """Execute one job. Returns the payload the parent will post-process."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        return _fail("BROWSER_DEPENDENCY_UNAVAILABLE",
                     f"playwright is not importable ({type(exc).__name__}); "
                     "install it with `pip install playwright` and "
                     "`python -m playwright install chromium`")

    op = job.get("op") or "verify"
    url = job.get("url")
    viewport = job.get("viewport") or {}
    timeout_ms = int(job.get("navigation_timeout_ms") or 20_000)
    headless = bool(job.get("headless", True))
    executable_path = job.get("executable_path") or None
    artifact_path = job.get("artifact_path") or None

    console_errors: list[str] = []
    page_errors: list[str] = []
    network_errors: list[dict[str, Any]] = []

    launch_kwargs: dict[str, Any] = {
        "headless": headless,
        "args": _safe_args(job.get("browser_args")) or ["--disable-dev-shm-usage"],
    }
    if executable_path:
        launch_kwargs["executable_path"] = executable_path

    policy = replace(UrlPolicy(**(job.get("policy") or {})), resolve_dns=True)
    with GuardedProxy(policy) as proxy, sync_playwright() as driver:
        launch_kwargs["proxy"] = {"server": proxy.url, "bypass": "<-loopback>"}
        launch_kwargs["args"] += ["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"]
        try:
            browser = driver.chromium.launch(**launch_kwargs)
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            code = ("BROWSER_DEPENDENCY_UNAVAILABLE"
                    if "executable" in message.lower() or "install" in message.lower()
                    else "BROWSER_LAUNCH_FAILED")
            return _fail(code, message[:600])

        if op == "probe":
            version = browser.version
            browser.close()
            return {"ok": True, "probe": {"engine": "playwright-chromium",
                                          "browser_version": version,
                                          "executable_path": executable_path}}

        try:
            context = browser.new_context(
                viewport={"width": int(viewport.get("width") or 1280),
                          "height": int(viewport.get("height") or 800)},
                service_workers="block", accept_downloads=False,
                ignore_https_errors=bool(job.get("ignore_https_errors", False)),
            )
            # No storage is ever loaded and none is ever persisted: each run
            # starts from a clean context, so a verification cannot silently
            # depend on a cookie a previous chat left behind.
            blocked: list[str] = []
            def guard(route):
                try:
                    validate_url(route.request.url, policy)
                except UrlRejected as exc:
                    if len(blocked) < MAX_MESSAGES:
                        blocked.append(exc.code)
                    route.abort("blockedbyclient")
                else:
                    route.continue_()
            context.route("**/*", guard)
            context.route_web_socket("**/*", lambda ws: ws.close())
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            page.on("console", lambda msg: (
                console_errors.append(_clip(msg.text, MAX_MESSAGE_CHARS))
                if msg.type == "error" and len(console_errors) < MAX_MESSAGES else None))
            page.on("pageerror", lambda err: (
                page_errors.append(_clip(err, MAX_MESSAGE_CHARS))
                if len(page_errors) < MAX_MESSAGES else None))
            page.on("requestfailed", lambda req: (
                network_errors.append({"url": _clip(req.url, 300),
                                       "failure": _clip(
                                           getattr(req.failure, "error_text", req.failure), 200)})
                if len(network_errors) < MAX_MESSAGES else None))
            page.on("response", lambda res: (
                network_errors.append({"url": _clip(res.url, 300), "status": res.status})
                if res.status >= 400 and len(network_errors) < MAX_MESSAGES else None))

            status: int | None = None
            steps_out: list[dict[str, Any]] = []

            if url:
                try:
                    response = page.goto(url, wait_until="load", timeout=timeout_ms)
                    status = response.status if response else None
                except Exception as exc:  # noqa: BLE001
                    browser.close()
                    return _fail(blocked[0] if blocked else "NAVIGATION_FAILED",
                                 f"{type(exc).__name__}: {exc}"[:600])

            for step in list(job.get("steps") or [])[:25]:
                result = _run_step(page, step, timeout_ms)
                steps_out.append(result)
                if result.get("action") == "goto" and result.get("http_status") is not None:
                    status = result["http_status"]
                if result.get("status") == "FAILED":
                    # Stop at the first failed step. Continuing would run the
                    # remaining steps against a page state nobody predicted.
                    break

            try:
                text = page.inner_text("body", timeout=5_000)[:MAX_TEXT_CHARS]
            except Exception:  # noqa: BLE001 -- e.g. a non-HTML body
                text = ""
            try:
                title = page.title()[:300]
            except Exception:  # noqa: BLE001
                title = ""

            probes = _probe_selectors(page, job.get("selectors"))

            screenshot: str | None = None
            if artifact_path:
                try:
                    page.screenshot(path=artifact_path,
                                    full_page=bool(job.get("full_page", False)))
                    screenshot = artifact_path
                except Exception as exc:  # noqa: BLE001 -- never fatal
                    print(f"screenshot failed: {type(exc).__name__}: {exc}", file=sys.stderr)

            if blocked:
                return _fail(blocked[0], "a page request was blocked by URL policy")
            observation = {
                "final_url": _clip(page.url, 2_048),
                "status": status,
                "title": title,
                "text": text,
                "console_errors": console_errors[:MAX_MESSAGES],
                "page_errors": page_errors[:MAX_MESSAGES],
                "network_errors": network_errors[:MAX_MESSAGES],
                "selector_probes": probes,
                "screenshot": screenshot,
            }
            return {"ok": True, "observation": observation, "steps": steps_out}
        finally:
            # Closing the BROWSER closes every context and page under it, and
            # `sync_playwright`'s own __exit__ then stops the driver process.
            # Both must happen even on the error paths above, or this process
            # exits leaving a chromium behind.
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        job = json.loads(raw) if raw.strip() else {}
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(_fail("BAD_JOB", f"{type(exc).__name__}: {exc}")))
        return 0
    try:
        result = run_job(job)
    except Exception as exc:  # noqa: BLE001 -- stdout must always be one JSON object
        result = _fail("BROWSER_WORKER_ERROR", f"{type(exc).__name__}: {exc}"[:600])
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
