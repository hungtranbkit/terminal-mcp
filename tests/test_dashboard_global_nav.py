"""Every dashboard full page carries the SAME menu, and this walks the routes.

THE COMPLAINT: "several dashboard screens hide the main navigation; once you
enter one you must press browser Back before you can see the menu again."

That was exactly true. /dashboard carried the whole menu; Sessions, Nodes,
Fleet, Audit, Work, AI Usage, Projects, Agents, Global Tasks, Backlog, Notes,
the Terminal Wall and both ops screens carried a single "back to dashboard"
link or nothing at all.

So this file does not test the constants -- the constants are easy to keep
right. It walks the LIVE Starlette route table of a real assembled app, finds
every route that serves HTML, and asserts the marker is there. A page added
next year without navigation fails here rather than in somebody's browser.
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from terminal_mcp.agent_registry import AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.config import (AppConfig, InputPolicyConfig, NotesConfig,
                                 PermissionsConfig, SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.dashboard_nav import (DESTINATIONS, EMBEDDED_ROUTES, NAV_MARKER,
                                        full_page_routes, with_global_nav)
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.project_runtime import ProjectRuntimeService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=True, default_input=True),
        # Notes has its own auth boundary on top of the dashboard's. Left on,
        # its page 303s to /login and this app has no login form, so the nav
        # inside it would never be exercised. Turned off HERE only -- the
        # guard itself is asserted intact by
        # test_an_auth_guarded_page_still_refuses_before_it_renders_a_menu.
        notes=NotesConfig(require_auth=False),
    )
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    agents = AgentService(AgentRegistryStore(tmp_path / "agents.db"), queue=queue, skill_roots=())
    projects = ProjectRuntimeService(agents)
    service = TerminalService(config)
    notes = _notes_service(tmp_path)
    server = build_mcp(service, queue=queue, agents=agents, project_runtime=projects)
    register_dashboard(server, service, queue=queue, agents=agents, projects=projects,
                       notes=notes)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})


def _notes_service(tmp_path):
    from terminal_mcp.notes_service import NotesService
    from terminal_mcp.notes_store import NotesStore

    return NotesService(NotesStore(tmp_path / "notes.db"),
                        attachments_dir=tmp_path / "attachments")


def _page_routes(app) -> list[str]:
    """Every GET route under /dashboard that is not an API, an asset, or
    parameterised. Read off the real route table, never a hand-kept list."""
    found = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if not path.startswith("/dashboard") or "GET" not in methods:
            continue
        if path.startswith("/dashboard/api/") or "{" in path:
            continue
        found.append(path)
    return sorted(set(found))


# ---------------------------------------------------------------------------
# Coverage: the test that fails when a page is added without navigation.
# ---------------------------------------------------------------------------

def test_every_dashboard_full_page_route_serves_the_global_nav(client):
    routes = _page_routes(client.app)
    assert len(routes) >= 14, f"only found {routes} -- the route scan is not seeing the app"

    missing = []
    for path in routes:
        if path in EMBEDDED_ROUTES:
            continue
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            # /dashboard/requirements serves the doc as plain text on purpose.
            continue
        if NAV_MARKER not in response.text:
            missing.append(path)
    assert missing == [], (
        f"these pages are served without the global menu, so reaching any other "
        f"section from them needs browser Back: {missing}")


def test_the_routes_the_nav_advertises_all_exist_and_answer(client):
    """A menu that links to a 404 is worse than no menu."""
    broken = []
    for destination in DESTINATIONS:
        response = client.get(destination.href)
        if response.status_code != 200:
            broken.append((destination.href, response.status_code))
    assert broken == [], broken


def test_full_page_routes_lists_exactly_the_html_destinations(client):
    """The helper other callers read. `/dashboard/requirements` is a real
    destination served as text/plain, so it is advertised but is not a page
    that can carry a menu -- and that distinction has to be in one place."""
    pages = set(full_page_routes())
    assert "/dashboard/requirements" not in pages
    assert pages == {d.href for d in DESTINATIONS if d.renders_nav}
    for href in pages:
        response = client.get(href)
        assert "text/html" in response.headers.get("content-type", ""), href
        assert NAV_MARKER in response.text, href


def test_an_auth_guarded_page_still_refuses_before_it_renders_a_menu(tmp_path, monkeypatch):
    """Adding navigation must not have widened anything. Notes keeps its own
    boundary on top of the dashboard's, and it still fires FIRST -- the menu
    is not a way to see a page you are not allowed to see."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=True, default_input=True),
        notes=NotesConfig(require_auth=True),
    )
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    agents = AgentService(AgentRegistryStore(tmp_path / "agents.db"), queue=queue, skill_roots=())
    service = TerminalService(config)
    server = build_mcp(service, queue=queue, agents=agents,
                       project_runtime=ProjectRuntimeService(agents))
    register_dashboard(server, service, queue=queue, agents=agents,
                       projects=ProjectRuntimeService(agents),
                       notes=_notes_service(tmp_path))
    guarded = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})

    response = guarded.get("/dashboard/notes", follow_redirects=False)
    assert response.status_code in (303, 401, 403)
    assert NAV_MARKER not in response.text


