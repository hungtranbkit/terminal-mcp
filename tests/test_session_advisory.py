from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from terminal_mcp.session_advisory import create_session_with_advisories
from terminal_mcp.task_labels import summarize_prompt


def test_concurrent_creates_serialize_task_check_and_label_commit(tmp_path):
    class Controller:
        def __init__(self):
            self.rows = []
            self.lock = threading.Lock()

        def terminal_list_sessions(self, *, budget_seconds=None):
            time.sleep(0.05)  # let a competing create reach the same check window
            with self.lock:
                return {"sessions": [dict(row) for row in self.rows]}

        def terminal_create_session(self, name, agent_type, cwd, *, initial_prompt=None, **kwargs):
            with self.lock:
                self.rows.append({"name": name, "cwd": cwd})
            return {"session": name, "node_id": "local", "state": "READY"}

    controller = Controller()
    labels = {}

    def create(name):
        def commit_label(result):
            labels[("local", name)] = {"summary": summarize_prompt("implement shared task")}

        return create_session_with_advisories(
            controller, name=name, agent_type="shell", cwd=str(tmp_path),
            initial_prompt="implement shared task", label_index=lambda: dict(labels),
            on_created=commit_label)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ("session-a", "session-b")))

    warnings = [item for result in results for item in result.get("advisories", [])
                if item["code"] == "SAME_TASK_SESSION_EXISTS"]
    assert len(warnings) == 1
    assert warnings[0]["sessions"] in (["session-a"], ["session-b"])


def test_partial_inventory_requires_explicit_confirmation_before_create(tmp_path):
    class Controller:
        def __init__(self):
            self.created = []

        def terminal_list_sessions(self, *, budget_seconds=None):
            return {"sessions": [], "unreachable_nodes": [{"node_id": "offline"}]}

        def terminal_create_session(self, name, agent_type, cwd, **kwargs):
            self.created.append(name)
            return {"session": name}

    controller = Controller()
    blocked = create_session_with_advisories(controller, name="new", agent_type="shell", cwd=str(tmp_path))
    assert blocked["error"] == "SESSION_CREATE_ADVISORY"
    assert blocked["advisories"][0]["code"] == "SESSION_AUDIT_PARTIAL"
    assert blocked["created"] is False
    assert controller.created == []

    created = create_session_with_advisories(controller, name="new", agent_type="shell",
                                             cwd=str(tmp_path), confirm_advisories=True)
    assert created["session"] == "new"
    assert controller.created == ["new"]
