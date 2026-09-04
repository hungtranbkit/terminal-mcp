"""Remote node connect/bootstrap over SSH -- direct LAN/IP SSH
(`transport_type=lan_ssh`) and SSH tunneled through Cloudflare Access
(`transport_type=cloudflare_ssh`, `cloudflared access ssh`), sharing the
exact same argv-building/host-key-pinning/bootstrap machinery (task's own
"Không dựng implementation song song" -- one transport abstraction, not
two parallel ones).

Security posture (every point mandatory per this feature's own task
spec, re-stated here since it drove every design choice in this file):

  - NEVER a shell string. Every external command is built as an argv
    LIST and run via subprocess with shell=False -- see build_ssh_argv,
    build_keyscan_argv. The one place OpenSSH itself parses a string
    (the `-o ProxyCommand=...` value, which OpenSSH always hands to
    `/bin/sh -c`) uses a FIXED, constant template containing only `%h`/
    `%p` -- OpenSSH's own tokens, substituted by ssh itself from the
    already-validated connection target, never raw user bytes
    interpolated into that string by this code.
  - hostname/username/port are all validated against a strict allowlist
    charset/format (validate_hostname_or_ip, validate_cloudflare_hostname,
    validate_username) BEFORE they ever reach an argv list, defense in
    depth on top of the argv-list-never-a-shell-string posture above.
  - A password/private key/passphrase is used ONLY for the single
    bootstrap subprocess call that installs the node agent -- held in
    memory for that call's duration, in a SshCredential whose own
    __repr__/__str__ are overridden to redact, and NEVER written to
    disk, a log line, or an audit row (dashboard.py's own audit call for
    these routes passes only non-secret fields). A private key, if
    supplied as PEM text, is written to a 0600 temp file for the
    subprocess call and unlinked in a `finally` immediately after.
  - Host keys are PINNED, never auto-accepted and never silently
    re-trusted on change -- see HostKeyStore. First-connect returns the
    presented fingerprint for the operator to explicitly approve (a
    separate "Trust & pin" action); every later connection is checked
    with StrictHostKeyChecking=yes against our OWN managed known_hosts
    file (never the operator's real ~/.ssh/known_hosts) and a changed
    fingerprint hard-fails as host_key_mismatch.
  - The Linux bootstrap script is ONE fixed, server-authored constant
    (BOOTSTRAP_SCRIPT_LINUX below) -- the browser never sends script/
    shell text, only structured fields (node_id, controller_url,
    display_name) substituted via shlex.quote into the script's own
    argv, not string-spliced into the script body itself.
  - Windows: this module NEVER claims to have performed a live WinRM/
    PowerShell-remoting install (no such integration exists here, and
    there is no real Windows host in this project's own dev/test
    environment to verify one against -- see docs/multi-node.md's
    existing "Windows node support" section for that same, already-
    established honesty policy). windows_bootstrap_guidance() always
    returns manual copy-paste instructions instead.
"""
from __future__ import annotations

import ipaddress
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .lan_discovery import is_lan_scannable

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

TRANSPORT_LAN_SSH = "lan_ssh"
TRANSPORT_CLOUDFLARE_SSH = "cloudflare_ssh"

# Fixed OpenSSH ProxyCommand template -- %h/%p are OpenSSH's OWN
# substitution tokens (filled in by ssh itself from the validated
# connection target before the string is ever handed to `sh -c`), never
# raw user-controlled bytes. This exact string never has anything
# interpolated into it by this module.
CLOUDFLARE_PROXY_COMMAND = "cloudflared access ssh --hostname %h"


class ValidationError(ValueError):
    pass


