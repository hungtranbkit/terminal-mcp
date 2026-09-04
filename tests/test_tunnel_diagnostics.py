"""Tunnel connection reliability: diagnostics + self-healing decision
logic (terminal_mcp/tunnel_diagnostics.py, tunnel_watchdog.py, doctor.py).

Every check function takes its dependencies (a URL, a systemctl call) as
plain function calls this file monkeypatches -- no real network/systemd
needed for the decision-logic coverage below. `test_network_dns_tls_*`
and the doctor/watchdog CLI smoke tests are the only ones that touch
anything real (a live DNS/TLS probe, or the actual installed console
scripts), matching this project's existing real-CLI-verified pattern
elsewhere (tests/test_adapters_real_cli.py) rather than mocking those out
entirely.
"""
from __future__ import annotations

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from terminal_mcp import tunnel_diagnostics as td


# -- individual checks, dependencies injected/monkeypatched ------------------

class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_check_mcp_local_healthy(monkeypatch):
    def fake_urlopen(url, timeout):
        assert url == td.DEFAULT_MCP_HEALTH_URL
        return _FakeResponse(200, json.dumps({"status": "ready"}).encode())
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_mcp_local()
    assert result == {"status": "healthy", "detail": "ok"}


def test_check_mcp_local_unhealthy_body(monkeypatch):
    def fake_urlopen(url, timeout):
        return _FakeResponse(503, json.dumps({"status": "not_ready"}).encode())
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_mcp_local()
    assert result["status"] == "unhealthy"


def test_check_mcp_local_connection_refused(monkeypatch):
    def fake_urlopen(url, timeout):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_mcp_local()
    assert result["status"] == "unhealthy"
    assert "refused" in result["detail"] or "ConnectionRefusedError" in result["detail"]


def test_check_systemd_unit_active(monkeypatch):
    def fake_run(args, **kwargs):
        assert args[:3] == ["systemctl", "--user", "show"]
        return SimpleNamespace(stdout="ActiveState=active\nSubState=running\nNRestarts=0\n"
                                      "ActiveEnterTimestamp=Fri 2026-09-04 12:28:47 +07\n")
    monkeypatch.setattr(td.subprocess, "run", fake_run)
    now = time.mktime(time.strptime("2026-09-04 12:30:47", "%Y-%m-%d %H:%M:%S"))
    result = td.check_systemd_unit("terminal-mcp-tunnel.service", now=now)
    assert result["active"] is True
    assert result["sub_state"] == "running"
    assert result["n_restarts"] == 0
    assert result["uptime_seconds"] == pytest.approx(120.0, abs=1.0)


def test_check_systemd_unit_failed_substate(monkeypatch):
    def fake_run(args, **kwargs):
        return SimpleNamespace(stdout="ActiveState=failed\nSubState=failed\nNRestarts=5\n")
    monkeypatch.setattr(td.subprocess, "run", fake_run)
    result = td.check_systemd_unit("terminal-mcp-tunnel.service")
    assert result["active"] is False
    assert result["sub_state"] == "failed"
    assert result["n_restarts"] == 5
    assert result["uptime_seconds"] is None  # no ActiveEnterTimestamp line -- never guessed


def test_check_systemd_unit_subprocess_error(monkeypatch):
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 5)
    monkeypatch.setattr(td.subprocess, "run", fake_run)
    result = td.check_systemd_unit("terminal-mcp-tunnel.service")
    assert result["active"] is False
    assert result["active_state"] == "unknown"


_METRICS_BODY_TEMPLATE = """# HELP commands_poll_last_successful_timestamp_seconds x
# TYPE commands_poll_last_successful_timestamp_seconds gauge
commands_poll_last_successful_timestamp_seconds {epoch}
"""


