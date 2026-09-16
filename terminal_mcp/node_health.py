"""Execution-aware node health with persisted circuit-breaker metadata."""
from __future__ import annotations

import random
import threading
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, TYPE_CHECKING

from .config import NodeHealthConfig
from .node_client import NodeClientError
from .node_models import (
    HEALTH_AUTH_UNAUTHORIZED,
    HEALTH_DEGRADED,
    HEALTH_EXECUTION_DOWN,
    HEALTH_EXECUTION_OK,
    HEALTH_OFFLINE,
    HEALTH_TRANSPORT_ONLINE,
    HEALTH_UNKNOWN,
    NODE_DEGRADED,
    NODE_OFFLINE,
    NODE_ONLINE,
    Node,
)
from .redaction import redact_text

if TYPE_CHECKING:
    from .lease import ResourceLockStore
    from .node_registry import NodeRegistry


@dataclass(frozen=True)
class NodeHealthPolicy:
    self_heal_enabled: bool = False
    self_heal_action: str = "none"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def classify_health(*, transport_status: str, execution_state: str,
                    consecutive_failures: int, failure_threshold: int) -> tuple[str, str]:
    """Pure classification: (health_state, backward-compatible status)."""
    # A heartbeat outside the fresh window is not allowed to borrow a prior
    # successful probe. This deliberately strengthens the old degraded
    # heartbeat state to OFFLINE at the composite layer.
    if transport_status != NODE_ONLINE:
        return HEALTH_OFFLINE, NODE_OFFLINE
    if execution_state == HEALTH_EXECUTION_OK:
        return HEALTH_EXECUTION_OK, NODE_ONLINE
    if execution_state == HEALTH_AUTH_UNAUTHORIZED:
        return HEALTH_AUTH_UNAUTHORIZED, NODE_DEGRADED
    if execution_state == HEALTH_EXECUTION_DOWN or consecutive_failures >= failure_threshold:
        return HEALTH_EXECUTION_DOWN, NODE_DEGRADED
    if consecutive_failures > 0 or execution_state == HEALTH_DEGRADED:
        return HEALTH_DEGRADED, NODE_DEGRADED
    return HEALTH_UNKNOWN, NODE_DEGRADED


