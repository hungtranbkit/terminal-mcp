from __future__ import annotations

import time
import subprocess

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.bindings import BindingStore


def test_real_tmux_list_tail_status_and_denial(read_config, tmux_session_factory):
    tmux_session_factory("test-running", "bash -lc 'for i in 1 2 3 4 5; do echo BUILD STEP $i; sleep 0.1; done; sleep 10'")
    tmux_session_factory("test-waiting", "bash -lc 'echo \"Do you want to continue? [y/N]\"; read answer; echo ANSWER=$answer; sleep 10'")
    tmux_session_factory("private-session", "bash -lc 'echo SHOULD_NOT_LEAK; sleep 10'")
    time.sleep(0.8)
    service = TerminalService(read_config)

    names = {item["name"] for item in service.terminal_list_sessions()["sessions"]}
    assert {"test-running", "test-waiting"} <= names
    assert "private-session" not in names
    assert "BUILD STEP 5" in service.terminal_tail("test-running", 20)["output"]
    assert service.terminal_tail("private-session", 20)["error"] == "ACCESS_DENIED"
    assert service.terminal_status("test-waiting")["state"] == "WAITING_INPUT"
    assert service.terminal_status("test-running")["state"] != "WAITING_INPUT"


def test_capture_limit_and_real_input(tmux_session_factory):
    tmux_session_factory("test-input", "bash -lc 'read value; echo VALUE=$value; sleep 10'")
    config = AppConfig(PermissionsConfig(True, True), ("test-*",), 5, 3,
                       InputPolicyConfig(allowed_session_patterns=("test-*",)))
    service = TerminalService(config)

    assert service.terminal_send_text("test-input", "hello-terminal-mcp", True)["sent"]
    time.sleep(0.3)
    assert "VALUE=hello-terminal-mcp" in service.terminal_tail("test-input", 20)["output"]
    capped = service.terminal_tail("test-input", 200)
    assert capped["truncated"]
    assert len(capped["output"].splitlines()) <= 5


def test_real_tmux_binding_remap_missing_and_cleanup(read_config, tmux_session_factory, tmp_path):
    tmux_session_factory("test-bind-claude", "bash -lc 'echo BOUND_A_READY; sleep 10'")
    tmux_session_factory("test-bind-codex", "bash -lc 'echo BOUND_B_READY; sleep 10'")
    time.sleep(0.3)
    service = TerminalService(read_config, bindings=BindingStore(tmp_path / "bindings.db"))

    assert service.terminal_bind("phase4-a", "test-bind-claude")["session_exists"]
    assert "BOUND_A_READY" in service.terminal_tail_bound("phase4-a")["output"]
    assert service.terminal_bind("phase4-a", "test-bind-codex")["error"] == "BINDING_EXISTS"
    assert service.terminal_bind("phase4-a", "test-bind-codex", replace=True)["replaced"]
    assert "BOUND_B_READY" in service.terminal_tail_bound("phase4-a")["output"]

    subprocess.run(
        ["tmux", "kill-session", "-t", "test-bind-codex"],
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert service.terminal_status_bound("phase4-a")["state"] == "MISSING"
    assert service.terminal_unbind("phase4-a")["unbound"]
