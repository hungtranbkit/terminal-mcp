"""The service behind Nodes -> + Add Node -> Windows.

Three operations, and deliberately only three:

  create_enrollment()   an operator asks for a new node. Returns a
                        one-time code + the download URL for the script
                        that carries it. Registers NOTHING yet -- a code
                        nobody redeems leaves no node behind.

  consume_enrollment()  the machine itself calls this, once, presenting
                        the code and its own facts (hostname, addresses,
                        the public half of the rescue keypair it just
                        generated). THIS is where the node becomes real:
                        a bearer token is minted, the node is registered,
                        its transports are recorded, a rescue port is
                        allocated. Returns the bootstrap payload -- the
                        only place any secret crosses the wire.

  remove_node()         revoke everything: pending codes, the bearer
                        token, the rescue port and its authorized_keys
                        line, the transport records, the registry row.

Everything else the dashboard shows (status, test primary, test rescue,
repair guidance) reads state these three wrote.

Secret discipline, enforced by tests (tests/test_windows_onboarding.py):
the only functions here that can return a secret are create_enrollment
(the code, once) and consume_enrollment (the bootstrap payload, once).
describe_node/list_enrollments/status never can -- they are built from
the stores' own redacted dataclasses, which do not carry a secret field
to leak.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import rescue_gateway
from .enrollment import (
    DEFAULT_TTL_SECONDS,
    EnrollmentStore,
    OS_WINDOWS,
    SUPPORTED_OS,
    suggest_node_id,
    validate_node_id,
)
from .node_transport import (
    KIND_LAN,
    KIND_REVERSE_SSH,
    KIND_TAILSCALE,
    TransportResolver,
    TransportStore,
)
from .windows_onboarding import PROFILES, profile_summary

_log = logging.getLogger(__name__)

_TAILNET_RE = re.compile(r"^100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")


class OnboardingError(Exception):
    """Carries a machine-readable code so routes can map it to a status
    without string-matching a message."""

    def __init__(self, code: str, detail: str = "", *, status: int = 400) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.status = status


def detect_controller_tailscale(*, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> dict[str, Any]:
    """Is THIS controller on a tailnet, and at what address?

    Read-only: `tailscale ip -4` and `tailscale status --json`. Never
    `tailscale up`, never anything that would join or leave a tailnet or
    touch credentials. A machine without the binary, or logged out,
    simply reports available=False -- which is what makes the Recommended
    connectivity preset degrade to LAN + rescue instead of generating an
    installer that points at a tailnet this controller cannot reach.
    """
    result: dict[str, Any] = {"available": False, "ip": None, "hostname": None, "reason": "tailscale_not_installed"}
    binary = shutil.which("tailscale")
    if not binary:
        return result
    try:
        completed = runner([binary, "status", "--json"], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        result["reason"] = f"tailscale_status_failed: {type(exc).__name__}"
        return result
    if completed.returncode != 0:
        result["reason"] = "tailscale_logged_out"
        return result
    try:
        status = json.loads(completed.stdout or "{}")
    except ValueError:
        result["reason"] = "tailscale_status_unparseable"
        return result
    self_node = status.get("Self") or {}
    addresses = [str(a) for a in (self_node.get("TailscaleIPs") or []) if _TAILNET_RE.match(str(a))]
    if not addresses:
        result["reason"] = "tailscale_no_ipv4_address"
        return result
    result.update(available=True, ip=addresses[0],
                  hostname=(self_node.get("DNSName") or self_node.get("HostName") or None), reason=None)
    return result


def is_tailnet_address(value: str) -> bool:
    return bool(_TAILNET_RE.match(str(value or "").strip()))


def read_controller_ssh_public_key(onboarding_config) -> tuple[str | None, str | None]:
    """(key, reason_it_is_missing). Inline config wins over a file path;
    a file that exists but holds something that is not an OpenSSH public
    key is refused rather than shipped -- pasting a PRIVATE key here is
    the mistake worth catching, and it is caught by the prefix check."""
    inline = (getattr(onboarding_config, "controller_ssh_public_key", "") or "").strip()
    if inline:
        return (inline, None) if _looks_like_public_key(inline) else (None, "controller_ssh_public_key_malformed")
    path_value = (getattr(onboarding_config, "controller_ssh_public_key_file", "") or "").strip()
    candidates = [Path(path_value).expanduser()] if path_value else [
        Path.home() / ".ssh" / "id_ed25519.pub", Path.home() / ".ssh" / "id_rsa.pub"]
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _looks_like_public_key(text):
            return text, None
        return None, "controller_ssh_public_key_malformed"
    return None, "controller_ssh_public_key_not_configured"


def _looks_like_public_key(text: str) -> bool:
    parts = str(text or "").split()
    return (len(parts) >= 2 and parts[0] in
            ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521")
            and re.fullmatch(r"[A-Za-z0-9+/=]{32,}", parts[1]) is not None)


def rescue_authorized_keys_dir() -> Path:
    override = os.environ.get("TERMINAL_MCP_RESCUE_KEYS_DIR")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "rescue-authorized-keys"


@dataclass(frozen=True)
class ConnectivityPlan:
    """What connectivity this node will actually get, decided server-side
    from real facts (does the controller have a tailnet? is a rescue
    gateway configured?) rather than from what the form asked for. The
    form's Advanced toggles can only ever turn something OFF."""
    tailscale: bool
    tailscale_reason: str | None
    rescue: bool
    rescue_reason: str | None
    lan: bool

    def to_dict(self) -> dict[str, Any]:
        return {"tailscale": self.tailscale, "tailscale_reason": self.tailscale_reason,
                "rescue": self.rescue, "rescue_reason": self.rescue_reason, "lan": self.lan}


