"""ProjectFlow Control Tower read model + safe controls (issue #106, P2).

ONE OBJECT, MANY READERS. `snapshot()` (`autopilot_status/v1`) is what the
dashboard renders, what the Gateway Task Center (#115) consumes over its BFF,
and what a read-only ChatGPT report summarizes -- so all three read the same
canonical rows and cannot disagree.

HONESTY RULES, enforced here rather than left to a UI:
  * no percentage anywhere: outcome progress is `completed/total` tasks, and an
    outcome is never "done" because its tasks are (outcomes.py);
  * CODE_READY, DEVICE_QA and RELEASE_READY are separate and UNKNOWN unless a
    verified fact establishes them -- verified code is not device QA, and
    neither is a release (a release additionally needs owner approval);
  * in-flight work nobody has observed recently, or whose node is
    unreachable, is shown UNKNOWN, never RUNNING;
  * prompts are not part of the read model; evidence strings are redacted.

CONTROLS reuse ProjectService.pause/resume, which leave a lane paused by
anyone else untouched. Each control is audited on the event bus with the
authenticated actor. Notifications are DELTAS over the event log (`since_seq`):
only a verified milestone, a QA failure or an owner decision is news.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import autopilot as ap
from . import queue_store as qs
from .outcomes import OUTCOME_DONE
from .redaction import redact_text
from .verify_queue import evidence_verdict

SNAPSHOT_SCHEMA = "autopilot_status/v1"
UNKNOWN = "UNKNOWN"
READY = "READY"
NOT_READY = "NOT_READY"

_OBSERVED = (qs.DISPATCHING, qs.DISPATCH_UNCERTAIN, qs.RUNNING)
_EVIDENCE_KEYS = ("commit_sha", "command", "exit_code", "tests_passed", "tests_failed")
_NOTIFY = {"TASK_COMPLETED": "MILESTONE_VERIFIED", "VERIFY_FAIL": "QA_FAILED",
           "AUTOPILOT_OWNER_ATTENTION": "NEEDS_OWNER"}


def _safe(value: Any) -> Any:
    return redact_text(value)[:300] if isinstance(value, str) else value


class ControlTower:
    def __init__(self, *, store: qs.QueueStore, verify: Any, outcomes: Any, bus: Any,
                 projects: Any, policy: ap.AutopilotPolicy, loop: Any = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.verify = verify
        self.outcomes = outcomes
        self.bus = bus
        self.projects = projects
        self.policy = policy
        self.loop = loop
        self.clock = clock

    # -- read model --------------------------------------------------------

    def _observation(self, project_id: str) -> tuple[dict[str, Any] | None, bool]:
        report = (getattr(self.loop, "last_reports", None) or {}).get(project_id)
        if not report or "observed_at" not in report:
            return report, False
        stale_after = max(3 * self.policy.interval_seconds, 120.0)
        return report, (self.clock() - float(report["observed_at"])) <= stale_after

    def _mode(self, project_id: str) -> str:
        if not self.policy.enabled:
            return ap.STATE_DISABLED
        if self.policy.kill_switch:
            return ap.STATE_HALTED
        project = self.policy.project(project_id)
        if project is None:
            return ap.STATE_NOT_OPTED_IN
        return ap.MODE_DISPATCH if project.dispatch else ap.MODE_OBSERVE_ONLY

    def _paused_lanes(self, project_id: str) -> list[dict[str, Any]]:
        lanes = set(self.store.lanes_for_project(project_id))
        project = self.policy.project(project_id)
        if project is not None:
            lanes.update(project.lanes)
        paused = []
        for lane in sorted(lanes):
            state = self.store.lane_status(lane)
            if state["paused"]:
                paused.append({"lane": lane, "origin": state["paused_origin"],
                               "reason": _safe(state["paused_reason"])})
        return paused

    def _task_view(self, task: qs.QueueTask, report: dict[str, Any] | None,
                   fresh: bool) -> dict[str, Any]:
        display, stale = task.status, False
        if task.status in _OBSERVED:
            if not fresh:
                display, stale = UNKNOWN, True
            elif task.id in (report or {}).get("unknown", ()):
                display = UNKNOWN
            elif task.id in (report or {}).get("waiting_input", ()):
                display = "WAITING_INPUT"
        jobs = self.verify.list_jobs(task_id=task.id, limit=1) if self.verify else []
        evidence = None
        if task.status == qs.COMPLETED and task.verification_evidence:
            evidence = {k: _safe(task.verification_evidence[k])
                        for k in _EVIDENCE_KEYS if k in task.verification_evidence}
        return {
            "id": task.id, "title": _safe(task.title), "status": task.status,
            "display_status": display, "stale": stale, "lane": task.session,
            "node_id": task.execution_node_id or task.node_id, "outcome_id": task.outcome_id,
            "depends_on": list(task.depends_on), "attempt": task.attempt_count,
            "started_at": task.started_at, "completed_at": task.completed_at,
            "updated_at": task.updated_at, "last_error": _safe(task.last_error),
            "qa": self._qa_view(task, jobs[0], report if fresh else None) if jobs else None,
            "evidence_ref": evidence,
        }

    @staticmethod
    def _qa_view(task: qs.QueueTask, job: Any, report: dict[str, Any] | None) -> dict[str, Any]:
        """The verify job plus, when the autopilot sends QA, who it went to and
        why it is not moving -- the latter only from a FRESH observation."""
        meta = (task.metadata.get(ap.METADATA_KEY) or {}).get("qa") or {}
        mine = meta.get("job_id") == job.id
        waiting = next((item for item in (report or {}).get("qa_waiting", ())
                        if item.get("verify_job") == job.id), None)
        return {"job_id": job.id, "status": job.status, "attempt": job.attempt,
                "commit_sha": job.commit_sha,
                "verifier": job.verifier or (meta.get("verifier") if mine else None),
                "dispatches": int(meta.get("dispatches") or 0) if mine else 0,
                "waiting_reason": waiting.get("reason") if waiting else None,
                "waiting_detail": _safe(waiting.get("detail")) if waiting else None}

    def _readiness(self, tasks: list[qs.QueueTask], outcomes: list[Any]) -> dict[str, str]:
        live = [t for t in tasks if t.status not in (qs.CANCELLED, qs.SKIPPED)]
        if not live:
            code = UNKNOWN
        elif all(t.status == qs.COMPLETED and evidence_verdict(t.verification_evidence)[0]
                 for t in live):
            code = READY
        else:
            code = NOT_READY
        device = UNKNOWN
        verdicts = [t.verification_evidence.get("device_qa") for t in live
                    if isinstance(t.verification_evidence, dict)
                    and "device_qa" in t.verification_evidence]
        if verdicts:
            passed = [isinstance(v, dict) and v.get("passed") is True for v in verdicts]
            device = READY if all(passed) else NOT_READY
        if code == NOT_READY or device == NOT_READY:
            release = NOT_READY
        elif (code == READY and device == READY and outcomes
              and all(o.status == OUTCOME_DONE for o in outcomes)):
            # Facts allow a release; deploying still needs explicit owner approval.
            release = READY
        else:
            release = UNKNOWN
        return {"CODE_READY": code, "DEVICE_QA": device, "RELEASE_READY": release}

    def snapshot(self, project_id: str) -> dict[str, Any]:
        project = self.policy.project(project_id)
        report, fresh = self._observation(project_id)
        tasks = self.store.list_tasks_for_project(project_id, limit=500)
        outcomes = self.outcomes.list_outcomes(project_id=project_id)
        task_views = [self._task_view(t, report, fresh) for t in tasks]
        needs_owner = [{"task_id": t["id"], "status": t["status"], "reason": t["last_error"]}
                       for t in task_views if t["status"] in (qs.BLOCKED, qs.FAILED)]
        needs_owner += [{"task_id": t["id"], "status": "WAITING_INPUT", "reason": None}
                        for t in task_views if t["display_status"] == "WAITING_INPUT"]
        verifying = {t["id"] for t in task_views if t["status"] == qs.VERIFYING}
        if fresh:  # QA held for a human (no verdict, stale commit, budget spent)
            needs_owner += [{"task_id": item["task_id"], "status": qs.VERIFYING,
                             "reason": item["reason"]}
                            for item in (report or {}).get("owner_attention", ())
                            if item.get("task_id") in verifying]
        paused = self._paused_lanes(project_id)
        return {
            "schema": SNAPSHOT_SCHEMA, "project_id": project_id,
            "generated_at": qs.iso_now(),
            "autopilot": {
                "enabled": self.policy.enabled, "mode": self._mode(project_id),
                "paused": bool(paused), "paused_lanes": paused,
                "observer_fresh": fresh,
                "last_tick": ({k: report.get(k) for k in ("state", "observed_at", "idle_reason")}
                              if report else None),
                "approval": ({"approved_by": project.approved_by,
                              "approval_ref": project.approval_ref} if project else None),
            },
            "outcomes": [{
                "id": o.id, "title": _safe(o.title), "status": o.status,
                "acceptance_criteria": [_safe(c) for c in o.acceptance_criteria],
                "progress": {k: self.outcomes.progress(o.id)[k] for k in ("completed", "total")},
            } for o in outcomes],
            "tasks": task_views,
            "readiness": self._readiness(tasks, outcomes),
            "release_requires_owner_approval": True,
            "needs_owner": needs_owner,
        }

    def overview(self) -> dict[str, Any]:
        rows = []
        for project in self.policy.projects:
            snap = self.snapshot(project.project_id)
            counts: dict[str, int] = {}
            for task in snap["tasks"]:
                counts[task["display_status"]] = counts.get(task["display_status"], 0) + 1
            rows.append({"project_id": project.project_id, "mode": snap["autopilot"]["mode"],
                         "paused": snap["autopilot"]["paused"], "counts": counts,
                         "readiness": snap["readiness"],
                         "needs_owner": len(snap["needs_owner"])})
        return {"schema": "autopilot_overview/v1", "generated_at": qs.iso_now(), "projects": rows}

    # -- controls ------------------------------------------------------------

    def _audit(self, project_id: str, kind: str, actor: str, idempotency_key: str | None,
               payload: dict[str, Any]) -> None:
        key = f"control:{project_id}:{kind}:{idempotency_key}" if idempotency_key else None
        try:
            self.bus.publish(f"CONTROL_TOWER_{kind}", project_id=project_id, entity_type="project",
                             entity_id=project_id, actor=actor, idempotency_key=key,
                             payload={k: _safe(v) for k, v in payload.items()})
        except Exception:  # noqa: BLE001 -- the lane state is the truth; audit is best effort
            pass

    def pause(self, project_id: str, *, actor: str, reason: str | None,
              idempotency_key: str | None = None) -> dict[str, Any]:
        if self.policy.project(project_id) is None:
            return {"error": ap.STATE_NOT_OPTED_IN, "project_id": project_id}
        result = self.projects.pause(project_id, reason=reason, actor=actor)
        self._audit(project_id, "PAUSE", actor, idempotency_key,
                    {"reason": reason, "paused": ",".join(result.get("paused", []))})
        return result

    def resume(self, project_id: str, *, actor: str, verified_identity: str | None,
               idempotency_key: str | None = None) -> dict[str, Any]:
        project = self.policy.project(project_id)
        if project is None:
            return {"error": ap.STATE_NOT_OPTED_IN, "project_id": project_id}
        # Resuming re-enables dispatch: only a VERIFIED owner identity may.
        if not verified_identity or verified_identity not in project.owner_identities:
            self._audit(project_id, "RESUME_REFUSED", actor, None, {"reason": "not an owner"})
            return {"error": "OWNER_APPROVAL_REQUIRED", "project_id": project_id,
                    "detail": "resume needs a verified identity listed as a project owner"}
        # Never force: a lane paused by anyone else stays paused.
        result = self.projects.resume(project_id, actor=actor, force=False)
        self._audit(project_id, "RESUME", actor, idempotency_key,
                    {"resumed": ",".join(result.get("resumed", []))})
        return result

    # -- notifications ---------------------------------------------------------

    def notifications(self, project_id: str, *, since_seq: int = 0,
                      limit: int = 200) -> dict[str, Any]:
        events = self.bus.list_events(project_id=project_id, since_seq=int(since_seq),
                                      limit=max(1, min(int(limit), 500)))
        seen: set[tuple[str, str]] = set()
        items = []
        for event in events:
            kind = _NOTIFY.get(str(event.get("type")))
            if kind is None:
                continue
            marker = (kind, str(event.get("entity_id")))
            if marker in seen:
                continue
            seen.add(marker)
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            items.append({"kind": kind, "seq": event["seq"], "entity_id": event.get("entity_id"),
                          "at": event.get("created_at"),
                          "reason": _safe(payload.get("reason") or payload.get("detail"))})
        next_seq = max([int(since_seq)] + [int(e["seq"]) for e in events])
        return {"project_id": project_id, "notifications": items, "next_seq": next_seq}


def build_workspace_probe(session_ops: Any, repo_evidence: Any) -> Any:
    """lane -> {"dirty", "head"} read WHERE the lane lives: the session's cwd
    and node from the controller, then the node-aware repo-evidence collector
    the dispatch gate already uses. Any failure raises, which the autopilot
    reads as an UNKNOWN workspace (no dispatch) and an unknown HEAD (no QA)."""
    def probe(lane: str) -> dict[str, Any]:
        status = session_ops.terminal_status(lane) or {}
        if status.get("error"):
            raise RuntimeError(f"status {status.get('error')}")
        cwd = str(status.get("cwd") or "").strip()
        if not cwd:
            raise RuntimeError("session reports no cwd")
        evidence = repo_evidence(cwd, status.get("node_id"))
        return {"dirty": not evidence.clean, "head": evidence.head}
    return probe


def build_verifier_probe(session_ops: Any) -> Any:
    """verifier session -> {"available", "reason"}. Only a status read that
    succeeds counts as available; an unknown session or an offline node does
    not, and the reason is shown instead of sending blind."""
    def probe(session: str) -> dict[str, Any]:
        status = session_ops.terminal_status(session) or {}
        if status.get("error"):
            return {"available": False, "reason": str(status.get("error"))}
        return {"available": True, "reason": None}
    return probe


def build_autopilot_runtime(config: Any, *, queue: Any, bus: Any, outcomes: Any, projects: Any,
                            locks: Any, direct_tasks: Any, instance_id: str,
                            session_ops: Any = None, repo_evidence: Any = None,
                            ) -> tuple[ControlTower | None, ap.AutopilotLoop | None]:
    """Server wiring. The Control Tower read model exists whenever a queue is
    wired (it shows DISABLED when the policy is off). The loop exists only
    when the policy is enabled with at least one project, and the caller still
    has to start() it."""
    if queue is None or getattr(queue, "store", None) is None:
        return None, None
    policy = config.autopilot
    verify = getattr(queue, "verify_queue", None)
    loop = None
    if policy.enabled and policy.projects and direct_tasks is not None and verify is not None:
        autopilot = ap.ProjectAutopilot(
            store=queue.store, verify=verify, outcomes=outcomes, bus=bus, locks=locks,
            dispatcher=ap.DirectTaskDispatcher(direct_tasks), policy=policy,
            instance_id=instance_id,
            workspace_probe=(build_workspace_probe(session_ops, repo_evidence)
                             if session_ops is not None and repo_evidence is not None else None),
            verifier_probe=build_verifier_probe(session_ops) if session_ops is not None else None)
        loop = ap.AutopilotLoop(autopilot)
    tower = ControlTower(store=queue.store, verify=verify, outcomes=outcomes, bus=bus,
                         projects=projects, policy=policy, loop=loop)
    return tower, loop
