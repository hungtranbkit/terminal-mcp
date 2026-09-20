"""The real AgentRunner adapter: sessions, guarded transport, durable dispatch.

WHAT THESE DEFEND

The adapter's job is to hand a prompt to a real agent and get a trustworthy
answer back. Almost everything that can go wrong there is a way of being
CONFIDENTLY WRONG rather than obviously broken, so most of these tests assert
that a plausible-looking non-answer is refused:

* our own dispatched prompt read back as the agent's reply
* a marker from an earlier attempt, still on screen
* "done" with no artifact written
* a second prompt sent into an agent that is still working
* a "fresh, independent" evaluator that is actually the builder's pane

SAFETY: every session here is a fake object in-process. Nothing in this file
opens a tmux session, reaches a node, or touches the production queue
database. The one test that would use a real session is skipped unless it is
explicitly enabled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from terminal_mcp import harness_policy as policy
from terminal_mcp import harness_state as state
from terminal_mcp.harness_engine import AgentResult, CheckResult, HarnessEngine
from terminal_mcp.harness_runner import (DEFAULT_STALL_SECONDS, NoSessionAvailable,
                                         SessionBroker, TerminalAgentRunner,
                                         build_agent_text,
                                         dispatch_idempotency_key)
from terminal_mcp.harness_store import HarnessStore
from terminal_mcp.queue_engine import COMPLETION_INSTRUCTION_SENTENCE

CONFIRMED = {"delivery_state": "SUBMIT_CONFIRMED", "press_enter": True, "enter_sent": True}


# ---------------------------------------------------------------------------
# a fake fleet
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self, name, *, runtime="claude", state="IDLE", output="",
                 alive=True, input_allowed=True, node_id="local"):
        self.name = name
        self.runtime = runtime
        self.state = state
        self.output = output
        self.alive = alive
        self.input_allowed = input_allowed
        self.node_id = node_id


class FakeOps:
    """The narrow SessionOps slice, in process. Records everything."""

    def __init__(self, sessions=()):
        self.sessions = {s.name: s for s in sessions}
        self.sends: list[dict] = []
        self.created: list[dict] = []
        self.send_response = dict(CONFIRMED)
        self.idempotent: dict[str, dict] = {}

    def terminal_status(self, session):
        found = self.sessions.get(session)
        if found is None or not found.alive:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        return {"state": found.state, "last_output": found.output,
                "node_id": found.node_id, "input_required": found.state == "WAITING_INPUT"}

    def terminal_tail(self, session, lines=None):
        found = self.sessions.get(session)
        if found is None or not found.alive:
            return {"error": "SESSION_NOT_FOUND"}
        return {"output": found.output, "node_id": found.node_id}

    def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
        key = kwargs.get("idempotency_key")
        found = self.sessions.get(session)
        if found is None or not found.alive:
            return {"error": "SESSION_NOT_FOUND"}
        if key and key in self.idempotent:
            # core.py's own idempotent_sends behaviour: the ORIGINAL result,
            # and no second prompt in the pane.
            return dict(self.idempotent[key])
        self.sends.append({"session": session, "text": text, "key": key})
        found.output += "\n" + text
        # A session that takes a prompt starts working. Modelling it as still
        # IDLE would let the broker hand it a second task, which the real one
        # refuses -- and a fake that is easier to satisfy than the real thing
        # tests nothing.
        found.state = "RUNNING"
        response = dict(self.send_response)
        response["session"] = session
        if key:
            self.idempotent[key] = dict(response)
        return response

    def terminal_list_sessions(self):
        return {"sessions": [
            {"name": s.name, "agent_type": s.runtime, "state": s.state,
             "node_id": s.node_id, "input_allowed": s.input_allowed,
             "node_online": True}
            for s in self.sessions.values() if s.alive]}

    def terminal_create_session(self, name, agent_type="shell", cwd=None, **kwargs):
        if name in self.sessions:
            return {"error": "SESSION_ALREADY_EXISTS"}
        self.sessions[name] = FakeSession(name, runtime=agent_type)
        self.created.append({"name": name, "agent_type": agent_type, "cwd": cwd})
        return {"session": name, "node_id": "local", "agent_type": agent_type}


@pytest.fixture
def store(tmp_path):
    return HarnessStore(tmp_path / "queue.db")


@pytest.fixture
def artifacts(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    return root


def _run(store, *, task_id="MOB-1", session=None, worktree=None):
    run, _ = store.create_run(task_id=task_id, prompt="do the thing", title="Thing",
                              project_id="pilot")
    fields = {}
    if session:
        fields["builder_session_id"] = session
    if worktree:
        fields["worktree_path"] = worktree
        fields["branch"] = "harness/x"
    if fields:
        store.patch_run(run.id, **fields)
    return store.require_run(run.id)


class FakePrompt:
    def __init__(self, text="build it", delta=False):
        self.text = text
        self.delta = delta
        self.bytes = len(text.encode())
        self.tokens_estimate = max(1, len(text) // 4)
        self.pack_key = None
        self.cache_hit = False


def _finish(ops, store, session_name, dispatch, payload):
    """What a real agent does: write the file, then print the marker."""
    Path(dispatch["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dispatch["artifact_path"]).write_text(json.dumps(payload))
    ops.sessions[session_name].output += (
        f"\nI have finished.\n"
        f"###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={dispatch['id']} attempt={dispatch['attempt']} "
        f"nonce={dispatch['nonce']} status=completion_candidate "
        f"summary_sha256=abc###\n")
    ops.sessions[session_name].state = "IDLE"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def test_a_dispatch_sends_once_and_returns_without_waiting(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)

    assert result.pending is True
    assert result.newly_dispatched is True
    assert result.dispatch_id
    assert len(ops.sends) == 1
    dispatch = store.open_dispatch_for(run.id)
    assert dispatch["state"] == "accepted"
    assert dispatch["session_id"] == "claude-a"


def test_stepping_the_same_stage_again_does_not_send_a_second_prompt(store, artifacts):
    """The duplicate-dispatch failure. Whoever drives ticks may step a stage
    any number of times, and only the first of those may send."""
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)

    first = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                       tier="balanced", session_id=None, iteration=1)
    for _ in range(5):
        again = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                           tier="balanced", session_id=None, iteration=1)
        assert again.pending is True
        assert again.newly_dispatched is False
        assert again.dispatch_id == first.dispatch_id

    assert len(ops.sends) == 1, "one prompt, however many times the stage was stepped"


def test_the_idempotency_key_is_stable_across_restarts():
    assert (dispatch_idempotency_key("run1", 2, "builder", 1)
            == dispatch_idempotency_key("run1", 2, "builder", 1))
    assert (dispatch_idempotency_key("run1", 2, "builder", 1)
            != dispatch_idempotency_key("run1", 2, "builder", 2))
    assert dispatch_idempotency_key("run1", 2, "builder", 1).startswith("harness:")


def test_a_refused_delivery_is_infrastructure_not_a_wrong_answer(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    ops.send_response = {"delivery_state": "TEXT_SENT", "press_enter": True}
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=_run(store),
                        tier="balanced", session_id=None, iteration=1)

    assert result.infra_failure is True
    assert result.pending is False
    assert "refused" in result.error


def test_an_unknown_delivery_is_held_open_never_resent(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    ops.send_response = {"delivery_state": "DELIVERY_UNKNOWN", "press_enter": True}
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.pending is True
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
               tier="balanced", session_id=None, iteration=1)
    assert len(ops.sends) == 1, "never resend an unknown delivery"


# ---------------------------------------------------------------------------
# completion: the marker says finished, the file says what
# ---------------------------------------------------------------------------

def test_the_answer_is_read_from_the_file_not_the_pane(store, artifacts):
    """The pane has no scrollback. A long answer printed there is gone."""
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)

    _finish(ops, store, "claude-a", dispatch, {"reported": "done", "commit": "abc123"})
    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)

    assert result.pending is False
    assert result.payload == {"reported": "done", "commit": "abc123"}
    assert result.commit == "abc123"
    assert store.latest_dispatch(run.id, iteration=1, role=policy.BUILDER)["state"] == "completed"


def test_our_own_dispatched_prompt_is_not_read_back_as_the_answer(store, artifacts):
    """Our template contains a fully valid marker. Reading it back is how a
    session that did nothing gets recorded as finished."""
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)
    ops.sessions["claude-a"].state = "IDLE"   # it went quiet without answering
    # The pane contains ONLY our prompt -- the agent has written nothing.
    Path(dispatch["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dispatch["artifact_path"]).write_text('{"reported": "done"}')

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)

    assert result.pending is True, "our own template must never complete a dispatch"
    assert store.open_dispatch_for(run.id) is not None


def test_a_marker_from_an_earlier_attempt_does_not_complete_this_one(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)
    Path(dispatch["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dispatch["artifact_path"]).write_text('{"reported": "done"}')
    ops.sessions["claude-a"].state = "IDLE"
    ops.sessions["claude-a"].output += (
        "\nfinished\n###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={dispatch['id']} attempt={dispatch['attempt']} "
        "nonce=a-stale-nonce-from-before status=completion_candidate "
        "summary_sha256=abc###\n")

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.pending is True, "the nonce binds the marker to THIS dispatch"


def test_done_with_no_artifact_is_infrastructure_not_a_failed_iteration(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)
    ops.sessions["claude-a"].state = "IDLE"
    ops.sessions["claude-a"].output += (
        "\ndone\n###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={dispatch['id']} attempt={dispatch['attempt']} nonce={dispatch['nonce']} "
        "status=completion_candidate summary_sha256=abc###\n")

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.infra_failure is True
    assert "wrote no artifact" in result.error


def test_a_fenced_json_artifact_is_still_read(store, artifacts):
    """Recovering from three backticks beats failing a whole iteration."""
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)
    Path(dispatch["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dispatch["artifact_path"]).write_text('```json\n{"reported": "done"}\n```')
    ops.sessions["claude-a"].state = "IDLE"
    ops.sessions["claude-a"].output += (
        "\ndone\n###TERMINAL_MCP_COMPLETION protocol=terminal-mcp-completion/v1 "
        f"task_id={dispatch['id']} attempt={dispatch['attempt']} nonce={dispatch['nonce']} "
        "status=completion_candidate summary_sha256=abc###\n")

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.payload == {"reported": "done"}


# ---------------------------------------------------------------------------
# session choice
# ---------------------------------------------------------------------------

def test_a_healthy_builder_session_is_reused(store, artifacts):
    ops = FakeOps([FakeSession("claude-a", output="context left: 40%")])
    broker = SessionBroker(ops, store)
    run = _run(store, session="claude-a")

    pick = broker.pick(run=run, role=policy.BUILDER)
    assert pick.session == "claude-a"
    assert pick.reused is True
    assert ops.created == []


def test_a_session_over_the_context_ceiling_is_not_reused(store, artifacts):
    """At 90% the session is about to be replaced; one more task means doing
    the work twice."""
    ops = FakeOps([FakeSession("claude-a", output="Context low (9% remaining)")])
    broker = SessionBroker(ops, store)
    run = _run(store, session="claude-a")

    pick = broker.pick(run=run, role=policy.BUILDER)
    assert pick.session != "claude-a"
    assert pick.spawned is True


def test_a_busy_session_is_not_handed_more_work(store, artifacts):
    ops = FakeOps([FakeSession("claude-a", state="RUNNING")])
    broker = SessionBroker(ops, store)
    run = _run(store, session="claude-a")

    pick = broker.pick(run=run, role=policy.BUILDER)
    assert pick.session != "claude-a"


def test_a_session_waiting_on_a_human_is_not_handed_more_work(store, artifacts):
    ops = FakeOps([FakeSession("claude-a", state="WAITING_INPUT")])
    broker = SessionBroker(ops, store)
    run = _run(store, session="claude-a")
    assert broker.pick(run=run, role=policy.BUILDER).session != "claude-a"


def test_a_critical_evaluator_never_inherits_any_session_this_run_used(store, artifacts):
    """The whole value being paid for is that it did not watch the Builder
    reason. Handing it the Builder's pane destroys the thing being bought."""
    ops = FakeOps([FakeSession("claude-a", output="context left: 40%")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store, session="claude-a")
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id="claude-a", iteration=1)
    run = store.require_run(run.id)

    pick = runner.broker.pick(run=run, role=policy.EVALUATOR, independent=True)
    assert pick.session != "claude-a"
    assert pick.spawned is True
    assert pick.reused is False


