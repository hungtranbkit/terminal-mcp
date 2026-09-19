"""Durable Agent identity and a versioned Skill Registry.

WHY THIS IS A STORE WHEN worker_registry.py IS DELIBERATELY NOT ONE

worker_registry.py opens by explaining why it owns no state: a "worker" is a
`(node_id, session)` pair whose every attribute already lives in the node
registry, the capability profiles and `queue_tasks.claimed_by`, so a `workers`
table would duplicate three sources and immediately drift from them.

An Agent is the opposite kind of thing, and the distinction is the whole point
of TMCP-AGENT-RUNTIME-001:

    Agent    durable identity -- outlives any session, owns tasks, carries
             skills, accumulates a run history
    Session  disposable runtime -- created, replaced, killed, re-matched by
             the router whenever the fleet changes

Nothing in the fleet persisted the first of those. An agent existed only as
whatever session happened to be running its work, so replacing the session
destroyed the identity, and "which agent owns this task" had no answer. That
is a fact about the world with no other home, which is exactly when a new
store is justified -- and it composes with, rather than replaces,
worker_registry's view of what a session can currently do.

SKILLS ARE VERSIONED BECAUSE UNVERSIONED ONES LIE

A skill is a prompt: text an agent loads to do a job a particular way. Its
behaviour changes when the text changes, so "the agent ran the review skill"
is only a meaningful claim about an evidence trail if the exact version is
recorded. Registering the same skill id twice with different content creates a
new VERSION rather than overwriting, and a binding may pin a version or float
to the latest -- stated explicitly, never inferred.

FILESYSTEM LOADING IS EXPLICIT, ROOTED AND BOUNDED

Reading `skills/<id>/SKILL.md` off disk means a caller-supplied identifier
reaches a path join, which is the classic traversal sink. Three rules, all
enforced in skill_packages.py and all tested:

    * the id must match a strict slug pattern -- no separators at all, so
      "../../etc/passwd" is rejected before any path is built;
    * the resolved path must stay inside an APPROVED root, re-checked after
      symlink resolution rather than before;
    * the read is bounded, so a huge or endless file cannot exhaust memory.

Loading is never implicit. Nothing here scans a directory as a side effect of
an unrelated call; an operator (or a tool call) names the skill to register.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .schema import Migration, apply_migrations

# -- agent state --------------------------------------------------------
AGENT_ACTIVE = "ACTIVE"
AGENT_DISABLED = "DISABLED"
AGENT_STATES = (AGENT_ACTIVE, AGENT_DISABLED)

# -- skill binding kinds ------------------------------------------------
SKILL_BASE = "BASE"
"""Loaded for every task this agent runs -- its standing competence."""
SKILL_TASK = "TASK"
"""Available to the agent, applied only when a task asks for it. Kept apart
from BASE so an agent's default prompt does not grow without bound every time
someone teaches it one more optional trick."""
SKILL_KINDS = (SKILL_BASE, SKILL_TASK)

#: A binding that names no version floats to whatever is newest.
LATEST = "latest"

#: Default concurrent runtimes per agent. ONE, deliberately: an agent is an
#: identity with a single train of thought, and letting it hold two sessions
#: at once is how two runtimes end up editing the same worktree. An operator
#: raises it per agent when the work is genuinely parallel.
DEFAULT_MAX_SESSIONS = 1

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class AgentRegistryError(ValueError):
    """A refusal a caller can act on. Never used for programming errors."""


def valid_slug(value: str | None) -> bool:
    """Agent and skill ids are slugs, not free text.

    This is the FIRST line of the traversal defence: an id that cannot contain
    `/`, `\\`, `.` or whitespace cannot be turned into a path outside its root
    no matter what the path code does with it later."""
    return bool(value and isinstance(value, str) and _SLUG.match(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str | None:
    return json.dumps(value) if value else None


def _parse(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    project_id: str | None = None
    description: str = ""
    runtime: str | None = None
    """Preferred agent_type (claude/codex). None means the router decides."""
    state: str = AGENT_ACTIVE
    max_sessions: int = DEFAULT_MAX_SESSIONS
    repo: str | None = None
    workspace: str | None = None
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    disabled_at: str | None = None

    @property
    def enabled(self) -> bool:
        return self.state == AGENT_ACTIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "project_id": self.project_id,
            "description": self.description, "runtime": self.runtime, "state": self.state,
            "enabled": self.enabled, "max_sessions": self.max_sessions, "repo": self.repo,
            "workspace": self.workspace, "model": self.model, "metadata": self.metadata,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "disabled_at": self.disabled_at,
        }


@dataclass(frozen=True)
class Skill:
    id: str
    version: str
    name: str = ""
    summary: str = ""
    source: str = "inline"
    package_path: str | None = None
    content_sha: str | None = None
    body: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        row = {
            "id": self.id, "version": self.version, "name": self.name,
            "summary": self.summary, "source": self.source,
            "package_path": self.package_path, "content_sha": self.content_sha,
            "metadata": self.metadata, "created_at": self.created_at,
            "body_chars": len(self.body),
        }
        # The body is a prompt and can be large; a listing must not carry N of
        # them. Callers that actually need the text ask for it.
        if include_body:
            row["body"] = self.body
        return row


@dataclass(frozen=True)
class SkillBinding:
    agent_id: str
    skill_id: str
    kind: str
    version: str | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"agent_id": self.agent_id, "skill_id": self.skill_id, "kind": self.kind,
                "version": self.version or LATEST, "created_at": self.created_at}


def _create_v1(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE agents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            project_id TEXT,
            description TEXT NOT NULL DEFAULT '',
            runtime TEXT,
            state TEXT NOT NULL DEFAULT 'ACTIVE',
            max_sessions INTEGER NOT NULL DEFAULT 1,
            repo TEXT,
            workspace TEXT,
            model TEXT,
            metadata TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            disabled_at TEXT
        )
        """
    )
    connection.execute("CREATE INDEX idx_agents_project ON agents(project_id)")
    connection.execute("CREATE INDEX idx_agents_state ON agents(state)")
    connection.execute(
        """
        CREATE TABLE skills (
            id TEXT NOT NULL,
            version TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'inline',
            package_path TEXT,
            content_sha TEXT,
            body TEXT NOT NULL DEFAULT '',
            metadata TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (id, version)
        )
        """
    )
    connection.execute("CREATE INDEX idx_skills_id ON skills(id, created_at)")
    connection.execute(
        """
        CREATE TABLE agent_skills (
            agent_id TEXT NOT NULL,
            skill_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'BASE',
            version TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (agent_id, skill_id, kind),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute("CREATE INDEX idx_agent_skills_skill ON agent_skills(skill_id)")
    connection.execute(
        """
        CREATE TABLE agent_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id TEXT NOT NULL,
            task_id TEXT,
            session TEXT,
            node_id TEXT,
            status TEXT NOT NULL,
            detail TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("CREATE INDEX idx_agent_runs_agent ON agent_runs(agent_id, id DESC)")
    connection.execute(
        "CREATE UNIQUE INDEX idx_agent_runs_task ON agent_runs(agent_id, task_id) "
        "WHERE task_id IS NOT NULL")


AGENT_MIGRATIONS = [
    Migration(1, "TMCP-AGENT-RUNTIME-001 Phase B: agents/skills/agent_skills/agent_runs", _create_v1),
]


def default_agent_db_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_AGENT_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "agents.db"


class AgentRegistryStore:
    """SQLite persistence for agents, skills and their bindings.

    Same posture as every other store in this project: 0700 state dir, 0600 db
    file, WAL, row_factory=Row, migrations through schema.py's tracked
    Migration/apply_migrations from the first version."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_agent_db_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, AGENT_MIGRATIONS)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- agents ----------------------------------------------------------

    @staticmethod
    def _agent(row: sqlite3.Row) -> Agent:
        return Agent(
            id=row["id"], name=row["name"], project_id=row["project_id"],
            description=row["description"], runtime=row["runtime"], state=row["state"],
            max_sessions=row["max_sessions"], repo=row["repo"], workspace=row["workspace"],
            model=row["model"], metadata=_parse(row["metadata"], {}),
            created_at=row["created_at"], updated_at=row["updated_at"],
            disabled_at=row["disabled_at"])

    def create_agent(self, agent_id: str, *, name: str | None = None, project_id: str | None = None,
                     description: str = "", runtime: str | None = None,
                     max_sessions: int = DEFAULT_MAX_SESSIONS, repo: str | None = None,
                     workspace: str | None = None, model: str | None = None,
                     metadata: dict[str, Any] | None = None) -> Agent:
        if not valid_slug(agent_id):
            raise AgentRegistryError(
                f"invalid agent id {agent_id!r}: lowercase letters, digits, '-' and '_' only, "
                f"1-64 characters")
        if max_sessions < 1:
            raise AgentRegistryError("max_sessions must be at least 1")
        now = _now()
        try:
            with self._connection() as connection:
                connection.execute(
                    "INSERT INTO agents (id, name, project_id, description, runtime, state, "
                    "max_sessions, repo, workspace, model, metadata, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, name or agent_id, project_id, description, runtime, AGENT_ACTIVE,
                     max_sessions, repo, workspace, model, _json(metadata), now, now))
        except sqlite3.IntegrityError as exc:
            raise AgentRegistryError(f"agent {agent_id!r} already exists") from exc
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def get_agent(self, agent_id: str) -> Agent | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return self._agent(row) if row else None

    def list_agents(self, *, project_id: str | None = None, state: str | None = None,
                    limit: int = 200) -> list[Agent]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if state:
            clauses.append("state = ?")
            params.append(state)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM agents {where} ORDER BY name LIMIT ?", (*params, limit)).fetchall()
        return [self._agent(row) for row in rows]

    #: Fields update_agent will write. Anything else is refused by name rather
    #: than silently ignored -- a typo'd field that appears to succeed is how a
    #: caller comes to believe it changed something it did not.
    UPDATABLE = ("name", "project_id", "description", "runtime", "max_sessions",
                 "repo", "workspace", "model", "metadata", "state")

    def update_agent(self, agent_id: str, **fields: Any) -> Agent:
        agent = self.get_agent(agent_id)
        if agent is None:
            raise AgentRegistryError(f"agent {agent_id!r} not found")
        unknown = sorted(key for key in fields if key not in self.UPDATABLE)
        if unknown:
            raise AgentRegistryError(
                f"cannot update {', '.join(unknown)}; updatable fields are "
                f"{', '.join(self.UPDATABLE)}")
        if "state" in fields and fields["state"] not in AGENT_STATES:
            raise AgentRegistryError(f"state must be one of {', '.join(AGENT_STATES)}")
        if "max_sessions" in fields and int(fields["max_sessions"]) < 1:
            raise AgentRegistryError("max_sessions must be at least 1")
        assignments, params = [], []
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            params.append(_json(value) if key == "metadata" else value)
        if "state" in fields:
            assignments.append("disabled_at = ?")
            params.append(_now() if fields["state"] == AGENT_DISABLED else None)
        assignments.append("updated_at = ?")
        params.append(_now())
        with self._connection() as connection:
            connection.execute(f"UPDATE agents SET {', '.join(assignments)} WHERE id = ?",
                               (*params, agent_id))
        return self.get_agent(agent_id)  # type: ignore[return-value]

    def disable_agent(self, agent_id: str) -> Agent:
        """Disable, never delete.

        An agent owns tasks and a run history; deleting the row would orphan
        both and make a completed task's `agent_id` a dangling reference. A
        disabled agent keeps answering questions about what it did, and simply
        stops being started."""
        return self.update_agent(agent_id, state=AGENT_DISABLED)

    def enable_agent(self, agent_id: str) -> Agent:
        return self.update_agent(agent_id, state=AGENT_ACTIVE)

    # -- skills ----------------------------------------------------------

    @staticmethod
    def _skill(row: sqlite3.Row) -> Skill:
        return Skill(
            id=row["id"], version=row["version"], name=row["name"], summary=row["summary"],
            source=row["source"], package_path=row["package_path"], content_sha=row["content_sha"],
            body=row["body"], metadata=_parse(row["metadata"], {}), created_at=row["created_at"])

    def register_skill(self, skill_id: str, *, version: str, body: str, name: str = "",
                       summary: str = "", source: str = "inline", package_path: str | None = None,
                       content_sha: str | None = None,
                       metadata: dict[str, Any] | None = None) -> Skill:
        """Record one VERSION of a skill. Re-registering the same (id, version)
        with identical content is a no-op; with different content it is an
        error, because silently changing what a version means would make every
        evidence trail that cites it wrong."""
        if not valid_slug(skill_id):
            raise AgentRegistryError(
                f"invalid skill id {skill_id!r}: lowercase letters, digits, '-' and '_' only")
        if not version or not isinstance(version, str):
            raise AgentRegistryError("a skill version is required")
        existing = self.get_skill(skill_id, version=version)
        if existing is not None:
            if existing.body == body:
                return existing
            raise AgentRegistryError(
                f"skill {skill_id!r} version {version!r} is already registered with different "
                f"content; register a new version instead of redefining this one")
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO skills (id, version, name, summary, source, package_path, "
                "content_sha, body, metadata, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (skill_id, version, name or skill_id, summary, source, package_path,
                 content_sha, body, _json(metadata), _now()))
        return self.get_skill(skill_id, version=version)  # type: ignore[return-value]

    def get_skill(self, skill_id: str, *, version: str | None = None) -> Skill | None:
        """One skill version, or the newest when none is named.

        "Newest" is by registration time, not by parsing the version string: a
        version is an opaque label here, and inventing semver ordering for
        labels this store never validated would order "1.10" before "1.9"."""
        with self._connection() as connection:
            if version and version != LATEST:
                row = connection.execute(
                    "SELECT * FROM skills WHERE id = ? AND version = ?",
                    (skill_id, version)).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM skills WHERE id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (skill_id,)).fetchone()
        return self._skill(row) if row else None

    def list_skills(self, *, latest_only: bool = True, limit: int = 200) -> list[Skill]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM skills ORDER BY id, created_at DESC, rowid DESC").fetchall()
        skills = [self._skill(row) for row in rows]
        if not latest_only:
            return skills[:limit]
        seen: set[str] = set()
        newest: list[Skill] = []
        for skill in skills:
            if skill.id in seen:
                continue
            seen.add(skill.id)
            newest.append(skill)
        return newest[:limit]

    def skill_versions(self, skill_id: str) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT version FROM skills WHERE id = ? ORDER BY created_at DESC, rowid DESC",
                (skill_id,)).fetchall()
        return [row["version"] for row in rows]

    # -- bindings ----------------------------------------------------------

    def bind_skill(self, agent_id: str, skill_id: str, *, kind: str = SKILL_BASE,
                   version: str | None = None) -> SkillBinding:
        if self.get_agent(agent_id) is None:
            raise AgentRegistryError(f"agent {agent_id!r} not found")
        if kind not in SKILL_KINDS:
            raise AgentRegistryError(f"kind must be one of {', '.join(SKILL_KINDS)}")
        if self.get_skill(skill_id, version=version) is None:
            raise AgentRegistryError(
                f"skill {skill_id!r}"
                + (f" version {version!r}" if version and version != LATEST else "")
                + " is not registered; register it before binding")
        pinned = None if not version or version == LATEST else version
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO agent_skills (agent_id, skill_id, kind, version, created_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_id, skill_id, kind) DO UPDATE SET version = excluded.version",
                (agent_id, skill_id, kind, pinned, _now()))
            row = connection.execute(
                "SELECT * FROM agent_skills WHERE agent_id = ? AND skill_id = ? AND kind = ?",
                (agent_id, skill_id, kind)).fetchone()
        return SkillBinding(agent_id=row["agent_id"], skill_id=row["skill_id"], kind=row["kind"],
                            version=row["version"], created_at=row["created_at"])

    def unbind_skill(self, agent_id: str, skill_id: str, *, kind: str | None = None) -> int:
        with self._connection() as connection:
            if kind:
                cursor = connection.execute(
                    "DELETE FROM agent_skills WHERE agent_id = ? AND skill_id = ? AND kind = ?",
                    (agent_id, skill_id, kind))
            else:
                cursor = connection.execute(
                    "DELETE FROM agent_skills WHERE agent_id = ? AND skill_id = ?",
                    (agent_id, skill_id))
            return cursor.rowcount

    def agent_skills(self, agent_id: str, *, kind: str | None = None) -> list[SkillBinding]:
        clause = " AND kind = ?" if kind else ""
        params: tuple[Any, ...] = (agent_id, kind) if kind else (agent_id,)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_skills WHERE agent_id = ?{clause} ORDER BY kind, skill_id",
                params).fetchall()
        return [SkillBinding(agent_id=row["agent_id"], skill_id=row["skill_id"], kind=row["kind"],
                             version=row["version"], created_at=row["created_at"]) for row in rows]

    def resolve_skills(self, agent_id: str, *, extra: Sequence[str] = (),
                       include_task_skills: bool = False) -> list[str]:
        """The skill ids a run should load, as `id@version` strings.

        BASE skills always; TASK skills only when the caller asks for them.
        The version is resolved HERE, at start time, so the run records what
        was actually loaded rather than a floating pointer that means something
        different next week."""
        resolved: list[str] = []
        wanted = [(binding.skill_id, binding.version) for binding in self.agent_skills(agent_id)
                  if binding.kind == SKILL_BASE
                  or (include_task_skills and binding.kind == SKILL_TASK)]
        wanted.extend((str(item), None) for item in extra)
        for skill_id, version in wanted:
            skill = self.get_skill(skill_id, version=version)
            label = f"{skill_id}@{skill.version}" if skill else str(skill_id)
            if label not in resolved:
                resolved.append(label)
        return resolved

    # -- run history --------------------------------------------------------

    def record_run(self, agent_id: str, *, task_id: str | None, session: str | None = None,
                   node_id: str | None = None, status: str = "STARTED",
                   detail: str | None = None) -> None:
        """One line per (agent, task). Upserted, so a task's run row tracks its
        latest state instead of the history growing a row per transition --
        the dashboard wants "what happened to this task", not a transition log,
        which queue_events already is."""
        now = _now()
        with self._connection() as connection:
            if task_id:
                connection.execute(
                    "INSERT INTO agent_runs (agent_id, task_id, session, node_id, status, detail, "
                    "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(agent_id, task_id) WHERE task_id IS NOT NULL DO UPDATE SET "
                    "session = excluded.session, node_id = excluded.node_id, "
                    "status = excluded.status, detail = excluded.detail, "
                    "updated_at = excluded.updated_at",
                    (agent_id, task_id, session, node_id, status, detail, now, now))
            else:
                connection.execute(
                    "INSERT INTO agent_runs (agent_id, task_id, session, node_id, status, detail, "
                    "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (agent_id, None, session, node_id, status, detail, now, now))

    def recent_runs(self, agent_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                (agent_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def run_counts(self, agent_id: str) -> dict[str, int]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM agent_runs WHERE agent_id = ? "
                "GROUP BY status", (agent_id,)).fetchall()
        return {row["status"]: row["total"] for row in rows}