def validate_hostname_or_ip(value: str, *, allow_public: bool = False) -> str:
    """For a DIRECT (lan_ssh) target: must be a syntactically valid
    hostname or IPv4 literal, and every address it resolves to must be
    LAN-scannable (see lan_discovery.is_lan_scannable) unless
    allow_public is explicitly set (an admin-only config override, off
    by default) -- checking EVERY resolved address, not just the first,
    is deliberate: a hostname resolving to a mix of private and public
    addresses is refused outright rather than trusting whichever
    happened to be tried first (defeats a DNS-rebinding-style SSRF
    attempt where a name resolves differently across two lookups)."""
    value = (value or "").strip()
    if not value:
        raise ValidationError("host is required")
    if len(value) > 253:
        raise ValidationError("host too long")
    try:
        addr = ipaddress.ip_address(value)
        addresses = [addr]
    except ValueError:
        if not _HOSTNAME_RE.match(value):
            raise ValidationError("invalid hostname/IP format") from None
        try:
            infos = socket.getaddrinfo(value, None, family=socket.AF_INET)
        except OSError as exc:
            raise ValidationError(f"could not resolve host: {exc}") from None
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        if not addresses:
            raise ValidationError("host did not resolve to any address")
    if not allow_public:
        for addr in addresses:
            if not isinstance(addr, ipaddress.IPv4Address) or not is_lan_scannable(addr):
                raise ValidationError(
                    f"{value!r} resolves to {addr} which is not a private/LAN address -- "
                    "refused (SSRF protection); enable nodes.remote_connect.allow_public_manual_add "
                    "to override for a deliberately-public target"
                )
    return value


def validate_cloudflare_hostname(value: str) -> str:
    """For a cloudflare_ssh target: syntactic hostname validation ONLY --
    no private-IP requirement, since the actual network target is
    Cloudflare's own edge (the whole point of the tunnel), not an
    address this host connects to directly."""
    value = (value or "").strip()
    if not value or len(value) > 253 or not _HOSTNAME_RE.match(value):
        raise ValidationError("invalid Cloudflare Access hostname")
    return value


def validate_username(value: str) -> str:
    value = (value or "").strip()
    if not _USERNAME_RE.match(value):
        raise ValidationError("username must match ^[A-Za-z0-9_.-]{1,32}$")
    return value


def validate_node_id(value: str) -> str:
    value = (value or "").strip()
    if not _NODE_ID_RE.match(value):
        raise ValidationError("node_id must match ^[A-Za-z0-9_-]{1,64}$")
    return value


