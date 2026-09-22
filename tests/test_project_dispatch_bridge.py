from __future__ import annotations

import json
from pathlib import Path

import pytest

from terminal_mcp.project_dispatch_bridge import ProjectDispatchBridge, ProjectDispatchRule


class FakeQueue:
    def __init__(self):
        self.calls = []

    def enqueue(self, session, prompt, **kwargs):
        self.calls.append((session, prompt, kwargs))
        return {"status": "TASK_ACCEPTED", "task_id": "q-next", "deduplicated": False}


def write_planner(root: Path, action: str = "CLAIM") -> None:
    planner = root / "tools" / "orchestration" / "continuous_dispatch.py"
    planner.parent.mkdir(parents=True)
    planner.write_text(
        "import json, sys\n"
        f"print(json.dumps({{'action':'{action}','taskId':'NWR-NEXT','reason':'test'}}))\n",
        encoding="utf-8",
    )


def rule(root: Path) -> ProjectDispatchRule:
    return ProjectDispatchRule(
        name="novaretail",
        session_patterns=("linux-codex-work",),
        repo_root=str(root),
    )


def event(event_id="e1", event_type="TASK_COMPLETED", session="linux-codex-work"):
    return {
        "id": event_id,
        "type": event_type,
        "entity_id": "NWR-DONE",
        "payload": {"session": session},
    }


def test_completed_event_enqueues_continuation(tmp_path):
    write_planner(tmp_path)
    queue = FakeQueue()
    bridge = ProjectDispatchBridge(queue, (rule(tmp_path),))

    result = bridge.handle(event())

    assert result["action"] == "CONTINUATION_ENQUEUED"
    assert result["planner_action"] == "CLAIM"
    assert result["planner_task_id"] == "NWR-NEXT"
    assert queue.calls[0][0] == "linux-codex-work"
    assert "CONTINUOUS_DISPATCH_EVENT" in queue.calls[0][1]
    assert queue.calls[0][2]["request_key"] == "project-dispatch:novaretail:e1:CLAIM:NWR-NEXT"


def test_same_event_uses_stable_request_key(tmp_path):
    write_planner(tmp_path)
    queue = FakeQueue()
    bridge = ProjectDispatchBridge(queue, (rule(tmp_path),))
    bridge.handle(event("same"))
    bridge.handle(event("same"))
    assert queue.calls[0][2]["request_key"] == queue.calls[1][2]["request_key"]


def test_non_dispatch_planner_action_is_not_enqueued(tmp_path):
    write_planner(tmp_path, action="NONE")
    queue = FakeQueue()
    bridge = ProjectDispatchBridge(queue, (rule(tmp_path),))
    result = bridge.handle(event())
    assert result["action"] == "PLANNER_NO_DISPATCH"
    assert queue.calls == []


def test_session_and_event_must_match_rule(tmp_path):
    write_planner(tmp_path)
    queue = FakeQueue()
    bridge = ProjectDispatchBridge(queue, (rule(tmp_path),))
    assert bridge.handle(event(session="other-work"))["action"] == "NO_RULE"
    assert bridge.handle(event(event_type="TASK_STARTED"))["action"] == "NO_RULE"
    assert queue.calls == []


def test_planner_must_stay_inside_repo_root(tmp_path):
    queue = FakeQueue()
    bad = ProjectDispatchRule(
        name="bad",
        session_patterns=("linux-codex-work",),
        repo_root=str(tmp_path),
        planner_path="../outside.py",
    )
    bridge = ProjectDispatchBridge(queue, (bad,))
    with pytest.raises(RuntimeError, match="planner failed"):
        bridge.handle(event())


def test_planner_failure_never_enqueues(tmp_path):
    queue = FakeQueue()
    bridge = ProjectDispatchBridge(queue, (rule(tmp_path),))
    with pytest.raises(RuntimeError, match="planner failed"):
        bridge.handle(event())
    assert queue.calls == []
