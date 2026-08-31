from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from .models import SessionInfo


class TmuxError(RuntimeError):
    pass


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

    def capture_lines(self, session: str, lines: int) -> list[str]:
        lines = max(1, lines)
        result = self._run(["capture-pane", "-p", "-J", "-S", f"-{lines}", "-t", session])
        return result.stdout.rstrip("\n").splitlines()

    def send_text(self, session: str, text: str, press_enter: bool) -> None:
        self._run(["send-keys", "-t", session, "-l", "--", text])
        if press_enter:
            self._run(["send-keys", "-t", session, "Enter"])

    def send_keys(self, session: str, keys: list[str]) -> None:
        for key in keys:
            self._run(["send-keys", "-t", session, key])


def parse_session_line(line: str) -> SessionInfo:
    parts = line.split("|", 7)
    if len(parts) != 8:
        raise TmuxError("unexpected tmux session format")
    name, attached, windows, created, activity, pane_pid, command, pane_dead = parts
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
        )
    except ValueError as exc:
        raise TmuxError("invalid numeric field from tmux") from exc


def iso_timestamp(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

