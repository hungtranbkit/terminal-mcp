# Multi-node session management

**Status: backend + tests + dashboard UI implemented and live-smoke-tested
on this host (Dell, node id `local`). Not yet live-verified against a real
second machine** — no M910 (or any other second host) was reachable from
this session. Everything short of "a real second box actually joined the
fleet" has been built and exercised for real: a real `terminal-node-agent`
subprocess on a real port, real HTTP round-trips, real tmux sessions moved
between two node identities in the same test suite. See **Known
limitations** and **Bringing up the M910** below for exactly what remains.

## What this is

Converts Terminal MCP from a single-host tmux manager into a Controller
(this Dell: dashboard + MCP/API + node registry + scheduler) with
lightweight `terminal-node-agent` processes on worker nodes. ChatGPT/Claude
Code's own experience is **unchanged** — the same session names, the same
tool calls — routing to whichever node actually holds a session is
transparent. The one deliberate visible addition is additive: every
session-shaped response now also carries `node_id`/`node_name`.

**Phase A/B guarantee, and the reason every existing test still passes
unmodified**: with only the local node registered (today's actual
deployment — nothing else configured), every routed operation resolves to
the exact same `TerminalService` instance the dashboard/MCP tools already
used directly. `ControllerService` adds routing; it never re-implements
tmux/permission/audit logic TerminalService already owns.

## Architecture

```
                     ┌─────────────────────────────┐
  ChatGPT/Claude ──► │   MCP tools (mcp_app.py)     │
       Code          │   Dashboard (dashboard.py)   │
                     │            │                  │
                     │            ▼                  │
                     │   ControllerService            │  (controller.py)
                     │   ├─ NodeRegistry (sqlite)      │  (node_registry.py)
                     │   ├─ scheduler.choose_node()    │  (scheduler.py)
                     │   └─ NodeClient per node         │  (node_client.py)
                     │        ├─ LocalNodeClient ──────┼──► TerminalService (this host)
                     │        └─ RemoteNodeClient ─────┼──► HTTP + bearer token
                     └─────────────────────────────┘         │
                                                                ▼
                                                   ┌─────────────────────────┐
                                                   │  terminal-node-agent      │ (node_agent.py)
                                                   │  (runs ON the worker node) │
                                                   │  wraps its OWN TerminalService│
                                                   │  pushes heartbeat ─────────┼──► Controller's
                                                   └─────────────────────────┘   /dashboard/api/nodes/
                                                                                  {id}/heartbeat
```

The local node is a node like any other in every piece of business logic —
only its **transport** differs (`LocalNodeClient`: in-process, zero network
hop vs `RemoteNodeClient`: HTTP + bearer token). No code path special-cases
"is this the local node" except the transport construction itself.

## Files

| Piece | File |
|---|---|
| Node data model, overload/heartbeat threshold dataclasses, shared `node_to_dict` | `terminal_mcp/node_models.py` |
| Node registry (sqlite) + the overload heuristic | `terminal_mcp/node_registry.py` |
| Auto-placement scheduler (pure, deterministic) | `terminal_mcp/scheduler.py` |
| `NodeClient` protocol + Local/Remote implementations | `terminal_mcp/node_client.py` |
| Controller: routing, resolution, fleet views, move workflow | `terminal_mcp/controller.py` |
| `terminal-node-agent` (runs on a worker node) | `terminal_mcp/node_agent.py` |
| Dashboard node routes + Nodes admin page | `terminal_mcp/dashboard.py` (`/dashboard/api/nodes*`, `/dashboard/nodes`) |
| MCP tools (`terminal_list_nodes`/`terminal_node_status`/`terminal_node_sessions`, `node=` on `terminal_create_session`) | `terminal_mcp/mcp_app.py` |
| Config schema (`nodes:` section) | `terminal_mcp/config.py` |
| `terminal-mcp-doctor nodes` | `terminal_mcp/doctor.py` |
| Deploy script + systemd unit for a worker node | `deploy/install-node-agent.sh`, `deploy/systemd/terminal-node-agent.service.example` |
| Tests | `tests/test_node_registry.py`, `test_scheduler.py`, `test_node_client.py`*, `test_controller.py`, `test_node_agent.py`, `test_dashboard_nodes.py`, `test_move_session.py`, `test_config_nodes.py`, `test_doctor_nodes.py`, plus a real end-to-end smoke test in `test_transports.py` |

\* `node_client.py`'s `RemoteNodeClient` is exercised directly by
`test_node_agent.py` (a real HTTP round-trip against a real
`terminal-node-agent` ASGI app) rather than a separate unit-test file —
there's no meaningful way to test an HTTP client's wire behavior without a
real server on the other end.

## The overload heuristic

`node_registry.classify_capacity()` — a pure function over a plain dict, so
it's testable without any I/O — turns smoothed metrics into
`healthy`/`busy`/`overloaded`/`unknown`:

