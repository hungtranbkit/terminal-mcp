from terminal_mcp.herdr_core_backend import HerdrCoreBackend
from terminal_mcp.session_backend_factory import BackendMode, build_session_backend, backend_mode
from terminal_mcp.tmux import TmuxClient


def test_tmux_only_is_default_even_when_hc_exists():
    assert backend_mode({}) is BackendMode.TMUX_ONLY
    assert isinstance(build_session_backend(node_id="hp-linux", env={}), TmuxClient)


def test_herdr_default_requires_explicit_node_allowlist():
    env = {"TERMINAL_MCP_SESSION_MODE": "HERDR_DEFAULT", "TERMINAL_MCP_HERDR_NODES": "dell-linux",
           "TERMINAL_MCP_HC_BIN": "/bin/true"}
    assert isinstance(build_session_backend(node_id="hp-linux", env=env), TmuxClient)
    assert isinstance(build_session_backend(node_id="dell-linux", env=env), HerdrCoreBackend)


def test_dual_read_and_canary_preserve_tmux_primary():
    for mode in ("DUAL_READ", "HERDR_CANARY"):
        backend = build_session_backend(node_id="dell-linux", env={
            "TERMINAL_MCP_SESSION_MODE": mode,
            "TERMINAL_MCP_HERDR_NODES": "dell-linux",
        })
        assert isinstance(backend, TmuxClient)


def test_herdr_default_fails_closed_when_binary_or_timeout_is_invalid():
    common = {"TERMINAL_MCP_SESSION_MODE": "HERDR_DEFAULT", "TERMINAL_MCP_HERDR_NODES": "hp-linux"}
    assert isinstance(build_session_backend(node_id="hp-linux", env={
        **common, "TERMINAL_MCP_HC_BIN": "/missing/hc",
    }), TmuxClient)
    assert isinstance(build_session_backend(node_id="hp-linux", env={
        **common, "TERMINAL_MCP_HC_BIN": "/bin/true", "TERMINAL_MCP_HC_TIMEOUT": "bad",
    }), TmuxClient)
