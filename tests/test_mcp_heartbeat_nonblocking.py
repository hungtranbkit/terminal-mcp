"""Exercise the real nested MCP heartbeat function without a live controller.

The heartbeat is advisory: a transient local store/agent exception must never
prevent list_nodes, list_sessions or any other MCP tool from executing.
"""
from __future__ import annotations

import ast
import logging
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "terminal_mcp" / "mcp_app.py"


def isolated_heartbeat(*, controller=None, available_agent_types=None):
    # Compile the production nested function, not a handwritten reimplementation.
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_refresh_local_heartbeat"
    ]
    assert len(matches) == 1, "production heartbeat implementation moved"
    code = compile(
        ast.fix_missing_locations(ast.Module(body=matches, type_ignores=[])),
        str(SOURCE), "exec",
    )
    lock = threading.Lock()
    next_refresh = [0.0]
    tmux = SimpleNamespace(list_sessions=Mock(return_value=[]))
    terminal = SimpleNamespace(
        tmux=tmux,
        config=SimpleNamespace(
            session_lifecycle=SimpleNamespace(launch_commands={})
        ),
    )
    controller = controller or SimpleNamespace(refresh_local_heartbeat=Mock())
    env = {
        "time": time,
        "terminal": terminal,
        "controller": controller,
        "available_agent_types": available_agent_types or (lambda _: []),
        "_local_heartbeat_lock": lock,
        "_local_heartbeat_next_refresh": next_refresh,
        "_LOCAL_HEARTBEAT_MIN_INTERVAL_SECONDS": 5.0,
        "_LOGGER": logging.getLogger("terminal_mcp.mcp_app"),
    }
    exec(code, env)
    return env["_refresh_local_heartbeat"], controller, lock, next_refresh


class BestEffortMcpHeartbeatTests(unittest.TestCase):
    def test_success_keeps_refresh_and_unlocks(self):
        refresh, controller, lock, next_refresh = isolated_heartbeat()
        refresh()
        controller.refresh_local_heartbeat.assert_called_once()
        self.assertGreater(next_refresh[0], time.monotonic())
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_controller_store_failure_does_not_block_tool(self):
        controller = SimpleNamespace(
            refresh_local_heartbeat=Mock(side_effect=RuntimeError("private database path"))
        )
        refresh, _, lock, next_refresh = isolated_heartbeat(controller=controller)
        with self.assertLogs("terminal_mcp.mcp_app", level="WARNING") as recorded:
            refresh()  # must not propagate a controller/SQLite exception
        self.assertEqual(controller.refresh_local_heartbeat.call_count, 1)
        self.assertGreater(next_refresh[0], time.monotonic())
        self.assertIn("LOCAL_HEARTBEAT_REFRESH_FAILED", recorded.output[0])
        self.assertNotIn("private database path", recorded.output[0])
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()
        refresh()  # failed attempts must also respect the cooldown
        self.assertEqual(controller.refresh_local_heartbeat.call_count, 1)

    def test_agent_inventory_failure_is_also_best_effort(self):
        def broken_inventory(_):
            raise ValueError("sensitive process arguments")
        refresh, controller, lock, next_refresh = isolated_heartbeat(
            available_agent_types=broken_inventory
        )
        with self.assertLogs("terminal_mcp.mcp_app", level="WARNING") as recorded:
            refresh()
        controller.refresh_local_heartbeat.assert_not_called()
        self.assertNotIn("sensitive process arguments", recorded.output[0])
        self.assertGreater(next_refresh[0], time.monotonic())
        self.assertTrue(lock.acquire(blocking=False))
        lock.release()

    def test_busy_heartbeat_lock_does_not_block_read(self):
        refresh, controller, lock, _ = isolated_heartbeat()
        self.assertTrue(lock.acquire(blocking=False))
        try:
            refresh()
        finally:
            lock.release()
        controller.refresh_local_heartbeat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
