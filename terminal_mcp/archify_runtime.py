"""Safe adapter for one shared tt-a1i/archify runtime."""

from __future__ import annotations

import os
import re
import signal
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from .archify_source import DIAGRAM_TYPES


class ArchifyRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str, *, detail: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.detail = detail


def default_archify_runtime_dir() -> Path:
    override = os.environ.get("TERMINAL_MCP_ARCHIFY_HOME")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "archify-runtime"


class ArchifyRuntime:
    def __init__(
        self,
        runtime_dir: str | Path,
        *,
        node_bin: str | None = None,
        timeout: float = 120.0,
        max_output_bytes: int = 64 * 1024,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        # The caller resolves precedence once: an explicit config value wins,
        # while default_archify_runtime_dir() applies the environment override
        # when config is empty. Keeping that decision out of this low-level
        # adapter prevents a process environment variable from silently
        # replacing an explicit operator configuration.
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.cli_path = self.runtime_dir / "bin" / "archify.mjs"
        self.node_bin = node_bin or shutil.which("node")
        self.timeout = max(1.0, float(timeout))
        self.max_output_bytes = max(1024, int(max_output_bytes))
        self._runner = runner
        self._popen_factory = popen_factory

    def _text(self, value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        encoded = (value or "").encode("utf-8", errors="replace")
        if len(encoded) > self.max_output_bytes:
            encoded = encoded[:self.max_output_bytes]
            return encoded.decode("utf-8", errors="ignore") + " [truncated]"
        return encoded.decode("utf-8", errors="replace")

    def _run(self, argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        if self._runner is not None:
            return self._runner(
                list(argv), cwd=str(self.runtime_dir), text=True, capture_output=True,
                timeout=timeout, check=False, shell=False,
            )
        popen_kwargs = {
            "cwd": str(self.runtime_dir), "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE, "shell": False,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        process = self._popen_factory(list(argv), **popen_kwargs)
        captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        stop_capture = threading.Event()

        def drain(name: str, stream: Any) -> None:
            while True:
                chunk = stream.read(64 * 1024)
                if not chunk:
                    break
                if stop_capture.is_set():
                    break
                remaining = self.max_output_bytes - len(captured[name])
                if remaining > 0:
                    captured[name].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated[name] = True

        threads = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        def stop_process_group(sig: int) -> None:
            if os.name == "posix" and getattr(process, "pid", None) is not None:
                try:
                    os.killpg(process.pid, sig)
                    return
                except ProcessLookupError:
                    return
                except OSError:
                    pass
            if sig == signal.SIGKILL:
                process.kill()

        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process_group(signal.SIGKILL)
            process.wait()
            raise
        finally:
            # The renderer is a one-shot child. Kill any descendant that kept
            # an inherited pipe open after the leader exited, then bound the
            # drain joins so timeout enforcement cannot be defeated by a
            # detached/grandchild process.
            if os.name == "posix":
                stop_process_group(signal.SIGKILL)
            for thread in threads:
                thread.join(timeout=0.25)
            stop_capture.set()

        def captured_text(name: str) -> str:
            value = bytes(captured[name]).decode("utf-8", errors="replace")
            return value + (" [truncated]" if truncated[name] else "")

        return subprocess.CompletedProcess(
            list(argv), process.returncode,
            stdout=captured_text("stdout"), stderr=captured_text("stderr"),
        )

    def status(self) -> dict[str, Any]:
        base = {"runtime_dir": str(self.runtime_dir), "cli_path": str(self.cli_path)}
        if not self.cli_path.is_file():
            return {**base, "ready": False, "state": "runtime_missing",
                    "message": "Shared Archify runtime is not installed or configured."}
        if not self.node_bin:
            return {**base, "ready": False, "state": "node_missing",
                    "message": "Node.js 18 or newer is required for Archify."}
        try:
            version = self._run([self.node_bin, "--version"], timeout=min(self.timeout, 5.0))
        except (OSError, subprocess.SubprocessError) as exc:
            return {**base, "ready": False, "state": "node_missing",
                    "message": f"Node.js could not be executed: {type(exc).__name__}."}
        match = re.search(r"v?(\d+)", self._text(version.stdout))
        if version.returncode != 0 or match is None:
            return {**base, "ready": False, "state": "node_missing",
                    "message": "Node.js version could not be determined."}
        node_version = self._text(version.stdout).strip()
        if int(match.group(1)) < 18:
            return {**base, "ready": False, "state": "node_too_old", "node_version": node_version,
                    "message": "Archify requires Node.js 18 or newer."}
        try:
            doctor = self._run([self.node_bin, str(self.cli_path), "doctor"],
                               timeout=min(self.timeout, 15.0))
        except (OSError, subprocess.SubprocessError) as exc:
            return {**base, "ready": False, "state": "doctor_failed", "node_version": node_version,
                    "message": f"Archify doctor could not run: {type(exc).__name__}."}
        if doctor.returncode != 0:
            detail = self._text(doctor.stderr or doctor.stdout).strip()
            return {**base, "ready": False, "state": "doctor_failed", "node_version": node_version,
                    "message": "Archify doctor reported an unhealthy runtime.", "detail": detail}
        return {**base, "ready": True, "state": "ready", "node_version": node_version,
                "message": "Shared Archify runtime is ready."}

    def render(self, diagram_type: str, input_path: str | Path, output_path: str | Path,
               *, repo_root: str | Path | None = None) -> dict[str, Any]:
        if diagram_type not in DIAGRAM_TYPES:
            raise ArchifyRuntimeError("INVALID_DIAGRAM_TYPE", f"Unsupported diagram type: {diagram_type}")
        dependency = self.status()
        if not dependency["ready"]:
            raise ArchifyRuntimeError("ARCHIFY_UNAVAILABLE", dependency["message"],
                                      detail=dependency.get("detail", ""))
        source = Path(input_path).resolve()
        output = Path(output_path).resolve()
        argv = [self.node_bin or "node", str(self.cli_path), "render", diagram_type,
                str(source), str(output), "--quality", "standard"]
        if diagram_type == "architecture" and repo_root is not None:
            argv.extend(["--repo-root", str(Path(repo_root).resolve())])
        try:
            result = self._run(argv, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise ArchifyRuntimeError("GENERATION_TIMEOUT", "Archify generation timed out.") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise ArchifyRuntimeError("GENERATION_FAILED",
                                      f"Archify could not start: {type(exc).__name__}.") from exc
        stdout = self._text(result.stdout).strip()
        stderr = self._text(result.stderr).strip()
        if result.returncode != 0:
            raise ArchifyRuntimeError("GENERATION_FAILED",
                                      f"Archify exited with code {result.returncode}.",
                                      detail=stderr or stdout)
        if not output.is_file():
            raise ArchifyRuntimeError("ARTIFACT_MISSING", "Archify did not produce the HTML artifact.")
        return {"returncode": result.returncode, "stdout": stdout, "stderr": stderr,
                "output_bytes": output.stat().st_size, "command": argv}
