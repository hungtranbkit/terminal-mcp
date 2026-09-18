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
    path = _write_config(tmp_path, {"queue": {
        "enabled": True,
        "poll_interval_seconds": 2,
        "drain_enabled": True,
        "drain_batch_size": 17,
    }})
    config = load_config(path)
    assert config.queue.enabled is True
    assert config.queue.poll_interval_seconds == 2
    assert config.queue.drain_enabled is True
    assert config.queue.drain_batch_size == 17


def test_queue_config_rejects_too_small_a_poll_interval(tmp_path):
    path = _write_config(tmp_path, {"queue": {"poll_interval_seconds": 0.1}})
    with pytest.raises(ValueError, match="poll_interval_seconds"):
        load_config(path)


def test_queue_config_rejects_invalid_drain_values(tmp_path):
    path = _write_config(tmp_path, {"queue": {"drain_enabled": "yes"}})
    with pytest.raises(ValueError, match="drain_enabled"):
        load_config(path)

    path = _write_config(tmp_path, {"queue": {"drain_batch_size": 0}})
    with pytest.raises(ValueError, match="drain_batch_size"):
        load_config(path)


def test_queue_config_loads_project_dispatch_rule(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write_config(tmp_path, {"queue": {
        "enabled": True,
        "drain_enabled": True,
        "project_dispatch_rules": [{
            "name": "novaretail",
            "session_patterns": ["linux-codex-work"],
            "repo_root": str(repo),
            "planner_path": "tools/orchestration/continuous_dispatch.py",
            "event_types": ["TASK_COMPLETED"],
            "timeout_seconds": 8,
        }],
    }})
    config = load_config(path)
    assert len(config.queue.project_dispatch_rules) == 1
    rule = config.queue.project_dispatch_rules[0]
    assert rule.name == "novaretail"
    assert rule.session_patterns == ("linux-codex-work",)
    assert rule.repo_root == str(repo)
    assert rule.timeout_seconds == 8


def test_queue_config_rejects_escaping_project_planner(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = _write_config(tmp_path, {"queue": {
        "project_dispatch_rules": [{
            "name": "bad",
            "session_patterns": ["*-work"],
            "repo_root": str(repo),
            "planner_path": "../outside.py",
        }],
    }})
    with pytest.raises(ValueError, match="planner_path"):
        load_config(path)
