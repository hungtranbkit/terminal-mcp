"""Notes / Ideas Store -- a cross-project, local-first kho ghi chú: the
durable half of "user thấy một ý tưởng hay trong khi chat, nói 'lưu lại',
và tìm lại được nó sáu tuần sau".

Deliberately NOT tied to any one project (unlike backlog_store.py, whose
source of truth is a JSON file inside one repo). A note is captured
mid-conversation, often before anybody knows which project it belongs to
-- so `project_id`/`project_name` are nullable, settable later
(note_link_to_project), and never part of the identity. One controller-
side SQLite file holds every project's notes together, because the whole
value is cross-project recall: "đã lưu gì về landing page" must search
everything at once, which a per-repo file layout cannot do.

Distinct from session_knowledge.py, which is the closest neighbour and
shares this file's SQLite/FTS5 shape: that store captures what a session
*emitted*, automatically, with a retention cap. This one holds what a
human deliberately *kept*, with no retention cap at all -- an idea is
never evicted to save space.

Two storage tiers, on purpose:
  - SQLite (this file) holds text + metadata only.
  - Attachment BYTES live on the filesystem (notes_service.py owns that
    half). The DB stores only metadata + a storage_path. A base64 blob
    in a SQLite column would make every note_list/note_search query drag
    megabytes of image through the row cache, and would make "backup the
    notes DB" mean a multi-GB file -- so the bytes stay out of it.

Full-text search is FTS5 over title/summary/original_content/analysis/
tags, ranked by bm25, maintained EXPLICITLY (not by trigger) because the
indexed `tags_text` column is a derived projection of the JSON `tags`
column and no trigger can compute it. Every write path goes through
_reindex() in the same transaction as the row write, so the index cannot
drift from the table. FTS5 missing from a sqlite3 build degrades to a
LIKE scan -- slower, still correct -- the same documented fallback
session_knowledge.py already uses.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import Migration, apply_migrations

SCHEMA_VERSION = 1

# Type: what KIND of thing was kept. Open-ended in spirit but a closed
# list in code -- a typo'd type silently creates a filter bucket the UI
# never shows, which is how a note becomes unfindable.
TYPE_IDEA = "idea"
TYPE_REFERENCE = "reference"
TYPE_TODO = "todo"
TYPE_RESEARCH = "research"
TYPE_PROMPT = "prompt"
TYPE_DESIGN = "design"
TYPE_OTHER = "other"
TYPES = (TYPE_IDEA, TYPE_REFERENCE, TYPE_TODO, TYPE_RESEARCH, TYPE_PROMPT, TYPE_DESIGN, TYPE_OTHER)

# Status: how far the idea has travelled. ARCHIVED is a resting place, not
# a deletion -- deletion is `deleted_at` (soft), a separate axis, so
# "archived" stays browsable.
STATUS_NEW = "new"
STATUS_REVIEWING = "reviewing"
STATUS_PLANNED = "planned"
STATUS_APPLIED = "applied"
STATUS_ARCHIVED = "archived"
STATUSES = (STATUS_NEW, STATUS_REVIEWING, STATUS_PLANNED, STATUS_APPLIED, STATUS_ARCHIVED)

SORT_NEWEST = "newest"
SORT_OLDEST = "oldest"
SORT_UPDATED = "updated"
SORTS = (SORT_NEWEST, SORT_OLDEST, SORT_UPDATED)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
TITLE_FALLBACK_CHARS = 80

# bm25 column weights, in the notes_fts column order below. note_id is
# UNINDEXED but still occupies a bm25 argument slot. Title outranks
# everything (a note whose *title* is about landing pages is a better hit
# than one that merely mentions the phrase deep in a pasted transcript);
# tags are weighted high too because they are deliberate human labels,
# not incidental prose.
_BM25_WEIGHTS = (0.0, 10.0, 4.0, 2.0, 2.0, 6.0)


def default_notes_path() -> Path:
    """Same three-step resolution every other store in this project uses
    (explicit env var -> XDG_STATE_HOME -> ~/.local/state), so an
    isolated test run or a second instance redirects this store exactly
    the way it redirects all the others."""
    override = os.environ.get("TERMINAL_MCP_NOTES_DB")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "notes.db"


def new_note_id() -> str:
    return f"note_{uuid.uuid4().hex}"


def new_attachment_id() -> str:
    return f"att_{uuid.uuid4().hex}"


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _create_baseline_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            original_content TEXT NOT NULL DEFAULT '',
            analysis TEXT NOT NULL DEFAULT '',
            source_url TEXT,
            source_chat TEXT,
            source_session TEXT,
            type TEXT NOT NULL DEFAULT 'idea',
            status TEXT NOT NULL DEFAULT 'new',
            tags TEXT NOT NULL DEFAULT '[]',
            project_id TEXT,
            project_name TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            applied_at TEXT,
            applied_ref TEXT,
            deleted_at TEXT
        )
        """
    )
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(deleted_at, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notes_status ON notes(deleted_at, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notes_type ON notes(deleted_at, type, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(deleted_at, project_id, created_at DESC)",
    ):
        connection.execute(statement)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS note_attachments (
            id TEXT PRIMARY KEY,
            note_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            storage_path TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_note_attachments_note "
        "ON note_attachments(note_id, created_at)"
    )


NOTES_MIGRATIONS: list[Migration] = [
    Migration(1, "notes + note_attachments baseline (Notes/Ideas V1)", _create_baseline_schema),
]


class NotesError(Exception):
    """Carries a stable, machine-readable code -- the MCP/dashboard layers
    turn `code` straight into their own error field, so a caller can
    branch on it instead of parsing prose."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        body = {"error": self.code, "message": self.message}
        body.update(self.detail)
        return body


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    note_id: str
    filename: str
    mime_type: str
    size: int
    sha256: str
    storage_path: str
    width: int | None
    height: int | None
    created_at: str

    def to_dict(self, *, include_storage_path: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id, "note_id": self.note_id, "filename": self.filename,
            "mime_type": self.mime_type, "size": self.size, "sha256": self.sha256,
            "width": self.width, "height": self.height, "created_at": self.created_at,
            # The browser/ChatGPT-facing way to fetch the bytes. Never the
            # on-disk path: that is an internal detail and handing it out
            # is how a filesystem gets probed (see notes_service.py's own
            # traversal defence).
            "url": f"/dashboard/api/notes/attachment?id={self.id}",
        }
        if include_storage_path:
            body["storage_path"] = self.storage_path
        return body


def normalise_tags(tags: Any) -> list[str]:
    """Tags arrive from three places (MCP kwargs, a dashboard form, a
    JSON column) in three shapes. One normaliser for all of them: a
    comma-separated string splits, a list stringifies, everything is
    trimmed, empties dropped, case-insensitive duplicates collapsed to
    the FIRST spelling seen (so "MESFlow" is not shadowed by a later
    "mesflow"), order preserved."""
    if tags is None:
        return []
    if isinstance(tags, str):
        candidates = tags.split(",")
    elif isinstance(tags, (list, tuple, set)):
        candidates = list(tags)
    else:
        raise NotesError("INVALID_TAGS", "tags must be a list or a comma-separated string")
    out: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        tag = str(raw).strip()
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out


def fallback_title(*sources: str | None) -> str:
    """A note with no title is still a note -- ChatGPT sometimes has only
    the pasted content. Build a short one from the first non-empty source
    rather than storing "" and rendering a blank card."""
    for source in sources:
        text = " ".join((source or "").split())
        if not text:
            continue
        if len(text) <= TITLE_FALLBACK_CHARS:
            return text
        return text[:TITLE_FALLBACK_CHARS].rstrip() + "…"
    return "Ghi chú không có tiêu đề"


def fts_query(query: str) -> str:
    """Every whitespace-separated term becomes its own quoted FTS5
    phrase, ANDed (FTS5's implicit default). Identical reasoning to
    session_knowledge.py's own _fts_query: a query containing FTS5
    operator characters (NEAR, ^, *, :, AND/OR) is treated as literal
    text, never parsed as query syntax -- no user-supplied FTS5
    injection, and a query like "landing page (v2)" cannot raise."""
    terms = query.split()
    if not terms:
        return '""'
    return " ".join('"' + term.replace('"', '""') + '"' for term in terms)


class NotesStore:
    """SQLite persistence for notes + attachment metadata. Text only --
    attachment bytes are notes_service.py's business."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else default_notes_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_available = True
        self._migrate()

    # -- schema ------------------------------------------------------------

    def _migrate(self) -> None:
        """Versioned (PRAGMA user_version via schema.apply_migrations --
        this project's newest convention, see schema.py) rather than the
        older "ALTER TABLE if the column happens to be absent" pattern:
        this store is brand new, so it starts life with a real, ordered,
        tracked migration list and every future change appends to it.
        Idempotent by construction -- a second open applies nothing."""
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            apply_migrations(connection, NOTES_MIGRATIONS)
            self._ensure_fts(connection)
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    def _ensure_fts(self, connection: sqlite3.Connection) -> None:
        """Outside the versioned migrations on purpose: whether FTS5 exists
        is a property of the sqlite3 BUILD, not of the database's schema
        version. Stamping a migration as applied on a build without FTS5
        would permanently mark this db as indexed when it is not -- so the
        virtual table is (re)attempted on every open and its absence is
        recorded in memory only."""
        try:
            connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5("
                "note_id UNINDEXED, title, summary, original_content, analysis, tags_text, "
                "tokenize='unicode61 remove_diacritics 2')"
            )
        except sqlite3.OperationalError:
            # FTS5 absent from this sqlite3 build. search() falls back to a
            # LIKE scan -- slower, still correct; nothing else cares.
            self._fts_available = False

    @property
    def fts_available(self) -> bool:
        return self._fts_available

    # -- connection plumbing (same shape as session_knowledge.py) ---------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextlib.contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # -- FTS index maintenance --------------------------------------------

    def _reindex(self, connection: sqlite3.Connection, row: sqlite3.Row | dict) -> None:
        """Called inside the SAME transaction as every row write, so the
        index and the table commit or roll back together."""
        if not self._fts_available:
            return
        note_id = row["id"]
        connection.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
        if row["deleted_at"]:
            # A soft-deleted note leaves the index entirely: search must
            # not surface it, and filtering it out afterwards would
            # silently shrink every page of results below its limit.
            return
        tags = json.loads(row["tags"]) if row["tags"] else []
        connection.execute(
            "INSERT INTO notes_fts(note_id, title, summary, original_content, analysis, tags_text) "
            "VALUES(?,?,?,?,?,?)",
            (note_id, row["title"] or "", row["summary"] or "", row["original_content"] or "",
             row["analysis"] or "", " ".join(tags)),
        )

    def reindex_all(self) -> int:
        """Rebuild the whole FTS index from the notes table. Not needed in
        normal operation (every write reindexes); it exists so a DB
        restored from a backup taken mid-write, or one written by an older
        build, can be repaired without touching the data."""
        if not self._fts_available:
            return 0
        with self._connection() as connection:
            connection.execute("DELETE FROM notes_fts")
            rows = connection.execute("SELECT * FROM notes WHERE deleted_at IS NULL").fetchall()
            for row in rows:
                self._reindex(connection, row)
            return len(rows)

    # -- validation --------------------------------------------------------

    @staticmethod
    def validate_type(value: str | None) -> str:
        if value is None or value == "":
            return TYPE_IDEA
        if value not in TYPES:
            raise NotesError("INVALID_TYPE", f"type must be one of {', '.join(TYPES)}",
                             given=value, allowed=list(TYPES))
        return value

    @staticmethod
    def validate_status(value: str | None) -> str:
        if value is None or value == "":
            return STATUS_NEW
        if value not in STATUSES:
            raise NotesError("INVALID_STATUS", f"status must be one of {', '.join(STATUSES)}",
                             given=value, allowed=list(STATUSES))
        return value

    # -- writes ------------------------------------------------------------

    def create(self, *, title: str | None = None, summary: str = "", original_content: str = "",
               analysis: str = "", source_url: str | None = None, source_chat: str | None = None,
               source_session: str | None = None, type: str | None = None, status: str | None = None,
               tags: Any = None, project_id: str | None = None, project_name: str | None = None,
               note_id: str | None = None) -> dict[str, Any]:
        note_type = self.validate_type(type)
        note_status = self.validate_status(status)
        tag_list = normalise_tags(tags)
        # Checked BEFORE deriving a fallback title: the fallback is never
        # empty ("Ghi chú không có tiêu đề"), so testing it here would make
        # this guard unreachable and store a note with no content at all.
        if not any((field or "").strip() for field in
                   (title, summary, original_content, analysis, source_url)):
            raise NotesError("EMPTY_NOTE", "a note needs at least one of title/summary/"
                                           "original_content/analysis/source_url")
        resolved_title = (title or "").strip() or fallback_title(summary, original_content, analysis, source_url)
        now = iso_now()
        record_id = note_id or new_note_id()
        with self._connection() as connection:
            existing = connection.execute("SELECT id FROM notes WHERE id = ?", (record_id,)).fetchone()
            if existing is not None:
                raise NotesError("NOTE_EXISTS", f"note {record_id} already exists", note_id=record_id)
            connection.execute(
                "INSERT INTO notes(id, title, summary, original_content, analysis, source_url, "
                "source_chat, source_session, type, status, tags, project_id, project_name, "
                "created_at, updated_at, applied_at, applied_ref, deleted_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (record_id, resolved_title, summary or "", original_content or "", analysis or "",
                 source_url or None, source_chat or None, source_session or None, note_type,
                 note_status, json.dumps(tag_list, ensure_ascii=False), project_id or None,
                 project_name or None, now, now,
                 now if note_status == STATUS_APPLIED else None, None),
            )
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (record_id,)).fetchone()
            self._reindex(connection, row)
        return self.get(record_id)

    _UPDATABLE = ("title", "summary", "original_content", "analysis", "source_url",
                  "source_chat", "source_session", "project_id", "project_name", "applied_ref")

    def update(self, note_id: str, **fields: Any) -> dict[str, Any]:
        """Partial update: only keys actually present in `fields` are
        written, so a caller that means "just change the status" cannot
        accidentally blank out the analysis by omitting it."""
        assignments: list[str] = []
        values: list[Any] = []
        for key in self._UPDATABLE:
            if key not in fields:
                continue
            value = fields[key]
            if key in ("title", "summary", "original_content", "analysis"):
                values.append("" if value is None else str(value))
            else:
                text = None if value is None else str(value).strip()
                values.append(text or None)
            assignments.append(f"{key} = ?")
        if "type" in fields:
            assignments.append("type = ?")
            values.append(self.validate_type(fields["type"]))
        if "tags" in fields:
            assignments.append("tags = ?")
            values.append(json.dumps(normalise_tags(fields["tags"]), ensure_ascii=False))
        new_status: str | None = None
        if "status" in fields:
            new_status = self.validate_status(fields["status"])
            assignments.append("status = ?")
            values.append(new_status)
        with self._connection() as connection:
            row = self._require_live_row(connection, note_id)
            if "title" in fields and not str(fields["title"] or "").strip():
                # An explicit blank title re-derives the fallback from
                # whatever the note will hold AFTER this update.
                merged = {**dict(row), **{k: v for k, v in fields.items() if k in self._UPDATABLE}}
                index = assignments.index("title = ?")
                values[index] = fallback_title(merged.get("summary"), merged.get("original_content"),
                                               merged.get("analysis"), merged.get("source_url"))
            if new_status == STATUS_APPLIED and not row["applied_at"]:
                assignments.append("applied_at = ?")
                values.append(iso_now())
            elif new_status is not None and new_status != STATUS_APPLIED:
                # Moving back out of "applied" clears the stamp -- keeping
                # an applied_at on a note that is no longer applied is the
                # kind of quiet inconsistency that makes a report lie.
                assignments.append("applied_at = NULL")
            if not assignments:
                return self.get(note_id)
            assignments.append("updated_at = ?")
            values.append(iso_now())
            connection.execute(f"UPDATE notes SET {', '.join(assignments)} WHERE id = ?",
                               (*values, note_id))
            fresh = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            self._reindex(connection, fresh)
        return self.get(note_id)

    def mark_applied(self, note_id: str, *, applied_ref: str | None = None,
                     project_id: str | None = None, project_name: str | None = None) -> dict[str, Any]:
        fields: dict[str, Any] = {"status": STATUS_APPLIED}
        if applied_ref is not None:
            fields["applied_ref"] = applied_ref
        if project_id is not None:
            fields["project_id"] = project_id
        if project_name is not None:
            fields["project_name"] = project_name
        return self.update(note_id, **fields)

    def link_to_project(self, note_id: str, *, project_id: str | None = None,
                        project_name: str | None = None) -> dict[str, Any]:
        if not (project_id or project_name):
            raise NotesError("PROJECT_REQUIRED", "pass project_id and/or project_name")
        fields: dict[str, Any] = {}
        if project_id is not None:
            fields["project_id"] = project_id
        if project_name is not None:
            fields["project_name"] = project_name
        return self.update(note_id, **fields)

    def soft_delete(self, note_id: str) -> dict[str, Any]:
        """Soft by default: the note leaves every listing and the search
        index but its row (and its attachment files) survive, so a
        mis-clicked delete is recoverable with restore()."""
        with self._connection() as connection:
            self._require_live_row(connection, note_id)
            now = iso_now()
            connection.execute("UPDATE notes SET deleted_at = ?, updated_at = ? WHERE id = ?",
                               (now, now, note_id))
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            self._reindex(connection, row)
        return {"ok": True, "note_id": note_id, "deleted_at": now, "hard": False}

    def restore(self, note_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise NotesError("NOTE_NOT_FOUND", f"no note {note_id}", note_id=note_id)
            connection.execute("UPDATE notes SET deleted_at = NULL, updated_at = ? WHERE id = ?",
                               (iso_now(), note_id))
            fresh = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            self._reindex(connection, fresh)
        return self.get(note_id)

    def hard_delete(self, note_id: str) -> list[AttachmentRecord]:
        """Remove the row for real. Returns the attachment records the
        caller must now unlink -- the store never touches the filesystem
        itself (that is notes_service.py's single responsibility), and
        returning them rather than deleting silently is what keeps
        orphaned files from accumulating."""
        with self._connection() as connection:
            row = connection.execute("SELECT id FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise NotesError("NOTE_NOT_FOUND", f"no note {note_id}", note_id=note_id)
            attachments = [_row_to_attachment(item) for item in connection.execute(
                "SELECT * FROM note_attachments WHERE note_id = ?", (note_id,)).fetchall()]
            connection.execute("DELETE FROM note_attachments WHERE note_id = ?", (note_id,))
            connection.execute("DELETE FROM notes WHERE id = ?", (note_id,))
            if self._fts_available:
                connection.execute("DELETE FROM notes_fts WHERE note_id = ?", (note_id,))
        return attachments

    # -- attachments (metadata only) ---------------------------------------

    def add_attachment(self, note_id: str, *, filename: str, mime_type: str, size: int,
                       sha256: str, storage_path: str, width: int | None = None,
                       height: int | None = None, attachment_id: str | None = None) -> AttachmentRecord:
        with self._connection() as connection:
            self._require_live_row(connection, note_id)
            record_id = attachment_id or new_attachment_id()
            now = iso_now()
            connection.execute(
                "INSERT INTO note_attachments(id, note_id, filename, mime_type, size, sha256, "
                "storage_path, width, height, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, note_id, filename, mime_type, int(size), sha256, storage_path,
                 width, height, now),
            )
            connection.execute("UPDATE notes SET updated_at = ? WHERE id = ?", (now, note_id))
            row = connection.execute("SELECT * FROM note_attachments WHERE id = ?",
                                     (record_id,)).fetchone()
        return _row_to_attachment(row)

    def get_attachment(self, attachment_id: str) -> AttachmentRecord:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM note_attachments WHERE id = ?",
                                     (attachment_id,)).fetchone()
        if row is None:
            raise NotesError("ATTACHMENT_NOT_FOUND", f"no attachment {attachment_id}",
                             attachment_id=attachment_id)
        return _row_to_attachment(row)

    def remove_attachment(self, attachment_id: str) -> AttachmentRecord:
        """Hard-removes the metadata row and returns it so the caller can
        unlink the file. Unlike a note, an attachment is not soft-deleted:
        a row pointing at a file the user asked to remove is worse than no
        row -- the UI would render a broken image forever."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM note_attachments WHERE id = ?",
                                     (attachment_id,)).fetchone()
            if row is None:
                raise NotesError("ATTACHMENT_NOT_FOUND", f"no attachment {attachment_id}",
                                 attachment_id=attachment_id)
            connection.execute("DELETE FROM note_attachments WHERE id = ?", (attachment_id,))
            connection.execute("UPDATE notes SET updated_at = ? WHERE id = ?",
                               (iso_now(), row["note_id"]))
        return _row_to_attachment(row)

    # -- reads -------------------------------------------------------------

    def _require_live_row(self, connection: sqlite3.Connection, note_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise NotesError("NOTE_NOT_FOUND", f"no note {note_id}", note_id=note_id)
        if row["deleted_at"]:
            raise NotesError("NOTE_DELETED", f"note {note_id} is deleted (restore it first)",
                             note_id=note_id, deleted_at=row["deleted_at"])
        return row

    def get(self, note_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            if row is None:
                raise NotesError("NOTE_NOT_FOUND", f"no note {note_id}", note_id=note_id)
            if row["deleted_at"] and not include_deleted:
                raise NotesError("NOTE_DELETED", f"note {note_id} is deleted",
                                 note_id=note_id, deleted_at=row["deleted_at"])
            attachments = [_row_to_attachment(item).to_dict() for item in connection.execute(
                "SELECT * FROM note_attachments WHERE note_id = ? ORDER BY created_at, id",
                (note_id,)).fetchall()]
        return _row_to_note(row, attachments)

    def _filter_sql(self, *, type: str | None, status: str | None, tag: str | None,
                    project: str | None, since: str | None, until: str | None,
                    include_deleted: bool, include_archived: bool,
                    alias: str = "notes") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if not include_deleted:
            clauses.append(f"{alias}.deleted_at IS NULL")
        if type:
            clauses.append(f"{alias}.type = ?")
            values.append(self.validate_type(type))
        if status:
            clauses.append(f"{alias}.status = ?")
            values.append(self.validate_status(status))
        elif not include_archived:
            # Archived notes are hidden unless asked for by name: the
            # default view is "what is still live". Asking for
            # status=archived explicitly always works.
            clauses.append(f"{alias}.status != ?")
            values.append(STATUS_ARCHIVED)
        if tag:
            # Tags are a JSON array in one column. Matching on the
            # rendered JSON with a quoted, case-folded LIKE is exact at
            # the element level ("ui" never matches "uikit") without a
            # second table -- json_each would need a correlated subquery
            # for the same answer.
            clauses.append(f"lower({alias}.tags) LIKE ?")
            values.append(f'%"{tag.strip().casefold()}"%')
        if project:
            needle = f"%{project.strip().casefold()}%"
            clauses.append(f"(lower(COALESCE({alias}.project_id,'')) LIKE ? "
                           f"OR lower(COALESCE({alias}.project_name,'')) LIKE ?)")
            values.extend((needle, needle))
        if since:
            clauses.append(f"{alias}.created_at >= ?")
            values.append(since)
        if until:
            clauses.append(f"{alias}.created_at <= ?")
            values.append(until)
        return (" AND ".join(clauses) if clauses else "1=1"), values

    def list(self, *, type: str | None = None, status: str | None = None, tag: str | None = None,
             project: str | None = None, since: str | None = None, until: str | None = None,
             sort: str = SORT_NEWEST, limit: int = DEFAULT_LIMIT, offset: int = 0,
             include_deleted: bool = False, include_archived: bool = False) -> dict[str, Any]:
        if sort not in SORTS:
            raise NotesError("INVALID_SORT", f"sort must be one of {', '.join(SORTS)}",
                             given=sort, allowed=list(SORTS))
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        where, values = self._filter_sql(type=type, status=status, tag=tag, project=project,
                                         since=since, until=until, include_deleted=include_deleted,
                                         include_archived=include_archived)
        order = {SORT_NEWEST: "notes.created_at DESC, notes.id DESC",
                 SORT_OLDEST: "notes.created_at ASC, notes.id ASC",
                 SORT_UPDATED: "notes.updated_at DESC, notes.id DESC"}[sort]
        with self._connection() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS n FROM notes WHERE {where}", values).fetchone()["n"]
            rows = connection.execute(
                f"SELECT * FROM notes WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*values, limit, offset)).fetchall()
            items = [_row_to_note(row, self._attachments_for(connection, row["id"])) for row in rows]
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "returned": len(items), "has_more": offset + len(items) < total, "sort": sort}

    def search(self, query: str, *, type: str | None = None, status: str | None = None,
               tag: str | None = None, project: str | None = None, since: str | None = None,
               until: str | None = None, limit: int = 20, offset: int = 0,
               include_archived: bool = True) -> dict[str, Any]:
        """Ranked full-text search across title + summary + original_content
        + analysis + tags. `include_archived` defaults TRUE here (unlike
        list()): an explicit search is a recall request -- "did I ever save
        anything about X" -- and silently hiding an archived hit is exactly
        the failure this whole store exists to prevent."""
        query = (query or "").strip()
        limit = max(1, min(int(limit), MAX_LIMIT))
        offset = max(0, int(offset))
        if not query:
            # An empty query is a browse, not an error -- same filters,
            # newest first, so the UI can share one code path.
            listed = self.list(type=type, status=status, tag=tag, project=project, since=since,
                               until=until, limit=limit, offset=offset,
                               include_archived=include_archived)
            listed["query"] = ""
            listed["ranked"] = False
            return listed
        where, values = self._filter_sql(type=type, status=status, tag=tag, project=project,
                                         since=since, until=until, include_deleted=False,
                                         include_archived=include_archived)
        ranked = False
        with self._connection() as connection:
            rows: list[sqlite3.Row] = []
            scores: dict[str, float] = {}
            total = 0
            if self._fts_available:
                try:
                    weights = ", ".join(str(weight) for weight in _BM25_WEIGHTS)
                    sql = (f"SELECT notes.*, bm25(notes_fts, {weights}) AS rank "
                           "FROM notes_fts JOIN notes ON notes.id = notes_fts.note_id "
                           f"WHERE notes_fts MATCH ? AND {where} "
                           "ORDER BY rank ASC, notes.created_at DESC LIMIT ? OFFSET ?")
                    matched = connection.execute(sql, (fts_query(query), *values, limit, offset)).fetchall()
                    total = connection.execute(
                        "SELECT COUNT(*) AS n FROM notes_fts JOIN notes ON notes.id = notes_fts.note_id "
                        f"WHERE notes_fts MATCH ? AND {where}", (fts_query(query), *values)).fetchone()["n"]
                    rows = list(matched)
                    # bm25 is negative-better; report a positive score so
                    # "higher is more relevant" holds for every consumer
                    # (ChatGPT included) without a footnote.
                    scores = {row["id"]: round(-float(row["rank"]), 4) for row in rows}
                    ranked = True
                except sqlite3.OperationalError:
                    rows = []
            if not ranked:
                needle = f"%{query.casefold()}%"
                like = ("(lower(notes.title) LIKE ? OR lower(notes.summary) LIKE ? "
                        "OR lower(notes.original_content) LIKE ? OR lower(notes.analysis) LIKE ? "
                        "OR lower(notes.tags) LIKE ?)")
                like_values = [needle] * 5
                total = connection.execute(
                    f"SELECT COUNT(*) AS n FROM notes WHERE {like} AND {where}",
                    (*like_values, *values)).fetchone()["n"]
                rows = connection.execute(
                    f"SELECT * FROM notes WHERE {like} AND {where} "
                    "ORDER BY notes.created_at DESC LIMIT ? OFFSET ?",
                    (*like_values, *values, limit, offset)).fetchall()
                scores = {}
            items = []
            for position, row in enumerate(rows, start=1 + offset):
                note = _row_to_note(row, self._attachments_for(connection, row["id"]))
                # bm25 alone is not a self-explanatory number (with a single
                # matching document its idf term is 0, so a perfectly good
                # hit scores 0.0). rank_position is the unambiguous signal a
                # consumer summarising results should order by.
                note["rank_position"] = position
                note["score"] = scores.get(row["id"])
                note["excerpt"] = build_excerpt(row, query)
                items.append(note)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "returned": len(items), "has_more": offset + len(items) < total,
                "query": query, "ranked": ranked}

    def _attachments_for(self, connection: sqlite3.Connection, note_id: str) -> list[dict[str, Any]]:
        return [_row_to_attachment(item).to_dict() for item in connection.execute(
            "SELECT * FROM note_attachments WHERE note_id = ? ORDER BY created_at, id",
            (note_id,)).fetchall()]

    def facets(self) -> dict[str, Any]:
        """The values that actually exist, for populating filter dropdowns
        -- so the UI offers "MESFlow" because a note really carries it,
        never a hardcoded list that drifts from the data."""
        with self._connection() as connection:
            types = {row["type"]: row["n"] for row in connection.execute(
                "SELECT type, COUNT(*) AS n FROM notes WHERE deleted_at IS NULL GROUP BY type")}
            statuses = {row["status"]: row["n"] for row in connection.execute(
                "SELECT status, COUNT(*) AS n FROM notes WHERE deleted_at IS NULL GROUP BY status")}
            projects: list[dict[str, Any]] = []
            for row in connection.execute(
                    "SELECT project_id, project_name, COUNT(*) AS n FROM notes "
                    "WHERE deleted_at IS NULL AND (project_id IS NOT NULL OR project_name IS NOT NULL) "
                    "GROUP BY project_id, project_name ORDER BY n DESC"):
                projects.append({"project_id": row["project_id"], "project_name": row["project_name"],
                                 "count": row["n"]})
            tag_counts: dict[str, int] = {}
            tag_labels: dict[str, str] = {}
            for row in connection.execute("SELECT tags FROM notes WHERE deleted_at IS NULL"):
                for tag in json.loads(row["tags"] or "[]"):
                    key = tag.casefold()
                    tag_counts[key] = tag_counts.get(key, 0) + 1
                    tag_labels.setdefault(key, tag)
            tags = [{"tag": tag_labels[key], "count": count}
                    for key, count in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))]
            deleted = connection.execute(
                "SELECT COUNT(*) AS n FROM notes WHERE deleted_at IS NOT NULL").fetchone()["n"]
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM notes WHERE deleted_at IS NULL").fetchone()["n"]
        return {"types": types, "statuses": statuses, "projects": projects, "tags": tags,
                "total": total, "deleted": deleted, "all_types": list(TYPES),
                "all_statuses": list(STATUSES), "fts": self._fts_available}


EXCERPT_RADIUS = 110


def build_excerpt(row: sqlite3.Row | dict, query: str) -> str:
    """A short, deterministic window around the first matching term --
    built here rather than with FTS5's snippet() so it works identically
    on the LIKE fallback path, and so the caller (ChatGPT summarising
    "what did I save about X") gets the same shape either way."""
    terms = [term.casefold() for term in query.split() if term]
    for field in ("summary", "analysis", "original_content", "title"):
        text = " ".join(str((row[field] if field in row.keys() else "") or "").split()) \
            if hasattr(row, "keys") else " ".join(str(row.get(field) or "").split())
        if not text:
            continue
        folded = text.casefold()
        position = -1
        for term in terms:
            position = folded.find(term)
            if position >= 0:
                break
        if position < 0:
            continue
        start = max(0, position - EXCERPT_RADIUS // 2)
        end = min(len(text), start + EXCERPT_RADIUS)
        excerpt = text[start:end]
        return ("…" if start > 0 else "") + excerpt + ("…" if end < len(text) else "")
    # No term literally present (a diacritics-folded FTS hit, e.g. "y
    # tuong" matching "ý tưởng") -- fall back to the head of the note
    # rather than an empty string.
    for field in ("summary", "analysis", "original_content", "title"):
        text = " ".join(str((row[field] if hasattr(row, "keys") else row.get(field)) or "").split())
        if text:
            return text[:EXCERPT_RADIUS] + ("…" if len(text) > EXCERPT_RADIUS else "")
    return ""


def _row_to_note(row: sqlite3.Row, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "summary": row["summary"],
        "original_content": row["original_content"], "analysis": row["analysis"],
        "source_url": row["source_url"], "source_chat": row["source_chat"],
        "source_session": row["source_session"], "type": row["type"], "status": row["status"],
        "tags": json.loads(row["tags"] or "[]"), "project_id": row["project_id"],
        "project_name": row["project_name"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "applied_at": row["applied_at"],
        "applied_ref": row["applied_ref"], "deleted_at": row["deleted_at"],
        "attachments": attachments, "attachment_count": len(attachments),
    }


def _row_to_attachment(row: sqlite3.Row) -> AttachmentRecord:
    return AttachmentRecord(
        id=row["id"], note_id=row["note_id"], filename=row["filename"],
        mime_type=row["mime_type"], size=row["size"], sha256=row["sha256"],
        storage_path=row["storage_path"], width=row["width"], height=row["height"],
        created_at=row["created_at"],
    )
