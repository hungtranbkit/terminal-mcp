"""Project Backlog -- controller-side store, keyed by CANONICAL PROJECT ID.

Why this replaced "the file in the repo is the source of truth"
(decision 2026-09-09, after the fleet was actually measured):

  One project genuinely runs on many machines. Measuring this deployment
  found `git:github.com/hungtranbkit/terminal-mcp` live across 3 nodes in
  7 different checkouts (worktrees and scratch clones included), and
  `git:github.com/hungtranbkit/offline-pos` across dell-5530 + local in 3
  checkouts. A backlog stored in ONE checkout is therefore invisible to
  every other node working the SAME project -- verified: the controller
  answered PATH_NOT_ALLOWED for m910's, macbook's and dell-5530's
  checkout paths, because those paths do not exist on the controller at
  all. The old design's "portable, reaches other nodes" claim was simply
  not true in practice.

  The identity half was already right: project_id comes from the
  normalised git remote, so all 7 checkouts collapse to one id. Only the
  STORAGE was wrong. So: the controller -- the single point every node
  already talks to -- holds the canonical backlog keyed by that id, and
  the repo file becomes an export/import projection (see
  backlog_service.export_file/import_file) that keeps the portability and
  git-review benefits without being the thing agents race on.

Schema: one row per project, one row per item, `revision` per PROJECT
(not global) so optimistic concurrency stays scoped to the backlog a
caller is actually editing. Same connection/WAL/0600/migration
discipline as every other store here.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .backlog_store import (
    SCHEMA_VERSION, STATUSES, TERMINAL_STATUSES, new_item_id, normalise_item, now_iso,
)
from .schema import Migration, apply_migrations

BACKLOG_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: project-keyed backlog", lambda connection: None),
]

_RECONCILE_LIST_FIELDS = ("dependencies", "acceptance_criteria", "tags", "history")
_RECONCILE_SCALAR_FIELDS = (
    "title", "description", "status", "priority", "type", "order", "created_at",
    "updated_at", "source", "assignee", "session", "node_id", "queue_task_id",
    "branch", "worktree", "blocked_reason",
)


def _stable_union(current: list[Any], historical: list[Any]) -> list[Any]:
    """Return a deterministic, idempotent union for scalar or object metadata."""
    keyed: dict[str, Any] = {}
    for value in [*current, *historical]:
        key = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        keyed.setdefault(key, value)
    return [keyed[key] for key in sorted(keyed)]


def reconcile_item(current: dict[str, Any], historical: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Keep canonical fields, merging only additive historical metadata."""
    merged = dict(current)
    differing = [field for field in _RECONCILE_SCALAR_FIELDS
                 if current.get(field) != historical.get(field)]
    for field in _RECONCILE_LIST_FIELDS:
        merged[field] = _stable_union(current.get(field) or [], historical.get(field) or [])
    current_evidence = current.get("evidence") or {}
    historical_evidence = historical.get("evidence") or {}
    evidence: dict[str, Any] = dict(current_evidence)
    for field in sorted(set(current_evidence) | set(historical_evidence)):
        left, right = current_evidence.get(field), historical_evidence.get(field)
        if isinstance(left, list) or isinstance(right, list):
            evidence[field] = _stable_union(left if isinstance(left, list) else [],
                                            right if isinstance(right, list) else [])
        elif field not in evidence:
            evidence[field] = right
    merged["evidence"] = evidence
    return merged, differing


def default_backlog_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_BACKLOG_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "backlog.db"


