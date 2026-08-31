from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionInfo:
    name: str
    attached: bool
    windows: int
    created_epoch: int
    activity_epoch: int
    pane_pid: int
    pane_current_command: str
    pane_dead: bool

