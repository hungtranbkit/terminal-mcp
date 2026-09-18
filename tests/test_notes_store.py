"""Notes/Ideas store: schema, CRUD, filters, pagination, and real FTS5
ranking. Every assertion runs against a real SQLite file (never a mock) --
the same discipline the rest of this suite uses.
"""
from __future__ import annotations

import sqlite3

import pytest

from terminal_mcp.notes_store import (
    STATUS_APPLIED, STATUS_ARCHIVED, STATUS_NEW, STATUSES, TYPES, NotesError, NotesStore,
    build_excerpt, default_notes_path, fallback_title, fts_query, normalise_tags,
)


@pytest.fixture
def store(tmp_path):
    return NotesStore(tmp_path / "notes.db")


# -- schema / migration ---------------------------------------------------

def test_migration_creates_schema_and_stamps_user_version(tmp_path):
    path = tmp_path / "notes.db"
    NotesStore(path)
    connection = sqlite3.connect(path)
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"notes", "note_attachments"} <= tables
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migration_is_idempotent_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "notes.db"
    first = NotesStore(path)
    note = first.create(title="Kept")
    second = NotesStore(path)
    assert second.get(note["id"])["title"] == "Kept"
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 1
    # A third open must still not disturb anything.
    assert NotesStore(path).list()["total"] == 1


def test_default_path_honours_the_env_override_then_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_NOTES_DB", str(tmp_path / "explicit.db"))
    assert default_notes_path() == tmp_path / "explicit.db"
    monkeypatch.delenv("TERMINAL_MCP_NOTES_DB")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_notes_path() == tmp_path / "state" / "terminal-mcp" / "notes.db"


def test_the_db_file_is_not_world_readable(tmp_path):
    store = NotesStore(tmp_path / "notes.db")
    assert store.path.stat().st_mode & 0o077 == 0


# -- create / validation --------------------------------------------------

def test_create_returns_every_field_it_was_given(store):
    note = store.create(
        title="Landing page mới", summary="Tóm tắt", original_content="Nội dung gốc",
        analysis="Phân tích của ChatGPT", source_url="https://example.com/a",
        source_chat="chatgpt-thread-1", source_session="claude-x", type="design",
        status="reviewing", tags=["mesflow", "landing"], project_name="MESFlow",
        project_id="git:github.com/x/mesflow")
    assert note["title"] == "Landing page mới"
    assert note["analysis"] == "Phân tích của ChatGPT"
    assert note["type"] == "design" and note["status"] == "reviewing"
    assert note["tags"] == ["mesflow", "landing"]
    assert note["project_name"] == "MESFlow"
    assert note["source_chat"] == "chatgpt-thread-1"
    assert note["created_at"] and note["updated_at"]
    assert note["attachments"] == [] and note["attachment_count"] == 0
    assert note["id"].startswith("note_")


def test_create_derives_a_title_when_none_is_given(store):
    note = store.create(original_content="x" * 300)
    assert note["title"].endswith("…")
    assert len(note["title"]) <= 81


def test_create_refuses_a_completely_empty_note(store):
    with pytest.raises(NotesError) as excinfo:
        store.create()
    assert excinfo.value.code == "EMPTY_NOTE"


@pytest.mark.parametrize("field,value,code", [
    ("type", "bogus", "INVALID_TYPE"),
    ("status", "bogus", "INVALID_STATUS"),
])
def test_create_rejects_unknown_enum_values(store, field, value, code):
    with pytest.raises(NotesError) as excinfo:
        store.create(title="x", **{field: value})
    assert excinfo.value.code == code
    # The error must teach the caller what IS allowed.
    assert excinfo.value.detail["allowed"]


def test_defaults_are_idea_and_new(store):
    note = store.create(title="x")
    assert note["type"] == "idea" and note["status"] == STATUS_NEW


def test_creating_with_applied_status_stamps_applied_at(store):
    assert store.create(title="x", status="applied")["applied_at"]


# -- tags -----------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    (None, []),
    ("", []),
    ("a, b ,, c", ["a", "b", "c"]),
    (["a", "A", " a "], ["a"]),
    (["MESFlow", "mesflow"], ["MESFlow"]),
    (("x", "y"), ["x", "y"]),
])
def test_tags_normalise(given, expected):
    assert normalise_tags(given) == expected


