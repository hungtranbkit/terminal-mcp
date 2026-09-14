"""Fleet audit aggregation -- the READ path that answers "who sent what,
where" across the fleet without moving anybody's audit rows.

BACKLOG blg_178d7b6506b7: "Each node writes its own audit.db. There is no
fleet-wide audit view, so answering 'who sent what, where' means reading N
databases."

WHAT THIS IS NOT. It is not replication. Nothing is copied, shipped,
mirrored or written anywhere: each node's `audit.db` remains the single
source of truth for that node's own rows, exactly as before, and this
module never writes. A read-time scatter-gather over the fleet transport
that already exists (NodeClient) is enough to answer the question, and an
aggregation store would immediately need its own retention, backfill,
conflict and drift story to answer the same question less truthfully. If
a later requirement genuinely needs history for nodes that are offline,
THAT is when a store earns its place -- see "Known limitations" in
docs/REQUIREMENTS.md.

IDENTITY AND PROVENANCE. `input_audit.id` is a per-node AUTOINCREMENT
integer, so id 41 exists on every node and means something different on
each. Two nodes' rows are therefore never distinguishable by id alone,
which is the whole dedupe problem. Every aggregated row carries:

    node_id       the CONTROLLER's id for the node that served the row
    node_row_id   that node's own local input_audit.id, preserved
    audit_uid     f"{node_id}:{node_row_id}" -- stable and globally unique

`node_id` is OVERWRITTEN, never defaulted. A remote node reports its own
rows from its own point of view, where its node_id is the literal string
"local" (core.py's REGISTRY_LOCAL_NODE_ID convention) -- the same trap
controller.terminal_knowledge_search_fleet documents having fallen into
with a no-op `setdefault`, which silently mislabeled every remote row as
"local". `audit_uid` is derived AFTER that overwrite for the same reason.

ORDERING IS A TOTAL ORDER, not a sort hint:

    (timestamp DESC, node_id ASC, node_row_id DESC)

All three parts are needed. Timestamp alone ties constantly (two nodes,
same second). Adding node_id breaks ties deterministically but two rows
from one node can still share a timestamp, so node_row_id closes it. The
result does not depend on which node answered first, how fast, or in what
order the registry happened to list them -- the same fleet state always
produces the same page.

PAGINATION is cursor-based on exactly that key, because offset paging over
a live, append-heavy table returns duplicates and holes as new rows land.
The cursor is opaque to callers but deliberately NOT encrypted -- it is
provenance, not a secret (see encode_cursor).

FRESHNESS IS REPORTED, NOT GUESSED. A fleet read is a set of point-in-time
reads of N independent databases; it is never a consistent snapshot and
this module does not pretend otherwise. Every response carries per-node
`fetched_at`, and `complete=False` the moment any node did not answer --
a partial page that LOOKS complete is the failure mode worth engineering
against, because the missing rows are invisible by construction.

NO SECRET PAYLOAD EXPANSION. This module adds provenance fields and
nothing else. What travels is what `AuditStore.list()` already returns --
`text_preview` (already redaction-filtered and truncated to 240 chars by
audit.sanitized_preview) and `text_sha256` (a fingerprint) -- never raw
prompt text, which no audit row has ever stored. `_PROVENANCE_FIELDS`
below is the entire delta, and a test asserts the aggregated field set is
a subset of the local one plus those three.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from typing import Any, Iterable

SCHEMA_VERSION = 1
"""Bumped when the AGGREGATED row/response shape changes. The per-node
`input_audit` schema has its own separate migration series (audit.py's
AUDIT_MIGRATIONS) and the two are deliberately not coupled: a node may
add a column without every consumer of this view needing to care."""

_PROVENANCE_FIELDS = ("node_id", "node_row_id", "audit_uid")
"""The ENTIRE delta this aggregation adds to a local audit row."""

DEFAULT_LIMIT = 50
MAX_LIMIT = 500
"""Matches AuditStore.list's own cap rather than inventing a second one."""

_CURSOR_SEPARATOR = "\x1f"


@dataclass(frozen=True)
class NodeReport:
    """One node's contribution, including the ones that contributed
    nothing. An offline node appears here with `ok=False` and a reason
    rather than being omitted -- a caller must be able to see WHICH part
    of the fleet a partial answer is missing, not merely that it is
    partial."""
    node_id: str
    ok: bool
    rows: int = 0
    error: str | None = None
    status: str | None = None
    fetched_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "ok": self.ok, "rows": self.rows, "error": self.error,
                "status": self.status, "fetched_at": self.fetched_at}


@dataclass
class FleetAuditPage:
    rows: list[dict[str, Any]] = field(default_factory=list)
    nodes: list[NodeReport] = field(default_factory=list)
    next_cursor: str | None = None
    complete: bool = True
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "events": self.rows,
            "nodes": [node.to_dict() for node in self.nodes],
            "node_errors": {node.node_id: node.error for node in self.nodes if node.error},
            "next_cursor": self.next_cursor, "complete": self.complete,
            "partial": not self.complete,
            # Same posture as controller.terminal_knowledge_search_fleet:
            # aggregated rows contain text a remote agent produced, so they
            # are flagged as untrusted for any caller that renders them.
            "untrusted_output": True, "untrusted_fields": ["events"],
        }


