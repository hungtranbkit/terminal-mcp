"""Moving fleet objects between machines, in both directions.

THE POINT IS THAT THIS IS NOT HUB-AND-SPOKE
-------------------------------------------
Today's topology is one controller polling `/v1/*` on each node agent; the
agents know nothing about the fleet. That is why losing m910 loses the fleet
view. The sync below is deliberately SYMMETRIC -- an exchange, not a fetch:
each side sends what it has and merges what it receives, and `merge` is
idempotent and ownership-scoped, so it produces the same state regardless of
who initiated. A controller running it against three nodes and a node running
it against two peers are the same operation.

Phase 1 (this change) still has the controller INITIATE, because it is the
process that holds the node tokens and can reach everyone. But the wire
contract, the store and the merge are peer-symmetric and already live on
every node, so turning on node-initiated sync is a scheduling change, not a
redesign. Concretely, for M910-off: every node already holds a full durable
copy the moment it has synced once, and `FleetSyncService.exchange` works in
whichever direction it is called.

NOTHING SECRET CROSSES THE WIRE. Every object is scrubbed on projection, on
export and again on merge (`scrub_payload` raises rather than strips). The
node-agent bearer token used to AUTHENTICATE a sync is the existing per-node
token this controller already holds; it is never part of a payload.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .fleet_registry import FleetObject, FleetRegistryStore, SecretLeak

SYNC_PATH = "/v1/fleet/objects"
# A single exchange is capped so one enormous fleet cannot produce a request
# that times out forever and never makes progress. The cursor makes the next
# call resume, and merge idempotency makes a partial batch safe.
MAX_BATCH = 2000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SyncResult:
    peer_node: str
    ok: bool
    pulled: int = 0
    pushed: int = 0
    applied: int = 0
    skipped: int = 0
    rejected: int = 0
    error: str | None = None
    error_class: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"peer_node": self.peer_node, "ok": self.ok, "pulled": self.pulled,
                "pushed": self.pushed, "applied": self.applied, "skipped": self.skipped,
                "rejected": self.rejected, "error": self.error,
                "error_class": self.error_class}


class FleetSyncService:
    """One exchange with one peer, and a sweep over many.

    `transport` is injected rather than imported so this is testable without a
    network and so the same service works over the node-agent HTTP client, a
    future peer-to-peer channel, or a local in-process pair.
    """

    def __init__(self, store: FleetRegistryStore, *, local_node_id: str,
                 transport: Callable[..., dict[str, Any]] | None = None) -> None:
        self.store = store
        self.local_node_id = local_node_id
        self._transport = transport

    # -- one peer ------------------------------------------------------------

    def exchange(self, peer_node: str, *, transport: Callable[..., dict[str, Any]] | None = None,
                 endpoint: str | None = None, since: str | None = None) -> SyncResult:
        """Send ours, merge theirs. Order matters only for failure reporting.

        A peer that is down is a NORMAL state, not an error to escalate: the
        local copy is still authoritative for reading, which is the entire
        reason it exists. So a transport failure is recorded against the peer
        and returned, never raised into the caller's sweep.
        """
        send = transport or self._transport
        if send is None:
            raise RuntimeError("no transport configured for fleet sync")
        outgoing = [obj.as_dict() for obj in self.store.export(since=since)][:MAX_BATCH]
        try:
            response = send(peer_node=peer_node, endpoint=endpoint,
                            objects=outgoing, since=since)
        except Exception as exc:  # noqa: BLE001 -- an unreachable peer is normal
            error = f"{type(exc).__name__}: {exc}"
            self.store.record_sync(peer_node, endpoint=endpoint, error=error)
            return SyncResult(peer_node=peer_node, ok=False, pushed=0,
                              error=error, error_class=type(exc).__name__)
        incoming = response.get("objects") or []
        merged = self.store.merge(incoming, source_node=peer_node)
        accepted = int((response.get("merge") or {}).get("applied", 0))
        self.store.record_sync(peer_node, endpoint=endpoint,
                               pulled=len(incoming), pushed=accepted)
        return SyncResult(peer_node=peer_node, ok=True, pulled=len(incoming),
                          pushed=accepted, applied=merged["applied"],
                          skipped=merged["skipped"], rejected=merged["rejected"])

    def sweep(self, peers: Iterable[tuple[str, str | None]]) -> list[SyncResult]:
        """Every peer, one at a time, never stopping on a failure.

        Sequential on purpose: a sync is cheap and infrequent, and doing them
        in parallel would multiply the blast radius of a peer that hangs.
        """
        return [self.exchange(peer, endpoint=endpoint) for peer, endpoint in peers]

    # -- serving side --------------------------------------------------------

    def handle_exchange(self, body: dict[str, Any], *,
                        source_node: str | None = None) -> dict[str, Any]:
        """The other half: merge what a peer sent, answer with what we have.

        This is what makes the protocol symmetric -- the responder does
        exactly what the initiator does, in the opposite order.
        """
        objects = body.get("objects") or []
        if not isinstance(objects, list):
            return {"error": "objects must be a list", "objects": [], "merge": {}}
        merged = self.store.merge(objects, source_node=source_node or "peer")
        since = body.get("since")
        ours = [obj.as_dict() for obj in self.store.export(since=since if isinstance(since, str) else None)]
        return {
            "local_node_id": self.local_node_id,
            "merge": merged,
            "objects": ours[:MAX_BATCH],
            "truncated": len(ours) > MAX_BATCH,
            "at": _now(),
        }


def http_transport(client_for: Callable[[str], Any]) -> Callable[..., dict[str, Any]]:
    """Transport over the existing node-agent HTTP client.

    Reuses whatever auth that client already has -- this module never sees,
    stores or forwards a token.
    """
    def send(*, peer_node: str, endpoint: str | None, objects: list[dict[str, Any]],
             since: str | None) -> dict[str, Any]:
        client = client_for(peer_node)
        return client.post_json(SYNC_PATH, {"objects": objects, "since": since,
                                            "from": peer_node})
    return send
