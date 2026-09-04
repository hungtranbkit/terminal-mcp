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
reproducible in a test.

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

On the M910 itself:

```bash
git clone <this-repo-url> ~/terminal-mcp   # or copy an existing checkout over
cd ~/terminal-mcp
./deploy/install-node-agent.sh --controller http://<dell-lan-ip>:8766 --node-id m910
```

The script prints the exact `config.yaml` block and environment variable
to add on the controller (this Dell) when it finishes. Then, on the Dell:

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
