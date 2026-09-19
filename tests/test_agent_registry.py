"""TMCP-AGENT-RUNTIME-001 Phase B -- Agent Registry, Skill Registry, agent_start.

The thesis under test is one sentence: an Agent is a DURABLE identity and a
Session is a DISPOSABLE runtime. Most of what follows is that sentence made
falsifiable -- ownership survives a session being taken away, capacity is an
agent property rather than a session one, and skills are pinned at start time
so a run's evidence says what was actually loaded.

The other half is the traversal defence on filesystem skill loading, which is
tested against the specific shapes that defeat a naive check: `..`, absolute
paths, a NUL byte, and a symlink that escapes an approved root.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from terminal_mcp import skill_packages
from terminal_mcp.agent_registry import (
    AGENT_MIGRATIONS, AGENT_ACTIVE, AGENT_DISABLED, DEFAULT_MAX_SESSIONS, LATEST, SKILL_BASE, SKILL_TASK,
    AgentRegistryError, AgentRegistryStore, valid_slug,
)
from terminal_mcp.agent_service import AgentService
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import BOUND, QUEUED, UNROUTED, WAITING_RUNTIME, QueueStore
from terminal_mcp.skill_packages import SkillPackageError, load_package, resolve_package_dir


@pytest.fixture
def store(tmp_path):
    return AgentRegistryStore(tmp_path / "agents.db")


@pytest.fixture
def queue(tmp_path):
    return QueueService(QueueStore(tmp_path / "queue.db"))


@pytest.fixture
def service(store, queue):
    return AgentService(store, queue=queue, skill_roots=())


# ---------------------------------------------------------------------------
# Migrations.
# ---------------------------------------------------------------------------

def test_migrations_create_every_table_and_are_idempotent(tmp_path):
    path = tmp_path / "agents.db"
    first = AgentRegistryStore(path)
    first.create_agent("a1", name="One")
    # Re-opening the same file must apply nothing and lose nothing.
    second = AgentRegistryStore(path)
    assert second.get_agent("a1").name == "One"

    import sqlite3
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert {"agents", "skills", "agent_skills", "agent_runs",
            "projects", "project_phase_history"} <= tables
    assert version == len(AGENT_MIGRATIONS)


def test_the_database_file_is_not_world_readable(tmp_path):
    store = AgentRegistryStore(tmp_path / "agents.db")
    assert oct(os.stat(store.path).st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# Agent CRUD.
# ---------------------------------------------------------------------------

def test_create_get_list_update_and_disable(store):
    created = store.create_agent("reviewer", name="Reviewer", project_id="novaretail",
                                 runtime="claude", repo="/repos/web")
    assert created.state == AGENT_ACTIVE
    assert created.max_sessions == DEFAULT_MAX_SESSIONS == 1
    assert store.get_agent("reviewer").name == "Reviewer"
    assert [agent.id for agent in store.list_agents(project_id="novaretail")] == ["reviewer"]

    updated = store.update_agent("reviewer", name="Senior Reviewer", max_sessions=3)
    assert updated.name == "Senior Reviewer"
    assert updated.max_sessions == 3

    disabled = store.disable_agent("reviewer")
    assert disabled.state == AGENT_DISABLED
    assert disabled.enabled is False
    assert disabled.disabled_at
    # Disabled, never deleted: its history must stay readable.
    assert store.get_agent("reviewer") is not None
    assert store.enable_agent("reviewer").enabled is True


def test_a_duplicate_agent_id_is_refused(store):
    store.create_agent("dup")
    with pytest.raises(AgentRegistryError, match="already exists"):
        store.create_agent("dup")


@pytest.mark.parametrize("bad", ["../escape", "has space", "UPPER", "", "a/b", "x" * 65, "-lead"])
def test_an_invalid_agent_id_is_refused(store, bad):
    with pytest.raises(AgentRegistryError):
        store.create_agent(bad)


def test_updating_an_unknown_field_is_refused_by_name_not_ignored(store):
    store.create_agent("a1")
    with pytest.raises(AgentRegistryError, match="cannot update"):
        store.update_agent("a1", nonsense=1)


def test_max_sessions_below_one_is_refused(store):
    with pytest.raises(AgentRegistryError):
        store.create_agent("a1", max_sessions=0)


# ---------------------------------------------------------------------------
# Skill packages -- the traversal defence.
# ---------------------------------------------------------------------------

@pytest.fixture
def skill_root(tmp_path):
    root = tmp_path / "skills"
    (root / "review").mkdir(parents=True)
    (root / "review" / "SKILL.md").write_text(
        "---\nname: Review\nversion: 2.1\ndescription: careful review\nowner: qa\n---\n"
        "Read the diff and report findings.\n", encoding="utf-8")
    return root


def test_a_real_package_loads_with_its_front_matter(skill_root):
    package = load_package("review", [skill_root])
    assert package.skill_id == "review"
    assert package.version == "2.1"
    assert package.name == "Review"
    assert package.summary == "careful review"
    assert package.body.startswith("Read the diff")
    assert package.metadata == {"owner": "qa"}
    assert package.content_sha


@pytest.mark.parametrize("attack", [
    "../../../../etc/passwd",
    "..",
    "../review",
    "/etc/passwd",
    "review/../../etc",
    "review\x00.md",
    "reviewate\\..\\..\\etc",
])
def test_path_traversal_is_rejected_before_any_path_is_built(skill_root, attack):
    with pytest.raises(SkillPackageError, match="invalid skill id"):
        resolve_package_dir(attack, [skill_root])


def test_a_symlink_escaping_the_approved_root_is_rejected_after_resolution(tmp_path):
    """The check that a naive textual prefix test fails: the unresolved path is
    inside the root, and only resolution reveals that it is not."""
    root = tmp_path / "skills"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "SKILL.md").parent.mkdir(parents=True)
    (outside / "SKILL.md").write_text("secret", encoding="utf-8")
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SkillPackageError, match="outside its approved root"):
        resolve_package_dir("escape", [root])


def test_a_skill_outside_every_approved_root_is_not_found(tmp_path, skill_root):
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(SkillPackageError, match="no SKILL.md found"):
        resolve_package_dir("review", [other])


def test_no_approved_roots_is_a_clear_refusal_not_a_silent_empty(tmp_path):
    with pytest.raises(SkillPackageError, match="no approved skill roots"):
        resolve_package_dir("review", [])


def test_an_oversized_skill_body_is_refused_rather_than_truncated(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    (root / "huge").mkdir(parents=True)
    monkeypatch.setattr(skill_packages, "MAX_SKILL_BYTES", 64)
    (root / "huge" / "SKILL.md").write_text("x" * 200, encoding="utf-8")
    with pytest.raises(SkillPackageError, match="larger than"):
        load_package("huge", [root])


def test_discover_reports_what_it_skipped_and_why(tmp_path, skill_root):
    (skill_root / "Not A Slug").mkdir()
    (skill_root / "Not A Slug" / "SKILL.md").write_text("x", encoding="utf-8")
    rows = skill_packages.discover([skill_root])
    found = [row for row in rows if "skill_id" in row]
    skipped = [row["skipped"] for row in rows if "skipped" in row]
    assert [row["skill_id"] for row in found] == ["review"]
    assert skipped and skipped[0]["name"] == "Not A Slug"


# ---------------------------------------------------------------------------
# Skill registry + bindings.
# ---------------------------------------------------------------------------

def test_registering_the_same_content_twice_is_a_no_op(store):
    first = store.register_skill("lint", version="1", body="run the linter")
    second = store.register_skill("lint", version="1", body="run the linter")
    assert first.created_at == second.created_at
    assert store.skill_versions("lint") == ["1"]


def test_redefining_a_version_with_different_content_is_refused(store):
    store.register_skill("lint", version="1", body="run the linter")
    with pytest.raises(AgentRegistryError, match="different content"):
        store.register_skill("lint", version="1", body="something else entirely")


def test_a_new_version_is_added_and_latest_resolves_to_it(store):
    store.register_skill("lint", version="1", body="v1")
    store.register_skill("lint", version="2", body="v2")
    assert store.get_skill("lint").version == "2"
    assert store.get_skill("lint", version="1").body == "v1"
    assert set(store.skill_versions("lint")) == {"1", "2"}
    assert [skill.version for skill in store.list_skills()] == ["2"]


def test_binding_a_skill_requires_the_agent_and_the_skill_to_exist(store):
    with pytest.raises(AgentRegistryError, match="agent"):
        store.bind_skill("ghost", "lint")
    store.create_agent("a1")
    with pytest.raises(AgentRegistryError, match="not registered"):
        store.bind_skill("a1", "lint")


def test_base_and_task_skills_are_separate_bindings(store):
    store.create_agent("a1")
    store.register_skill("lint", version="1", body="x")
    store.register_skill("deploy", version="1", body="y")
    store.bind_skill("a1", "lint", kind=SKILL_BASE)
    store.bind_skill("a1", "deploy", kind=SKILL_TASK)

    assert store.resolve_skills("a1") == ["lint@1"]
    assert set(store.resolve_skills("a1", include_task_skills=True)) == {"lint@1", "deploy@1"}


def test_a_pinned_binding_stays_on_its_version_while_a_floating_one_moves(store):
    store.create_agent("a1")
    store.register_skill("lint", version="1", body="v1")
    store.bind_skill("a1", "lint", version="1")
    store.register_skill("lint", version="2", body="v2")
    assert store.resolve_skills("a1") == ["lint@1"]

    store.bind_skill("a1", "lint", version=LATEST)
    assert store.resolve_skills("a1") == ["lint@2"]


def test_rebinding_the_same_skill_updates_rather_than_duplicating(store):
    store.create_agent("a1")
    store.register_skill("lint", version="1", body="x")
    store.bind_skill("a1", "lint")
    store.bind_skill("a1", "lint")
    assert len(store.agent_skills("a1")) == 1


def test_register_skill_through_the_service_loads_from_an_approved_root(store, queue, skill_root):
    service = AgentService(store, queue=queue, skill_roots=[str(skill_root)])
    result = service.register_skill("review")
    assert result["skill"]["version"] == "2.1"
    assert result["skill"]["source"] == "filesystem"
    assert result["loaded_from"].endswith("review/SKILL.md")


def test_register_skill_through_the_service_refuses_traversal(store, queue, skill_root):
    service = AgentService(store, queue=queue, skill_roots=[str(skill_root)])
    result = service.register_skill("../../../etc")
    assert result["error"] == "SKILL_PACKAGE_REJECTED"
    assert "invalid skill id" in result["detail"]
