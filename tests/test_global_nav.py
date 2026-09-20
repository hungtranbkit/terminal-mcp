"""The ONE shared global navigation shell, and the coverage that keeps it.

WHAT THIS IS FOR. Every full page in this project is an independent
module-level HTML constant, so each one carried whatever header its author
happened to write -- usually none. An operator who opened /dashboard/agents
from a link had no way to reach Projects, Nodes or Sessions except the
browser's Back button. That was the reported complaint.

The enforcement test is test_every_html_page_carries_the_global_nav: it walks
the REAL route table of a REAL app, fetches every route that answers with
HTML, and fails unless the response carries the shell. It is deliberately not
a list of known pages -- a list would have to be updated by the same person
who forgot the nav. It already paid for itself: it found three pages
(/dashboard/ops/dispatch-settings and the two novaretail-dispatch mounts)
that a source-level grep for the page constants had missed entirely, and two
of those turned out to be tag-soup documents with no <head>/<body> for the
injector to find.
"""
from __future__ import annotations

import logging

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig, NotesConfig,
                                 PermissionsConfig)
from terminal_mcp.controller import ControllerService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.dashboard_nav import (APP_NAV, DASHBOARD_NAV, MOBILE_MARKER, NAV_MARKER,
                                        NavItem, NavSurface, apply, default_breadcrumbs, render)
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.node_client import LocalNodeClient
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.notes_service import NotesService
from terminal_mcp.notes_store import NotesStore
from terminal_mcp.webauth import WebAuthStore
from terminal_mcp.webauth_dashboard import register_webauth_dashboard

PASSWORD = "correct horse battery staple 123"
BASE_URL = "https://testserver"

# Pages that are deliberately NOT part of the navigation, each with the reason
# it is exempt. Kept tiny and explicit: an exemption is a decision someone has
# to write down, not a default.
EXEMPT_PAGES = {
    # Pre-authentication. Offering a menu of pages the visitor cannot open
    # would be both useless and a small disclosure of the fleet's shape.
    "/login": "unauthenticated login form",
}


def _config(**dashboard_kwargs) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(**dashboard_kwargs) if dashboard_kwargs else DashboardConfig(),
        notes=NotesConfig(),
    )


@pytest.fixture(autouse=True)
def _quiet_logs():
    logging.disable(logging.CRITICAL)
    yield
    logging.disable(logging.NOTSET)


@pytest.fixture
def rig(tmp_path):
    """One app with BOTH surfaces registered, exactly as production does.

    The notes service is wired on purpose: without it /dashboard/notes answers
    404 and would silently drop out of the coverage sweep -- a page escaping
    the check by being broken is the one outcome this test must not allow.
    """
    service = TerminalService(_config(), grants=SessionGrantStore(tmp_path / "grants.db"))
    (tmp_path / "inbox").mkdir(exist_ok=True)
    notes = NotesService(NotesStore(tmp_path / "notes.db"),
                         attachments_dir=tmp_path / "attachments",
                         attachment_source_roots=(str(tmp_path / "inbox"),))
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("operator", PASSWORD)
    server = build_mcp(service, notes=notes)
    register_dashboard(server, service, controller=controller, notes=notes, webauth=webauth)
    register_webauth_dashboard(server, service, webauth, controller=controller)
    client = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                        headers={"Origin": BASE_URL})
    # AUTHENTICATED ON PURPOSE. /app/* and /dashboard/notes redirect an
    # anonymous caller to /login, and TestClient follows redirects -- so an
    # unauthenticated sweep silently graded the login page five times instead
    # of the five real pages. A page escaping the coverage check by being
    # unreachable is the one outcome this test must not allow.
    client.post("/login", data={"username": "operator", "password": PASSWORD},
                follow_redirects=False)
    return server, client


def _shell(body: str) -> str:
    """Just the injected nav, not the page's own legacy header.

    Several pages already carried a hand-written menu of their own; those are
    the pages this shell exists to replace, and they are not evidence about
    what the shell offers.
    """
    start = body.index('<div class="tmcp-nav"')
    return body[start:body.index("</div>", body.index("</ul>", start))]


def _page_routes(server) -> list[str]:
    """Every GET route that could plausibly answer with a full page.

    Path-parameter routes are excluded because there is no filename to ask
    for; they serve assets, not pages.
    """
    return sorted({
        route.path for route in server._custom_starlette_routes
        if getattr(route, "methods", None) and "GET" in route.methods
        and "{" not in route.path and "/api/" not in route.path and "/ws/" not in route.path
    })


def _html_pages(server, client) -> list[tuple[str, str]]:
    """(path, body) for every route that actually answered with HTML."""
    pages = []
    for path in _page_routes(server):
        if path in EXEMPT_PAGES:
            continue
        response = client.get(path)
        if "text/html" in response.headers.get("content-type", ""):
            pages.append((path, response.text))
    return pages


# ---------------------------------------------------------------------------
# The enforcement test.
# ---------------------------------------------------------------------------

