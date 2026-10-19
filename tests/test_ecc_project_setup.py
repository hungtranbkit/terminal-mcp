from pathlib import Path

from terminal_mcp.ecc_setup import plan_install, run_install


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "ecc"
    for name in ("tdd-workflow", "security-review", "ignored"):
        folder = source / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    (source / "hooks").mkdir()
    (source / "hooks" / "hooks.json").write_text("global hook config", encoding="utf-8")
    return source


def test_preflight_and_dry_run_plan_only_project_local_skill_paths(tmp_path):
    source = _source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    plan = plan_install(source, project, target="both", skills=("tdd-workflow",))
    assert plan.ready
    assert {item.relative_to(project).as_posix() for item in plan.destinations} == {
        ".claude/skills/tdd-workflow", ".agents/skills/tdd-workflow",
    }
    result = run_install(plan, apply=False)
    assert result["status"] == "DRY_RUN"
    assert not (project / ".claude").exists()
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".codex").exists()


def test_apply_copies_only_selected_skills_to_project_scoped_roots(tmp_path):
    source = _source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    plan = plan_install(source, project, target="both", skills=("tdd-workflow",))
    result = run_install(plan, apply=True)
    assert result["status"] == "APPLIED"
    assert (project / ".claude/skills/tdd-workflow/SKILL.md").is_file()
    assert (project / ".agents/skills/tdd-workflow/SKILL.md").is_file()
    assert not (project / ".claude/hooks").exists()
    assert not (project / ".codex").exists()
    assert not (project / ".claude/skills/ignored").exists()


def test_collision_blocks_whole_plan_without_overwriting_existing_skill(tmp_path):
    source = _source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    existing = project / ".agents/skills/tdd-workflow/SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("user skill", encoding="utf-8")
    plan = plan_install(source, project, target="both", skills=("tdd-workflow",))
    assert not plan.ready
    assert "already exists" in plan.errors[0]
    result = run_install(plan, apply=True)
    assert result["status"] == "BLOCKED"
    assert existing.read_text(encoding="utf-8") == "user skill"
    assert not (project / ".claude/skills/tdd-workflow").exists()


def test_symlinked_skill_source_is_rejected(tmp_path):
    source = _source(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("secret", encoding="utf-8")
    (source / "skills" / "linked").symlink_to(outside, target_is_directory=True)
    project = tmp_path / "project"
    project.mkdir()
    plan = plan_install(source, project, target="claude", skills=("linked",))
    assert not plan.ready
    assert "symlink" in plan.errors[0]


def test_destination_parent_symlink_cannot_escape_project(tmp_path):
    source = _source(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    plan = plan_install(source, project, target="codex", skills=("tdd-workflow",))
    assert not plan.ready
    assert "escapes project root" in plan.errors[0]
