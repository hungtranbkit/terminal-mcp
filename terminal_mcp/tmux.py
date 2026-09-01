from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone

from .models import SessionInfo


class TmuxError(RuntimeError):
    pass


SEND_TEXT_ENTER_SETTLE_SECONDS = 0.08
"""Delay between the literal-text send-keys call and the Enter send-keys
call in `send_text`. `tmux send-keys` writes bytes to the pane's pty and
returns almost instantly -- it does not wait for the *receiving* program to
finish processing them. Two send-keys calls fired back-to-back with zero
gap can therefore land an Enter keystroke while an interactive TUI's own
async input handling (redraw batching, bracketed-paste detection, a
debounced line editor -- exactly what Codex/Claude-style CLIs use) is still
mid-cycle from the preceding text; some of those debounce/redraw windows
swallow an Enter that arrives too soon rather than queuing it, producing
"the text is fully typed but nothing executes until a human presses Enter
[again]". This fixed, small settle window gives the target process time to
finish consuming the text before Enter is sent as a genuinely separate,
later keystroke. See tests/fixtures/laggy_line_reader.py for a real,
disposable-tmux-pane reproduction of this exact race, and
TerminalService._send_text_and_verify (core.py) for the best-effort
post-send confirmation layered on top of this."""


class TmuxClient:
    SESSION_FORMAT = "|".join(
        (
            "#{session_name}",
            "#{session_attached}",
            "#{session_windows}",
            "#{session_created}",
            "#{session_activity}",
            "#{pane_pid}",
            "#{pane_current_command}",
            "#{pane_dead}",
            "#{session_id}",  # tmux's own "$N" id -- P0-2 identity pinning
            "#{pane_id}",     # tmux's own "%N" id -- P0-2 identity pinning
            "#{pane_in_mode}",  # copy-mode/other tmux client mode -- P0 audit finding #14
        )
    )

    def __init__(self, binary: str = "tmux") -> None:
        self.binary = binary

    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                [self.binary, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TmuxError(f"tmux invocation failed: {type(exc).__name__}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or "tmux command failed"
            raise TmuxError(detail)
        return result

    def list_sessions(self) -> list[SessionInfo]:
        result = self._run(["list-sessions", "-F", self.SESSION_FORMAT], check=False)
        if result.returncode != 0:
            if "no server running" in result.stderr or "failed to connect" in result.stderr:
                return []
            raise TmuxError(result.stderr.strip() or "unable to list sessions")
        sessions: list[SessionInfo] = []
        for line in result.stdout.splitlines():
            if line.strip():
                sessions.append(parse_session_line(line))
        return sessions

    def get_session(self, name: str) -> SessionInfo | None:
        return next((item for item in self.list_sessions() if item.name == name), None)

    def capture_lines(self, session: str, lines: int, *, ansi: bool = False) -> list[str]:
        """Return at most `lines` of the most recent real content from the pane.

        tmux's `-S`/`-E` addressing is relative to the visible pane, not a
        literal "last N lines" window: with no `-E`, `-S -{lines}` always
        captures through the bottom of the *current visible pane* on top of
        the requested history offset, so raw output can hold far more than
        `lines` rows (and, when the pane is taller than the real output,
        blank padding rows below the last real line). `rstrip` drops that
        trailing blank padding; the final slice then deterministically bounds
        the result to the requested count so callers get exactly the N most
        recent real lines — or fewer, if the session hasn't produced that
        much output yet — regardless of pane geometry.

        `ansi=True` adds tmux's `-e`, which includes the SGR (colour/style)
        escape sequences tmux's own terminal emulation has already resolved
        for each cell — not raw, unprocessed program output, so this never
        introduces cursor-movement or other control sequences, only `ESC [
        ... m` runs. Defaults to False so every existing caller (terminal_tail,
        terminal_capture, terminal_status) is completely unaffected; only the
        dashboard's terminal-style renderer opts in.
        """
        lines = max(1, lines)
        args = ["capture-pane", "-p", "-J"]
        if ansi:
            args.append("-e")
        args += ["-S", f"-{lines}", "-t", session]
        result = self._run(args)
        captured = result.stdout.rstrip("\n").splitlines()
        return captured[-lines:]

    def send_text(self, session: str, text: str, press_enter: bool) -> None:
        self._run(["send-keys", "-t", session, "-l", "--", text])
        if press_enter:
            # See SEND_TEXT_ENTER_SETTLE_SECONDS above -- this is the fix
            # for the intermittent "typed but not submitted" race, not an
            # incidental pause: never remove it without also removing the
            # reason it exists.
            time.sleep(SEND_TEXT_ENTER_SETTLE_SECONDS)
            self._run(["send-keys", "-t", session, "Enter"])

    def send_keys(self, session: str, keys: list[str]) -> None:
        for key in keys:
            self._run(["send-keys", "-t", session, key])


def parse_session_line(line: str) -> SessionInfo:
    parts = line.split("|", 10)
    if len(parts) != 11:
        raise TmuxError("unexpected tmux session format")
    (name, attached, windows, created, activity, pane_pid, command, pane_dead,
     session_id, pane_id, pane_in_mode) = parts
    try:
        return SessionInfo(
            name=name,
            attached=bool(int(attached)),
            windows=int(windows),
            created_epoch=int(created),
            activity_epoch=int(activity),
            pane_pid=int(pane_pid),
            pane_current_command=command,
            pane_dead=bool(int(pane_dead)),
            session_id=session_id,
            pane_id=pane_id,
            pane_in_mode=bool(int(pane_in_mode)),
        )
    except ValueError as exc:
        raise TmuxError("invalid numeric field from tmux") from exc


def iso_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

