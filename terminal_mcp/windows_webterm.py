"""Live WebSocket viewer for a WindowsSessionBackend session -- the
Windows-side analogue of webterm.py's `WebTerminalProcess`, implementing
the EXACT SAME shape (`read(timeout) -> bytes|None`, `write(bytes)`,
`resize(cols, rows)`, `alive()`, `close()`) so webterm.py's own
`pump_websocket` (the actual bidirectional WS<->pty bridge, and its wire
protocol) is reused completely UNCHANGED for both backends -- never a
second, parallel pump implementation.

The one real architectural difference from `WebTerminalProcess`, and the
whole reason this class exists instead of just reusing that one directly:
tmux's own `attach-session` IS a new, disposable OS process each time (a
"client" in tmux's own model) -- WindowsSessionBackend has no such native
per-viewer client concept (see windows_backend.py's own module docstring
for why). This class is instead a thin VIEW onto the backend's already-
running, shared reader loop: `register_viewer`/`unregister_viewer`
attach/detach without spawning or killing anything, and `close()` never
touches the underlying process -- exactly the "disconnect browser không
kill process" guarantee, expressed through this class specifically.
"""
from __future__ import annotations

import queue

from .windows_backend import WindowsSessionBackend

MAX_INPUT_FRAME_BYTES = 65536  # same ceiling webterm.py's own WebTerminalProcess.write already applies


class WindowsTerminalViewer:
    def __init__(self, backend: WindowsSessionBackend, session: str, *, readonly: bool) -> None:
        self.backend = backend
        self.session = session
        self.readonly = readonly
        self._closed = False
        self._on_detach_called = False

        def _on_detach() -> None:
            # Wired to backend.detach_session(session) -- when that's
            # called (e.g. the dashboard's own "Detach" action, or a
            # takeover), this viewer's read() loop must stop waiting on
            # a queue nothing will ever post to again.
            self._closed = True

        self._on_detach = _on_detach
        self._queue = backend.register_viewer(session, _on_detach)

    def resize(self, cols: object, rows: object) -> None:
        if not isinstance(cols, int) or not isinstance(rows, int) or isinstance(cols, bool) or isinstance(rows, bool):
            return
        try:
            self.backend.resize(self.session, max(1, rows), max(1, cols))
        except Exception:  # noqa: BLE001 -- a resize failure is never fatal to the connection
            pass

    def write(self, data: bytes) -> None:
        if self.readonly or self._closed:
            return
        try:
            self.backend.write_raw(self.session, data[:MAX_INPUT_FRAME_BYTES].decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 -- a write failure here must not crash the WS pump
            pass

    def read(self, timeout: float = 0.5) -> bytes | None:
        if self._closed:
            return b""
        try:
            chunk = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if chunk == "":
            self._closed = True
            return b""
        return chunk.encode("utf-8", errors="replace")

    def alive(self) -> bool:
        if self._closed:
            return False
        info = self.backend.get_session(self.session)
        return info is not None and not info.pane_dead

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.backend.unregister_viewer(self.session, self._on_detach, self._queue)
        except Exception:  # noqa: BLE001 -- closing must never raise
            pass
