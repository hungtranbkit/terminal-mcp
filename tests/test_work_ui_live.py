"""The Work page must show state CHANGING, against a real server.

The bug this covers: a task legitimately parked in VERIFYING rendered exactly
like one that had just arrived there, and the API payload was byte-identical
between polls. The page polled correctly every 6s and re-rendered an
identical DOM, so it looked frozen while it was in fact faithfully showing a
stall nobody could see.

No request interception here. A real Starlette app over real HTTP, real
SQLite, and a real browser -- mocking the API would prove only that the mock
changed.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.work_service import WorkService
from terminal_mcp.work_store import WorkStore


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live(tmp_path_factory, request):
    """A real server over a real temp database."""
    import uvicorn

    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.mcp_app import build_mcp
    from tests.conftest import _OPEN_ACCESS

    tmp = tmp_path_factory.mktemp("worklive")
    # The dashboard builds its OWN WorkService from the default store path, so
    # the temp databases have to be selected by environment rather than passed
    # in -- otherwise this test would read production state.
    import os

    os.environ["TERMINAL_MCP_QUEUE_DB"] = str(tmp / "queue.db")
    os.environ["TERMINAL_MCP_WORK_DB"] = str(tmp / "work.db")
    queue = QueueService(QueueStore(tmp / "queue.db"))
    work = WorkService(WorkStore(tmp / "work.db"), queue=queue)

    config = AppConfig(PermissionsConfig(True, False), ("test-*", "agent-*", "*-work"),
                       50, 20, session_access=_OPEN_ACCESS)
    service = TerminalService(config)
    server = build_mcp(service, queue=queue)
    register_dashboard(server, service, queue=queue)

    port = _free_port()
    app = server.streamable_http_app()
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if getattr(uv, "started", False):
            break
        time.sleep(0.05)
    yield {"base": f"http://127.0.0.1:{port}", "queue": queue, "work": work, "tmp": tmp}
    uv.should_exit = True
    thread.join(timeout=5)


def test_the_api_payload_advances_while_a_task_waits(live):
    """Two reads of a waiting task must differ, or the DOM cannot change."""
    queue, work = live["queue"], live["work"]
    created = work.create(title="live", goal="g", lane="ui-live-work",
                          tasks=[{"title": "t1", "prompt": "do it"}])
    if created.get("error"):
        pytest.skip(f"work lane refused: {created['error']}")
    work_id = created["work"]["work_id"]
    task_id = created["tasks"][0]["queue_task_id"]

    queue.store.transition_task(task_id, "PRECHECK", event_type="PRECHECK")
    first = work.status(work_id)
    time.sleep(1.2)
    second = work.status(work_id)

    def waiting(payload):
        for task in payload.get("tasks", []):
            if task.get("waiting_seconds") is not None:
                return task["waiting_seconds"]
        return None

    assert waiting(first) is not None, "a waiting task must report how long it has waited"
    assert waiting(second) > waiting(first), "the payload must advance between reads"


def test_a_long_wait_is_marked_stale_with_a_reason(live):
    queue, work = live["queue"], live["work"]
    created = work.create(title="stale", goal="g", lane="ui-stale-work",
                          tasks=[{"title": "t1", "prompt": "do it"}])
    if created.get("error"):
        pytest.skip(f"work lane refused: {created['error']}")
    task_id = created["tasks"][0]["queue_task_id"]
    for state in ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING"):
        queue.store.transition_task(task_id, state, event_type=state)
    # Backdate the VERIFYING transition to look like yesterday's stall.
    with queue.store._connection() as connection:
        connection.execute(
            "UPDATE queue_events SET timestamp = ? WHERE task_id = ? AND event_type = 'VERIFYING'",
            ("2026-09-13T00:00:00+00:00", task_id))
    payload = work.status(created["work"]["work_id"])
    task = next(t for t in payload["tasks"] if t.get("waiting_seconds") is not None)
    assert task["waiting_stale"] is True
    assert "completion marker" in task["waiting_reason"]


def test_the_browser_sees_the_dom_change_as_state_moves(live):
    """A real browser, a real server, and state changing underneath it.

    Asserts on the task's row in the QUEUE table, not on #detail's text.
    The detail pane draws a flow strip naming every state -- QUEUED,
    PRECHECK, READY, DISPATCHING, RUNNING, VERIFYING, COMPLETED -- so
    "RUNNING" is present in that text at all times and matching on it proves
    nothing. The row cell carries the state the task is actually in.
    """
    ROW = """() => {
        const rows = [...document.querySelectorAll('#detail table tr')];
        const row = rows.find((r) => r.innerText.includes('browser task'));
        return row ? row.innerText : '';
    }"""

    def row_contains(word):
        return ("""(w) => {
            const rows = [...document.querySelectorAll('#detail table tr')];
            const row = rows.find((r) => r.innerText.includes('browser task'));
            return !!row && row.innerText.includes(w);
        }""", word)
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright

    queue, work = live["queue"], live["work"]
    created = work.create(title="browser", goal="g", lane="ui-browser-work",
                          tasks=[{"title": "browser task", "prompt": "do it"}])
    if created.get("error"):
        pytest.skip(f"work lane refused: {created['error']}")
    work_id = created["work"]["work_id"]
    task_id = created["tasks"][0]["queue_task_id"]
    queue.store.transition_task(task_id, "PRECHECK", event_type="PRECHECK")

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console",
                lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        try:
            page.goto(live["base"] + "/dashboard/work", wait_until="domcontentloaded")
            page.wait_for_selector("#works .card", timeout=20000)
            # Open this run so its TASK rows render.
            page.evaluate("(id) => { state.selected = id; load(); }", work_id)
            page.wait_for_selector("#detail table tr", timeout=20000)
            expression, word = row_contains("PRECHECK")
            page.wait_for_function(expression, arg=word, timeout=20000)
            before = page.evaluate(ROW)

            # Move the task for real, in the database the page is reading.
            for state in ("READY", "DISPATCHING", "RUNNING"):
                queue.store.transition_task(task_id, state, event_type=state)

            # The page polls every 6s; wait for the change rather than sleeping
            # a fixed amount, so a slow box does not fail a working page.
            expression, word = row_contains("RUNNING")
            page.wait_for_function(expression, arg=word, timeout=25000)
            after = page.evaluate(ROW)

            assert "PRECHECK" in before and "RUNNING" not in before
            assert "RUNNING" in after, f"task row never showed RUNNING: {after!r}"
            assert before != after, "the DOM did not change although the task did"
            # A JS exception would freeze the page far more effectively than
            # any backend stall, so it must not hide behind a pass.
            assert not errors, f"javascript errors on the Work page: {errors[:3]}"
        finally:
            browser.close()


def test_a_waiting_task_shows_its_age_and_that_age_advances(live):
    """The value that makes the DOM differ between polls must be visible."""
    sync_playwright = pytest.importorskip(
        "playwright.sync_api", reason="playwright not installed").sync_playwright

    queue, work = live["queue"], live["work"]
    created = work.create(title="ageing", goal="g", lane="ui-age-work",
                          tasks=[{"title": "ageing task", "prompt": "do it"}])
    if created.get("error"):
        pytest.skip(f"work lane refused: {created['error']}")
    work_id = created["work"]["work_id"]
    task_id = created["tasks"][0]["queue_task_id"]
    for state in ("PRECHECK", "READY", "DISPATCHING", "RUNNING", "VERIFYING"):
        queue.store.transition_task(task_id, state, event_type=state)

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(
                args=["--no-sandbox", "--no-zygote", "--single-process", "--disable-gpu"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(live["base"] + "/dashboard/work", wait_until="domcontentloaded")
            page.wait_for_selector("#works .card", timeout=20000)
            page.evaluate("(id) => { state.selected = id; load(); }", work_id)
            page.wait_for_function(
                "() => document.querySelector('#detail').innerText.includes('đang chờ')",
                timeout=20000)
            first = page.inner_text("#detail")
            # Two poll cycles: the age must move on, which is precisely what
            # the frozen-looking page was missing.
            page.wait_for_timeout(14000)
            second = page.inner_text("#detail")
            assert "đang chờ" in first and "VERIFYING" in first
            assert first != second, "the waiting age never advanced between polls"
        finally:
            browser.close()
