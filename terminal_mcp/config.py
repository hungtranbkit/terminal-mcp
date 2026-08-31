from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PermissionsConfig:
    terminal_read: bool = True
    terminal_input: bool = False


@dataclass(frozen=True)
class AppConfig:
    permissions: PermissionsConfig
    allowed_session_patterns: tuple[str, ...]
    max_capture_lines: int = 2000
    default_tail_lines: int = 200


DEFAULT_PATTERNS = ("claude-*", "codex-*", "agent-*", "test-*")


def default_config_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "config.yaml"


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else default_config_path()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    permissions = raw.get("permissions", {})
    patterns = raw.get("allowed_session_patterns", list(DEFAULT_PATTERNS))
    max_lines = int(raw.get("max_capture_lines", 2000))
    tail_lines = int(raw.get("default_tail_lines", 200))

    if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
        raise ValueError("allowed_session_patterns must be a non-empty list of strings")
    if not 1 <= max_lines <= 100_000:
        raise ValueError("max_capture_lines must be between 1 and 100000")
    if not 1 <= tail_lines <= max_lines:
        raise ValueError("default_tail_lines must be between 1 and max_capture_lines")

    return AppConfig(
        permissions=PermissionsConfig(
            terminal_read=bool(permissions.get("terminal_read", True)),
            terminal_input=bool(permissions.get("terminal_input", False)),
        ),
        allowed_session_patterns=tuple(patterns),
        max_capture_lines=max_lines,
        default_tail_lines=tail_lines,
    )

