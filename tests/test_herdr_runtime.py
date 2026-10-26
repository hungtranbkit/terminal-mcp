"""Herdr pilot adapter: flag gating, state mapping, timeouts/breaker fallback,
kill guard, unified tmux/herdr rows, routes, and an opt-in live smoke.

The snapshot fixture is trimmed from real `herdr api snapshot` output of
herdr 0.9.5 (protocol 22) on HP, 2026-10-26.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import pytest
from starlette.testclient import TestClient

from terminal_mcp.herdr_runtime import (BREAKER_SECONDS, HerdrConfig, HerdrError,
                                        HerdrRuntimeAdapter, normalize_state, unified_rows)

NOW = 1_800_000_000.0


def _snapshot(agent_status: str = "working", seq: int = 1, label: str = "tmcp-pilot") -> dict:
    return {"id": "cli:api:snapshot", "result": {"type": "session_snapshot", "snapshot": {
        "version": "0.9.5", "protocol": 22,
        "workspaces": [{"workspace_id": "w1", "label": label, "agent_status": "unknown"}],
        "panes": [
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent_status": "unknown",
             "cwd": "/srv/pilot", "foreground_cwd": "/srv/pilot",
             "terminal_title_stripped": "kimex@hp: ~/pilot"},
            {"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1",
             "agent_status": agent_status, "cwd": "/srv/repo"},
        ],
        "agents": [{"agent": "probe", "agent_status": agent_status, "pane_id": "w1:p2",
                    "workspace_id": "w1", "state_change_seq": seq}],
    }}}


STATUS_OK = {"server": {"status": "running", "running": True, "version": "0.9.5", "protocol": 22,
                        "compatible": True, "session": "terminalmcp-pilot"}}


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def __call__(self) -> float:
        return self.at


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.responses: dict[str, object] = {"status": STATUS_OK, "api": _snapshot(),
                                             "pane": {"result": {"type": "ok"}},
                                             "session": {"stopped": True}}

    def __call__(self, argv, env, timeout):
        self.calls.append((argv, env))
        response = self.responses[argv[3]]
        if isinstance(response, BaseException):
            raise response
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")


def _adapter(tmp_path, *, enabled=True, nodes=("hp-linux",), runner=None, **kw):
    config = HerdrConfig(enabled=enabled, nodes=nodes, binary="/opt/herdr",
                         state_dir=str(tmp_path / "state"), **kw)
    clock = Clock()
    adapter = HerdrRuntimeAdapter(config, "hp-linux", runner=runner or FakeRunner(), clock=clock)
    return adapter, clock


# -- gating -------------------------------------------------------------------

def test_flag_defaults_off_and_runs_nothing(tmp_path):
    assert HerdrConfig.from_env({}).enabled is False
    runner = FakeRunner()
    adapter, _ = _adapter(tmp_path, enabled=False, runner=runner)
    assert adapter.snapshot() == {"source": "herdr", "enabled": False, "status": "disabled",
                                  "rows": []}
    assert adapter.health()["status"] == "disabled"
    assert runner.calls == []


def test_enabled_but_node_not_allowlisted_is_inert(tmp_path):
    cfg = HerdrConfig.from_env({"HERDR_ENABLED": "1", "TERMINAL_MCP_HERDR_NODES": "nec1, m910"})
    assert cfg.enabled and cfg.nodes == ("nec1", "m910")
    assert cfg.active_for("hp-linux") is False and cfg.active_for("nec1") is True
    runner = FakeRunner()
    adapter, _ = _adapter(tmp_path, nodes=("nec1",), runner=runner)
    assert adapter.snapshot()["status"] == "disabled"
    assert runner.calls == []


def test_calls_are_scoped_argv_with_pilot_session_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/tmp/someone-elses.sock")
    monkeypatch.setenv("HERDR_PANE_ID", "w9:p9")
    runner = FakeRunner()
    adapter, _ = _adapter(tmp_path, runner=runner)
    adapter.snapshot()
    argv, env = runner.calls[0]
    assert argv[:3] == ["/opt/herdr", "--session", "terminalmcp-pilot"]
    assert env["HERDR_SESSION"] == "terminalmcp-pilot"
    assert "HERDR_SOCKET_PATH" not in env and "HERDR_PANE_ID" not in env


@pytest.mark.parametrize("session", ["default", "Bad Name", "../x", ""])
def test_refuses_default_or_malformed_session(tmp_path, session):
    runner = FakeRunner()
    adapter, _ = _adapter(tmp_path, runner=runner, session=session)
    payload = adapter.snapshot()
    assert payload["status"] == "degraded" and payload["blocker"] == "HERDR_SESSION_REFUSED"
    assert runner.calls == []


# -- reads --------------------------------------------------------------------

def test_snapshot_rows_states_mapping_and_timestamps(tmp_path):
    task_map = tmp_path / "map.json"
    task_map.write_text(json.dumps({"probe": {"task_id": "T-42", "repo": "terminal-mcp"}}))
    runner = FakeRunner()
    adapter, clock = _adapter(tmp_path, runner=runner, task_map_path=str(task_map))
    first = adapter.snapshot()
    assert first["status"] == "ok" and first["version"] == "0.9.5"
    by_pane = {r["pane_id"]: r for r in first["rows"]}
    shell, agent = by_pane["w1:p1"], by_pane["w1:p2"]
    assert shell["is_agent"] is False and shell["state"] == "unknown"
    assert agent["key"] == "herdr:hp-linux/terminalmcp-pilot/w1:p2"
    assert (agent["agent"], agent["state"], agent["task_id"], agent["repo"]) == (
        "probe", "working", "T-42", "terminal-mcp")
    assert agent["first_seen_at"] == agent["state_changed_at"] == NOW
    assert first["counts"]["working"] == 1

    clock.at += 2                     # inside cache window: no new call
    assert adapter.snapshot() is first and len(runner.calls) == 1

    clock.at += 10                    # same state: state_changed_at kept
    assert {r["pane_id"]: r for r in adapter.snapshot()["rows"]}["w1:p2"][
        "state_changed_at"] == NOW
    runner.responses["api"] = _snapshot("blocked", seq=2)
    clock.at += 10
    blocked = {r["pane_id"]: r for r in adapter.snapshot()["rows"]}["w1:p2"]
    assert blocked["state"] == "blocked" and blocked["blocker"] == "AGENT_BLOCKED"
    assert blocked["state_changed_at"] == clock.at and blocked["first_seen_at"] == NOW


@pytest.mark.parametrize("raw,want", [("working", "working"), ("BLOCKED", "blocked"),
                                      ("idle", "idle"), ("done", "done"), (None, "unknown"),
                                      ("thinking", "unknown")])
def test_normalize_state(raw, want):
    assert normalize_state(raw) == want


def test_health_ok_and_not_running(tmp_path):
    runner = FakeRunner()
    adapter, _ = _adapter(tmp_path, runner=runner)
    assert adapter.health()["status"] == "ok"
    runner.responses["status"] = {"server": {"running": False}}
    health = adapter.health()
    assert health["status"] == "degraded" and health["blocker"] == "HERDR_SERVER_NOT_RUNNING"


# -- failure / fallback -------------------------------------------------------

def test_timeout_opens_breaker_and_serves_stale_rows(tmp_path):
    runner = FakeRunner()
    adapter, clock = _adapter(tmp_path, runner=runner)
    adapter.snapshot()
    runner.responses["api"] = subprocess.TimeoutExpired(["herdr"], 3)
    clock.at += 10
    degraded = adapter.snapshot()
    assert degraded["status"] == "degraded" and degraded["blocker"] == "HERDR_TIMEOUT"
    assert degraded["rows"] and all(r["stale"] and r["state"] == "unknown"
                                    for r in degraded["rows"])
    calls = len(runner.calls)
    clock.at += 10                    # breaker open: no subprocess at all
    assert adapter.snapshot()["blocker"] == "HERDR_BREAKER_OPEN"
    assert len(runner.calls) == calls
    runner.responses["api"] = _snapshot()
    clock.at += BREAKER_SECONDS       # half-open: tries again and recovers
    assert adapter.snapshot()["status"] == "ok"


def test_missing_binary_and_cli_error(tmp_path):
    runner = FakeRunner()
    runner.responses["api"] = FileNotFoundError("/opt/herdr")
    adapter, _ = _adapter(tmp_path, runner=runner)
    assert adapter.snapshot()["blocker"] == "HERDR_NOT_INSTALLED"

    def failing(argv, env, timeout):
        return subprocess.CompletedProcess(argv, 1, "", '{"error":"no server"}')
    adapter2, _ = _adapter(tmp_path, runner=failing)
    assert adapter2.health()["blocker"] == "HERDR_CLI_ERROR"


def test_real_subprocess_timeout_is_bounded(tmp_path):
    """Integration: a real hung `herdr` executable is killed at the timeout."""
    fake = tmp_path / "herdr"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    config = HerdrConfig(enabled=True, nodes=("hp-linux",), binary=str(fake),
                         timeout_seconds=0.5, state_dir=str(tmp_path))
    adapter = HerdrRuntimeAdapter(config, "hp-linux")
    started = time.monotonic()
    payload = adapter.snapshot()
    assert time.monotonic() - started < 5
    assert payload["blocker"] == "HERDR_TIMEOUT"


def test_real_subprocess_success_path(tmp_path):
    fake = tmp_path / "herdr"
    fake.write_text("#!/bin/sh\ncat <<'EOF'\n" + json.dumps(_snapshot()) + "\nEOF\n")
    fake.chmod(0o755)
    config = HerdrConfig(enabled=True, nodes=("hp-linux",), binary=str(fake),
                         state_dir=str(tmp_path))
    rows = HerdrRuntimeAdapter(config, "hp-linux").snapshot()["rows"]
    assert {r["pane_id"] for r in rows} == {"w1:p1", "w1:p2"}


# -- kill guard ---------------------------------------------------------------

def test_mutations_require_flag_owner_confirm_and_owned_workspace(tmp_path):
    runner = FakeRunner()
    off, _ = _adapter(tmp_path, enabled=False, runner=runner)
    with pytest.raises(HerdrError) as exc:
        off.stop_session(confirm="terminalmcp-pilot")
    assert exc.value.code == "HERDR_DISABLED"

    adapter, _ = _adapter(tmp_path, runner=runner)
    for call in (lambda: adapter.close_pane("w1:p1", confirm="w1:p1"),
                 lambda: adapter.stop_session(confirm="terminalmcp-pilot")):
        with pytest.raises(HerdrError) as exc:
            call()
        assert exc.value.code == "HERDR_NOT_OWNER"
    assert runner.calls == []

    adapter.write_owner()
    with pytest.raises(HerdrError) as exc:
        adapter.close_pane("w1:p1", confirm="w1:p2")
    assert exc.value.code == "CONFIRMATION_REQUIRED"
    with pytest.raises(HerdrError) as exc:
        adapter.close_pane("w1:p1;rm -rf /", confirm="w1:p1;rm -rf /")
    assert exc.value.code == "HERDR_BAD_PANE_ID"
    with pytest.raises(HerdrError) as exc:
        adapter.stop_session(confirm="default")
    assert exc.value.code == "CONFIRMATION_REQUIRED"

    runner.responses["api"] = _snapshot(label="someone-elses")
    with pytest.raises(HerdrError) as exc:
        adapter.close_pane("w1:p1", confirm="w1:p1")
    assert exc.value.code == "HERDR_NOT_OWNER"

    runner.responses["api"] = _snapshot()
    adapter.close_pane("w1:p1", confirm="w1:p1")
    adapter.stop_session(confirm="terminalmcp-pilot")
    argvs = [argv[3:] for argv, _ in runner.calls]
    assert ["pane", "close", "w1:p1"] in argvs
    assert ["session", "stop", "terminalmcp-pilot", "--json"] in argvs
    assert not any(a[:2] == ["server", "stop"] for a in argvs)
    assert all(argv[2] == "terminalmcp-pilot" for argv, _ in runner.calls)


def test_owner_record_for_other_session_is_refused(tmp_path):
    adapter, _ = _adapter(tmp_path)
    adapter.write_owner()
    other, _ = _adapter(tmp_path, session="other-pilot")
    with pytest.raises(HerdrError) as exc:
        other.stop_session(confirm="other-pilot")
    assert exc.value.code == "HERDR_NOT_OWNER"


# -- unified rows -------------------------------------------------------------

def test_unified_rows_sources_and_dedup(tmp_path):
    adapter, _ = _adapter(tmp_path)
    herdr = adapter.snapshot()
    herdr["rows"][1]["task_id"] = "T-1"
    tmux = [{"name": "work", "node_id": "hp-linux", "task_id": "T-1"},
            {"name": "work", "node_id": "hp-linux"},
            {"name": "other", "node_id": "nec1"}]
    out = unified_rows(tmux, herdr, local_node_id="hp-linux")
    keys = [r["key"] for r in out["rows"]]
    assert keys.count("tmux:hp-linux/work") == 1
    assert "tmux:nec1/other" in keys
    assert "herdr:hp-linux/terminalmcp-pilot/w1:p1" in keys
    assert "herdr:hp-linux/terminalmcp-pilot/w1:p2" not in keys
    work = next(r for r in out["rows"] if r["key"] == "tmux:hp-linux/work")
    assert work["also_in"] == ["herdr:hp-linux/terminalmcp-pilot/w1:p2"]
    assert out["sources"]["herdr"]["status"] == "ok"
    assert {r["source"] for r in out["rows"]} == {"tmux", "herdr"}


def test_unified_rows_without_herdr_is_tmux_only():
    out = unified_rows([{"name": "a"}], None, local_node_id="hp-linux")
    assert [r["key"] for r in out["rows"]] == ["tmux:hp-linux/a"]
    assert out["sources"]["herdr"]["status"] == "disabled"


# -- live monitor + routes ----------------------------------------------------

class _Controller:
    local_node_id = "local"

    def terminal_list_sessions(self):
        return {"sessions": [{"name": "s", "node_id": "local", "readable": True,
                              "created": NOW - 30}], "unreachable_nodes": []}

    def terminal_status(self, target):
        return {"exists": True, "state": "IDLE", "reason": "idle"}

    def terminal_tail(self, *a, **k):
        return {"lines": []}


class _Boom:
    def snapshot(self):
        raise RuntimeError("socket gone")


def test_live_monitor_carries_herdr_and_survives_adapter_failure(tmp_path):
    from terminal_mcp.live_sessions import LiveSessionMonitor

    assert LiveSessionMonitor(_Controller(), clock=lambda: NOW).snapshot()["runtimes"] == {}
    adapter, _ = _adapter(tmp_path)
    payload = LiveSessionMonitor(_Controller(), herdr=adapter, clock=lambda: NOW).snapshot()
    assert payload["runtimes"]["herdr"]["status"] == "ok"
    assert [s["session"] for s in payload["sessions"]] == ["s"]
    broken = LiveSessionMonitor(_Controller(), herdr=_Boom(), clock=lambda: NOW).snapshot()
    assert broken["runtimes"]["herdr"]["blocker"] == "HERDR_ADAPTER_ERROR"
    assert [s["session"] for s in broken["sessions"]] == ["s"]


def test_runtime_sessions_route_default_is_tmux_only(tmp_path, monkeypatch):
    monkeypatch.delenv("HERDR_ENABLED", raising=False)
    from tests.test_live_sessions import _servers

    server = _servers(tmp_path)
    client = TestClient(server.streamable_http_app(), base_url="https://testserver")
    response = client.get("/dashboard/api/runtime-sessions")
    assert response.status_code == 200
    body = response.json()
    assert body["sources"]["herdr"]["status"] == "disabled"
    assert all(r["source"] == "tmux" for r in body["rows"])
    page = client.get("/dashboard/live")
    assert "renderRuntimes" in page.text and 'id="runtimes"' in page.text


def test_runtime_sessions_route_is_read_guarded(tmp_path):
    from terminal_mcp.audit import AuditStore
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
    response = TestClient(server.streamable_http_app()).get("/dashboard/api/runtime-sessions")
    assert response.status_code == 403
    assert response.json()["error"] == "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"


# -- opt-in live smoke --------------------------------------------------------

@pytest.mark.skipif(os.environ.get("TERMINAL_MCP_HERDR_LIVE_SMOKE") != "1",
                    reason="set TERMINAL_MCP_HERDR_LIVE_SMOKE=1 on a node running the pilot")
def test_live_pilot_smoke():
    config = HerdrConfig.from_env({**os.environ, "HERDR_ENABLED": "1",
                                   "TERMINAL_MCP_HERDR_NODES": "smoke"})
    adapter = HerdrRuntimeAdapter(config, "smoke")
    health = adapter.health()
    assert health["status"] == "ok", health
    assert health["version"] == "0.9.5"
    payload = adapter.snapshot(force=True)
    assert payload["status"] == "ok", payload
    assert any(r["pane_id"] == "w1:p1" for r in payload["rows"])
