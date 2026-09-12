"""Fleet Metadata Registry -- what every node knows about every other node,
kept locally so it survives the controller going away.

WHY THIS IS A REPLICATION LAYER AND NOT A NEW REGISTRY
------------------------------------------------------
Audited before writing a line (2026-09-12). This project already has, and
this module deliberately does NOT duplicate:

  node_registry.py     one row per node: platform, endpoint, capabilities,
                       contract_version, heartbeat -> derived online/offline.
  connection_store.py  per-node SSH transport: hostname/username/port and a
                       PINNED host_key_fingerprint, already secret-free by
                       construction (the bearer token lives in a 0600 file,
                       referenced by PATH; the bootstrap password/key is
                       in-memory for one subprocess and never persisted).
  session_registry.py  durable session records with a real `stable_session_id`
                       that survives rename, and real tombstones (deleted_at
                       + status DELETED).
  project_identity.py  canonical project_id derived from the git remote, so
                       the same repo checked out on two nodes is one project.
  node_profile.py      readiness/NEEDS_AUTH, with "secrets never move" as its
                       stated posture.
  contract.py          protocol generation + capability flags.

Those stores are the SOURCE OF TRUTH and stay so. What was missing is that
they all live on ONE machine: the controller polls node agents over /v1/*,
and the agents know nothing about the fleet or each other. Lose m910 and a
surviving node cannot answer "what nodes exist, how do I SSH to them, what
was running where" -- the data existed, but only in one place.

So this module adds exactly one thing: a versioned, tombstoned envelope
those rows are PROJECTED into, which can be merged between peers and kept
in a local durable cache on every node. Reading the fleet still goes to the
real stores when they are reachable; the cache answers when they are not.

OWNERSHIP IS THE CONFLICT POLICY
--------------------------------
Every object names an `owner_node`: the node whose local truth it describes.
Only the owner mints new revisions of it. A peer merges a foreign object
verbatim and never edits it. That makes this single-writer-per-object, which
is why there are no vector clocks here and no merge function that has to
invent a winner from two concurrent edits -- the situation cannot arise.

Ties (the same revision arriving twice, e.g. after a reconnect) are broken
deterministically and identically on every node: tombstone first, then the
later `updated_at`, then the lexicographically smaller `source_node`. Two
nodes given the same two versions always agree.

SECRETS DO NOT REPLICATE. NOT EVER.
-----------------------------------
No private key, password, passphrase, bearer token, bootstrap secret or
~/.ssh/id_* content is projected, merged, stored or served by anything here.
What travels is identity and reference only: a host key FINGERPRINT (a hash,
which is what pinning compares anyway), and a `secret_ref` that is a logical
NAME -- "the token for node X lives in env var Y on that machine" -- never
its value. `scrub_payload` enforces this on the way in and on the way out,
and refuses rather than silently dropping, so a future field that smuggles a
secret fails a test instead of shipping. A node that lacks a credential
reports NEEDS_AUTH; it never receives one from a peer.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .schema import Migration, apply_migrations

SCHEMA_GENERATION = 1

# Object kinds. Deliberately few and coarse: one row per real-world thing an
# operator asks about, not one per table column.
KIND_NODE = "node"
KIND_SESSION = "session"
KIND_PROJECT = "project"
KIND_SSH_TARGET = "ssh_target"
KINDS = (KIND_NODE, KIND_SESSION, KIND_PROJECT, KIND_SSH_TARGET)

# Credential posture for an ssh_target, from the perspective of the node that
# published it. A peer reading MISSING_CREDENTIAL must ask a human, never a
# machine -- that is the whole point of it being a distinct state.
CRED_PRESENT = "PRESENT"            # this node holds a usable local credential
CRED_NEEDS_AUTH = "NEEDS_AUTH"      # a credential exists but is not authorised
CRED_MISSING = "MISSING_CREDENTIAL"  # nothing local; a human must provision it
CRED_UNKNOWN = "UNKNOWN"


class SecretLeak(ValueError):
    """Raised when a payload carries something that must never replicate.

    Deliberately fatal rather than a silent strip: a dropped field is a bug
    that ships, an exception is a bug that fails a test.
    """


# Matched against FIELD NAMES, not values -- a value-based scan would both
# miss an unrecognised secret format and false-positive on a fingerprint.
# `*_ref`, `*_path` and `*_file` are explicitly allowed through: pointing AT
# a secret is the supported pattern (connection_store.py's own token_file).
_SECRET_NAME = re.compile(
    r"(?:^|_)(?:secret|password|passwd|passphrase|token|apikey|api_key|"
    r"private_key|privatekey|credential|auth|bearer|cookie|session_key)(?:$|_)",
    re.IGNORECASE)
_REF_SUFFIX = re.compile(r"_(?:ref|path|file|env|status|state|url)$", re.IGNORECASE)
# A PEM block is the one value-level check worth doing: it is unambiguous,
# and it is exactly what "do not copy ~/.ssh/id_*" means in practice.
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def scrub_payload(payload: dict[str, Any], *, where: str = "payload") -> dict[str, Any]:
    """Refuses a payload that carries a secret. Returns it unchanged if clean.

    Applied on projection (before a local write), on merge (before accepting
    a peer's object) and on serve (before handing anything to an API), so no
    single forgotten call site can open a hole.
    """
    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                here = f"{path}.{key}"
                if _SECRET_NAME.search(str(key)) and not _REF_SUFFIX.search(str(key)):
                    raise SecretLeak(
                        f"{here} names a secret; replicate a reference "
                        f"(<name>_ref/_path/_env) or a fingerprint instead")
                walk(item, here)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")
        elif isinstance(value, str) and _PEM.search(value):
            raise SecretLeak(f"{path} contains private key material")

    walk(payload, where)
    return payload


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(payload: dict[str, Any]) -> str:
    """Stable over key order, so re-projecting unchanged data is a no-op.

    This is what keeps the revision counter from churning: the projectors run
    on every poll, and without this every poll would mint a new revision for
    every object and make the whole fleet re-sync data that did not change.
    """
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FleetObject:
    """One replicated fact, with everything a merge needs to decide."""

    kind: str
    object_id: str
    owner_node: str      # whose truth this is; only the owner mints revisions
    revision: int
    updated_at: str
    source_node: str     # who handed it to us (may differ from owner: relay)
    deleted: bool = False
    deleted_at: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "object_id": self.object_id,
            "owner_node": self.owner_node, "revision": self.revision,
            "updated_at": self.updated_at, "source_node": self.source_node,
            "deleted": self.deleted, "deleted_at": self.deleted_at,
            "payload": scrub_payload(dict(self.payload), where=f"{self.kind}:{self.object_id}"),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FleetObject":
        kind = str(raw.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(f"unknown object kind: {kind!r}")
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        object_id = str(raw.get("object_id") or "")
        scrub_payload(payload, where=f"{kind}:{object_id}")
        return cls(
            kind=kind, object_id=object_id,
            owner_node=str(raw.get("owner_node") or ""),
            revision=int(raw.get("revision") or 0),
            updated_at=str(raw.get("updated_at") or _now()),
            source_node=str(raw.get("source_node") or ""),
            deleted=bool(raw.get("deleted")),
            deleted_at=(str(raw["deleted_at"]) if raw.get("deleted_at") else None),
            payload=payload,
        )


def wins(incoming: FleetObject, existing: FleetObject) -> bool:
    """Does `incoming` replace `existing`? Deterministic on every node.

    Order: higher revision, then a tombstone over a live record at the SAME
    revision (a delete observed concurrently with an edit must not be undone
    by a reconnect replaying the edit), then the later `updated_at`, then the
    smaller `source_node` purely so two nodes never disagree.
    """
    if incoming.revision != existing.revision:
        return incoming.revision > existing.revision
    if incoming.deleted != existing.deleted:
        return incoming.deleted
    if incoming.updated_at != existing.updated_at:
        return incoming.updated_at > existing.updated_at
    return incoming.source_node < existing.source_node


FLEET_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: fleet_objects + fleet_peers", lambda connection: None),
]


def default_db_path(state_home: str | None = None) -> Path:
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "fleet_registry.db"


class FleetRegistryStore:
    """The local durable copy. Exists on EVERY node, not just the controller.

    That is the whole M910-off story: this file is what a surviving node
    reads to answer "what nodes exist, how do I reach them, what was running
    where" when the controller is gone. It is a cache of replicated facts,
    never the source of truth for the local node's own data -- those stores
    stay authoritative and are re-projected on every sync.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None, *,
                 local_node_id: str = "local") -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.local_node_id = local_node_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._create()
        apply_migrations(self._connection, FLEET_MIGRATIONS)

    def _create(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_objects (
                    kind TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    owner_node TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    source_node TEXT NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    deleted_at TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT NOT NULL DEFAULT '',
                    merged_at TEXT NOT NULL,
                    PRIMARY KEY (kind, object_id)
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fleet_objects_owner "
                "ON fleet_objects(owner_node)")
            # Sync bookkeeping per peer. `cursor` is the highest merged_seq we
            # have taken from that peer; it is advisory only -- correctness
            # never depends on it, because merge is idempotent and a peer that
            # replays everything from zero produces the same state.
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_peers (
                    peer_node TEXT PRIMARY KEY,
                    endpoint TEXT,
                    last_pull_at TEXT,
                    last_push_at TEXT,
                    last_ok_at TEXT,
                    last_error TEXT,
                    objects_pulled INTEGER NOT NULL DEFAULT 0,
                    objects_pushed INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    # -- reads ---------------------------------------------------------------

    def get(self, kind: str, object_id: str) -> FleetObject | None:
        row = self._connection.execute(
            "SELECT * FROM fleet_objects WHERE kind = ? AND object_id = ?",
            (kind, object_id)).fetchone()
        return _row_to_object(row) if row else None

    def list(self, *, kind: str | None = None, owner_node: str | None = None,
             include_deleted: bool = False) -> list[FleetObject]:
        sql = "SELECT * FROM fleet_objects WHERE 1=1"
        args: list[Any] = []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if owner_node:
            sql += " AND owner_node = ?"
            args.append(owner_node)
        if not include_deleted:
            sql += " AND deleted = 0"
        sql += " ORDER BY kind, object_id"
        return [_row_to_object(row) for row in self._connection.execute(sql, args).fetchall()]

    def export(self, *, since: str | None = None,
               include_deleted: bool = True) -> list[FleetObject]:
        """Everything a peer should receive.

        Tombstones are INCLUDED by default and that is not optional: a peer
        that only ever receives live rows can never learn about a delete, and
        would hand the deleted object straight back on its next push.
        """
        sql = "SELECT * FROM fleet_objects"
        args: list[Any] = []
        if since:
            sql += " WHERE merged_at > ?"
            args.append(since)
        if not include_deleted:
            sql += (" AND" if since else " WHERE") + " deleted = 0"
        sql += " ORDER BY merged_at"
        return [_row_to_object(row) for row in self._connection.execute(sql, args).fetchall()]

    # -- writes --------------------------------------------------------------

    def publish(self, kind: str, object_id: str, payload: dict[str, Any], *,
                owner_node: str | None = None, now: str | None = None) -> FleetObject:
        """Record a fact this node owns, minting a revision ONLY if it changed.

        Returning the unchanged object on a no-op is what makes the projectors
        safe to run on every poll: the fleet only re-syncs what actually moved.
        """
        owner = owner_node or self.local_node_id
        scrub_payload(payload, where=f"{kind}:{object_id}")
        digest = content_hash(payload)
        stamp = now or _now()
        existing = self.get(kind, object_id)
        if existing is not None and existing.owner_node != owner:
            # Another node owns this. Publishing over it would make two
            # writers for one object and break the single-writer premise the
            # whole conflict policy rests on.
            raise ValueError(
                f"{kind}:{object_id} is owned by {existing.owner_node!r}, "
                f"not {owner!r} -- only its owner may publish it")
        if existing is not None and not existing.deleted:
            row = self._connection.execute(
                "SELECT content_hash FROM fleet_objects WHERE kind = ? AND object_id = ?",
                (kind, object_id)).fetchone()
            if row and row["content_hash"] == digest:
                return existing
        revision = (existing.revision + 1) if existing else 1
        obj = FleetObject(kind=kind, object_id=object_id, owner_node=owner,
                          revision=revision, updated_at=stamp,
                          source_node=self.local_node_id, deleted=False,
                          deleted_at=None, payload=payload)
        self._write(obj, digest, stamp)
        return obj

    def retire(self, kind: str, object_id: str, *, now: str | None = None) -> FleetObject | None:
        """Tombstone. The row is KEPT -- deleting it would let the next sync
        resurrect the object from a peer that had not heard yet."""
        existing = self.get(kind, object_id)
        if existing is None:
            return None
        if existing.deleted:
            return existing
        stamp = now or _now()
        obj = replace(existing, revision=existing.revision + 1, updated_at=stamp,
                      source_node=self.local_node_id, deleted=True,
                      deleted_at=stamp, payload={})
        self._write(obj, content_hash({}), stamp)
        return obj

    def merge(self, objects: Iterable[FleetObject | dict[str, Any]], *,
              source_node: str | None = None, now: str | None = None) -> dict[str, Any]:
        """Apply a peer's objects. Idempotent: same input twice, same state.

        Returns a summary rather than raising on a bad object -- one
        malformed row from a peer must not abort the whole sync and strand
        every other object in the batch.
        """
        stamp = now or _now()
        applied = skipped = rejected = 0
        errors: list[str] = []
        for raw in objects:
            try:
                incoming = raw if isinstance(raw, FleetObject) else FleetObject.from_dict(raw)
            except (SecretLeak, ValueError) as exc:
                rejected += 1
                errors.append(str(exc)[:200])
                continue
            if source_node:
                incoming = replace(incoming, source_node=source_node)
            if not incoming.object_id or not incoming.owner_node:
                rejected += 1
                errors.append("object_id and owner_node are required")
                continue
            existing = self.get(incoming.kind, incoming.object_id)
            if existing is not None and not wins(incoming, existing):
                skipped += 1
                continue
            self._write(incoming, content_hash(incoming.payload), stamp)
            applied += 1
        return {"applied": applied, "skipped": skipped, "rejected": rejected,
                "errors": errors[:10], "at": stamp}

    def _write(self, obj: FleetObject, digest: str, merged_at: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO fleet_objects (kind, object_id, owner_node, revision,
                    updated_at, source_node, deleted, deleted_at, payload,
                    content_hash, merged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, object_id) DO UPDATE SET
                    owner_node = excluded.owner_node,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at,
                    source_node = excluded.source_node,
                    deleted = excluded.deleted,
                    deleted_at = excluded.deleted_at,
                    payload = excluded.payload,
                    content_hash = excluded.content_hash,
                    merged_at = excluded.merged_at
                """,
                (obj.kind, obj.object_id, obj.owner_node, obj.revision,
                 obj.updated_at, obj.source_node, 1 if obj.deleted else 0,
                 obj.deleted_at, json.dumps(obj.payload, sort_keys=True),
                 digest, merged_at))

    # -- peer bookkeeping ----------------------------------------------------

    def record_sync(self, peer_node: str, *, endpoint: str | None = None,
                    pulled: int | None = None, pushed: int | None = None,
                    error: str | None = None, now: str | None = None) -> None:
        stamp = now or _now()
        with self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO fleet_peers (peer_node) VALUES (?)", (peer_node,))
            sets = ["endpoint = COALESCE(?, endpoint)"]
            args: list[Any] = [endpoint]
            if pulled is not None:
                sets += ["last_pull_at = ?", "objects_pulled = objects_pulled + ?"]
                args += [stamp, pulled]
            if pushed is not None:
                sets += ["last_push_at = ?", "objects_pushed = objects_pushed + ?"]
                args += [stamp, pushed]
            if error:
                sets.append("last_error = ?")
                args.append(error[:500])
            else:
                sets += ["last_ok_at = ?", "last_error = NULL"]
                args.append(stamp)
            args.append(peer_node)
            self._connection.execute(
                f"UPDATE fleet_peers SET {', '.join(sets)} WHERE peer_node = ?", args)

    def peers(self) -> list[dict[str, Any]]:
        return [dict(row) for row in
                self._connection.execute("SELECT * FROM fleet_peers ORDER BY peer_node")]

    def close(self) -> None:
        self._connection.close()


def _row_to_object(row: sqlite3.Row) -> FleetObject:
    return FleetObject(
        kind=row["kind"], object_id=row["object_id"], owner_node=row["owner_node"],
        revision=int(row["revision"]), updated_at=row["updated_at"],
        source_node=row["source_node"], deleted=bool(row["deleted"]),
        deleted_at=row["deleted_at"], payload=json.loads(row["payload"] or "{}"))
