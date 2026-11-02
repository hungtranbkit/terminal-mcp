"""One-way, disposable projection from Repo Kanban into a Kanzo database.

This module intentionally has no path back to Terminal MCP.  It accepts the
already-sanitized Repo Kanban view and atomically replaces a dedicated Kanzo
home, making Kanzo a UI projection rather than a task authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_CREDENTIAL = re.compile(
    r"(?i)(?:gh[pousr]_[A-Za-z0-9_]{20,}|bearer\s+[A-Za-z0-9._-]{16,}|"
    r"(?:password|token|secret)\s*[:=]\s*\S+)"
)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ProjectionError(ValueError):
    """The supplied snapshot is unsafe or violates the projection contract."""


@dataclass(frozen=True)
class ProjectionResult:
    projects: int
    tasks: int
    duplicates: int
    digest: str
    database: Path


def snapshot_from_repo_kanban(
    service: Any, repo_ids: list[str] | tuple[str, ...]
) -> dict[str, Any]:
    """Read an allowlisted subset through ``RepoKanbanService`` only.

    The adapter deliberately does not read repositories, GitHub, sessions, or
    queues itself.  That keeps Repo Kanban's existing sanitization and source
    boundaries authoritative.
    """
    summaries = service.list_projects().get("projects", [])
    configured = {
        project.get("id"): project
        for project in summaries
        if isinstance(project, Mapping)
    }
    unknown = sorted(set(repo_ids) - set(configured))
    if unknown:
        raise ProjectionError(
            f"project is not configured in Repo Kanban: {', '.join(unknown)}"
        )
    projects: list[dict[str, Any]] = []
    for repo_id in sorted(set(repo_ids)):
        summary = dict(configured[repo_id])
        board = service.get_board(repo_id)
        if not isinstance(board, Mapping):
            raise ProjectionError(
                f"Repo Kanban returned an invalid board for {repo_id}"
            )
        summary["cards"] = list(board.get("cards") or [])
        projects.append(summary)
    return {"projects": projects}


_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '6'), ('changes', '0');
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL, repo TEXT UNIQUE, created_at TEXT NOT NULL, color TEXT
);
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
    position REAL NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '', human INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX tasks_by_column ON tasks(status, position);
CREATE INDEX tasks_by_project ON tasks(project_id);
CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX comments_by_task ON comments(task_id);
CREATE TABLE project_repos (
    repo TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX project_repos_by_project ON project_repos(project_id);
CREATE TABLE images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    comment_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    file TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX images_by_task ON images(task_id);
CREATE TABLE tmcp_projection (
    canonical_key TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    source_url TEXT,
    source_status TEXT NOT NULL,
    last_synced TEXT NOT NULL,
    content_digest TEXT NOT NULL
);
CREATE TRIGGER projects_insert AFTER INSERT ON projects BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER projects_update AFTER UPDATE ON projects BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER projects_delete AFTER DELETE ON projects BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER tasks_insert AFTER INSERT ON tasks BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER tasks_update AFTER UPDATE ON tasks BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER tasks_delete AFTER DELETE ON tasks BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER comments_insert AFTER INSERT ON comments BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER comments_update AFTER UPDATE ON comments BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER comments_delete AFTER DELETE ON comments BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER project_repos_insert AFTER INSERT ON project_repos BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER project_repos_delete AFTER DELETE ON project_repos BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER images_insert AFTER INSERT ON images BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
CREATE TRIGGER images_delete AFTER DELETE ON images BEGIN UPDATE meta SET value = value + 1 WHERE key = 'changes'; END;
"""


def _text(value: Any, field: str, limit: int = 1000) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        raise ProjectionError(f"{field} is too long")
    if _CREDENTIAL.search(text):
        raise ProjectionError(f"{field} contains credential-shaped text")
    return text


def _repo_name(project: Mapping[str, Any]) -> str | None:
    remote = _text(project.get("remote"), "project.remote", 300)
    prefix = "github.com/"
    return remote[len(prefix) :] if remote.startswith(prefix) else None


def _links(card: Mapping[str, Any], repo: str | None) -> list[str]:
    links: list[str] = []
    for kind in ("issues", "prs"):
        for item in card.get(kind) or []:
            if not isinstance(item, Mapping):
                continue
            explicit = _text(item.get("url"), f"card.{kind}.url", 500)
            if explicit:
                parsed = urlsplit(explicit)
                parts = parsed.path.strip("/").split("/")
                expected_kind = "issues" if kind == "issues" else "pull"
                expected_repo = repo.split("/", 1) if repo else []
                if (
                    parsed.scheme != "https"
                    or parsed.netloc != "github.com"
                    or parsed.query
                    or parsed.fragment
                    or any(part in {"", ".", ".."} for part in parts)
                    or len(parts) != 4
                    or parts[:2] != expected_repo
                    or parts[2] != expected_kind
                    or not parts[3].isdigit()
                    or item.get("number") != int(parts[3])
                ):
                    raise ProjectionError("cross-project source URL")
                links.append(explicit)
                continue
            number = item.get("number")
            if repo and isinstance(number, int) and number > 0:
                segment = "issues" if kind == "issues" else "pull"
                links.append(f"https://github.com/{repo}/{segment}/{number}")
    return links


