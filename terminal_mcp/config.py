from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .node_models import NodeHeartbeatThresholds, OverloadThresholds


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
class QueueConfig:
    """Supervisor Queue v2's own AUTO-DISPATCH background loop
    (queue_loop.py) -- a SEPARATE, independent global kill switch from
    supervisor.enabled above (this drives QueueEngine.tick() in a loop,
    not SupervisorService.run_once()). Disabled by default everywhere,
    same posture as every other autonomous-background-thread config in
    this project: an operator must explicitly opt in. Even once enabled
    here, NO session is actually touched until its own lane also sets
    queue_lanes.auto_dispatch_enabled (a separate, PER-SESSION opt-in
    this config has no effect on) -- see queue_loop.py's own module
    docstring for the full two-gate safety reasoning."""
    enabled: bool = False
    poll_interval_seconds: float = 3.0
    # Bus-driven reaction (queue_event_drain.py), inside the SAME loop -- never
    # a second scheduler. Its own flag rather than riding on `enabled` above:
    # turning on auto-dispatch is a decision about lanes, while turning on the
    # drain is a decision about consuming the event bus, and an operator should
    # be able to make the first without silently also making the second.
    drain_enabled: bool = False
    drain_batch_size: int = 25


@dataclass(frozen=True)
class SubmitProfile:
    """Bounded, evidence-gated Enter submission policy for one agent."""
    max_enter_attempts: int = 1
    enter_interval_ms: int = 180
    verify_after_each_enter: bool = True
    fixed_enter_count: int = 0


@dataclass(frozen=True)
class SubmitConfig:
    default: SubmitProfile = SubmitProfile()
    # Programmatic/default AppConfig preserves the historical single-Enter
    # behavior; the production config.yaml opts Codex into the verified
    # three-attempt profile explicitly.
    codex: SubmitProfile = SubmitProfile()
    # Claude and unknown agents are deliberately single-submit: one injected
    # prompt and at most the initial Enter, never watchdog Enter retries.
    claude: SubmitProfile = SubmitProfile()


@dataclass(frozen=True)
class AutoRecoveryConfig:
    """Auto Recovery (2026-09-07, task: "Auto Recovery cho session sau
    reboot/crash/node-agent restart") -- see recovery_engine.py's own
    module docstring for the full design. Disabled by default (`enabled
    = False`), same posture as every other autonomous-background-thread
    config in this project (queue/supervisor/integration_loop) --
    UNLIKE ai_usage's own default-on read, this one takes real
    autonomous ACTION (spawns a real new process) and must be an
    explicit operator opt-in. A per-session override still exists
    (session_registry.py's own `auto_recovery_enabled` column) even
    once this global switch is on.

    max_attempts: bounded retry -- a session whose own recovery_attempts
    (session_registry.py) already reached this is refused (BLOCKED,
    never a silent infinite retry loop) until a human intervenes
    (terminal_recover_session's own `force=True` explicitly resets and
    retries anyway, an explicit override).
    lock_ttl_seconds: the recovery lock's own TTL (reuses lease.py's
    already-real, already-tested PaneLeaseStore -- never a second lock
    table) -- sized comfortably above one real registry_reopen attempt's
    own worst-case duration (RESUME_VERIFY_TIMEOUT_SECONDS=15s plus
    create/launch overhead, core.py) so a genuinely in-flight attempt is
    never falsely reclaimed, short enough that a crashed attempt's lock
    is not stuck for long.
    reconcile_poll_seconds: the background reconciliation loop's own
    fallback poll interval (mirrors queue_loop.py/integration_loop.py's
    own pattern) -- the REAL trigger is a node ONLINE transition
    (node_registry.py's own sync_status_transitions, already real), this
    is only the safety-net poll for whatever that misses (e.g. a node
    that was already online when this loop started)."""
    enabled: bool = False
    # Staleness bound. A session that went MISSING long ago is not something
    # today's reconcile pass should resurrect: this registry accumulates a row
    # for every session that ever existed, including disposable ones from test
    # runs that ended normally. Measured live on this deployment before this
    # existed: 207 MISSING records, 136 of them "recoverable" -- enabling
    # auto-recovery globally would have spawned 136 real processes, nearly all
    # of them long-dead test sessions.
    #
    # Age is measured from last_seen_at (when the session was last observed
    # ALIVE). 0 disables the bound. An explicit human force=True always
    # bypasses it -- reopening something old on purpose stays possible.
    #
    # One hour, not a day: a node coming back from a reboot rejoins within
    # minutes, so anything a legitimate recovery needs to catch is very
    # recent. Measured against this deployment's real registry, the two
    # windows are worlds apart -- a 24h bound left 135 of 136 "recoverable"
    # records eligible, a 1h bound leaves 2, and the age histogram has an
    # actual gap there (nothing between ~8 minutes and ~10 hours). The long
    # tail is disposable test sessions that ended normally and must never be
    # resurrected.
    max_missing_age_seconds: float = 3600.0
    # Only auto-recreate sessions this controller was ASKED to create.
    #
    # A session it merely observed -- someone's own `tmux new-session`, a test
    # fixture, an editor terminal -- has no recorded launch command, so
    # recreating it means guessing at another process's argv and calling the
    # guess a recovery. Measured on this fleet: of 264 records, 29 carry a
    # launch command, and all 9 that auto-recovery would otherwise have
    # respawned carried none. With this on, the blast radius of enabling
    # auto-recovery here went from 9 junk processes to 0.
    #
    # force=True always bypasses it: an operator reopening something
    # explicitly is making that judgement themselves.
    managed_sessions_only: bool = True
    max_attempts: int = 3
    lock_ttl_seconds: float = 60.0
    reconcile_poll_seconds: float = 30.0


@dataclass(frozen=True)
class SubmitWatchdogConfig:
    """Codex-only verified submit timing; retries are Enter-only."""
    enabled: bool = True
    poll_interval_seconds: float = 0.4
    timeout_seconds: float = 5.0
    max_enter_attempts: int = 3
    sweeper_interval_seconds: float = 1.5
    retry_agent_types: tuple[str, ...] = ("codex",)


@dataclass(frozen=True)
class IntegrationLoopConfig:
    """3-role pipeline's own event-driven WAIT/wake background loop
    (integration_loop.py) -- a SEPARATE, independent global kill switch
    from queue.enabled above (this drives IntegrationEngine.tick() in a
    loop, not QueueEngine.tick()). Disabled by default, same posture as
    every other autonomous-background-thread config in this project.
    Unlike queue.enabled, there is no separate PER-PROJECT opt-in column
    needed here -- a project must already be explicitly configured via
    terminal_integration_configure (real setup, not a bare flag) before
    any Handoff can even be published for it, and an already-real
    per-project `paused` flag (integration_service.py's pause/resume)
    is the reuse-not-rebuild equivalent of queue_lanes.auto_dispatch_
    enabled's per-lane gate -- see integration_loop.py's own module
    docstring for the full two-gate reasoning."""
    enabled: bool = False
    fallback_poll_seconds: float = 5.0