def test_tags_reject_a_nonsense_type():
    with pytest.raises(NotesError) as excinfo:
        normalise_tags(42)
    assert excinfo.value.code == "INVALID_TAGS"


# -- update ---------------------------------------------------------------

def test_update_is_partial_and_leaves_unnamed_fields_alone(store):
    note = store.create(title="T", summary="S", analysis="A", tags=["a"])
    updated = store.update(note["id"], status="planned")
    assert updated["status"] == "planned"
    assert updated["summary"] == "S" and updated["analysis"] == "A"
    assert updated["tags"] == ["a"]
    assert updated["updated_at"] >= note["updated_at"]


def test_update_replaces_the_whole_tag_list(store):
    note = store.create(title="T", tags=["a", "b"])
    assert store.update(note["id"], tags=["c"])["tags"] == ["c"]


def test_update_to_applied_stamps_applied_at_and_leaving_clears_it(store):
    note = store.create(title="T")
    applied = store.update(note["id"], status=STATUS_APPLIED)
    assert applied["applied_at"]
    back = store.update(note["id"], status="planned")
    assert back["applied_at"] is None


def test_update_with_a_blank_title_rederives_the_fallback(store):
    note = store.create(title="Original", summary="Một tóm tắt đủ dài để làm tiêu đề")
    assert store.update(note["id"], title="")["title"].startswith("Một tóm tắt")


def test_update_of_a_missing_note_is_a_clean_error(store):
    with pytest.raises(NotesError) as excinfo:
        store.update("note_nope", status="planned")
    assert excinfo.value.code == "NOTE_NOT_FOUND"


def test_mark_applied_records_the_reference_and_project(store):
    note = store.create(title="T")
    applied = store.mark_applied(note["id"], applied_ref="commit abc123", project_name="MESFlow")
    assert applied["status"] == STATUS_APPLIED
    assert applied["applied_ref"] == "commit abc123"
    assert applied["project_name"] == "MESFlow"
    assert applied["applied_at"]


def test_link_to_project_changes_only_the_link(store):
    note = store.create(title="T", summary="S", tags=["a"])
    linked = store.link_to_project(note["id"], project_id="git:x/y", project_name="Y")
    assert (linked["project_id"], linked["project_name"]) == ("git:x/y", "Y")
    assert linked["summary"] == "S" and linked["tags"] == ["a"] and linked["title"] == "T"


def test_link_to_project_needs_something_to_link(store):
    note = store.create(title="T")
    with pytest.raises(NotesError) as excinfo:
        store.link_to_project(note["id"])
    assert excinfo.value.code == "PROJECT_REQUIRED"


# -- delete / restore -----------------------------------------------------

def test_soft_delete_hides_the_note_but_keeps_it_recoverable(store):
    note = store.create(title="Sẽ xoá", summary="unique-soft-delete-token")
    store.soft_delete(note["id"])
    assert store.list()["total"] == 0
    assert store.search("unique-soft-delete-token")["total"] == 0
    with pytest.raises(NotesError) as excinfo:
        store.get(note["id"])
    assert excinfo.value.code == "NOTE_DELETED"
    assert store.get(note["id"], include_deleted=True)["deleted_at"]
    restored = store.restore(note["id"])
    assert restored["deleted_at"] is None
    assert store.search("unique-soft-delete-token")["total"] == 1


def test_hard_delete_removes_the_row_and_reports_its_attachments(store):
    note = store.create(title="Sẽ xoá hẳn")
    store.add_attachment(note["id"], filename="a.png", mime_type="image/png", size=3,
                         sha256="deadbeef", storage_path="/tmp/whatever/a.png")
    orphans = store.hard_delete(note["id"])
    assert [record.filename for record in orphans] == ["a.png"]
    with pytest.raises(NotesError) as excinfo:
        store.get(note["id"], include_deleted=True)
    assert excinfo.value.code == "NOTE_NOT_FOUND"


def test_a_deleted_note_refuses_new_attachments(store):
    note = store.create(title="T")
    store.soft_delete(note["id"])
    with pytest.raises(NotesError) as excinfo:
        store.add_attachment(note["id"], filename="a.png", mime_type="image/png", size=1,
                             sha256="x", storage_path="/tmp/a.png")
    assert excinfo.value.code == "NOTE_DELETED"


