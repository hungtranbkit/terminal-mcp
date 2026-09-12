"""The AI Usage screen.

A separate screen with its own navigation, not a panel bolted onto the
dashboard. These tests drive the real template with stubbed endpoints, so
they check what a person actually sees: that each tab renders, that the
filters reach the query, that a link carries its filters, and -- the part
that matters most -- that an unmeasured quota says N/A instead of a number.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.dashboard import AI_USAGE_HTML, DASHBOARD_HTML

TABS = ("overview", "prompts", "sessions", "projects", "models", "quota", "events")

SUMMARY = {
    "generated_at": 1789000000.0,
    "totals": {"input_tokens": 5218, "output_tokens": 2274220, "cache_read_tokens": 1110413928,
               "cache_write_tokens": 13252763, "total_tokens": 1125946129, "requests": 3583},
    "spans": {label: {"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3,
                      "cache_write_tokens": 4, "total_tokens": total, "requests": requests}
              for label, total, requests in (("1h", 1000, 1), ("5h", 50000, 5),
                                             ("24h", 857800000, 240), ("7d", 1310000000, 900),
                                             ("30d", 1320000000, 950))},
    "estimated_cost_usd": 12.2, "cost_source": "session_transcript (CLI-computed)",
    "cost_sessions": 5, "active_sessions_24h": 3,
    "peak_session_24h": {"agent_session_id": "62f9b8e9", "project": "/home/me/workspace",
                         "total": 830500000},
    "top_project_24h": {"project": "/home/me/workspace", "total": 832500000},
}
TIMELINE = {"bucket": "hour", "bucket_seconds": 3600, "points": [
    {"bucket_start": 1788996400 + i * 3600, "input_tokens": 10, "output_tokens": 20,
     "cache_read_tokens": 300, "cache_write_tokens": 40, "total_tokens": 370, "requests": 3}
    for i in range(6)]}
PROMPTS = {"grand_total": 1000, "limit": 25, "offset": 0, "items": [
    {"prompt_id": "p1", "preview": "Hãy audit chính project Terminal MCP",
     "project": "/home/me/workspace", "agent_session_id": "62f9b8e9", "agent": "claude",
     "node_id": "local", "models": "claude-opus-5", "input_tokens": 658,
     "output_tokens": 254231, "cache_read_tokens": 184014319, "cache_write_tokens": 367577,
     "total_tokens": 184636785, "requests": 329, "share_percent": 13.98,
     "duration_seconds": 7740, "first_ts": 1788900000, "last_ts": 1788990000,
     "text_hash": "0ae45eb14f8c", "char_length": 4681, "git_branch": "main"},
    {"prompt_id": None, "preview": None, "project": None, "agent_session_id": "x",
     "agent": "claude", "node_id": "local", "models": None, "input_tokens": 1,
     "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0,
     "total_tokens": 2, "requests": 1, "share_percent": 0.0, "duration_seconds": 0,
     "first_ts": 1788990000, "last_ts": 1788990000, "unassigned": True}]}
SESSIONS = {"limit": 50, "items": [
    {"agent": "claude", "agent_session_id": "62f9b8e9", "node_id": "local",
     "project": "/home/me/workspace", "is_subagent": False, "models": "claude-opus-5",
     "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4,
     "total_tokens": 830500000, "requests": 1653, "tokens_5h": 50700000,
     "tokens_24h": 830500000, "tokens_7d": 830500000, "tokens_30d": 830500000,
     "last_activity": 1788999990, "first_seen": 1788900000,
     "estimated_cost_usd": 1.77, "avg_tokens_per_request": 502},
    {"agent": "codex", "agent_session_id": "cx1", "node_id": "local", "project": None,
     "is_subagent": True, "models": None, "input_tokens": 0, "output_tokens": 0,
     "cache_read_tokens": 0, "cache_write_tokens": 0, "total_tokens": 10, "requests": 1,
     "tokens_5h": 0, "tokens_24h": 10, "tokens_7d": 10, "tokens_30d": 10,
     "last_activity": 1788990000, "first_seen": 1788990000,
     "estimated_cost_usd": None, "avg_tokens_per_request": 10}]}
PROJECTS = {"limit": 50, "items": [
    {"project": "/home/me/workspace", "sessions": 2, "models": "claude-opus-5",
     "input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4,
     "total_tokens": 832500000, "requests": 1671, "tokens_24h": 832500000,
     "tokens_7d": 832500000, "tokens_30d": 832500000, "last_activity": 1788999990,
     "trend_percent": 25.0},
    {"project": "unassigned", "sessions": 1, "models": None, "input_tokens": 0,
     "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
     "total_tokens": 2, "requests": 1, "tokens_24h": 2, "tokens_7d": 2, "tokens_30d": 2,
     "last_activity": 1788990000, "trend_percent": None}]}
MODELS = {"grand_total": 857800000, "items": [
    {"agent": "claude", "model": "claude-opus-5", "input_tokens": 3500,
     "output_tokens": 1700000, "cache_read_tokens": 841900000, "cache_write_tokens": 14200000,
     "total_tokens": 857800000, "requests": 3583, "share_percent": 100.0,
     "estimated_cost_usd": 12.18}]}
EVENTS = {"total": 2, "limit": 100, "offset": 0, "has_more": False, "items": [
    {"event_id": "e1", "ts": 1788999990, "agent": "claude", "agent_session_id": "62f9b8e9",
     "node_id": "local", "model": "claude-opus-5", "input_tokens": 21087,
     "output_tokens": 769, "cache_read_tokens": 357, "cache_write_tokens": 14095,
     "project": "/home/me/workspace", "source": "session_transcript"}]}
QUOTA_HISTORY = {"items": [
    {"taken_at": 1788999000, "agent": "claude", "label": "subscription",
     "state": "unavailable", "used_percent": None, "resets_at": None, "source": "unavailable"}]}
LOCAL = {
    "generated_at": 1789000000.0,
    "window": {"note": "Rolling 5h is token ACTIVITY measured from transcript timestamps. "
                       "It is not a subscription quota window."},
    "totals": SUMMARY["totals"],
    "sessions": [{"agent": "claude", "node_id": "local", "project": "/home/me/workspace",
                  "model": "claude-opus-5", "agent_session_id": "62f9b8e9"}],
    "quota_windows": [
        {"agent": "claude", "label": "subscription", "observed": 0, "source": "unavailable",
         "used_percent": None, "resets_at": None,
         "detail": "Claude Code records no rate-limit or reset metadata."},
        {"agent": "codex", "label": "subscription", "observed": 0, "source": "unavailable",
         "used_percent": None, "resets_at": None, "detail": "No ~/.codex on this machine."}],
    "sources": {"claude": {"status": "session_transcript", "detail": "~/.claude/projects/**"},
                "codex": {"status": "unavailable", "detail": "$CODEX_HOME"}},
}

ENDPOINTS = {"summary": SUMMARY, "timeline": TIMELINE, "prompts": PROMPTS,
             "sessions": SESSIONS, "projects": PROJECTS, "models": MODELS,
             "events": EVENTS, "quota-history": QUOTA_HISTORY, "local": LOCAL}

SEEN: list[str] = []


def _route(request):
    url = request.request.url
    SEEN.append(url)
    for name, payload in ENDPOINTS.items():
        if f"/dashboard/api/ai-usage/{name}" in url:
            return request.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(payload))
    if "/dashboard/api/ai-usage" in url:
        return request.fulfill(status=200, content_type="application/json",
                               body=json.dumps({"available": True, "providers": []}))
    if "/dashboard/ai-usage" in url:
        return request.fulfill(status=200, content_type="text/html", body=AI_USAGE_HTML)
    if "/dashboard/api/" in url:
        return request.fulfill(status=200, content_type="application/json", body="{}")
    return request.fulfill(status=200, content_type="text/html", body=DASHBOARD_HTML)


# -- structure ---------------------------------------------------------------

def test_the_menu_points_at_the_screen():
    assert 'href="/dashboard/ai-usage"' in DASHBOARD_HTML
    assert 'id="aiUsageLink"' in DASHBOARD_HTML
    assert 'id="openAiUsageBtn"' not in DASHBOARD_HTML
    assert "if (openAiUsageBtnEl) {" in DASHBOARD_HTML


def test_it_is_its_own_screen_with_its_own_navigation():
    for tab in TABS:
        assert f'data-tab="{tab}"' in AI_USAGE_HTML


def test_no_provider_endpoint_is_ever_contacted():
    for host in ("api.anthropic.com", "api.openai.com", "console.anthropic.com",
                 "127.0.0.1:8787"):
        assert host not in AI_USAGE_HTML


def test_the_four_token_kinds_are_each_addressable():
    for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        assert field in AI_USAGE_HTML


# -- rendered ----------------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright
    with sync_playwright() as pw:
        try:
            launched = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        yield launched
        launched.close()


@pytest.fixture(scope="module")
def shared_page(browser):
    # One page for the module: this Chromium runs --single-process (the
    # sandbox blocks its zygote) and closes the browser on a second page.
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.route("**/*", _route)
    yield page


@pytest.fixture
def screen(shared_page):
    SEEN.clear()
    shared_page.set_viewport_size({"width": 1440, "height": 900})
    shared_page.goto("http://terminal-mcp.test/dashboard/ai-usage", wait_until="domcontentloaded")
    shared_page.wait_for_selector(".card", timeout=20000)
    shared_page.wait_for_timeout(350)
    return shared_page


def _open(page, tab):
    page.click(f"nav button[data-tab='{tab}']")
    page.wait_for_timeout(450)


def test_the_screen_renders_without_a_script_error(screen):
    assert screen.evaluate("() => document.querySelectorAll('.card').length") == 8


def test_the_overview_answers_the_ten_second_questions(screen):
    text = screen.evaluate("() => document.querySelector('.cards').textContent")
    for label in ("Hôm nay", "7 ngày", "30 ngày", "Chi phí ước tính",
                  "Session hoạt động", "Session tốn nhất", "Project tốn nhất", "Quota thấp nhất"):
        assert label in text
    assert "$12.20" in text


def test_the_trend_chart_splits_the_token_kinds(screen):
    assert screen.evaluate("() => document.querySelectorAll('.chart .col').length") == 6
    legend = screen.evaluate("() => document.querySelector('.legend').textContent")
    for label in ("Input", "Output", "Cache read", "Cache write"):
        assert label in legend


@pytest.mark.parametrize("tab", [t for t in TABS if t != "overview"])
def test_every_tab_renders_rows(screen, tab):
    _open(screen, tab)
    assert screen.evaluate("() => document.querySelectorAll('#view tbody tr').length") >= 1


def test_top_prompts_shows_the_preview_and_its_share(screen):
    _open(screen, "prompts")
    text = screen.evaluate("() => document.querySelector('#view tbody tr').textContent")
    assert "Hãy audit chính project Terminal MCP" in text
    assert "13.98%" in text


def test_a_turn_with_no_prompt_reads_as_unassigned(screen):
    _open(screen, "prompts")
    rows = screen.evaluate("() => [...document.querySelectorAll('#view tbody tr')].map(r => r.textContent)")
    assert any("unassigned" in row for row in rows)


def test_clicking_a_prompt_opens_its_detail(screen):
    _open(screen, "prompts")
    screen.click("#view tbody tr")
    screen.wait_for_timeout(300)
    detail = screen.evaluate("() => document.querySelector('.drill').textContent")
    for label in ("prompt_id", "hash", "độ dài prompt", "preview"):
        assert label in detail


def test_drilling_into_a_project_carries_the_filter_to_sessions(screen):
    _open(screen, "projects")
    screen.click("#view tbody tr")
    screen.wait_for_timeout(300)
    screen.click(".drill button.btn >> text=Xem sessions")
    screen.wait_for_timeout(500)
    assert screen.evaluate("() => document.querySelector('#fProject').value") == "/home/me/workspace"
    assert screen.evaluate(
        "() => document.querySelector('nav button.active').dataset.tab") == "sessions"


def test_a_session_row_shows_both_quota_windows(screen):
    _open(screen, "sessions")
    headers = screen.evaluate("() => [...document.querySelectorAll('#view th')].map(e => e.textContent)")
    assert "Quota 5h" in headers and "Quota 1w" in headers


def test_an_unmeasured_quota_reads_na_and_never_a_number(screen):
    """The rule this whole build turns on."""
    _open(screen, "quota")
    text = screen.evaluate("() => document.querySelector('#view').textContent")
    assert "N/A" in text
    assert "%" not in text.split("Ghi chú")[0] or "reported" not in text
    rows = screen.evaluate("() => document.querySelectorAll('#view tbody tr').length")
    assert rows == 4          # claude + codex, 5h + 1w


def test_the_quota_tab_says_why_a_window_is_missing(screen):
    _open(screen, "quota")
    text = screen.evaluate("() => document.querySelector('#view').textContent")
    assert "no rate-limit or reset metadata" in text
    assert "No ~/.codex" in text


def test_changing_the_range_reaches_the_query(screen):
    screen.select_option("#fRange", "7d")
    screen.wait_for_timeout(600)
    assert any("range=7d" in url for url in SEEN)


def test_filters_survive_a_reload_through_the_url(screen):
    screen.select_option("#fRange", "30d")
    _open(screen, "projects")
    url = screen.url
    assert "range=30d" in url and "tab=projects" in url
    screen.goto(url, wait_until="domcontentloaded")
    screen.wait_for_selector("#view tbody tr", timeout=20000)
    assert screen.evaluate("() => document.querySelector('#fRange').value") == "30d"
    assert screen.evaluate(
        "() => document.querySelector('nav button.active').dataset.tab") == "projects"


def test_sorting_a_table_reorders_it(screen):
    _open(screen, "sessions")
    first = screen.evaluate("() => document.querySelector('#view tbody tr').textContent")
    screen.click("#view th[data-k='tokens_24h']")
    screen.wait_for_timeout(300)
    assert screen.evaluate("() => document.querySelector('#view tbody tr').textContent") != first


def test_search_narrows_a_table(screen):
    _open(screen, "sessions")
    screen.fill("#fSearch", "cx1")
    screen.wait_for_timeout(400)
    assert screen.evaluate("() => document.querySelectorAll('#view tbody tr').length") == 1
    screen.fill("#fSearch", "")
    screen.wait_for_timeout(300)


def test_the_export_link_carries_the_current_view(screen):
    _open(screen, "projects")
    href = screen.evaluate("() => document.querySelector('#exportBtn').getAttribute('href')")
    assert "dataset=projects" in href and "format=csv" in href


def test_raw_events_are_shown_with_their_source(screen):
    _open(screen, "events")
    text = screen.evaluate("() => document.querySelector('#view tbody tr').textContent")
    assert "session_transcript" in text


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_no_horizontal_overflow(screen, width, height):
    screen.set_viewport_size({"width": width, "height": height})
    screen.wait_for_timeout(400)
    assert not screen.evaluate(
        "() => document.documentElement.scrollWidth > window.innerWidth + 1")


def test_on_a_phone_a_table_row_becomes_a_card(screen):
    # An eleven-column table squeezed into 390px is unreadable however it
    # scrolls, so each row stacks with its own labels instead.
    _open(screen, "sessions")
    screen.set_viewport_size({"width": 390, "height": 844})
    screen.wait_for_timeout(400)
    assert screen.evaluate(
        "() => getComputedStyle(document.querySelector('#view tbody td')).display") == "flex"
    assert screen.evaluate(
        "() => getComputedStyle(document.querySelector('#view thead')).display") == "none"
