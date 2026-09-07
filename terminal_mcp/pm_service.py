"""PM/Orchestrator Agent -- the I/O glue between PMStore (declarative
Capability Profiles + the append-only decision log), pm_router.py (the
pure deterministic algorithm), and QueueService (the ONE real queue
engine this routes tasks through -- §20.0's own binding "one queue
engine" rule). No background loop in this checkpoint -- every routing
decision is made by an explicit call (`route_task`/`route_all_
unassigned`), never a poll thread; a PM auto-loop is a disclosed,
separate, not-yet-built future increment (see docs/REQUIREMENTS.md
§20.2's own MODE note), matching this project's own standing "SUGGEST
first, prove it live, only then consider more automation" rollout
discipline for every other autonomous feature (Queue auto-dispatch,
Supervisor v2).

Modes (reused vocabulary, same posture as Supervisor's own observe_
only/suggest_only/approved_auto_continue -- never a fourth one):
  SUGGEST (default) -- computes and PERSISTS a routing decision, but
    never calls QueueService.assign_task itself. A human/caller reviews
    it (pm_explain/eligible_workers) and calls approve_routing to
    actually move the task.
  AUTO -- same computation, but a ROUTED decision is immediately
    followed by a real QueueService.assign_task call. Only meant to be
    used after a live disposable E2E pass per project (docs/
    REQUIREMENTS.md §20.2) -- this module does not itself enforce that;
    the CALLER (an MCP tool, a future PM loop) is responsible for never
    defaulting a real project to AUTO without that pass having happened.

`online`/`permissions_ok`/`queue_depth` are derived HERE, live, from the
real ControllerService/QueueService at decision time -- never persisted
on the CapabilityProfile itself (see pm_store.py's own docstring for
why: a stale snapshot could silently drift from reality).
"""
from __future__ import annotations

from typing import Any, Callable

from .permissions import valid_session_name
from .pm_router import BLOCKED, NO_ELIGIBLE_WORKER, ROUTED, WorkerCandidate, route_task
from .pm_store import PMStore
from .queue_service import QueueService

MODE_SUGGEST = "SUGGEST"
MODE_AUTO = "AUTO"
VALID_MODES = (MODE_SUGGEST, MODE_AUTO)


