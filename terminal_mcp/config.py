from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit as _urlsplit

import yaml

from . import endpoint_policy
from .node_models import NodeHeartbeatThresholds, OverloadThresholds
from .project_task_feeder import ProjectFeedConfig


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
    # Canonical project backlog -> queue bridge. Empty by default: no project
    # file is read and no new work is created unless an operator explicitly
    # maps a -work lane to a TASKS.json path.
    project_feeds: tuple[ProjectFeedConfig, ...] = ()


@dataclass(frozen=True)
class AgentsConfig:
    """TMCP-AGENT-RUNTIME-001 Phase B: the Agent/Skill runtime.

    `skill_roots` is the ONLY place a skill package may be read from. It is
    configuration, never a caller-supplied path, which is what stops a tool
    call from widening its own search -- see skill_packages.py for the full
    three-rule traversal defence. Empty means "use the defaults"
    (the repo's own skills/ directory and the per-user Claude skills home),
    never "anywhere".
    """
    enabled: bool = True
    skill_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouterConfig:
    """TMCP-TASK-ROUTER-001: which routing behaviours are allowed to act.

    THE DEFAULTS ARE NOT THE USUAL "OFF UNTIL AN OPERATOR OPTS IN", AND THAT
    IS DELIBERATE. Every other autonomous switch in this project defaults off
    because turning it on means the system may CREATE or SEND work nobody
    asked for. Routing creates nothing: it only decides where work a caller
    has already durably enqueued should run. Shipping it off by default would
    mean shipping the bug it fixes -- a task left QUEUED beside an idle,
    compatible session -- still armed on every deployment, which is precisely
    the outcome this feature exists to remove.

    `spawn_enabled` is the exception and IS off by default, because creating a
    session is the one routing action that changes the fleet rather than
    merely using it. An operator turns that on when they want the router to
    grow capacity on demand.
    """
    #: Master switch. Off means route_start and the rescue sweep both decline,
    #: and every existing session-targeted path keeps working exactly as before.
    enabled: bool = True
    #: The restart-safe reconcile that re-asks "could this run now?" for every
    #: task with no runtime. This is the half that eliminates the stuck-QUEUED
    #: bug for tasks nobody re-submits.
    rescue_enabled: bool = True
    #: Create a compatible session when nothing eligible exists. Off by
    #: default: this is the only routing action that grows the fleet.
    spawn_enabled: bool = False
    #: Hard ceiling on router-created sessions, so a pathological backlog
    #: cannot spawn without bound even once spawning is enabled.
    max_spawned_sessions: int = 4
    #: agent_type used for a spawn when the task expresses no preference.
    #: NOT "shell": a plain shell cannot receive a queued dispatch at all (see
    #: session_matcher.DISPATCHABLE_RUNTIMES), so spawning one would create a
    #: session the matcher must immediately reject.
    default_runtime: str = "claude"
    #: Seconds between rescue sweeps. Independent of the queue loop's own
    #: poll interval so a fast dispatch cadence does not force a fleet
    #: listing every three seconds.
    rescue_interval_seconds: float = 10.0
    #: Most tasks one sweep will attempt to place.
    rescue_batch_size: int = 20
    #: WALL-CLOCK CEILING ON THE SYNCHRONOUS HALF OF route_start. Driving the
    #: engine means remote node I/O per tick, and an unbounded drive turns a
    #: submission into a long poll -- the exact thing a durable receipt exists
    #: to avoid. When the budget runs out the task stays bound and the server
    #: keeps advancing it; the only thing that stops is the caller's wait.
    dispatch_budget_seconds: float = 12.0
    #: How many ranked candidates get a live status probe before we commit.
    #: Each one is a round trip, so this is the other half of the latency bill.
    #: Sized for a fleet of this size: too low and the router stops looking
    #: while good runtimes sit unexamined, which it now reports rather than
    #: mislabelling as "nothing eligible".
    probe_limit: int = 8
    #: WALL-CLOCK CEILING ON THE FLEET SNAPSHOT ALONE, carved out of
    #: dispatch_budget_seconds rather than added to it. Measured live on
    #: hp-linux (2026-09-20) against a five-node fleet: a serial session
    #: listing took 3.6s-25.4s and consumed the entire 12s dispatch budget, so
    #: project_start returned with dispatch_ticks=0 and the task never started
    #: synchronously. Looking at the fleet is a means; starting the task is the
    #: end, so the snapshot is capped at this OR half the dispatch budget,
    #: whichever is smaller, and the rest is left for real dispatch ticks.
    snapshot_budget_seconds: float = 4.0
    #: How old a fallback fleet snapshot may be when a fresh listing comes back
    #: empty because nodes timed out. Routing on a recent snapshot risks a
    #: candidate that has since gone busy -- which the live probe re-checks and
    #: the atomic bind refuses outright. Routing on NOTHING defers the whole
    #: queue on one slow node, which nothing downstream corrects.
    stale_snapshot_max_age_seconds: float = 60.0


@dataclass(frozen=True)
class LLMGovernorConfig:
    """Process-wide admission limits for provider-bound agent work.

    Values are environment controlled so a production rollback/tune does not
    require a code change.  Limits are intentionally finite by default.
    """
    global_max_concurrency: int = 4
    openrouter_max_concurrency: int = 2
    codex_max_concurrency: int = 2
    claude_max_concurrency: int = 1
    queue_wait_timeout_seconds: float = 900.0
    retry_max_attempts: int = 4
    retry_base_delay_seconds: float = 2.0
    retry_max_delay_seconds: float = 32.0
    retry_jitter: bool = True
    cooldown_429_seconds: float = 30.0

    def provider_limit(self, provider: str) -> int:
        return {
            "openrouter": self.openrouter_max_concurrency,
            "codex": self.codex_max_concurrency,
            "claude": self.claude_max_concurrency,
        }.get(provider.casefold(), self.global_max_concurrency)


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
    max_enter_attempts: int = 2
    # Hard cap shared by the initial/manual Enter and all background recovery.
    max_total_enters: int = 6
    sweeper_interval_seconds: float = 3.0
    ttl_seconds: float = 600.0
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
class LifecycleConfig:
    """Lifecycle Close-Loop V1 (lifecycle_service.py): the reconcile pass
    that carries work from a verify PASS through merge and release to a
    safely reaped worktree.

    OFF by default, same posture as every other autonomous loop here. With
    `enabled: false` the service is still constructed and every method is
    still callable by hand (the MCP tools, a test) -- only the AUTOMATIC
    invocation from MaintenanceLoop is gated, matching the established
    "manual call always available, only the automatic trigger is gated"
    convention of queue/integration_loop/auto_recovery.

    `worktree_roots` is a genuine safety boundary, not a convenience: the
    reaper refuses to remove any worktree outside it. Empty means "no
    containment check", which is why it is empty only in a test rig --
    a real deployment names its roots explicitly, and the reaper is
    therefore incapable of touching a path the operator never listed."""
    enabled: bool = False
    main_branch: str = "main"
    environment: str = "dev"
    worktree_roots: tuple[str, ...] = ()
    reconcile_limit: int = 50
    allow_unverified_integration: bool = True
    """Backward compatibility, made explicit. Before this feature a task
    published its integration handoff on COMPLETED, so integration never
    required a verification at all. Leaving this True keeps a deployment
    that never adopted the verify queue working exactly as before -- but
    the bypass is no longer silent: it is suppressed entirely whenever the
    task has a verify job, and when it does fire it writes a
    VERIFICATION_BYPASSED event naming the task. Set False to require a
    VERIFIED_PASS before anything may enter the merge queue."""


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