def test_every_html_page_carries_the_global_nav(rig):
    """A full page added later without the shell fails HERE, not in a bug
    report from someone pressing Back."""
    server, client = rig
    pages = _html_pages(server, client)
    # A sanity floor: if the discovery above ever silently matches nothing,
    # the assertions below would all pass vacuously.
    assert len(pages) >= 15, f"route discovery found only {len(pages)} pages"

    missing = [path for path, body in pages if NAV_MARKER not in body]
    assert not missing, (
        f"these full pages have no global nav: {missing}. Serve them through "
        f"dashboard.py's _nav_page() (or webauth_dashboard.py's _app_page()) "
        f"rather than returning the HTML constant directly.")


def test_every_page_is_usable_on_a_phone(rig):
    """"Has a menu" must not quietly mean "has a menu you cannot open"."""
    server, client = rig
    missing = [path for path, body in _html_pages(server, client) if MOBILE_MARKER not in body]
    assert not missing, f"no mobile disclosure control on: {missing}"


def test_every_page_offers_the_primary_destinations(rig):
    """The menu is the same menu everywhere -- that is the entire point."""
    server, client = rig
    for path, body in _html_pages(server, client):
        if path.startswith("/app"):
            expected = APP_NAV.primary
        else:
            expected = DASHBOARD_NAV.primary
        shell = _shell(body)
        for item in expected:
            assert f'href="{item.href}"' in shell, f"{path} is missing the {item.label!r} link"


def test_every_nav_link_points_at_a_route_that_exists(rig):
    """A menu of 404s is worse than no menu."""
    server, _client = rig
    known = set(_page_routes(server))
    for surface in (DASHBOARD_NAV, APP_NAV):
        for item in surface.items:
            assert item.href in known, f"{surface.brand} nav links to unrouted {item.href}"
        assert surface.home in known


def test_every_nav_link_actually_answers(rig):
    """Registered is not the same as reachable."""
    server, client = rig
    for item in DASHBOARD_NAV.items:
        response = client.get(item.href)
        assert response.status_code == 200, f"{item.href} answered {response.status_code}"


# ---------------------------------------------------------------------------
# Active state and breadcrumbs.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,label", [
    ("/dashboard", "Home"),
    ("/dashboard/projects", "Projects"),
    ("/dashboard/agents", "Agents"),
    ("/dashboard/tasks", "Global Tasks"),
    ("/dashboard/sessions", "Sessions"),
    ("/dashboard/nodes", "Nodes"),
    ("/dashboard/work", "Queue / Review"),
    ("/dashboard/backlog", "Backlog"),
])
def test_the_current_page_is_marked_active(rig, path, label):
    _server, client = rig
    body = client.get(path).text
    assert 'aria-current="page"' in body
    # The active entry is the one for THIS page, not merely some entry.
    item = next(entry for entry in DASHBOARD_NAV.items if entry.href == path)
    marked = f'class="tmcp-nav__link is-active" href="{item.href}" aria-current="page"'
    assert marked in body, f"{path} did not mark {label!r} as the active destination"


def test_exactly_one_destination_is_active_per_page(rig):
    _server, client = rig
    for item in DASHBOARD_NAV.items:
        body = client.get(item.href).text
        assert body.count('class="tmcp-nav__link is-active"') == 1, item.href
        assert body.count(NAV_MARKER) == 1, item.href


def test_pages_carry_a_nested_breadcrumb_trail(rig):
    _server, client = rig
    body = client.get("/dashboard/projects").text
    assert 'aria-label="Breadcrumb"' in body
    # Home is a link (you can go up); the current page is not.
    assert '<li><a href="/dashboard">Home</a></li>' in body
    assert '<li aria-current="page">Projects</li>' in body


def test_the_home_page_breadcrumb_does_not_link_to_itself(rig):
    _server, client = rig
    body = client.get("/dashboard").text
    assert '<li aria-current="page">Home</li>' in body
    assert '<li><a href="/dashboard">Home</a></li>' not in body


def test_a_page_may_supply_its_own_deeper_trail():
    """Projects/Agents detail views pass a nested trail of their own."""
    html = apply("<html><head></head><body>x</body></html>", DASHBOARD_NAV, "projects",
                 (("Home", "/dashboard"), ("Projects", "/dashboard/projects"),
                  ("orchnav-live", None)))
    assert '<li><a href="/dashboard/projects">Projects</a></li>' in html
    assert '<li aria-current="page">orchnav-live</li>' in html


# ---------------------------------------------------------------------------
# The two surfaces stay separate.
# ---------------------------------------------------------------------------

def test_the_app_surface_uses_the_shell_with_its_own_destinations(rig):
    """/app is behind a session cookie and /dashboard is behind Cloudflare
    Access, so one menu spanning both would offer links nobody can open."""
    _server, client = rig
    client.post("/login", data={"username": "operator", "password": PASSWORD},
                follow_redirects=False)
    shell = _shell(client.get("/app").text)
    assert NAV_MARKER in shell
    assert 'href="/app/sessions"' in shell
    assert 'href="/dashboard/projects"' not in shell


