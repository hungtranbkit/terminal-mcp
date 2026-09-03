from __future__ import annotations

import inspect
import re
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import dashboard as dashboard_module
from terminal_mcp.config import AppConfig, DashboardConfig, InputPolicyConfig, PermissionsConfig, load_config
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import DASHBOARD_HTML, SESSIONS_ADMIN_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp

REPO_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"


def test_dashboard_routes_are_registered(read_config):
    service = TerminalService(read_config)
    server = build_mcp(service)
    register_dashboard(server, service)

    # Only Route entries have .methods -- the web terminal's WebSocketRoute
    # (/dashboard/ws/terminal) doesn't, and isn't what this test is about.
    routes = {route.path: set(route.methods) for route in server._custom_starlette_routes
              if hasattr(route, "methods")}
    assert routes["/dashboard"] == {"GET", "HEAD"}
    assert routes["/dashboard/sessions"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/sessions"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/session"] == {"GET", "HEAD"}
    assert routes["/dashboard/api/session/input"] == {"POST"}


def test_sessions_admin_page_reachable_and_uses_the_same_api(tmp_path, tmux_session_factory):
    # /dashboard/sessions is a second VIEW of the same data/mutations, not
    # a new privilege surface -- same read guard as /dashboard, same
    # /dashboard/api/sessions fetch, same grant-read/grant-input routes.
    tmux_session_factory("test-admin-screen")
    service = TerminalService(AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    ))
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())
    r = client.get("/dashboard/sessions")
    assert r.status_code == 200
    assert "Quản lý session" in r.text
    assert "/dashboard/api/sessions" in r.text
    assert "/dashboard/api/session/grant-read" in r.text
    assert "/dashboard/api/session/grant-input" in r.text


def test_sessions_admin_page_respects_the_same_cf_access_guard():
    config = load_config(REPO_CONFIG_PATH)
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())
    r = client.get("/dashboard/sessions", follow_redirects=False)
    assert r.status_code == 403
    assert r.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_sessions_admin_html_shows_every_session_never_hides_ungranted(tmux_session_factory):
    # A never-granted session must be listed with a badge, not filtered
    # out by default -- the "chỉ hiện session chưa whitelist" checkbox is
    # an opt-in narrowing filter, not the default view.
    assert 'id="onlyGrantable"' in SESSIONS_ADMIN_HTML
    assert "Chỉ hiện session chưa whitelist" in SESSIONS_ADMIN_HTML
    assert "onlyGrantableEl.checked && !grantable(row)) return false;" in SESSIONS_ADMIN_HTML
    assert "rows.length ? 'Không có session khớp bộ lọc.'" in SESSIONS_ADMIN_HTML


def test_sessions_admin_reuses_the_permission_modal_and_bulk_bar():
    assert "function openPermModal(" in SESSIONS_ADMIN_HTML
    assert "function applyPreset(" in SESSIONS_ADMIN_HTML
    assert "function renderBulkBar(" in SESSIONS_ADMIN_HTML
    assert "🔓 Xem + gửi" in SESSIONS_ADMIN_HTML
    assert "👁 Chỉ xem" in SESSIONS_ADMIN_HTML
    assert "🔒 Thu hồi" in SESSIONS_ADMIN_HTML


def test_sessions_admin_detach_toggle_shares_the_same_localstorage_key():
    # Detach state must stay in sync with the main dashboard's own tab
    # strip within the same browser -- both pages read/write the exact
    # same key.
    assert "const DETACHED_KEY = 'terminal-mcp:detached-sessions';" in SESSIONS_ADMIN_HTML
    assert "const DETACHED_KEY = 'terminal-mcp:detached-sessions';" in DASHBOARD_HTML


def test_dashboard_has_a_nav_link_to_the_sessions_admin_screen():
    assert 'href="/dashboard/sessions"' in DASHBOARD_HTML
    assert 'id="sessionsAdminLink"' in DASHBOARD_HTML


def test_sessions_admin_uses_safe_dom_rendering():
    assert "innerHTML" not in SESSIONS_ADMIN_HTML
    assert "textContent" in SESSIONS_ADMIN_HTML


def test_dashboard_uses_safe_dom_rendering():
    assert "innerHTML" not in DASHBOARD_HTML
    assert "textContent" in DASHBOARD_HTML
    assert "Whitelisted tmux session monitor" in DASHBOARD_HTML


def test_dashboard_auto_scrolls_output_to_newest_line():
    # UX guard: the pane must land on the newest (bottom) output without the
    # viewer having to scroll manually, without reordering the rendered lines.
    assert "outputEl.scrollTop = outputEl.scrollHeight" in DASHBOARD_HTML


def test_dashboard_opening_a_session_always_starts_followed():
    # Explicit regression for "initial open lands at the latest line": opening
    # a session (or switching to a different one) always re-arms auto-follow
    # regardless of whatever scroll state was left over from a previous
    # session, so the very first render snaps to the newest line.
    assert "const switchedSession = selected !== lastRenderedSession;" in DASHBOARD_HTML
    assert "if (switchedSession) setAutoFollow(true);" in DASHBOARD_HTML
    assert "if (autoFollow) { outputEl.scrollTop = outputEl.scrollHeight; }" in DASHBOARD_HTML


def test_dashboard_output_pane_has_bounded_scroll_container():
    # Regression guard for a real layout bug: `main` previously used
    # min-height (a floor, not a cap), so the grid row was content-sized and
    # #output's scrollHeight never exceeded its clientHeight — overflow:auto
    # never engaged, scrollTop assignments were no-ops, and the whole *page*
    # scrolled instead of the terminal pane. Now `body` is a flex app-shell
    # (html/body height:100dvh + overflow:hidden) and `main` gets `flex:1;
    # min-height:0` — a bounded box that can still shrink below its
    # content's natural size — rather than a hardcoded "75px header" calc;
    # grid-template-rows:minmax(0,1fr) + min-height:0 down the chain give
    # #output a real bounded box to scroll within, verified live in a real
    # browser (see the report for this task).
    assert "height:100dvh" in DASHBOARD_HTML
    assert "overflow:hidden" in DASHBOARD_HTML
    assert "flex:1; min-height:0" in DASHBOARD_HTML
    assert "grid-template-rows:minmax(0,1fr)" in DASHBOARD_HTML
    assert "min-height:0" in DASHBOARD_HTML


def test_dashboard_mobile_terminal_bar_wraps_instead_of_clipping():
    # Regression guard for a real narrow-viewport bug: the follow/jump
    # buttons (flex:0 0 auto, non-shrinking) plus the title didn't fit a
    # phone-width bar and were clipped invisible by the panel's
    # overflow:hidden. flex-wrap lets the controls drop to their own line
    # instead of being cut off.
    assert "flex-wrap:wrap" in DASHBOARD_HTML


def test_dashboard_term_controls_group_itself_wraps_and_can_shrink():
    # Regression for a real bug found live in the supervisor pass: once
    # search/copy/font buttons brought .term-controls to 7 items, its own
    # `flex:0 0 auto` (flex-shrink:0) meant it always demanded its full
    # max-content (unwrapped) width and never actually shrank enough for
    # its own flex-wrap to engage — buttons silently overflowed past the
    # 390px shell instead of dropping to a new line. flex-shrink must be
    # allowed (min-width:0 too, the same automatic-minimum-size trap fixed
    # elsewhere in this file) for the wrap to actually take effect.
    assert "flex:1 1 auto; min-width:0" in DASHBOARD_HTML
    controls_rule = DASHBOARD_HTML.split(".term-controls {", 1)[1].split("}", 1)[0]
    assert "flex-wrap:wrap" in controls_rule


def test_dashboard_mobile_uses_smaller_terminal_font_desktop_unchanged():
    # Mobile-only: a compact ~11-12px font with tight line-height fits
    # substantially more real terminal output on a phone screen. Desktop's
    # base #output rule (14px body font, 1.45 line-height) must stay
    # untouched — the smaller sizing only appears inside the narrow-viewport
    # media query, never as a global change to the base rule.
    media_start = DASHBOARD_HTML.index("@media (max-width:760px)")
    base_css = DASHBOARD_HTML[:media_start]
    mobile_css = DASHBOARD_HTML[media_start:]

    assert "font-size" not in base_css.split("#output {", 1)[1].split("}", 1)[0]
    assert "#output { font-size:11.5px; line-height:1.3;" in mobile_css