class PMService:
    def __init__(self, pm_store: PMStore, queue: QueueService, controller: Any | None = None, *,
                permission_checker: Callable[[str, str], bool] | None = None) -> None:
        self.store = pm_store
        self.queue = queue
        # `controller` is OPTIONAL and only ever consulted for a best-
        # effort node-online check (list_nodes()) -- every existing
        # caller that doesn't wire one (most tests, a single-node
        # deployment with no ControllerService of its own) gets
        # `online=True` for every candidate, same "best-effort, never
        # blocks on an optional integration" posture this project uses
        # elsewhere (e.g. TerminalService._start_knowledge_capture_for_
        # new_session's own try/except).
        self.controller = controller
        # `permission_checker(node_id, session) -> bool` is OPTIONAL,
        # injected by the real caller (mcp_app.py/dashboard.py wire a
        # closure over their own TerminalService/grants for the local
        # node) -- deliberately NOT reached into ControllerService's own
        # internals here (no `controller._clients[...]._terminal`
        # reaching-through-two-layers-of-"private" fragility). Routing
        # is a placement/assignment decision, not itself a security
        # boundary: the ACTUAL send still goes through the real, full
        # _input_authorized/grants gate at send time regardless of what
        # this check says -- so defaulting to True when no checker is
        # wired is safe, never a permission bypass.
        self.permission_checker = permission_checker

    # -- capability profile CRUD -------------------------------------------

    def upsert_capability(self, node_id: str, session: str, **fields: Any) -> dict[str, Any]:
        if not valid_session_name(session):
            return {"error": "INVALID_SESSION_NAME", "session": session}
        profile = self.store.upsert_capability(node_id, session, **fields)
        return {"capability": profile.to_dict()}

    def list_capabilities(self) -> dict[str, Any]:
        return {"capabilities": [p.to_dict() for p in self.store.list_capabilities()]}

    def delete_capability(self, node_id: str, session: str) -> dict[str, Any]:
        deleted = self.store.delete_capability(node_id, session)
        return {"deleted": deleted, "node_id": node_id, "session": session}

    # -- candidate assembly (real, live state) -----------------------------

    def _node_online(self, node_id: str) -> bool:
        if self.controller is None:
            return True
        try:
            nodes = self.controller.list_nodes()
        except Exception:  # noqa: BLE001 -- best-effort only, never blocks routing
            return True
        for node in nodes:
            if node.id == node_id:
                return node.status != "offline"
        return True  # unknown node (not yet registered/heartbeated) -- assume reachable, never hard-fail on this alone

    def _permissions_ok(self, node_id: str, session: str) -> bool:
        if self.permission_checker is None:
            return True
        try:
            return bool(self.permission_checker(node_id, session))
        except Exception:  # noqa: BLE001 -- best-effort only, never blocks routing
            return True

    def _candidates(self) -> list[WorkerCandidate]:
        pending = self.queue.pending_counts()
        candidates = []
        for profile in self.store.list_capabilities():
            candidates.append(WorkerCandidate(
                node_id=profile.node_id, session=profile.session, os=profile.os,
                runtime_tools=profile.runtime_tools, project_affinity=profile.project_affinity,
                role=profile.role, skills=profile.skills,
                online=self._node_online(profile.node_id),
                permissions_ok=self._permissions_ok(profile.node_id, profile.session),
                queue_depth=pending.get(profile.session, 0),
            ))
        return candidates

    def eligible_workers(self, task_id: str) -> dict[str, Any]:
        """Explainability tool (task's own explicit "xem eligible
        workers"): for a real task, which candidates pass the hard gate
        and which don't (and why) -- without actually routing/assigning
        anything. Read-only."""
        status = self.queue.task_status(task_id)
        if "error" in status:
            return status
        task = status["task"]
        from .pm_router import hard_gate_failure
        candidates = self._candidates()
        eligible = []
        ineligible = []
        for candidate in candidates:
            failure = hard_gate_failure(task, candidate)
            if failure is None:
                eligible.append(candidate.key())
            else:
                ineligible.append({"candidate": candidate.key(), "reason": failure})
        return {"task_id": task_id, "eligible": eligible, "ineligible": ineligible,
               "total_candidates": len(candidates)}

    # -- routing -------------------------------------------------------------

    def route_task(self, task_id: str, *, mode: str = MODE_SUGGEST) -> dict[str, Any]:
        if mode not in VALID_MODES:
            return {"error": "INVALID_MODE", "mode": mode, "valid_modes": list(VALID_MODES)}
        status = self.queue.task_status(task_id)
        if "error" in status:
            return status
        task = status["task"]
        candidates = self._candidates()
        decision = route_task(task, candidates)

        if decision.status == ROUTED:
            record_status = "ROUTED" if mode == MODE_AUTO else "SUGGESTED"
            recorded = self.store.record_decision(
                task_id=task_id, mode=mode, status=record_status, reason=decision.reason,
                chosen_node_id=decision.chosen.node_id, chosen_session=decision.chosen.session,
                score_breakdown=decision.score_breakdown, evidence=decision.evidence,
            )
            result = {"task_id": task_id, "mode": mode, "decision": recorded.to_dict()}
            if mode == MODE_AUTO:
                assign_result = self.queue.assign_task(task_id, decision.chosen.session)
                result["assign_result"] = assign_result
            return result

        # NO_ELIGIBLE_WORKER / BLOCKED -- still recorded (real audit
        # trail either way, task's own explicit "task history phải
        # persist"), never silently dropped.
        recorded = self.store.record_decision(
            task_id=task_id, mode=mode, status=decision.status, reason=decision.reason,
            score_breakdown=decision.score_breakdown, evidence=decision.evidence,
        )
        return {"task_id": task_id, "mode": mode, "decision": recorded.to_dict()}

    def approve_routing(self, task_id: str) -> dict[str, Any]:
        """A human approving a SUGGESTED routing decision -- the ONLY
        way a SUGGEST-mode decision actually results in a real
        QueueService.assign_task call. Refuses (NO_SUGGESTED_DECISION)
        if the latest decision for this task isn't a pending SUGGESTED
        one (already approved, was NO_ELIGIBLE_WORKER/BLOCKED, or no
        decision exists at all) -- never guesses which session to use."""
        latest = self.store.latest_decision_for_task(task_id)
        if latest is None or latest.status != "SUGGESTED":
            return {"error": "NO_SUGGESTED_DECISION", "task_id": task_id,
                    "latest_status": latest.status if latest else None}
        assign_result = self.queue.assign_task(task_id, latest.chosen_session)
        if "error" in assign_result:
            return assign_result
        approved = self.store.record_decision(
            task_id=task_id, mode=latest.mode, status="APPROVED_AND_ASSIGNED",
            reason=f"human-approved: {latest.reason}", chosen_node_id=latest.chosen_node_id,
            chosen_session=latest.chosen_session, score_breakdown=latest.score_breakdown,
            evidence={"approved_decision_id": latest.decision_id},
        )
        return {"task_id": task_id, "decision": approved.to_dict(), "assign_result": assign_result}

    def route_all_unassigned(self, *, mode: str = MODE_SUGGEST) -> dict[str, Any]:
        """Explicit, manual, one-shot sweep over every Backlog/UNASSIGNED
        task -- NOT a background loop (see this module's own docstring).
        Never touches an already-assigned task."""
        if mode not in VALID_MODES:
            return {"error": "INVALID_MODE", "mode": mode, "valid_modes": list(VALID_MODES)}
        board = self.queue.board()
        results = [self.route_task(task["id"], mode=mode) for task in board["backlog"]]
        return {"mode": mode, "routed_count": len(results), "results": results}

    def explain(self, task_id: str) -> dict[str, Any]:
        """`pm_explain` (§20.7) -- the full decision history for one
        task, newest first, real audit trail."""
        decisions = self.store.list_decisions_for_task(task_id)
        return {"task_id": task_id, "decisions": [d.to_dict() for d in decisions]}