@dataclass(frozen=True)
class AiUsageConfig:
    """Read-only integration with the separate 'AI Usage Monitor' local
    service (a SEPARATE project -- its own repo, its own systemd user
    service, ~/.local/share/ai-usage-monitor -- see docs/REQUIREMENTS.md's
    own "AI Usage" section for the full audit). This project makes ZERO
    changes to that service and duplicates NONE of its usage-collection
    logic -- it only reads that service's own already-computed
    `/api/usage` JSON over a plain local HTTP GET. Defaults ON (unlike
    every autonomous-background-thread config in this project, which
    defaults OFF): there is no autonomous ACTION to gate here, only a
    read, and a down/missing service degrades cleanly (see ai_usage_
    service.py) rather than breaking anything. `base_url` is deliberately
    a plain override (not multi-node) -- the AI Usage Monitor reflects
    THIS machine's own local CLI credential files, not a fleet-wide
    concept; when this project's own control plane later moves to a VPS
    (see the Internet/VPS migration roadmap), `base_url` is the one
    value an operator repoints, e.g. to a tunneled/loopback-forwarded
    address on the new host -- never made public on the internet."""
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8787"
    timeout_seconds: float = 2.0
    cache_ttl_seconds: float = 20.0
    warning_threshold_percent: float = 70.0
    critical_threshold_percent: float = 90.0


# The image types notes_service.py can both content-sniff and serve.
# Kept as a literal here rather than imported from notes_service so
# config.py's import graph stays as narrow as it is today;
# tests/test_notes_config.py asserts the two lists never drift apart.
_NOTES_SERVABLE_MIME_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