def validate_port(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise ValidationError("port must be an integer") from None
    if not (1 <= port <= 65535):
        raise ValidationError("port out of range")
    return port


@dataclass
class SshCredential:
    """Held in memory only, for the duration of one bootstrap/test call.
    __repr__/__str__ redact everything except the username -- so an
    accidental `logging.info("...%s", credential)` or an unhandled
    exception's own repr in a traceback can never leak a secret."""
    username: str
    password: str | None = None
    private_key_pem: str | None = None
    key_passphrase: str | None = None

    def __post_init__(self) -> None:
        if bool(self.password) == bool(self.private_key_pem):
            # exactly one of the two must be set -- "ưu tiên SSH key"
            # (task spec) is enforced by the caller choosing which field
            # to populate; this object itself just refuses an ambiguous
            # or empty credential outright.
            raise ValidationError("exactly one of password or private_key_pem must be provided")

    def __repr__(self) -> str:
        kind = "key" if self.private_key_pem else "password"
        return f"SshCredential(username={self.username!r}, auth={kind}, <redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class SshTarget:
    transport_type: str  # lan_ssh | cloudflare_ssh
    host: str  # direct IP/hostname (lan_ssh) or the Cloudflare Access SSH hostname (cloudflare_ssh)
    port: int
    username: str


def build_ssh_argv(target: SshTarget, *, known_hosts_file: Path, strict_host_key_checking: bool,
                   identity_file: Path | None = None, remote_command: list[str] | None = None,
                   connect_timeout: int = 10, batch_mode: bool = True) -> list[str]:
    """Pure function, fully unit-testable without any real network/SSH --
    always an argv LIST, never a shell string (see this module's own
    docstring). `strict_host_key_checking=False` is used ONLY for the
    very first key-probe step (ssh-keyscan doesn't need it at all, but
    this flag exists for symmetry/tests) -- every REAL connection this
    module makes after that always passes True against our own pinned
    known_hosts_file."""
    argv = [
        "ssh",
        "-o", f"ConnectTimeout={int(connect_timeout)}",
        "-o", f"StrictHostKeyChecking={'yes' if strict_host_key_checking else 'no'}",
        "-o", f"UserKnownHostsFile={known_hosts_file}",
        "-o", "PasswordAuthentication=" + ("no" if batch_mode else "yes"),
        "-o", "BatchMode=" + ("yes" if batch_mode else "no"),
        "-o", "NumberOfPasswordPrompts=1",
    ]
    if target.transport_type == TRANSPORT_CLOUDFLARE_SSH:
        argv += ["-o", f"ProxyCommand={CLOUDFLARE_PROXY_COMMAND}"]
    if identity_file is not None:
        argv += ["-i", str(identity_file)]
    argv += ["-p", str(target.port), f"{target.username}@{target.host}"]
    if remote_command:
        argv += remote_command
    return argv


def build_keyscan_argv(target: SshTarget) -> list[str]:
    argv = ["ssh-keyscan", "-T", "10", "-p", str(target.port)]
    if target.transport_type == TRANSPORT_CLOUDFLARE_SSH:
        argv += ["-O", f"ProxyCommand={CLOUDFLARE_PROXY_COMMAND}"]
    argv += [target.host]
    return argv


# -- host-key pinning ---------------------------------------------------
# A tiny store, keyed by (transport_type, host, port) BEFORE a node_id
# exists (during Test Connection, ahead of any successful bootstrap) --
# separate from connection_store.py's own per-node host_key_fingerprint
# column, which is where the fingerprint ends up permanently once a
# bootstrap actually succeeds and a real node_id is assigned.

def _target_key(target: SshTarget) -> str:
    return f"{target.transport_type}:{target.host}:{target.port}"


@dataclass
class HostKeyProbeResult:
    ok: bool
    fingerprint: str | None = None
    key_line: str | None = None
    error: str | None = None
    error_class: str | None = None  # cloudflared_missing | access_login_required | dns_tls_fail | unreachable


_ERROR_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("access_login_required", ("please open the following url", "failed to authenticate", "login required",
                               "unable to find token")),
    ("dns_tls_fail", ("no such host", "could not resolve", "certificate", "x509", "tls handshake")),
    ("unreachable", ("connection refused", "connection timed out", "no route to host", "network is unreachable")),
)


def _classify_stderr(stderr: str) -> str | None:
    lowered = stderr.lower()
    for error_class, patterns in _ERROR_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return error_class
    return None


class HostKeyStore:
    """In-memory pending-trust cache (per DiscoveryService/dashboard
    process lifetime) -- a host key an operator has explicitly approved
    via "Trust & pin" during THIS session's Test Connection flow, before
    a node_id/ConnectionStore row exists for it yet. Once bootstrap
    succeeds, the fingerprint is copied into connection_store.py's
    permanent per-node record; this cache is only the staging area."""

    def __init__(self) -> None:
        self._pending: dict[str, str] = {}

    def trust(self, target: SshTarget, fingerprint: str) -> None:
        self._pending[_target_key(target)] = fingerprint

    def pinned_for(self, target: SshTarget) -> str | None:
        return self._pending.get(_target_key(target))

    def forget(self, target: SshTarget) -> None:
        self._pending.pop(_target_key(target), None)


