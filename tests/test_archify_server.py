from pathlib import Path

from terminal_mcp.config import AppConfig, ArchifyConfig, PermissionsConfig, RepoReadConfig
from terminal_mcp.server_http import build_archify_service


def test_build_archify_service_uses_shared_persistent_state_and_configured_roots(
    tmp_path: Path, monkeypatch,
) -> None:
    state_home = tmp_path / "state"
    project_root = tmp_path / "projects"
    runtime_root = tmp_path / "shared-archify"
    project_root.mkdir()
    runtime_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("TERMINAL_MCP_ARCHIFY_HOME", str(tmp_path / "env-runtime"))

    config = AppConfig(
        permissions=PermissionsConfig(),
        allowed_session_patterns=("test-*",),
        repo_read=RepoReadConfig(allowed_roots=(str(tmp_path / "wrong-root"),)),
        archify=ArchifyConfig(
            runtime_dir=str(runtime_root),
            allowed_roots=(str(project_root),),
            timeout_seconds=17,
            max_output_bytes=12345,
        ),
    )

    service = build_archify_service(config)
    try:
        assert service.store.path == state_home / "terminal-mcp" / "archify.db"
        assert service.artifact_root == state_home / "terminal-mcp" / "archify_artifacts"
        assert service.policy.allowed_roots == (project_root.resolve(),)
        assert service.runtime.runtime_dir == runtime_root.resolve()
        assert service.runtime.timeout == 17
        assert service.runtime.max_output_bytes == 12345
    finally:
        service.close()


def test_build_archify_service_falls_back_to_existing_repo_read_roots(
    tmp_path: Path, monkeypatch,
) -> None:
    root = tmp_path / "repos"
    root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TERMINAL_MCP_ARCHIFY_HOME", raising=False)
    config = AppConfig(
        permissions=PermissionsConfig(),
        allowed_session_patterns=("test-*",),
        repo_read=RepoReadConfig(allowed_roots=(str(root),)),
    )

    service = build_archify_service(config)
    try:
        assert service.policy.allowed_roots == (root.resolve(),)
        assert service.runtime.runtime_dir == (
            tmp_path / "state" / "terminal-mcp" / "archify-runtime"
        ).resolve()
    finally:
        service.close()


def test_build_archify_service_matches_repo_read_home_fallback(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("TERMINAL_MCP_ARCHIFY_HOME", raising=False)
    config = AppConfig(
        permissions=PermissionsConfig(),
        allowed_session_patterns=("test-*",),
    )

    service = build_archify_service(config)
    try:
        assert service.policy.allowed_roots == (Path.home().resolve(),)
    finally:
        service.close()
