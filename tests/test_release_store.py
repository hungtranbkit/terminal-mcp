"""Release lifecycle -- release_store.py (docs/REQUIREMENTS.md §20.6
Phase C). Pure store-level tests, same posture as test_queue_store.py."""
from __future__ import annotations

import pytest

from terminal_mcp.release_store import (
    DEPLOYED, DEPLOYING, MERGED, RELEASE_CANDIDATE, ROLLED_BACK, VERIFIED_PROD, InvalidReleaseTransitionError,
    ReleaseStore, is_valid_release_transition,
)


@pytest.fixture
def store(tmp_path):
    return ReleaseStore(tmp_path / "release.db")


def test_create_release_starts_at_merged(store):
    release = store.create_release(project="proj-a", task_id="t1", environment="dev", artifact_ref="sha123")
    assert release.status == MERGED
    assert release.project == "proj-a"
    assert release.environment == "dev"


def test_get_release_returns_none_for_unknown(store):
    assert store.get_release("no-such-id") is None


def test_list_releases_filters_by_project(store):
    store.create_release(project="proj-a", task_id="t1", environment="dev", artifact_ref="sha1")
    store.create_release(project="proj-b", task_id="t2", environment="dev", artifact_ref="sha2")
    assert len(store.list_releases(project="proj-a")) == 1
    assert len(store.list_releases()) == 2


@pytest.mark.parametrize("from_status,to_status", [
    (MERGED, RELEASE_CANDIDATE), (RELEASE_CANDIDATE, DEPLOYING),
    (DEPLOYING, DEPLOYED), (DEPLOYING, ROLLED_BACK),
    (DEPLOYED, VERIFIED_PROD), (DEPLOYED, ROLLED_BACK),
    (VERIFIED_PROD, ROLLED_BACK),
])
def test_valid_transitions_accepted(from_status, to_status):
    assert is_valid_release_transition(from_status, to_status) is True


@pytest.mark.parametrize("from_status,to_status", [
    (MERGED, DEPLOYING), (MERGED, DEPLOYED), (RELEASE_CANDIDATE, DEPLOYED),
    (DEPLOYED, DEPLOYING), (VERIFIED_PROD, DEPLOYED), (ROLLED_BACK, MERGED),
])
def test_invalid_transitions_rejected(from_status, to_status):
    assert is_valid_release_transition(from_status, to_status) is False


def test_rolled_back_is_terminal():
    for status in (MERGED, RELEASE_CANDIDATE, DEPLOYING, DEPLOYED, VERIFIED_PROD):
        assert is_valid_release_transition(ROLLED_BACK, status) is False


def test_verified_prod_can_still_roll_back_later(store):
    # A real production incident discovered AFTER verification -- not
    # a terminal dead end.
    release = store.create_release(project="p", task_id="t1", environment="prod", artifact_ref="sha1",
                                    known_good_artifact_ref="sha0", rollback_plan="revert to sha0")
    store.transition_release(release.id, RELEASE_CANDIDATE)
    store.transition_release(release.id, DEPLOYING)
    store.transition_release(release.id, DEPLOYED)
    store.transition_release(release.id, VERIFIED_PROD)
    rolled_back = store.transition_release(release.id, ROLLED_BACK, reason="found a real prod issue")
    assert rolled_back.status == ROLLED_BACK
    assert rolled_back.rollback_reason == "found a real prod issue"


def test_transition_release_raises_on_invalid_transition(store):
    release = store.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    with pytest.raises(InvalidReleaseTransitionError):
        store.transition_release(release.id, DEPLOYING)  # skips RELEASE_CANDIDATE


def test_transition_release_stamps_deployed_at_and_verified_at(store):
    release = store.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    store.transition_release(release.id, RELEASE_CANDIDATE)
    store.transition_release(release.id, DEPLOYING)
    deployed = store.transition_release(release.id, DEPLOYED)
    assert deployed.deployed_at is not None
    verified = store.transition_release(release.id, VERIFIED_PROD)
    assert verified.verified_at is not None


def test_list_events_records_full_real_history(store):
    release = store.create_release(project="p", task_id="t1", environment="dev", artifact_ref="sha1")
    store.transition_release(release.id, RELEASE_CANDIDATE)
    store.transition_release(release.id, DEPLOYING)
    events = store.list_events(release.id)
    to_statuses = [e["to_status"] for e in events]
    assert to_statuses == [MERGED, RELEASE_CANDIDATE, DEPLOYING]
