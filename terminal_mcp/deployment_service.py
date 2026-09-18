"""Deployment redundancy as an operation: probe a path, pick a node, hold a
lease, and help a node get its own key onto a target.

Probing is NON-DESTRUCTIVE by construction. Every SSH invocation here is
`BatchMode=yes` with a remote command that only reports (`true`, `command -v`)
-- it never writes, never restarts anything, never touches authorized_keys,
and never prompts, so a doctor run cannot change the thing it is measuring.

Reconcile is the one write, and it is deliberately split: generating a key
and emitting a MANAGED BLOCK in ~/.ssh/config happen locally on the node that
will use them, while installing the public key on the TARGET is left to a
human or an automation that already holds the right. A tool that installs its
own trust is a tool that can be told to install someone else's.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .deployment_redundancy import (KIND_DEPLOY_PATH, KIND_DEPLOY_TARGET, PATH_READY,
                                    DeploymentPath, DeploymentTarget, choose_deploy_node,
                                    deploy_lease_key, evaluate_target)
from .fleet_registry import FleetRegistryStore

# The managed block is delimited so reconcile can rewrite ITS OWN lines and
# nothing else. An operator's hand-written config above or below survives
# untouched, which is the difference between a helpful tool and one nobody
# lets near their machine twice.
BLOCK_BEGIN = "# >>> terminal-mcp managed deployment paths >>>"
BLOCK_END = "# <<< terminal-mcp managed deployment paths <<<"

DEFAULT_PROBE_TIMEOUT = 12.0
DEPLOY_LEASE_TTL = 900.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    reason: str
    latency_ms: float | None = None
    auth_ok: bool | None = None
    deploy_prereqs_ok: bool | None = None
    host_key_fingerprint: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "latency_ms": self.latency_ms,
                "auth_ok": self.auth_ok, "deploy_prereqs_ok": self.deploy_prereqs_ok,
                "host_key_fingerprint": self.host_key_fingerprint}


def probe_path(path: DeploymentPath, *, known_hosts_file: Path | None = None,
               identity_file: Path | None = None,
               deploy_prereqs: Sequence[str] = ("git",),
               runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
               timeout: float = DEFAULT_PROBE_TIMEOUT) -> ProbeResult:
    """One read-only reachability + auth + prereq check.

    The remote command is `command -v <tool>` per prerequisite: it answers
    "could a deploy run here" without running one. BatchMode=yes means a
    missing credential fails fast as NEEDS_AUTH instead of hanging on a
    password prompt -- which is also what keeps this safe to run on a
    schedule.
    """
    if not path.host:
        return ProbeResult(ok=False, reason="no host configured for this path")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(timeout)}",
            "-o", "StrictHostKeyChecking=yes"]
    if known_hosts_file is not None:
        argv += ["-o", f"UserKnownHostsFile={known_hosts_file}"]
    if identity_file is not None:
        argv += ["-o", "IdentitiesOnly=yes", "-i", str(identity_file)]
    if path.proxy_jump:
        argv += ["-J", str(path.proxy_jump)]
    if path.port and int(path.port) != 22:
        argv += ["-p", str(int(path.port))]
    destination = f"{path.username}@{path.host}" if path.username else str(path.host)
    # Read-only by construction: `command -v` reports, it does not act.
    checks = " && ".join(f"command -v {tool} >/dev/null" for tool in deploy_prereqs) or "true"
    argv += [destination, f"true && {checks}"]

    started = time.monotonic()
    try:
        completed = runner(argv, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as exc:  # noqa: BLE001 -- an unreachable target is data
        return ProbeResult(ok=False, reason=f"{type(exc).__name__}: {exc}")
    latency = (time.monotonic() - started) * 1000.0
    stderr = (completed.stderr or "").strip()
    if completed.returncode == 0:
        return ProbeResult(ok=True, reason="reachable, authenticated, prerequisites present",
                           latency_ms=round(latency, 1), auth_ok=True,
                           deploy_prereqs_ok=True)
    lowered = stderr.casefold()
    # Authentication failing is a DIFFERENT state from the host being
    # unreachable: one needs a public key installed, the other needs a
    # network fixed, and telling an operator the wrong one wastes a day.
    if "permission denied" in lowered or "no supported authentication" in lowered:
        return ProbeResult(ok=True, reason="reachable but authentication was refused",
                           latency_ms=round(latency, 1), auth_ok=False)
    if "host key verification failed" in lowered:
        return ProbeResult(ok=False, reason="host key verification failed",
                           latency_ms=round(latency, 1))
    if completed.returncode == 127 or "command not found" in lowered:
        return ProbeResult(ok=True, reason="reachable and authenticated, but deploy "
                                           "prerequisites are missing on the target",
                           latency_ms=round(latency, 1), auth_ok=True,
                           deploy_prereqs_ok=False)
    return ProbeResult(ok=False, reason=(stderr.splitlines()[-1] if stderr
                                         else f"ssh exited {completed.returncode}"),
                       latency_ms=round(latency, 1))


class DeploymentRegistry:
    """Targets and paths, stored as two more fleet-registry kinds.

    Not a new database: they replicate, tombstone and refuse secrets exactly
    like every other fleet object, which is also what gets them onto the
    surviving node when the controller dies.
    """

    def __init__(self, store: FleetRegistryStore, *, local_node_id: str) -> None:
        self.store = store
        self.local_node_id = local_node_id

    def put_target(self, target: DeploymentTarget) -> dict[str, Any]:
        obj = self.store.publish(KIND_DEPLOY_TARGET, f"deploy_target:{target.target_id}",
                                 target.payload(), owner_node=self.local_node_id)
        return obj.as_dict()

    def put_path(self, path: DeploymentPath) -> dict[str, Any]:
        # Owned by the node the path belongs to: only that machine can
        # honestly say whether ITS route works, which is the same
        # single-writer rule the rest of the registry runs on.
        obj = self.store.publish(KIND_DEPLOY_PATH, path.object_id(), path.payload(),
                                 owner_node=path.node_id)
        return obj.as_dict()

    def retire_target(self, target_id: str) -> None:
        self.store.retire(KIND_DEPLOY_TARGET, f"deploy_target:{target_id}")
        for path in self.paths(target_id):
            self.store.retire(KIND_DEPLOY_PATH, path.object_id())

    def targets(self) -> list[DeploymentTarget]:
        out = []
        for obj in self.store.list(kind=KIND_DEPLOY_TARGET):
            payload = obj.payload
            out.append(DeploymentTarget(
                target_id=payload.get("target_id", ""),
                display_name=payload.get("display_name", ""),
                project_id=payload.get("project_id"),
                min_independent_paths=int(payload.get("min_independent_paths") or 2),
                primary_node=payload.get("primary_node"),
                backup_nodes=tuple(payload.get("backup_nodes") or ()),
                deploy_command_ref=payload.get("deploy_command_ref"),
                description=payload.get("description"),
                tags=tuple(payload.get("tags") or ())))
        return sorted(out, key=lambda t: t.target_id)

    def paths(self, target_id: str | None = None) -> list[DeploymentPath]:
        out = []
        for obj in self.store.list(kind=KIND_DEPLOY_PATH):
            payload = obj.payload
            if target_id and payload.get("target_id") != target_id:
                continue
            out.append(DeploymentPath(
                target_id=payload.get("target_id", ""), node_id=payload.get("node_id", ""),
                ssh_alias=payload.get("ssh_alias"), host=payload.get("host"),
                port=int(payload.get("port") or 22), username=payload.get("username"),
                transport=payload.get("transport") or "unknown",
                proxy_jump=payload.get("proxy_jump"),
                independence_group=payload.get("independence_group"),
                host_key_fingerprint=payload.get("host_key_fingerprint"),
                public_key_id=payload.get("public_key_id"),
                cert_issuer=payload.get("cert_issuer"),
                capabilities=tuple(payload.get("capabilities") or ()),
                last_probe_at=payload.get("last_probe_at"),
                last_probe_ok=payload.get("last_probe_ok"),
                last_probe_reason=payload.get("last_probe_reason"),
                latency_ms=payload.get("latency_ms"), auth_ok=payload.get("auth_ok"),
                deploy_prereqs_ok=payload.get("deploy_prereqs_ok"),
                role=payload.get("role") or "backup"))
        return sorted(out, key=lambda p: (p.target_id, p.node_id))

    def record_probe(self, path: DeploymentPath, result: ProbeResult, *,
                     now: str | None = None) -> DeploymentPath:
        updated = replace(
            path, last_probe_at=now or _now(), last_probe_ok=result.ok,
            last_probe_reason=result.reason, latency_ms=result.latency_ms,
            auth_ok=result.auth_ok if result.auth_ok is not None else path.auth_ok,
            deploy_prereqs_ok=(result.deploy_prereqs_ok
                               if result.deploy_prereqs_ok is not None
                               else path.deploy_prereqs_ok),
            host_key_fingerprint=(result.host_key_fingerprint
                                  or path.host_key_fingerprint))
        self.put_path(updated)
        return updated


class DeploymentService:
    """Evaluation, dispatch and reconcile over the registry above."""

    def __init__(self, registry: DeploymentRegistry, *, locks: Any = None,
                 node_online: Callable[[], dict[str, bool]] | None = None,
                 node_aliases: Callable[[], dict[str, set[str]]] | None = None) -> None:
        self.registry = registry
        self.locks = locks
        self._node_online = node_online or (lambda: {})
        self._node_aliases = node_aliases or (lambda: {})

    def evaluate(self, target_id: str) -> dict[str, Any] | None:
        target = next((t for t in self.registry.targets() if t.target_id == target_id), None)
        if target is None:
            return None
        return evaluate_target(target, self.registry.paths(target_id),
                               node_online=self._node_online(),
                               node_aliases=self._node_aliases())

    def evaluate_all(self) -> list[dict[str, Any]]:
        online, aliases = self._node_online(), self._node_aliases()
        return [evaluate_target(target, self.registry.paths(target.target_id),
                                node_online=online, node_aliases=aliases)
                for target in self.registry.targets()]

    def choose(self, target_id: str) -> dict[str, Any]:
        evaluation = self.evaluate(target_id)
        if evaluation is None:
            return {"error": "UNKNOWN_TARGET", "target_id": target_id}
        return {"target_id": target_id, **choose_deploy_node(evaluation),
                "status": evaluation["status"],
                "redundancy": evaluation["redundancy"]}

    def dry_run_failover(self, target_id: str, *, assume_offline: Sequence[str] = ()) -> dict[str, Any]:
        """What would happen if these nodes were gone -- without making them gone.

        The point is to be able to answer "are we actually covered?" on a
        Tuesday afternoon rather than finding out during an incident, so it
        touches nothing: it re-evaluates against a hypothetical.
        """
        target = next((t for t in self.registry.targets() if t.target_id == target_id), None)
        if target is None:
            return {"error": "UNKNOWN_TARGET", "target_id": target_id}
        online = dict(self._node_online())
        for node in assume_offline:
            online[node] = False
        evaluation = evaluate_target(target, self.registry.paths(target_id),
                                     node_online=online, node_aliases=self._node_aliases())
        return {"target_id": target_id, "assumed_offline": list(assume_offline),
                "status": evaluation["status"], "redundancy": evaluation["redundancy"],
                "deploy_available": evaluation["deploy_available"],
                "would_choose": choose_deploy_node(evaluation),
                "paths": evaluation["paths"], "warnings": evaluation["warnings"]}

    # -- dispatch ------------------------------------------------------------

    def acquire_deploy_lease(self, target_id: str, *, owner_id: str | None = None,
                             project_id: str = "deployments",
                             ttl_seconds: float = DEPLOY_LEASE_TTL) -> dict[str, Any]:
        """One lease per TARGET. Two dispatchers, one deploy.

        Reuses ResourceLockStore because its acquire() is the atomic
        check-and-set that a SELECT-then-write already lost a real race to in
        this codebase -- rewriting that here would be re-earning a scar.
        """
        if self.locks is None:
            return {"acquired": False, "error": "NO_LEASE_STORE"}
        owner = owner_id or f"deploy-{uuid.uuid4().hex[:12]}"
        key = deploy_lease_key(target_id)
        # ResourceLockStore.acquire returns a DICT, and a non-empty dict is
        # truthy -- reading it as a boolean made every lease "succeed" and
        # silently disabled the one thing this lease exists to do.
        outcome = self.locks.acquire(project_id, key, owner, ttl_seconds=ttl_seconds,
                                     reason=f"deploy dispatch for {target_id}")
        acquired = bool(outcome.get("acquired"))
        holder = outcome.get("lock") if acquired else outcome.get("holder")
        return {"acquired": acquired, "owner_id": owner if acquired else None,
                "lease_key": key, "held_by": (holder or {}).get("owner_id"),
                "expires_at": (holder or {}).get("expires_at")}

    def release_deploy_lease(self, target_id: str, owner_id: str, *,
                             project_id: str = "deployments") -> bool:
        if self.locks is None:
            return False
        # release() genuinely returns a bool, unlike acquire() -- checked
        # rather than assumed, after acquire's dict return silently disabled
        # this lease once already.
        return bool(self.locks.release(project_id, deploy_lease_key(target_id), owner_id))

    def dispatch(self, target_id: str, *, owner_id: str | None = None,
                 project_id: str = "deployments") -> dict[str, Any]:
        """Pick a node and claim the right to deploy, atomically enough.

        The lease is taken BEFORE the choice is reported, so a second caller
        arriving in the same instant is told who holds it rather than being
        handed a node to also deploy from.
        """
        choice = self.choose(target_id)
        if choice.get("error") or not choice.get("node_id"):
            return {**choice, "lease": None, "dispatched": False}
        lease = self.acquire_deploy_lease(target_id, owner_id=owner_id, project_id=project_id)
        if not lease.get("acquired"):
            return {**choice, "lease": lease, "dispatched": False,
                    "reason": f"a deploy for {target_id} is already in flight "
                              f"(held by {lease.get('held_by')})"}
        return {**choice, "lease": lease, "dispatched": True}


# -- reconcile / bootstrap ---------------------------------------------------

@dataclass(frozen=True)
class KeyMaterialSummary:
    """What a node has, described without any of it.

    There is no field here that could hold key bytes, which is deliberate:
    the type itself makes leaking one a compile-time-shaped mistake rather
    than a review-time one.
    """

    path: str
    public_key_path: str
    exists: bool
    created: bool
    fingerprint: str | None
    public_key: str | None       # the PUBLIC half -- meant to be handed out
    key_type: str = "ed25519"

    def as_dict(self) -> dict[str, Any]:
        return {"private_key_path": self.path, "public_key_path": self.public_key_path,
                "exists": self.exists, "created": self.created,
                "fingerprint": self.fingerprint, "public_key": self.public_key,
                "key_type": self.key_type}


def ensure_deploy_key(key_path: str | os.PathLike[str], *, comment: str,
                      runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                      create: bool = True) -> KeyMaterialSummary:
    """This node's own deployment key, generated here if missing.

    NEVER copied from or to another machine, and the private half is never
    read by this function -- only its path, its mode and its fingerprint. The
    public half IS returned, because handing it to an operator is the entire
    point: that is how a second independent path gets authorised without any
    private key moving.
    """
    private = Path(key_path).expanduser()
    public = Path(str(private) + ".pub")
    created = False
    if not private.exists():
        if not create:
            return KeyMaterialSummary(path=str(private), public_key_path=str(public),
                                      exists=False, created=False, fingerprint=None,
                                      public_key=None)
        private.parent.mkdir(parents=True, exist_ok=True)
        # 0700 on ~/.ssh and 0600 on the key: OpenSSH refuses a key with
        # looser permissions, so getting this wrong produces a path that
        # fails only in production.
        os.chmod(private.parent, stat.S_IRWXU)
        runner(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment,
                "-f", str(private)], capture_output=True, text=True, timeout=60)
        created = private.exists()
    if private.exists():
        os.chmod(private, stat.S_IRUSR | stat.S_IWUSR)
    fingerprint = None
    public_text = None
    if public.exists():
        public_text = public.read_text(encoding="utf-8", errors="replace").strip()
        result = runner(["ssh-keygen", "-lf", str(public)], capture_output=True,
                        text=True, timeout=30)
        if result.returncode == 0 and result.stdout:
            parts = result.stdout.split()
            fingerprint = next((p for p in parts if p.startswith("SHA256:")), None)
    return KeyMaterialSummary(path=str(private), public_key_path=str(public),
                              exists=private.exists(), created=created,
                              fingerprint=fingerprint, public_key=public_text)


def render_managed_block(paths: Iterable[DeploymentPath], *,
                         identity_file: str | None = None) -> str:
    """The ~/.ssh/config stanzas this project owns.

    Emitted between fixed markers so reconcile rewrites only its own lines.
    An operator's hand-written config outside the block is never read,
    reordered or touched.
    """
    lines = [BLOCK_BEGIN,
             "# Generated by terminal-mcp. Edit outside this block; anything",
             "# inside it is replaced on the next reconcile."]
    for path in sorted(paths, key=lambda p: (p.target_id, p.node_id)):
        if not path.ssh_alias or not path.host:
            continue
        lines.append(f"Host {path.ssh_alias}")
        lines.append(f"    HostName {path.host}")
        if path.username:
            lines.append(f"    User {path.username}")
        if path.port and int(path.port) != 22:
            lines.append(f"    Port {int(path.port)}")
        if identity_file:
            lines.append(f"    IdentityFile {identity_file}")
            lines.append("    IdentitiesOnly yes")
        # A managed path is a DIRECT path. Writing a ProxyJump here would
        # manufacture exactly the dependency this feature exists to detect.
        lines.append("    StrictHostKeyChecking yes")
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def apply_managed_block(config_text: str, block: str) -> str:
    """Replace our block, or append it. Everything else survives byte for byte."""
    if BLOCK_BEGIN in config_text and BLOCK_END in config_text:
        head, _, rest = config_text.partition(BLOCK_BEGIN)
        _, _, tail = rest.partition(BLOCK_END)
        return head.rstrip("\n") + ("\n\n" if head.strip() else "") + block + tail.lstrip("\n")
    separator = "\n" if config_text.endswith("\n") or not config_text else "\n\n"
    return config_text + separator + block
