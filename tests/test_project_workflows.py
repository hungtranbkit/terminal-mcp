import json
from dataclasses import replace
from pathlib import Path

import yaml

from terminal_mcp.project_workflows import (
    DeployProfile,
    HealthProbe,
    ProjectProfile,
    ProjectProfileRegistry,
    ProjectWorkflowTools,
)


class FakeTerminal:
    def __init__(self):
        self.bindings = {"primary": "worker-1"}

    def terminal_get_binding(self, name):
        if name not in self.bindings:
            return {"error": "BINDING_NOT_FOUND"}
        return {"binding": name, "session": self.bindings[name]}


class FakeCompact:
    def __init__(self):
        self.terminal = FakeTerminal()
        self.statuses = {}
        self.tails = {}
        self.sent = []
        self.send_result = {
            "status": "SUBMIT_CONFIRMED", "submission_id": "sub-1",
            "evidence": {"enter_count": 1}, "reason": "",
        }

    def _resolve(self, target):
        if target.startswith("binding:"):
            return "binding", target.split(":", 1)[1]
        if target.startswith("session:"):
            return "session", target.split(":", 1)[1]
        return "session", target

    def _status(self, target):
        return {"target": target, "target_type": self._resolve(target)[0],
                **self.statuses.get(target, {"error": "SESSION_NOT_FOUND", "reason": "missing"})}

    def _tail(self, target, lines):
        return {"output": self.tails.get(target, ""), "truncated": False}

    def send_task(self, target, task, wait, timeout, idempotency_key=None):
        self.sent.append((target, task, wait, timeout, idempotency_key))
        return dict(self.send_result)


class FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, session, prompt, **kwargs):
        self.calls.append((session, prompt, kwargs))
        return {"status": "TASK_ACCEPTED", "task_id": "q-1", "queue_position": 2,
                "deduplicated": False}


class FakeSupervisor:
    def __init__(self):
        self.events = {}

    def list_events(self, target=None, limit=10):
        return {"events": self.events.get(target, [])}


class FakeAudit:
    def __init__(self):
        self.results = {}
        self.claimed = set()
        self.events = []

    def claim_idempotency_key(self, key, stale_after_seconds=30):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def get_idempotent_result(self, key):
        return self.results.get(key)

    def store_idempotent_result(self, key, result):
        self.results[key] = dict(result)

    def record(self, **kwargs):
        self.events.append(kwargs)


class FakeLocks:
    def __init__(self):
        self.conflict = False
        self.releases = []

    def acquire(self, project, resource, owner, **kwargs):
        if self.conflict:
            return {"acquired": False, "holder": {"owner_id": "other"}}
        return {"acquired": True, "lock": {"owner_id": owner}}

    def release(self, project, resource, owner):
        self.releases.append((project, resource, owner))
        return True


class FakeController:
    def node_health_summary(self):
        return {"counts": {"EXECUTION_OK": 1, "EXECUTION_DOWN": 1},
                "healthy": 1, "total": 2,
                "blockers": [{"node_id": "dead-node", "state": "EXECUTION_DOWN",
                              "reason": "execution probe timed out"}], "truncated": False}


def profile(*, targets=("session:worker-1", "session:worker-2"), deploy=True, probes=None):
    return ProjectProfile(
        project_id="fixture", aliases=("fx",), repo_root="/repo",
        preferred_targets=tuple(targets), target_capabilities={}, bindings=(), sessions=(),
        git_remote=None,
        health_probes=tuple(probes or (HealthProbe("local", "fixture", "fixture.pass"),)),
        deploy=(DeployProfile("fixture.build", "fixture.noop", ("local",), (),
                              "git_head", True) if deploy else None),
    )


def service(*, project_profile=None, probe_runner=None, command_runner=None):
    compact, queue, supervisor, locks, audit = (
        FakeCompact(), FakeQueue(), FakeSupervisor(), FakeLocks(), FakeAudit())
    svc = ProjectWorkflowTools(
        ProjectProfileRegistry([project_profile or profile()]), compact, queue,
        supervisor, locks, audit,
        probe_runner=probe_runner,
        command_runner=command_runner,
        git_runner=lambda root: {"status": "OK", "branch": "main", "sha": "a" * 40, "dirty": False},
    )
    return svc, compact, queue, supervisor, locks, audit