def test_dashboard_mobile_sidebar_collapses_and_reopens_as_drawer():
    # Mobile: once a session is open, the sidebar hides by default (freeing
    # essentially the full viewport for the terminal pane) and reopens as a
    # dismissible overlay drawer via the ☰ Sessions control, without
    # resizing/displacing the terminal. Desktop/tablet keeps the sidebar
    # permanently visible — none of this is gated only inside the media
    # query text itself, so also check the toggle defaults to hidden and the
    # drawer rules are scoped under body.has-selection (mobile-only state).
    assert 'id="sessionsToggle"' in DASHBOARD_HTML
    assert 'id="sessionsPanel"' in DASHBOARD_HTML
    assert 'id="sidebarBackdrop"' in DASHBOARD_HTML
    assert ".sessions-toggle { display:none }" in DASHBOARD_HTML  # hidden by default (desktop and pre-selection mobile)
    assert "body.has-selection #sessionsPanel { display:none }" in DASHBOARD_HTML
    assert "body.has-selection.sidebar-visible #sessionsPanel" in DASHBOARD_HTML
    assert "body.has-selection.sidebar-visible #sidebarBackdrop" in DASHBOARD_HTML


def test_dashboard_sidebar_toggle_and_backdrop_wired_to_state():
    # The toggle opens/closes the drawer; picking a session always closes it
    # again (hide-by-default-once-opened); the backdrop closes without
    # changing the current selection.
    assert "sessionsToggleEl.onclick = () => { sidebarForcedOpen = !sidebarForcedOpen; updateSidebarVisibility(); };" in DASHBOARD_HTML
    assert "sidebarBackdropEl.onclick = () => { sidebarForcedOpen = false; updateSidebarVisibility(); };" in DASHBOARD_HTML
    assert "sidebarForcedOpen = false; // opening a session always closes the mobile drawer again" in DASHBOARD_HTML


def test_dashboard_mobile_sidebar_respects_safe_areas():
    assert "env(safe-area-inset-top)" in DASHBOARD_HTML
    assert "viewport-fit=cover" in DASHBOARD_HTML


def test_dashboard_mobile_compact_chrome_desktop_unaffected():
    # Requirement: substantially smaller header/subtitle/status card on
    # mobile so the terminal gets the space; desktop's base header/#summary
    # rules (22px/28px padding, 20px title) must stay untouched — the
    # smaller sizing only appears inside the narrow-viewport media query.
    media_start = DASHBOARD_HTML.index("@media (max-width:760px)")
    base_css = DASHBOARD_HTML[:media_start]
    mobile_css = DASHBOARD_HTML[media_start:]

    assert "padding:22px 28px" in base_css  # desktop header untouched
    assert "font-size:20px" in base_css  # desktop h1 untouched
    assert "header { padding:8px 12px" in mobile_css
    assert "h1 { font-size:15px }" in mobile_css
    assert "-webkit-line-clamp:2" in mobile_css  # compact status card, capped instead of growing


def test_dashboard_mobile_composer_is_compact_and_shell_pinned():
    # Requirement: more compact input composer on mobile, and it stays
    # "pinned" simply by construction — it's the last row of the same
    # bounded app-shell as everything else (see the no-outer-scroll test),
    # so there is no page scroll left that could carry it away regardless
    # of content length above it.
    media_start = DASHBOARD_HTML.index("@media (max-width:760px)")
    mobile_css = DASHBOARD_HTML[media_start:]
    assert "#inputBar { padding:8px 10px" in mobile_css
    assert "#inputBar input[type=text] { padding:7px 9px" in mobile_css


def test_dashboard_fullscreen_control_present_and_wired():
    # A dedicated fullscreen control exists, is disabled with no session
    # selected (mirroring follow/jump), and drives a body class the mobile
    # CSS keys off — plus opportunistic (never depended-on) real Fullscreen
    # API progressive enhancement, with an Escape-key and
    # fullscreenchange-event fallback so state never gets stuck out of sync.
    assert 'id="fullscreenBtn"' in DASHBOARD_HTML
    assert "fullscreenBtnEl.disabled = !selected;" in DASHBOARD_HTML
    assert "document.body.classList.toggle('fullscreen-terminal', value);" in DASHBOARD_HTML
    assert "fullscreenBtnEl.onclick = () => setFullscreen(!fullscreenTerminal);" in DASHBOARD_HTML
    assert "if (document.documentElement.requestFullscreen)" in DASHBOARD_HTML
    assert "document.addEventListener('fullscreenchange'" in DASHBOARD_HTML
    assert "event.key === 'Escape' && fullscreenTerminal" in DASHBOARD_HTML


def test_dashboard_fullscreen_hides_chrome_and_fills_terminal_on_mobile():
    # In fullscreen, mobile hides every non-terminal element and gives the
    # single remaining .detail row (.term) the full bounded height via
    # minmax(0,1fr) — the same mechanism, not a special case, so the
    # config-driven line bound / ANSI rendering / auto-follow inside
    # #output are completely untouched by any of this (pure presentation).
    assert "body.fullscreen-terminal header," in DASHBOARD_HTML
    assert "body.fullscreen-terminal #summary," in DASHBOARD_HTML
    # Regression guard: #grantBar went from always-empty (SHOW_GRANT_
    # CONTROLS=false) to real, often-visible content once the permission
    # modal work re-enabled it -- it was never in this hidden list before
    # (harmless while always empty), so re-enabling it without adding it
    # here would have made fullscreen mode visibly leak the permission bar
    # instead of showing "essentially only the terminal pane", exactly the
    # bug a real agent-browser screenshot caught before this test existed.
    assert "body.fullscreen-terminal #grantBar," in DASHBOARD_HTML
    assert "body.fullscreen-terminal #inputBar," in DASHBOARD_HTML
    assert "body.fullscreen-terminal .sessions-toggle," in DASHBOARD_HTML
    assert "body.fullscreen-terminal .detail { grid-template-rows:minmax(0,1fr) }" in DASHBOARD_HTML


def test_dashboard_exiting_fullscreen_restores_layout_on_session_loss():
    # "Exiting must restore the normal mobile layout": if the viewed
    # session disappears entirely while in fullscreen, there's nothing left
    # to show fullscreen, so it exits automatically rather than leaving a
    # blank fullscreen shell. persist:false so this forced exit never wipes
    # out the user's actual remembered fullscreen preference (see the
    # remembered-fullscreen-preference tests below).
    assert (
        "if (fullscreenTerminal) setFullscreen(false, { persist: false }); "
        "// forced exit — the remembered preference is unrelated and must survive"
        in DASHBOARD_HTML
    )


def test_dashboard_remembers_last_session_name_only():
    # Only the session identifier is ever written to localStorage — never
    # tail output, status, or anything else that could carry secrets.
    assert "localStorage.setItem(LAST_SESSION_KEY, name);" in DASHBOARD_HTML
    assert "localStorage.getItem(LAST_SESSION_KEY);" in DASHBOARD_HTML
    # Both reads and writes are guarded: localStorage can throw (private
    # browsing / disabled storage), and losing this is never fatal.
    assert "try { localStorage.setItem(LAST_SESSION_KEY, name); } catch (error)" in DASHBOARD_HTML
    assert "try { return localStorage.getItem(LAST_SESSION_KEY); } catch (error)" in DASHBOARD_HTML


def test_dashboard_auto_selects_remembered_or_first_session_once():
    # On load: remembered session if it's still readable (statically
    # whitelisted OR explicitly granted), else the first available
    # readable session — and only ever on the first load, never fighting a
    # manual selection/clear on the recurring 5s poll. Since the session
    # listing now includes restricted (not-yet-granted) sessions too (the
    # dashboard-grant feature), auto-select filters to readableRows first
    # rather than picking rows[0] blindly, so a first-time viewer is never
    # auto-opened straight into a locked placeholder.
    assert "let autoSelectAttempted = false;" in DASHBOARD_HTML
    assert "if (!autoSelectAttempted) {" in DASHBOARD_HTML
    assert "const readableRows = rows.filter(row => row.effective_read && !detachedSessions.has(row.name));" in DASHBOARD_HTML
    assert "if (!selected && readableRows.length) {" in DASHBOARD_HTML
    assert (
        "const target = (remembered && readableRows.some(row => row.name === remembered)) "
        "? remembered : readableRows[0].name;"
        in DASHBOARD_HTML
    )
    assert "selectSession(target);" in DASHBOARD_HTML


def test_dashboard_renders_ansi_via_dom_only():
    # The terminal-style renderer must build styled runs with real DOM APIs
    # (never string concatenation into markup) and must handle SGR colour
    # codes specifically, since that's exactly what tmux `capture-pane -e`
    # emits (see terminal_mcp/tmux.py).
    assert "createElement('span')" in DASHBOARD_HTML
    assert "span.textContent = run.t" in DASHBOARD_HTML
    assert "CSI_RE" in DASHBOARD_HTML and "\\x1b\\[" in DASHBOARD_HTML
    assert "innerHTML" not in DASHBOARD_HTML


def test_dashboard_has_terminal_style_presentation():
    # Dark, monospace, terminal-pane look (not the plain <pre> block it was).
    assert "--term-bg" in DASHBOARD_HTML
    assert "var(--mono)" in DASHBOARD_HTML
    assert "white-space:pre-wrap" in DASHBOARD_HTML


