"""P0.3 Worker capability -- the TOOL/RUNTIME axis of what a node can do.

The fleet already reports platform / session_backend / shell_capabilities /
agent_types. What it could not express is the axis the coordination
roadmap actually routes on: "can this node run Playwright", "can it build
a .NET/WPF app". dell-5530 is Windows/ConPTY while m910 and macbook are
POSIX/tmux -- that difference is visible today, but the TOOLS on each are
not.

PROBED, NEVER DECLARED -- the same rule, for the same reason, as
agent_availability.py: that module exists because a node whose operator
wrote `claude: claude` into config but never installed the CLI was still
reported (and scheduled) as claude-capable, failing only at launch. A
capability here means "this binary actually resolves on THIS node right
now", never "someone typed it into a config file".

That is also why this does NOT reuse the existing `labels` column.
`labels` is an operator-supplied grouping tag; folding probed capability
into it would make "the node really has playwright" indistinguishable
from "an operator tagged this node playwright" -- precisely the
distinction agent_availability.py was written to protect.

CHEAP BY CONSTRUCTION: detection is `shutil.which` only (via
launcher_resolution.resolve_launcher, the same helper agent_types uses),
never running the tool. A heartbeat fires every 20s on every node; a
probe that executed `dotnet --list-sdks` or `npx playwright --version`
would put real load on every node forever, and a slow/hanging tool would
stall the heartbeat loop. Results are cached with a TTL so repeated
heartbeats do not re-walk PATH.

DELIBERATELY NOT INFERRED: "wpf"/"webview2" are not reported just because
a node is Windows. Platform is already its own field, and asserting a
build capability from the OS alone would be exactly the unverified claim
this module refuses to make. What IS reported is the evidence found --
e.g. `dotnet` -- and a consumer may combine that with `platform` itself.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Iterable

from .launcher_resolution import resolve_launcher

# (capability_name, binary_to_probe). Ordered, so the reported tuple is
# stable across heartbeats and diffs cleanly in the dashboard/registry.
DEFAULT_CAPABILITY_PROBES: tuple[tuple[str, str], ...] = (
    ("git", "git"),
    ("node", "node"),
    ("npm", "npm"),
    ("python", "python3"),
    ("docker", "docker"),
    ("dotnet", "dotnet"),
    ("playwright", "playwright"),
    ("tmux", "tmux"),
    ("rustc", "rustc"),
    ("go", "go"),
    ("java", "java"),
)

CAPABILITY_CACHE_TTL_SECONDS = 300.0
_CACHE: dict[str, tuple[float, tuple[str, ...]]] = {}
_CACHE_LOCK = threading.Lock()


def _probe_list() -> tuple[tuple[str, str], ...]:
    """`TERMINAL_MCP_CAPABILITY_PROBES` may EXTEND the built-in list with
    `name=binary` pairs (comma-separated) -- an operator can teach a node
    about a tool this module has never heard of. It can never make a
    capability appear that is not actually installed: whatever is added
    is still probed, not trusted."""
    raw = os.environ.get("TERMINAL_MCP_CAPABILITY_PROBES", "").strip()
    if not raw:
        return DEFAULT_CAPABILITY_PROBES
    extra: list[tuple[str, str]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, binary = part.partition("=")
        name, binary = name.strip(), (binary.strip() or name.strip())
        if name:
            extra.append((name, binary))
    known = {name for name, _ in DEFAULT_CAPABILITY_PROBES}
    return DEFAULT_CAPABILITY_PROBES + tuple(p for p in extra if p[0] not in known)


def probe_capabilities(*, use_cache: bool = True,
                       probes: Iterable[tuple[str, str]] | None = None) -> tuple[str, ...]:
    """Tool/runtime capabilities this node ACTUALLY has, right now.

    Returns only names whose binary resolves. An empty tuple is a
    legitimate answer (a bare container with none of these tools), and is
    reported as such rather than as "unknown"."""
    probe_pairs = tuple(probes) if probes is not None else _probe_list()
    key = ",".join(name for name, _ in probe_pairs)
    now = time.monotonic()
    if use_cache:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None and now - cached[0] < CAPABILITY_CACHE_TTL_SECONDS:
                return cached[1]
    found = tuple(name for name, binary in probe_pairs if resolve_launcher(binary) is not None)
    if use_cache:
        with _CACHE_LOCK:
            _CACHE[key] = (now, found)
    return found


def clear_cache() -> None:
    """Test/ops hook -- forces the next probe to re-walk PATH."""
    with _CACHE_LOCK:
        _CACHE.clear()
