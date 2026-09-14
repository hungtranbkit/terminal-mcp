"""Notes / Ideas -- the service half: everything that touches the
FILESYSTEM (attachment bytes) plus the one place note validation and
error codes live, so the MCP tools (mcp_app.py) and the dashboard routes
(dashboard.py) share one implementation instead of two drifting copies.

Why attachments are files, never DB blobs: see notes_store.py's header.
This module owns that boundary and all four of its risks:

  1. MIME is decided by the BYTES, not by the caller. A declared
     content-type and a file extension are both attacker-controlled; the
     magic-number sniff below is the authority, and a file whose real
     type is not on the allowlist is refused even if it is named .png.
  2. The on-disk name is a generated uuid + the extension canonical for
     the SNIFFED type -- the caller's filename never reaches the
     filesystem at all (it is kept in the DB for display only). That
     makes path traversal structurally impossible rather than filtered:
     there is no user-controlled component in the path to traverse with.
  3. Reads are confined. `source_path` (the local-connector transport)
     must resolve inside a configured root, and serving an attachment
     re-verifies that the DB's storage_path is still inside the
     attachments directory before opening it -- so a tampered row cannot
     turn the serving endpoint into an arbitrary-file reader.
  4. Writes are atomic: bytes go to a temp file in the SAME directory
     (same filesystem, so os.replace is a rename, not a copy) and are
     fsync'd before the replace. A crash mid-write leaves a stray temp
     file, never a truncated attachment a note already points at.

Attachment transport, stated honestly (the task's own "Không giả vờ hỗ trợ
một file transport MCP mà runtime hiện không có"): this MCP runtime has no
binary channel. So there are exactly three real ways bytes arrive, all
tested:
  - `source_path`: a file already on this host, inside an allowed root.
    This is the local-connector path -- ChatGPT saves/downloads the image
    through a shell it already drives, then names the path.
  - `data_base64`: an in-band string, decoded HERE and written straight
    to a file. Never stored base64 in the DB. Size-capped on the DECODED
    length, checked before the decode allocates.
  - the dashboard's own multipart upload (dashboard.py), for a human
    dragging a screenshot into the web UI.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import logging
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from .notes_store import (
    STATUS_APPLIED, NotesError, NotesStore, default_notes_path, new_attachment_id,
)

_LOGGER = logging.getLogger(__name__)

# Magic-number table. Deliberately short: these are the types the UI can
# actually PREVIEW, which is the whole point of an attachment here. A
# format not in this table is refused rather than stored un-renderable.
MIME_PNG = "image/png"
MIME_JPEG = "image/jpeg"
MIME_WEBP = "image/webp"
MIME_GIF = "image/gif"

CANONICAL_EXTENSIONS = {
    MIME_PNG: ".png",
    MIME_JPEG: ".jpg",
    MIME_WEBP: ".webp",
    MIME_GIF: ".gif",
}
DEFAULT_ALLOWED_MIME_TYPES = (MIME_PNG, MIME_JPEG, MIME_WEBP, MIME_GIF)

# 10 MiB. A screenshot is ~100KB-2MB; this leaves room for a full-page
# capture while keeping one careless paste from filling the disk.
DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

_FILENAME_SAFE = re.compile(r"[^\w \-.()\[\]]", flags=re.UNICODE)
_MAX_FILENAME_CHARS = 120




def sniff_mime(head: bytes) -> str | None:
    """Decide the type from the bytes. Returns None for anything not on
    the allowlist -- including a real image format we deliberately do not
    accept, which is refused the same way arbitrary binary is."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return MIME_PNG
    if head.startswith(b"\xff\xd8\xff"):
        return MIME_JPEG
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return MIME_WEBP
    if head.startswith((b"GIF87a", b"GIF89a")):
        return MIME_GIF
    return None


