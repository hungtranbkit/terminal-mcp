"""Mobile portrait: the terminal is the page.

The bug, from a real iPhone screenshot: header + session filter + node/
session strip + status card + term bar + composer + quick-key row between
them left the OUTPUT -- the only thing anyone opens this page to read --
with about a third of the screen, and less once the strip filled up.
Measured on the pre-redesign build at 844x390 (landscape) it was 20px. On
390x844 the strip alone was allowed 38vh.

So portrait is not a squeezed desktop:
  * one row of chrome above the terminal, one below;
  * the session list is a bottom SHEET, not a permanent column;
  * status/permissions/quick-keys are on demand.

Two kinds of test here. The structural ones always run and pin the
information architecture. The measurement ones drive a real Chromium at
real iPhone viewports and assert the actual pixel outcome -- they are the
only ones that can prove "the output is most of the screen", so when no
browser is installed they skip loudly rather than pretending.
"""
from __future__ import annotations

import json
import re

import pytest

from terminal_mcp.dashboard import DASHBOARD_HTML

# iPhone 14 / 15 Pro Max CSS pixel sizes, the two the task names.
PORTRAIT_VIEWPORTS = [(390, 844), (430, 932)]
MIN_OUTPUT_SHARE = 0.55        # the task's own floor
KEYBOARD_UP = (390, 420)       # virtual keyboard eating half the screen


# -- structure ---------------------------------------------------------------

def _portrait_css() -> str:
    start = DASHBOARD_HTML.index("@media (max-width:760px) and (orientation:portrait)")
    depth, i = 0, DASHBOARD_HTML.index("{", start)
    for j in range(i, len(DASHBOARD_HTML)):
        if DASHBOARD_HTML[j] == "{":
            depth += 1
        elif DASHBOARD_HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return DASHBOARD_HTML[start:j + 1]
    raise AssertionError("portrait media query never closes")


def test_the_page_declares_a_portrait_specific_layout():
    assert "@media (max-width:760px) and (orientation:portrait)" in DASHBOARD_HTML


def test_the_session_list_is_a_sheet_in_portrait_not_a_permanent_row():
    css = _portrait_css()
    assert "position:fixed" in css.split(".tabbar-row")[1].split("}")[0]
    assert "body.sessions-open .tabbar-row { transform:translateY(0) }" in css


def test_the_session_list_and_the_terminal_are_the_same_elements_as_on_desktop():
    # The whole redesign is layout. If the sheet were a second list, node
    # grouping/filter/status dots would have to be reimplemented and would
    # drift -- this repo has removed exactly that duplication once before.
    assert DASHBOARD_HTML.count('id="tabbar"') == 1
    assert DASHBOARD_HTML.count('id="sessionFilter"') == 1
    assert DASHBOARD_HTML.count('id="output"') == 1


def test_status_and_permissions_are_not_between_the_header_and_the_output():
    css = _portrait_css()
    inspector = css.split("#sessionInspector")[1].split("}")[0]
    assert "position:fixed" in inspector
    # With the inspector out of flow the grid is terminal + composer only.
    assert ".detail { grid-template-rows:minmax(0,1fr) auto auto auto auto }" in css
    assert ".term { grid-row:1 }" in css


def test_the_terminal_gets_the_flexible_track_when_the_strip_leaves_the_flow():
    # The regression this caught when measured: `.tabbar-row` going
    # position:fixed made `.detail` the first in-flow grid item, so it
    # landed in the `auto` track meant for the strip and collapsed the
    # terminal to 31px of an 844px screen.
    assert "main { padding:0; gap:0; grid-template-rows:minmax(0,1fr) }" in _portrait_css()


def test_quick_keys_collapse_behind_a_button_in_portrait():
    css = _portrait_css()
    assert "#keyPad:not([hidden]) { display:none }" in css
    assert "body.keys-open #keyPad:not([hidden]) { display:flex }" in css
    # [hidden] is JS state meaning "input is not usable here" and must keep
    # winning over the expanded rule.
    assert 'id="keysToggleBtn"' in DASHBOARD_HTML


def test_every_quick_key_is_still_present():
    for key in ("Up", "Down", "Left", "Right", "Tab", "Escape", "Enter"):
        assert f'data-key="{key}"' in DASHBOARD_HTML