@dataclass(frozen=True)
class NotesConfig:
    """Notes / Ideas store (notes_store.py + notes_service.py) -- the
    cross-project kho ghi chú ChatGPT writes into when the user says "lưu
    lại". Defaults ON, same reasoning as ai_usage above: there is no
    autonomous background ACTION to gate here, only a store that answers
    reads and writes when something asks it to. Nothing runs, nothing is
    captured, and no file is written until a caller explicitly creates a
    note.

    `attachments_dir` empty means "beside the notes DB under the same
    state root" (notes_service.default_attachments_dir) -- so an
    XDG_STATE_HOME override relocates the DB and its images together,
    which is what makes an isolated test run or a second instance
    actually isolated.

    `attachment_source_roots` is empty BY DEFAULT and that is the whole
    point: with no root configured, the source_path attachment transport
    is refused outright (ATTACHMENT_SOURCE_DISABLED) and the only way to
    attach bytes is data_base64 or the dashboard's own upload form.
    Adding a root is an operator granting this store permission to read
    files from exactly that subtree -- never an implicit licence to read
    anywhere the server process happens to have access to."""
    enabled: bool = True
    # Application-layer authentication for the Notes HTTP surface (the
    # page, its JSON API, and attachment serving). ON by default, and
    # unlike every other gate in this file that default is a TIGHTENING of
    # what the route would otherwise do, not a feature switch: notes hold
    # whatever the operator chose to keep, so "reachable the socket is
    # reachable" is the wrong posture for them even though it is the
    # historical one for /dashboard/*.
    #
    # Satisfied by EITHER of the two identities this project already has
    # (no third mechanism is introduced -- see dashboard._notes_auth_guard):
    # a webauth session cookie (webauth.py, the /login path) or a verified
    # Cloudflare Access assertion (cf_access.py, when
    # dashboard.cloudflare_access_team_domain/audience are configured).
    # Edge-only Access is explicitly NOT enough: cloudflared connects to
    # this process over loopback, so tunnel traffic is indistinguishable
    # from local traffic once it arrives -- exactly the gap cf_access.py's
    # own docstring warns about.
    #
    # Set false only for a genuinely single-user loopback-only box where
    # logging in is pure friction; it returns these routes to the same
    # unauthenticated posture the rest of /dashboard/* still has.
    require_auth: bool = True
    attachments_dir: str = ""
    max_attachment_bytes: int = 10 * 1024 * 1024
    allowed_mime_types: tuple[str, ...] = ("image/png", "image/jpeg", "image/webp", "image/gif")
    attachment_source_roots: tuple[str, ...] = ()


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
class SessionKnowledgeConfig:
    """Session Knowledge Store (session_knowledge.py) -- captures every
    session's REAL output for search/timeline/recovery. Disabled by
    default, same posture as session_lifecycle/terminal_input above, and
    for an even more concrete reason than "not yet reviewed": capture has
    a REAL, externally-visible side effect on the actual tmux pane
    (starting `pipe-pane`) for every session core.py's own reconcile pass
    ever sees, including ones this config had nothing to do with creating
    -- a real, live incident during this feature's own development (every
    OTHER real session sharing this host's tmux server briefly had pipe-
    pane silently turned on by an unrelated test run). An existing
    deployment's config.yaml keeps today's exact behavior (no capture at
    all) until an operator explicitly opts in here -- and even then,
    _capture_session_knowledge (core.py) only ever touches a session
    whose cwd resolves inside session_lifecycle.allowed_cwd_roots, never
    an arbitrary other session on the same host, as a second, independent
    safety boundary."""
    enabled: bool = False


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
    starts the session's plain default shell, no entry needed.

    resume_capable_agent_types: which agent_type values support this
    project's own conversation-continuity mechanism (Phase 0 node-agent
    restart-safety follow-up, 2026-09-07: `--session-id <uuid>` at
    creation, `--resume <uuid>` on registry-reopen) -- a real, verified-
    only allowlist, never a guess. Live-verified via `claude --help` on
    a real Windows node (dell-5530, Claude Code 2.1.258): both flags
    exist and behave as documented. "codex" is deliberately NOT in the
    default -- its CLI was never installed/verified on any node this
    project has live access to; adding it here without first verifying
    its own `--help`/behavior would be exactly the kind of unverified
    assumption this project's own standing rules forbid. Any agent_type
    NOT in this set still creates/reopens normally -- it just never gets
    `--session-id`/`--resume` appended, and `registry_reopen` for it
    always does an honest metadata-only recreate (never claims
    conversation continuity it can't verify)."""
    enabled: bool = False
    allowed_cwd_roots: tuple[str, ...] = ()
    protected_sessions: tuple[str, ...] = ("terminal-mcp",)
    launch_commands: tuple[tuple[str, str], ...] = (("claude", "claude"), ("codex", "codex"))
    resume_capable_agent_types: tuple[str, ...] = ("claude",)
    # Codex CLI's current, audited YOLO flag. Shared by create and
    # registry-reopen; never assembled by individual callers.
    codex_yolo: bool = True
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
class WorkConfig:
    # OFF by default, and that differs from FleetSyncConfig on purpose: the
    # fleet loop only re-projects local data, while this one can cause an
    # agent to be handed work on a real session. A capability that acts on
    # its own starts disabled. See work_loop.py.
    enabled: bool = False
    interval_seconds: int = 20
    max_runs_per_tick: int = 25
    # The privileged action: turning a `-work` lane's auto-dispatch on. A
    # deployment can keep the coordinator's bookkeeping and still leave the
    # actual enabling to a human.
    auto_enable_dispatch: bool = True
    lease_seconds: int = 900
    max_revisions: int = 3
    no_progress_limit: int = 3


@dataclass(frozen=True)
class RepoReadConfig:
    """Read-only repository access for the `repo_*` MCP tools (see
    repo_read.py, repo_service.py).

    ON by default, unlike `session_lifecycle`/`work` -- and the difference
    is the point rather than an oversight. Those gates guard capabilities
    that CREATE something (a real process, an auto-dispatched prompt);
    this one only reads, has no write primitive to lose control of, and
    would be useless to an operator who had to edit config before an
    agent could look at a file. The real boundary here is
    `allowed_roots`, not the on/off flag.

    `allowed_roots` empty (the default) reuses
    `session_lifecycle.allowed_cwd_roots` -- the allowlist this
    deployment has ALREADY curated for "paths a session may live in",
    which is very nearly the set of repos worth reading -- and falls back
    to the server's own home directory when that is unset too. Never to
    "/" and never to an unbounded root; see repo_read.RepoReadPolicy.

    `extra_secret_globs` is additive only. There is deliberately no
    config key that REMOVES a built-in secret glob, so no config edit can
    reopen a path repo_read.py closed."""

    enabled: bool = True
    allowed_roots: tuple[str, ...] = ()
    max_bytes: int = 256_000
    max_lines: int = 2_000
    max_results: int = 200
    max_tree_entries: int = 2_000
    max_tree_depth: int = 6
    max_log_entries: int = 200
    max_diff_bytes: int = 400_000
    timeout_seconds: float = 15.0
    extra_secret_globs: tuple[str, ...] = ()

    def to_policy(self, fallback_roots: tuple[str, ...] = ()) -> Any:
        """Build the immutable RepoReadPolicy the engine actually
        enforces. Imported lazily so config.py keeps no import-time
        dependency on a feature module -- the same layering every other
        section here holds to."""
        from .repo_read import RepoReadPolicy

        return RepoReadPolicy(
            enabled=self.enabled,
            allowed_roots=tuple(self.allowed_roots or fallback_roots),
            max_bytes=self.max_bytes, max_lines=self.max_lines,
            max_results=self.max_results, max_tree_entries=self.max_tree_entries,
            max_tree_depth=self.max_tree_depth, max_log_entries=self.max_log_entries,
            max_diff_bytes=self.max_diff_bytes, timeout_seconds=self.timeout_seconds,
            extra_secret_globs=tuple(self.extra_secret_globs))


@dataclass(frozen=True)
class FleetSyncConfig:
    # ON by default, like MaintenanceConfig and for the same reason: this is
    # not an optional feature, it is what keeps an already-shipped one
    # telling the truth. Without it the fleet cache decays past
    # fleet_service.STALE_AFTER_SECONDS (900) within fifteen minutes of every
    # controller start, and every auth/readiness view can only answer
    # UNKNOWN_STALE. See fleet_loop.py.
    enabled: bool = True
    # Three cycles of headroom under the 900s stale threshold, so two
    # consecutive failed cycles still leave the data inside it.
    interval_seconds: int = 300
    # Separable: a deployment whose node agents all predate /v1/fleet/* keeps
    # the local refresh (the half that makes its own dashboard correct)
    # without emitting a 404 per node per cycle.
    peer_exchange_enabled: bool = True


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
class RemoteNodeConfig:
    """One operator-declared remote node (task item 15's "cấu hình đăng
    ký" -- the config-driven half of remote registration; the node's own
    terminal-node-agent process, task item 2, is the other half). Never
    holds the shared secret itself -- `token_env` names the environment
    variable this process reads it from at startup (same posture as every
    other bearer-token secret in this project), so config.yaml itself
    stays safe to read/back up/commit without leaking a credential."""
    node_id: str
    display_name: str
    hostname: str
    endpoint: str
    token_env: str
    max_sessions: int | None = None
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class DiscoveryConfig:
    """LAN device discovery (lan_discovery.py) -- Nodes page "Discover
    devices" feature. Every numeric here is a safety knob (task's own
    explicit "rate limit/concurrency cap/timeout"), never something a
    scan can exceed regardless of how many/large the local NICs' own
    subnets are -- see lan_discovery.local_ipv4_subnets/DiscoveryService
    for how each one is actually enforced."""
    enabled: bool = True
    agent_port: int = 8790
    concurrency: int = 32
    host_timeout_seconds: float = 0.35
    max_hosts_per_scan: int = 512
    overall_timeout_seconds: float = 45.0
    cooldown_seconds: float = 5.0


@dataclass(frozen=True)
class RemoteConnectConfig:
    """Remote node connect/bootstrap (remote_connect.py) -- SSH-based
    onboarding for a node found by LAN discovery, a manually-typed
    hostname/IP, or a Cloudflare Access SSH hostname.
    allow_public_manual_add: off by default (task's own explicit SSRF
    requirement) -- an operator deliberately choosing to connect to a
    public-IP LAN-SSH target must opt in here first; a Cloudflare Access
    hostname is never subject to this check at all (see
    remote_connect.validate_cloudflare_hostname's own docstring for
    why)."""
    allow_public_manual_add: bool = False
    ssh_connect_timeout_seconds: float = 10.0
    bootstrap_timeout_seconds: float = 60.0


@dataclass(frozen=True)
class TailscaleOnboardConfig:
    """Tailscale as a node's PRIMARY transport. `enabled` only expresses
    the operator's intent -- whether a given onboarding actually uses
    Tailscale is decided at enrollment time by whether this controller
    itself is on a tailnet (node_onboarding.detect_controller_tailscale),
    so a deployment with no tailnet silently falls back to LAN + rescue
    instead of generating an installer that cannot possibly work.

    auth_key_env names the ENVIRONMENT VARIABLE holding a Tailscale auth
    key, never the key itself -- same posture as RemoteNodeConfig.
    token_env. The key is handed to a node exactly once, inside the
    authenticated enrollment-exchange response, and never written into
    the downloadable script or any log line. Leave it unset and the
    installer prints an interactive `tailscale up` instruction instead:
    one extra manual step, zero reusable secret in flight."""
    enabled: bool = True
    auth_key_env: str = "TERMINAL_MCP_TAILSCALE_AUTH_KEY"
    tags: tuple[str, ...] = ()
    unattended: bool = True
    login_server: str = ""  # Headscale/self-hosted coordination server, if any


@dataclass(frozen=True)
class RescueTunnelConfig:
    """The reverse-SSH rescue path (rescue_gateway.py). OFF by default and
    deliberately so: turning it on without a real gateway would produce
    installers that fail halfway. With `enabled: false` the whole feature
    reports `configured=false, reason=rescue_disabled` everywhere it is
    surfaced, and onboarding still completes over the primary path.

    gateway_host_key is one known_hosts line -- a PUBLIC key, safe in
    config.yaml and safe to ship to a node. There is deliberately no
    field here for a PRIVATE key: the node generates its own keypair
    locally and only its public half ever reaches this controller."""
    enabled: bool = False
    gateway_host: str = ""
    gateway_port: int = 22
    gateway_user: str = ""
    gateway_host_key: str = ""
    port_range_start: int = 22000
    port_range_end: int = 22999
    keepalive_interval_seconds: int = 30
    keepalive_count_max: int = 3
    retry_seconds: int = 15


@dataclass(frozen=True)
class OnboardingConfig:
    """Self-service node onboarding (Nodes -> + Add Node -> Windows).

    controller_url is what the generated installer calls back to. Empty
    (the default) means "derive it from the request that asked for the
    script", which is right for every normal deployment and wrong only
    behind a proxy that rewrites Host -- set it explicitly there.

    controller_ssh_public_key(_file) is the PUBLIC key the installer
    installs into the new node's authorized_keys so this controller can
    SSH in. A public key: safe in config, safe in the payload, useless to
    anyone who intercepts it."""
    enabled: bool = True
    enrollment_ttl_seconds: int = 900
    controller_url: str = ""
    controller_ssh_public_key: str = ""
    controller_ssh_public_key_file: str = ""
    agent_port: int = 8790
    heartbeat_interval_seconds: int = 30
    # Firewall: which source range the installer opens inbound TCP 22 to
    # on the node. Default is Tailscale's CGNAT range -- NOT "any", so a
    # laptop that later joins a cafe network is not serving sshd to it.
    ssh_firewall_cidrs: tuple[str, ...] = ("100.64.0.0/10",)
    tailscale: TailscaleOnboardConfig = TailscaleOnboardConfig()
    rescue: RescueTunnelConfig = RescueTunnelConfig()


@dataclass(frozen=True)
class NodesConfig:
    """Multi-node session management (controller.py/node_registry.py/
    scheduler.py). overload_thresholds/heartbeat_thresholds are the exact
    same dataclasses NodeRegistry itself uses -- this is purely where an
    operator overrides their defaults, never a second copy of the logic.
    remote_nodes lists nodes to auto-register at startup (server_http.py);
    an empty tuple (the default) is exactly today's single-local-node
    deployment, unchanged."""
    overload_thresholds: OverloadThresholds = OverloadThresholds()
    heartbeat_thresholds: NodeHeartbeatThresholds = NodeHeartbeatThresholds()
    remote_nodes: tuple[RemoteNodeConfig, ...] = ()
    discovery: DiscoveryConfig = DiscoveryConfig()
    remote_connect: RemoteConnectConfig = RemoteConnectConfig()
    onboarding: OnboardingConfig = OnboardingConfig()


@dataclass(frozen=True)
class SessionAccessConfig:
    """What a session may be read from / sent to, when no explicit grant says
    otherwise.

    This REPLACES the old session-name whitelist as the enforcement input.
    Access is now decided by explicit, user-issued grants (grants.db) plus
    this default policy -- never by whether a session's NAME happens to match
    a glob in config.yaml. The whitelist produced a genuinely contradictory
    state that was reported as a bug: a session could list `allowed=false`
    (name not in the glob list) while `effective_read`/`effective_input` were
    both true (an explicit grant said so), and the two fields meant different
    things that looked like they should agree.

    Defaults are OPEN: a session the owner just created, or that was just
    discovered, is readable and writable immediately, with no grant step.
    Deliberate, and an explicit product decision -- the closed default that
    preceded it produced exactly one recurring outcome, a brand-new session
    stuck behind a permission nobody had granted yet, which is worse than
    useless for a session manager.

    ABSENCE OF A RECORD MEANS ALLOW, not deny. A grant row now exists only
    because someone deliberately CHANGED something, and its most useful shape
    is an explicit revoke -- an optional lock, never a prerequisite.

    This does not make the deployment open. The boundaries that actually gate
    access are untouched: account/webauth/Cloudflare Access on the dashboard,
    node bearer tokens between controller and agents, the sensitive-name floor
    (root/ssh/password/secret/database, refused whatever any policy says),
    input_policy.denied_session_patterns, and the global
    permissions.terminal_read/terminal_input switches. What is gone is the
    per-session paperwork, not the perimeter.

    `allowed_session_patterns`/`input_policy.allowed_session_patterns` are
    still PARSED, but only as a one-time migration source -- see
    TerminalService.migrate_whitelist_to_grants. They no longer authorize
    anything by themselves.
    """
    default_read: bool = True
    default_input: bool = True
    # One-time conversion of the old name whitelist into real grants, so a
    # deployment upgrading to this does not silently lose access to every
    # session it had whitelisted. Idempotent: it only ever ADDS a grant for a
    # session that has none.
    migrate_whitelist_on_start: bool = True


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
    fleet_sync: FleetSyncConfig = FleetSyncConfig()
    repo_read: RepoReadConfig = RepoReadConfig()
    work: WorkConfig = WorkConfig()
    session_lifecycle: SessionLifecycleConfig = SessionLifecycleConfig()
    session_knowledge: SessionKnowledgeConfig = SessionKnowledgeConfig()
    session_access: SessionAccessConfig = SessionAccessConfig()
    ask_chatgpt: AskChatGptConfig = AskChatGptConfig()
    nodes: NodesConfig = NodesConfig()
    queue: QueueConfig = QueueConfig()
    submit: SubmitConfig = SubmitConfig()
    integration_loop: IntegrationLoopConfig = IntegrationLoopConfig()
    submit_watchdog: SubmitWatchdogConfig = SubmitWatchdogConfig()
    ai_usage: AiUsageConfig = AiUsageConfig()
    notes: NotesConfig = NotesConfig()
    auto_recovery: AutoRecoveryConfig = AutoRecoveryConfig()
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
    submit_raw = raw.get("submit", {})
    if not isinstance(submit_raw, dict):
        raise ValueError("submit must be a mapping")

    def load_submit_profile(raw_profile: object, default: SubmitProfile) -> SubmitProfile:
        if not isinstance(raw_profile, dict):
            raw_profile = {}
        max_attempts = int(raw_profile.get("max_enter_attempts", default.max_enter_attempts))
        interval_ms = int(raw_profile.get("enter_interval_ms", default.enter_interval_ms))
        fixed_count = int(raw_profile.get("fixed_enter_count", default.fixed_enter_count))
        if not 1 <= max_attempts <= 5:
            raise ValueError("submit.*.max_enter_attempts must be between 1 and 5")
        if not 50 <= interval_ms <= 1000:
            raise ValueError("submit.*.enter_interval_ms must be between 50 and 1000")
        if not 0 <= fixed_count <= 5:
            raise ValueError("submit.*.fixed_enter_count must be between 0 and 5")
        return SubmitProfile(max_enter_attempts=max_attempts, enter_interval_ms=interval_ms,
                             verify_after_each_enter=bool(raw_profile.get(
                                 "verify_after_each_enter", default.verify_after_each_enter)),
                             fixed_enter_count=fixed_count)

    submit_defaults = SubmitConfig()
    submit_config = SubmitConfig(
        default=load_submit_profile(submit_raw.get("default", {}), submit_defaults.default),
        codex=load_submit_profile(submit_raw.get("codex", {}), submit_defaults.codex),
        claude=load_submit_profile(submit_raw.get("claude", {}), submit_defaults.claude),
    )
    # Environment overrides are intentionally narrow and numeric; they are
    # useful for a live node/backend diagnosis without embedding secrets or
    # prompt content in config.
    submit_env_names = (
        "TERMINAL_MCP_CODEX_SUBMIT_ENTER_MAX_ATTEMPTS",
        "TERMINAL_MCP_CODEX_SUBMIT_ENTER_INTERVAL_MS",
        "TERMINAL_MCP_CODEX_SUBMIT_VERIFY_AFTER_EACH_ENTER",
        "TERMINAL_MCP_CODEX_SUBMIT_FIXED_ENTER_COUNT",
    )
    if any(os.environ.get(name) is not None for name in submit_env_names):
        submit_config = SubmitConfig(
            default=submit_config.default,
            codex=load_submit_profile({
                "max_enter_attempts": os.environ.get("TERMINAL_MCP_CODEX_SUBMIT_ENTER_MAX_ATTEMPTS",
                                                    submit_config.codex.max_enter_attempts),
                "enter_interval_ms": os.environ.get("TERMINAL_MCP_CODEX_SUBMIT_ENTER_INTERVAL_MS",
                                                     submit_config.codex.enter_interval_ms),
                "verify_after_each_enter": os.environ.get("TERMINAL_MCP_CODEX_SUBMIT_VERIFY_AFTER_EACH_ENTER",
                                                          str(submit_config.codex.verify_after_each_enter)).lower() == "true",
                "fixed_enter_count": os.environ.get("TERMINAL_MCP_CODEX_SUBMIT_FIXED_ENTER_COUNT",
                                                    submit_config.codex.fixed_enter_count),
            }, submit_config.codex),
            claude=submit_config.claude,
        )

    def string_tuple(name: str, default: tuple[str, ...], *, allow_empty: bool = False) -> tuple[str, ...]:
        value = input_raw.get(name, list(default))
        if not isinstance(value, list) or (not value and not allow_empty) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"input_policy.{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}list of strings")
        return tuple(value)

    # May be EMPTY. This list no longer authorizes anything -- it is only the
    # migration input that converts a pre-grants deployment's whitelist into
    # real grants (see SessionAccessConfig). A deployment that has finished
    # that migration, or was never on a whitelist at all, must be able to say
    # so; requiring a non-empty list forced it to keep a dead setting alive,
    # and a node agent written against the new model simply failed to start.
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
        raise ValueError("allowed_session_patterns must be a list of non-empty strings (it may be empty)")
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

    submit_raw = raw.get("submit_watchdog", {})
    if not isinstance(submit_raw, dict):
        raise ValueError("submit_watchdog must be a mapping")
    submit_defaults = SubmitWatchdogConfig()
    # NOTE: a DIFFERENT name from the SubmitConfig built earlier in this
    # function. Both used to be called `submit_config`, so this assignment
    # silently clobbered the parsed per-agent submit profiles -- see the
    # AppConfig(...) call below, where `submit=` then received a
    # SubmitWatchdogConfig. That made the whole `submit:` config block and
    # every TERMINAL_MCP_CODEX_SUBMIT_* env override dead config.
    watchdog_config = SubmitWatchdogConfig(
        enabled=bool(submit_raw.get("enabled", submit_defaults.enabled)),
        poll_interval_seconds=float(submit_raw.get("poll_interval_seconds", submit_defaults.poll_interval_seconds)),
        timeout_seconds=float(submit_raw.get("timeout_seconds", submit_defaults.timeout_seconds)),
        max_enter_attempts=int(submit_raw.get("max_enter_attempts", submit_defaults.max_enter_attempts)),
        sweeper_interval_seconds=float(submit_raw.get("sweeper_interval_seconds", submit_defaults.sweeper_interval_seconds)),
        retry_agent_types=tuple(submit_raw.get("retry_agent_types", submit_defaults.retry_agent_types)),
    )
    if not 0.3 <= watchdog_config.poll_interval_seconds <= 0.5:
        raise ValueError("submit_watchdog.poll_interval_seconds must be between 0.3 and 0.5")
    if watchdog_config.timeout_seconds <= 0 or watchdog_config.sweeper_interval_seconds < 1:
        raise ValueError("submit_watchdog timeouts must be positive")
    if not 1 <= watchdog_config.max_enter_attempts <= 5:
        raise ValueError("submit_watchdog.max_enter_attempts must be between 1 and 5")
    if not watchdog_config.retry_agent_types or not all(isinstance(agent, str) and agent for agent in watchdog_config.retry_agent_types):
        raise ValueError("submit_watchdog.retry_agent_types must be a non-empty list of agent types")

    nodes_raw = raw.get("nodes", {})
    if not isinstance(nodes_raw, dict):
        raise ValueError("nodes must be a mapping")
    overload_raw = nodes_raw.get("overload_thresholds", {})
    if not isinstance(overload_raw, dict):
        raise ValueError("nodes.overload_thresholds must be a mapping")
    overload_defaults = OverloadThresholds()
    overload_thresholds = OverloadThresholds(
        ram_busy_percent=float(overload_raw.get("ram_busy_percent", overload_defaults.ram_busy_percent)),
        ram_overloaded_percent=float(overload_raw.get("ram_overloaded_percent", overload_defaults.ram_overloaded_percent)),
        swap_overloaded_percent=float(overload_raw.get("swap_overloaded_percent", overload_defaults.swap_overloaded_percent)),
        cpu_busy_percent=float(overload_raw.get("cpu_busy_percent", overload_defaults.cpu_busy_percent)),
        cpu_overloaded_percent=float(overload_raw.get("cpu_overloaded_percent", overload_defaults.cpu_overloaded_percent)),
        sustained_seconds=float(overload_raw.get("sustained_seconds", overload_defaults.sustained_seconds)),
        load_factor_busy=float(overload_raw.get("load_factor_busy", overload_defaults.load_factor_busy)),
        disk_free_overloaded_percent=float(overload_raw.get("disk_free_overloaded_percent", overload_defaults.disk_free_overloaded_percent)),
        smoothing_alpha=float(overload_raw.get("smoothing_alpha", overload_defaults.smoothing_alpha)),
    )
    if not 0.0 < overload_thresholds.smoothing_alpha <= 1.0:
        raise ValueError("nodes.overload_thresholds.smoothing_alpha must be between 0 (exclusive) and 1")
    if overload_thresholds.sustained_seconds < 0:
        raise ValueError("nodes.overload_thresholds.sustained_seconds must be at least 0")

    heartbeat_raw = nodes_raw.get("heartbeat", {})
    if not isinstance(heartbeat_raw, dict):
        raise ValueError("nodes.heartbeat must be a mapping")
    heartbeat_defaults = NodeHeartbeatThresholds()
    heartbeat_thresholds = NodeHeartbeatThresholds(
        degraded_after_seconds=float(heartbeat_raw.get("degraded_after_seconds", heartbeat_defaults.degraded_after_seconds)),
        offline_after_seconds=float(heartbeat_raw.get("offline_after_seconds", heartbeat_defaults.offline_after_seconds)),
    )
    if heartbeat_thresholds.degraded_after_seconds <= 0 or heartbeat_thresholds.offline_after_seconds <= 0:
        raise ValueError("nodes.heartbeat thresholds must be positive")
    if heartbeat_thresholds.offline_after_seconds < heartbeat_thresholds.degraded_after_seconds:
        raise ValueError("nodes.heartbeat.offline_after_seconds must be >= degraded_after_seconds")

    remote_raw = nodes_raw.get("remote", [])
    if not isinstance(remote_raw, list):
        raise ValueError("nodes.remote must be a list")
    seen_node_ids: set[str] = set()
    remote_nodes: list[RemoteNodeConfig] = []
    for index, entry in enumerate(remote_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"nodes.remote[{index}] must be a mapping")
        node_id = entry.get("node_id")
        endpoint = entry.get("endpoint")
        token_env = entry.get("token_env")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"nodes.remote[{index}].node_id is required and must be a non-empty string")
        if node_id == "local":
            raise ValueError(f"nodes.remote[{index}].node_id cannot be 'local' -- that id is reserved for this host")
        if node_id in seen_node_ids:
            raise ValueError(f"nodes.remote contains node_id {node_id!r} more than once")
        seen_node_ids.add(node_id)
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError(f"nodes.remote[{index}].endpoint is required and must be a non-empty string")
        if not isinstance(token_env, str) or not token_env:
            raise ValueError(f"nodes.remote[{index}].token_env is required and must be a non-empty string "
                             "(names the environment variable holding this node's shared secret -- "
                             "the secret itself never lives in config.yaml)")
        max_sessions = entry.get("max_sessions")
        if max_sessions is not None and (not isinstance(max_sessions, int) or max_sessions < 1):
            raise ValueError(f"nodes.remote[{index}].max_sessions must be a positive integer if given")
        remote_nodes.append(RemoteNodeConfig(
            node_id=node_id,
            display_name=entry.get("display_name") or node_id,
            hostname=entry.get("hostname") or node_id,
            endpoint=endpoint,
            token_env=token_env,
            max_sessions=max_sessions,
            timeout_seconds=float(entry.get("timeout_seconds", 10.0)),
        ))
    discovery_raw = nodes_raw.get("discovery", {})
    if not isinstance(discovery_raw, dict):
        raise ValueError("nodes.discovery must be a mapping")
    discovery_defaults = DiscoveryConfig()
    discovery_config = DiscoveryConfig(
        enabled=bool(discovery_raw.get("enabled", discovery_defaults.enabled)),
        agent_port=int(discovery_raw.get("agent_port", discovery_defaults.agent_port)),
        concurrency=int(discovery_raw.get("concurrency", discovery_defaults.concurrency)),
        host_timeout_seconds=float(discovery_raw.get("host_timeout_seconds", discovery_defaults.host_timeout_seconds)),
        max_hosts_per_scan=int(discovery_raw.get("max_hosts_per_scan", discovery_defaults.max_hosts_per_scan)),
        overall_timeout_seconds=float(discovery_raw.get("overall_timeout_seconds", discovery_defaults.overall_timeout_seconds)),
        cooldown_seconds=float(discovery_raw.get("cooldown_seconds", discovery_defaults.cooldown_seconds)),
    )
    if discovery_config.concurrency < 1 or discovery_config.max_hosts_per_scan < 1:
        raise ValueError("nodes.discovery.concurrency and max_hosts_per_scan must be at least 1")
    if discovery_config.host_timeout_seconds <= 0 or discovery_config.overall_timeout_seconds <= 0:
        raise ValueError("nodes.discovery timeouts must be positive")

    remote_connect_raw = nodes_raw.get("remote_connect", {})
    if not isinstance(remote_connect_raw, dict):
        raise ValueError("nodes.remote_connect must be a mapping")
    remote_connect_defaults = RemoteConnectConfig()
    remote_connect_config = RemoteConnectConfig(
        allow_public_manual_add=bool(remote_connect_raw.get("allow_public_manual_add",
                                                            remote_connect_defaults.allow_public_manual_add)),
        ssh_connect_timeout_seconds=float(remote_connect_raw.get("ssh_connect_timeout_seconds",
                                                                 remote_connect_defaults.ssh_connect_timeout_seconds)),
        bootstrap_timeout_seconds=float(remote_connect_raw.get("bootstrap_timeout_seconds",
                                                               remote_connect_defaults.bootstrap_timeout_seconds)),
    )

    onboarding_config = _load_onboarding_config(nodes_raw.get("onboarding", {}))

    nodes_config = NodesConfig(overload_thresholds=overload_thresholds, heartbeat_thresholds=heartbeat_thresholds,
                               remote_nodes=tuple(remote_nodes), discovery=discovery_config,
                               remote_connect=remote_connect_config, onboarding=onboarding_config)

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
            # Empty is valid, same reason as allowed_session_patterns above:
            # migration input, not an authorization list.
            allowed_session_patterns=string_tuple("allowed_session_patterns",
                                                  InputPolicyConfig.allowed_session_patterns, allow_empty=True),
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
        fleet_sync=_load_fleet_sync_config(raw.get("fleet_sync", {})),
        repo_read=_load_repo_read_config(raw.get("repo_read", {})),
        work=_load_work_config(raw.get("work", {})),
        session_lifecycle=_load_session_lifecycle_config(raw.get("session_lifecycle", {})),
        session_knowledge=_load_session_knowledge_config(raw.get("session_knowledge", {})),
        session_access=_load_session_access_config(raw.get("session_access", {})),
        ask_chatgpt=_load_ask_chatgpt_config(raw.get("ask_chatgpt", {})),
        nodes=nodes_config,
        queue=_load_queue_config(raw.get("queue", {})),
        submit=submit_config,
        integration_loop=_load_integration_loop_config(raw.get("integration_loop", {})),
        submit_watchdog=watchdog_config,
        ai_usage=_load_ai_usage_config(raw.get("ai_usage", {})),
        notes=_load_notes_config(raw.get("notes", {})),
        auto_recovery=_load_auto_recovery_config(raw.get("auto_recovery", {})),
    )


