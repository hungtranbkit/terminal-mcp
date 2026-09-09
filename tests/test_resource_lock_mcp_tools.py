"""P0.6 resource locks -- MCP tool surface, through the real
server.call_tool path a ChatGPT/Claude Code client uses. Driving
ResourceLockStore directly in Python cannot catch a tool wrapper that
forgot a parameter, or one that lets a bad input reach the store as a
crash instead of a structured refusal."""
from __future__ import annotations

import json

import pytest

from terminal_mcp.lease import ResourceLockStore
from terminal_mcp.mcp_app import build_mcp

PROJECT = "git:github.com/acme/widget"


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def rig(tmp_path):
    store = ResourceLockStore(tmp_path / "leases.db")
    return build_mcp(resource_locks=store), store


@pytest.mark.anyio
async def test_lock_renew_unlock_round_trip(rig):
    server, store = rig
    got = await _call(server, "terminal_resource_lock", project_id=PROJECT,
                      resource_key="src/app.py", owner_id="worker-A", reason="refactor")
    assert got["acquired"] is True and got["lock"]["reason"] == "refactor"

    renewed = await _call(server, "terminal_resource_renew", project_id=PROJECT,
                          resource_key="src/app.py", owner_id="worker-A", ttl_seconds=600)
    assert renewed["renewed"] is True

    holder = await _call(server, "terminal_resource_holder", project_id=PROJECT,
                         resource_key="src/app.py")
    assert holder["owner_id"] == "worker-A" and holder["expired"] is False

    released = await _call(server, "terminal_resource_unlock", project_id=PROJECT,
                           resource_key="src/app.py", owner_id="worker-A")
    assert released["released"] is True
    assert (await _call(server, "terminal_resource_holder", project_id=PROJECT,
                        resource_key="src/app.py"))["held"] is False


@pytest.mark.anyio
async def test_a_contended_lock_reports_the_holder_through_the_tool(rig):
    server, _ = rig
    await _call(server, "terminal_resource_lock", project_id=PROJECT,
                resource_key="branch:main", owner_id="worker-A", reason="rebasing")
    denied = await _call(server, "terminal_resource_lock", project_id=PROJECT,
                         resource_key="branch:main", owner_id="worker-B")
    assert denied["acquired"] is False
    assert denied["holder"]["owner_id"] == "worker-A"
    assert denied["holder"]["reason"] == "rebasing"


@pytest.mark.anyio
async def test_lock_many_is_all_or_nothing_through_the_tool(rig):
    server, _ = rig
    await _call(server, "terminal_resource_lock", project_id=PROJECT,
                resource_key="b.py", owner_id="worker-B")
    result = await _call(server, "terminal_resource_lock_many", project_id=PROJECT,
                         resource_keys=["a.py", "b.py"], owner_id="worker-A")
    assert result["acquired"] is False and result["conflict"] == "b.py"
    # a.py was free but must NOT have been taken.
    assert (await _call(server, "terminal_resource_holder", project_id=PROJECT,
                        resource_key="a.py"))["held"] is False

    ok = await _call(server, "terminal_resource_lock_many", project_id=PROJECT,
                     resource_keys=["a.py", "c.py"], owner_id="worker-A")
    assert ok["acquired"] is True and ok["resource_keys"] == ["a.py", "c.py"]


@pytest.mark.anyio
async def test_project_scoping_through_the_tool(rig):
    server, _ = rig
    a = await _call(server, "terminal_resource_lock", project_id=PROJECT,
                    resource_key="src/app.py", owner_id="worker-A")
    b = await _call(server, "terminal_resource_lock", project_id="git:github.com/acme/other",
                    resource_key="src/app.py", owner_id="worker-B")
    assert a["acquired"] is True and b["acquired"] is True


@pytest.mark.anyio
async def test_bad_input_is_a_structured_refusal_not_a_crash(rig):
    """A control character in a key could otherwise collide two projects'
    locks; it must come back as INVALID_REQUEST, never as a tool crash."""
    server, _ = rig
    for kwargs in ({"project_id": PROJECT, "resource_key": "a\x1fb", "owner_id": "w"},
                   {"project_id": "", "resource_key": "a.py", "owner_id": "w"},
                   {"project_id": PROJECT, "resource_key": "a.py", "owner_id": "  "}):
        result = await _call(server, "terminal_resource_lock", **kwargs)
        assert result["error"] == "INVALID_REQUEST", result

    empty = await _call(server, "terminal_resource_lock_many", project_id=PROJECT,
                        resource_keys=[], owner_id="worker-A")
    assert empty["error"] == "INVALID_REQUEST"


@pytest.mark.anyio
async def test_unlock_all_and_listing(rig):
    server, _ = rig
    for key in ("a.py", "b.py"):
        await _call(server, "terminal_resource_lock", project_id=PROJECT,
                    resource_key=key, owner_id="worker-A")
    await _call(server, "terminal_resource_lock", project_id=PROJECT,
                resource_key="c.py", owner_id="worker-B")

    listed = await _call(server, "terminal_resource_locks", project_id=PROJECT)
    assert listed["count"] == 3
    mine = await _call(server, "terminal_resource_locks", owner_id="worker-A")
    assert {r["resource_key"] for r in mine["locks"]} == {"a.py", "b.py"}

    dropped = await _call(server, "terminal_resource_unlock_all", owner_id="worker-A")
    assert dropped["released"] == 2
    assert (await _call(server, "terminal_resource_locks"))["count"] == 1


@pytest.mark.anyio
async def test_force_unlock_is_a_separate_verb_requiring_actor_and_reason(rig):
    server, _ = rig
    await _call(server, "terminal_resource_lock", project_id=PROJECT,
                resource_key="branch:main", owner_id="worker-A", ttl_seconds=3600)

    # The ordinary unlock cannot break someone else's lock.
    assert (await _call(server, "terminal_resource_unlock", project_id=PROJECT,
                        resource_key="branch:main", owner_id="worker-B"))["released"] is False

    refused = await _call(server, "terminal_resource_force_unlock", project_id=PROJECT,
                          resource_key="branch:main", actor="operator", reason="")
    assert refused["error"] == "INVALID_REQUEST"

    broken = await _call(server, "terminal_resource_force_unlock", project_id=PROJECT,
                         resource_key="branch:main", actor="operator",
                         reason="worker-A's node was rebuilt")
    assert broken["released"] is True
    assert broken["previous_holder"]["owner_id"] == "worker-A"


@pytest.mark.anyio
async def test_locks_never_expose_the_internal_composite_key(rig):
    server, _ = rig
    await _call(server, "terminal_resource_lock", project_id=PROJECT,
                resource_key="src/app.py", owner_id="worker-A")
    listed = await _call(server, "terminal_resource_locks")
    assert all("lock_key" not in row for row in listed["locks"])
    assert "lock_key" not in await _call(server, "terminal_resource_holder",
                                         project_id=PROJECT, resource_key="src/app.py")


@pytest.fixture
def anyio_backend():
    return "asyncio"