- RAM/CPU/swap are **EWMA-smoothed** at every heartbeat write (default
  `alpha=0.4`) — a single noisy spike is damped to well under half its own
  swing, never immediately flips a node's status.
- CPU/load being high must be **sustained** for `sustained_seconds`
  (default 300s, tracked as real elapsed wall-clock time across
  heartbeats via `high_cpu_since`/`high_load_since` columns, correct
  however often heartbeats actually arrive) before escalating from `busy`
  to `overloaded`.
- Swap usage alone **never** triggers `overloaded` — only combined with
  RAM already above the busy threshold (a node can legitimately use some
  swap while otherwise healthy).
- Disk free below `disk_free_overloaded_percent` (default 10%) is an
  immediate `overloaded` — no smoothing, since a filling disk is slow and
  monotonic; smoothing it only adds lag to a real warning.

All thresholds are operator-configurable via `config.yaml`'s
`nodes.overload_thresholds`/`nodes.heartbeat` (see `test_config_nodes.py`
for the exact schema; unset = the built-in defaults above, i.e. today's
deployment is unaffected by this section existing).

A node's `online`/`degraded`/`offline` status is **never persisted** —
always derived from `last_heartbeat_at` age at read time
(`degraded_after_seconds=60`, `offline_after_seconds=180` by default), so
a registry row can sit quiet for days and still correctly report
`offline` without any background sweep. The row itself is **never
deleted** just because a node went quiet (task's own "session registry
không biến mất ngay" requirement) — only the derived label changes, and it
flips straight back to `online` the instant a fresh heartbeat arrives.

## Scheduler (`node="auto"`)

`scheduler.choose_node()` is a pure function: eligibility gate (online,
not draining, not overloaded, has the required `agent_type` capability —
`"shell"` is always eligible since every node that can run tmux at all can
host one, `claude`/`codex` require the node to have actually reported that
launcher — under `max_sessions`, above the disk floor), then deterministic
lexicographic scoring: `(RAM headroom, CPU headroom, -session count,
node_id)` — node_id is the LAST tiebreak, purely for reproducibility on a
genuine tie, never a meaningful ranking signal. The exact same input node
list always produces the exact same placement decision — no randomness, so
a placement is always explainable (`PlacementResult.reason`) and
reproducible in a test. `required_platform` ("linux"/"windows"), when
given, filters to that platform before scoring -- `terminal_create_session`
exposes this as an optional `platform` parameter, on top of the existing
`agent_type` filter (unaffected, unchanged).

## Windows node support

**Status: backend + tests + installer implemented. Not live-verified
against a real Windows machine** -- no Windows host was reachable from
this session either. Everything short of "a real ConPTY spawn on real
Windows" has been exercised for real on this Linux dev host: a real
child process driving every session operation through the exact same
`TerminalService` business logic Linux uses, a real WebSocket relay to a
real second process, real capability-detection logic. See each
subsection below for exactly what is/isn't verified.

### The abstraction: `SessionBackend`

`session_backend.py` defines `SessionBackend` as a `Protocol` matching
`TmuxClient`'s own existing method surface EXACTLY (`list_sessions`,
`get_session`, `capture_lines`, `send_text`, `send_keys`, `new_session`,
`detach_session`, `kill_session`, `exit_copy_mode`). `TmuxClient` is not
modified at all to "implement" it -- Python Protocols are structural, so
it already satisfies the Protocol by having those exact methods
(confirmed: `isinstance(TmuxClient(), SessionBackend) is True`).

`TerminalService.__init__`'s `tmux` parameter is now typed
`SessionBackend | None` instead of `TmuxClient | None` -- a pure type-
annotation change, zero runtime behavior change for any existing caller
(`self.tmux = tmux or TmuxClient()` is untouched). This is the whole
reason Windows support needed **no changes to core.py's actual business
logic**: every permission check, audit record, redaction, kill/reopen-
metadata capture, and the reliable-submission verification state machine
(adapters.py) already operate purely in terms of session names, plain-
text capture lines, PIDs, and opaque identity strings -- never anything
tmux-specific. A `WindowsSessionBackend`-backed `TerminalService` runs
the identical code path. Proven directly, not just asserted: `tests/
test_windows_terminal_service_integration.py` constructs a real
`TerminalService(config, tmux=WindowsSessionBackend(...))` and drives
create/status/tail/send (through the FULL identity-pinning + delivery-
state verification stack)/kill/reopen through it, plus confirms
permission denial (`INPUT_DISABLED`) is enforced identically to the
tmux-backed case.

### `WindowsSessionBackend` (windows_backend.py)

