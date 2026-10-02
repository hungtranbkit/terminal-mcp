"""Per-session current task label ("Task hiện tại" on /dashboard/live).

Covers the label rule (task_labels.py), its storage in the audit DB, every
submission surface that writes it (compact send/send_wait/supervise/create,
native create_session/enqueue_task/delete_session) and the Live Session
Monitor payload + page that show it.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from terminal_mcp.audit import AuditStore
from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.live_sessions import LiveSessionMonitor
from terminal_mcp.live_sessions_page import LIVE_SESSIONS_HTML
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.run_journal import RunJournalStore
from terminal_mcp.task_labels import (SUMMARY_MAX_CHARS, TaskLabeler, is_continuation,
                                      is_substantial_prompt, summarize_prompt, title_from)

NOW = 1_800_000_000.0
IDLE = {"exists": True, "state": "IDLE", "reason": "shell prompt is back"}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# -- summarizer / rule units -------------------------------------------------

def test_summarizer_normalizes_markdown_and_takes_first_meaningful_line():
    prompt = ("\n\n## **Fix** the [login](https://x/y) CSRF bug. Then deploy.\n"
              "- step one\n```py\nprint(1)\n```\n")
    assert summarize_prompt(prompt) == "Fix the login CSRF bug."
    assert summarize_prompt("```\ncode only\n```\n---\n- [ ] Add tests for the parser") \
        == "Add tests for the parser"
    assert summarize_prompt("   ") is None and summarize_prompt(None) is None


def test_summarizer_caps_length_and_redacts_secrets():
    long = "Implement " + " ".join(f"word{i}" for i in range(80))
    summary = summarize_prompt(long)
    assert len(summary) <= SUMMARY_MAX_CHARS and summary.endswith("…")
    assert "sk-abc123secret" not in summarize_prompt("export OPENAI_API_KEY=sk-abc123secret and run it")


def test_continuation_detection():
    for text in ("y", "Yes", "continue", "tiếp tục", "ok, go ahead", "approve", "retry please", "  "):
        assert is_continuation(text), text
    assert not is_continuation("Refactor the billing module to use the new pricing API")
    assert not is_substantial_prompt("continue with the next step please")
    assert is_substantial_prompt("Refactor the billing module to use the new pricing API everywhere")
    assert title_from(None, {"task_summary": "Billing refactor"}) == "Billing refactor"
    assert title_from("  ", None) is None


# -- compact send / send_wait -------------------------------------------------

class FakeController:
    local_node_id = "local"

    def __init__(self):
        self.send_result = {"delivery_state": "SUBMIT_CONFIRMED", "press_enter": True,
                            "enter_sent": True}
        self.statuses: dict[str, dict] = {}

    def terminal_send_text(self, session, text, press_enter, dry_run, **kwargs):
        node, _, bare = session.rpartition("/")
        return {"session": bare, **({"node_id": node} if node else {}), **self.send_result}

    def terminal_status(self, session):
        return self.statuses.get(session, {**IDLE, "session": session})

    def terminal_status_bounded(self, session, timeout_seconds):
        return self.terminal_status(session)

    def terminal_tail(self, session, lines):
        return {"session": session, "output": "$ ", "truncated": False}


class FakeTerminal:
    def terminal_get_binding(self, binding):
        return {"error": "BINDING_NOT_FOUND"}


@pytest.fixture
def rig(tmp_path):
    controller = FakeController()
    compact = CompactTerminalTools(FakeTerminal(), controller,
                                   run_journal=RunJournalStore(tmp_path / "journal.db"))
    audit = AuditStore(tmp_path / "audit.db")
    compact.task_labels = TaskLabeler(audit, local_node_id="local")
    return compact, controller, audit


def _label(audit, session, node="local"):
    return audit.get_task_label(node, session)


def test_explicit_title_persists_and_second_title_replaces_first(rig):
    compact, _controller, audit = rig
    first = compact.turn(action="send", target="worker", text="do the thing",
                         title="Fix login CSRF")
    assert first["status"] == "SUBMIT_CONFIRMED"
    assert first["current_task"]["summary"] == "Fix login CSRF"
    assert _label(audit, "worker")["source"] == "title"
    compact.turn(action="send", target="worker", text="new work", title="Add task label to monitor")
    assert _label(audit, "worker")["summary"] == "Add task label to monitor"


def test_continuation_send_without_title_never_replaces(rig):
    compact, _controller, audit = rig
    compact.turn(action="send", target="worker", text="build it", title="Build the release")
    for text in ("y", "continue", "approve", "ok go ahead",
                 "Now also refactor the billing module to use the new pricing API everywhere"):
        result = compact.turn(action="send", target="worker", text=text)
        assert "current_task" not in result
    assert _label(audit, "worker")["summary"] == "Build the release"


def test_untitled_substantial_send_only_labels_an_unlabeled_session(rig):
    compact, _controller, audit = rig
    compact.turn(action="send", target="fresh", text="y")
    assert _label(audit, "fresh") is None
    compact.turn(action="send", target="fresh",
                 text="Refactor the billing module to use the new pricing API everywhere.\nDetails...")
    label = _label(audit, "fresh")
    assert label["source"] == "prompt_fallback"
    assert label["summary"].startswith("Refactor the billing module")


def test_failed_send_does_not_label(rig):
    compact, controller, audit = rig
    controller.send_result = {"delivery_state": "BLOCKED", "error": "ACCESS_DENIED"}
    result = compact.turn(action="send", target="worker", text="x", title="Should not stick")
    assert result["status"] == "BLOCKED"
    assert _label(audit, "worker") is None


def test_send_wait_title_labels_direct_session_without_queue(rig):
    compact, _controller, audit = rig
    result = compact.turn(action="send_wait", target="direct-1", text="pytest -q",
                          title="Run the regression suite", timeout=2, poll_interval=1)
    assert result["send"]["status"] == "SUBMIT_CONFIRMED"
    assert result["current_task"]["summary"] == "Run the regression suite"
    assert _label(audit, "direct-1")["source"] == "title"


def test_metadata_task_summary_is_a_title_alias(rig):
    compact, _controller, audit = rig
    compact.turn(action="send", target="worker", text="go", metadata={"task_summary": "Ship v2"})
    assert _label(audit, "worker")["summary"] == "Ship v2"


def test_remote_target_is_keyed_by_node(rig):
    compact, _controller, audit = rig
    compact.turn(action="send", target="m910/builder", text="go", title="Remote build")
    assert _label(audit, "builder", node="m910")["summary"] == "Remote build"
    assert _label(audit, "builder") is None


def test_label_store_failure_never_breaks_the_send(rig):
    compact, _controller, _audit = rig

    class Broken:
        def __getattr__(self, name):
            raise RuntimeError("db gone")

    compact.task_labels = TaskLabeler(Broken(), local_node_id="local")
    result = compact.turn(action="send", target="worker", text="go", title="T")
    assert result["status"] == "SUBMIT_CONFIRMED"
    assert "current_task" not in result


def test_compact_create_session_title_overrides_prompt_summary(rig):
    compact, _controller, audit = rig
    compact.handlers["create_session"] = lambda name, **kwargs: {"session": name, "state": "READY"}
    result = compact.turn(action="create_session", target="new-1", initial_prompt="long prompt",
                          title="Port the parser")
    assert result["status"] == "OK"
    assert _label(audit, "new-1")["summary"] == "Port the parser"


def test_supervise_labels_with_title_or_prompt(rig):
    compact, _controller, audit = rig

    class Supervisor:
        def start(self, target, **kwargs):
            return {"status": "RUNNING", "task_id": "dt_1"}

    compact.direct_tasks = Supervisor()
    compact.turn(action="supervise", target="sup", text="Migrate the orders table to the v3 schema")
    label = _label(audit, "sup")
    assert label["source"] == "supervised" and label["task_id"] == "dt_1"
    compact.turn(action="supervise", target="sup", text="x", title="Orders migration")
    assert _label(audit, "sup")["summary"] == "Orders migration"


# -- native tools through the real built server -------------------------------

def _server(tmp_path):
    from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp

    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)))
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    return build_mcp(service), service


def test_create_session_initial_prompt_labels_and_empty_create_clears(tmp_path):
    server, service = _server(tmp_path)
    controller = server.compact_tools.controller
    local = controller.local_node_id
    controller.terminal_create_session = lambda name, *a, **k: {
        "session": name, "state": "READY", "node_id": local}
    create = server.compact_tools.handlers["create_session"]
    result = create("test-new", initial_prompt="# Task\nAdd retry to the uploader. More text.")
    assert result["current_task"]["summary"] == "Add retry to the uploader."
    assert service.audit.get_task_label(local, "test-new")["source"] == "initial_prompt"
    # Recreated empty under the same name: the old label must not survive.
    create("test-new")
    assert service.audit.get_task_label(local, "test-new") is None
    # Failed create changes nothing.
    service.audit.set_task_label(node_id=local, session="test-x", summary="keep", source="title")
    controller.terminal_create_session = lambda name, *a, **k: {"error": "SESSION_ALREADY_EXISTS"}
    create("test-x")
    assert service.audit.get_task_label(local, "test-x")["summary"] == "keep"


def test_enqueue_labels_durable_task_when_queue_opted_in(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_ENABLE_QUEUE", "1")
    server, service = _server(tmp_path)
    local = server.compact_tools.controller.local_node_id
    enqueue = server.compact_tools.handlers["enqueue_task"]
    accepted = enqueue("test-lane", "Write the migration for invoices.\nmore", request_key="rk-1")
    assert accepted.get("task_id"), accepted
    label = service.audit.get_task_label(local, "test-lane")
    assert label["source"] == "durable_task"
    assert label["summary"] == "Write the migration for invoices."
    assert label["task_id"] == accepted["task_id"]
    enqueue("test-lane", "whatever", title="Invoices v2")
    assert service.audit.get_task_label(local, "test-lane")["summary"] == "Invoices v2"


def test_enqueue_disabled_by_default_writes_no_label(tmp_path, monkeypatch):
    monkeypatch.delenv("TERMINAL_MCP_ENABLE_QUEUE", raising=False)
    server, service = _server(tmp_path)
    local = server.compact_tools.controller.local_node_id
    result = server.compact_tools.handlers["enqueue_task"]("test-lane", "x", title="T")
    assert result.get("error") or result.get("status") == "FAILED"
    assert service.audit.get_task_label(local, "test-lane") is None


def test_terminal_turn_help_tells_callers_to_pass_title(tmp_path):
    server, _service = _server(tmp_path)
    tool = next(t for t in server._tool_manager.list_tools() if t.name == "terminal_turn")
    assert 'title="<short summary>"' in tool.description
    assert "Omit title" in tool.description


# -- live monitor payload -----------------------------------------------------

class MonitorController:
    local_node_id = "local"

    def __init__(self):
        self.rows: list[dict] = []
        self.status: dict[str, object] = {}

    def add(self, name, *, node_id="local", created=NOW - 60):
        self.rows.append({"name": name, "node_id": node_id, "node_name": node_id,
                          "created": _iso(created), "effective_read": True})
        key = name if node_id == "local" else f"{node_id}/{name}"
        self.status[key] = {**IDLE, "last_output": "$ ", "session": name}

    def terminal_list_sessions(self):
        return {"sessions": [dict(r) for r in self.rows], "unreachable_nodes": []}

    def terminal_status(self, target):
        value = self.status[target]
        if isinstance(value, Exception):
            raise value
        return dict(value)


def _label_at(audit, node, session, summary, at, source="title", task_id=None):
    audit.set_task_label(node_id=node, session=session, summary=summary, source=source,
                         task_id=task_id)
    with audit._connection() as connection:
        connection.execute("UPDATE session_task_labels SET updated_at = ? WHERE node_id = ? AND session = ?",
                           (_iso(at), node, session))


@pytest.fixture
def mon(tmp_path):
    controller = MonitorController()
    audit = AuditStore(tmp_path / "audit.db")
    queue = QueueStore(tmp_path / "queue.db")
    monitor = LiveSessionMonitor(controller, audit=audit, queue_store=queue, ttl_seconds=0,
                                 clock=lambda: NOW)
    return controller, audit, queue, monitor


def _entry(payload, name):
    return next(s for s in payload["sessions"] if s["session"] == name)


def test_label_appears_in_live_payload_and_survives_idle(mon):
    controller, audit, _queue, monitor = mon
    controller.add("worker")
    _label_at(audit, "local", "worker", "Fix login CSRF", NOW - 20)
    entry = _entry(monitor.snapshot(), "worker")
    assert entry["active"] is False                       # finished turn ...
    assert entry["current_task"]["summary"] == "Fix login CSRF"   # ... keeps its task
    assert entry["current_task"]["updated_at"] == pytest.approx(NOW - 20)
    assert entry["current_task"]["source"] == "title"
    token = entry["change_token"]
    _label_at(audit, "local", "worker", "Second task", NOW - 5)
    again = _entry(monitor.snapshot(force=True), "worker")
    assert again["current_task"]["summary"] == "Second task"
    assert again["change_token"] != token                 # the page repaints the card


def test_empty_session_has_no_current_task(mon):
    controller, _audit, _queue, monitor = mon
    controller.add("blank")
    assert _entry(monitor.snapshot(), "blank")["current_task"] is None


def test_durable_task_fallback_when_no_label(mon):
    controller, _audit, queue, monitor = mon
    controller.add("lane-a")
    queue.append_tasks("lane-a", [{"prompt": "Implement the widget", "title": "Widget"}])
    current = _entry(monitor.snapshot(), "lane-a")["current_task"]
    assert current["summary"] == "Widget" and current["origin"] == "task"
    assert current["source"] == "queue_task"


def test_label_wins_over_durable_task(mon):
    controller, audit, queue, monitor = mon
    controller.add("lane-b")
    queue.append_tasks("lane-b", [{"prompt": "p", "title": "Old queued"}])
    _label_at(audit, "local", "lane-b", "Newer titled task", NOW - 1)
    assert _entry(monitor.snapshot(), "lane-b")["current_task"]["summary"] == "Newer titled task"


def test_label_older_than_session_is_ignored_as_orphan(mon):
    controller, audit, _queue, monitor = mon
    controller.add("reused", created=NOW - 60)
    _label_at(audit, "local", "reused", "From a deleted session", NOW - 3600)
    _label_at(audit, "local", "gone", "Session no longer exists", NOW - 10)
    payload = monitor.snapshot()
    assert _entry(payload, "reused")["current_task"] is None
    assert all(s["session"] != "gone" for s in payload["sessions"])


def test_prompt_derived_summary_is_withheld_without_preview_rights(mon):
    controller, audit, _queue, monitor = mon
    controller.add("p1")
    controller.add("p2")
    _label_at(audit, "local", "p1", "Prompt summary", NOW - 5, source="prompt_fallback")
    _label_at(audit, "local", "p2", "Chosen title", NOW - 5, source="title")
    payload = monitor.snapshot(include_previews=False)
    assert _entry(payload, "p1")["current_task"]["summary"] is None
    assert _entry(payload, "p1")["current_task"]["summary_withheld"] is True
    assert _entry(payload, "p2")["current_task"]["summary"] == "Chosen title"


def test_remote_and_offline_rows_keep_their_label(mon):
    controller, audit, _queue, monitor = mon
    controller.add("builder", node_id="m910")
    controller.add("down", node_id="hp")
    controller.status["hp/down"] = ConnectionError("node unreachable")
    _label_at(audit, "m910", "builder", "Remote build", NOW - 5)
    _label_at(audit, "hp", "down", "Offline work", NOW - 5)
    _label_at(audit, "local", "builder", "Wrong node", NOW - 5)
    payload = monitor.snapshot()
    assert _entry(payload, "builder")["current_task"]["summary"] == "Remote build"
    down = _entry(payload, "down")
    assert down["state"] == "OFFLINE"
    assert down["current_task"]["summary"] == "Offline work"


def test_label_source_failure_degrades_without_breaking(mon):
    controller, audit, _queue, monitor = mon
    controller.add("worker")

    def boom(**_kwargs):
        raise RuntimeError("locked")

    audit.task_label_index = boom
    payload = monitor.snapshot()
    assert _entry(payload, "worker")["current_task"] is None
    assert any(e.startswith("task_labels:") for e in payload["source_errors"])


def test_monitor_without_label_support_still_works(tmp_path):
    controller = MonitorController()
    controller.add("worker")

    class OldAudit:
        def latest_input_index(self, since):
            return {}

    monitor = LiveSessionMonitor(controller, audit=OldAudit(), ttl_seconds=0, clock=lambda: NOW)
    assert _entry(monitor.snapshot(), "worker")["current_task"] is None


# -- page ---------------------------------------------------------------------

def test_page_shows_task_hien_tai_near_name_and_highlights_changes():
    html = LIVE_SESSIONS_HTML
    assert "Task hiện tại" in html
    assert "Chưa gắn task" in html
    assert "current_task" in html
    # Placed right after the name row, before the meta stats.
    assert html.index("card.append(currentTask(s, now));") < html.index("const meta = el('div', 'meta');")
    # Relabel detection across polls + a timed highlight.
    assert "relabeledAt" in html and "cur-changed" in html and "@keyframes relabel" in html
    assert "TASK MỚI" in html
    # 1-2 line clamp, not hidden in the expandable tail.
    assert "-webkit-line-clamp:2" in html
    # Summary goes in via textContent (el()), never innerHTML.
    assert "innerHTML" not in html


def test_audit_migration_is_idempotent(tmp_path):
    path = tmp_path / "audit.db"
    AuditStore(path).set_task_label(node_id="n", session="s", summary="a", source="title")
    reopened = AuditStore(path)
    assert reopened.get_task_label("n", "s")["summary"] == "a"
    assert reopened.task_label_index()[("n", "s")]["source"] == "title"
    assert reopened.delete_task_label("n", "s") == 1
