from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PermissionsConfig:
    terminal_read: bool = True
    terminal_input: bool = False
    # Prompt-submission reliability upgrade, permission-model normalization
    # (see docs/prompt-submission.md): the three concepts a caller actually
    # cares about are `read` (terminal_read), `send_prompt` (terminal_input
    # gating terminal_send_text/_bound -- the verified, adapter-guarded
    # composition path input_policy.allow_send_text already scopes further),
    # and `send_keys` (raw, unverified key sequences -- already restricted
    # to a fixed vocabulary by input_policy.allow_keys/sensitive_keys_
    # require_confirmation). What was missing was a way to disable raw
    # send_keys specifically while keeping send_prompt enabled -- both were
    # gated ONLY by the single terminal_input flag. Defaults to True (every
    # existing config.yaml keeps its exact current behavior unchanged); set
    # False to disable terminal_send_keys entirely while terminal_send_text/
    # terminal_send_bound (send_prompt) keep working.
    allow_send_keys: bool = True
    # ask_chatgpt bridge (docs/ask-chatgpt-bridge.md §7): independent of
    # every field above, same reasoning as allow_send_keys's own split from
    # terminal_input -- "can send text to a tmux pane" must never imply
    # "can drive a browser session against a third-party product," even
    # though both are mechanically "sending text somewhere." This is the
    # single global on/off switch for the whole feature; AskChatGptConfig
    # below only ever holds *operational* parameters (timeouts, allowed
    # modes/models, the tool round-trip allowlist), never a second enabled
    # flag -- one gate, checked first, not two to keep in sync. Defaults
    # False: every existing deployment is completely unaffected until an
    # operator explicitly opts in.
    ask_chatgpt: bool = False


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
    # P0 Part C: default bound for a per-watch verifier's test_command
    # (verifier.py) -- a watch can override this via its own
    # verifier_timeout_seconds when configured, this is only the default
    # used when it doesn't specify one.
    verifier_timeout_seconds: float = 120.0


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
    # Web terminal (xterm.js over WebSocket, attaching a browser directly
    # to an existing tmux session's real pty -- webterm.py). Disabled by
    # default, same opt-in posture as session_lifecycle.enabled: this
    # spawns a real OS process (`tmux attach-session`) with a live,
    # bidirectional pty, not just another read of already-captured pane
    # text, so an existing deployment's config.yaml keeps its exact
    # current behavior until an operator explicitly opts in here. Gates
    # BOTH dashboard variants identically (TerminalService.
    # terminal_web_terminal_access, the one place this flag is checked --
    # see core.py) -- there is no separate flag per dashboard.
    web_terminal_enabled: bool = False


@dataclass(frozen=True)
class SessionLifecycleConfig:
    """New tmux session create/detach/delete (dashboard + the parallel
    terminal_create_session/_detach_session/_delete_session MCP tools --
    core.py's SessionLifecycleService is the ONE implementation both
    callers share; see lifecycle.py). Disabled by default, same posture
    as terminal_input: this is a capability that creates real processes,
    not read-only observation, so an existing deployment's config.yaml
    keeps its exact current behavior (no lifecycle tools/routes usable)
    until an operator explicitly opts in here.

    allowed_cwd_roots: absolute paths a requested working_directory must
    resolve (symlinks followed) inside of -- empty (the default) falls
    back to `(str(Path.home()),)` at use time (see lifecycle.py), never
    to "/" or an unbounded root. protected_sessions: never deletable via
    this feature, regardless of caller -- "terminal-mcp" (this project's
    own controlling session) is always in the effective set even if an
    operator's list omits it; see _load_session_lifecycle_config below.
    launch_commands: agent_type -> the exact literal argv token run as
    the new session's initial command (never client-supplied -- see
    README's "Create an agent tmux session" for the same two binaries
    invoked here, "claude"/"codex"). "shell" always ignores this and
    starts the session's plain default shell, no entry needed."""
    enabled: bool = False
    allowed_cwd_roots: tuple[str, ...] = ()
    protected_sessions: tuple[str, ...] = ("terminal-mcp",)
    launch_commands: tuple[tuple[str, str], ...] = (("claude", "claude"), ("codex", "codex"))
    create_ready_timeout_seconds: float = 5.0
    default_grant_mode: str = "none"

    def __post_init__(self) -> None:
        # The "terminal-mcp is always protected, even if omitted" guarantee
        # (see the class docstring and _load_session_lifecycle_config's own
        # comment) previously lived ONLY in the YAML-loading function below
        # -- true for a config read from config.yaml, but silently false for
        # any other construction path (tests building SessionLifecycleConfig
        # directly, or a future embedder assembling AppConfig in Python).
        # Enforced here instead, on the dataclass itself, so it holds no
        # matter how this config object came to exist -- frozen dataclasses
        # still allow this one exception via object.__setattr__, exactly
        # the pattern __post_init__ exists for.
        if "terminal-mcp" not in self.protected_sessions:
            object.__setattr__(self, "protected_sessions", (*self.protected_sessions, "terminal-mcp"))


