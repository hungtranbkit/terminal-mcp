"""Release lifecycle -- policy layer (release_service.py, docs/
REQUIREMENTS.md §20.6 Phase C). Direct ReleaseStore tests (no MCP
layer)."""
from __future__ import annotations

import pytest

from terminal_mcp.release_service import ReleaseService
from terminal_mcp.release_store import ReleaseStore


@pytest.fixture
def releases(tmp_path):
    return ReleaseService(ReleaseStore(tmp_path / "release.db"))


def test_create_release_dev_needs_no_rollback_plan(releases):
    result = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    assert "error" not in result
    assert result["release"]["environment"] == "dev"


def test_create_release_prod_requires_rollback_plan_and_known_good(releases):
    result = releases.create_release(project="p", task_id="t1", environment="prod", artifact_ref="sha1")
    assert result["error"] == "PROD_RELEASE_REQUIRES_ROLLBACK_PLAN"
    assert set(result["missing_fields"]) == {"known_good_artifact_ref", "rollback_plan"}


def test_create_release_prod_with_both_fields_succeeds(releases):
    result = releases.create_release(project="p", task_id="t1", environment="prod", artifact_ref="sha1",
                                     known_good_artifact_ref="sha0", rollback_plan="revert to sha0")
    assert "error" not in result


def test_create_release_invalid_environment(releases):
    result = releases.create_release(project="p", task_id="t1", environment="qa-nonstandard",
                                     artifact_ref="sha1")
    assert result["error"] == "INVALID_ENVIRONMENT"


def test_create_release_requires_artifact_ref(releases):
    result = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="")
    assert result["error"] == "ARTIFACT_REF_REQUIRED"


def test_advance_release_dev_never_needs_approval(releases):
    created = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    release_id = created["release"]["id"]
    releases.advance_release(release_id, "RELEASE_CANDIDATE")
    result = releases.advance_release(release_id, "DEPLOYING")
    assert "error" not in result


def test_advance_release_prod_deploying_requires_approval(releases):
    created = releases.create_release(project="p", task_id="t1", environment="prod", artifact_ref="sha1",
                                      known_good_artifact_ref="sha0", rollback_plan="revert")
    release_id = created["release"]["id"]
    releases.advance_release(release_id, "RELEASE_CANDIDATE")
    result = releases.advance_release(release_id, "DEPLOYING")  # no approved_by
    assert result["error"] == "PROD_DEPLOY_REQUIRES_APPROVAL"


def test_advance_release_prod_deploying_with_approval_succeeds(releases):
    created = releases.create_release(project="p", task_id="t1", environment="prod", artifact_ref="sha1",
                                      known_good_artifact_ref="sha0", rollback_plan="revert")
    release_id = created["release"]["id"]
    releases.advance_release(release_id, "RELEASE_CANDIDATE")
    result = releases.advance_release(release_id, "DEPLOYING", approved_by="hung@example.com")
    assert "error" not in result
    assert result["release"]["approved_by"] == "hung@example.com"
    assert result["release"]["approved_at"] is not None


def test_advance_release_invalid_transition_reported(releases):
    created = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    result = releases.advance_release(created["release"]["id"], "DEPLOYED")  # skips 2 states
    assert result["error"] == "INVALID_RELEASE_TRANSITION"


def test_advance_release_unknown_id(releases):
    result = releases.advance_release("no-such-id", "RELEASE_CANDIDATE")
    assert result["error"] == "RELEASE_NOT_FOUND"


def test_rollback_release_requires_a_reason(releases):
    created = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    result = releases.rollback_release(created["release"]["id"], reason="")
    assert result["error"] == "ROLLBACK_REASON_REQUIRED"


def test_rollback_release_real_flow(releases):
    created = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    release_id = created["release"]["id"]
    releases.advance_release(release_id, "RELEASE_CANDIDATE")
    releases.advance_release(release_id, "DEPLOYING")
    result = releases.rollback_release(release_id, reason="deploy script failed", actor="ops-bot")
    assert result["release"]["status"] == "ROLLED_BACK"
    assert result["release"]["rollback_reason"] == "deploy script failed"


def test_status_returns_release_and_full_event_history(releases):
    created = releases.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    release_id = created["release"]["id"]
    releases.advance_release(release_id, "RELEASE_CANDIDATE")
    status = releases.status(release_id)
    assert status["release"]["status"] == "RELEASE_CANDIDATE"
    assert len(status["events"]) == 2


def test_status_unknown_release(releases):
    assert releases.status("no-such-id") == {"error": "RELEASE_NOT_FOUND", "release_id": "no-such-id"}


def test_list_releases_real_filtering(releases):
    releases.create_release(project="proj-a", task_id="t1", environment="dev", artifact_ref="sha1")
    releases.create_release(project="proj-b", task_id="t2", environment="dev", artifact_ref="sha2")
    assert len(releases.list_releases(project="proj-a")["releases"]) == 1
    assert len(releases.list_releases()["releases"]) == 2