def _load_onboarding_config(raw: object) -> OnboardingConfig:
    """nodes.onboarding -- validated hard, because every field here ends up
    inside a script that runs as Administrator on someone else's machine.
    An invalid value is a startup error, never a silently-corrected
    default: a typo'd port range that quietly became 22000-22999 would be
    discovered as a port collision months later."""
    if not isinstance(raw, dict):
        raise ValueError("nodes.onboarding must be a mapping")
    defaults = OnboardingConfig()

    ttl = int(raw.get("enrollment_ttl_seconds", defaults.enrollment_ttl_seconds))
    if ttl < 60 or ttl > 86400:
        raise ValueError("nodes.onboarding.enrollment_ttl_seconds must be between 60 and 86400")
    agent_port = int(raw.get("agent_port", defaults.agent_port))
    if not (1 <= agent_port <= 65535):
        raise ValueError("nodes.onboarding.agent_port must be a valid TCP port")
    heartbeat = int(raw.get("heartbeat_interval_seconds", defaults.heartbeat_interval_seconds))
    if heartbeat < 5:
        raise ValueError("nodes.onboarding.heartbeat_interval_seconds must be at least 5")
    controller_url = str(raw.get("controller_url", defaults.controller_url) or "").strip()
    if controller_url and not controller_url.startswith(("http://", "https://")):
        raise ValueError("nodes.onboarding.controller_url must start with http:// or https://")

    firewall_raw = raw.get("ssh_firewall_cidrs", defaults.ssh_firewall_cidrs)
    if isinstance(firewall_raw, str):
        firewall_raw = [firewall_raw]
    if not isinstance(firewall_raw, (list, tuple)):
        raise ValueError("nodes.onboarding.ssh_firewall_cidrs must be a list of CIDRs")
    firewall_cidrs = []
    for entry in firewall_raw:
        text = str(entry).strip()
        if not text:
            continue
        if text.lower() in ("any", "0.0.0.0/0", "*"):
            # Allowed, but only when spelled out -- see the field comment.
            firewall_cidrs.append("any")
            continue
        try:
            ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"nodes.onboarding.ssh_firewall_cidrs entry {text!r} is not a CIDR: {exc}") from exc
        firewall_cidrs.append(text)

    tailscale_raw = raw.get("tailscale", {})
    if not isinstance(tailscale_raw, dict):
        raise ValueError("nodes.onboarding.tailscale must be a mapping")
    ts_defaults = TailscaleOnboardConfig()
    tags_raw = tailscale_raw.get("tags", ts_defaults.tags)
    if isinstance(tags_raw, str):
        tags_raw = [tags_raw]
    if not isinstance(tags_raw, (list, tuple)):
        raise ValueError("nodes.onboarding.tailscale.tags must be a list")
    tailscale = TailscaleOnboardConfig(
        enabled=bool(tailscale_raw.get("enabled", ts_defaults.enabled)),
        auth_key_env=str(tailscale_raw.get("auth_key_env", ts_defaults.auth_key_env) or "").strip(),
        tags=tuple(str(tag).strip() for tag in tags_raw if str(tag).strip()),
        unattended=bool(tailscale_raw.get("unattended", ts_defaults.unattended)),
        login_server=str(tailscale_raw.get("login_server", ts_defaults.login_server) or "").strip(),
    )
    if tailscale.login_server and not tailscale.login_server.startswith(("http://", "https://")):
        raise ValueError("nodes.onboarding.tailscale.login_server must start with http:// or https://")
    # A key inline in config.yaml is the one thing this whole design exists
    # to prevent -- refuse it loudly rather than accept and redact it.
    if "auth_key" in tailscale_raw:
        raise ValueError("nodes.onboarding.tailscale.auth_key is not accepted -- put the key in an environment "
                         "variable and name it with auth_key_env instead")

    rescue_raw = raw.get("rescue", {})
    if not isinstance(rescue_raw, dict):
        raise ValueError("nodes.onboarding.rescue must be a mapping")
    for forbidden in ("gateway_private_key", "private_key", "gateway_password", "password"):
        if forbidden in rescue_raw:
            raise ValueError(f"nodes.onboarding.rescue.{forbidden} is not accepted -- the rescue tunnel uses a "
                             "keypair the NODE generates locally; this controller never holds a node private key")
    rs_defaults = RescueTunnelConfig()
    rescue = RescueTunnelConfig(
        enabled=bool(rescue_raw.get("enabled", rs_defaults.enabled)),
        gateway_host=str(rescue_raw.get("gateway_host", rs_defaults.gateway_host) or "").strip(),
        gateway_port=int(rescue_raw.get("gateway_port", rs_defaults.gateway_port)),
        gateway_user=str(rescue_raw.get("gateway_user", rs_defaults.gateway_user) or "").strip(),
        gateway_host_key=str(rescue_raw.get("gateway_host_key", rs_defaults.gateway_host_key) or "").strip(),
        port_range_start=int(rescue_raw.get("port_range_start", rs_defaults.port_range_start)),
        port_range_end=int(rescue_raw.get("port_range_end", rs_defaults.port_range_end)),
        keepalive_interval_seconds=int(rescue_raw.get("keepalive_interval_seconds",
                                                      rs_defaults.keepalive_interval_seconds)),
        keepalive_count_max=int(rescue_raw.get("keepalive_count_max", rs_defaults.keepalive_count_max)),
        retry_seconds=int(rescue_raw.get("retry_seconds", rs_defaults.retry_seconds)),
    )
    if not (1 <= rescue.gateway_port <= 65535):
        raise ValueError("nodes.onboarding.rescue.gateway_port must be a valid TCP port")
    if rescue.port_range_start < 1024 or rescue.port_range_end > 65535:
        raise ValueError("nodes.onboarding.rescue port range must lie within 1024-65535")
    if rescue.port_range_start > rescue.port_range_end:
        raise ValueError("nodes.onboarding.rescue.port_range_start must be <= port_range_end")
    if rescue.keepalive_interval_seconds < 5 or rescue.keepalive_count_max < 1 or rescue.retry_seconds < 5:
        raise ValueError("nodes.onboarding.rescue keepalive/retry values are too small to be useful")

    return OnboardingConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        enrollment_ttl_seconds=ttl,
        controller_url=controller_url,
        controller_ssh_public_key=str(raw.get("controller_ssh_public_key",
                                              defaults.controller_ssh_public_key) or "").strip(),
        controller_ssh_public_key_file=str(raw.get("controller_ssh_public_key_file",
                                                   defaults.controller_ssh_public_key_file) or "").strip(),
        agent_port=agent_port,
        heartbeat_interval_seconds=heartbeat,
        ssh_firewall_cidrs=tuple(firewall_cidrs) or defaults.ssh_firewall_cidrs,
        tailscale=tailscale,
        rescue=rescue,
    )


