"""config.py's `nodes:` section -- overload/heartbeat threshold overrides
and operator-declared remote nodes (task item 15's config-driven half of
remote registration). Backward compatibility: a config.yaml with no
`nodes:` section at all (every deployment before this feature, including
the real production config.yaml) must load with exactly the built-in
defaults, unchanged."""
from __future__ import annotations

import pytest
import yaml

from terminal_mcp.config import RemoteNodeConfig, load_config
from terminal_mcp.node_models import NodeHeartbeatThresholds, OverloadThresholds


def _write(tmp_path, raw: dict):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _base_raw(**nodes_overrides) -> dict:
    raw = {"permissions": {"terminal_read": True}, "allowed_session_patterns": ["test-*"]}
    if nodes_overrides:
        raw["nodes"] = nodes_overrides
    return raw


def test_missing_nodes_section_is_pure_defaults(tmp_path):
    config = load_config(_write(tmp_path, _base_raw()))
    assert config.nodes.overload_thresholds == OverloadThresholds()
    assert config.nodes.heartbeat_thresholds == NodeHeartbeatThresholds()
    assert config.nodes.remote_nodes == ()


def test_overload_thresholds_partial_override_keeps_other_defaults(tmp_path):
    config = load_config(_write(tmp_path, _base_raw(overload_thresholds={"ram_busy_percent": 70.0})))
    assert config.nodes.overload_thresholds.ram_busy_percent == 70.0
    assert config.nodes.overload_thresholds.cpu_busy_percent == OverloadThresholds().cpu_busy_percent


def test_smoothing_alpha_out_of_range_rejected(tmp_path):
    with pytest.raises(ValueError, match="smoothing_alpha"):
        load_config(_write(tmp_path, _base_raw(overload_thresholds={"smoothing_alpha": 0.0})))
    with pytest.raises(ValueError, match="smoothing_alpha"):
        load_config(_write(tmp_path, _base_raw(overload_thresholds={"smoothing_alpha": 1.5})))


def test_heartbeat_thresholds_override(tmp_path):
    config = load_config(_write(tmp_path, _base_raw(heartbeat={"degraded_after_seconds": 30, "offline_after_seconds": 90})))
    assert config.nodes.heartbeat_thresholds.degraded_after_seconds == 30.0
    assert config.nodes.heartbeat_thresholds.offline_after_seconds == 90.0


def test_heartbeat_offline_must_be_at_least_degraded(tmp_path):
    with pytest.raises(ValueError, match="offline_after_seconds"):
        load_config(_write(tmp_path, _base_raw(heartbeat={"degraded_after_seconds": 100, "offline_after_seconds": 50})))


def test_remote_node_full_declaration_parsed(tmp_path):
    config = load_config(_write(tmp_path, _base_raw(remote=[{
        "node_id": "m910", "display_name": "M910 Workstation", "hostname": "m910.local",
        "endpoint": "http://192.168.1.50:8790", "token_env": "TERMINAL_MCP_NODE_TOKEN_M910",
        "max_sessions": 20, "timeout_seconds": 5.0,
    }])))
    assert config.nodes.remote_nodes == (
        RemoteNodeConfig(node_id="m910", display_name="M910 Workstation", hostname="m910.local",
                         endpoint="http://192.168.1.50:8790", token_env="TERMINAL_MCP_NODE_TOKEN_M910",
                         max_sessions=20, timeout_seconds=5.0),
    )


def test_remote_node_minimal_declaration_defaults_display_name_and_hostname_to_node_id(tmp_path):
    config = load_config(_write(tmp_path, _base_raw(remote=[{
        "node_id": "m910", "endpoint": "http://192.168.1.50:8790", "token_env": "TERMINAL_MCP_NODE_TOKEN_M910",
    }])))
    node = config.nodes.remote_nodes[0]
    assert node.display_name == "m910"
    assert node.hostname == "m910"
    assert node.max_sessions is None
    assert node.timeout_seconds == 10.0


def test_remote_node_missing_required_field_rejected(tmp_path):
    for missing in ("node_id", "endpoint", "token_env"):
        entry = {"node_id": "m910", "endpoint": "http://x:8790", "token_env": "TERMINAL_MCP_NODE_TOKEN_M910"}
        del entry[missing]
        with pytest.raises(ValueError, match=missing):
            load_config(_write(tmp_path, _base_raw(remote=[entry])))


def test_remote_node_id_local_is_reserved(tmp_path):
    with pytest.raises(ValueError, match="reserved"):
        load_config(_write(tmp_path, _base_raw(remote=[{
            "node_id": "local", "endpoint": "http://x:8790", "token_env": "TOKEN",
        }])))


def test_remote_node_duplicate_id_rejected(tmp_path):
    entry = {"node_id": "m910", "endpoint": "http://x:8790", "token_env": "TOKEN"}
    with pytest.raises(ValueError, match="more than once"):
        load_config(_write(tmp_path, _base_raw(remote=[dict(entry), dict(entry)])))


def test_remote_node_max_sessions_must_be_positive_int_if_given(tmp_path):
    with pytest.raises(ValueError, match="max_sessions"):
        load_config(_write(tmp_path, _base_raw(remote=[{
            "node_id": "m910", "endpoint": "http://x:8790", "token_env": "TOKEN", "max_sessions": 0,
        }])))


def test_multiple_remote_nodes(tmp_path):
    config = load_config(_write(tmp_path, _base_raw(remote=[
        {"node_id": "m910", "endpoint": "http://192.168.1.50:8790", "token_env": "TERMINAL_MCP_NODE_TOKEN_M910"},
        {"node_id": "laptop2", "endpoint": "http://192.168.1.51:8790", "token_env": "TERMINAL_MCP_NODE_TOKEN_LAPTOP2"},
    ])))
    assert {n.node_id for n in config.nodes.remote_nodes} == {"m910", "laptop2"}


def test_real_production_config_yaml_declares_the_real_windows_node(tmp_path):
    # Documents the current, deliberate state of the real deployment
    # config: dell-5530 (a real Windows machine, bootstrapped live over
    # SSH -- see docs/multi-node.md's own "LAN discovery + remote
    # connect" section) is the one node declared here, exactly the shape
    # server_http.py's own startup loop expects (a real config.yaml
    # entry, never a silent auto-registration). Loads cleanly through
    # the real loader, not just a bare YAML parse, so a schema mistake
    # here would fail this test the same way it would fail the real
    # service's own startup.
    import pathlib
    from terminal_mcp.config import load_config
    real_config_path = pathlib.Path(__file__).parents[1] / "config.yaml"
    config = load_config(str(real_config_path))
    assert [n.node_id for n in config.nodes.remote_nodes] == ["dell-5530"]
    node = config.nodes.remote_nodes[0]
    assert node.endpoint == "http://192.168.1.250:8790"
    assert node.token_env == "TERMINAL_MCP_NODE_TOKEN_DELL_5530"
