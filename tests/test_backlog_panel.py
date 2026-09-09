"""Project Backlog dashboard panel (/dashboard/backlog).

The panel renders text that AGENTS write into a repo file, so the XSS
posture is not incidental here -- an item titled `<img onerror=...>` must
reach the DOM as text. These tests pin that the page never assigns
innerHTML from data and builds everything with textContent.
"""
from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import BACKLOG_HTML, DASHBOARD_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from tests.test_backlog import make_config, make_repo


@pytest.fixture
def rig(tmp_path):
    repo = make_repo(tmp_path / "widget")
    config = make_config(tmp_path)
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    backlog = BacklogService(config, queue=queue)
    terminal = TerminalService(config)
    server = build_mcp(terminal, queue=queue, backlog=backlog)
    register_dashboard(server, terminal, queue=queue, backlog=backlog)
    return server, repo, backlog


def test_page_route_registered(rig):
    server, _, _ = rig
    paths = {r.path for r in server._custom_starlette_routes if hasattr(r, "methods")}
    assert "/dashboard/backlog" in paths


def test_page_is_reachable_and_guarded(rig):
    server, _, _ = rig
    client = TestClient(server.streamable_http_app())
    response = client.get("/dashboard/backlog")
    assert response.status_code in (200, 401, 403)     # never 404/500
    if response.status_code == 200:
        assert "Project Backlog" in response.text
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("Cache-Control") == "no-store"


def test_main_dashboard_links_to_the_panel():
    assert '/dashboard/backlog' in DASHBOARD_HTML
    assert 'id="backlogLink"' in DASHBOARD_HTML


# ---------------------------------------------------------------- XSS posture
def test_panel_never_assigns_innerhtml_from_data():
    """No ASSIGNMENT to innerHTML/outerHTML anywhere -- backlog text is
    written by agents into a repo file. Matches the assignment itself
    rather than the mere word, so a comment saying "never innerHTML"
    does not trip it (and a real `x.innerHTML = data` still does)."""
    offenders = re.findall(r"\.(?:inner|outer)HTML\s*=", BACKLOG_HTML)
    assert offenders == [], offenders


def test_panel_builds_content_with_textcontent():
    assert BACKLOG_HTML.count("textContent") >= 10
    # No template-literal HTML injection helpers.
    assert "insertAdjacentHTML" not in BACKLOG_HTML
    assert "document.write" not in BACKLOG_HTML


def test_panel_has_no_inline_event_handler_attributes():
    """on*= attributes in markup would execute attacker-controlled text if
    ever interpolated; handlers are attached in JS instead."""
    assert not re.search(r"<[a-zA-Z]+[^>]*\son(click|error|load)\s*=", BACKLOG_HTML)


def test_hostile_backlog_content_is_served_as_data_not_markup(rig):
    """End-to-end: a hostile title survives the API as a JSON string --
    the page renders it via textContent, so it can never become markup."""
    server, repo, backlog = rig
    hostile = '<img src=x onerror="alert(1)">'
    backlog.add(str(repo), tasks=[{"title": hostile}])
    client = TestClient(server.streamable_http_app())
    response = client.get("/dashboard/api/backlog", params={"path": str(repo)})
    if response.status_code == 200:
        assert response.json()["items"][0]["title"] == hostile      # stored verbatim
        assert response.headers["content-type"].startswith("application/json")
    # and the page itself never contains the payload
    assert hostile not in BACKLOG_HTML


# ---------------------------------------------------------------- behaviour
def test_panel_talks_only_to_the_backlog_api(rig):
    urls = set(re.findall(r"'(/dashboard/api/[a-z/]+)", BACKLOG_HTML))
    assert urls <= {"/dashboard/api/backlog", "/dashboard/api/backlog/update",
                    "/dashboard/api/backlog/dispatch", "/dashboard/api/backlog/add",
                    "/dashboard/api/backlog/complete"}, urls


def test_panel_sends_expected_revision_on_every_write():
    """Optimistic concurrency must not be bypassed by the UI."""
    for route in ("backlog/update", "backlog/dispatch", "backlog/complete", "backlog/add"):
        block = BACKLOG_HTML.split(f"/dashboard/api/{route}", 1)[1][:400]
        assert "expected_revision" in block, route


def test_complete_prompts_for_evidence():
    """The verified-done gate must be visible in the UI, not a surprise
    error -- the prompt asks for the evidence the API requires."""
    assert "Evidence" in BACKLOG_HTML
    assert "commit" in BACKLOG_HTML


def test_panel_exposes_the_status_vocabulary():
    for status in ("BACKLOG", "READY", "IN_PROGRESS", "BLOCKED", "NEEDS_REVIEW", "DONE", "CANCELLED"):
        assert status in BACKLOG_HTML, status
    for priority in ("P0", "P1", "P2", "P3"):
        assert priority in BACKLOG_HTML
