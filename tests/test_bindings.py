from __future__ import annotations

from terminal_mcp.bindings import BindingStore, valid_binding_name
from terminal_mcp.config import AppConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.models import SessionInfo


class FakeTmux:
    def __init__(self, sessions: dict[str, str] | None = None) -> None:
        self.sessions = sessions or {}
        self.sent: list[tuple[str, str, bool]] = []

    def get_session(self, name: str):
        if name not in self.sessions:
            return None
        return SessionInfo(name, False, 1, 1, 1, 123, "bash", False)

    def capture_lines(self, session: str, lines: int, *, ansi: bool = False) -> list[str]:
        if session not in self.sessions:
            from terminal_mcp.tmux import TmuxError
            raise TmuxError("missing")
        return self.sessions[session].splitlines()[-lines:]

    def send_text(self, session: str, text: str, press_enter: bool) -> None:
        self.sent.append((session, text, press_enter))


def service(tmp_path, *, global_read=True, global_input=False) -> tuple[TerminalService, FakeTmux]:
    tmux = FakeTmux({
        "test-bind-a": "BOUND_A_READY\nOPENAI_API_KEY=sk-live-secret",
        "test-bind-b": "BOUND_B_READY",
        "private-one": "private",
        "test-secret-one": "secret",
    })
    config = AppConfig(PermissionsConfig(global_read, global_input), ("test-*",), 50, 20)
    return TerminalService(config, tmux, BindingStore(tmp_path / "bindings.db")), tmux


def test_binding_name_validation():
    for name in ("mesflow-dev", "projectflow.test", "codex_qa", "a" * 64):
        assert valid_binding_name(name)
    for name in ("../../root", "my binding", "$(whoami)", "UPPER", "a" * 65, ""):
        assert not valid_binding_name(name)


def test_bind_get_list_duplicate_replace_and_unbind(tmp_path):
    terminal, _ = service(tmp_path)
    created = terminal.terminal_bind("phase4-a", "test-bind-a")
    assert created["session"] == "test-bind-a"
    assert created["read_enabled"] is True
    assert created["input_enabled"] is False
    assert created["effective_input"] is False
    assert terminal.terminal_get_binding("phase4-a")["session_exists"] is True
    assert terminal.terminal_list_bindings()[0]["state"] == "RUNNING"
    assert terminal.terminal_bind("phase4-a", "test-bind-b")["error"] == "BINDING_EXISTS"
    replaced = terminal.terminal_bind("phase4-a", "test-bind-b", replace=True)
    assert replaced["session"] == "test-bind-b"
    assert replaced["replaced"] is True
    assert terminal.terminal_unbind("phase4-a")["unbound"] is True
    assert terminal.terminal_get_binding("phase4-a")["error"] == "BINDING_NOT_FOUND"


def test_bind_rejects_missing_forbidden_and_sensitive(tmp_path):
    terminal, _ = service(tmp_path)
    assert terminal.terminal_bind("missing", "test-missing")["error"] == "SESSION_NOT_FOUND"
    assert terminal.terminal_bind("private", "private-one")["error"] == "ACCESS_DENIED"
    assert terminal.terminal_bind("sensitive", "test-secret-one")["error"] == "ACCESS_DENIED"


def test_bound_permissions_and_redaction(tmp_path):
    terminal, tmux = service(tmp_path, global_input=False)
    terminal.terminal_bind("phase4-a", "test-bind-a", input_enabled=True)
    tail = terminal.terminal_tail_bound("phase4-a")
    assert "BOUND_A_READY" in tail["output"]
    assert "sk-live-secret" not in tail["output"]
    assert "<REDACTED>" in tail["output"]
    assert terminal.terminal_send_bound("phase4-a", "continue")["error"] == "INPUT_DISABLED"
    assert tmux.sent == []

    enabled_global, _ = service(tmp_path, global_input=True)
    enabled_global.terminal_bind("read-only", "test-bind-b")
    assert enabled_global.terminal_send_bound("read-only", "continue")["error"] == "BINDING_INPUT_DISABLED"

    read_disabled, _ = service(tmp_path, global_read=False)
    assert read_disabled.terminal_tail_bound("phase4-a")["error"] == "READ_DISABLED"


def test_session_disappears_but_binding_persists(tmp_path):
    terminal, tmux = service(tmp_path)
    terminal.terminal_bind("phase4-a", "test-bind-a")
    del tmux.sessions["test-bind-a"]
    assert terminal.terminal_get_binding("phase4-a")["session_exists"] is False
    assert terminal.terminal_tail_bound("phase4-a")["error"] == "SESSION_NOT_FOUND"
    assert terminal.terminal_status_bound("phase4-a")["state"] == "MISSING"


def test_sqlite_persists_across_store_instances(tmp_path):
    terminal, tmux = service(tmp_path)
    terminal.terminal_bind("phase4-a", "test-bind-a")
    config = terminal.config
    restarted = TerminalService(config, tmux, BindingStore(tmp_path / "bindings.db"))
    assert restarted.terminal_get_binding("phase4-a")["session"] == "test-bind-a"
