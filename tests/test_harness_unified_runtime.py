"""Canonical service + optional runtime integration. No real sessions or models."""
import json
from pathlib import Path

import pytest

from terminal_mcp import harness_policy as policy
from terminal_mcp.harness_engine import AgentResult
from terminal_mcp.harness_runner import SessionBroker, TerminalAgentRunner, NoSessionAvailable
from terminal_mcp.harness_service import HarnessService
from terminal_mcp.harness_store import HarnessStore
from tests.test_harness_runner import FakeOps, FakeSession, FakePrompt, _run


@pytest.fixture
def store(tmp_path):
    return HarnessStore(tmp_path / "queue.db")


def test_canonical_service_stops_on_pending_agent(store, tmp_path):
    class PendingRunner:
        calls = 0
        def run(self, **kwargs):
            self.calls += 1
            return AgentResult(pending=True, dispatch_id="in-flight")
    runner = PendingRunner()
    service = HarnessService(store=store, runner=runner, repo_root=str(tmp_path))
    result = service.start(task_id="pending", prompt="Build feature", acceptance=["works"],
                           checks=["true"], steps=20)
    assert result["drive_error"] is None
    assert result["steps"][-1]["pending"]
    assert runner.calls == 1


@pytest.mark.parametrize("inherited", [False, True])
def test_broker_does_not_reuse_remote_session(store, inherited):
    ops = FakeOps([FakeSession("harness-remote", node_id="remote")])
    ops.local_node_id = "controller"
    run = _run(store, session="harness-remote" if inherited else None)
    with pytest.raises(NoSessionAvailable):
        SessionBroker(ops, store).pick(run=run, role=policy.BUILDER, allow_spawn=False)


def test_broker_pins_spawn_to_controller_node(store):
    class FleetOps(FakeOps):
        local_node_id = "controller"
        requested_nodes = []
        def terminal_create_session(self, name, agent_type="shell", cwd=None, **kwargs):
            self.requested_nodes.append(kwargs.get("node"))
            result = super().terminal_create_session(name, agent_type, cwd, **kwargs)
            result["node_id"] = self.local_node_id
            self.sessions[name].node_id = self.local_node_id
            return result
    ops = FleetOps()
    SessionBroker(ops, store).pick(run=_run(store), role=policy.BUILDER)
    assert ops.requested_nodes == ["controller"]


def test_runner_refuses_remote_persisted_artifact_without_abandoning_dispatch(store, tmp_path):
    ops = FakeOps([FakeSession("harness-remote", node_id="remote")])
    run = _run(store)
    artifact = tmp_path / "answer.json"
    artifact.write_text(json.dumps({"harness_nonce": "nonce", "answer": "wrong node"}))
    dispatch, _ = store.open_dispatch(run_id=run.id, task_id=run.task_id,
        iteration=1, role=policy.BUILDER, attempt=1, idempotency_key="old-remote",
        nonce="nonce", session_id="harness-remote", node_id="remote",
        artifact_path=str(artifact))
    runner = TerminalAgentRunner(ops, store, artifacts_root=tmp_path)
    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.infra_failure
    assert "local" in result.error.lower()
    assert store.open_dispatch_for(run.id) is not None
    assert not ops.sends


def test_repeated_idle_polls_reach_stall_timeout(store, tmp_path, monkeypatch):
    ops = FakeOps([FakeSession("harness-local")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=tmp_path, stall_seconds=60)
    run = _run(store)
    args = dict(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
                session_id=None, iteration=1)
    runner.run(**args)
    ops.sessions["harness-local"].state = "IDLE"
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    for now in (0.0, 30.0, 59.0):
        clock[0] = now
        assert runner.run(**args).pending
    clock[0] = 61.0
    result = runner.run(**args)
    assert result.infra_failure
    assert "stalled" in result.error


@pytest.mark.parametrize("progress", ["output", "running", "unknown"])
def test_progress_or_uncertainty_resets_idle_timeout(store, tmp_path, monkeypatch, progress):
    ops = FakeOps([FakeSession("harness-local")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=tmp_path, stall_seconds=60)
    run = _run(store)
    args = dict(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
                session_id=None, iteration=1)
    runner.run(**args)
    session = ops.sessions["harness-local"]
    session.state = "IDLE"
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    assert runner.run(**args).pending
    clock[0] = 59.0
    if progress == "output":
        session.output += "new progress"
    else:
        session.state = "RUNNING" if progress == "running" else "UNKNOWN"
    assert runner.run(**args).pending
    clock[0] = 61.0
    session.state = "IDLE"
    assert runner.run(**args).pending


def test_direct_terminal_service_uses_configured_registry_node(store):
    ops = FakeOps([FakeSession("harness-local", node_id="configured-local")])
    ops.REGISTRY_LOCAL_NODE_ID = "configured-local"
    pick = SessionBroker(ops, store).pick(run=_run(store), role=policy.BUILDER, allow_spawn=False)
    assert pick.session == "harness-local"
