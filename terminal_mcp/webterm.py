"""Web terminal: attach a real browser (xterm.js) directly to an existing
tmux session over a WebSocket, via a PTY-wrapped `tmux attach-session`.

This is deliberately a NEW, narrow data-plane primitive, not a rework of
TmuxClient (tmux.py): every existing tmux call in this project is a
request/response subprocess.run (fire a command, read its output, exit)
-- fine for control-plane operations (capture-pane, send-keys, list-
sessions), completely wrong shape for a live interactive terminal, whose
job is streaming a long-lived process's own stdin/stdout. What IS shared
with the rest of the project: the tmux *binary path* (config-driven,
passed in by the caller -- see TmuxClient.binary), the session-name
validation (permissions.valid_session_name, checked by the caller before
ever constructing a WebTerminalProcess), and the fact that the ONLY tmux
subcommand this module is capable of invoking is `attach-session` --
never anything caller/browser-supplied. There is no separate "spawn
arbitrary command" path anywhere in this file.

Security posture:
  - `session` must already be validated and confirmed to name a currently
    EXISTING tmux session by the caller (TerminalService.
    terminal_web_terminal_access) -- this module never creates a session
    and never guesses at one; a request for a session that doesn't exist
    must fail before a WebTerminalProcess is ever constructed.
  - The only flags this class can ever pass to `tmux attach-session` are
    `-r` (read-only client) and `-d` (detach other clients) -- both
    booleans decided entirely by the caller from server-side authorization
    state (TerminalService._read_authorized/_input_authorized), never from
    a raw browser-supplied string.
  - `readonly=True` is enforced TWICE, independently: tmux's own `-r` flag
    (tmux itself refuses to forward keystrokes from a read-only client to
    the pane) AND this class's own `write()` becoming a silent no-op. A
    bug in one layer does not remove the other.
  - Closing this process (browser tab closed, WebSocket dropped) is
    exactly equivalent to any other tmux client disconnecting: tmux
    detaches that one client. The pane/process/session this attach was
    watching is completely unaffected and keeps running -- this class
    never calls kill-session, never touches any client but its own.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import pty
import select
import struct
import subprocess
import termios
from typing import Any

import anyio
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

_log = logging.getLogger(__name__)

# Bounds for client-requested resize -- generous enough for any real
# terminal (including a large desktop monitor), never so large that a
# hostile/buggy client can make this process allocate/ioctl something
# absurd. Purely a sanity clamp; tmux itself would reject an insane value
# anyway, this just avoids ever asking it to.
MAX_RESIZE_COLS = 500
MAX_RESIZE_ROWS = 200
READ_CHUNK_BYTES = 65536
# Bounds a single client->server input frame -- normal typed/pasted input
# is nowhere near this; this is only a backstop against a misbehaving or
# hostile client trying to push an unbounded write per message.
MAX_INPUT_FRAME_BYTES = 65536
PTY_READ_POLL_SECONDS = 1.0


class WebTerminalProcess:
    """One PTY-backed `tmux attach-session -t <name>` client process."""

    def __init__(self, tmux_binary: str, session: str, *, readonly: bool, takeover: bool) -> None:
        self.session = session
        self.readonly = readonly
        master_fd, slave_fd = pty.openpty()
        args = [tmux_binary, "attach-session", "-t", session]
        if takeover:
            args.append("-d")
        if readonly:
            args.append("-r")
        try:
            self._proc = subprocess.Popen(
                args, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True, close_fds=True,
            )
        finally:
            # The child has its own dup of the slave fd now (or the Popen
            # call itself failed, in which case there is nothing left to
            # close it for) -- this process's own copy must be closed
            # either way, or the child's stdin/stdout would never see EOF
            # when this end of the pty is later closed.
            os.close(slave_fd)
        self.master_fd = master_fd
        self._closed = False

    def resize(self, cols: object, rows: object) -> None:
        if not isinstance(cols, int) or not isinstance(rows, int) or isinstance(cols, bool) or isinstance(rows, bool):
            return
        cols = max(1, min(MAX_RESIZE_COLS, cols))
        rows = max(1, min(MAX_RESIZE_ROWS, rows))
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def write(self, data: bytes) -> None:
        # readonly is the second, independent enforcement layer described
        # in this module's docstring -- tmux's own `-r` flag is the first;
        # this never forwards a byte regardless of what tmux would do.
        if self.readonly or self._closed:
            return
        with contextlib.suppress(OSError):
            os.write(self.master_fd, data[:MAX_INPUT_FRAME_BYTES])

    def read(self, timeout: float = PTY_READ_POLL_SECONDS) -> bytes | None:
        """Blocking (bounded by `timeout`) read of whatever the pty has
        buffered right now. Returns b"" on EOF (the attach client process
        exited -- e.g. the session itself was killed out of band), None
        on a plain timeout (nothing to read yet; caller should loop and
        re-check liveness/cancellation)."""
        if self._closed:
            return b""
        try:
            ready, _, _ = select.select([self.master_fd], [], [], timeout)
        except OSError:
            return b""
        if not ready:
            return None
        try:
            return os.read(self.master_fd, READ_CHUNK_BYTES)
        except OSError:
            return b""

    def alive(self) -> bool:
        return not self._closed and self._proc.poll() is None

    def close(self) -> None:
        """Terminates ONLY this attach client process. tmux treats that
        exactly like any other client disconnecting (SIGHUP on its pty) --
        it detaches this one client. The session/pane/underlying program
        this was attached to is completely unaffected and keeps running;
        this never calls kill-session and never touches any other client
        that may also be attached."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._proc.terminate()
        with contextlib.suppress(Exception):
            self._proc.wait(timeout=2)
        with contextlib.suppress(Exception):
            self._proc.kill()
        with contextlib.suppress(OSError):
            os.close(self.master_fd)


