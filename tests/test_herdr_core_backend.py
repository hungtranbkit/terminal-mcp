from __future__ import annotations

import json
import subprocess

import pytest

from terminal_mcp.herdr_core_backend import HerdrCoreBackend, HerdrCoreError


def _runner(responses, calls):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        value = responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return subprocess.CompletedProcess(argv, value[0], value[1], value[2])
    return run


def _terminal(**overrides):
    value = {
        "address": "t7", "name": "codex-p0", "labels": {},
        "argv": ["codex"], "cwd": "/home/kimex/workspace/demo",
        "state": "live", "created_ms": 1_800_000_000_000,
        "screen": "exact", "cols": 200, "rows": 120, "pid": 4242,
        "foreground": {"argv": ["codex"], "cwd": "/home/kimex/workspace/demo"},
        "reported": True, "status": {"state": "working", "app": "codex"},
        "status_ms": 1_800_000_001_000,
    }
    value.update(overrides)
    return value


def test_list_maps_stable_native_identity_and_observed_fields():
    calls = []
    payload = {"terminals": [{"address": "t7", "name": "codex-p0", "state": "live"}],
               "hidden_finished": 0, "machines": []}
    backend = HerdrCoreBackend(binary="/opt/hc", runner=_runner([
        (0, json.dumps(payload), ""), (0, json.dumps(_terminal()), ""),
    ], calls))

    sessions = backend.list_sessions()

    assert sessions[0].name == "codex-p0"
    assert sessions[0].session_id == "herdr-core:t7"
    assert sessions[0].pane_id == "t7"
    assert sessions[0].pane_pid == 4242
    assert sessions[0].pane_current_command == "codex"
    assert sessions[0].pane_current_path == "/home/kimex/workspace/demo"
    assert calls[0][0] == ["/opt/hc", "--json", "ls"]
    assert calls[1][0] == ["/opt/hc", "--json", "get", "t7"]
    assert calls[0][1]["shell"] is False


def test_read_and_send_use_structured_cli_and_bounded_timeout():
    calls = []
    responses = [
        (0, json.dumps({"text": "a\nb\n", "complete": True, "how": "history"}), ""),
        (0, json.dumps({"turn": 3, "status": "idle", "started": True}), ""),
    ]
    backend = HerdrCoreBackend(binary="hc", timeout_seconds=2.5, runner=_runner(responses, calls))

    assert backend.capture_lines("t7", 2) == ["a", "b"]
    backend.send_text("t7", "hello", True)

    assert calls[0][0] == ["hc", "--json", "read", "--lines", "2", "t7"]
    assert calls[1][0] == ["hc", "--json", "send", "t7", "hello"]
    assert all(call[1]["timeout"] == 2.5 for call in calls)


def test_timeout_and_structured_error_are_typed_and_redacted():
    timeout = subprocess.TimeoutExpired(["hc", "ls"], 1)
    backend = HerdrCoreBackend(runner=_runner([timeout], []), timeout_seconds=1)
    with pytest.raises(HerdrCoreError) as timed:
        backend.list_sessions()
    assert timed.value.code == "HERDR_TIMEOUT"

    error = {"error": {"kind": "blocked", "message": "token=secret", "hint": "approve"}}
    backend = HerdrCoreBackend(runner=_runner([(1, "", json.dumps(error))], []))
    with pytest.raises(HerdrCoreError) as failed:
        backend.send_text("t7", "x", True)
    assert failed.value.code == "HERDR_BLOCKED"
    assert "secret" not in str(failed.value)


def test_create_uses_argv_not_shell_line_and_labels_backend_identity():
    calls = []
    result = _terminal(address="t9", name="agent-9", argv=["codex", "--yolo"])
    backend = HerdrCoreBackend(binary="hc", runner=_runner([(0, json.dumps(result), "")], calls))

    backend.new_session("agent-9", "/home/kimex/workspace/demo", "codex", extra_args=("--yolo",))

    assert calls[0][0] == [
        "hc", "--json", "run", "--bg", "--name", "agent-9",
        "--cwd", "/home/kimex/workspace/demo", "--label", "terminal-mcp=1",
        "--no-parent", "--outlive-parent", "codex", "--yolo",
    ]


def test_keys_use_native_api_and_unsupported_detach_never_fakes_success():
    calls = []
    backend = HerdrCoreBackend(runner=_runner([(0, json.dumps({"address": "t7"}), "")], calls))
    backend.send_keys("t7", ["enter", "ctrl+c"])
    assert calls[0][0] == ["hc", "--json", "keys", "t7", "enter", "ctrl+c"]
    with pytest.raises(HerdrCoreError, match="UNSUPPORTED"):
        backend.detach_session("t7")
