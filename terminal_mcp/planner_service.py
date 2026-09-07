"""Planner (task-breaking) -- the I/O glue between PlannerStore (the
plan-proposal log) and QueueService (the ONE real queue engine -- same
§20.0 binding rule every other Unified Task System slice follows).

Deliberately NOT an automatic complexity-based splitter (task's own
explicit "không chia vụn vô nghĩa" -- never split into meaningless
fragments): this project's own standing rule is deterministic-first,
no ML/LLM call to invent scope that doesn't already exist (see
coordinator.py's own disclosed scope_reasoner precedent) -- there is no
reliable, non-guessing way to decide FROM SCRATCH whether/how to split
a task's own prose. What THIS module provides is the real, tested,
safe INFRASTRUCTURE for a split whose decomposition content (child
titles/prompts/acceptance criteria/dependency shape) is supplied by the
CALLER (a human, ChatGPT, or a future smarter Planner mode) --
validation (NEEDS_CLARIFICATION for genuinely incomplete children,
never guessed), parent/child linking, a real depends_on DAG reusing
§7's own existing dependency mechanism, and the parent-completion rule
(§20.1) once every child is done.

Same SUGGEST/AUTO modes as the PM (docs/REQUIREMENTS.md §20.2's own
MODE note, reused vocabulary): SUGGEST computes + validates + PERSISTS
a proposal without creating anything; a human calls `approve_split` to
actually create the children. AUTO creates them immediately. No OFF
mode of its own -- a caller simply never calls this module if the
feature isn't wanted for a project (same as PM has no enforced project-
level toggle in this checkpoint either).
"""
from __future__ import annotations

import json
from typing import Any

from .planner_store import APPROVED, NEEDS_CLARIFICATION, PROPOSED, PlannerStore
from .pm_service import VALID_MODES
from .queue_service import QueueService

# Same MODE constants as pm_service.py (MODE_SUGGEST/MODE_AUTO) -- reused
# directly rather than re-declared, so the two features can never drift
# to different spellings of the same vocabulary.
MODE_SUGGEST = "SUGGEST"
MODE_AUTO = "AUTO"

# A parent can only be split while it still has no real work in flight --
# once it's already been claimed for dispatch/is running/is terminal,
# splitting it now would be racing the engine's own tick (same
# discipline queue_store.py's own MOVABLE_STATUSES already established
# for the Kanban's move_task_to_session).
SPLITTABLE_STATUSES = ("QUEUED",)


