"""Bounded project-level workflows composed from existing Terminal MCP services."""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from .config import default_config_path


MAX_PROJECT_OUTPUT_CHARS = 12_000
MAX_PROJECT_TAIL_LINES = 20
MAX_PROJECT_TAIL_CHARS = 1_000
BUSY_STATES = {"BUSY", "RUNNING", "WORKING", "DISPATCHING", "VERIFYING"}
TAIL_STATES = {
    "BLOCKED", "ERROR", "FAILED", "FINISHED", "DONE", "COMPLETED",
    "UNKNOWN", "WAITING_INPUT", "NEEDS_INPUT", "NEEDS_REPLAN",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_SAFE_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")


@dataclass(frozen=True)
class HealthProbe:
    probe_id: str
    kind: str
    value: str
    public: bool = False


@dataclass(frozen=True)
class DeployProfile:
    build_command_id: str | None
    service_id: str
    health_probe_ids: tuple[str, ...]
    public_probe_ids: tuple[str, ...]
    rollback_capture_method: str
    preview_safe: bool


@dataclass(frozen=True)
class ProjectProfile:
    project_id: str
    aliases: tuple[str, ...]
    repo_root: str
    preferred_targets: tuple[str, ...]
    target_capabilities: dict[str, tuple[str, ...]]
    bindings: tuple[str, ...]
    sessions: tuple[str, ...]
    git_remote: str | None
    health_probes: tuple[HealthProbe, ...]
    deploy: DeployProfile | None

    @property
    def targets(self) -> tuple[str, ...]:
        ordered = [*self.preferred_targets,
                   *(f"binding:{v}" for v in self.bindings),
                   *(f"session:{v}" for v in self.sessions)]
        return tuple(dict.fromkeys(ordered))

    def normalize_target(self, target: str) -> str | None:
        if target in self.targets:
            return target
        for configured in self.targets:
            if configured in {f"binding:{target}", f"session:{target}"}:
                return configured
        if target in self.bindings:
            return f"binding:{target}"
        if target in self.sessions:
            return f"session:{target}"
        return None


class ProjectProfileRegistry:
    """Operator allowlist loaded from the existing config file.

    An absent section produces an empty registry and changes no legacy behavior.
    """

    def __init__(self, profiles: list[ProjectProfile] | tuple[ProjectProfile, ...] = ()) -> None:
        self._profiles = {profile.project_id.casefold(): profile for profile in profiles}
        self._aliases: dict[str, ProjectProfile] = {}
        for profile in profiles:
            for name in (profile.project_id, *profile.aliases):
                key = name.casefold()
                if key in self._aliases and self._aliases[key] != profile:
                    raise ValueError(f"duplicate project profile alias: {name}")
                self._aliases[key] = profile

    @classmethod
    def from_config(cls, path: str | Path | None = None) -> "ProjectProfileRegistry":
        config_path = Path(path) if path else default_config_path()
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        entries = raw.get("project_profiles", [])
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ValueError("project_profiles must be a list")
        return cls([_parse_profile(entry, index) for index, entry in enumerate(entries)])

    def get(self, project: str) -> ProjectProfile | None:
        if not isinstance(project, str):
            return None
        return self._aliases.get(project.strip().casefold())


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _parse_profile(raw: Any, index: int) -> ProjectProfile:
    if not isinstance(raw, dict):
        raise ValueError(f"project_profiles[{index}] must be a mapping")
    project_id = raw.get("project_id") or raw.get("id")
    repo_root = raw.get("repo_root")
    if not isinstance(project_id, str) or not _SAFE_ID.fullmatch(project_id):
        raise ValueError(f"project_profiles[{index}].project_id is invalid")
    if not isinstance(repo_root, str) or not Path(repo_root).is_absolute():
        raise ValueError(f"project_profiles[{index}].repo_root must be absolute")
    preferred_raw = raw.get("preferred_targets", [])
    if not isinstance(preferred_raw, list):
        raise ValueError(f"project_profiles[{index}].preferred_targets must be a list")
    preferred: list[str] = []
    capabilities: dict[str, tuple[str, ...]] = {}
    for pos, item in enumerate(preferred_raw):
        if isinstance(item, str):
            target, caps = item, ()
        elif isinstance(item, dict):
            target = item.get("target")
            caps = _strings(item.get("capabilities", []), f"preferred_targets[{pos}].capabilities")
        else:
            raise ValueError(f"project_profiles[{index}].preferred_targets[{pos}] is invalid")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"project_profiles[{index}].preferred_targets[{pos}].target is required")
        preferred.append(target.strip())
        capabilities[target.strip()] = caps
    probes_raw = raw.get("health_probes", [])
    if not isinstance(probes_raw, list):
        raise ValueError(f"project_profiles[{index}].health_probes must be a list")
    probes: list[HealthProbe] = []
    for pos, probe in enumerate(probes_raw):
        if not isinstance(probe, dict):
            raise ValueError(f"project_profiles[{index}].health_probes[{pos}] must be a mapping")
        probe_id, kind = probe.get("id"), probe.get("kind")
        value = probe.get("url") if kind == "http" else probe.get("check_id")
        if not all(isinstance(v, str) and v for v in (probe_id, kind, value)):
            raise ValueError(f"project_profiles[{index}].health_probes[{pos}] is incomplete")
        if kind not in {"http", "fixture"}:
            raise ValueError(f"unsupported health probe kind: {kind}")
        if kind == "http" and urlparse(value).scheme not in {"http", "https"}:
            raise ValueError(f"project_profiles[{index}].health_probes[{pos}].url must use http(s)")
        probes.append(HealthProbe(probe_id, kind, value, bool(probe.get("public", False))))
    deploy_raw = raw.get("deploy")
    deploy = None
    if deploy_raw is not None:
        if not isinstance(deploy_raw, dict):
            raise ValueError(f"project_profiles[{index}].deploy must be a mapping")
        deploy = DeployProfile(
            build_command_id=deploy_raw.get("build_command_id"),
            service_id=str(deploy_raw.get("service_id") or ""),
            health_probe_ids=_strings(deploy_raw.get("health_probe_ids", []), "deploy.health_probe_ids"),
            public_probe_ids=_strings(deploy_raw.get("public_probe_ids", []), "deploy.public_probe_ids"),
            rollback_capture_method=str(deploy_raw.get("rollback_capture_method") or ""),
            preview_safe=bool(deploy_raw.get("preview_safe", False)),
        )
    return ProjectProfile(
        project_id=project_id,
        aliases=_strings(raw.get("aliases", []), "aliases"),
        repo_root=str(Path(repo_root)),
        preferred_targets=tuple(preferred),
        target_capabilities=capabilities,
        bindings=_strings(raw.get("bindings", []), "bindings"),
        sessions=_strings(raw.get("sessions", []), "sessions"),
        git_remote=raw.get("git_remote"),
        health_probes=tuple(probes),
        deploy=deploy,
    )