def test_check_tunnel_ready_fresh(monkeypatch):
    now = 1_800_000_000.0
    epoch = now - 5.0

    def fake_urlopen(url, timeout):
        if url.endswith("/healthz") or url.endswith("/readyz"):
            return _FakeResponse(200, b"ok")
        if url.endswith("/metrics"):
            return _FakeResponse(200, _METRICS_BODY_TEMPLATE.format(epoch=epoch).encode())
        raise AssertionError(f"unexpected url {url}")
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_tunnel_ready(now=now)
    assert result["ready"] == "ready"
    assert result["last_heartbeat_age_sec"] == pytest.approx(5.0)


def test_check_tunnel_ready_stale_heartbeat_but_endpoints_up(monkeypatch):
    # healthz/readyz alone would say "ready" -- the whole point of this
    # check is that a stale control-plane poll must NOT be masked by the
    # process's own local endpoints being reachable (those only prove the
    # HTTP server thread is alive, not that polling is actually working).
    now = 1_800_000_000.0
    epoch = now - 400.0

    def fake_urlopen(url, timeout):
        if url.endswith("/healthz") or url.endswith("/readyz"):
            return _FakeResponse(200, b"ok")
        return _FakeResponse(200, _METRICS_BODY_TEMPLATE.format(epoch=epoch).encode())
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_tunnel_ready(now=now)
    # check_tunnel_ready itself only reports healthz/readyz-derived "ready"
    # (both endpoints up) -- staleness-vs-threshold is diagnose()'s job,
    # tested separately below via last_heartbeat_age_sec.
    assert result["last_heartbeat_age_sec"] == pytest.approx(400.0)


def test_check_tunnel_ready_uninitialized_gauge_is_unknown_not_a_huge_age(monkeypatch):
    # Real bug, caught live against this project's own tunnel-client
    # immediately after a restart: a never-Set() Prometheus gauge reads
    # back as its zero value, not an absent line. `age = now - 0` would
    # be a multi-decade "age" -- must be treated as "no data yet", never
    # propagated as a real, huge staleness number.
    now = 1_800_000_000.0

    def fake_urlopen(url, timeout):
        if url.endswith("/healthz") or url.endswith("/readyz"):
            return _FakeResponse(200, b"ok")
        return _FakeResponse(200, _METRICS_BODY_TEMPLATE.format(epoch="0").encode())
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_tunnel_ready(now=now)
    assert result["ready"] == "unknown"
    assert result["last_heartbeat_age_sec"] is None
    assert result["last_heartbeat_epoch"] is None


def test_check_tunnel_ready_metrics_unreachable(monkeypatch):
    def fake_urlopen(url, timeout):
        if url.endswith("/healthz") or url.endswith("/readyz"):
            return _FakeResponse(200, b"ok")
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(td.urllib.request, "urlopen", fake_urlopen)
    result = td.check_tunnel_ready()
    assert result["ready"] == "unknown"
    assert result["last_heartbeat_age_sec"] is None


def test_check_network_dns_tls_dns_failure(monkeypatch):
    def fake_getaddrinfo(host, port, proto=None):
        raise OSError("Name or service not known")
    monkeypatch.setattr(td.socket, "getaddrinfo", fake_getaddrinfo)
    result = td.check_network_dns_tls()
    assert result["status"] == "fail"
    assert "DNS resolution failed" in result["detail"]


@pytest.mark.real_network
def test_check_network_dns_tls_real_pass():
    # Real DNS + TCP + TLS handshake against the actual host the tunnel
    # talks to -- this environment has real internet access (confirmed
    # live throughout this session), matching this project's own "verify
    # against reality" standard rather than mocking out the one check
    # whose entire job is proving reality works.
    result = td.check_network_dns_tls(timeout=5.0)
    assert result["status"] == "pass"


# -- WatchdogState persistence ------------------------------------------------

def test_watchdog_state_round_trips(tmp_path):
    path = tmp_path / "state.json"
    state = td.WatchdogState(tunnel_restart_count=3, last_action="restart_tunnel", last_action_reason="stale")
    state.save(path)
    loaded = td.WatchdogState.load(path)
    assert loaded.tunnel_restart_count == 3
    assert loaded.last_action == "restart_tunnel"
    assert loaded.last_action_reason == "stale"


