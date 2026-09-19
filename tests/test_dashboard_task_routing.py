"""Global Tasks must never show an unexplained QUEUED (TMCP-TASK-ROUTER-001).

A queued card has to carry WHY it is queued and WHICH sessions were rejected;
a routed card has to name the session and node it actually runs on. Those are
the two facts the board could not previously report, and they are what turns
"it's just sitting there" into something an operator can act on.

State is redirected to tmp_path so the suite never migrates or reads the real
deployment's queue database.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import GLOBAL_TASKS_HTML, register_dashboard
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import BOUND, WAITING_RUNTIME, QueueStore


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App + the ONE QueueService both surfaces share.

    Passed explicitly to build_mcp AND register_dashboard, exactly as
    server_http.main does: left to default, the dashboard builds itself an
    ephemeral private store and the board would be reading a different
    database than the test writes to."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = AppConfig(
        PermissionsConfig(True, True), ("test-*",), 50, 20,
        InputPolicyConfig(allowed_session_patterns=("test-*",)),
        session_access=SessionAccessConfig(default_read=True, default_input=True),
    )
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    service = TerminalService(config)
    server = build_mcp(service, queue=queue)
    register_dashboard(server, service, queue=queue)
    test_client = TestClient(server.streamable_http_app(),
                             headers={"Origin": "http://testserver"})
    return test_client, queue.store


def test_a_queued_card_carries_its_routing_reason_and_top_rejections(client):
    test_client, store = client
    (task_id,) = store.set_tasks("test-lane", [{"prompt": "stuck work"}])
    store.record_routing_deferral(task_id, evidence={
        "reason": "no eligible session among 3 candidates (2x SESSION_BUSY, 1x WAITING_INPUT)",
        "rejected": [
            {"session": "agent-a", "rejected": "SESSION_BUSY",
             "rejected_detail": "session is actively running something else", "score": 20},
            {"session": "agent-b", "rejected": "WAITING_INPUT",
             "rejected_detail": "session is blocked waiting for human input", "score": 10},
        ],
    })

    body = test_client.get("/dashboard/api/tasks/board").json()
    card = next(row for column in ("backlog", "queued", "running", "blocked_review", "done")
                for row in body[column] if row["id"] == task_id)

    assert card["routing_state"] == WAITING_RUNTIME
    assert card["router_reason"].startswith("no eligible session among 3 candidates")
    assert [row["session"] for row in card["router_rejections"]] == ["agent-a", "agent-b"]
    assert card["router_rejections"][0]["reason"] == "SESSION_BUSY"
    assert card["router_rejections"][0]["detail"]
    # The full evidence blob is deliberately NOT shipped to a board that
    # refreshes on a timer.
    assert "routing_evidence" not in card


def test_a_routed_card_names_the_session_and_node_it_runs_on(client):
    test_client, store = client
    (task_id,) = store.set_tasks("test-lane", [{"prompt": "routed work"}])
    store.bind_task_to_session(task_id, "test-runner", node_id="hp-linux",
                               evidence={"reason": "score 60: +20 session is IDLE",
                                         "score": 60})

    body = test_client.get("/dashboard/api/tasks/board").json()
    card = next(row for column in ("backlog", "queued", "running", "blocked_review", "done")
                for row in body[column] if row["id"] == task_id)

    assert card["execution_session"] == "test-runner"
    assert card["execution_node_id"] == "hp-linux"
    assert card["routing_state"] == BOUND
    assert card["router_reason"] == "score 60: +20 session is IDLE"


def test_a_task_the_router_never_touched_reads_unrouted_and_gains_no_noise(client):
    test_client, store = client
    (task_id,) = store.set_tasks("test-lane", [{"prompt": "legacy work"}])

    body = test_client.get("/dashboard/api/tasks/board").json()
    card = next(row for column in ("backlog", "queued", "running", "blocked_review", "done")
                for row in body[column] if row["id"] == task_id)

    assert card["routing_state"] == "UNROUTED"
    assert card["execution_session"] is None
    assert "router_reason" not in card
    assert "router_rejections" not in card


def test_the_board_template_renders_the_routing_fields():
    """The data is useless if the page never draws it."""
    assert "execution_session" in GLOBAL_TASKS_HTML
    assert "router_reason" in GLOBAL_TASKS_HTML
    assert "router_rejections" in GLOBAL_TASKS_HTML
    assert "WAITING_RUNTIME" in GLOBAL_TASKS_HTML


def test_global_tasks_router_rejection_join_is_valid_js_escape():
    assert r".join('\n')" in GLOBAL_TASKS_HTML
