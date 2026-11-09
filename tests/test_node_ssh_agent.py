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


def test_sshd_preflight_retries_only_missing_hostkeys_with_temporary_validation_key(monkeypatch):
    import subprocess

    calls = []
    temporary_key = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "/usr/sbin/sshd" and "-o" not in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="sshd: no hostkeys available -- exiting.\n")
        if argv[0] == "/usr/bin/ssh-keygen":
            key = Path(argv[argv.index("-f") + 1])
            temporary_key.append(key)
            key.write_text("temporary validation key")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[0] == "/usr/sbin/sshd" and "-o" in argv:
            return subprocess.CompletedProcess(
                argv, 0,
                stdout="port 22\npubkeyauthentication yes\nauthorizedkeysfile .ssh/authorized_keys\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(node_ssh_agent.shutil, "which", lambda name: {
        "ssh-keygen": "/usr/bin/ssh-keygen", "sshd": "/usr/sbin/sshd"}.get(name))
    monkeypatch.setattr(node_ssh_agent.subprocess, "run", run)

    result = node_ssh_agent._sshd_config_output(
        "/usr/sbin/sshd", "user=dell,host=localhost,addr=127.0.0.1")

    assert result.returncode == 0
    assert "authorizedkeysfile .ssh/authorized_keys" in result.stdout
    assert len(calls) == 3
    assert calls[0] == ["/usr/sbin/sshd", "-T", "-C", "user=dell,host=localhost,addr=127.0.0.1"]
    assert calls[1][:3] == ["/usr/bin/ssh-keygen", "-q", "-t"]
    assert calls[2][0:2] == ["/usr/sbin/sshd", "-T"]
    assert calls[2][2] == "-o"
    assert temporary_key and not temporary_key[0].exists()


def test_sshd_preflight_does_not_retry_other_configuration_errors(monkeypatch):
    import subprocess

    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Bad configuration option\n")

    monkeypatch.setattr(node_ssh_agent.shutil, "which", lambda name: "/usr/bin/ssh-keygen")
    monkeypatch.setattr(node_ssh_agent.subprocess, "run", run)

    result = node_ssh_agent._sshd_config_output(
        "/usr/sbin/sshd", "user=dell,host=localhost,addr=127.0.0.1")

    assert result.returncode == 1
    assert len(calls) == 1