@dataclass(frozen=True)
class SessionHealthConfig:
    """Thresholds for session resource health (TMCP-SESSION-HEALTH-001).

    Read-only and defaults ON, for the same reason AiUsageConfig above
    does: there is no autonomous ACTION gated here. terminal_status only
    ever REPORTS a recommended_action; nothing in this project acts on it,
    so the worst case of enabling it is an extra bounded pane parse and (a
    cached) `git status` per status call. Set `enabled: false` to drop the
    `resource` block from the payload entirely -- every existing field is
    unaffected either way.

    The four percentages are the documented policy table. They apply to
    CONTEXT ONLY; quota severity reuses ai_usage's own
    warning/critical_threshold_percent rather than duplicating a second
    pair of numbers that could drift from the dashboard's.
    """
    enabled: bool = True
    watch_percent: float = 70.0
    prepare_rollover_percent: float = 85.0
    finish_rollover_percent: float = 92.0
    checkpoint_only_percent: float = 97.0
    # One bounded, cached `git status --porcelain` per cwd per TTL, so
    # `git.dirty` is real evidence rather than an assumption. Turning this
    # off reports dirty=None, which the rollover hook treats as
    # not-proven-safe -- never as clean.
    git_probe_enabled: bool = True
    git_probe_cache_seconds: float = 10.0


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
class WorktreeJanitorConfig:
    """Worktree Janitor (docs/WORKTREE_JANITOR.md). P0 ships the CLASSIFIER
    ONLY -- there is no executor, so no setting here can cause a deletion.

    `mode` defaults to observe_only (invariant I8) and mirrors
    supervisor2.POLICY_MODES' escalation shape. It is recorded in every report
    so an operator can see which mode produced a verdict; the classifier never
    acts in any mode.

    `allowed_roots` empty means NOTHING is collectable -- deliberately the
    opposite of repo_read's fallback-to-home behaviour. A reclaim deletes; its
    allowlist must be stated explicitly by an operator, never inherited.

    `extra_valuable_globs` is additive only. There is no key that removes a
    built-in valuable glob, so no config edit can make a credential file or a
    sqlite db collectable."""

    mode: str = "observe_only"  # observe_only | suggest_only | auto_execute
    allowed_roots: tuple[str, ...] = ()
    integration_ref: str = "main"
    allow_preserved_unmerged: bool = False
    grace_seconds: int = 86_400
    grace_floor_seconds: int = 3_600
    max_evidence_age_seconds: float = 120.0
    max_candidates_per_run: int = 50
    timeout_seconds: float = 20.0
    extra_valuable_globs: tuple[str, ...] = ()
    # P3 sweep. `sweep_enabled` gates only the BACKGROUND THREAD -- run_once()
    # stays callable via MCP with it off, this project's standing "the manual
    # path always works, only the automatic trigger is gated" posture.
    sweep_enabled: bool = False
    sweep_interval_seconds: float = 900.0
    sweep_budget_seconds: float = 60.0
    orphan_confirm_runs: int = 2
    orphan_min_age_seconds: float = 3600.0
    repo_roots: tuple[str, ...] = ()

    def to_policy(self) -> Any:
        from .worktree_janitor import JanitorPolicy

        return JanitorPolicy(
            mode=self.mode, allowed_roots=tuple(self.allowed_roots),
            integration_ref=self.integration_ref,
            allow_preserved_unmerged=self.allow_preserved_unmerged,
            grace_seconds=self.grace_seconds,
            max_evidence_age_seconds=self.max_evidence_age_seconds,
            timeout_seconds=self.timeout_seconds,
            extra_valuable_globs=tuple(self.extra_valuable_globs))


@dataclass(frozen=True)
class PromptDeliveryConfig:
    """Prompt delivery / acceptance gate (see delivery_gate.py).

    ADVISORY BY DEFAULT, and that default is the whole reason this can ship
    at all: in "advisory" the gate is computed, recorded to audit/events and
    returned as additive fields, while every existing transition happens
    exactly as it does today. Zero behaviour change until an operator sets
    "enforce". The escalation shape mirrors supervisor2.POLICY_MODES -- the
    project's audited precedent for exactly this kind of rollout.

    In "enforce", a send that does not reach DELIVERED cannot advance a
    queue task to RUNNING or a supervisor action to observing. That IS a
    real behaviour change (a task that today goes RUNNING on a confirmed
    submit with no acceptance evidence would instead hold), which is why it
    is opt-in and per-deployment.

    `require_acceptance=False` keeps gate 1 (the positive delivery_state
    allowlist, which is pure hardening and cannot fail open) while skipping
    gate 2 -- for a deployment that wants the denylist fixed without the
    extra post-submit observation.
    """

    mode: str = "advisory"  # advisory | enforce
    require_acceptance: bool = True
    # How long to wait after a confirmed submit before deciding acceptance
    # was not observed. Short: this is one extra observation, not a poll
    # loop -- the completion watcher already owns long-running observation.
    acceptance_timeout_seconds: float = 3.0
    acceptance_poll_interval_seconds: float = 0.4
    acceptance_capture_lines: int = 40

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"


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
class UiWorkflowConfig:
    """The central UI workflow policy (ui_workflow.py, TMCP-UI-WORKFLOW-001).

    ON by default, unlike `browser`. The difference is what the capability
    DOES: the browser gateway opens outbound connections and renders what
    comes back, while this module only DECIDES -- it selects a project
    profile, orders the precedence layers and grades an audit result. It
    opens no socket, touches no file and installs nothing, so there is
    nothing here an operator needs to opt into.

    `installed_skills` is declared rather than probed, and that is the
    determinism requirement rather than laziness: the runtime must not fetch
    an arbitrary skill mid-task, so the policy reports a skill it was not
    told about as MISSING instead of going to look for it. An operator who
    installs Taste records it here once.

    `default_project` is the profile used when neither an explicit project_id
    nor the repo path matches one. Empty means fall back to the built-in
    generic profile, which is the strictest posture -- see ui_workflow.GENERIC
    for why an unrecognised project gets the tightest rules, not the loosest.
    """

    enabled: bool = True
    installed_skills: tuple[str, ...] = ()
    default_project: str = ""


