import json

from terminal_mcp.prompt_start_watcher import PromptStartWatcher, load_watcher_config


def test_config_defaults_and_bounds(tmp_path):
    path = tmp_path / "watcher.json"
    path.write_text(json.dumps({"max_enters": 99, "interval_seconds": 1,
                                "include_sessions": ["codex1"]}))
    cfg = load_watcher_config(path)
    assert cfg["max_enters"] == 6
    assert cfg["interval_seconds"] == 3
    assert cfg["include_sessions"] == ["codex1"]


def test_disabled_cycle_is_read_only(tmp_path):
    config = tmp_path / "watcher.json"
    config.write_text(json.dumps({"enabled": False}))
    state = tmp_path / "state.json"
    result = PromptStartWatcher(config_path=config, state_path=state).run_once()
    assert result["enabled"] is False
    assert result["tracked_count"] == 0
    assert json.loads(state.read_text())["enabled"] is False
