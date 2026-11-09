from __future__ import annotations

from types import SimpleNamespace
import os
from pathlib import Path

from terminal_mcp.node_client import NodeClientError
from terminal_mcp.node_client import RemoteNodeClient
from terminal_mcp.node_ssh import NodeSSHService


class Registry:
    def get(self, node_id):
        return SimpleNamespace(endpoint="http://10.1.2.3:8790")


class Controller:
    registry = Registry()
    def __init__(self, client): self.client = client
    def client_for(self, node_id): return self.client


class Client:
    def __init__(self, result): self.result = result
    def ssh_manage(self, action, public_key=None): return self.result


def test_old_agent_endpoint_is_reported_unsupported_not_success_or_offline(tmp_path):
    client = Client({})
    def unsupported(action, public_key=None):
        raise NodeClientError("POST /v1/ssh/manage -> HTTP 404", http_status=404)
    client.ssh_manage = unsupported
    service = NodeSSHService(Controller(client), tmp_path / "keys")
    result = service.status("hp-linux")
    assert result["status"] == "unsupported"
    assert result["error"] == "unsupported"


def test_online_heartbeat_without_node_client_is_not_authorization(tmp_path):
    service = NodeSSHService(Controller(None), tmp_path / "keys")
    result = service.status("hp-linux")
    assert result["status"] == "offline"
    assert result["configured"] is False


def test_profile_store_directory_is_private_and_never_contains_key_material(tmp_path):
    service = NodeSSHService(Controller(Client({"status": "needs_admin"})), tmp_path / "keys")
    result = service.setup("hp-linux")
    assert result["status"] == "needs_admin"
    assert not list((tmp_path / "keys").glob("**/id_ed25519"))


def test_verify_uses_isolated_strict_identity_and_checks_nonce_user_and_node(monkeypatch, tmp_path):
    import json
    import subprocess
    import re

    service = NodeSSHService(Controller(Client({"status": "ready"})), tmp_path / "keys")
    key, known_hosts, profile_path = service._paths("hp-linux")
    key.write_text("private")
    known_hosts.write_text("10.1.2.3 ssh-ed25519 dGVzdA==\n")
    profile_path.write_text(json.dumps({"status": "verified", "verified": True, "address": "10.1.2.3",
                                        "username": "alice", "port": 22, "expected_hostname": "node-a"}))
    seen = {}
    def run(argv, **kwargs):
        seen["argv"] = argv
        remote = argv[-1]
        nonce = re.search(r"'([0-9a-f]{32})'", remote).group(1)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{nonce}\nalice\nnode-a\n", stderr="")
    monkeypatch.setattr("terminal_mcp.node_ssh.subprocess.run", run)

    result = service.verify("hp-linux")

    assert result["status"] == "verified"
    argv = seen["argv"]
    assert "-F" in argv and os.devnull in argv
    assert "IdentityAgent=none" in argv and "IdentitiesOnly=yes" in argv
    assert "BatchMode=yes" in argv and "StrictHostKeyChecking=yes" in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv


def test_verify_rejects_host_identity_that_does_not_match_authenticated_node(monkeypatch, tmp_path):
    import json
    import subprocess
    import re

    service = NodeSSHService(Controller(Client({"status": "ready"})), tmp_path / "keys")
    key, known_hosts, profile_path = service._paths("hp-linux")
    key.write_text("private")
    known_hosts.write_text("10.1.2.3 ssh-ed25519 dGVzdA==\n")
    profile_path.write_text(json.dumps({"status": "verified", "verified": True, "address": "10.1.2.3",
                                        "username": "alice", "port": 22, "expected_hostname": "node-a"}))
    def run(argv, **kwargs):
        nonce = re.search(r"'([0-9a-f]{32})'", argv[-1]).group(1)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{nonce}\nalice\nother-node\n", stderr="")
    monkeypatch.setattr("terminal_mcp.node_ssh.subprocess.run", run)
    assert service.verify("hp-linux")["status"] == "identity_mismatch"


def test_exec_uses_structured_argv_and_strict_pinned_options(monkeypatch, tmp_path):
    import json
    import subprocess

    service = NodeSSHService(Controller(Client({"status": "ready"})), tmp_path / "keys")
    key, known_hosts, profile_path = service._paths("hp-linux")
    key.write_text("private")
    known_hosts.write_text("10.1.2.3 ssh-ed25519 dGVzdA==\n")
    profile_path.write_text(json.dumps({"status": "verified", "verified": True, "address": "10.1.2.3",
                                        "username": "alice", "port": 22}))
    captured = {}
    def run(argv, **kwargs):
        if argv[-1].startswith("printf"):
            import re
            nonce = re.search(r"'([0-9a-f]{32})'", argv[-1]).group(1)
            return subprocess.CompletedProcess(argv, 0, stdout=f"{nonce}\nalice\nnode-a\n", stderr="")
        captured.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr("terminal_mcp.node_ssh.subprocess.run", run)

    result = service.exec("hp-linux", ["printf", "%s", "hello world"], timeout=999)

    assert result["status"] == "completed" and result["exit_code"] == 0
    assert captured["argv"][-1] == "exec printf %s 'hello world'"
    assert captured["kwargs"]["timeout"] == 30.0
    assert "IdentityAgent=none" in captured["argv"]
    assert "StrictHostKeyChecking=yes" in captured["argv"]
    assert f"UserKnownHostsFile={known_hosts}" in captured["argv"]


