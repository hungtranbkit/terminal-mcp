from __future__ import annotations

import re
import time

from .models import SessionInfo


WAIT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"do you want to continue",
        r"what should claude do instead",
        r"press enter",
        r"enter password",
        r"password\s*:\s*$",
        r"\[y/n\]",
        r"\[Y/n\]",
        r"continue\?\s*$",
        r"\bapprove\b",
        r"\bpermission\b",
        r"waiting for input",
    )
)
ACTIVE_COMMANDS = {"claude", "codex", "python", "python3", "pytest", "node", "npm", "bash", "zsh"}


def detect_waiting_input(output: str) -> tuple[bool, str]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    recent = lines[-12:]
    # A prompt must be very near the current pane bottom to avoid matching old logs.
    for offset, line in enumerate(reversed(recent[-4:])):
        for pattern in WAIT_PATTERNS:
            if pattern.search(line):
                return True, f"recent prompt matched {pattern.pattern!r} at bottom offset {offset}"
    return False, "no high-confidence input prompt in the last four non-empty lines"


def classify_status(session: SessionInfo, output: str, now: int | None = None) -> tuple[str, bool, str]:
    waiting, reason = detect_waiting_input(output)
    if waiting:
        return "WAITING_INPUT", True, reason
    if session.pane_dead:
        return "IDLE", False, "tmux reports the active pane is dead"
    age = max(0, (now if now is not None else int(time.time())) - session.activity_epoch)
    command = session.pane_current_command.casefold()
    if command in ACTIVE_COMMANDS and age <= 60:
        return "RUNNING", False, f"current command is {command!r}; tmux activity age is {age}s"
    if command in {"bash", "zsh", "sh", "fish"} and age > 60:
        return "IDLE", False, f"shell pane has no tmux activity for {age}s"
    return "UNKNOWN", False, f"current command is {command or 'unknown'!r}; activity age is {age}s; {reason}"