def test_an_embedded_route_is_documented_rather_than_silently_skipped():
    """A route with no nav must be a decision somebody wrote down."""
    assert "/dashboard/terminal" in EMBEDDED_ROUTES
    assert len(EMBEDDED_ROUTES["/dashboard/terminal"]) > 40, "the reason is the point"


def test_no_page_route_is_missing_from_the_menu(client):
    """The other direction: a page that exists but is unreachable from the
    menu is still a page you can only get to by typing the URL."""
    advertised = {d.href for d in DESTINATIONS}
    orphans = [path for path in _page_routes(client.app)
               if path not in advertised and path not in EMBEDDED_ROUTES]
    assert orphans == [], f"served but not in the menu: {orphans}"


# ---------------------------------------------------------------------------
# Behaviour of the shell itself.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/dashboard", "home"),
    ("/dashboard/projects", "projects"),
    ("/dashboard/agents", "agents"),
    ("/dashboard/tasks", "tasks"),
    ("/dashboard/sessions", "sessions"),
    ("/dashboard/nodes", "nodes"),
    ("/dashboard/work", "work"),
    ("/dashboard/backlog", "backlog"),
    ("/dashboard/notes", "notes"),
    ("/dashboard/fleet", "fleet"),
    ("/dashboard/audit", "audit"),
    ("/dashboard/ai-usage", "ai-usage"),
    ("/dashboard/terminal-wall", "wall"),
])
def test_each_page_marks_itself_as_the_current_one(client, path, expected):
    body = client.get(path).text
    assert f'data-tmcp-active="{expected}"' in body
    href = next(d.href for d in DESTINATIONS if d.key == expected)
    assert re.search(rf'<a href="{re.escape(href)}" aria-current="page"', body), (
        f"{path} does not highlight its own entry")


def test_the_narrow_screen_menu_still_reaches_every_destination(client):
    """On a phone the overflow panel IS the menu. A panel that carried only
    the secondary entries would be the original complaint in a smaller
    window."""
    body = client.get("/dashboard/projects").text
    panel = body[body.index('id="tmcpNavPanel"'):]
    panel = panel[:panel.index("</div>")]
    for destination in DESTINATIONS:
        assert f'href="{destination.href}"' in panel, destination.key


def test_the_menu_is_keyboard_reachable_and_announced(client):
    body = client.get("/dashboard/sessions").text
    assert 'aria-label="Main navigation"' in body
    assert 'aria-expanded="false"' in body and 'aria-controls="tmcpNavPanel"' in body
    assert 'aria-label="Breadcrumb"' in body
    # Links, not click handlers -- Tab and Enter work with no scripting at all.
    assert '<a href="/dashboard/projects"' in body


def test_the_bar_is_compact(client):
    """Chrome on an operations screen. Every pixel it takes is a pixel of
    session output somebody is not reading."""
    from terminal_mcp.dashboard_nav import NAV_HEIGHT_PX

    assert NAV_HEIGHT_PX <= 44
    assert f"--tmcp-nav-h: {NAV_HEIGHT_PX}px" in client.get("/dashboard").text


def test_nothing_request_controlled_is_interpolated_into_the_bar(client):
    """The bar is on every page, so it is the worst possible injection
    surface. Its markup is module constants end to end."""
    body = client.get("/dashboard/projects?project_id=%3Cscript%3Ealert(1)%3C/script%3E").text
    bar = body[body.index(f'class="{NAV_MARKER}'):body.index("</nav>")]
    assert "<script" not in bar
    assert "alert(1)" not in bar


def test_the_breadcrumb_helper_never_writes_html(client):
    body = client.get("/dashboard/projects").text
    script = body[body.index("window.tmcpBreadcrumb"):]
    script = script[:script.index("}})();") if "}})();" in script else len(script)]
    assert "innerHTML" not in script, "a project name is user-controlled text"
    assert "textContent" in script


def test_injection_is_idempotent():
    page = "<!doctype html><html><head></head><body>x</body></html>"
    once = with_global_nav(page, "home")
    assert with_global_nav(once, "home") == once
    assert once.count(NAV_MARKER + '"') + once.count(NAV_MARKER + " ") >= 1


def test_an_unknown_destination_key_is_refused_at_import_time():
    with pytest.raises(ValueError):
        with_global_nav("<html><head></head><body></body></html>", "not-a-page")
