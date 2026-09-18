"""Orchestration V1 -- the WORKER view: roles, capabilities, liveness.

WHAT THE AUDIT FOUND. "Worker" was a `(node_id, session)` string pair passed
around, with no runtime entity behind it. `pm_router.WorkerCandidate` is
assembled at decision time and explicitly never persisted; a worker otherwise
existed only as a side effect of `queue_tasks.claimed_by` being non-null. And
`role` was a nullable free-text column matched by string equality -- there
were no ROLE_* constants anywhere in the codebase.

WHY THIS IS A VIEW AND NOT A FOURTH STORE. Everything a worker is made of is
already persisted, in three places that each own their piece correctly:

    pm_store.capability_profiles   declared skills, role, affinity, WIP limit
    node_registry.nodes            PROBED tools, platform, liveness, capacity
    queue_tasks.claimed_by         what this worker is doing right now

Adding a `workers` table would duplicate all three and immediately start
drifting from them. So this composes, exactly as ProjectService does, and
owns no state of its own -- a property asserted by its tests.

DECLARED vs DETECTED IS PRESERVED, NOT FLATTENED. capability_probe.py exists
because a node whose operator *wrote* `claude:` into a config but never
installed the CLI was still scheduled as claude-capable. The node axis is
therefore probed and the session axis is declared, and merging them into one
undifferentiated set would destroy exactly the distinction that was built to
prevent that failure. `Worker.capabilities` reports both, tagged by source,
and `detected_capabilities` is the subset a caller can actually trust.

STALENESS IS REPORTED, NOT GUESSED. Node capabilities carry no verified_at
column, so the only honest freshness signal is the node's own heartbeat age.
That is surfaced as `capability_age_seconds` rather than being silently
treated as fresh -- an online node with a three-month-old probe would
otherwise be indistinguishable from one probed twenty seconds ago.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

ROLE_WORKER = "WORKER"
ROLE_VERIFIER = "VERIFIER"
ROLE_INTEGRATOR = "INTEGRATOR"
ROLE_DEPLOYER = "DEPLOYER"
ROLE_COORDINATOR = "COORDINATOR"

ALL_ROLES = (ROLE_WORKER, ROLE_VERIFIER, ROLE_INTEGRATOR, ROLE_DEPLOYER, ROLE_COORDINATOR)
"""The five runtime roles. Previously `role` was free text compared with
string equality, so "verifier", "Verifier" and "verifer" were three different
roles and none of them was wrong. A worker may hold SEVERAL roles -- the same
session can implement and later verify a different project's work -- so roles
are a set, not a single column value."""

DEFAULT_ROLES = (ROLE_WORKER,)
"""A session with no declared role is a plain worker. That is what every
existing session already is in practice, so the default changes nothing."""

WORKER_IDLE = "IDLE"
WORKER_BUSY = "BUSY"
WORKER_OFFLINE = "OFFLINE"


def normalise_roles(roles: Sequence[str] | str | None) -> tuple[str, ...]:
    """Upper-case, de-duplicated, order-stable, unknown values dropped.

    Dropping rather than raising is deliberate: role data arrives from
    operator-typed profiles, and one typo should narrow a worker's
    eligibility, never break the read that lists it."""
    if roles is None:
        return ()
    if isinstance(roles, str):
        roles = [roles]
    seen, out = set(), []
    for role in roles:
        value = str(role or "").strip().upper()
        if value in ALL_ROLES and value not in seen:
            seen.add(value)
            out.append(value)
    return tuple(out)


@dataclass(frozen=True)
class Worker:
    node_id: str
    session: str
    roles: tuple[str, ...]
    status: str
    detected_capabilities: tuple[str, ...] = ()
    declared_capabilities: tuple[str, ...] = ()
    platform: str | None = None
    project_affinity: str | None = None
    max_queued: int | None = None
    current_task_id: str | None = None
    current_project_id: str | None = None
    queue_depth: int = 0
    node_status: str | None = None
    capability_age_seconds: float | None = None
    skills: tuple[dict[str, Any], ...] = ()
    has_profile: bool = False

    @property
    def key(self) -> str:
        return f"{self.node_id}/{self.session}"

    @property
    def capabilities(self) -> tuple[str, ...]:
        """Everything this worker can do, from either source. Use
        `detected_capabilities` when the answer must be trustworthy."""
        return tuple(dict.fromkeys((*self.detected_capabilities, *self.declared_capabilities)))

    def has_role(self, role: str) -> bool:
        return role.strip().upper() in self.roles

    def can(self, required: Sequence[str], *, trust_declared: bool = True) -> bool:
        """AND semantics, matching verify_queue's own matcher. With
        trust_declared=False only PROBED capability counts -- what a
        scheduler should use before sending work somewhere expensive."""
        pool = set(self.capabilities if trust_declared else self.detected_capabilities)
        return set(str(r) for r in required).issubset(pool)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "node_id": self.node_id, "session": self.session,
            "roles": list(self.roles), "status": self.status,
            "capabilities": list(self.capabilities),
            "detected_capabilities": list(self.detected_capabilities),
            "declared_capabilities": list(self.declared_capabilities),
            "platform": self.platform, "project_affinity": self.project_affinity,
            "max_queued": self.max_queued, "current_task_id": self.current_task_id,
            "current_project_id": self.current_project_id, "queue_depth": self.queue_depth,
            "node_status": self.node_status,
            "capability_age_seconds": self.capability_age_seconds,
            "skills": [dict(s) for s in self.skills], "has_profile": self.has_profile,
        }


def _age_seconds(iso_timestamp: str | None) -> float | None:
    if not iso_timestamp:
        return None
    text = str(iso_timestamp).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


class WorkerRegistry:
    """Composes the three stores that already hold a worker's pieces. Owns
    no state; every dependency is optional and a missing one degrades the
    view rather than failing it."""

    def __init__(self, *, pm_store: Any = None, node_registry: Any = None,
                 queue: Any = None) -> None:
        self.pm_store = pm_store
        self.node_registry = node_registry
        self.queue = queue

    # -- declaration (delegates; no storage here) ------------------------

    def declare(self, node_id: str, session: str, *, roles: Sequence[str] | None = None,
                skills: Sequence[dict[str, Any]] | None = None,
                runtime_tools: Sequence[str] | None = None,
                project_affinity: str | None = None, os: str | None = None,
                max_queued: int | None = None) -> dict[str, Any]:
        """Declare what a session is FOR. Roles are validated against
        ALL_ROLES here so an unknown role cannot silently make a worker
        ineligible for everything."""
        if self.pm_store is None:
            return {"error": "PM_STORE_UNAVAILABLE",
                    "detail": "no capability store wired, so a declaration has nowhere to live"}
        wanted = list(roles or DEFAULT_ROLES)
        valid = normalise_roles(wanted)
        rejected = [r for r in wanted if str(r).strip().upper() not in ALL_ROLES]
        if not valid:
            return {"error": "INVALID_ROLE", "rejected": rejected, "allowed": list(ALL_ROLES)}
        profile = self.pm_store.upsert_capability(
            node_id, session, os=os, runtime_tools=tuple(runtime_tools or ()),
            project_affinity=project_affinity,
            # pm_store.role is a single TEXT column; roles are stored as a
            # comma-joined set so the existing schema carries a set without
            # a migration, and normalise_roles is the only reader.
            role=",".join(valid), skills=tuple(skills or ()), max_queued=max_queued)
        return {"declared": True, "roles": list(valid), "rejected_roles": rejected,
                "profile": profile.to_dict() if hasattr(profile, "to_dict") else None}

    # -- reads -----------------------------------------------------------

    def list_workers(self, *, project_id: str | None = None, role: str | None = None,
                     required_capabilities: Sequence[str] = (),
                     online_only: bool = True, trust_declared: bool = True) -> list[Worker]:
        workers = self._assemble()
        wanted_role = role.strip().upper() if role else None
        out = []
        for worker in workers:
            if online_only and worker.status == WORKER_OFFLINE:
                continue
            if wanted_role and not worker.has_role(wanted_role):
                continue
            if required_capabilities and not worker.can(required_capabilities,
                                                        trust_declared=trust_declared):
                continue
            if project_id and worker.project_affinity and worker.project_affinity != project_id:
                continue
            out.append(worker)
        return out

    def get(self, node_id: str, session: str) -> Worker | None:
        for worker in self._assemble():
            if worker.node_id == node_id and worker.session == session:
                return worker
        return None

    def roles_summary(self) -> dict[str, int]:
        """How many live workers hold each role -- the read that answers
        "can this fleet verify anything at all right now"."""
        counts = {role: 0 for role in ALL_ROLES}
        for worker in self.list_workers(online_only=True):
            for role in worker.roles:
                counts[role] = counts.get(role, 0) + 1
        return counts

    # -- assembly --------------------------------------------------------

    def _assemble(self) -> list[Worker]:
        profiles = {}
        if self.pm_store is not None:
            for profile in self.pm_store.list_capabilities():
                profiles[(profile.node_id, profile.session)] = profile

        nodes = {}
        if self.node_registry is not None:
            for node in self.node_registry.list():
                nodes[node.id] = node

        busy: dict[tuple[str, str], Any] = {}
        depth: dict[tuple[str, str], int] = {}
        if self.queue is not None:
            from .queue_store import TERMINAL_STATUSES
            for lane in self.queue.store.list_all_lanes():
                session = lane.get("session")
                for task in lane.get("tasks", []):
                    if task.get("status") in TERMINAL_STATUSES:
                        continue
                    node_id = task.get("node_id") or "local"
                    key = (node_id, session)
                    depth[key] = depth.get(key, 0) + 1
                    if task.get("claimed_by") and key not in busy:
                        busy[key] = task

        # A worker exists if EITHER a profile declares it or a live session
        # is running work -- neither source alone sees the whole fleet.
        keys = set(profiles) | set(busy) | set(depth)
        workers = []
        for node_id, session in sorted(keys):
            profile = profiles.get((node_id, session))
            node = nodes.get(node_id)
            task = busy.get((node_id, session))
            detected: tuple[str, ...] = ()
            platform = None
            node_status = None
            age = None
            if node is not None:
                from .verify_queue import node_capability_set
                detected = tuple(sorted(node_capability_set(node)))
                platform = getattr(node, "platform", None)
                node_status = getattr(node, "status", None)
                age = _age_seconds(getattr(node, "last_heartbeat_at", None))
            declared: tuple[str, ...] = ()
            roles = DEFAULT_ROLES
            affinity = None
            max_queued = None
            skills: tuple[dict[str, Any], ...] = ()
            if profile is not None:
                declared = tuple(profile.runtime_tools or ())
                roles = normalise_roles((profile.role or "").split(",")) or DEFAULT_ROLES
                affinity = profile.project_affinity
                max_queued = profile.max_queued
                skills = tuple(profile.skills or ())
                declared = tuple(dict.fromkeys(
                    (*declared, *(str(s.get("name")) for s in skills if s.get("name")))))
            if node is not None and node_status != "online":
                status = WORKER_OFFLINE
            elif node is None and self.node_registry is not None:
                status = WORKER_OFFLINE
            elif task is not None:
                status = WORKER_BUSY
            else:
                status = WORKER_IDLE
            workers.append(Worker(
                node_id=node_id, session=session, roles=roles, status=status,
                detected_capabilities=detected, declared_capabilities=declared,
                platform=platform, project_affinity=affinity, max_queued=max_queued,
                current_task_id=(task or {}).get("id"),
                current_project_id=(task or {}).get("project_id"),
                queue_depth=depth.get((node_id, session), 0), node_status=node_status,
                capability_age_seconds=age, skills=skills, has_profile=profile is not None))
        return workers