@dataclass(frozen=True)
class BrowserGatewayConfig:
    """The Phase-1 browser gateway (browser_gateway.py, browser_worker.py).

    OFF by default, unlike `repo_read`. The difference is what the
    capability can DO: repo_read only reads files already on this box,
    while this one opens outbound network connections from inside the
    trust boundary and renders whatever comes back. That is worth an
    explicit operator decision, so an install that never wants a browser
    never grows one by upgrading.

    `allow_private_networks` is the setting to think hardest about.
    Loopback requires explicit permission for a local dev server,
    but the LAN is not the same thing, and a chat-driven browser that can
    reach it is a port scanner with a friendly interface. Instance
    metadata endpoints are blocked unconditionally by
    browser_safety.validate_url and no setting here can reopen them.

    `executable` empty means Playwright's own bundled Chromium, which is
    the supported path. Point it at a system Chrome only when that build
    is the thing under test.

    Timeouts are two-layer by design: `navigation_timeout_seconds` is what
    Playwright enforces per operation, and `hard_timeout_seconds` is the
    wall clock after which the parent KILLS the worker's process group. The
    second exists because the first cannot cover a driver that never
    answers at all."""

    enabled: bool = False
    #: Empty -> Playwright's bundled Chromium.
    executable: str = ""
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 800
    navigation_timeout_seconds: float = 20.0
    hard_timeout_seconds: float = 25.0
    allow_loopback: bool = False
    allow_private_networks: bool = False
    allow_url_patterns: tuple[str, ...] = ()
    deny_url_patterns: tuple[str, ...] = ()
    #: Screenshots are written here. Empty -> a subdirectory of the
    #: server's own state directory; never the repo, so an artifact can
    #: never be committed by accident.
    artifact_dir: str = ""
    keep_artifacts: int = 50
    screenshots_enabled: bool = False
    ignore_https_errors: bool = False
    browser_args: tuple[str, ...] = ()


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
    lifecycle_key_retention_days: int = 90
    """Lifecycle request keys outlive send keys by design. A send key only
    needs to cover a keystroke retry window (30 days is already generous);
    a lifecycle key guards a worktree that may legitimately sit at
    VERIFIED_PROD for weeks before anyone reaps it, and pruning it early
    would make an already-completed cleanup look un-attempted."""


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
    # Conservative by default. The only supported action asks the already
    # supervised agent process to stop gracefully; its host service manager
    # owns the single replacement process.
    self_heal_enabled: bool = False
    self_heal_action: str = "none"


