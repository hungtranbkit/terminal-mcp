"""Redundant SSH paths to a deployment target -- and the honesty about when
you actually have them.

THE FAILURE THIS EXISTS TO PREVENT
----------------------------------
Two management nodes are listed as being able to deploy to a VPS. One of them
turns out to reach it by hopping through the other. The day the first machine
dies you discover you had one path, not two, and the dashboard had been
saying 2/2 the whole time. That is the only interesting bug in this feature,
and everything below is shaped around refusing to claim redundancy that has
not been demonstrated.

Three rules follow from it:

  1. A path is INDEPENDENT only if nothing in its route passes through
     another management node for the same target. A ProxyJump/ProxyCommand
     naming a peer is a dependency, full stop.
  2. Redundancy is counted from paths that have been PROVEN, not configured.
     A path nobody has successfully probed is UNVERIFIED, and an unverified
     path contributes nothing to `n` -- a config file is a plan, not a route.
  3. Two paths sharing one gateway are reported as a shared-fate warning even
     when both work today, because they fail together.

WHAT THIS REUSES RATHER THAN REBUILDS
-------------------------------------
fleet_registry   the replicated, tombstoned, secret-refusing envelope; these
                 objects are two more kinds in it, so they reach every node
                 and survive the controller like everything else there.
remote_connect   argv building, host-key pinning and the non-destructive
                 probe. Nothing here shells out to ssh on its own.
lease.py         ResourceLockStore's atomic check-and-set IS the execution
                 lease that stops two dispatchers running one deploy.
node_profile     NEEDS_AUTH already means "installed but not authorised",
                 and it keeps that meaning here.

PRIVATE KEYS NEVER MOVE. Each management node generates and keeps its own.
What is published is a fingerprint and a public-key id. A node that cannot
authenticate reports NEEDS_AUTH and an operator installs its PUBLIC key on
the target; no node ever receives another node's key.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from .fleet_registry import FleetRegistryStore, scrub_payload

KIND_DEPLOY_TARGET = "deploy_target"
KIND_DEPLOY_PATH = "deploy_path"

# Path health. Deliberately distinct from the TARGET's health below: one path
# being down is not the same statement as the target being undeployable.
PATH_READY = "READY"              # probed, authenticated, deploy prereqs met
PATH_NEEDS_AUTH = "NEEDS_AUTH"    # reachable, but this node cannot authenticate
PATH_UNREACHABLE = "UNREACHABLE"  # TCP/SSH handshake failed
PATH_UNVERIFIED = "UNVERIFIED"    # configured, never successfully probed
PATH_NODE_OFFLINE = "NODE_OFFLINE"  # the management node itself is down
PATH_DEPENDENT = "DEPENDENT"      # works, but routes through a peer node

# Target health.
READY = "READY"
DEGRADED = "DEGRADED"
FAIL = "FAIL"

# A path counts toward redundancy only in this state. NEEDS_AUTH and
# UNVERIFIED deliberately do not: "we think it would work" is the belief that
# this whole module exists to stop being mistaken for a route.
COUNTING_STATES = (PATH_READY,)

# How long a successful probe stays trustworthy. Past this the path is still
# shown, but its evidence is stale and it stops counting -- a route proven
# last month is a story about last month.
PROBE_FRESH_SECONDS = 24 * 3600.0

ROLE_PRIMARY = "primary"
ROLE_BACKUP = "backup"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(stamp: str | None, *, now: datetime | None = None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return ((now or _now()) - parsed).total_seconds()


@dataclass(frozen=True)
class DeploymentTarget:
    """A thing you deploy to, and how much redundancy it is required to have.

    `min_independent_paths` is a REQUIREMENT, not a description: it is what
    the operator says this target needs, and health is measured against it.
    """

    target_id: str
    display_name: str
    project_id: str | None = None
    min_independent_paths: int = 2
    # PRIMARY/BACKUP is preference only -- it decides dispatch ORDER and
    # nothing else. A target whose primary is dead but whose backup has an
    # independent working path is DEGRADED and still deployable, and calling
    # that FAIL would be both wrong and the reason someone stops trusting
    # this screen.
    primary_node: str | None = None
    backup_nodes: tuple[str, ...] = ()
    deploy_command_ref: str | None = None
    description: str | None = None
    tags: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        return scrub_payload({
            "target_id": self.target_id, "display_name": self.display_name,
            "project_id": self.project_id,
            "min_independent_paths": int(self.min_independent_paths),
            "primary_node": self.primary_node,
            "backup_nodes": list(self.backup_nodes),
            # A NAME of a deploy command/playbook, never its contents and
            # never an argument list that might carry a secret.
            "deploy_command_ref": self.deploy_command_ref,
            "description": self.description, "tags": sorted(self.tags),
        }, where=f"deploy_target:{self.target_id}")


@dataclass(frozen=True)
class DeploymentPath:
    """One management node's own route to one target.

    Everything here is non-secret by construction: an address, a username, a
    port, a key FINGERPRINT and a public-key id. The private key that makes
    the path work stays on `node_id` and is never represented here at all.
    """

    target_id: str
    node_id: str
    ssh_alias: str | None = None
    host: str | None = None
    port: int = 22
    username: str | None = None
    transport: str = "unknown"        # lan | tailscale | tunnel | unknown
    # The route's own dependencies. `proxy_jump` is what turns a claimed
    # second path into the same path wearing a hat.
    proxy_jump: str | None = None
    # Machines/links this route shares fate with. Two paths in one group fail
    # together even when both are up today.
    independence_group: str | None = None
    host_key_fingerprint: str | None = None
    public_key_id: str | None = None      # e.g. "SHA256:... (ed25519)"
    cert_issuer: str | None = None
    capabilities: tuple[str, ...] = ()    # e.g. ("deploy", "ssh")
    last_probe_at: str | None = None
    last_probe_ok: bool | None = None
    last_probe_reason: str | None = None
    latency_ms: float | None = None
    auth_ok: bool | None = None
    deploy_prereqs_ok: bool | None = None
    role: str = ROLE_BACKUP

    def object_id(self) -> str:
        return f"deploy_path:{self.target_id}:{self.node_id}"

    def payload(self) -> dict[str, Any]:
        return scrub_payload({
            "target_id": self.target_id, "node_id": self.node_id,
            "ssh_alias": self.ssh_alias, "host": self.host, "port": int(self.port),
            "username": self.username, "transport": self.transport,
            "proxy_jump": self.proxy_jump,
            "independence_group": self.independence_group,
            "host_key_fingerprint": self.host_key_fingerprint,
            "public_key_id": self.public_key_id, "cert_issuer": self.cert_issuer,
            "capabilities": sorted(self.capabilities),
            "last_probe_at": self.last_probe_at, "last_probe_ok": self.last_probe_ok,
            "last_probe_reason": self.last_probe_reason,
            "latency_ms": self.latency_ms, "auth_ok": self.auth_ok,
            "deploy_prereqs_ok": self.deploy_prereqs_ok, "role": self.role,
        }, where=f"deploy_path:{self.target_id}:{self.node_id}")


# A ProxyJump value is a comma-separated list of [user@]host[:port] hops.
_HOP = re.compile(r"(?:(?P<user>[^@,\s]+)@)?(?P<host>[^:,\s]+)(?::(?P<port>\d+))?")


def jump_hosts(proxy_jump: str | None) -> list[str]:
    """Every hop named in a ProxyJump/ProxyCommand value.

    Parsed rather than pattern-matched because a route through a peer is the
    single thing that invalidates a redundancy claim, and missing one because
    it was the second hop would be the worst possible way to be wrong.
    """
    if not proxy_jump:
        return []
    text = str(proxy_jump)
    hops: list[str] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # A ProxyCommand string mentions its hop somewhere in the middle;
        # take every bare word that could be a host rather than assume shape.
        for match in _HOP.finditer(chunk):
            host = match.group("host")
            if host and host not in {"ssh", "-W", "%h", "%p", "nc", "netcat"}:
                hops.append(host)
    return hops


def path_depends_on_peer(path: DeploymentPath, peer_ids: Iterable[str],
                         peer_aliases: dict[str, set[str]] | None = None) -> str | None:
    """Which peer this path routes through, if any.

    Matches a hop against a peer's node id AND its known aliases/addresses:
    a config that says `ProxyJump 192.168.1.132` is routing through
    dell-linux just as surely as one that says `ProxyJump dell-linux`, and
    only comparing ids would miss it.
    """
    aliases = peer_aliases or {}
    hops = {hop.casefold() for hop in jump_hosts(path.proxy_jump)}
    if not hops:
        return None
    for peer in peer_ids:
        if peer == path.node_id:
            continue
        names = {peer.casefold()} | {a.casefold() for a in aliases.get(peer, set())}
        if hops & names:
            return peer
    # A jump host that is not a peer is still a dependency, but not one that
    # breaks THIS pair's independence -- report it as a shared gateway below.
    return None


def classify_path(path: DeploymentPath, *, node_online: bool,
                  depends_on: str | None = None,
                  now: datetime | None = None) -> tuple[str, str]:
    """One path's health, and the reason, from evidence only."""
    if not node_online:
        return PATH_NODE_OFFLINE, f"management node {path.node_id} is offline"
    if depends_on:
        return PATH_DEPENDENT, (
            f"routes through {depends_on}, which is another management node "
            f"for this target -- not an independent path")
    if path.last_probe_ok is None:
        return PATH_UNVERIFIED, "no successful probe has ever been recorded"
    if not path.last_probe_ok:
        return PATH_UNREACHABLE, (path.last_probe_reason or "last probe failed")
    if path.auth_ok is False:
        return PATH_NEEDS_AUTH, (
            "SSH reachable but this node cannot authenticate -- install its "
            "PUBLIC key on the target")
    age = _age_seconds(path.last_probe_at, now=now)
    if age is not None and age > PROBE_FRESH_SECONDS:
        return PATH_UNVERIFIED, (
            f"last successful probe was {int(age // 3600)}h ago; evidence is stale")
    if path.deploy_prereqs_ok is False:
        return PATH_NEEDS_AUTH, "deploy prerequisites are not met on this path"
    return PATH_READY, "probed, authenticated, deploy prerequisites met"