@dataclass(frozen=True)
class AskChatGptConfig:
    """ask_chatgpt bridge operational parameters (docs/ask-chatgpt-
    bridge.md, Phase A) -- everything EXCEPT the on/off decision, which is
    `permissions.ask_chatgpt` alone (see that field's own docstring for
    why this dataclass deliberately has no second `enabled` field).

    bridge_turn_ttl_seconds: fixed TTL from claim time (same fixed-TTL-
    no-renewal-thread posture as lease.py's DEFAULT_LEASE_TTL_SECONDS) --
    a claimed bridge_turns row still non-terminal past this age is swept
    to CANCELLED (reason=CAPABILITY_EXPIRED) by BridgeService.
    sweep_expired(), never left claimed forever.

    default_mode/default_model/default_effort: resolved for an ask_chatgpt
    call that omits the corresponding field -- NEVER inferred from the
    prompt itself (docs/ask-chatgpt-bridge.md §2's "no silent fallback").
    allowed_modes/allowed_models/allowed_efforts: when non-empty, an
    explicitly-*requested* value outside this set is FAILED
    (MODE_NOT_AVAILABLE/MODEL_NOT_AVAILABLE/EFFORT_NOT_AVAILABLE) rather
    than silently substituted -- empty (the default) means "any value is
    accepted as given," since this project has no way yet to know what a
    real deployment's actual choices are (Phase D's problem).

    round_trip_allowed_tools: frozen onto each bridge_turns row at claim
    time (§6/§7) -- the tool-round-trip allowlist a Phase E broker would
    enforce. Empty by default: no round-trip tool capability exists at
    all until an operator both enables this feature AND explicitly names
    tools here. "terminal_send_keys" can NEVER appear in the effective
    set regardless of config -- enforced below, in code, not just in this
    docstring, exactly like allow_send_keys existing specifically so raw
    key-injection can be independently disabled; a capability born from a
    browser-driven, third-party-hosted conversation is the caller this
    project should trust LEAST with raw keys.

    max_concurrent_turns: bounded concurrency (§11) -- a genuinely NEW
    claim (never a same-idempotency_key replay, which always proceeds
    immediately regardless of this bound) waits, polling, for a free slot
    until timeout_seconds elapses, then fails QUEUE_TIMEOUT rather than
    exceeding the bound or dropping the request silently. Conservative
    default (1): this project has no evidence yet for what any given host
    can actually sustain (docs/ask-chatgpt-bridge.md §2 explicitly
    declines to copy codex-chatgpt-web's "five" without such evidence).

    min_timeout_seconds/max_timeout_seconds: bounds a caller's requested
    timeout_seconds must fall within -- same "never let a caller-supplied
    number be unboundedly small or large" posture as
    session_lifecycle.create_ready_timeout_seconds' own [0.5, 60] clamp."""
    bridge_turn_ttl_seconds: float = 300.0
    default_mode: str | None = None
    default_model: str | None = None
    default_effort: str | None = None
    allowed_modes: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    allowed_efforts: tuple[str, ...] = ()
    round_trip_allowed_tools: tuple[str, ...] = ()
    max_concurrent_turns: int = 1
    min_timeout_seconds: float = 5.0
    max_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        # See this class's own docstring on round_trip_allowed_tools --
        # this is the "in code, not just documented" half of that promise.
        # Frozen dataclass: object.__setattr__ is the sanctioned exception,
        # same pattern as SessionLifecycleConfig.__post_init__ above.
        if "terminal_send_keys" in self.round_trip_allowed_tools:
            object.__setattr__(
                self, "round_trip_allowed_tools",
                tuple(tool for tool in self.round_trip_allowed_tools if tool != "terminal_send_keys"),
            )


