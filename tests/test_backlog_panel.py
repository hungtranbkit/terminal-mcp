"""Project Backlog dashboard panel (/dashboard/backlog).

The panel renders text that AGENTS write into a repo file, so the XSS
posture is not incidental here -- an item titled `<img onerror=...>` must
reach the DOM as text. These tests pin that the page never assigns
innerHTML from data and builds everything with textContent.
"""
from __future__ import annotations

import re
import shutil
import subprocess

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
                    "/dashboard/api/backlog/complete",
                    # The project picker's own list -- read-only, and the
                    # only non-backlog endpoint this page is allowed.
                    "/dashboard/api/projects"}, urls


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


# ------------------------------------------------- project picker (P0.1 follow-up)
#
# The panel used to be addressable ONLY by a local filesystem path, which
# cannot express the thing that actually matters here: one project has
# checkouts on several machines, so a path identifies a CHECKOUT and only
# ever reaches the controller's own box.

def test_panel_has_a_project_picker():
    assert 'id="projectPicker"' in BACKLOG_HTML
    assert "/dashboard/api/projects" in BACKLOG_HTML


def test_every_write_addresses_the_project_through_one_selector():
    """Each mutation body is built from selector(), so there is exactly
    ONE place deciding project_id-vs-path. A write that hardcoded
    `path: state.path` again would silently edit the wrong project when a
    remote project is picked -- which is the whole bug this closes."""
    for route in ("backlog/update", "backlog/dispatch", "backlog/complete", "backlog/add"):
        block = BACKLOG_HTML.split(f"/dashboard/api/{route}", 1)[1][:400]
        assert "Object.assign(selector()" in block, route
        assert "path: state.path" not in block, route


def test_selector_prefers_project_id_over_path():
    """Mirrors BacklogService._project's own precedence. If the UI sent
    both, the service would use project_id anyway -- sending only the
    chosen one keeps the request honest about what it is addressing."""
    body = BACKLOG_HTML.split("function selector()", 1)[1][:220]
    assert "state.projectId ? {project_id: state.projectId} : {path: state.path}" in body


def test_read_sends_project_id_instead_of_path_when_one_is_picked():
    block = BACKLOG_HTML.split("async function load()", 1)[1][:700]
    assert "params.set('project_id', state.projectId)" in block
    assert "else if (path) params.set('path', path)" in block


def test_path_box_is_disabled_while_a_project_is_picked():
    """Two selectors that can disagree is a bug waiting to happen; the
    path box is inert whenever the picker is driving."""
    assert "pathEl.disabled = !!state.projectId" in BACKLOG_HTML


def test_a_stale_stored_project_is_surfaced_not_silently_swapped():
    """localStorage can name a project the fleet no longer reports. It
    must be shown as missing rather than falling back to whichever project
    happens to be first -- editing the wrong project's plan is exactly the
    failure this panel must not have."""
    block = BACKLOG_HTML.split("if (state.projectId && !rows.some", 1)[1][:400]
    assert "không còn trong fleet" in block


def test_picker_failure_does_not_take_the_panel_down():
    block = BACKLOG_HTML.split("async function loadProjects()", 1)[1][:600]
    assert "vẫn dùng được ô đường dẫn" in block


def test_unreachable_nodes_are_reported_not_hidden():
    """list_projects returns node_errors when a node could not be read;
    a picker that dropped them would quietly show an incomplete fleet."""
    assert "data.node_errors" in BACKLOG_HTML


def test_picker_options_use_textcontent_only():
    """Project names come from git remotes -- still not trusted markup."""
    block = BACKLOG_HTML.split("async function loadProjects()", 1)[1][:1600]
    assert "opt.textContent" in block
    assert "innerHTML" not in block


def test_panel_no_longer_renders_the_removed_backlog_file_field():
    """The store became controller-authoritative (P0.1): the response
    stopped carrying `backlog_file`, so the chip that rendered it was
    always empty. It is replaced by the project's real identity."""
    # Match the USE, not the word -- the comment above the replacement
    # names the removed field on purpose, and must not trip this.
    assert "chip(clean(data.backlog_file))" not in BACKLOG_HTML
    assert not re.search(r"=\s*clean\(data\.backlog_file\)", BACKLOG_HTML)
    assert "project.git_remote" in BACKLOG_HTML


# ---------------------------------------------- inline script must actually parse
#
# A REAL bug this caught: BACKLOG_HTML is an ordinary (non-raw) Python
# triple-quoted string, so `join('\n')` written in the source put a
# LITERAL NEWLINE inside a JavaScript string literal. That is a syntax
# error, and a syntax error anywhere kills the WHOLE script -- the panel
# rendered its static markup and then did nothing at all: no project
# list, no items, no buttons. It was invisible to every test here because
# they all assert on the HTML text, and no test ever parsed the JS.

def _inline_scripts(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.DOTALL))


def test_no_separator_string_literal_holds_a_raw_control_char():
    """Narrow, zero-false-positive guard for the exact defect, so it runs
    on hosts without node: a `.split('`/`.join('` whose literal is a raw
    newline or tab instead of the escape `\\n`. A general JS parser is
    node's job (below) -- attempting quote-parity in Python drowns in
    apostrophes inside comments, which is why this stays targeted."""
    for name in ("BACKLOG_HTML", "DASHBOARD_HTML", "GLOBAL_TASKS_HTML",
                 "NODES_ADMIN_HTML", "SESSIONS_ADMIN_HTML", "WEBTERM_HTML"):
        import terminal_mcp.dashboard as dashboard_module
        js = _inline_scripts(getattr(dashboard_module, name))
        bad = re.findall(r"\.(?:split|join)\(\s*['\"][\n\t\r]", js)
        assert not bad, f"{name}: raw control char inside a separator string literal"


@pytest.mark.parametrize("name", ["BACKLOG_HTML", "DASHBOARD_HTML", "GLOBAL_TASKS_HTML",
                                  "NODES_ADMIN_HTML", "SESSIONS_ADMIN_HTML", "WEBTERM_HTML"])
def test_every_dashboard_template_script_parses(tmp_path, name):
    """Parse each page's inline script with a real JS engine. Covers every
    template, not just the one that was broken -- the mistake is a
    property of embedding JS in a non-raw Python string, so any of them
    could acquire it."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed on this host")
    import terminal_mcp.dashboard as dashboard_module
    source = _inline_scripts(getattr(dashboard_module, name))
    assert source.strip(), f"{name} has no inline script to check"
    path = tmp_path / f"{name}.js"
    path.write_text(source)
    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, f"{name} inline script does not parse:\n{result.stderr}"
