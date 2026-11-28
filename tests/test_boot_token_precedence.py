"""A config-declared node must boot with the token its credential ledger
says is ACTIVE, not whatever a stale EnvironmentFile still carries.

Incident 2026-10-26 (node `macbook`): a confirmed rotation wrote the new
token to the 0600 token file and the ledger, but the controller's systemd
EnvironmentFile still held a pre-reinstall token. The next controller
restart registered the node with the env value, every outbound call got
401, and the node-agent's auth throttle turned that into a standing 429.
Inbound heartbeats kept verifying (the ledger knew the right token), so
the node looked half-alive for hours.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import pytest

from terminal_mcp.connection_store import ConnectionStore
from terminal_mcp.node_credentials import NodeCredentialStore, token_fingerprint
from terminal_mcp.server_http import register_remote_nodes, resolve_boot_token

UNASSIGNABLE = "192.0.2.1"  # RFC 5737 TEST-NET-1, never this host
ENV = "TOKEN_ENV_FOR_TEST"
STALE = "a" * 64
ROTATED = "b" * 64


@dataclass
class _Remote:
    node_id: str
    endpoint: str
    display_name: str = "n"
    hostname: str = "h"
    token_env: str = ENV
    max_sessions: int | None = None
    timeout_seconds: float = 5.0
    self_heal_enabled: bool = False
    self_heal_action: str = "none"


class _Config:
    def __init__(self, remotes):
        self.nodes = type("N", (), {"remote_nodes": remotes})()


class _RecordingController:
    def __init__(self):
        self.tokens = {}

    def register_remote_node(self, node_id, **kwargs):
        self.tokens[node_id] = kwargs["token"]


@pytest.fixture
def stores(tmp_path):
    connections = ConnectionStore(tmp_path / "connections.db")
    credentials = NodeCredentialStore(tmp_path / "node-credentials.db")
    return connections, credentials


def _save_token_file(connections, node_id, token):
    path = connections.write_token(node_id, token)
    connections.save(node_id, transport_type="agent_token", endpoint=f"http://{UNASSIGNABLE}:8790",
                     hostname=node_id, username=None, port=None, host_key_fingerprint=None,
                     token_file=path)


def test_rotated_token_file_wins_over_stale_env(monkeypatch, stores, caplog):
    connections, credentials = stores
    credentials.adopt("macbook", ROTATED)
    _save_token_file(connections, "macbook", ROTATED)
    monkeypatch.setenv(ENV, STALE)
    controller = _RecordingController()

    with caplog.at_level(logging.WARNING):
        assert register_remote_nodes(controller, _Config([_Remote("macbook", f"http://{UNASSIGNABLE}:8790")]),
                                     connection_store=connections, credentials=credentials) == ["macbook"]

    assert controller.tokens["macbook"] == ROTATED
    # The env var is what inbound verification and later code read; it must
    # agree with what was registered, or the drift just moves elsewhere.
    assert os.environ[ENV] == ROTATED
    # Fingerprints only, never the secret itself.
    assert token_fingerprint(STALE) in caplog.text and token_fingerprint(ROTATED) in caplog.text
    assert STALE not in caplog.text and ROTATED not in caplog.text


def test_env_token_kept_when_it_is_the_active_credential(monkeypatch, stores):
    connections, credentials = stores
    credentials.adopt("n1", ROTATED)
    _save_token_file(connections, "n1", STALE)  # a lagging file must not win
    token, source = resolve_boot_token("n1", ROTATED, connection_store=connections, credentials=credentials)
    assert (token, source) == (ROTATED, "env")


def test_unmanaged_node_keeps_legacy_env_behaviour(stores):
    connections, credentials = stores
    _save_token_file(connections, "legacy", ROTATED)
    assert resolve_boot_token("legacy", STALE, connection_store=connections,
                              credentials=credentials) == (STALE, "env")


def test_revoked_token_file_is_never_promoted(stores):
    """Precedence only chooses between tokens the ledger accepts. A file
    holding a revoked credential must not replace the env value."""
    connections, credentials = stores
    credentials.adopt("n2", STALE)
    credentials.replace("n2", ROTATED)  # STALE is now revoked
    _save_token_file(connections, "n2", STALE)
    token, source = resolve_boot_token("n2", "c" * 64, connection_store=connections, credentials=credentials)
    assert (token, source) == ("c" * 64, "env")


def test_token_file_used_when_env_is_unset(monkeypatch, stores):
    connections, credentials = stores
    credentials.adopt("n3", ROTATED)
    _save_token_file(connections, "n3", ROTATED)
    monkeypatch.delenv(ENV, raising=False)
    controller = _RecordingController()
    assert register_remote_nodes(controller, _Config([_Remote("n3", f"http://{UNASSIGNABLE}:8790")]),
                                 connection_store=connections, credentials=credentials) == ["n3"]
    assert controller.tokens["n3"] == ROTATED


def test_without_stores_behaviour_is_unchanged(monkeypatch):
    monkeypatch.setenv(ENV, STALE)
    controller = _RecordingController()
    register_remote_nodes(controller, _Config([_Remote("n4", f"http://{UNASSIGNABLE}:8790")]))
    assert controller.tokens["n4"] == STALE
