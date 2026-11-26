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
import socket
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

from .node_client import NodeClientError
from .config import ManagedSSHRouteConfig
from .remote_connect import validate_hostname_or_ip, validate_node_id

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NodeSSHService:
    """Resolve registered nodes, manage private keys and require a real SSH proof."""

    def __init__(self, controller: Any, state_dir: str | Path, *, ssh: str = "ssh",
                 keygen: str = "ssh-keygen", timeout: float = 6.0, audit: Any = None,
                 routes: dict[str, ManagedSSHRouteConfig] | None = None) -> None:
        self.controller = controller
        self.state_dir = Path(state_dir)
        if self.state_dir.is_symlink():
            raise ValueError("SSH state directory must not be a symbolic link")
        self.ssh = ssh
        self.keygen = keygen
        self.timeout = min(max(float(timeout), 1.0), 10.0)
        self.audit = audit
        self.routes = dict(routes or {})
        self._lock = threading.RLock()

    @staticmethod
    def configured_routes(config: Any) -> dict[str, ManagedSSHRouteConfig]:
        return {node.node_id: node.ssh_route for node in config.nodes.remote_nodes
                if node.ssh_route is not None}

    def _route_binding(self, node_id: str) -> str | None:
        route = self.routes.get(node_id)
        if route is None:
            return None
        return hashlib.sha256(json.dumps({"node_id": node_id, **asdict(route)},
                                         sort_keys=True).encode()).hexdigest()

    def _ssh_route(self, node_id: str, profile: dict[str, Any], known_hosts: Path,
                   preflight: dict[str, Any] | None = None) -> tuple[str, int, list[str]]:
        """Revalidate the authenticated agent and existing pin, never trust a route.

        Profile address/port remain the original pin alias. A transport override
        must not rewrite keys, known_hosts, registry or the agent's HTTP endpoint.
        """
        route = self.routes.get(node_id)
        if route is None:
            return str(profile.get("address") or ""), int(profile.get("port") or 22), []
        node, client, registered_host, _ = self._node_target(node_id)
        if (node.endpoint != route.registered_endpoint
                or getattr(client, "base_url", None) != route.registered_endpoint.rstrip("/")
                or getattr(node, "id", None) != node_id):
            raise ValueError("ssh_route_registry_mismatch")
        observed = preflight if preflight is not None else client.ssh_manage("status")
        if (observed.get("status") != "ready" or observed.get("node_id") != node_id
                or observed.get("username") != route.username
                or observed.get("hostname") != route.hostname
                or self._fingerprint(str(observed.get("host_public_key") or "")) != route.host_key_fingerprint):
            raise ValueError("ssh_route_agent_identity_mismatch")
        key_path = known_hosts.parent / "id_ed25519"
        if (profile.get("node_id") != node_id or profile.get("address") != registered_host
                or profile.get("username") != route.username
                or observed.get("port") != profile.get("port")
                or profile.get("expected_hostname") != route.hostname
                or profile.get("host_key_fingerprint") != route.host_key_fingerprint
                or type(profile.get("port")) is not int
                or not 1 <= profile["port"] <= 65535
                or key_path.is_symlink() or not key_path.is_file()
                or known_hosts.is_symlink() or not known_hosts.is_file()):
            raise ValueError("ssh_route_existing_profile_mismatch")
        alias = registered_host if profile["port"] == 22 else f"[{registered_host}]:{profile['port']}"
        lines = known_hosts.read_text(encoding="utf-8").strip().splitlines()
        parts = lines[0].split() if len(lines) == 1 else []
        if (len(parts) < 3 or parts[0] != alias
                or self._fingerprint(" ".join(parts[1:])) != route.host_key_fingerprint):
            raise ValueError("ssh_route_existing_pin_mismatch")
        route_error = self._registered_route_diagnostic(route.address, route.port)
        if route_error:
            raise ValueError(str(route_error["error"]))
        # The alias is the already pinned identity, not the alternate IP or a
        # browser-supplied hostname. Disable OpenSSH's automatic key updates.
        return route.address, route.port, ["-o", f"HostKeyAlias={alias}", "-o", "UpdateHostKeys=no"]

    @staticmethod
    def _route_failure(node_id: str, exc: Exception) -> dict[str, Any]:
        return {"node_id": node_id, "status": "route_binding_failed", "error": "route_binding_failed",
                "detail": str(exc), "verified": False, "connect_available": False}

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

    def _registered_route_diagnostic(self, host: str, port: int) -> dict[str, Any] | None:
        """Fail closed when the registered address does not reach node OpenSSH."""
        try:
            with socket.create_connection((host, port), timeout=min(self.timeout, 2.0)) as stream:
                stream.settimeout(min(self.timeout, 2.0))
                raw = stream.recv(256)
        except (OSError, socket.timeout):
            return {"status": "route_unreachable", "error": "route_unreachable",
                    "detail": "registered SSH route did not return an SSH banner"}
        banner = raw.decode("ascii", errors="replace").splitlines()[0][:120] if raw else ""
        if not banner:
            return {"status": "route_unreachable", "error": "route_unreachable",
                    "detail": "registered SSH route did not return an SSH banner"}
        if banner.startswith("SSH-2.0-Tailscale"):
            return {"status": "route_collision", "error": "route_collision",
                    "route_banner": banner,
                    "detail": "registered SSH route terminates at Tailscale SSH, not the node OpenSSH server"}
        if not banner.startswith("SSH-"):
            return {"status": "route_banner_mismatch", "error": "route_banner_mismatch",
                    "route_banner": banner,
                    "detail": "registered SSH route returned a non-SSH banner"}
        return None

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
        cached_default_verified = (bool(profile.get("verified")) and node_id not in self.routes
                                   and not profile.get("verified_route"))
        try:
            node, client, host, port = self._node_target(node_id)
            result = client.ssh_manage("status")
        except NodeClientError as exc:
            status = "unsupported" if exc.http_status == 404 else "auth_failed" if exc.http_status in {401, 403} else "offline"
            return {"node_id": node_id, "status": status, "error": status,
                    "configured": profile.get("status") in {"configured", "verified"},
                    "verified": cached_default_verified,
                    "connect_available": cached_default_verified,
                    "address": profile.get("address"), "username": profile.get("username"),
                    "port": profile.get("port"), "host_key_fingerprint": profile.get("host_key_fingerprint"),
                    "verified_at": profile.get("verified_at"),
                    "detail": "node SSH management operation is not available" if status == "unsupported" else str(exc)}
        except (ValueError, ConnectionError) as exc:
            status = "offline" if str(exc) == "node_offline" else "error"
            return {"node_id": node_id, "status": status, "error": str(exc),
                    "configured": profile.get("status") in {"configured", "verified"},
                    "verified": cached_default_verified,
                    "connect_available": cached_default_verified,
                    "address": profile.get("address"), "username": profile.get("username"),
                    "port": profile.get("port"), "host_key_fingerprint": profile.get("host_key_fingerprint"),
                    "verified_at": profile.get("verified_at")}
        route_options = []
        if node_id in self.routes:
            try:
                host, port, route_options = self._ssh_route(node_id, profile, known_hosts, result)
            except (ValueError, ConnectionError, OSError, NodeClientError) as exc:
                return self._route_failure(node_id, exc)
        if result.get("status") == "ready" and not route_options:
            route_error = self._registered_route_diagnostic(
                host, int(result.get("port") or port))
            if route_error is not None:
                return {"node_id": node_id, **route_error,
                        "configured": profile.get("status") in {"configured", "verified"},
                        "verified": False, "connect_available": False,
                        "address": profile.get("address", host),
                        "username": profile.get("username", result.get("username")),
                        "port": profile.get("port", result.get("port", port)),
                        "host_key_fingerprint": profile.get("host_key_fingerprint"),
                        "verified_at": profile.get("verified_at"),
                        "agent_preflight": result.get("status"),
                        "private_key_present": key_path.exists(),
                        "pinned_host_key": known_hosts.exists()}
        stored_status = profile.get("status")
        observed_status = stored_status or ("not_configured" if result.get("status") == "ready"
                                            else result.get("status", "unknown"))
        if profile.get("verified_route") != self._route_binding(node_id) or (route_options and not profile.get("verified")):
            observed_status = "configured"
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
                "configured": bool(route_options) or stored_status in {"configured", "verified"},
                "verified": bool(profile.get("verified")) and observed_status == "verified",
                "connect_available": bool(profile.get("verified"))
                and (observed_status == "verified" if route_options or profile.get("verified_route")
                     else observed_status != "host_key_mismatch")
                and key_path.exists() and known_hosts.exists(),
                "address": host if route_options else profile.get("address", host), "username": profile.get("username", result.get("username")),
                "port": port if route_options else profile.get("port", result.get("port", port)),
                "host_key_fingerprint": profile.get("host_key_fingerprint"),
                "verified_at": profile.get("verified_at"),
                "error": (None if route_options and observed_status == "configured" else profile.get("error")) or result.get("error"),
                "detail": result.get("reason"),
                "agent_preflight": result.get("status"),
                "private_key_present": key_path.exists(),
                "pinned_host_key": known_hosts.exists()}

    def setup(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            key_path, known_hosts, profile_path = self._paths(node_id)
            if node_id in self.routes:
                # Existing managed credentials are a prerequisite. Route setup is
                # validation only; it must never silently install or re-trust keys.
                return self.status(node_id)
            try:
                node, client, host, port = self._node_target(node_id)
                preflight = client.ssh_manage("status")
                if preflight.get("status") != "ready":
                    return {"node_id": node_id, **preflight}
                route_error = self._registered_route_diagnostic(
                    host, int(preflight.get("port") or port))
                if route_error is not None:
                    return {"node_id": node_id, **route_error}
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
            host, port, route_options = self._ssh_route(node_id, profile, known_hosts)
            validate_hostname_or_ip(host)
        except (ValueError, NodeClientError, ConnectionError, OSError) as exc:
            if node_id not in self.routes:
                return {"node_id": node_id, "status": "error", "error": "invalid_registered_address"}
            return self._route_failure(node_id, exc)
        remote = "exec " + shlex.join(argv)
        ssh_argv = [self.ssh, "-F", os.devnull, "-i", str(key_path), "-p", str(port),
                    "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                    "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", *route_options, f"{user}@{host}", remote]
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
        if profile.get("verified_route") != self._route_binding(node_id):
            raise ValueError("ssh_route_requires_verification")
        try:
            host, port, route_options = self._ssh_route(node_id, profile, known_hosts)
        except (NodeClientError, ConnectionError, OSError) as exc:
            raise ValueError("ssh_route_unavailable") from exc
        validate_hostname_or_ip(host)
        if not key_path.is_file() or not known_hosts.is_file():
            raise ValueError("ssh_profile_incomplete")
        remote = "tmux new-session -A -s tmcp-managed-terminal"
        return [self.ssh, "-F", os.devnull, "-i", str(key_path), "-p", str(port),
                "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=5", *route_options, "-tt",
                f"{user}@{host}", remote]

    def verify(self, node_id: str, *, persist: bool = True) -> dict[str, Any]:
        key_path, known_hosts, profile_path = self._paths(node_id)
        profile = self._load_profile(profile_path)
        if not profile or not key_path.exists() or not known_hosts.exists():
            return {"node_id": node_id, "status": "not_configured", "verified": False}
        nonce = secrets.token_hex(16)
        host = str(profile.get("address") or "")
        user = str(profile.get("username") or "")
        port = int(profile.get("port") or 22)
        try:
            host, port, route_options = self._ssh_route(node_id, profile, known_hosts)
            validate_hostname_or_ip(host)
        except (NodeClientError, ConnectionError, OSError) as exc:
            return self._route_failure(node_id, exc)
        except ValueError as exc:
            if node_id not in self.routes:
                return {"node_id": node_id, "status": "error", "error": "invalid_registered_address"}
            return self._route_failure(node_id, exc)
        route_error = self._registered_route_diagnostic(host, port)
        if route_error is not None:
            profile.update(status=route_error["status"], verified=False,
                           error=route_error["error"])
            if persist:
                self._store_profile(profile_path, profile)
            return {"node_id": node_id, **route_error, "verified": False}
        argv = [self.ssh, "-F", os.devnull, "-i", str(key_path), "-p", str(port), "-o", "IdentityAgent=none",
                "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=5", *route_options, f"{user}@{host}",
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
            if persist:
                self._store_profile(profile_path, profile)
            return {"node_id": node_id, "status": status, "verified": False}
        output = (result.stdout or "").splitlines()
        expected_hostname = str(profile.get("expected_hostname") or "")
        ok = (len(output) >= 3 and output[0].strip() == nonce and output[1].strip() == user
              and bool(output[2].strip()) and (not expected_hostname or output[2].strip().casefold() == expected_hostname.casefold()))
        if not ok:
            profile.update(status="identity_mismatch", verified=False, error="identity_mismatch")
            if persist:
                self._store_profile(profile_path, profile)
            return {"node_id": node_id, "status": "identity_mismatch", "verified": False}
        profile.update(status="verified", verified=True, error=None, verified_route=self._route_binding(node_id), verified_hostname=output[2].strip(),
                       verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if persist:
            self._store_profile(profile_path, profile)
        return {"node_id": node_id, "status": "verified", "verified": True,
                "address": host, "username": user, "port": port,
                "host_key_fingerprint": profile.get("host_key_fingerprint"), "hostname": output[2].strip()}
