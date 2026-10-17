# HP production rollout runbook: 2026-10-17

Target: HP `hp-linux` (Tailscale 100.67.53.117), user `kimex`, canonical
checkout `/home/kimex/workspace/terminal-mcp`.

- Running SHA: `1b8e7a3816c2a25b87be0bd3de3a372f26dbcfd4`, 58 commits behind.
- Target: `origin/main` `76ebad5754f43e83398c018c3ee01d7c257a69fa` (#61/#66),
  plus the supervisor fix branch `fix/supervisor-blocked-parser-usage-limit`
  once it is merged.

## Why this was not executed from m910

As of 2026-10-17, m910 (`mesflow`) cannot run commands on HP through any
authorized path:

- `ssh kimex@100.67.53.117` with the purpose-scoped controller key returns
  `Permission denied (publickey)`.
- `tailscale ssh kimex@hp-linux` fails because no host key is known
  (strict checking).
- The controller listener on `100.67.53.117:8766` serves only heartbeat and
  enrollment. Every other route has to come over loopback.
- HP `create_session` is at capacity 10, so no new agent can start on HP.
- m910 has no GitHub write credential (no `gh`, no token), so the fix is a
  local commit plus a patch file, not a PR.

**Who must act:** the HP owner. Either run the steps below in a shell on HP,
or authorize the m910 controller key for `kimex@hp-linux` and say explicitly
that production activation is approved.

## 0. Preserve: do not touch

- Leave session `hp-codex-terminal58-finish-20261016` alone (WAITING_INPUT at
  the Codex model menu, quota exhausted). Do not approve or answer it.
- Leave every tmux session alone. The service units must keep
  `KillMode=process`; check this before restarting anything.
- Leave the DBs under `~/.local/state/terminal-mcp/*.db` alone. Back them up;
  never replace them.

## 1. Audit the failed task (read-only)

```bash
cd /home/kimex/workspace
git -C terminal-mcp status --short; git -C terminal-mcp stash list
ls -la .terminal-mcp-worktrees/
for w in .terminal-mcp-worktrees/*/; do echo "$w"; git -C "$w" log --oneline -3; git -C "$w" status --short | head; done
git -C terminal-mcp for-each-ref refs/terminal-mcp/checkpoints --sort=-committerdate | head
sqlite3 ~/.local/state/terminal-mcp/direct_tasks.db \
  "select task_id,target,state,continuations,max_continuations,substr(reason,1,160) from direct_tasks order by created_at desc limit 20;"
```

## 2. Preflight and backups

```bash
cd /home/kimex/workspace/terminal-mcp
test -z "$(git status --porcelain)" || { echo DIRTY; exit 1; }
STAMP=$(date +%Y%m%d-%H%M%S); B=~/terminal-mcp-backups/$STAMP; mkdir -p "$B"
git rev-parse HEAD > "$B/PREV_SHA"
for db in ~/.local/state/terminal-mcp/*.db; do sqlite3 "$db" ".backup '$B/$(basename "$db")'"; done
cp -a config.yaml "$B/" 2>/dev/null; cp -a ~/.config/terminal-mcp "$B/config-dir" 2>/dev/null
systemctl --user cat terminal-mcp-http terminal-mcp-chatgpt-v1 > "$B/units.txt"
grep -q 'KillMode=process' "$B/units.txt" || { echo 'KillMode=process missing: STOP'; exit 1; }
tmux ls > "$B/tmux-before.txt"
```

## 3. Compatibility tests (isolated checkout, already PASS for 76ebad5)

```bash
cd /home/kimex/workspace/.terminal-mcp-worktrees/readiness-main-20261017
git fetch origin && git checkout --detach <TARGET_SHA>
.venv/bin/python -m pytest -q tests/test_direct_task_lifecycle.py \
  tests/test_direct_task_blocked_parser.py tests/test_issue52_supervised_dispatch.py tests/test_headless.py
# Point the isolated checkout at a copy of the production config and DBs to check migrations:
TMPS=$(mktemp -d); cp ~/terminal-mcp-backups/$STAMP/*.db "$TMPS/"
XDG_STATE_HOME=$TMPS .venv/bin/python -c "from terminal_mcp.direct_task import DirectTaskStore, default_direct_task_path as p; DirectTaskStore(p()); print('migrate ok')"
```

## 4. Activate (only with explicit production approval)

```bash
cd /home/kimex/workspace/terminal-mcp
git fetch origin && git checkout --detach <TARGET_SHA>
.venv/bin/pip install -e . --no-deps -q
systemctl --user restart terminal-mcp-http terminal-mcp-chatgpt-v1
tmux ls | diff - ~/terminal-mcp-backups/$STAMP/tmux-before.txt && echo 'sessions preserved'
```

## 5. Live validation (required before anyone calls this complete)

1. `create_session` with agent codex: wait for `READY`, i.e. a stable composer
   (#66). Capacity is 10, so the owner has to free one slot first. Do not
   kill a user session to get one.
2. `terminal_turn action=supervise` with a trivial step that ends in
   `TMCP-DONE`. The task has to reach `DONE` and the step has to actually run.
3. `supervise_status` shows `running_confirmed: true`.

## 6. Rollback

```bash
cd /home/kimex/workspace/terminal-mcp
git checkout --detach "$(cat ~/terminal-mcp-backups/$STAMP/PREV_SHA)"
.venv/bin/pip install -e . --no-deps -q
systemctl --user restart terminal-mcp-http terminal-mcp-chatgpt-v1
# Restore a DB only if a migration broke it, with the service stopped:
# sqlite3 ~/.local/state/terminal-mcp/<db> ".restore '$B/<db>'"
```
