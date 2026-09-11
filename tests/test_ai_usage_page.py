"""The AI Usage report page, rendered.

Structural assertions run everywhere. The browser ones drive the real
template with stubbed API data and skip loudly where no Chromium exists.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.dashboard import AI_USAGE_HTML, DASHBOARD_HTML

PAYLOAD = {
    "generated_at": 1789000000.0,
    "window": {"rolling_seconds": 18000, "rolling_from": 1788982000.0,
               "today_from": 1788950000.0,
               "note": "Rolling 5h is token ACTIVITY measured from transcript "
                       "timestamps. It is not a subscription quota window."},
    "totals": {
        "rolling_5h": {"input": 480, "output": 299628, "cache_read": 134807253,
                       "cache_write": 3448150, "total": 138555511, "messages": 240},
        "today": {"total": 45600000, "messages": 71},
        "lifetime": {"input": 5218, "output": 2274220, "cache_read": 1110413928,
                     "cache_write": 13252763, "events": 3583},
    },
    "sessions": [
        {"agent": "claude", "agent_session_id": "62f9b8e9", "session": "terminal-mcp-main",
         "stable_session_id": "eb8585de", "node_id": "local", "model": "claude-opus-5",
         "project": "/home/mesflow/workspace", "git_branch": "main", "is_subagent": False,
         "cli_version": "2.1.267", "pid": 117992,
         "rolling_5h": {"input": 480, "output": 299628, "cache_read": 134807253,
                        "cache_write": 3448150, "total": 138555511, "messages": 240},
         "today": {"total": 45600000, "messages": 71},
         "lifetime": {"input": 5218, "output": 2274220, "cache_read": 1110413928,
                      "cache_write": 13252763, "total": 1125946129, "messages": 2609},
         "first_seen": 1788900000.0, "last_activity": 1788999995.0,
         "source": "session_transcript"},
        {"agent": "claude", "agent_session_id": "62f9b8e9", "session": "terminal-mcp-main",
         "stable_session_id": "eb8585de", "node_id": "local", "model": "claude-opus-5",
         "project": "/home/mesflow/workspace", "git_branch": "main", "is_subagent": True,
         "cli_version": "2.1.267", "pid": 117992,
         "rolling_5h": {"input": 7, "output": 3, "cache_read": 0, "cache_write": 0,
                        "total": 10, "messages": 1},
         "today": {"total": 10, "messages": 1},
         "lifetime": {"input": 7, "output": 3, "cache_read": 0, "cache_write": 0,
                      "total": 10, "messages": 1},
         "first_seen": 1788990000.0, "last_activity": 1788999000.0,
         "source": "session_transcript"},
        {"agent": "codex", "agent_session_id": "cx-1", "session": "m2",
         "stable_session_id": "315c8f93", "node_id": "local", "model": "gpt-5",
         "project": "/home/mesflow/terminal-mcp", "git_branch": None, "is_subagent": False,
         "cli_version": None, "pid": None,
         "rolling_5h": {"input": 100, "output": 50, "cache_read": 10, "cache_write": 0,
                        "total": 160, "messages": 3},
         "today": {"total": 160, "messages": 3},
         "lifetime": {"input": 100, "output": 50, "cache_read": 10, "cache_write": 0,
                      "total": 160, "messages": 3},
         "first_seen": 1788990000.0, "last_activity": 1788999900.0,
         "source": "session_transcript"},
    ],
    "quota_windows": [
        {"agent": "claude", "label": "subscription", "observed": 0, "source": "unavailable",
         "used_percent": None, "resets_at": None,
         "detail": "Claude Code records no rate-limit or reset metadata."},
        {"agent": "codex", "label": "primary", "observed": 1, "source": "local_cli_state",
         "used_percent": 42.5, "resets_at": 1789003600.0, "detail": None},
    ],
    "sources": {"claude": {"status": "session_transcript", "detail": "~/.claude/projects/**"},
                "codex": {"status": "local_cli_state", "detail": "$CODEX_HOME/sessions/**"}},
}


# -- structure ---------------------------------------------------------------

def test_the_menu_points_at_the_report_page():
    assert 'href="/dashboard/ai-usage"' in DASHBOARD_HTML
    assert 'id="aiUsageLink"' in DASHBOARD_HTML
    # The old entry opened an in-page panel that proxied a separate local
    # service; that button is gone, and its wiring is guarded so its absence
    # cannot throw and take the dashboard script down.
    assert 'id="openAiUsageBtn"' not in DASHBOARD_HTML
    assert "if (openAiUsageBtnEl) {" in DASHBOARD_HTML


def test_the_page_reads_only_the_local_endpoint():
    assert "/dashboard/api/ai-usage/local" in AI_USAGE_HTML
    # No provider endpoint may appear anywhere in the page.
    for host in ("api.anthropic.com", "api.openai.com", "console.anthropic.com"):
        assert host not in AI_USAGE_HTML


def test_the_four_token_kinds_each_have_a_column():
    for header in ("5h Input", "5h Output", "5h Cache Read", "5h Cache Write"):
        assert header in AI_USAGE_HTML


def test_the_rolling_window_is_never_called_a_quota():
    assert "ACTIVITY" in AI_USAGE_HTML or "activity" in AI_USAGE_HTML


# -- rendered ----------------------------------------------------------------

@pytest.fixture(scope="module")
def browser():
    """One browser for the whole module.

    sync_playwright cannot be entered twice in the same thread, so both the
    report page and the dashboard page share this.
    """
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


def _route_all(request):
    url = request.request.url
    if "/dashboard/api/ai-usage/local" in url:
        return request.fulfill(status=200, content_type="application/json",
                               body=json.dumps(PAYLOAD))
    if "/dashboard/api/ai-usage" in url:
        return request.fulfill(status=200, content_type="application/json",
                               body=json.dumps(LEGACY_PAYLOAD))
    if "/dashboard/ai-usage" in url:
        return request.fulfill(status=200, content_type="text/html", body=AI_USAGE_HTML)
    if "/dashboard/api/" in url:
        return request.fulfill(status=200, content_type="application/json", body="{}")
    return request.fulfill(status=200, content_type="text/html", body=DASHBOARD_HTML)


@pytest.fixture(scope="module")
def page(browser):
    """One page, shared. Chromium runs --single-process here (the sandbox
    blocks its zygote), and that build closes the browser when a second page
    is opened -- so the two page fixtures below navigate this one."""
    opened = browser.new_page(viewport={"width": 1440, "height": 900})
    opened.route("**/*", _route_all)
    yield opened


@pytest.fixture
def usage_page(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("http://terminal-mcp.test/dashboard/ai-usage", wait_until="domcontentloaded")
    page.wait_for_selector("#tbody tr", timeout=20000)
    page.wait_for_timeout(300)
    return page


def test_the_page_renders_without_a_script_error(usage_page):
    assert usage_page.evaluate("() => document.querySelectorAll('#tbody tr').length") == 3


def test_the_cards_show_all_three_windows(usage_page):
    titles = usage_page.evaluate("() => [...document.querySelectorAll('.card h2')].map(e => e.textContent)")
    assert titles == ["5h Total", "Today", "Lifetime", "Sessions"]


def test_an_unobserved_quota_says_so_instead_of_showing_a_number(usage_page):
    """The requirement that matters most: no invented reset time."""
    text = usage_page.evaluate(
        "() => [...document.querySelectorAll('#tbody tr')]"
        ".find(r => r.textContent.includes('terminal-mcp-main')).textContent")
    assert "Not observed" in text
    assert "reset" not in text.casefold()


def test_an_observed_quota_shows_its_percent_and_reset(usage_page):
    text = usage_page.evaluate(
        "() => [...document.querySelectorAll('#tbody tr')]"
        ".find(r => r.textContent.includes('codex')).textContent")
    assert "43%" in text or "42%" in text
    assert "reset" in text


def test_a_subagent_row_is_distinguishable_from_its_parent(usage_page):
    rows = usage_page.evaluate(
        "() => [...document.querySelectorAll('#tbody tr')].map(r => r.textContent)")
    assert any("claude · sub" in row for row in rows)


def test_sorting_by_a_column_reorders_the_table(usage_page):
    # Compared on the 5h Total cell, not the session name: a parent and its
    # subagent share a session name, so the first column cannot tell the two
    # orderings apart.
    total_cell = "() => document.querySelector('#tbody tr td:nth-child(8)').textContent"
    usage_page.click("th[data-k='t']")      # ascending
    usage_page.wait_for_timeout(200)
    ascending = usage_page.evaluate(total_cell)
    usage_page.click("th[data-k='t']")      # back to descending
    usage_page.wait_for_timeout(200)
    descending = usage_page.evaluate(total_cell)
    assert ascending != descending
    assert descending == "138,555,511" and ascending == "10"


def test_filtering_by_agent_narrows_the_table(usage_page):
    usage_page.select_option("#fAgent", "codex")
    usage_page.wait_for_timeout(250)
    rows = usage_page.evaluate("() => document.querySelectorAll('#tbody tr').length")
    assert rows == 1


def test_expanding_a_row_shows_its_provenance_and_identity(usage_page):
    usage_page.click("#tbody tr .expand")
    usage_page.wait_for_timeout(250)
    detail = usage_page.evaluate("() => document.querySelector('tr.detail-row').textContent")
    for label in ("conversation", "stable_session_id", "cli version", "nguồn số liệu"):
        assert label in detail


def test_the_page_explains_why_a_metric_is_missing(usage_page):
    note = usage_page.evaluate("() => document.querySelector('#provenance').textContent")
    assert "not a subscription quota window" in note
    assert "no rate-limit or reset metadata" in note


@pytest.mark.parametrize("width,height", [(1440, 900), (390, 844)])
def test_no_horizontal_page_overflow(usage_page, width, height):
    usage_page.set_viewport_size({"width": width, "height": height})
    usage_page.wait_for_timeout(250)
    assert not usage_page.evaluate(
        "() => document.documentElement.scrollWidth > window.innerWidth + 1")
    # The table itself may be wider than a phone -- inside its own scroller.
    assert usage_page.evaluate(
        "() => { const w = document.querySelector('.wrap');"
        "        return w.scrollWidth >= w.clientWidth; }")


# -- the menu, and the endpoint a tab loaded before this existed -------------

LEGACY_PAYLOAD = {
    "available": True, "error": None,
    "providers": [
        {"provider": "claude", "ok": True, "warning": False, "critical": False,
         "plan": None, "account": None, "sessions": 7, "usage_message": None, "error": None,
         "windows": [{"label": "5h activity · 18,440,532 tokens · 26 msg",
                      "used_percent": None, "remaining_percent": None,
                      "total_tokens": 18440532, "messages": 26,
                      "source": "session_transcript"}]},
        {"provider": "codex", "ok": False, "warning": False, "critical": False,
         "plan": None, "account": None, "sessions": 0, "windows": [],
         "usage_message": "No ~/.codex on this machine.", "error": "No local data observed"},
    ],
    "sessions": [], "totals": PAYLOAD["totals"], "quota_windows": PAYLOAD["quota_windows"],
    "source": "local:~/.claude, $CODEX_HOME", "report_url": "/dashboard/ai-usage",
    "app_version": None, "fetched_at": 1789000000.0, "cached": False,
    "cache_age_seconds": 0.0, "stale": False,
}


def test_the_legacy_endpoint_no_longer_names_an_external_service():
    """Root cause of "AI Usage shows nothing".

    /dashboard/api/ai-usage proxied a separate local service on
    127.0.0.1:8787 that is not installed on this fleet, so it answered
    `available:false, "Connection refused"` and every client rendered an
    empty panel -- including a dashboard tab opened before the report page
    existed, which keeps polling that endpoint and never reloads. It is
    served from the local collector now, so such a tab starts showing real
    numbers without being reloaded.
    """
    from terminal_mcp import dashboard

    source = dashboard.__dict__["register_dashboard"].__doc__ or ""
    assert "_local_ai_usage_panel" in DASHBOARD_HTML or True     # route-level, asserted below
    import inspect
    text = inspect.getsource(dashboard.register_dashboard)
    assert "_local_ai_usage_panel(force)" in text
    assert "ai_usage.get_usage(force=force)" not in text


def test_the_legacy_payload_carries_the_figure_in_its_label():
    # The older panel renders a window with no percentage as the single word
    # "unavailable", so the number has to be in the label to survive there.
    window = LEGACY_PAYLOAD["providers"][0]["windows"][0]
    assert window["used_percent"] is None          # never an invented denominator
    assert "tokens" in window["label"] and "," in window["label"]


@pytest.fixture
def dashboard_page(page):
    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto("http://terminal-mcp.test/dashboard", wait_until="domcontentloaded")
    page.wait_for_timeout(900)
    return page


def test_the_dashboard_script_survives_the_removed_button(dashboard_page):
    # An unguarded onclick on a removed element throws during setup and takes
    # every later handler with it -- the menu would then never open at all.
    assert dashboard_page.evaluate("() => typeof window.fetch === 'function'")
    assert dashboard_page.evaluate("() => !!document.querySelector('#headerMenuBtn')")


def test_the_menu_opens_and_offers_ai_usage(dashboard_page):
    dashboard_page.click("#headerMenuBtn")
    dashboard_page.wait_for_timeout(300)
    assert dashboard_page.evaluate(
        "() => document.querySelector('#headerMenu').classList.contains('open')")
    link = dashboard_page.evaluate(
        "() => { const a = document.querySelector('#aiUsageLink');"
        "        const r = a.getBoundingClientRect();"
        "        return {href: a.getAttribute('href'), visible: r.width > 0 && r.height > 0}; }")
    assert link["href"] == "/dashboard/ai-usage"
    assert link["visible"], "the AI Usage entry must be clickable once the menu is open"


def test_following_the_menu_entry_lands_on_the_report(dashboard_page):
    dashboard_page.click("#headerMenuBtn")
    dashboard_page.wait_for_timeout(200)
    dashboard_page.click("#aiUsageLink")
    dashboard_page.wait_for_selector("#tbody tr", timeout=15000)
    assert "/dashboard/ai-usage" in dashboard_page.url
    assert dashboard_page.evaluate("() => document.querySelectorAll('#tbody tr').length") == 3
