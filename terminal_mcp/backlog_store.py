"""Project Backlog -- persistence. The SOURCE OF TRUTH is a JSON file
INSIDE the project repo (`.terminal-mcp/backlog.json`), not a controller
database.

Why a file in the repo, and why JSON:
  - Portable + versionable: the backlog travels with a clone, can be
    diffed/reviewed/reverted in git, and is readable by a human without
    this server running at all. A controller-side SQLite table would
    strand the plan on one machine -- the exact failure the multi-node
    work spent this whole project avoiding.
  - JSON, not YAML, even though PyYAML is already a dependency: JSON
    round-trips byte-exactly (no anchors/aliases/tag ambiguity, no
    "yes"->True surprises), and a hand-edit that breaks it fails LOUDLY
    at parse time instead of silently changing a value's type. Merge
    friendliness is addressed by the SERIALISATION shape instead --
    one item per line-block, keys always in the same order, items sorted
    by a stable key -- so two agents appending different items produce a
    clean, line-oriented git diff.
  - A controller-side index/cache MAY be layered later; this module is
    written so the file remains authoritative either way.

Concurrency (real, not theoretical -- several agents on several nodes
share one repo checkout):
  - Every read-modify-write takes an OS advisory lock (`fcntl.flock`) on
    a SEPARATE lock file, so the lock never competes with the atomic
    replace of the data file itself.
  - Every write is atomic: write a temp file in the SAME directory, then
    `os.replace` -- readers see either the old or the new file, never a
    truncated one, and a crash mid-write cannot corrupt the backlog.
  - `revision` increments on every write. Callers pass
    `expected_revision` to get optimistic-concurrency protection: a
    stale writer is REFUSED (REVISION_CONFLICT) rather than silently
    clobbering the other agent's edit.

Manual edits are a supported, first-class case: a human may open the
file and change it. `load()` validates and REPAIRS structurally
(defaulting missing optional fields, never inventing content), and
reports what it had to normalise so `backlog_validate` can show it.
"""
from __future__ import annotations

import errno
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # POSIX advisory locking; absent on Windows nodes.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on Windows
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
BACKLOG_DIRNAME = ".terminal-mcp"
BACKLOG_FILENAME = "backlog.json"

# Lifecycle. BACKLOG is "captured, not promised"; READY means groomed and
# dispatchable; DONE is reserved for VERIFIED completion (see
# backlog_service's own gate) -- an agent asserting success is never
# enough to reach it.
STATUS_BACKLOG = "BACKLOG"
STATUS_READY = "READY"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_BLOCKED = "BLOCKED"
STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
STATUS_DONE = "DONE"
STATUS_CANCELLED = "CANCELLED"
STATUSES = (STATUS_BACKLOG, STATUS_READY, STATUS_IN_PROGRESS, STATUS_BLOCKED,
            STATUS_NEEDS_REVIEW, STATUS_DONE, STATUS_CANCELLED)
OPEN_STATUSES = (STATUS_BACKLOG, STATUS_READY, STATUS_IN_PROGRESS, STATUS_BLOCKED, STATUS_NEEDS_REVIEW)
TERMINAL_STATUSES = (STATUS_DONE, STATUS_CANCELLED)

PRIORITIES = ("P0", "P1", "P2", "P3")
TYPES = ("feature", "bug", "chore", "incident", "research", "docs", "test")

# Field order is FIXED so a rewritten file diffs cleanly against the
# previous one instead of reordering keys on every save.
_ITEM_FIELDS: tuple[tuple[str, Any], ...] = (
    ("id", ""), ("title", ""), ("description", ""), ("status", STATUS_BACKLOG),
    ("priority", "P2"), ("type", "feature"), ("order", 0),
    ("created_at", ""), ("updated_at", ""), ("source", "unknown"),
    ("dependencies", list), ("acceptance_criteria", list), ("tags", list),
    ("assignee", None), ("session", None), ("node_id", None),
    ("queue_task_id", None), ("branch", None), ("worktree", None),
    ("blocked_reason", None), ("evidence", dict), ("history", list),
)
_EVIDENCE_FIELDS = ("commits", "tests", "deploys", "notes")


class BacklogError(RuntimeError):
    """Structural/IO failure. Application-level refusals (unknown id,
    revision conflict) are returned as {"error": ...} dicts by the
    service layer instead, matching this project's existing convention."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_item_id() -> str:
    return f"blg_{uuid.uuid4().hex[:12]}"


def backlog_path(repo_root: str | os.PathLike[str]) -> Path:
    return Path(repo_root) / BACKLOG_DIRNAME / BACKLOG_FILENAME


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


@contextmanager
def file_lock(path: Path, *, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Advisory lock around a whole read-modify-write. Uses a SEPARATE
    `.lock` file because the data file is replaced (not written in
    place) by every save -- a lock held on the old inode would protect
    nothing once os.replace swapped it out.

    On a platform without fcntl (a Windows node), this degrades to
    no-locking rather than failing: `revision`/`expected_revision`
    optimistic concurrency still protects against lost updates there,
    which is the guarantee that actually matters. Documented instead of
    silently assumed."""
    if fcntl is None:  # pragma: no cover - Windows-only path
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = _lock_path(path)
    handle = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = datetime.now(timezone.utc).timestamp() + timeout_seconds
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise BacklogError(f"could not lock {lock_file}: {exc}") from exc
                if datetime.now(timezone.utc).timestamp() >= deadline:
                    raise BacklogError(
                        f"timed out after {timeout_seconds}s waiting for the backlog lock ({lock_file}) -- "
                        "another agent is holding it"
                    ) from exc
                import time as _time
                _time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def empty_document(project: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "project": project or {}, "revision": 0,
            "updated_at": now_iso(), "items": []}