def test_dashboard_has_follow_pause_and_jump_controls():
    # Explicit UX controls, plus the implicit near-bottom auto-pause/resume
    # that keeps a manual scroll-up from being forcibly pulled back down.
    assert 'id="followToggle"' in DASHBOARD_HTML
    assert 'id="jumpBtn"' in DASHBOARD_HTML
    assert "Auto-follow: ON" in DASHBOARD_HTML
    assert "Auto-follow: PAUSED" in DASHBOARD_HTML
    assert "function nearBottom(el)" in DASHBOARD_HTML
    assert "outputEl.addEventListener('scroll'" in DASHBOARD_HTML
    # Jump-to-latest always re-arms follow and snaps down, regardless of
    # current scroll position.
    assert "jumpBtnEl.onclick = () => { setAutoFollow(true); outputEl.scrollTop = outputEl.scrollHeight; };" in DASHBOARD_HTML


def test_dashboard_keeps_session_status_display():
    # RUNNING / WAITING_INPUT / etc. and its reason must still be shown above
    # the terminal pane — this redesign only touches the output viewer.
    assert "data.status.state" in DASHBOARD_HTML
    assert "data.status.reason" in DASHBOARD_HTML
    assert "state-WAITING_INPUT" in DASHBOARD_HTML


def test_session_detail_tail_is_bounded_recent_and_chronological(read_config, tmux_session_factory):
    # Enough lines that the visible pane + config's default_tail_lines window
    # (see conftest.py) can't possibly cover the whole thing, so a truly
    # unbounded/full-history read would be distinguishable from a tail.
    session = tmux_session_factory(
        "test-tail-order",
        "bash -lc 'for i in $(seq -w 1 100); do echo line$i; done; sleep 30'",
    )
    client, _ = _client(read_config)
    response = client.get(f"/dashboard/api/session?name={session}")
    assert response.status_code == 200
    lines = response.json()["tail"]["output"].splitlines()

    # Exact bound: read_config's default_tail_lines is 20 (see conftest.py), and
    # 100 real lines were produced, so exactly the most recent 20 come back —
    # not "at least" or "roughly" 20, and no leftover blank padding rows.
    assert lines == [f"line{i:03d}" for i in range(81, 101)]


def test_session_detail_tail_length_is_driven_by_config_not_hardcoded(tmux_session_factory):
    # Same session, two services differing only in default_tail_lines, called via
    # terminal_tail(name) with no explicit `lines` — proves the dashboard route
    # (terminal_mcp/dashboard.py) no longer hardcodes 200 and instead flows
    # through config.default_tail_lines, the project's existing single source of
    # truth for "how many recent lines" (see config.yaml).
    session = tmux_session_factory(
        "test-tail-config",
        "bash -lc 'for i in $(seq -w 1 100); do echo line$i; done; sleep 30'",
    )
    small_client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 90, 3))
    large_client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 90, 60))

    small_output = small_client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]
    large_output = large_client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]

    # Each config's configured count comes back exactly, not just "more than".
    assert len(small_output.splitlines()) == 3
    assert len(large_output.splitlines()) == 60


def test_session_detail_tail_respects_1000_line_config_exactly(tmux_session_factory):
    # The actual value now shipped in config.yaml: 1200 real lines produced,
    # default_tail_lines=1000 configured -> exactly the most recent 1000
    # come back, oldest-first/newest-last, with no reordering or off-by-
    # some slop.
    session = tmux_session_factory(
        "test-tail-1000",
        "bash -lc 'for i in $(seq -w 1 1200); do echo line$i; done; sleep 30'",
    )
    client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 1500, 1000))
    response = client.get(f"/dashboard/api/session?name={session}")
    assert response.status_code == 200
    lines = response.json()["tail"]["output"].splitlines()

    assert len(lines) == 1000
    assert lines == [f"line{i:04d}" for i in range(201, 1201)]


def test_repo_config_yaml_default_tail_lines_is_1000():
    # Guards the actual deployed source of truth the dashboard route reads
    # (terminal.terminal_tail(name) with no explicit `lines`): config.yaml's
    # default_tail_lines, not a hardcoded route-level number.
    config = load_config(REPO_CONFIG_PATH)
    assert config.default_tail_lines == 1000


@pytest.fixture
def input_config() -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True),
        ("test-*", "agent-*"),
        50,
        20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
    )


def _client(config: AppConfig) -> tuple[TestClient, TerminalService]:
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    # Origin defaulted to match TestClient's own base_url ("http://testserver")
    # so every existing test keeps sending a same-origin request by default,
    # exactly like a real browser's fetch() would from the dashboard's own
    # page -- the CSRF/Origin check itself (dashboard.py's _origin_allowed)
    # is exercised by its own dedicated tests instead of every other test
    # in this file having to add the header itself.
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"}), service


