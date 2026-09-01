from __future__ import annotations

import time
import subprocess

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.bindings import BindingStore
from terminal_mcp.tmux import TmuxClient


def test_real_tmux_list_tail_status_and_denial(read_config, tmux_session_factory):
    tmux_session_factory("test-running", "bash -lc 'for i in 1 2 3 4 5; do echo BUILD STEP $i; sleep 0.1; done; sleep 10'")
    tmux_session_factory("test-waiting", "bash -lc 'echo \"Do you want to continue? [y/N]\"; read answer; echo ANSWER=$answer; sleep 10'")
    tmux_session_factory("private-session", "bash -lc 'echo SHOULD_NOT_LEAK; sleep 10'")
    time.sleep(0.8)
    service = TerminalService(read_config)

    # terminal_list_sessions discovers the FULL tmux inventory (see
    # core.py's dashboard-grant feature) -- "private-session" (outside
    # the static whitelist) is listed too, but strictly as metadata: no
    # content field anywhere, and its capability flags all say no.
    rows = {item["name"]: item for item in service.terminal_list_sessions()["sessions"]}
    assert {"test-running", "test-waiting", "private-session"} <= set(rows)
    private_row = rows["private-session"]
    assert private_row["allowed"] is False
    assert private_row["read_allowed"] is False
    assert private_row["input_allowed"] is False
    assert set(private_row) == {"name", "allowed", "attached", "windows", "created",
                                "activity", "read_allowed", "read_granted",
                                "input_allowed", "input_granted"}  # no content field, ever
    assert "BUILD STEP 5" in service.terminal_tail("test-running", 20)["output"]
    # Discovery never grants access -- still the exact same ACCESS_DENIED
    # a raw, unmodified whitelist check has always produced.
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


def test_capture_lines_returns_exact_bounded_recent_count(tmux_session_factory):
    # Regression for the pre-existing tmux capture quirk: `-S -N` with no `-E`
    # always captures through the bottom of the *visible* pane on top of the
    # requested history offset, so raw output could hold far more than N rows.
    # capture_lines must deterministically bound to exactly N of the most
    # recent real lines whenever more than N are available.
    session = tmux_session_factory(
        "test-exact-bound",
        "bash -lc 'for i in $(seq -w 1 500); do echo line$i; done; sleep 15'",
    )
    time.sleep(0.3)
    client = TmuxClient()
    for n in (1, 7, 50, 300):
        result = client.capture_lines(session, n)
        assert len(result) == n
        assert result[-1] == "line500"
        assert result[0] == f"line{501 - n:03d}"
    # Chronological within the window too: oldest of the window first, newest last.
    window = client.capture_lines(session, 10)
    assert window == [f"line{i:03d}" for i in range(491, 501)]


def test_capture_lines_returns_all_available_when_fewer_than_requested(tmux_session_factory):
    # The other edge of the same fix: a small headless pane pads its capture
    # with blank rows below short real output. Those must not be counted as
    # "content" or corrupt the bound — fewer real lines than requested means
    # every real line comes back, nothing more.
    session = tmux_session_factory("test-short-output", "bash -lc 'echo only-line; sleep 15'")
    time.sleep(0.3)
    client = TmuxClient()
    result = client.capture_lines(session, 300)
    assert result == ["only-line"]


def test_capture_lines_ansi_true_preserves_escapes_default_strips_them(tmux_session_factory):
    # Real ANSI color output from an actual program, captured through tmux.
    session = tmux_session_factory(
        "test-ansi-capture",
        "bash -lc 'printf \"\\x1b[31mERROR\\x1b[0m plain\\n\"; sleep 15'",
    )
    time.sleep(0.3)
    client = TmuxClient()

    plain = client.capture_lines(session, 10)  # default: ansi=False, unchanged behavior
    colored = client.capture_lines(session, 10, ansi=True)

    joined_plain = "\n".join(plain)
    joined_colored = "\n".join(colored)
    assert "\x1b[" not in joined_plain
    assert "ERROR" in joined_plain
    assert "\x1b[" in joined_colored
    assert "ERROR" in joined_colored


def test_terminal_tail_default_is_byte_identical_regardless_of_ansi_output(read_config, tmux_session_factory):
    # The MCP-facing contract (ChatGPT/Claude call terminal_tail with no
    # `ansi` kwarg) must stay exactly as before this feature: no escape
    # sequences ever leak into it, even when the pane really has color.
    session = tmux_session_factory(
        "test-mcp-tail-plain",
        "bash -lc 'printf \"\\x1b[32mBUILD OK\\x1b[0m\\n\"; sleep 15'",
    )
    time.sleep(0.3)
    service = TerminalService(read_config)
    result = service.terminal_tail(session, 10)
    assert "\x1b[" not in result["output"]
    assert "BUILD OK" in result["output"]


def test_terminal_tail_ansi_redacts_secret_even_when_colored(tmux_session_factory):
    # End-to-end: a real tmux session prints a secret wrapped in real ANSI
    # color codes; the ansi=True path (used by the dashboard) must still
    # redact it, same as the security regression already covered at the
    # redaction-function level in test_redaction.py.
    session = tmux_session_factory(
        "test-ansi-redact",
        "bash -lc 'printf \"OPENAI_API_KEY=sk-\\x1b[31mlivesecretvalue1234567890\\x1b[0m\\n\"; sleep 15'",
    )
    time.sleep(0.3)
    config = AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20)
    service = TerminalService(config)
    result = service.terminal_tail(session, 10, ansi=True)
    assert "livesecretvalue1234567890" not in result["output"]
    assert "<REDACTED>" in result["output"]
    # Whitelist is unaffected by the ansi flag: a disallowed session is still denied.
    assert service.terminal_tail("private-ansi-session", 10, ansi=True)["error"] == "ACCESS_DENIED"


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
