from __future__ import annotations

import hashlib
import stat

from terminal_mcp.audit import AuditStore
from terminal_mcp.bindings import BindingStore
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.models import SessionInfo


class FakeTmux:
    def __init__(self, commands=None):
        self.commands = commands or {"claude-test": "claude", "ssh-test": "bash"}
        self.sent = []

    def get_session(self, name):
        command = self.commands.get(name)
        return None if command is None else SessionInfo(name, False, 1, 1, 1, 1, command, False)

    def capture_lines(self, session, lines):
        # Genuinely grows (a new line per send_text/send_keys call
        # recorded) rather than just changing content in place -- P0 Part A
        # adapters (CodexAdapter/ClaudeAdapter) require real line-count
        # growth as submission evidence, not a bare "did the content
        # differ" check (see adapters.py), so the fake must model an
        # actually-growing pane to exercise the same confirm-on-first-poll
        # path this fixture has always relied on.
        return ["ready", "prompt>", *[f"call-{i}" for i in range(len(self.sent))]]

    def send_text(self, session, text, press_enter):
        self.sent.append((session, text, press_enter))

    def send_keys(self, session, keys):
        self.sent.append((session, keys))


def make_service(tmp_path, *, enabled=True, command="claude", max_text=12_000):
    tmux = FakeTmux({"claude-test": command, "ssh-test": "bash"})
    config = AppConfig(
        PermissionsConfig(True, enabled), ("claude-*", "ssh-*"), 50, 20,
        InputPolicyConfig(
            allowed_session_patterns=("claude-*", "ssh-*"),
            denied_session_patterns=("ssh-*",), max_text_length=max_text,
        ),
    )
    audit = AuditStore(tmp_path / "audit.db")
    bindings = BindingStore(tmp_path / "bindings.db")
    return TerminalService(config, tmux, bindings, audit), tmux, audit


def test_global_policy_size_dry_run_and_audit(tmp_path):
    disabled, _, disabled_audit = make_service(tmp_path / "disabled", enabled=False)
    assert disabled.terminal_send_text("claude-test", "hello")["error"] == "INPUT_DISABLED"
    assert disabled_audit.list()[0]["result"] == "BLOCKED"

    service, tmux, audit = make_service(tmp_path / "enabled", max_text=5)
    assert service.terminal_send_text("ssh-test", "hi")["error"] == "ACCESS_DENIED"
    assert service.terminal_send_text("claude-test", "123456")["error"] == "INPUT_TOO_LARGE"
    result = service.terminal_send_text("claude-test", "hello", True, dry_run=True)
    assert result["would_send"] and not tmux.sent
    assert audit.list()[0]["result"] == "DRY_RUN"


def test_binding_permission_and_literal_send(tmp_path):
    service, tmux, audit = make_service(tmp_path)
    service.terminal_bind("agent", "claude-test")
    assert service.terminal_send_bound("agent", "x")["error"] == "BINDING_INPUT_DISABLED"
    service.terminal_bind("agent", "claude-test", replace=True, input_enabled=True)
    text = "```sh\necho '$HOME' && $(whoami)\n```"
    result = service.terminal_send_bound("agent", text, True)
    assert result["sent"]
    assert result["submit_status"] == "SUBMIT_CONFIRMED"
    # text is sent literal-only (Enter is now a separate, later send_keys
    # call -- see the lost-Enter settle-delay fix in core.py/tmux.py).
    assert tmux.sent[-2] == ("claude-test", text, False)
    assert tmux.sent[-1] == ("claude-test", ["Enter"])
    assert audit.list(binding="agent")[0]["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_keys_sensitive_target_and_context(tmp_path):
    service, tmux, _ = make_service(tmp_path)
    assert service.terminal_send_keys("claude-test", ["F13"])["error"] == "KEY_NOT_ALLOWED"
    assert service.terminal_send_keys("claude-test", ["C-c"])["error"] == "CONFIRMATION_REQUIRED"
    assert service.terminal_send_keys("claude-test", ["C-c"], True)["sent"]
    service.tmux.commands["claude-test"] = "ssh"
    assert service.terminal_send_text("claude-test", "hello")["error"] == "SENSITIVE_TARGET"
    context = service.terminal_input_context(session="claude-test")
    assert context["current_command"] == "ssh" and context["effective_input"] is False


def test_audit_redacts_secret_never_stores_full_text_and_filters(tmp_path):
    service, _, audit = make_service(tmp_path)
    secret = "OPENAI_API_KEY=sk-super-secret-value continue"
    service.terminal_send_text("claude-test", secret, dry_run=True)
    event = service.terminal_list_input_audit(session="claude-test")["events"][0]
    assert "sk-super-secret-value" not in event["preview"]
    assert "<REDACTED>" in event["preview"]
    assert event["text_length"] == len(secret)
    assert secret.encode() not in audit.path.read_bytes()
    assert stat.S_IMODE(audit.path.stat().st_mode) == 0o600
    assert service.terminal_list_input_audit(binding="missing")["events"] == []
