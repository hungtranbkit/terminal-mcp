"""Attachment storage: real files on a real filesystem, and every one of
the four risks notes_service.py's own header names -- content-sniffed MIME,
no user-controlled path component, confined reads, atomic writes.

The hostile cases here are the point of the file: a shell script named
.png, a declared type that disagrees with the bytes, an oversized upload, a
traversal attempt through both `source_path` and `filename`, and a
tampered DB row pointing outside the store.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from terminal_mcp.notes_service import (
    DEFAULT_ALLOWED_MIME_TYPES, NotesService, default_attachments_dir, probe_dimensions,
    sanitise_filename, sniff_mime,
)
from terminal_mcp.notes_store import NotesError, NotesStore
from tests.fixtures.notes_images import (
    NOT_AN_IMAGE, gif_bytes, jpeg_bytes, png_bytes, webp_bytes,
)


@pytest.fixture
def inbox(tmp_path):
    directory = tmp_path / "inbox"
    directory.mkdir()
    return directory


@pytest.fixture
def service(tmp_path, inbox):
    return NotesService(NotesStore(tmp_path / "notes.db"),
                        attachments_dir=tmp_path / "attachments",
                        attachment_source_roots=(str(inbox),))


@pytest.fixture
def note(service):
    return service.create(title="Có ảnh", summary="một ghi chú có ảnh")


# -- sniffing / dimensions ------------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    (png_bytes(), "image/png"),
    (jpeg_bytes(), "image/jpeg"),
    (webp_bytes(), "image/webp"),
    (gif_bytes(), "image/gif"),
    (NOT_AN_IMAGE, None),
    (b"", None),
    (b"%PDF-1.7\n", None),
    (b"\x89PNG", None),
])
def test_sniff_mime_reads_the_bytes(payload, expected):
    assert sniff_mime(payload[:64]) == expected


@pytest.mark.parametrize("payload,expected", [
    (png_bytes(200, 120), (200, 120)),
    (jpeg_bytes(300, 200), (300, 200)),
    (webp_bytes(64, 48), (64, 48)),
    (gif_bytes(32, 16), (32, 16)),
    (NOT_AN_IMAGE, (None, None)),
])
def test_probe_dimensions_needs_no_image_library(payload, expected):
    assert probe_dimensions(payload[:1024]) == expected


# -- filename sanitising --------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    ("shot.png", "shot.png"),
    ("../../../etc/passwd.png", "passwd.png"),
    ("..\\..\\windows\\system32\\a.png", "a.png"),
    ("/absolute/path/b.png", "b.png"),
    ("Ảnh chụp màn hình.png", "Ảnh chụp màn hình.png"),
    ("no-extension", "no-extension.png"),
    ("weird;|&$name.png", "weird____name.png"),
    ("", "attachment.png"),
    (None, "attachment.png"),
    ("...", "attachment.png"),
])
def test_sanitise_filename_never_yields_a_path(given, expected):
    result = sanitise_filename(given, mime_type="image/png")
    assert result == expected
    assert "/" not in result and "\\" not in result
    assert Path(result).name == result


def test_sanitise_filename_forces_the_extension_of_the_sniffed_type():
    # A JPEG named .png must not keep the misleading extension.
    assert sanitise_filename("photo.png", mime_type="image/jpeg") == "photo.jpg"
    assert sanitise_filename("photo.jpeg", mime_type="image/jpeg") == "photo.jpg"


def test_sanitise_filename_bounds_the_length():
    assert len(sanitise_filename("n" * 5000, mime_type="image/png")) <= 124


# -- the happy path -------------------------------------------------------

def test_add_attachment_writes_a_real_file_and_stores_only_metadata(service, note, tmp_path):
    payload = png_bytes(200, 120)
    record = service.add_attachment(note["id"], filename="Màn hình.png", data=payload)
    assert record["mime_type"] == "image/png"
    assert record["size"] == len(payload)
    assert (record["width"], record["height"]) == (200, 120)
    assert record["filename"] == "Màn hình.png"
    assert record["url"] == "/dashboard/api/notes/attachment?id=" + record["id"]
    # No storage_path is ever handed out through the API surface.
    assert "storage_path" not in record
    stored = list((tmp_path / "attachments").rglob("*.png"))
    assert len(stored) == 1
    assert stored[0].read_bytes() == payload
    # YYYY/MM layout, and the on-disk name is the attachment id -- no part
    # of the caller's filename reached the path.
    assert stored[0].stem == record["id"]
    assert stored[0].parent.name.isdigit() and stored[0].parent.parent.name.isdigit()
    assert "Màn" not in str(stored[0])


def test_the_attachment_file_is_not_world_readable(service, note, tmp_path):
    service.add_attachment(note["id"], data=png_bytes())
    stored = next((tmp_path / "attachments").rglob("*.png"))
    assert stored.stat().st_mode & 0o077 == 0


def test_the_note_reports_its_attachments(service, note):
    service.add_attachment(note["id"], filename="a.png", data=png_bytes())
    service.add_attachment(note["id"], filename="b.jpg", data=jpeg_bytes())
    fresh = service.get(note["id"])
    assert fresh["attachment_count"] == 2
    assert {a["filename"] for a in fresh["attachments"]} == {"a.png", "b.jpg"}


def test_sha256_is_the_real_digest(service, note):
    import hashlib
    payload = png_bytes()
    record = service.add_attachment(note["id"], data=payload)
    assert record["sha256"] == hashlib.sha256(payload).hexdigest()


def test_open_attachment_returns_the_exact_bytes(service, note):
    payload = webp_bytes(64, 48)
    record = service.add_attachment(note["id"], filename="x.webp", data=payload)
    served, served_bytes = service.open_attachment(record["id"])
    assert served_bytes == payload
    assert served["mime_type"] == "image/webp"


def test_removing_an_attachment_deletes_its_file(service, note, tmp_path):
    record = service.add_attachment(note["id"], data=png_bytes())
    stored = next((tmp_path / "attachments").rglob("*.png"))
    assert service.remove_attachment(record["id"])["file_removed"] is True
    assert not stored.exists()
    assert service.get(note["id"])["attachment_count"] == 0
    with pytest.raises(NotesError) as excinfo:
        service.open_attachment(record["id"])
    assert excinfo.value.code == "ATTACHMENT_NOT_FOUND"


def test_hard_deleting_a_note_unlinks_its_images(service, note, tmp_path):
    service.add_attachment(note["id"], data=png_bytes())
    service.add_attachment(note["id"], data=jpeg_bytes())
    result = service.delete(note["id"], hard=True)
    assert result["attachments_removed"] == 2 and result["files_removed"] == 2
    assert not list((tmp_path / "attachments").rglob("*.png"))
    assert not list((tmp_path / "attachments").rglob("*.jpg"))


def test_soft_deleting_a_note_keeps_its_images_on_disk(service, note, tmp_path):
    service.add_attachment(note["id"], data=png_bytes())
    service.delete(note["id"])
    assert len(list((tmp_path / "attachments").rglob("*.png"))) == 1
    service.restore(note["id"])
    assert service.get(note["id"])["attachment_count"] == 1


# -- hostile input --------------------------------------------------------

def test_a_script_named_png_is_refused(service, note):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], filename="innocent.png", data=NOT_AN_IMAGE)
    assert excinfo.value.code == "ATTACHMENT_MIME_NOT_ALLOWED"


def test_a_declared_type_that_disagrees_with_the_bytes_is_refused(service, note):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], filename="a.gif", data=png_bytes(),
                               declared_mime_type="image/gif")
    assert excinfo.value.code == "ATTACHMENT_MIME_MISMATCH"
    assert excinfo.value.detail["detected"] == "image/png"


def test_a_generic_octet_stream_declaration_is_accepted(service, note):
    # Browsers and shells routinely send this; the bytes still decide.
    record = service.add_attachment(note["id"], filename="a.png", data=png_bytes(),
                                    declared_mime_type="application/octet-stream")
    assert record["mime_type"] == "image/png"


def test_a_charset_suffix_on_the_declared_type_is_tolerated(service, note):
    record = service.add_attachment(note["id"], data=png_bytes(),
                                    declared_mime_type="image/png; charset=binary")
    assert record["mime_type"] == "image/png"


def test_a_type_off_the_configured_allowlist_is_refused(tmp_path, note, service):
    narrow = NotesService(service.store, attachments_dir=tmp_path / "narrow",
                          allowed_mime_types=("image/png",))
    with pytest.raises(NotesError) as excinfo:
        narrow.add_attachment(note["id"], data=gif_bytes())
    assert excinfo.value.code == "ATTACHMENT_MIME_NOT_ALLOWED"
    assert excinfo.value.detail["allowed"] == ["image/png"]


def test_an_oversized_attachment_is_refused_and_writes_nothing(tmp_path, service, note):
    service.max_attachment_bytes = 64
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], data=png_bytes(400, 400))
    assert excinfo.value.code == "ATTACHMENT_TOO_LARGE"
    assert not list((tmp_path / "attachments").rglob("*.png"))
    assert service.get(note["id"])["attachment_count"] == 0


def test_an_empty_attachment_is_refused(service, note):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], data=b"")
    assert excinfo.value.code in ("ATTACHMENT_EMPTY", "ATTACHMENT_TRANSPORT_REQUIRED")


def test_exactly_one_transport_per_call(service, note):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"])
    assert excinfo.value.code == "ATTACHMENT_TRANSPORT_REQUIRED"
    with pytest.raises(NotesError):
        service.add_attachment(note["id"], data=png_bytes(), data_base64="x")


def test_attaching_to_a_missing_note_leaves_no_orphan_file(tmp_path, service):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment("note_nope", data=png_bytes())
    assert excinfo.value.code == "NOTE_NOT_FOUND"
    assert not list((tmp_path / "attachments").rglob("*.png"))


# -- base64 transport -----------------------------------------------------

def test_base64_transport_writes_a_file_not_a_blob(service, note, tmp_path):
    import base64
    payload = png_bytes(80, 40)
    record = service.add_attachment(note["id"], filename="b64.png",
                                    data_base64=base64.b64encode(payload).decode())
    assert (record["width"], record["height"]) == (80, 40)
    stored = next((tmp_path / "attachments").rglob("*.png"))
    assert stored.read_bytes() == payload
    # And nothing base64-shaped went into the database.
    with service.store._connection() as connection:  # noqa: SLF001 - asserting storage shape
        row = connection.execute("SELECT * FROM note_attachments WHERE id = ?",
                                 (record["id"],)).fetchone()
    assert base64.b64encode(payload).decode()[:40] not in str(tuple(row))


def test_a_data_uri_is_accepted(service, note):
    import base64
    uri = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    assert service.add_attachment(note["id"], data_base64=uri)["mime_type"] == "image/png"


def test_bad_base64_is_a_clean_error(service, note):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], data_base64="not base64 at all !!!")
    assert excinfo.value.code == "ATTACHMENT_BAD_BASE64"


def test_an_oversized_base64_payload_is_refused_before_decoding(service, note):
    service.max_attachment_bytes = 16
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], data_base64="A" * 10_000)
    assert excinfo.value.code == "ATTACHMENT_TOO_LARGE"


# -- source_path transport ------------------------------------------------

def test_source_path_reads_a_file_inside_an_allowed_root(service, note, inbox):
    (inbox / "screenshot.png").write_bytes(png_bytes(120, 90))
    record = service.add_attachment(note["id"], source_path=str(inbox / "screenshot.png"))
    assert record["filename"] == "screenshot.png"
    assert (record["width"], record["height"]) == (120, 90)


def test_source_path_is_refused_outright_with_no_root_configured(tmp_path, note, service):
    unconfigured = NotesService(service.store, attachments_dir=tmp_path / "a2")
    with pytest.raises(NotesError) as excinfo:
        unconfigured.add_attachment(note["id"], source_path="/etc/hostname")
    assert excinfo.value.code == "ATTACHMENT_SOURCE_DISABLED"


@pytest.mark.parametrize("path", ["/etc/hostname", "/etc/passwd"])
def test_source_path_outside_every_root_is_refused(service, note, path):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path=path)
    assert excinfo.value.code in ("ATTACHMENT_SOURCE_NOT_ALLOWED", "ATTACHMENT_SOURCE_NOT_FOUND")


def test_source_path_traversal_out_of_a_root_is_refused(service, note, inbox, tmp_path):
    (tmp_path / "secret.png").write_bytes(png_bytes())
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path=str(inbox / ".." / "secret.png"))
    assert excinfo.value.code == "ATTACHMENT_SOURCE_NOT_ALLOWED"


def test_a_symlink_inside_a_root_pointing_out_is_refused(service, note, inbox, tmp_path):
    (tmp_path / "outside.png").write_bytes(png_bytes())
    link = inbox / "looks-local.png"
    link.symlink_to(tmp_path / "outside.png")
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path=str(link))
    assert excinfo.value.code == "ATTACHMENT_SOURCE_NOT_ALLOWED"


def test_a_relative_source_path_is_refused(service, note):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path="inbox/a.png")
    assert excinfo.value.code == "ATTACHMENT_SOURCE_NOT_ABSOLUTE"


def test_a_missing_source_path_is_a_clean_error(service, note, inbox):
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path=str(inbox / "nope.png"))
    assert excinfo.value.code == "ATTACHMENT_SOURCE_NOT_FOUND"


def test_a_directory_is_not_a_file(service, note, inbox):
    (inbox / "adir").mkdir()
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path=str(inbox / "adir"))
    assert excinfo.value.code in ("ATTACHMENT_SOURCE_NOT_A_FILE", "ATTACHMENT_UNREADABLE")


def test_an_oversized_source_file_is_refused_without_reading_it(service, note, inbox):
    (inbox / "big.png").write_bytes(png_bytes(400, 400))
    service.max_attachment_bytes = 32
    with pytest.raises(NotesError) as excinfo:
        service.add_attachment(note["id"], source_path=str(inbox / "big.png"))
    assert excinfo.value.code == "ATTACHMENT_TOO_LARGE"


# -- serving confinement --------------------------------------------------

def test_a_tampered_row_cannot_turn_serving_into_an_arbitrary_file_read(service, note):
    record = service.add_attachment(note["id"], data=png_bytes())
    with service.store._connection() as connection:  # noqa: SLF001 - simulating tampering
        connection.execute("UPDATE note_attachments SET storage_path = ? WHERE id = ?",
                           ("/etc/hostname", record["id"]))
    with pytest.raises(NotesError) as excinfo:
        service.open_attachment(record["id"])
    assert excinfo.value.code == "ATTACHMENT_PATH_OUTSIDE_STORE"


def test_a_tampered_row_cannot_make_remove_unlink_an_arbitrary_file(service, note, tmp_path):
    victim = tmp_path / "precious.txt"
    victim.write_text("do not delete me")
    record = service.add_attachment(note["id"], data=png_bytes())
    with service.store._connection() as connection:  # noqa: SLF001 - simulating tampering
        connection.execute("UPDATE note_attachments SET storage_path = ? WHERE id = ?",
                           (str(victim), record["id"]))
    assert service.remove_attachment(record["id"])["file_removed"] is False
    assert victim.exists()


def test_a_file_that_vanished_from_storage_is_reported_not_crashed(service, note, tmp_path):
    record = service.add_attachment(note["id"], data=png_bytes())
    next((tmp_path / "attachments").rglob("*.png")).unlink()
    with pytest.raises(NotesError) as excinfo:
        service.open_attachment(record["id"])
    assert excinfo.value.code == "ATTACHMENT_FILE_MISSING"


# -- atomicity / layout ---------------------------------------------------

def test_no_temp_file_survives_a_successful_write(service, note, tmp_path):
    service.add_attachment(note["id"], data=png_bytes())
    leftovers = [p for p in (tmp_path / "attachments").rglob(".tmp-*")]
    assert leftovers == []


def test_a_failed_metadata_write_rolls_the_file_back(service, note, tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise sqlite_error()

    def sqlite_error():
        import sqlite3
        return sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service.store, "add_attachment", explode)
    with pytest.raises(Exception):
        service.add_attachment(note["id"], data=png_bytes())
    assert not list((tmp_path / "attachments").rglob("*.png"))


def test_default_attachments_dir_follows_the_notes_db(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_NOTES_ATTACHMENTS_DIR", raising=False)
    monkeypatch.delenv("TERMINAL_MCP_NOTES_DB", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_attachments_dir() == tmp_path / "state" / "terminal-mcp" / "notes_attachments"
    monkeypatch.setenv("TERMINAL_MCP_NOTES_ATTACHMENTS_DIR", str(tmp_path / "elsewhere"))
    assert default_attachments_dir() == tmp_path / "elsewhere"


def test_the_default_allowlist_is_the_previewable_image_set():
    assert set(DEFAULT_ALLOWED_MIME_TYPES) == {"image/png", "image/jpeg", "image/webp", "image/gif"}


def test_facets_report_the_storage_limits(service):
    facets = service.facets()
    assert facets["max_attachment_bytes"] == service.max_attachment_bytes
    assert facets["allowed_mime_types"] == list(DEFAULT_ALLOWED_MIME_TYPES)
    assert facets["attachments_dir"] == str(service.attachments_dir)


def test_data_survives_a_fresh_service_over_the_same_files(tmp_path, inbox):
    first = NotesService(NotesStore(tmp_path / "notes.db"),
                         attachments_dir=tmp_path / "attachments",
                         attachment_source_roots=(str(inbox),))
    note = first.create(title="Bền vững", summary="phải còn sau restart")
    record = first.add_attachment(note["id"], filename="a.png", data=png_bytes(70, 30))
    # A whole new process would do exactly this: reopen the same paths.
    second = NotesService(NotesStore(tmp_path / "notes.db"),
                          attachments_dir=tmp_path / "attachments")
    reloaded = second.get(note["id"])
    assert reloaded["title"] == "Bền vững"
    assert reloaded["attachments"][0]["id"] == record["id"]
    assert second.open_attachment(record["id"])[1] == png_bytes(70, 30)
    assert second.search("bền vững")["total"] == 1
