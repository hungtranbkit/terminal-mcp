"""Signed webhook -> local CI task -> autopilot -> independent QA (#106 staging).

Synthetic executors only (no tmux, node or repo). Proves the ingress feeds
the EXISTING queue/autopilot/verify path: the node being offline mutates
nothing, two coordinators dispatch once, a redelivery after a restart adds
nothing, and a QA failure is repaired within the bounded budget.
"""
from __future__ import annotations

import json
import os
import threading

from starlette.testclient import TestClient

from terminal_mcp import autopilot as ap
from terminal_mcp import github_ingress as gi
from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                 SessionAccessConfig)
from terminal_mcp.core import TerminalService
from terminal_mcp.mcp_app import build_mcp

from .test_autopilot_qa_dispatch import LANE, PROJECT, QA, SHA_A, World
from .test_github_ingress import SECRET, headers, push

REPO = "hungtranbkit/synthetic-app"


def _ingress(w, tmp_path):
    secret = tmp_path / "hook.secret"
    secret.write_bytes(SECRET)
    os.chmod(secret, 0o600)
    policy = gi.GithubIngressPolicy(enabled=True, secret_file=str(secret), repos=(
        gi.GithubRepoRoute(repo=REPO, project_id=PROJECT, lane=LANE),))
    return gi.GithubIngress(policy, store=w.store, bus=w.bus)


_BODIES: dict = {}


def _deliver(ingress, sha=SHA_A, delivery="aaaaaaaa-0000-0000-0000-000000000001"):
    # A GitHub redelivery carries the IDENTICAL body: build it once per delivery.
    body = _BODIES.setdefault((sha, delivery), json.dumps(push(sha=sha, repo=REPO)).encode())
    return ingress.handle(headers(body, delivery=delivery), body)


def test_webhook_to_verified_ci_with_offline_node_race_restart_and_bounded_repair(tmp_path):
    w = World(tmp_path, max_repairs=1)
    ingress = _ingress(w, tmp_path)
    status, body = _deliver(ingress)
    assert status == 202
    task_id = body["task_id"]

    # Node offline: the key is persisted before the send, nothing is sent, and
    # the task is never reported RUNNING.
    w.dispatcher.offline = True
    for _ in range(2):
        w.autopilot().tick(PROJECT)
    key = w.store.get_task(task_id).dispatch_idempotency_key
    assert key and w.status(task_id) == "DISPATCHING" and w.dispatcher.sends == []
    w.dispatcher.offline = False

    # Two coordinators race: one dispatch.
    w.locks.force_release(PROJECT, ap.COORDINATOR_RESOURCE, actor="test", reason="race")
    errors = []

    def run(name):
        try:
            for _ in range(4):
                w.autopilot(name).tick(PROJECT)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(f"hp-{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and w.dispatcher.sends_to(LANE) == 1
    assert [k for t, _, k in w.dispatcher.sends if t == LANE] == [key]   # same key, once

    # A restarted controller receives GitHub's redelivery: nothing new.
    restarted = _ingress(w, tmp_path)
    assert _deliver(restarted)[1]["duplicate"] is True
    assert len(w.store.list_tasks_for_project(PROJECT, limit=50)) == 1

    # Worker reports DONE -> independent QA FAIL -> one bounded repair -> PASS.
    w.locks.force_release(PROJECT, ap.COORDINATOR_RESOURCE, actor="test", reason="handover")
    w.worker_done(task_id)
    w.qa.act(passed=False)
    report = w.autopilot().tick(PROJECT)
    assert report["repairs"] == [task_id]
    w.worker_done(task_id)
    job, result = w.qa.act(passed=True)
    assert result["ok"] and w.status(task_id) == "COMPLETED"
    # The provably-unsent offline dispatch was requeued (gates re-run), which
    # counts as an attempt; QA always verifies the task's CURRENT attempt.
    assert job.attempt == w.store.get_task(task_id).attempt_count
    assert w.store.get_task(task_id).metadata["github_ingress"]["sha"] == SHA_A
    assert w.dispatcher.qa_sends(QA)[-1][2].endswith(f"{job.id}:1")


def test_http_route_is_post_only_and_signed(tmp_path):
    w = World(tmp_path)
    ingress = _ingress(w, tmp_path)
    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)),
                       session_access=SessionAccessConfig(default_read=True, default_input=True))
    server = build_mcp(TerminalService(config))
    gi.register_github_ingress(server, ingress)
    client = TestClient(server.streamable_http_app())
    body = json.dumps(push(repo=REPO)).encode()
    assert client.get("/github/webhook").status_code == 405
    bad = client.post("/github/webhook", content=body,
                      headers=headers(body, signature="sha256=" + "0" * 64))
    assert bad.status_code == 401 and w.store.list_tasks_for_project(PROJECT, limit=5) == []
    ok = client.post("/github/webhook", content=body, headers=headers(body))
    assert ok.status_code == 202 and ok.json()["accepted"] is True
    big = client.post("/github/webhook", content=b"x" * 2_000_000, headers=headers(b"x"))
    assert big.status_code == 413