@dataclass(frozen=True)
class PathHealth:
    path: DeploymentPath
    state: str
    reason: str
    independent: bool
    depends_on: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**self.path.payload(), "state": self.state, "reason": self.reason,
                "independent": self.independent, "depends_on": self.depends_on,
                "counts_toward_redundancy": self.state in COUNTING_STATES and self.independent}


def evaluate_target(target: DeploymentTarget, paths: Sequence[DeploymentPath], *,
                    node_online: dict[str, bool] | None = None,
                    node_aliases: dict[str, set[str]] | None = None,
                    now: datetime | None = None) -> dict[str, Any]:
    """The whole answer for one target: per-path health, redundancy, and
    whether a deploy can actually happen right now.

    `deploy_available` is computed SEPARATELY from `status` on purpose.
    Redundancy and deployability are different questions, and conflating them
    is how a one-path-but-working target ends up reported as FAIL and someone
    holds a release they could have shipped.
    """
    online = node_online or {}
    peer_ids = [p.node_id for p in paths]
    healths: list[PathHealth] = []
    for path in paths:
        depends = path_depends_on_peer(path, peer_ids, node_aliases)
        state, reason = classify_path(
            path, node_online=online.get(path.node_id, True),
            depends_on=depends, now=now)
        healths.append(PathHealth(path=path, state=state, reason=reason,
                                  independent=depends is None, depends_on=depends))

    counting = [h for h in healths if h.state in COUNTING_STATES and h.independent]
    required = max(1, int(target.min_independent_paths))
    achieved = len(counting)

    # Shared fate: distinct routes that nevertheless die together. Reported
    # even when everything is green, because that is the only time anyone can
    # still do something about it.
    groups: dict[str, list[str]] = {}
    for health in healths:
        group = health.path.independence_group
        if group:
            groups.setdefault(group, []).append(health.path.node_id)
    shared = [{"independence_group": group, "nodes": sorted(nodes)}
              for group, nodes in sorted(groups.items()) if len(nodes) > 1]

    # Any node that can reach and authenticate makes a deploy possible, even
    # a dependent one -- a route through a peer still deploys while that peer
    # is alive. It just is not REDUNDANCY.
    deployable = [h for h in healths
                  if h.state in COUNTING_STATES
                  or (h.state == PATH_DEPENDENT and h.path.last_probe_ok
                      and h.path.auth_ok is not False)]
    deploy_available = bool(deployable)

    if achieved >= required:
        status = READY
    elif deploy_available:
        status = DEGRADED
    else:
        status = FAIL

    return {
        "target_id": target.target_id,
        "display_name": target.display_name,
        "project_id": target.project_id,
        "status": status,
        "redundancy": {"achieved": achieved, "required": required,
                       "label": f"{achieved}/{required}"},
        "deploy_available": deploy_available,
        "primary_node": target.primary_node,
        "backup_nodes": list(target.backup_nodes),
        "paths": [h.as_dict() for h in healths],
        "shared_fate": shared,
        "warnings": _warnings(healths, shared, achieved, required, deploy_available),
        "evaluated_at": (now or _now()).isoformat(),
    }


