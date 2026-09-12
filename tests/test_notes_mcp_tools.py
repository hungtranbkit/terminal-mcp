"""The notes store through the REAL MCP surface -- the exact path ChatGPT
takes when the user says "lưu lại". Contract tests: the tools exist, their
schemas teach the workflow, and the documented end-to-end flow (create ->
attach -> search -> open -> mark applied) actually works.
"""
from __future__ import annotations

import base64
import json

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, NotesConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.notes_service import NotesService
from terminal_mcp.notes_store import NotesStore
from tests.fixtures.notes_images import NOT_AN_IMAGE, jpeg_bytes, png_bytes

REQUIRED_TOOLS = ("note_create", "note_get", "note_search", "note_list", "note_update",
                  "note_delete", "note_add_attachment", "note_link_to_project",
                  "note_mark_applied")


def make_config(notes: NotesConfig | None = None) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("*",)),
        notes=notes or NotesConfig(),
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def rig(tmp_path):
    notes = NotesService(NotesStore(tmp_path / "notes.db"),
                         attachments_dir=tmp_path / "attachments",
                         attachment_source_roots=(str(tmp_path / "inbox"),))
    (tmp_path / "inbox").mkdir()
    server = build_mcp(TerminalService(make_config()), notes=notes)
    return server, notes, tmp_path


