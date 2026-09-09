"""Auto Recovery -- the reconciliation engine (task: "Auto Recovery cho
session sau reboot/crash/node-agent restart"). Automatically restores a
lost session's OWN identity (name, cwd, agent, conversation continuity
when the provider supports it) once its node reconnects, instead of
leaving it to sit MISSING/OFFLINE until a human explicitly calls
terminal_registry_reopen.

REUSE, NOT REBUILD (this project's own §20.0 binding rule): every real
mechanism this needs already exists --
  - session_registry.py's own MISSING/OFFLINE/recoverable/resumable
    status machinery (unchanged) is the durable record of WHAT needs
    recovering.
  - core.py's terminal_registry_reopen (unchanged) already does the
    actual work: spawns a genuinely NEW process from registry metadata,
    passes --resume when a real conversation_id is on record, VERIFIES
    the resume actually worked before ever reporting success (never a
    fake resurrection). This engine is a thin, policy-gated, locked,
    AUTOMATIC caller of that SAME method via controller.terminal_
    registry_reopen -- it does not reimplement spawning at all.
  - lease.py's PaneLeaseStore (unchanged) is reused directly as the
    recovery lock (pane_key = f"recovery:{node_id}/{session_name}") --
    the exact same atomic, cross-process, TTL-based acquire/release
    primitive already proven for send/verify serialization, never a
    second lock table.
  - node_registry.py's own sync_status_transitions (unchanged) is the
    real trigger: a node's OFFLINE/DEGRADED -> ONLINE transition is
    exactly "this node just reconnected", already detected as a side
    effect of the existing controller.list_nodes() poll.

TASK CONTEXT (item 6): deliberately NOT duplicated onto session_
registry.py -- a RUNNING/WAITING_SESSION task's own state is already
durable in queue_store.py (queue_tasks), unaffected by the session's own
process death. The existing SESSION_UNREACHABLE_ERRORS -> mark_waiting_
session path (queue_engine.py, pre-existing) already reattaches a task
to its session the moment that session becomes reachable/reachable-again
-- this engine adds NO new task-reattachment code, it only makes the
SESSION itself reachable again sooner. See tests/test_recovery_engine.py
for the real disposable proof that this pre-existing path really does
pick a WAITING_SESSION task back up once its session round-trips through
MISSING -> recovered -> ACTIVE.

COMPOSER TEXT IS EPHEMERAL, BY CONSTRUCTION (item 6's second half):
terminal_registry_reopen always spawns a genuinely NEW process -- the
OLD process's own pty buffer (and whatever was typed into its composer
but never submitted) is gone the instant that process exits, unrecoverable
by any means this project has. This is never worked around here (no
attempt to scrape/replay a dead process's own buffer) -- see checkpoint()
below for the one real, honest mitigation available: recording that a
session's state was last CONFIRMED safe as of a given point, never a
claim of literally preserving unsubmitted text.

EXACTLY-ONCE (item 5): the recovery lock (lease.py, above) makes two
concurrent recovery attempts for the SAME session collapse into one --
the loser's acquire() call fails cleanly (RECOVERY_IN_PROGRESS), never a
duplicate terminal_registry_reopen/duplicate spawn. recovery_generation
(session_registry.py) is a real, durable, never-reused counter per
attempt, independent of the lock (survives a lock TTL expiry/crash
mid-attempt without ever being confused with an earlier attempt)."""
from __future__ import annotations

import logging
import uuid
from typing import Any

from .core import (
    RECOVERY_STATE_BLOCKED, RECOVERY_STATE_DEGRADED, RECOVERY_STATE_PENDING, RECOVERY_STATE_RESUMED_OK,
)
from .lease import PaneLeaseStore
from .session_registry import SessionRegistryStore

_LOGGER = logging.getLogger(__name__)

LOCK_KEY_PREFIX = "recovery:"


def _lock_key(node_id: str, session_name: str) -> str:
    return f"{LOCK_KEY_PREFIX}{node_id}/{session_name}"