def test_kill_stays_a_menu_item_not_a_permanent_button():
    assert 'id="termKillBtn"' in DASHBOARD_HTML
    menu = DASHBOARD_HTML[DASHBOARD_HTML.index('id="termMenuPanel"'):]
    assert 'id="termKillBtn"' in menu[:menu.index("</div>")]


def test_there_is_a_one_tap_way_back_to_the_latest_output():
    assert 'id="jumpFab"' in DASHBOARD_HTML
    assert "jumpFabEl.hidden = autoFollow || !selected;" in DASHBOARD_HTML


def test_selecting_a_session_closes_the_sheet():
    assert "setSessionsDrawer(false);" in DASHBOARD_HTML


def test_viewport_meta_handles_notch_and_soft_keyboard():
    meta = re.search(r'<meta name="viewport" content="([^"]+)"', DASHBOARD_HTML).group(1)
    assert "viewport-fit=cover" in meta                 # safe areas are addressable
    assert "interactive-widget=resizes-content" in meta  # keyboard shrinks, not scrolls
    assert "100dvh" in DASHBOARD_HTML                   # not the broken 100vh alone


def test_portrait_respects_the_safe_area():
    css = _portrait_css()
    for inset in ("safe-area-inset-top", "safe-area-inset-bottom",
                  "safe-area-inset-left", "safe-area-inset-right"):
        assert inset in css


def test_touch_targets_are_at_least_44px():
    assert "min-height:44px" in DASHBOARD_HTML


# -- the deprecated vocabulary -----------------------------------------------

def test_no_visible_whitelist_wording_survives():
    # Access is default-open: absence of a grant record means ALLOW. A UI
    # that still says "whitelist" is describing a mechanism that no longer
    # exists, which is worse than saying nothing.
    from terminal_mcp import dashboard

    for name in ("DASHBOARD_HTML", "SESSIONS_ADMIN_HTML", "NODES_ADMIN_HTML",
                 "GLOBAL_TASKS_HTML", "BACKLOG_HTML", "WEBTERM_HTML"):
        html = getattr(dashboard, name)
        # Only user-visible text: strip comments and script bodies, where
        # the word legitimately survives in explanatory prose.
        visible = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        visible = re.sub(r"<script>.*?</script>", "", visible, flags=re.S)
        visible = re.sub(r"<style>.*?</style>", "", visible, flags=re.S)
        assert "whitelist" not in visible.casefold(), f"{name} still says whitelist to the user"


# -- measurement -------------------------------------------------------------
#
# Structure is necessary but not sufficient: "the output is most of the
# screen" is a statement about pixels, and only a real engine can settle it.
# The regression that proves the point was invisible to every assertion
# above -- making the tab strip position:fixed took it out of the grid flow,
# so `.detail` landed in the `auto` track and the terminal rendered 31px
# tall on an 844px screen, with correct-looking CSS throughout.

SESSION_ROWS = [
    {"name": f"session-{i:02d}", "node_id": "dell-linux" if i % 2 else "local",
     "node_name": "dell-linux" if i % 2 else "m910", "state": "RUNNING",
     "attached": False, "windows": 1, "effective_read": True, "effective_input": True,
     "read_allowed": True, "input_allowed": True, "read_granted": True,
     "input_granted": True, "allowed": True,
     "created": "2026-09-11T00:00:00Z", "activity": "2026-09-11T00:00:00Z"}
    for i in range(24)
]
SESSION_DETAIL = {
    "session": "session-00", "state": "RUNNING", "read_allowed": True,
    "input_allowed": True, "effective_read": True, "effective_input": True,
    # The exact shape loadDetail renders: it reads data.status.state and
    # data.tail.output, and throws before painting if either is absent --
    # which is how this fixture first produced a permanently "Đang tải…"
    # pane and two failing tests that looked like layout bugs.
    "status": {"state": "RUNNING", "reason": "idle"},
    "tail": {"output": "\n".join(f"log line {i:04d}" for i in range(500))},
}

MEASURE_JS = """() => {
  const box = (s) => { const e = document.querySelector(s); return e ? e.getBoundingClientRect() : null; };
  const style = (s) => { const e = document.querySelector(s); return e ? getComputedStyle(e) : null; };
  const out = box('#output'), drawer = box('#sessionsDrawer');
  return {
    vw: window.innerWidth, vh: window.innerHeight,
    output_h: out ? out.height : 0,
    output_share: out ? out.height / window.innerHeight : 0,
    output_visible: !!out && out.height > 0 && out.top < window.innerHeight && out.bottom > 0,
    drawer_onscreen: drawer ? drawer.top < window.innerHeight - 4 : null,
    drawer_position: style('#sessionsDrawer').position,
    inspector_position: style('#sessionInspector').position,
    horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1,
    page_scrollable: document.documentElement.scrollHeight > window.innerHeight + 1,
    session_name: document.querySelector('#mobileSessionName').textContent,
    output_len: document.querySelector('#output').textContent.length,
  };
}"""


