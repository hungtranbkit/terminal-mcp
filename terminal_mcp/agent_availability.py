"""Real launcher-binary availability -- shared by every node type (local,
Linux terminal-node-agent, Windows terminal-node-agent) so `agent_types`
in a heartbeat means the same thing everywhere: "this node's own config
names a launcher for this agent_type AND that binary is actually
resolvable on THIS node", never just "an operator typed this into
config.yaml somewhere".

Fixes a real, pre-existing gap this project already had on Linux (not
only a new Windows requirement): before this module existed, every
heartbeat path (dashboard.py's _refresh_local_heartbeat, node_agent.py's
_heartbeat_loop) built `agent_types` from `config.session_lifecycle.
launch_commands` alone -- a node whose operator configured `claude:
claude` but never actually installed the `claude` CLI would still be
reported (and therefore scheduled) as claude-capable, only failing later
at actual launch time (LAUNCHER_NOT_CONFIGURED/LAUNCH_FAILED). Task
item's own explicit requirement ("Nếu Claude/Codex CLI không có trên
Windows node thì capability báo false") is applied identically on every
platform here, not special-cased to Windows.
"""
from __future__ import annotations

import shutil


def available_agent_types(launch_commands: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    """`launch_commands` is `config.session_lifecycle.launch_commands`
    (`((agent_type, launcher_binary), ...)`). Returns `("shell", ...)` --
    "shell" is always included (every node that can run a session at all
    can host a plain shell, matching scheduler.py's own `_eligible`
    logic) plus every agent_type whose launcher `shutil.which` actually
    resolves on THIS node, in the order given. `shutil.which` itself
    already searches the platform-native way (PATH on Linux, PATH +
    PATHEXT `.exe`/`.cmd`/`.bat`/... on Windows) -- no separate Windows-
    specific lookup needed."""
    available = ["shell"]
    for agent_type, launcher in launch_commands:
        if agent_type == "shell":
            continue  # already included unconditionally above
        if shutil.which(launcher) is not None:
            available.append(agent_type)
    return tuple(available)