@dataclass(frozen=True)
class NodeHealthConfig:
    enabled: bool = True
    probe_interval_seconds: float = 20.0
    probe_timeout_seconds: float = 3.0
    execution_down_after_failures: int = 2
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 300.0
    backoff_jitter_ratio: float = 0.2


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

    bootstrap_origin is the PUBLIC, machine-facing origin -- the one a
    machine being onboarded can actually reach. It exists because the
    operator's Dashboard hostname usually cannot be that origin: it sits
    behind Cloudflare Access, and a machine mid-enrollment has no Access
    session and no way to obtain one. Deriving the callback from the
    browser's Host header therefore hands every new machine an origin that
    answers its enrollment POST with an Access login page. Setting this
    pins the machine-side origin instead of inferring it, and suppresses
    the browser Host as a candidate entirely -- see controller_urls() in
    node_onboarding.py. Empty (the default) keeps the LAN behaviour, where
    the browser Host IS a good guess because the operator and the new
    machine are on the same network.

    It is an ORIGIN: scheme and host (and port), never a path. The
    Dashboard stays where it is; this only changes what machines are told.

    controller_ssh_public_key(_file) is the PUBLIC key the installer
    installs into the new node's authorized_keys so this controller can
    SSH in. A public key: safe in config, safe in the payload, useless to
    anyone who intercepts it."""
    enabled: bool = True
    enrollment_ttl_seconds: int = 900
    # How long a pairing handle stays usable. The handle is minted when the
    # helper DOWNLOAD starts, so this clock has to cover the operator
    # finding the file, clearing SmartScreen on an unsigned binary and
    # accepting UAC. The old 120s default expired before the helper ever
    # ran. Single-use is unchanged -- this widens the window for ONE
    # redemption, not the number of them.
    pairing_handle_ttl_seconds: int = 900
    controller_url: str = ""
    bootstrap_origin: str = ""
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
    health: NodeHealthConfig = NodeHealthConfig()


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
    browser: BrowserGatewayConfig = BrowserGatewayConfig()
    ui_workflow: UiWorkflowConfig = UiWorkflowConfig()
    worktree_janitor: WorktreeJanitorConfig = WorktreeJanitorConfig()
    prompt_delivery: PromptDeliveryConfig = PromptDeliveryConfig()
    work: WorkConfig = WorkConfig()
    session_lifecycle: SessionLifecycleConfig = SessionLifecycleConfig()
    session_knowledge: SessionKnowledgeConfig = SessionKnowledgeConfig()
    session_access: SessionAccessConfig = SessionAccessConfig()
    ask_chatgpt: AskChatGptConfig = AskChatGptConfig()
    nodes: NodesConfig = NodesConfig()
    queue: QueueConfig = QueueConfig()
    router: RouterConfig = RouterConfig()
    agents: AgentsConfig = AgentsConfig()
    llm_governor: LLMGovernorConfig = LLMGovernorConfig()
    submit: SubmitConfig = SubmitConfig()
    integration_loop: IntegrationLoopConfig = IntegrationLoopConfig()
    lifecycle: LifecycleConfig = LifecycleConfig()
    submit_watchdog: SubmitWatchdogConfig = SubmitWatchdogConfig()
    ai_usage: AiUsageConfig = AiUsageConfig()
    notes: NotesConfig = NotesConfig()
    auto_recovery: AutoRecoveryConfig = AutoRecoveryConfig()
    session_health: SessionHealthConfig = SessionHealthConfig()
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
        if not 1 <= max_attempts <= 6:
            raise ValueError("submit.*.max_enter_attempts must be between 1 and 6")
        if not 50 <= interval_ms <= 1000:
            raise ValueError("submit.*.enter_interval_ms must be between 50 and 1000")
        if not 0 <= fixed_count <= 6:
            raise ValueError("submit.*.fixed_enter_count must be between 0 and 6")
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
        max_total_enters=int(submit_raw.get("max_total_enters", submit_defaults.max_total_enters)),
        sweeper_interval_seconds=float(submit_raw.get("sweeper_interval_seconds", submit_defaults.sweeper_interval_seconds)),
        ttl_seconds=float(submit_raw.get("ttl_seconds", submit_defaults.ttl_seconds)),
        retry_agent_types=tuple(submit_raw.get("retry_agent_types", submit_defaults.retry_agent_types)),
    )
    if not 0.3 <= watchdog_config.poll_interval_seconds <= 0.5:
        raise ValueError("submit_watchdog.poll_interval_seconds must be between 0.3 and 0.5")
    if watchdog_config.timeout_seconds <= 0 or watchdog_config.sweeper_interval_seconds < 3:
        raise ValueError("submit_watchdog timeouts must be positive")
    if not 1 <= watchdog_config.max_enter_attempts <= 6:
        raise ValueError("submit_watchdog.max_enter_attempts must be between 1 and 6")
    if not 1 <= watchdog_config.max_total_enters <= 6:
        raise ValueError("submit_watchdog.max_total_enters must be between 1 and 6")
    if watchdog_config.ttl_seconds <= 0:
        raise ValueError("submit_watchdog.ttl_seconds must be positive")
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

    health_raw = nodes_raw.get("health", {})
    if not isinstance(health_raw, dict):
        raise ValueError("nodes.health must be a mapping")
    health_defaults = NodeHealthConfig()
    node_health_config = NodeHealthConfig(
        enabled=bool(health_raw.get("enabled", health_defaults.enabled)),
        probe_interval_seconds=float(health_raw.get("probe_interval_seconds", health_defaults.probe_interval_seconds)),
        probe_timeout_seconds=float(health_raw.get("probe_timeout_seconds", health_defaults.probe_timeout_seconds)),
        execution_down_after_failures=int(health_raw.get(
            "execution_down_after_failures", health_defaults.execution_down_after_failures)),
        backoff_base_seconds=float(health_raw.get("backoff_base_seconds", health_defaults.backoff_base_seconds)),
        backoff_max_seconds=float(health_raw.get("backoff_max_seconds", health_defaults.backoff_max_seconds)),
        backoff_jitter_ratio=float(health_raw.get("backoff_jitter_ratio", health_defaults.backoff_jitter_ratio)),
    )
    if node_health_config.probe_interval_seconds < 1:
        raise ValueError("nodes.health.probe_interval_seconds must be at least 1")
    if not 0 < node_health_config.probe_timeout_seconds < 5:
        raise ValueError("nodes.health.probe_timeout_seconds must be greater than 0 and less than 5")
    if node_health_config.execution_down_after_failures < 2:
        raise ValueError("nodes.health.execution_down_after_failures must be at least 2")
    if (node_health_config.backoff_base_seconds < 1 or
            node_health_config.backoff_max_seconds < node_health_config.backoff_base_seconds):
        raise ValueError("nodes.health backoff must be positive and max >= base")
    if not 0 <= node_health_config.backoff_jitter_ratio <= 0.5:
        raise ValueError("nodes.health.backoff_jitter_ratio must be between 0 and 0.5")

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
        self_heal_enabled = bool(entry.get("self_heal_enabled", False))
        self_heal_action = str(entry.get("self_heal_action", "none"))
        if self_heal_action not in {"none", "graceful_agent_restart"}:
            raise ValueError(f"nodes.remote[{index}].self_heal_action is unsupported")
        if self_heal_enabled and self_heal_action == "none":
            raise ValueError(f"nodes.remote[{index}] enables self-heal without an action")
        remote_nodes.append(RemoteNodeConfig(
            node_id=node_id,
            display_name=entry.get("display_name") or node_id,
            hostname=entry.get("hostname") or node_id,
            endpoint=endpoint,
            token_env=token_env,
            max_sessions=max_sessions,
            timeout_seconds=float(entry.get("timeout_seconds", 10.0)),
            self_heal_enabled=self_heal_enabled,
            self_heal_action=self_heal_action,
        ))
    # Scheme/host gate, as a SECOND pass over the parsed entries.
    #
    # A plaintext endpoint pointed at a public host would put that node's
    # bearer token on the wire in the clear on every request -- see
    # endpoint_policy for the rule. It runs after the loop above rather
    # than inside it so that every structural and cross-entry problem
    # (missing token_env, a duplicate node_id, a bad max_sessions) still
    # reports itself first: those are cheap and local, this one costs a
    # DNS lookup and would otherwise pre-empt them on an entry that is
    # invalid for a much more obvious reason.
    for index, remote in enumerate(remote_nodes):
        try:
            endpoint_policy.validate_node_endpoint(
                remote.endpoint, context=f"nodes.remote[{index}].endpoint")
        except endpoint_policy.EndpointPolicyError as exc:
            raise ValueError(str(exc)) from None

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
                               remote_connect=remote_connect_config, onboarding=onboarding_config,
                               health=node_health_config)

    def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
        value = int(os.environ.get(name, default))
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        return value

    def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
        value = float(os.environ.get(name, default))
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        return value

    def _env_bool(name: str, default: bool) -> bool:
        raw_value = os.environ.get(name)
        if raw_value is None:
            return default
        if raw_value.casefold() in {"1", "true", "yes", "on"}:
            return True
        if raw_value.casefold() in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{name} must be a boolean")

    governor_defaults = LLMGovernorConfig()
    governor_config = LLMGovernorConfig(
        global_max_concurrency=_env_int(
            "LLM_GLOBAL_MAX_CONCURRENCY", governor_defaults.global_max_concurrency),
        openrouter_max_concurrency=_env_int(
            "LLM_OPENROUTER_MAX_CONCURRENCY", governor_defaults.openrouter_max_concurrency),
        codex_max_concurrency=_env_int(
            "LLM_CODEX_MAX_CONCURRENCY", governor_defaults.codex_max_concurrency),
        claude_max_concurrency=_env_int(
            "LLM_CLAUDE_MAX_CONCURRENCY", governor_defaults.claude_max_concurrency),
        queue_wait_timeout_seconds=_env_float(
            "LLM_QUEUE_WAIT_TIMEOUT_SEC", governor_defaults.queue_wait_timeout_seconds, minimum=0.1),
        retry_max_attempts=_env_int(
            "LLM_RETRY_MAX_ATTEMPTS", governor_defaults.retry_max_attempts),
        retry_base_delay_seconds=_env_float(
            "LLM_RETRY_BASE_DELAY_SEC", governor_defaults.retry_base_delay_seconds),
        retry_max_delay_seconds=_env_float(
            "LLM_RETRY_MAX_DELAY_SEC", governor_defaults.retry_max_delay_seconds),
        retry_jitter=_env_bool("LLM_RETRY_JITTER", governor_defaults.retry_jitter),
        cooldown_429_seconds=_env_float(
            "LLM_429_COOLDOWN_SEC", governor_defaults.cooldown_429_seconds),
    )
    if governor_config.retry_max_delay_seconds < governor_config.retry_base_delay_seconds:
        raise ValueError("LLM_RETRY_MAX_DELAY_SEC must be >= LLM_RETRY_BASE_DELAY_SEC")

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
        browser=_load_browser_gateway_config(raw.get("browser", {})),
        ui_workflow=_load_ui_workflow_config(raw.get("ui_workflow", {})),
        worktree_janitor=_load_worktree_janitor_config(raw.get("worktree_janitor", {})),
        prompt_delivery=_load_prompt_delivery_config(raw.get("prompt_delivery", {})),
        work=_load_work_config(raw.get("work", {})),
        session_lifecycle=_load_session_lifecycle_config(raw.get("session_lifecycle", {})),
        session_knowledge=_load_session_knowledge_config(raw.get("session_knowledge", {})),
        session_access=_load_session_access_config(raw.get("session_access", {})),
        ask_chatgpt=_load_ask_chatgpt_config(raw.get("ask_chatgpt", {})),
        nodes=nodes_config,
        queue=_load_queue_config(raw.get("queue", {})),
        router=_load_router_config(raw.get("router", {})),
        agents=_load_agents_config(raw.get("agents", {})),
        llm_governor=governor_config,
        submit=submit_config,
        integration_loop=_load_integration_loop_config(raw.get("integration_loop", {})),
        lifecycle=_load_lifecycle_config(raw.get("lifecycle", {})),
        submit_watchdog=watchdog_config,
        ai_usage=_load_ai_usage_config(raw.get("ai_usage", {})),
        notes=_load_notes_config(raw.get("notes", {})),
        auto_recovery=_load_auto_recovery_config(raw.get("auto_recovery", {})),
        session_health=_load_session_health_config(raw.get("session_health", {})),
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
    handle_ttl = int(raw.get("pairing_handle_ttl_seconds", defaults.pairing_handle_ttl_seconds))
    if handle_ttl < 30 or handle_ttl > 3600:
        raise ValueError("nodes.onboarding.pairing_handle_ttl_seconds must be between 30 and 3600")
    bootstrap_origin = str(raw.get("bootstrap_origin", defaults.bootstrap_origin) or "").strip().rstrip("/")
    if bootstrap_origin:
        if not bootstrap_origin.startswith(("http://", "https://")):
            raise ValueError("nodes.onboarding.bootstrap_origin must start with http:// or https://")
        # An origin, not a URL. A path here would be silently concatenated
        # onto every machine-side route and produce 404s that look like the
        # controller is down.
        parsed = _urlsplit(bootstrap_origin)
        if not parsed.hostname:
            raise ValueError("nodes.onboarding.bootstrap_origin must include a hostname")
        if parsed.path or parsed.query or parsed.fragment:
            raise ValueError("nodes.onboarding.bootstrap_origin must be an origin "
                             "(scheme://host[:port]) with no path, query or fragment")

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
        pairing_handle_ttl_seconds=handle_ttl,
        controller_url=controller_url,
        bootstrap_origin=bootstrap_origin,
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


def _load_session_health_config(raw: object) -> SessionHealthConfig:
    if not isinstance(raw, dict):
        raw = {}
    defaults = SessionHealthConfig()
    watch = float(raw.get("watch_percent", defaults.watch_percent))
    prepare = float(raw.get("prepare_rollover_percent", defaults.prepare_rollover_percent))
    finish = float(raw.get("finish_rollover_percent", defaults.finish_rollover_percent))
    checkpoint = float(raw.get("checkpoint_only_percent", defaults.checkpoint_only_percent))
    cache_seconds = float(raw.get("git_probe_cache_seconds", defaults.git_probe_cache_seconds))
    for name, value in (("watch_percent", watch), ("prepare_rollover_percent", prepare),
                        ("finish_rollover_percent", finish), ("checkpoint_only_percent", checkpoint)):
        if not 0 <= value <= 100:
            raise ValueError(f"session_health.{name} must be 0-100")
    # Strictly increasing, because a non-monotonic table would make a band
    # unreachable -- a misconfiguration that silently never fires is worse
    # than a startup error.
    if not watch < prepare < finish < checkpoint:
        raise ValueError("session_health thresholds must be strictly increasing: "
                         "watch_percent < prepare_rollover_percent < finish_rollover_percent "
                         "< checkpoint_only_percent")
    if cache_seconds < 0:
        raise ValueError("session_health.git_probe_cache_seconds must be non-negative")
    return SessionHealthConfig(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        watch_percent=watch, prepare_rollover_percent=prepare,
        finish_rollover_percent=finish, checkpoint_only_percent=checkpoint,
        git_probe_enabled=bool(raw.get("git_probe_enabled", defaults.git_probe_enabled)),
        git_probe_cache_seconds=cache_seconds,
    )


def _load_integration_loop_config(raw: object) -> IntegrationLoopConfig:
    if not isinstance(raw, dict):
        raw = {}
    fallback_poll = float(raw.get("fallback_poll_seconds", IntegrationLoopConfig.fallback_poll_seconds))
    if fallback_poll < 0.5:
        raise ValueError("integration_loop.fallback_poll_seconds must be at least 0.5")
    return IntegrationLoopConfig(enabled=bool(raw.get("enabled", False)), fallback_poll_seconds=fallback_poll)


def _load_lifecycle_config(raw: object) -> LifecycleConfig:
    if not isinstance(raw, dict):
        raw = {}
    roots = raw.get("worktree_roots", [])
    if not isinstance(roots, list):
        raise ValueError("lifecycle.worktree_roots must be a list of paths")
    limit = int(raw.get("reconcile_limit", LifecycleConfig.reconcile_limit))
    if limit < 1:
        raise ValueError("lifecycle.reconcile_limit must be at least 1")
    main_branch = str(raw.get("main_branch", LifecycleConfig.main_branch)).strip()
    if not main_branch:
        raise ValueError("lifecycle.main_branch must be a non-empty branch name")
    environment = str(raw.get("environment", LifecycleConfig.environment)).strip()
    return LifecycleConfig(
        enabled=bool(raw.get("enabled", False)), main_branch=main_branch,
        environment=environment or LifecycleConfig.environment,
        worktree_roots=tuple(str(r) for r in roots), reconcile_limit=limit,
        allow_unverified_integration=bool(raw.get(
            "allow_unverified_integration", LifecycleConfig.allow_unverified_integration)),
    )


def _load_agents_config(agents_raw: object) -> AgentsConfig:
    if not isinstance(agents_raw, dict):
        agents_raw = {}
    roots_raw = agents_raw.get("skill_roots", []) or []
    if isinstance(roots_raw, str):
        roots_raw = [roots_raw]
    if not isinstance(roots_raw, list) or not all(isinstance(item, str) for item in roots_raw):
        raise ValueError("agents.skill_roots must be a list of paths")
    return AgentsConfig(
        enabled=bool(agents_raw.get("enabled", AgentsConfig.enabled)),
        skill_roots=tuple(roots_raw),
    )


def _load_router_config(router_raw: object) -> RouterConfig:
    """`router:` in config.yaml. Every field is validated, because a router
    whose batch size is 0 or whose interval is negative fails silently -- it
    just stops rescuing, which looks exactly like the bug it fixes."""
    if not isinstance(router_raw, dict):
        router_raw = {}
    interval = float(router_raw.get("rescue_interval_seconds", RouterConfig.rescue_interval_seconds))
    if interval < 1.0:
        raise ValueError("router.rescue_interval_seconds must be at least 1.0")
    batch = int(router_raw.get("rescue_batch_size", RouterConfig.rescue_batch_size))
    if batch < 1 or batch > 500:
        raise ValueError("router.rescue_batch_size must be between 1 and 500")
    max_spawned = int(router_raw.get("max_spawned_sessions", RouterConfig.max_spawned_sessions))
    if max_spawned < 0 or max_spawned > 100:
        raise ValueError("router.max_spawned_sessions must be between 0 and 100")
    budget = float(router_raw.get("dispatch_budget_seconds", RouterConfig.dispatch_budget_seconds))
    if not 1.0 <= budget <= 60.0:
        raise ValueError("router.dispatch_budget_seconds must be between 1 and 60")
    probe_limit = int(router_raw.get("probe_limit", RouterConfig.probe_limit))
    if not 1 <= probe_limit <= 25:
        raise ValueError("router.probe_limit must be between 1 and 25")
    snapshot_budget = float(router_raw.get("snapshot_budget_seconds",
                                           RouterConfig.snapshot_budget_seconds))
    if not 0.5 <= snapshot_budget <= 30.0:
        raise ValueError("router.snapshot_budget_seconds must be between 0.5 and 30")
    stale_snapshot = float(router_raw.get("stale_snapshot_max_age_seconds",
                                          RouterConfig.stale_snapshot_max_age_seconds))
    if not 0.0 <= stale_snapshot <= 3600.0:
        raise ValueError("router.stale_snapshot_max_age_seconds must be between 0 and 3600")
    return RouterConfig(
        enabled=bool(router_raw.get("enabled", RouterConfig.enabled)),
        rescue_enabled=bool(router_raw.get("rescue_enabled", RouterConfig.rescue_enabled)),
        spawn_enabled=bool(router_raw.get("spawn_enabled", RouterConfig.spawn_enabled)),
        max_spawned_sessions=max_spawned,
        default_runtime=str(router_raw.get("default_runtime", RouterConfig.default_runtime)),
        rescue_interval_seconds=interval,
        rescue_batch_size=batch,
        dispatch_budget_seconds=budget,
        probe_limit=probe_limit,
        snapshot_budget_seconds=snapshot_budget,
        stale_snapshot_max_age_seconds=stale_snapshot,
    )


def _load_queue_config(queue_raw: object) -> QueueConfig:
    if not isinstance(queue_raw, dict):
        queue_raw = {}
    poll_interval = float(queue_raw.get("poll_interval_seconds", QueueConfig.poll_interval_seconds))
    if poll_interval < 0.5:
        raise ValueError("queue.poll_interval_seconds must be at least 0.5")
    drain_batch_size = int(queue_raw.get("drain_batch_size", QueueConfig.drain_batch_size))
    if drain_batch_size < 1 or drain_batch_size > 500:
        raise ValueError("queue.drain_batch_size must be between 1 and 500")
    feeds_raw = queue_raw.get("project_feeds", [])
    if feeds_raw is None:
        feeds_raw = []
    if not isinstance(feeds_raw, list):
        raise ValueError("queue.project_feeds must be a list")
    feeds: list[ProjectFeedConfig] = []
    seen_lanes: set[str] = set()
    for index, item in enumerate(feeds_raw):
        if not isinstance(item, dict):
            raise ValueError(f"queue.project_feeds[{index}] must be a mapping")
        preferred = item.get("preferred_task_ids", [])
        if isinstance(preferred, str):
            preferred = [preferred]
        if not isinstance(preferred, list) or not all(isinstance(v, str) for v in preferred):
            raise ValueError(f"queue.project_feeds[{index}].preferred_task_ids must be a list of strings")
        feed = ProjectFeedConfig(
            project_id=str(item.get("project_id") or ""),
            lane=str(item.get("lane") or ""),
            registry_path=str(item.get("registry_path") or ""),
            owner=(str(item.get("owner")) if item.get("owner") is not None else None),
            preferred_task_ids=tuple(preferred),
            max_registry_bytes=int(item.get("max_registry_bytes", 5_000_000)),
            target_session=(str(item.get("target_session")) if item.get("target_session") is not None else None),
            target_node_id=(str(item.get("target_node_id")) if item.get("target_node_id") is not None else None),
            target_node_name=(str(item.get("target_node_name")) if item.get("target_node_name") is not None else None),
            allowed_agent_types=tuple(str(v) for v in (item.get("allowed_agent_types") or [])),
            required_capabilities=tuple(str(v) for v in (item.get("required_capabilities") or [])),
            infer_task_size=bool(item.get("infer_task_size", False)),
        )
        if feed.lane in seen_lanes:
            raise ValueError(f"duplicate queue.project_feeds lane: {feed.lane}")
        seen_lanes.add(feed.lane)
        feeds.append(feed)
    return QueueConfig(
        enabled=bool(queue_raw.get("enabled", False)),
        poll_interval_seconds=poll_interval,
        drain_enabled=bool(queue_raw.get("drain_enabled", False)),
        drain_batch_size=drain_batch_size,
        project_feeds=tuple(feeds),
    )


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


def _load_worktree_janitor_config(raw: object) -> WorktreeJanitorConfig:
    """Fail-closed: an unknown mode is a config ERROR, never silently
    downgraded. An operator who typed "auto" instead of "auto_execute" must be
    told, not left believing the janitor is enforcing (or that it is safe)."""
    if not isinstance(raw, dict):
        raw = {}
    mode = raw.get("mode", WorktreeJanitorConfig.mode)
    if mode not in ("observe_only", "suggest_only", "auto_execute"):
        raise ValueError("worktree_janitor.mode must be one of: observe_only, "
                         "suggest_only, auto_execute")
    roots = raw.get("allowed_roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise ValueError("worktree_janitor.allowed_roots must be a list of strings")
    for root in roots:
        if not root.startswith("/") and not root.startswith("~"):
            raise ValueError(f"worktree_janitor.allowed_roots entry {root!r} must be absolute")
        if root.strip() == "/":
            raise ValueError("worktree_janitor.allowed_roots may not contain '/'")
    ref = raw.get("integration_ref", WorktreeJanitorConfig.integration_ref)
    if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
        raise ValueError("worktree_janitor.integration_ref must be a non-empty ref name")
    allow_preserved = raw.get("allow_preserved_unmerged",
                              WorktreeJanitorConfig.allow_preserved_unmerged)
    if not isinstance(allow_preserved, bool):
        raise ValueError("worktree_janitor.allow_preserved_unmerged must be a boolean")
    globs = raw.get("extra_valuable_globs", [])
    if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
        raise ValueError("worktree_janitor.extra_valuable_globs must be a list of strings")

    def bounded(key: str, default: Any, low: float, high: float) -> Any:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"worktree_janitor.{key} must be a number")
        if not low <= value <= high:
            raise ValueError(f"worktree_janitor.{key} must be between {low} and {high}")
        return value

    grace = int(bounded("grace_seconds", WorktreeJanitorConfig.grace_seconds, 300, 2_592_000))
    floor = int(bounded("grace_floor_seconds", WorktreeJanitorConfig.grace_floor_seconds,
                        60, 2_592_000))
    if floor > grace:
        raise ValueError("worktree_janitor.grace_floor_seconds must not exceed grace_seconds")
    sweep_enabled = raw.get("sweep_enabled", WorktreeJanitorConfig.sweep_enabled)
    if not isinstance(sweep_enabled, bool):
        raise ValueError("worktree_janitor.sweep_enabled must be a boolean")
    sweep_roots = raw.get("repo_roots", [])
    if not isinstance(sweep_roots, list) or not all(isinstance(r, str) and r for r in sweep_roots):
        raise ValueError("worktree_janitor.repo_roots must be a list of strings")
    for root in sweep_roots:
        if not root.startswith("/") and not root.startswith("~"):
            raise ValueError(f"worktree_janitor.repo_roots entry {root!r} must be absolute")
    return WorktreeJanitorConfig(
        sweep_enabled=sweep_enabled,
        sweep_interval_seconds=float(bounded(
            "sweep_interval_seconds", WorktreeJanitorConfig.sweep_interval_seconds, 30, 86_400)),
        sweep_budget_seconds=float(bounded(
            "sweep_budget_seconds", WorktreeJanitorConfig.sweep_budget_seconds, 1, 3_600)),
        orphan_confirm_runs=int(bounded(
            "orphan_confirm_runs", WorktreeJanitorConfig.orphan_confirm_runs, 1, 100)),
        orphan_min_age_seconds=float(bounded(
            "orphan_min_age_seconds", WorktreeJanitorConfig.orphan_min_age_seconds, 0, 2_592_000)),
        repo_roots=tuple(sweep_roots),
        mode=mode, allowed_roots=tuple(roots), integration_ref=ref.strip(),
        allow_preserved_unmerged=allow_preserved, grace_seconds=grace,
        grace_floor_seconds=floor,
        max_evidence_age_seconds=float(bounded(
            "max_evidence_age_seconds", WorktreeJanitorConfig.max_evidence_age_seconds, 5, 3_600)),
        max_candidates_per_run=int(bounded(
            "max_candidates_per_run", WorktreeJanitorConfig.max_candidates_per_run, 1, 5_000)),
        timeout_seconds=float(bounded(
            "timeout_seconds", WorktreeJanitorConfig.timeout_seconds, 1, 300)),
        extra_valuable_globs=tuple(globs))


def _load_prompt_delivery_config(raw: object) -> PromptDeliveryConfig:
    """Fail-closed validation: an unknown mode is a config ERROR, never
    silently downgraded to advisory. An operator who typed "enforced" must
    be told, not quietly left unprotected."""
    if not isinstance(raw, dict):
        raw = {}
    mode = raw.get("mode", PromptDeliveryConfig.mode)
    if mode not in ("advisory", "enforce"):
        raise ValueError("prompt_delivery.mode must be one of: advisory, enforce")
    require = raw.get("require_acceptance", PromptDeliveryConfig.require_acceptance)
    if not isinstance(require, bool):
        raise ValueError("prompt_delivery.require_acceptance must be a boolean")
    timeout = raw.get("acceptance_timeout_seconds", PromptDeliveryConfig.acceptance_timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ValueError("prompt_delivery.acceptance_timeout_seconds must be a number")
    if not 0.2 <= float(timeout) <= 60.0:
        raise ValueError("prompt_delivery.acceptance_timeout_seconds must be between 0.2 and 60")
    poll = raw.get("acceptance_poll_interval_seconds",
                   PromptDeliveryConfig.acceptance_poll_interval_seconds)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)):
        raise ValueError("prompt_delivery.acceptance_poll_interval_seconds must be a number")
    if not 0.05 <= float(poll) <= float(timeout):
        raise ValueError("prompt_delivery.acceptance_poll_interval_seconds must be between "
                         "0.05 and acceptance_timeout_seconds")
    lines = raw.get("acceptance_capture_lines", PromptDeliveryConfig.acceptance_capture_lines)
    if isinstance(lines, bool) or not isinstance(lines, int) or not 5 <= lines <= 500:
        raise ValueError("prompt_delivery.acceptance_capture_lines must be an integer 5..500")
    return PromptDeliveryConfig(mode=mode, require_acceptance=require,
                                acceptance_timeout_seconds=float(timeout),
                                acceptance_poll_interval_seconds=float(poll),
                                acceptance_capture_lines=lines)


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


def _load_ui_workflow_config(raw: object) -> UiWorkflowConfig:
    """Strict, fail-closed, same posture as the browser loader.

    A malformed skill list is an error rather than a silent empty tuple: a
    typo there would make the policy report every skill missing, which reads
    as "nothing is installed" and is exactly the kind of quiet wrong answer
    this section exists to avoid.
    """
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get("enabled", UiWorkflowConfig.enabled)
    if not isinstance(enabled, bool):
        raise ValueError("ui_workflow.enabled must be a boolean")
    skills = raw.get("installed_skills", [])
    if not isinstance(skills, list) or not all(isinstance(x, str) and x.strip() for x in skills):
        raise ValueError("ui_workflow.installed_skills must be a list of non-empty strings")
    default_project = raw.get("default_project", "")
    if not isinstance(default_project, str):
        raise ValueError("ui_workflow.default_project must be a string")
    return UiWorkflowConfig(
        enabled=enabled,
        installed_skills=tuple(x.strip() for x in skills),
        default_project=default_project.strip(),
    )


def _load_browser_gateway_config(raw: object) -> BrowserGatewayConfig:
    """Strict, fail-closed validation, for the same reason repo_read's is:
    a caps section nobody can trust is worse than no caps at all.

    Note there is no key here that removes a blocked scheme or unblocks an
    instance-metadata address. Those live in browser_safety.py and are
    deliberately unreachable from config -- an operator can widen which
    HOSTS are reachable, never which SCHEMES are."""
    if not isinstance(raw, dict):
        raw = {}

    def flag(key: str, default: bool) -> bool:
        value = raw.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"browser.{key} must be a boolean")
        return value

    def bounded(key: str, default: int, low: int, high: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"browser.{key} must be an integer")
        if not low <= value <= high:
            raise ValueError(f"browser.{key} must be between {low} and {high}")
        return value

    def seconds(key: str, default: float, low: float, high: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"browser.{key} must be a number")
        if not low <= float(value) <= high:
            raise ValueError(f"browser.{key} must be between {low} and {high}")
        return float(value)

    def patterns(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
            raise ValueError(f"browser.{key} must be a list of non-empty strings")
        return tuple(value)

    for key in ("executable", "artifact_dir"):
        value = raw.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"browser.{key} must be a string")

    args = raw.get("browser_args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) and a for a in args):
        raise ValueError("browser.browser_args must be a list of non-empty strings")

    navigation = seconds("navigation_timeout_seconds",
                         BrowserGatewayConfig.navigation_timeout_seconds, 1.0, 120.0)
    hard = seconds("hard_timeout_seconds", BrowserGatewayConfig.hard_timeout_seconds, 2.0, 300.0)
    if hard <= navigation:
        # The hard kill exists to catch what the per-operation timeout
        # cannot. Set at or below it, it fires first and every slow-but-fine
        # page becomes a spurious BROWSER_TIMEOUT.
        raise ValueError("browser.hard_timeout_seconds must be greater than "
                         "browser.navigation_timeout_seconds")
    return BrowserGatewayConfig(
        enabled=flag("enabled", BrowserGatewayConfig.enabled),
        executable=str(raw.get("executable", "") or ""),
        headless=flag("headless", BrowserGatewayConfig.headless),
        viewport_width=bounded("viewport_width", BrowserGatewayConfig.viewport_width, 200, 4_096),
        viewport_height=bounded("viewport_height", BrowserGatewayConfig.viewport_height, 200, 4_096),
        navigation_timeout_seconds=navigation,
        hard_timeout_seconds=hard,
        allow_loopback=flag("allow_loopback", BrowserGatewayConfig.allow_loopback),
        allow_private_networks=flag("allow_private_networks",
                                    BrowserGatewayConfig.allow_private_networks),
        allow_url_patterns=patterns("allow_url_patterns"),
        deny_url_patterns=patterns("deny_url_patterns"),
        artifact_dir=str(raw.get("artifact_dir", "") or ""),
        keep_artifacts=bounded("keep_artifacts", BrowserGatewayConfig.keep_artifacts, 0, 1_000),
        screenshots_enabled=flag("screenshots_enabled",
                                 BrowserGatewayConfig.screenshots_enabled),
        ignore_https_errors=flag("ignore_https_errors",
                                 BrowserGatewayConfig.ignore_https_errors),
        browser_args=tuple(args),
    )


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
    lifecycle_days = int(maintenance_raw.get(
        "lifecycle_key_retention_days", MaintenanceConfig.lifecycle_key_retention_days,
    ))
    if interval < 60:
        raise ValueError("maintenance.interval_seconds must be at least 60")
    if audit_retention < 1:
        raise ValueError("maintenance.audit_retention must be at least 1")
    if action_retention < 1:
        raise ValueError("maintenance.action_retention must be at least 1")
    if idempotency_days < 1:
        raise ValueError("maintenance.idempotency_key_retention_days must be at least 1")
    if lifecycle_days < 1:
        raise ValueError("maintenance.lifecycle_key_retention_days must be at least 1")
    return MaintenanceConfig(
        interval_seconds=interval, audit_retention=audit_retention,
        action_retention=action_retention, idempotency_key_retention_days=idempotency_days,
        lifecycle_key_retention_days=lifecycle_days,
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
