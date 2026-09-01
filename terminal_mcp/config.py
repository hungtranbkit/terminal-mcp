from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PermissionsConfig:
    terminal_read: bool = True
    terminal_input: bool = False


@dataclass(frozen=True)
class InputPolicyConfig:
    allowed_session_patterns: tuple[str, ...] = (
        "claude-*", "codex-*", "mesflow-*", "projectflow-*", "terminal-mcp-*",
    )
    denied_session_patterns: tuple[str, ...] = (
        "root*", "ssh-*", "database-*", "password-*", "secret-*", "prod-shell-*",
    )
    allow_send_text: bool = True
    allow_keys: tuple[str, ...] = ("Enter", "Escape", "Up", "Down", "Left", "Right", "Tab")
    sensitive_keys_require_confirmation: tuple[str, ...] = ("C-c", "C-d")
    max_text_length: int = 12_000
    allowed_sensitive_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class SupervisorConfig:
    # Disabled by default everywhere (example, and production config.yaml
    # unless an operator explicitly opts in) — v1 is detection/event-queue
    # only, never autonomous, but it still runs an always-on background
    # poll loop and that should never be silently on.
    enabled: bool = False
    poll_interval_seconds: int = 20
    idle_threshold_seconds: int = 45
    max_iterations: int = 20
    same_failure_limit: int = 2
    event_retention: int = 500
    watched_session_patterns: tuple[str, ...] = ()
    watched_bindings: tuple[str, ...] = ()
    # v2 (decision/send layer) global kill switch — independent of, and in
    # addition to, each watch's own per-watch policy_mode (which already
    # defaults to observe_only). Both gates must be satisfied before
    # supervisor2_execute_send will ever actually send anything: this one
    # protects the whole instance even if some watch's policy was
    # (mis)configured to approved_auto_continue.
    v2_enabled: bool = False
    # Minimum quiet time (pane output unchanged, no state regression) a
    # COMPLETION_CANDIDATE must hold, on a *later* poll than the one that
    # first detected it, before it is promoted to VERIFIED_DONE. A matched
    # single-use nonce (see supervisor.py) skips this wait -- that is
    # already stronger, harder-to-spoof evidence than elapsed silence.
    completion_verify_quiet_seconds: int = 10


@dataclass(frozen=True)
class DashboardConfig:
    # A boundary specific to the web dashboard's own mutation routes
    # (session input, supervisor event ack, supervisor2 pause) --
    # independent of, and enforced in *addition to*, permissions.
    # terminal_input/input_policy (which still gate the underlying
    # TerminalService calls exactly as before regardless of this flag).
    # Lets an operator publish a read-only dashboard (e.g. over a public
    # tunnel) while a separate, locally-only MCP control plane keeps full
    # input capability, or vice versa. Defaults True so existing
    # deployments/UI keep working unchanged unless an operator opts into
    # the stricter split.
    mutations_enabled: bool = True
    # Cloudflare Access identity verification for mutation routes (P1
    # hardening item #2): the tunnel config only makes Access *redirect*
    # unauthenticated browsers to a login page at the edge -- it proves
    # nothing to this application about a request that DOES arrive, since
    # that trust is entirely topological (this origin is reachable only
    # through the tunnel today, but that is a deployment fact, not
    # something this code verifies). When both fields below are set, every
    # mutation route additionally requires and cryptographically verifies
    # the Cf-Access-Jwt-Assertion header Access attaches to authenticated
    # requests (RS256, verified against team_domain's published JWKS, aud
    # must match audience, exp/nbf enforced) -- see cf_access.py. Left
    # unset (the default), this check is a complete no-op: an operator not
    # using Cloudflare Access is unaffected, and the dashboard's existing
    # behavior is unchanged.
    cloudflare_access_team_domain: str | None = None
    cloudflare_access_audience: str | None = None
    # CSRF/Origin defense (P1 hardening item #3): unlike the Access check
    # above, this is NOT opt-in -- every mutation route always requires a
    # same-origin Origin (or Referer) header, since the dashboard's own JS
    # always sends one and no legitimate cross-site caller needs to POST
    # here. allowed_origins lets an operator add extra trusted origins
    # (e.g. a reverse proxy that rewrites Host) beyond the request's own
    # Host header, which is always accepted.
    allowed_origins: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    permissions: PermissionsConfig
    allowed_session_patterns: tuple[str, ...]
    max_capture_lines: int = 2000
    default_tail_lines: int = 200
    input_policy: InputPolicyConfig = InputPolicyConfig()
    supervisor: SupervisorConfig = SupervisorConfig()
    dashboard: DashboardConfig = DashboardConfig()


DEFAULT_PATTERNS = ("claude-*", "codex-*", "agent-*", "test-*")