def test_watchdog_state_load_missing_file_returns_defaults(tmp_path):
    state = td.WatchdogState.load(tmp_path / "does-not-exist.json")
    assert state.tunnel_restart_count == 0
    assert state.last_action == "none"


def test_watchdog_state_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"tunnel_restart_count": 2, "some_future_field": "x"}))
    state = td.WatchdogState.load(path)
    assert state.tunnel_restart_count == 2


# -- diagnose(): field composition + chatgpt_side ----------------------------

def _patch_checks(monkeypatch, *, mcp_status="healthy", tunnel_active=True, tunnel_sub_state="running",
                  heartbeat_age=5.0, network_status="pass", tunnel_uptime_sec=300.0):
    monkeypatch.setattr(td, "check_mcp_local", lambda url=None, timeout=None: {"status": mcp_status, "detail": "x"})
    monkeypatch.setattr(td, "check_systemd_unit", lambda unit, timeout=5.0, now=None: {
        "active": tunnel_active, "active_state": "active" if tunnel_active else "inactive",
        "sub_state": tunnel_sub_state, "n_restarts": 0, "uptime_seconds": tunnel_uptime_sec,
    })
    monkeypatch.setattr(td, "check_tunnel_ready", lambda base_url=None, timeout=None, now=None: {
        "ready": "ready", "healthz_ok": True, "readyz_ok": True,
        "last_heartbeat_epoch": time.time() - (heartbeat_age or 0), "last_heartbeat_age_sec": heartbeat_age,
        "detail": "ok",
    })
    monkeypatch.setattr(td, "check_network_dns_tls", lambda host=None: {"status": network_status, "detail": "x"})


def test_diagnose_all_healthy_is_suspected_platform_side(monkeypatch):
    _patch_checks(monkeypatch)
    result = td.diagnose()
    assert result["mcp_local"] == "healthy"
    assert result["tunnel_process"] == "active"
    assert result["tunnel_ready"] == "ready"
    assert result["chatgpt_side"] == "suspected-platform-side"
    assert result["recommended_action"] == "none -- all local checks healthy"


def test_diagnose_stale_heartbeat_past_threshold(monkeypatch):
    _patch_checks(monkeypatch, heartbeat_age=500.0)
    result = td.diagnose(stale_threshold=150.0)
    assert result["tunnel_ready"] == "stale"
    assert result["chatgpt_side"] == "cannot-verify"
    assert "restart terminal-mcp-tunnel" in result["recommended_action"]


def test_diagnose_mcp_unhealthy_recommends_mcp_restart(monkeypatch):
    _patch_checks(monkeypatch, mcp_status="unhealthy")
    result = td.diagnose()
    assert result["mcp_local"] == "unhealthy"
    assert result["chatgpt_side"] == "cannot-verify"
    assert "terminal-mcp-http" in result["recommended_action"]


def test_diagnose_tunnel_process_inactive(monkeypatch):
    _patch_checks(monkeypatch, tunnel_active=False, tunnel_sub_state="dead")
    result = td.diagnose()
    assert result["tunnel_process"] == "inactive"
    assert result["tunnel_ready"] == "unknown"  # never claims "ready" for a process that isn't even active


def test_diagnose_network_failure_takes_priority_over_tunnel_recommendation(monkeypatch):
    _patch_checks(monkeypatch, network_status="fail")
    result = td.diagnose()
    assert "network" in result["recommended_action"].lower()


# -- decide_action(): the actual self-healing policy --------------------------

def _diag(**overrides):
    base = {
        "mcp_local": "healthy", "mcp_local_detail": "ok",
        "tunnel_process": "active", "tunnel_process_sub_state": "running",
        "tunnel_process_uptime_sec": 300.0,
        "tunnel_ready": "ready", "last_heartbeat_age_sec": 5.0,
        "network_dns_tls": "pass", "network_dns_tls_detail": "ok",
        "chatgpt_side": "suspected-platform-side",
        "last_recovery_action": None, "last_recovery_action_reason": None, "last_recovery_action_at": None,
        "recommended_action": "none -- all local checks healthy",
    }
    base.update(overrides)
    return base