A ConPTY-attached persistent PowerShell/cmd process **per session**,
managed entirely by this one backend instance (no separate server
process the way tmux has one -- see "Known limitations" below for
exactly what that means). A background reader thread per session drains
output into a bounded ring buffer continuously, independent of whether
any WebSocket viewer is attached -- this is what makes "disconnect
browser không kill process" real: closing a viewer only unregisters it
from that buffer/live-feed, never touches the underlying process,
exactly like tmux `detach-client`. `kill_session` genuinely terminates
the process tree, freeing its RAM, exactly like tmux `kill-session`.

`capture_lines` returns the buffered history PLUS the still-in-progress
trailing line (no newline yet -- e.g. a prompt waiting for input) --
without this, the reliable-submission verification poll (which diffs a
pre-Enter vs post-Enter capture) could see two identical snapshots
across a send that only ever touched the buffered partial line. A real
bug in the reader loop's own EOF detection (conflating "nothing to read
within this poll's timeout" with "the process actually exited",
permanently stopping the reader after the first idle gap) was caught and
fixed by this backend's own test suite -- see `windows_backend.py`'s
`_reader_loop` for the fix and the exact failure mode it prevents.

The real `pywinpty` import (`_default_process_factory`) is LAZY -- only
reached when actually spawning a session -- so this whole module imports
cleanly on Linux. Every test instead injects `_FakePty`
(tests/test_windows_backend.py), a REAL POSIX-pty-backed child process
satisfying the exact same `PtyProcessLike` shape pywinpty's own
`PtyProcess` has -- so the backend's own logic (registry, buffering,
identity, attach/detach, kill, path validation) is exercised against a
real running process, not a mock of this module's own behavior. Only the
actual `pywinpty`/ConPTY call itself is untested here.

