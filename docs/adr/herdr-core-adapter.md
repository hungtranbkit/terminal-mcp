# ADR: herdr-core dual session backend

Status: pilot / canary only (2026-11-08)

## Decision

Terminal MCP keeps `tmux` as the default and historical source. New local
sessions may use `herdr-core` only when both
`TERMINAL_MCP_SESSION_MODE=HERDR_DEFAULT` and the local node is present in
`TERMINAL_MCP_HERDR_NODES`. `DUAL_READ` and `HERDR_CANARY` preserve tmux as
the control backend. Selection never migrates a running session.

The implementation targets `hc`, not the unrelated `herdr` TUI server. It
was verified against `hc --help`, `hc --skill`, `hc schema --json`, upstream
commit `cd12ddd88bc11490228343291dc1096c9ed3bf77`, release 0.1.18 and OpenRPC
document version 1.3.2. Pilot binary SHA-256:
`baa9d208d6400556a6e379152455fe1301e0e1acdbc114ff60b79d64425179cf`.

`HerdrCoreBackend` uses fixed argv arrays, `shell=False`, JSON responses and
bounded timeouts. Identity is `herdr-core:<address>`; names are display
aliases only. `hc ls` is a compact projection, so each live address is
resolved with `hc get` before identity/PID/cwd is trusted. Status is accepted
only from the structured OSC 7501 record returned by core. No screen-idle
heuristic is interpreted as blocked or done.

## Capability and limits

| Capability | tmux | herdr-core 0.1.18 | Pilot |
|---|---|---|---|
| create/list/get/read/send/keys/kill | yes | yes | PASS (dummy shell) |
| stable native ID | session/pane IDs | terminal address | PASS |
| attach | `tmux attach` | `hc attach` | command verified; interactive attach not automated |
| backend detach | detach clients | client closes or Ctrl+\\ | UNSUPPORTED API (truthful) |
| structured state | no native OSC contract | OSC 7501 only | UNKNOWN for dummy shell |
| survive client detach | yes | yes | upstream contract; not soak-tested here |
| survive core/host loss | tmux server dependent | original program is not resurrected after actual loss | no automatic rerun |

Existing grant, identity-pin, input-policy and audit code remains above the
`SessionBackend` protocol and is reused. `hc send` always submits; callers
requesting non-submitting text receive `UNSUPPORTED`.

## Rollout and rollback

Default / rollback:

```sh
export TERMINAL_MCP_SESSION_MODE=TMUX_ONLY
unset TERMINAL_MCP_HERDR_NODES TERMINAL_MCP_HC_BIN TERMINAL_MCP_HC_SESSION
```

Canary observation (still tmux primary):

```sh
export TERMINAL_MCP_SESSION_MODE=HERDR_CANARY
export TERMINAL_MCP_HERDR_NODES=hp-linux
export TERMINAL_MCP_HC_BIN=$HOME/.local/opt/herdr-core/0.1.18/herdr-core
```

Do not stop core or remove a terminal unless it belongs to the isolated
pilot namespace. Rollback only changes selection for future controller
starts/new sessions; it never kills or migrates tmux sessions.

## Gate verdict

`CANARY_ONLY`: unit and HP dummy-shell E2E pass, but Dell is at capacity and
no real Dell Codex/Claude canary or 24/48-hour soak was performed. Herdr is
also slower in this local microbenchmark, especially guarded send. Promotion
to `HERDR_DEFAULT` is forbidden until issue #109's complete gate passes.

