"""remote_connect.py -- SSH target validation (SSRF), argv building (never
a shell string), host-key probing/pinning/mismatch, credential
redaction, bootstrap script templating, Windows manual-fallback honesty.

Every test that touches a real subprocess (`ssh`/`ssh-keyscan`/`ssh-
keygen`) injects a fake `runner` callable instead -- fully deterministic,
no real network/SSH server needed. The exception is the small `real_ssh`-
marked block at the bottom, which drives THIS host's own real sshd
(127.0.0.1, key already in ~/.ssh/authorized_keys) end to end -- not run
by default (see pyproject.toml's own marker docstring), but was run for
real during this feature's own development; see the task's final report
for that evidence."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from terminal_mcp import remote_connect as rc


# ---------------------------------------------------------------------------
# validation (SSRF + format)
# ---------------------------------------------------------------------------


def test_validate_hostname_or_ip_accepts_private_ipv4_literal():
    assert rc.validate_hostname_or_ip("192.168.1.50") == "192.168.1.50"


def test_validate_hostname_or_ip_rejects_public_ip_by_default():
    with pytest.raises(rc.ValidationError, match="not a private/LAN address"):
        rc.validate_hostname_or_ip("8.8.8.8")


def test_validate_hostname_or_ip_allows_public_when_explicitly_overridden():
    assert rc.validate_hostname_or_ip("8.8.8.8", allow_public=True) == "8.8.8.8"


def test_validate_hostname_or_ip_rejects_empty_and_too_long():
    with pytest.raises(rc.ValidationError):
        rc.validate_hostname_or_ip("")
    with pytest.raises(rc.ValidationError):
        rc.validate_hostname_or_ip("a" * 300)


def test_validate_hostname_or_ip_rejects_malformed_hostname_without_a_dns_lookup():
    # Shell-metacharacter-laced input must be refused by FORMAT alone,
    # before any resolution is even attempted (defense in depth on top of
    # "never a shell string" -- see build_ssh_argv/build_keyscan_argv).
    with pytest.raises(rc.ValidationError, match="invalid hostname"):
        rc.validate_hostname_or_ip("host; rm -rf /")
    with pytest.raises(rc.ValidationError, match="invalid hostname"):
        rc.validate_hostname_or_ip("$(whoami)")
    with pytest.raises(rc.ValidationError, match="invalid hostname"):
        rc.validate_hostname_or_ip("host`id`")


def test_validate_hostname_or_ip_rejects_unresolvable_hostname():
    with pytest.raises(rc.ValidationError, match="could not resolve"):
        rc.validate_hostname_or_ip("this-host-should-never-resolve.invalid")


def test_validate_cloudflare_hostname_has_no_private_ip_requirement():
    # A Cloudflare Access SSH hostname is never a direct network target --
    # syntactic hostname validation only, deliberately no resolution/SSRF
    # check (the whole point of the tunnel).
    assert rc.validate_cloudflare_hostname("ssh.m910.example.com") == "ssh.m910.example.com"


def test_validate_cloudflare_hostname_rejects_shell_metacharacters():
    with pytest.raises(rc.ValidationError):
        rc.validate_cloudflare_hostname("ssh.example.com; rm -rf /")
    with pytest.raises(rc.ValidationError):
        rc.validate_cloudflare_hostname("")


@pytest.mark.parametrize("username", ["dell", "pi_01", "user.name", "user-name"])
def test_validate_username_accepts_safe_charset(username):
    assert rc.validate_username(username) == username


@pytest.mark.parametrize("username", ["", "user name", "user;rm", "user$(whoami)", "a" * 40])
def test_validate_username_rejects_unsafe_or_oversized(username):
    with pytest.raises(rc.ValidationError):
        rc.validate_username(username)


def test_validate_node_id_matches_the_existing_onboarding_route_convention():
    # Format only -- "local" being a reserved id is controller.py's own
    # business-logic concern (NodeRegistry/ControllerService), checked
    # separately at the dashboard route layer (NODE_ALREADY_EXISTS), not
    # this format-only validator's job.
    assert rc.validate_node_id("m910") == "m910"
    with pytest.raises(rc.ValidationError):
        rc.validate_node_id("bad id!")


def test_validate_port_defaults_and_rejects_out_of_range():
    assert rc.validate_port(None, default=22) == 22
    assert rc.validate_port("2222", default=22) == 2222
    with pytest.raises(rc.ValidationError):
        rc.validate_port(0, default=22)
    with pytest.raises(rc.ValidationError):
        rc.validate_port(70000, default=22)
    with pytest.raises(rc.ValidationError):
        rc.validate_port("not-a-number", default=22)


# ---------------------------------------------------------------------------
# SshCredential -- exactly one of password/private_key_pem, redaction
# ---------------------------------------------------------------------------


def test_credential_requires_exactly_one_auth_method():
    with pytest.raises(rc.ValidationError):
        rc.SshCredential(username="dell")  # neither
    with pytest.raises(rc.ValidationError):
        rc.SshCredential(username="dell", password="x", private_key_pem="y")  # both


def test_credential_repr_and_str_never_leak_the_secret():
    cred = rc.SshCredential(username="dell", password="super-secret-password")
    assert "super-secret-password" not in repr(cred)
    assert "super-secret-password" not in str(cred)
    assert "redacted" in repr(cred)
    key_cred = rc.SshCredential(username="dell", private_key_pem="-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END-----")
    assert "BEGIN OPENSSH" not in repr(key_cred)
    assert "BEGIN OPENSSH" not in str(key_cred)


# ---------------------------------------------------------------------------
# build_ssh_argv / build_keyscan_argv -- pure functions, argv LISTS, never
# a shell string (except the ONE fixed ProxyCommand template, asserted
# below to contain no user-controlled bytes).
# ---------------------------------------------------------------------------


def test_build_ssh_argv_is_a_plain_list_never_a_shell_string():
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")
    argv = rc.build_ssh_argv(target, known_hosts_file=__import__("pathlib").Path("/tmp/kh"), strict_host_key_checking=True)
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert argv[0] == "ssh"
    assert "192.168.1.50" not in " ".join(argv[:-1])  # only appears once, as its own argv element (the target)
    assert argv[-1] == "pi@192.168.1.50"


def test_build_ssh_argv_lan_ssh_has_no_proxycommand():
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")
    argv = rc.build_ssh_argv(target, known_hosts_file=__import__("pathlib").Path("/tmp/kh"), strict_host_key_checking=True)
    assert not any("ProxyCommand" in part for part in argv)


def test_build_ssh_argv_cloudflare_ssh_uses_the_fixed_proxycommand_template_only():
    target = rc.SshTarget(transport_type=rc.TRANSPORT_CLOUDFLARE_SSH, host="ssh.m910.example.com", port=22, username="pi")
    argv = rc.build_ssh_argv(target, known_hosts_file=__import__("pathlib").Path("/tmp/kh"), strict_host_key_checking=True)
    proxy_opt = next(part for part in argv if part.startswith("ProxyCommand="))
    assert proxy_opt == f"ProxyCommand={rc.CLOUDFLARE_PROXY_COMMAND}"
    # The hostname itself never appears inside the ProxyCommand string --
    # only OpenSSH's own %h/%p tokens do; the hostname is a SEPARATE argv
    # element (the ssh target), never spliced into this shell-parsed value.
    assert "ssh.m910.example.com" not in proxy_opt
    assert "%h" in proxy_opt and "%p" not in proxy_opt  # %p not needed/used here, %h is


def test_build_ssh_argv_identity_file_and_batch_mode():
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="10.0.0.5", port=2222, username="pi")
    argv = rc.build_ssh_argv(target, known_hosts_file=__import__("pathlib").Path("/tmp/kh"), strict_host_key_checking=True,
                             identity_file=__import__("pathlib").Path("/tmp/key"), batch_mode=True)
    assert "-i" in argv and argv[argv.index("-i") + 1] == "/tmp/key"
    assert "BatchMode=yes" in argv
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2222"


def test_build_keyscan_argv_cloudflare_uses_fixed_proxycommand_too():
    target = rc.SshTarget(transport_type=rc.TRANSPORT_CLOUDFLARE_SSH, host="ssh.example.com", port=22, username="pi")
    argv = rc.build_keyscan_argv(target)
    assert argv[0] == "ssh-keyscan"
    proxy_opt = next(part for part in argv if part.startswith("ProxyCommand="))
    assert proxy_opt == f"ProxyCommand={rc.CLOUDFLARE_PROXY_COMMAND}"
    assert argv[-1] == "ssh.example.com"


# ---------------------------------------------------------------------------
# Host-key probing / pinning / mismatch -- fake runner, deterministic.
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def test_probe_host_key_ok(monkeypatch):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")
    calls = []

    def fake_runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ssh-keyscan":
            return _FakeCompleted(stdout="192.168.1.50 ssh-ed25519 AAAAfakekeydata\n")
        if argv[0] == "ssh-keygen":
            return _FakeCompleted(stdout="256 SHA256:fakefingerprint host (ED25519)\n")
        raise AssertionError(f"unexpected command {argv}")

    result = rc.probe_host_key(target, runner=fake_runner)
    assert result.ok is True
    assert result.fingerprint == "SHA256:fakefingerprint"
    assert result.key_line == "192.168.1.50 ssh-ed25519 AAAAfakekeydata"


def test_probe_host_key_classifies_unreachable():
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")

    def fake_runner(argv, **kwargs):
        return _FakeCompleted(stdout="", stderr="ssh: connect to host 192.168.1.50 port 22: Connection refused")
    result = rc.probe_host_key(target, runner=fake_runner)
    assert result.ok is False
    assert result.error_class == "unreachable"


def test_probe_host_key_cloudflared_missing_detected_before_any_subprocess_call(monkeypatch):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_CLOUDFLARE_SSH, host="ssh.example.com", port=22, username="pi")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    calls = []
    result = rc.probe_host_key(target, runner=lambda *a, **k: calls.append(1))
    assert result.error_class == "cloudflared_missing"
    assert calls == []  # never even tried to run ssh-keyscan


@pytest.mark.parametrize("stderr,expected_class", [
    ("Please open the following URL and log in: https://...", "access_login_required"),
    ("dial tcp: lookup ssh.example.com: no such host", "dns_tls_fail"),
    ("x509: certificate signed by unknown authority", "dns_tls_fail"),
    ("dial tcp 1.2.3.4:443: connect: connection refused", "unreachable"),
])
def test_classify_stderr_patterns(stderr, expected_class):
    assert rc._classify_stderr(stderr) == expected_class


def test_host_key_store_trust_and_pinned_for_and_forget():
    store = rc.HostKeyStore()
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")
    assert store.pinned_for(target) is None
    store.trust(target, "SHA256:abc")
    assert store.pinned_for(target) == "SHA256:abc"
    store.forget(target)
    assert store.pinned_for(target) is None


def test_test_connection_reports_host_key_new_when_never_pinned(tmp_path):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")

    def fake_runner(argv, **kwargs):
        if argv[0] == "ssh-keyscan":
            return _FakeCompleted(stdout="192.168.1.50 ssh-ed25519 AAAAfakekeydata\n")
        return _FakeCompleted(stdout="256 SHA256:fresh host (ED25519)\n")

    result = rc.test_connection(target, known_hosts_dir=tmp_path, host_key_store=rc.HostKeyStore(), runner=fake_runner)
    assert result.stage == "host_key_new"
    assert result.ok is False
    assert result.fingerprint == "SHA256:fresh"


def test_test_connection_hard_fails_on_mismatch_never_auto_accepts(tmp_path):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")

    def fake_runner(argv, **kwargs):
        if argv[0] == "ssh-keyscan":
            return _FakeCompleted(stdout="192.168.1.50 ssh-ed25519 AAAAchangedkeydata\n")
        return _FakeCompleted(stdout="256 SHA256:CHANGED host (ED25519)\n")

    result = rc.test_connection(target, known_hosts_dir=tmp_path, host_key_store=rc.HostKeyStore(),
                                pinned_fingerprint="SHA256:OLD", runner=fake_runner)
    assert result.stage == "host_key_mismatch"
    assert result.ok is False


def test_test_connection_ok_when_pinned_matches(tmp_path):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")

    def fake_runner(argv, **kwargs):
        if argv[0] == "ssh-keyscan":
            return _FakeCompleted(stdout="192.168.1.50 ssh-ed25519 AAAAfakekeydata\n")
        return _FakeCompleted(stdout="256 SHA256:MATCH host (ED25519)\n")

    result = rc.test_connection(target, known_hosts_dir=tmp_path, host_key_store=rc.HostKeyStore(),
                                pinned_fingerprint="SHA256:MATCH", runner=fake_runner)
    assert result.stage == "ok"
    assert result.ok is True


# ---------------------------------------------------------------------------
# Linux bootstrap -- script templating (fixed script, structured args only,
# no browser-supplied shell text), credential-path selection, host-key
# re-check before ever running.
# ---------------------------------------------------------------------------


def test_run_linux_bootstrap_refuses_when_pinned_fingerprint_does_not_match(tmp_path):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")
    cred = rc.SshCredential(username="pi", password="x")

    def fake_runner(argv, **kwargs):
        if argv[0] == "ssh-keyscan":
            return _FakeCompleted(stdout="192.168.1.50 ssh-ed25519 AAAAfakekeydata\n")
        if argv[0] == "ssh-keygen":
            return _FakeCompleted(stdout="256 SHA256:actual host (ED25519)\n")
        raise AssertionError("must never attempt the real ssh bootstrap call on a host-key mismatch")

    result = rc.run_linux_bootstrap(target, cred, node_id="pi01", controller_url="http://10.0.0.1:8766",
                                    token="tok", bind_host="192.168.1.50", known_hosts_dir=tmp_path,
                                    pinned_fingerprint="SHA256:expected-but-wrong", runner=fake_runner)
    assert result.ok is False
    assert result.detail == "host_key_mismatch"


def test_run_linux_bootstrap_key_auth_passes_script_via_stdin_and_args_via_positional(tmp_path):
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="192.168.1.50", port=22, username="pi")
    cred = rc.SshCredential(username="pi", private_key_pem="-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----\n")
    captured = {}

    def fake_runner(argv, **kwargs):
        if argv[0] == "ssh-keyscan":
            return _FakeCompleted(stdout="192.168.1.50 ssh-ed25519 AAAAfakekeydata\n")
        if argv[0] == "ssh-keygen":
            return _FakeCompleted(stdout="256 SHA256:MATCH host (ED25519)\n")
        if argv[0] == "ssh":
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            return _FakeCompleted(stdout="bootstrap ok\n", returncode=0)
        raise AssertionError(f"unexpected command {argv}")

    result = rc.run_linux_bootstrap(target, cred, node_id="pi01", controller_url="http://10.0.0.1:8766",
                                    token="secret-token-value", bind_host="192.168.1.50", known_hosts_dir=tmp_path,
                                    pinned_fingerprint="SHA256:MATCH", runner=fake_runner)
    assert result.ok is True
    assert captured["input"] == rc.BOOTSTRAP_SCRIPT_LINUX  # the ONE fixed script -- never browser-supplied text
    assert "pi01" in " ".join(captured["argv"])  # node_id passed as a positional arg
    assert "secret-token-value" in " ".join(captured["argv"])  # token passed structurally, not spliced into the script
    assert "-i" in captured["argv"]  # identity file used for key auth
    assert "BatchMode=yes" in captured["argv"]


def test_windows_bootstrap_guidance_never_claims_a_live_install():
    guidance = rc.windows_bootstrap_guidance(node_id="m910", controller_url="http://10.0.0.1:8766",
                                             token="tok-value", hostname="m910.local")
    assert guidance["status"] == "manual_required"
    assert "install-node-agent.ps1" in guidance["install_command"]
    assert "tok-value" in guidance["install_command"]
    assert isinstance(guidance["instructions"], list) and guidance["instructions"]


# ---------------------------------------------------------------------------
# Real, live smoke test against THIS host's own real sshd (127.0.0.1) --
# opt-in only (pytest -m real_ssh), skipped if key-based localhost login
# isn't already set up (never modifies ~/.ssh/authorized_keys itself).
# ---------------------------------------------------------------------------


@pytest.mark.real_ssh
def test_real_localhost_ssh_key_auth_and_host_key_pin_end_to_end(tmp_path):
    if shutil.which("ssh") is None or shutil.which("ssh-keyscan") is None:
        pytest.skip("ssh/ssh-keyscan not installed")
    from pathlib import Path
    key_path = Path.home() / ".ssh" / "id_ed25519"
    if not key_path.exists():
        pytest.skip("no ~/.ssh/id_ed25519 on this host")
    probe = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                            "-o", "StrictHostKeyChecking=accept-new", "127.0.0.1", "true"],
                           capture_output=True, timeout=10)
    if probe.returncode != 0:
        pytest.skip("key-based localhost SSH login is not already set up on this host")

    import os
    target = rc.SshTarget(transport_type=rc.TRANSPORT_LAN_SSH, host="127.0.0.1", port=22, username=os.environ.get("USER", "root"))
    probed = rc.probe_host_key(target)
    assert probed.ok is True
    assert probed.fingerprint.startswith("SHA256:")

    cred = rc.SshCredential(username=target.username, private_key_pem=key_path.read_text())
    result = rc.test_connection_with_credential(target, cred, known_hosts_dir=tmp_path, pinned_fingerprint=probed.fingerprint)
    assert result.stage == "ok"
    assert result.ok is True

    mismatch = rc.test_connection_with_credential(target, cred, known_hosts_dir=tmp_path,
                                                  pinned_fingerprint="SHA256:deliberately-wrong")
    assert mismatch.stage == "host_key_mismatch"
