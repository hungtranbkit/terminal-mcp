"""Desktop: the session list is a column, and the badges tell the truth.

Three live defects this file pins down, all found by measuring the real
served page rather than reading the CSS:

1. The session strip stacked one wrapping row per node. On this fleet
   (20 sessions, 5 nodes) it took 306px at 1440x900 and 342px at
   1920x1080, so chrome above the terminal reached 488px / 587px and the
   terminal got ~36% of a desktop screen -- while most of each node row
   sat empty (the macbook group: one tab, ~1300px of nothing).

2. Every session tab showed the amber "needs attention" mark, permanently.
   `badge.hidden = true` did nothing because an author `display:inline-block`
   beats the browser's own [hidden] rule at equal specificity -- the same
   trap .task-pending-badge already carried an override for. 20 false
   alarms on a 20-session fleet is worse than no signal.

3. Collapsing a node group did nothing at all, for the same reason on
   .node-tabs: measured 306px before and after clicking a group header.
"""
from __future__ import annotations

import json
import re

import pytest

from terminal_mcp.dashboard import DASHBOARD_HTML

DESKTOP = (1440, 900)
WIDE = (1920, 1080)
MIN_DESKTOP_OUTPUT_SHARE = 0.55


def _wide_css() -> str:
    start = DASHBOARD_HTML.index("@media (min-width:1100px)")
    depth = 0
    for i in range(DASHBOARD_HTML.index("{", start), len(DASHBOARD_HTML)):
        if DASHBOARD_HTML[i] == "{":
            depth += 1
        elif DASHBOARD_HTML[i] == "}":
            depth -= 1
            if depth == 0:
                return DASHBOARD_HTML[start:i + 1]
    raise AssertionError("wide media query never closes")


# -- structure ---------------------------------------------------------------

def test_wide_screens_lay_the_session_list_out_as_a_column():
    css = _wide_css()
    assert "grid-template-columns:260px minmax(0,1fr)" in css
    assert "flex-direction:column" in css


def test_the_column_is_the_same_list_not_a_second_one():
    # One navigation surface. A second list would have to reimplement node
    # grouping, the filter and status dots, and would drift from them.
    assert DASHBOARD_HTML.count('id="tabbar"') == 1
    assert DASHBOARD_HTML.count('id="sessionFilter"') == 1


def test_node_headers_stick_while_the_column_scrolls():
    # A session name alone does not say which machine it is on, which is
    # the whole point of grouping.
    assert ".node-group-toggle { position:sticky" in _wide_css()


def test_the_inspector_does_not_grow_a_sheet_header_on_desktop():
    # It is inline here, not a sheet; a close button on it read as broken
    # and cost ~55px of terminal height.
    css = _wide_css()
    assert "#sessionsDrawer > .drawer-head { display:flex }" in css
    assert "#sessionInspector > .drawer-head { display:none }" in css


@pytest.mark.parametrize("selector", [".attn-badge", ".node-tabs"])
def test_hideable_elements_override_the_author_display_rule(selector):
    # The bug class: an author `display:` beats the UA's [hidden] rule, so
    # `el.hidden = true` silently does nothing.
    assert f"{selector}[hidden] {{ display:none }}" in DASHBOARD_HTML


def test_every_class_the_script_hides_can_actually_be_hidden():
    """The general form of the rule above, so the next one is caught here.

    Any element created with a class whose CSS sets a visible `display`,
    and which the script later toggles via `.hidden`, needs an explicit
    `[hidden]` override or the toggle is a no-op.
    """
    style = DASHBOARD_HTML[DASHBOARD_HTML.index("<style>"):DASHBOARD_HTML.index("</style>")]
    created = dict(re.findall(
        r"const (\w+) = document\.createElement\([^)]*\);\s*\1\.className = '([\w-]+)'", DASHBOARD_HTML))
    hidden_by_script = set(re.findall(r"(\w+)\.hidden\s*=", DASHBOARD_HTML))
    offenders = []
    for var, css_class in created.items():
        if var not in hidden_by_script:
            continue
        rule = re.search(rf"\.{re.escape(css_class)}\s*\{{([^}}]*)\}}", style)
        if not rule or "display:" not in rule.group(1) or "display:none" in rule.group(1):
            continue
        if f".{css_class}[hidden]" not in style:
            offenders.append(css_class)
    assert not offenders, f"classes the script hides but CSS keeps visible: {offenders}"


# -- measurement -------------------------------------------------------------

SESSION_NODES = [("m1", "local", "Local"), ("m2", "local", "Local"),
                 ("terminal-mcp-main", "local", "Local"),
                 ("win1", "dell-5530", "dell-5530 (Windows)"),
                 ("win2", "dell-5530", "dell-5530 (Windows)"),
                 ("wtest", "dell-5530", "dell-5530 (Windows)")] + [
                (f"dl{i}", "dell-linux", "dell-linux (Dell Latitude 5511)") for i in range(9)] + [
                (f"hp{i}", "hp-linux", "hp-linux (HP EliteDesk 800 G4)") for i in range(4)] + [
                ("mac1", "macbook", "macbook (macOS)")]