class NodeHealthService:
    def __init__(self, registry: "NodeRegistry", locks: "ResourceLockStore",
                 config: NodeHealthConfig | None = None, *,
                 now: Callable[[], datetime] | None = None,
                 jitter: Callable[[float, float], float] | None = None) -> None:
        self.registry = registry
        self.locks = locks
        self.config = config or NodeHealthConfig()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.jitter = jitter or random.uniform
        self.policies: dict[str, NodeHealthPolicy] = {}

    def set_policy(self, node_id: str, policy: NodeHealthPolicy) -> None:
        self.policies[node_id] = policy

    def _delay(self, failures: int) -> float:
        base = min(self.config.backoff_max_seconds,
                   self.config.backoff_base_seconds * (2 ** max(0, failures - 1)))
        spread = base * self.config.backoff_jitter_ratio
        return min(self.config.backoff_max_seconds,
                   max(1.0, base + self.jitter(-spread, spread)))

    @staticmethod
    def _safe_error(exc: Exception | str) -> str:
        text = redact_text(str(exc)).replace("\n", " ")
        return text[:239] + ("…" if len(text) > 240 else "")

    def _cached(self, node: Node, *, reconnect_status: str | None = None) -> Node:
        health, legacy = classify_health(
            transport_status=node.transport_status or node.status,
            execution_state=node.execution_state,
            consecutive_failures=node.consecutive_failures,
            failure_threshold=self.config.execution_down_after_failures,
        )
        return replace(node, status=legacy, health_state=health,
                       transport_state=(HEALTH_TRANSPORT_ONLINE
                                        if (node.transport_status or node.status) == NODE_ONLINE
                                        else HEALTH_OFFLINE),
                       reconnect_status=reconnect_status or node.reconnect_status)

    def evaluate(self, node: Node, client: Any, *, force_probe: bool = False) -> Node:
        transport = node.transport_status or node.status
        node = replace(node, transport_status=transport)
        if not self.config.enabled:
            return node
        if transport != NODE_ONLINE:
            return replace(node, status=NODE_OFFLINE, health_state=HEALTH_OFFLINE,
                           transport_state=HEALTH_OFFLINE,
                           execution_state=node.execution_state or HEALTH_UNKNOWN)
        now = self.now()
        last_probe = _parse_time(node.last_probe_at)
        next_retry = _parse_time(node.next_retry_at)
        if not force_probe:
            if next_retry and now < next_retry:
                return self._cached(node, reconnect_status="BACKOFF")
            if (last_probe and node.execution_state == HEALTH_EXECUTION_OK and
                    (now - last_probe).total_seconds() < self.config.probe_interval_seconds):
                return self._cached(node)

        owner = f"node-health:{uuid.uuid4()}"
        lock = self.locks.acquire("terminal-mcp", f"node-health:{node.id}", owner,
                                  ttl_seconds=self.config.probe_timeout_seconds * 2 + 5,
                                  reason="execution health probe")
        if not lock.get("acquired"):
            return self._cached(node, reconnect_status="SUPPRESSED_CONCURRENT_PROBE")
        try:
            return self._probe_locked(node, client, now=now)
        finally:
            self.locks.release("terminal-mcp", f"node-health:{node.id}", owner)

    def _probe_locked(self, node: Node, client: Any, *, now: datetime) -> Node:
        generation = None
        try:
            transport_probe = getattr(client, "health", None)
            if transport_probe is not None:
                transport_result = transport_probe()
                if (not isinstance(transport_result, dict) or
                        transport_result.get("status") not in {"ok", "OK"}):
                    raise NodeClientError("transport health response was not ok")
                generation = transport_result.get("agent_generation")
            probe = getattr(client, "execution_probe", None)
            result = (probe(self.config.probe_timeout_seconds) if probe is not None
                      else client.list_sessions())
            if not isinstance(result, dict):
                raise NodeClientError("execution probe returned a malformed response")
            if result.get("agent_process_alive") is False:
                raise NodeClientError("execution child is not alive", error_code="EXECUTION_CHILD_DOWN")
            if result.get("execution_ok") is False or result.get("error"):
                raise NodeClientError(str(result.get("error") or "execution backend unavailable"),
                                      error_code="EXECUTION_BACKEND_DOWN")
        except Exception as exc:  # normalized below; one node never breaks fleet listing
            return self._failure(node, client, exc, now=now, generation=generation)

        persisted = self.registry.record_health_probe(
            node.id, state=HEALTH_EXECUTION_OK, success=True, probed_at=now,
            consecutive_failures=0, next_retry_at=None, last_error=None,
            reconnect_status="IDLE", agent_generation=generation,
        ) or node
        return replace(persisted, status=NODE_ONLINE, transport_status=NODE_ONLINE,
                       transport_state=HEALTH_TRANSPORT_ONLINE,
                       health_state=HEALTH_EXECUTION_OK, execution_state=HEALTH_EXECUTION_OK)

    def _failure(self, node: Node, client: Any, exc: Exception, *, now: datetime,
                 generation: str | None) -> Node:
        failures = node.consecutive_failures + 1
        definitive_execution_down = (isinstance(exc, NodeClientError) and
                                     getattr(exc, "error_code", None) in {
                                         "EXECUTION_CHILD_DOWN", "EXECUTION_BACKEND_DOWN"})
        if definitive_execution_down:
            failures = max(failures, self.config.execution_down_after_failures)
        unauthorized = (isinstance(exc, NodeClientError) and
                        getattr(exc, "http_status", None) in {401, 403})
        state = (HEALTH_AUTH_UNAUTHORIZED if unauthorized else
                 HEALTH_EXECUTION_DOWN if failures >= self.config.execution_down_after_failures else
                 HEALTH_DEGRADED)
        delay = self._delay(failures)
        next_retry = now + timedelta(seconds=delay)
        reconnect_status = "REPORT_ONLY" if unauthorized else "BACKOFF"
        heal_attempted = False
        policy = self.policies.get(node.id, NodeHealthPolicy())
        if (not unauthorized and failures >= self.config.execution_down_after_failures and
                policy.self_heal_enabled and policy.self_heal_action != "none"):
            same_generation_already_requested = bool(
                node.last_self_heal_at and
                (not generation or not node.agent_generation or generation == node.agent_generation)
            )
            if same_generation_already_requested:
                reconnect_status = "SELF_HEAL_AWAITING_REPLACEMENT"
            else:
                heal_attempted = True
                healer = getattr(client, "self_heal", None)
                if healer is None:
                    reconnect_status = "SELF_HEAL_UNAVAILABLE"
                else:
                    try:
                        answer = healer(policy.self_heal_action)
                        reconnect_status = ("SELF_HEAL_REQUESTED" if isinstance(answer, dict) and
                                            not answer.get("error") else "SELF_HEAL_FAILED")
                    except Exception:
                        reconnect_status = "SELF_HEAL_FAILED"
        persisted = self.registry.record_health_probe(
            node.id, state=state, success=False, probed_at=now,
            consecutive_failures=failures, next_retry_at=next_retry,
            last_error=self._safe_error(exc), reconnect_status=reconnect_status,
            agent_generation=generation, self_heal_attempted=heal_attempted,
        ) or node
        health, legacy = classify_health(
            transport_status=NODE_ONLINE, execution_state=state,
            consecutive_failures=failures,
            failure_threshold=self.config.execution_down_after_failures,
        )
        return replace(persisted, status=legacy, transport_status=NODE_ONLINE,
                       transport_state=HEALTH_TRANSPORT_ONLINE,
                       health_state=health, execution_state=state)

    def summary(self, nodes: list[Node]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        blockers = []
        for node in nodes:
            counts[node.health_state] = counts.get(node.health_state, 0) + 1
            if node.health_state != HEALTH_EXECUTION_OK:
                blockers.append({"node_id": node.id, "state": node.health_state,
                                 "reason": node.last_error or
                                           ("heartbeat is not fresh" if node.health_state == HEALTH_OFFLINE
                                            else "execution probe has not succeeded")})
        return {"counts": counts, "healthy": counts.get(HEALTH_EXECUTION_OK, 0),
                "total": len(nodes), "blockers": blockers[:20],
                "truncated": len(blockers) > 20}


class NodeHealthLoop:
    """One conservative process-local reconciler for the controller.

    The service's durable lock and backoff remain authoritative; this loop
    merely supplies periodic demand so opt-in recovery does not depend on an
    operator keeping the dashboard open.
    """

    def __init__(self, reconcile: Callable[[], Any], interval_seconds: float) -> None:
        self.reconcile = reconcile
        self.interval_seconds = max(1.0, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="terminal-mcp-node-health", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=min(2.0, self.interval_seconds))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                # One unexpected adapter/store failure cannot kill future
                # health checks. Individual node failures are normalized by
                # NodeHealthService before reaching this boundary.
                pass
            self._stop.wait(self.interval_seconds)
