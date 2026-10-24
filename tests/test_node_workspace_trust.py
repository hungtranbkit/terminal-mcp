"""Linux node workspace creation and first-run scoped trust regressions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prepare-node-workspace.py"


def run_setup(tmp_path: Path, config_text: str, *, repeat: bool = False):
    config = tmp_path / "node-config.yaml"
    if not repeat:
        config.write_text(config_text)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--config", str(config)],
        env={**os.environ, "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=10,
    )
    return result, config


def test_new_workspace_scoped_trust_and_allowlist(tmp_path):
    data = 'session_lifecycle:\n  enabled: true\n  allowed_cwd_roots:\n    - /home/dell/workspace\n    - /home/dell\n  protected_sessions:\n    - terminal-mcp\n'
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text('[tui]\nanimations = true\n')
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {"/outside": {"hasTrustDialogAccepted": False}}, "example": "keep"}))
    result, config = run_setup(tmp_path, data)
    assert result.returncode == 0, result.stderr
    workspace = tmp_path / "workspace"
    assert workspace.is_dir() and not workspace.is_symlink()
    new = config.read_text()
    assert new.index("    - " + str(workspace)) < new.index("    - /home/dell/workspace")
    codex = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert codex["projects"][str(workspace)]["trust_level"] == "trusted"
    assert codex["tui"]["animations"] is True
    claude = json.loads((tmp_path / ".claude.json").read_text())
    assert claude["projects"][str(workspace)]["hasTrustDialogAccepted"] is True
    assert claude["projects"]["/outside"]["hasTrustDialogAccepted"] is False
    originals = [p.read_bytes() for p in (config, tmp_path / ".codex/config.toml", tmp_path / ".claude.json")]
    again, _ = run_setup(tmp_path, data, repeat=True)
    assert again.returncode == 0, again.stderr
    assert originals == [p.read_bytes() for p in (config, tmp_path / ".codex/config.toml", tmp_path / ".claude.json")]


def test_new_workspace_without_trust_files(tmp_path):
    r, conf = run_setup(tmp_path, "session_lifecycle:\n  enabled: true\n")
    assert r.returncode == 0, r.stderr
    assert "allowed_cwd_roots:" in conf.read_text()
    assert (tmp_path / "workspace").is_dir()


def test_preexisting_symlink_fails_closed(tmp_path):
    target = tmp_path / "some-other-project"
    target.mkdir()
    (tmp_path / "workspace").symlink_to(target, target_is_directory=True)
    r, _ = run_setup(tmp_path, "session_lifecycle:\n  enabled: true\n")
    assert r.returncode != 0
    assert not (tmp_path / ".codex/config.toml").exists()


def test_explicit_untrust_not_overridden(tmp_path):
    (tmp_path / ".codex").mkdir()
    workspace = tmp_path / "workspace"
    (tmp_path / ".codex/config.toml").write_text('[projects."'+str(workspace)+'"]\ntrust_level = "untrusted"\n')
    r, _ = run_setup(tmp_path, "session_lifecycle:\n  enabled: true\n")
    assert r.returncode != 0
    assert "manual review" in r.stderr
    assert 'untrusted' in (tmp_path / ".codex/config.toml").read_text()


def test_paired_installer_invokes_setup_only_in_paired_mode():
    installer = (Path(__file__).resolve().parents[1] / "deploy/install-node-agent.sh").read_text()
    assert 'if [[ "$PAIRED_MODE" -eq 1 ]]; then' in installer
    assert '"$SOURCE_DIR/scripts/prepare-node-workspace.py" --config "$REPO_DIR/config.yaml"' in installer


def test_pristine_repo_example_enables_managed_session_workspace(tmp_path):
    import yaml
    example = (Path(__file__).resolve().parents[1] / "config.example.yaml").read_text()
    result, config = run_setup(tmp_path, example)
    assert result.returncode == 0, result.stderr
    parsed = yaml.safe_load(config.read_text())
    assert parsed["session_lifecycle"]["enabled"] is True
    assert parsed["session_lifecycle"]["allowed_cwd_roots"][0] == str(tmp_path / "workspace")
    again, _ = run_setup(tmp_path, example, repeat=True)
    assert again.returncode == 0, again.stderr
    assert yaml.safe_load(config.read_text()) == parsed
