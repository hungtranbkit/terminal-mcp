"""The Notes/Ideas web surface: the page renders, its JSON routes work,
and the guards actually hold -- an unauthenticated cross-site caller must
not be able to write, and no request may name a filesystem path.

Uses the real Starlette app (TestClient) over a real SQLite file and a real
attachment directory, the same shape as test_backlog_dashboard.py.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import AppConfig, InputPolicyConfig, NotesConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.notes_service import NotesService
from terminal_mcp.notes_store import NotesStore
from terminal_mcp.webauth import SESSION_COOKIE_NAME, WebAuthStore
from tests.fixtures.notes_images import NOT_AN_IMAGE, jpeg_bytes, png_bytes

# The dashboard's always-on CSRF guard refuses a mutation with no
# same-origin Origin/Referer -- sending it is what the real page's fetch()
# does, so these headers exercise the guard rather than bypass it.
SAME_ORIGIN = {"Origin": "http://testserver"}

NOTES_ROUTES = ("/dashboard/notes", "/dashboard/api/notes", "/dashboard/api/notes/facets",
                "/dashboard/api/notes/note", "/dashboard/api/notes/create",
                "/dashboard/api/notes/update", "/dashboard/api/notes/mark-applied",
                "/dashboard/api/notes/delete", "/dashboard/api/notes/restore",
                "/dashboard/api/notes/attachment", "/dashboard/api/notes/attachment/upload",
                "/dashboard/api/notes/attachment/remove")

MUTATION_ROUTES = ("/dashboard/api/notes/create", "/dashboard/api/notes/update",
                   "/dashboard/api/notes/mark-applied", "/dashboard/api/notes/delete",
                   "/dashboard/api/notes/restore", "/dashboard/api/notes/attachment/remove")


def make_config(notes: NotesConfig | None = None, **dashboard_kwargs) -> AppConfig:
    from terminal_mcp.config import DashboardConfig
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("*",)),
        dashboard=DashboardConfig(**dashboard_kwargs) if dashboard_kwargs else DashboardConfig(),
        notes=notes or NotesConfig(),
    )


def build_rig(tmp_path, *, config: AppConfig | None = None, notes: NotesService | None = None,
              pass_notes: bool = True):
    """Returns (server, notes, webauth). The Notes surface requires an
    application-layer session (notes.require_auth, default on -- see
    tests/test_notes_auth.py for that boundary itself), so every rig here
    carries a real WebAuthStore and the client below logs into it. These
    tests are about the ROUTES' behaviour, so they run authenticated; the
    unauthenticated cases live in test_notes_auth.py."""
    config = config or make_config()
    if notes is None and pass_notes:
        (tmp_path / "inbox").mkdir(exist_ok=True)
        notes = NotesService(NotesStore(tmp_path / "notes.db"),
                             attachments_dir=tmp_path / "attachments",
                             attachment_source_roots=(str(tmp_path / "inbox"),))
    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("operator", "correct horse battery staple")
    terminal = TerminalService(config)
    server = build_mcp(terminal, notes=notes)
    register_dashboard(server, terminal, notes=notes, webauth=webauth)
    return server, notes, webauth


def authenticated_client(server, webauth) -> TestClient:
    client = TestClient(server.streamable_http_app())
    client.cookies.set(SESSION_COOKIE_NAME, webauth.create_session("operator"))
    return client


@pytest.fixture
def rig(tmp_path):
    server, notes, webauth = build_rig(tmp_path)
    return authenticated_client(server, webauth), notes, server


def create(client, **fields):
    response = client.post("/dashboard/api/notes/create", json=fields, headers=SAME_ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()


# -- registration / page --------------------------------------------------

def test_every_notes_route_is_registered(rig):
    _, _, server = rig
    registered = {route.path for route in server._custom_starlette_routes  # noqa: SLF001
                  if hasattr(route, "methods")}
    for route in NOTES_ROUTES:
        assert route in registered, route


def test_the_page_renders_in_vietnamese_and_is_not_framable(rig):
    client, _, _ = rig
    response = client.get("/dashboard/notes")
    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    body = response.text
    assert "<title>Ghi chú / Ý tưởng</title>" in body
    assert "Tìm trong các ý tưởng đã lưu" in body
    # All three view modes, and the board's own Vietnamese lifecycle.
    for label in ("Thư viện", "Danh sách", "Bảng", "Đã áp dụng", "Sẽ làm", "Đang xem"):
        assert label in body, label
    # Mobile: a viewport meta and a real narrow-screen breakpoint.
    assert 'name="viewport"' in body
    assert "@media (max-width:860px)" in body
    # It talks only to its own routes -- no external CDN or font host.
    assert "https://" not in body.split("<script>")[1]


def test_the_css_has_no_invalid_attribute_selectors(rig):
    """Found by real-browser QA, and Firefox-only: `[data-open=1]` is an
    INVALID selector -- an unquoted attribute value must be a CSS identifier,
    and identifiers cannot start with a digit. Chromium parses it leniently;
    Firefox drops the whole rule, so the drawer never slid in, the backdrop
    never appeared and no toast was ever visible. Guarding the whole class
    rather than the three rules that were wrong."""
    import re
    client, _, _ = rig
    css = client.get("/dashboard/notes").text.split("<style>")[1].split("</style>")[0]
    invalid = sorted(set(re.findall(r"\[[a-zA-Z-]+=[0-9][^\"'\]]*\]", css)))
    assert invalid == [], invalid
    # And the rules that drive the drawer/backdrop/toast are present in their
    # quoted, valid form.
    for rule in ('#drawer[data-open="1"]', '#backdrop[data-open="1"]', '#toast[data-open="1"]'):
        assert rule in css, rule


def test_the_page_references_only_its_own_api_routes(rig):
    client, _, _ = rig
    body = client.get("/dashboard/notes").text
    for route in ("/dashboard/api/notes?", "/dashboard/api/notes/facets",
                  "/dashboard/api/notes/note?id=", "/dashboard/api/notes/create",
                  "/dashboard/api/notes/update", "/dashboard/api/notes/delete",
                  "/dashboard/api/notes/mark-applied",
                  "/dashboard/api/notes/attachment/upload",
                  "/dashboard/api/notes/attachment/remove"):
        assert route in body, route


# -- guards ---------------------------------------------------------------

@pytest.mark.parametrize("route", MUTATION_ROUTES)
def test_every_mutation_refuses_a_cross_site_caller(rig, route):
    client, _, _ = rig
    response = client.post(route, json={"id": "note_x"}, headers={"Origin": "http://evil.test"})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


@pytest.mark.parametrize("route", MUTATION_ROUTES)
def test_every_mutation_refuses_a_request_with_no_origin_at_all(rig, route):
    client, _, _ = rig
    assert client.post(route, json={"id": "note_x"}).status_code == 403


def test_uploads_are_behind_the_same_csrf_guard(rig):
    client, _, _ = rig
    response = client.post("/dashboard/api/notes/attachment/upload", data={"note_id": "x"},
                           files={"file": ("a.png", png_bytes(), "image/png")},
                           headers={"Origin": "http://evil.test"})
    assert response.status_code == 403


def test_mutations_disabled_blocks_writes_but_not_reads(tmp_path):
    server, _, webauth = build_rig(tmp_path, config=make_config(mutations_enabled=False))
    client = authenticated_client(server, webauth)
    assert client.get("/dashboard/notes").status_code == 200
    assert client.get("/dashboard/api/notes").status_code == 200
    response = client.post("/dashboard/api/notes/create", json={"title": "x"}, headers=SAME_ORIGIN)
    assert response.status_code == 403
    assert response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


def test_routes_answer_503_when_notes_are_disabled(tmp_path):
    server, _, webauth = build_rig(tmp_path, config=make_config(NotesConfig(enabled=False)),
                                   pass_notes=False)
    client = authenticated_client(server, webauth)
    registered = {route.path for route in server._custom_starlette_routes  # noqa: SLF001
                  if hasattr(route, "methods")}
    # Registered but unavailable -- never a 404 that looks like the feature
    # was never built.
    assert "/dashboard/api/notes" in registered
    for response in (client.get("/dashboard/notes"),
                     client.get("/dashboard/api/notes"),
                     client.post("/dashboard/api/notes/create", json={"title": "x"},
                                 headers=SAME_ORIGIN)):
        assert response.status_code == 503
        assert response.json()["error"] == "NOTES_DISABLED"


# -- CRUD over HTTP -------------------------------------------------------

def test_create_read_update_delete_restore(rig):
    client, _, _ = rig
    note = create(client, title="Landing MESFlow", summary="Ý tưởng hero",
                  analysis="Proof + CTA", tags=["mesflow", "landing"])
    assert note["tags"] == ["mesflow", "landing"]

    detail = client.get("/dashboard/api/notes/note?id=" + note["id"])
    assert detail.status_code == 200 and detail.json()["analysis"] == "Proof + CTA"

    updated = client.post("/dashboard/api/notes/update",
                          json={"id": note["id"], "status": "planned"}, headers=SAME_ORIGIN)
    assert updated.json()["status"] == "planned"
    assert updated.json()["analysis"] == "Proof + CTA"

    applied = client.post("/dashboard/api/notes/mark-applied",
                          json={"id": note["id"], "applied_ref": "commit abc"},
                          headers=SAME_ORIGIN)
    assert applied.json()["status"] == "applied" and applied.json()["applied_at"]

    assert client.post("/dashboard/api/notes/delete", json={"id": note["id"]},
                       headers=SAME_ORIGIN).status_code == 200
    assert client.get("/dashboard/api/notes").json()["total"] == 0
    assert client.get("/dashboard/api/notes/note?id=" + note["id"]).status_code == 410
    assert client.post("/dashboard/api/notes/restore", json={"id": note["id"]},
                       headers=SAME_ORIGIN).status_code == 200
    assert client.get("/dashboard/api/notes").json()["total"] == 1


def test_create_ignores_fields_it_does_not_own(rig):
    client, _, _ = rig
    response = client.post("/dashboard/api/notes/create",
                           json={"title": "T", "id": "note_i_chose_this",
                                 "deleted_at": "2020-01-01", "applied_at": "2020-01-01"},
                           headers=SAME_ORIGIN)
    note = response.json()
    assert note["id"] != "note_i_chose_this"
    assert note["deleted_at"] is None and note["applied_at"] is None


@pytest.mark.parametrize("route", ["/dashboard/api/notes/update", "/dashboard/api/notes/delete",
                                   "/dashboard/api/notes/restore",
                                   "/dashboard/api/notes/mark-applied",
                                   "/dashboard/api/notes/attachment/remove"])
def test_a_missing_id_is_a_400_not_a_crash(rig, route):
    client, _, _ = rig
    response = client.post(route, json={}, headers=SAME_ORIGIN)
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"


def test_a_malformed_json_body_is_a_clean_400(rig):
    client, _, _ = rig
    response = client.post("/dashboard/api/notes/update", content=b"{not json",
                           headers={**SAME_ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 400


def test_validation_errors_map_to_useful_statuses(rig):
    client, _, _ = rig
    assert client.post("/dashboard/api/notes/create", json={"title": "x", "type": "bogus"},
                       headers=SAME_ORIGIN).status_code == 400
    assert client.get("/dashboard/api/notes/note?id=note_nope").status_code == 404
    assert client.get("/dashboard/api/notes/note").status_code == 400
    assert client.get("/dashboard/api/notes?sort=sideways").status_code == 400


# -- list / search / filters over HTTP ------------------------------------

@pytest.fixture
def seeded(rig):
    client, _, _ = rig
    create(client, title="Alpha idea", summary="landing page hero", type="idea",
           tags=["ui"], project_name="MESFlow")
    create(client, title="Beta reference", summary="colour tokens", type="reference",
           status="planned", tags=["ui", "docs"], project_name="SubsVid")
    create(client, title="Gamma archived", summary="old plan", type="idea", status="archived",
           tags=["ui"])
    return client


@pytest.mark.parametrize("query,expected", [
    ("", 2),
    ("?include_archived=1", 3),
    ("?status=archived", 1),
    ("?type=reference", 1),
    ("?tag=ui", 2),
    ("?tag=docs", 1),
    ("?project=mesflow", 1),
    ("?type=idea&tag=ui", 1),
    ("?until=1999-01-01T00:00:00Z", 0),
])
def test_list_filters_over_http(seeded, query, expected):
    assert seeded.get("/dashboard/api/notes" + query).json()["total"] == expected


def test_search_over_http_is_ranked_and_finds_archived_notes(seeded):
    body = seeded.get("/dashboard/api/notes?q=landing page").json()
    assert body["ranked"] is True and body["total"] == 1
    assert body["items"][0]["rank_position"] == 1 and body["items"][0]["excerpt"]
    # An archived note is still recallable by search (it would be invisible
    # in the default browse above).
    assert seeded.get("/dashboard/api/notes?q=old plan").json()["total"] == 1


def test_search_matches_analysis_text(rig):
    client, _, _ = rig
    create(client, title="Nothing in the title", analysis="dùng bm25 để xếp hạng")
    assert client.get("/dashboard/api/notes?q=bm25").json()["total"] == 1


def test_pagination_over_http(rig):
    client, _, _ = rig
    for index in range(5):
        create(client, title=f"Note {index}")
    first = client.get("/dashboard/api/notes?limit=2&offset=0").json()
    second = client.get("/dashboard/api/notes?limit=2&offset=2").json()
    assert first["total"] == 5 and first["has_more"] is True
    assert len(second["items"]) == 2
    assert not ({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})


def test_a_nonsense_limit_falls_back_instead_of_erroring(rig):
    client, _, _ = rig
    assert client.get("/dashboard/api/notes?limit=abc&offset=xyz").status_code == 200


def test_facets_over_http(seeded):
    facets = seeded.get("/dashboard/api/notes/facets").json()
    assert facets["types"]["idea"] == 2
    assert {entry["tag"] for entry in facets["tags"]} == {"ui", "docs"}
    assert facets["max_attachment_bytes"] > 0
    assert "image/png" in facets["allowed_mime_types"]


# -- attachments over HTTP ------------------------------------------------

def test_upload_serve_and_remove_an_image(rig, tmp_path):
    client, _, _ = rig
    note = create(client, title="Có ảnh")
    payload = png_bytes(320, 180)
    response = client.post("/dashboard/api/notes/attachment/upload",
                           data={"note_id": note["id"]},
                           files={"file": ("Ảnh chụp.png", payload, "image/png")},
                           headers=SAME_ORIGIN)
    assert response.status_code == 200
    record = response.json()
    assert record["mime_type"] == "image/png"
    assert (record["width"], record["height"]) == (320, 180)
    assert record["filename"] == "Ảnh chụp.png"

    served = client.get(record["url"])
    assert served.status_code == 200
    assert served.content == payload
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert "inline" in served.headers["content-disposition"]
    # The on-disk path never leaks, in either direction.
    assert "storage_path" not in record
    assert str(tmp_path) not in served.headers.get("content-disposition", "")

    assert client.get("/dashboard/api/notes/note?id=" + note["id"]).json()["attachment_count"] == 1
    removed = client.post("/dashboard/api/notes/attachment/remove", json={"id": record["id"]},
                          headers=SAME_ORIGIN)
    assert removed.status_code == 200 and removed.json()["file_removed"] is True
    assert client.get(record["url"]).status_code == 404


@pytest.mark.parametrize("name,payload,mime,status,code", [
    ("evil.png", NOT_AN_IMAGE, "image/png", 415, "ATTACHMENT_MIME_NOT_ALLOWED"),
    ("a.gif", png_bytes(), "image/gif", 415, "ATTACHMENT_MIME_MISMATCH"),
    ("empty.png", b"", "image/png", 400, "ATTACHMENT_EMPTY"),
])
def test_hostile_uploads_are_refused_with_a_useful_status(rig, name, payload, mime, status, code):
    client, _, _ = rig
    note = create(client, title="T")
    response = client.post("/dashboard/api/notes/attachment/upload",
                           data={"note_id": note["id"]},
                           files={"file": (name, payload, mime)}, headers=SAME_ORIGIN)
    assert response.status_code == status
    assert response.json()["error"] == code


def test_an_oversized_upload_is_refused_with_413(rig, tmp_path):
    server, notes, webauth = build_rig(tmp_path)
    notes.max_attachment_bytes = 64
    client = authenticated_client(server, webauth)
    note = create(client, title="T")
    response = client.post("/dashboard/api/notes/attachment/upload",
                           data={"note_id": note["id"]},
                           files={"file": ("big.png", png_bytes(400, 400), "image/png")},
                           headers=SAME_ORIGIN)
    assert response.status_code == 413
    assert response.json()["error"] == "ATTACHMENT_TOO_LARGE"


def test_a_filename_that_is_a_path_cannot_escape_the_store(rig, tmp_path):
    client, _, _ = rig
    note = create(client, title="T")
    response = client.post("/dashboard/api/notes/attachment/upload",
                           data={"note_id": note["id"]},
                           files={"file": ("../../../../etc/cron.d/evil.png",
                                           png_bytes(), "image/png")},
                           headers=SAME_ORIGIN)
    assert response.status_code == 200
    assert response.json()["filename"] == "evil.png"
    stored = list((tmp_path / "attachments").rglob("*.png"))
    assert len(stored) == 1
    assert (tmp_path / "attachments") in stored[0].parents


def test_upload_needs_both_a_note_id_and_a_file(rig):
    client, _, _ = rig
    assert client.post("/dashboard/api/notes/attachment/upload", data={"note_id": "x"},
                       headers=SAME_ORIGIN).status_code == 400
    assert client.post("/dashboard/api/notes/attachment/upload",
                       files={"file": ("a.png", png_bytes(), "image/png")},
                       headers=SAME_ORIGIN).status_code == 400


def test_uploading_to_a_missing_note_is_a_404(rig):
    client, _, _ = rig
    response = client.post("/dashboard/api/notes/attachment/upload",
                           data={"note_id": "note_nope"},
                           files={"file": ("a.png", png_bytes(), "image/png")},
                           headers=SAME_ORIGIN)
    assert response.status_code == 404


def test_the_attachment_route_takes_an_id_never_a_path(rig):
    client, _, _ = rig
    assert client.get("/dashboard/api/notes/attachment").status_code == 400
    for hostile in ("../../../../etc/passwd", "/etc/passwd", "att_nope"):
        response = client.get("/dashboard/api/notes/attachment?id=" + hostile)
        assert response.status_code == 404
        assert response.json()["error"] == "ATTACHMENT_NOT_FOUND"


def test_a_second_upload_of_the_same_image_is_a_separate_attachment(rig):
    client, _, _ = rig
    note = create(client, title="T")
    ids = set()
    for _ in range(2):
        response = client.post("/dashboard/api/notes/attachment/upload",
                               data={"note_id": note["id"]},
                               files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")},
                               headers=SAME_ORIGIN)
        ids.add(response.json()["id"])
    assert len(ids) == 2
    assert client.get("/dashboard/api/notes/note?id=" + note["id"]).json()["attachment_count"] == 2


# -- one shared store across both surfaces --------------------------------

def test_the_mcp_tools_and_the_dashboard_share_one_store(tmp_path):
    """The bug this guards against: each surface silently building its own
    private default store, so a note ChatGPT saved never appears on the
    page (and vice versa)."""
    import anyio

    notes = NotesService(NotesStore(tmp_path / "notes.db"),
                         attachments_dir=tmp_path / "attachments")
    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("operator", "pw")
    terminal = TerminalService(make_config())
    server = build_mcp(terminal, notes=notes)
    register_dashboard(server, terminal, notes=notes, webauth=webauth)
    client = authenticated_client(server, webauth)

    note = create(client, title="Tạo từ web", summary="zzshared")

    async def search_via_mcp():
        import json
        result = await server.call_tool("note_search", {"query": "zzshared"})
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)

    body = anyio.run(search_via_mcp)
    assert body["items"][0]["id"] == note["id"]
