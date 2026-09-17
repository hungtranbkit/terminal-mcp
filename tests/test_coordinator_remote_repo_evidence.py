"""Pre-dispatch repo evidence must be collected where the SESSION lives.

The bug these cover: the gate was handed only a cwd, so it ran `git` on the
controller. For a session on another node that path does not exist locally,
and the gate reported "could not read git/repo status" -- blaming a healthy
repository for the controller having looked on the wrong machine. Work on
every remote node was therefore undispatchable, and the persisted task sat
still while the worker was busy.

SAFETY: every session/cwd here is a disposable tmp_path fixture.
"""

from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.coordinator import (
    NEEDS_HUMAN, READY, CoordinatorGate, RepoEvidence, RepoEvidenceError,
    RepoEvidenceUnavailable, SessionSnapshot, git_repo_evidence,
    node_aware_repo_evidence,
)
from terminal_mcp.queue_store import QueueStore


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "T"], check=True)
    (root / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def _task(store, session="lane-a", **overrides):
    task = {"prompt": "please implement the widget exporter carefully"}
    task.update(overrides)
    (task_id,) = store.set_tasks(session, [task])
    store.claim_next_task(session, claimed_by="engine-1")
    return store.get_task(task_id)


def _evidence(**overrides):
    base = dict(branch="main", head="abc123", clean=True, status_lines=())
    base.update(overrides)
    return RepoEvidence(**base)


def _contract_payload(**overrides):
    """A payload shaped like a CURRENT agent.

    These fixtures predate repo_valid/collected_at/contract_capabilities. The
    collector now refuses a payload that omits them -- an agent that cannot
    say the repo was valid, or when it looked, has not verified anything --
    so the fixtures move to the real shape rather than the guarantees moving.
    """
    from datetime import datetime, timezone

    from terminal_mcp.contract import describe

    payload = {"repo_valid": True, "exists": True, "readable": True,
               "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               **describe()}
    payload.update(overrides)
    return payload


class _FakeClient:
    """A node agent that can answer repo questions."""

    def __init__(self, payload):
        self.payload = payload
        self.asked = []

    def repo_evidence(self, cwd):
        self.asked.append(cwd)
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _OldClient:
    """A node agent predating /v1/repo-evidence."""


# -- the collector ------------------------------------------------------------

def test_local_session_reads_the_local_repo(repo):
    evidence = git_repo_evidence(str(repo), None)
    assert evidence.branch and evidence.head
    assert evidence.clean is True


def test_a_path_absent_on_this_host_is_unavailable_not_a_repo_failure():
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        git_repo_evidence("/definitely/not/here", "dell-linux")
    message = str(excinfo.value)
    # The distinction that matters: this must not read as a broken repo.
    assert "does not exist on this host" in message
    assert "dell-linux" in message


def test_unavailable_is_a_repo_evidence_error_so_old_handlers_still_catch_it():
    assert issubclass(RepoEvidenceUnavailable, RepoEvidenceError)


def test_remote_session_asks_that_node(repo):
    client = _FakeClient(_contract_payload(
        branch="feature/x", head="deadbeef", clean=True, status_lines=[],
        has_upstream=True, ahead=2, behind=0))
    collect = node_aware_repo_evidence(local_node_id="local",
                                       node_client_factory=lambda node: client)
    evidence = collect("/home/dell/workspace/thing", "dell-linux")
    assert client.asked == ["/home/dell/workspace/thing"]
    assert evidence.branch == "feature/x"
    assert (evidence.ahead, evidence.behind) == (2, 0)
    assert evidence.diverged is False      # ahead only is routine, not a divergence


def test_remote_dirty_repo_is_reported_as_dirty_not_unavailable():
    client = _FakeClient(_contract_payload(
        branch="main", head="abc", clean=False, status_lines=[" M app.py"]))
    collect = node_aware_repo_evidence(local_node_id="local",
                                       node_client_factory=lambda node: client)
    evidence = collect("/remote/path", "dell-linux")
    assert evidence.clean is False
    assert evidence.status_lines == (" M app.py",)


def test_local_node_id_still_uses_the_local_filesystem(repo):
    def factory(node):
        raise AssertionError("the local node must never be asked over the network")

    collect = node_aware_repo_evidence(local_node_id="local", node_client_factory=factory)
    assert collect(str(repo), "local").clean is True
    assert collect(str(repo), None).clean is True


def test_an_agent_without_the_endpoint_is_unavailable():
    collect = node_aware_repo_evidence(local_node_id="local",
                                       node_client_factory=lambda node: _OldClient())
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        collect("/remote/path", "dell-linux")
    assert "predates" in str(excinfo.value)


def test_an_unreachable_node_is_unavailable_not_a_repo_failure():
    collect = node_aware_repo_evidence(
        local_node_id="local",
        node_client_factory=lambda node: (_ for _ in ()).throw(RuntimeError("node offline")))
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        collect("/remote/path", "dell-linux")
    assert "node offline" in str(excinfo.value)


def test_a_node_that_errors_is_unavailable():
    collect = node_aware_repo_evidence(
        local_node_id="local",
        node_client_factory=lambda node: _FakeClient(RuntimeError("HTTP 502")))
    with pytest.raises(RepoEvidenceUnavailable):
        collect("/remote/path", "dell-linux")


def test_a_node_reporting_a_broken_repo_is_a_repo_failure_not_unavailable():
    # The node ANSWERED. It is authoritative about its own filesystem, so this
    # is real evidence the repo is bad -- and must NOT be waivable by
    # allow_unverified_repo, which exists only for "we could not look".
    collect = node_aware_repo_evidence(
        local_node_id="local",
        node_client_factory=lambda node: _FakeClient({"error": "REPO_EVIDENCE_FAILED",
                                                      "detail": "not a git repository"}))
    with pytest.raises(RepoEvidenceError) as excinfo:
        collect("/remote/path", "dell-linux")
    assert not isinstance(excinfo.value, RepoEvidenceUnavailable)
    assert "not a git repository" in str(excinfo.value)      # the node's own detail survives


def test_a_broken_remote_repo_is_not_waivable(store):
    task = _task(store, metadata={"allow_unverified_repo": True})
    gate = CoordinatorGate(evidence_collector=node_aware_repo_evidence(
        local_node_id="local",
        node_client_factory=lambda node: _FakeClient({"error": "REPO_EVIDENCE_FAILED",
                                                      "detail": "not a git repository"})))
    decision = gate.review(task, store=store, session=_remote_session())
    assert decision.status == NEEDS_HUMAN


def test_no_adapter_configured_is_unavailable():
    collect = node_aware_repo_evidence(local_node_id="local", node_client_factory=None)
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        collect("/remote/path", "dell-linux")
    assert "no node adapter" in str(excinfo.value)


# -- the gate -----------------------------------------------------------------

def _remote_session(cwd="/home/dell/workspace/thing"):
    return SessionSnapshot(node_id="dell-linux", cwd=cwd, current_command="claude")


def test_gate_dispatches_a_remote_session_when_its_node_answers(store):
    task = _task(store)
    client = _FakeClient(_contract_payload(branch="main", head="abc", clean=True,
                                           status_lines=[]))
    gate = CoordinatorGate(evidence_collector=node_aware_repo_evidence(
        local_node_id="local", node_client_factory=lambda node: client))
    decision = gate.review(task, store=store, session=_remote_session())
    # The whole point: a remote worker is now dispatchable.
    assert decision.status == READY


def test_gate_fails_closed_when_evidence_is_unavailable(store):
    task = _task(store)
    gate = CoordinatorGate(evidence_collector=node_aware_repo_evidence(
        local_node_id="local", node_client_factory=lambda node: _OldClient()))
    decision = gate.review(task, store=store, session=_remote_session())
    assert decision.status == NEEDS_HUMAN
    # ...but with the REAL reason, not a false claim about the repo.
    assert "could not be collected" in decision.reason
    assert "repo_evidence_unavailable" in decision.evidence
    assert decision.evidence["session_node_id"] == "dell-linux"
    assert "could not read git/repo status" not in decision.reason


def test_unverified_repo_can_be_authorised_explicitly(store):
    task = _task(store, metadata={"allow_unverified_repo": True})
    gate = CoordinatorGate(evidence_collector=node_aware_repo_evidence(
        local_node_id="local", node_client_factory=lambda node: _OldClient()))
    decision = gate.review(task, store=store, session=_remote_session())
    # A deliberate, per-task decision -- never a silent default.
    assert decision.status == READY


def test_a_genuine_repo_failure_is_still_reported_as_one(store):
    task = _task(store)

    def collector(cwd, node_id=None):
        raise RepoEvidenceError("git exited 128: not a git repository")

    gate = CoordinatorGate(evidence_collector=collector)
    decision = gate.review(task, store=store, session=_remote_session())
    assert decision.status == NEEDS_HUMAN
    assert "could not read git/repo status" in decision.reason


def test_a_dirty_remote_repo_still_blocks(store):
    task = _task(store)
    client = _FakeClient(_contract_payload(branch="main", head="abc", clean=False,
                                           status_lines=[" M app.py"]))
    gate = CoordinatorGate(evidence_collector=node_aware_repo_evidence(
        local_node_id="local", node_client_factory=lambda node: client))
    decision = gate.review(task, store=store, session=_remote_session())
    assert decision.status != READY
    assert decision.blockers == (" M app.py",)


def test_a_non_git_local_cwd_still_fails_closed(store, tmp_path):
    task = _task(store)
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    gate = CoordinatorGate(evidence_collector=node_aware_repo_evidence(local_node_id="local"))
    decision = gate.review(task, store=store,
                           session=SessionSnapshot(node_id="local", cwd=str(plain),
                                                   current_command="claude"))
    assert decision.status == NEEDS_HUMAN


def test_legacy_single_argument_collectors_still_work(store):
    task = _task(store)
    gate = CoordinatorGate(evidence_collector=lambda cwd: _evidence())
    assert gate.review(task, store=store, session=_remote_session()).status == READY


def test_a_pre_contract_payload_is_not_treated_as_verified():
    """The shape these fixtures used to carry must now be refused.

    Kept as its own test so the fixture modernisation above cannot quietly
    become a weakening: an agent that answers without repo_valid has not
    verified anything, and must read as UNAVAILABLE rather than as a pass.
    """
    legacy = {"branch": "main", "head": "abc", "clean": True, "status_lines": []}
    collect = node_aware_repo_evidence(
        local_node_id="local", node_client_factory=lambda node: _FakeClient(legacy))
    with pytest.raises(RepoEvidenceUnavailable):
        collect("/remote/path", "dell-linux")
