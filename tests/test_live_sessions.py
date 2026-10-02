"""Live Session Monitor: aggregation, direct sends, task mapping, isolation, routes.

The monitor's promise is that a session ChatGPT just created and direct-sent
work to shows up -- active, with what it was asked -- even though no durable
task row exists for it. These tests drive LiveSessionMonitor with a fake
controller (the same node-routing surface the real one exposes) plus REAL
audit/queue/direct-task stores in tmp_path, then check the HTTP surfaces.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from terminal_mcp.audit import AuditStore
from terminal_mcp.direct_task import DirectTaskStore
from terminal_mcp.live_sessions import (INPUT_ACTIVE_SECONDS, SETTLE_SECONDS, LiveSessionMonitor,
                                        live_state)
from terminal_mcp.queue_store import QueueStore

NOW = 1_800_000_000.0
SHELL_RUNNING = {"exists": True, "state": "RUNNING",
                 "reason": "current command is 'bash'; tmux activity age is 1s"}
SHELL_IDLE = {"exists": True, "state": "IDLE",
              "reason": "shell prompt is back at the bottom of the pane; the command has finished"}
SLEEP_RUNNING = {"exists": True, "state": "RUNNING",
                 "reason": "current command is 'sleep'; tmux activity age is 2s"}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class Clock:
    def __init__(self, at: float = NOW) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at


class FakeController:
    """terminal_list_sessions / terminal_status / terminal_tail, nothing else.

    Write methods are absent on purpose: the monitor must never need one.
    """
    local_node_id = "local"

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.status: dict[str, dict] = {}
        self.unreachable: list[dict] = []
        self.status_calls: list[str] = []
        self.tail_calls: list[str] = []
        self.list_error: Exception | None = None

    def add(self, name: str, *, node_id: str = "local", created: float = NOW - 30,
            readable: bool = True, status: dict | None = None, output: str = "") -> None:
        self.rows.append({"name": name, "node_id": node_id, "node_name": node_id.upper(),
                          "created": _iso(created), "attached": False,
                          "effective_read": readable})
        key = name if node_id == "local" else f"{node_id}/{name}"
        self.status[key] = {**(status or SHELL_IDLE), "last_output": output, "session": name}

    def set(self, target: str, *, status: dict | None = None, output: str | None = None) -> None:
        current = self.status[target]
        if status is not None:
            current = {**status, "last_output": current.get("last_output", "")}
        if output is not None:
            current = {**current, "last_output": output}
        self.status[target] = current

    def terminal_list_sessions(self):
        if self.list_error:
            raise self.list_error
        return {"sessions": [dict(r) for r in self.rows], "unreachable_nodes": list(self.unreachable)}

    def terminal_status(self, target):
        self.status_calls.append(target)
        value = self.status[target]
        if isinstance(value, Exception):
            raise value
        return dict(value)

    def terminal_tail(self, target, lines=None, *, ansi=False):
        self.tail_calls.append(target)
        return {"output": "\n".join(f"line {i}" for i in range(lines or 10))}


@pytest.fixture
def rig(tmp_path):
    clock = Clock()
    controller = FakeController()
    audit = AuditStore(tmp_path / "audit.db")
    queue = QueueStore(tmp_path / "queue.db")
    direct = DirectTaskStore(tmp_path / "direct.db")
    monitor = LiveSessionMonitor(controller, audit=audit, queue_store=queue,
                                 direct_tasks=direct, ttl_seconds=0, clock=clock)
    return clock, controller, audit, queue, direct, monitor


def _by(payload, name):
    return next(s for s in payload["sessions"] if s["session"] == name)


def _record_send(audit, session, text, at):
    # Real AuditStore rows, then the timestamp pinned to the test clock.
    audit.record(action="send_text", session=session, text=text, result="SENT", press_enter=True)
    with audit._connection() as connection:
        connection.execute("UPDATE input_audit SET timestamp = ? WHERE id = (SELECT MAX(id) FROM input_audit)",
                           (_iso(at),))


# -- direct send, no durable task -------------------------------------------

def test_direct_send_session_without_task_is_visible_and_active(rig):
    clock, controller, audit, _queue, _direct, monitor = rig
    controller.add("chatgpt-direct-1", status=SHELL_RUNNING, output="$ for i in 1 2 3; do ...")
    audit.record(action="create_session", session="chatgpt-direct-1", result="CREATED")
    _record_send(audit, "chatgpt-direct-1", "for i in 1 2 3; do echo step $i; sleep 2; done", NOW - 3)

    entry = _by(monitor.snapshot(), "chatgpt-direct-1")

    assert entry["task"] is None
    assert entry["active"] is True
    assert entry["state"] == "RUNNING"
    assert entry["activity_source"] == "direct_input"
    assert entry["is_new"] is True                      # created 30s ago
    assert "echo step" in entry["last_input_preview"]
    assert entry["last_input_at"] == pytest.approx(NOW - 3, abs=1)
    assert entry["last_activity_age_seconds"] == pytest.approx(3, abs=1)
    assert entry["elapsed_seconds"] == pytest.approx(30, abs=1)


def test_previews_are_withheld_when_the_caller_may_not_read_them(rig):
    _clock, controller, audit, *_rest, monitor = rig
    controller.add("s1", status=SHELL_RUNNING)
    _record_send(audit, "s1", "secret-ish prompt", NOW - 2)
    entry = _by(monitor.snapshot(include_previews=False), "s1")
    assert entry["last_input_preview"] is None
    assert entry["last_input_at"] is not None


def test_new_session_appears_on_next_poll_and_sorts_first(rig):
    clock, controller, _audit, *_rest, monitor = rig
    controller.add("old-idle", created=NOW - 86400, status=SHELL_IDLE, output="$ ")
    first = monitor.snapshot()
    assert [s["session"] for s in first["sessions"]] == ["old-idle"]
    assert _by(first, "old-idle")["is_new"] is False

    clock.at += 2
    controller.add("brand-new", created=clock.at - 1, status=SLEEP_RUNNING, output="step 1")
    second = monitor.snapshot()
    assert second["sessions"][0]["session"] == "brand-new"
    assert second["sessions"][0]["is_new"] is True
    assert second["sessions"][0]["active"] is True
    assert second["counts"]["active"] == 1


def test_running_output_then_prompt_back_transitions_to_idle_with_completion(rig):
    clock, controller, *_rest, monitor = rig
    controller.add("work", created=NOW - 600, status=SHELL_IDLE, output="$ ")
    monitor.snapshot()                                   # baseline

    clock.at += 2
    controller.set("work", status=SLEEP_RUNNING, output="$ ./steps.sh\nstep 1")
    running = _by(monitor.snapshot(), "work")
    assert running["state"] == "RUNNING" and running["active"]
    assert running["is_new"] is True                     # newly activated

    clock.at += 2
    controller.set("work", output="$ ./steps.sh\nstep 1\nstep 2")
    mid = _by(monitor.snapshot(), "work")
    assert mid["state"] == "RUNNING"
    assert mid["lines"][-1] == "step 2"
    assert mid["change_token"] != running["change_token"]
    assert mid["output_change_witnessed"] is True

    clock.at += 1
    controller.set("work", status=SHELL_IDLE, output="$ ./steps.sh\nstep 1\nstep 2\nALL DONE\n$ ")
    clock.at += SETTLE_SECONDS + 1
    monitor.snapshot()
    clock.at += SETTLE_SECONDS + 1
    done = _by(monitor.snapshot(), "work")
    assert done["state"] == "IDLE"
    assert done["active"] is False
    assert done["completion"]["summary"] in ("$", "ALL DONE")
    assert done["finished_at"] is not None
    assert done["recent"] is True


def test_live_state_prefers_finished_prompt_over_recent_output():
    class Change:
        witnessed = True
        age_seconds = SETTLE_SECONDS + 0.5
        watched_for = 30.0
    state, _reason, _src = live_state(SHELL_IDLE, "$ ", change=Change(), last_input_age=2)
    assert state == "IDLE"


def test_stale_input_alone_does_not_keep_a_session_active():
    state, _reason, src = live_state({"exists": True, "state": "UNKNOWN",
                                      "reason": "current command is 'claude'"}, "",
                                     change=None, last_input_age=INPUT_ACTIVE_SECONDS + 5)
    assert state != "RUNNING" and src is None


# -- durable task mapping ----------------------------------------------------

def test_queue_task_is_mapped_by_session(rig):
    _clock, controller, _audit, queue, _direct, monitor = rig
    controller.add("lane-a", status=SHELL_IDLE, output="$ ")
    queue.append_tasks("lane-a", [{"prompt": "Implement the widget end to end", "title": "Widget"}])
    entry = _by(monitor.snapshot(), "lane-a")
    assert entry["task"]["kind"] == "queue"
    assert entry["task"]["title"] == "Widget"
    assert "widget" in entry["task"]["summary"].lower()
    assert entry["task"]["status"] == "QUEUED"
    assert entry["active"] is True                       # open task keeps it business-active


def test_supervised_direct_task_is_mapped_and_wins(rig):
    _clock, controller, _audit, queue, direct, monitor = rig
    controller.add("sup", status=SHELL_IDLE, output="$ ")
    queue.append_tasks("sup", [{"prompt": "older queued", "title": "Q"}])
    direct.insert({"task_id": "dt_abc", "target": "sup", "mode": "shell", "state": "RUNNING",
                   "steps": ["run the migration", "verify"], "max_continuations": 6,
                   "created_at": NOW - 50, "updated_at": NOW - 5})
    entry = _by(monitor.snapshot(), "sup")
    assert entry["task"]["kind"] == "supervised"
    assert entry["task"]["id"] == "dt_abc"
    assert entry["task"]["title"] == "run the migration"
    assert entry["active"] is True


# -- isolation / degradation -------------------------------------------------

def test_one_failing_status_does_not_blank_the_others(rig):
    _clock, controller, *_rest, monitor = rig
    controller.add("good", status=SLEEP_RUNNING, output="ok")
    controller.add("bad", node_id="m910")
    controller.status["m910/bad"] = RuntimeError("node timed out")
    controller.unreachable = [{"node_id": "dell-5530", "node_name": "Dell", "status": "timeout"}]
    payload = monitor.snapshot()
    assert _by(payload, "good")["state"] == "RUNNING"
    bad = _by(payload, "bad")
    assert bad["state"] == "OFFLINE"
    assert "timed out" in (bad["error"] or bad["reason"])
    assert bad["qualified"] == "m910/bad"
    assert "m910/bad" in controller.status_calls           # remote rows are node-qualified
    assert payload["unreachable_nodes"][0]["node_id"] == "dell-5530"


def test_listing_failure_returns_an_empty_degraded_payload(rig):
    _clock, controller, *_rest, monitor = rig
    controller.list_error = RuntimeError("tmux gone")
    payload = monitor.snapshot()
    assert payload["sessions"] == []
    assert any("tmux gone" in e for e in payload["source_errors"])


def test_broken_context_sources_degrade_without_failing(rig):
    _clock, controller, *_rest = rig

    class Broken:
        def latest_input_index(self, since):
            raise RuntimeError("audit locked")

        def live_task_rows(self, since):
            raise RuntimeError("queue locked")

    controller.add("s", status=SLEEP_RUNNING, output="x")
    monitor = LiveSessionMonitor(controller, audit=Broken(), queue_store=Broken(), ttl_seconds=0,
                                 clock=Clock())
    payload = monitor.snapshot()
    assert _by(payload, "s")["state"] == "RUNNING"
    assert len(payload["source_errors"]) == 2


def test_restricted_session_is_listed_without_reading_its_pane(rig):
    _clock, controller, *_rest, monitor = rig
    controller.add("private", readable=False)
    entry = _by(monitor.snapshot(), "private")
    assert entry["state"] == "RESTRICTED"
    assert entry["lines"] == []
    assert "private" not in controller.status_calls


def test_expand_fetches_a_longer_tail_only_for_requested_sessions(rig):
    _clock, controller, *_rest, monitor = rig
    controller.add("a", status=SLEEP_RUNNING, output="x")
    controller.add("b", status=SLEEP_RUNNING, output="y")
    payload = monitor.snapshot(expand=("local/a",))
    assert len(_by(payload, "a")["tail_full"]) == 120
    assert _by(payload, "b")["tail_full"] is None
    assert controller.tail_calls == ["a"]


def test_snapshot_is_cached_within_ttl(tmp_path):
    controller = FakeController()
    controller.add("s", status=SLEEP_RUNNING, output="x")
    clock = Clock()
    monitor = LiveSessionMonitor(controller, ttl_seconds=5, clock=clock)
    monitor.snapshot()
    clock.at += 1
    again = monitor.snapshot()
    assert again["cached"] is True
    assert controller.status_calls == ["s"]


# -- routes ------------------------------------------------------------------

def _servers(tmp_path):
    from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp
    from terminal_mcp.webauth import WebAuthStore
    from terminal_mcp.webauth_dashboard import register_webauth_dashboard

    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)))
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    webauth = WebAuthStore(tmp_path / "webauth.db")
    webauth.create_or_replace_user("admin", "correct horse battery staple 123")
    server = build_mcp(service)
    register_dashboard(server, service, webauth=webauth)
    register_webauth_dashboard(server, service, webauth)
    return server


def test_dashboard_live_page_and_api(tmp_path):
    server = _servers(tmp_path)
    client = TestClient(server.streamable_http_app(), base_url="https://testserver")
    page = client.get("/dashboard/live")
    assert page.status_code == 200
    assert "/dashboard/api/live-sessions" in page.text
    assert "data-global-nav" in page.text
    assert 'href="/dashboard/live"' in page.text
    api = client.get("/dashboard/api/live-sessions")
    assert api.status_code == 200
    body = api.json()
    assert body["read_only"] is True
    assert isinstance(body["sessions"], list)
    assert api.headers["cache-control"] == "no-store"
    # Both surfaces share one monitor (one change tracker).
    assert getattr(server, "live_session_monitor", None) is not None


def test_app_live_mirror_requires_login_and_rewrites_api(tmp_path):
    server = _servers(tmp_path)
    anon = TestClient(server.streamable_http_app(), base_url="https://testserver",
                      headers={"Origin": "https://testserver"})
    assert anon.get("/app/live", follow_redirects=False).status_code == 303
    assert anon.get("/app/api/live-sessions").status_code == 401
    login = anon.post("/login", data={"username": "admin",
                                      "password": "correct horse battery staple 123"},
                      follow_redirects=False)
    assert login.status_code == 303
    page = anon.get("/app/live")
    assert page.status_code == 200
    assert "/app/api/live-sessions" in page.text
    assert "/dashboard/api/" not in page.text
    api = anon.get("/app/api/live-sessions")
    assert api.status_code == 200 and "sessions" in api.json()


def test_dashboard_live_routes_are_read_guarded_when_access_is_configured(tmp_path):
    from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig,
                                     PermissionsConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp

    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)),
                       dashboard=DashboardConfig(
                           cloudflare_access_team_domain="test-team.cloudflareaccess.com",
                           cloudflare_access_audience="test-aud"))
    service = TerminalService(config, audit=AuditStore(tmp_path / "audit.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())
    for path in ("/dashboard/live", "/dashboard/api/live-sessions"):
        response = client.get(path)
        assert response.status_code == 403, path
        assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"