async def call(server, tool, **kwargs):
    result = await server.call_tool(tool, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


# -- contract -------------------------------------------------------------

@pytest.mark.anyio
async def test_every_required_tool_is_registered(rig):
    server, _, _ = rig
    names = {tool.name for tool in await server.list_tools()}
    for tool in REQUIRED_TOOLS:
        assert tool in names, tool
    # The extras the UI and recovery need are part of the surface too.
    for tool in ("note_restore", "note_remove_attachment", "note_facets"):
        assert tool in names, tool


@pytest.mark.anyio
async def test_the_notes_tools_default_on_with_a_bare_build(tmp_path, monkeypatch):
    """server.py calls build_mcp() bare -- the stdio surface must expose the
    same note_* tools the HTTP one does, which is what the backlog/events
    defaulting comment in build_mcp is about."""
    monkeypatch.setenv("TERMINAL_MCP_NOTES_DB", str(tmp_path / "notes.db"))
    monkeypatch.setenv("TERMINAL_MCP_NOTES_ATTACHMENTS_DIR", str(tmp_path / "att"))
    server = build_mcp(TerminalService(make_config()))
    names = {tool.name for tool in await server.list_tools()}
    assert set(REQUIRED_TOOLS) <= names


@pytest.mark.anyio
async def test_the_tools_are_absent_when_notes_are_disabled(tmp_path):
    server = build_mcp(TerminalService(make_config(NotesConfig(enabled=False))))
    names = {tool.name for tool in await server.list_tools()}
    assert not (set(REQUIRED_TOOLS) & names)


@pytest.mark.anyio
async def test_tool_descriptions_teach_the_workflow(rig):
    """A model must be able to learn this feature from the schema alone."""
    server, _, _ = rig
    tools = {tool.name: tool for tool in await server.list_tools()}
    create = tools["note_create"].description
    assert "lưu lại" in create
    assert "original_content" in create and "analysis" in create
    # The honest transport story has to be in the schema, not only the docs.
    attach = tools["note_add_attachment"].description
    assert "data_base64" in attach and "source_path" in attach
    search = tools["note_search"].description
    assert "rank_position" in search
    assert "FTS5" in search or "bm25" in search or "local" in search


@pytest.mark.anyio
async def test_note_create_schema_exposes_every_documented_field(rig):
    server, _, _ = rig
    tools = {tool.name: tool for tool in await server.list_tools()}
    properties = set(tools["note_create"].input_schema["properties"])
    assert {"title", "summary", "original_content", "analysis", "source_url", "source_chat",
            "source_session", "type", "status", "tags", "project_id", "project_name",
            "attachment_paths", "attachments_base64"} <= properties


# -- the documented workflow ---------------------------------------------

@pytest.mark.anyio
async def test_the_full_chatgpt_workflow(rig):
    server, _, _ = rig
    note = await call(
        server, "note_create",
        title="Landing page MESFlow", original_content="user dán link + mô tả ở đây",
        analysis="Nên dùng hero + social proof, CTA ở đầu trang",
        source_url="https://example.com/inspiration", source_chat="chatgpt:thread-42",
        tags=["mesflow", "landing"], type="idea",
        attachments_base64=[{"filename": "shot.png",
                             "data_base64": base64.b64encode(png_bytes(320, 180)).decode()}])
    assert note["id"].startswith("note_")
    assert note["attachment_count"] == 1
    assert note["attachment_results"][0]["width"] == 320

    found = await call(server, "note_search", query="landing page MESFlow")
    assert found["total"] == 1
    hit = found["items"][0]
    assert hit["id"] == note["id"]
    assert hit["rank_position"] == 1 and hit["excerpt"]
    assert hit["attachments"][0]["url"].startswith("/dashboard/api/notes/attachment?id=")

    detail = await call(server, "note_get", note_id=note["id"])
    assert detail["analysis"].startswith("Nên dùng hero")

    linked = await call(server, "note_link_to_project", note_id=note["id"], project_name="MESFlow")
    assert linked["project_name"] == "MESFlow"

    applied = await call(server, "note_mark_applied", note_id=note["id"],
                         applied_ref="commit 8f54fc7")
    assert applied["status"] == "applied" and applied["applied_at"]
    assert applied["applied_ref"] == "commit 8f54fc7"


@pytest.mark.anyio
async def test_note_create_without_a_title_still_works(rig):
    server, _, _ = rig
    note = await call(server, "note_create", original_content="chỉ có nội dung, không tiêu đề")
    assert note["title"]


@pytest.mark.anyio
async def test_note_list_filters_and_paginates(rig):
    server, _, _ = rig
    for index in range(4):
        await call(server, "note_create", title=f"Ghi chú {index}", type="research",
                   tags=["batch"], project_name="MESFlow")
    await call(server, "note_create", title="Khác", type="idea", tags=["solo"])
    assert (await call(server, "note_list", type="research"))["total"] == 4
    assert (await call(server, "note_list", tag="solo"))["total"] == 1
    assert (await call(server, "note_list", project="mesflow"))["total"] == 4
    page = await call(server, "note_list", limit=2, offset=0)
    assert len(page["items"]) == 2 and page["has_more"] is True
    assert (await call(server, "note_list", sort="oldest"))["items"][0]["title"] == "Ghi chú 0"


@pytest.mark.anyio
async def test_note_update_is_partial(rig):
    server, _, _ = rig
    note = await call(server, "note_create", title="T", summary="S", analysis="A")
    updated = await call(server, "note_update", note_id=note["id"], status="reviewing")
    assert updated["status"] == "reviewing"
    assert updated["summary"] == "S" and updated["analysis"] == "A"


@pytest.mark.anyio
async def test_note_update_with_nothing_to_change_says_so(rig):
    server, _, _ = rig
    note = await call(server, "note_create", title="T")
    assert (await call(server, "note_update", note_id=note["id"]))["error"] == "NOTHING_TO_UPDATE"


@pytest.mark.anyio
async def test_delete_is_soft_and_restorable(rig):
    server, _, _ = rig
    note = await call(server, "note_create", title="Tạm xoá", summary="zzsoftdelete")
    assert (await call(server, "note_delete", note_id=note["id"]))["hard"] is False
    assert (await call(server, "note_list"))["total"] == 0
    assert (await call(server, "note_search", query="zzsoftdelete"))["total"] == 0
    assert (await call(server, "note_get", note_id=note["id"]))["error"] == "NOTE_DELETED"
    assert (await call(server, "note_restore", note_id=note["id"]))["id"] == note["id"]
    assert (await call(server, "note_list"))["total"] == 1


@pytest.mark.anyio
async def test_hard_delete_removes_the_files_too(rig):
    server, _, tmp_path = rig
    note = await call(server, "note_create", title="Xoá hẳn",
                      attachments_base64=[{"data_base64": base64.b64encode(png_bytes()).decode()}])
    result = await call(server, "note_delete", note_id=note["id"], hard=True)
    assert result["hard"] is True and result["files_removed"] == 1
    assert not list((tmp_path / "attachments").rglob("*.png"))


# -- attachments through the tool surface ---------------------------------

@pytest.mark.anyio
async def test_add_attachment_by_base64_and_remove_it(rig):
    server, _, _ = rig
    note = await call(server, "note_create", title="Ảnh")
    record = await call(server, "note_add_attachment", note_id=note["id"], filename="a.jpg",
                        data_base64=base64.b64encode(jpeg_bytes(300, 200)).decode())
    assert record["mime_type"] == "image/jpeg" and record["width"] == 300
    assert (await call(server, "note_get", note_id=note["id"]))["attachment_count"] == 1
    assert (await call(server, "note_remove_attachment", attachment_id=record["id"]))["ok"] is True
    assert (await call(server, "note_get", note_id=note["id"]))["attachment_count"] == 0


@pytest.mark.anyio
async def test_add_attachment_by_source_path_inside_an_allowed_root(rig):
    server, _, tmp_path = rig
    (tmp_path / "inbox" / "shot.png").write_bytes(png_bytes(90, 60))
    note = await call(server, "note_create", title="Từ đĩa")
    record = await call(server, "note_add_attachment", note_id=note["id"],
                        source_path=str(tmp_path / "inbox" / "shot.png"))
    assert record["filename"] == "shot.png" and record["height"] == 60


@pytest.mark.anyio
async def test_note_create_reports_per_attachment_failures_without_losing_the_note(rig):
    server, _, _ = rig
    note = await call(server, "note_create", title="Một ảnh tốt một ảnh xấu",
                      attachments_base64=[
                          {"filename": "ok.png", "data_base64": base64.b64encode(png_bytes()).decode()},
                          {"filename": "bad.png", "data_base64": base64.b64encode(NOT_AN_IMAGE).decode()}])
    assert note["id"]
    assert note["attachment_count"] == 1
    codes = [item.get("error") for item in note["attachment_results"]]
    assert codes == [None, "ATTACHMENT_MIME_NOT_ALLOWED"]


@pytest.mark.anyio
async def test_source_path_outside_the_allowed_roots_is_refused(rig):
    server, _, _ = rig
    note = await call(server, "note_create", title="T")
    result = await call(server, "note_add_attachment", note_id=note["id"],
                        source_path="/etc/hostname")
    assert result["error"] in ("ATTACHMENT_SOURCE_NOT_ALLOWED", "ATTACHMENT_SOURCE_NOT_FOUND")


# -- errors are dicts, never exceptions -----------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("tool,kwargs,code", [
    ("note_get", {"note_id": "note_nope"}, "NOTE_NOT_FOUND"),
    ("note_create", {"title": "x", "type": "bogus"}, "INVALID_TYPE"),
    ("note_create", {"title": "x", "status": "bogus"}, "INVALID_STATUS"),
    ("note_create", {}, "EMPTY_NOTE"),
    ("note_list", {"sort": "sideways"}, "INVALID_SORT"),
    ("note_delete", {"note_id": "note_nope"}, "NOTE_NOT_FOUND"),
    ("note_mark_applied", {"note_id": "note_nope"}, "NOTE_NOT_FOUND"),
    ("note_remove_attachment", {"attachment_id": "att_nope"}, "ATTACHMENT_NOT_FOUND"),
    ("note_link_to_project", {"note_id": "note_nope", "project_name": "x"}, "NOTE_NOT_FOUND"),
])
async def test_errors_come_back_as_a_documented_code(rig, tool, kwargs, code):
    server, _, _ = rig
    result = await call(server, tool, **kwargs)
    assert result["error"] == code
    assert result["message"]


@pytest.mark.anyio
async def test_search_is_fully_local_and_deterministic(rig):
    """Acceptance criterion 1: no external AI service. The same query must
    return the same ranking twice, from SQLite alone."""
    server, _, _ = rig
    await call(server, "note_create", title="Ý tưởng landing page", summary="hero")
    await call(server, "note_create", title="Ghi chú khác", original_content="landing")
    first = await call(server, "note_search", query="landing")
    second = await call(server, "note_search", query="landing")
    assert [i["id"] for i in first["items"]] == [i["id"] for i in second["items"]]
    assert first["ranked"] is True


@pytest.mark.anyio
async def test_facets_expose_real_filter_values(rig):
    server, _, _ = rig
    await call(server, "note_create", title="A", tags=["mesflow"], project_name="MESFlow")
    facets = await call(server, "note_facets")
    assert facets["types"]["idea"] == 1
    assert [entry["tag"] for entry in facets["tags"]] == ["mesflow"]
    assert facets["projects"][0]["project_name"] == "MESFlow"
