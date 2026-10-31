"""Policy objects for ProjectFlow Autopilot (issue #106).

Dependency-free on purpose: config.py builds these at load time, and must not
import the runtime (queue_store, coordinator, ...) to do so. Validation lives in
__post_init__, so an unsafe combination cannot exist in memory, whether it came
from YAML or from a test.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlannerProviderConfig:
    """P1 AI planner provider. `kind="deterministic"` (default) never leaves the
    process. `kind="gateway"` needs an https/loopback endpoint and a
    `token_file` PATH; an inline token is refused so a secret can never sit in
    config or be logged with it."""
    kind: str = "deterministic"
    endpoint: str | None = None
    token_file: str | None = None
    model: str | None = None
    max_calls_per_hour: int = 6
    max_tasks: int = 8
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.kind not in ("deterministic", "gateway"):
            raise ValueError("autopilot.planner.kind must be deterministic or gateway")
        if self.kind == "gateway":
            endpoint = str(self.endpoint or "")
            if not (endpoint.startswith("https://") or endpoint.startswith("http://127.0.0.1")
                    or endpoint.startswith("http://localhost")):
                raise ValueError("autopilot.planner.endpoint must be https or loopback")
            if not str(self.token_file or "").strip():
                raise ValueError("autopilot.planner.token_file is required for kind=gateway")
        if not 0 <= int(self.max_calls_per_hour) <= 60:
            raise ValueError("autopilot.planner.max_calls_per_hour must be 0..60")
        if not 1 <= int(self.max_tasks) <= 20:
            raise ValueError("autopilot.planner.max_tasks must be 1..20")


@dataclass(frozen=True)
class AutopilotProjectPolicy:
    project_id: str
    dispatch: bool = False
    approved_by: str | None = None
    approval_ref: str | None = None
    lanes: tuple[str, ...] = ()
    max_in_flight: int = 1
    max_repair_attempts: int = 2
    verify_required_capabilities: tuple[str, ...] = ()
    # Identities allowed to RESUME (re-enable dispatch) from the Control Tower.
    # Empty means approved_by alone. Pausing is a safety action open to any
    # authenticated operator.
    owners: tuple[str, ...] = ()

    @property
    def owner_identities(self) -> tuple[str, ...]:
        if self.owners:
            return self.owners
        return (self.approved_by,) if self.approved_by else ()

    def __post_init__(self) -> None:
        if not str(self.project_id or "").strip():
            raise ValueError("autopilot project_id is required")
        if self.dispatch and not (str(self.approved_by or "").strip()
                                  and str(self.approval_ref or "").strip()):
            raise ValueError(f"autopilot dispatch for {self.project_id} requires approved_by "
                             f"and approval_ref -- dispatch is never implied")
        if self.dispatch and not self.lanes:
            raise ValueError(f"autopilot dispatch for {self.project_id} requires an explicit "
                             f"lanes allowlist")
        if not 1 <= int(self.max_in_flight) <= 8:
            raise ValueError("max_in_flight must be 1..8")
        if not 0 <= int(self.max_repair_attempts) <= 5:
            raise ValueError("max_repair_attempts must be 0..5")


@dataclass(frozen=True)
class AutopilotPolicy:
    enabled: bool = False
    kill_switch: bool = False
    projects: tuple[AutopilotProjectPolicy, ...] = ()
    lease_seconds: float = 60.0
    task_lease_seconds: float = 300.0
    event_batch: int = 200
    interval_seconds: float = 15.0
    planner: PlannerProviderConfig = PlannerProviderConfig()

    def project(self, project_id: str) -> AutopilotProjectPolicy | None:
        return next((p for p in self.projects if p.project_id == project_id), None)


def load_autopilot_config(raw: Any) -> AutopilotPolicy:
    """Parse the `autopilot:` config block. Absent -> everything off."""
    if raw is None:
        return AutopilotPolicy()
    if not isinstance(raw, dict):
        raise ValueError("autopilot must be a mapping")
    allowed = {"enabled", "kill_switch", "projects", "lease_seconds", "task_lease_seconds",
               "event_batch", "interval_seconds", "planner"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"autopilot: unknown key(s) {sorted(unknown)}")
    projects_raw = raw.get("projects") or []
    if not isinstance(projects_raw, list):
        raise ValueError("autopilot.projects must be a list")
    projects = []
    for index, item in enumerate(projects_raw):
        if not isinstance(item, dict):
            raise ValueError(f"autopilot.projects[{index}] must be a mapping")
        if "token" in item or "secret" in item:
            raise ValueError(f"autopilot.projects[{index}]: inline secrets are not accepted")
        lanes = item.get("lanes") or []
        caps = item.get("verify_required_capabilities") or []
        if not isinstance(lanes, list) or not all(isinstance(x, str) for x in lanes):
            raise ValueError(f"autopilot.projects[{index}].lanes must be a list of strings")
        projects.append(AutopilotProjectPolicy(
            project_id=str(item.get("project_id") or ""),
            dispatch=item.get("dispatch") is True,
            approved_by=item.get("approved_by"), approval_ref=item.get("approval_ref"),
            lanes=tuple(lanes), max_in_flight=int(item.get("max_in_flight", 1)),
            max_repair_attempts=int(item.get("max_repair_attempts", 2)),
            verify_required_capabilities=tuple(str(c) for c in caps),
            owners=tuple(str(o) for o in (item.get("owners") or []))))
    ids = [p.project_id for p in projects]
    if len(ids) != len(set(ids)):
        raise ValueError("autopilot.projects has duplicate project_id")
    lane_owner: dict[str, str] = {}
    for project in projects:
        for lane in project.lanes:
            if lane_owner.setdefault(lane, project.project_id) != project.project_id:
                raise ValueError(f"autopilot lane {lane!r} is allowlisted for two projects")
    planner_raw = raw.get("planner") or {}
    if not isinstance(planner_raw, dict):
        raise ValueError("autopilot.planner must be a mapping")
    if "token" in planner_raw or "api_key" in planner_raw:
        raise ValueError("autopilot.planner: inline secrets are not accepted; use token_file")
    planner = PlannerProviderConfig(**{k: v for k, v in planner_raw.items()
                                       if k in PlannerProviderConfig.__dataclass_fields__})
    return AutopilotPolicy(
        enabled=raw.get("enabled") is True, kill_switch=raw.get("kill_switch") is True,
        projects=tuple(projects),
        lease_seconds=float(raw.get("lease_seconds", AutopilotPolicy.lease_seconds)),
        task_lease_seconds=float(raw.get("task_lease_seconds", AutopilotPolicy.task_lease_seconds)),
        event_batch=int(raw.get("event_batch", AutopilotPolicy.event_batch)),
        interval_seconds=float(raw.get("interval_seconds", AutopilotPolicy.interval_seconds)),
        planner=planner)
