"""Connection reliability for the OpenAI Secure MCP Tunnel (terminal-mcp-
tunnel.service) -- diagnostics + the self-healing decision logic, shared
by `terminal-mcp-doctor` (read-only, human/machine output) and
`terminal-mcp-tunnel-watchdog` (the same checks, acting on them).

Real-evidence root cause (journalctl --user -u terminal-mcp-tunnel,
2026-08-31 through 2026-09-04, ~4 days): 1232 WARN/ERROR log lines, the
overwhelming majority "poll failed; backing off" with the underlying
error `dial tcp: lookup api.openai.com on 127.0.0.53:53: server
misbehaving` (80+ occurrences -- systemd-resolved's local stub resolver
having a bad patch) plus a handful of `network is unreachable` dialing an
IPv6 address for api.openai.com. tunnel-client's OWN internal backoff/
retry recovers from nearly all of these within one or two poll cycles
("poller recovered; polling operational" follows every one of them in
the log) -- which is exactly WHY `systemctl --user show ... NRestarts`
reads 0 across the same 4 days: the OS process never exits, so systemd's
Restart= policy never engages at all. A long enough run of these
(sustained local DNS flakiness, or several backed-off retries in a row)
can push the tunnel's time-since-last-successful-control-plane-poll past
whatever staleness threshold OpenAI's platform uses for "has this tunnel
been seen recently" (reported to the user as "Tunnel-client has not been
seen for 300 seconds") while the process itself stays `active (running)`
throughout -- a "hung but alive" failure mode systemd's own process-level
restart policy is structurally unable to detect. That gap is what the
watchdog below exists to close: it checks APPLICATION-level liveness
(the tunnel's own last-successful-poll timestamp), not just process
liveness, and restarts ONLY the tunnel service when that specific gap
opens -- never touching local MCP/tmux for a tunnel-side staleness event.

Four failure domains this module tells apart, per the task's own framing:
  1. local MCP server down            -> mcp_local="unhealthy"
  2. tunnel-client process down/hung  -> tunnel_process="inactive" OR
                                          tunnel_process="active" with
                                          tunnel_ready="stale"/"unknown"
  3. host/network unavailable         -> network_dns_tls="fail"
  4. OpenAI/ChatGPT-side state issue  -> everything local checks out
                                          (mcp_local healthy, tunnel_ready
                                          fresh) but ChatGPT still can't
                                          reach it -- chatgpt_side=
                                          "suspected_platform_side" (see
                                          this module's own docstring
                                          note: local code cannot verify
                                          the ChatGPT-side connector state
                                          at all, only rule out every
                                          local cause).
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MCP_HEALTH_URL = "http://127.0.0.1:8766/health/ready"
DEFAULT_TUNNEL_HEALTH_BASE = "http://127.0.0.1:8767"
DEFAULT_TUNNEL_UNIT = "terminal-mcp-tunnel.service"
DEFAULT_MCP_UNIT = "terminal-mcp-http.service"
DEFAULT_DNS_HOST = "api.openai.com"
DEFAULT_HTTP_TIMEOUT_SECONDS = 3.0

# "90-180s" per the task's own framing -- 150s sits in the middle, well
# above normal long-poll/backoff jitter (tunnel-client's own poll-timeout
# is 30s, retry_in_ms backoff observed up to ~10s per attempt in the real
# log evidence above) but short enough that a genuine hang is caught well
# before it reaches the reported 300s "not seen" threshold.
DEFAULT_STALE_THRESHOLD_SECONDS = 150.0

# Cooldown/backoff (task item 3's "tránh restart loop khi OpenAI-side
# outage", item 9 Test D): after this many consecutive restarts of the
# SAME target with no intervening healthy check, stop restarting it and
# report cooldown_active instead -- a real platform-side outage must
# never turn into an unbounded local restart loop.
DEFAULT_COOLDOWN_TRIGGER_COUNT = 3
DEFAULT_COOLDOWN_SECONDS = 900.0  # 15 minutes

# How long a just-(re)started tunnel-client gets before "no heartbeat
# yet" counts as a hang worth restarting for, rather than the expected,
# normal startup window before its first control-plane poll completes.
# Comfortably above the tunnel's own 30s poll-timeout + backoff, well
# under the stale threshold above it.
DEFAULT_STARTUP_GRACE_SECONDS = 60.0


def default_state_path() -> Path:
    import os
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "terminal-mcp" / "tunnel_watchdog_state.json"


def _now() -> float:
    return time.time()


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Individual checks -- each one is a pure function of its inputs (a URL, a
# unit name), no shared state, so each is independently unit-testable by
# injecting a fake urlopen/subprocess.run.
# ---------------------------------------------------------------------------

def check_mcp_local(*, url: str = DEFAULT_MCP_HEALTH_URL,
                    timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """The exact endpoint every other health check in this project already
    uses (server_http's own /health/ready) -- never a second, parallel
    notion of "is the local MCP server healthy"."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read())
            if response.status == 200 and body.get("status") == "ready":
                return {"status": "healthy", "detail": "ok"}
            return {"status": "unhealthy", "detail": f"status={response.status} body={body}"}
    except urllib.error.URLError as exc:
        return {"status": "unhealthy", "detail": f"{type(exc).__name__}: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001 -- a health probe must never raise, only report
        return {"status": "unhealthy", "detail": f"{type(exc).__name__}: {exc}"}


def check_systemd_unit(unit: str, *, timeout: float = 5.0, now: float | None = None) -> dict[str, Any]:
    """`systemctl --user show` for ActiveState, SubState, and how long the
    unit has been active. The SubState distinction matters:
    SubState="failed" (StartLimitBurst exhausted) is a DIFFERENT recovery
    action (reset-failed, then start) from SubState="dead"/inactive (a
    plain start suffices), which ActiveState alone can't tell apart.
    `uptime_seconds` exists so decide_action can give a just-(re)started
    process a startup grace period before treating "no heartbeat yet" as
    a hang worth restarting for -- see decide_action's own comment on
    why that grace period is not optional."""
    now = _now() if now is None else now
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState",
             "-p", "NRestarts", "-p", "ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        active_state = values.get("ActiveState", "unknown")
        uptime_seconds = None
        timestamp = values.get("ActiveEnterTimestamp", "")
        if timestamp:
            try:
                # systemd's own default format, e.g. "Fri 2026-09-04 12:28:47
                # +07" -- a bare 2-digit UTC offset with no minutes, which
                # %z does NOT accept on its own (needs at least +HHMM);
                # confirmed live against this host's own real
                # `systemctl --user show` output, not assumed.
                normalized = re.sub(r"([+-]\d{2})$", r"\1:00", timestamp)
                active_since = datetime.strptime(normalized, "%a %Y-%m-%d %H:%M:%S %z").timestamp()
                uptime_seconds = max(0.0, now - active_since)
            except ValueError:
                pass  # unparsed timestamp -- uptime stays None, never a guessed value
        return {
            "active": active_state == "active",
            "active_state": active_state,
            "sub_state": values.get("SubState", "unknown"),
            "n_restarts": int(values.get("NRestarts", 0) or 0),
            "uptime_seconds": uptime_seconds,
        }
    except Exception as exc:  # noqa: BLE001
        return {"active": False, "active_state": "unknown", "sub_state": "unknown",
               "n_restarts": None, "uptime_seconds": None, "error": f"{type(exc).__name__}: {exc}"}


_LAST_POLL_METRIC = re.compile(
    r"^commands_poll_last_successful_timestamp_seconds(?:\{[^}]*\})?\s+([0-9.eE+\-]+)\s*$", re.MULTILINE,
)
# 2020-01-01T00:00:00Z -- anything earlier (in practice: exactly 0.0, a
# never-Set() Prometheus gauge's zero value) is treated as "no successful
# poll yet", never a real timestamp. See check_tunnel_ready's own comment
# on this at the one call site that uses it.
_EARLIEST_PLAUSIBLE_EPOCH = 1_577_836_800.0


def check_tunnel_ready(*, base_url: str = DEFAULT_TUNNEL_HEALTH_BASE,
                       timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
                       now: float | None = None) -> dict[str, Any]:
    """tunnel-client's own /healthz + /readyz + the last-successful-
    control-plane-poll gauge from /metrics (`commands_poll_last_
    successful_timestamp_seconds`) -- the exact metric backing
    `tunnel-client health --require-control-plane-poll` (confirmed live
    against a real running instance; parsed directly from /metrics here
    instead of shelling out to that CLI, so this has no runtime
    dependency on the tunnel-client binary's own subcommands staying
    stable). This is the ONE signal that tells "process alive" (systemd/
    check_systemd_unit) apart from "actually still talking to the control
    plane" -- the real failure mode this whole module exists for."""
    now = _now() if now is None else now
    healthz_ok = _probe_ok(f"{base_url}/healthz", timeout)
    readyz_ok = _probe_ok(f"{base_url}/readyz", timeout)
    try:
        with urllib.request.urlopen(f"{base_url}/metrics", timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"ready": "unknown", "healthz_ok": healthz_ok, "readyz_ok": readyz_ok,
               "last_heartbeat_epoch": None, "last_heartbeat_age_sec": None,
               "detail": f"metrics unreachable: {type(exc).__name__}: {exc}"}
    match = _LAST_POLL_METRIC.search(body)
    if match is None:
        return {"ready": "unknown", "healthz_ok": healthz_ok, "readyz_ok": readyz_ok,
               "last_heartbeat_epoch": None, "last_heartbeat_age_sec": None,
               "detail": "commands_poll_last_successful_timestamp_seconds not found in /metrics"}
    epoch = float(match.group(1))
    # A freshly-registered-but-never-Set() Prometheus gauge reads back as
    # its zero value (0.0), not an absent line -- real, observed live
    # against this project's own tunnel-client immediately after a
    # restart, before its first poll had completed. Treating that as a
    # real epoch would compute age = now - 0 ~= now (a multi-decade
    # "age"), which is not a stale heartbeat, it's simply "no successful
    # poll yet" -- exactly like the metric being absent, never a fake
    # multi-billion-second staleness that would trigger an immediate,
    # spurious restart the instant a legitimate restart has ever happened.
    if epoch < _EARLIEST_PLAUSIBLE_EPOCH:
        return {"ready": "unknown", "healthz_ok": healthz_ok, "readyz_ok": readyz_ok,
               "last_heartbeat_epoch": None, "last_heartbeat_age_sec": None,
               "detail": f"commands_poll_last_successful_timestamp_seconds={epoch} looks uninitialized "
                        "(no successful poll yet since the process started)"}
    age = max(0.0, now - epoch)
    return {"ready": "ready" if healthz_ok and readyz_ok else "stale",
           "healthz_ok": healthz_ok, "readyz_ok": readyz_ok,
           "last_heartbeat_epoch": epoch, "last_heartbeat_age_sec": age, "detail": "ok"}


def _probe_ok(url: str, timeout: float) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def check_network_dns_tls(*, host: str = DEFAULT_DNS_HOST, port: int = 443,
                          timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Independent of the tunnel-client's own control-plane poll -- a
    real DNS resolution + TCP connect + TLS handshake against the same
    host the tunnel talks to, so a genuine local network/DNS/TLS problem
    (the real root cause found in this project's own log evidence, see
    module docstring) is distinguishable from every other failure domain,
    not just inferred from the tunnel's own error strings."""
    try:
        addr_info = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return {"status": "fail", "detail": f"DNS resolution failed: {exc}"}
    if not addr_info:
        return {"status": "fail", "detail": "DNS resolution returned no addresses"}
    context = ssl.create_default_context()
    last_error: str | None = None
    for family, socktype, proto, _canon, sockaddr in addr_info:
        try:
            with socket.socket(family, socktype, proto) as raw_sock:
                raw_sock.settimeout(timeout)
                raw_sock.connect(sockaddr)
                with context.wrap_socket(raw_sock, server_hostname=host):
                    return {"status": "pass", "detail": f"connected to {sockaddr[0]}"}
        except OSError as exc:
            last_error = f"{sockaddr[0]}: {exc}"
            continue
    return {"status": "fail", "detail": f"TCP/TLS connect failed for every resolved address: {last_error}"}


# ---------------------------------------------------------------------------
# Persisted watchdog state -- item 3/8's "persist last action/reason/
# timestamp", "metrics/counters", used by both the watchdog (to decide/
# record) and the doctor CLI + dashboard banner (to report, read-only).
# ---------------------------------------------------------------------------

@dataclass
class WatchdogState:
    tunnel_restart_count: int = 0
    mcp_restart_count: int = 0
    consecutive_tunnel_restarts: int = 0
    consecutive_mcp_restarts: int = 0
    last_tunnel_restart_at: float | None = None
    last_mcp_restart_at: float | None = None
    tunnel_cooldown_until: float | None = None
    mcp_cooldown_until: float | None = None
    last_action: str = "none"
    last_action_reason: str = ""
    last_action_at: float | None = None
    last_check_at: float | None = None
    last_heartbeat_age_sec: float | None = None
    consecutive_healthy_checks: int = 0

    @classmethod
    def load(cls, path: Path) -> "WatchdogState":
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls()
        known_fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in raw.items() if k in known_fields})

    def save(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(self.__dict__, indent=2, sort_keys=True)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(payload)
        tmp_path.replace(path)  # atomic on the same filesystem -- never a half-written state file
        try:
            path.chmod(0o600)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Diagnosis: combines the checks above into the exact field set item 4
# asks for. Pure given its inputs (checks are injectable for tests).
# ---------------------------------------------------------------------------

def diagnose(*, mcp_url: str = DEFAULT_MCP_HEALTH_URL, tunnel_base_url: str = DEFAULT_TUNNEL_HEALTH_BASE,
            tunnel_unit: str = DEFAULT_TUNNEL_UNIT, dns_host: str = DEFAULT_DNS_HOST,
            stale_threshold: float = DEFAULT_STALE_THRESHOLD_SECONDS,
            state: WatchdogState | None = None, skip_network_check: bool = False) -> dict[str, Any]:
    mcp = check_mcp_local(url=mcp_url)
    tunnel_proc = check_systemd_unit(tunnel_unit)
    tunnel_ready_info = check_tunnel_ready(base_url=tunnel_base_url)
    network = check_network_dns_tls(host=dns_host) if not skip_network_check else {"status": "unknown", "detail": "skipped"}

    age = tunnel_ready_info.get("last_heartbeat_age_sec")
    if not tunnel_proc["active"]:
        tunnel_ready = "unknown"
    elif age is None:
        tunnel_ready = tunnel_ready_info["ready"]  # "unknown" (metrics unreachable) or already computed
    elif age > stale_threshold:
        tunnel_ready = "stale"
    else:
        tunnel_ready = "ready"

    # chatgpt_side: this process can only ever RULE OUT local causes, never
    # confirm or deny anything about ChatGPT's own connector/session state
    # (task item 7). "suspected-platform-side" is raised ONLY when every
    # local check this host can run has passed -- mcp_local healthy,
    # tunnel_ready fresh, DNS/TLS to api.openai.com reachable -- meaning a
    # still-failing ChatGPT tool call has nothing local left to explain
    # it. Otherwise "cannot-verify": a local cause is already evident (or
    # the network check itself couldn't run), so there is no basis yet to
    # even suspect the platform side -- fix the local cause first.
    if mcp["status"] == "healthy" and tunnel_ready == "ready" and network["status"] in ("pass", "unknown"):
        chatgpt_side = "suspected-platform-side"
    else:
        chatgpt_side = "cannot-verify"

    recommended_action = _recommend(mcp["status"], tunnel_proc, tunnel_ready, network["status"])

    return {
        "mcp_local": mcp["status"],
        "mcp_local_detail": mcp["detail"],
        "tunnel_process": "active" if tunnel_proc["active"] else tunnel_proc["active_state"],
        "tunnel_process_sub_state": tunnel_proc["sub_state"],
        "tunnel_process_uptime_sec": (
            round(tunnel_proc["uptime_seconds"], 1) if tunnel_proc.get("uptime_seconds") is not None else None
        ),
        "tunnel_ready": tunnel_ready,
        "last_heartbeat_age_sec": round(age, 1) if age is not None else None,
        "network_dns_tls": network["status"],
        "network_dns_tls_detail": network["detail"],
        "chatgpt_side": chatgpt_side,
        "last_recovery_action": state.last_action if state else None,
        "last_recovery_action_reason": state.last_action_reason if state else None,
        "last_recovery_action_at": _iso(state.last_action_at) if state else None,
        "recommended_action": recommended_action,
    }


def _recommend(mcp_status: str, tunnel_proc: dict[str, Any], tunnel_ready: str, network_status: str) -> str:
    if mcp_status != "healthy":
        return "restart terminal-mcp-http.service (local MCP server unhealthy)"
    if network_status == "fail":
        return "check host network/DNS -- api.openai.com unreachable from this host"
    if not tunnel_proc["active"]:
        if tunnel_proc["sub_state"] == "failed":
            return "systemctl --user reset-failed terminal-mcp-tunnel.service && systemctl --user start terminal-mcp-tunnel.service"
        return "systemctl --user start terminal-mcp-tunnel.service"
    if tunnel_ready == "stale":
        return "restart terminal-mcp-tunnel.service (process alive but control-plane poll is stale)"
    if tunnel_ready == "unknown":
        return "investigate terminal-mcp-tunnel.service health port (127.0.0.1:8767) -- unreachable while the unit reports active"
    return "none -- all local checks healthy"


# One recent-action-aware label, coarser than the full diagnosis, for the
# dashboard's own small health banner (task item 5: "chỉ cần một trạng
# thái tổng... không spam UI"). Never a fifth state, never more detail --
# a dashboard viewer wanting the full breakdown already has `terminal-mcp-
# doctor connection`.
BANNER_CONNECTED = "Connected"
BANNER_RECOVERING = "Recovering"
BANNER_TUNNEL_STALE = "Local OK but tunnel stale"
BANNER_MCP_DOWN = "Local MCP down"
# Window after a recovery action within which a NOW-healthy state still
# reads as "Recovering" rather than silently reverting straight to
# "Connected" -- long enough for an operator glancing at the dashboard to
# actually see that something just happened, short enough to never look
# like a permanently-stuck state.
RECOVERING_DISPLAY_SECONDS = 120.0


def banner_status(diag: dict[str, Any], state: WatchdogState, *, now: float | None = None) -> str:
    now = _now() if now is None else now
    healthy = diag["mcp_local"] == "healthy" and diag["tunnel_ready"] == "ready"
    recently_acted = (
        state.last_action in ("restart_mcp", "restart_tunnel", "reset_failed_tunnel_then_start")
        and state.last_action_at is not None
        and (now - state.last_action_at) < RECOVERING_DISPLAY_SECONDS
    )
    if healthy:
        return BANNER_RECOVERING if recently_acted else BANNER_CONNECTED
    if diag["mcp_local"] != "healthy":
        return BANNER_MCP_DOWN
    return BANNER_TUNNEL_STALE


# ---------------------------------------------------------------------------
# Decision logic (watchdog only) -- pure function of (diagnosis, state,
# thresholds) -> an action to take, so the cooldown/backoff behavior
# (task item 3's "tránh restart loop", item 9 Test D) is unit-testable
# without a real tunnel or a real outage.
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    action: str  # "none" | "restart_mcp" | "restart_tunnel" | "reset_failed_tunnel_then_start"
    reason: str


def decide_action(diag: dict[str, Any], state: WatchdogState, *,
                  cooldown_trigger_count: int = DEFAULT_COOLDOWN_TRIGGER_COUNT,
                  cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                  startup_grace_seconds: float = DEFAULT_STARTUP_GRACE_SECONDS,
                  now: float | None = None) -> Decision:
    now = _now() if now is None else now

    # Local MCP down -- its own target/cooldown, completely independent of
    # the tunnel's state; never conflated into one restart decision.
    if diag["mcp_local"] != "healthy":
        if state.mcp_cooldown_until and now < state.mcp_cooldown_until:
            remaining = round(state.mcp_cooldown_until - now)
            return Decision("none", f"mcp_cooldown_active ({remaining}s remaining) -- "
                            f"repeated local MCP restarts without recovery, avoiding a restart loop")
        return Decision("restart_mcp", f"mcp_local={diag['mcp_local']}: {diag['mcp_local_detail']}")

    # Local MCP is healthy from here on -- any tunnel-side action is safe
    # to consider independently.
    if diag["tunnel_process_sub_state"] == "failed":
        return Decision("reset_failed_tunnel_then_start",
                        "tunnel unit is in systemd 'failed' SubState (StartLimitBurst likely exhausted)")

    if diag["tunnel_process"] != "active":
        # Not "failed" specifically -- a plain (not StartLimit-exhausted)
        # inactive/dead state. Restart=always on the unit itself should
        # already be bringing it back; this is a fallback in case it
        # somehow isn't (e.g. it was stopped externally).
        return Decision("restart_tunnel", f"tunnel_process={diag['tunnel_process']} (not active)")

    if diag["tunnel_ready"] in ("stale", "unknown"):
        # Startup grace period: a just-(re)started process legitimately
        # has no heartbeat yet (tunnel_ready="unknown", last_heartbeat_
        # age_sec=None) until its first control-plane poll completes --
        # real, observed live against this project's own tunnel-client
        # (a never-Set() Prometheus gauge reads back as 0.0, not absent;
        # see check_tunnel_ready's own comment). Without this grace
        # period, restarting the unit would make it look freshly-
        # unhealthy to the very next check, restarting it again --
        # exactly the restart loop this whole module exists to prevent,
        # except self-inflicted instead of platform-side. Only applies
        # while there is NO real numeric age at all (a genuinely stale
        # but non-zero age, e.g. 500s on a long-running process, is never
        # excused by this -- that path is unaffected).
        uptime = diag.get("tunnel_process_uptime_sec")
        if diag.get("last_heartbeat_age_sec") is None and uptime is not None and uptime < startup_grace_seconds:
            return Decision("none", f"startup_grace_period (uptime={uptime}s < {startup_grace_seconds}s, "
                            "no heartbeat yet -- expected for a just-started process)")
        if state.tunnel_cooldown_until and now < state.tunnel_cooldown_until:
            remaining = round(state.tunnel_cooldown_until - now)
            return Decision("none", f"tunnel_cooldown_active ({remaining}s remaining) -- "
                            f"repeated tunnel restarts without the heartbeat recovering, "
                            f"suspected_platform_side or a sustained local network issue -- avoiding a restart loop")
        age = diag.get("last_heartbeat_age_sec")
        detail = f"age={age}s" if age is not None else "heartbeat unreadable"
        return Decision("restart_tunnel", f"tunnel_ready={diag['tunnel_ready']} ({detail})")

    return Decision("none", "all checks healthy")


def apply_decision(decision: Decision, state: WatchdogState, *,
                   tunnel_unit: str = DEFAULT_TUNNEL_UNIT, mcp_unit: str = DEFAULT_MCP_UNIT,
                   cooldown_trigger_count: int = DEFAULT_COOLDOWN_TRIGGER_COUNT,
                   cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
                   run: Any = None, now: float | None = None) -> WatchdogState:
    """Mutates and returns `state` to reflect `decision` having been
    carried out -- `run` defaults to a real `subprocess.run` (injectable
    for tests, which must never actually call systemctl). Counting and
    cooldown are per-target (tunnel vs mcp), never shared, so a run of
    bad tunnel restarts can never suppress a genuinely-needed MCP
    restart or vice versa."""
    now = _now() if now is None else now
    run = subprocess.run if run is None else run
    state.last_check_at = now
    state.last_heartbeat_age_sec = None  # caller (watchdog main) fills this in from the diagnosis after

    if decision.action == "none":
        # A clean check resets the consecutive-restart counters for BOTH
        # targets -- a cooldown is for repeated restarts with no
        # intervening recovery, not a permanent state once triggered.
        if "cooldown_active" not in decision.reason:
            state.consecutive_tunnel_restarts = 0
            state.consecutive_mcp_restarts = 0
            state.consecutive_healthy_checks += 1
            # A genuine recovery ends any leftover cooldown immediately --
            # a stale cooldown_until timestamp must never suppress a
            # restart for a NEW, later staleness episode.
            state.tunnel_cooldown_until = None
            state.mcp_cooldown_until = None
        state.last_action = "none"
        state.last_action_reason = decision.reason
        state.last_action_at = now
        return state

    if decision.action == "restart_mcp":
        run(["systemctl", "--user", "restart", mcp_unit], capture_output=True, text=True, timeout=30, check=False)
        state.mcp_restart_count += 1
        state.consecutive_mcp_restarts += 1
        state.consecutive_healthy_checks = 0
        state.last_mcp_restart_at = now
        if state.consecutive_mcp_restarts >= cooldown_trigger_count:
            state.mcp_cooldown_until = now + cooldown_seconds
    elif decision.action == "restart_tunnel":
        run(["systemctl", "--user", "restart", tunnel_unit], capture_output=True, text=True, timeout=30, check=False)
        state.tunnel_restart_count += 1
        state.consecutive_tunnel_restarts += 1
        state.consecutive_healthy_checks = 0
        state.last_tunnel_restart_at = now
        if state.consecutive_tunnel_restarts >= cooldown_trigger_count:
            state.tunnel_cooldown_until = now + cooldown_seconds
    elif decision.action == "reset_failed_tunnel_then_start":
        run(["systemctl", "--user", "reset-failed", tunnel_unit], capture_output=True, text=True, timeout=30, check=False)
        run(["systemctl", "--user", "start", tunnel_unit], capture_output=True, text=True, timeout=30, check=False)
        state.tunnel_restart_count += 1
        state.consecutive_tunnel_restarts += 1
        state.consecutive_healthy_checks = 0
        state.last_tunnel_restart_at = now
        if state.consecutive_tunnel_restarts >= cooldown_trigger_count:
            state.tunnel_cooldown_until = now + cooldown_seconds

    state.last_action = decision.action
    state.last_action_reason = decision.reason
    state.last_action_at = now
    return state