def _load_auto_recovery_config(raw: object) -> AutoRecoveryConfig:
    if not isinstance(raw, dict):
        raw = {}
    max_attempts = int(raw.get("max_attempts", AutoRecoveryConfig.max_attempts))
    lock_ttl = float(raw.get("lock_ttl_seconds", AutoRecoveryConfig.lock_ttl_seconds))
    reconcile_poll = float(raw.get("reconcile_poll_seconds", AutoRecoveryConfig.reconcile_poll_seconds))
    max_missing_age = float(raw.get("max_missing_age_seconds", AutoRecoveryConfig.max_missing_age_seconds))
    if max_attempts < 1:
        raise ValueError("auto_recovery.max_attempts must be at least 1")
    if lock_ttl <= 0:
        raise ValueError("auto_recovery.lock_ttl_seconds must be positive")
    if reconcile_poll < 0.5:
        raise ValueError("auto_recovery.reconcile_poll_seconds must be at least 0.5")
    if max_missing_age < 0:
        raise ValueError("auto_recovery.max_missing_age_seconds must be >= 0 (0 disables the bound)")
    return AutoRecoveryConfig(enabled=bool(raw.get("enabled", False)), max_attempts=max_attempts,
                              lock_ttl_seconds=lock_ttl, reconcile_poll_seconds=reconcile_poll,
                              max_missing_age_seconds=max_missing_age)