def _route(route, html):
    url = route.request.url
    if "/dashboard/api/sessions" in url:
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"sessions": SESSION_ROWS}))
    if "/dashboard/api/session" in url:
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps(SESSION_DETAIL))
    if "/dashboard" in url and "/api/" not in url:
        return route.fulfill(status=200, content_type="text/html", body=html)
    return route.fulfill(status=200, content_type="application/json", body="{}")


@pytest.fixture(scope="module")
def phone_page():
    """A real Chromium at a real phone viewport, with a session already open.

    Skips rather than fails when no browser is installed: this repo does not
    vendor one, and a missing dev dependency is not a product regression.
    """
    sync_playwright = pytest.importorskip(
        "playwright.sync_api",
        reason="playwright not installed -- viewport measurements skipped",
    ).sync_playwright

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                # This host runs the suite inside a restricted sandbox where
                # Chromium's zygote cannot fork; these make it start anyway.
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001 -- any launch failure means "no browser here"
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.route("**/*", lambda route: _route(route, DASHBOARD_HTML))
        page.goto("http://terminal-mcp.test/dashboard", wait_until="domcontentloaded")
        page.wait_for_selector("#tabbar .tab", timeout=15000)
        page.click("#mobileSessionsBtn")
        page.wait_for_timeout(300)
        page.click("#tabbar .tab")
        page.wait_for_timeout(700)
        yield page
        browser.close()


def _at(page, width, height):
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(300)
    return page.evaluate(MEASURE_JS)


@pytest.mark.parametrize("width,height", PORTRAIT_VIEWPORTS)
def test_output_is_most_of_the_screen_on_load(phone_page, width, height):
    m = _at(phone_page, width, height)
    assert m["output_visible"]
    assert m["output_share"] >= MIN_OUTPUT_SHARE, (
        f"terminal got {m['output_share']:.0%} of {width}x{height}, "
        f"needs >= {MIN_OUTPUT_SHARE:.0%}")
    assert not m["page_scrollable"], "the page itself must not scroll; only the panes do"


@pytest.mark.parametrize("width,height", PORTRAIT_VIEWPORTS)
def test_no_horizontal_overflow(phone_page, width, height):
    assert not _at(phone_page, width, height)["horizontal_overflow"]


def test_the_session_list_is_offscreen_until_asked_for(phone_page):
    m = _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    assert m["drawer_position"] == "fixed"
    assert m["drawer_onscreen"] is False


def test_opening_and_closing_the_sheet_keeps_the_session_and_its_output(phone_page):
    before = _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    phone_page.click("#mobileSessionsBtn")
    phone_page.wait_for_timeout(300)
    opened = phone_page.evaluate(MEASURE_JS)
    assert opened["drawer_onscreen"] is True
    # The list scrolls inside the sheet; it never drags the page with it.
    assert phone_page.evaluate(
        "() => { const t = document.querySelector('#tabbar'); return t.scrollHeight > t.clientHeight + 1; }")
    assert not opened["page_scrollable"]

    phone_page.click("#sessionsDrawerClose")
    phone_page.wait_for_timeout(300)
    after = phone_page.evaluate(MEASURE_JS)
    assert after["drawer_onscreen"] is False
    assert after["session_name"] == before["session_name"]
    assert after["output_len"] == before["output_len"]
    assert after["output_share"] >= MIN_OUTPUT_SHARE


def test_choosing_a_session_returns_to_the_terminal(phone_page):
    _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    phone_page.click("#mobileSessionsBtn")
    phone_page.wait_for_timeout(300)
    phone_page.click("#tabbar .tab:nth-of-type(2)")
    phone_page.wait_for_timeout(500)
    assert phone_page.evaluate("() => !document.body.classList.contains('sessions-open')")
    assert phone_page.evaluate(MEASURE_JS)["output_share"] >= MIN_OUTPUT_SHARE