async def pump_websocket(websocket: WebSocket, proc: Any) -> None:
    """Bidirectional bridge between an already-`accept()`ed WebSocket and
    an already-spawned WebTerminalProcess -- or any OTHER object with the
    same shape (`read(timeout) -> bytes|None`, `write(bytes)`,
    `resize(cols, rows)`, `alive() -> bool`, `close()`) -- this function
    itself never constructs or type-checks `proc`, only calls those five
    methods, so a non-tmux backend's own equivalent (windows_webterm.py's
    `WindowsTerminalViewer`, used by node_agent.py's remote-node WS
    route) reuses this exact pump unmodified. `proc` is typed `Any`
    rather than `WebTerminalProcess` specifically for this reason -- the
    real contract is this docstring's method list, not the class name.
    Returns once either side has closed; the caller is responsible for
    `proc.close()` afterward (this function never assumes it owns the
    process's lifetime beyond the pump itself, so a caller can inspect/
    log state after this returns).

    Wire protocol, deliberately minimal:
      server -> client: BINARY frames are raw pty output bytes (exactly
        what a real terminal emulator would receive); TEXT frames are
        small JSON control messages (`{"type": "ready", ...}` once, right
        after accept -- sent by the caller, not this function --  and
        `{"type": "closed", ...}` right before this function closes the
        socket itself).
      client -> server: BINARY frames are raw keystroke bytes, written to
        the pty verbatim (a no-op if the process is read-only -- see
        WebTerminalProcess.write); TEXT frames are JSON control messages,
        currently only `{"type": "resize", "cols": N, "rows": N}`. Any
        other/malformed text frame is silently ignored rather than
        tearing down the connection -- a forward-compatible client
        sending a message type this server doesn't understand yet should
        never be treated as an error.
    """
    async def _from_pty() -> None:
        while True:
            chunk = await anyio.to_thread.run_sync(proc.read, PTY_READ_POLL_SECONDS)
            if chunk is None:
                if not proc.alive():
                    break
                continue
            if chunk == b"":
                break
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            try:
                await websocket.send_bytes(chunk)
            except Exception:
                break
        with contextlib.suppress(Exception):
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json({"type": "closed", "reason": "session_client_exited"})

    async def _from_ws() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                proc.write(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            _handle_control_message(proc, text)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_from_pty)
        try:
            await _from_ws()
        except WebSocketDisconnect:
            pass
        finally:
            tg.cancel_scope.cancel()


def _handle_control_message(proc: WebTerminalProcess, text: str) -> None:
    try:
        payload: Any = json.loads(text)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("type") == "resize":
        proc.resize(payload.get("cols"), payload.get("rows"))
    # Every other/unknown message type is silently ignored -- see
    # pump_websocket's own docstring for why.