def _state(card: Mapping[str, Any]) -> tuple[str, list[str]]:
    source = _text(card.get("status"), "card.status", 40).upper() or "UNKNOWN"
    warnings = [str(v).lower() for v in (card.get("warnings") or [])]
    owner = _text(card.get("owner"), "card.owner", 40).lower()
    tags: list[str] = []
    if source == "NEW":
        status = "todo"
    elif source == "OPEN":
        status, tags = "todo", ["github-open"]
    elif source == "DRAFT":
        status, tags = "review", ["github-draft"]
    elif source == "OPEN_PR":
        status, tags = "review", ["github-open-pr"]
    elif source == "CLOSED":
        status, tags = "todo", ["github-closed", "not-verified-done"]
    elif source == "RUNNING":
        status = "doing"
    elif source in {"REVIEW", "QA_PENDING", "WAITING_QA"}:
        status, tags = "review", ["waiting-qa"]
    elif source == "DONE":
        evidence = _text(card.get("evidence"), "card.evidence", 4000).strip()
        invalid = not evidence or any(
            "thiếu bằng chứng" in w
            or "code đổi sau" in w
            or "stale" in w
            or "qa" in w
            and "fail" in w
            for w in warnings
        )
        status, tags = ("review", ["qa-pending"]) if invalid else ("done", ["verified"])
    else:
        status = "todo"
        tags.append(
            {
                "BLOCKED": "blocked",
                "WAITING_INPUT": "waiting-input",
                "FAILED": "failed",
                "STALE": "stale",
                "UNKNOWN": "unknown",
                "CANCELLED": "cancelled",
                "NOT_PLANNED": "not-planned",
            }.get(source, "unknown")
        )
    if any("stale" in w for w in warnings) and "stale" not in tags:
        tags.append("stale")
    if owner:
        tags.append(owner)
    return status, tags


def _description(card: Mapping[str, Any], links: list[str], synced_at: str) -> str:
    lines = [
        "Read-only Terminal MCP projection. Kanzo edits do not update or dispatch canonical work.",
        f"Canonical ID: {_text(card.get('id'), 'card.id', 200)}",
        f"Canonical status: {_text(card.get('status'), 'card.status', 40).upper() or 'UNKNOWN'}",
        f"Owner: {_text(card.get('owner'), 'card.owner', 40) or 'Unknown'}",
        f"Last source update: {_text(card.get('last_updated') or card.get('updated'), 'card.last_updated', 80) or 'UNKNOWN'}",
        f"Last synced: {synced_at}",
    ]
    lines.extend(f"Source: {link}" for link in links)
    created = _text(card.get("created"), "card.created", 80)
    if created:
        lines.append(f"Source created: {created}")
    labels = [_text(label, "card.label", 100) for label in (card.get("labels") or [])]
    if labels:
        lines.append(f"Source labels: {', '.join(labels)}")
    evidence = _text(card.get("evidence"), "card.evidence", 4000)
    next_step = _text(card.get("next"), "card.next", 2000)
    if evidence:
        lines.append(f"Evidence: {evidence}")
    if next_step:
        lines.append(f"Next: {next_step}")
    warnings = [_text(w, "card.warning", 500) for w in (card.get("warnings") or [])]
    lines.extend(f"Warning: {warning}" for warning in warnings)
    for session in card.get("sessions") or []:
        if not isinstance(session, Mapping):
            continue
        name = _text(session.get("session_name"), "session.name", 200)
        node = _text(session.get("node") or session.get("node_id"), "session.node", 100)
        state = _text(
            session.get("state") or session.get("last_known_state"), "session.state", 80
        )
        lines.append(f"Session: {name} on {node} — {state or 'UNKNOWN'}")
    return "\n".join(lines)