def test_the_terminal_survives_the_soft_keyboard(phone_page):
    # Standing in for iOS shrinking the visual viewport: the newest output
    # must still be on screen, not pushed out by the composer and toolbars.
    m = _at(phone_page, *KEYBOARD_UP)
    assert m["output_visible"]
    assert m["output_h"] > 0
    assert not m["page_scrollable"]
    assert not m["horizontal_overflow"]


def test_quick_keys_expand_and_collapse(phone_page):
    _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    assert phone_page.evaluate("() => getComputedStyle(document.querySelector('#keysToggleBtn')).display") != "none"
    phone_page.click("#keysToggleBtn")
    phone_page.wait_for_timeout(200)
    assert phone_page.evaluate("() => document.body.classList.contains('keys-open')")
    phone_page.click("#keysToggleBtn")
    phone_page.wait_for_timeout(200)
    assert phone_page.evaluate("() => !document.body.classList.contains('keys-open')")
    assert phone_page.evaluate(MEASURE_JS)["output_share"] >= MIN_OUTPUT_SHARE


def test_a_long_log_scrolls_inside_the_output_only(phone_page):
    _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    assert phone_page.evaluate(
        "() => { const o = document.querySelector('#output'); return o.scrollHeight > o.clientHeight + 1; }")
    assert not phone_page.evaluate(MEASURE_JS)["page_scrollable"]


def test_scrolling_up_pauses_follow_and_offers_the_way_back(phone_page):
    _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    phone_page.evaluate("() => { const o = document.querySelector('#output'); o.scrollTop = 0; o.dispatchEvent(new Event('scroll')); }")
    phone_page.wait_for_timeout(250)
    assert phone_page.evaluate("() => !document.querySelector('#jumpFab').hidden"), \
        "a paused view must offer a one-tap way back to the latest output"
    phone_page.click("#jumpFab")
    phone_page.wait_for_timeout(250)
    assert phone_page.evaluate("() => document.querySelector('#jumpFab').hidden")
    assert phone_page.evaluate(
        "() => { const o = document.querySelector('#output');"
        "        return o.scrollHeight - o.scrollTop - o.clientHeight < 40; }")


def test_desktop_layout_is_untouched(phone_page):
    m = _at(phone_page, 1440, 900)
    assert m["drawer_position"] == "static", "the tab strip stays a strip on desktop"
    assert m["inspector_position"] == "static", "status/permissions stay in the column on desktop"
    assert not m["horizontal_overflow"]


# The screenshot found what the height measurements could not: both control
# rows were 455px wide inside a 390px viewport, so "Gửi", the Enter
# checkbox, fullscreen and the "..." menu (Kill, Chi tiết, Search, Copy)
# were all off-screen -- unreachable, with no horizontal page scrollbar to
# reveal them, because the app shell is overflow:hidden. Height was fine
# throughout. So: assert every interactive control is actually on screen.

CONTROLS = ["#mobileSessionsBtn", "#keysToggleBtn", "#inputText", "#inputSend",
            "#inputBar label", "#followToggle", "#taskManagerBtn",
            "#fullscreenBtn", "#termMenuBtn"]

CLIP_JS = """(selectors) => {
  const vw = window.innerWidth;
  const bad = [];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (!el) { bad.push([sel, 'missing']); continue; }
    if (getComputedStyle(el).display === 'none') continue;   // deliberately hidden
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.right > vw + 1 || r.left < -1) bad.push([sel, Math.round(r.left) + '..' + Math.round(r.right)]);
  }
  return bad;
}"""


@pytest.mark.parametrize("width,height", PORTRAIT_VIEWPORTS)
def test_every_control_is_reachable_inside_the_viewport(phone_page, width, height):
    _at(phone_page, width, height)
    clipped = phone_page.evaluate(CLIP_JS, CONTROLS)
    assert clipped == [], f"controls pushed outside a {width}px viewport: {clipped}"


def test_the_overflow_menu_still_holds_the_actions_it_owns(phone_page):
    # Shrinking the row must not have been done by dropping controls.
    _at(phone_page, *PORTRAIT_VIEWPORTS[0])
    phone_page.click("#termMenuBtn")
    phone_page.wait_for_timeout(200)
    for item in ("#termKillBtn", "#inspectorBtn", "#searchToggleBtn", "#copyBtn", "#jumpBtn"):
        assert phone_page.evaluate(
            "(sel) => { const e = document.querySelector(sel);"
            "           const r = e.getBoundingClientRect();"
            "           return r.width > 0 && r.right <= window.innerWidth + 1; }", item), item
    phone_page.keyboard.press("Escape")
