from __future__ import annotations

import yaml
import pytest

from terminal_mcp.archify_policy import (
    ArchifyPolicyError,
    ArchifyProjectPolicy,
)
from terminal_mcp.config import load_config


def _config(tmp_path, archify: object):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"archify": archify}), encoding="utf-8")
    return path


@pytest.mark.parametrize("root", ["/", "relative/project"])
def test_archify_roots_must_be_absolute_and_not_filesystem_root(tmp_path, root):
    with pytest.raises(ValueError, match="archify.allowed_roots"):
        load_config(_config(tmp_path, {"allowed_roots": [root]}))


def test_archify_config_loads_bounded_values(tmp_path):
    config = load_config(_config(tmp_path, {
        "enabled": False,
        "runtime_dir": str(tmp_path / "runtime"),
        "allowed_roots": [str(tmp_path)],
        "max_files": 25,
        "max_source_bytes": 8192,
        "workers": 2,
    }))

    assert config.archify.enabled is False
    assert config.archify.allowed_roots == (str(tmp_path),)
    assert config.archify.max_files == 25
    assert config.archify.max_source_bytes == 8192
    assert config.archify.workers == 2


def test_project_selection_rejects_dot_dot_and_symlink_escape(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (outside / ".git").mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    policy = ArchifyProjectPolicy((str(allowed),))

    with pytest.raises(ArchifyPolicyError, match="PROJECT_NOT_ALLOWED"):
        policy.resolve_project(allowed / ".." / "outside")
    with pytest.raises(ArchifyPolicyError, match="PROJECT_NOT_ALLOWED"):
        policy.resolve_project(allowed / "escape")


def test_project_selection_accepts_a_manifest_project_inside_root(tmp_path):
    allowed = tmp_path / "allowed"
    project = allowed / "terminal-mcp"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='terminal-mcp'\n", encoding="utf-8")
    policy = ArchifyProjectPolicy((str(allowed),))

    selected = policy.resolve_project(project)

    assert selected.path == str(project.resolve())
    assert selected.name == "terminal-mcp"


def test_discovery_is_bounded_and_does_not_follow_symlinked_directories(tmp_path):
    allowed = tmp_path / "allowed"
    first = allowed / "one"
    second = allowed / "nested" / "two"
    outside = tmp_path / "outside"
    for project in (first, second, outside):
        project.mkdir(parents=True)
        (project / ".git").mkdir()
    (allowed / "outside-link").symlink_to(outside, target_is_directory=True)

    projects = ArchifyProjectPolicy(
        (str(allowed),), max_projects=1, max_discovery_depth=2,
    ).discover_projects()

    assert len(projects) == 1
    assert projects[0].path.startswith(str(allowed.resolve()))
    assert projects[0].name != "outside"


def test_discovery_stops_after_bounded_candidate_entries(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    for index in range(6):
        child = allowed / f"item-{index}"
        child.mkdir()
        (child / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    policy = ArchifyProjectPolicy((allowed,), max_projects=20, max_discovery_depth=1,
                                  max_entries=3)

    assert len(policy.discover_projects()) <= 3