def probe_dimensions(head: bytes) -> tuple[int | None, int | None]:
    """Width/height straight out of the header, no image library (Pillow is
    NOT a dependency of this project and adding one to read two integers
    would be a poor trade). Best-effort by design: an unparseable header
    returns (None, None) and the attachment is still stored -- dimensions
    are a nice-to-have for the gallery's layout, never a gate."""
    try:
        if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
            return (int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big"))
        if head.startswith((b"GIF87a", b"GIF89a")) and len(head) >= 10:
            return (int.from_bytes(head[6:8], "little"), int.from_bytes(head[8:10], "little"))
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            chunk = head[12:16]
            if chunk == b"VP8X" and len(head) >= 30:
                width = int.from_bytes(head[24:27], "little") + 1
                height = int.from_bytes(head[27:30], "little") + 1
                return (width, height)
            if chunk == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
                width = int.from_bytes(head[26:28], "little") & 0x3FFF
                height = int.from_bytes(head[28:30], "little") & 0x3FFF
                return (width, height)
            if chunk == b"VP8L" and len(head) >= 25:
                bits = int.from_bytes(head[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            return (None, None)
        if head.startswith(b"\xff\xd8\xff"):
            # Walk the JPEG marker chain to the first SOFn frame header.
            offset = 2
            limit = len(head)
            while offset + 9 <= limit:
                if head[offset] != 0xFF:
                    offset += 1
                    continue
                marker = head[offset + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    offset += 2
                    continue
                segment_length = int.from_bytes(head[offset + 2:offset + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                              0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height = int.from_bytes(head[offset + 5:offset + 7], "big")
                    width = int.from_bytes(head[offset + 7:offset + 9], "big")
                    return (width, height)
                if segment_length <= 0:
                    break
                offset += 2 + segment_length
    except (IndexError, ValueError):  # pragma: no cover - malformed header
        return (None, None)
    return (None, None)


def sanitise_filename(filename: str | None, *, mime_type: str) -> str:
    """The DISPLAY name only -- it never becomes part of a path (see this
    module's header). Still sanitised, because it is rendered in the web
    UI and sent in a Content-Disposition header: strip directory
    components, normalise unicode, drop control/separator characters,
    bound the length, and force the extension canonical for the sniffed
    type so a .png-named JPEG cannot mislead a downstream viewer."""
    extension = CANONICAL_EXTENSIONS[mime_type]
    raw = (filename or "").strip()
    # PurePosixPath/ntpath both: a Windows-style "..\\x" must not survive
    # as a name either, even though it is only ever displayed.
    raw = raw.replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFC", raw)
    raw = "".join(char for char in raw if unicodedata.category(char)[0] != "C")
    raw = _FILENAME_SAFE.sub("_", raw).strip(" .")
    stem = raw[: -len(extension)] if raw.casefold().endswith(extension) else raw
    for other in set(CANONICAL_EXTENSIONS.values()) | {".jpeg"}:
        if stem.casefold().endswith(other):
            stem = stem[: -len(other)]
            break
    stem = stem.strip(" ._") or "attachment"
    stem = stem[:_MAX_FILENAME_CHARS]
    return f"{stem}{extension}"


def default_attachments_dir() -> Path:
    """Sits beside the notes DB under the same state root, so the two
    halves of one note back up and relocate together (an XDG_STATE_HOME
    override moves both, which is what makes an isolated test run or a
    second instance actually isolated)."""
    override = os.environ.get("TERMINAL_MCP_NOTES_ATTACHMENTS_DIR")
    if override:
        return Path(override).expanduser()
    return default_notes_path().parent / "notes_attachments"


class NotesService:
    """One implementation of every note operation, shared by the MCP tool
    surface and the dashboard routes.

    Single-user by design, and that is a real, documented limitation, not
    an oversight: this dashboard has ONE operator identity (webauth.py /
    Cloudflare Access in front of it), so a note has no owner column and
    every authenticated caller sees every note. A half-built multi-tenant
    model (an owner column nothing enforces) would be worse than none --
    it would read like a boundary while enforcing nothing. Adding real
    per-user scoping later means one migration plus a filter in
    _filter_sql, and the dashboard's own identity plumbing already exists
    to feed it.
    """

    def __init__(self, store: NotesStore | None = None, *, attachments_dir: str | Path | None = None,
                 max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES,
                 allowed_mime_types: tuple[str, ...] = DEFAULT_ALLOWED_MIME_TYPES,
                 attachment_source_roots: tuple[str, ...] = ()) -> None:
        self.store = store or NotesStore()
        self.attachments_dir = Path(attachments_dir).expanduser() if attachments_dir \
            else default_attachments_dir()
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        self.max_attachment_bytes = int(max_attachment_bytes)
        self.allowed_mime_types = tuple(allowed_mime_types)
        self.attachment_source_roots = tuple(
            str(Path(root).expanduser().resolve()) for root in attachment_source_roots)

    @classmethod
    def from_config(cls, config: Any, store: NotesStore | None = None) -> NotesService:
        """Build from AppConfig.notes (config.py's NotesConfig)."""
        notes_config = getattr(config, "notes", None)
        if notes_config is None:
            return cls(store)
        return cls(
            store,
            attachments_dir=notes_config.attachments_dir or None,
            max_attachment_bytes=notes_config.max_attachment_bytes,
            allowed_mime_types=tuple(notes_config.allowed_mime_types),
            attachment_source_roots=tuple(notes_config.attachment_source_roots),
        )

    # -- notes (thin pass-through; the store owns validation) --------------

    def create(self, **fields: Any) -> dict[str, Any]:
        note = self.store.create(**fields)
        # Moderate logging (the task's own "log action quan trọng ở mức vừa
        # phải, không ghi nội dung nhạy cảm"): identity + shape only. The
        # note's own text is never logged -- that is the part most likely to
        # be private, and it is already durably stored anyway.
        _LOGGER.info("note created id=%s type=%s status=%s tags=%d project=%s",
                     note["id"], note["type"], note["status"], len(note["tags"]),
                     note["project_id"] or note["project_name"] or "-")
        return note

    def get(self, note_id: str, *, include_deleted: bool = False) -> dict[str, Any]:
        return self.store.get(note_id, include_deleted=include_deleted)

    def list(self, **filters: Any) -> dict[str, Any]:
        return self.store.list(**filters)

    def search(self, query: str, **filters: Any) -> dict[str, Any]:
        return self.store.search(query, **filters)

    def update(self, note_id: str, **fields: Any) -> dict[str, Any]:
        note = self.store.update(note_id, **fields)
        _LOGGER.info("note updated id=%s fields=%s", note_id, ",".join(sorted(fields)))
        return note

    def link_to_project(self, note_id: str, **fields: Any) -> dict[str, Any]:
        return self.store.link_to_project(note_id, **fields)

    def mark_applied(self, note_id: str, **fields: Any) -> dict[str, Any]:
        note = self.store.mark_applied(note_id, **fields)
        _LOGGER.info("note applied id=%s at=%s ref=%s", note_id, note["applied_at"],
                     note["applied_ref"] or "-")
        return note

    def restore(self, note_id: str) -> dict[str, Any]:
        return self.store.restore(note_id)

    def delete(self, note_id: str, *, hard: bool = False) -> dict[str, Any]:
        """Soft by default. `hard=True` also unlinks every attachment file
        -- the only path in this module that removes bytes, and it reports
        what it removed rather than doing it silently."""
        if not hard:
            result = self.store.soft_delete(note_id)
            _LOGGER.info("note soft-deleted id=%s", note_id)
            return result
        attachments = self.store.hard_delete(note_id)
        removed = 0
        for record in attachments:
            if self._unlink_attachment_file(record.storage_path):
                removed += 1
        _LOGGER.info("note hard-deleted id=%s attachments=%d files_removed=%d",
                     note_id, len(attachments), removed)
        return {"ok": True, "note_id": note_id, "hard": True,
                "attachments_removed": len(attachments), "files_removed": removed}

    def facets(self) -> dict[str, Any]:
        body = self.store.facets()
        body["attachments_dir"] = str(self.attachments_dir)
        body["max_attachment_bytes"] = self.max_attachment_bytes
        body["allowed_mime_types"] = list(self.allowed_mime_types)
        return body

    # -- attachments -------------------------------------------------------

    def add_attachment(self, note_id: str, *, filename: str | None = None,
                       source_path: str | None = None, data_base64: str | None = None,
                       stream: BinaryIO | None = None, data: bytes | None = None,
                       declared_mime_type: str | None = None) -> dict[str, Any]:
        """Exactly one transport per call. `declared_mime_type` is accepted
        (browsers and callers send it) but only ever CHECKED against the
        sniffed type -- never trusted in its place."""
        transports = [name for name, value in (("source_path", source_path),
                                               ("data_base64", data_base64),
                                               ("stream", stream), ("data", data))
                      if value is not None]
        if len(transports) != 1:
            raise NotesError("ATTACHMENT_TRANSPORT_REQUIRED",
                             "pass exactly one of source_path / data_base64 / stream / data",
                             given=transports)
        # Fail before reading any bytes if the note is not there to hold
        # them -- otherwise a bad note_id leaves an orphan file behind.
        self.store.get(note_id)

        if source_path is not None:
            payload, display_name = self._read_source_path(source_path, filename)
        elif data_base64 is not None:
            payload = self._decode_base64(data_base64)
            display_name = filename
        elif stream is not None:
            payload = self._read_stream(stream)
            display_name = filename
        else:
            payload = bytes(data or b"")
            display_name = filename
        return self._store_bytes(note_id, payload, display_name, declared_mime_type)

    def _store_bytes(self, note_id: str, payload: bytes, filename: str | None,
                     declared_mime_type: str | None) -> dict[str, Any]:
        if not payload:
            raise NotesError("ATTACHMENT_EMPTY", "attachment has no bytes")
        if len(payload) > self.max_attachment_bytes:
            raise NotesError("ATTACHMENT_TOO_LARGE",
                             f"attachment is {len(payload)} bytes, limit is {self.max_attachment_bytes}",
                             size=len(payload), max_bytes=self.max_attachment_bytes)
        mime_type = sniff_mime(payload[:64])
        if mime_type is None or mime_type not in self.allowed_mime_types:
            raise NotesError("ATTACHMENT_MIME_NOT_ALLOWED",
                             "attachment content is not an allowed image type "
                             f"({', '.join(self.allowed_mime_types)})",
                             detected=mime_type, declared=declared_mime_type,
                             allowed=list(self.allowed_mime_types))
        if declared_mime_type and declared_mime_type.split(";")[0].strip().casefold() not in (
                mime_type, "application/octet-stream"):
            # A mismatch is refused, not silently corrected: it means the
            # caller and the bytes disagree about what this file is, and
            # guessing which one is right is how a mislabelled file ends up
            # served with the wrong Content-Type.
            raise NotesError("ATTACHMENT_MIME_MISMATCH",
                             f"declared {declared_mime_type} but content is {mime_type}",
                             detected=mime_type, declared=declared_mime_type)
        display_name = sanitise_filename(filename, mime_type=mime_type)
        width, height = probe_dimensions(payload[:1024])
        digest = hashlib.sha256(payload).hexdigest()
        attachment_id = new_attachment_id()
        storage_path = self._write_atomic(attachment_id, mime_type, payload)
        try:
            record = self.store.add_attachment(
                note_id, filename=display_name, mime_type=mime_type, size=len(payload),
                sha256=digest, storage_path=str(storage_path), width=width, height=height,
                attachment_id=attachment_id)
        except Exception:
            # The row is the source of truth; a file with no row is an
            # orphan nothing will ever clean up, so undo the write.
            self._unlink_attachment_file(str(storage_path))
            raise
        _LOGGER.info("note attachment added note=%s attachment=%s mime=%s size=%d",
                     note_id, record.id, mime_type, record.size)
        return record.to_dict()

    def remove_attachment(self, attachment_id: str) -> dict[str, Any]:
        record = self.store.remove_attachment(attachment_id)
        removed = self._unlink_attachment_file(record.storage_path)
        _LOGGER.info("note attachment removed note=%s attachment=%s file_removed=%s",
                     record.note_id, record.id, removed)
        return {"ok": True, "attachment_id": record.id, "note_id": record.note_id,
                "file_removed": removed}

    def open_attachment(self, attachment_id: str) -> tuple[dict[str, Any], bytes]:
        """Read an attachment's bytes for serving. Re-verifies containment
        (risk 3 in this module's header) before opening: the path comes out
        of the DB, and a DB row is not a capability to read any file on
        this host."""
        record = self.store.get_attachment(attachment_id)
        path = self._resolve_inside_attachments(record.storage_path)
        try:
            payload = path.read_bytes()
        except FileNotFoundError:
            raise NotesError("ATTACHMENT_FILE_MISSING",
                             "the attachment's file is gone from storage",
                             attachment_id=attachment_id) from None
        except OSError as exc:
            raise NotesError("ATTACHMENT_UNREADABLE", f"cannot read attachment: {exc}",
                             attachment_id=attachment_id) from None
        if record.mime_type not in self.allowed_mime_types:
            # Belt and braces: a row written by an older/looser build must
            # not be served with an arbitrary Content-Type.
            raise NotesError("ATTACHMENT_MIME_NOT_ALLOWED", "stored type is not servable",
                             detected=record.mime_type)
        return record.to_dict(), payload

    # -- filesystem plumbing ----------------------------------------------

    def _write_atomic(self, attachment_id: str, mime_type: str, payload: bytes) -> Path:
        now = datetime.now(timezone.utc)
        directory = self.attachments_dir / f"{now.year:04d}" / f"{now.month:02d}"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{attachment_id}{CANONICAL_EXTENSIONS[mime_type]}"
        handle, temp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(directory))
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, target)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise
        return target

    def _unlink_attachment_file(self, storage_path: str) -> bool:
        try:
            path = self._resolve_inside_attachments(storage_path)
        except NotesError:
            # Refuse to unlink anything outside the store's own directory,
            # whatever the row claims.
            _LOGGER.warning("refusing to unlink attachment path outside the notes store")
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:  # pragma: no cover - permissions/IO
            _LOGGER.warning("could not unlink attachment file: %s", exc)
            return False

    def _resolve_inside_attachments(self, candidate: str | Path) -> Path:
        root = self.attachments_dir.resolve()
        path = Path(candidate).expanduser()
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        if resolved != root and root not in resolved.parents:
            raise NotesError("ATTACHMENT_PATH_OUTSIDE_STORE",
                             "attachment path is outside the notes attachment store")
        return resolved

    def _read_source_path(self, source_path: str, filename: str | None) -> tuple[bytes, str | None]:
        if not self.attachment_source_roots:
            raise NotesError("ATTACHMENT_SOURCE_DISABLED",
                             "source_path attachments are disabled: configure "
                             "notes.attachment_source_roots first, or use data_base64")
        path = Path(source_path).expanduser()
        if not path.is_absolute():
            raise NotesError("ATTACHMENT_SOURCE_NOT_ABSOLUTE",
                             "source_path must be an absolute path", source_path=source_path)
        try:
            resolved = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise NotesError("ATTACHMENT_SOURCE_NOT_FOUND", "source_path does not exist",
                             source_path=source_path) from None
        # resolve(strict=True) has already followed every symlink, so this
        # check applies to the REAL target -- a symlink inside an allowed
        # root pointing at /etc/shadow does not pass.
        if not self._inside_any_root(resolved):
            raise NotesError("ATTACHMENT_SOURCE_NOT_ALLOWED",
                             "source_path is outside every configured "
                             "notes.attachment_source_roots entry",
                             allowed_roots=list(self.attachment_source_roots))
        if not resolved.is_file():
            raise NotesError("ATTACHMENT_SOURCE_NOT_A_FILE", "source_path is not a regular file",
                             source_path=source_path)
        size = resolved.stat().st_size
        if size > self.max_attachment_bytes:
            raise NotesError("ATTACHMENT_TOO_LARGE",
                             f"attachment is {size} bytes, limit is {self.max_attachment_bytes}",
                             size=size, max_bytes=self.max_attachment_bytes)
        return resolved.read_bytes(), filename or resolved.name

    def _inside_any_root(self, resolved: Path) -> bool:
        for root in self.attachment_source_roots:
            root_path = Path(root)
            if resolved == root_path or root_path in resolved.parents:
                return True
        return False

    def _decode_base64(self, data_base64: str) -> bytes:
        payload = data_base64.strip()
        if payload.startswith("data:"):
            # Accept a data: URI verbatim -- it is what a browser's
            # FileReader and many clipboard helpers produce.
            _, _, payload = payload.partition(",")
        # Check the ENCODED length first: base64 is 4/3 the size of the
        # bytes it carries, so this refuses an oversized payload before
        # b64decode allocates it.
        if len(payload) > ((self.max_attachment_bytes + 2) // 3) * 4 + 16:
            raise NotesError("ATTACHMENT_TOO_LARGE",
                             "base64 payload exceeds the attachment size limit",
                             max_bytes=self.max_attachment_bytes)
        try:
            return base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            raise NotesError("ATTACHMENT_BAD_BASE64", "data_base64 is not valid base64") from None

    def _read_stream(self, stream: BinaryIO) -> bytes:
        # Read one byte past the limit so an oversized upload is refused on
        # the strength of what was actually read, without ever buffering
        # the whole of a huge body.
        payload = stream.read(self.max_attachment_bytes + 1)
        if payload and len(payload) > self.max_attachment_bytes:
            raise NotesError("ATTACHMENT_TOO_LARGE",
                             f"attachment exceeds the {self.max_attachment_bytes} byte limit",
                             max_bytes=self.max_attachment_bytes)
        return payload or b""


__all__ = [
    "NotesService", "NotesError", "NotesStore", "STATUS_APPLIED",
    "DEFAULT_ALLOWED_MIME_TYPES", "DEFAULT_MAX_ATTACHMENT_BYTES",
    "default_attachments_dir", "probe_dimensions", "sanitise_filename", "sniff_mime",
]