Windows path validation: `validate_windows_cwd`/`validate_windows_session_
name` are a defense-in-depth SHAPE check (drive-rooted, no UNC, no NUL)
tested with `PureWindowsPath`-equivalent string logic, decoupled from any
real filesystem (there is no `C:\` to check against on this host). The
REAL containment/existence check is `resolve_cwd` (lifecycle.py) --
completely unmodified, reused as-is: it's built entirely on `pathlib`,
which becomes a real `WindowsPath` automatically when this code actually
runs on Windows, so symlink-escape/allowed-roots protection already
applies correctly there with zero Windows-specific code. `new_session`
deliberately does NOT re-run the shape check on `resolve_cwd`'s own
output (a real caught bug during development: doing so would incorrectly
reject `resolve_cwd`'s valid output on this Linux test host, and on real
Windows it would just be redundant).

Command injection: every process spawn is a plain argv LIST handed
directly to the process factory (`pywinpty`/`subprocess`), never a
formatted shell string, never `shell=True` -- the exact same discipline
`TmuxClient._run` already uses for every tmux invocation, extended to
this backend rather than a new convention.

### Metrics (host_metrics.py)

`collect()` now dispatches on `sys.platform`: unchanged `/proc`-based
Linux path, or a new Windows path using `ctypes` + kernel32
(`GlobalMemoryStatusEx` for RAM/page-file, `GetSystemTimes` for a two-
sample CPU% delta) -- still pure stdlib, no psutil, matching this
project's existing "no new dependency" discipline on both platforms.
Windows has no native load-average concept; `load1`/`load5`/`load15` are
always `None` there (the overload heuristic already treats a `None`
metric as simply not contributing to that check, never as zero). Windows
"swap" is approximated from page-file commit accounting -- not an exact
match to Linux's own separate-partition swap semantic, documented as an
approximation. **Not live-verified**: `ctypes.windll` doesn't exist on
this Linux host at all, so only the DISPATCH logic and the CPU%-delta
MATH are tested (via monkeypatching); the real Win32 API calls
themselves are untested here (`tests/test_host_metrics.py`).

### Capability reporting

Every node's heartbeat now carries `platform` ("linux"/"windows"),
`session_backend` ("tmux"/"windows_pty"), `shell_capabilities` (e.g.
`("powershell", "cmd")`), and `wsl_available` (bool) -- new `nodes` table
columns (`node_registry.py`, added via the same "PRAGMA table_info, ALTER
TABLE ADD COLUMN only if missing" idiom `bindings.py`/`audit.py` already
use, verified live against the real, already-populated production
`nodes.db` on this host). `claude_available`/`codex_available` are
derived at the display layer from `agent_types` rather than stored
separately (same information, no duplicate source of truth).

`agent_availability.py` is a new, small module: `available_agent_types()`
checks `shutil.which()` for each configured launcher, returned only if it
actually resolves. This fixes a REAL, pre-existing gap on Linux too (not
just a new Windows requirement): before this, `agent_types` was built
from `config.session_lifecycle.launch_commands` alone -- a node whose
operator configured `claude: claude` but never installed the CLI was
still reported (and scheduled) as claude-capable, only failing later at
actual launch. Applied identically on every heartbeat path (local,
`node_agent.py`, `windows_agent.py`, `doctor.py`).

### Open Terminal for a remote node

`node_agent.py` gained a `/v1/ws/terminal` WebSocket route (bearer-token
authenticated, same shared secret as its other routes) -- backend-aware:
a tmux-backed node reuses `webterm.py`'s existing `WebTerminalProcess`
(`tmux attach-session`) completely unmodified; a Windows node uses a new
`WindowsTerminalViewer` (`windows_webterm.py`) implementing the identical
`read/write/resize/alive/close` shape, so `webterm.py`'s own
`pump_websocket` (the actual bidirectional bridge and wire protocol) is
reused UNCHANGED for both -- never a second, backend-specific pump.

`dashboard.py`'s existing (local-only, before this) `/dashboard/ws/
terminal` route now falls back to resolving the session via the
controller when it isn't local, and (if found on a remote, online node)
proxies the browser's WebSocket to that node's own `/v1/ws/terminal`
via a real outbound `websockets.connect()` -- this generalizes to ANY
remote node (Linux or Windows), not only Windows; Open Terminal for a
remote Linux node had the exact same gap before this. Read authorization
for the session must already be settled by the LOCAL (controller-side)
whitelist/grant check before the remote fallback is even attempted (the
existing `terminal_web_terminal_access` call already does this, and only
falls through to the remote lookup on `SESSION_NOT_FOUND` specifically,
never on `ACCESS_DENIED`); input authorization for a remote session uses
`revalidate_identity=False` (same coarse signal the discovery endpoints
already use for this exact reason) since P0-2 identity re-pinning needs
this node's own tmux -- a statically-whitelisted remote session is
unaffected (that check short-circuits before identity ever matters);
only a grant-based (non-whitelisted) remote session's input check is
coarser than the local case.

**This is the one piece verified against a REAL second process, not just
an in-process fake** (`tests/test_remote_webterm_proxy.py`): a real
`terminal-node-agent` subprocess on a real port, a real outbound
`websockets.connect()` from the dashboard's own relay code, real
bidirectional byte frames -- exactly the piece where a subtly-wrong
assumption about the `websockets` library's own API could have gone
uncaught by a pure unit test.

### `terminal-windows-node-agent` (windows_agent.py)

Reuses `node_agent.py`'s `build_node_agent`/`_heartbeat_loop` completely
unmodified -- the only Windows-specific code is which `SessionBackend`
`TerminalService` is constructed with, and the capability values this
process reports. `detect_shell_capabilities()`/`detect_wsl_available()`
use `shutil.which()` (same mechanism as agent_availability.py), which
works correctly on any OS -- confirmed on THIS Linux host: both correctly
report nothing found (no `powershell.exe`/`pwsh.exe`/`cmd.exe`/`wsl.exe`
here), real evidence rather than an assumption. A real subprocess smoke
test (`tests/test_windows_agent.py`) confirms the full HTTP/heartbeat
surface starts and survives an unreachable controller AND a real (and,
on this host, expected) `pywinpty` import failure on session-create
without crashing the process -- everything short of an actual Windows
session spawn.

`deploy/install-node-agent.ps1` mirrors the Linux install script's own
steps (repo checkout, venv, `pip install -e .[windows]`, token
generation, config.yaml bootstrap, printed controller-side instructions)
plus a Windows Scheduled Task registration (`Register-ScheduledTask`,
`AtLogOn` trigger, auto-restart settings) for auto-start-after-reboot/
auto-reconnect. **Not executed anywhere** -- no PowerShell interpreter is
available in this environment at all, so this script could only be
manually, carefully reviewed for syntax correctness (and one real bug --
an ambiguous string-concatenation expression passed to `Write-Warning`
-- was caught and fixed during that review), never actually run. Treat
it as a strong first draft that needs a real dry run on Windows before
being trusted unattended.

### Dashboard UI

The Nodes admin page (`/dashboard/nodes`) now shows an OS badge (🐧/🪟)
and a capability line (session_backend, claude/codex/WSL availability)
per node card and in the detail view, plus a "+ Thêm node" onboarding
flow: a short form (node id/hostname/endpoint/platform) that calls a new
`/dashboard/api/node/generate-onboarding` route, which generates a fresh
token server-side and returns the exact install command + config.yaml
block + env var to copy -- shown once, never persisted, never logged.
This never registers a node itself (confirmed by its own test) -- the
operator still does the same manual config.yaml + env var + safe-restart
steps as any other remote node onboarding.

### Windows known limitations

- **Not live-verified against a real Windows machine** -- see each
  subsection above for exactly what could and couldn't be exercised
  without one.
- **No separate persistent server the way tmux has one.** A session's
  process is a child of this ONE node-agent process; if that process
  itself is killed/crashes (not a browser disconnect -- the AGENT
  process), its sessions' survival depends on OS-level process-group
  semantics this project does not attempt to control and could not
  verify. Treat a Windows node-agent restart as disruptive to that
  node's own sessions until verified otherwise on real hardware.
- **`pane_current_command`/`pane_current_path` are approximations**, not
  a native OS query the way tmux's own `#{pane_current_command}` is --
  they report the launched command/cwd, not necessarily the CURRENT
  foreground child process or directory if the shell has since `cd`'d or
  launched something else.
- **`wsl_tmux` is reported as informational only** (`wsl_available`) --
  no actual WSL-backed SessionBackend was built (task's own "không được
  bắt buộc WSL" -- native Windows always has its own working backend
  regardless); building one would need its own live verification this
  environment cannot provide either.
- **The PowerShell installer is unexecuted** -- manually reviewed only,
  see its own subsection above.

## LAN discovery + remote connect (Nodes page "Connect Node")

One-click node onboarding, added on top of everything above without
changing any of it — `ControllerService.register_remote_node` (the same
method `server_http.py`'s own config.yaml-driven startup loop already
called) is now ALSO called directly from live dashboard routes, so a
node can be connected at runtime, no config.yaml edit + restart required.

**Files**: `lan_discovery.py` (subnet enumeration + scan engine),
`remote_connect.py` (SSH target/argv/host-key-pinning/bootstrap/Windows-
manual-fallback), `connection_store.py` (durable connect metadata + a
0600 bearer-token file per node, never the secret itself in the sqlite
row), plus new routes/UI in `dashboard.py` (`/dashboard/api/nodes/
discovery/*`, `/dashboard/api/nodes/connect/*`, a "Connect Node" panel on
`/dashboard/nodes`).

**Discovery**: pure stdlib + asyncio, no new dependency. Subnets come
from parsing this host's own `ip -o -4 addr show`/`ip -o link show up`
(UP, non-loopback NICs only), filtered to RFC1918+link-local
(`lan_discovery.is_lan_scannable`) and capped in size
(`nodes.discovery.max_hosts_per_scan`, default 512) — a subnet too big to
scan safely is skipped entirely, never silently narrowed. Online-host
evidence is the kernel's own ARP/neighbor table (`ip neigh show`,
read-only) plus a FIXED, small TCP-connect probe set (node-agent port +
22/5985/5986 — never a port sweep), bounded by a concurrency semaphore, a
per-host timeout, and an overall wall-clock budget
(`asyncio.wait_for`). Exactly one scan runs at a time, rate-limited by a
cooldown between scans. Results are classified Already connected /
Connectable (agent's own `/v1/health` answered) / Needs setup (SSH or
WinRM port open, no agent) / Unknown (ARP-alive, nothing else) — never a
fake state invented beyond what was actually observed.

