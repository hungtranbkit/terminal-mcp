"""The two bounds on injected skill content that nothing else pinned.

Count is already covered (test_project_start.test_skill_injection_is_bounded_
by_count) and so is version pinning. These are the other two dimensions the
production-readiness pass asks for: a SIZE ceiling, and containment inside the
configured skill roots.

Both are safety properties rather than niceties. An unbounded body front-loads
an agent's context with more briefing than request; an uncontained one reads a
file the operator never approved.
"""
from __future__ import annotations

import pytest

from terminal_mcp.agent_registry import AgentRegistryStore
from terminal_mcp.agent_service import AgentService
from terminal_mcp.queue_engine import MAX_SKILL_PREAMBLE_CHARS, QueueEngine
from terminal_mcp.queue_service import QueueService
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.skill_packages import MAX_SKILL_BYTES, SkillPackageError, load_package


@pytest.fixture
def wired(tmp_path):
    queue = QueueService(QueueStore(tmp_path / "queue.db"))
    agents = AgentService(AgentRegistryStore(tmp_path / "agents.db"), queue=queue, skill_roots=())
    return queue, agents


def _task_with_skill(queue, agents, *, body, skill_id="huge", version="1"):
    agents.store.register_skill(skill_id, version=version, name="Huge", body=body)
    (task_id,) = queue.store.set_tasks("lane", [{"prompt": "the actual request"}])
    queue.store.set_agent_binding(task_id, skill_ids=[f"{skill_id}@{version}"])
    return queue.store.get_task(task_id)


def test_an_oversized_briefing_is_truncated_with_a_visible_marker(wired):
    """Silently dropping the tail would hide that a skill is too large, and
    stopping mid-sentence reads as corruption."""
    queue, agents = wired
    body = "\n".join(f"line {n} of a very long standing instruction" for n in range(4000))
    task = _task_with_skill(queue, agents, body=body)

    engine = QueueEngine(queue.store, ops=None, coordinator=None)
    engine.skill_loader = agents.skill_preamble
    preamble = engine._skills_preamble(task)

    assert preamble is not None
    assert len(preamble) <= MAX_SKILL_PREAMBLE_CHARS + 80
    assert preamble.rstrip().endswith("[skill briefing truncated at the configured size limit]")


def test_a_briefing_inside_the_limit_is_passed_through_whole(wired):
    queue, agents = wired
    body = "keep every word of this"
    task = _task_with_skill(queue, agents, body=body)

    engine = QueueEngine(queue.store, ops=None, coordinator=None)
    engine.skill_loader = agents.skill_preamble
    preamble = engine._skills_preamble(task)

    assert body in preamble
    assert "truncated" not in preamble


def test_a_failing_skill_store_never_blocks_a_dispatch(wired):
    """A briefing is an enhancement. A task that cannot be briefed still runs."""
    queue, agents = wired
    task = _task_with_skill(queue, agents, body="x")

    def boom(_task):
        raise RuntimeError("skill store is down")

    engine = QueueEngine(queue.store, ops=None, coordinator=None)
    engine.skill_loader = boom
    assert engine._skills_preamble(task) is None


def test_a_skill_package_outside_its_approved_root_is_refused(tmp_path):
    approved = tmp_path / "skills"
    (approved / "honest").mkdir(parents=True)
    (approved / "honest" / "SKILL.md").write_text("real content")
    outside = tmp_path / "elsewhere"
    (outside / "sneaky").mkdir(parents=True)
    (outside / "sneaky" / "SKILL.md").write_text("content the operator never approved")
    (approved / "sneaky").symlink_to(outside / "sneaky", target_is_directory=True)

    assert load_package("honest", [approved]).body == "real content"
    with pytest.raises(SkillPackageError) as excinfo:
        load_package("sneaky", [approved])
    assert "outside its approved root" in str(excinfo.value)


def test_a_skill_body_larger_than_the_byte_limit_is_refused(tmp_path):
    root = tmp_path / "skills"
    (root / "big").mkdir(parents=True)
    (root / "big" / "SKILL.md").write_text("x" * (MAX_SKILL_BYTES + 10))

    with pytest.raises(SkillPackageError) as excinfo:
        load_package("big", [root])
    assert "byte limit" in str(excinfo.value)


def test_with_no_roots_configured_nothing_is_loadable(tmp_path):
    """Empty roots means NOTHING is readable -- deliberately, not 'everything'."""
    with pytest.raises(SkillPackageError):
        load_package("anything", [])