def probe_host_key(target: SshTarget, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                   timeout: float = 15.0) -> HostKeyProbeResult:
    if target.transport_type == TRANSPORT_CLOUDFLARE_SSH and shutil.which("cloudflared") is None:
        return HostKeyProbeResult(ok=False, error="cloudflared binary not found on this controller",
                                  error_class="cloudflared_missing")
    argv = build_keyscan_argv(target)
    try:
        result = runner(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return HostKeyProbeResult(ok=False, error="ssh-keyscan not found", error_class="unreachable")
    except subprocess.TimeoutExpired:
        return HostKeyProbeResult(ok=False, error="timed out probing host key", error_class="unreachable")
    key_line = next((line for line in result.stdout.splitlines() if line and not line.startswith("#")), None)
    if not key_line:
        error_class = _classify_stderr(result.stderr) or "unreachable"
        return HostKeyProbeResult(ok=False, error=(result.stderr.strip() or "no host key returned"),
                                  error_class=error_class)
    fingerprint = _fingerprint_of(key_line, runner=runner)
    if fingerprint is None:
        return HostKeyProbeResult(ok=False, error="could not compute host key fingerprint", error_class="unreachable")
    return HostKeyProbeResult(ok=True, fingerprint=fingerprint, key_line=key_line)


def _fingerprint_of(key_line: str, *, runner: Callable[..., subprocess.CompletedProcess]) -> str | None:
    try:
        result = runner(["ssh-keygen", "-lf", "/dev/stdin"], input=key_line, capture_output=True,
                        text=True, timeout=5.0)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(SHA256:[A-Za-z0-9+/]+)", result.stdout)
    return match.group(1) if match else None


@dataclass
class ConnectionTestResult:
    stage: str  # cloudflared_missing | host_key_new | host_key_mismatch | auth_fail | unreachable | ok | access_login_required | dns_tls_fail
    ok: bool
    fingerprint: str | None = None
    detail: str | None = None


def test_connection(target: SshTarget, *, known_hosts_dir: Path, host_key_store: HostKeyStore,
                    pinned_fingerprint: str | None = None,
                    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> ConnectionTestResult:
    """Host-key probe + pin check ONLY -- deliberately does not attempt a
    real authenticated connection (that needs a credential, which this
    step doesn't require the operator to have typed in yet -- matches
    the task's own "Test Connection trước khi bootstrap" as a cheap,
    credential-free first check). auth_fail is only ever returned by
    test_connection_with_credential below."""
    probe = probe_host_key(target, runner=runner)
    if not probe.ok:
        return ConnectionTestResult(stage=probe.error_class or "unreachable", ok=False, detail=probe.error)

    known = pinned_fingerprint or host_key_store.pinned_for(target)
    if known is None:
        return ConnectionTestResult(stage="host_key_new", ok=False, fingerprint=probe.fingerprint,
                                    detail="host key not yet trusted -- explicitly approve it (Trust & pin) "
                                          "before continuing")
    if known != probe.fingerprint:
        return ConnectionTestResult(stage="host_key_mismatch", ok=False, fingerprint=probe.fingerprint,
                                    detail=f"pinned fingerprint {known} does not match presented "
                                          f"{probe.fingerprint} -- possible MITM or the host was reinstalled; "
                                          "use 'Update pinned key' to explicitly re-trust if this is expected")
    return ConnectionTestResult(stage="ok", ok=True, fingerprint=probe.fingerprint)


def test_connection_with_credential(target: SshTarget, credential: SshCredential, *, known_hosts_dir: Path,
                                    pinned_fingerprint: str,
                                    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                                    timeout: float = 20.0) -> ConnectionTestResult:
    """A real (but harmless -- `true`) authenticated connection, AFTER
    the host key is already pinned and matches. Only ever called once
    the operator has supplied a credential and Test Connection's
    key-only stage above already returned ok. Re-probes the host key
    fresh (rather than trusting a value from an earlier call) and
    refuses to proceed on any mismatch against `pinned_fingerprint` --
    the one and only path a known_hosts entry ever gets written from."""
    probe = probe_host_key(target, runner=runner)
    if not probe.ok:
        return ConnectionTestResult(stage=probe.error_class or "unreachable", ok=False, detail=probe.error)
    if probe.fingerprint != pinned_fingerprint:
        return ConnectionTestResult(stage="host_key_mismatch", ok=False, fingerprint=probe.fingerprint,
                                    detail=f"pinned fingerprint {pinned_fingerprint} does not match presented "
                                          f"{probe.fingerprint}")
    known_hosts_file = _write_pinned_known_hosts(known_hosts_dir, target, probe.key_line)
    identity_file = None
    try:
        if credential.private_key_pem:
            identity_file = _write_temp_key(credential.private_key_pem)
        argv = build_ssh_argv(target, known_hosts_file=known_hosts_file, strict_host_key_checking=True,
                              identity_file=identity_file, remote_command=["true"],
                              batch_mode=bool(credential.private_key_pem))
        stdout, stderr, code = _run_ssh(argv, credential, runner=runner, timeout=timeout)
    finally:
        if identity_file is not None:
            _shred(identity_file)
    if code == 0:
        return ConnectionTestResult(stage="ok", ok=True, fingerprint=pinned_fingerprint)
    lowered = stderr.lower()
    if "permission denied" in lowered or "authentication failed" in lowered:
        return ConnectionTestResult(stage="auth_fail", ok=False, detail="SSH authentication was rejected")
    error_class = _classify_stderr(stderr) or "unreachable"
    return ConnectionTestResult(stage=error_class, ok=False, detail=stderr.strip()[:500])


def _write_pinned_known_hosts(known_hosts_dir: Path, target: SshTarget, key_line: str | None) -> Path:
    """Writes the FRESHLY-probed key line (never a cached/stale one --
    every caller re-probes via probe_host_key immediately before this)
    into a per-target file this module fully owns -- never the
    operator's real ~/.ssh/known_hosts. `ssh-keyscan`'s own output line
    is already in known_hosts format (`host keytype base64key`), used
    verbatim; StrictHostKeyChecking=yes against this file is what makes
    the fingerprint check above actually load-bearing for the real SSH
    connection that follows, not just an advisory comparison."""
    known_hosts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = known_hosts_dir / f"{_target_key(target).replace(':', '_').replace('/', '_')}.known_hosts"
    if key_line:
        path.write_text(key_line + "\n")
        path.chmod(0o600)
    return path


def _write_temp_key(private_key_pem: str) -> Path:
    fd, name = tempfile.mkstemp(prefix="tmcp-sshkey-")
    os.close(fd)
    path = Path(name)
    path.write_text(private_key_pem if private_key_pem.endswith("\n") else private_key_pem + "\n")
    path.chmod(0o600)
    return path


def _shred(path: Path) -> None:
    try:
        size = path.stat().st_size
        with open(path, "r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    finally:
        path.unlink(missing_ok=True)


def _run_ssh(argv: list[str], credential: SshCredential, *,
            runner: Callable[..., subprocess.CompletedProcess], timeout: float) -> tuple[str, str, int]:
    """Password auth without paramiko/sshpass (neither is a dependency of
    this project): drive the real `ssh` client over a pty and answer its
    own password prompt exactly once. Key-based auth needs none of this
    -- BatchMode=yes and -i already handle it non-interactively, this
    helper is only exercised for password credentials."""
    if credential.private_key_pem:
        result = runner(argv, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode
    return _run_ssh_with_password(argv, credential.password or "", timeout=timeout)


def _run_ssh_with_password(argv: list[str], password: str, *, timeout: float) -> tuple[str, str, int]:
    import pty
    import select
    import time as _time

    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd, stderr=subprocess.PIPE,
                                   close_fds=True, text=False)
        os.close(slave_fd)
        slave_fd = -1
        output = b""
        deadline = _time.monotonic() + timeout
        password_sent = False
        while _time.monotonic() < deadline:
            if process.poll() is not None:
                break
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if not password_sent and (b"assword:" in output or b"Password:" in output):
                    os.write(master_fd, password.encode() + b"\n")
                    password_sent = True
        try:
            _, stderr = process.communicate(timeout=max(1.0, deadline - _time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()
        return output.decode(errors="replace"), (stderr or b"").decode(errors="replace"), process.returncode or 0
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)


# -- Linux bootstrap: ONE fixed, server-authored script ------------------
# The browser NEVER sends this text -- only node_id/controller_url/
# display_name, substituted via the script's own POSITIONAL ARGS
# ($1/$2/$3), never string-spliced into the script body. Mirrors
# deploy/install-node-agent.sh's own steps (pip install this same
# package from the controller's own git remote is out of scope for a
# one-click flow -- this assumes the target already has network access
# to install terminal-mcp itself via pip from PyPI/git, exactly like a
# manual deploy would).
BOOTSTRAP_SCRIPT_LINUX = r"""#!/bin/sh
set -eu
NODE_ID="$1"; CONTROLLER_URL="$2"; TOKEN="$3"; BIND_HOST="$4"
echo "== terminal-node-agent bootstrap: node_id=$NODE_ID host=$BIND_HOST =="
if ! command -v terminal-node-agent >/dev/null 2>&1; then
  if command -v pip3 >/dev/null 2>&1; then PIP=pip3; else PIP=pip; fi
  "$PIP" install --user --upgrade terminal-mcp >/dev/null 2>&1 || {
    echo "ERROR: could not install terminal-mcp via pip -- install it manually, see docs/multi-node.md" >&2
    exit 1
  }
fi
mkdir -p "$HOME/.config/terminal-mcp"
umask 077
printf '%s' "$TOKEN" > "$HOME/.config/terminal-mcp/node-agent.token"
chmod 600 "$HOME/.config/terminal-mcp/node-agent.token"
echo "Token written to ~/.config/terminal-mcp/node-agent.token (0600)."
echo "Start it (foreground, for this bootstrap's own verification) with:"
echo "  TERMINAL_MCP_NODE_TOKEN=\"$TOKEN\" nohup terminal-node-agent --node-id \"$NODE_ID\" \\"
echo "    --controller-url \"$CONTROLLER_URL\" --host \"$BIND_HOST\" --port 8790 \\"
echo "    > \"$HOME/.config/terminal-mcp/node-agent.log\" 2>&1 &"
TERMINAL_MCP_NODE_TOKEN="$TOKEN" nohup terminal-node-agent --node-id "$NODE_ID" \
  --controller-url "$CONTROLLER_URL" --host "$BIND_HOST" --port 8790 \
  > "$HOME/.config/terminal-mcp/node-agent.log" 2>&1 &
disown || true
sleep 2
echo "== bootstrap script done =="
"""


@dataclass
class BootstrapResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    detail: str | None = None


def run_linux_bootstrap(target: SshTarget, credential: SshCredential, *, node_id: str, controller_url: str,
                        token: str, bind_host: str, known_hosts_dir: Path, pinned_fingerprint: str,
                        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                        timeout: float = 60.0) -> BootstrapResult:
    validate_node_id(node_id)
    probe = probe_host_key(target, runner=runner)
    if not probe.ok:
        return BootstrapResult(ok=False, stdout="", stderr=probe.error or "", returncode=-1,
                               detail=probe.error_class or "unreachable")
    if probe.fingerprint != pinned_fingerprint:
        return BootstrapResult(ok=False, stdout="", stderr="", returncode=-1, detail="host_key_mismatch")
    known_hosts_file = _write_pinned_known_hosts(known_hosts_dir, target, probe.key_line)
    identity_file = None
    try:
        if credential.private_key_pem:
            identity_file = _write_temp_key(credential.private_key_pem)
        # Positional args ONLY -- shlex.quote defends the (unlikely, since
        # these are all already-validated) case of a value containing a
        # space/metacharacter; the values themselves never touch a shell
        # string on THIS side either, they're separate argv/stdin-script
        # elements throughout.
        remote_args = [shlex.quote(node_id), shlex.quote(controller_url), shlex.quote(token),
                       shlex.quote(bind_host)]
        argv = build_ssh_argv(target, known_hosts_file=known_hosts_file, strict_host_key_checking=True,
                              identity_file=identity_file, remote_command=["sh", "-s", "--", *remote_args],
                              batch_mode=bool(credential.private_key_pem))
        stdout, stderr, code = _run_ssh_stdin(argv, credential, BOOTSTRAP_SCRIPT_LINUX, runner=runner, timeout=timeout)
    finally:
        if identity_file is not None:
            _shred(identity_file)
    return BootstrapResult(ok=(code == 0), stdout=stdout, stderr=stderr, returncode=code)


def _run_ssh_stdin(argv: list[str], credential: SshCredential, script: str, *,
                   runner: Callable[..., subprocess.CompletedProcess], timeout: float) -> tuple[str, str, int]:
    if credential.private_key_pem:
        result = runner(argv, input=script, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.stderr, result.returncode
    return _run_ssh_with_password_and_stdin(argv, credential.password or "", script, timeout=timeout)


def _run_ssh_with_password_and_stdin(argv: list[str], password: str, script: str, *, timeout: float) -> tuple[str, str, int]:
    # Same pty-driven password handoff as _run_ssh_with_password, plus
    # feeding the fixed bootstrap script on stdin once the password
    # prompt is answered (ssh's own pty merges stdin for both once the
    # remote `sh -s` starts reading).
    import pty
    import select
    import time as _time

    master_fd, slave_fd = pty.openpty()
    try:
        process = subprocess.Popen(argv, stdin=slave_fd, stdout=slave_fd, stderr=subprocess.PIPE,
                                   close_fds=True, text=False)
        os.close(slave_fd)
        slave_fd = -1
        output = b""
        deadline = _time.monotonic() + timeout
        password_sent = False
        script_sent = False
        while _time.monotonic() < deadline:
            if process.poll() is not None:
                break
            ready, _, _ = select.select([master_fd], [], [], 0.5)
            if master_fd in ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if not password_sent and (b"assword:" in output):
                    os.write(master_fd, password.encode() + b"\n")
                    password_sent = True
                elif password_sent and not script_sent:
                    os.write(master_fd, script.encode())
                    os.write(master_fd, b"\x04")  # EOT -- end of stdin for the remote `sh -s`
                    script_sent = True
        try:
            _, stderr = process.communicate(timeout=max(1.0, deadline - _time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            _, stderr = process.communicate()
        return output.decode(errors="replace"), (stderr or b"").decode(errors="replace"), process.returncode or 0
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)


# -- Windows: always honest manual guidance ------------------------------

def windows_bootstrap_guidance(*, node_id: str, controller_url: str, token: str, hostname: str) -> dict[str, Any]:
    """Never claims to have performed a live install (see this module's
    own docstring) -- always returns copy-paste manual instructions
    pointing at the SAME deploy/install-node-agent.ps1 this project's
    config-driven onboarding already uses (dashboard.py's existing
    node_generate_onboarding route), so there is exactly one Windows
    install path, not a second one invented for this feature."""
    validate_node_id(node_id)
    command = (
        f".\\deploy\\install-node-agent.ps1 -ControllerUrl \"{controller_url}\" "
        f"-NodeId \"{node_id}\" -Token \"{token}\""
    )
    return {
        "status": "manual_required",
        "reason": "no live WinRM/PowerShell-remoting bootstrap is implemented -- this project has no way to "
                  "verify one against a real Windows host, so it never pretends to auto-install (see "
                  "docs/multi-node.md's Windows node support section)",
        "instructions": [
            f"1. RDP/console vào {hostname} (hoặc máy Windows đích).",
            "2. Copy deploy/install-node-agent.ps1 từ repo này sang máy đó (bất kỳ cách nào -- USB, share, scp nếu có OpenSSH).",
            "3. Mở PowerShell (Run as Administrator) và chạy lệnh dưới đây.",
        ],
        "install_command": command,
        "token": token,
    }