def test_two_runs_never_get_the_same_session_at_once(store, artifacts):
    """The duplicate-spawn guard's other half: a pane holding one run's open
    dispatch is not a candidate for another run."""
    ops = FakeOps([FakeSession("claude-a", output="context left: 40%")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    first = _run(store, task_id="A-1")
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=first, tier="balanced",
               session_id=None, iteration=1)

    second = _run(store, task_id="B-1")
    pick = runner.broker.pick(run=second, role=policy.BUILDER)
    assert pick.session != "claude-a"
    assert pick.spawned is True


def test_an_unknown_runtime_is_refused_rather_than_dispatched_into(store):
    ops = FakeOps([FakeSession("x")])
    broker = SessionBroker(ops, store)
    with pytest.raises(NoSessionAvailable):
        broker.pick(run=_run(store), role=policy.BUILDER, runtime="gemini")


def test_nothing_available_and_no_spawn_is_infrastructure(store, artifacts):
    ops = FakeOps([])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts, allow_spawn=False)
    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=_run(store),
                        tier="balanced", session_id=None, iteration=1)
    assert result.infra_failure is True
    assert result.pending is False


def test_a_spawned_session_opens_in_the_runs_worktree(store, artifacts, tmp_path):
    worktree = str(tmp_path / "wt")
    ops = FakeOps([])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store, worktree=worktree)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    assert ops.created[0]["cwd"] == worktree