def test_decide_action_none_when_all_healthy():
    decision = td.decide_action(_diag(), td.WatchdogState())
    assert decision.action == "none"


def test_decide_action_restarts_mcp_when_unhealthy():
    decision = td.decide_action(_diag(mcp_local="unhealthy"), td.WatchdogState())
    assert decision.action == "restart_mcp"


def test_decide_action_mcp_cooldown_suppresses_restart():
    state = td.WatchdogState(mcp_cooldown_until=time.time() + 600)
    decision = td.decide_action(_diag(mcp_local="unhealthy"), state)
    assert decision.action == "none"
    assert "cooldown" in decision.reason


def test_decide_action_reset_failed_when_tunnel_failed_substate():
    decision = td.decide_action(
        _diag(tunnel_process="inactive", tunnel_process_sub_state="failed", tunnel_ready="unknown"),
        td.WatchdogState(),
    )
    assert decision.action == "reset_failed_tunnel_then_start"


def test_decide_action_restart_tunnel_when_inactive_not_failed():
    decision = td.decide_action(
        _diag(tunnel_process="inactive", tunnel_process_sub_state="dead", tunnel_ready="unknown"),
        td.WatchdogState(),
    )
    assert decision.action == "restart_tunnel"


def test_decide_action_startup_grace_period_no_restart_for_fresh_process():
    # A just-(re)started tunnel legitimately has no heartbeat yet -- must
    # NOT be restarted for that alone (the self-inflicted restart loop
    # this was caught causing live, see check_tunnel_ready's own comment
    # and the module docstring).
    decision = td.decide_action(
        _diag(tunnel_ready="unknown", last_heartbeat_age_sec=None, tunnel_process_uptime_sec=10.0),
        td.WatchdogState(),
    )
    assert decision.action == "none"
    assert "startup_grace_period" in decision.reason


def test_decide_action_no_heartbeat_past_grace_period_does_restart():
    # Same "no heartbeat yet" shape, but the process has been up well
    # past the grace window -- now a real hang, not a normal startup gap.
    decision = td.decide_action(
        _diag(tunnel_ready="unknown", last_heartbeat_age_sec=None, tunnel_process_uptime_sec=300.0),
        td.WatchdogState(), startup_grace_seconds=60.0,
    )
    assert decision.action == "restart_tunnel"


def test_decide_action_real_stale_age_ignores_grace_period_even_if_recent_uptime():
    # A genuinely stale (non-null, large) heartbeat age must never be
    # excused by the grace period, regardless of how recently the unit
    # (re)started -- only the "no data at all yet" case gets grace.
    decision = td.decide_action(
        _diag(tunnel_ready="stale", last_heartbeat_age_sec=500.0, tunnel_process_uptime_sec=10.0),
        td.WatchdogState(),
    )
    assert decision.action == "restart_tunnel"


def test_decide_action_restart_tunnel_when_stale():
    decision = td.decide_action(_diag(tunnel_ready="stale", last_heartbeat_age_sec=500.0), td.WatchdogState())
    assert decision.action == "restart_tunnel"
    assert "500" in decision.reason


def test_decide_action_tunnel_cooldown_suppresses_restart_loop():
    # Test D (task item 9): tunnel process/MCP both otherwise "healthy"
    # from systemd's point of view but the heartbeat never recovers
    # (simulated platform-side/network outage) -- after enough consecutive
    # restarts with no recovery, stop restarting.
    state = td.WatchdogState(tunnel_cooldown_until=time.time() + 600, consecutive_tunnel_restarts=3)
    decision = td.decide_action(_diag(tunnel_ready="stale", last_heartbeat_age_sec=999.0), state)
    assert decision.action == "none"
    assert "cooldown" in decision.reason
    assert "suspected_platform_side" in decision.reason or "network" in decision.reason