def _load_notes_config(raw: object) -> NotesConfig:
    if not isinstance(raw, dict):
        raw = {}
    max_bytes = int(raw.get("max_attachment_bytes", NotesConfig.max_attachment_bytes))
    if max_bytes <= 0:
        raise ValueError("notes.max_attachment_bytes must be positive")
    allowed_raw = raw.get("allowed_mime_types", NotesConfig.allowed_mime_types)
    if isinstance(allowed_raw, str):
        allowed_raw = [allowed_raw]
    allowed = tuple(str(item).strip().lower() for item in allowed_raw if str(item).strip())
    if not allowed:
        raise ValueError("notes.allowed_mime_types must list at least one type")
    unsupported = [item for item in allowed if item not in _NOTES_SERVABLE_MIME_TYPES]
    if unsupported:
        # Fail LOUDLY at config load rather than accepting a type the
        # content sniffer cannot recognise -- that combination would
        # silently refuse every upload of that type at runtime with a
        # confusing "not an allowed image type" error.
        raise ValueError(
            "notes.allowed_mime_types contains types this build cannot verify or serve: "
            + ", ".join(unsupported) + " (supported: " + ", ".join(sorted(_NOTES_SERVABLE_MIME_TYPES)) + ")")
    roots_raw = raw.get("attachment_source_roots", NotesConfig.attachment_source_roots)
    if isinstance(roots_raw, str):
        roots_raw = [roots_raw]
    roots = tuple(str(item).strip() for item in roots_raw if str(item).strip())
    for root in roots:
        if not Path(root).expanduser().is_absolute():
            raise ValueError(f"notes.attachment_source_roots entries must be absolute paths: {root}")
    return NotesConfig(
        enabled=bool(raw.get("enabled", NotesConfig.enabled)),
        require_auth=bool(raw.get("require_auth", NotesConfig.require_auth)),
        attachments_dir=str(raw.get("attachments_dir", NotesConfig.attachments_dir) or ""),
        max_attachment_bytes=max_bytes,
        allowed_mime_types=allowed,
        attachment_source_roots=roots,
    )


