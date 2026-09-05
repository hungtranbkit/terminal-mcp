"""SessionBackend -- the platform abstraction multi-node Windows support
(task: "Không giả định mọi node đều có tmux") is built on.

`TerminalService` (core.py) has always talked to ONE object it calls
`self.tmux` for every actual session operation (list/get/capture/send/
create/detach/kill/exit_copy_mode) -- every permission check, audit
record, redaction, kill/reopen-metadata capture, and the whole reliable-
submission verification state machine (adapters.py) already live in
TerminalService itself, entirely in terms of that narrow, already-
generic-shaped surface (session names, plain-text capture lines, PIDs,
opaque identity strings) -- none of it actually depends on tmux
specifically. This module makes that implicit contract explicit as a
`Protocol`, so TerminalService can be constructed with ANY object
satisfying it, not only a `TmuxClient`.

`TmuxClient` (tmux.py) is NOT modified to implement this -- Python
Protocols are structural (duck-typed): TmuxClient already has every
method this Protocol names, with matching signatures, so it satisfies
`SessionBackend` automatically. This is deliberate: the Linux path stays
byte-for-byte the same code it always was (task: "Linux backend giữ tmux
như hiện tại"), and gets this abstraction for free, at zero risk to the
existing, live, tested behavior.

A Windows node (windows_backend.py) implements this SAME Protocol over a
ConPTY-backed persistent PowerShell/cmd process supervisor instead of
tmux, and `TerminalService` runs completely UNCHANGED on top of it --
every permission/grant/audit/reopen-metadata/reliable-submission
behavior a Linux node already has, a Windows node gets automatically,
with no duplicated business logic.

See `windows_backend.py` for the Windows implementation and its own
documented limitations (no separate persistent server process the way
tmux has one -- see that module's docstring), and `docs/multi-node.md`
for the full architecture.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import SessionInfo
from .tmux import TmuxError

__all__ = ["SessionBackend", "SessionBackendError"]

# Re-exported under a platform-neutral name -- see SessionBackendError's
# own docstring below for why this is a plain alias, not a new class.
SessionBackendError = TmuxError


@runtime_checkable
class SessionBackend(Protocol):
    """The exact method surface `TerminalService` calls on `self.tmux`.
    Matches `TmuxClient` (tmux.py) precisely -- see that class for the
    authoritative behavioral contract of each method (this Protocol only
    fixes the shape; the docstrings on TmuxClient's own methods, and on
    WindowsSessionBackend's, are the real spec each implementation must
    honor: idempotency, error types, what "attached" means, etc)."""

    def list_sessions(self) -> list[SessionInfo]: ...

    def get_session(self, name: str) -> SessionInfo | None: ...

    def capture_lines(self, session: str, lines: int, *, ansi: bool = False) -> list[str]: ...

    def send_text(self, session: str, text: str, press_enter: bool) -> None: ...

    def send_keys(self, session: str, keys: list[str]) -> None: ...

    def new_session(self, name: str, cwd: str, command: str | None = None, *,
                    show_on_desktop: bool = False) -> tuple[bool, str | None] | None: ...

    def detach_session(self, name: str) -> None: ...

    def kill_session(self, name: str) -> None: ...

    def exit_copy_mode(self, session: str) -> None: ...


# SessionBackendError = tmux.TmuxError (aliased above), deliberately NOT a
# new exception class. TerminalService (core.py) catches `TmuxError` by
# name in ~28 places -- every one of them is how a real backend failure
# (process spawn failed, pty broke, session vanished mid-call) already
# turns into a clean {"error": "TMUX_ERROR", ...} response instead of an
# unhandled exception. Reusing that exact class for WindowsSessionBackend
# too means EVERY one of those 28 call sites keeps working, unmodified,
# for a Windows-backed TerminalService -- the alternative (a new,
# separate exception type) would need every one of them touched to add a
# second `except` clause, for no behavioral benefit. The name "TmuxError"
# is a small, deliberate wart for a non-tmux backend to raise; importing
# it here under a neutral alias (`SessionBackendError`) is the fix for
# that wart everywhere except the one place (core.py's existing `except
# TmuxError` clauses) where changing it isn't worth the risk.