# -- list / filter / pagination -------------------------------------------

@pytest.fixture
def seeded(store):
    store.create(title="Alpha idea", type="idea", status="new", tags=["ui"],
                 project_name="MESFlow", summary="landing page hero")
    store.create(title="Beta reference", type="reference", status="planned", tags=["ui", "docs"],
                 project_name="SubsVid", summary="colour tokens")
    store.create(title="Gamma research", type="research", status="applied", tags=["perf"],
                 project_id="git:github.com/x/mesflow", summary="index tuning")
    store.create(title="Delta archived", type="idea", status=STATUS_ARCHIVED, tags=["ui"],
                 summary="old plan")
    return store


def test_list_hides_archived_by_default_but_can_show_it(seeded):
    assert seeded.list()["total"] == 3
    assert seeded.list(include_archived=True)["total"] == 4
    # Asking for archived BY NAME always works, even with the flag off.
    assert seeded.list(status=STATUS_ARCHIVED)["total"] == 1


@pytest.mark.parametrize("filters,expected", [
    ({"type": "idea"}, 1),
    ({"status": "planned"}, 1),
    ({"tag": "ui"}, 2),
    ({"tag": "UI"}, 2),
    ({"tag": "u"}, 0),
    ({"project": "mesflow"}, 2),
    ({"project": "subsvid"}, 1),
    ({"type": "idea", "tag": "ui"}, 1),
])
def test_list_filters(seeded, filters, expected):
    assert seeded.list(**filters)["total"] == expected


def test_list_paginates_with_a_stable_total(seeded):
    first = seeded.list(limit=2, offset=0)
    second = seeded.list(limit=2, offset=2)
    assert first["total"] == second["total"] == 3
    assert first["has_more"] is True and second["has_more"] is False
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    ids = {item["id"] for item in first["items"]} | {item["id"] for item in second["items"]}
    assert len(ids) == 3


def test_list_sort_orders(seeded):
    newest = seeded.list(sort="newest")["items"]
    oldest = seeded.list(sort="oldest")["items"]
    assert [n["id"] for n in newest] == [n["id"] for n in reversed(oldest)]


def test_list_rejects_an_unknown_sort(seeded):
    with pytest.raises(NotesError) as excinfo:
        seeded.list(sort="sideways")
    assert excinfo.value.code == "INVALID_SORT"


def test_list_limit_is_capped_not_rejected(seeded):
    assert seeded.list(limit=10_000)["limit"] == 200


def test_date_filters_use_created_at(seeded):
    everything = seeded.list()
    cutoff = everything["items"][0]["created_at"]
    assert seeded.list(since=cutoff)["total"] >= 1
    assert seeded.list(until="1999-01-01T00:00:00+00:00")["total"] == 0


# -- search ---------------------------------------------------------------

def test_search_spans_every_text_field(store):
    title = store.create(title="zzunique in the title")
    summary = store.create(title="a", summary="zzunique in the summary")
    original = store.create(title="b", original_content="zzunique in the original")
    analysis = store.create(title="c", analysis="zzunique in the analysis")
    tagged = store.create(title="d", tags=["zzunique"])
    found = {item["id"] for item in store.search("zzunique", limit=50)["items"]}
    assert found == {title["id"], summary["id"], original["id"], analysis["id"], tagged["id"]}


def test_search_ranks_a_title_match_above_a_body_mention(store):
    store.create(title="Random note",
                 original_content="a passing mention of the landing page idea, buried in prose")
    winner = store.create(title="Landing page MESFlow", summary="hero redesign")
    result = store.search("landing page")
    assert result["ranked"] is True
    assert result["items"][0]["id"] == winner["id"]
    assert result["items"][0]["rank_position"] == 1
    assert result["items"][1]["rank_position"] == 2


def test_search_results_carry_an_excerpt(store):
    store.create(title="Note", analysis="Chúng ta nên dùng bm25 để xếp hạng kết quả tìm kiếm")
    hit = store.search("bm25")["items"][0]
    assert "bm25" in hit["excerpt"]