**Connect methods** (all converge on the same `register_remote_node` +
`ConnectionStore.save`/`write_token` pair):
- **Add by Agent Token** — the node-agent is already running (deployed
  earlier by hand, or by a discovery "Connect" click on a Connectable
  row); the operator pastes its existing bearer token; this route
  verifies it with a real `/v1/health` call before ever registering.
- **Add Remote SSH** (`transport_type=lan_ssh`) / **Add via Cloudflare
  Tunnel** (`transport_type=cloudflare_ssh`) — same SSH machinery, only
  the transport differs: `cloudflare_ssh` uses `-o
  ProxyCommand="cloudflared access ssh --hostname %h"`, a FIXED constant
  template (OpenSSH's own `%h` token, substituted by ssh itself — never
  the operator's hostname spliced into that shell-parsed string). Host
  key: `Test Connection` runs `ssh-keyscan` (no full handshake) and
  returns the presented `SHA256:` fingerprint; a NEW key requires an
  explicit `Trust & pin` click before anything else proceeds; a CHANGED
  key hard-fails (`host_key_mismatch`) with no auto-accept path at all,
  ever. Bootstrap runs ONE fixed, server-authored script
  (`remote_connect.BOOTSTRAP_SCRIPT_LINUX`) over that pinned connection —
  the browser sends only structured fields (`node_id`, `controller_url`,
  a freshly server-generated token), never shell text; a password
  credential is fed through a small pty-driven prompt-answer helper (no
  paramiko/sshpass dependency), never logged or persisted. On success the
  new agent's own `/v1/health` is polled for real before it's ever
  registered.
- **Windows** — always returns manual copy-paste instructions
  (`remote_connect.windows_bootstrap_guidance`, pointing at the same
  `deploy/install-node-agent.ps1` config-driven onboarding already uses).
  This project has no real Windows host to verify a live WinRM/
  PowerShell-remoting install against (same honesty policy as the
  "Windows node support" section above) — it never claims to have done
  one.

**A real bug found and fixed while building this**: `dashboard.py`'s
`node_heartbeat` route authenticates an inbound push by re-reading an env
var named `TERMINAL_MCP_NODE_TOKEN_<NODE_ID>` — but only
`node_generate_onboarding` (not `node_heartbeat`'s own lookup) replaced
`-` with `_` in that name, so a hyphenated `node_id` (e.g. `m-910`) would
generate one env var at onboarding time and look up a different, invalid
one at heartbeat time, silently rejecting every heartbeat from that node
forever. Fixed by centralizing the naming convention in one function
(`node_token_env_var`), used by both call sites plus this feature's own
routes (which set that same env var in-process at connect time, so a
node's heartbeat verifies immediately — no separate manual `export`
step). `server_http.py`'s startup also re-registers every previously-
connected node (from `ConnectionStore`, using its saved 0600 token file —
no credential re-entry) and re-sets this same env var, so a controller
restart never loses a discovery/SSH-connected node's ability to receive
heartbeats.

**Cloudflare-tunnel SSH's own real limitation, disclosed rather than
hidden**: after bootstrapping a node over `cloudflared access ssh`, the
CONTROLLER still needs an ordinary `http(s)://host:port` to reach that
node's own agent afterward for status/tail/send/kill/etc — SSH-only
Cloudflare Access does not also proxy the agent's separate HTTP port.
The bootstrap route requires an explicit `agent_endpoint_host` for this
transport (a direct LAN/VPN address, or a second Cloudflare Access TCP
application the operator has already configured for that port) rather
than silently guessing or failing to mention it — this feature has no
Cloudflare account-API integration anywhere and cannot auto-provision
that second Access application itself.

**Security model**: every point from this feature's own task spec, as
implemented — see `remote_connect.py`'s own module docstring for the
full, itemized rationale (SSRF-safe validation on every hostname/IP,
argv-only subprocess calls with exactly one fixed shell-parsed string
containing no user bytes, credential never persisted/logged, host-key
pin-not-auto-trust, server-templated bootstrap script only). CSRF/origin
+ Cloudflare Access guards are the SAME `_mutation_guard`/`_read_guard`
every other dashboard route already uses — no separate auth model for
this feature.

**Not done**: a dedicated queryable audit table for connect attempts
(logged via the same structured `_log.info`/`_log.warning` calls the
existing node routes already use, redacted the same way — no new
`audit.db`-style store, matching the "No per-node audit database" known
limitation above); MAC-vendor (OUI) lookup in the scan table (shown as a
bare MAC address only, no vendor guess — adding an OUI database was
judged not worth a new bundled data file for this); a live WinRM/
PowerShell-remoting bootstrap (see above).

## Kill / Reopen / Move semantics

- **Kill/Reopen**: unchanged from the existing single-node feature, just
  routed — `terminal_kill_session`/`terminal_reopen_session` always kill
  and reopen on the SAME node a session was already on (reopen never
  moves a session).
- **Move** (`ControllerService.terminal_move_session`) — **NOT live
  migration**. No process memory, scrollback, or shell history crosses
  nodes; the target gets a fresh process from an explicit `agent_type`/
  `cwd`, exactly like `terminal_reopen_session` already does for "same
  node, new process under an old name", just landing on a different node.

  **Ordering: create-on-target first, verify `READY`, only then stop the
  source.** This is the one deliberate deviation from a literal
  "prepare → sync → verify → stop → create" reading: stopping the source
  before confirming the target is genuinely ready would risk leaving
  *nothing* working anywhere if target creation then failed. Creating on
  the target first means a failed create is always a pure no-op — the
  source is never touched. Verified with real tmux evidence (not just the
  returned dict) in `test_move_session.py`, including: successful move
  (source really gone, target really exists), and four independent
  failure-safety cases (create errors, `CREATED`-but-not-`READY`, target
  unreachable, name collision on target) each proving the source survives.

  **Workspace sync is explicitly out of scope for this method.** It never
  copies files between nodes itself — `cwd` must already resolve on the
  *target* node's own `allowed_cwd_roots`, exactly like any other
  `terminal_create_session` call. Copying a git repo or workspace directory
  to the target is the operator's own job (a manual `git clone`/`rsync`,
  or deploy tooling), done *before* calling move. This project's
  node-agent HTTP surface deliberately exposes no generic file-sync
  operation (task's own "không expose arbitrary shell endpoint" — a
  file-sync endpoint is exactly the kind of broad primitive that
  constraint rules out without a much more careful, separately-designed
  scope).

  **Not yet exposed as an MCP tool or a dashboard button.** The backend
  method and its full safety-property test suite exist and pass; wiring a
  human-facing (dashboard) or agent-facing (MCP) trigger for it is
  deliberately deferred — this keeps today's blast radius at zero (no new
  way for ChatGPT or a stray click to move a real session) until it can be
  exercised against a real second node, not just a `FakeNodeClient`.

  **Cross-platform (Linux↔Windows) compatibility** is checked BEFORE the
  target is even attempted: `agent_type` must already be in the target
  node's own reported `agent_types` (a cheap, local check — no network
  round-trip, and no different, in principle, from the same check for a
  same-platform move) — a Windows target reporting `agent_types=
  ("shell",)` (no claude/codex CLI found there) cleanly refuses a move
  requesting `agent_type="codex"`, source completely untouched, before
  ever contacting the target. `cwd` compatibility has no such local
  shortcut and is caught the normal way, by the target's own real
  `resolve_cwd` check inside `create_session` — same "target first, source
  never touched on failure" guarantee either way.

