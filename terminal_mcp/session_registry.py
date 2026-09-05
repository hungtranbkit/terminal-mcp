"""Persistent Session Registry -- a durable SQLite record of every session
this project has ever discovered/created, independent of whether the
underlying tmux/Windows-backend process is still alive.

Real problem this exists to fix: a tmux-server restart (or a Windows
node-agent restart) silently destroys every live session's only record
of itself -- once the process is gone, so is any way to say "there used
to be a session called X, working on project Y, here's how to get back
to it." This project already tracks ONE narrow slice of that (killed_
sessions.py, populated only by an explicit terminal_kill_session call) --
this store is the general case: EVERY session this process has ever
observed (created by this project, or merely discovered running on the
host), reconciled on every listing pass, never deleted just because the
runtime process disappeared.

Canonical identity is `(node_id, session_name)` -- node-aware by
construction (task's own "Canonical identity phải node-aware/stable"),
so a same-named session on two different nodes is two distinct rows,
never conflated. This mirrors controller.py's own "node_id/session"
qualified-name convention (resolve_session) rather than inventing a
second naming scheme.

Status lifecycle (`status` column), one direction at a time, never a
guess:
  ACTIVE   -- observed running as of the most recent reconcile pass.
  MISSING  -- was ACTIVE, is no longer in the node's own live session
              list, but the NODE itself is still reachable (the session
              itself is gone -- tmux-server restart, Windows agent
              restart, an out-of-band kill, ...).
  OFFLINE  -- the NODE itself is unreachable; this session's own fate is
              simply unknown until that node reconnects (never
              downgraded to MISSING just because we can't currently ask).
  KILLED   -- an explicit terminal_kill_session/terminal_delete_session
              call is why this session is gone (same event killed_
              sessions.py already captures reopen metadata for).
  DELETED  -- a real, deliberate "permanently forget this registry row"
              action (terminal_registry_purge) -- a SEPARATE action from
              Kill, confirmed hard by its own caller, never implied by
              Kill/Delete of the runtime session (task item 6's own
              explicit requirement). The row is kept (a tombstone, not a
              hard DELETE) so a purge itself has an audit trail.

Nothing here ever gates read/input authorization -- that stays exactly
where it already was (grants.py/core.py's _read_authorized/
_input_authorized). `read_granted`/`input_granted` here are a point-in-
time INFORMATIONAL snapshot only (for display/search/history), always
re-read live from grants.py at the moment any real access decision is
made, same "never a second, possibly-divergent copy of an authorization
decision" discipline as every other snapshot field in this project.
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

REGISTRY_MIGRATIONS: list[Migration] = [
    Migration(1, "baseline: session_records", lambda connection: None),
]

STATUS_ACTIVE = "ACTIVE"
STATUS_MISSING = "MISSING"
STATUS_OFFLINE = "OFFLINE"
STATUS_KILLED = "KILLED"
STATUS_DELETED = "DELETED"
# "Recoverable" (task's own vocabulary) is never a stored status of its
# own -- it's these three, computed at read time (see SessionRecord.
# recoverable below) exactly like `allowed`/`effective_read` already
# fold two things into one derived field elsewhere in this project,
# rather than a fourth status value that could drift out of sync with
# what these three already mean.
RECOVERABLE_STATUSES = (STATUS_MISSING, STATUS_OFFLINE, STATUS_KILLED)

# Bounded, read-only git introspection -- never shell=True, never a
# caller-supplied argv, only these four fixed, well-known subcommands
# against a caller-supplied CWD (already validated by the caller against
# allowed_cwd_roots before this is ever invoked from a live reconcile
# pass -- see core.py's TerminalService._reconcile_session_registry).
_GIT_TIMEOUT_SECONDS = 3.0


def _run_git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=_GIT_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def probe_project_info(cwd: str | None) -> dict[str, str | None]:
    """Best-effort, read-only project/git introspection for `cwd` -- never
    raises, never mutates anything, returns all-None fields for a path
    that isn't a git repo (or doesn't exist, or `git` isn't installed).
    Deliberately narrow: exactly the 4 fields task item 2 asks for
    (repo_root/git_remote/git_branch/last_commit), nothing else (no
    diff, no log history, no file listing)."""
    if not cwd or not os.path.isdir(cwd):
        return {"repo_root": None, "git_remote": None, "git_branch": None, "last_commit": None}
    repo_root = _run_git(cwd, "rev-parse", "--show-toplevel")
    if repo_root is None:
        return {"repo_root": None, "git_remote": None, "git_branch": None, "last_commit": None}
    return {
        "repo_root": repo_root,
        "git_remote": _run_git(cwd, "remote", "get-url", "origin"),
        "git_branch": _run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
        "last_commit": _run_git(cwd, "log", "-1", "--format=%H %ci %s"),
    }


def default_session_registry_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_SESSION_REGISTRY_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "session_registry.db"


@dataclass(frozen=True)
class SessionRecord:
    node_id: str
    session_name: str
    display_name: str | None
    node_name: str | None
    backend_type: str | None
    cwd: str | None
    repo_root: str | None
    git_remote: str | None
    git_branch: str | None
    last_commit: str | None
    agent_type: str | None
    launch_command: str | None
    launcher_type: str | None
    created_at: str
    last_seen_at: str
    last_activity_at: str | None
    last_known_state: str | None
    status: str
    killed_at: str | None
    deleted_at: str | None
    offline_at: str | None
    metadata_complete: bool
    read_granted: bool
    input_granted: bool
    grant_updated_at: str | None
    binding_names: tuple[str, ...] = field(default_factory=tuple)
    notes: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def recoverable(self) -> bool:
        return self.status in RECOVERABLE_STATUSES and self.metadata_complete

    def key(self) -> str:
        # The one qualified-name string every caller/UI/search result
        # uses -- same "node_id/session" shape controller.py's own
        # resolve_session already accepts everywhere else.
        return f"{self.node_id}/{self.session_name}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _from_row(row: sqlite3.Row | None) -> SessionRecord | None:
    if row is None:
        return None
    return SessionRecord(
        node_id=row["node_id"], session_name=row["session_name"], display_name=row["display_name"],
        node_name=row["node_name"], backend_type=row["backend_type"], cwd=row["cwd"],
        repo_root=row["repo_root"], git_remote=row["git_remote"], git_branch=row["git_branch"],
        last_commit=row["last_commit"], agent_type=row["agent_type"], launch_command=row["launch_command"],
        launcher_type=row["launcher_type"], created_at=row["created_at"], last_seen_at=row["last_seen_at"],
        last_activity_at=row["last_activity_at"], last_known_state=row["last_known_state"],
        status=row["status"], killed_at=row["killed_at"], deleted_at=row["deleted_at"],
        offline_at=row["offline_at"], metadata_complete=bool(row["metadata_complete"]),
        read_granted=bool(row["read_granted"]), input_granted=bool(row["input_granted"]),
        grant_updated_at=row["grant_updated_at"],
        binding_names=tuple(json.loads(row["binding_names"] or "[]")),
        notes=row["notes"], tags=tuple(json.loads(row["tags"] or "[]")),
    )


class SessionRegistryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_session_registry_path()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS session_records (
                    node_id TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    display_name TEXT,
                    node_name TEXT,
                    backend_type TEXT,
                    cwd TEXT,
                    repo_root TEXT,
                    git_remote TEXT,
                    git_branch TEXT,
                    last_commit TEXT,
                    agent_type TEXT,
                    launch_command TEXT,
                    launcher_type TEXT,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_activity_at TEXT,
                    last_known_state TEXT,
                    status TEXT NOT NULL,
                    killed_at TEXT,
                    deleted_at TEXT,
                    offline_at TEXT,
                    metadata_complete INTEGER NOT NULL DEFAULT 0,
                    read_granted INTEGER NOT NULL DEFAULT 0,
                    input_granted INTEGER NOT NULL DEFAULT 0,
                    grant_updated_at TEXT,
                    binding_names TEXT NOT NULL DEFAULT '[]',
                    notes TEXT,
                    tags TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (node_id, session_name)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_records_status ON session_records(status)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS drop_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_id TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    detail TEXT,
                    acknowledged INTEGER NOT NULL DEFAULT 0,
                    acknowledged_at TEXT,
                    acknowledged_by TEXT,
                    recovered INTEGER NOT NULL DEFAULT 0,
                    recovered_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_drop_events_ack ON drop_events(acknowledged, detected_at)"
            )
            apply_migrations(connection, REGISTRY_MIGRATIONS)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    # -- reads -----------------------------------------------------------

    def get(self, node_id: str, session_name: str) -> SessionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM session_records WHERE node_id = ? AND session_name = ?",
                (node_id, session_name),
            ).fetchone()
        return _from_row(row)

    def list(self, *, node_id: str | None = None, statuses: tuple[str, ...] | None = None,
             limit: int = 500) -> list[SessionRecord]:
        limit = max(1, min(limit, 2000))
        clauses, params = [], []
        if node_id is not None:
            clauses.append("node_id = ?")
            params.append(node_id)
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM session_records {where} ORDER BY last_seen_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [r for r in (_from_row(row) for row in rows) if r is not None]

    def search(self, query: str, *, limit: int = 100) -> list[SessionRecord]:
        """Search by session name / cwd / repo_root / git_remote / node_id
        / notes -- task item 9's own explicit scenario: a session lost its
        NAME (recreated, renamed, or simply gone) but the project it was
        working on is still findable by path/repo. Also task item 10's own
        "ChatGPT phải tìm được bằng 'session quản lý bán hàng đâu'" --
        the query is split into whitespace-separated WORDS, each of which
        must appear somewhere across a row's combined searchable text
        (never all of `query` as one contiguous substring): a query of
        "ban hang" (a natural-language phrase, space-separated) still
        matches a session literally named "quan_ly_ban_hang"
        (underscore-separated) this way, without needing a real fuzzy
        matcher or a second, normalized copy of every field. Case-
        insensitive throughout (Python's own .casefold(), broader than
        SQLite's ASCII-only LIKE) -- fetches the whole table and filters
        in Python rather than SQL for this reason; a session inventory
        this project would ever have stays small enough that this is
        never a real performance concern."""
        limit = max(1, min(limit, 500))
        words = [w for w in query.casefold().split() if w]
        if not words:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM session_records ORDER BY last_seen_at DESC"
            ).fetchall()
        matched: list[SessionRecord] = []
        for row in rows:
            haystack = " ".join(str(row[col] or "") for col in
                                ("session_name", "cwd", "repo_root", "git_remote", "node_id",
                                 "display_name", "notes")).casefold()
            if all(word in haystack for word in words):
                record = _from_row(row)
                if record is not None:
                    matched.append(record)
            if len(matched) >= limit:
                break
        return matched

    # -- writes ------------------------------------------------------------

    def upsert_seen(self, node_id: str, session_name: str, *, node_name: str | None = None,
                    backend_type: str | None = None, cwd: str | None = None,
                    agent_type: str | None = None, launch_command: str | None = None,
                    launcher_type: str | None = None, last_known_state: str | None = None,
                    read_granted: bool = False, input_granted: bool = False,
                    binding_names: tuple[str, ...] = (), backfill_project: bool = True,
                    now: str | None = None) -> SessionRecord:
        """Called on every reconcile pass for a session CURRENTLY observed
        alive -- status always becomes ACTIVE (a session that reappears
        after being MISSING/KILLED/OFFLINE is exactly as "back" as one
        that was ACTIVE all along; killed_at/offline_at are cleared, not
        just left stale). Existing project-info fields (cwd/repo_root/...)
        are preserved rather than overwritten with None when a caller
        doesn't have fresher info to offer this call -- only a genuinely
        new, non-empty value ever replaces what's already recorded."""
        now = now or _now_iso()
        existing = self.get(node_id, session_name)
        # Only actually shell out to git the FIRST time this session's cwd
        # is seen (or if the cwd itself changed) -- a reconcile pass runs
        # on every dashboard poll, and re-probing already-known project
        # info on every single one of those would be a real, needless
        # per-poll subprocess cost for something that essentially never
        # changes for a long-lived session.
        need_probe = backfill_project and cwd and (existing is None or existing.repo_root is None
                                                    or existing.cwd != cwd)
        project = probe_project_info(cwd) if need_probe else {}
        repo_root = project.get("repo_root") or (existing.repo_root if existing else None)
        git_remote = project.get("git_remote") or (existing.git_remote if existing else None)
        git_branch = project.get("git_branch") or (existing.git_branch if existing else None)
        last_commit = project.get("last_commit") or (existing.last_commit if existing else None)
        cwd = cwd or (existing.cwd if existing else None)
        agent_type = agent_type or (existing.agent_type if existing else None)
        metadata_complete = bool(agent_type) and (agent_type == "shell" or bool(cwd))
        created_at = existing.created_at if existing else now
        binding_json = json.dumps(list(binding_names) or (list(existing.binding_names) if existing else []))
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO session_records
                   (node_id, session_name, node_name, backend_type, cwd, repo_root, git_remote,
                    git_branch, last_commit, agent_type, launch_command, launcher_type,
                    created_at, last_seen_at, last_activity_at, last_known_state, status,
                    killed_at, deleted_at, offline_at, metadata_complete,
                    read_granted, input_granted, grant_updated_at, binding_names)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE',
                           NULL, NULL, NULL, ?, ?, ?, ?, ?)
                   ON CONFLICT(node_id, session_name) DO UPDATE SET
                       node_name = excluded.node_name, backend_type = excluded.backend_type,
                       cwd = excluded.cwd, repo_root = excluded.repo_root,
                       git_remote = excluded.git_remote, git_branch = excluded.git_branch,
                       last_commit = excluded.last_commit, agent_type = excluded.agent_type,
                       launch_command = COALESCE(excluded.launch_command, session_records.launch_command),
                       launcher_type = COALESCE(excluded.launcher_type, session_records.launcher_type),
                       last_seen_at = excluded.last_seen_at, last_activity_at = excluded.last_activity_at,
                       last_known_state = excluded.last_known_state, status = 'ACTIVE',
                       killed_at = NULL, deleted_at = NULL, offline_at = NULL,
                       metadata_complete = excluded.metadata_complete,
                       read_granted = excluded.read_granted, input_granted = excluded.input_granted,
                       grant_updated_at = excluded.grant_updated_at, binding_names = excluded.binding_names
                """,
                (node_id, session_name, node_name, backend_type, cwd, repo_root, git_remote,
                 git_branch, last_commit, agent_type, launch_command, launcher_type,
                 created_at, now, now, last_known_state,
                 int(metadata_complete), int(read_granted), int(input_granted), now, binding_json),
            )
        return self.get(node_id, session_name)

    def mark_missing(self, node_id: str, session_names_seen: set[str], *, now: str | None = None) -> list[str]:
        """The other half of reconcile: any record for `node_id` that was
        ACTIVE as of the previous pass, but is NOT in `session_names_seen`
        this time, is now MISSING -- the session itself vanished (tmux-
        server restart, an out-of-band kill, ...) even though the NODE
        answered fine. Returns the names actually transitioned (empty on
        an ordinary pass where nothing changed)."""
        now = now or _now_iso()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT session_name FROM session_records WHERE node_id = ? AND status = 'ACTIVE'",
                (node_id,),
            ).fetchall()
            vanished = [r["session_name"] for r in rows if r["session_name"] not in session_names_seen]
            if vanished:
                connection.executemany(
                    "UPDATE session_records SET status = 'MISSING', offline_at = NULL "
                    "WHERE node_id = ? AND session_name = ?",
                    [(node_id, name) for name in vanished],
                )
        return vanished

    def mark_node_offline(self, node_id: str, *, now: str | None = None) -> int:
        """Every currently-ACTIVE record for a node that just went
        unreachable becomes OFFLINE (never MISSING -- MISSING means the
        node confirmed the session is gone; OFFLINE means we simply
        cannot ask right now, so the session's own fate stays unknown
        until the node reconnects and a real reconcile pass runs)."""
        now = now or _now_iso()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE session_records SET status = 'OFFLINE', offline_at = ? "
                "WHERE node_id = ? AND status = 'ACTIVE'",
                (now, node_id),
            )
        return cursor.rowcount

    # -- watchdog: unexpected drop events -----------------------------------
    # (task: "theo dõi và noti cho user biết session nào bị rớt đột ngột,
    # và hỗ trợ hồi phục nó") -- deliberately keyed off mark_missing's own
    # ALREADY-COMPUTED "vanished" list (core.py's caller passes it straight
    # here) rather than re-deriving anything: a session that was ACTIVE and
    # is now gone, with NO corresponding terminal_kill_session/_delete_
    # session call, is exactly "dropped unexpectedly" -- an explicit Kill
    # never reaches this path at all (see core.py's terminal_kill_session,
    # which calls mark_killed, never mark_missing, for the session it just
    # killed).

    def record_drop_event(self, node_id: str, session_name: str, kind: str, *,
                          detail: str | None = None, now: str | None = None) -> int:
        now = now or _now_iso()
        with self._connection() as connection:
            cursor = connection.execute(
                "INSERT INTO drop_events (node_id, session_name, kind, detected_at, detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (node_id, session_name, kind, now, detail),
            )
            return int(cursor.lastrowid)

    def list_drop_events(self, *, unacknowledged_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
        clause = "WHERE acknowledged = 0" if unacknowledged_only else ""
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM drop_events {clause} ORDER BY detected_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_drop_event(self, event_id: int, *, by: str | None = None) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE drop_events SET acknowledged = 1, acknowledged_at = ?, acknowledged_by = ? WHERE id = ?",
                (_now_iso(), by, event_id),
            )
        return cursor.rowcount == 1

    def mark_drop_event_recovered(self, event_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE drop_events SET recovered = 1, recovered_at = ? WHERE id = ?",
                (_now_iso(), event_id),
            )
        return cursor.rowcount == 1

    def mark_drop_events_recovered_for(self, node_id: str, session_name: str) -> int:
        """A session that had an unrecovered drop event is seen ACTIVE
        again (reopened through terminal_registry_reopen, or simply
        recreated under the same name some other way) -- called from the
        SAME reconcile pass that upserts it back to ACTIVE (core.py), so
        this never needs its own polling loop. Marks every still-
        unrecovered event for that (node, name), not just the most recent
        one -- a session that dropped, got reopened, dropped again, and
        is now back a third time should not leave an earlier event
        looking unresolved forever."""
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE drop_events SET recovered = 1, recovered_at = ? "
                "WHERE node_id = ? AND session_name = ? AND recovered = 0",
                (_now_iso(), node_id, session_name),
            )
        return cursor.rowcount

    def mark_killed(self, node_id: str, session_name: str, *, killed_by: str | None = None,
                    reopen_metadata: dict[str, Any] | None = None, cwd: str | None = None,
                    agent_type: str | None = None, backend_type: str | None = None,
                    now: str | None = None) -> SessionRecord | None:
        """An UPSERT, not a plain UPDATE -- a session created and killed in
        quick succession, with no reconcile pass (no dashboard/MCP listing
        call) ever having run in between, would otherwise have NO row to
        update at all, silently losing the kill event and its metadata
        entirely. `cwd`/`agent_type`/`backend_type`, when the caller
        already captured them at kill time (core.py's terminal_kill_
        session already does, for killed_sessions.py's own reopen
        metadata -- reused here rather than re-derived), seed a
        brand-new row exactly like reconcile would have; for an existing
        row they're used only to fill in a still-missing field, never to
        overwrite a value reconcile already captured while the session
        was alive."""
        now = now or _now_iso()
        existing = self.get(node_id, session_name)
        effective_cwd = existing.cwd if existing and existing.cwd else cwd
        effective_agent = existing.agent_type if existing and existing.agent_type else agent_type
        effective_backend = existing.backend_type if existing and existing.backend_type else backend_type
        metadata_complete = bool(effective_agent) and (effective_agent == "shell" or bool(effective_cwd))
        created_at = existing.created_at if existing else now
        notes = json.dumps(reopen_metadata) if reopen_metadata else (existing.notes if existing else None)
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO session_records
                   (node_id, session_name, backend_type, cwd, agent_type, created_at, last_seen_at,
                    status, killed_at, metadata_complete, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'KILLED', ?, ?, ?)
                   ON CONFLICT(node_id, session_name) DO UPDATE SET
                       status = 'KILLED', killed_at = excluded.killed_at,
                       cwd = COALESCE(session_records.cwd, excluded.cwd),
                       agent_type = COALESCE(session_records.agent_type, excluded.agent_type),
                       backend_type = COALESCE(session_records.backend_type, excluded.backend_type),
                       metadata_complete = excluded.metadata_complete,
                       notes = COALESCE(excluded.notes, session_records.notes)""",
                (node_id, session_name, effective_backend, effective_cwd, effective_agent, created_at, now,
                 now, int(metadata_complete), notes),
            )
        return self.get(node_id, session_name)

    def touch_grant(self, node_id: str, session_name: str, *, read_granted: bool, input_granted: bool,
                    now: str | None = None) -> None:
        """Informational snapshot only -- see this module's own docstring.
        A no-op if the session has no registry row yet (grants can exist
        for a session this registry has never reconciled, e.g. right
        after a fresh grant on a session created before this feature
        existed -- the next ordinary reconcile pass will create the row
        and pick this up then)."""
        now = now or _now_iso()
        with self._connection() as connection:
            connection.execute(
                "UPDATE session_records SET read_granted = ?, input_granted = ?, grant_updated_at = ? "
                "WHERE node_id = ? AND session_name = ?",
                (int(read_granted), int(input_granted), now, node_id, session_name),
            )

    def purge(self, node_id: str, session_name: str, *, purged_by: str | None = None,
              now: str | None = None) -> bool:
        """The ONE real hard-delete path (task item 6: "Delete permanently
        là action riêng có confirm mạnh") -- everywhere else in this
        module only ever changes `status`, never removes a row. Still
        keeps a minimal tombstone (a single row in the same table, status
        DELETED) rather than a bare SQL DELETE, so a purge itself has a
        durable record (task's own "Có tombstone/history")."""
        now = now or _now_iso()
        existing = self.get(node_id, session_name)
        if existing is None:
            return False
        with self._connection() as connection:
            connection.execute(
                "UPDATE session_records SET status = 'DELETED', deleted_at = ?, "
                "notes = ? WHERE node_id = ? AND session_name = ?",
                (now, f"purged by {purged_by or 'unknown'} at {now}", node_id, session_name),
            )
        return True

    def upsert_manual(self, node_id: str, session_name: str, *, status: str, node_name: str | None = None,
                      backend_type: str | None = None, cwd: str | None = None, agent_type: str | None = None,
                      backfill_project: bool = True, notes: str | None = None,
                      now: str | None = None) -> SessionRecord:
        """Manual/migration entry point -- task item 11 (ingest existing
        killed-session metadata) and item 13 (backfill a record for a
        project whose session is already gone with no other trace, e.g.
        found only via old audit-log text). Never overwrites an existing
        row's status if one already exists and is more specific than
        what's being inserted here -- this is for FILLING GAPS, not
        overriding a reconcile pass's own, more current view."""
        now = now or _now_iso()
        existing = self.get(node_id, session_name)
        if existing is not None:
            return existing
        project = probe_project_info(cwd) if (backfill_project and cwd) else {}
        metadata_complete = bool(agent_type) and (agent_type == "shell" or bool(cwd))
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO session_records
                   (node_id, session_name, node_name, backend_type, cwd, repo_root, git_remote,
                    git_branch, last_commit, agent_type, created_at, last_seen_at, status,
                    metadata_complete, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(node_id, session_name) DO NOTHING""",
                (node_id, session_name, node_name, backend_type, cwd, project.get("repo_root"),
                 project.get("git_remote"), project.get("git_branch"), project.get("last_commit"),
                 agent_type, now, now, status, int(metadata_complete), notes),
            )
        return self.get(node_id, session_name)