def test_search_is_diacritics_insensitive(store):
    note = store.create(title="Ý tưởng landing", summary="mô tả")
    assert store.search("y tuong")["items"][0]["id"] == note["id"]
    assert store.search("Ý TƯỞNG")["items"][0]["id"] == note["id"]


def test_search_includes_archived_by_default(store):
    store.create(title="zzarchived idea", status=STATUS_ARCHIVED)
    assert store.search("zzarchived")["total"] == 1
    assert store.search("zzarchived", include_archived=False)["total"] == 0


def test_search_combines_the_query_with_filters(seeded):
    assert seeded.search("landing", project="MESFlow")["total"] == 1
    assert seeded.search("landing", project="SubsVid")["total"] == 0
    assert seeded.search("page", type="reference")["total"] == 0


def test_search_paginates(store):
    for index in range(5):
        store.create(title=f"paged idea {index}", summary="zzpaged")
    first = store.search("zzpaged", limit=2, offset=0)
    second = store.search("zzpaged", limit=2, offset=2)
    assert first["total"] == 5 and first["has_more"] is True
    assert second["items"][0]["rank_position"] == 3
    assert not ({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})


def test_an_empty_query_degrades_to_a_browse(seeded):
    result = seeded.search("   ")
    assert result["ranked"] is False and result["query"] == ""
    assert result["total"] == seeded.list(include_archived=True)["total"]


@pytest.mark.parametrize("hostile", ['landing"', "a AND b", "NEAR(x y)", "col:value", "*", "^x", ")("])
def test_search_never_lets_fts5_syntax_through(store, hostile):
    store.create(title="a plain note")
    # Must not raise, and must not be interpreted as FTS5 query syntax.
    assert store.search(hostile)["total"] >= 0


def test_fts_query_quotes_every_term():
    assert fts_query('landing "page"') == '"landing" """page"""'
    assert fts_query("") == '""'


def test_search_falls_back_to_like_when_fts_is_unavailable(store, monkeypatch):
    note = store.create(title="Landing page fallback", summary="hero")
    monkeypatch.setattr(store, "_fts_available", False)
    result = store.search("landing")
    assert result["ranked"] is False
    assert [item["id"] for item in result["items"]] == [note["id"]]
    assert result["items"][0]["score"] is None
    # The excerpt still works on this path -- it is built from the row, not
    # from FTS5's snippet().
    assert result["items"][0]["excerpt"]


def test_reindex_all_rebuilds_the_index_from_the_table(store):
    note = store.create(title="Rebuildable", summary="zzreindex")
    with store._connection() as connection:  # noqa: SLF001 - repairing the index is the point
        connection.execute("DELETE FROM notes_fts")
    assert store.search("zzreindex")["total"] == 0
    assert store.reindex_all() == 1
    assert store.search("zzreindex")["items"][0]["id"] == note["id"]


# -- facets ---------------------------------------------------------------

def test_facets_report_what_actually_exists(seeded):
    facets = seeded.facets()
    assert facets["types"]["idea"] == 2
    assert facets["statuses"]["planned"] == 1
    assert {entry["tag"] for entry in facets["tags"]} == {"ui", "docs", "perf"}
    assert dict((entry["tag"], entry["count"]) for entry in facets["tags"])["ui"] == 3
    assert any(entry["project_name"] == "MESFlow" for entry in facets["projects"])
    assert set(facets["all_types"]) == set(TYPES)
    assert set(facets["all_statuses"]) == set(STATUSES)
    assert facets["fts"] is True


def test_facets_count_soft_deleted_notes_separately(store):
    note = store.create(title="T")
    store.soft_delete(note["id"])
    facets = store.facets()
    assert facets["total"] == 0 and facets["deleted"] == 1


# -- small helpers --------------------------------------------------------

def test_fallback_title_uses_the_first_non_empty_source():
    assert fallback_title(None, "", "  ", "https://x") == "https://x"
    assert fallback_title("short") == "short"
    assert fallback_title("y" * 500).endswith("…")
    assert fallback_title() == "Ghi chú không có tiêu đề"


def test_build_excerpt_falls_back_when_no_term_matches_literally():
    row = {"summary": "Ý tưởng về trang đích", "analysis": "", "original_content": "", "title": "x"}
    assert build_excerpt(row, "y tuong").startswith("Ý tưởng")