## Security

- Every node-to-node hop (`RemoteNodeClient` → `terminal-node-agent`) is
  `Authorization: Bearer <token>`, constant-time-compared
  (`hmac.compare_digest`), a per-node shared secret (never a global one) —
  a wrong/missing token is refused with 401 **before** touching
  `TerminalService` at all. Verified live: a caught, fixed bug during this
  feature's own development (see below) came from *not* enforcing this
  strictly enough on the client side, not the server side, which was
  correct from the start.
- `terminal-node-agent`'s HTTP surface exposes only the same narrow
  operation set `NodeClient` defines — no raw shell, no arbitrary command
  execution, no tmux socket access from outside that one process. This is
  the same reason `terminal_move_session` doesn't sync files itself (see
  above).
- Every existing permission/whitelist/grant check in `TerminalService`
  applies identically whether a call arrives locally or was routed through
  the controller — routing is a thin wrapper, never a second
  authorization decision. Verified in `test_controller.py`
  (`test_permission_denial_preserved_through_routing`): a routed
  `terminal_send_text` on an input-disabled config returns the exact same
  error as calling `TerminalService` directly.
- The heartbeat-receiving route (`POST /dashboard/api/nodes/{id}/heartbeat`)
  is bearer-token authenticated (`TERMINAL_MCP_NODE_TOKEN_<NODE_ID>`), a
  **different** guard from every other dashboard route (Cloudflare
  Access/webauth) — this one is machine-to-machine, not browser-facing.
  Verified: a valid token for node A cannot authenticate as node B
  (`test_heartbeat_route_token_is_per_node_not_shared`).