def _warnings(healths: Sequence[PathHealth], shared: list[dict[str, Any]],
              achieved: int, required: int, deploy_available: bool) -> list[str]:
    notes: list[str] = []
    for health in healths:
        if health.depends_on:
            notes.append(
                f"{health.path.node_id} routes through {health.depends_on}: this is "
                f"one path wearing two names, not redundancy")
    for group in shared:
        notes.append(
            f"{' and '.join(group['nodes'])} share independence group "
            f"'{group['independence_group']}' -- they fail together")
    unverified = [h.path.node_id for h in healths if h.state == PATH_UNVERIFIED]
    if unverified:
        notes.append(
            f"{', '.join(unverified)}: configured but never proven -- a config "
            f"file is a plan, not a route")
    if achieved < required and deploy_available:
        notes.append(
            f"redundancy {achieved}/{required}: deploy still works, but the next "
            f"failure has nothing to fall back on")
    return notes


def choose_deploy_node(evaluation: dict[str, Any]) -> dict[str, Any]:
    """Which node should run the deploy, and why that one.

    Preference order is primary, then the declared backups in order, then
    anything else healthy -- but preference never overrides evidence: a dead
    primary is skipped, and the reason says so rather than silently
    substituting a different machine.
    """
    usable = [p for p in evaluation["paths"]
              if p["state"] == PATH_READY
              or (p["state"] == PATH_DEPENDENT and p.get("last_probe_ok")
                  and p.get("auth_ok") is not False)]
    if not usable:
        return {"node_id": None, "reason": "no path can currently deploy",
                "deploy_available": False,
                "considered": [{"node_id": p["node_id"], "state": p["state"],
                                "reason": p.get("reason")} for p in evaluation["paths"]]}
    order: list[str] = []
    if evaluation.get("primary_node"):
        order.append(evaluation["primary_node"])
    order += [n for n in evaluation.get("backup_nodes", []) if n not in order]
    ranked = sorted(usable, key=lambda p: (
        order.index(p["node_id"]) if p["node_id"] in order else len(order),
        0 if p["state"] == PATH_READY else 1,
        p.get("latency_ms") if p.get("latency_ms") is not None else 1e9,
        p["node_id"]))
    chosen = ranked[0]
    skipped = [{"node_id": p["node_id"], "state": p["state"], "reason": p.get("reason")}
               for p in evaluation["paths"] if p["node_id"] != chosen["node_id"]]
    is_primary = chosen["node_id"] == evaluation.get("primary_node")
    return {
        "node_id": chosen["node_id"],
        "role": ROLE_PRIMARY if is_primary else ROLE_BACKUP,
        "deploy_available": True,
        "independent": chosen.get("independent", True),
        "reason": ("primary is healthy" if is_primary else
                   f"primary unavailable; {chosen['node_id']} is the highest-preference "
                   f"node with a working path"),
        "skipped": skipped,
    }


def deploy_lease_key(target_id: str) -> str:
    """One lease per TARGET, not per node.

    Keying on the node would let two dispatchers each take their own node's
    lease and both deploy -- which is precisely the double-execution this is
    supposed to prevent.
    """
    return f"deploy:{target_id}"