class PlannerService:
    def __init__(self, planner_store: PlannerStore, queue: QueueService) -> None:
        self.store = planner_store
        self.queue = queue

    def _validate_children(self, children: list[dict[str, Any]]) -> str | None:
        if not children:
            return "children list must not be empty"
        for index, child in enumerate(children):
            if not isinstance(child, dict) or not child.get("prompt"):
                return f"child[{index}] is missing a required 'prompt'"
            if not child.get("acceptance_criteria"):
                return f"child[{index}] is missing required 'acceptance_criteria' -- NEEDS_CLARIFICATION"
        return None

    def propose_split(self, parent_task_id: str, children: list[dict[str, Any]], *,
                      mode: str = MODE_SUGGEST) -> dict[str, Any]:
        if mode not in VALID_MODES:
            return {"error": "INVALID_MODE", "mode": mode, "valid_modes": list(VALID_MODES)}
        status = self.queue.task_status(parent_task_id)
        if "error" in status:
            return status
        parent = status["task"]
        if parent["status"] not in SPLITTABLE_STATUSES:
            # This single check ALSO covers "already split" -- _apply_
            # split always parks an approved parent in BLOCKED (never
            # QUEUED again), so a parent that's already been split can
            # never reach here with status still QUEUED. No separate
            # ALREADY_SPLIT check is needed (one would be dead code,
            # never reachable given that invariant).
            return {"error": "PARENT_NOT_SPLITTABLE", "parent_task_id": parent_task_id,
                    "status": parent["status"], "splittable_statuses": list(SPLITTABLE_STATUSES)}

        validation_error = self._validate_children(children)
        if validation_error is not None:
            proposal = self.store.create_proposal(
                parent_task_id=parent_task_id, mode=mode, status=NEEDS_CLARIFICATION,
                children_spec=children, reason=validation_error,
            )
            return {"proposal": proposal.to_dict()}

        reason = f"proposed split of {parent_task_id} into {len(children)} children"
        if mode == MODE_AUTO:
            proposal = self.store.create_proposal(
                parent_task_id=parent_task_id, mode=mode, status=PROPOSED,
                children_spec=children, reason=reason,
            )
            return self._apply_split(proposal.proposal_id)

        proposal = self.store.create_proposal(
            parent_task_id=parent_task_id, mode=mode, status=PROPOSED,
            children_spec=children, reason=reason,
        )
        return {"proposal": proposal.to_dict()}

    def approve_split(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get_proposal(proposal_id)
        if proposal is None or proposal.status != PROPOSED:
            return {"error": "NO_PENDING_PROPOSAL", "proposal_id": proposal_id,
                    "latest_status": proposal.status if proposal else None}
        return self._apply_split(proposal_id)

    def _apply_split(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.store.get_proposal(proposal_id)
        parent_task_id = proposal.parent_task_id
        child_task_ids: list[str] = []
        for child in proposal.children_spec:
            depends_on = [child_task_ids[i] for i in (child.get("depends_on_indices") or [])
                         if 0 <= i < len(child_task_ids)]
            metadata = dict(child.get("metadata") or {})
            metadata["parent_task_id"] = parent_task_id
            metadata["acceptance_criteria"] = child["acceptance_criteria"]
            result = self.queue.create_task(
                child.get("title") or "", child["prompt"], session=child.get("session"),
                priority=child.get("priority", 0), project=child.get("project"),
                metadata=metadata, depends_on=depends_on or None,
            )
            if "error" in result:
                # Rare (children already validated above) -- stop and
                # disclose exactly how far it got rather than silently
                # leaving a half-created split with no record of it.
                self.store.mark_decided(proposal_id, status="NEEDS_CLARIFICATION", child_task_ids=child_task_ids)
                return {"error": "CHILD_CREATION_FAILED", "detail": result,
                        "partial_child_task_ids": child_task_ids}
            child_task_ids.append(result["task_id"])

        # Park the parent in BLOCKED -- it has no more real work of its
        # own to dispatch (queue_store.py's own VALID_TRANSITIONS comment
        # on BLOCKED explains the new BLOCKED->COMPLETED edge this
        # enables later, once every child finishes).
        parent_row = self.queue.store.get_task(parent_task_id)
        parent_metadata = dict(parent_row.metadata)
        parent_metadata["is_split_parent"] = True
        parent_metadata["child_task_ids"] = child_task_ids
        self.queue.store.transition_task(parent_task_id, "PRECHECK", event_type="SPLIT_STARTED",
                                         reason=f"split into {len(child_task_ids)} children")
        self.queue.store.transition_task(
            parent_task_id, "BLOCKED", event_type="SPLIT_INTO_CHILDREN",
            reason=f"split into {len(child_task_ids)} children -- see child_task_ids",
            extra_fields={"metadata": json.dumps(parent_metadata)},
        )
        self.store.mark_decided(proposal_id, status=APPROVED, child_task_ids=child_task_ids)
        return {"proposal_id": proposal_id, "parent_task_id": parent_task_id, "child_task_ids": child_task_ids}

    def children_progress(self, parent_task_id: str) -> dict[str, Any]:
        """Real child status, read fresh every call -- the Kanban card's
        own "x/y done" (never a separately-computed/cached percentage).
        A full-scan over every lane's own tasks (same style board()
        already uses at this project's real, small scale) filtering on
        each task's own metadata.parent_task_id -- deliberately no new
        index/column for this, see this module's own docstring."""
        children = []
        for lane in self.queue.store.list_all_lanes():
            for task in lane["tasks"]:
                if (task.get("metadata") or {}).get("parent_task_id") == parent_task_id:
                    children.append(task)
        done = sum(1 for c in children if c["status"] == "COMPLETED")
        terminal_not_done = sum(1 for c in children if c["status"] in ("CANCELLED", "SKIPPED"))
        return {"parent_task_id": parent_task_id, "total": len(children), "done": done,
                "terminal_not_done": terminal_not_done, "children": children}

    def complete_parent_if_children_done(self, parent_task_id: str) -> dict[str, Any]:
        """Explicit, manual call (no background loop in this checkpoint,
        same posture as PM's own route_all_unassigned) -- applies §20.1's
        parent-completion rule: COMPLETED only once every non-terminal-
        cancelled child itself reaches COMPLETED. Refuses (NOT_A_SPLIT_
        PARENT) unless the task's own metadata.is_split_parent is True
        -- the guard that keeps the new BLOCKED->COMPLETED transition
        safe (queue_store.py's own VALID_TRANSITIONS comment)."""
        parent_row = self.queue.store.get_task(parent_task_id)
        if parent_row is None:
            return {"error": "TASK_NOT_FOUND", "task_id": parent_task_id}
        if not parent_row.metadata.get("is_split_parent"):
            return {"error": "NOT_A_SPLIT_PARENT", "task_id": parent_task_id}
        if parent_row.status != "BLOCKED":
            return {"error": "PARENT_NOT_AWAITING_CHILDREN", "status": parent_row.status}
        progress = self.children_progress(parent_task_id)
        if progress["total"] == 0:
            return {"error": "NO_CHILDREN_FOUND", "task_id": parent_task_id}
        still_open = progress["total"] - progress["done"] - progress["terminal_not_done"]
        if still_open > 0:
            return {"completed": False, "reason": f"{still_open} child task(s) not finished yet",
                    "progress": progress}
        completed = self.queue.store.transition_task(
            parent_task_id, "COMPLETED", event_type="PARENT_COMPLETED_VIA_CHILDREN",
            reason=f"all {progress['total']} children finished ({progress['done']} completed, "
                  f"{progress['terminal_not_done']} cancelled/skipped)",
            extra_fields={"verification_evidence": json.dumps(
                {"completed_via_children": True,
                 "child_task_ids": [c["id"] for c in progress["children"]]})},
        )
        return {"completed": True, "task": completed.to_dict(), "progress": progress}
