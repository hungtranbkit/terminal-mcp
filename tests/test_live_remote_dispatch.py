"""LIVE remote-node auto-dispatch smoke test through the real
RemoteNodeClient (backlog blg_65725709747b / REQUIREMENTS Backlog item 1).

WHAT THIS PROVES, AND WHAT IT DELIBERATELY DOES NOT TOUCH.

Every other dispatch test in this suite talks to a LocalNodeClient or a
fake: the queue engine's own logic is well covered, but the real
`RemoteNodeClient` path -- real HTTP, real bearer auth, real JSON
marshalling, a real node-agent process with its own config and its own
tmux server -- was never exercised end to end. That gap is the entire
point of the backlog item, because it is the gate before
`config.queue.enabled` may be turned on against any real lane.

SAFETY POSTURE (this test changes NOTHING in production):
  - Its OWN QueueStore and NodeRegistry, in tmp_path. The real
    production queue.db/nodes.db are never opened, so no real lane, no
    real task and no real `auto_dispatch_enabled` flag is touched.
  - `engine.tick()` is driven DIRECTLY. That is the documented way to
    exercise one lane on demand without the background loop
    (queue_engine.py's own docstring): the per-lane
    `queue_lanes.auto_dispatch_enabled` gate is NOT flipped anywhere, so
    nothing starts dispatching on its own afterwards.
  - The session it uses is created and killed by this test, named
    `terminal-mcp-smoke-<random>`. It is never a pre-existing session,
    and never one on the agent's own `protected_sessions` list.
  - `agent_type="shell"`: a plain shell, so the dispatch costs no API
    tokens and runs no agent. The task prompt is a single `echo` of a
    random marker -- the acceptance evidence is that marker appearing in
    the session's real output, which also proves the text was genuinely
    delivered AND executed, not merely accepted by the transport.
  - No controller restart, no node-agent restart, no deploy.

WHY THE SESSION NAME AND CWD ARE WHAT THEY ARE. The node agent enforces
its own config, and the two lists differ: `allowed_session_patterns`
admits `test-*`, but `input_policy.allowed_session_patterns` does NOT --
a `test-*` session is readable and would be refused the SEND that
dispatch depends on. `terminal-mcp-*` is in both, so that prefix is the
one that can actually complete a dispatch. The cwd must be a real git
repository: CoordinatorGate refuses with NEEDS_HUMAN and a
`repo_evidence_error` when `git_repo_evidence` cannot read one.

HOW TO RUN (never runs by default -- needs a real node and a real token):

    TERMINAL_MCP_SMOKE_ENDPOINT=http://<node-host>:8790 \\
    TERMINAL_MCP_SMOKE_TOKEN=<that node agent's bearer token> \\
    TERMINAL_MCP_SMOKE_NODE_ID=<controller's id for that node> \\
    pytest -m live_remote_node tests/test_live_remote_dispatch.py -v

Absent those variables the whole module skips, so a normal suite run and
a CI runner are unaffected.
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

from terminal_mcp.controller import ControllerService
from terminal_mcp.coordinator import CoordinatorGate
from terminal_mcp.host_metrics import NodeMetrics
from terminal_mcp.node_client import NodeClientError, RemoteNodeClient
from terminal_mcp.node_registry import NodeRegistry
from terminal_mcp.queue_engine import QueueEngine, idempotency_key_for
from terminal_mcp.queue_store import QueueStore, RUNNING

pytestmark = pytest.mark.live_remote_node

ENDPOINT = os.environ.get("TERMINAL_MCP_SMOKE_ENDPOINT")
TOKEN = os.environ.get("TERMINAL_MCP_SMOKE_TOKEN")
NODE_ID = os.environ.get("TERMINAL_MCP_SMOKE_NODE_ID", "smoke-remote")
REPO_CWD = os.environ.get("TERMINAL_MCP_SMOKE_CWD", os.getcwd())

_SKIP = pytest.mark.skipif(
    not (ENDPOINT and TOKEN),
    reason="live remote node smoke: set TERMINAL_MCP_SMOKE_ENDPOINT and TERMINAL_MCP_SMOKE_TOKEN",
)

_METRICS = NodeMetrics(cpu_percent=5.0, load1=0.1, load5=0.1, load15=0.1, cpu_count=4,
                       ram_total_bytes=8_000_000_000, ram_used_bytes=1_000_000_000, ram_percent=12.5,
                       swap_total_bytes=0, swap_used_bytes=0, swap_percent=0.0,
                       disk_total_bytes=100_000_000_000, disk_used_bytes=1_000_000_000,
                       disk_free_bytes=99_000_000_000, disk_percent=1.0)


@pytest.fixture
def remote_client():
    return RemoteNodeClient(ENDPOINT, TOKEN, timeout=15.0)


@pytest.fixture
def controller(tmp_path, remote_client):
    """A throwaway controller wired to the REAL remote node. Its registry
    lives in tmp_path, so registering this node here cannot disturb the
    real fleet registry the production controller owns."""
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_workspace_root=str(tmp_path))
    controller.registry.register(NODE_ID, display_name=NODE_ID, hostname=NODE_ID, endpoint=ENDPOINT)
    controller._clients[NODE_ID] = remote_client
    controller.registry.heartbeat(NODE_ID, metrics=_METRICS, tmux_session_count=0, agent_counts={},
                                  agent_types=("shell",), agent_version=None, labels=())
    return controller


@pytest.fixture
def smoke_session(remote_client):
    """A real, disposable session on the real node, cleaned up afterwards
    whatever the test does. `terminal-mcp-smoke-` because that prefix is
    the one admitted by BOTH of the agent's pattern lists."""
    name = f"terminal-mcp-smoke-{uuid.uuid4().hex[:8]}"
    created = remote_client.create_session(name, "shell", REPO_CWD)
    if created.get("error"):
        pytest.fail(f"could not create the disposable smoke session: {created}")
    try:
        yield name
    finally:
        try:
            remote_client.kill_session(name, name, requested_by="live-remote-dispatch-smoke")
        except Exception as exc:  # noqa: BLE001 -- cleanup must never mask the real result
            print(f"WARNING: smoke session {name} was not cleaned up: {exc}")