- Every registry write (`register`/`heartbeat`/`set_draining`) is logged
  at the dashboard route layer with node_id/action/requesting-identity
  (`_log.info("dashboard node_drain node_id=%s draining=%s identity=%s", ...)`)
  — the same structured-logger pattern this project already uses
  elsewhere, not a new/separate audit store for nodes specifically (a
  reasonable future addition if node-level actions need the same
  durable, queryable audit trail `audit.py` gives session input).

## A real bug this feature's own testing caught

`RemoteNodeClient._request()` originally tried to parse *any* HTTP error
response body as valid JSON and return it as if it were a legitimate
application result — including a 401 Unauthorized. Caught live: a
deliberately-wrong bearer token against a real running `terminal-node-agent`
subprocess came back as if the (wrong) request had actually succeeded.
Root cause: `terminal-node-agent` never answers an application-level error
(`SESSION_NOT_FOUND`, `ACCESS_DENIED`, ...) with a non-200 status — those
always come back as a normal 200 with `{"error": ...}` in the body, exactly
like calling `TerminalService` directly returns a plain dict, never an
exception. The *only* non-200 responses that agent ever sends are
transport/auth failures. Fixed to always raise `NodeClientError` on any
`HTTPError`. Regression-tested (`test_node_client` coverage inside
`test_node_agent.py`'s auth tests, plus the exact-bug-shaped case is called
out in `node_client.py`'s own code comment so it can't regress silently).

## Config

```yaml
nodes:
  overload_thresholds:      # all optional -- shown values are the defaults
    ram_busy_percent: 80
    ram_overloaded_percent: 90
    swap_overloaded_percent: 20
    cpu_busy_percent: 85
    cpu_overloaded_percent: 95
    sustained_seconds: 300
    load_factor_busy: 1.2
    disk_free_overloaded_percent: 10
    smoothing_alpha: 0.4
  heartbeat:                 # optional
    degraded_after_seconds: 60
    offline_after_seconds: 180
  remote:                    # optional, empty by default -- today's deployment
    - node_id: m910
      display_name: "M910 Workstation"
      hostname: "m910.local"
      endpoint: "http://192.168.1.50:8790"   # LAN/VPN address, never through a public tunnel
      token_env: TERMINAL_MCP_NODE_TOKEN_M910  # the secret itself lives ONLY in this env var
      max_sessions: 20        # optional
      timeout_seconds: 10.0   # optional
```

The real production `config.yaml` on this host has **no `nodes:` section
at all** — confirmed by its own regression test
(`test_real_production_config_yaml_has_no_nodes_section_yet`) — exactly the
"no big-bang rewrite" requirement: multi-node is available, nothing is
opted into yet.