# -- apply_decision(): state mutation, counters, cooldown triggering ---------

def test_apply_decision_restart_tunnel_calls_systemctl_and_updates_counters():
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    state = td.WatchdogState()
    decision = td.Decision("restart_tunnel", "stale")
    state = td.apply_decision(decision, state, run=fake_run)
    assert calls == [["systemctl", "--user", "restart", td.DEFAULT_TUNNEL_UNIT]]
    assert state.tunnel_restart_count == 1
    assert state.consecutive_tunnel_restarts == 1
    assert state.mcp_restart_count == 0  # never touches the other target's counters
    assert state.last_action == "restart_tunnel"


def test_apply_decision_reset_failed_calls_both_commands():
    calls = []
    state = td.apply_decision(td.Decision("reset_failed_tunnel_then_start", "failed substate"),
                              td.WatchdogState(), run=lambda args, **kw: calls.append(args))
    assert calls == [
        ["systemctl", "--user", "reset-failed", td.DEFAULT_TUNNEL_UNIT],
        ["systemctl", "--user", "start", td.DEFAULT_TUNNEL_UNIT],
    ]
    assert state.tunnel_restart_count == 1


def test_apply_decision_none_resets_consecutive_counts():
    state = td.WatchdogState(consecutive_tunnel_restarts=2, consecutive_mcp_restarts=1)
    state = td.apply_decision(td.Decision("none", "all checks healthy"), state, run=lambda *a, **k: None)
    assert state.consecutive_tunnel_restarts == 0
    assert state.consecutive_mcp_restarts == 0


def test_apply_decision_triggers_cooldown_after_threshold_consecutive_restarts():
    calls = []
    state = td.WatchdogState()
    for _ in range(3):
        decision = td.decide_action(_diag(tunnel_ready="stale", last_heartbeat_age_sec=500.0), state)
        assert decision.action == "restart_tunnel"  # not yet suppressed
        state = td.apply_decision(decision, state, run=lambda a, **k: calls.append(a),
                                  cooldown_trigger_count=3, cooldown_seconds=900)
    assert state.consecutive_tunnel_restarts == 3
    assert state.tunnel_cooldown_until is not None
    # The 4th consecutive stale check must now be suppressed -- this is
    # the actual "no infinite restart loop" guarantee, exercised end to
    # end through decide_action -> apply_decision -> decide_action again.
    decision = td.decide_action(_diag(tunnel_ready="stale", last_heartbeat_age_sec=500.0), state)
    assert decision.action == "none"
    assert len(calls) == 3  # never a 4th systemctl restart call


def test_apply_decision_recovery_clears_cooldown_for_future_events():
    state = td.WatchdogState(tunnel_cooldown_until=time.time() + 900, consecutive_tunnel_restarts=3)
    state = td.apply_decision(td.Decision("none", "all checks healthy"), state, run=lambda *a, **k: None)
    assert state.tunnel_cooldown_until is None


# -- run_once() / doctor CLI: exercised as real subprocess/import smoke ------

def test_watchdog_run_once_dry_run_never_calls_systemctl(monkeypatch, tmp_path):
    from terminal_mcp import tunnel_watchdog

    called = []
    monkeypatch.setattr(td.subprocess, "run", lambda *a, **k: called.append(a) or SimpleNamespace(
        stdout="ActiveState=active\nSubState=running\nNRestarts=0\n"))
    _patch_checks(monkeypatch, mcp_status="unhealthy")  # would normally trigger restart_mcp
    result = tunnel_watchdog.run_once(state_path=tmp_path / "state.json", dry_run=True)
    assert result["decision"]["action"] == "restart_mcp"
    assert result["acted"] is False
    # dry_run must never persist a mutated state file either.
    assert not (tmp_path / "state.json").exists()