def attach_provenance(row: dict[str, Any], node_id: str) -> dict[str, Any]:
    """Stamp the controller's node identity onto one row and derive its
    fleet-unique id. Always an OVERWRITE of `node_id` -- see this module's
    docstring for the real bug that makes this non-optional."""
    stamped = dict(row)
    node_row_id = stamped.get("id")
    stamped["node_id"] = node_id
    stamped["node_row_id"] = node_row_id
    stamped["audit_uid"] = f"{node_id}:{node_row_id}"
    return stamped


def sort_key(row: dict[str, Any]) -> tuple[str, str, int]:
    """The three ordering fields of one row, most significant first.

    Returns the VALUES, not a single sortable key -- the directions are
    mixed, so _sorted_rows applies them in separate stable passes. Missing
    or malformed values sort last rather than raising: a row a node sent us
    is data, not a contract."""
    timestamp = row.get("timestamp") or ""
    node_id = row.get("node_id") or ""
    raw_id = row.get("node_row_id")
    node_row_id = raw_id if isinstance(raw_id, int) else -1
    return (timestamp, node_id, node_row_id)


def _sorted_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable, total, and independent of input order.

    The composite order mixes directions (timestamp DESC, node_id ASC,
    node_row_id DESC) and node_id is a string, so there is no single
    reversible key to sort by -- negating a string is the classic bug
    here. Instead this leans on Python's sort being STABLE and sorts by
    each field separately, LEAST significant first, so each later pass
    preserves the ordering the earlier ones established."""
    ordered = sorted(rows, key=lambda row: sort_key(row)[2], reverse=True)   # node_row_id DESC
    ordered = sorted(ordered, key=lambda row: sort_key(row)[1])              # node_id ASC
    return sorted(ordered, key=lambda row: sort_key(row)[0], reverse=True)   # timestamp DESC


def encode_cursor(row: dict[str, Any]) -> str:
    """Opaque-but-not-secret. Base64 keeps callers from parsing and
    depending on the shape (so the key can change without breaking them),
    but a cursor only ever encodes a position already visible in the rows
    the caller just received -- encrypting it would imply a confidentiality
    guarantee that does not exist and cannot be honoured by a stateless
    read path."""
    timestamp, node_id, node_row_id = sort_key(row)
    raw = _CURSOR_SEPARATOR.join((str(timestamp), str(node_id), str(node_row_id)))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(cursor: str | None) -> tuple[str, str, int] | None:
    """None for absent OR unparseable. A bad cursor restarts from the
    newest page instead of erroring: a cursor is a position hint, and
    failing a whole fleet read because a caller round-tripped one through
    something lossy would trade a useful answer for a pedantic one."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    parts = raw.split(_CURSOR_SEPARATOR)
    if len(parts) != 3:
        return None
    try:
        return (parts[0], parts[1], int(parts[2]))
    except ValueError:
        return None


def _strictly_after(row: dict[str, Any], cursor: tuple[str, str, int]) -> bool:
    """Is this row strictly PAST the cursor position in the total order?

    Descending on timestamp/node_row_id and ascending on node_id, so
    "after" means older-or-equal-timestamp and, at an exact tie, a
    later position in the composite order."""
    timestamp, node_id, node_row_id = sort_key(row)
    cursor_ts, cursor_node, cursor_row = cursor
    if timestamp != cursor_ts:
        return timestamp < cursor_ts
    if node_id != cursor_node:
        return node_id > cursor_node
    return node_row_id < cursor_row


def merge_pages(per_node_rows: dict[str, list[dict[str, Any]]], *, limit: int = DEFAULT_LIMIT,
                cursor: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
    """k-way merge of each node's own newest-first rows into one page.

    Dedupe is by `audit_uid`, which makes this idempotent in both ways
    that matter: the same node answering twice (a retry, or a node
    registered under two ids pointing at one host) contributes each row
    once, and two DIFFERENT nodes that both happen to have local id 41
    contribute two distinct rows -- the case a naive dedupe on `id` would
    silently collapse into one, losing a real audit row.

    Returns (rows, next_cursor). `next_cursor` is set whenever this page
    came back FULL, and None only when it came back short -- a short page
    is the only state from which "there is definitely nothing more" can be
    concluded without a second round trip. Deciding from `len(candidates)
    > limit` instead would be wrong: each node was itself asked for a
    bounded number of rows, so the merge cannot see past what it fetched
    and would report "done" while rows remained on the nodes. The cost is
    that a caller looping to exhaustion makes one final request that comes
    back empty; losing audit rows silently is not an acceptable trade for
    saving it."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    position = decode_cursor(cursor)

    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for node_id, rows in per_node_rows.items():
        for row in rows:
            stamped = attach_provenance(row, node_id)
            uid = stamped["audit_uid"]
            if uid in seen:
                continue
            seen.add(uid)
            if position is not None and not _strictly_after(stamped, position):
                continue
            candidates.append(stamped)

    ordered = _sorted_rows(candidates)
    page = ordered[:limit]
    next_cursor = encode_cursor(page[-1]) if len(page) == limit else None
    return page, next_cursor
