"""Controller-managed, node-scoped SSH profile and strict verification."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import shlex
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from .node_client import NodeClientError
from .remote_connect import validate_hostname_or_ip, validate_node_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NodeSSHService:
    """Resolve registered nodes, manage private keys and require a real SSH proof."""

    def __init__(self, controller: Any, state_dir: str | Path, *, ssh: str = "ssh",
                 keygen: str = "ssh-keygen", timeout: float = 6.0, audit: Any = None) -> None:
        self.controller = controller
        self.state_dir = Path(state_dir)
        if self.state_dir.is_symlink():
            raise ValueError("SSH state directory must not be a symbolic link")
        self.ssh = ssh
        self.keygen = keygen
        self.timeout = min(max(float(timeout), 1.0), 10.0)
        self.audit = audit
        self._lock = threading.RLock()

    def _paths(self, node_id: str) -> tuple[Path, Path, Path]:
        validate_node_id(node_id)
        if not _SAFE_ID.fullmatch(node_id):
            raise ValueError("invalid node_id")
        if self.state_dir.is_symlink():
            raise ValueError("SSH state directory must not be a symbolic link")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        node_dir = self.state_dir / node_id
        if node_dir.is_symlink():
            raise ValueError("SSH node profile directory must not be a symbolic link")
        node_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(node_dir, 0o700)
        return node_dir / "id_ed25519", node_dir / "known_hosts", node_dir / "profile.json"

    @staticmethod
    def _fingerprint(public_key: str) -> str:
        parts = public_key.strip().split()
        if len(parts) < 2:
            raise ValueError("invalid host public key")
        blob = base64.b64decode(parts[1], validate=True)
        digest = base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
        return f"SHA256:{digest}"

    def _node_target(self, node_id: str) -> tuple[Any, Any, str, int]:
        node = self.controller.registry.get(node_id)
        client = self.controller.client_for(node_id)
        if node is None:
            raise ValueError("node_not_found")
        if client is None:
            raise ConnectionError("node_offline")
        parsed = urlsplit(str(node.endpoint or ""))
        host = parsed.hostname
        if not host:
            raise ValueError("registered_node_endpoint_has_no_host")
        validate_hostname_or_ip(host)
        return node, client, host, 22

    def _load_profile(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _store_profile(self, path: Path, profile: dict[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=".profile-", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(profile, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    def _ensure_key(self, key_path: Path) -> str:
        public_path = Path(str(key_path) + ".pub")
        if key_path.is_symlink() or public_path.is_symlink():
            raise ValueError("SSH key files must not be symbolic links")
        if not key_path.exists() and public_path.exists():
            raise ValueError("public key exists without its private key; refusing to replace it")
        if not key_path.exists():
            subprocess.run([self.keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
                           capture_output=True, timeout=self.timeout, check=True)
            os.chmod(key_path, 0o600)
            os.chmod(public_path, 0o600)
        elif not public_path.exists():
            derived = subprocess.run([self.keygen, "-y", "-f", str(key_path)], capture_output=True,
                                     text=True, timeout=self.timeout, check=True).stdout.strip()
            if not derived:
                raise ValueError("could not derive public key from controller private key")
            public_path.write_text(derived + "\n", encoding="utf-8")
            os.chmod(public_path, 0o600)
            os.chmod(key_path, 0o600)
        else:
            if not key_path.is_file() or not public_path.is_file():
                raise ValueError("SSH key path is not a regular file")
            os.chmod(key_path, 0o600)
            os.chmod(public_path, 0o600)
        parts = public_path.read_text(encoding="utf-8").strip().split()
        if len(parts) < 2:
            raise ValueError("invalid generated SSH public key")
        controller_id = str(getattr(self.controller, "local_node_id", "controller"))
        expected_prefix = f"terminal-mcp:{controller_id}-{key_path.parent.name}:"
        existing_comment = parts[2] if len(parts) >= 3 else ""
        comment = (existing_comment if re.fullmatch(re.escape(expected_prefix) + r"\d{8}", existing_comment)
                   else f"{expected_prefix}{datetime.now(timezone.utc):%Y%m%d}")
        public_path.write_text(f"{parts[0]} {parts[1]} {comment}\n", encoding="utf-8")
        os.chmod(public_path, 0o600)
        return f"{parts[0]} {parts[1]} {comment}"

    def status(self, node_id: str) -> dict[str, Any]:
        key_path, known_hosts, profile_path = self._paths(node_id)
        profile = self._load_profile(profile_path)
        try:
            node, client, host, port = self._node_target(node_id)
            result = client.ssh_manage("status")
        except NodeClientError as exc:
            status = "unsupported" if exc.http_status == 404 else "auth_failed" if exc.http_status in {401, 403} else "offline"
            return {"node_id": node_id, "status": status, "error": status,
                    "configured": profile.get("status") in {"configured", "verified"},
                    "verified": bool(profile.get("verified")),
                    "connect_available": bool(profile.get("verified")),
                    "address": profile.get("address"), "username": profile.get("username"),
                    "port": profile.get("port"), "host_key_fingerprint": profile.get("host_key_fingerprint"),
                    "verified_at": profile.get("verified_at"),
                    "detail": "node SSH management operation is not available" if status == "unsupported" else str(exc)}
        except (ValueError, ConnectionError) as exc:
            status = "offline" if str(exc) == "node_offline" else "error"
            return {"node_id": node_id, "status": status, "error": str(exc),
                    "configured": profile.get("status") in {"configured", "verified"},
                    "verified": bool(profile.get("verified")),
                    "connect_available": bool(profile.get("verified")),
                    "address": profile.get("address"), "username": profile.get("username"),
                    "port": profile.get("port"), "host_key_fingerprint": profile.get("host_key_fingerprint"),
                    "verified_at": profile.get("verified_at")}
        stored_status = profile.get("status")
        observed_status = stored_status or ("not_configured" if result.get("status") == "ready"
                                            else result.get("status", "unknown"))
        current_fingerprint = None
        if result.get("host_public_key"):
            try:
                current_fingerprint = self._fingerprint(str(result["host_public_key"]))
            except (ValueError, TypeError):
                observed_status = "host_key_mismatch"
        if (profile.get("host_key_fingerprint") and result.get("status") == "ready"
                and current_fingerprint != profile.get("host_key_fingerprint")):
            observed_status = "host_key_mismatch"
        elif result.get("status") not in {"ready", "configured", "verified"}:
            observed_status = str(result.get("status") or "error")
        return {"node_id": node_id,
                "status": observed_status,
                "configured": stored_status in {"configured", "verified"},
                "verified": bool(profile.get("verified")) and observed_status == "verified",
                "connect_available": bool(profile.get("verified")) and observed_status != "host_key_mismatch"
                and key_path.exists() and known_hosts.exists(),
                "address": profile.get("address", host), "username": profile.get("username", result.get("username")),
                "port": profile.get("port", result.get("port", port)),
                "host_key_fingerprint": profile.get("host_key_fingerprint"),
                "verified_at": profile.get("verified_at"),
                "error": profile.get("error") or result.get("error"),
                "detail": result.get("reason"),
                "agent_preflight": result.get("status"),
                "private_key_present": key_path.exists(),
                "pinned_host_key": known_hosts.exists()}

    def setup(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            key_path, known_hosts, profile_path = self._paths(node_id)
            try:
                node, client, host, port = self._node_target(node_id)
                preflight = client.ssh_manage("status")
                if preflight.get("status") != "ready":
                    return {"node_id": node_id, **preflight}
                pub = self._ensure_key(key_path)
                host_public_key = str(preflight.get("host_public_key") or "")
                fingerprint = self._fingerprint(host_public_key)
                if known_hosts.exists():
                    previous = known_hosts.read_text(encoding="utf-8").split()
                    if len(previous) < 3 or self._fingerprint(" ".join(previous[1:])) != fingerprint:
                        profile = self._load_profile(profile_path)
                        profile.update(status="host_key_mismatch", verified=False, error="host_key_mismatch")
                        self._store_profile(profile_path, profile)
                        return {"node_id": node_id, "status": "host_key_mismatch", "error": "host_key_mismatch"}
                else:
                    host_pattern = host if int(preflight.get("port") or port) == 22 else f"[{host}]:{int(preflight.get('port') or port)}"
                    known_hosts.write_text(f"{host_pattern} {host_public_key.strip()}\n", encoding="utf-8")
                    os.chmod(known_hosts, 0o600)
                configured = client.ssh_manage("setup", pub)
                if configured.get("status") != "configured":
                    return {"node_id": node_id, **configured}
                username = configured.get("username")
                ssh_port = int(configured.get("port") or port)
                expected_hostname = configured.get("hostname") or preflight.get("hostname")
                previous_profile = self._load_profile(profile_path)
                same_verified_identity = (
                    previous_profile.get("status") == "verified"
                    and previous_profile.get("verified") is True
                    and previous_profile.get("verified_at")
                    and previous_profile.get("address") == host
                    and previous_profile.get("username") == username
                    and previous_profile.get("port") == ssh_port
                    and previous_profile.get("host_key_fingerprint") == fingerprint
                    and str(previous_profile.get("expected_hostname") or "").casefold()
                    == str(expected_hostname or "").casefold()
                )
                profile = {"node_id": node_id, "address": host, "username": username,
                           "port": ssh_port, "host_key_fingerprint": fingerprint,
                           "expected_hostname": expected_hostname,
                           "status": "verified" if same_verified_identity else "configured",
                           "verified": bool(same_verified_identity)}
                if same_verified_identity:
                    profile["verified_at"] = previous_profile["verified_at"]
                    profile["verified_hostname"] = previous_profile.get("verified_hostname")
                self._store_profile(profile_path, profile)
                return {"node_id": node_id, **profile}
            except NodeClientError as exc:
                status = "unsupported" if exc.http_status == 404 else "auth_failed" if exc.http_status in {401, 403} else "offline"
                return {"node_id": node_id, "status": status, "error": status,
                        "detail": "node SSH management operation is not available" if status == "unsupported" else str(exc)}
            except (ValueError, ConnectionError, OSError, subprocess.SubprocessError) as exc:
                code = "offline" if str(exc) == "node_offline" else "error"
                return {"node_id": node_id, "status": code, "error": code, "detail": str(exc)}

    def revoke(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            key_path, known_hosts, profile_path = self._paths(node_id)
            public_path = Path(str(key_path) + ".pub")
            if not public_path.exists():
                return {"node_id": node_id, "status": "not_configured", "revoked": False}
            try:
                _node, client, _host, _port = self._node_target(node_id)
                result = client.ssh_manage("revoke", public_path.read_text(encoding="utf-8").strip())
            except NodeClientError as exc:
                status = "unsupported" if exc.http_status == 404 else "auth_failed" if exc.http_status in {401, 403} else "offline"
                return {"node_id": node_id, "status": status, "error": status}
            except (ValueError, ConnectionError) as exc:
                status = "offline" if str(exc) == "node_offline" else "error"
                return {"node_id": node_id, "status": status, "error": str(exc)}
            if result.get("status") not in {"removed", "configured"}:
                return {"node_id": node_id, **result}
            # Remove controller secrets/pins only after the node confirms it
            # removed this exact managed public key.
            for path in (key_path, public_path, known_hosts, profile_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            return {"node_id": node_id, "status": "revoked", "revoked": True,
                    "changed": bool(result.get("changed"))}

    def connect(self, node_id: str) -> dict[str, Any]:
        result = self.verify(node_id)
        if result.get("status") != "verified":
            return result
        return {**result, "terminal_path": f"/dashboard/terminal?transport=ssh&node_id={node_id}"}

    def exec(self, node_id: str, argv: list[str], *, timeout: float = 15.0) -> dict[str, Any]:
        if (not isinstance(argv, list) or not argv or len(argv) > 64
                or any(not isinstance(value, str) or not value or chr(0) in value for value in argv)
                or sum(len(value) for value in argv) > 8192):
            return {"node_id": node_id, "status": "error", "error": "invalid_argv"}
        _key_path, _known_hosts, profile_path = self._paths(node_id)
        stored_profile = self._load_profile(profile_path)
        if stored_profile.get("status") != "verified" or not stored_profile.get("verified"):
            return {"node_id": node_id, "status": "not_configured", "error": "ssh_profile_not_verified"}
        checked = self.verify(node_id)
        if checked.get("status") != "verified":
            return {**checked, "status": checked.get("status", "error")}
        key_path, known_hosts, profile_path = self._paths(node_id)
        profile = self._load_profile(profile_path)
        if profile.get("status") != "verified" or not profile.get("verified"):
            return {"node_id": node_id, "status": "not_configured", "error": "ssh_profile_not_verified"}
        host, user, port = str(profile.get("address") or ""), str(profile.get("username") or ""), int(profile.get("port") or 22)
        try:
            validate_hostname_or_ip(host)
        except ValueError:
            return {"node_id": node_id, "status": "error", "error": "invalid_registered_address"}
        remote = "exec " + shlex.join(argv)
        ssh_argv = [self.ssh, "-F", os.devnull, "-i", str(key_path), "-p", str(port),
                    "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                    "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", f"{user}@{host}", remote]
        try:
            bounded_timeout = float(timeout)
        except (TypeError, ValueError):
            return {"node_id": node_id, "status": "error", "error": "invalid_timeout"}
        if not math.isfinite(bounded_timeout):
            return {"node_id": node_id, "status": "error", "error": "invalid_timeout"}
        bounded_timeout = min(max(bounded_timeout, 1.0), 30.0)
        try:
            result = subprocess.run(ssh_argv, capture_output=True, text=True, timeout=bounded_timeout, check=False)
        except subprocess.TimeoutExpired:
            outcome = {"node_id": node_id, "status": "timeout", "exit_code": None}
        except OSError:
            outcome = {"node_id": node_id, "status": "unsupported", "error": "ssh_client_missing"}
        else:
            outcome = {"node_id": node_id, "status": "completed" if result.returncode == 0 else "command_failed",
                       "exit_code": result.returncode, "stdout": (result.stdout or "")[:16000],
                       "stderr": (result.stderr or "")[:4000]}
        if self.audit is not None:
            self.audit.record(action="node_ssh_exec", session=None, node_id=node_id,
                              result=str(outcome.get("status")), text=argv[0][:80], source_transport="mcp")
        return outcome

    def connect_argv(self, node_id: str, *, readonly: bool = False) -> list[str]:
        key_path, known_hosts, profile_path = self._paths(node_id)
        profile = self._load_profile(profile_path)
        if profile.get("status") != "verified" or not profile.get("verified"):
            raise ValueError("ssh_profile_not_verified")
        host, user, port = str(profile.get("address") or ""), str(profile.get("username") or ""), int(profile.get("port") or 22)
        validate_hostname_or_ip(host)
        if not key_path.is_file() or not known_hosts.is_file():
            raise ValueError("ssh_profile_incomplete")
        remote = "tmux new-session -A -s tmcp-managed-terminal"
        return [self.ssh, "-F", os.devnull, "-i", str(key_path), "-p", str(port),
                "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", "-tt",
                f"{user}@{host}", remote]

    def verify(self, node_id: str) -> dict[str, Any]:
        key_path, known_hosts, profile_path = self._paths(node_id)
        profile = self._load_profile(profile_path)
        if not profile or not key_path.exists() or not known_hosts.exists():
            return {"node_id": node_id, "status": "not_configured", "verified": False}
        nonce = secrets.token_hex(16)
        host = str(profile.get("address") or "")
        user = str(profile.get("username") or "")
        port = int(profile.get("port") or 22)
        try:
            validate_hostname_or_ip(host)
        except ValueError:
            return {"node_id": node_id, "status": "error", "error": "invalid_registered_address"}
        argv = [self.ssh, "-F", os.devnull, "-i", str(key_path), "-p", str(port), "-o", "IdentityAgent=none",
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5", f"{user}@{host}",
                f"printf '%s\\n' '{nonce}'; id -un; hostname"]
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired:
            return {"node_id": node_id, "status": "timeout", "verified": False}
        except OSError:
            return {"node_id": node_id, "status": "unsupported", "error": "ssh_client_missing", "verified": False}
        if result.returncode != 0:
            text = (result.stderr or "").lower()
            status = "host_key_mismatch" if "host key" in text or "remote host identification" in text else "auth_failed" if "permission denied" in text else "port_closed" if "refused" in text or "timed out" in text or "no route" in text else "error"
            profile.update(status=status, verified=False, error=status)
            self._store_profile(profile_path, profile)
            return {"node_id": node_id, "status": status, "verified": False}
        output = (result.stdout or "").splitlines()
        expected_hostname = str(profile.get("expected_hostname") or "")
        ok = (len(output) >= 3 and output[0].strip() == nonce and output[1].strip() == user
              and bool(output[2].strip()) and (not expected_hostname or output[2].strip().casefold() == expected_hostname.casefold()))
        if not ok:
            profile.update(status="identity_mismatch", verified=False, error="identity_mismatch")
            self._store_profile(profile_path, profile)
            return {"node_id": node_id, "status": "identity_mismatch", "verified": False}
        profile.update(status="verified", verified=True, error=None, verified_hostname=output[2].strip(),
                       verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._store_profile(profile_path, profile)
        return {"node_id": node_id, "status": "verified", "verified": True,
                "address": host, "username": user, "port": port,
                "host_key_fingerprint": profile.get("host_key_fingerprint"), "hostname": output[2].strip()}