@dataclass(frozen=True)
class MaintenanceConfig:
    # P1 hardening item #9: periodic retention pruning (audit.db's
    # input_audit/idempotent_sends, supervisor.db's supervisor_actions --
    # supervisor_events already has its own event_retention, pruned every
    # v1 poll cycle) and WAL checkpointing, on a fixed background
    # interval independent of whether Supervisor Loop v1 is enabled (this
    # is baseline database hygiene every deployment needs -- audit.db
    # accumulates from any terminal_send_text/_keys call regardless).
    # See maintenance.py.
    interval_seconds: int = 1800
    audit_retention: int = 20_000
    action_retention: int = 5_000
    idempotency_key_retention_days: int = 30


@dataclass(frozen=True)
class AppConfig:
    permissions: PermissionsConfig
    allowed_session_patterns: tuple[str, ...]
    max_capture_lines: int = 2000
    default_tail_lines: int = 200
    input_policy: InputPolicyConfig = InputPolicyConfig()
    supervisor: SupervisorConfig = SupervisorConfig()
    dashboard: DashboardConfig = DashboardConfig()
    maintenance: MaintenanceConfig = MaintenanceConfig()
    session_lifecycle: SessionLifecycleConfig = SessionLifecycleConfig()
    ask_chatgpt: AskChatGptConfig = AskChatGptConfig()
    # Loop-protection metadata schema (see docs/prompt-submission.md, P11):
    # terminal_send_text/_granted accept optional origin/trace_id/parent_
    # turn_id/depth kwargs (all unused by every current caller -- MCP tools,
    # dashboard, Supervisor v2 -- so this changes no existing behavior).
    # This is the one number actually enforced today: a caller that DOES
    # pass depth > this value is refused (AGENT_BRIDGE_DEPTH_EXCEEDED),
    # fail-closed, before anything is sent. Sized for one bridge hop (e.g.
    # a future ChatGPT-Web-adapter turn re-entering a Codex/Claude session)
    # without allowing an unbounded agent-to-agent forwarding chain.
    max_agent_bridge_depth: int = 2


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
    max_agent_bridge_depth = int(raw.get("max_agent_bridge_depth", 2))
    if max_agent_bridge_depth < 0:
        raise ValueError("max_agent_bridge_depth must be at least 0")

    return AppConfig(
        permissions=PermissionsConfig(
            terminal_read=bool(permissions.get("terminal_read", True)),
            terminal_input=bool(permissions.get("terminal_input", False)),
            allow_send_keys=bool(permissions.get("allow_send_keys", True)),
            ask_chatgpt=bool(permissions.get("ask_chatgpt", False)),
        ),
        max_agent_bridge_depth=max_agent_bridge_depth,
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
        maintenance=_load_maintenance_config(raw.get("maintenance", {})),
        session_lifecycle=_load_session_lifecycle_config(raw.get("session_lifecycle", {})),
        ask_chatgpt=_load_ask_chatgpt_config(raw.get("ask_chatgpt", {})),
    )


def _load_maintenance_config(maintenance_raw: object) -> MaintenanceConfig:
    if not isinstance(maintenance_raw, dict):
        maintenance_raw = {}
    interval = int(maintenance_raw.get("interval_seconds", MaintenanceConfig.interval_seconds))
    audit_retention = int(maintenance_raw.get("audit_retention", MaintenanceConfig.audit_retention))
    action_retention = int(maintenance_raw.get("action_retention", MaintenanceConfig.action_retention))
    idempotency_days = int(maintenance_raw.get(
        "idempotency_key_retention_days", MaintenanceConfig.idempotency_key_retention_days,
    ))
    if interval < 60:
        raise ValueError("maintenance.interval_seconds must be at least 60")
    if audit_retention < 1:
        raise ValueError("maintenance.audit_retention must be at least 1")
    if action_retention < 1:
        raise ValueError("maintenance.action_retention must be at least 1")
    if idempotency_days < 1:
        raise ValueError("maintenance.idempotency_key_retention_days must be at least 1")
    return MaintenanceConfig(
        interval_seconds=interval, audit_retention=audit_retention,
        action_retention=action_retention, idempotency_key_retention_days=idempotency_days,
    )


_SAFE_LAUNCH_TOKEN = re.compile(r"^[A-Za-z0-9_./-]{1,128}$")