A remote node declared in config but whose `token_env` isn't set at
startup is **skipped with a warning log**, never a startup-fatal error —
`terminal-mcp-doctor nodes` reports it under `skipped_remote_nodes` so an
in-progress onboarding is visible, not silent.

## MCP / dashboard surface

New MCP tools: `terminal_list_nodes`, `terminal_node_status(node_id)`,
`terminal_node_sessions(node_id)` — all read-only from this surface
(drain/test-connection stay dashboard-only, same "control vs discovery"
split session grants already draw). `terminal_create_session` gained a
`node` parameter (`"auto"` default, or an explicit node_id).

Dashboard: a third admin page, `/dashboard/nodes` (linked from the main
page's header, next to the existing "⚙ Quản lý" link) — overview cards
(status dot, capacity badge, CPU/RAM/swap/disk meters, session count,
heartbeat age) and a detail panel per node (session list, Drain/Resume,
Test connection, Refresh). The main sidebar's session rows now show a
small node label next to the session name (e.g. "mesflow&nbsp;&nbsp;Dell")
— flat, no grouping, no second competing list, matching the earlier
dashboard-cleanup task's own constraint.

## Known limitations

- **Not live-verified against a real second machine.** Every piece has
  been exercised for real (a real `terminal-node-agent` subprocess on a
  real port, real HTTP, real tmux) except "a genuinely separate host on
  the network." See **Bringing up the M910** below for the exact remaining
  steps — they're operational (run the install script on that machine),
  not further development.
- **`terminal_move_session` has no MCP/dashboard trigger yet** — backend +
  tests only (see above for why).
- **The rich, grant-aware sidebar session list (`/dashboard/api/sessions`)
  stays local-node-only for now** — it calls `TerminalService.
  dashboard_list_sessions()` directly for its richer per-session
  read/input-grant fields, which `ControllerService.terminal_list_sessions()`'s
  narrower fleet-wide merge doesn't carry. Every row it returns today
  genuinely IS on the local node (tagged as such), so nothing is
  mislabeled — merging in a remote node's own sessions with the same
  grant-aware detail is deferred, not silently dropped.
- **Bindings and Supervisor stay local-node-scoped** — both operate on
  session names within this one process's own stores; a binding/watch on
  a session that later moves to another node is not automatically
  followed. Documented directly in `ControllerService.terminal_input_context`'s
  own binding-only branch.
- **No per-node audit database** — node actions (register/drain/heartbeat)
  are structured-logged, not written to a queryable `audit.db`-style store
  the way session input already is.
- **Workspace sync is manual** (see Move semantics above) — this project
  intentionally does not add a generic remote file-copy primitive.
- **Node management (page and API routes) exists only on the
  Cloudflare-Access-gated dashboard (`dashboard.py`)** — the separate
  password-login dashboard (`webauth_dashboard.py`, a full, independent
  duplicate of `dashboard.py`'s routes under `/app/...`, predating this
  feature) has no node routes, no Nodes page, and no `ControllerService`
  of its own at all. A password-login user sees no multi-node UI or API;
  extending that surface to match is deferred, not silently broken (there
  was nothing there for it to break).

## Bringing up the M910 (exact steps)

**If the M910 runs Linux**, on the M910 itself:

```bash
git clone <this-repo-url> ~/terminal-mcp   # or copy an existing checkout over
cd ~/terminal-mcp
./deploy/install-node-agent.sh --controller http://<dell-lan-ip>:8766 --node-id m910
```

**If the M910 runs Windows**, from an elevated PowerShell prompt on the
M910 itself:

```powershell
git clone <this-repo-url> C:\terminal-mcp   # or copy an existing checkout over
cd C:\terminal-mcp
.\deploy\install-node-agent.ps1 -ControllerUrl http://<dell-lan-ip>:8766 -NodeId m910
```

(Not executed anywhere in this session — no Windows/PowerShell available
— see this doc's own Windows node support section for exactly what was
and wasn't verified about this specific script.)

Either script prints the exact `config.yaml` block and environment
variable to add on the controller (this Dell) when it finishes. Then, on
the Dell:

1. Add the printed `nodes.remote` block to this repo's `config.yaml`.
2. Export the printed `TERMINAL_MCP_NODE_TOKEN_M910=...` wherever
   `terminal-mcp-http.service`'s own environment comes from (its systemd
   unit's `EnvironmentFile`, never inline in the unit).
3. Safe-restart `terminal-mcp-http.service` — verify every existing tmux
   session's `session_created` timestamp is unchanged before/after, same
   as any other restart of this service.
4. `terminal-mcp-doctor nodes` — `m910` should show `status=online` within
   one heartbeat interval (~20s default).
5. From the dashboard's `/dashboard/nodes` page (or `terminal_create_session`
   with `node="m910"` or `node="auto"`), create a disposable session on it
   and confirm tail/status/send round-trip correctly — this is the one
   check this session could not perform itself, with no second machine
   reachable.
