"""Fixed, authenticated node-local operations for controller-managed SSH.

This module never installs/enables sshd, edits sshd policy/firewalls, elevates,
or executes caller-provided commands.  It can only add/remove one validated
controller public key in the effective user's authorized_keys file.
"""
from __future__ import annotations

import base64
import re
import socket
import getpass
import os
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
from pathlib import Path

_LOCK = threading.RLock()
_MANAGED_COMMENT_RE = re.compile(r"^terminal-mcp:[A-Za-z0-9_-]{1,64}-[A-Za-z0-9_-]{1,64}:\d{8}$")
_KEY_TYPES = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384",
              "ecdsa-sha2-nistp521"}


def _validate_public_key(value: str) -> str:
    parts = (value or "").strip().split()
    if len(parts) < 2 or parts[0] not in _KEY_TYPES or len(parts) > 3:
        raise ValueError("invalid managed public key")
    try:
        decoded = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        raise ValueError("invalid managed public key") from None
    if len(decoded) < 4 or len(decoded) > 16384 or len(parts) != 3 or not _MANAGED_COMMENT_RE.fullmatch(parts[2]):
        raise ValueError("invalid managed public key")
    type_length = int.from_bytes(decoded[:4], "big")
    if type_length <= 0 or 4 + type_length > len(decoded) or decoded[4:4 + type_length].decode("ascii", "ignore") != parts[0]:
        raise ValueError("invalid managed public key")
    return f"{parts[0]} {parts[1]} {parts[2]}"


def _atomic_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise PermissionError("authorized_keys path must not be a symbolic link")
    if os.name != "nt":
        owner = os.geteuid()
        parent_stat = path.parent.stat()
        if parent_stat.st_uid != owner or parent_stat.st_mode & 0o022:
            raise PermissionError(".ssh directory ownership or permissions are unsafe")
    if path.exists() and not path.is_file():
        raise PermissionError("authorized_keys is not a regular file")
    if path.exists() and os.name != "nt":
        current = path.stat()
        if current.st_uid != os.geteuid() or current.st_mode & 0o022:
            raise PermissionError("authorized_keys ownership or permissions are unsafe")
    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=".tmcp-authorized-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, min(old_mode, 0o600))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines))
            if lines:
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def add_managed_key(path: str | Path, public_key: str) -> dict[str, object]:
    path = Path(path)
    managed = _validate_public_key(public_key)
    with _LOCK:
        if path.is_symlink() or path.parent.is_symlink():
            raise PermissionError("authorized_keys path must not be a symbolic link")
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        if managed in lines:
            return {"status": "configured", "changed": False}
        # Keep the file byte-content semantics line-oriented and preserve every
        # existing user line, including comments and options.
        _atomic_lines(path, [*lines, managed])
    return {"status": "configured", "changed": True}


def remove_managed_key(path: str | Path, public_key: str) -> dict[str, object]:
    path = Path(path)
    managed = _validate_public_key(public_key)
    with _LOCK:
        if path.is_symlink() or path.parent.is_symlink():
            raise PermissionError("authorized_keys path must not be a symbolic link")
        if not path.exists():
            return {"status": "removed", "changed": False}
        lines = path.read_text(encoding="utf-8").splitlines()
        remaining = [line for line in lines if line != managed]
        changed = len(remaining) != len(lines)
        if changed:
            _atomic_lines(path, remaining)
    return {"status": "removed", "changed": changed}


def _sshd_effective_policy(output: str) -> tuple[bool, int, str | None]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2:
            values[fields[0].casefold()] = fields[1].strip()
    try:
        ports = [int(port) for port in values.get("port", "22").split()]
    except ValueError:
        return False, 22, "sshd reports an invalid listening port."
    if len(ports) != 1 or not 1 <= ports[0] <= 65535:
        return False, 22, "Multiple/non-standard sshd ports need an administrator-managed node profile."
    authorized_files = values.get("authorizedkeysfile", "").split()
    supported_path = any(path in {".ssh/authorized_keys", "~/.ssh/authorized_keys"} for path in authorized_files)
    if values.get("pubkeyauthentication", "yes").casefold() != "yes" or not supported_path:
        return False, ports[0], "sshd policy does not use public keys from this user's .ssh/authorized_keys."
    return True, ports[0], None


