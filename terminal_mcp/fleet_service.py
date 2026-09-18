"""The one entry point the API, the MCP tools, the doctor and the dashboard
all use for fleet metadata.

Keeps the split clean: `fleet_registry` is storage and merge rules,
`fleet_projection` turns local truth into replicable facts, `fleet_sync`
moves them, and this decides WHEN -- and answers the questions an operator
actually asks ("what nodes exist", "how do I reach dell-linux", "is what I am
looking at stale").

Every read here works with the controller gone. That is not a side effect: it
is the reason the local store exists, and `offline_view` is the method the
M910-off story is tested through.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from .fleet_projection import (SshTargetFacts, local_network_identity, project_nodes,
                               project_projects, project_sessions, project_ssh_targets,
                               ssh_targets_from_config, ssh_targets_from_connections)
from .fleet_registry import (CRED_MISSING, CRED_NEEDS_AUTH, KIND_NODE, KIND_PROJECT,
                             KIND_SESSION, KIND_SSH_TARGET, FleetRegistryStore)
from .fleet_sync import FleetSyncService

# How old a node's replicated view may get before the UI should say so. Well
# above the sync interval so an ordinary slow cycle is not called stale, well
# below "useless" so a genuinely stranded node is obvious.
STALE_AFTER_SECONDS = 900.0

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(stamp: str | None, *, now: datetime | None = None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return ((now or _now()) - parsed).total_seconds()


class FleetService:
    def __init__(self, store: FleetRegistryStore, *, local_node_id: str,
                 sync: FleetSyncService | None = None) -> None:
        self.store = store
        self.local_node_id = local_node_id
        self.sync = sync or FleetSyncService(store, local_node_id=local_node_id)
        # Set by whoever starts the refresh loop. Optional: a node agent and
        # every test construct this service without one, and readiness simply
        # omits the loop check rather than inventing a verdict about a loop
        # that does not exist here.
        self._sync_loop: Any = None

    def attach_sync_loop(self, loop: Any) -> None:
        self._sync_loop = loop

    def sync_loop_status(self) -> dict[str, Any] | None:
        loop = self._sync_loop
        if loop is None:
            return None
        try:
            return loop.status()
        except Exception:  # noqa: BLE001 -- readiness never fails on introspection
            return {"enabled": True, "running": False, "last_error": "status unavailable"}

    # -- publishing this node's truth ---------------------------------------

    def refresh_local(self, *, nodes: Iterable[Any] = (), sessions: Iterable[Any] = (),
                      connections: Iterable[Any] = (), ssh_config_path=None,
                      network: dict[str, Any] | None = None,
                      include_ssh_config: bool = True) -> dict[str, Any]:
        """Re-project everything this node knows. Safe to call on every poll.

        Idempotent by construction: `publish` only mints a revision when the
        content hash moves, so a quiet fleet produces zero new revisions and
        the next sync transfers nothing.
        """
        sessions = list(sessions)
        summary: dict[str, Any] = {
            "nodes": project_nodes(self.store, nodes, local_node_id=self.local_node_id),
            "sessions": project_sessions(self.store, sessions),
            "projects": project_projects(self.store, sessions,
                                         local_node_id=self.local_node_id),
        }
        targets: list[SshTargetFacts] = list(ssh_targets_from_connections(connections))
        if include_ssh_config:
            targets += ssh_targets_from_config(ssh_config_path)
        summary["ssh"] = project_ssh_targets(self.store, targets,
                                             local_node_id=self.local_node_id)
        # This machine's own addresses, attached to its own node object --
        # CREATING that object if nothing else published one. A bare node
        # agent has no node_registry rows at all, so without this the one
        # machine that most needs to announce how it can be reached would be
        # the only one that never did. It is also what makes a peer able to
        # find a survivor after the controller is gone.
        identity = network if network is not None else local_network_identity()
        object_id = f"node:{self.local_node_id}"
        existing = self.store.get(KIND_NODE, object_id)
        payload = dict(existing.payload) if (existing and not existing.deleted) else {
            "node_id": self.local_node_id,
        }
        payload.update({key: value for key, value in identity.items()})
        self.store.publish(KIND_NODE, object_id, payload, owner_node=self.local_node_id)
        summary["network"] = identity
        return summary

    # -- reading, with or without a controller -------------------------------

    def offline_view(self, *, now: datetime | None = None) -> dict[str, Any]:
        """The whole fleet as last replicated, from local disk only.

        Makes no network call by design -- this is exactly what a node reads
        when the controller is unreachable, so it must not be able to block
        on one.
        """
        stamp = now or _now()
        nodes = []
        for obj in self.store.list(kind=KIND_NODE):
            age = _age_seconds(obj.updated_at, now=stamp)
            nodes.append({
                **obj.payload,
                "object_id": obj.object_id, "owner_node": obj.owner_node,
                "revision": obj.revision, "metadata_updated_at": obj.updated_at,
                "metadata_age_seconds": round(age, 1) if age is not None else None,
                "metadata_stale": bool(age is not None and age > STALE_AFTER_SECONDS),
                "source_node": obj.source_node,
            })
        nodes.sort(key=lambda n: (0 if n.get("node_id") == self.local_node_id else 1,
                                  str(n.get("display_name") or n.get("node_id") or "")))
        sessions = [{**obj.payload, "revision": obj.revision,
                     "metadata_updated_at": obj.updated_at}
                    for obj in self.store.list(kind=KIND_SESSION)]
        projects = [{**obj.payload, "revision": obj.revision}
                    for obj in self.store.list(kind=KIND_PROJECT)]
        return {
            "local_node_id": self.local_node_id,
            "served_from": "local_cache",
            "nodes": nodes, "sessions": sessions, "projects": projects,
            "ssh_targets": self.ssh_inventory(now=stamp),
            "peers": self.store.peers(),
            "generated_at": stamp.isoformat(),
        }

    def ssh_inventory(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        """SSH targets, ordered the way an operator would try them.

        `preferred_order` first (a pinned connection beats a config guess),
        then tailnet ahead of LAN -- a tailnet address keeps working from
        outside the building, which is the case you are in when you need this.
        """
        rank = {"tailscale": 0, "lan": 1, "tunnel": 2, "unknown": 3}
        rows = []
        for obj in self.store.list(kind=KIND_SSH_TARGET):
            age = _age_seconds(obj.payload.get("last_verified_at"), now=now)
            rows.append({**obj.payload, "object_id": obj.object_id,
                         "owner_node": obj.owner_node, "revision": obj.revision,
                         "metadata_updated_at": obj.updated_at,
                         "last_verified_age_seconds": round(age, 1) if age is not None else None})
        rows.sort(key=lambda r: (int(r.get("preferred_order") or 100),
                                 rank.get(str(r.get("transport")), 9),
                                 str(r.get("alias") or "")))
        return rows

    # -- readiness -----------------------------------------------------------

    def readiness(self, *, now: datetime | None = None,
                  known_fingerprints: dict[str, str] | None = None) -> dict[str, Any]:
        """PASS / WARN / FAIL per check, with the evidence that decided it.

        A missing credential is WARN, not FAIL: the fleet is still readable
        and the node is still usable locally -- what it cannot do is take
        over, and saying FAIL for that would train an operator to ignore it.
        A FINGERPRINT MISMATCH is FAIL, always: that is the one condition here
        that can mean someone is impersonating a node.
        """
        stamp = now or _now()
        checks: list[dict[str, Any]] = []
        view = self.offline_view(now=stamp)

        # A stale registry now has a specific, actionable cause: either the
        # refresh loop is not running, or it is running and failing. Saying
        # only "metadata is old" sends an operator looking at the nodes when
        # the problem is on this box.
        loop = self.sync_loop_status() if callable(getattr(self, "sync_loop_status", None)) else None
        if loop is not None:
            if not loop.get("enabled"):
                status, summary = WARN, "fleet refresh loop is disabled by config"
            elif not loop.get("running"):
                status, summary = FAIL, "fleet refresh loop is not running; metadata will go stale"
            elif loop.get("last_error"):
                status, summary = WARN, f"last refresh cycle reported {loop['last_error']}"
            else:
                status, summary = PASS, (
                    f"refreshing every {loop.get('interval_seconds')}s; "
                    f"last cycle {loop.get('age_seconds')}s ago")
            checks.append(_check("fleet_refresh_loop", status, summary, {
                "enabled": loop.get("enabled"), "running": loop.get("running"),
                "cycles": loop.get("cycles"), "age_seconds": loop.get("age_seconds"),
                "interval_seconds": loop.get("interval_seconds"),
                "peers": loop.get("peers")}))

        stale = [n for n in view["nodes"] if n.get("metadata_stale")]
        never = [n for n in view["nodes"] if n.get("metadata_age_seconds") is None]
        checks.append(_check(
            "registry_sync_age",
            FAIL if not view["nodes"] else (WARN if stale or never else PASS),
            ("no node metadata has ever been replicated" if not view["nodes"]
             else f"{len(stale)} node(s) older than {int(STALE_AFTER_SECONDS)}s"
             if stale else "every node's metadata is current"),
            {"stale_nodes": [n.get("node_id") for n in stale],
             "node_count": len(view["nodes"])}))

        versions = {n.get("node_id"): n.get("contract_version") for n in view["nodes"]}
        distinct = {v for v in versions.values() if v is not None}
        checks.append(_check(
            "contract_version_drift",
            WARN if len(distinct) > 1 else PASS,
            (f"nodes report contract versions {sorted(distinct)}" if len(distinct) > 1
             else "every node speaks the same contract generation"),
            {"versions": versions}))

        missing = [t for t in view["ssh_targets"]
                   if t.get("credential_status") in (CRED_MISSING, CRED_NEEDS_AUTH)]
        checks.append(_check(
            "ssh_credentials", WARN if missing else PASS,
            (f"{len(missing)} SSH target(s) have no usable local credential"
             if missing else "every SSH target has a local credential"),
            {"targets": [{"alias": t.get("alias"), "status": t.get("credential_status")}
                         for t in missing]}))

        unpinned = [t for t in view["ssh_targets"] if not t.get("host_key_fingerprint")]
        checks.append(_check(
            "ssh_host_key_pinning", WARN if unpinned else PASS,
            (f"{len(unpinned)} SSH target(s) have no pinned host key"
             if unpinned else "every SSH target is pinned"),
            {"targets": [t.get("alias") for t in unpinned]}))

        # A fingerprint that CHANGED is a different claim from one that was
        # never taken, and the only check here that can indicate impersonation.
        mismatches = []
        for target in view["ssh_targets"]:
            expected = (known_fingerprints or {}).get(str(target.get("alias")))
            actual = target.get("host_key_fingerprint")
            if expected and actual and expected != actual:
                mismatches.append({"alias": target.get("alias"),
                                   "expected": expected, "observed": actual})
        checks.append(_check(
            "ssh_fingerprint_mismatch", FAIL if mismatches else PASS,
            (f"{len(mismatches)} target(s) present a different host key than pinned"
             if mismatches else "no host key has changed"),
            {"mismatches": mismatches}))

        peers = view["peers"]
        broken = [p for p in peers if p.get("last_error")]
        checks.append(_check(
            "peer_sync", WARN if broken or not peers else PASS,
            ("no peer has ever been synced" if not peers
             else f"{len(broken)} peer(s) failed their last sync" if broken
             else "every peer synced cleanly"),
            {"peers": [{"peer_node": p.get("peer_node"), "last_error": p.get("last_error"),
                        "last_ok_at": p.get("last_ok_at")} for p in peers]}))

        # "Never verified" and "verified last week" are different claims, and
        # collapsing them made this check pass on a fleet whose routes had
        # never been tested at all -- including one whose address is known to
        # be dead. Both are reported, separately.
        week = 7 * 24 * 3600
        aged = [t for t in view["ssh_targets"]
                if (t.get("last_verified_age_seconds") or 0) > week]
        unverified = [t for t in view["ssh_targets"]
                      if t.get("last_verified_age_seconds") is None]
        checks.append(_check(
            "stale_routes", WARN if (aged or unverified) else PASS,
            (f"{len(aged)} route(s) unverified for over a week, "
             f"{len(unverified)} never verified at all"
             if (aged or unverified) else "every route has been verified recently"),
            {"aged": [t.get("alias") for t in aged],
             "never_verified": [t.get("alias") for t in unverified]}))

        # Deployment redundancy, folded into the SAME readiness report rather
        # than a second doctor: an operator asking "is the fleet ok" should
        # not have to know that redundancy lives somewhere else.
        checks.extend(self._deployment_checks(stamp))

        worst = FAIL if any(c["status"] == FAIL for c in checks) else (
            WARN if any(c["status"] == WARN for c in checks) else PASS)
        return {"status": worst, "checks": checks, "local_node_id": self.local_node_id,
                "generated_at": stamp.isoformat()}


    def _deployment_checks(self, stamp: datetime) -> list[dict[str, Any]]:
        """Per-target redundancy, reported as ordinary readiness checks.

        A target that is DEGRADED but deployable is WARN, never FAIL -- the
        release can still ship, and crying FAIL over it is how a team learns
        to ignore this screen. Only a target that cannot deploy at all is
        FAIL.
        """
        try:
            from .deployment_redundancy import DEGRADED, FAIL as T_FAIL, READY as T_READY
            from .deployment_service import DeploymentRegistry, DeploymentService
        except Exception:  # noqa: BLE001 -- optional surface
            return []
        registry = DeploymentRegistry(self.store, local_node_id=self.local_node_id)
        if not registry.targets():
            return []
        online = {}
        for node in self.offline_view(now=stamp)["nodes"]:
            node_id = node.get("node_id")
            if node_id:
                online[node_id] = str(node.get("status") or "").casefold() != "offline"
        service = DeploymentService(registry, node_online=lambda: online)
        checks: list[dict[str, Any]] = []
        for evaluation in service.evaluate_all():
            status = (PASS if evaluation["status"] == T_READY
                      else WARN if evaluation["deploy_available"] else FAIL)
            checks.append(_check(
                f"deployment_redundancy:{evaluation['target_id']}", status,
                (f"{evaluation['redundancy']['label']} independent path(s); "
                 f"deploy {'available' if evaluation['deploy_available'] else 'UNAVAILABLE'}"),
                {"status": evaluation["status"],
                 "redundancy": evaluation["redundancy"],
                 "deploy_available": evaluation["deploy_available"],
                 "warnings": evaluation["warnings"],
                 "paths": [{"node_id": p["node_id"], "state": p["state"],
                            "independent": p["independent"], "depends_on": p["depends_on"],
                            "reason": p["reason"]}
                           for p in evaluation["paths"]]}))
        return checks



def auth_status_for_node(node: dict[str, Any]) -> tuple[str, str]:
    """Whether a node is authenticated -- or whether we simply cannot say.

    The fleet view is a CACHE. Reading `status` straight out of it and
    labelling the node AUTHENTICATED asserts something about right now from
    evidence that may be hours old, which is how an operator gets told a dead
    node is fine. Observed live on 2026-09-12: hp-linux had been offline for
    five minutes while this reported AUTHENTICATED from a 6.8-hour-old cache.

    So staleness wins over the cached verdict. "I don't know" is a worse
    answer to give and a better one to be right about.
    """
    if node.get("metadata_stale"):
        age = node.get("metadata_age_seconds")
        when = f"{int(age // 3600)}h" if isinstance(age, (int, float)) and age >= 3600 else (
            f"{int(age)}s" if isinstance(age, (int, float)) else "unknown age")
        return "UNKNOWN_STALE", (
            f"fleet metadata for this node is {when} old; its live auth state "
            f"has not been observed since")
    if str(node.get("status") or "").casefold() == "online":
        return "AUTHENTICATED", "node answered its most recent heartbeat"
    return "UNREACHABLE", f"node status is {node.get('status') or 'unknown'}"


def _check(name: str, status: str, summary: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"check": name, "status": status, "summary": summary, "evidence": evidence}


class ControllerFleetSync:
    """The controller's sweep: re-project local truth, then exchange with
    every reachable node.

    This is Phase 1's initiator. It is NOT the protocol's centre -- the same
    `exchange` runs node-to-node -- but the controller is the process that
    already holds every node's token, so it is the cheapest place to schedule
    from today. A node whose agent predates the fleet endpoint answers 404 and
    is recorded as unsupported rather than failed: an old node must not make
    the fleet look broken.
    """

    def __init__(self, service: FleetService, controller: Any) -> None:
        self.service = service
        self.controller = controller

    def _peer_transport(self):
        def send(*, peer_node: str, endpoint: str | None, objects, since):
            client = self.controller.client_for(peer_node)
            return client.fleet_exchange(objects=objects, since=since,
                                         source_node=self.service.local_node_id)
        return send

    def run_once(self, *, sessions: Iterable[Any] = (), connections: Iterable[Any] = (),
                 network: dict[str, Any] | None = None) -> dict[str, Any]:
        nodes = list(self.controller.list_nodes())
        refreshed = self.service.refresh_local(
            nodes=nodes, sessions=sessions, connections=connections, network=network)
        results = []
        transport = self._peer_transport()
        for node in nodes:
            node_id = getattr(node, "id", None)
            if not node_id or node_id == self.service.local_node_id:
                continue
            result = self.service.sync.exchange(node_id, transport=transport,
                                                endpoint=getattr(node, "endpoint", None))
            results.append(result.as_dict())
        return {"refreshed": refreshed, "peers": results,
                "at": _now().isoformat()}
