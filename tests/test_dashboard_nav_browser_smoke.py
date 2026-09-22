"""A real browser walks the dashboard and never touches the Back button.

Marked `browser_smoke`, so it is opt-in like every other real-Chromium test
here: `pytest -m browser_smoke tests/test_dashboard_nav_browser_smoke.py`.

WHY A REAL BROWSER, NEXT TO tests/test_global_nav.py. That file proves the bar
is IN every page, by walking the real route table -- which is the check that
stops a new page shipping without navigation, and is the more important of the
two. It cannot prove the bar is VISIBLE. A bar that is present but zero-height,
painted under a full-height flex child, wrapped onto two rows by a page's own
`nav {}` rule, or with its overflow drawer clipped away by an inherited
`overflow: auto` would pass every markup assertion and fail the only thing the
operator asked for.

So this measures the RENDERED bar on each page, at each viewport, against the
real app on loopback -- including the pages whose layouts it has to survive:
full-height flex columns, ordinary flow, and AI Usage's CSS grid with a bare
`nav` sidebar rule of its own.
"""
from __future__ import annotations

import re
import socket
import threading
import time

import pytest

from terminal_mcp.agent_registry import AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.config import (AppConfig, InputPolicyConfig, NotesConfig,
                                 PermissionsConfig, SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp import dashboard_nav
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.project_runtime import ProjectRuntimeService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore

pytestmark = pytest.mark.browser_smoke

LAPTOP = {"width": 1366, "height": 768}
DESKTOP = {"width": 1920, "height": 1080}
PHONE = {"width": 390, "height": 844}

#: Every /dashboard destination the shared surface advertises, except the web
#: terminal: it attaches a browser to one pane's real pty, and measuring the
#: chrome over a live terminal says nothing useful.
PAGES = [item for item in dashboard_nav.DASHBOARD_NAV.items
         if item.href != "/dashboard/terminal"]

#: Read out of the shipped stylesheet rather than repeated here. A number
#: copied into a test stops being a check of anything.
NAV_HEIGHT_PX = int(re.search(r"--tmcp-nav-h:\s*(\d+)px", dashboard_nav.NAV_CSS).group(1))

#: The hops the complaint was about -- entering any of these used to be a
#: one-way trip. Ordered as an operator would actually walk them.
WALK = ["projects", "agents", "tasks", "sessions", "nodes", "home"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    import os

    import uvicorn

    tmp_path = tmp_path_factory.mktemp("navsmoke")
    os.environ["XDG_STATE_HOME"] = str(tmp_path / "state")
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=True, default_input=True),
        # Notes keeps its own boundary on top of the dashboard's; left on, its
        # page 303s to a login form this app does not serve, so the bar inside
        # it would never be exercised. tests/test_global_nav.py is where the
        # guard itself is asserted intact.
        notes=NotesConfig(require_auth=False),
    )
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    agents = AgentService(AgentRegistryStore(tmp_path / "agents.db"), queue=queue, skill_roots=())
    projects = ProjectRuntimeService(agents)
    service = TerminalService(config)
    from terminal_mcp.notes_service import NotesService
    from terminal_mcp.notes_store import NotesStore

    notes = NotesService(NotesStore(tmp_path / "notes.db"),
                         attachments_dir=tmp_path / "attachments")
    server = build_mcp(service, queue=queue, agents=agents, project_runtime=projects)
    register_dashboard(server, service, queue=queue, agents=agents, projects=projects,
                       notes=notes)

    port = _free_port()
    uvi = uvicorn.Server(uvicorn.Config(server.streamable_http_app(), host="127.0.0.1",
                                        port=port, log_level="error"))
    thread = threading.Thread(target=uvi.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not uvi.started:
        time.sleep(0.05)
    if not uvi.started:  # pragma: no cover -- the server never came up
        pytest.skip("the test server did not start")
    yield f"http://127.0.0.1:{port}"
    uvi.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def page(server_url):
    sync_playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright not installed -- browser navigation smoke skipped").sync_playwright
    with sync_playwright() as pw:
        try:
            # The same arg set tests/test_dashboard_desktop_layout.py uses, and
            # for the same reason: chrome-headless-shell cannot start its GPU
            # subprocess on a sandboxed host and aborts unless kept in one
            # process.
            browser = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        opened = browser.new_page(viewport=LAPTOP)
        yield opened
        browser.close()


def _nav_box(page):
    return page.evaluate("""() => {
      const bar = document.querySelector('.tmcp-nav');
      if (!bar) return null;
      const r = bar.getBoundingClientRect();
      const style = getComputedStyle(bar);
      return { top: r.top, height: r.height, width: r.width,
               direction: style.flexDirection, wrap: style.flexWrap,
               overflow: style.overflowY,
               visible: style.display !== 'none' && style.visibility !== 'hidden' };
    }""")


def _goto(page, server_url, href):
    page.goto(server_url + href, wait_until="domcontentloaded")
    page.wait_for_selector(".tmcp-nav", timeout=15000)
    page.wait_for_timeout(250)


# ---------------------------------------------------------------------------
# The bar is really on screen, on every page, at every size.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("destination", PAGES, ids=lambda d: d.key)
@pytest.mark.parametrize("viewport", [LAPTOP, DESKTOP], ids=["1366x768", "1920x1080"])
def test_the_menu_is_rendered_and_on_screen(page, server_url, destination, viewport):
    page.set_viewport_size(viewport)
    _goto(page, server_url, destination.href)
    box = _nav_box(page)
    assert box is not None, f"{destination.href} rendered no menu at all"
    assert box["visible"], f"{destination.href} has a menu that is not displayed"
    assert box["height"] >= NAV_HEIGHT_PX - 2, (
        f"{destination.href}: the bar collapsed to {box['height']}px -- present in the "
        f"markup, invisible on screen, which is the failure this pass is about")
    assert box["top"] <= 1, f"{destination.href}: the bar is {box['top']}px down the page"
    assert box["width"] >= viewport["width"] * 0.5


@pytest.mark.parametrize("destination", PAGES, ids=lambda d: d.key)
def test_a_page_cannot_restyle_the_shared_bar_out_of_shape(page, server_url, destination):
    """These pages were written independently and several style bare element
    selectors -- AI Usage's own sidebar is a bare `nav` rule with
    flex-direction:column, overflow-y:auto and a right border. The shell is a
    <div role="navigation"> for exactly that reason, but "which rule won" is
    the one thing a stylesheet cannot tell you, so it is measured."""
    page.set_viewport_size(LAPTOP)
    _goto(page, server_url, destination.href)
    box = _nav_box(page)
    assert box["direction"] == "row", f"{destination.href}: the bar stacked vertically"
    assert box["overflow"] == "visible", (
        f"{destination.href}: overflow is {box['overflow']!r}, which clips the drawer")
    assert box["height"] <= NAV_HEIGHT_PX * 2, (
        f"{destination.href}: the bar grew to {box['height']}px -- it wrapped")


@pytest.mark.parametrize("destination", PAGES, ids=lambda d: d.key)
def test_the_menu_does_not_steal_the_page(page, server_url, destination):
    """Compact means compact: this is chrome on an operations screen."""
    page.set_viewport_size(LAPTOP)
    _goto(page, server_url, destination.href)
    box = _nav_box(page)
    assert box["height"] / LAPTOP["height"] < 0.12, (
        f"{destination.href}: the menu took {box['height']}px of {LAPTOP['height']}px")


def test_no_page_scrolls_sideways_because_of_the_menu(page, server_url):
    page.set_viewport_size(LAPTOP)
    for destination in PAGES:
        _goto(page, server_url, destination.href)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 2, f"{destination.href} scrolls {overflow}px sideways"


# ---------------------------------------------------------------------------
# The actual complaint: getting from one section to another.
# ---------------------------------------------------------------------------

def test_a_whole_tour_of_the_dashboard_without_pressing_back(page, server_url):
    """THE HEADLINE. Start at Home and click through every primary section
    using only the menu. Nothing here calls go_back()."""
    page.set_viewport_size(LAPTOP)
    _goto(page, server_url, "/dashboard")
    visited = ["/dashboard"]

    for key in WALK:
        destination = next(d for d in dashboard_nav.DASHBOARD_NAV.items if d.key == key)
        link = page.locator(f'.tmcp-nav__menu a[href="{destination.href}"]')
        assert link.count() >= 1, f"no menu link to {key} from {visited[-1]}"
        link.first.click()
        page.wait_for_url(f"**{destination.href}", timeout=15000)
        page.wait_for_selector(".tmcp-nav", timeout=15000)
        current = page.evaluate("() => location.pathname")
        assert current == destination.href, f"clicking {key} landed on {current}"
        assert _nav_box(page)["visible"], f"the menu vanished on {key}"
        visited.append(current)

    assert len(set(visited)) >= len(WALK), visited


def test_a_secondary_section_is_one_click_from_a_primary_one(page, server_url):
    """Fleet, Audit, AI Usage and the ops screens live in the overflow
    drawer. One click to open it, one to arrive -- never Back."""
    page.set_viewport_size(LAPTOP)
    _goto(page, server_url, "/dashboard/projects")
    page.click(".tmcp-nav__more > summary")
    page.wait_for_timeout(200)
    page.click('.tmcp-nav__drawer a[href="/dashboard/audit"]')
    page.wait_for_url("**/dashboard/audit", timeout=15000)
    assert page.evaluate("() => location.pathname") == "/dashboard/audit"
    assert _nav_box(page)["visible"]


def test_the_overflow_drawer_opens_on_every_page(page, server_url):
    """The drawer is absolutely positioned inside the bar. A page whose own
    styles gave the bar `overflow: auto` would clip it into a one-line
    scroller, which looks exactly like the menu not working."""
    page.set_viewport_size(LAPTOP)
    for destination in PAGES:
        _goto(page, server_url, destination.href)
        page.click(".tmcp-nav__more > summary")
        page.wait_for_timeout(150)
        height = page.evaluate(
            "() => document.querySelector('.tmcp-nav__drawer').getBoundingClientRect().height")
        assert height > 80, f"{destination.href}: the drawer opened {height}px tall"


def test_the_current_page_is_marked_in_the_menu(page, server_url):
    page.set_viewport_size(LAPTOP)
    for key in ("projects", "agents", "sessions", "nodes"):
        destination = next(d for d in dashboard_nav.DASHBOARD_NAV.items if d.key == key)
        _goto(page, server_url, destination.href)
        marked = page.evaluate("""() => {
          const a = document.querySelector('.tmcp-nav a[aria-current="page"]');
          return a ? a.getAttribute('href') : null;
        }""")
        assert marked == destination.href, f"{key} highlighted {marked}"


# ---------------------------------------------------------------------------
# Narrow screens.
# ---------------------------------------------------------------------------

def test_on_a_phone_every_destination_is_still_reachable(page, server_url):
    page.set_viewport_size(PHONE)
    _goto(page, server_url, "/dashboard/projects")

    collapsed = page.evaluate(
        "() => getComputedStyle(document.querySelector('.tmcp-nav__menu')).display === 'none'")
    assert collapsed, "the wide menu is still shown on a 390px screen"

    page.click(".tmcp-nav__burger")
    page.wait_for_timeout(200)
    shown = page.evaluate(
        "() => getComputedStyle(document.querySelector('.tmcp-nav__menu')).display !== 'none'")
    assert shown, "the burger did not open the menu"

    hrefs = page.evaluate(
        "() => [...document.querySelectorAll('.tmcp-nav a')].map(a => a.getAttribute('href'))")
    for destination in dashboard_nav.DASHBOARD_NAV.primary:
        assert destination.href in hrefs, f"{destination.key} unreachable on a phone"

    page.click('.tmcp-nav__menu a[href="/dashboard/nodes"]')
    page.wait_for_url("**/dashboard/nodes", timeout=15000)
    assert _nav_box(page)["visible"]


def test_the_menu_is_keyboard_reachable(page, server_url):
    """A keyboard user must reach the first destination without a mouse, and
    the disclosure must be operable -- it is a native control for that
    reason."""
    page.set_viewport_size(LAPTOP)
    _goto(page, server_url, "/dashboard/agents")
    page.evaluate("() => document.body.focus()")
    reached = None
    for _ in range(6):
        page.keyboard.press("Tab")
        reached = page.evaluate("""() => {
          const a = document.activeElement;
          return a && a.closest && a.closest('.tmcp-nav')
            ? (a.getAttribute('href') || a.id || a.tagName) : null;
        }""")
        if reached:
            break
    assert reached, "six tabs from the top of the page never reached the menu"
