"""Four real, user-reported dashboard UX bugs, all on the main dashboard's
Windows-Terminal-style tab bar (DASHBOARD_HTML) unless noted:

1. Create Session had no reachable node/host selector from the main
   dashboard -- the selector itself already existed and worked correctly
   on /dashboard/sessions (live-verified with a real headless browser
   against real production node data: options for Auto/local/dell-5530/
   m910, correctly labeled, correctly filtered by capability), the actual
   gap was that the main dashboard had no entry point to it at all.
2. The tab strip couldn't be dragged horizontally on a narrow (mobile)
   viewport -- root cause: `.tabbar-row` (a grid item of `main`) had no
   `min-width:0`, so it grew to its own unclamped content width (measured
   1599px on a 390px viewport) instead of being constrained to the
   column, which meant `#tabbar`'s own `overflow-x:auto` never had
   anything to actually clip against.
3. A tab's label carried the node/host name alongside the session name,
   duplicating information that belongs in the session's own detail view.
4. A single click/tap sometimes didn't switch the active session -- root
   cause: renderRows() called tabbarEl.replaceChildren() and rebuilt
   every tab element from scratch on every 5s poll, whether or not
   anything had actually changed; a click landing between "the browser
   dispatches the event" and "the poll tears the target node out of the
   DOM" was silently lost. Fixed by reusing the same per-session DOM node
   across polls (see tabEls/buildTabEl/updateTabEl) instead of rebuilding.

These are lightweight, dependency-free string/structural assertions
against the actual rendered HTML/JS source, matching this file's own
established convention (see test_dashboard.py) -- real interactive
verification (headless Chrome against real production node/session data:
single click reliably switching across repeated attempts, a real
touch-drag swipe scrolling the strip, scrollIntoView bringing an
off-screen tab into view, the node dropdown's real options) was done
live and is reported in the session, not re-implemented here (this
project deliberately never adds a browser-automation dependency, see
test_prompt_submission_upgrade.py::
test_no_playwright_or_browser_automation_dependency_declared).
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from terminal_mcp.dashboard import DASHBOARD_HTML, SESSIONS_ADMIN_HTML


# -- Item 1: reachable node/host selector ------------------------------------

def test_main_dashboard_has_a_reachable_new_session_entry_point():
    # A dedicated "+ New session" tab-strip button existed briefly but was
    # removed (mobile tab-strip fix, item 3): it duplicated the header's
    # own "⚙ Quản lý session" link to the exact same place and, worse, ate
    # real horizontal room from the session strip on a narrow screen. The
    # header link (present since before either fix) is the one reachable
    # entry point now -- nothing is actually less reachable than before.
    assert 'id="newSessionLinkBtn"' not in DASHBOARD_HTML
    assert 'id="sessionsAdminLink"' in DASHBOARD_HTML
    assert 'href="/dashboard/sessions"' in DASHBOARD_HTML


def test_main_dashboard_does_not_duplicate_the_create_session_form():
    # The task's own "không redesign lớn" / "không làm feature khác" --
    # the fix is a link to the existing, already-working form, never a
    # second copy of its node-selector/agent-type/submit logic.
    assert 'id="csModal"' not in DASHBOARD_HTML
    assert "loadNodesForCreateModal" not in DASHBOARD_HTML


def test_sessions_admin_create_form_still_has_its_full_node_selector():
    # The selector this bug report is actually about -- unchanged by this
    # fix, still present exactly as before (live-verified separately).
    assert 'id="csNode"' in SESSIONS_ADMIN_HTML
    assert "Auto (Recommended)" in SESSIONS_ADMIN_HTML
    assert "function nodeCapable(node, agentType)" in SESSIONS_ADMIN_HTML
    assert "function loadNodesForCreateModal()" in SESSIONS_ADMIN_HTML


# -- Item 2: mobile horizontal tab scroll ------------------------------------

def test_tabbar_row_has_the_load_bearing_min_width_zero():
    # The real root cause (see module docstring) -- without this, .tabbar's
    # own overflow-x:auto never has anything to clip against on a narrow
    # viewport. Scoped to the exact rule, not just "min-width:0 appears
    # somewhere in the file" (it legitimately appears elsewhere too, for
    # unrelated elements).
    start = DASHBOARD_HTML.index(".tabbar-row {")
    end = DASHBOARD_HTML.index("}", start)
    rule = DASHBOARD_HTML[start:end]
    assert "min-width:0" in rule


def test_tabbar_has_touch_friendly_scroll_properties():
    start = DASHBOARD_HTML.index(".tabbar {")
    end = DASHBOARD_HTML.index("}", start)
    rule = DASHBOARD_HTML[start:end]
    assert "overflow-x:auto" in rule
    assert "touch-action:pan-x" in rule
    assert "-webkit-overflow-scrolling:touch" in rule
    assert "overscroll-behavior-x:contain" in rule


def test_active_tab_scrolls_into_view_on_select():
    assert "activeRefs.tab.scrollIntoView({ block: 'nearest', inline: 'nearest' })" in DASHBOARD_HTML


# -- Item 3: tab label is session name only ----------------------------------

def test_tab_node_css_class_removed():
    assert ".tab-node {" not in DASHBOARD_HTML


def test_tab_building_code_never_reads_node_name():
    # buildTabEl/updateTabEl are the only place a tab's own DOM structure
    # is assembled now (see item 4) -- neither may reference row.node_name.
    start = DASHBOARD_HTML.index("function buildTabEl(name)")
    end = DASHBOARD_HTML.index("function renderRows(rows)")
    tab_building_js = DASHBOARD_HTML[start:end]
    assert "node_name" not in tab_building_js
    assert "nodeLabel" not in tab_building_js


def test_node_info_moved_to_the_session_detail_header_instead():
    assert "rowForNode.node_name" in DASHBOARD_HTML
    assert "· node: ${rowForNode.node_name}" in DASHBOARD_HTML
    # Only for a non-local session -- showing "· node: Local" on every
    # single local tab (the overwhelming majority today) would be exactly
    # the clutter task item 3 is asking to remove, just moved rather than
    # actually fixed.
    assert "rowForNode.node_id !== 'local'" in DASHBOARD_HTML


# -- Item 4: single click always switches the active session ----------------

def test_renderrows_no_longer_tears_down_and_rebuilds_every_tab_on_a_poll():
    start = DASHBOARD_HTML.index("function renderRows(rows) {")
    end = DASHBOARD_HTML.index("\n    }\n", start)
    render_rows_js = DASHBOARD_HTML[start:end]
    # The old bug's own signature: unconditional teardown on every call.
    assert "tabbarEl.replaceChildren()" not in render_rows_js


def test_tab_dom_nodes_are_persistent_across_polls():
    assert "const tabEls = new Map();" in DASHBOARD_HTML
    assert "function buildTabEl(name)" in DASHBOARD_HTML
    assert "function updateTabEl(refs, row)" in DASHBOARD_HTML
    # Reused (moved), not recreated, when a session that already has a
    # tab appears again in a later poll.
    assert "else { tabbarEl.append(refs.tab); }" in DASHBOARD_HTML


def test_click_and_keydown_handlers_are_rebound_on_every_update_not_just_build():
    # Guards against a subtler version of the same bug class: if these
    # were only ever set once (in buildTabEl) they'd forever close over
    # the FIRST row object a session ever had, going stale (e.g.
    # kill_reopen_ready) even though the click would still technically
    # fire once.
    start = DASHBOARD_HTML.index("function updateTabEl(refs, row)")
    end = DASHBOARD_HTML.index("function renderRows(rows)")
    update_js = DASHBOARD_HTML[start:end]
    assert "tab.onclick = activate;" in update_js
    assert "tab.onkeydown" in update_js
    assert "closeBtn.onclick" in update_js


# -- Mobile tab-strip fix (follow-up, real phone screenshot) ----------------
# Real root cause: the old mobile override set ONLY a max-width:140px on
# .tab with no min-width at all -- .tab-name (no width of its own) absorbed
# almost the entire squeeze once more than a couple of sessions existed,
# so short/medium names truncated far more aggressively than the space
# available actually required (the strip had plenty of scroll room; tabs
# were just needlessly narrow). Separately, "+ New session" and the killed-
# sessions toggle lived as fixed-width siblings INSIDE .tabbar-row, further
# shrinking the room left for the scrollable tab strip on a narrow screen.


def _mobile_media_block() -> str:
    # DASHBOARD_HTML's own single "@media (max-width:760px), (max-height:
    # 760px)" block (the app-shell/mobile-layout one -- SESSIONS_ADMIN_
    # HTML/NODES_ADMIN_HTML have their own separate, differently-worded
    # media queries this constant doesn't include at all) -- sliced by a
    # generous fixed length rather than a second marker, since this is
    # the only such block in this constant's text.
    start = DASHBOARD_HTML.index("@media (max-width:760px), (max-height:760px)")
    return DASHBOARD_HTML[start:start + 3000]


def test_mobile_tab_has_a_min_width_not_just_a_max_width():
    mobile_css = _mobile_media_block()
    start = mobile_css.index(".tab {")
    end = mobile_css.index("}", start)
    rule = mobile_css[start:end]
    assert "min-width:" in rule
    assert "max-width:140px" not in rule  # the old, too-narrow value


def test_mobile_tab_name_has_its_own_explicit_max_width():
    mobile_css = _mobile_media_block()
    start = mobile_css.index(".tab-name {")
    end = mobile_css.index("}", start)
    rule = mobile_css[start:end]
    assert "max-width:" in rule
    # No font-size change from this fix specifically -- the pre-existing
    # font-size:12.5px in this same rule is untouched, not removed.
    assert "font-size:12.5px" in rule


def test_tabbar_row_holds_only_the_tab_strip_no_action_buttons():
    start = DASHBOARD_HTML.index('<div class="tabbar-row">')
    end = DASHBOARD_HTML.index("</div>", start) + len("</div>")
    row_html = DASHBOARD_HTML[start:end]
    assert 'id="tabbar"' in row_html
    assert 'id="killedMenu"' not in row_html
    assert 'id="killedToggle"' not in row_html
    assert "New session" not in row_html


def test_killed_sessions_toggle_lives_in_the_header_now():
    header_start = DASHBOARD_HTML.index("<header>")
    header_end = DASHBOARD_HTML.index("</header>") + len("</header>")
    header_html = DASHBOARD_HTML[header_start:header_end]
    assert 'id="killedMenu"' in header_html
    assert 'id="killedToggle"' in header_html
    assert 'id="killedList"' in header_html


def test_no_duplicate_element_ids_for_the_relocated_killed_menu():
    for marker in ('id="killedMenu"', 'id="killedToggle"', 'id="killedList"'):
        assert DASHBOARD_HTML.count(marker) == 1, f"{marker} should appear exactly once"


def test_syntax_of_the_full_tab_bar_script_block(tmp_path):
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not available -- structural assertions above still ran")
    import re
    match = re.search(r"<script>(.*?)</script>", DASHBOARD_HTML, re.S)
    assert match, "expected exactly one <script> block to extract"
    script_path = tmp_path / "dashboard.js"
    script_path.write_text(match.group(1), encoding="utf-8")
    result = subprocess.run([node, "--check", str(script_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