def default_config_path() -> Path:
    override = os.environ.get("TERMINAL_MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[1] / "config.yaml"


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else default_config_path()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    permissions = raw.get("permissions", {})
    patterns = raw.get("allowed_session_patterns", list(DEFAULT_PATTERNS))
    max_lines = int(raw.get("max_capture_lines", 2000))
    tail_lines = int(raw.get("default_tail_lines", 200))
    input_raw = raw.get("input_policy", {})

    def string_tuple(name: str, default: tuple[str, ...], *, allow_empty: bool = False) -> tuple[str, ...]:
        value = input_raw.get(name, list(default))
        if not isinstance(value, list) or (not value and not allow_empty) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"input_policy.{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}list of strings")
        return tuple(value)

    if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
        raise ValueError("allowed_session_patterns must be a non-empty list of strings")
    if not 1 <= max_lines <= 100_000:
        raise ValueError("max_capture_lines must be between 1 and 100000")
    if not 1 <= tail_lines <= max_lines:
        raise ValueError("default_tail_lines must be between 1 and max_capture_lines")
    max_text_length = int(input_raw.get("max_text_length", 12_000))
    if not 1 <= max_text_length <= 1_000_000:
        raise ValueError("input_policy.max_text_length must be between 1 and 1000000")

    supervisor_raw = raw.get("supervisor", {})

    def string_tuple_supervisor(name: str) -> tuple[str, ...]:
        value = supervisor_raw.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"supervisor.{name} must be a list of strings")
        return tuple(value)

    poll_interval = int(supervisor_raw.get("poll_interval_seconds", 20))
    idle_threshold = int(supervisor_raw.get("idle_threshold_seconds", 45))
    max_iterations = int(supervisor_raw.get("max_iterations", 20))
    same_failure_limit = int(supervisor_raw.get("same_failure_limit", 2))
    event_retention = int(supervisor_raw.get("event_retention", 500))
    completion_verify_quiet_seconds = int(supervisor_raw.get("completion_verify_quiet_seconds", 10))
    # >=5s floor: even with enabled:true and a mistyped tiny interval, this
    # never becomes an accidental hot loop hammering tmux/CPU.
    if poll_interval < 5:
        raise ValueError("supervisor.poll_interval_seconds must be at least 5")
    if idle_threshold < 1:
        raise ValueError("supervisor.idle_threshold_seconds must be at least 1")
    if max_iterations < 1:
        raise ValueError("supervisor.max_iterations must be at least 1")
    if same_failure_limit < 1:
        raise ValueError("supervisor.same_failure_limit must be at least 1")
    if not 1 <= event_retention <= 100_000:
        raise ValueError("supervisor.event_retention must be between 1 and 100000")
    if completion_verify_quiet_seconds < 1:
        raise ValueError("supervisor.completion_verify_quiet_seconds must be at least 1")

    return AppConfig(
        permissions=PermissionsConfig(
            terminal_read=bool(permissions.get("terminal_read", True)),
            terminal_input=bool(permissions.get("terminal_input", False)),
        ),
        allowed_session_patterns=tuple(patterns),
        max_capture_lines=max_lines,
        default_tail_lines=tail_lines,
        input_policy=InputPolicyConfig(
            allowed_session_patterns=string_tuple("allowed_session_patterns", InputPolicyConfig.allowed_session_patterns),
            denied_session_patterns=string_tuple("denied_session_patterns", InputPolicyConfig.denied_session_patterns),
            allow_send_text=bool(input_raw.get("allow_send_text", True)),
            allow_keys=string_tuple("allow_keys", InputPolicyConfig.allow_keys),
            sensitive_keys_require_confirmation=string_tuple(
                "sensitive_keys_require_confirmation", InputPolicyConfig.sensitive_keys_require_confirmation,
            ),
            max_text_length=max_text_length,
            allowed_sensitive_commands=string_tuple("allowed_sensitive_commands", (), allow_empty=True),
        ),
        supervisor=SupervisorConfig(
            enabled=bool(supervisor_raw.get("enabled", False)),
            poll_interval_seconds=poll_interval,
            idle_threshold_seconds=idle_threshold,
            max_iterations=max_iterations,
            same_failure_limit=same_failure_limit,
            event_retention=event_retention,
            watched_session_patterns=string_tuple_supervisor("watched_session_patterns"),
            watched_bindings=string_tuple_supervisor("watched_bindings"),
            v2_enabled=bool(supervisor_raw.get("v2_enabled", False)),
            completion_verify_quiet_seconds=completion_verify_quiet_seconds,
        ),
        dashboard=_load_dashboard_config(raw.get("dashboard", {})),
    )


def _load_dashboard_config(dashboard_raw: object) -> DashboardConfig:
    if not isinstance(dashboard_raw, dict):
        dashboard_raw = {}
    team_domain = dashboard_raw.get("cloudflare_access_team_domain")
    audience = dashboard_raw.get("cloudflare_access_audience")
    if team_domain is not None and not (isinstance(team_domain, str) and team_domain.strip()):
        raise ValueError("dashboard.cloudflare_access_team_domain must be a non-empty string")
    if audience is not None and not (isinstance(audience, str) and audience.strip()):
        raise ValueError("dashboard.cloudflare_access_audience must be a non-empty string")
    origins = dashboard_raw.get("allowed_origins", [])
    if not isinstance(origins, list) or not all(isinstance(o, str) and o for o in origins):
        raise ValueError("dashboard.allowed_origins must be a list of strings")
    return DashboardConfig(
        mutations_enabled=bool(dashboard_raw.get("mutations_enabled", True)),
        cloudflare_access_team_domain=team_domain,
        cloudflare_access_audience=audience,
        allowed_origins=tuple(origins),
    )