def _load_ai_usage_config(raw: object) -> AiUsageConfig:
    if not isinstance(raw, dict):
        raw = {}
    timeout = float(raw.get("timeout_seconds", AiUsageConfig.timeout_seconds))
    cache_ttl = float(raw.get("cache_ttl_seconds", AiUsageConfig.cache_ttl_seconds))
    warning = float(raw.get("warning_threshold_percent", AiUsageConfig.warning_threshold_percent))
    critical = float(raw.get("critical_threshold_percent", AiUsageConfig.critical_threshold_percent))
    if timeout <= 0:
        raise ValueError("ai_usage.timeout_seconds must be positive")
    if cache_ttl < 0:
        raise ValueError("ai_usage.cache_ttl_seconds must be non-negative")
    if not (0 <= warning <= 100) or not (0 <= critical <= 100):
        raise ValueError("ai_usage.warning_threshold_percent/critical_threshold_percent must be 0-100")
    return AiUsageConfig(
        enabled=bool(raw.get("enabled", True)), base_url=str(raw.get("base_url", AiUsageConfig.base_url)),
        timeout_seconds=timeout, cache_ttl_seconds=cache_ttl,
        warning_threshold_percent=warning, critical_threshold_percent=critical,
    )


def _load_integration_loop_config(raw: object) -> IntegrationLoopConfig:
    if not isinstance(raw, dict):
        raw = {}
    fallback_poll = float(raw.get("fallback_poll_seconds", IntegrationLoopConfig.fallback_poll_seconds))
    if fallback_poll < 0.5:
        raise ValueError("integration_loop.fallback_poll_seconds must be at least 0.5")
    return IntegrationLoopConfig(enabled=bool(raw.get("enabled", False)), fallback_poll_seconds=fallback_poll)


