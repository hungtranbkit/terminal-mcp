# Hourly Terminal MCP node janitor — 2026-10-25

The fleet Doctor on HP gives health/placement advice, but advisory mode does not free node capacity. The per-node janitor actually reclaims **proven idle** tmux sessions. It runs locally per accessible Linux node, once per hour. Active work is not migrated or killed.

## Verified production pilot

- HP Linux: 10 original sessions, 6 terminated detached shells, 1 shell skipped due to a live child process; current tmux count with maintenance shell was 5. Timer enabled/active, service Result=success.
- Dell Linux: 20 original sessions, 2 detached Claude sessions and 12 idle Codex composer sessions terminated after checking child processes and task status. Other sessions with live python/pnpm children or uncertain screen state were preserved. Timer enabled/active, service Result=success.
- Doctor controller-level hourly timer remains enabled on HP, records node alerts and **advisory** placement. The 'node=auto' controller route currently fails with NODE_UNREACHABLE(local); fix tracked in issue #93. Do not claim full automatic task distribution.

## Safety policy

- Only tmux names with explicit Terminal MCP-managed node prefixes hp-/dell-/nec1-/m910-; never maintenance/Doctor/controller sessions.
- Exact current pane, one pane, not attached, two checks before killing, and idle ≥24 hours for shell or Codex under session pressure; otherwise Codex ≥48 hours.
- Shell must have no child process; Codex must show an empty composer and have only known long-lived local tool helper children. Claude is not automatically killed by this v1 policy.
- Skip any session referenced by an unfinished record in local direct_tasks.db when available. This is not a substitute for controller-wide durable queue reconciliation; implementing full central queue protection and queue-aware reaping remains required before enabling more aggressive policies.
- No project/worktree/database/Docker volume/model deletion, no process kill by PID. App cache cleanup is **limited** to .tmp/.partial/.part files older than 7 days under terminal-mcp/tmp directories, at most 128 MiB per run.
- Audit: ~/.local/state/terminal-mcp/node-janitor/{last.json,audit.jsonl}; dry-run by omitting --apply. One run closes at most six sessions and is protected by a nonblocking flock.

## Installation per node

1. Copy `deploy/fleet-node-janitor.py` into `~/.local/bin/terminal-mcp-node-janitor.py` (mode 0700).
2. Render `deploy/fleet-node-janitor.service.template`: substitute `@NODE@` (registered node ID) and `@CAP@` (real node admission cap, e.g. HP=10, Dell=20). Place it in `~/.config/systemd/user/terminal-mcp-node-janitor.service`.
3. Copy `deploy/fleet-node-janitor.timer` to the same unit directory, run `systemctl --user daemon-reload`, `systemctl --user enable --now terminal-mcp-node-janitor.timer`.
4. Run dry-run first: `python3 ~/.local/bin/terminal-mcp-node-janitor.py --node NODE --cap CAP`. Then one smoke `systemctl --user start terminal-mcp-node-janitor.service`, verify Result=success and last.json. Check systemctl --user list-timers --all.

Rollback: `systemctl --user disable --now terminal-mcp-node-janitor.timer` (does not affect controller or agents). Existing reclaimed tmux panes cannot be resumed; projects/files remain.

CI/GitHub Actions are not used for deployment.