def _json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False))


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)] + "…", True


def _cap_result(result: dict[str, Any], limit: int) -> dict[str, Any]:
    """Keep a valid structured response while enforcing a hard JSON-char cap."""
    result["truncated"] = bool(result.get("truncated", False))
    result.setdefault("omitted_count", 0)
    if _json_size(result) <= limit:
        return result
    result["truncated"] = True
    for row in result.get("targets", []):
        if row.pop("tail", None) is not None:
            result["omitted_count"] += 1
    for key in ("ready", "next_actions", "blockers"):
        values = result.get(key)
        while isinstance(values, list) and len(values) > 3 and _json_size(result) > limit:
            values.pop()
            result["omitted_count"] += 1
    targets = result.get("targets")
    while isinstance(targets, list) and len(targets) > 1 and _json_size(result) > limit:
        targets.pop()
        result["omitted_count"] += 1
    for key in ("ready", "next_actions", "blockers", "health"):
        value = result.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for field in ("reason", "detail", "error"):
                        if field in item:
                            item[field] = _bounded_text(item[field], 160)[0]
    result["next_cursor"] = result.get("omitted_count") or None
    if _json_size(result) > limit:
        keep = {k: result.get(k) for k in ("status", "project", "workflow_id", "summary",
                                           "truncated", "omitted_count", "next_cursor") if k in result}
        keep["truncated"] = True
        keep["omitted_count"] = result.get("omitted_count", 0) + 1
        return keep
    return result