def _load_session_lifecycle_config(raw: object) -> SessionLifecycleConfig:
    if not isinstance(raw, dict):
        raw = {}
    enabled = bool(raw.get("enabled", False))
    roots = raw.get("allowed_cwd_roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise ValueError("session_lifecycle.allowed_cwd_roots must be a list of strings")
    protected = raw.get("protected_sessions", ["terminal-mcp"])
    if not isinstance(protected, list) or not all(isinstance(p, str) and p for p in protected):
        raise ValueError("session_lifecycle.protected_sessions must be a list of strings")
    # "terminal-mcp" (this server's own controlling session) is always
    # protected -- an operator's config can only ADD names, never remove
    # this one, so a misconfigured/emptied list can never make this
    # project's own session deletable from its own dashboard/MCP surface.
    protected_set = tuple(dict.fromkeys([*protected, "terminal-mcp"]))
    launch_raw = raw.get("launch_commands", {"claude": "claude", "codex": "codex"})
    if not isinstance(launch_raw, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) and v for k, v in launch_raw.items()
    ):
        raise ValueError("session_lifecycle.launch_commands must be a mapping of agent_type -> command")
    for agent_type, command in launch_raw.items():
        if not _SAFE_LAUNCH_TOKEN.fullmatch(command):
            raise ValueError(f"session_lifecycle.launch_commands[{agent_type!r}] is not a safe launcher token")
    timeout = float(raw.get("create_ready_timeout_seconds", SessionLifecycleConfig.create_ready_timeout_seconds))
    if not 0.5 <= timeout <= 60:
        raise ValueError("session_lifecycle.create_ready_timeout_seconds must be between 0.5 and 60")
    grant_mode = raw.get("default_grant_mode", SessionLifecycleConfig.default_grant_mode)
    if grant_mode not in ("none", "read", "read_send"):
        raise ValueError("session_lifecycle.default_grant_mode must be one of: none, read, read_send")
    return SessionLifecycleConfig(
        enabled=enabled, allowed_cwd_roots=tuple(roots), protected_sessions=protected_set,
        launch_commands=tuple(sorted(launch_raw.items())), create_ready_timeout_seconds=timeout,
        default_grant_mode=grant_mode,
    )


def _load_ask_chatgpt_config(raw: object) -> AskChatGptConfig:
    if not isinstance(raw, dict):
        raw = {}

    def optional_string(name: str) -> str | None:
        value = raw.get(name)
        if value is not None and not (isinstance(value, str) and value.strip()):
            raise ValueError(f"ask_chatgpt.{name} must be a non-empty string")
        return value

    def string_tuple(name: str) -> tuple[str, ...]:
        value = raw.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"ask_chatgpt.{name} must be a list of strings")
        return tuple(value)

    ttl = float(raw.get("bridge_turn_ttl_seconds", AskChatGptConfig.bridge_turn_ttl_seconds))
    if not 5 <= ttl <= 3600:
        raise ValueError("ask_chatgpt.bridge_turn_ttl_seconds must be between 5 and 3600")
    max_concurrent = int(raw.get("max_concurrent_turns", AskChatGptConfig.max_concurrent_turns))
    if max_concurrent < 1:
        raise ValueError("ask_chatgpt.max_concurrent_turns must be at least 1")
    min_timeout = float(raw.get("min_timeout_seconds", AskChatGptConfig.min_timeout_seconds))
    max_timeout = float(raw.get("max_timeout_seconds", AskChatGptConfig.max_timeout_seconds))
    if not 0 < min_timeout <= max_timeout:
        raise ValueError("ask_chatgpt.min_timeout_seconds must be > 0 and <= max_timeout_seconds")
    return AskChatGptConfig(
        bridge_turn_ttl_seconds=ttl,
        default_mode=optional_string("default_mode"),
        default_model=optional_string("default_model"),
        default_effort=optional_string("default_effort"),
        allowed_modes=string_tuple("allowed_modes"),
        allowed_models=string_tuple("allowed_models"),
        allowed_efforts=string_tuple("allowed_efforts"),
        round_trip_allowed_tools=string_tuple("round_trip_allowed_tools"),
        max_concurrent_turns=max_concurrent,
        min_timeout_seconds=min_timeout,
        max_timeout_seconds=max_timeout,
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
        web_terminal_enabled=bool(dashboard_raw.get("web_terminal_enabled", False)),
    )
