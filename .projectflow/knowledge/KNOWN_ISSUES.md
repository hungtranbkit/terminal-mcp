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