# ---------------------------------------------------------------------------
# death, stalling and recovery
# ---------------------------------------------------------------------------

def test_a_dead_session_is_infrastructure_and_the_work_is_not_lost(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store, worktree="/tmp/wt/x")
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)

    ops.sessions["claude-a"].alive = False  # the session is killed
    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)

    assert result.infra_failure is True
    assert "session gone" in result.error
    assert store.latest_dispatch(run.id, iteration=1, role=policy.BUILDER)["state"] == "failed"


def test_a_quiet_session_stalls_only_after_the_configured_patience(store, artifacts):
    """A Builder thinking and a Builder stalled look identical from outside,
    and the cost of being wrong in the impatient direction is killing work."""
    ops = FakeOps([FakeSession("claude-a")])
    patient = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    patient.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
                session_id=None, iteration=1)
    ops.sessions["claude-a"].state = "IDLE"   # it went quiet without answering
    assert patient.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                       tier="balanced", session_id=None, iteration=1).pending is True

    impatient = TerminalAgentRunner(ops, store, artifacts_root=artifacts, stall_seconds=0.0)
    result = impatient.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                           tier="balanced", session_id=None, iteration=1)
    assert result.infra_failure is True
    assert "stalled" in result.error


def test_a_session_that_starts_asking_a_human_is_not_prompted_again(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    ops.sessions["claude-a"].state = "WAITING_INPUT"

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.infra_failure is True
    assert "waiting on a human" in result.error
    assert len(ops.sends) == 1


# ---------------------------------------------------------------------------
# through the engine
# ---------------------------------------------------------------------------

def _engine(store, ops, artifacts, repo, **kwargs):
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    return HarnessEngine(store, runner=runner, repo_root=str(repo),
                         check_runner=lambda cmds, cwd=None, **kw: [
                             CheckResult(command=c, exit_code=0, output="ok") for c in cmds],
                         **kwargs), runner


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.ts").write_text("export const a = 1;\n")
    return root


def test_the_engine_stops_at_a_pending_agent_instead_of_looping(store, artifacts, repo):
    ops = FakeOps([FakeSession("claude-a")])
    engine, _runner = _engine(store, ops, artifacts, repo)
    run, _ = engine.start(task_id="MOB-1", prompt="build it", title="Build",
                          project_id="pilot", acceptance=["it builds"], checks=["true"],
                          changed_paths=["src"])

    outcomes = engine.drive(run.id, max_steps=20)

    assert outcomes[-1].pending is True
    assert outcomes[-1].action == "awaiting_builder"
    assert store.require_run(run.id).stage == state.BUILDING
    assert len(ops.sends) == 1, "drive must not re-prompt while an agent works"


def test_one_llm_call_is_charged_however_many_times_it_is_observed(store, artifacts, repo):
    ops = FakeOps([FakeSession("claude-a")])
    engine, _runner = _engine(store, ops, artifacts, repo)
    run, _ = engine.start(task_id="MOB-2", prompt="build it", title="Build",
                          project_id="pilot", acceptance=["it builds"], checks=["true"],
                          changed_paths=["src"])
    engine.drive(run.id)
    for _ in range(6):
        engine.step(run.id)

    assert store.efficiency(run.id)["llm_calls"] == 1


def test_a_run_reaches_merge_ready_once_the_agent_answers(store, artifacts, repo):
    ops = FakeOps([FakeSession("claude-a")])
    engine, _runner = _engine(store, ops, artifacts, repo)
    run, _ = engine.start(task_id="MOB-3", prompt="build it", title="Build",
                          project_id="pilot", acceptance=["it builds"], checks=["true"],
                          changed_paths=["src"])
    engine.drive(run.id)
    dispatch = store.open_dispatch_for(run.id)
    _finish(ops, store, "claude-a", dispatch, {"reported": "done", "commit": "c1"})

    engine.drive(run.id, max_steps=10)
    assert store.require_run(run.id).stage == state.MERGE_READY


def test_a_killed_builder_resumes_the_same_run_iteration_and_worktree(store, artifacts, repo):
    """The kill/recovery proof, through the real adapter."""
    ops = FakeOps([FakeSession("claude-a")])
    engine, _runner = _engine(store, ops, artifacts, repo)
    run, _ = engine.start(task_id="MOB-4", prompt="build it", title="Build",
                          project_id="pilot", acceptance=["it builds"], checks=["true"],
                          changed_paths=["src"])
    engine.drive(run.id)
    store.patch_run(run.id, worktree_path="/tmp/wt/mob-4", branch="harness/mob-4")
    before = store.require_run(run.id)
    engine.checkpoint(run.id, note="before the kill", remaining=["finish the ETA row"])

    ops.sessions["claude-a"].alive = False       # kill it
    outcome = engine.step(run.id)
    assert outcome.to_stage == state.FAILED_INFRA

    engine.resume(run.id)
    after = store.require_run(run.id)
    assert after.id == before.id
    assert after.current_iteration == before.current_iteration
    assert after.worktree_path == "/tmp/wt/mob-4"
    assert after.branch == "harness/mob-4"
    assert after.contract_hash == before.contract_hash


def test_the_stage_and_the_dispatch_survive_a_controller_restart(store, artifacts, repo, tmp_path):
    """Nothing the adapter needs lives in memory. A new store, a new engine
    and a new runner over the same file carry on observing the same agent."""
    ops = FakeOps([FakeSession("claude-a")])
    engine, _runner = _engine(store, ops, artifacts, repo)
    run, _ = engine.start(task_id="MOB-5", prompt="build it", title="Build",
                          project_id="pilot", acceptance=["it builds"], checks=["true"],
                          changed_paths=["src"])
    engine.drive(run.id)
    dispatch_before = store.open_dispatch_for(run.id)

    # The controller restarts: every object is rebuilt from the database.
    reborn_store = HarnessStore(tmp_path / "queue.db")
    reborn_engine, _r2 = _engine(reborn_store, ops, artifacts, repo)

    assert reborn_store.require_run(run.id).stage == state.BUILDING
    assert reborn_store.open_dispatch_for(run.id)["id"] == dispatch_before["id"]

    _finish(ops, reborn_store, "claude-a", dispatch_before, {"reported": "done"})
    reborn_engine.drive(run.id, max_steps=10)

    assert reborn_store.require_run(run.id).stage == state.MERGE_READY
    assert len(ops.sends) == 1, "a restart must not re-prompt a working agent"


# ---------------------------------------------------------------------------
# the prompt wrapper
# ---------------------------------------------------------------------------

def test_the_prompt_is_included_verbatim_and_first():
    text = build_agent_text(prompt_text="ORIGINAL REQUEST", role="builder",
                            dispatch_id="dsp_1", attempt=1, nonce="n", 
                            artifact_path="/tmp/a.json", run_id="r", iteration=2)
    assert text.startswith("ORIGINAL REQUEST")
    assert "/tmp/a.json" in text
    assert COMPLETION_INSTRUCTION_SENTENCE in text
    assert "nonce=n" in text
    assert "no scrollback" in text


# ---------------------------------------------------------------------------
# the real-session smoke test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("HARNESS_REAL_SESSION"),
                    reason="set HARNESS_REAL_SESSION=1 to drive a real tmux session")