class RecoveryEngine:
    def __init__(self, registry: SessionRegistryStore, controller: Any, lease_store: PaneLeaseStore,
                config: Any) -> None:
        self.registry = registry
        self.controller = controller
        self.lease_store = lease_store
        self.config = config
        # The optional background loop (recovery_loop.py) -- same
        # "constructed lazily by mcp_app.py, exposed as its own
        # attribute so server_http.py can start/stop it" convention as
        # QueueService.loop/IntegrationService.loop.
        self.loop: Any = None

    def _recovery_allowed(self, record: Any) -> tuple[bool, str | None]:
        """Tri-state per-session override (session_registry.py's own
        auto_recovery_enabled column) wins over the global config.auto_
        recovery.enabled default -- see that column's own migration
        docstring."""
        if record.auto_recovery_enabled is not None:
            if record.auto_recovery_enabled:
                return True, None
            return False, "auto_recovery disabled for this session (explicit per-session override)"
        if self.config.enabled:
            return True, None
        return False, "auto_recovery.enabled is False globally, and this session has no per-session override"

    def _qualified(self, node_id: str, session_name: str) -> str:
        # ALWAYS the qualified node_id/session form, local node
        # included: controller.resolve_session's own bare-name lookup
        # only searches CURRENTLY-listed live sessions (via each node's
        # own list_sessions), which a MISSING/OFFLINE session by
        # definition never is -- see terminal_registry_reopen's own
        # docstring. The qualified form skips that liveness check
        # entirely, which is exactly what recovering a gone session
        # needs, regardless of which node it's on.
        return f"{node_id}/{session_name}"

    def recover_session(self, node_id: str, session_name: str, *, requested_by: str | None = None,
                        force: bool = False) -> dict[str, Any]:
        """The one real recovery attempt -- called by reconcile_node
        below (automatic) or directly by terminal_recover_session (a
        human's explicit manual trigger, which is exactly the SAME code
        path, never a second implementation). `force=True` bypasses
        BOTH the policy gate and the max_attempts bound (an explicit,
        deliberate human override -- never automatic)."""
        record = self.registry.get(node_id, session_name)
        if record is None:
            return {"error": "REGISTRY_RECORD_NOT_FOUND", "node_id": node_id, "session": session_name}
        if not record.recoverable:
            return {"error": "NOT_RECOVERABLE", "node_id": node_id, "session": session_name,
                    "status": record.status, "metadata_complete": record.metadata_complete}
        if not force:
            allowed, reason = self._recovery_allowed(record)
            if not allowed:
                self.registry.set_recovery_state(node_id, session_name, RECOVERY_STATE_BLOCKED, detail=reason)
                return {"error": "RECOVERY_BLOCKED", "node_id": node_id, "session": session_name, "reason": reason}
            if record.recovery_attempts >= self.config.max_attempts:
                reason = (f"recovery_attempts ({record.recovery_attempts}) reached config.auto_recovery."
                         f"max_attempts ({self.config.max_attempts}) -- needs a human (force=true) to retry again")
                self.registry.set_recovery_state(node_id, session_name, RECOVERY_STATE_BLOCKED, detail=reason)
                return {"error": "RECOVERY_BLOCKED", "node_id": node_id, "session": session_name, "reason": reason}

        lock_key = _lock_key(node_id, session_name)
        owner = f"recovery-{uuid.uuid4().hex[:12]}"
        if not self.lease_store.acquire(lock_key, owner, ttl_seconds=self.config.lock_ttl_seconds):
            # Someone else (another reconcile pass, another controller
            # process, a concurrent manual call) is already recovering
            # this exact session RIGHT NOW -- exactly-once, item 5.
            return {"error": "RECOVERY_IN_PROGRESS", "node_id": node_id, "session": session_name}
        try:
            was_resumable = record.resumable
            generation = self.registry.begin_recovery_attempt(node_id, session_name)
            self.registry.set_recovery_state(node_id, session_name, RECOVERY_STATE_PENDING,
                                             detail=f"generation {generation}")
            qualified = self._qualified(node_id, session_name)
            result = self.controller.terminal_registry_reopen(qualified, requested_by=requested_by)
            if "error" in result:
                # terminal_registry_reopen already set RECOVERY_FAILED
                # itself when it got as far as a resume attempt (core.
                # py's own code) -- for every OTHER failure (metadata
                # incomplete, launch failed before a resume was even
                # attempted) that method never touches recovery_state at
                # all, so this always makes the reason visible either
                # way, never silence.
                detail = result.get("recovery_detail") or result.get("error")
                self.registry.set_recovery_state(node_id, session_name, RECOVERY_STATE_BLOCKED, detail=str(detail))
                return {**result, "node_id": node_id, "session": session_name, "generation": generation}
            if was_resumable:
                # terminal_registry_reopen's own code already set
                # RESUMED_OK (verified) as its last write for this path.
                final_state = RECOVERY_STATE_RESUMED_OK
            else:
                # upsert_seen (called inside terminal_registry_reopen on
                # success) already cleared recovery_state to NULL --
                # explicitly overwrite with DEGRADED: a real new process
                # now exists, but there was never a conversation_id to
                # verify continuity against at all. Never silently look
                # like an ordinary healthy session.
                final_state = RECOVERY_STATE_DEGRADED
                self.registry.set_recovery_state(
                    node_id, session_name, RECOVERY_STATE_DEGRADED,
                    detail="recreated from metadata only -- no conversation_id recorded, continuity not verified")
            self.registry.reset_recovery_attempts(node_id, session_name)
            return {**result, "node_id": node_id, "session": session_name, "generation": generation,
                    "recovery_state": final_state}
        finally:
            self.lease_store.release(lock_key, owner)

    def reconcile_node(self, node_id: str, *, requested_by: str | None = None) -> list[dict[str, Any]]:
        """One full pass: every recoverable record for `node_id` gets
        one recover_session attempt (never force -- a background
        reconciliation pass NEVER bypasses policy/max_attempts; only an
        explicit human call does). A record's own lock/attempt bound
        means calling this repeatedly (e.g. every reconcile_poll_
        seconds tick) is always safe -- an already-in-flight or already-
        exhausted session is simply skipped with its own real reason,
        never retried in a tight loop."""
        listing = self.controller.registry_list(node_id, recoverable_only=True)
        if "error" in listing:
            return [{"error": listing["error"], "node_id": node_id, "detail": listing.get("detail")}]
        results = []
        for row in listing.get("records", []):
            session_name = row["session_name"]
            results.append(self.recover_session(node_id, session_name, requested_by=requested_by))
        return results

    def checkpoint(self, node_id: str, session_name: str, *, detail: str) -> dict[str, Any]:
        """The one real, honest mitigation for "composer text is
        ephemeral" (see this module's own docstring) -- records that
        this session's state was CONFIRMED safe as of now, never a
        claim of capturing unsubmitted text. `detail` is caller-
        supplied (e.g. "task <id> reached COMPLETED", "manual operator
        checkpoint") -- this function has no opinion on what counts as
        safe, it only persists the caller's own claim durably."""
        ok = self.registry.checkpoint(node_id, session_name, detail=detail)
        if not ok:
            return {"error": "REGISTRY_RECORD_NOT_FOUND", "node_id": node_id, "session": session_name}
        record = self.registry.get(node_id, session_name)
        return {"node_id": node_id, "session": session_name, "last_checkpoint_at": record.last_checkpoint_at,
                "last_checkpoint_detail": record.last_checkpoint_detail}
