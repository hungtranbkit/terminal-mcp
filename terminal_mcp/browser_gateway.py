"""Phase-1 browser gateway: let a chat ask Terminal MCP to verify a web UI.

WHAT THIS IS FOR
----------------
An agent working in a session starts a dev server and says "the dashboard
renders now". Nobody can see it. Before this, confirming that meant a human
opening a browser, which is exactly the loop Terminal MCP exists to close
for terminals. `browser_verify` closes it for web UIs: one call, a real
Chromium, a deterministic PASS/FAIL against assertions the caller wrote.

THREE TOOLS, NOT THIRTY
-----------------------
`browser_verify`, `browser_run_task`, `browser_status` and (because a
screenshot is genuinely a different question from a verdict)
`browser_screenshot`. The surface is deliberately tiny for the same reason
chatgpt_sidecar publishes one tool: on the ChatGPT connector a call is a
visible "Called tool" line, and a browser API decomposed into
navigate/click/read/assert would turn one verification into six of them.
Each tool here answers a whole question.

WHERE THE PIECES LIVE
---------------------
  browser_safety.py  URL policy (schemes, metadata hard-block, allow/deny)
                     and output scrubbing. No browser, no config.
  browser_script.py  The assertion and task-step grammars, and the verdict
                     evaluator. No browser.
  browser_worker.py  The child process that actually drives Playwright.
  browser_gateway.py (this file) composes them, enforces the wall-clock
                     kill, and shapes the compact result a chat receives.

That split is what makes the interesting parts testable without launching
anything: everything that decides -- is this URL allowed, did this
assertion pass, what does the caller see -- runs in the parent.

ENGINE
------
Playwright driving local Chromium. Browser Use was considered and is NOT
Phase 1: it needs an LLM in the loop, which means an API key in this
process and a verdict only as trustworthy as a second model's summary. An
adapter seam is kept (`ENGINE`, and the worker's job/result JSON contract)
so an agentic engine can be added later for `browser_run_task` without the
external tool contract moving.

FAIL CLOSED, ALWAYS BOUNDED
---------------------------
Disabled by default. Refuses before launching anything when the URL is not
allowed. Kills the worker's whole process group on the wall clock. Never
returns an unbounded page, and never returns text that has not been through
the project's redaction rules.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, replace
import logging
import os
import signal
import selectors
import tempfile
import subprocess
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from . import browser_script
from .browser_safety import (MAX_MESSAGES, MAX_TEXT_CHARS, UrlPolicy, UrlRejected,
                             redaction_applied, scrub, scrub_many, validate_url)
from .config import BrowserGatewayConfig

_log = logging.getLogger(__name__)

#: The only engine Phase 1 ships. Reported on every result so a reader can
#: tell later which engine produced a verdict once there is more than one.
ENGINE = "playwright-chromium"

#: Grace added to the hard timeout before SIGKILL follows SIGTERM. Short:
#: a worker that ignored SIGTERM is already wedged.
_KILL_GRACE_SECONDS = 3.0

#: Runs remembered for `browser_status`. A ring buffer, not a log: this is
#: "what has this gateway been doing", not an audit trail.
_HISTORY = 20


def _state_dir() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp"


class BrowserGateway:
    """One gateway per server. Holds no browser -- every run is its own
    process and its own clean browser context, so nothing carries between
    calls and there is no session state to leak or to go stale."""

    def __init__(self, config: BrowserGatewayConfig | None = None, *,
                 runner: Any = None) -> None:
        self.config = config or BrowserGatewayConfig()
        # Injectable so the parent's timeout/redaction/bounding logic can be
        # tested without Chromium. Production always uses _spawn_worker.
        self._runner = runner or self._spawn_worker
        self._history: deque[dict[str, Any]] = deque(maxlen=_HISTORY)

    # -- configuration -----------------------------------------------------

    @classmethod
    def from_config(cls, config: BrowserGatewayConfig | None = None, **kwargs: Any) -> "BrowserGateway":
        """Apply environment overrides on top of config.yaml.

        Env wins because these are the settings an operator reaches for
        while debugging a specific run (`headless=false` to watch it), and
        editing config.yaml to do that would mean restarting into a state
        they then have to remember to undo.
        """
        from .config import _load_browser_gateway_config
        values = asdict(config or BrowserGatewayConfig())
        for key, default in values.items():
            raw = os.environ.get("TERMINAL_MCP_BROWSER_" + key.upper())
            if raw is None:
                if isinstance(default, tuple):
                    values[key] = list(default)
                continue
            if isinstance(default, bool):
                if raw.lower() not in ("true", "false", "1", "0"):
                    raise ValueError(f"invalid boolean browser environment setting: {key}")
                values[key] = raw.lower() in ("true", "1")
            elif isinstance(default, int):
                values[key] = int(raw)
            elif isinstance(default, float):
                values[key] = float(raw)
            elif isinstance(default, tuple):
                values[key] = json.loads(raw)
            else:
                values[key] = raw
        base = _load_browser_gateway_config(values)
        return cls(base, **kwargs)

    @property
    def policy(self) -> UrlPolicy:
        return UrlPolicy(
            allow_loopback=self.config.allow_loopback,
            allow_private_networks=self.config.allow_private_networks,
            allow_patterns=tuple(self.config.allow_url_patterns),
            deny_patterns=tuple(self.config.deny_url_patterns),
            resolve_dns=False, # DNS runs inside the hard-deadline worker.
        )

    def artifact_dir(self) -> Path:
        configured = (self.config.artifact_dir or "").strip()
        path = Path(configured).expanduser() if configured else _state_dir() / "browser-artifacts"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        return path

    # -- the worker --------------------------------------------------------

    def _spawn_worker(self, job: dict[str, Any], hard_timeout: float) -> dict[str, Any]:
        """Run one job in a child process, and guarantee it is gone after.

        `start_new_session=True` puts the worker in its own process GROUP,
        which is what makes the kill below cover Chromium and the Playwright
        driver too -- killing only the Python child would leave the browser
        it spawned running forever.
        """
        deadline = time.monotonic() + hard_timeout
        chunks = {"stdout": bytearray(), "stderr": bytearray()}
        # File input avoids a blocked stdin write before the timeout starts.
        with tempfile.TemporaryFile() as source, selectors.DefaultSelector() as selector:
            source.write(json.dumps(job).encode())
            source.seek(0)
            try:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "terminal_mcp.browser_worker"],
                    stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                return {"ok": False, "error": "BROWSER_LAUNCH_FAILED", "detail": str(exc)}
            try:
                for name in chunks:
                    pipe = getattr(proc, name)
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, name)
                while selector.get_map() or proc.poll() is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return {"ok": False, "error": "BROWSER_TIMEOUT",
                                "detail": "browser exceeded its hard deadline and was terminated"}
                    for key, _ in selector.select(min(remaining, 0.1)):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        chunks[key.data].extend(data)
                        if sum(map(len, chunks.values())) > 262144:
                            return {"ok": False, "error": "BROWSER_OUTPUT_LIMIT",
                                    "detail": "worker exceeded the 256 KiB output budget"}
            finally:
                self._kill_group(proc)
                proc.stdout.close()
                proc.stderr.close()
        out = chunks["stdout"].decode("utf-8", errors="replace")
        err = chunks["stderr"].decode("utf-8", errors="replace")
        if err:
            _log.debug("browser worker stderr: %s", scrub(err, 500))
        if proc.returncode != 0 and not (out or "").strip():
            return {"ok": False, "error": "BROWSER_CRASHED",
                    "detail": f"worker exited {proc.returncode}: {scrub(err, 300)}"}
        try:
            payload = json.loads((out or "").strip().splitlines()[-1])
            if not isinstance(payload, dict):
                raise ValueError("worker response must be an object")
            return payload
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "BROWSER_WORKER_ERROR",
                    "detail": f"unreadable worker output ({type(exc).__name__})"}

    @staticmethod
    def _kill_group(proc: subprocess.Popen) -> None:
        # The leader may already be reaped while Chromium remains alive.
        # start_new_session fixes pgid == pid; getpgid would fail in that case.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)

    # -- shared plumbing ---------------------------------------------------

    def _unavailable(self) -> dict[str, Any] | None:
        if not self.config.enabled:
            return {"status": "ERROR", "error": "BROWSER_GATEWAY_DISABLED",
                    "detail": "set browser.enabled: true in config.yaml (or "
                              "TERMINAL_MCP_BROWSER_ENABLED=1) to allow this server "
                              "to drive a browser", "engine": ENGINE}
        return None

    def _viewport(self, width: Any, height: Any) -> dict[str, int]:
        def clamp(value: Any, default: int) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return default
            return max(200, min(4_096, number))
        return {"width": clamp(width, self.config.viewport_width),
                "height": clamp(height, self.config.viewport_height)}

    def _budget(self, timeout_seconds: Any) -> tuple[int, float]:
        """(per-operation ms, wall-clock seconds).

        A caller-supplied timeout narrows the configured one and can never
        widen it: a chat must not be able to pin a browser open for ten
        minutes by asking nicely.
        """
        navigation = float(self.config.navigation_timeout_seconds)
        hard = float(self.config.hard_timeout_seconds)
        try:
            requested = float(timeout_seconds)
        except (TypeError, ValueError):
            requested = 0.0
        if math.isfinite(requested) and requested > 0:
            navigation = max(1.0, min(navigation, requested))
            hard = min(hard, requested + 5.0)
        return int(navigation * 1_000), hard

    def _artifact_path(self, want: bool) -> str | None:
        if not want or not self.config.screenshots_enabled:
            return None
        try:
            return str(self.artifact_dir() / f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.png")
        except OSError as exc:  # noqa: BLE001 -- a screenshot is never worth failing a run
            _log.warning("browser: artifact dir unusable (%s)", exc)
            return None

    def _prune_artifacts(self) -> None:
        keep = int(self.config.keep_artifacts)
        try:
            files = sorted(self.artifact_dir().glob("*.png"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for stale in files[:max(0, len(files) - keep)]:
            try:
                stale.unlink()
            except OSError:
                continue

    def _job(self, *, url: str | None, viewport: dict[str, int], nav_ms: int,
             selectors: list[str], steps: list[dict[str, Any]] | None,
             artifact: str | None, op: str = "verify") -> dict[str, Any]:
        return {
            "policy": asdict(self.policy),
            "op": op, "url": url, "viewport": viewport,
            "navigation_timeout_ms": nav_ms,
            "headless": bool(self.config.headless),
            "executable_path": (self.config.executable or "").strip() or None,
            "selectors": selectors, "steps": steps or [],
            "artifact_path": artifact,
            "ignore_https_errors": bool(self.config.ignore_https_errors),
            "browser_args": list(self.config.browser_args),
        }

    def _observation_fields(self, observation: browser_script.Observation) -> dict[str, Any]:
        """The evidence block, scrubbed and bounded, with truncation stated.

        Counts come from the RAW lists, not the trimmed ones: "3 console
        errors, 2 shown" is useful, "2 console errors" when there were
        three is a lie the reader cannot detect.
        """
        console, console_dropped = scrub_many(observation.console_errors)
        page_errs, page_dropped = scrub_many(observation.page_errors)
        network_rows = [
            f"{row.get('status') or row.get('failure') or 'ERR'} {row.get('url', '')}"
            for row in observation.network_errors]
        network, network_dropped = scrub_many(network_rows)
        block: dict[str, Any] = {
            "final_url": scrub(observation.final_url, 2_048),
            "http_status": observation.status,
            "title": scrub(observation.title, 300),
            "console_errors": console,
            "console_error_count": len(observation.console_errors),
            "page_errors": page_errs,
            "page_error_count": len(observation.page_errors),
            "network_errors": network,
            "network_error_count": len(observation.network_errors),
        }
        dropped = console_dropped + page_dropped + network_dropped
        if dropped:
            block["messages_omitted"] = dropped
        if observation.screenshot:
            block["screenshot"] = observation.screenshot
        return block

    def _record(self, kind: str, url: str, status: str, duration_ms: int) -> None:
        self._history.append({
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "tool": kind,
            "url": scrub(url, 200), "status": status, "duration_ms": duration_ms,
        })

    def _error(self, code: str, detail: str, **extra: Any) -> dict[str, Any]:
        return {"status": "ERROR", "error": code, "detail": scrub(detail, 600),
                "engine": ENGINE, **extra}

    # -- tools -------------------------------------------------------------

    def verify(self, url: str, assertions: Any = None, *,
               viewport_width: Any = None, viewport_height: Any = None,
               timeout_seconds: Any = None, screenshot: bool = False,
               wait_for: str | None = None) -> dict[str, Any]:
        """Open `url`, judge `assertions`, return one compact verdict."""
        disabled = self._unavailable()
        if disabled:
            return disabled
        started = time.monotonic()
        try:
            target = validate_url(url, self.policy)
        except UrlRejected as exc:
            return self._error(exc.code, exc.detail, url=scrub(url, 300))

        checks, parse_errors = browser_script.parse_assertions(assertions)
        viewport = self._viewport(viewport_width, viewport_height)
        nav_ms, hard = self._budget(timeout_seconds)
        steps: list[dict[str, Any]] = []
        if isinstance(wait_for, str) and wait_for.strip():
            steps.append({"action": "wait_for", "target": wait_for.strip()[:300]})
        job = self._job(url=target, viewport=viewport, nav_ms=nav_ms,
                        selectors=browser_script.selectors_for(checks),
                        steps=steps, artifact=self._artifact_path(screenshot))

        raw = self._runner(job, hard)
        duration_ms = int((time.monotonic() - started) * 1_000)
        if not raw.get("ok"):
            self._record("browser_verify", target, "ERROR", duration_ms)
            return self._error(raw.get("error") or "BROWSER_ERROR",
                               raw.get("detail") or "the browser reported no detail",
                               url=scrub(target, 300), duration_ms=duration_ms)

        observation = browser_script.Observation.from_payload(raw.get("observation"))
        results = browser_script.evaluate(checks, observation)
        failed = [r for r in results if r["status"] == "FAIL"]
        errored = [r for r in results if r["status"] == "ERROR"]
        # No assertions is not a PASS by default: the caller asked "does this
        # page work" and we can only answer "it loaded". Say that, and let the
        # HTTP status and the error counts carry the weight.
        if parse_errors:
            verdict = "ERROR"
        elif errored:
            verdict = "ERROR"
        elif failed:
            verdict = "FAIL"
        elif checks:
            verdict = "PASS"
        else:
            verdict = "PASS" if (observation.status or 200) < 400 else "FAIL"

        result: dict[str, Any] = {
            "status": verdict,
            "url": scrub(target, 300),
            "engine": ENGINE,
            "viewport": viewport,
            "duration_ms": duration_ms,
            "checks": results,
            "summary": {"total": len(results), "passed": len(results) - len(failed) - len(errored),
                        "failed": len(failed), "errors": len(errored)},
            "failures": [scrub(f"{r['assertion']} -- {r.get('detail', '')}") for r in failed + errored],
            **self._observation_fields(observation),
        }
        if parse_errors:
            result["assertion_errors"] = [scrub(e) for e in parse_errors]
        if redaction_applied(observation.text, observation.final_url,
                             " ".join(observation.console_errors)):
            result["redacted"] = True
        self._prune_artifacts()
        self._record("browser_verify", target, verdict, duration_ms)
        return result

    def run_task(self, task: str, *, url: str | None = None,
                 viewport_width: Any = None, viewport_height: Any = None,
                 timeout_seconds: Any = None, session_id: str | None = None,
                 screenshot: bool = False) -> dict[str, Any]:
        """Run a short scripted browser task written in plain steps.

        `session_id` is a CORRELATION LABEL only. Phase 1 starts every run
        in a fresh browser context with no stored cookies or origin state,
        so two calls sharing a session_id do not share a login. It is
        echoed back and recorded so a multi-step chat can group its own
        runs; it is documented this way rather than silently ignored,
        because a caller who believes it persists a session would build on
        an assumption that is not true.
        """
        disabled = self._unavailable()
        if disabled:
            return disabled
        started = time.monotonic()

        steps, parse_errors = browser_script.parse_task(task)
        if parse_errors or not steps:
            # Never half-execute. See browser_script.parse_task.
            return self._error(
                "TASK_NOT_PLANNABLE",
                "this task could not be turned into browser steps; Phase 1 runs a "
                "deterministic step grammar, not an agent. Use steps like "
                "'open <url>', 'click <selector>', 'fill <selector> with <text>', "
                "'wait for <selector>', 'assert text contains: <text>'.",
                unparsed=[scrub(e) for e in parse_errors][:10],
                session_id=scrub(session_id, 120) if session_id else None)

        target: str | None = None
        if url:
            try:
                target = validate_url(url, self.policy)
            except UrlRejected as exc:
                return self._error(exc.code, exc.detail, url=scrub(url, 300))
        # Every `goto` inside the task goes through the SAME gate the `url`
        # argument does -- otherwise the policy would be a front door with
        # the back door left open.
        for step in steps:
            if step.action == "goto":
                try:
                    validate_url(step.target, self.policy)
                except UrlRejected as exc:
                    return self._error(exc.code, f"step {step.raw!r}: {exc.detail}")

        viewport = self._viewport(viewport_width, viewport_height)
        nav_ms, hard = self._budget(timeout_seconds)
        checks = [s.check for s in steps if s.check is not None]
        wire_steps = [{"action": s.action, "target": s.target, "value": s.value} for s in steps]
        want_shot = screenshot or any(s.action == "screenshot" for s in steps)
        job = self._job(url=target, viewport=viewport, nav_ms=nav_ms,
                        selectors=browser_script.selectors_for(checks, steps),
                        steps=wire_steps, artifact=self._artifact_path(want_shot),
                        op="task")

        raw = self._runner(job, hard)
        duration_ms = int((time.monotonic() - started) * 1_000)
        if not raw.get("ok"):
            self._record("browser_run_task", target or "(task)", "ERROR", duration_ms)
            return self._error(raw.get("error") or "BROWSER_ERROR",
                               raw.get("detail") or "the browser reported no detail",
                               duration_ms=duration_ms)

        observation = browser_script.Observation.from_payload(raw.get("observation"))
        step_results = list(raw.get("steps") or [])
        check_results = browser_script.evaluate(checks, observation)
        step_failed = [s for s in step_results if s.get("status") in ("FAILED", "ERROR")]
        check_failed = [c for c in check_results if c["status"] != "PASS"]
        verdict = "FAILED" if (step_failed or check_failed) else "OK"

        result: dict[str, Any] = {
            "status": verdict,
            "engine": ENGINE,
            "task": "deterministic browser actions",
            "session_id": scrub(session_id, 120) if session_id else None,
            "session_state": "not-persisted",
            "viewport": viewport,
            "duration_ms": duration_ms,
            "steps": [{"action": scrub(s.get("action"), 60),
                       "target": scrub(s.get("target"), 200),
                       "status": scrub(s.get("status"), 20),
                       **({"detail": scrub(s.get("detail"), 300)} if s.get("detail") else {})}
                      for s in step_results[:browser_script.MAX_STEPS]],
            "checks": check_results,
            "failures": ([scrub(f"step {s.get('action')} {s.get('target', '')}: {s.get('detail', '')}")
                          for s in step_failed[:browser_script.MAX_STEPS]]
                         + [f"{c['assertion']} -- {c.get('detail', '')}" for c in check_failed]),
            **self._observation_fields(observation),
        }
        self._prune_artifacts()
        self._record("browser_run_task", target or "(task)", verdict, duration_ms)
        return result

    def screenshot(self, url: str, *, viewport_width: Any = None,
                   viewport_height: Any = None, full_page: bool = False,
                   timeout_seconds: Any = None) -> dict[str, Any]:
        """Capture one screenshot. Returns the artifact PATH, never the
        image bytes: a base64 PNG in a tool result is tens of thousands of
        tokens of context for something the operator reads off disk."""
        disabled = self._unavailable()
        if disabled:
            return disabled
        if not self.config.screenshots_enabled:
            return self._error("SCREENSHOTS_DISABLED",
                               "browser.screenshots_enabled is false")
        started = time.monotonic()
        try:
            target = validate_url(url, self.policy)
        except UrlRejected as exc:
            return self._error(exc.code, exc.detail, url=scrub(url, 300))
        viewport = self._viewport(viewport_width, viewport_height)
        nav_ms, hard = self._budget(timeout_seconds)
        job = self._job(url=target, viewport=viewport, nav_ms=nav_ms, selectors=[],
                        steps=[], artifact=self._artifact_path(True), op="screenshot")
        job["full_page"] = bool(full_page)

        raw = self._runner(job, hard)
        duration_ms = int((time.monotonic() - started) * 1_000)
        if not raw.get("ok"):
            self._record("browser_screenshot", target, "ERROR", duration_ms)
            return self._error(raw.get("error") or "BROWSER_ERROR",
                               raw.get("detail") or "the browser reported no detail",
                               duration_ms=duration_ms)
        observation = browser_script.Observation.from_payload(raw.get("observation"))
        path = observation.screenshot
        size = None
        if path:
            try:
                size = Path(path).stat().st_size
            except OSError:
                size = None
        self._prune_artifacts()
        self._record("browser_screenshot", target, "OK" if path else "ERROR", duration_ms)
        if not path:
            return self._error("SCREENSHOT_FAILED", "the browser produced no image",
                               duration_ms=duration_ms)
        return {"status": "OK", "engine": ENGINE, "url": scrub(target, 300),
                "screenshot": path, "bytes": size, "viewport": viewport,
                "full_page": bool(full_page), "duration_ms": duration_ms,
                "final_url": scrub(observation.final_url, 2_048),
                "http_status": observation.status}

    def status(self, *, probe: bool = False) -> dict[str, Any]:
        """Whether this gateway can actually drive a browser right now.

        `probe=False` answers from configuration and an import check alone,
        which costs nothing. `probe=True` really launches and closes
        Chromium -- the only answer that is evidence rather than a guess,
        and the one to use when a chat reports that verification "isn't
        working".
        """
        config = self.config
        available, detail = self._dependency_state()
        body: dict[str, Any] = {
            "enabled": bool(config.enabled),
            "engine": ENGINE,
            "dependency_available": available,
            "dependency_detail": detail,
            "headless": bool(config.headless),
            "executable": scrub((config.executable or "").strip() or "(playwright bundled chromium)"),
            "default_viewport": {"width": config.viewport_width, "height": config.viewport_height},
            "navigation_timeout_seconds": config.navigation_timeout_seconds,
            "hard_timeout_seconds": config.hard_timeout_seconds,
            "url_policy": {
                "allowed_schemes": ["http", "https"],
                "allow_loopback": config.allow_loopback,
                "allow_private_networks": config.allow_private_networks,
                "allow_patterns": [scrub(p) for p in config.allow_url_patterns[:20]],
                "deny_patterns": [scrub(p) for p in config.deny_url_patterns[:20]],
                "metadata_endpoints": "always blocked",
            },
            "screenshots_enabled": bool(config.screenshots_enabled),
            "session_state": "not-persisted (every run uses a clean browser context)",
            "recent_runs": list(self._history)[-10:],
        }
        if config.screenshots_enabled:
            try:
                body["artifact_dir"] = str(self.artifact_dir())
            except OSError as exc:  # noqa: BLE001
                body["artifact_dir"] = f"(unusable: {type(exc).__name__})"
        if probe:
            if not config.enabled:
                body["probe"] = {"ok": False, "error": "BROWSER_GATEWAY_DISABLED"}
            else:
                _nav_ms, hard = self._budget(None)
                raw = self._runner({"op": "probe",
                                    "executable_path": (config.executable or "").strip() or None,
                                    "headless": True,
                                    "browser_args": list(config.browser_args)}, hard)
                body["probe"] = ({"ok": True, **(raw.get("probe") or {})} if raw.get("ok")
                                 else {"ok": False, "error": raw.get("error"),
                                       "detail": scrub(raw.get("detail"), 400)})
        return body

    @staticmethod
    def _dependency_state() -> tuple[bool, str]:
        """Typed dependency-unavailable answer, without importing Playwright
        into the server process -- `find_spec` looks, it does not load."""
        import importlib.util

        try:
            spec = importlib.util.find_spec("playwright")
        except (ImportError, ValueError):
            spec = None
        if spec is None:
            return False, ("playwright is not installed; `pip install playwright` then "
                           "`python -m playwright install chromium`")
        return True, "playwright is importable"
