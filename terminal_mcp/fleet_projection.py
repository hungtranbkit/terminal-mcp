"""Turning this node's own stores into replicable facts -- and SSH targets
into facts that carry no secret.

Everything here READS existing sources of truth (node_registry,
session_registry, connection_store, project_identity, ~/.ssh/config) and
writes envelopes into the local FleetRegistryStore. Nothing here is a second
place to edit fleet data: a projector that disagrees with its source is a
bug in the projector, and the next pass overwrites it.

Object ids are chosen so that RENAME KEEPS IDENTITY:
  node:<node_id>                    node_id is already the canonical key
  session:<node_id>:<stable_id>     stable_session_id survives rename; the
                                    session NAME is payload, not identity
  project:<project_id>              derived from the git remote, so the same
                                    repo on two nodes is ONE object
  ssh:<fingerprint|host:port:user>  see ssh_target_id -- a host key
                                    fingerprint identifies a machine even
                                    after its address or alias changes
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .fleet_registry import (CRED_MISSING, CRED_NEEDS_AUTH, CRED_PRESENT, CRED_UNKNOWN,
                             KIND_NODE, KIND_PROJECT, KIND_SESSION, KIND_SSH_TARGET,
                             FleetRegistryStore, scrub_payload)

TRANSPORT_LAN = "lan"
TRANSPORT_TAILSCALE = "tailscale"
TRANSPORT_TUNNEL = "tunnel"
TRANSPORT_UNKNOWN = "unknown"

# Tailnet addresses are the 100.64.0.0/10 CGNAT range Tailscale uses. Judging
# transport by address beats trusting an alias: an operator's SSH config says
# nothing about which path it takes.
_TAILSCALE_V4 = re.compile(r"^100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.")
_RFC1918 = re.compile(r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)")


def classify_transport(host: str | None, *, proxy: str | None = None) -> str:
    host = (host or "").strip()
    if proxy:
        return TRANSPORT_TUNNEL
    if _TAILSCALE_V4.match(host) or host.endswith(".ts.net"):
        return TRANSPORT_TAILSCALE
    if _RFC1918.match(host):
        return TRANSPORT_LAN
    return TRANSPORT_UNKNOWN


def ssh_target_id(*, host_key_fingerprint: str | None, host: str | None,
                  port: int | None, username: str | None) -> str:
    """Identity for an SSH target, most durable evidence first.

    A host key fingerprint identifies the MACHINE: the same box reached by
    LAN IP, by tailnet IP and through a tunnel is one target with three
    routes, not three targets -- which is exactly the dedupe the fleet needs
    so an operator does not see `dell-linux` three times. Only when no
    fingerprint has been observed does it fall back to the address tuple,
    and that fallback is stated in the payload as `id_basis` so nobody
    mistakes a provisional id for a verified one.
    """
    if host_key_fingerprint:
        digest = hashlib.sha256(host_key_fingerprint.strip().encode()).hexdigest()[:20]
        return f"ssh:fp:{digest}"
    basis = f"{(host or '').strip().casefold()}:{port or 22}:{(username or '').strip()}"
    return f"ssh:addr:{hashlib.sha256(basis.encode()).hexdigest()[:20]}"


@dataclass(frozen=True)
class SshTargetFacts:
    """Everything about an SSH target that is safe to hand another machine.

    The omissions are the design: no key material, no password, no
    passphrase, no agent socket path that could be forwarded. `secret_ref`
    is a NAME an operator can look up on the owning node, never a value.
    """

    alias: str
    host: str | None
    port: int
    username: str | None
    transport: str
    proxy_jump: str | None = None
    host_key_fingerprint: str | None = None
    known_host_source: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()
    source: str = "unknown"
    credential_status: str = CRED_UNKNOWN
    secret_ref: str | None = None
    last_verified_at: str | None = None
    reachability: str | None = None
    preferred_order: int = 100
    aliases: tuple[str, ...] = ()
    node_id: str | None = None

    def payload(self) -> dict[str, Any]:
        data = {
            "alias": self.alias, "aliases": sorted(set(self.aliases) | {self.alias}),
            "host": self.host, "port": self.port, "username": self.username,
            "transport": self.transport, "proxy_jump": self.proxy_jump,
            "host_key_fingerprint": self.host_key_fingerprint,
            "known_host_source": self.known_host_source,
            "description": self.description, "tags": sorted(self.tags),
            "source": self.source,
            # Credential POSTURE, never a credential. A peer reading
            # MISSING_CREDENTIAL must ask a human.
            "credential_status": self.credential_status,
            "secret_ref": self.secret_ref,
            "last_verified_at": self.last_verified_at,
            "reachability": self.reachability,
            "preferred_order": self.preferred_order,
            "node_id": self.node_id,
            "id_basis": "host_key_fingerprint" if self.host_key_fingerprint else "address",
        }
        return scrub_payload(data, where=f"ssh_target:{self.alias}")


# -- SSH config ingest -------------------------------------------------------

# Only these keywords are read. An allowlist rather than "parse everything and
# filter later": IdentityFile is deliberately absent, so a key PATH never even
# enters the projector, let alone its contents.
_WANTED = {"hostname", "user", "port", "proxyjump", "proxycommand"}


def parse_ssh_config(text: str) -> list[dict[str, Any]]:
    """A deliberately small OpenSSH config reader.

    Reads Host blocks and a fixed allowlist of keywords. Never opens an
    IdentityFile, never follows Include (a path traversal surface for zero
    benefit here), never evaluates Match. Wildcard hosts are skipped: `Host *`
    is a defaults block, not a target, and publishing it would invent a
    machine that does not exist.
    """
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("=", " ", 1).split(None, 1)
        if len(parts) != 2:
            continue
        keyword, value = parts[0].casefold(), parts[1].strip()
        if keyword == "host":
            names = [name for name in value.split() if "*" not in name and "?" not in name]
            current = {"aliases": names} if names else None
            if current:
                blocks.append(current)
            continue
        if current is None or keyword not in _WANTED:
            continue
        current[keyword] = value
    return [block for block in blocks if block.get("aliases")]


def ssh_targets_from_config(path: str | os.PathLike[str] | None = None,
                            ) -> list[SshTargetFacts]:
    """Auto-discovery, read-only, from the operator's own ~/.ssh/config.

    Credential status is decided by EXISTENCE only -- `os.path.exists` on the
    IdentityFile, never an open() -- so this can say "that node has a key for
    this target" without any process here ever holding key bytes.
    """
    config_path = Path(path) if path else Path.home() / ".ssh" / "config"
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    targets: list[SshTargetFacts] = []
    for block in parse_ssh_config(text):
        aliases = block["aliases"]
        alias = aliases[0]
        host = block.get("hostname") or alias
        proxy = block.get("proxyjump") or block.get("proxycommand")
        try:
            port = int(block.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        targets.append(SshTargetFacts(
            alias=alias, aliases=tuple(aliases), host=host, port=port,
            username=block.get("user"), transport=classify_transport(host, proxy=proxy),
            proxy_jump=proxy, source="ssh_config",
            credential_status=_identity_status(text, aliases),
            description=None))
    return targets


def _identity_status(config_text: str, aliases: list[str]) -> str:
    """Does this host block name an IdentityFile, and does it exist HERE?

    Existence only. The file is never opened, so this can report "the
    credential is present on that node" without any possibility of the
    credential itself travelling.
    """
    wanted = set(aliases)
    in_block = False
    for raw in config_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.replace("=", " ", 1).split(None, 1)
        if len(parts) != 2:
            continue
        keyword, value = parts[0].casefold(), parts[1].strip()
        if keyword == "host":
            in_block = bool(wanted & set(value.split()))
            continue
        if in_block and keyword == "identityfile":
            candidate = Path(value).expanduser()
            return CRED_PRESENT if candidate.exists() else CRED_MISSING
    return CRED_UNKNOWN


# -- projectors --------------------------------------------------------------

def project_nodes(store: FleetRegistryStore, nodes: Iterable[Any], *,
                  local_node_id: str) -> int:
    """One object per node the local registry knows about.

    Only the LOCAL node's own row is owned here. A row this controller holds
    about a remote node is that node's truth to publish, not ours -- so it is
    published under the remote's ownership and will be overwritten the moment
    the real owner reports. That keeps single-writer honest while still
    giving a fresh peer something to read before it has talked to everyone.
    """
    written = 0
    for node in nodes:
        node_id = getattr(node, "id", None) or getattr(node, "node_id", None)
        if not node_id:
            continue
        payload = {
            "node_id": node_id,
            "display_name": getattr(node, "display_name", None),
            "platform": getattr(node, "platform", None),
            "hostname": getattr(node, "hostname", None),
            "endpoint": getattr(node, "endpoint", None),
            "session_backend": getattr(node, "session_backend", None),
            "status": getattr(node, "status", None),
            "last_heartbeat_at": getattr(node, "last_heartbeat_at", None),
            "draining": bool(getattr(node, "draining", False)),
            "agent_types": sorted(getattr(node, "agent_types", []) or []),
            "capabilities": sorted(getattr(node, "capabilities", []) or []),
            "contract_version": getattr(node, "contract_version", 0),
            "contract_capabilities": sorted(getattr(node, "contract_capabilities", []) or []),
            "agent_version": getattr(node, "agent_version", None),
            "shell_capabilities": sorted(getattr(node, "shell_capabilities", []) or []),
            "wsl_available": bool(getattr(node, "wsl_available", False)),
            "tmux_session_count": getattr(node, "tmux_session_count", None),
            "labels": sorted(getattr(node, "labels", []) or []),
            # The node-agent bearer token is referenced by the env var name
            # the owning machine reads it from. The value never appears.
            "auth_token_ref": getattr(node, "auth_token_ref", None),
        }
        store.publish(KIND_NODE, f"node:{node_id}", payload, owner_node=node_id)
        written += 1
    return written


def project_sessions(store: FleetRegistryStore, records: Iterable[Any]) -> int:
    """Session inventory -- metadata only.

    NO pane text, NO prompt, NO output, NO command arguments beyond the
    launcher label already stored. This is what makes the sync safe to run
    between machines with different operators: it answers "what exists and
    where", never "what was typed".
    """
    written = 0
    for record in records:
        node_id = getattr(record, "node_id", None)
        stable = getattr(record, "stable_session_id", None) or getattr(record, "session_name", None)
        if not node_id or not stable:
            continue
        object_id = f"session:{node_id}:{stable}"
        status = getattr(record, "status", None)
        if status == "DELETED" or getattr(record, "deleted_at", None):
            # A purge on the owner is a fleet-wide delete, not a local one.
            store.retire(KIND_SESSION, object_id)
            written += 1
            continue
        payload = {
            "stable_session_id": stable,
            "node_id": node_id,
            "session_name": getattr(record, "session_name", None),
            "display_name": getattr(record, "display_name", None),
            "cwd": getattr(record, "cwd", None),
            "repo_root": getattr(record, "repo_root", None),
            "git_remote": getattr(record, "git_remote", None),
            "git_branch": getattr(record, "git_branch", None),
            "agent_type": getattr(record, "agent_type", None),
            "launcher_type": getattr(record, "launcher_type", None),
            "state": getattr(record, "last_known_state", None),
            "status": status,
            "last_activity_at": getattr(record, "last_activity_at", None),
            "last_seen_at": getattr(record, "last_seen_at", None),
            "created_at": getattr(record, "created_at", None),
            "bindings": sorted(getattr(record, "binding_names", []) or []),
            "tags": sorted(getattr(record, "tags", []) or []),
            "read_granted": bool(getattr(record, "read_granted", False)),
            "input_granted": bool(getattr(record, "input_granted", False)),
            "recovery_state": getattr(record, "recovery_state", None),
            "desired_state": getattr(record, "recovery_state", None),
            "created_by_controller": bool(getattr(record, "created_by_controller", False)),
        }
        store.publish(KIND_SESSION, object_id, payload, owner_node=node_id)
        written += 1
    return written


def project_projects(store: FleetRegistryStore, records: Iterable[Any], *,
                     local_node_id: str) -> int:
    """Which project lives where, keyed on canonical project identity.

    A project is the one object kind that is genuinely fleet-wide rather than
    node-owned: the same repo is checked out on several machines. It is owned
    by the node that first published it and carries a per-node `checkouts`
    list, so another node reading it can see where else the work exists.
    """
    by_project: dict[str, dict[str, Any]] = {}
    for record in records:
        remote = getattr(record, "git_remote", None)
        root = getattr(record, "repo_root", None)
        if not remote and not root:
            continue
        project_id = _project_id(remote, root)
        entry = by_project.setdefault(project_id, {
            "project_id": project_id, "git_remote": remote, "checkouts": [],
        })
        checkout = {
            "node_id": getattr(record, "node_id", None),
            "repo_root": root,
            "git_branch": getattr(record, "git_branch", None),
            "session_name": getattr(record, "session_name", None),
        }
        if checkout not in entry["checkouts"]:
            entry["checkouts"].append(checkout)
    written = 0
    for project_id, payload in by_project.items():
        payload["checkouts"] = sorted(
            payload["checkouts"], key=lambda c: (str(c.get("node_id")), str(c.get("repo_root"))))
        object_id = f"project:{project_id}"
        # "Owned by the node that FIRST published it" (see this function's own
        # docstring) means every later node must leave it alone -- `publish`
        # enforces that by raising, and a controller re-projects the whole
        # fleet on every sync cycle, so asking unconditionally here guaranteed
        # a raise on any fleet where another node got there first.
        #
        # That is not a hypothetical: the records this projector reads are
        # FLEET-wide (`refresh_local` passes the controller's own cross-node
        # session listing), so a controller derives projects for repos that
        # only ever existed on another machine and would claim all of them.
        # One such project was enough to abort the whole refresh -- projects,
        # ssh targets and this node's own identity row all stopped being
        # projected, silently, on every cycle.
        #
        # Skipping is the correct resolution rather than a workaround: the
        # owner re-projects the same project from its own sessions, and the
        # tombstone/revision rules mean our copy converges on its next sync.
        existing = store.get(KIND_PROJECT, object_id)
        if existing is not None and not existing.deleted and existing.owner_node != local_node_id:
            continue
        store.publish(KIND_PROJECT, object_id, payload, owner_node=local_node_id)
        written += 1
    return written


def _project_id(remote: str | None, root: str | None) -> str:
    """Canonical-remote-first, mirroring project_identity.py's own precedence
    (remote survives being cloned elsewhere; a path does not)."""
    from .project_identity import normalise_git_remote

    if remote:
        try:
            canonical = normalise_git_remote(remote)
        except Exception:  # noqa: BLE001 -- a weird remote is not fatal here
            canonical = remote
        if canonical:
            return hashlib.sha256(str(canonical).encode()).hexdigest()[:20]
    return "path:" + hashlib.sha256(str(root or "").encode()).hexdigest()[:20]


def project_ssh_targets(store: FleetRegistryStore, targets: Iterable[SshTargetFacts], *,
                        local_node_id: str) -> dict[str, Any]:
    """Publish SSH targets, deduped by machine identity.

    Two config entries that resolve to the same host key fingerprint are ONE
    target with several aliases and routes -- see ssh_target_id. The merge of
    duplicates happens here rather than in the UI so every consumer, including
    a peer that only ever sees the replicated object, gets the deduped view.
    """
    merged: dict[str, SshTargetFacts] = {}
    duplicates = 0
    for target in targets:
        key = ssh_target_id(host_key_fingerprint=target.host_key_fingerprint,
                            host=target.host, port=target.port, username=target.username)
        existing = merged.get(key)
        if existing is None:
            merged[key] = target
            continue
        duplicates += 1
        # Keep the better-evidenced record and union what is safe to union.
        winner, loser = ((target, existing) if target.host_key_fingerprint
                         and not existing.host_key_fingerprint else (existing, target))
        merged[key] = SshTargetFacts(
            **{**winner.__dict__,
               "aliases": tuple(sorted(set(winner.aliases) | set(loser.aliases)
                                       | {winner.alias, loser.alias})),
               "credential_status": _best_credential(winner.credential_status,
                                                     loser.credential_status),
               "preferred_order": min(winner.preferred_order, loser.preferred_order)})
    published = skipped_foreign = 0
    for key, target in merged.items():
        # Same single-writer rule as project_projects above, and the same
        # reason it has to be checked rather than assumed: an ssh target is
        # keyed on the HOST it points at, not on who observed it, so two
        # nodes that can both reach one box derive the same key and the
        # second one to ask would raise and abort the rest of the refresh.
        existing = store.get(KIND_SSH_TARGET, key)
        if existing is not None and not existing.deleted and existing.owner_node != local_node_id:
            skipped_foreign += 1
            continue
        store.publish(KIND_SSH_TARGET, key, target.payload(), owner_node=local_node_id)
        published += 1
    return {"published": published, "deduped": duplicates,
            "skipped_foreign": skipped_foreign}


_CRED_RANK = {CRED_PRESENT: 0, CRED_NEEDS_AUTH: 1, CRED_MISSING: 2, CRED_UNKNOWN: 3}


def _best_credential(*statuses: str) -> str:
    return min(statuses, key=lambda s: _CRED_RANK.get(s, 9))


def ssh_targets_from_connections(rows: Iterable[Any]) -> list[SshTargetFacts]:
    """The controller's own ConnectionStore rows, which already carry a
    PINNED host key fingerprint -- the strongest identity available, and the
    reason these are projected alongside ~/.ssh/config rather than instead
    of it."""
    targets = []
    for row in rows:
        host = getattr(row, "hostname", None)
        transport_type = getattr(row, "transport_type", "") or ""
        transport = (TRANSPORT_TUNNEL if "cloudflare" in transport_type
                     else classify_transport(host))
        token_file = getattr(row, "token_file", None)
        targets.append(SshTargetFacts(
            alias=getattr(row, "node_id", None) or host or "?",
            host=host, port=int(getattr(row, "port", None) or 22),
            username=getattr(row, "username", None), transport=transport,
            host_key_fingerprint=getattr(row, "host_key_fingerprint", None),
            known_host_source="connection_store",
            source="connection_store",
            node_id=getattr(row, "node_id", None),
            # A PATH to a token file, never its contents -- and only that the
            # file exists, which is what decides whether this node can act.
            secret_ref=(f"file:{token_file}" if token_file else None),
            credential_status=(CRED_PRESENT if token_file and Path(str(token_file)).exists()
                               else CRED_MISSING if token_file else CRED_UNKNOWN),
            last_verified_at=getattr(row, "updated_at", None),
            preferred_order=10))
    return targets


# -- this machine's own network identity -------------------------------------

def local_network_identity(*, runner=None, timeout: float = 5.0) -> dict[str, Any]:
    """LAN and Tailscale addresses for THIS machine, best-effort.

    `tailscale ip -4` and `tailscale status --json --self` are status reads,
    not a login and not an auth-key operation -- nothing here can join a
    tailnet or read one's credentials. A machine without Tailscale simply
    reports None rather than an error, because "no tailnet" is a normal fleet
    state and must not look like a failure.
    """
    import shutil
    import subprocess

    run = runner or subprocess.run
    identity: dict[str, Any] = {"lan_ip": None, "tailscale_ip": None,
                                "tailscale_hostname": None}
    try:
        result = run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                     capture_output=True, text=True, timeout=timeout)
        for line in (result.stdout or "").splitlines():
            fields = line.split()
            if len(fields) < 4:
                continue
            address = fields[3].split("/")[0]
            if _TAILSCALE_V4.match(address):
                identity["tailscale_ip"] = identity["tailscale_ip"] or address
            elif _RFC1918.match(address):
                identity["lan_ip"] = identity["lan_ip"] or address
    except Exception:  # noqa: BLE001 -- an address probe never breaks a sync
        pass
    if shutil.which("tailscale"):
        try:
            result = run(["tailscale", "status", "--json"], capture_output=True,
                         text=True, timeout=timeout)
            import json as _json

            data = _json.loads(result.stdout or "{}")
            self_node = data.get("Self") or {}
            identity["tailscale_hostname"] = self_node.get("DNSName") or self_node.get("HostName")
            for address in self_node.get("TailscaleIPs") or []:
                if _TAILSCALE_V4.match(str(address)):
                    identity["tailscale_ip"] = identity["tailscale_ip"] or str(address)
        except Exception:  # noqa: BLE001 -- tailscale absent/logged out is normal
            pass
    return identity
