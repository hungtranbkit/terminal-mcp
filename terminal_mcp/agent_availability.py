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

from .launcher_resolution import resolve_launcher


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
        if resolve_launcher(launcher) is not None:
            available.append(agent_type)
    return tuple(available)


def agent_type_evidence(launch_commands: tuple[tuple[str, str], ...]) -> dict[str, dict[str, str]]:
    """The SAME answer, plus WHY -- `{agent_type: {available, launcher, detail}}`.

    "hp-linux does not offer claude" and "hp-linux offers claude" are both
    answers an operator can act on. "hp-linux's agent_types is
    ["shell"]" is neither: it does not say whether the launcher is
    unconfigured, misnamed, or installed somewhere this process's PATH cannot
    see -- which is by far the most common cause, because a systemd user unit
    does not inherit a login shell's PATH and therefore often cannot see
    `~/.local/bin`.

    Reporting the resolved absolute path when it IS found is the other half:
    it is the difference between believing a capability and being able to
    check it. Never executes the launcher -- see this module's own docstring
    and capability_probe.py for why detection stays `shutil.which`.
    """
    evidence: dict[str, dict[str, str]] = {
        "shell": {"available": True, "launcher": "", "detail": "every node can host a plain shell"},
    }
    for agent_type, launcher in launch_commands:
        if agent_type == "shell":
            continue
        if not launcher:
            evidence[agent_type] = {
                "available": False, "launcher": "",
                "detail": f"no launcher is configured for {agent_type!r} in "
                          f"session_lifecycle.launch_commands"}
            continue
        resolved = resolve_launcher(launcher)
        if resolved is None:
            evidence[agent_type] = {
                "available": False, "launcher": launcher,
                "detail": f"launcher {launcher!r} does not resolve on this node's effective PATH; "
                          f"install it, or give the service a PATH that includes it (a systemd "
                          f"user unit does not inherit a login shell's PATH)"}
            continue
        evidence[agent_type] = {"available": True, "launcher": launcher,
                                "detail": f"resolved to {resolved}"}
    return evidence