def test_real_session_smoke(tmp_path):
    """One real session, one real send, one real artifact read back.

    Kept out of the default run because it opens a tmux session on this
    machine. It is the only test in this file that touches anything real, and
    it is the one that proves the fakes above are shaped correctly.
    """
    from terminal_mcp.controller import ControllerService

    controller = ControllerService()
    store = HarnessStore(tmp_path / "queue.db")
    runner = TerminalAgentRunner(controller, store, artifacts_root=tmp_path / "artifacts",
                                 default_runtime="claude")
    run = _run(store, task_id="SMOKE-1")
    result = runner.run(role=policy.BUILDER, prompt=FakePrompt("say hello"), run=run,
                        tier="economy", session_id=None, iteration=1)
    assert result.pending or result.infra_failure
    dispatch = store.latest_dispatch(run.id, iteration=1, role=policy.BUILDER)
    assert dispatch is not None
    assert dispatch["idempotency_key"].startswith("harness:")


def test_an_artifact_carrying_this_dispatchs_nonce_completes_it_alone(store, artifacts):
    """Claude Code repaints with no scrollback, so a printed marker may
    already be erased. The nonce in the FILE is the stronger signal: it is
    unguessable and unique to this attempt, so a file carrying it cannot be a
    leftover, an echo, or anything but a deliberate answer to this request."""
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)

    Path(dispatch["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dispatch["artifact_path"]).write_text(json.dumps(
        {"reported": "done", "harness_nonce": dispatch["nonce"]}))
    # The pane still shows only our own prompt -- no marker was ever readable.
    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)

    assert result.pending is False
    assert result.payload["reported"] == "done"


