from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from terminal_mcp import node_ssh_agent

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhY terminal-mcp:hp-linux-test-node:20261109"


def test_managed_key_append_is_idempotent_and_preserves_user_keys(tmp_path):
    authorized = tmp_path / ".ssh" / "authorized_keys"
    authorized.parent.mkdir()
    authorized.parent.chmod(0o700)
    user_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhY user@host"
    authorized.write_text(user_key + "\n")
    authorized.chmod(0o600)

    first = node_ssh_agent.add_managed_key(authorized, PUBLIC_KEY)
    second = node_ssh_agent.add_managed_key(authorized, PUBLIC_KEY)

    assert first == {"status": "configured", "changed": True}
    assert second == {"status": "configured", "changed": False}
    assert authorized.read_text().splitlines() == [user_key, PUBLIC_KEY]
    assert authorized.stat().st_mode & 0o777 == 0o600


def test_revoke_removes_only_exact_managed_key_and_keeps_file_when_other_keys_remain(tmp_path):
    authorized = tmp_path / ".ssh" / "authorized_keys"
    authorized.parent.mkdir()
    authorized.parent.chmod(0o700)
    user_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhY user@host"
    other_managed = PUBLIC_KEY.replace("terminal-mcp:hp-linux-test-node:20261109", "terminal-mcp:hp-linux-other-node:20261109")
    authorized.write_text(f"{user_key}\n{PUBLIC_KEY}\n{other_managed}\n")
    authorized.chmod(0o600)

    result = node_ssh_agent.remove_managed_key(authorized, PUBLIC_KEY)

    assert result == {"status": "removed", "changed": True}
    assert authorized.read_text().splitlines() == [user_key, other_managed]


def test_concurrent_setup_appends_one_managed_entry(tmp_path):
    authorized = tmp_path / ".ssh" / "authorized_keys"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _n: node_ssh_agent.add_managed_key(authorized, PUBLIC_KEY), range(16)))
    assert authorized.read_text().splitlines() == [PUBLIC_KEY]
    assert sum(bool(result["changed"]) for result in results) == 1


def test_invalid_public_key_never_changes_authorized_keys(tmp_path):
    authorized = tmp_path / ".ssh" / "authorized_keys"
    authorized.parent.mkdir()
    authorized.write_text("user-key\n")

    with pytest.raises(ValueError, match="invalid managed public key"):
        node_ssh_agent.add_managed_key(authorized, "../../authorized_keys")

    assert authorized.read_text() == "user-key\n"


def test_setup_reports_needs_admin_when_service_or_key_path_is_not_writable(monkeypatch, tmp_path):
    monkeypatch.setattr(node_ssh_agent, "_platform_preflight", lambda: {
        "platform": "linux", "ssh_server": "missing", "status": "needs_admin",
        "reason": "OpenSSH server is not installed; install/enable sshd with an administrator.",
    })
    result = node_ssh_agent.manage_ssh("setup", PUBLIC_KEY, home=tmp_path)
    assert result["status"] == "needs_admin"
    assert "administrator" in result["reason"]


def test_sshd_policy_preflight_requires_public_key_and_expected_authorized_keys_path():
    valid = "port 2222\npubkeyauthentication yes\nauthorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\n"
    assert node_ssh_agent._sshd_effective_policy(valid) == (True, 2222, None)
    missing = "port 22\npubkeyauthentication no\nauthorizedkeysfile /etc/ssh/keys/%u\n"
    ok, port, reason = node_ssh_agent._sshd_effective_policy(missing)
    assert (ok, port) == (False, 22)
    assert "does not use public keys" in reason


def test_sshd_policy_rejects_ambiguous_multiple_ports():
    ok, _port, reason = node_ssh_agent._sshd_effective_policy(
        "port 22 2222\npubkeyauthentication yes\nauthorizedkeysfile .ssh/authorized_keys\n")
    assert ok is False
    assert "Multiple" in reason
