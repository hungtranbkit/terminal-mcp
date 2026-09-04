"""Auto placement (task item 6) -- deterministic scoring over a list of
candidate Node snapshots, pure function, no I/O of its own (the caller
already fetched the node list from NodeRegistry.list()). Deterministic on
purpose: the exact same input node list always produces the exact same
placement decision, no randomness, so a placement can always be explained
after the fact (`reason` on the result) and reproduced in a test.
"""
from __future__ import annotations

from dataclasses import dataclass

from .node_models import CAPACITY_OVERLOADED, NODE_ONLINE, Node


@dataclass(frozen=True)
class PlacementResult:
    node_id: str | None
    reason: str
    candidates_considered: int
    excluded: tuple[tuple[str, str], ...] = ()  # (node_id, why-excluded), for diagnostics/doctor


def _eligible(node: Node, *, required_agent_type: str, min_disk_free_bytes: int,
              required_platform: str | None = None) -> tuple[bool, str | None]:
    """One node's eligibility gate -- ALL must pass before it's even
    scored. Returns (eligible, exclusion_reason)."""
    if node.status != NODE_ONLINE:
        return False, f"status={node.status}"
    if node.draining:
        return False, "draining"
    if node.capacity_status == CAPACITY_OVERLOADED:
        return False, f"overloaded ({', '.join(node.overload_reasons) or 'no reason recorded'})"
    if required_platform is not None and node.platform != required_platform:
        # Multi-node Windows support -- a caller that explicitly needs a
        # specific platform (e.g. a session that only makes sense on
        # Windows, or a Linux-only workflow) is never silently placed on
        # the wrong one. Omitted (the default): any platform is eligible,
        # exactly today's single-platform behavior, unchanged.
        return False, f"platform={node.platform!r} != required {required_platform!r}"
    if required_agent_type not in ("shell", *node.agent_types):
        # "shell" needs no launcher at all -- every node that can run tmux
        # at all can host a plain shell session; claude/codex require the
        # node to have actually reported that capability at heartbeat time.
        return False, f"agent_type={required_agent_type!r} not in node's capabilities {node.agent_types!r}"
    if node.max_sessions is not None and (node.tmux_session_count or 0) >= node.max_sessions:
        return False, f"at max_sessions ({node.tmux_session_count}/{node.max_sessions})"
    if node.disk_free_bytes is not None and node.disk_free_bytes < min_disk_free_bytes:
        return False, f"disk_free_bytes {node.disk_free_bytes} < required {min_disk_free_bytes}"
    return True, None


def _score(node: Node) -> tuple[float, float, float, str]:
    """Higher is better on every axis. Returned as a tuple so Python's own
    tuple comparison does lexicographic ranking with NO floating-point
    tie-breaking ambiguity: (RAM headroom, CPU/load headroom, -session
    count) in that priority order, ties broken by node id (the LAST tuple
    element) purely for determinism when every real axis is truly tied --
    never as a meaningful ranking signal of its own."""
    ram_headroom = 100.0 - (node.ram_percent_smoothed if node.ram_percent_smoothed is not None else node.ram_percent or 50.0)
    cpu = node.cpu_percent_smoothed if node.cpu_percent_smoothed is not None else node.cpu_percent
    cpu_headroom = 100.0 - cpu if cpu is not None else 50.0
    session_penalty = -(node.tmux_session_count or 0)
    return (ram_headroom, cpu_headroom, session_penalty, node.id)


def choose_node(nodes: list[Node], *, required_agent_type: str = "shell",
                min_disk_free_bytes: int = 1024 * 1024 * 1024,  # 1 GiB -- a real floor, not zero
                labels_required: tuple[str, ...] = (),
                required_platform: str | None = None) -> PlacementResult:
    """The Auto scheduler (task item 6, extended by multi-node Windows
    support's own "Scheduler Auto phải xét platform requirement"). `nodes`
    should already be the CURRENT NodeRegistry.list() snapshot -- this
    function trusts it entirely and does no additional freshness check
    itself (status is already derived by NodeRegistry.list() at the
    moment it was called). `required_platform` ("linux"/"windows"), when
    given, excludes every node of the other platform before scoring;
    omitted (the default), platform plays no role at all -- identical to
    this function's behavior before Windows nodes existed."""
    excluded: list[tuple[str, str]] = []
    eligible: list[Node] = []
    for node in nodes:
        ok, reason = _eligible(node, required_agent_type=required_agent_type, min_disk_free_bytes=min_disk_free_bytes,
                               required_platform=required_platform)
        if not ok:
            excluded.append((node.id, reason or "ineligible"))
            continue
        if labels_required and not set(labels_required).issubset(node.labels):
            excluded.append((node.id, f"missing required labels {labels_required!r} (has {node.labels!r})"))
            continue
        eligible.append(node)

    if not eligible:
        return PlacementResult(
            node_id=None,
            reason="no eligible node: " + ("; ".join(f"{nid}: {why}" for nid, why in excluded) or "no nodes registered"),
            candidates_considered=len(nodes), excluded=tuple(excluded),
        )

    eligible.sort(key=_score, reverse=True)
    winner = eligible[0]
    return PlacementResult(
        node_id=winner.id,
        reason=(f"selected {winner.id}: RAM headroom={100.0 - (winner.ram_percent_smoothed or winner.ram_percent or 0):.0f}%, "
               f"sessions={winner.tmux_session_count or 0}, capacity={winner.capacity_status}"),
        candidates_considered=len(nodes), excluded=tuple(excluded),
    )
