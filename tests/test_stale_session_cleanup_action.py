"""The stale-session report grew ONE action, and it re-checks everything.

The report an operator is looking at is seconds old at best. A session that
was idle when the page rendered may be running work by the time they click,
so the click authorizes deleting a session that is STILL a candidate -- never
one that merely was.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from terminal_mcp import session_matcher as sm
from terminal_mcp.stale_sessions import CLEANUP_REFUSED, cleanup_candidates, cleanup_session


def _candidate(session, **overrides):
    base = dict(
        session=session, node_id="local", node_name="Local", node_online=True,
        node_draining=False, node_capacity="healthy", node_agent_types=("claude",),
        is_local_node=True, runtime="claude", state="IDLE", state_probed=False,
        input_allowed=True, stale_identity_pin=False, registry_status="ACTIVE",
        cwd="/gone", worktree_path="/gone", worktree_exists=False, repo=None,
        branch=None, dirty=None, context_percent=None, project=None, project_id=None,
        agent_id=None, skills=(), bindings=(), active_tasks=0, queued_tasks=0,
        claimed_by_task=None,
    )
    base.update(overrides)
    return sm.SessionCandidate(**base)


class _Router:
    def __init__(self, candidates):
        self._candidates = list(candidates)
        self.refreshes = 0

    def candidates(self, *, refresh=False, budget_seconds=None):
        if refresh:
            self.refreshes += 1
        return list(self._candidates)


class _Controller:
    def __init__(self):
        self.deleted: list[str] = []

    def terminal_delete_session(self, name, requested_by=None):
        self.deleted.append(name)
        return {"deleted": name}


def test_a_still_stale_session_is_deleted_with_its_evidence():
    router = _Router([_candidate("abandoned")])
    controller = _Controller()

    result = cleanup_session(router, "abandoned", controller=controller, requested_by="op")

    assert result["deleted"] is True
    assert controller.deleted == ["abandoned"]
    assert any("no longer exists" in line for line in result["evidence"])
    assert router.refreshes == 1, "the fleet view must be re-read, never taken from a cache"


def test_a_session_that_went_busy_since_the_report_is_refused():
    router = _Router([_candidate("abandoned", state="RUNNING")])
    controller = _Controller()

    result = cleanup_session(router, "abandoned", controller=controller)

    assert result["error"] == CLEANUP_REFUSED
    assert controller.deleted == []
    assert result["note"] == "nothing was deleted"


def test_a_session_that_picked_up_a_task_is_refused_and_says_so():
    router = _Router([_candidate("abandoned", active_tasks=1)])
    controller = _Controller()

    result = cleanup_session(router, "abandoned", controller=controller)

    assert result["error"] == CLEANUP_REFUSED
    assert "HAS_TASKS" in result["reason"]
    assert controller.deleted == []


def test_a_session_waiting_for_a_human_is_refused():
    router = _Router([_candidate("abandoned", state="WAITING_INPUT")])
    controller = _Controller()
    assert cleanup_session(router, "abandoned", controller=controller)["error"] == CLEANUP_REFUSED
    assert controller.deleted == []


def test_a_bound_session_holding_a_task_claim_is_refused():
    router = _Router([_candidate("abandoned", claimed_by_task="t1")])
    controller = _Controller()
    assert cleanup_session(router, "abandoned", controller=controller)["error"] == CLEANUP_REFUSED
    assert controller.deleted == []


def test_a_protected_admin_shell_can_never_be_cleaned_up():
    class _Config:
        protected_sessions = ("admin-shell",)

    router = _Router([_candidate("admin-shell")])
    controller = _Controller()

    result = cleanup_session(router, "admin-shell", controller=controller, config=_Config())

    assert result["error"] == CLEANUP_REFUSED
    assert "PROTECTED" in result["reason"]
    assert controller.deleted == []


def test_a_session_whose_worktree_exists_after_all_is_refused():
    router = _Router([_candidate("healthy", worktree_exists=True, registry_status="ACTIVE")])
    controller = _Controller()
    assert cleanup_session(router, "healthy", controller=controller)["error"] == CLEANUP_REFUSED
    assert controller.deleted == []


def test_a_fleet_we_could_not_read_deletes_nothing():
    class _Broken:
        def candidates(self, *, refresh=False, budget_seconds=None):
            raise RuntimeError("fleet down")

    controller = _Controller()
    result = cleanup_session(_Broken(), "abandoned", controller=controller)
    assert result["error"] == "FLEET_READ_FAILED"
    assert controller.deleted == []


def test_the_report_itself_is_still_report_only():
    router = _Router([_candidate("abandoned")])
    report = cleanup_candidates(router)
    assert report["report_only"] is True
    assert [row["session"] for row in report["candidates"]] == ["abandoned"]