def test_an_artifact_with_the_wrong_nonce_does_not_complete_a_dispatch(store, artifacts):
    ops = FakeOps([FakeSession("claude-a")])
    runner = TerminalAgentRunner(ops, store, artifacts_root=artifacts)
    run = _run(store)
    runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run, tier="balanced",
               session_id=None, iteration=1)
    dispatch = store.open_dispatch_for(run.id)
    Path(dispatch["artifact_path"]).parent.mkdir(parents=True, exist_ok=True)
    Path(dispatch["artifact_path"]).write_text(json.dumps(
        {"reported": "done", "harness_nonce": "a-nonce-from-some-other-attempt"}))

    result = runner.run(role=policy.BUILDER, prompt=FakePrompt(), run=run,
                        tier="balanced", session_id=None, iteration=1)
    assert result.pending is True


def test_the_prompt_asks_for_the_nonce_in_the_file():
    text = build_agent_text(prompt_text="x", role="builder", dispatch_id="d",
                            attempt=1, nonce="NONCE123", artifact_path="/tmp/a.json",
                            run_id="r", iteration=1)
    assert '"harness_nonce": "NONCE123"' in text


def test_the_adapter_spawns_through_either_session_api(store, artifacts):
    """A multi-node controller places a session on a node; a single-host
    service has no `node` parameter at all. Both are valid SessionOps."""
    class LocalOnlyOps(FakeOps):
        def terminal_create_session(self, name, agent_type="shell", cwd=None):
            return super().terminal_create_session(name, agent_type=agent_type, cwd=cwd)

    ops = LocalOnlyOps([])
    broker = SessionBroker(ops, store)
    pick = broker.spawn(run=_run(store), role=policy.BUILDER, runtime="claude")
    assert pick.spawned is True
    assert ops.created[0]["agent_type"] == "claude"

    multi = FakeOps([])
    assert "node" in SessionBroker(multi, store)._spawn_kwargs()