def test_exec_refuses_unverified_profile_and_invalid_command_arguments(tmp_path):
    service = NodeSSHService(Controller(Client({"status": "ready"})), tmp_path / "keys")
    _key, _known_hosts, profile_path = service._paths("hp-linux")
    profile_path.write_text('{"status":"configured","verified":false}')
    assert service.exec("hp-linux", ["id"])["error"] == "ssh_profile_not_verified"
    assert service.exec("hp-linux", ["id", "\narg"])["status"] == "not_configured"
    assert service.exec("hp-linux", ["id", chr(0)])["error"] == "invalid_argv"


def test_setup_never_mutates_node_when_authenticated_host_key_disagrees_with_pin(monkeypatch, tmp_path):
    from terminal_mcp.node_ssh import NodeSSHService

    class RecordingClient:
        def __init__(self): self.calls = []
        def ssh_manage(self, action, public_key=None):
            self.calls.append(action)
            return {"status": "ready", "host_public_key": "ssh-ed25519 dGVzdA== host", "username": "alice",
                    "hostname": "node-a", "port": 22}

    client = RecordingClient()
    service = NodeSSHService(Controller(client), tmp_path / "keys")
    _key, known_hosts, _profile = service._paths("hp-linux")
    known_hosts.write_text("10.1.2.3 ssh-ed25519 b3RoZXI= old-pin\n")
    monkeypatch.setattr(service, "_ensure_key", lambda _path: "ssh-ed25519 dGVzdA== terminal-mcp:hp-linux-dell-linux:20261109")

    result = service.setup("hp-linux")

    assert result["status"] == "host_key_mismatch"
    assert client.calls == ["status"]


def test_connect_argv_uses_fixed_persistent_tmux_and_strict_hostkey(tmp_path):
    import json

    service = NodeSSHService(Controller(Client({"status": "ready"})), tmp_path / "keys")
    key, known_hosts, profile = service._paths("hp-linux")
    key.write_text("private")
    known_hosts.write_text("10.1.2.3 ssh-ed25519 dGVzdA==\n")
    profile.write_text(json.dumps({"status": "verified", "verified": True, "address": "10.1.2.3",
                                   "username": "alice", "port": 22}))
    argv = service.connect_argv("hp-linux")
    assert argv[-2:] == ["alice@10.1.2.3", "tmux new-session -A -s tmcp-managed-terminal"]
    assert "StrictHostKeyChecking=yes" in argv
    assert "IdentityAgent=none" in argv
    assert "GlobalKnownHostsFile=/dev/null" in argv


def test_controller_keypair_is_private_and_tagged_for_the_registered_target(tmp_path):
    import re

    controller = Controller(Client({"status": "ready"}))
    controller.local_node_id = "hp-linux"
    service = NodeSSHService(controller, tmp_path / "keys")
    key_path, _known_hosts, profile_path = service._paths("dell-linux")
    public = service._ensure_key(key_path)
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert Path(str(key_path) + ".pub").stat().st_mode & 0o777 == 0o600
    assert re.fullmatch(r"terminal-mcp:hp-linux-dell-linux:\d{8}", public.split()[-1])
    assert not profile_path.exists()
    assert "PRIVATE KEY" in key_path.read_text()


def test_controller_key_store_refuses_orphaned_public_key_instead_of_overwriting(tmp_path):
    service = NodeSSHService(Controller(Client({"status": "ready"})), tmp_path / "keys")
    key_path, _known_hosts, _profile = service._paths("dell-linux")
    Path(str(key_path) + ".pub").write_text("existing-public-key\n")
    try:
        service._ensure_key(key_path)
    except ValueError as exc:
        assert "refusing to replace" in str(exc)
    else:
        raise AssertionError("orphaned public key must be protected")
    assert Path(str(key_path) + ".pub").read_text() == "existing-public-key\n"
    assert not key_path.exists()


def test_remote_node_client_serializes_only_fixed_ssh_operation(monkeypatch):
    client = RemoteNodeClient("http://node.test:8790", "private-token")
    calls = []
    monkeypatch.setattr(client, "_request", lambda method, path, **kwargs: calls.append((method, path, kwargs)) or {"status": "ready"})
    assert client.ssh_manage("setup", "ssh-ed25519 AAAA terminal-mcp:hp-dell:20261109") == {"status": "ready"}
    assert calls == [("POST", "/v1/ssh/manage", {"body": {
        "action": "setup", "public_key": "ssh-ed25519 AAAA terminal-mcp:hp-dell:20261109"},
        "timeout_seconds": 5})]
    try:
        client.ssh_manage("exec", "id")
    except NodeClientError as exc:
        assert "unsupported SSH management action" in str(exc)
    else:
        raise AssertionError("node agent client must not expose an arbitrary shell operation")