def test_session_input_blocked_when_input_permission_disabled(read_config):
    client, _ = _client(read_config)  # read_config has terminal_input=False
    response = client.post("/dashboard/api/session/input", json={"name": "test-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "INPUT_DISABLED"


def test_session_input_blocked_for_unmatched_session(input_config):
    client, _ = _client(input_config)
    response = client.post("/dashboard/api/session/input", json={"name": "agent-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "ACCESS_DENIED"


def test_session_input_rejects_malformed_body(input_config):
    client, _ = _client(input_config)
    response = client.post("/dashboard/api/session/input", json={"name": "test-x"})
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_REQUEST"


def test_session_input_sends_text_to_allowed_session(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-input")
    client, _ = _client(input_config)
    response = client.post(
        "/dashboard/api/session/input",
        json={"name": session, "text": "echo hi", "press_enter": False},
    )
    assert response.status_code == 200
    body = response.json()
    # press_enter=False -> submit_status is TEXT_SENT (nothing to confirm),
    # not the fixed dict this asserted before submit_status existed.
    # correlation_id/delivery_state/enter_sent (P0 Part A) vary per call /
    # are new fields -- checked for presence/shape, not pinned by value.
    correlation_id = body.pop("correlation_id")
    assert isinstance(correlation_id, str) and len(correlation_id) == 32
    # submission_id is an additive alias of correlation_id (prompt-
    # submission reliability upgrade, P6) -- popped the same way, checked
    # for the same value, not pinned into the literal dict below.
    assert body.pop("submission_id") == correlation_id
    assert body == {"session": session, "sent": True, "characters": len("echo hi"),
                    "press_enter": False, "submit_status": "TEXT_SENT",
                    "delivery_state": "TEXT_SENT", "enter_sent": False,
                    "agent_type": "generic", "evidence": ["TEXT_SENT"], "activation_attempts": 0}


def test_session_input_idempotency_key_prevents_duplicate_send(input_config, tmux_session_factory):
    import uuid

    session = tmux_session_factory("test-dashboard-idem", "bash -lc 'read x; echo GOT:$x; sleep 10'")
    client, _ = _client(input_config)
    # _client() uses the real default AuditStore path (not isolated per
    # test), so the key itself must be unique per run -- a fixed literal
    # would collide with a prior run's own claim of the same key and
    # silently replay THAT run's stored result instead of exercising
    # anything about this run's session.
    key = f"dash-key-{uuid.uuid4()}"
    body = {"name": session, "text": "y", "press_enter": True, "idempotency_key": key}
    first = client.post("/dashboard/api/session/input", json=body).json()
    second = client.post("/dashboard/api/session/input", json=body).json()
    assert first == second
    time.sleep(0.3)
    detail = client.get(f"/dashboard/api/session?name={session}").json()
    assert detail["tail"]["output"].count("GOT:y") == 1


def test_session_detail_reports_input_allowed(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-detail")
    client, _ = _client(input_config)
    response = client.get(f"/dashboard/api/session?name={session}")
    assert response.status_code == 200
    assert response.json()["input_allowed"] is True


def test_session_detail_preserves_real_ansi_color(read_config, tmux_session_factory):
    # The route now requests ansi=True: real color output from the pane must
    # reach the JSON response as raw SGR escape sequences for the renderer
    # to parse, not stripped plain text.
    session = tmux_session_factory(
        "test-tail-ansi-route",
        "bash -lc 'printf \"\\x1b[32mBUILD OK\\x1b[0m\\n\"; sleep 30'",
    )
    client, _ = _client(read_config)
    output = client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]
    assert "\x1b[" in output
    assert "BUILD OK" in output


def test_session_detail_redacts_secret_even_when_colored(read_config, tmux_session_factory):
    # Note: the session name deliberately avoids "secret"/"password"/etc. —
    # those trigger a separate, stricter sensitive-session-name whitelist rule
    # (see terminal_mcp/permissions.py) unrelated to what this test covers.
    session = tmux_session_factory(
        "test-tail-ansi-key",
        "bash -lc 'printf \"OPENAI_API_KEY=sk-\\x1b[31mlivesecretvalue1234567890\\x1b[0m\\n\"; sleep 30'",
    )
    client, _ = _client(read_config)
    output = client.get(f"/dashboard/api/session?name={session}").json()["tail"]["output"]
    assert "livesecretvalue1234567890" not in output
    assert "<REDACTED>" in output


def test_session_detail_ansi_still_enforces_whitelist():
    # Security regression: the ansi=True path is a rendering detail, not a
    # second, less-guarded read path — an unlisted, ungranted session is
    # still denied (READ_RESTRICTED -- the dashboard-grant feature's more
    # precise error than the old bare ACCESS_DENIED, but still a clean 403
    # with zero content in the response).
    client, _ = _client(AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20))
    response = client.get("/dashboard/api/session?name=private-ansi")
    assert response.status_code == 403
    body = response.json()
    assert body["error"] == "READ_RESTRICTED"
    assert "tail" not in body and "status" not in body


def test_session_detail_ansi_read_only_input_route_unaffected(read_config):
    # The output viewer stays read-only by default: with terminal_input
    # disabled (read_config), the separate input route still refuses to send,
    # unchanged by anything in this redesign.
    client, _ = _client(read_config)
    response = client.post("/dashboard/api/session/input", json={"name": "test-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "INPUT_DISABLED"


# ---------------------------------------------------------------------------
# Phase 3: attention state + sorting, mobile font controls, search + copy
# ---------------------------------------------------------------------------


def test_sessions_route_includes_state_and_sorts_attention_first(read_config, tmux_session_factory):
    # Real classify_status() output (via terminal_status), not a new/looser
    # heuristic. A session sitting at a live "[y/N]"-style prompt must sort
    # ahead of everything else, regardless of activity recency.
    tmux_session_factory("test-attn-idle", "bash -lc 'sleep 20'")
    tmux_session_factory(
        "test-attn-waiting",
        "bash -lc 'echo \"Do you want to continue? [y/N]\"; read x; sleep 20'",
    )
    time.sleep(0.4)
    client, _ = _client(read_config)
    response = client.get("/dashboard/api/sessions")
    assert response.status_code == 200
    rows = response.json()["sessions"]
    by_name = {row["name"]: row for row in rows}

    assert by_name["test-attn-waiting"]["state"] == "WAITING_INPUT"
    names = [row["name"] for row in rows]
    assert names.index("test-attn-waiting") < names.index("test-attn-idle")


def test_sessions_route_preserves_unknown_state_not_misclassified(read_config, tmux_session_factory):
    # A freshly created, silent shell has no high-confidence evidence either
    # way — classify_status() must still say UNKNOWN, not WAITING_INPUT, and
    # the sessions route must carry that through unchanged (no new/looser
    # interpretation invented for this feature).
    tmux_session_factory("test-attn-unknown", "bash -lc 'sleep 20'")
    time.sleep(0.4)
    client, _ = _client(read_config)
    rows = client.get("/dashboard/api/sessions").json()["sessions"]
    row = next(r for r in rows if r["name"] == "test-attn-unknown")
    assert row["state"] in ("UNKNOWN", "IDLE")  # never WAITING_INPUT for silent, ambiguous output
    assert row["state"] != "WAITING_INPUT"


def test_sessions_route_deterministic_fallback_and_activity_order(read_config, tmux_session_factory):
    # No attention-needed sessions: ordering falls back to most-recent-
    # activity, and ties/absence of a clear signal never drop a session.
    tmux_session_factory("test-attn-a", "bash -lc 'sleep 20'")
    time.sleep(0.05)
    tmux_session_factory("test-attn-b", "bash -lc 'sleep 20'")
    time.sleep(0.4)
    client, _ = _client(read_config)
    rows = client.get("/dashboard/api/sessions").json()["sessions"]
    names = {row["name"] for row in rows}
    assert {"test-attn-a", "test-attn-b"} <= names  # no allowed session ever hidden


def test_sessions_route_fans_out_correctly_across_many_sessions(read_config, tmux_session_factory):
    # P1 items #4/#5: the per-row terminal_status() calls now fire
    # concurrently (anyio task group) instead of serially -- with enough
    # rows to make a fan-out bug (a race writing into the wrong row, a
    # dropped row, a state lost/overwritten) plausible if the concurrent
    # version were wrong, every row's state must still exactly match what
    # a direct, single terminal_status() call for that same session reports.
    names = [f"test-fanout-{i}" for i in range(10)]
    for name in names:
        tmux_session_factory(name, "bash -lc 'sleep 20'")
    time.sleep(0.4)
    client, service = _client(read_config)
    rows = client.get("/dashboard/api/sessions").json()["sessions"]
    by_name = {row["name"]: row for row in rows}
    assert set(names) <= set(by_name)
    for name in names:
        direct_state = service.terminal_status(name)["state"]
        assert by_name[name]["state"] == direct_state


def test_concurrent_dashboard_requests_all_succeed(read_config, tmux_session_factory):
    # A crude but real proof that the async-offload change (P1 item #4)
    # didn't introduce a race/deadlock: fire a batch of concurrent, mixed
    # GET requests (sessions list + per-session detail) at the same running
    # server and confirm every single one comes back successfully. Not a
    # timing assertion (those are flaky) -- a correctness one.
    import concurrent.futures

    session = tmux_session_factory("test-concurrent-dash", "bash -lc 'sleep 20'")
    time.sleep(0.3)
    client, _ = _client(read_config)

    def _get(path: str) -> int:
        return client.get(path).status_code

    paths = ["/dashboard/api/sessions", f"/dashboard/api/session?name={session}"] * 8
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(_get, paths))
    assert all(code == 200 for code in statuses)


def test_sessions_route_lists_unwhitelisted_sessions_as_restricted_not_hidden(read_config, tmux_session_factory):
    # Dashboard session-discovery feature: a non-whitelisted session now
    # APPEARS in the list (name/attached/windows/activity are tmux
    # metadata, not pane content -- safe to show), but is never treated as
    # readable: allowed/effective_read are both False, state is the
    # RESTRICTED sentinel (terminal_status is never even called for it),
    # and it carries no grant. The real security boundary -- no CONTENT
    # ever reaches an ungranted caller -- is covered by
    # test_session_detail_ansi_still_enforces_whitelist and the dedicated
    # grant tests below, not by hiding the row.
    tmux_session_factory("private-attn-check", "bash -lc 'echo Do you want to continue? [y/N]; sleep 20'")
    time.sleep(0.4)
    client, _ = _client(read_config)
    rows = client.get("/dashboard/api/sessions").json()["sessions"]
    row = next((r for r in rows if r["name"] == "private-attn-check"), None)
    assert row is not None  # listed, not hidden
    assert row["allowed"] is False
    assert row["effective_read"] is False
    assert row["effective_input"] is False
    assert row["state"] == "RESTRICTED"
    assert row["grant"] == {"read_enabled": False, "input_enabled": False}
    # No pane content anywhere in the listing response for it.
    assert "last_output" not in row and "output" not in row


def test_dashboard_attention_badge_and_sort_wiring_present():
    assert "row.state === 'WAITING_INPUT'" in DASHBOARD_HTML
    assert "class=\"attn-badge\"" not in DASHBOARD_HTML  # built via DOM, not a literal HTML string
    assert "badge.className = 'attn-badge'" in DASHBOARD_HTML
    assert "needs-attention" in DASHBOARD_HTML
    assert "Rows already arrive sorted attention-first" in DASHBOARD_HTML  # no client-side re-sort


def test_dashboard_font_controls_present_bounded_and_persisted():
    assert 'id="fontDecBtn"' in DASHBOARD_HTML
    assert 'id="fontIncBtn"' in DASHBOARD_HTML
    assert "FONT_SIZE_MIN = 9, FONT_SIZE_MAX = 16" in DASHBOARD_HTML
    assert "outputEl.style.fontSize = outputFontSize + 'px';" in DASHBOARD_HTML
    # Only the page font, never anything else (body/header/etc. untouched by this control).
    assert "document.body.style.fontSize" not in DASHBOARD_HTML
    assert "localStorage.setItem(FONT_SIZE_KEY, String(outputFontSize));" in DASHBOARD_HTML
    assert "localStorage.getItem(FONT_SIZE_KEY)" in DASHBOARD_HTML
    assert "try {" in DASHBOARD_HTML.split("FONT_SIZE_KEY", 2)[1]  # storage reads/writes are guarded


def test_dashboard_search_controls_present_and_case_insensitive():
    assert 'id="searchToggleBtn"' in DASHBOARD_HTML
    assert 'id="searchInput"' in DASHBOARD_HTML
    assert 'id="searchPrevBtn"' in DASHBOARD_HTML
    assert 'id="searchNextBtn"' in DASHBOARD_HTML
    assert "haystack = text.toLowerCase();" in DASHBOARD_HTML
    assert "needle = query.toLowerCase();" in DASHBOARD_HTML
    # Plain literal indexOf-based scan, never a user-supplied regex.
    assert "new RegExp(" not in DASHBOARD_HTML
    assert "haystack.indexOf(needle, from)" in DASHBOARD_HTML


def test_dashboard_search_row_hidden_attribute_actually_wins():
    # Regression for a real bug found live: `.term-search { display:flex }`
    # and the browser's own `[hidden] { display:none }` share specificity,
    # and the page rule (cascading after the UA stylesheet) was winning —
    # the search row stayed visibly open even with hidden=true. An explicit
    # `.term-search[hidden]` rule is required to make `hidden` win.
    assert ".term-search[hidden] { display:none }" in DASHBOARD_HTML


def test_dashboard_search_scrolls_current_match_without_breaking_follow():
    assert "match.span.scrollIntoView({ block: 'center' });" in DASHBOARD_HTML
    # No special-casing needed: scrollIntoView reuses the exact same
    # near-bottom pause/resume path already covering manual scroll-up.
    assert "if (nearBottom(outputEl)) { if (!autoFollow) setAutoFollow(true); }" in DASHBOARD_HTML


def test_dashboard_search_closes_on_session_switch():
    assert "closeSearch(); // a search from a different session's content wouldn't make sense to keep open" in DASHBOARD_HTML


def test_dashboard_copy_uses_plain_text_never_ansi_markup():
    assert 'id="copyBtn"' in DASHBOARD_HTML
    assert "navigator.clipboard.writeText(text)" in DASHBOARD_HTML
    assert "outputEl.textContent" in DASHBOARD_HTML  # plain text only, never innerHTML/markup
    # Selection must actually belong to the output pane, else fall back
    # instead of silently doing nothing or copying an unrelated selection.
    assert "outputEl.contains(window.getSelection().anchorNode)" in DASHBOARD_HTML
    assert "selectionInOutput ? selectionText : outputEl.textContent" in DASHBOARD_HTML


def test_dashboard_search_and_copy_do_not_persist_content():
    # Only four lightweight UI preferences are ever written to localStorage
    # anywhere in this file: the last-viewed session *name*, the font-size
    # number, the fullscreen on/off flag, and the set of session *names*
    # detached from the tab bar — never search terms, copied text, or any
    # rendered output/session content.
    keys = set(re.findall(r"localStorage\.(?:setItem|getItem)\(([A-Za-z_]+)", DASHBOARD_HTML))
    assert keys == {"LAST_SESSION_KEY", "FONT_SIZE_KEY", "FULLSCREEN_KEY", "DETACHED_KEY"}


def test_dashboard_new_controls_disabled_without_a_selected_session():
    assert "searchToggleBtnEl.disabled = !selected;" in DASHBOARD_HTML
    assert "copyBtnEl.disabled = !selected;" in DASHBOARD_HTML


def test_dashboard_fullscreen_and_mobile_layout_regressions_still_hold():
    # This phase must not touch the fixed app-shell / fullscreen mechanics
    # from the previous commits.
    assert "height:100dvh" in DASHBOARD_HTML
    assert "body.fullscreen-terminal header," in DASHBOARD_HTML
    assert (
        "const target = (remembered && readableRows.some(row => row.name === remembered)) "
        "? remembered : readableRows[0].name;"
        in DASHBOARD_HTML
    )


# ---------------------------------------------------------------------------
# Supervisor batch: connection health, remembered fullscreen, mobile input polish
# ---------------------------------------------------------------------------


def test_dashboard_health_indicator_reflects_connection_and_marks_output_stale():
    assert 'id="liveBadge"' in DASHBOARD_HTML
    assert "function setConnectionState(ok)" in DASHBOARD_HTML
    assert "'● RECONNECTING…'" in DASHBOARD_HTML
    assert "'● OFFLINE'" in DASHBOARD_HTML
    assert "outputEl.classList.add('stale')" in DASHBOARD_HTML
    assert "outputEl.classList.remove('stale')" in DASHBOARD_HTML
    assert "#output.stale { opacity:.55 }" in DASHBOARD_HTML


def test_dashboard_health_indicator_never_clears_last_rendered_output():
    # "Keep last rendered output visible but clearly marked stale" — the
    # failure branch must never wipe outputEl's content, only dim it via the
    # CSS class above; recovery is automatic (the existing 5s poll loop is
    # already the retry mechanism, no separate timer needed).
    refresh_fn = DASHBOARD_HTML.split("async function refresh() {", 1)[1].split("\n    refresh();", 1)[0]
    assert "outputEl.replaceChildren()" not in refresh_fn
    assert "outputEl.textContent = ''" not in refresh_fn
    # A genuine network/server failure (not an auth redirect -- see
    # AuthRequiredError below) must still land on setConnectionState(false).
    assert "} else { setConnectionState(false); }" in DASHBOARD_HTML


def test_dashboard_health_indicator_no_new_backend_route():
    # Purely reactive to the two fetches loadSessions/loadDetail already
    # make — no new endpoint, no extra polling path added for the health
    # indicator specifically (the other fetch()s below belong to the
    # separate Supervisor Loop v1/v2 features' summary/ack/pause calls, the
    # dashboard-grant feature's single shared postGrant() helper, whose one
    # fetch() call serves both grant-read and grant-input via a `path`
    # parameter rather than a separate literal call site for each, and the
    # shared fetchJSON() wrapper loadSessions/loadDetail now both go
    # through -- one more literal "fetch(" substring for that wrapper's own
    # internal call, not a new call SITE or endpoint).
    assert DASHBOARD_HTML.count("fetch(") == 9  # sessions, session detail, session/input, postGrant, supervisor, supervisor/ack, supervisor2, supervisor2/pause, fetchJSON's own internal fetch()


def test_dashboard_auth_required_distinguished_from_offline():
    # URGENT incident fix: an expired Cloudflare Access browser session
    # (fetch() transparently following a redirect to Access's own login
    # page, landing here as a normal 200 HTML response, not a network
    # error) must be reported as a sign-in problem, never mislabeled as a
    # server/tunnel outage -- but ONLY on positive evidence of that
    # specific redirect (never merely "response wasn't JSON", which a real
    # 502/503/proxy-error page also is).
    assert "class AuthRequiredError extends Error {}" in DASHBOARD_HTML
    assert "async function fetchJSON(url, options)" in DASHBOARD_HTML
    # Exact-hostname or proper-subdomain match only -- a naive
    # `.endsWith('cloudflareaccess.com')` would also match a lookalike host
    # like "notcloudflareaccess.com".
    assert "host === 'cloudflareaccess.com' || host.endsWith('.cloudflareaccess.com')" in DASHBOARD_HTML
    assert "response.status === 401" in DASHBOARD_HTML
    assert "function setAuthRequiredState()" in DASHBOARD_HTML
    assert "SIGN-IN REQUIRED" in DASHBOARD_HTML
    assert "if (error instanceof AuthRequiredError) { setAuthRequiredState(); }" in DASHBOARD_HTML
    # A non-JSON body still falls through to a plain Error -- reported as
    # OFFLINE (setConnectionState(false)), same as a real outage always has
    # been. Status code ALONE must never trigger this: this app's own
    # routes legitimately answer denials (403 READ_RESTRICTED, etc) with a
    # genuine JSON body, which every caller already reads via `data.error`
    # -- treating a non-2xx JSON response as a hard failure here would
    # break that entirely (a real regression caught live before shipping).
    assert "if (!contentType.includes('application/json'))" in DASHBOARD_HTML
    assert "!response.ok" not in DASHBOARD_HTML
    # Both loadSessions and loadDetail -- the two fetches every refresh()
    # cycle depends on for the health badge -- go through the wrapper.
    assert "await fetchJSON('/dashboard/api/sessions'" in DASHBOARD_HTML
    assert "await fetchJSON(`/dashboard/api/session?name=" in DASHBOARD_HTML


def test_dashboard_grant_controls_have_an_obvious_entry_point():
    # UX fix: a new, not-yet-granted session must have an obvious, direct
    # path to being granted from the dashboard -- the SHOW_GRANT_CONTROLS
    # kill switch from the earlier mobile-overlap hotfix is gone entirely;
    # granting is reachable from the session row (lock icon), its tab, its
    # own open card (#grantBar), and the bulk-select bar, all opening the
    # same #permModal.
    assert "SHOW_GRANT_CONTROLS" not in DASHBOARD_HTML
    assert "function openPermModal(" in DASHBOARD_HTML
    assert "function renderPermModalBody(" in DASHBOARD_HTML
    assert "function applyPreset(" in DASHBOARD_HTML
    # Exactly two ideas are ever exposed to the operator -- never the raw
    # allowed/whitelist/read_granted/input_granted vocabulary.
    assert "🔓 Xem + gửi" in DASHBOARD_HTML
    assert "👁 Chỉ xem" in DASHBOARD_HTML
    assert "🔒 Thu hồi" in DASHBOARD_HTML
    # A never-granted session is still listed, with a badge, not hidden.
    assert "🔒 Chưa cấp quyền" in DASHBOARD_HTML
    # Per-row lock icon + checkbox, gated on `grantable()` (never for a
    # statically-whitelisted row, which has nothing to grant/revoke).
    assert "function grantable(row) { return !row.allowed; }" in DASHBOARD_HTML
    assert "className = 'perm-btn'" in DASHBOARD_HTML
    assert "className = 'sess-check'" in DASHBOARD_HTML
    # Bulk-select bar applies a preset directly to every checked session.
    assert "function renderBulkBar()" in DASHBOARD_HTML
    assert "const bulkSelected = new Set();" in DASHBOARD_HTML
    # Backend this UI drives is unchanged -- grant_session_read/
    # grant_session_input are still called exactly as before, from the
    # same POST routes. These live in dashboard.py's Python source (the
    # route registration), not in DASHBOARD_HTML, so check the module
    # source directly.
    module_source = inspect.getsource(dashboard_module)
    assert "terminal.grant_session_read(name, enabled" in module_source
    assert "terminal.grant_session_input(name, enabled" in module_source
    assert '"/dashboard/api/session/grant-read", methods=["POST"]' in module_source
    assert '"/dashboard/api/session/grant-input", methods=["POST"]' in module_source


def test_dashboard_grantbar_hidden_attribute_actually_hides_it():
    # A plain `#grantBar { display:flex; ... }` rule (needed for its own
    # visible layout when shown) would otherwise outrank the browser's
    # default `[hidden] { display:none }` UA rule by specificity, leaving
    # an "empty but still visually present" bar even while .hidden is set
    # -- exactly the kind of subtle bug this hotfix exists to close. Same
    # class of fix applied to #sessionTabs for the (rarer) all-detached
    # case too.
    assert "#grantBar[hidden] { display:none }" in DASHBOARD_HTML
    assert "#sessionTabs[hidden] { display:none }" in DASHBOARD_HTML


def test_dashboard_detail_grid_rows_match_children_one_to_one():
    # DOM/CSS layout contract, the direct cause of the real overlap this
    # hotfix fixes: .detail's grid-template-rows previously listed 4
    # tracks for what had grown to 6 direct children (#sessionTabs,
    # #summary, #grantBar, .term, #inputNote, #inputBar) -- auto-placement
    # silently handed the one flexible (minmax(0,1fr)) track to #summary
    # instead of .term (the actual output viewport), which then let
    # #summary's content overflow into the rows below it. Two invariants
    # are asserted here so this can't silently regress again: the
    # explicit row-track COUNT must equal 6, and every one of the 6
    # children must carry its own explicit `grid-row:N` (not rely on
    # sequential auto-placement, which reassigns everyone once any one of
    # them toggles display:none -- e.g. the now-permanently-hidden
    # #grantBar above).
    detail_rule = re.search(r"\.detail \{ display:grid; grid-template-rows:([^;]+);", DASHBOARD_HTML)
    assert detail_rule is not None
    tracks = detail_rule.group(1).split()
    assert len(tracks) == 6
    assert tracks == ["auto", "auto", "auto", "minmax(0,1fr)", "auto", "auto"]  # .term (position 4) is the ONE growing track
    expected_grid_rows = {
        "#sessionTabs": 1, "#summary": 2, "#grantBar": 3, ".term": 4, "#inputNote": 5, "#inputBar": 6,
    }
    for selector, row in expected_grid_rows.items():
        assert f"grid-row:{row};" in DASHBOARD_HTML or f"grid-row:{row} " in DASHBOARD_HTML, \
            f"{selector} must explicitly claim grid-row {row}"


def test_dashboard_fullscreen_preference_persisted_and_restored_once():
    assert "const FULLSCREEN_KEY = 'terminal-mcp:fullscreen';" in DASHBOARD_HTML
    assert "function recalledFullscreen()" in DASHBOARD_HTML
    assert "localStorage.setItem(FULLSCREEN_KEY, value ? '1' : '0');" in DASHBOARD_HTML
    assert "localStorage.getItem(FULLSCREEN_KEY) === '1';" in DASHBOARD_HTML
    assert "let fullscreenRestoreAttempted = false;" in DASHBOARD_HTML
    assert "if (!fullscreenRestoreAttempted) {" in DASHBOARD_HTML
    # Restoration only happens after a session is already selected/rendered
    # (and therefore already scrolled to its latest line), never before —
    # so it can never skip or race the initial auto-scroll-to-latest. It is
    # the very next statement after the fullscreenRestoreAttempted guard.
    assert (
        "if (!fullscreenRestoreAttempted) {\n"
        "          fullscreenRestoreAttempted = true;\n"
        "          if (selected && recalledFullscreen()) setFullscreen(true, { persist: false });"
        in DASHBOARD_HTML
    )


def test_dashboard_auto_follow_preference_deliberately_not_persisted():
    # Unlike session/font/fullscreen, auto-follow is intentionally NOT
    # persisted across reloads: restoring a remembered "paused" state would
    # skip the initial scroll-to-latest on the very next open, which is an
    # explicit, repeatedly-reiterated UX requirement this project keeps. The
    # localStorage key inventory (see test_dashboard_search_and_copy_do_not_
    # persist_content) has exactly three keys — no auto-follow key exists,
    # and the function that flips the toggle never touches storage at all.
    assert "AUTO_FOLLOW_KEY" not in DASHBOARD_HTML
    set_auto_follow_fn = DASHBOARD_HTML.split("function setAutoFollow(value) {", 1)[1].split("}", 1)[0]
    assert "localStorage" not in set_auto_follow_fn


def test_dashboard_mobile_text_inputs_avoid_ios_safari_zoom():
    # iOS Safari auto-zooms the whole page (breaking the fixed app-shell)
    # when a focused text input computes to under 16px. Padding can still
    # shrink for compactness; font-size must not, on either input.
    media_start = DASHBOARD_HTML.index("@media (max-width:760px)")
    mobile_css = DASHBOARD_HTML[media_start:]
    assert ".term-search input[type=text] { padding:4px 6px; font-size:16px }" in mobile_css
    assert "#inputBar input[type=text] { padding:7px 9px; font-size:16px }" in mobile_css


def test_dashboard_viewport_resizes_for_onscreen_keyboard():
    assert "interactive-widget=resizes-content" in DASHBOARD_HTML
    assert "viewport-fit=cover" in DASHBOARD_HTML  # unrelated safe-area support must survive alongside it


def test_dashboard_mobile_batch_no_unexpected_route_changes(read_config):
    # This whole batch (health indicator, remembered fullscreen, mobile input
    # polish) is presentation-only: the same routes as before that batch,
    # with the same methods, still registered — no new endpoint was added
    # for it. (The two /dashboard/api/supervisor* routes, the session_
    # lifecycle create/detach/delete routes, and the web terminal's own
    # routes below belong to later, separate features, not this batch.)
    service = TerminalService(read_config)
    server = build_mcp(service)
    register_dashboard(server, service)
    routes = {route.path: set(route.methods) for route in server._custom_starlette_routes
              if hasattr(route, "methods")}
    assert routes == {
        "/dashboard": {"GET", "HEAD"},
        "/dashboard/sessions": {"GET", "HEAD"},
        "/dashboard/api/sessions": {"GET", "HEAD"},
        "/dashboard/api/session": {"GET", "HEAD"},
        "/dashboard/api/session/input": {"POST"},
        "/dashboard/api/session/grant-read": {"POST"},
        "/dashboard/api/session/grant-input": {"POST"},
        "/dashboard/api/session/create": {"POST"},
        "/dashboard/api/session/detach": {"POST"},
        "/dashboard/api/session/delete": {"POST"},
        "/dashboard/assets/{filename}": {"GET", "HEAD"},
        "/dashboard/terminal": {"GET", "HEAD"},
        "/dashboard/api/supervisor": {"GET", "HEAD"},
        "/dashboard/api/supervisor/ack": {"POST"},
        "/dashboard/api/supervisor2": {"GET", "HEAD"},
        "/dashboard/api/supervisor2/pause": {"POST"},
    }
    # The web terminal's WebSocket route is registered too, just outside
    # this HTTP-methods-only dict (WebSocketRoute has no .methods).
    ws_paths = {route.path for route in server._custom_starlette_routes if not hasattr(route, "methods")}
    assert ws_paths == {"/dashboard/ws/terminal"}


def test_dashboard_supervisor_batch_input_route_still_gated(read_config):
    # And the input route itself still refuses exactly as before —
    # terminal_input/whitelist/input_policy gating is untouched by any of
    # this batch's presentation-only changes.
    client, _ = _client(read_config)  # read_config has terminal_input disabled
    response = client.post("/dashboard/api/session/input", json={"name": "test-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "INPUT_DISABLED"


# ---------------------------------------------------------------------------
# Near-edge mobile layout + fullscreen/state persistence across orientation
# ---------------------------------------------------------------------------


def test_dashboard_mobile_media_query_matches_landscape_phones_too():
    # A phone rotated to landscape can exceed 760px of *width* (e.g. 852px
    # on an iPhone 15 Pro) while its height stays well under 760px. The
    # mobile breakpoint must match on either axis, or the whole mobile
    # block -- including every body.fullscreen-terminal rule -- silently
    # stops applying mid-rotation even though the JS fullscreen state
    # (and selected session, auto-follow, font size) never changed.
    assert "@media (max-width:760px), (max-height:760px)" in DASHBOARD_HTML
    # Only the one, combined mobile query should exist -- a second,
    # narrower @media block would be exactly the kind of orientation trap
    # this fixes if introduced by accident.
    assert DASHBOARD_HTML.count("@media") == 1


def test_dashboard_fullscreen_rules_live_inside_the_orientation_safe_query():
    # body.fullscreen-terminal's chrome-hiding rules must be inside the
    # combined (width OR height) query, not a width-only one, or fullscreen
    # visually "exits" (header/sidebar reappear) on rotation to landscape.
    media_start = DASHBOARD_HTML.index("@media (max-width:760px), (max-height:760px)")
    mobile_css = DASHBOARD_HTML[media_start:]
    assert "body.fullscreen-terminal header," in mobile_css
    assert "body.fullscreen-terminal main { padding:0; gap:0 }" in mobile_css


def test_dashboard_selected_session_runs_near_edge_to_edge_on_mobile():
    media_start = DASHBOARD_HTML.index("@media (max-width:760px), (max-height:760px)")
    mobile_css = DASHBOARD_HTML[media_start:]
    assert "body.has-selection main { gap:6px;" in mobile_css
    # Still safe-area aware -- never flush under a notch/home-indicator.
    assert "env(safe-area-inset-left)" in mobile_css.split("body.has-selection main", 1)[1][:200]
    # The actual output area's own font-size/padding is untouched by this
    # change -- only the empty margin around the panel shrinks.
    assert "#output { font-size:11.5px; line-height:1.3; padding:10px 12px }" in mobile_css


def test_dashboard_orientation_resize_never_touches_persisted_ui_state():
    # Rotating/resizing must re-snap scroll when auto-follow is on (the
    # same pattern already used for the fullscreen-toggle transition) but
    # must never write to any of the localStorage-backed UI state keys --
    # fullscreen/session/font-size all stay exactly as they were.
    assert "window.addEventListener('resize', () => {" in DASHBOARD_HTML
    assert "window.addEventListener('orientationchange', () => {" in DASHBOARD_HTML
    resize_start = DASHBOARD_HTML.index("window.addEventListener('resize'")
    orientation_start = DASHBOARD_HTML.index("window.addEventListener('orientationchange'")
    resize_body = DASHBOARD_HTML[resize_start:resize_start + 200]
    orientation_body = DASHBOARD_HTML[orientation_start:orientation_start + 200]
    for body in (resize_body, orientation_body):
        assert "outputEl.scrollTop = outputEl.scrollHeight" in body
        assert "localStorage.setItem" not in body


# ---------------------------------------------------------------------------
# P0-1: dashboard.mutations_enabled -- an explicit boundary independent of
# terminal_input/input_policy, in front of every dashboard POST route
# ---------------------------------------------------------------------------


@pytest.fixture
def read_only_dashboard_config() -> AppConfig:
    from terminal_mcp.config import DashboardConfig

    return AppConfig(
        PermissionsConfig(True, True),  # terminal_input itself stays ON --
        ("test-*", "agent-*"), 50, 20,  # this is a dashboard-specific gate,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),  # not a
        dashboard=DashboardConfig(mutations_enabled=False),        # replacement for it.
    )


def test_dashboard_mutations_disabled_blocks_session_input_even_with_terminal_input_on(
    read_only_dashboard_config,
):
    client, _ = _client(read_only_dashboard_config)
    response = client.post("/dashboard/api/session/input", json={"name": "test-x", "text": "hi"})
    assert response.status_code == 403
    assert response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


def test_dashboard_mutations_disabled_blocks_supervisor_ack(read_only_dashboard_config):
    client, _ = _client(read_only_dashboard_config)
    response = client.post("/dashboard/api/supervisor/ack", json={"id": 1})
    assert response.status_code == 403
    assert response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


def test_dashboard_mutations_disabled_blocks_supervisor2_pause(read_only_dashboard_config):
    client, _ = _client(read_only_dashboard_config)
    response = client.post("/dashboard/api/supervisor2/pause", json={"target": "x", "kind": "session"})
    assert response.status_code == 403
    assert response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


def test_dashboard_mutations_disabled_read_routes_still_fully_work(read_only_dashboard_config):
    # The core P0-1 claim: read routes are available *independently* of
    # the mutation gate -- disabling mutations must never take reads down.
    client, _ = _client(read_only_dashboard_config)
    assert client.get("/dashboard").status_code == 200
    assert client.get("/dashboard/api/sessions").status_code == 200
    # 404 (SESSION_NOT_FOUND, "test-x" doesn't exist) is the read route
    # working normally -- the point being tested is that it is NOT 403
    # DASHBOARD_MUTATIONS_DISABLED.
    session_response = client.get("/dashboard/api/session", params={"name": "test-x"})
    assert session_response.status_code == 404
    assert session_response.json().get("error") != "DASHBOARD_MUTATIONS_DISABLED"
    assert client.get("/dashboard/api/supervisor").status_code == 200
    assert client.get("/dashboard/api/supervisor2").status_code == 200


def test_dashboard_mutations_enabled_by_default_preserves_existing_ui(input_config):
    # Default (no `dashboard:` section in config at all) must be
    # mutations_enabled=True -- this flag is opt-in-to-restrict, not
    # opt-in-to-allow, so existing deployments/UI are unaffected.
    assert input_config.dashboard.mutations_enabled is True
    client, _ = _client(input_config)
    response = client.post("/dashboard/api/session/input", json={"name": "test-x", "text": "hi"})
    # Still reaches the real guard chain (ACCESS_DENIED here, since
    # "test-x" isn't a real tmux session) rather than being blocked by the
    # mutation gate itself.
    assert response.status_code in (403, 404)
    assert response.json()["error"] != "DASHBOARD_MUTATIONS_DISABLED"


def test_load_config_parses_dashboard_mutations_enabled(tmp_path):
    from terminal_mcp.config import load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "permissions:\n  terminal_read: true\n  terminal_input: true\n"
        "allowed_session_patterns: ['test-*']\n"
        "dashboard:\n  mutations_enabled: false\n"
    )
    config = load_config(config_path)
    assert config.dashboard.mutations_enabled is False


def test_load_config_defaults_dashboard_mutations_enabled_true_when_omitted(tmp_path):
    from terminal_mcp.config import load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "permissions:\n  terminal_read: true\n"
        "allowed_session_patterns: ['test-*']\n"
    )
    config = load_config(config_path)
    assert config.dashboard.mutations_enabled is True


# ---------------------------------------------------------------------------
# P1 hardening #3: CSRF/Origin defense on mutation routes
# ---------------------------------------------------------------------------


def test_mutation_blocked_with_no_origin_or_referer_header(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-noorigin", "bash -lc 'read x; sleep 10'")
    service = TerminalService(input_config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())  # no default Origin header this time
    response = client.post("/dashboard/api/session/input", json={"name": session, "text": "y"})
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_mutation_blocked_with_cross_origin_header(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-xorigin", "bash -lc 'read x; sleep 10'")
    client, _ = _client(input_config)
    response = client.post(
        "/dashboard/api/session/input", json={"name": session, "text": "y"},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_mutation_allowed_via_referer_fallback_when_origin_absent(input_config, tmux_session_factory):
    session = tmux_session_factory("test-dashboard-referer", "bash -lc 'read x; sleep 10'")
    service = TerminalService(input_config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())
    response = client.post(
        "/dashboard/api/session/input", json={"name": session, "text": "y"},
        headers={"Referer": "http://testserver/dashboard"},
    )
    assert response.status_code == 200  # never even reaches ORIGIN_NOT_ALLOWED


def test_mutation_allowed_from_an_explicitly_configured_extra_origin(tmux_session_factory):
    session = tmux_session_factory("test-dashboard-extraorigin", "bash -lc 'read x; sleep 10'")
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(allowed_origins=("https://proxy.example.com",)),
    )
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())
    response = client.post(
        "/dashboard/api/session/input", json={"name": session, "text": "y"},
        headers={"Origin": "https://proxy.example.com"},
    )
    assert response.status_code == 200


def test_load_config_parses_dashboard_allowed_origins(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_session_patterns: ['test-*']\n"
        "dashboard:\n  allowed_origins: ['https://a.example.com', 'https://b.example.com']\n"
    )
    config = load_config(config_path)
    assert config.dashboard.allowed_origins == ("https://a.example.com", "https://b.example.com")


# ---------------------------------------------------------------------------
# P1 hardening #2: Cloudflare Access identity verification (opt-in, no-op
# unless both cloudflare_access_team_domain and _audience are configured --
# verified against `read_config`/`input_config`, neither of which set them).
# ---------------------------------------------------------------------------


def _cf_access_config(**dashboard_overrides) -> AppConfig:
    return AppConfig(
        PermissionsConfig(True, True), ("test-*", "agent-*"), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(
            cloudflare_access_team_domain="test-team.cloudflareaccess.com",
            cloudflare_access_audience="test-aud",
            **dashboard_overrides,
        ),
    )


def test_cloudflare_access_not_configured_is_a_complete_noop(input_config, tmux_session_factory):
    # input_config has neither cloudflare_access_team_domain nor _audience
    # set -- no Cf-Access-Jwt-Assertion header is required or checked at all.
    session = tmux_session_factory("test-dashboard-noaccess", "bash -lc 'read x; sleep 10'")
    client, _ = _client(input_config)
    response = client.post("/dashboard/api/session/input", json={"name": session, "text": "y"})
    assert response.status_code == 200


def test_cloudflare_access_configured_blocks_mutation_with_no_assertion(tmux_session_factory, monkeypatch):
    session = tmux_session_factory("test-dashboard-cfnoassert", "bash -lc 'read x; sleep 10'")
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    response = client.post("/dashboard/api/session/input", json={"name": session, "text": "y"})
    assert response.status_code == 403
    assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_cloudflare_access_configured_blocks_mutation_with_invalid_assertion(tmux_session_factory, monkeypatch):
    import terminal_mcp.dashboard as dashboard_module

    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: None)
    session = tmux_session_factory("test-dashboard-cfbad", "bash -lc 'read x; sleep 10'")
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    response = client.post(
        "/dashboard/api/session/input", json={"name": session, "text": "y"},
        headers={"Cf-Access-Jwt-Assertion": "not-a-real-token"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_cloudflare_access_configured_allows_mutation_with_verified_assertion(tmux_session_factory, monkeypatch):
    import terminal_mcp.dashboard as dashboard_module
    from terminal_mcp.cf_access import AccessIdentity

    identity = AccessIdentity(email="operator@example.com", subject="user-1", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: identity)
    session = tmux_session_factory("test-dashboard-cfok", "bash -lc 'read x; sleep 10'")
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    response = client.post(
        "/dashboard/api/session/input", json={"name": session, "text": "y"},
        headers={"Cf-Access-Jwt-Assertion": "a-token-that-would-verify"},
    )
    assert response.status_code == 200


def test_cloudflare_access_accepts_cf_authorization_cookie_fallback(tmux_session_factory, monkeypatch):
    import terminal_mcp.dashboard as dashboard_module
    from terminal_mcp.cf_access import AccessIdentity

    identity = AccessIdentity(email="operator@example.com", subject="user-1", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: identity)
    session = tmux_session_factory("test-dashboard-cfcookie", "bash -lc 'read x; sleep 10'")
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"},
                        cookies={"CF_Authorization": "cookie-token"})
    response = client.post("/dashboard/api/session/input", json={"name": session, "text": "y"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# P0 audit re-pass: GET/read routes previously had NO app-level auth at all
# -- only the POST/mutation routes went through _mutation_guard, relying
# entirely on network/tunnel topology for read protection. _read_guard
# closes that gap with the CF Access check alone (no CSRF/Origin, no
# mutations_enabled gate -- see _read_guard's own docstring for why).
# ---------------------------------------------------------------------------

_GET_ROUTES = ("/dashboard", "/dashboard/api/sessions", "/dashboard/api/session",
              "/dashboard/api/supervisor", "/dashboard/api/supervisor2")


def test_cloudflare_access_not_configured_get_routes_are_noop(input_config, tmux_session_factory):
    # input_config has neither cloudflare_access_team_domain nor _audience
    # set -- every GET route behaves exactly as before this fix.
    session = tmux_session_factory("test-dashboard-cf-get-noop", "bash -lc 'sleep 10'")
    client, _ = _client(input_config)
    for path in _GET_ROUTES:
        response = client.get(path, params={"name": session} if path.endswith("/session") else None)
        assert response.status_code == 200, (path, response.text)


def test_cloudflare_access_configured_blocks_get_routes_with_no_assertion(tmux_session_factory):
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())
    for path in _GET_ROUTES:
        response = client.get(path, params={"name": "whatever"} if path.endswith("/session") else None)
        assert response.status_code == 403, path
        assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


def test_cloudflare_access_configured_allows_get_routes_with_verified_assertion(tmux_session_factory, monkeypatch):
    import terminal_mcp.dashboard as dashboard_module
    from terminal_mcp.cf_access import AccessIdentity

    identity = AccessIdentity(email="viewer@example.com", subject="user-2", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: identity)
    session = tmux_session_factory("test-dashboard-cf-get-ok", "bash -lc 'sleep 10'")
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(),
                        headers={"Cf-Access-Jwt-Assertion": "a-token-that-would-verify"})
    for path in _GET_ROUTES:
        response = client.get(path, params={"name": session} if path.endswith("/session") else None)
        assert response.status_code == 200, (path, response.text)


def test_cloudflare_access_get_routes_never_require_an_origin_header(tmux_session_factory, monkeypatch):
    # Unlike POST/mutation routes, a GET (a normal top-level navigation
    # loading the dashboard URL directly) does not reliably send Origin --
    # _read_guard must never require it, only _mutation_guard does.
    import terminal_mcp.dashboard as dashboard_module
    from terminal_mcp.cf_access import AccessIdentity

    identity = AccessIdentity(email="viewer@example.com", subject="user-3", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: identity)
    config = _cf_access_config()
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    # No Origin header at all, on purpose.
    client = TestClient(server.streamable_http_app(),
                        headers={"Cf-Access-Jwt-Assertion": "a-token-that-would-verify"})
    response = client.get("/dashboard")
    assert response.status_code == 200


def test_cloudflare_access_get_routes_work_even_when_mutations_disabled(tmux_session_factory, monkeypatch):
    # Reading must stay available independent of dashboard.mutations_
    # enabled -- that flag is a *write*-path gate, and this is the
    # "read-only dashboard" tunnel's whole purpose.
    import terminal_mcp.dashboard as dashboard_module
    from terminal_mcp.cf_access import AccessIdentity

    identity = AccessIdentity(email="viewer@example.com", subject="user-4", raw_claims={})
    monkeypatch.setattr(dashboard_module, "verify_access_assertion", lambda token, **kw: identity)
    config = _cf_access_config(mutations_enabled=False)
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app(),
                        headers={"Cf-Access-Jwt-Assertion": "a-token-that-would-verify"})
    response = client.get("/dashboard/api/sessions")
    assert response.status_code == 200
    # The write path is still correctly refused.
    post_response = client.post("/dashboard/api/supervisor/ack", json={"id": 1},
                                headers={"Origin": "http://testserver"})
    assert post_response.status_code == 403
    assert post_response.json()["error"] == "DASHBOARD_MUTATIONS_DISABLED"


def test_load_config_rejects_blank_cloudflare_access_fields(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "allowed_session_patterns: ['test-*']\n"
        "dashboard:\n  cloudflare_access_team_domain: ''\n"
    )
    with pytest.raises(ValueError, match="cloudflare_access_team_domain"):
        load_config(config_path)


def test_dashboard_state_order_includes_completion_candidate_and_verified_done():
    # P0-7/P0-8: state_counts from the backend now use COMPLETION_CANDIDATE/
    # VERIFIED_DONE, not "DONE" -- a stale client-side order array keyed on
    # the old name would silently drop both new states from the badge/panel
    # (counts[state] always 0 for a name that no longer appears as a key).
    assert "COMPLETION_CANDIDATE" in DASHBOARD_HTML
    assert "VERIFIED_DONE" in DASHBOARD_HTML
    order_start = DASHBOARD_HTML.index("SUPERVISOR_STATE_ORDER = [")
    order_line = DASHBOARD_HTML[order_start:DASHBOARD_HTML.index("]", order_start)]
    assert "'DONE'" not in order_line
    assert "'COMPLETION_CANDIDATE'" in order_line
    assert "'VERIFIED_DONE'" in order_line
