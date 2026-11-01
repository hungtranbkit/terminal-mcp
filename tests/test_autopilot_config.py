"""Autopilot config: absent means off; unsafe combinations cannot load."""
from __future__ import annotations

import pytest
import yaml

from terminal_mcp.autopilot_policy import load_autopilot_config
from terminal_mcp.config import load_config

APPROVED = {"project_id": "synthetic/p", "dispatch": True, "approved_by": "owner",
            "approval_ref": "https://github.com/hungtranbkit/terminal-mcp/issues/106",
            "lanes": ["p-work-1"]}


def test_defaults_are_off(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("{}\n")
    policy = load_config(path).autopilot
    assert policy.enabled is False and policy.projects == () and policy.kill_switch is False
    assert policy.planner.kind == "deterministic"
    assert load_autopilot_config(None).enabled is False


def test_full_config_loads_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"autopilot": {"enabled": True, "projects": [APPROVED]}}))
    policy = load_config(path).autopilot
    assert policy.enabled is True
    assert policy.project("synthetic/p").dispatch is True


@pytest.mark.parametrize("item", [
    {**APPROVED, "approved_by": None},
    {**APPROVED, "approval_ref": ""},
    {**APPROVED, "lanes": []},
    {**APPROVED, "token": "x"},
    {**APPROVED, "max_in_flight": 50},
])
def test_unsafe_project_entries_are_refused(item):
    with pytest.raises(ValueError):
        load_autopilot_config({"enabled": True, "projects": [item]})


def test_dispatch_must_be_literal_true():
    policy = load_autopilot_config({"projects": [{**APPROVED, "dispatch": "yes"}]})
    assert policy.project("synthetic/p").dispatch is False


def test_lane_cannot_belong_to_two_projects():
    other = {**APPROVED, "project_id": "synthetic/q"}
    with pytest.raises(ValueError, match="two projects"):
        load_autopilot_config({"projects": [APPROVED, other]})


def test_unknown_keys_and_inline_planner_secrets_are_refused():
    with pytest.raises(ValueError):
        load_autopilot_config({"enable": True})
    with pytest.raises(ValueError):
        load_autopilot_config({"planner": {"kind": "gateway", "endpoint": "https://gw",
                                           "token": "sk-live"}})
    with pytest.raises(ValueError):
        load_autopilot_config({"planner": {"kind": "gateway", "endpoint": "http://evil.example"}})
    policy = load_autopilot_config({"planner": {"kind": "gateway", "endpoint": "https://gw.internal",
                                                "token_file": "/run/secrets/gw"}})
    assert policy.planner.kind == "gateway"