class ProjectWorkflowTools:
    def __init__(self, registry: ProjectProfileRegistry, compact: Any, queue: Any,
                 supervisor: Any, locks: Any, audit: Any, *, controller: Any = None,
                 probe_runner: Callable[[HealthProbe], dict[str, Any]] | None = None,
                 command_runner: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
                 git_runner: Callable[[str], dict[str, Any]] | None = None) -> None:
        self.registry = registry
        self.compact = compact
        self.queue = queue
        self.supervisor = supervisor
        self.locks = locks
        self.audit = audit
        self.controller = controller
        self.probe_runner = probe_runner or self._run_probe
        self.command_runner = command_runner or self._run_command
        self.git_runner = git_runner or self._git_info

    def _profile(self, project: str) -> ProjectProfile | dict[str, Any]:
        profile = self.registry.get(project)
        return profile or {"status": "BLOCKED", "error": "PROJECT_NOT_ALLOWLISTED", "project": project}

    @staticmethod
    def _git_info(repo_root: str) -> dict[str, Any]:
        root = Path(repo_root)
        if not root.is_dir():
            return {"status": "UNAVAILABLE", "error": "REPO_ROOT_NOT_FOUND"}
        def run(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(["git", "-C", str(root), *args], check=False,
                                  capture_output=True, text=True, timeout=5)
        branch = run("branch", "--show-current")
        sha = run("rev-parse", "HEAD")
        dirty = run("status", "--porcelain")
        if sha.returncode:
            return {"status": "UNAVAILABLE", "error": "NOT_A_GIT_REPOSITORY"}
        return {"status": "OK", "branch": branch.stdout.strip() or "DETACHED",
                "sha": sha.stdout.strip(), "dirty": bool(dirty.stdout.strip())}

    @staticmethod
    def _run_probe(probe: HealthProbe) -> dict[str, Any]:
        started = time.monotonic()
        if probe.kind == "fixture":
            ok = probe.value == "fixture.pass"
            return {"id": probe.probe_id, "status": "OK" if ok else "FAILED",
                    "latency_ms": round((time.monotonic() - started) * 1000, 1)}
        try:
            request = urllib.request.Request(probe.value, method="GET")
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - operator allowlist
                code = int(response.status)
            return {"id": probe.probe_id, "status": "OK" if 200 <= code < 400 else "FAILED",
                    "http_status": code, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001 - normalized probe failure
            return {"id": probe.probe_id, "status": "FAILED",
                    "error": type(exc).__name__,
                    "latency_ms": round((time.monotonic() - started) * 1000, 1)}

    @staticmethod
    def _run_command(command_id: str, context: dict[str, Any]) -> dict[str, Any]:
        # Fixed IDs only. A project profile cannot smuggle argv or shell text.
        if command_id in {"fixture.noop", "fixture.build"}:
            return {"status": "OK", "command_id": command_id}
        return {"status": "BLOCKED", "error": "COMMAND_ID_NOT_ALLOWLISTED", "command_id": command_id}

    def _events(self, target: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows = self.supervisor.list_events(target=target, limit=10).get("events", [])
        ready, blocked = [], []
        for row in rows:
            state = str(row.get("state", "")).upper()
            item = {"target": target, "state": state, "event_type": row.get("event_type"),
                    "reason": _bounded_text(row.get("reason"), 240)[0]}
            if state in {"READY", "DONE", "VERIFIED_DONE", "COMPLETED"}:
                ready.append(item)
            elif state in {"BLOCKED", "FAILED", "NEEDS_INPUT", "NEEDS_REPLAN"}:
                blocked.append(item)
        return ready[:3], blocked[:3]

    def project_check(self, project: str, targets: list[str] | None = None, *,
                      include_git: bool = True, include_deploy: bool = True,
                      include_health: bool = True, tail_lines: int = 8,
                      max_output_chars: int = MAX_PROJECT_OUTPUT_CHARS) -> dict[str, Any]:
        profile = self._profile(project)
        if isinstance(profile, dict):
            return profile
        if (isinstance(tail_lines, bool) or not isinstance(tail_lines, int) or
                not 0 <= tail_lines <= MAX_PROJECT_TAIL_LINES):
            return {"status": "BLOCKED", "error": "INVALID_TAIL_LINES", "allowed": "0..20"}
        if (isinstance(max_output_chars, bool) or not isinstance(max_output_chars, int) or
                not 1_000 <= max_output_chars <= MAX_PROJECT_OUTPUT_CHARS):
            return {"status": "BLOCKED", "error": "INVALID_MAX_OUTPUT_CHARS", "allowed": "1000..12000"}
        allowed = set(profile.targets)
        if targets is not None and (not isinstance(targets, list) or len(targets) > 25 or
                                    any(not isinstance(target, str) or not target for target in targets)):
            return {"status": "BLOCKED", "error": "INVALID_TARGETS", "max_targets": 25}
        requested = list(targets) if targets is not None else list(profile.targets)
        selected = [profile.normalize_target(target) or target for target in requested]
        disallowed = [target for target in selected if target not in allowed]
        if disallowed:
            return {"status": "BLOCKED", "error": "TARGET_NOT_ALLOWLISTED", "targets": disallowed}
        rows, blockers, ready = [], [], []
        counts: dict[str, int] = {}
        for target in selected:
            status = self.compact._status(target)
            if "error" in status:
                row = {"target": target, "state": "UNREACHABLE", "error": status["error"],
                       "reason": _bounded_text(status.get("reason") or status["error"], 240)[0]}
                blockers.append({"target": target, "reason": row["reason"]})
            else:
                state = str(status.get("state", "UNKNOWN")).upper()
                row = {"target": target, "target_type": status.get("target_type"), "state": state,
                       "input_required": bool(status.get("input_required", False)),
                       "reason": _bounded_text(status.get("reason"), 240)[0]}
                event_ready, event_blocked = self._events(target)
                if tail_lines and (state in TAIL_STATES or event_ready):
                    tail = self.compact._tail(target, tail_lines)
                    rendered, clipped = _bounded_text(tail.get("output", ""), MAX_PROJECT_TAIL_CHARS)
                    row["tail"] = rendered
                    row["tail_truncated"] = clipped or bool(tail.get("truncated"))
                ready.extend(event_ready)
                blockers.extend(event_blocked)
            counts[row["state"]] = counts.get(row["state"], 0) + 1
            rows.append(row)
        health = [self.probe_runner(probe) for probe in profile.health_probes] if include_health else []
        blockers.extend({"probe": row["id"], "reason": row.get("error") or "health probe failed"}
                        for row in health if row.get("status") != "OK")
        git = self.git_runner(profile.repo_root) if include_git else {"status": "SKIPPED"}
        if include_git and git.get("status") != "OK":
            blockers.append({"component": "git", "reason": git.get("error") or "git unavailable"})
        deploy = ({"status": "CONFIGURED", "service_id": profile.deploy.service_id,
                   "preview_safe": profile.deploy.preview_safe}
                  if include_deploy and profile.deploy else
                  {"status": "NOT_CONFIGURED" if include_deploy else "SKIPPED"})
        node_health = ({"counts": {}, "healthy": 0, "total": 0, "blockers": []}
                       if not include_health or self.controller is None else
                       self.controller.node_health_summary())
        blockers.extend({"node_id": row.get("node_id"), "reason": row.get("reason"),
                         "state": row.get("state")}
                        for row in node_health.get("blockers", []))
        result = {"status": "BLOCKED" if blockers else "OK", "project": profile.project_id,
                  "summary": {"target_count": len(rows), "state_counts": counts,
                              "blocker_count": len(blockers), "ready_count": len(ready)},
                  "targets": rows, "git": git, "deploy": deploy, "health": health,
                  "node_health": node_health,
                  "blockers": blockers[:20], "ready": ready[:20],
                  "next_actions": (["resolve blockers before dispatch"] if blockers else
                                   ["project is ready for the next requested action"]),
                  "untrusted_output": True, "untrusted_fields": ["targets[].tail"]}
        return _cap_result(result, max_output_chars)

    def _idempotent_begin(self, namespace: str, key: str | None) -> tuple[str, dict[str, Any] | None]:
        effective = key or str(uuid.uuid4())
        if not _SAFE_ID.fullmatch(effective):
            return "", {"status": "BLOCKED", "error": "INVALID_IDEMPOTENCY_KEY"}
        audit_key = f"{namespace}:{effective}"
        if not self.audit.claim_idempotency_key(audit_key, stale_after_seconds=900):
            existing = self.audit.get_idempotent_result(audit_key)
            return audit_key, existing or {"status": "BUSY", "error": "DUPLICATE_IN_PROGRESS"}
        return audit_key, None

    def _store(self, audit_key: str, result: dict[str, Any]) -> dict[str, Any]:
        bounded = _cap_result(result, MAX_PROJECT_OUTPUT_CHARS)
        self.audit.store_idempotent_result(audit_key, bounded)
        return bounded

    def _finish_dispatch(self, audit_key: str, result: dict[str, Any]) -> dict[str, Any]:
        self.audit.record(action="project_dispatch", session=result.get("selected_target"),
                          result=str(result.get("status", "FAILED")),
                          reason=result.get("reason") or result.get("error"),
                          correlation_id=result.get("workflow_id"),
                          actor=f"project:{result.get('project', 'unknown')}")
        return self._store(audit_key, result)

    def project_dispatch(self, project: str, task: str, target: str | None = None, *,
                         wait_for_accept: bool = True, queue_if_busy: bool = True,
                         idempotency_key: str | None = None, timeout: float = 30) -> dict[str, Any]:
        profile = self._profile(project)
        if isinstance(profile, dict):
            return profile
        if not isinstance(task, str) or not task.strip():
            return {"status": "BLOCKED", "error": "TASK_REQUIRED"}
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            return {"status": "BLOCKED", "error": "INVALID_TIMEOUT", "allowed": "0 < timeout <= 30"}
        normalized_target = profile.normalize_target(target) if target is not None else None
        if target is not None and normalized_target is None:
            return {"status": "BLOCKED", "error": "TARGET_NOT_ALLOWLISTED", "target": target}
        audit_key, replay = self._idempotent_begin(f"project-dispatch:{profile.project_id}", idempotency_key)
        if replay is not None:
            return replay
        candidates = [normalized_target] if normalized_target else list(profile.targets)
        statuses = [(candidate, self.compact._status(candidate)) for candidate in candidates]
        selected = next((candidate for candidate, row in statuses
                         if "error" not in row and str(row.get("state", "UNKNOWN")).upper() not in BUSY_STATES),
                        candidates[0] if candidates else None)
        if selected is None:
            return self._finish_dispatch(audit_key, {"status": "BLOCKED", "error": "NO_ALLOWLISTED_TARGET",
                                                     "project": profile.project_id,
                                                     "workflow_id": str(uuid.uuid4())})
        selected_status = next(row for candidate, row in statuses if candidate == selected)
        state = str(selected_status.get("state", "UNKNOWN")).upper()
        workflow_id = str(uuid.uuid4())
        if state in BUSY_STATES or "error" in selected_status:
            if not queue_if_busy:
                return self._finish_dispatch(audit_key, {"status": "BUSY", "project": profile.project_id,
                                                         "selected_target": selected, "queued": False, "sent": False,
                                                         "reason": selected_status.get("error") or state,
                                                         "workflow_id": workflow_id})
            kind, value = self.compact._resolve(selected)
            if kind is None:
                return self._finish_dispatch(audit_key, {"status": "BLOCKED", "project": profile.project_id,
                                                         "workflow_id": workflow_id, **value})
            session = value
            if kind == "binding":
                binding = self.compact.terminal.terminal_get_binding(value)
                if "error" in binding or not binding.get("session"):
                    return self._finish_dispatch(audit_key, {
                        "status": "BLOCKED", "project": profile.project_id,
                        "workflow_id": workflow_id, "selected_target": selected,
                        "error": binding.get("error", "BINDING_NOT_FOUND")})
                session = binding["session"]
            queued = self.queue.enqueue(session, task, title=f"Project dispatch: {profile.project_id}",
                                        metadata={"project": profile.project_id, "workflow_id": workflow_id},
                                        request_key=audit_key)
            result = {"status": queued.get("status", "FAILED"), "project": profile.project_id,
                      "selected_target": selected, "queued": "error" not in queued, "sent": False,
                      "queue_id": queued.get("task_id"), "queue_position": queued.get("queue_position"),
                      "deduplicated": queued.get("deduplicated", False), "workflow_id": workflow_id}
            if "error" in queued:
                result.update(status="FAILED", error=queued["error"])
            return self._finish_dispatch(audit_key, result)
        sent = self.compact.send_task(selected, task, wait_for_accept, timeout,
                                      idempotency_key=f"{audit_key}:send")
        result = {"status": sent.get("status", "FAILED"), "project": profile.project_id,
                  "selected_target": selected, "queued": False,
                  "sent": sent.get("status") == "SUBMIT_CONFIRMED",
                  "submission_id": sent.get("submission_id"), "evidence": sent.get("evidence", {}),
                  "reason": sent.get("reason"), "workflow_id": workflow_id}
        return self._finish_dispatch(audit_key, result)

    def _audit_phase(self, workflow_id: str, project: str, phase: str, result: str,
                     reason: str | None = None) -> None:
        self.audit.record(action=f"deploy_preview:{phase}", session=None, result=result,
                          reason=reason, correlation_id=workflow_id, actor=f"project:{project}")

    def deploy_preview(self, project: str, sha: str | None = None,
                       source_branch: str | None = None, *, verify: bool = True,
                       wait: bool = True, timeout: float = 600,
                       idempotency_key: str | None = None) -> dict[str, Any]:
        profile = self._profile(project)
        if isinstance(profile, dict):
            return profile
        if sha is not None and not _SAFE_SHA.fullmatch(sha):
            return {"status": "BLOCKED", "error": "INVALID_SHA"}
        if source_branch is not None and not _SAFE_REF.fullmatch(source_branch):
            return {"status": "BLOCKED", "error": "INVALID_SOURCE_BRANCH"}
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 600:
            return {"status": "BLOCKED", "error": "INVALID_TIMEOUT", "allowed": "1..600"}
        deploy = profile.deploy
        missing = []
        if deploy is None:
            missing.append("deploy")
        else:
            if not deploy.service_id:
                missing.append("deploy.service_id")
            if not deploy.rollback_capture_method:
                missing.append("deploy.rollback_capture_method")
            if not deploy.preview_safe:
                missing.append("deploy.preview_safe=true")
            known_probes = {probe.probe_id for probe in profile.health_probes}
            for probe_id in (*deploy.health_probe_ids, *deploy.public_probe_ids):
                if probe_id not in known_probes:
                    missing.append(f"health_probes[{probe_id}]")
        if missing:
            return {"status": "BLOCKED", "error": "DEPLOY_PROFILE_INCOMPLETE", "missing_fields": missing,
                    "project": profile.project_id}
        audit_key, replay = self._idempotent_begin(f"deploy-preview:{profile.project_id}", idempotency_key)
        if replay is not None:
            return replay
        workflow_id = str(uuid.uuid4())
        lock_key = f"deploy:{deploy.service_id}"
        lock = self.locks.acquire(profile.project_id, lock_key, workflow_id,
                                  ttl_seconds=float(timeout) + 30, reason="deploy preview")
        if not lock.get("acquired"):
            return self._store(audit_key, {"status": "LOCKED", "project": profile.project_id,
                                           "workflow_id": workflow_id, "holder": lock.get("holder")})
        phases: list[dict[str, Any]] = []
        started = time.monotonic()
        try:
            self._audit_phase(workflow_id, profile.project_id, "PRECHECK", "STARTED")
            git = self.git_runner(profile.repo_root)
            if git.get("status") != "OK":
                result = {"status": "BLOCKED", "error": "PRECHECK_FAILED", "git": git}
                self._audit_phase(workflow_id, profile.project_id, "PRECHECK", "BLOCKED", git.get("error"))
                return self._store(audit_key, {**result, "project": profile.project_id,
                                               "workflow_id": workflow_id, "phases": phases})
            phases.append({"phase": "PRECHECK", "status": "OK"})
            if deploy.rollback_capture_method != "git_head":
                result = {"status": "BLOCKED", "error": "ROLLBACK_CAPTURE_NOT_ALLOWLISTED"}
                return self._store(audit_key, {**result, "project": profile.project_id,
                                               "workflow_id": workflow_id, "phases": phases})
            rollback = {"method": "git_head", "sha": git.get("sha")}
            phases.append({"phase": "CAPTURE_ROLLBACK", "status": "OK"})
            self._audit_phase(workflow_id, profile.project_id, "CAPTURE_ROLLBACK", "OK")
            if deploy.build_command_id:
                build = self.command_runner(deploy.build_command_id,
                                            {"project": profile.project_id, "repo_root": profile.repo_root,
                                             "sha": sha, "source_branch": source_branch})
                phases.append({"phase": "BUILD", "status": build.get("status"),
                               "command_id": deploy.build_command_id})
                self._audit_phase(workflow_id, profile.project_id, "BUILD", str(build.get("status")),
                                  build.get("error"))
                if build.get("status") != "OK":
                    return self._store(audit_key, {"status": "BLOCKED", "error": build.get("error", "BUILD_FAILED"),
                                                   "project": profile.project_id, "workflow_id": workflow_id,
                                                   "phases": phases, "rollback": rollback})
            activate = self.command_runner(deploy.service_id,
                                           {"project": profile.project_id, "repo_root": profile.repo_root,
                                            "sha": sha, "source_branch": source_branch, "preview": True})
            phases.append({"phase": "ACTIVATE", "status": activate.get("status"),
                           "service_id": deploy.service_id})
            self._audit_phase(workflow_id, profile.project_id, "ACTIVATE", str(activate.get("status")),
                              activate.get("error"))
            if activate.get("status") != "OK":
                return self._store(audit_key, {"status": "BLOCKED", "error": activate.get("error", "ACTIVATE_FAILED"),
                                               "project": profile.project_id, "workflow_id": workflow_id,
                                               "phases": phases, "rollback": rollback})
            probe_map = {probe.probe_id: probe for probe in profile.health_probes}
            health = [self.probe_runner(probe_map[key]) for key in deploy.health_probe_ids] if verify else []
            phases.append({"phase": "HEALTH", "status": "OK" if all(p.get("status") == "OK" for p in health) else "FAILED"})
            self._audit_phase(workflow_id, profile.project_id, "HEALTH", phases[-1]["status"])
            public = [self.probe_runner(probe_map[key]) for key in deploy.public_probe_ids] if verify else []
            phases.append({"phase": "PUBLIC_PROOF", "status": "OK" if all(p.get("status") == "OK" for p in public) else "FAILED"})
            self._audit_phase(workflow_id, profile.project_id, "PUBLIC_PROOF", phases[-1]["status"])
            failed = [probe for probe in (*health, *public) if probe.get("status") != "OK"]
            final_status = "ROLLBACK_REQUIRED" if failed else "COMPLETE"
            self._audit_phase(workflow_id, profile.project_id, final_status, final_status,
                              "probe failure" if failed else None)
            return self._store(audit_key, {"status": final_status, "project": profile.project_id,
                                           "workflow_id": workflow_id, "requested_sha": sha,
                                           "source_branch": source_branch, "wait": bool(wait),
                                           "phases": phases, "health": health, "public_proof": public,
                                           "rollback": rollback,
                                           "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
        finally:
            self.locks.release(profile.project_id, lock_key, workflow_id)
