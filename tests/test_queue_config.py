"""QueueConfig (config.py) -- the AUTO-DISPATCH background loop's own
GLOBAL kill switch (task: production-readiness pass on Supervisor Queue
v2). Disabled by default; backward-compatible with a config.yaml that
has no `queue:` section at all (every config.yaml this project shipped
before this feature)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from terminal_mcp.config import QueueConfig, load_config


def _write_config(tmp_path: Path, extra: dict) -> Path:
    base = {
        "permissions": {"terminal_read": True, "terminal_input": False},
        "allowed_session_patterns": ["test-*"],
    }
    base.update(extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(base))
    return path


def test_queue_config_defaults_when_absent(tmp_path):
    path = _write_config(tmp_path, {})
    config = load_config(path)
    assert config.queue == QueueConfig()
    assert config.queue.enabled is False


def test_queue_config_can_be_explicitly_enabled(tmp_path):
    path = _write_config(tmp_path, {"queue": {"enabled": True, "poll_interval_seconds": 2}})
    config = load_config(path)
    assert config.queue.enabled is True
    assert config.queue.poll_interval_seconds == 2


def test_queue_config_rejects_too_small_a_poll_interval(tmp_path):
    path = _write_config(tmp_path, {"queue": {"poll_interval_seconds": 0.1}})
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        load_config(path)
