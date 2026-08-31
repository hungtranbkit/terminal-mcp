from __future__ import annotations

import subprocess
import time

import pytest

from terminal_mcp.config import AppConfig, PermissionsConfig


@pytest.fixture
def read_config() -> AppConfig:
    return AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*"), 50, 20)


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", *args], check=check, capture_output=True, text=True, timeout=10)


@pytest.fixture
def tmux_session_factory():
    created: list[str] = []

    def create(name: str, command: str = "bash") -> str:
        tmux("kill-session", "-t", name, check=False)
        tmux("new-session", "-d", "-s", name, command)
        created.append(name)
        time.sleep(0.15)
        return name

    yield create
    for name in created:
        tmux("kill-session", "-t", name, check=False)