def test_registry_absent_is_empty_and_config_profile_round_trips(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("permissions: {}\n")
    assert ProjectProfileRegistry.from_config(empty).get("anything") is None
    configured = tmp_path / "configured.yaml"
    configured.write_text(yaml.safe_dump({"project_profiles": [{
        "project_id": "fixture", "aliases": ["fx"], "repo_root": str(tmp_path),
        "preferred_targets": [{"target": "session:test-1", "capabilities": ["python"]}],
        "sessions": ["test-2"], "health_probes": [
            {"id": "safe", "kind": "fixture", "check_id": "fixture.pass"}],
        "deploy": {"service_id": "fixture.noop", "rollback_capture_method": "git_head",
                   "health_probe_ids": ["safe"], "preview_safe": True},
    }]}))
    loaded = ProjectProfileRegistry.from_config(configured).get("fx")
    assert loaded.project_id == "fixture"
    assert loaded.targets == ("session:test-1", "session:test-2")


def test_project_check_mixed_states_unreachable_and_useful_tail_only():
    svc, compact, _queue, supervisor, _locks, _audit = service()
    compact.statuses["session:worker-1"] = {"state": "RUNNING", "reason": "working"}
    compact.statuses["session:worker-2"] = {"state": "WAITING_INPUT", "input_required": True}
    compact.tails["session:worker-1"] = "must be omitted"
    compact.tails["session:worker-2"] = "answer the prompt"
    supervisor.events["session:worker-2"] = [
        {"state": "BLOCKED", "event_type": "INPUT_REQUIRED", "reason": "needs operator"}]
    result = svc.project_check("fx")
    assert result["status"] == "BLOCKED"
    assert result["summary"]["state_counts"] == {"RUNNING": 1, "WAITING_INPUT": 1}
    assert "tail" not in result["targets"][0]
    assert result["targets"][1]["tail"] == "answer the prompt"
    missing = svc.project_check("fixture", targets=["session:worker-2"])
    assert missing["targets"][0]["state"] == "WAITING_INPUT"


def test_project_check_unreachable_is_row_and_output_is_hard_bounded():
    svc, compact, _queue, _supervisor, _locks, _audit = service()
    compact.statuses["session:worker-1"] = {"state": "FAILED", "reason": "x" * 5000}
    compact.tails["session:worker-1"] = "z" * 50_000
    result = svc.project_check("fixture", max_output_chars=1000)
    assert result["truncated"] is True
    assert len(json.dumps(result, separators=(",", ":"), ensure_ascii=False)) <= 1000
    unreachable = svc.project_check("fixture", targets=["session:worker-2"], max_output_chars=1000)
    assert unreachable["targets"][0]["state"] == "UNREACHABLE"


def test_project_check_adds_compact_node_health_summary():
    svc, compact, _queue, _supervisor, _locks, _audit = service()
    svc.controller = FakeController()
    compact.statuses["session:worker-1"] = {"state": "IDLE", "reason": "ready"}
    compact.statuses["session:worker-2"] = {"state": "IDLE", "reason": "ready"}
    result = svc.project_check("fixture")
    assert result["node_health"]["counts"] == {"EXECUTION_OK": 1, "EXECUTION_DOWN": 1}
    assert result["status"] == "BLOCKED"
    assert any(row.get("node_id") == "dead-node" for row in result["blockers"])


def test_dispatch_explicit_target_sends_once_and_retry_is_idempotent():
    svc, compact, _queue, _supervisor, _locks, _audit = service()
    compact.statuses["session:worker-1"] = {"state": "IDLE"}
    first = svc.project_dispatch("fixture", "do work", "worker-1", idempotency_key="same")
    second = svc.project_dispatch("fixture", "do work", "worker-1", idempotency_key="same")
    assert first == second
    assert first["status"] == "SUBMIT_CONFIRMED"
    assert first["sent"] is True
    assert len(compact.sent) == 1
    assert compact.sent[0][4].endswith(":send")


def test_dispatch_auto_select_skips_busy_target():
    svc, compact, _queue, _supervisor, _locks, _audit = service()
    compact.statuses["session:worker-1"] = {"state": "RUNNING"}
    compact.statuses["session:worker-2"] = {"state": "IDLE"}
    result = svc.project_dispatch("fixture", "task", idempotency_key="auto")
    assert result["selected_target"] == "session:worker-2"
    assert compact.sent[0][0] == "session:worker-2"


def test_dispatch_busy_queues_durably_or_reports_busy():
    svc, compact, queue, _supervisor, _locks, _audit = service()
    compact.statuses["session:worker-1"] = {"state": "RUNNING"}
    compact.statuses["session:worker-2"] = {"state": "BUSY"}
    queued = svc.project_dispatch("fixture", "queued task", idempotency_key="queued")
    assert queued["status"] == "TASK_ACCEPTED"
    assert queued["queued"] is True and queued["queue_id"] == "q-1"
    assert queue.calls[0][0] == "worker-1"
    busy = svc.project_dispatch("fixture", "no queue", queue_if_busy=False,
                                idempotency_key="busy")
    assert busy["status"] == "BUSY" and busy["sent"] is False


def test_dispatch_permission_denial_is_preserved():
    svc, compact, _queue, _supervisor, _locks, _audit = service()
    compact.statuses["session:worker-1"] = {"state": "IDLE"}
    compact.send_result = {"status": "BLOCKED", "reason": "ACCESS_DENIED",
                           "evidence": {"enter_count": 0}}
    result = svc.project_dispatch("fixture", "denied", "session:worker-1",
                                  idempotency_key="denied")
    assert result["status"] == "BLOCKED"
    assert result["sent"] is False
    assert result["reason"] == "ACCESS_DENIED"


def test_deploy_missing_profile_is_blocked_with_fields():
    svc, *_ = service(project_profile=profile(deploy=False))
    result = svc.deploy_preview("fixture", idempotency_key="missing")
    assert result["status"] == "BLOCKED"
    assert result["error"] == "DEPLOY_PROFILE_INCOMPLETE"
    assert "deploy" in result["missing_fields"]


def test_deploy_lock_conflict_executes_nothing():
    calls = []
    svc, _compact, _queue, _supervisor, locks, _audit = service(
        command_runner=lambda command, context: calls.append(command) or {"status": "OK"})
    locks.conflict = True
    result = svc.deploy_preview("fixture", idempotency_key="locked")
    assert result["status"] == "LOCKED"
    assert calls == []


def test_deploy_fixture_happy_path_is_locked_audited_and_idempotent():
    calls = []
    svc, _compact, _queue, _supervisor, locks, audit = service(
        command_runner=lambda command, context: calls.append(command) or {"status": "OK"})
    first = svc.deploy_preview("fixture", sha="a" * 40, source_branch="main",
                               idempotency_key="deploy-once")
    second = svc.deploy_preview("fixture", sha="a" * 40, source_branch="main",
                                idempotency_key="deploy-once")
    assert first == second
    assert first["status"] == "COMPLETE"
    assert calls == ["fixture.build", "fixture.noop"]
    assert len(locks.releases) == 1
    assert {event["action"] for event in audit.events} >= {
        "deploy_preview:PRECHECK", "deploy_preview:CAPTURE_ROLLBACK",
        "deploy_preview:BUILD", "deploy_preview:ACTIVATE", "deploy_preview:COMPLETE",
    }


def test_deploy_health_failure_requires_rollback_without_running_rollback():
    bad = profile(probes=(HealthProbe("local", "fixture", "fixture.fail"),))
    svc, _compact, _queue, _supervisor, locks, _audit = service(project_profile=bad)
    result = svc.deploy_preview("fixture", idempotency_key="health-fail")
    assert result["status"] == "ROLLBACK_REQUIRED"
    assert result["rollback"]["sha"] == "a" * 40
    assert result["health"][0]["status"] == "FAILED"
    assert len(locks.releases) == 1


def test_deploy_profile_value_cannot_become_arbitrary_shell():
    base = profile()
    unsafe = replace(base, deploy=replace(base.deploy, service_id="sh -c arbitrary"))
    svc, *_ = service(project_profile=unsafe)
    result = svc.deploy_preview("fixture", idempotency_key="unsafe-command")
    assert result["status"] == "BLOCKED"
    assert result["error"] == "COMMAND_ID_NOT_ALLOWLISTED"
