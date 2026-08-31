from terminal_mcp.config import AppConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.permissions import session_allowed


def test_whitelist_and_default_deny(read_config):
    assert session_allowed("test-one", read_config)
    assert session_allowed("agent-build", read_config)
    assert not session_allowed("personal", read_config)
    assert not session_allowed("test-secret", read_config)
    assert not session_allowed("test-one;display-message", read_config)


def test_sensitive_name_requires_exact_pattern():
    cfg = AppConfig(PermissionsConfig(), ("root",), 50, 20)
    assert session_allowed("root", cfg)


def test_input_disabled_by_default(read_config):
    service = TerminalService(read_config)
    assert service.terminal_send_text("test-any", "hello")["error"] == "INPUT_DISABLED"
    assert service.terminal_send_keys("test-any", ["Enter"])["error"] == "INPUT_DISABLED"


def test_key_allowlist_rejects_arbitrary_key():
    cfg = AppConfig(PermissionsConfig(True, True), ("test-*",), 50, 20)
    service = TerminalService(cfg)
    result = service.terminal_send_keys("test-any", ["run-shell"])
    assert result["error"] in {"KEY_NOT_ALLOWED", "ACCESS_DENIED"}
