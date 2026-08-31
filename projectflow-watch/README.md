# ProjectFlow tmux watcher

Monitor-only watcher for the `projectflow-main` tmux session. Polls every 5
minutes, classifies pane state from a sanitized recent tail, and persists the
result. It never sends keystrokes, never mutates the target session, and has
no autonomous input path.

This directory is the version-controlled source of truth. It is deployed by
copying the files below to their runtime locations — there is no installer
script here (deliberately out of scope; install once by hand):

```bash
cp bin/projectflow-watch bin/projectflow-watch-status bin/projectflow-watch-tail ~/.local/bin/
chmod 755 ~/.local/bin/projectflow-watch ~/.local/bin/projectflow-watch-status ~/.local/bin/projectflow-watch-tail
cp systemd/projectflow-watch.service systemd/projectflow-watch.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now projectflow-watch.timer
```

## Layout

- `bin/projectflow-watch` — core watcher (stdlib-only Python 3, no third-party deps).
  Run `projectflow-watch --self-test` to execute the built-in classification fixtures.
- `bin/projectflow-watch-status` — prints the last persisted status (`--json` for raw output).
- `bin/projectflow-watch-tail` — prints the last sanitized tail excerpt, or `--history`
  for recent state transitions.
- `systemd/projectflow-watch.service` — `Type=oneshot`, runs one poll.
- `systemd/projectflow-watch.timer` — `OnBootSec=2min`, `OnUnitActiveSec=5min`, `Persistent=true`.

## Runtime state (not tracked here)

```
~/.projectflow-watch/status.json   # current snapshot, overwritten atomically each poll, mode 0600
~/.projectflow-watch/history.log   # appended only on state change, mode 0600
~/.projectflow-watch/watch.lock    # prevents overlapping runs
```

## States

`RUNNING`, `WAITING_INPUT`, `PLAN_APPROVAL`, `DONE`, `ERROR`, `SESSION_MISSING`, `UNKNOWN`.

See the classification patterns and their known limitations directly in
`bin/projectflow-watch` (heuristic, favors `UNKNOWN` over a confident wrong guess).

## Security

- Read-only against tmux: only `has-session`, `display-message`, and `capture-pane` are
  ever called. No `send-keys`/`send-text`, no auto-approve, no mutation of the watched session.
- Basic redaction (API keys, Bearer/Authorization, password/token fields, AWS keys, PEM
  blocks) is applied before anything is persisted — not full DLP.
- Runs as the invoking user under a user-level systemd unit; no root required.
- Independent of the `terminal_mcp` package — this is a standalone tool kept in this repo
  for version control only, isolated from the ProjectFlow application repo itself.