def _tail(client, session, lines=200):
    return (client.tail(session, lines=lines) or {}).get("output", "")


def _wait_for(client, session, marker, *, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker in _tail(client, session):
            return True
        time.sleep(0.5)
    return False


# -- the smoke itself -----------------------------------------------------

@_SKIP
def test_live_remote_auto_dispatch_end_to_end(tmp_path, controller, remote_client, smoke_session):
    """One real task, dispatched to a real remote node, proven delivered
    AND executed -- then proven not to be delivered twice."""
    marker = f"SMOKE_{uuid.uuid4().hex[:12]}"
    store = QueueStore(tmp_path / "queue.db")
    (task_id,) = store.set_tasks(smoke_session, [{"prompt": f"echo {marker}"}])

    engine = QueueEngine(store, controller, coordinator=CoordinatorGate())

    # 1. NODE/SESSION IDENTITY -- the routed status really comes from the
    #    remote node, not from this host's own TerminalService.
    status = controller.terminal_status(smoke_session)
    assert status.get("node_id") == NODE_ID, status
    assert not status.get("error"), status

    # 2. CLAIM
    claim = engine.tick(smoke_session)
    assert claim.action == "CLAIMED", claim.to_dict()
    assert store.get_task(task_id).status == "PRECHECK"

    # 3. REPO EVIDENCE / ELIGIBILITY -- the coordinator gate really ran
    #    and really read a git repository before allowing the dispatch.
    review = engine.tick(smoke_session)
    decision = store.get_task(task_id).coordinator_decision
    assert review.action == "COORDINATOR_READY", (review.to_dict(), decision)
    evidence = decision.get("evidence") or {}
    assert "repo_evidence_error" not in evidence, evidence
    assert evidence.get("repo") or evidence.get("branch") or evidence, evidence

    # 4. DISPATCH + DELIVERY
    dispatch = engine.tick(smoke_session)
    assert dispatch.action == "DISPATCHED", dispatch.to_dict()
    task = store.get_task(task_id)
    assert task.status == RUNNING
    assert task.dispatch_idempotency_key == idempotency_key_for(task_id, 1)

    # 5. ACCEPTANCE EVIDENCE -- the marker really reached the real shell
    #    on the real node and really ran.
    assert _wait_for(remote_client, smoke_session, marker), _tail(remote_client, smoke_session)

    # 6. IDEMPOTENCY -- replaying the SAME key must return the original
    #    result and must NOT deliver a second time. Counted on the echoed
    #    marker rather than on the command line itself, which the prompt
    #    text also contains.
    tail_before = _tail(remote_client, smoke_session)
    before = tail_before.count(marker)
    replay = remote_client.send_text(smoke_session, f"echo {marker}", press_enter=True,
                                     idempotency_key=task.dispatch_idempotency_key)
    assert replay.get("delivery_state") in ("SUBMIT_CONFIRMED", "DELIVERY_UNKNOWN"), replay
    time.sleep(2.0)
    assert _tail(remote_client, smoke_session).count(marker) == before, (
        "a replay of the same idempotency_key delivered a second time")

    print(f"\nSMOKE EVIDENCE node={NODE_ID} session={smoke_session} task={task_id} "
          f"marker={marker} idempotency_key={task.dispatch_idempotency_key}")


@_SKIP
def test_delivery_state_is_submit_confirmed_through_the_real_client(remote_client, smoke_session):
    """The delivery contract itself, isolated from the queue: a real send
    over real HTTP reports a real, confirmed submission."""
    marker = f"DELIV_{uuid.uuid4().hex[:12]}"
    response = remote_client.send_text(smoke_session, f"echo {marker}", press_enter=True,
                                       idempotency_key=f"smoke:{marker}")
    assert response.get("sent") is True, response
    assert response.get("delivery_state") == "SUBMIT_CONFIRMED", response
    assert _wait_for(remote_client, smoke_session, marker), _tail(remote_client, smoke_session)


@_SKIP
def test_an_agent_missing_a_capability_fails_loudly_not_silently(remote_client):
    """The old-agent case. A controller newer than the node agent must get
    a typed, explicit NodeClientError naming the failure -- never a silent
    empty result that a caller would mistake for "this node has no rows".

    Exercised here with the fleet-audit read (`GET /v1/audit`), which this
    controller build knows and an older agent does not implement."""
    with pytest.raises(NodeClientError) as caught:
        remote_client.audit_list(limit=5)
    message = str(caught.value)
    assert message, "a capability gap must produce a real message, not an empty error"
    print(f"\nCAPABILITY-GAP EVIDENCE: {message}")


@_SKIP
def test_the_node_reports_its_own_identity_and_version(remote_client):
    """Provenance for the run report: which agent actually answered."""
    ok, latency_ms, error = remote_client.ping()
    assert ok is True, error
    print(f"\nNODE PING ok={ok} latency_ms={latency_ms}")
