# Herdr pilot (P0) — HP only

Herdr (<https://github.com/herdrdev/herdr>) runs beside tmux as a second,
read-mostly runtime source. tmux stays the default: nothing in this pilot
creates, sends to, or adopts a tmux session, and the code runs no Herdr
subprocess unless the pilot flag is on for this node.

Code: `terminal_mcp/herdr_runtime.py`. Tests: `tests/test_herdr_runtime.py`.

## What is installed on HP (2026-10-26)

| Item | Location |
|---|---|
| Herdr binary v0.9.5 (pinned, no sudo) | `~/.local/opt/herdr/v0.9.5/herdr` + `SHA256SUMS` + `PROVENANCE.txt` |
| Pilot herdr config (not the global one) | `~/.config/terminal-mcp/herdr-pilot/config.toml` |
| Pilot server | transient user unit `herdr-terminalmcp-pilot.service` (MemoryMax 256M, CPUQuota 50%) |
| Herdr session dir, 0600 sockets | `~/.config/herdr/sessions/terminalmcp-pilot/` |
| Owner record, task map, canary output | `~/.local/state/terminal-mcp/herdr-pilot/{owner.json,task-map.json,status.json}` |
| Canary code (2 files pinned at commit 89e6cae) | `~/.local/opt/terminal-mcp-herdr-pilot/89e6cae/` |
| Canary timer (every 2 min, read-only) | `~/.config/systemd/user/terminal-mcp-herdr-canary.{service,timer}` |

Provenance: tag `v0.9.5` → commit `2ecf19a9`, built by the `Release` workflow
(run 38593819055) and uploaded by `github-actions[bot]`. The asset sha256
`14d9e8bc…929d` matches GitHub's digest. The tag is unsigned and no SLSA
attestation is published, so the GitHub digest is the strongest check
available.

Herdr opens no TCP port. Its API is a unix socket with mode 0600. The pilot
config turns off `version_check`, `manifest_check`, sound, toasts and
`resume_agents_on_restore`.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `HERDR_ENABLED` | off | Master flag (`1/true/yes/on`). |
| `TERMINAL_MCP_HERDR_NODES` | empty | Comma list of node ids. Both this and the flag are required. |
| `TERMINAL_MCP_HERDR_BIN` | `~/.local/opt/herdr/v0.9.5/herdr` | Pinned binary. |
| `TERMINAL_MCP_HERDR_SESSION` | `terminalmcp-pilot` | Named session. `default` is always refused. |
| `TERMINAL_MCP_HERDR_CONFIG_PATH` | `~/.config/terminal-mcp/herdr-pilot/config.toml` | Passed as `HERDR_CONFIG_PATH`. |
| `TERMINAL_MCP_HERDR_STATE_DIR` | `~/.local/state/terminal-mcp/herdr-pilot` | Holds `owner.json`. |
| `TERMINAL_MCP_HERDR_TIMEOUT` | `3` | Seconds per CLI call. |
| `TERMINAL_MCP_HERDR_CACHE_SECONDS` | `5` | Snapshot cache. A failure opens a 30 s breaker. |
| `TERMINAL_MCP_HERDR_TASK_MAP` | unset | JSON file mapping a pane id, agent name or workspace label to `{"task_id", "repo"}`. |

## Surfaces

- `GET /dashboard/api/runtime-sessions` returns `{rows, sources}`. Each row
  has `source` set to `tmux` or `herdr`. Rows are deduplicated by key. When a
  herdr row has a `task_id` that a tmux row already has, the tmux row stays
  primary and lists the herdr key under `also_in`. The route uses the same
  `_read_guard` as `/dashboard/live`.
- `/dashboard/live` and the `/app/live` mirror show a "herdr pilot" table
  only when the adapter is enabled. When Herdr is down, the table shows a
  static banner with the blocker code and marks the old rows `(cũ)`. It
  never shows a spinner.
- Herdr states are passed through as `working`, `blocked`, `idle`, `done`
  or `unknown`. A plain shell pane is `unknown` with `is_agent: false`.
  Herdr's JSON carries no timestamps, so `first_seen_at` and
  `state_changed_at` record when this process observed each value.
- There is no HTTP mutation. `close-pane` and `stop-session` exist only in
  the CLI, and each needs all of: the flag, the node in the allowlist,
  `owner.json` for that exact session, a workspace label that starts with
  `tmcp-` (for panes), and `--confirm <target>`. The code never calls
  `herdr server stop` or touches the `default` session.

CLI, with the pilot environment set:

```bash
python3 -m terminal_mcp.herdr_runtime health
python3 -m terminal_mcp.herdr_runtime list
python3 -m terminal_mcp.herdr_runtime attach-command   # prints argv; run it in your own terminal
python3 -m terminal_mcp.herdr_runtime close-pane w1:p2 --confirm w1:p2
python3 -m terminal_mcp.herdr_runtime stop-session --confirm terminalmcp-pilot
```

To attach, run `herdr session attach terminalmcp-pilot`. To detach, press
`ctrl+b q` or close the client. Detaching never stops the panes. This was
verified on HP: three client disconnects, and `w1:p1` kept the same
`shell_pid` and heartbeat each time.

## Enable / disable

**Canary (live now, pilot-only):** the timer writes `status.json`. It does
not touch the production controller.

```bash
systemctl --user status terminal-mcp-herdr-canary.timer
cat ~/.local/state/terminal-mcp/herdr-pilot/status.json
```

**Dashboard (not yet live; needs a planned controller restart):**

1. Build the deploy tree as a merge of the live deploy branch and
   `feat/herdr-pilot-20261026`.
2. Add a drop-in `zz…-herdr-pilot.conf` with
   `Environment=HERDR_ENABLED=1`, `TERMINAL_MCP_HERDR_NODES=hp-linux`, and
   `TERMINAL_MCP_HERDR_TASK_MAP=…`.
3. Restart in an approved window.

To turn it off, remove the drop-in, then run `daemon-reload` and restart.
With the flag off, the code is inert.

## Rollback (scoped; nothing else is touched)

```bash
systemctl --user disable --now terminal-mcp-herdr-canary.timer
rm ~/.config/systemd/user/terminal-mcp-herdr-canary.{service,timer} && systemctl --user daemon-reload
# stop ONLY the pilot session (owner-checked), then its transient unit
PYTHONPATH=~/.local/opt/terminal-mcp-herdr-pilot/89e6cae HERDR_ENABLED=1 TERMINAL_MCP_HERDR_NODES=hp-linux \
  python3 -s -m terminal_mcp.herdr_runtime stop-session --confirm terminalmcp-pilot
systemctl --user stop herdr-terminalmcp-pilot.service
# optional removal of the pilot's files
rm -rf ~/.local/opt/herdr ~/.local/opt/terminal-mcp-herdr-pilot ~/.config/terminal-mcp/herdr-pilot \
       ~/.config/herdr/sessions/terminalmcp-pilot ~/.local/state/terminal-mcp/herdr-pilot
```

To restart the pilot after a reboot or rollback, run:

```bash
systemd-run --user --unit=herdr-terminalmcp-pilot -p MemoryMax=256M -p MemorySwapMax=0 -p CPUQuota=50% -p TasksMax=128 \
  --setenv=HERDR_CONFIG_PATH=$HOME/.config/terminal-mcp/herdr-pilot/config.toml --working-directory=$HOME \
  ~/.local/opt/herdr/v0.9.5/herdr --session terminalmcp-pilot server
```

## Persistence limits

Detaching keeps the processes running. A reboot, `session stop` or server
stop ends every pane process. When Herdr restarts, it restores the saved
layout (`session.json`). It resumes agents only for official integrations
that report session refs, and only when `resume_agents_on_restore` is on,
which it is not in the pilot. Do not treat a Herdr pane as durable work
state. Task truth stays in Terminal MCP.

## Extending to NEC1 and m910 (not done)

1. Install the same pinned binary on the node. NEC1 needs `herdr-linux-x86_64`.
   Check `uname -m` on m910 first. Verify the sha256 against the GitHub digest
   on that node. Do not use `curl | sh`.
2. Copy the pilot `config.toml` and start a named session under the node's
   own user.
3. Add the node id to `TERMINAL_MCP_HERDR_NODES` in **that node's** agent
   environment. The adapter reads only the local Herdr. There is no
   cross-node socket forwarding, and `herdr --machine` is not used.
4. Remote rows reach the controller only through the node agent's existing
   authenticated channel. That is a follow-up: P0 reads the local node only.
   Never expose a Herdr socket over TCP or SSH forwarding.
5. Enable one node at a time and watch `status.json` and the live page for
   24 hours before adding the next.
