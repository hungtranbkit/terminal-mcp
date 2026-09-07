"""Release lifecycle -- policy layer (docs/REQUIREMENTS.md §20.6 Phase
C). Wraps release_store.py's real state machine with the two explicit
policies the task itself asks for:

1. A known-good artifact + rollback plan is REQUIRED (not optional) on
   any `prod` release -- refused at creation time, never silently
   allowed through with these blank.
2. Advancing a `prod` release into `DEPLOYING` requires an explicit
   human approval (`approved_by`, a real, non-empty identity string) --
   never auto-approved, regardless of risk_level.

Environment model + "no production deploy/secret access by default"
(a Release/Deploy Agent role, §20.2's own Capability Profile `role`
field already supports this as a free-string value -- e.g. "release" --
with zero schema change) is disclosed as PARTIAL here: this module
enforces the explicit-approval-string requirement and records a real,
queryable audit trail (release_events), but does NOT technically
enforce WHO is allowed to supply that approval via a session-identity
check -- this project's MCP tool layer has no caller-identity system
wired to Capability Profiles for that purpose today. A future increment
could add that; this module's own honest job is the state machine +
the two explicit content-of-request policies above.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .release_store import DEPLOYING, ENVIRONMENTS, ROLLED_BACK, InvalidReleaseTransitionError, ReleaseStore

PROD_ENVIRONMENT = "prod"


class ReleaseService:
    def __init__(self, store: ReleaseStore) -> None:
        self.store = store

    def create_release(self, *, project: str, task_id: str, environment: str, artifact_ref: str,
                       known_good_artifact_ref: str | None = None,
                       rollback_plan: str | None = None) -> dict[str, Any]:
        if environment not in ENVIRONMENTS:
            return {"error": "INVALID_ENVIRONMENT", "environment": environment, "valid": list(ENVIRONMENTS)}
        if not artifact_ref:
            return {"error": "ARTIFACT_REF_REQUIRED"}
        if environment == PROD_ENVIRONMENT:
            missing = []
            if not known_good_artifact_ref:
                missing.append("known_good_artifact_ref")
            if not rollback_plan:
                missing.append("rollback_plan")
            if missing:
                return {"error": "PROD_RELEASE_REQUIRES_ROLLBACK_PLAN", "missing_fields": missing,
                        "reason": "a known-good artifact + rollback plan is required (not optional) "
                                 "for any production release"}
        release = self.store.create_release(
            project=project, task_id=task_id, environment=environment, artifact_ref=artifact_ref,
            known_good_artifact_ref=known_good_artifact_ref, rollback_plan=rollback_plan,
        )
        return {"release": release.to_dict()}

    def advance_release(self, release_id: str, to_status: str, *, approved_by: str | None = None,
                        reason: str | None = None) -> dict[str, Any]:
        release = self.store.get_release(release_id)
        if release is None:
            return {"error": "RELEASE_NOT_FOUND", "release_id": release_id}
        if release.environment == PROD_ENVIRONMENT and to_status == DEPLOYING and not approved_by:
            return {"error": "PROD_DEPLOY_REQUIRES_APPROVAL", "release_id": release_id,
                    "reason": "advancing a prod release into DEPLOYING requires an explicit "
                             "human approval (approved_by) -- never auto-approved"}
        extra_fields = None
        if approved_by:
            extra_fields = {"approved_by": approved_by, "approved_at": datetime.now(timezone.utc).isoformat()}
        try:
            updated = self.store.transition_release(
                release_id, to_status, reason=reason, actor=approved_by, extra_fields=extra_fields,
            )
        except InvalidReleaseTransitionError as exc:
            return {"error": "INVALID_RELEASE_TRANSITION", "release_id": release_id, "detail": str(exc)}
        return {"release": updated.to_dict()}

    def rollback_release(self, release_id: str, *, reason: str, actor: str | None = None) -> dict[str, Any]:
        if not reason:
            return {"error": "ROLLBACK_REASON_REQUIRED"}
        release = self.store.get_release(release_id)
        if release is None:
            return {"error": "RELEASE_NOT_FOUND", "release_id": release_id}
        try:
            updated = self.store.transition_release(release_id, ROLLED_BACK, reason=reason, actor=actor)
        except InvalidReleaseTransitionError as exc:
            return {"error": "INVALID_RELEASE_TRANSITION", "release_id": release_id, "detail": str(exc)}
        return {"release": updated.to_dict()}

    def status(self, release_id: str) -> dict[str, Any]:
        release = self.store.get_release(release_id)
        if release is None:
            return {"error": "RELEASE_NOT_FOUND", "release_id": release_id}
        return {"release": release.to_dict(), "events": self.store.list_events(release_id)}

    def list_releases(self, *, project: str | None = None) -> dict[str, Any]:
        return {"releases": [r.to_dict() for r in self.store.list_releases(project=project)]}
