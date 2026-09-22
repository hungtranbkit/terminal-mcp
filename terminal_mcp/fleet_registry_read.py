"""Fleet-aware session registry reads (task blg_84f09bbc1798).

`session_registry.py` is per-node-agent-process-local: every node keeps
its OWN session_registry.db, and `TerminalService.terminal_registry_
list/_search` only ever read the local one. That is correct for the node
agent itself (and MUST stay that way -- `/v1/registry` is served by
LocalNodeClient.registry_list, so making the local read fleet-aware
would make every node fan out to every other node on every controller
poll), but it means the question a user actually asks -- "where is my
session?" -- could only ever be answered one node at a time.

This module is the merge layer, and it is deliberately PURE: it takes
already-collected per-node results plus the node statuses the caller
already knows, and returns one merged view. No network, no store, no
clock it doesn't accept as an argument. controller.py does the
collecting; every rule below is testable without either.

THE FOUR RULES, and why each is what it is:

1. **Authoritative identity.** A registry row records its own node_id as
   `"local"` (core.REGISTRY_LOCAL_NODE_ID), because from inside a node
   agent that IS the truth. Merged across a fleet it is worse than
   useless: every node's rows claim to be "local", so identity collides
   and dedupe is impossible. In a fleet view `node_id` is therefore
   rewritten to the id of the node that actually answered, and the raw
   value is preserved as `source_node_id` so nothing is lost. `node_name`
   comes from the node registry, which is the only place a real display
   name exists.

2. **Dedupe on (owning node, session name).** The controller registers
   its OWN local node in `_clients`, so a naive fan-out returns the local
   node's rows twice -- once from the direct local read and once routed
   through its own client. Those are the same rows, and a merged list
   that shows both is simply wrong.

3. **Never invent remote data.** A node we could not reach contributes
   NO records -- not a cached guess, not an empty list silently merged in
   as if it had genuinely reported zero sessions. It is reported
   separately in `unavailable_nodes` with the real error, so a caller can
   tell "nothing there" from "could not look".

4. **UNKNOWN/stale must never look active.** A record whose source node
   is not confirmed fresh cannot be presented as ACTIVE: the process it
   describes may have died minutes ago and nobody would know. Such a
   record keeps its raw `status` verbatim (never rewritten -- that is the
   node's own last word, and callers depend on it) but gains
   `effective_status = "UNKNOWN"`. Statuses that are already not-active
   (MISSING/KILLED/OFFLINE/DELETED) are left alone: staleness cannot make
   a dead session more alive.

BACKWARD COMPATIBILITY. Every field an existing caller already reads is
carried through untouched, and local records are emitted FIRST in their
existing `last_seen_at DESC` order, so a local-only caller sees exactly
what it saw before at the head of the list. Everything this module adds
is a NEW key. The local-only entry points (`terminal_registry_list`,
`terminal_registry_search`, `/v1/registry`) are not changed at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

# Node health, mirrored from node_models (imported lazily by callers so
# this module stays dependency-free and trivially testable).
NODE_ONLINE = "online"
NODE_DEGRADED = "degraded"
NODE_OFFLINE = "offline"

SOURCE_LOCAL = "local"
SOURCE_FLEET = "fleet"

FRESHNESS_LIVE = "live"
FRESHNESS_STALE = "stale"
FRESHNESS_UNKNOWN = "unknown"

STATUS_ACTIVE = "ACTIVE"
EFFECTIVE_UNKNOWN = "UNKNOWN"

#: Cap borrowed from session_registry.list's own bounds, so a fleet read
#: can never be asked to materialise more than the per-node reads could.
MAX_LIMIT = 2000
DEFAULT_LIMIT = 500


@dataclass(frozen=True)
class NodeSource:
    """One node's contribution to a fleet read.

    `records is None` means "we did not get an answer" and is treated
    completely differently from `records == []` ("this node genuinely has
    no registry rows") -- conflating the two is exactly how a fleet view
    starts quietly losing sessions."""

    node_id: str
    node_name: str | None = None
    status: str = NODE_OFFLINE
    records: tuple[dict[str, Any], ...] | None = None
    error: str | None = None
    fetched_at: str | None = None
    #: True for the controller's own direct local read -- wins over the
    #: same node's routed client result (rule 2).
    is_local: bool = False

    @property
    def reachable(self) -> bool:
        return self.records is not None and self.error is None

    @property
    def display_name(self) -> str:
        return self.node_name or self.node_id


def node_freshness(source: NodeSource) -> str:
    """A node's records are LIVE only when it actually answered AND its
    heartbeat says it is online. A DEGRADED node that answered is still
    STALE: it answered, but its own liveness is in doubt, so what it says
    about a session being ACTIVE cannot be trusted as current."""
    if not source.reachable:
        return FRESHNESS_UNKNOWN
    if source.status == NODE_ONLINE:
        return FRESHNESS_LIVE
    return FRESHNESS_STALE


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def annotate_record(record: dict[str, Any], source: NodeSource, freshness: str) -> dict[str, Any]:
    """One source record -> one fleet record. Additive: every existing key
    survives untouched except `node_id`/`node_name`, which are corrected
    to the authoritative values (rule 1) with the originals preserved."""
    annotated = dict(record)
    annotated["source_node_id"] = record.get("node_id")
    annotated["source_node_name"] = record.get("node_name")
    annotated["node_id"] = source.node_id
    annotated["node_name"] = source.display_name
    annotated["node_status"] = source.status
    annotated["node_online"] = source.status == NODE_ONLINE
    annotated["source"] = SOURCE_LOCAL if source.is_local else SOURCE_FLEET
    annotated["freshness"] = freshness
    annotated["stale"] = freshness != FRESHNESS_LIVE
    annotated["fetched_at"] = source.fetched_at
    session_name = record.get("session_name")
    annotated["fleet_key"] = f"{source.node_id}/{session_name}"

    # Rule 4. `status` is NEVER rewritten -- it is the node's own last
    # word and existing callers read it. `effective_status` is the field
    # a UI should colour green.
    raw_status = record.get("status")
    if freshness != FRESHNESS_LIVE and raw_status == STATUS_ACTIVE:
        annotated["effective_status"] = EFFECTIVE_UNKNOWN
    else:
        annotated["effective_status"] = raw_status
    return annotated


def _precedence(candidate: dict[str, Any], incumbent: dict[str, Any]) -> bool:
    """True when `candidate` should replace `incumbent` for the same
    (node, session) identity. Ordered, disclosed, and deterministic:

    1. A live source beats a stale/unknown one -- freshness is the whole
       point of preferring one copy over another.
    2. The controller's own direct local read beats the same node's
       routed client result (rule 2) -- same rows, one fewer hop.
    3. Otherwise the more recently seen row wins, and a row with no
       `last_seen_at` at all never displaces one that has it."""
    candidate_live = candidate.get("freshness") == FRESHNESS_LIVE
    incumbent_live = incumbent.get("freshness") == FRESHNESS_LIVE
    if candidate_live != incumbent_live:
        return candidate_live

    candidate_local = candidate.get("source") == SOURCE_LOCAL
    incumbent_local = incumbent.get("source") == SOURCE_LOCAL
    if candidate_local != incumbent_local:
        return candidate_local

    candidate_seen = _parse_iso(candidate.get("last_seen_at"))
    incumbent_seen = _parse_iso(incumbent.get("last_seen_at"))
    if candidate_seen is None:
        return False
    if incumbent_seen is None:
        return True
    return candidate_seen > incumbent_seen


def merge_sources(sources: Iterable[NodeSource]) -> dict[str, Any]:
    """Merge per-node results into one fleet view.

    Local sources are emitted first, then remote ones in the order given,
    each preserving its own incoming order (`last_seen_at DESC` from
    session_registry.list) -- so the head of the list is byte-comparable
    with what a local-only caller already saw."""
    ordered = sorted(sources, key=lambda s: 0 if s.is_local else 1)

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    nodes: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    duplicates = 0

    for source in ordered:
        freshness = node_freshness(source)
        node_summary = {
            "node_id": source.node_id, "node_name": source.display_name,
            "status": source.status, "online": source.status == NODE_ONLINE,
            "reachable": source.reachable, "freshness": freshness,
            "source": SOURCE_LOCAL if source.is_local else SOURCE_FLEET,
            "fetched_at": source.fetched_at, "error": source.error,
            "record_count": 0,
        }
        if not source.reachable:
            # Rule 3: contributes nothing, and says so out loud.
            nodes.append(node_summary)
            unavailable.append({"node_id": source.node_id, "node_name": source.display_name,
                                "status": source.status, "error": source.error or "NODE_UNREACHABLE"})
            continue

        contributed = 0
        for record in source.records or ():
            if not isinstance(record, dict):
                continue
            session_name = record.get("session_name")
            if not session_name:
                continue
            annotated = annotate_record(record, source, freshness)
            identity = (source.node_id, str(session_name))
            existing = merged.get(identity)
            if existing is None:
                merged[identity] = annotated
                order.append(identity)
                contributed += 1
                continue
            duplicates += 1
            if _precedence(annotated, existing):
                # Keep the winner, but never lose the fact that another
                # source also had this session.
                annotated["shadowed_source"] = existing.get("source")
                merged[identity] = annotated
            else:
                existing["shadowed_source"] = annotated.get("source")
        node_summary["record_count"] = contributed
        nodes.append(node_summary)

    records = [merged[identity] for identity in order]
    return {
        "records": records,
        "nodes": nodes,
        "unavailable_nodes": unavailable,
        "counts": {
            "total": len(records),
            "local": sum(1 for r in records if r.get("source") == SOURCE_LOCAL),
            "remote": sum(1 for r in records if r.get("source") == SOURCE_FLEET),
            "deduped": duplicates,
            "nodes_reporting": sum(1 for n in nodes if n["reachable"]),
            "nodes_unavailable": len(unavailable),
        },
    }


def matches_query(record: dict[str, Any], query: str) -> bool:
    """The SAME matching rule as session_registry.search -- whitespace-
    separated words, each of which must appear somewhere in the row's
    combined searchable text, case-insensitive via casefold().

    Reimplemented against a record dict rather than a DB row because a
    remote node exposes only `/v1/registry` (a plain listing) -- there is
    no remote search endpoint to delegate to. Keeping the rule identical
    here is what stops fleet search from quietly having different
    semantics than local search; tests pin the two against each other.

    `source_node_id` is included so a row still matches its own raw node
    id after rule 1 rewrote `node_id`."""
    words = [w for w in (query or "").casefold().split() if w]
    if not words:
        return False
    haystack = " ".join(str(record.get(field) or "") for field in (
        "session_name", "cwd", "repo_root", "git_remote", "node_id",
        "source_node_id", "display_name", "notes",
    )).casefold()
    return all(word in haystack for word in words)


def paginate(records: list[dict[str, Any]], *, limit: int | None = None,
             cursor: int | None = None) -> dict[str, Any]:
    """Offset pagination over an already-merged, deterministically ordered
    list. An offset (rather than a keyset) is honest here: the list is
    assembled in memory from bounded per-node reads, so there is no
    index to key off, and pretending otherwise would imply a stability
    guarantee across fan-outs that does not exist."""
    effective = DEFAULT_LIMIT if limit is None else max(1, min(int(limit), MAX_LIMIT))
    offset = max(0, int(cursor or 0))
    page = records[offset:offset + effective]
    next_cursor = offset + effective if offset + effective < len(records) else None
    return {"records": page, "limit": effective, "cursor": offset,
            "next_cursor": next_cursor, "total": len(records),
            "has_more": next_cursor is not None}
