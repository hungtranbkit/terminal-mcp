import json
import os
import subprocess
from pathlib import Path

import pytest

from terminal_mcp.agent_launch_policy import (
    LaunchPolicyError,
    build_permission_args,
    validate_project_directory,
    register_claude_project_trust,
    register_codex_project_trust,
)


def test_owner_policy_uses_explicit_cli_flags_and_native_opt_out():
    def probe(argv, **kwargs):
        assert argv[-1] == "--version"
        return subprocess.CompletedProcess(argv, 0, "cli 1.0", "")

    assert build_permission_args("claude", "owner", "claude", probe=probe) == (
        "--dangerously-skip-permissions",
    )
    assert build_permission_args("codex", "owner", "codex", probe=probe) == ("--yolo",)
    assert build_permission_args("codex", "native", "codex", probe=probe) == ()


def test_owner_policy_fails_closed_when_cli_rejects_requested_mode():
    def reject(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, "", "unrecognized option")

    with pytest.raises(LaunchPolicyError, match="PERMISSION_MODE_UNSUPPORTED"):
        build_permission_args("codex", "owner", "codex", probe=reject)


def test_project_directory_requires_exact_trusted_git_root_and_owner(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setattr("terminal_mcp.agent_launch_policy._git", lambda _cwd, flag: {
        "--show-toplevel": str(repo), "--git-common-dir": str(repo / ".git"),
    }[flag])
    monkeypatch.setattr("terminal_mcp.agent_launch_policy._owned_by_current_user", lambda _path: True)

    result = validate_project_directory(str(repo), allowed_roots=[str(tmp_path)],
                                        trusted_project_roots=[str(repo)])
    assert result.path == repo.resolve()
    assert result.repo_root == repo.resolve()

    with pytest.raises(LaunchPolicyError, match="PROJECT_ROOT_NOT_TRUSTED"):
        validate_project_directory(str(repo), allowed_roots=[str(tmp_path)],
                                   trusted_project_roots=[str(tmp_path / "other")])


def test_linked_worktree_inherits_trust_only_from_verified_common_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    worktree = tmp_path / "repo-worktree"
    repo.mkdir(); worktree.mkdir(); (worktree / ".git").write_text("gitdir: ../repo/.git/worktrees/wt")
    values = {"--show-toplevel": str(worktree), "--git-common-dir": str(repo / ".git")}
    monkeypatch.setattr("terminal_mcp.agent_launch_policy._git", lambda _cwd, flag: values[flag])
    monkeypatch.setattr("terminal_mcp.agent_launch_policy._owned_by_current_user", lambda _path: True)

    result = validate_project_directory(str(worktree), allowed_roots=[str(tmp_path)],
                                        trusted_project_roots=[str(repo)])
    assert result.path == worktree.resolve()
    assert result.common_dir == (repo / ".git").resolve()


def test_claude_trust_registers_only_exact_path_and_preserves_existing_config(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": False}},
                                  "mcpServers": {"keep": {"command": "tool"}}}))
    project = tmp_path / "repo"
    project.mkdir()
    (project / ".git").mkdir()

    result = register_claude_project_trust(project, config_path=config)
    updated = json.loads(config.read_text())
    assert result["registered"] is True
    assert updated["projects"][str(project.resolve())]["hasTrustDialogAccepted"] is True
    assert updated["projects"]["/other"]["hasTrustDialogAccepted"] is False
    assert updated["mcpServers"] == {"keep": {"command": "tool"}}
    assert (config.stat().st_mode & 0o777) == 0o600


def test_codex_trust_appends_supported_project_entry_without_rewriting_existing_config(tmp_path):
    config = tmp_path / "codex.toml"
    original = 'model = "gpt-6"\n\n[projects."/other"]\ntrust_level = "untrusted"\n'
    config.write_text(original)
    project = tmp_path / "repo"
    project.mkdir(); (project / ".git").mkdir()

    result = register_codex_project_trust(project, config_path=config)
    updated = config.read_text()
    assert result["registered"] is True
    assert updated.startswith(original)
    assert f'[projects."{project.resolve()}"]\ntrust_level = "trusted"' in updated
    assert (config.stat().st_mode & 0o777) == 0o600


def test_codex_trust_does_not_override_explicit_untrusted_choice(tmp_path):
    project = tmp_path / "repo"
    project.mkdir(); (project / ".git").mkdir()
    config = tmp_path / "codex.toml"
    config.write_text(f'[projects."{project.resolve()}"]\ntrust_level = "untrusted"\n')
    with pytest.raises(LaunchPolicyError, match="CODEX_PROJECT_EXPLICITLY_UNTRUSTED"):
        register_codex_project_trust(project, config_path=config)

