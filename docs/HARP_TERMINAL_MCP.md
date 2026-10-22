# Harp + Terminal MCP (optional macOS operator UI)

Status as of 2026-10-22: **staged integration / NOT installed / NOT a Terminal MCP transport fix**. Tracking: [#77](https://github.com/hungtranbkit/terminal-mcp/issues/77).

Upstream: [dominikmartn/harp](https://github.com/dominikmartn/harp), unofficial AGPL-3.0 Warp fork. Verified release: [v0.1.3](https://github.com/dominikmartn/harp/releases/tag/v0.1.3), DMG for **Apple Silicon macOS**, SHA-256 `3374749ac2a7b5a9d14c36737b1db3d7b309f44f6cd07bbd2b32862f3e929774`. Do not install an unverified binary or assume Intel macOS/Linux support.

## Scope and architecture

```text
MacBook (when online)
  Harp.app --- local terminal tabs and local tmux integration
       |
       +--- SSH interactive terminal ---> Dell Linux tmux/Codex
       +--- SSH interactive terminal ---> HP Linux tmux/Codex
       +--- SSH interactive terminal ---> m910 tmux (avoid new workload if RAM pressured)

ChatGPT ---> authenticated Terminal MCP tunnel ---> fed controller ---> node agents/tmux
                                                     (fleet source of truth)
```

Harp is **not** an MCP server, a secure MCP tunnel, an agent scheduler or a connection repair tool. It **cannot** repair `GET /v1/health` timeouts, `IDLE_GUARD_UNSUPPORTED`, `DIRECT_SUBMIT_NOT_ACCEPTED`, incorrect READY/tool permissions, duplicate dispatch or stale node registry. Fix those in Terminal MCP itself (issue #77).

Harp's `integrations.tmux` setting uses tmux on the machine running Harp. A plain SSH session showing remote tmux output is **not guaranteed** to be adopted into Harp's local tmux sidebar/groups; do not imply that Harp automatically aggregates remote Linux tmux sessions. Harp quota rings read the **Mac's local** Codex session logs or Claude status line; quota for Codex processes running on Dell/HP must be collected separately if desired. Keep Terminal MCP `list_nodes`, `list_sessions`, `inspect` and authenticated dashboard as the authoritative **fleet** view.

## Non-disruptive installation when MacBook is reachable

1. On the Mac, verify macOS/Apple Silicon with `uname -s` and `uname -m` (expect `Darwin`, `arm64`). Verify `tmux`, `ssh`, and your normal SSH aliases. Do not change remote agent or access policy just to make Harp work.
2. Download the **specific upstream v0.1.3 DMG** from the link above. Run `shasum -a 256 ~/Downloads/Harp-0.1.3.dmg` and compare to the SHA above. Check its macOS code signature/notarization before install if your local security policy requires it. Mount DMG; drag Harp into Applications. Do **not** replace Warp; they have different bundle IDs/settings.
3. Back up `~/.harp/settings.toml` if present, then add/update the following (the setting is **off by default**):

```toml
[integrations.tmux]
enabled = true
```

4. Quit and reopen Harp. Test with a **new harmless local** tmux session and a local Codex/Claude tab; check whether status shows the actual state. For remote machines, open explicit SSH tabs, e.g. `ssh -t dell-linux 'tmux attach -t EXISTING_SESSION'` after checking `ssh dell-linux 'tmux list-sessions'`. SSH aliases/usernames are site-specific; do not guess them or expose the tmux socket over the network.
5. Compare displayed session states against Terminal MCP `inspect`. If Harp disagrees, Terminal MCP is authoritative for node/session details. Never infer `tool_ready=true` from Harp's status indicator.

Read-only local/SSH prerequisites: `bash scripts/harp_macos_preflight.sh [ssh-alias ...]` (run on Mac after the relevant SSH aliases exist). This script does not install Harp or edit settings.

## Terminal MCP reliability gates (must be fixed/tested separately)

- **Execution-aware readiness**: `state=READY` but `tool_ready=false` is not full operational readiness. A real bounded tool/permission probe must prove tools work; otherwise label WAITING_PERMISSION/BLOCKED/UNKNOWN.
- **Guarded submit**: reproduction on 2026-10-22: newly created Codex session `dell-codex-terminalmcp-harp-20261022` returned `IDLE_GUARD_UNSUPPORTED`, `DIRECT_SUBMIT_NOT_ACCEPTED`, `dispatched=false`, `enter_sent=false`. Do **not** retry the full prompt under a new request key or call it running.
- **No double dispatch / premature complete**: success requires proved accepted input and bounded execution evidence. Preserve idempotency; distinguish WAITING_INPUT, STUCK_COMPOSER, RUNNING, DONE and UNKNOWN.
- **Node health/reconnect**: diagnose controller-vs-tunnel-vs-node separately, persist failure reason/last success, back off reconnect attempts. Mac and Dell Windows `/v1/health` timed out as of 2026-10-22; Harp cannot fix this.
- **Reconciliation**: inspect all node sessions, bindings, leases, dirty worktrees and active tasks before cleanup. Do not auto-delete on quiet/IDLE alone. Keep operator-disabled queue **OFF**.
- **Deployment**: identify the actual active unit and running `/version`; on federated controller the `terminal-mcp-http.service` unit may be retired. Do not start an old unit and collide on port 8766. Test and stage rollback before applying any production update.

### Read-only diagnostics on controller (Dell Linux; only when host access is restored)

```bash
systemctl --user list-units 'terminal-mcp*'
systemctl --user status terminal-mcp-fed-controller.service
systemctl --user status terminal-mcp-tunnel.service
curl --fail --max-time 3 -sS http://127.0.0.1:8766/version
terminal-mcp-doctor connection
```

No port forwarding/public exposure of `127.0.0.1:8766`. The authenticated tunnel remains required for ChatGPT.

## Safe rollback

- Harp UI: exit Harp, reopen Warp or usual macOS Terminal. Harp keeps its own `~/.harp` settings.
- tmux integration: change `[integrations.tmux] enabled = false` in `~/.harp/settings.toml` or restore the prior backup. This does not delete tmux sessions.
- Terminal MCP: no server-side changes are required to run Harp. Keep production unchanged until separately tested and accepted.

## Verification record

2026-10-22: Terminal MCP connected to dell-linux, hp-linux and m910; mac17-macbook-pro, old macbook and Dell Windows offline. Repo issue #77 created. A dedicated Codex session could be **created** but task dispatch **failed** with `IDLE_GUARD_UNSUPPORTED`; no Harp install, live Mac validation, bridge deployment or Terminal MCP bugfix has been verified at this stage. This documentation must not be interpreted as a completed production integration.

## Linux fallback while Mac is offline: Agent Deck

**Harp is macOS-only.** For Linux, [Agent Deck](https://github.com/asheshgoplani/agent-deck) is an *alternative* tmux-aware AI agent TUI (Codex/Claude/etc.), not a Harp port and not a replacement for the Terminal MCP controller. Do not claim any Harp DMG is installed on Linux.

Pinned pilot package: [Agent Deck v1.16.30](https://github.com/asheshgoplani/agent-deck/releases/tag/v1.16.30). Linux amd64 SHA-256 `88d9629488f17734057a7dbf9fcdf640ffb23311bf9e2ebf4d304123ff8b25bc`; Linux arm64 SHA-256 `88a9b4d1ccecbd7081ffd63d62986781553eb1c025e25d7eec2bbc7d0d667027` (release asset digests, verify each downloaded artifact).

For another **Linux** machine, run the repo-local `bash scripts/install_agent_deck_linux.sh`. The script installs a pinned checksum-verified binary into `~/.local/bin/agent-deck`, refuses to overwrite a different existing version, and does **not** change tmux config, create sessions, or use sudo. For a manual pilot, run `~/.local/bin/agent-deck --version`; open `~/.local/bin/agent-deck` interactively via trusted local terminal/SSH only when ready to test the TUI. Do not publish an unauthenticated web UI.

**Real HP Linux pilot (2026-10-22):** existing idle shell `hp-shell-bds-qa-20261020` was used to check `Linux x86_64`, tmux and curl. A pinned v1.16.30 amd64 archive was downloaded from the official release; `sha256sum -c` printed `OK`; binary installed at `/home/kimex/.local/bin/agent-deck`; `agent-deck --version` printed `Agent Deck v1.16.30`; CLI `--help` worked. **TUI not yet launched/validated**. No tmux config changed, no existing tmux sessions adopted, moved or deleted; external tmux sessions remain Terminal MCP's responsibility. m910 was intentionally skipped (RAM ~87%).

**Terminal MCP direct dispatch progress:** on Dell Linux, `turn(start)` had previously failed with `IDLE_GUARD_UNSUPPORTED` but `turn(supervise)` accepted the same repair goal as a *new, once-only* supervised task, receipt `EXECUTION_ACCEPTED`, `RUNNING`, id `dt_be4a28869f3a4dae`. This is a working dispatch **workaround**, not proof the broken `start` path is fixed. A dedicated Codex agent is repairing the issue on an isolated branch; don't label this PR as a production fix until code tests and actual deployed version are verified.