def _platform_preflight() -> dict[str, object]:
    if os.name == "nt":
        return {"status": "needs_admin", "platform": "windows", "ssh_server": "unverified",
                "reason": "Windows OpenSSH Server account/ACL setup needs an installed administrator helper."}
    if os.uname().sysname.casefold() not in {"linux", "darwin"}:
        return {"status": "unsupported", "platform": os.uname().sysname.casefold(),
                "reason": "This operating system is not supported by managed SSH setup."}
    platform = os.uname().sysname.casefold()
    sshd = shutil.which("sshd")
    if not sshd:
        return {"status": "needs_admin", "platform": platform, "ssh_server": "missing",
                "reason": "OpenSSH server is not installed; install and enable sshd/sshd service as an administrator."}
    try:
        result = subprocess.run([sshd, "-T", "-C", f"user={getpass.getuser()},host=localhost,addr=127.0.0.1"],
                                capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "needs_admin", "platform": platform, "ssh_server": "unverified",
                "reason": "Could not validate sshd configuration; administrator must inspect the service."}
    if result.returncode != 0:
        return {"status": "needs_admin", "platform": platform, "ssh_server": "invalid_config",
                "reason": "sshd configuration validation failed; no service changes were made."}
    policy_ok, ssh_port, policy_error = _sshd_effective_policy(result.stdout or "")
    if not policy_ok:
        return {"status": "needs_admin", "platform": platform, "ssh_server": "policy_unsupported",
                "reason": policy_error}
    try:
        with socket.create_connection(("127.0.0.1", ssh_port), timeout=1):
            listening = True
    except OSError:
        listening = False
    if not listening:
        return {"status": "needs_admin", "platform": platform, "ssh_server": "not_listening",
                "reason": "sshd is not listening on port 22; enable it with an administrator."}
    if os.geteuid() == 0:
        return {"status": "needs_admin", "platform": platform, "ssh_server": "listening",
                "reason": "Node agent runs as root; a non-root SSH login identity must be configured by an administrator."}
    home = Path.home()
    if not os.access(home, os.W_OK | os.X_OK):
        return {"status": "needs_admin", "platform": platform, "ssh_server": "listening",
                "reason": "The node-agent user cannot manage its authorized_keys; administrator action is required."}
    pub = Path("/etc/ssh/ssh_host_ed25519_key.pub")
    if not pub.is_file():
        pub = Path("/etc/ssh/ssh_host_rsa_key.pub")
    if not pub.is_file():
        return {"status": "needs_admin", "platform": platform, "ssh_server": "listening",
                "reason": "No readable SSH host public key is available for authenticated pinning."}
    return {"status": "ready", "platform": platform, "ssh_server": "listening",
            "username": getpass.getuser(), "hostname": socket.gethostname(),
            "port": ssh_port, "host_public_key": pub.read_text(encoding="utf-8").strip()}


def manage_ssh(action: str, public_key: str | None = None, *, home: str | Path | None = None) -> dict[str, object]:
    """Execute one allowlisted SSH lifecycle operation for this node/user."""
    if action not in {"status", "setup", "revoke"}:
        return {"status": "error", "error": "unsupported_action"}
    preflight = _platform_preflight()
    if action == "status":
        # Do not expose private paths or keys. Host public key is returned only
        # to the authenticated controller for pinning.
        return preflight
    if not public_key:
        return {"status": "error", "error": "public_key_required"}
    try:
        managed = _validate_public_key(public_key)
    except ValueError:
        return {"status": "error", "error": "invalid_public_key"}
    if action == "setup" and preflight.get("status") != "ready":
        return preflight
    if preflight.get("platform") not in {"linux", "darwin"}:
        return preflight
    target_home = Path(home) if home is not None else Path.home()
    try:
        result = (add_managed_key(target_home / ".ssh" / "authorized_keys", managed)
                  if action == "setup" else
                  remove_managed_key(target_home / ".ssh" / "authorized_keys", managed))
    except PermissionError:
        return {"status": "needs_admin", "reason": "Cannot safely update this user's authorized_keys; administrator action is required."}
    except OSError:
        return {"status": "error", "error": "authorized_keys_io_error"}
    return {**result, "platform": preflight.get("platform"), "username": preflight.get("username"),
            "port": preflight.get("port"), "hostname": preflight.get("hostname"),
            "host_public_key": preflight.get("host_public_key")}