class OnboardingService:
    def __init__(self, config, *, controller, connection_store, enrollment_store: EnrollmentStore,
                 transport_store: TransportStore, port_allocator: rescue_gateway.RescuePortAllocator,
                 audit=None, token_env_setter: Callable[[str, str], None] | None = None,
                 tailscale_detector: Callable[[], dict[str, Any]] = detect_controller_tailscale) -> None:
        self.config = config
        self.controller = controller
        self.connection_store = connection_store
        self.enrollments = enrollment_store
        self.transports = transport_store
        self.ports = port_allocator
        self.audit = audit
        self.resolver = TransportResolver(transport_store)
        self._set_token_env = token_env_setter or (lambda node_id, token: None)
        self._detect_tailscale = tailscale_detector

    # -- config helpers ----------------------------------------------------

    @property
    def onboarding_config(self):
        return self.config.nodes.onboarding

    def gateway(self) -> rescue_gateway.GatewayDescription:
        return rescue_gateway.describe(self.onboarding_config.rescue)

    def plan_connectivity(self, requested: dict[str, Any] | None = None) -> ConnectivityPlan:
        requested = requested or {}
        want_tailscale = bool(requested.get("tailscale", True))
        want_rescue = bool(requested.get("rescue", True))
        want_lan = bool(requested.get("lan", True))

        tailscale_reason: str | None = None
        tailscale_ok = False
        if not self.onboarding_config.tailscale.enabled:
            tailscale_reason = "tailscale_disabled_in_config"
        elif not want_tailscale:
            tailscale_reason = "declined_by_operator"
        else:
            detected = self._detect_tailscale()
            tailscale_ok = bool(detected.get("available"))
            tailscale_reason = None if tailscale_ok else (detected.get("reason") or "tailscale_unavailable")

        gateway = self.gateway()
        rescue_ok = False
        if not want_rescue:
            rescue_reason: str | None = "declined_by_operator"
        elif not gateway.configured:
            rescue_reason = gateway.reason
        else:
            rescue_ok, rescue_reason = True, None
        return ConnectivityPlan(tailscale=tailscale_ok, tailscale_reason=tailscale_reason,
                                rescue=rescue_ok, rescue_reason=rescue_reason, lan=want_lan)

    def controller_url(self, *, request_base_url: str | None = None) -> str:
        """What the generated installer will call back to, in priority
        order: the operator's explicit setting, then this controller's own
        tailnet address (the address a node can actually reach, and the
        one that bypasses a Cloudflare-Access-gated dashboard hostname),
        then the Host the browser used, then the bare hostname.

        The tailnet fallback has to name a port, and it must be the port
        THIS process serves on -- a staging controller started with
        TERMINAL_MCP_HTTP_PORT would otherwise hand every node a URL
        pointing at the production instance."""
        configured = (self.onboarding_config.controller_url or "").strip()
        if configured:
            return configured.rstrip("/")
        detected = self._detect_tailscale()
        if detected.get("available") and detected.get("ip"):
            return f"http://{detected['ip']}:{_controller_port()}"
        if request_base_url:
            return str(request_base_url).rstrip("/")
        return f"http://{socket.gethostname()}:{_controller_port()}"

    # -- create ------------------------------------------------------------

    def create_enrollment(self, *, node_id: str | None, display_name: str | None = None,
                          os_name: str = OS_WINDOWS, profile: str = "minimal",
                          connectivity: dict[str, Any] | None = None, created_by: str | None = None,
                          hostname_hint: str | None = None) -> dict[str, Any]:
        if not self.onboarding_config.enabled:
            raise OnboardingError("ONBOARDING_DISABLED", "nodes.onboarding.enabled is false", status=403)
        if os_name not in SUPPORTED_OS:
            raise OnboardingError("INVALID_REQUEST", f"os must be one of {', '.join(SUPPORTED_OS)}")
        if profile not in PROFILES:
            raise OnboardingError("INVALID_REQUEST", f"profile must be one of {', '.join(PROFILES)}")
        candidate = (node_id or "").strip() or suggest_node_id(hostname_hint or "")
        try:
            candidate = validate_node_id(candidate)
        except ValueError as exc:
            raise OnboardingError("INVALID_REQUEST", str(exc)) from exc
        if self.controller.node_status(candidate) is not None:
            raise OnboardingError("NODE_ALREADY_EXISTS", f"a node named {candidate!r} is already registered",
                                  status=409)
        plan = self.plan_connectivity(connectivity)
        record, code = self.enrollments.create(
            node_id=candidate, display_name=display_name, os_name=os_name, profile=profile,
            connectivity=plan.to_dict(), ttl_seconds=self.onboarding_config.enrollment_ttl_seconds,
            created_by=created_by)
        self._audit("node_enrollment_created", node_id=candidate,
                    detail={"enrollment_id": record.id, "profile": profile, "os": os_name,
                            "connectivity": plan.to_dict()})
        _log.info("onboarding: enrollment created node_id=%s profile=%s os=%s expires_at=%s by=%s",
                  candidate, profile, os_name, record.expires_at, created_by)
        return {"enrollment": record.to_dict(), "code": code, "connectivity": plan.to_dict(),
                "profile": profile_summary(profile), "ttl_seconds": self.onboarding_config.enrollment_ttl_seconds}

    def list_enrollments(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self.enrollments.list(limit=limit)]

    def revoke_enrollment(self, enrollment_id: str, *, by: str | None = None) -> bool:
        revoked = self.enrollments.revoke(enrollment_id, by=by)
        if revoked:
            self._audit("node_enrollment_revoked", node_id=None, detail={"enrollment_id": enrollment_id})
        return revoked

    # -- consume -----------------------------------------------------------

    def consume_enrollment(self, code: str, *, hostname: str, source_ip: str | None = None,
                           platform: str = "windows", addresses: dict[str, Any] | None = None,
                           rescue_public_key: str | None = None, agent_version: str | None = None,
                           request_base_url: str | None = None) -> dict[str, Any]:
        """Atomic, single-use. Everything after the code is consumed is
        best-effort-but-reported: a rescue gateway that is not configured
        does not fail the enrollment, it comes back as
        `rescue.configured=false` with a reason the installer prints."""
        if not self.onboarding_config.enabled:
            raise OnboardingError("ONBOARDING_DISABLED", "nodes.onboarding.enabled is false", status=403)
        record, error = self.enrollments.consume(code, hostname=hostname, source_ip=source_ip)
        if record is None:
            self._audit("node_enrollment_rejected", node_id=None,
                        detail={"reason": error, "hostname": hostname, "source_ip": source_ip})
            _log.warning("onboarding: enrollment rejected reason=%s hostname=%s source=%s",
                         error, hostname, source_ip)
            raise OnboardingError(error or "ENROLLMENT_NOT_FOUND", "enrollment code is not usable", status=401)

        node_id = record.node_id
        addresses = addresses or {}
        plan = ConnectivityPlan(
            tailscale=bool(record.connectivity.get("tailscale")),
            tailscale_reason=record.connectivity.get("tailscale_reason"),
            rescue=bool(record.connectivity.get("rescue")),
            rescue_reason=record.connectivity.get("rescue_reason"),
            lan=bool(record.connectivity.get("lan", True)),
        )
        controller_url = self.controller_url(request_base_url=request_base_url)
        agent_port = self.onboarding_config.agent_port

        # 1. Bearer token: minted here, stored 0600 as a file reference,
        #    handed to the node exactly once in this response.
        token = secrets.token_hex(32)
        token_file = self.connection_store.write_token(node_id, token)

        # 2. Transports, from what the node itself just reported. A
        #    tailnet address is only accepted as one if it actually IS in
        #    100.64.0.0/10 -- a node claiming "my tailscale IP is
        #    10.0.0.5" gets it recorded as LAN, not as the trusted
        #    overlay path.
        primary_endpoint = None
        tailnet_ip = str(addresses.get("tailscale_ip") or "").strip()
        lan_ip = str(addresses.get("lan_ip") or "").strip()
        if tailnet_ip and is_tailnet_address(tailnet_ip):
            primary_endpoint = f"http://{tailnet_ip}:{agent_port}"
            self.transports.upsert(node_id, KIND_TAILSCALE, endpoint=primary_endpoint,
                                   host=tailnet_ip, port=agent_port)
        if lan_ip and not is_tailnet_address(lan_ip):
            lan_endpoint = f"http://{lan_ip}:{agent_port}"
            primary_endpoint = primary_endpoint or lan_endpoint
            self.transports.upsert(node_id, KIND_LAN, endpoint=lan_endpoint, host=lan_ip, port=agent_port)
        endpoint = primary_endpoint or f"http://{hostname}:{agent_port}"

        # 3. Rescue port + the gateway authorized_keys line an admin (or a
        #    sync job) installs. Never fatal.
        rescue_payload = self._provision_rescue(node_id, plan=plan, public_key=rescue_public_key)

        # 4. Register. From this point the node's own heartbeats land in
        #    the registry and it shows on the Nodes page.
        self.connection_store.save(node_id, transport_type="agent_token", endpoint=endpoint,
                                   hostname=hostname or node_id, token_file=token_file)
        self.controller.register_remote_node(node_id, display_name=record.display_name,
                                             hostname=hostname or node_id, endpoint=endpoint, token=token)
        self._set_token_env(node_id, token)

        ssh_key, ssh_key_reason = read_controller_ssh_public_key(self.onboarding_config)
        tailscale_payload = self._tailscale_payload(plan)

        self._audit("node_enrolled", node_id=node_id,
                    detail={"enrollment_id": record.id, "hostname": hostname, "platform": platform,
                            "profile": record.profile, "endpoint": endpoint,
                            "tailscale": tailscale_payload["enabled"],
                            "rescue": rescue_payload["configured"],
                            "rescue_port": rescue_payload.get("reverse_port")})
        _log.info("onboarding: node enrolled node_id=%s hostname=%s endpoint=%s tailscale=%s rescue=%s",
                  node_id, hostname, endpoint, tailscale_payload["enabled"], rescue_payload["configured"])

        return {
            "node_id": node_id,
            "display_name": record.display_name,
            "profile": record.profile,
            "profile_detail": profile_summary(record.profile),
            "controller_url": controller_url,
            "heartbeat_path": f"/dashboard/api/nodes/{node_id}/heartbeat",
            "node_token": token,
            "heartbeat_interval_seconds": self.onboarding_config.heartbeat_interval_seconds,
            "agent_port": agent_port,
            "ssh": {
                "authorized_key": ssh_key,
                "authorized_key_reason": ssh_key_reason,
                "firewall_cidrs": list(self.onboarding_config.ssh_firewall_cidrs),
                "password_authentication": False,
            },
            "tailscale": tailscale_payload,
            "rescue": rescue_payload,
        }

    def _tailscale_payload(self, plan: ConnectivityPlan) -> dict[str, Any]:
        settings = self.onboarding_config.tailscale
        if not plan.tailscale:
            return {"enabled": False, "reason": plan.tailscale_reason, "auth_key": None,
                    "tags": [], "unattended": settings.unattended, "login_server": settings.login_server or None}
        auth_key = os.environ.get(settings.auth_key_env or "") or None
        return {
            "enabled": True,
            "reason": None if auth_key else "auth_key_not_configured_interactive_login_required",
            "auth_key": auth_key,
            "tags": list(settings.tags),
            "unattended": settings.unattended,
            "login_server": settings.login_server or None,
        }

    def _provision_rescue(self, node_id: str, *, plan: ConnectivityPlan,
                          public_key: str | None) -> dict[str, Any]:
        gateway = self.gateway()

        def _unavailable(reason: str) -> dict[str, Any]:
            # gateway.to_dict() carries its own `configured` -- spread it
            # FIRST so this function's verdict is what survives. (Getting
            # this order wrong once made an unconfigured gateway report
            # configured=true, which is the single worst possible lie this
            # payload could tell an installer.)
            payload = gateway.to_dict(include_host_key=False)
            payload.update({"configured": False, "reason": reason})
            return payload

        if not plan.rescue or not gateway.configured:
            return _unavailable(plan.rescue_reason or gateway.reason or "rescue_unavailable")
        if not public_key or not _looks_like_public_key(public_key):
            return _unavailable("node_rescue_public_key_missing_or_malformed")
        try:
            allocation = self.ports.allocate(node_id, public_key=public_key.strip())
        except RuntimeError as exc:
            return _unavailable(str(exc))
        line = rescue_gateway.build_authorized_keys_line(reverse_port=allocation.port,
                                                         public_key=public_key, node_id=node_id)
        installed = self._write_gateway_authorized_key(node_id, line)
        self.transports.upsert(node_id, KIND_REVERSE_SSH,
                               endpoint=f"ssh://{gateway.host}:{gateway.port}#127.0.0.1:{allocation.port}",
                               host=gateway.host, port=allocation.port)
        payload = gateway.to_dict(include_host_key=True)
        payload.update({"configured": True, "reason": None, "reverse_port": allocation.port,
                        "authorized_keys_line": line, "authorized_keys_synced": installed})
        return payload

    def _write_gateway_authorized_key(self, node_id: str, line: str) -> bool:
        """Writes the node's gateway authorized_keys line to a 0600 file the
        admin (or a sync job) pushes to the gateway. Returns False on any
        write failure -- the caller reports `authorized_keys_synced:false`
        and the installer says the tunnel will not come up until an admin
        installs it, which is true, instead of claiming success."""
        directory = rescue_authorized_keys_dir()
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = directory / f"{node_id}.pub"
            target.write_text(line.rstrip("\n") + "\n", encoding="utf-8")
            target.chmod(0o600)
            self._rebuild_gateway_authorized_keys(directory)
            return True
        except OSError:
            _log.exception("onboarding: could not stage the rescue authorized_keys line for %s", node_id)
            return False

    @staticmethod
    def _rebuild_gateway_authorized_keys(directory: Path) -> None:
        """One concatenated authorized_keys the admin copies to the gateway
        in a single scp/rsync, rather than N per-node snippets to merge by
        hand. Rewritten from scratch on every change, so a removed node's
        line disappears from it."""
        lines = []
        for path in sorted(directory.glob("*.pub")):
            with_text = path.read_text(encoding="utf-8").strip()
            if with_text:
                lines.append(with_text)
        combined = directory / "authorized_keys"
        combined.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
        combined.chmod(0o600)

    # -- status / removal ---------------------------------------------------

    def describe_node(self, node_id: str) -> dict[str, Any]:
        """Never returns a secret: transports carry endpoints, the rescue
        row carries a port and a flag for "we hold a public key", and the
        token is referenced only by the fact that a connection row exists."""
        node = self.controller.node_status(node_id)
        allocation = self.ports.get(node_id)
        connection = self.connection_store.get(node_id)
        return {
            "node_id": node_id,
            "registered": node is not None,
            "status": node.status if node else None,
            "platform": node.platform if node else None,
            "last_heartbeat_at": node.last_heartbeat_at if node else None,
            "endpoint": node.endpoint if node else None,
            "transports": [t.to_dict() for t in self.transports.list_for(node_id)],
            "rescue": allocation.to_dict() if allocation else None,
            "gateway": self.gateway().to_dict(),
            "has_credentials": bool(connection and connection.token_file),
            "enrollments": [record.to_dict() for record in self.enrollments.list(node_id=node_id, limit=10)],
        }

    def remove_node(self, node_id: str, *, by: str | None = None) -> dict[str, Any]:
        """Revokes everything Terminal MCP owns on/about this node, and
        nothing else. Deliberately does NOT try to reach the machine: a
        node being removed is very often a node that is already gone, and
        a removal that hangs on an unreachable host is a removal that
        never completes."""
        revoked_codes = self.enrollments.revoke_pending_for_node(node_id, by=by)
        released = self.ports.release(node_id)
        directory = rescue_authorized_keys_dir()
        stale = directory / f"{node_id}.pub"
        if stale.exists():
            try:
                stale.unlink()
                self._rebuild_gateway_authorized_keys(directory)
            except OSError:
                _log.exception("onboarding: could not remove the staged rescue key for %s", node_id)
        transports_removed = self.transports.delete_node(node_id)
        connection_removed = self.connection_store.delete(node_id)
        deregistered = self.controller.registry.deregister(node_id)
        self.controller._clients.pop(node_id, None)
        os.environ.pop(_token_env_var(node_id), None)
        self._audit("node_removed", node_id=node_id,
                    detail={"revoked_codes": revoked_codes, "rescue_port_released": released,
                            "transports_removed": transports_removed,
                            "connection_removed": connection_removed, "deregistered": deregistered})
        _log.info("onboarding: node removed node_id=%s codes=%s rescue=%s transports=%s registry=%s by=%s",
                  node_id, revoked_codes, released, transports_removed, deregistered, by)
        return {"ok": True, "node_id": node_id, "revoked_enrollments": revoked_codes,
                "rescue_port_released": released, "transports_removed": transports_removed,
                "credentials_removed": connection_removed, "deregistered": deregistered,
                "gateway_resync_required": released}

    # -- audit --------------------------------------------------------------

    def _audit(self, action: str, *, node_id: str | None, detail: dict[str, Any],
               actor: str | None = None, result: str = "ok") -> None:
        """Every enroll/revoke/remove/test event is recorded through the
        SAME AuditStore the rest of this project writes to -- one log, one
        retention policy, one place to look. `detail` goes through
        redaction.redact_text first, so even a caller that put a token in
        a field cannot write it to the audit log."""
        if self.audit is None:
            return
        try:
            from .redaction import redact_text
            payload = redact_text(json.dumps(detail, sort_keys=True, default=str))
            self.audit.record(action=action, session=None, result=result, reason=payload,
                              actor=actor, node_id=node_id, source_transport="dashboard")
        except Exception:  # noqa: BLE001 -- an audit failure must never break onboarding
            _log.exception("onboarding: audit write failed for %s", action)


def _token_env_var(node_id: str) -> str:
    return f"TERMINAL_MCP_NODE_TOKEN_{node_id.upper().replace('-', '_')}"


def _controller_port() -> int:
    """server_http.py's own port resolution, read here without importing
    it -- server_http imports dashboard, which imports this module, so an
    import the other way would close the cycle."""
    raw = os.environ.get("TERMINAL_MCP_HTTP_PORT")
    if raw:
        try:
            port = int(raw)
        except ValueError:
            return 8766
        if 1 <= port <= 65535:
            return port
    return 8766
