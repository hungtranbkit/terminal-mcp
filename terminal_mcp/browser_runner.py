"""Local execution: the managed browser, and one bounded harness call.

THREE THINGS LIVE HERE, and nothing else:

1. CAPABILITY/VERSION DETECTION -- is Browser Use / Browser Harness
   actually installed on THIS node, and at what version. Probed from the
   provisioning manifest and the real binaries, never declared in config
   (the same rule capability_probe.py exists to enforce). When it is
   missing the gateway degrades to a typed, actionable answer instead of
   raising: "optional dependency" means optional.

2. THE MANAGED BROWSER -- a DEDICATED Chrome, on loopback, with its own
   user-data-dir and its own debugging port. The gateway never attaches to
   whatever Chrome the operator happens to be running: on this very host a
   different project drives a logged-in profile on :9333, and a gateway
   that "found a browser" would be driving someone's real session. Ours is
   launched detached in its own process group and is reused across calls.

3. ONE BOUNDED HARNESS CALL -- the static executor is piped in, the plan
   goes on disk, the child gets a minimal environment, and a timeout kills
   the whole process group. No orphans: every child we start is in a group
   we can and do kill.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .browser_exec import RESULT_SENTINEL

DEFAULT_CDP_PORT = 9444
DEFAULT_CDP_HOST = "127.0.0.1"
BROWSER_START_TIMEOUT_SECONDS = 20.0

#: How long a probe result stays trusted. The runtime (is it installed, at
#: what version) changes only when an operator reprovisions; readiness can
#: change under us if the browser dies, so it is cached far more briefly
#: and any failure clears it immediately.
RUNTIME_CACHE_TTL_SECONDS = 300.0
READY_CACHE_TTL_SECONDS = 120.0

#: Chrome cannot start headless on every host with the default flag set --
#: this box's chrome-sandbox is not root-owned and its GPU process cannot
#: launch, which kills Chrome outright ("GPU process isn't usable.
#: Goodbye."). These flags are what a headless automation browser needs on
#: a server-shaped Linux host; each is overridable by an operator.
DEFAULT_CHROME_FLAGS: tuple[str, ...] = (
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-gpu-sandbox",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-extensions",
)

CHROME_CANDIDATES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")

#: A container image that ships a working Chromium. This is the FALLBACK,
#: and it exists because of a real, reproducible host defect: on
#: dell-linux a shell-launched Chrome answers CDP and accepts
#: Page.navigate, but its renderer never executes -- Runtime.evaluate
#: hangs, `--dump-dom` hangs, and it behaves identically headless, under
#: Xvfb, with --single-process, and with the system Chrome or a
#: user-owned Chromium build. The same Chromium inside this image, on the
#: host network, evaluates fine. See docs/browser-gateway.md.
DEFAULT_DOCKER_IMAGE = "mcr.microsoft.com/playwright:v1.63.0-noble"
CONTAINER_NAME_PREFIX = "tmcp-browser"

#: Launch strategies. "auto" tries the host and falls back to docker,
#: which is what makes the gateway work on a healthy node AND on this one.
LAUNCH_MODES = ("auto", "host", "docker", "external")

#: The renderer readiness probe. "CDP answers" is NOT readiness: a broken
#: Chrome answers /json/version perfectly and then never runs a line of
#: JavaScript. Readiness is "this browser evaluated an expression".
_PROBE_SCRIPT = "print('__TMCP_PROBE__' + str(js('1+1')))\n"
_PROBE_SENTINEL = "__TMCP_PROBE__"


def default_browser_home() -> Path:
    override = os.environ.get("TERMINAL_MCP_BROWSER_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "terminal-mcp" / "browser-harness"


def default_artifact_dir() -> Path:
    override = os.environ.get("TERMINAL_MCP_BROWSER_ARTIFACT_DIR")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "browser-artifacts"


@dataclass(frozen=True)
class BrowserRuntime:
    """What this node can actually do, as probed."""

    available: bool
    reason: str = ""
    package_version: str = ""
    harness_version: str = ""
    interpreter: str = ""
    cli: str = ""
    chrome: str = ""
    recording: str = "off"
    docker: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "browser_use_version": self.package_version,
            "harness_version": self.harness_version,
            "cli": self.cli,
            "chrome": self.chrome,
            "recording": self.recording,
            "docker_fallback": self.docker,
        }


def _which(name: str) -> str:
    from shutil import which

    return which(name) or ""


def detect_runtime(home: Path | None = None) -> BrowserRuntime:
    """Probe the installed Browser Use / Browser Harness, if any.

    Order: the provisioned venv first (what provision-browser-harness.sh
    creates), then a PATH install, so an operator who installed it their
    own way is still detected.
    """
    home = home or default_browser_home()
    cli = ""
    interpreter = ""
    package_version = ""

    venv_cli = home / "venv" / "bin" / "browser-harness"
    venv_python = home / "venv" / "bin" / "python"
    if venv_cli.exists() and venv_python.exists():
        cli, interpreter = str(venv_cli), str(venv_python)
    else:
        path_cli = _which("browser-harness")
        if path_cli:
            cli = path_cli

    if not cli:
        return BrowserRuntime(
            available=False,
            reason=("browser-harness is not installed; run "
                    "scripts/provision-browser-harness.sh on this node"),
            chrome=_chrome_binary(),
        )

    manifest = home / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            package_version = str(data.get("version", ""))
            interpreter = interpreter or str(data.get("interpreter", ""))
        except (OSError, json.JSONDecodeError):
            package_version = ""

    harness_version = _harness_version(cli)
    chrome = _chrome_binary()
    docker = bool(_which("docker"))
    if not chrome and not docker:
        return BrowserRuntime(
            available=False,
            reason="no Chrome/Chromium binary and no docker fallback for the managed browser",
            package_version=package_version, harness_version=harness_version,
            interpreter=interpreter, cli=cli,
        )
    return BrowserRuntime(
        available=True, package_version=package_version, harness_version=harness_version,
        interpreter=interpreter, cli=cli, chrome=chrome, docker=docker,
        recording="on" if recording_enabled() else "off",
    )


def _harness_version(cli: str) -> str:
    try:
        proc = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"\d+\.\d+\.\d+", (proc.stdout or "") + (proc.stderr or ""))
    return match.group(0) if match else ""


def _chrome_binary() -> str:
    override = os.environ.get("TERMINAL_MCP_BROWSER_CHROME")
    if override:
        return override if Path(override).exists() else ""
    for candidate in CHROME_CANDIDATES:
        found = _which(candidate)
        if found:
            return found
    return ""


def recording_enabled() -> bool:
    """Recording is OFF unless an operator turns it on, deliberately.

    A browser that silently records every verification into a video/trace
    directory is a privacy problem and a disk problem. The gateway never
    flips this on for a caller; only this env var does.
    """
    return (os.environ.get("TERMINAL_MCP_BROWSER_ALLOW_RECORDING") or "").strip() in {"1", "true", "yes"}


class LocalBrowserRunner:
    """Runs plans on this host. One instance owns one managed browser."""

    def __init__(self, *, home: Path | None = None, artifact_dir: Path | None = None,
                 cdp_host: str = DEFAULT_CDP_HOST, cdp_port: int | None = None) -> None:
        self.home = home or default_browser_home()
        self.artifact_dir = artifact_dir or default_artifact_dir()
        self.cdp_host = cdp_host
        self.cdp_port = int(cdp_port or os.environ.get("TERMINAL_MCP_BROWSER_CDP_PORT")
                            or DEFAULT_CDP_PORT)
        self.profile_dir = self.home / "chrome-profile"
        self.pid_file = self.home / "chrome.pid"
        # Two caches, both for the same reason: a probe that is honest is
        # also expensive (a `browser-harness --version` fork, a full
        # evaluate round trip), and re-paying it on every single verify
        # would put seconds of pure overhead on the latency budget.
        self._runtime_cache: tuple[float, BrowserRuntime] | None = None
        self._ready_until: float = 0.0

    # -- capability ------------------------------------------------------
    @property
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"

    def runtime(self, *, refresh: bool = False) -> BrowserRuntime:
        cached = self._runtime_cache
        if not refresh and cached and (time.monotonic() - cached[0]) < RUNTIME_CACHE_TTL_SECONDS:
            return cached[1]
        detected = detect_runtime(self.home)
        self._runtime_cache = (time.monotonic(), detected)
        return detected

    # -- managed browser -------------------------------------------------
    def browser_alive(self, timeout: float = 2.0) -> bool:
        try:
            with urllib.request.urlopen(f"{self.cdp_url}/json/version", timeout=timeout):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def renderer_ready(self, runtime: BrowserRuntime | None = None,
                       timeout: float = 40.0, *, force: bool = False) -> bool:
        """Did this browser actually EVALUATE something?

        The distinction matters more than it sounds. A Chrome whose
        renderer cannot start still binds the debugging port, still
        answers /json/version, and still returns a result for
        Page.navigate -- and then every plan hangs on its first
        assertion. Liveness is a port; readiness is an evaluation.
        """
        if not force and time.monotonic() < self._ready_until:
            return True
        runtime = runtime or self.runtime()
        if not runtime.cli:
            return False
        try:
            proc = subprocess.run(  # noqa: S603 -- fixed CLI, fixed script
                [runtime.cli], input=_PROBE_SCRIPT, capture_output=True, text=True,
                timeout=timeout, env=self._child_env(), start_new_session=True,
            )
        except (subprocess.SubprocessError, OSError):
            self._ready_until = 0.0
            return False
        ready = f"{_PROBE_SENTINEL}2" in (proc.stdout or "")
        self._ready_until = time.monotonic() + READY_CACHE_TTL_SECONDS if ready else 0.0
        return ready

    def ensure_browser(self, runtime: BrowserRuntime | None = None) -> dict[str, Any]:
        """Bring up a browser that can actually render, or say why not.

        Strategy order for "auto": reuse anything already serving the port
        (verified by the readiness probe, not by the port answering), then
        the host browser, then a container. TERMINAL_MCP_BROWSER_LAUNCH
        pins one strategy when an operator wants no guessing.
        """
        runtime = runtime or self.runtime()
        mode = (os.environ.get("TERMINAL_MCP_BROWSER_LAUNCH") or "auto").strip().lower()
        if mode not in LAUNCH_MODES:
            mode = "auto"

        if self.browser_alive():
            if self.renderer_ready(runtime):
                return {"ok": True, "started": False, "mode": "reused", "cdp_url": self.cdp_url}
            if mode == "external":
                return {"ok": False, "error": "the external browser at "
                                              f"{self.cdp_url} answers CDP but its renderer "
                                              "does not evaluate"}
            # A browser is holding the port but cannot render -- ours from
            # a previous run, or a wedged one. Stop it before trying
            # again, or the next launch silently fails to bind the port
            # and we keep driving the broken instance.
            self.stop_browser()
        elif mode == "external":
            return {"ok": False, "error": f"no browser is serving {self.cdp_url}"}

        attempts: list[str] = []
        if mode in ("auto", "host") and runtime.chrome:
            started = self._launch_host(runtime)
            if started.get("ok") and self.renderer_ready(runtime, force=True):
                return {**started, "mode": "host"}
            attempts.append("host: " + str(started.get("error", "renderer did not evaluate")))
            self.stop_browser()
        if mode in ("auto", "docker") and runtime.docker:
            started = self._launch_docker()
            if started.get("ok") and self.renderer_ready(runtime, force=True):
                return {**started, "mode": "docker"}
            attempts.append("docker: " + str(started.get("error", "renderer did not evaluate")))
            self.stop_browser()
        return {"ok": False, "error": "no browser could be brought up (" + "; ".join(attempts) + ")"
                if attempts else "no launch strategy is available on this node"}

    def _launch_host(self, runtime: BrowserRuntime) -> dict[str, Any]:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        flags = list(DEFAULT_CHROME_FLAGS)
        extra = (os.environ.get("TERMINAL_MCP_BROWSER_CHROME_FLAGS") or "").split()
        command = [
            runtime.chrome, *flags, *extra,
            f"--remote-debugging-port={self.cdp_port}",
            f"--remote-debugging-address={self.cdp_host}",
            f"--user-data-dir={self.profile_dir}",
            "--window-size=1348,768",
            "about:blank",
        ]
        log_path = self.home / "chrome.log"
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "ab") as log:
                proc = subprocess.Popen(  # noqa: S603 -- fixed binary + fixed flags
                    command, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except OSError as exc:
            return {"ok": False, "error": f"could not launch browser: {exc}"}

        try:
            self.pid_file.write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass

        deadline = time.monotonic() + BROWSER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.browser_alive(timeout=1.0):
                return {"ok": True, "started": True, "pid": proc.pid, "cdp_url": self.cdp_url}
            if proc.poll() is not None:
                return {"ok": False, "error": f"browser exited during startup (rc={proc.returncode});"
                                              f" see {log_path}"}
            time.sleep(0.4)
        self._terminate(proc.pid)
        return {"ok": False, "error": f"browser did not answer CDP within "
                                      f"{BROWSER_START_TIMEOUT_SECONDS:.0f}s; see {log_path}"}

    @property
    def container_name(self) -> str:
        return f"{CONTAINER_NAME_PREFIX}-{self.cdp_port}"

    def _launch_docker(self) -> dict[str, Any]:
        """Run the browser in a container on the host network.

        --network host (not a port mapping) keeps the CDP endpoint on
        loopback exactly as the host strategy does, so nothing downstream
        -- the harness, the allowlist, a local dev target the plan
        visits -- has to know which strategy won.
        """
        image = os.environ.get("TERMINAL_MCP_BROWSER_DOCKER_IMAGE") or DEFAULT_DOCKER_IMAGE
        subprocess.run(["docker", "rm", "-f", self.container_name],  # noqa: S603, S607
                       capture_output=True, text=True, timeout=60)
        launch = (
            "exec $(ls /ms-playwright/chromium-*/chrome-linux*/chrome | head -1) "
            "--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage "
            f"--remote-debugging-port={self.cdp_port} "
            f"--remote-debugging-address={self.cdp_host} "
            "--user-data-dir=/tmp/tmcp-profile --window-size=1348,768 "
            "--no-first-run --no-default-browser-check about:blank"
        )
        try:
            proc = subprocess.run(  # noqa: S603, S607 -- fixed argv, no caller data
                ["docker", "run", "--rm", "-d", "--name", self.container_name,
                 "--network", "host", "--shm-size=1g", image, "/bin/sh", "-c", launch],
                capture_output=True, text=True, timeout=180,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return {"ok": False, "error": f"docker run failed: {exc}"}
        if proc.returncode != 0:
            return {"ok": False, "error": _tail(proc.stderr or proc.stdout, 200)}

        deadline = time.monotonic() + BROWSER_START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.browser_alive(timeout=1.0):
                return {"ok": True, "started": True, "container": self.container_name,
                        "cdp_url": self.cdp_url}
            time.sleep(0.4)
        return {"ok": False, "error": "containerized browser did not answer CDP in time"}

    def stop_browser(self) -> dict[str, Any]:
        """Stop the managed browser, whichever way it was started.

        Only ever touches the pid WE recorded and the container WE named
        -- never a process discovered by matching a command line, which on
        this host would find another project's logged-in browser.
        """
        self._ready_until = 0.0
        pid = None
        try:
            pid = int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pid = None
        stopped = False
        if pid:
            stopped = self._terminate(pid)
            try:
                self.pid_file.unlink()
            except OSError:
                pass
        container = False
        if _which("docker"):
            try:
                proc = subprocess.run(  # noqa: S603, S607
                    ["docker", "rm", "-f", self.container_name],
                    capture_output=True, text=True, timeout=60)
                container = proc.returncode == 0
            except (subprocess.SubprocessError, OSError):
                container = False
        return {"stopped": stopped or container, "pid": pid, "container": container,
                "alive": self.browser_alive()}

    @staticmethod
    def _terminate(pid: int) -> bool:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    return sig is signal.SIGTERM
            for _ in range(10):
                time.sleep(0.2)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return True
        return False

    # -- execution -------------------------------------------------------
    def _child_env(self) -> dict[str, str]:
        """A MINIMAL environment for the child.

        The gateway's own process may hold node tokens, API keys and
        Cloudflare secrets; none of that has any business inside a browser
        automation subprocess (or in a crash dump from one). Only what the
        harness needs is passed through.
        """
        parent = os.environ
        env = {
            "PATH": parent.get("PATH", "/usr/bin:/bin"),
            "HOME": parent.get("HOME", str(Path.home())),
            "LANG": parent.get("LANG", "C.UTF-8"),
            "BU_CDP_URL": self.cdp_url,
            "BH_RECORD": "1" if recording_enabled() else "0",
            "BH_TAB_MARKER": "0",
            "BH_OPEN_LIVE_URL": "0",
            "BH_DOMAIN_SKILLS": "0",
        }
        for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMPDIR"):
            if key in parent:
                env[key] = parent[key]
        return env

    def execute(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        """Run one plan payload. Always returns a result dict; never raises."""
        runtime = self.runtime()
        if not runtime.available:
            return {"status": "ERROR", "checks": [], "errors": [runtime.reason],
                    "artifact": "", "elapsed_ms": 0, "degraded": True}

        browser = self.ensure_browser(runtime)
        if not browser.get("ok"):
            return {"status": "ERROR", "checks": [],
                    "errors": [str(browser.get("error", "browser unavailable"))],
                    "artifact": "", "elapsed_ms": 0}

        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        plan_dir = self.home / "plans"
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_path = plan_dir / f"plan-{int(time.time() * 1000)}-{os.getpid()}.json"
        try:
            plan_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:
            return {"status": "ERROR", "checks": [], "errors": [f"could not stage plan: {exc}"],
                    "artifact": "", "elapsed_ms": 0}

        script = Path(__file__).with_name("browser_exec.py").read_text(encoding="utf-8")
        env = self._child_env()
        env["TMCP_BROWSER_PLAN"] = str(plan_path)

        started = time.monotonic()
        proc = None
        try:
            proc = subprocess.Popen(  # noqa: S603 -- fixed CLI, plan is a file not an argument
                [runtime.cli], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
            )
            stdout, stderr = proc.communicate(script, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            if proc is not None:
                self._terminate(proc.pid)
                try:
                    proc.communicate(timeout=5)
                except (subprocess.SubprocessError, OSError):
                    pass
            return {"status": "TIMEOUT", "checks": [],
                    "errors": [f"plan exceeded {timeout_seconds:.0f}s and was terminated"],
                    "artifact": "", "elapsed_ms": int((time.monotonic() - started) * 1000)}
        except OSError as exc:
            return {"status": "ERROR", "checks": [], "errors": [f"harness invocation failed: {exc}"],
                    "artifact": "", "elapsed_ms": int((time.monotonic() - started) * 1000)}
        finally:
            try:
                plan_path.unlink()
            except OSError:
                pass

        result = parse_result(stdout or "")
        if result is None:
            tail = _tail(stderr or stdout or "", 400)
            return {"status": "ERROR", "checks": [],
                    "errors": [f"harness produced no result: {tail}" if tail
                               else "harness produced no result"],
                    "artifact": "", "elapsed_ms": int((time.monotonic() - started) * 1000)}
        result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
        return result


def parse_result(stdout: str) -> dict[str, Any] | None:
    """Pull the one sentinel-tagged JSON line out of harness chatter.

    The harness prints its own banners and log lines; the result is found
    by sentinel rather than by "the last line", which a stray warning
    would otherwise steal.
    """
    for line in reversed(stdout.splitlines()):
        index = line.find(RESULT_SENTINEL)
        if index >= 0:
            try:
                return json.loads(line[index + len(RESULT_SENTINEL):])
            except json.JSONDecodeError:
                return None
    return None


def _tail(text: str, limit: int) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text
