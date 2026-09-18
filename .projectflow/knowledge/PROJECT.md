# Terminal MCP

A controller that owns real terminal sessions across a small fleet, and an MCP
surface so an agent can read and drive them.

## Who uses it
One developer, operating a fleet of nodes (m910 local, dell-linux, hp-linux,
macbook, dell-5530/Windows) through a dashboard behind Cloudflare Access.

## What it must not break
- **Live sessions.** tmux and ConPTY sessions are attended work. A controller
  restart must not touch them; `KillMode=process` plus cgroup isolation is why
  it does not.
- **Node agents.** Never restarted as part of a controller deploy.
- **The Access gate.** Cloudflare Access and Tailscale are the perimeter.
  Nothing here bypasses them, and a 302 to a login challenge is the gate
  working, not a bug.
- **Secrets.** No credential is ever replicated between nodes, written into
  knowledge, echoed into output, or stored in a spec. Environment variable
  NAMES only.

## Opt-in model
Work-mode features apply to sessions whose name ends in `-work`. An ordinary
session is never claimed, never sent a prompt, and never has its state changed
by the Work runtime. That rule lives in one place: `work_eligibility.py`.
