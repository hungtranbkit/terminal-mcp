# Known Issues

- **`main` cannot be pushed from this host.** The remote is HTTPS with no
  credential helper, no `gh`, and no deploy key. The branch is many commits
  ahead of origin. Not worked around: improvising a credential is worse than
  the delay.
- **tmux `session_activity` is unreliable on this fleet.** The Terminal Wall
  keeps its own output-fingerprint history instead.
- **`codex` is not installed on m910.** One lifecycle test skips accordingly.
  (An earlier task to install it was explicitly cancelled by the user.)
- **Backlog writes are gated by `allowed_cwd_roots`.** The repo's `config.yaml`
  lists the dell host's layout; the deployed config at
  `~/.config/terminal-mcp/config.yaml` is the one that allows `/home/mesflow`.
- **dell-linux rebooted 2026-09-13 21:13 and lost all 9 tmux sessions**
  (dell3, dell4, facebook, mcp, mesflow, mesflow-dell, mesflow1, mesflow2,
  promptflow). They are gone, not hidden -- the reporting was correct.
- **hp-linux now reports only `hp1`** (it had hp1-hp4). Unexplained: SSH to
  that host fails host-key verification from m910, so it was flagged rather
  than guessed at.
- **dell-linux's tunnel unit is still `enabled`.** That is deliberate: the
  fallback must survive a reboot. The ExecCondition guard is what stops it
  joining while m910 is healthy. To force it in a real emergency, move
  `~/.config/systemd/user/terminal-mcp-tunnel.service.d/10-split-brain-guard.conf`
  aside and `daemon-reload`.