ROWS = [{"name": n, "node_id": i, "node_name": d,
         "state": "WAITING_INPUT" if n == "hp2" else "RUNNING",
         "attached": False, "windows": 1, "effective_read": True, "effective_input": True,
         "allowed": True, "grant": None, "kill_reopen_ready": True, "pending_count": 0,
         "resume_conversation_id": None, "session_backend": "tmux",
         "created": "2026-09-11T00:00:00Z", "activity": "2026-09-11T10:00:00Z"}
        for n, i, d in SESSION_NODES]
DETAIL = {"session": "m1", "status": {"state": "RUNNING", "reason": "idle"},
          "allowed": True, "input_allowed": True, "read_allowed": True,
          "effective_read": True, "effective_input": True, "grant": None,
          "tail": {"output": "\n".join(f"log line {i:04d}" for i in range(400))}}


def _route(route):
    url = route.request.url
    if "/dashboard/api/sessions" in url:
        return route.fulfill(status=200, content_type="application/json",
                             body=json.dumps({"sessions": ROWS}))
    if "/dashboard/api/session" in url:
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(DETAIL))
    if "/dashboard" in url and "/api/" not in url:
        return route.fulfill(status=200, content_type="text/html", body=DASHBOARD_HTML)
    return route.fulfill(status=200, content_type="application/json", body="{}")


@pytest.fixture(scope="module")
def desktop_page():
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed -- measurements skipped").sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": DESKTOP[0], "height": DESKTOP[1]})
        page.route("**/*", _route)
        page.goto("http://terminal-mcp.test/dashboard", wait_until="domcontentloaded")
        page.wait_for_selector("#tabbar .tab", timeout=20000)
        page.wait_for_timeout(600)
        page.evaluate("() => document.querySelector('#tabbar .tab').click()")
        page.wait_for_timeout(700)
        yield page
        browser.close()


def _select(page, name):
    page.evaluate(
        "(n) => { const t = [...document.querySelectorAll('#tabbar .tab')]"
        ".find(x => x.querySelector('.tab-name').textContent === n); if (t) t.click(); }", name)
    page.wait_for_timeout(500)


@pytest.mark.parametrize("width,height", [DESKTOP, WIDE])
def test_the_terminal_gets_most_of_a_desktop_screen(desktop_page, width, height):
    desktop_page.set_viewport_size({"width": width, "height": height})
    desktop_page.wait_for_timeout(350)
    share = desktop_page.evaluate(
        "() => document.querySelector('#output').getBoundingClientRect().height / window.innerHeight")
    assert share >= MIN_DESKTOP_OUTPUT_SHARE, f"terminal got {share:.0%} of {width}x{height}"


def test_only_sessions_that_need_attention_are_marked(desktop_page):
    desktop_page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    desktop_page.wait_for_timeout(300)
    visible = desktop_page.evaluate(
        "() => [...document.querySelectorAll('.tab .attn-badge')]"
        ".filter(e => getComputedStyle(e).display !== 'none').length")
    waiting = sum(1 for r in ROWS if r["state"] == "WAITING_INPUT")
    assert visible == waiting, f"{visible} tabs marked, {waiting} session actually waiting"


def test_collapsing_a_node_group_actually_collapses_it(desktop_page):
    desktop_page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    desktop_page.wait_for_timeout(300)
    before = desktop_page.evaluate("() => document.querySelector('#tabbar').scrollHeight")
    desktop_page.evaluate("() => document.querySelectorAll('.node-group-toggle')[2].click()")
    desktop_page.wait_for_timeout(400)
    after = desktop_page.evaluate("() => document.querySelector('#tabbar').scrollHeight")
    assert after < before, f"collapse changed nothing: {before} -> {after}"
    desktop_page.evaluate("() => document.querySelectorAll('.node-group-toggle')[2].click()")
    desktop_page.wait_for_timeout(400)


@pytest.mark.parametrize("name", ["m1", "hp3", "mac1", "win1"])
def test_the_selected_session_is_always_visible_in_the_column(desktop_page, name):
    # Selecting a session and not being able to see which one is selected is
    # the defect this catches; it happened for the first row of a group,
    # which sat behind its own sticky header.
    desktop_page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    _select(desktop_page, name)
    assert desktop_page.evaluate("""() => {
      const a = document.querySelector('#tabbar .tab.active');
      if (!a) return false;
      const row = a.getBoundingClientRect();
      const box = document.querySelector('#tabbar').getBoundingClientRect();
      const head = a.closest('.node-group').querySelector('.node-group-toggle');
      const cover = getComputedStyle(head).position === 'sticky'
        ? head.getBoundingClientRect().height : 0;
      return row.top >= box.top + cover - 1 && row.bottom <= box.bottom + 1;
    }"""), f"{name} is selected but not visible in the list"


def test_the_node_of_every_session_stays_identifiable(desktop_page):
    desktop_page.set_viewport_size({"width": DESKTOP[0], "height": DESKTOP[1]})
    desktop_page.wait_for_timeout(300)
    groups = desktop_page.evaluate(
        "() => [...document.querySelectorAll('.node-group-toggle')].map(g => g.title)")
    assert len(groups) == 5
    assert all(title for title in groups), "a node header with no accessible full name"


def test_no_horizontal_overflow_on_desktop(desktop_page):
    for width, height in (DESKTOP, WIDE):
        desktop_page.set_viewport_size({"width": width, "height": height})
        desktop_page.wait_for_timeout(300)
        assert not desktop_page.evaluate(
            "() => document.documentElement.scrollWidth > window.innerWidth + 1")
