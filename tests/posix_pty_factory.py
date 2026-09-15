"""A real POSIX-pty process factory, for running the session HOST on Linux.

terminal_mcp.windows_session_host's production factory is pywinpty, which only
exists on Windows. This module is what TERMINAL_MCP_SESSION_PTY_FACTORY points
at so the host's own entry point can be spawned, killed, orphaned and re-adopted
for real in this project's Linux CI -- the same reasoning (and the same
PtyProcessLike shape) as tests/test_windows_backend.py's `_FakePty`.

It is a faithful stand-in for the pty, not a stand-in for the logic under test:
the process is real, the pty is real, and the host code exercised against it is
the production code path.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import subprocess


class PosixPty:
    def __init__(self, argv: list[str], cwd: str) -> None:
        master_fd, slave_fd = pty.openpty()
        self._master_fd = master_fd
        self._proc = subprocess.Popen(argv, cwd=cwd, stdin=slave_fd, stdout=slave_fd,
                                      stderr=slave_fd, start_new_session=True,
                                      close_fds=True)
        os.close(slave_fd)
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    @property
    def pid(self) -> int:
        return self._proc.pid

    def isalive(self) -> bool:
        return self._proc.poll() is None

    def read(self, size: int = 4096) -> str:
        ready, _, _ = select.select([self._master_fd], [], [], 0.2)
        if not ready:
            return ""
        try:
            return os.read(self._master_fd, size).decode("utf-8", errors="replace")
        except OSError:
            return ""

    def write(self, data: str) -> int:
        return os.write(self._master_fd, data.encode("utf-8"))

    def setwinsize(self, rows: int, cols: int) -> None:
        import struct
        import termios

        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def terminate(self, force: bool = False) -> None:
        try:
            os.killpg(os.getpgid(self._proc.pid),
                      signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self._proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
        try:
            os.close(self._master_fd)
        except OSError:
            pass


def factory(argv: list[str], cwd: str) -> PosixPty:
    return PosixPty(argv, cwd)