def _load_queue_config(queue_raw: object) -> QueueConfig:
    if not isinstance(queue_raw, dict):
        queue_raw = {}
    poll_interval = float(queue_raw.get("poll_interval_seconds", QueueConfig.poll_interval_seconds))
    if poll_interval < 0.5:
        raise ValueError("queue.poll_interval_seconds must be at least 0.5")
    return QueueConfig(enabled=bool(queue_raw.get("enabled", False)), poll_interval_seconds=poll_interval)


def _load_session_knowledge_config(raw: object) -> SessionKnowledgeConfig:
    if not isinstance(raw, dict):
        return SessionKnowledgeConfig()
    return SessionKnowledgeConfig(enabled=bool(raw.get("enabled", False)))


def _load_session_access_config(raw: object) -> SessionAccessConfig:
    if not isinstance(raw, dict):
        return SessionAccessConfig()
    defaults = SessionAccessConfig()
    return SessionAccessConfig(
        default_read=bool(raw.get("default_read", defaults.default_read)),
        default_input=bool(raw.get("default_input", defaults.default_input)),
        migrate_whitelist_on_start=bool(raw.get("migrate_whitelist_on_start",
                                                defaults.migrate_whitelist_on_start)),
    )


def _load_work_config(raw: object) -> WorkConfig:
    if not isinstance(raw, dict):
        raw = {}
    interval = int(raw.get("interval_seconds", WorkConfig.interval_seconds))
    if interval < 5:
        raise ValueError("work.interval_seconds must be at least 5")
    max_runs = int(raw.get("max_runs_per_tick", WorkConfig.max_runs_per_tick))
    if max_runs < 1:
        raise ValueError("work.max_runs_per_tick must be at least 1")
    revisions = int(raw.get("max_revisions", WorkConfig.max_revisions))
    if revisions < 0:
        raise ValueError("work.max_revisions must not be negative")
    return WorkConfig(
        enabled=bool(raw.get("enabled", WorkConfig.enabled)),
        interval_seconds=interval, max_runs_per_tick=max_runs,
        auto_enable_dispatch=bool(raw.get("auto_enable_dispatch",
                                          WorkConfig.auto_enable_dispatch)),
        lease_seconds=int(raw.get("lease_seconds", WorkConfig.lease_seconds)),
        max_revisions=revisions,
        no_progress_limit=int(raw.get("no_progress_limit", WorkConfig.no_progress_limit)))


def _load_repo_read_config(raw: object) -> RepoReadConfig:
    """Validation is strict and fail-closed: a config that asks for an
    unbounded limit is a CONFIG ERROR, not something to silently clamp,
    because a caps section nobody can trust is worse than no caps at
    all. Each ceiling below is a sanity bound on operator intent, not the
    limit itself."""
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get("enabled", RepoReadConfig.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("repo_read.enabled must be a boolean")
    roots = raw.get("allowed_roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise ValueError("repo_read.allowed_roots must be a list of strings")
    for root in roots:
        # A relative root cannot be reasoned about (it would resolve
        # against whatever cwd the server happens to have) and "/" would
        # make the allowlist meaningless -- both are refused outright.
        if not root.startswith("/") and not root.startswith("~"):
            raise ValueError(f"repo_read.allowed_roots entry {root!r} must be an absolute path")
        if root.strip() == "/":
            raise ValueError("repo_read.allowed_roots may not contain '/'")
    globs = raw.get("extra_secret_globs", [])
    if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
        raise ValueError("repo_read.extra_secret_globs must be a list of strings")

    def bounded(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"repo_read.{key} must be an integer")
        if not low <= value <= high:
            raise ValueError(f"repo_read.{key} must be between {low} and {high}")
        return value

    timeout = raw.get("timeout_seconds", RepoReadConfig.timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("repo_read.timeout_seconds must be a number")
    if not 1.0 <= float(timeout) <= 120.0:
        raise ValueError("repo_read.timeout_seconds must be between 1 and 120")
    return RepoReadConfig(
        enabled=enabled, allowed_roots=tuple(roots),
        max_bytes=bounded("max_bytes", RepoReadConfig.max_bytes, 1_024, 8_000_000),
        max_lines=bounded("max_lines", RepoReadConfig.max_lines, 10, 100_000),
        max_results=bounded("max_results", RepoReadConfig.max_results, 1, 5_000),
        max_tree_entries=bounded("max_tree_entries", RepoReadConfig.max_tree_entries, 1, 50_000),
        max_tree_depth=bounded("max_tree_depth", RepoReadConfig.max_tree_depth, 1, 32),
        max_log_entries=bounded("max_log_entries", RepoReadConfig.max_log_entries, 1, 5_000),
        max_diff_bytes=bounded("max_diff_bytes", RepoReadConfig.max_diff_bytes, 1_024, 16_000_000),
        timeout_seconds=float(timeout), extra_secret_globs=tuple(globs))


def _load_fleet_sync_config(raw: object) -> FleetSyncConfig:
    if not isinstance(raw, dict):
        raw = {}
    interval = int(raw.get("interval_seconds", FleetSyncConfig.interval_seconds))
    # The floor is 60s for the same reason maintenance has one: a tighter
    # loop would re-project every store on this box continuously for data
    # that changes on the order of minutes.
    if interval < 60:
        raise ValueError("fleet_sync.interval_seconds must be at least 60")
    # An interval at or past the staleness threshold guarantees the very
    # condition the loop exists to prevent, so it is refused rather than
    # silently accepted.
    if interval >= 900:
        raise ValueError(
            "fleet_sync.interval_seconds must be under 900 (the staleness "
            "threshold) or the cache it refreshes is stale by definition")
    return FleetSyncConfig(
        enabled=bool(raw.get("enabled", FleetSyncConfig.enabled)),
        interval_seconds=interval,
        peer_exchange_enabled=bool(raw.get("peer_exchange_enabled",
                                           FleetSyncConfig.peer_exchange_enabled)))


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
    resume_capable_raw = raw.get("resume_capable_agent_types",
                                 list(SessionLifecycleConfig.resume_capable_agent_types))
    if not isinstance(resume_capable_raw, list) or not all(isinstance(a, str) and a for a in resume_capable_raw):
        raise ValueError("session_lifecycle.resume_capable_agent_types must be a list of strings")
    codex_yolo_raw = raw.get("codex_yolo", SessionLifecycleConfig.codex_yolo)
    if not isinstance(codex_yolo_raw, bool):
        raise ValueError("session_lifecycle.codex_yolo must be a boolean")
    if os.environ.get("TERMINAL_MCP_CODEX_YOLO") is not None:
        codex_yolo_raw = os.environ["TERMINAL_MCP_CODEX_YOLO"].strip().lower() == "true"
    return SessionLifecycleConfig(
        enabled=enabled, allowed_cwd_roots=tuple(roots), protected_sessions=protected_set,
        launch_commands=tuple(sorted(launch_raw.items())), create_ready_timeout_seconds=timeout,
        default_grant_mode=grant_mode, resume_capable_agent_types=tuple(resume_capable_raw),
        codex_yolo=codex_yolo_raw,
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
