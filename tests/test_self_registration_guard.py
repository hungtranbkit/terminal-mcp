"""A controller must never register itself as one of its own remote nodes.

docs/CONTROLLER_RUNBOOK.md calls this out by name, and it is easy to reach by
accident: the repo ships a TRACKED config.yaml, `default_config_path()` falls
back to it whenever TERMINAL_MCP_CONFIG is unset, and that tracked file went
on describing the previous controller host as a worker after the fleet had
already moved to a new one. Config review is not a durable guard; this is.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from terminal_mcp.server_http import endpoint_is_this_host, register_remote_nodes

# RFC 5737 TEST-NET-1: reserved for documentation, guaranteed never assigned
# to a real interface, so a bind against it must always fail.
UNASSIGNABLE = "192.0.2.1"


@pytest.mark.parametrize("endpoint", [
    "http://127.0.0.1:8790",
    "http://localhost:8790",
])
def test_loopback_is_recognised_as_this_host(endpoint):
    assert endpoint_is_this_host(endpoint) is True


@pytest.mark.parametrize("endpoint", [
    f"http://{UNASSIGNABLE}:8790",
    "http://198.51.100.7:8790",  # TEST-NET-2
])
def test_addresses_this_host_does_not_hold_are_remote(endpoint):
    assert endpoint_is_this_host(endpoint) is False


@pytest.mark.parametrize("endpoint", [
    "",
    "not-a-url",
    "http://",
    "http://host.invalid:8790",  # .invalid never resolves (RFC 2606)
])
def test_unusable_endpoints_are_not_claimed_as_local(endpoint):
    """Anything unparseable or unresolvable must fall through as NOT local,
    so it surfaces as an unreachable node rather than being silently dropped
    as 'that's us'."""
    assert endpoint_is_this_host(endpoint) is False


@dataclass
class _Remote:
    node_id: str
    endpoint: str
    display_name: str = "n"
    hostname: str = "h"
    token_env: str = "TOKEN_ENV_FOR_TEST"
    max_sessions: int | None = None
    timeout_seconds: float = 5.0


class _Config:
    def __init__(self, remotes):
        self.nodes = type("N", (), {"remote_nodes": remotes})()


class _Controller:
    def __init__(self):
        self.registered = []

    def register_remote_node(self, node_id, **kwargs):
        self.registered.append(node_id)


def test_refuses_a_remote_entry_pointing_at_this_host(monkeypatch):
    monkeypatch.setenv("TOKEN_ENV_FOR_TEST", "secret")
    controller = _Controller()
    registered = register_remote_nodes(controller, _Config([
        _Remote("myself", "http://127.0.0.1:8790"),
        _Remote("real-worker", f"http://{UNASSIGNABLE}:8790"),
    ]))
    assert registered == ["real-worker"]
    assert controller.registered == ["real-worker"]


def test_refuses_a_remote_entry_named_local(monkeypatch):
    """The in-process node is always `local`; a remote may never claim it,
    whatever address it gives."""
    monkeypatch.setenv("TOKEN_ENV_FOR_TEST", "secret")
    controller = _Controller()
    assert register_remote_nodes(controller, _Config([
        _Remote("local", f"http://{UNASSIGNABLE}:8790"),
    ])) == []
    assert controller.registered == []


def test_still_skips_a_node_whose_token_is_unset(monkeypatch):
    """Pre-existing fail-soft behaviour must survive the new guard."""
    monkeypatch.delenv("TOKEN_ENV_FOR_TEST", raising=False)
    controller = _Controller()
    assert register_remote_nodes(controller, _Config([
        _Remote("real-worker", f"http://{UNASSIGNABLE}:8790"),
    ])) == []


def test_a_normal_worker_is_still_registered(monkeypatch):
    monkeypatch.setenv("TOKEN_ENV_FOR_TEST", "secret")
    controller = _Controller()
    assert register_remote_nodes(controller, _Config([
        _Remote("dell-5530", f"http://{UNASSIGNABLE}:8790"),
    ])) == ["dell-5530"]
