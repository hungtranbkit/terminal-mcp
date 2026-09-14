"""The Work UI: the data it needs, and the one thing it must never show.

An ordinary session appearing under "Workers" would tell a human the runtime
had taken over their terminal. That is the assertion this file exists for;
everything else is making sure the screen has real data rather than a
plausible-looking shell.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from terminal_mcp import dashboard as dashboard_module
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import WORK_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.work_service import WorkService
from terminal_mcp.work_store import WorkStore


def _config():
    return AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                     InputPolicyConfig(allowed_session_patterns=("test-*",)))


SESSIONS = [
    {"name": "mesflow-work", "node_id": "local", "input_allowed": True,
     "current_command": "claude"},
    {"name": "offlinepos-work", "node_id": "hp-linux", "input_allowed": True,
     "current_command": "claude"},
    # The ones that must never be listed as workers.
    {"name": "terminal-mcp-main", "node_id": "local", "input_allowed": True,
     "current_command": "claude"},
    {"name": "m1", "node_id": "local", "input_allowed": True, "current_command": "claude"},
]
NODES = {"local": {"node_id": "local", "status": "online", "metadata_stale": False},
         "hp-linux": {"node_id": "hp-linux", "status": "offline", "metadata_stale": False}}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_WORK_DB", str(tmp_path / "work.db"))
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    service = WorkService(WorkStore(tmp_path / "work.db"), queue=queue)
    terminal = TerminalService(_config())
    server = build_mcp(terminal, queue=queue)
    register_dashboard(server, terminal, queue=queue)
    client = TestClient(server.streamable_http_app(),
                        headers={"Origin": "http://testserver"})
    return client, service, queue


# -- the guarantee ---------------------------------------------------------------

def test_an_ordinary_session_is_never_listed_as_a_worker(wired):
    """Not even as a rejected candidate. A human seeing their own terminal
    under "Workers" would reasonably conclude the runtime had taken it."""
    _client, service, _queue = wired
    workers = service.workers(sessions=SESSIONS, nodes=NODES)["workers"]
    names = {w["session"] for w in workers}
    assert names == {"mesflow-work", "offlinepos-work"}
    assert "terminal-mcp-main" not in names
    assert "m1" not in names
    assert all(w["is_work_session"] for w in workers)


def test_an_unusable_work_session_is_kept_with_its_reason(wired):
    """Those ARE workers, and "why is mine not being used" is what this
    answers. Dropping them would make the list lie by omission."""
    _client, service, _queue = wired
    workers = {w["session"]: w for w in service.workers(sessions=SESSIONS, nodes=NODES)["workers"]}
    offline = workers["offlinepos-work"]
    assert offline["state"] == "OFFLINE"
    assert offline["eligible"] is False
    assert offline["detail"], "a rejected worker must say why"


def test_a_worker_running_a_task_reports_busy_and_what_it_is_doing(wired):
    _client, service, queue = wired
    created = service.create(title="T", goal="G", lane="mesflow-work",
                             tasks=[{"title": "the task", "prompt": "p"}])
    task_id = created["tasks"][0]["queue_task_id"]
    for step in ("PRECHECK", "READY", "DISPATCHING", "RUNNING"):
        queue.store.transition_task(task_id, step, event_type="t")
    worker = {w["session"]: w for w in
              service.workers(sessions=SESSIONS, nodes=NODES)["workers"]}["mesflow-work"]
    assert worker["state"] == "BUSY"
    assert worker["current_task"]["title"] == "the task"
    assert worker["current_task"]["status"] == "RUNNING"


# -- the data the list needs -------------------------------------------------------

def test_the_list_carries_what_an_operator_scans_for(wired):
    """Triage without opening every run: who needs you, what is blocked, who
    is running it, when it last moved."""
    _client, service, queue = wired
    created = service.create(title="Ship", goal="G", lane="mesflow-work",
                             project_id="mesflow",
                             tasks=[{"title": "a", "prompt": "p1"},
                                    {"title": "b", "prompt": "p2"}])
    work_id = created["work"]["work_id"]
    running = created["tasks"][0]["queue_task_id"]
    for step in ("PRECHECK", "READY", "DISPATCHING", "RUNNING"):
        queue.store.transition_task(running, step, event_type="t")
    blocked = created["tasks"][1]["queue_task_id"]
    queue.store.transition_task(blocked, "PRECHECK", event_type="t")
    queue.store.transition_task(blocked, "BLOCKED", event_type="t")
    service.request_approval(work_id, kind="deploy", summary="restart it",
                             requested_by="agent")

    row = service.list_runs()["works"][0]
    assert row["project_id"] == "mesflow"
    assert row["running_workers"] == ["mesflow-work"]
    assert row["blocked_tasks"] == 1
    assert row["needs_you"] == 1
    assert row["needs_you_summary"] == "restart it"
    assert row["updated_at"]


def test_the_detail_carries_dependency_attempt_priority_and_worker(wired):
    _client, service, queue = wired
    created = service.create(title="T", goal="G", lane="mesflow-work",
                             tasks=[{"title": "a", "prompt": "p1", "priority": 5}])
    task_id = created["tasks"][0]["queue_task_id"]
    for step in ("PRECHECK", "READY", "DISPATCHING"):
        queue.store.transition_task(task_id, step, event_type="t")
    task = service.status(created["work"]["work_id"])["tasks"][0]
    for field in ("queue_status", "queue_position", "priority", "attempts",
                  "max_attempts", "depends_on", "worker_session", "claimed_by"):
        assert field in task, field
    assert task["priority"] == 5
    assert task["worker_session"] == "mesflow-work"


# -- HTTP surface ------------------------------------------------------------------

def test_the_workers_endpoint_is_a_read_behind_the_same_guard(wired):
    client, _service, _queue = wired
    assert client.get("/dashboard/api/work/workers").status_code == 200
    # A write method does not exist on it.
    assert client.post("/dashboard/api/work/workers", json={}).status_code in (405, 404)


def test_the_workers_endpoint_never_returns_an_ordinary_session(wired):
    client, _service, _queue = wired
    payload = client.get("/dashboard/api/work/workers").json()
    for worker in payload.get("workers", []):
        assert worker["session"].endswith("-work")


def test_every_endpoint_the_page_calls_exists(wired):
    """A page fetching a route that does not exist renders an empty shell and
    looks like a data problem."""
    import re

    client, _service, _queue = wired
    called = set(re.findall(r"api\('(/dashboard/api/work[^']*)'", WORK_HTML))
    called |= set(re.findall(r"api\('(/dashboard/api/work[^']*)'\s*\+", WORK_HTML))
    assert called, "the page must fetch something"
    for path in called:
        base = path.split("?")[0]
        response = client.get(base) if "approve" not in base and "control" not in base \
            and "continue" not in base and "create" not in base else None
        if response is not None:
            assert response.status_code == 200, base


def test_the_mutating_routes_still_require_csrf_origin(tmp_path, monkeypatch):
    """Loosening nothing: the Work writes sit behind the same guard as every
    other dashboard mutation."""
    monkeypatch.setenv("TERMINAL_MCP_WORK_DB", str(tmp_path / "w.db"))
    queue = QueueService(QueueStore(tmp_path / "q.db"))
    terminal = TerminalService(_config())
    server = build_mcp(terminal, queue=queue)
    register_dashboard(server, terminal, queue=queue)
    no_origin = TestClient(server.streamable_http_app())
    for path in ("/dashboard/api/work/create", "/dashboard/api/work/continue",
                 "/dashboard/api/work/control", "/dashboard/api/work/approve"):
        assert no_origin.post(path, json={}).status_code == 403, path
    # ...while reads stay available.
    assert no_origin.get("/dashboard/api/work").status_code == 200
    assert no_origin.get("/dashboard/api/work/workers").status_code == 200


# -- rendering ----------------------------------------------------------------------

def test_the_page_renders_and_terminal_mode_is_untouched(wired):
    client, _service, _queue = wired
    response = client.get("/dashboard/work")
    assert response.status_code == 200
    assert "Work" in response.text
    assert client.get("/dashboard").status_code == 200


def test_the_lifecycle_is_drawn_not_just_named():
    """An operator should see WHERE a task is rather than decode one word."""
    for step in ("QUEUED", "PRECHECK", "READY", "DISPATCHING", "RUNNING",
                 "VERIFYING", "COMPLETED"):
        assert f"'{step}'" in WORK_HTML, step
    for bad in ("BLOCKED", "FAILED", "REVISION_REQUIRED", "DISPATCH_UNCERTAIN",
                "WAITING_SESSION", "PAUSED"):
        assert bad in WORK_HTML, bad


def test_progress_is_rendered_from_the_payload_not_invented():
    """No animation standing in for real state: every number comes from a
    read of the queue and the work store."""
    assert "data.progress.percent" in WORK_HTML
    assert "done_weight" in WORK_HTML and "total_weight" in WORK_HTML
    for invented in ("Math.random", "setTimeout(() => { percent", "fakeProgress"):
        assert invented not in WORK_HTML


def test_the_page_says_why_a_run_is_not_finished():
    assert "contract.unmet" in WORK_HTML
    assert "Chưa đạt" in WORK_HTML


def test_mobile_puts_the_action_first_and_cannot_overflow_sideways():
    assert "#detail { order:-1 }" in WORK_HTML
    # The one wide element is a table, and it gets its own scroller rather
    # than pushing the page sideways.
    assert ".wrap { overflow-x:auto }" in WORK_HTML
    assert "@media (max-width:900px)" in WORK_HTML


def test_the_work_menu_entry_and_badge_exist():
    assert 'href="/dashboard/work"' in dashboard_module.DASHBOARD_HTML
    assert "work-badge" in dashboard_module.DASHBOARD_HTML
    assert "'pill WORK'" in WORK_HTML


# -- rendered in a real browser ------------------------------------------------------

WORKS = {"works": [
    {"work_id": "work_1", "title": "Ship the overview", "state": "RUNNING",
     "project_id": "mesflow", "lane": "mesflow-work", "updated_at": "2026-09-13T04:00:00+00:00",
     "done_criteria": ["tests pass"], "metadata": {"constraints": ["do not touch prod"]},
     "goal": "users see project health", "paused_reason": None, "failure_reason": None,
     "created_at": "2026-09-13T03:00:00+00:00", "created_by": "hung",
     "progress": {"percent": 50, "done_tasks": 1, "total_tasks": 2,
                  "done_weight": 1.0, "total_weight": 2.0, "blocked_tasks": 1},
     "running_workers": ["mesflow-work"], "blocked_tasks": 1, "needs_you": 1,
     "needs_you_summary": "restart the controller"}]}

WORKERS = {"workers": [
    {"session": "mesflow-work", "node_id": "local", "agent_type": "claude",
     "state": "BUSY", "eligible": True, "reason": "ELIGIBLE", "detail": "ok",
     "current_task": {"task_id": "q1", "title": "build it", "status": "RUNNING"},
     "is_work_session": True},
    {"session": "offlinepos-work", "node_id": "hp-linux", "agent_type": "claude",
     "state": "OFFLINE", "eligible": False, "reason": "NODE_UNREACHABLE",
     "detail": "node hp-linux is offline", "current_task": None, "is_work_session": True}]}

DETAIL = {
    "work": WORKS["works"][0],
    "progress": WORKS["works"][0]["progress"],
    "lane_is_work_session": True,
    "tasks": [
        {"work_task_id": "wt1", "queue_task_id": "q1", "title": "build it", "weight": 1.0,
         "required": True, "queue_status": "RUNNING", "queue_position": 0, "priority": 5,
         "attempts": 1, "max_attempts": 3, "depends_on": [], "worker_session": "mesflow-work",
         "claimed_by": "queue-engine", "last_error": None, "coordinator_reason": None},
        {"work_task_id": "wt2", "queue_task_id": "q2", "title": "verify it", "weight": 1.0,
         "required": True, "queue_status": "BLOCKED", "queue_position": 1, "priority": 0,
         "attempts": 2, "max_attempts": 3, "depends_on": ["q1"],
         "worker_session": "mesflow-work", "claimed_by": None,
         "last_error": "no route to host", "coordinator_reason": None}],
    "approvals": [], "pending_approvals": [
        {"approval_id": "a1", "work_id": "work_1", "kind": "production_deploy",
         "summary": "restart the controller", "detail": None, "requested_by": "agent:worker",
         "requested_at": "2026-09-13T04:00:00+00:00", "decision": "PENDING"}],
    "artifacts": [{"artifact_id": "ar1", "kind": "commit", "reference": "7a50597",
                   "summary": "the change", "created_at": "2026-09-13T04:00:00+00:00"}],
    "events": [{"id": 1, "kind": "planned", "summary": "2 task(s) queued",
                "created_at": "2026-09-13T04:00:00+00:00", "actor": "hung"}],
    "contract": {"satisfied": False,
                 "unmet": [{"reason": "TASK_BLOCKED", "title": "verify it"}]},
}


@pytest.fixture(scope="module")
def page(request):
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        view = browser.new_page(viewport={"width": 1440, "height": 900})

        def route(req):
            url = req.request.url
            if "/dashboard/api/work/workers" in url:
                return req.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(WORKERS))
            if "/dashboard/api/work?work=" in url:
                return req.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(DETAIL))
            if "/dashboard/api/work" in url:
                return req.fulfill(status=200, content_type="application/json",
                                   body=json.dumps(WORKS))
            return req.fulfill(status=200, content_type="text/html", body=WORK_HTML)

        view.route("**/*", route)
        view.goto("http://terminal-mcp.test/dashboard/work", wait_until="domcontentloaded")
        # A VISIBLE card: the create form is a .card that starts hidden, and
        # waiting on the bare selector resolved to it and timed out.
        view.wait_for_selector("#works .card", timeout=20000)
        view.wait_for_timeout(400)
        yield view
        browser.close()


def test_the_page_renders_real_payloads(page):
    text = page.evaluate("() => document.body.innerText")
    for expected in ("Ship the overview", "mesflow", "build it", "verify it",
                     "restart the controller", "7a50597"):
        assert expected in text, expected


def test_need_from_you_is_above_the_run_header(page):
    """On a phone this is the reason the screen was opened."""
    need_top = page.evaluate(
        "() => document.querySelector('.need').getBoundingClientRect().top")
    overview_top = page.evaluate(
        "() => [...document.querySelectorAll('#detail .card')][0].getBoundingClientRect().top")
    assert need_top < overview_top


def test_approve_and_reject_are_both_present(page):
    labels = page.evaluate(
        "() => [...document.querySelectorAll('.need button')].map(b => b.textContent)")
    assert any("Duyệt" in l for l in labels)
    assert any("Từ chối" in l for l in labels)


def test_the_lifecycle_strip_marks_where_each_task_is(page):
    at = page.evaluate("() => [...document.querySelectorAll('.step.at')].map(s => s.textContent)")
    assert "RUNNING" in at, at
    bad = page.evaluate("() => [...document.querySelectorAll('.step.bad')].map(s => s.textContent)")
    assert "BLOCKED" in bad, bad


def test_workers_shows_only_work_sessions_with_state(page):
    text = page.evaluate("() => document.querySelector('#workers').innerText")
    assert "mesflow-work" in text and "offlinepos-work" in text
    assert "BUSY" in text and "OFFLINE" in text
    assert "node hp-linux is offline" in text, "an unusable worker says why"
    assert "terminal-mcp-main" not in text and "\nm1\n" not in text


def test_the_queue_table_shows_priority_attempt_and_worker(page):
    text = page.evaluate("() => document.querySelectorAll('table')[0].innerText")
    # Headers are uppercased by CSS, so compare case-insensitively; the row
    # VALUES are what actually prove the columns are populated.
    upper = text.upper()
    for header in ("PRIO", "ATTEMPT", "WORKER"):
        assert header in upper, header
    assert "queue-engine" in text, "the claiming worker is shown"
    assert "\t5\t1\t" in text, "priority and attempt come from the queue row"


def test_no_horizontal_overflow_on_a_phone(page):
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(400)
    try:
        assert page.evaluate(
            "() => document.documentElement.scrollWidth <= window.innerWidth")
        # The detail column comes first on a phone.
        detail_top = page.evaluate(
            "() => document.querySelector('#detail').getBoundingClientRect().top")
        list_top = page.evaluate(
            "() => document.querySelector('#list').getBoundingClientRect().top")
        assert detail_top < list_top
    finally:
        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(200)


def test_live_polling_can_be_paused(page):
    page.click("#liveBtn")
    page.wait_for_timeout(200)
    assert page.evaluate("() => document.querySelector('#liveBtn').getAttribute('aria-pressed')") == "false"
    page.click("#liveBtn")
    page.wait_for_timeout(200)


def test_an_unknown_agent_is_shown_as_unknown_not_guessed():
    """Never guess the agent; prefer real evidence, else say unknown.

    The session listing still carries no agent field, so the worker route now
    probes the session and the label falls back to the OBSERVED command
    before giving up. That is evidence, not a guess. Defaulting to 'shell'
    labelled a Claude worker wrong and remains forbidden.
    """
    assert "worker.agent_type || evidence.current_command || 'agent ?'" in WORK_HTML
    assert "|| 'shell'" not in WORK_HTML


def test_a_worker_busy_outside_the_queue_is_not_labelled_idle():
    """IDLE invited dispatch into a session a human was already using."""
    assert "RUNNING_MANUAL" in WORK_HTML
    assert "worker.busy_untracked" in WORK_HTML
    # The label has to say WHY it is busy; the state name alone cannot.
    assert "không do queue giao" in WORK_HTML