class BacklogDB:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_backlog_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS backlog_projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT,
                    source TEXT,
                    git_remote TEXT,
                    is_portable INTEGER NOT NULL DEFAULT 1,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS backlog_items (
                    project_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    sort_order REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, id)
                )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_backlog_items_project_status "
                "ON backlog_items(project_id, status)")
            apply_migrations(connection, BACKLOG_MIGRATIONS)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------ project
    def ensure_project(self, identity: dict[str, Any]) -> None:
        now = now_iso()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO backlog_projects
                   (project_id, name, source, git_remote, is_portable, revision, created_at, updated_at)
                   VALUES (?,?,?,?,?,0,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET
                     name=COALESCE(excluded.name, backlog_projects.name),
                     git_remote=COALESCE(excluded.git_remote, backlog_projects.git_remote),
                     updated_at=excluded.updated_at""",
                (identity["project_id"], identity.get("name"), identity.get("source"),
                 identity.get("git_remote"), int(bool(identity.get("is_portable", True))), now, now))

    def project(self, project_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM backlog_projects WHERE project_id=?", (project_id,)).fetchone()
        return dict(row) if row else None

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT p.*,
                          (SELECT COUNT(*) FROM backlog_items i WHERE i.project_id=p.project_id) AS total,
                          (SELECT COUNT(*) FROM backlog_items i WHERE i.project_id=p.project_id
                             AND i.status NOT IN (?,?)) AS open_total
                   FROM backlog_projects p ORDER BY p.updated_at DESC""",
                TERMINAL_STATUSES).fetchall()
        return [dict(r) for r in rows]

    def revision(self, project_id: str) -> int:
        row = self.project(project_id)
        return int(row["revision"]) if row else 0

    # -------------------------------------------------------------- items
    def items(self, project_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM backlog_items WHERE project_id=? ORDER BY sort_order, id",
                (project_id,)).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def replace_items(self, project_id: str, items: list[dict[str, Any]]) -> int:
        """Write the whole item set for one project and bump its revision,
        in ONE transaction -- the service layer already computes the new
        set under a lock, and an all-or-nothing swap means a reader never
        observes a half-applied edit."""
        now = now_iso()
        with self._connection() as connection:
            connection.execute("DELETE FROM backlog_items WHERE project_id=?", (project_id,))
            connection.executemany(
                "INSERT INTO backlog_items (project_id,id,payload,status,priority,sort_order,updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                [(project_id, i["id"], json.dumps(i, ensure_ascii=False), i["status"],
                  i["priority"], float(i.get("order") or 0), i.get("updated_at") or now)
                 for i in items])
            connection.execute(
                "UPDATE backlog_projects SET revision=revision+1, updated_at=? WHERE project_id=?",
                (now, project_id))
            row = connection.execute(
                "SELECT revision FROM backlog_projects WHERE project_id=?", (project_id,)).fetchone()
        return int(row["revision"]) if row else 0

    def document(self, project_id: str) -> dict[str, Any]:
        """The same shape the file format uses, so export/import and every
        existing reader keep working unchanged."""
        project = self.project(project_id) or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "project": {"project_id": project_id, "name": project.get("name"),
                        "source": project.get("source"), "git_remote": project.get("git_remote"),
                        "is_portable": bool(project.get("is_portable", 1))},
            "revision": int(project.get("revision", 0)),
            "updated_at": project.get("updated_at") or now_iso(),
            "items": self.items(project_id),
        }

    def import_document(self, project_id: str, document: dict[str, Any], *, replace: bool) -> dict[str, Any]:
        """Merge (default) or replace a project's items from a file
        document. Merge is by item id: an incoming item with a known id
        UPDATES it, an unknown id is ADDED, and nothing local is deleted
        -- importing a teammate's committed backlog must never silently
        drop work only this controller knows about."""
        incoming = [normalise_item(i) for i in document.get("items", [])]
        incoming = [i for i in incoming if i]
        if replace:
            merged = incoming
            added, updated = len(incoming), 0
        else:
            existing = {i["id"]: i for i in self.items(project_id)}
            added = updated = 0
            for item in incoming:
                if item["id"] in existing:
                    existing[item["id"]] = item
                    updated += 1
                else:
                    existing[item["id"]] = item
                    added += 1
            merged = list(existing.values())
        revision = self.replace_items(project_id, merged)
        return {"added": added, "updated": updated, "total": len(merged), "revision": revision}

    def reconcile_document(self, project_id: str, document: dict[str, Any], *,
                           dry_run: bool = True) -> dict[str, Any]:
        """Safely union a historical projection into the canonical store.

        Existing canonical scalar/operational fields always win. Historical
        evidence, history, tags, dependencies and acceptance criteria are
        additive. A byte-equivalent second run performs no write or revision
        bump, which makes this suitable for restartable migrations.
        """
        incoming = [normalise_item(i) for i in document.get("items", [])]
        incoming = [i for i in incoming if i]
        existing_items = self.items(project_id)
        existing = {i["id"]: i for i in existing_items}
        added_ids: list[str] = []
        updated_ids: list[str] = []
        unchanged_ids: list[str] = []
        conflicts: list[dict[str, Any]] = []

        for historical in incoming:
            item_id = historical["id"]
            current = existing.get(item_id)
            if current is None:
                # Store new historical rows in the same canonical metadata
                # order used for overlaps. Otherwise the first re-run would
                # only reorder lists and spuriously bump the revision once.
                existing[item_id] = reconcile_item(historical, historical)[0]
                added_ids.append(item_id)
                continue
            merged, differing = reconcile_item(current, historical)
            if differing:
                conflicts.append({"id": item_id, "fields": differing,
                                  "resolution": "current_canonical_wins"})
            if merged == current:
                unchanged_ids.append(item_id)
            else:
                existing[item_id] = merged
                updated_ids.append(item_id)

        merged_items = list(existing.values())
        changed = bool(added_ids or updated_ids)
        revision_before = self.revision(project_id)
        revision_after = revision_before
        if changed and not dry_run:
            revision_after = self.replace_items(project_id, merged_items)
        return {
            "dry_run": bool(dry_run),
            "changed": changed,
            "before_total": len(existing_items),
            "after_total": len(merged_items),
            "added": len(added_ids),
            "updated": len(updated_ids),
            "unchanged": len(unchanged_ids),
            "added_ids": sorted(added_ids),
            "updated_ids": sorted(updated_ids),
            "conflicts": sorted(conflicts, key=lambda row: row["id"]),
            "revision_before": revision_before,
            "revision": revision_after,
        }