def test_the_dashboard_surface_does_not_advertise_app_pages(rig):
    _server, client = rig
    assert 'href="/app/sessions"' not in _shell(client.get("/dashboard").text)


def test_the_login_page_is_exempt_and_stays_exempt(rig):
    """Documented exemption, asserted so it cannot drift into an accident."""
    server, _client = rig
    anonymous = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                           headers={"Origin": BASE_URL})
    response = anonymous.get("/login")
    assert response.status_code == 200
    assert NAV_MARKER not in response.text


# ---------------------------------------------------------------------------
# The injector itself.
# ---------------------------------------------------------------------------

def test_apply_is_idempotent():
    page = "<html><head><title>t</title></head><body><p>hi</p></body></html>"
    once = apply(page, DASHBOARD_NAV, "home")
    assert once.count(NAV_MARKER) == 1
    assert apply(once, DASHBOARD_NAV, "home") == once


def test_apply_handles_a_tag_soup_page():
    """Half the ops pages are a doctype, a title, a style and content, with
    <head>/<body> implied. The style must still land before the content."""
    page = "<!doctype html><title>Ops</title><style>body{color:#fff}</style><div>body</div>"
    out = apply(page, DASHBOARD_NAV, "dispatch-settings")
    assert NAV_MARKER in out
    assert out.index("tmcp-global-nav-css") < out.index("<div>body</div>")
    assert out.startswith("<!doctype html><title>Ops</title>")


def test_apply_puts_the_stylesheet_in_head_and_the_bar_first_in_body():
    page = "<html><head><title>t</title></head><body><p>hi</p></body></html>"
    out = apply(page, DASHBOARD_NAV, "home")
    assert out.index("tmcp-global-nav-css") < out.index("</head>")
    assert out.index(NAV_MARKER) < out.index("<p>hi</p>")


def test_apply_leaves_an_empty_body_alone():
    assert apply("", DASHBOARD_NAV, "home") == ""


def test_breadcrumb_text_is_escaped():
    """Nothing here is caller-supplied today; escaping is what keeps that
    true if a page ever passes a project name through."""
    html = apply("<html><head></head><body></body></html>", DASHBOARD_NAV, "projects",
                 (("Home", "/dashboard"), ('<script>alert(1)</script>', None)))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_the_overflow_group_shows_the_active_secondary_page_by_name():
    """A secondary page must not look like nothing is selected."""
    html = render(DASHBOARD_NAV, "audit")
    assert "has-active" in html
    assert "<summary" in html and "Audit" in html


def test_a_surface_with_no_secondary_items_renders_no_overflow_group():
    surface = NavSurface(brand="X", home="/x", items=(NavItem("home", "Home", "/x"),))
    assert "tmcp-nav__more" not in render(surface, "home")


def test_default_breadcrumbs_are_home_then_section():
    assert default_breadcrumbs(DASHBOARD_NAV, "agents") == (
        ("Home", "/dashboard"), ("Agents", None))
    assert default_breadcrumbs(DASHBOARD_NAV, "home") == (("Home", None),)


def test_the_nav_never_restyles_the_page_it_joins():
    """These pages were written independently and several use bare element
    selectors, so every rule the shell adds is scoped to its own classes."""
    from terminal_mcp.dashboard_nav import NAV_CSS
    import re

    selectors = re.findall(r"^([^@{}/\s][^{}]*)\{", NAV_CSS, re.MULTILINE)
    for selector in selectors:
        for part in selector.split(","):
            part = part.strip()
            if not part or part.startswith((":root", "@")):
                continue
            assert "tmcp-" in part, f"unscoped global selector in the nav stylesheet: {part!r}"


# ---------------------------------------------------------------------------
# Security posture is unchanged.
# ---------------------------------------------------------------------------

def test_pages_keep_their_security_headers(rig):
    _server, client = rig
    for path in ("/dashboard", "/dashboard/projects", "/dashboard/agents"):
        response = client.get(path)
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Cache-Control"] == "no-store"


def test_the_nav_needs_no_script(rig):
    """The mobile disclosure is a real checkbox. Nothing the shell adds
    depends on JavaScript loading, or on the CSP being loosened for it."""
    from terminal_mcp.dashboard_nav import NAV_CSS

    assert "<script" not in NAV_CSS
    assert "<script" not in render(DASHBOARD_NAV, "home")


def test_an_unauthenticated_app_page_still_redirects(rig):
    """The shell must not have changed who can see a page."""
    server, _client = rig
    anonymous = TestClient(server.streamable_http_app(), base_url=BASE_URL,
                           headers={"Origin": BASE_URL})
    response = anonymous.get("/app", follow_redirects=False)
    assert response.status_code in (302, 303, 307)