def normalise_item(raw: Any, *, repairs: list[str] | None = None) -> dict[str, Any] | None:
    """One raw item (possibly hand-edited) -> a fully-populated item, or
    None if it is not even a dict. Missing optional fields are filled
    with defaults; unknown keys are PRESERVED (a human or a future
    version may have added something, and silently dropping their data
    would be worse than carrying it)."""
    if not isinstance(raw, dict):
        return None
    item: dict[str, Any] = {}
    for key, default in _ITEM_FIELDS:
        if key in raw and raw[key] is not None:
            item[key] = raw[key]
        else:
            item[key] = default() if callable(default) else default
            if key in ("id", "title") and repairs is not None:
                repairs.append(f"item missing {key!r}")
    if not item["id"]:
        item["id"] = new_item_id()
        if repairs is not None:
            repairs.append(f"assigned a new id to an item with none ({item['id']})")
    if item["status"] not in STATUSES:
        if repairs is not None:
            repairs.append(f"{item['id']}: unknown status {item['status']!r} -> {STATUS_BACKLOG}")
        item["status"] = STATUS_BACKLOG
    if item["priority"] not in PRIORITIES:
        if repairs is not None:
            repairs.append(f"{item['id']}: unknown priority {item['priority']!r} -> P2")
        item["priority"] = "P2"
    for key in ("dependencies", "acceptance_criteria", "tags", "history"):
        if not isinstance(item[key], list):
            item[key] = []
            if repairs is not None:
                repairs.append(f"{item['id']}: {key} was not a list -> []")
    if not isinstance(item["evidence"], dict):
        item["evidence"] = {}
        if repairs is not None:
            repairs.append(f"{item['id']}: evidence was not an object -> {{}}")
    for key in _EVIDENCE_FIELDS:
        item["evidence"].setdefault(key, [])
    if not isinstance(item["order"], (int, float)):
        item["order"] = 0
    item["created_at"] = item["created_at"] or now_iso()
    item["updated_at"] = item["updated_at"] or item["created_at"]
    # Preserve anything we did not model.
    for key, value in raw.items():
        item.setdefault(key, value)
    return item


def _ordered_item(item: dict[str, Any]) -> dict[str, Any]:
    ordered = {key: item[key] for key, _ in _ITEM_FIELDS if key in item}
    for key in sorted(k for k in item if k not in ordered):
        ordered[key] = item[key]
    return ordered


def serialise(document: dict[str, Any]) -> str:
    """Deterministic, merge-friendly text. Stable key order + one item
    per line-block + a trailing newline, so two agents adding different
    items produce a diff git can merge instead of a whole-file rewrite."""
    doc = {
        "schema_version": document.get("schema_version", SCHEMA_VERSION),
        "project": document.get("project") or {},
        "revision": int(document.get("revision", 0)),
        "updated_at": document.get("updated_at") or now_iso(),
        "items": [_ordered_item(i) for i in document.get("items", [])],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def load(path: Path) -> tuple[dict[str, Any], list[str]]:
    """Read + validate. Returns (document, repairs). A MISSING file is
    not an error -- it yields an empty document, so `get` on a project
    that has never had a backlog returns cleanly with metadata instead
    of failing (task item 8)."""
    if not path.exists():
        return empty_document(), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BacklogError(f"{path} is not readable/valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BacklogError(f"{path}: top level must be a JSON object, got {type(raw).__name__}")
    repairs: list[str] = []
    version = raw.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int) or version > SCHEMA_VERSION:
        raise BacklogError(
            f"{path}: schema_version {version!r} is newer than this server supports ({SCHEMA_VERSION}) -- "
            "refusing to rewrite it and risk dropping fields written by a newer version"
        )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw.get("items", []) or []:
        item = normalise_item(entry, repairs=repairs)
        if item is None:
            repairs.append("dropped a non-object entry in items[]")
            continue
        if item["id"] in seen:
            new_id = new_item_id()
            repairs.append(f"duplicate id {item['id']} -> reassigned {new_id}")
            item["id"] = new_id
        seen.add(item["id"])
        items.append(item)
    document = {
        "schema_version": SCHEMA_VERSION,
        "project": raw.get("project") if isinstance(raw.get("project"), dict) else {},
        "revision": int(raw.get("revision", 0)) if isinstance(raw.get("revision"), (int, float)) else 0,
        "updated_at": raw.get("updated_at") or now_iso(),
        "items": items,
    }
    return document, repairs


def save(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    """Atomic write, revision bumped. Caller must already hold file_lock."""
    document = dict(document)
    document["schema_version"] = SCHEMA_VERSION
    document["revision"] = int(document.get("revision", 0)) + 1
    document["updated_at"] = now_iso()
    payload = serialise(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".backlog-", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)  # atomic
    except BaseException:
        with __import__("contextlib").suppress(OSError):
            os.unlink(handle.name)
        raise
    return document
