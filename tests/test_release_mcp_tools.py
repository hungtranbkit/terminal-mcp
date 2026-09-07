"""Release lifecycle (docs/REQUIREMENTS.md §20.6 Phase C) -- MCP tool
surface. Exercises the real MCP call path, same pattern as other
Unified Task System checkpoints' own MCP test files."""
from __future__ import annotations

import json

import pytest

from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.release_service import ReleaseService
from terminal_mcp.release_store import ReleaseStore


async def _call(server, name, **kwargs):
    result = await server.call_tool(name, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.fixture
def server(tmp_path):
    release = ReleaseService(ReleaseStore(tmp_path / "release.db"))
    return build_mcp(release=release)


@pytest.mark.anyio
async def test_release_full_dev_lifecycle_through_real_mcp_path(server):
    created = await _call(server, "terminal_release_create", project="proj-a", task_id="t1",
                         environment="dev", artifact_ref="sha123")
    release_id = created["release"]["id"]
    assert created["release"]["status"] == "MERGED"

    await _call(server, "terminal_release_advance", release_id=release_id, to_status="RELEASE_CANDIDATE")
    await _call(server, "terminal_release_advance", release_id=release_id, to_status="DEPLOYING")
    deployed = await _call(server, "terminal_release_advance", release_id=release_id, to_status="DEPLOYED")
    assert deployed["release"]["status"] == "DEPLOYED"
    verified = await _call(server, "terminal_release_advance", release_id=release_id, to_status="VERIFIED_PROD")
    assert verified["release"]["status"] == "VERIFIED_PROD"

    status = await _call(server, "terminal_release_status", release_id=release_id)
    assert len(status["events"]) == 5  # MERGED (create) + 4 real transitions


@pytest.mark.anyio
async def test_release_prod_requires_rollback_plan_at_creation(server):
    result = await _call(server, "terminal_release_create", project="proj-a", task_id="t1",
                        environment="prod", artifact_ref="sha123")
    assert result["error"] == "PROD_RELEASE_REQUIRES_ROLLBACK_PLAN"


@pytest.mark.anyio
async def test_release_prod_deploy_requires_explicit_approval(server):
    created = await _call(server, "terminal_release_create", project="proj-a", task_id="t1",
                         environment="prod", artifact_ref="sha123", known_good_artifact_ref="sha000",
                         rollback_plan="revert to sha000")
    release_id = created["release"]["id"]
    await _call(server, "terminal_release_advance", release_id=release_id, to_status="RELEASE_CANDIDATE")
    unapproved = await _call(server, "terminal_release_advance", release_id=release_id, to_status="DEPLOYING")
    assert unapproved["error"] == "PROD_DEPLOY_REQUIRES_APPROVAL"

    approved = await _call(server, "terminal_release_advance", release_id=release_id, to_status="DEPLOYING",
                          approved_by="hung@example.com")
    assert "error" not in approved
    assert approved["release"]["approved_by"] == "hung@example.com"


@pytest.mark.anyio
async def test_release_rollback_through_real_mcp_path(server):
    created = await _call(server, "terminal_release_create", project="proj-a", task_id="t1",
                         environment="dev", artifact_ref="sha123")
    release_id = created["release"]["id"]
    await _call(server, "terminal_release_advance", release_id=release_id, to_status="RELEASE_CANDIDATE")
    await _call(server, "terminal_release_advance", release_id=release_id, to_status="DEPLOYING")
    result = await _call(server, "terminal_release_rollback", release_id=release_id,
                        reason="deploy script failed", actor="ops-bot")
    assert result["release"]["status"] == "ROLLED_BACK"


@pytest.mark.anyio
async def test_release_list_filters_by_project(server):
    await _call(server, "terminal_release_create", project="proj-a", task_id="t1", environment="dev",
              artifact_ref="sha1")
    await _call(server, "terminal_release_create", project="proj-b", task_id="t2", environment="dev",
              artifact_ref="sha2")
    result = await _call(server, "terminal_release_list", project="proj-a")
    assert len(result["releases"]) == 1