def _normalized(snapshot: Mapping[str, Any], synced_at: str) -> list[dict[str, Any]]:
    projects = snapshot.get("projects")
    if not isinstance(projects, list):
        raise ProjectionError("snapshot.projects must be a list")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_project in projects:
        if not isinstance(raw_project, Mapping):
            raise ProjectionError("project must be an object")
        slug = _text(raw_project.get("id"), "project.id", 64)
        if not _SLUG.fullmatch(slug):
            raise ProjectionError("invalid project id")
        repo = _repo_name(raw_project)
        cards: list[dict[str, Any]] = []
        for raw_card in raw_project.get("cards") or []:
            if not isinstance(raw_card, Mapping):
                raise ProjectionError("card must be an object")
            canonical_id = _text(raw_card.get("id"), "card.id", 200)
            key = f"{slug}:{canonical_id}"
            if key in seen:
                raise ProjectionError(f"duplicate canonical task: {key}")
            seen.add(key)
            links = _links(raw_card, repo)
            status, tags = _state(raw_card)
            title = _text(raw_card.get("title"), "card.title", 1000)
            description = _description(raw_card, links, synced_at)
            item = {
                "key": key,
                "canonical_id": canonical_id,
                "title": f"{canonical_id} · {title}",
                "description": description,
                "status": status,
                "tags": ",".join(tags),
                "updated_at": _text(
                    raw_card.get("last_updated") or raw_card.get("updated"),
                    "card.updated",
                    80,
                )
                or synced_at,
                "source_url": links[0] if links else None,
                "source_status": _text(
                    raw_card.get("status"), "card.status", 40
                ).upper()
                or "UNKNOWN",
            }
            item["digest"] = hashlib.sha256(
                json.dumps(item, sort_keys=True).encode()
            ).hexdigest()
            cards.append(item)
        output.append(
            {
                "slug": slug,
                "name": _text(raw_project.get("name") or slug, "project.name", 200),
                "repo": repo,
                "cards": sorted(cards, key=lambda c: c["key"]),
            }
        )
    return sorted(output, key=lambda p: p["slug"])


def _build_database(
    path: Path, projects: list[dict[str, Any]], synced_at: str, digest: str
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('tmcp_projection_digest', ?)",
            (digest,),
        )
        for project_id, project in enumerate(projects, 1):
            conn.execute(
                "INSERT INTO projects(id, slug, name, repo, created_at, color) VALUES(?,?,?,?,?,NULL)",
                (project_id, project["slug"], project["name"], None, synced_at),
            )
            if project["repo"]:
                conn.execute(
                    "INSERT INTO project_repos(repo, project_id) VALUES(?,?)",
                    (project["repo"], project_id),
                )
            for position, card in enumerate(project["cards"], 1):
                conn.execute(
                    "INSERT INTO tasks(project_id,title,description,status,position,created_at,updated_at,tags,human) "
                    "VALUES(?,?,?,?,?,?,?,?,1)",
                    (
                        project_id,
                        card["title"],
                        card["description"],
                        card["status"],
                        float(position),
                        synced_at,
                        card["updated_at"],
                        card["tags"],
                    ),
                )
                conn.execute(
                    "INSERT INTO tmcp_projection VALUES(?,?,?,?,?,?,?)",
                    (
                        card["key"],
                        project["slug"],
                        card["canonical_id"],
                        card["source_url"],
                        card["source_status"],
                        synced_at,
                        card["digest"],
                    ),
                )
        conn.commit()
        if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ProjectionError("generated database failed integrity check")
    finally:
        conn.close()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_projection(
    snapshot: Mapping[str, Any], home: Path | str, *, synced_at: str
) -> ProjectionResult:
    """Atomically reconcile a sanitized Repo Kanban snapshot into ``home``."""
    synced_at = _text(synced_at, "synced_at", 80)
    projects = _normalized(snapshot, synced_at)
    canonical = json.dumps(
        {"projects": projects, "synced_at": synced_at},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    home_path = Path(home).resolve()
    home_path.mkdir(mode=0o700, parents=True, exist_ok=True)
    database = home_path / "kanzo.db"
    manifest_path = home_path / "tmcp-projection.json"
    tasks = sum(len(project["cards"]) for project in projects)
    lock_path = home_path / "kanzo.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if database.exists():
            try:
                prior = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ProjectionError(
                    "existing Kanzo home is not a Terminal MCP projection"
                ) from exc
            if (
                prior.get("schema") != "tmcp-kanzo-projection/v1"
                or prior.get("authority") != "terminal-mcp"
            ):
                raise ProjectionError(
                    "existing Kanzo home is not a Terminal MCP projection"
                )

        # Always rebuild. Kanzo's UI is writable and its own writes do not
        # update our manifest, so a digest-only fast path would preserve a
        # local card move as if it were canonical.
        fd, name = tempfile.mkstemp(prefix=".kanzo.db.", suffix=".tmp", dir=home_path)
        os.close(fd)
        tmp = Path(name)
        try:
            _build_database(tmp, projects, synced_at, digest)
            with tmp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.replace(tmp, database)
            database.chmod(0o600)
            _fsync_directory(home_path)
        finally:
            tmp.unlink(missing_ok=True)
        _atomic_json(
            manifest_path,
            {
                "authority": "terminal-mcp",
                "digest": digest,
                "direction": "one-way",
                "disposable": True,
                "projects": len(projects),
                "schema": "tmcp-kanzo-projection/v1",
                "synced_at": synced_at,
                "tasks": tasks,
            },
        )
        _fsync_directory(home_path)
    return ProjectionResult(len(projects), tasks, 0, digest, database)
