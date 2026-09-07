"""Integration Agent -- the MCP-facing service layer over
integration_store.py/integration_engine.py (task: "3-role model: Coding
A/B + Integration Agent"). Same split as queue_service.py: this is
CRUD/status/control (configure/status/list/pause/resume/retry/force-
regression/promote) plus the one explicit `run_once` that actually
drives the engine forward one bounded step -- nothing here runs on a
timer by itself (see integration_engine.py's own WAITING_FOR_HANDOFF/
event-driven docstring)."""
from __future__ import annotations

from typing import Any

from .integration_engine import IntegrationEngine
from .integration_store import MERGE_READY, InvalidHandoffTransitionError, IntegrationStore


class IntegrationService:
    def __init__(self, store: IntegrationStore | None = None, engine: IntegrationEngine | None = None) -> None:
        self.store = store or IntegrationStore()
        self.engine = engine

    def configure(self, project: str, *, repo_path: str, integration_branch: str = "integration",
                 main_branch: str = "main", targeted_test_command: list[str] | None = None,
                 full_regression_command: list[str] | None = None, batch_size: int = 3,
                 batch_max_wait_seconds: float = 1800, auto_promote_enabled: bool = False,
                 session_ownership: dict[str, str] | None = None, review_depth: str = "basic",
                 allow_mechanical_conflict_resolution: bool = False) -> dict[str, Any]:
        return self.store.configure_pipeline(
            project, repo_path=repo_path, integration_branch=integration_branch, main_branch=main_branch,
            targeted_test_command=targeted_test_command, full_regression_command=full_regression_command,
            batch_size=batch_size, batch_max_wait_seconds=batch_max_wait_seconds,
            auto_promote_enabled=auto_promote_enabled, session_ownership=session_ownership,
            review_depth=review_depth, allow_mechanical_conflict_resolution=allow_mechanical_conflict_resolution,
        )

    def status(self, project: str) -> dict[str, Any]:
        pipeline = self.store.get_pipeline(project)
        if pipeline is None:
            return {"error": "PROJECT_NOT_CONFIGURED", "project": project}
        handoffs = self.store.list_handoffs(project)
        active = next((h for h in handoffs if h.status in ("CLAIMED", "MERGING", "TARGETED_TEST")), None)
        batches = self.store.list_batches(project, limit=10)
        return {
            "project": project, "pipeline": pipeline,
            "current_handoff": active.to_dict() if active else None,
            "handoff_counts": {
                status: sum(1 for h in handoffs if h.status == status)
                for status in ("READY_FOR_INTEGRATION", "CLAIMED", "MERGING", "TARGETED_TEST", "INTEGRATED",
                             "REWORK_REQUIRED", "BLOCKED")
            },
            "recent_batches": [b.to_dict() for b in batches],
        }

    def list_handoffs(self, project: str, *, status: str | None = None, limit: int = 100) -> dict[str, Any]:
        return {"project": project, "handoffs": [h.to_dict() for h in self.store.list_handoffs(
            project, status=status, limit=limit)]}

    # UI-facing lane label for one Handoff/RegressionBatch status -- the
    # Dashboard Supervisor/Coordinator panel's own "Waiting/Reviewing/
    # Merging/Test/Regression/Rework" vocabulary (task: "integration lane
    # trạng thái Waiting/Reviewing/Merging/Test/Regression/Rework"),
    # mapped from this store's own real status constants in exactly ONE
    # place so it's never re-derived/duplicated at the dashboard layer.
    _HANDOFF_LANE_LABEL = {
        "READY_FOR_INTEGRATION": "Waiting", "CLAIMED": "Reviewing", "MERGING": "Merging",
        "TARGETED_TEST": "Test", "INTEGRATED": "Integrated", "REWORK_REQUIRED": "Rework", "BLOCKED": "Blocked",
    }
    _BATCH_LANE_LABEL = {
        "REGRESSION_PENDING": "Regression (pending)", "REGRESSION_RUNNING": "Regression (running)",
        "MERGE_READY": "Merge ready", "REGRESSION_FAILED": "Regression failed",
    }

    def fleet_overview(self) -> dict[str, Any]:
        """Every configured project's own Integration lane state, in one
        call (task: Dashboard Supervisor/Coordinator panel's "integration
        handoffs" section) -- reuses status()'s own per-project
        aggregation for each, adding only the UI-facing lane_label. A
        project with no pipeline configured at all simply never appears
        (same posture as an unconfigured queue lane)."""
        projects = []
        for pipeline in self.store.list_pipelines():
            project = pipeline["project"]
            detail = self.status(project)
            current_handoff = detail["current_handoff"]
            open_batch = self.store.get_open_batch(project)
            projects.append({
                "project": project, "paused": pipeline["paused"], "repo_path": pipeline["repo_path"],
                "current_handoff": current_handoff,
                "current_lane": self._HANDOFF_LANE_LABEL.get(current_handoff["status"]) if current_handoff else None,
                "handoff_counts": detail["handoff_counts"],
                "open_batch": open_batch.to_dict() if open_batch else None,
                "open_batch_lane": self._BATCH_LANE_LABEL.get(open_batch.status) if open_batch else None,
            })
        return {"projects": projects}

    def pause(self, project: str, *, reason: str | None = None) -> dict[str, Any]:
        self.store.pause_pipeline(project, reason=reason)
        return self.status(project)

    def resume(self, project: str) -> dict[str, Any]:
        self.store.resume_pipeline(project)
        return self.status(project)

    def retry_handoff(self, project: str, handoff_id: str) -> dict[str, Any]:
        handoff = self.store.get_handoff(handoff_id)
        if handoff is None or handoff.project != project:
            return {"error": "HANDOFF_NOT_FOUND", "project": project, "handoff_id": handoff_id}
        try:
            updated = self.store.retry_handoff(handoff_id)
        except InvalidHandoffTransitionError as exc:
            return {"error": "INVALID_TRANSITION", "project": project, "handoff_id": handoff_id, "reason": str(exc)}
        return {"project": project, "handoff": updated.to_dict()}

    def events(self, project: str, limit: int = 50) -> dict[str, Any]:
        return {"project": project, "events": self.store.list_events(project, limit)}

    def run_once(self, project: str) -> dict[str, Any]:
        if self.engine is None:
            return {"error": "ENGINE_NOT_CONFIGURED"}
        return self.engine.tick(project).to_dict()

    def force_regression(self, project: str) -> dict[str, Any]:
        """Explicit, operator-requested regression run (item 8's own
        "Force Regression" action) -- creates a batch from whatever is
        currently INTEGRATED-but-unbatched, even if batch_size/
        batch_max_wait_seconds hasn't naturally been reached yet. A
        no-op (with a clear reason) if nothing is pending."""
        pending = self.store.pending_batch_handoffs(project)
        if not pending:
            return {"error": "NOTHING_PENDING", "project": project}
        batch = self.store.create_batch(project, [h.id for h in pending])
        return {"project": project, "batch": batch.to_dict()}

    def promote(self, project: str, batch_id: str) -> dict[str, Any]:
        """Explicit promotion to main -- the only path if auto_promote_
        enabled is False (the default) for this project. Refuses any
        batch not currently MERGE_READY."""
        if self.engine is None:
            return {"error": "ENGINE_NOT_CONFIGURED"}
        batch = self.store.get_batch(batch_id)
        if batch is None or batch.project != project:
            return {"error": "BATCH_NOT_FOUND", "project": project, "batch_id": batch_id}
        if batch.status != MERGE_READY:
            return {"error": "NOT_MERGE_READY", "status": batch.status}
        return self.engine.promote_to_main(project, batch_id)