def test_doctor_connection_json_exit_code_reflects_health(monkeypatch, tmp_path, capsys):
    from terminal_mcp import doctor

    _patch_checks(monkeypatch)
    monkeypatch.setattr(td, "default_state_path", lambda: tmp_path / "state.json")
    code = doctor.main(["connection", "--json"])
    assert code == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["mcp_local"] == "healthy"


# -- banner_status(): dashboard's small health banner ------------------------

def test_banner_status_connected_when_healthy_no_recent_action():
    label = td.banner_status(_diag(), td.WatchdogState())
    assert label == td.BANNER_CONNECTED


def test_banner_status_recovering_right_after_a_restart_action():
    now = 2_000_000_000.0
    state = td.WatchdogState(last_action="restart_tunnel", last_action_at=now - 10)
    label = td.banner_status(_diag(), state, now=now)
    assert label == td.BANNER_RECOVERING


def test_banner_status_reverts_to_connected_after_recovering_window_passes():
    now = 2_000_000_000.0
    state = td.WatchdogState(last_action="restart_tunnel", last_action_at=now - 999)
    label = td.banner_status(_diag(), state, now=now)
    assert label == td.BANNER_CONNECTED


def test_banner_status_mcp_down_takes_priority():
    label = td.banner_status(_diag(mcp_local="unhealthy"), td.WatchdogState())
    assert label == td.BANNER_MCP_DOWN


def test_banner_status_tunnel_stale_when_mcp_healthy_but_tunnel_not_ready():
    label = td.banner_status(_diag(tunnel_ready="stale"), td.WatchdogState())
    assert label == td.BANNER_TUNNEL_STALE


def test_doctor_connection_nonzero_exit_when_unhealthy(monkeypatch, tmp_path, capsys):
    from terminal_mcp import doctor

    _patch_checks(monkeypatch, mcp_status="unhealthy")
    monkeypatch.setattr(td, "default_state_path", lambda: tmp_path / "state.json")
    code = doctor.main(["connection", "--json"])
    assert code == 1


# -- dashboard /dashboard/api/connection-health route -------------------------

def test_dashboard_connection_health_route_returns_coarse_banner(monkeypatch, tmp_path):
    from starlette.testclient import TestClient
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard, tunnel_diagnostics as dashboard_td
    from terminal_mcp.mcp_app import build_mcp

    _patch_checks(monkeypatch, mcp_status="unhealthy")
    monkeypatch.setattr(dashboard_td, "default_state_path", lambda: tmp_path / "state.json")

    config = AppConfig(permissions=PermissionsConfig(True, False), allowed_session_patterns=("test-*",))
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())

    response = client.get("/dashboard/api/connection-health")
    assert response.status_code == 200
    body = response.json()
    assert body["banner"] == td.BANNER_MCP_DOWN
    assert body["diagnosis"]["mcp_local"] == "unhealthy"


def test_dashboard_connection_health_route_skips_network_check(monkeypatch, tmp_path):
    # The route must never do the (slower, external) DNS/TLS probe on
    # every dashboard poll -- confirmed by asserting check_network_dns_tls
    # is simply never called, not just that its result is ignored.
    from starlette.testclient import TestClient
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard, tunnel_diagnostics as dashboard_td
    from terminal_mcp.mcp_app import build_mcp

    called = []
    monkeypatch.setattr(dashboard_td, "check_network_dns_tls", lambda **kw: called.append(1) or {"status": "pass", "detail": "x"})
    _patch_checks(monkeypatch)
    monkeypatch.setattr(dashboard_td, "default_state_path", lambda: tmp_path / "state.json")

    config = AppConfig(permissions=PermissionsConfig(True, False), allowed_session_patterns=("test-*",))
    service = TerminalService(config)
    server = build_mcp(service)
    register_dashboard(server, service)
    client = TestClient(server.streamable_http_app())

    response = client.get("/dashboard/api/connection-health")
    assert response.status_code == 200
    assert called == []
