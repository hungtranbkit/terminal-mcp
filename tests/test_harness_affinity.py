"""Repo affinity: the node that has the repository, asked rather than assumed.

The bug this defends against is concrete and was live: the Harness decided
the UrbanFlow pilot could not run because `/home/dell/workspace/urbanflow`
did not exist on the CONTROLLER -- while the repository sat, present and
valid, on the fleet node `dell-linux`. A fleet whose work runs on four
machines cannot answer "is this startable" by stat()ing one of them.

So the tests are about where the question is sent and how the answer is
read:

  * the controller's own disk is never consulted  -> test_*_never_stats_*
  * a node without the path is rejected BY NAME   -> test_*_rejected_*
  * an unreachable node is not "does not have it" -> test_unreachable_*
  * the choice is stable for the same fleet       -> test_*_deterministic
  * nothing is ever copied anywhere               -> (no code path exists)
"""
from __future__ import annotations

import pytest

from terminal_mcp import harness_affinity as affinity
from terminal_mcp.harness_affinity import (NoEligibleNode, NodeVerdict, RepoAffinity,
                                           verdict_from_evidence, worktree_root_for)

REPO = "/home/dell/workspace/urbanflow"


class FakeNode:
    """A node that answers repo_evidence the way the real agent does."""

    def __init__(self, payloads: dict[str, dict] | None = None, raises=None):
        self.payloads = payloads or {}
        self.raises = raises
        self.asked: list[str] = []

    def repo_evidence(self, cwd: str) -> dict:
        self.asked.append(cwd)
        if self.raises is not None:
            raise self.raises
        if cwd in self.payloads:
            return self.payloads[cwd]
        return {"cwd": cwd, "exists": False, "readable": False, "repo_valid": False,
                "error": "PATH_NOT_FOUND",
                "detail": f"{cwd} does not exist on this node"}


def _has_repo(cwd=REPO, branch="main", head="e7d637c"):
    """The real shape, copied from a live dell-linux answer."""
    return {"cwd": cwd, "exists": True, "readable": True, "repo_valid": True,
            "branch": branch, "head": head, "clean": True,
            "collected_at": "2026-09-20T04:33:34+00:00", "contract_version": 1}


def _fleet(**nodes) -> RepoAffinity:
    return RepoAffinity(nodes, node_ids=sorted(nodes))


# ---------------------------------------------------------------------------
# reading one node's answer
# ---------------------------------------------------------------------------

def test_a_present_valid_readable_repo_is_eligible():
    v = verdict_from_evidence("dell-linux", _has_repo())
    assert v.eligible is True
    assert v.reason == affinity.HAS_REPO
    assert v.branch == "main" and v.head == "e7d637c"


@pytest.mark.parametrize("payload,reason", [
    ({"exists": False, "error": "PATH_NOT_FOUND", "detail": "no such path"},
     affinity.PATH_NOT_FOUND),
    ({"exists": True, "readable": False}, affinity.UNREADABLE),
    ({"exists": True, "readable": True, "repo_valid": False}, affinity.NOT_A_REPO),
    ({"exists": True, "readable": True, "error": "GIT_FAILED"}, affinity.NOT_A_REPO),
    ({}, affinity.NO_EVIDENCE_ENDPOINT),
])
def test_every_refusal_is_named(payload, reason):
    """"why did this not go to HP" must have an answer a person can read."""
    assert verdict_from_evidence("hp-linux", payload).reason == reason


def test_a_non_dict_answer_is_not_treated_as_a_repository():
    assert verdict_from_evidence("x", None).eligible is False
    assert verdict_from_evidence("x", "nope").reason == affinity.NO_EVIDENCE_ENDPOINT


def test_an_older_agent_that_names_a_branch_is_still_eligible():
    """A node whose agent predates `repo_valid` has told us it IS a repo by
    being able to say which branch it is on. Refusing it would reject a node
    for the age of its agent rather than anything about the repository."""
    v = verdict_from_evidence("m910", {"cwd": REPO, "branch": "main", "head": "abc"})
    assert v.eligible is True and v.branch == "main"


# ---------------------------------------------------------------------------
# asking the fleet
# ---------------------------------------------------------------------------

def test_the_node_with_the_repo_is_chosen_and_the_others_are_rejected_by_name():
    """The live case, exactly: dell-linux has UrbanFlow, HP does not."""
    fleet = _fleet(**{"dell-linux": FakeNode({REPO: _has_repo()}),
                      "hp-linux": FakeNode(),
                      "dell-5530": FakeNode()})

    chosen, verdicts = fleet.select(REPO)

    assert chosen is not None and chosen.node_id == "dell-linux"
    by_node = {v.node_id: v for v in verdicts}
    assert by_node["hp-linux"].eligible is False
    assert by_node["hp-linux"].reason == affinity.PATH_NOT_FOUND
    assert "does not exist" in by_node["hp-linux"].detail
    assert by_node["dell-5530"].reason == affinity.PATH_NOT_FOUND


def test_every_node_is_reported_not_just_the_winner():
    """A selector that answers only with its winner cannot be debugged."""
    fleet = _fleet(**{"dell-linux": FakeNode({REPO: _has_repo()}), "hp-linux": FakeNode()})
    _chosen, verdicts = fleet.select(REPO)
    assert {v.node_id for v in verdicts} == {"dell-linux", "hp-linux"}


def test_the_controller_filesystem_is_never_consulted(tmp_path, monkeypatch):
    """The original bug in one assertion: a path that does not exist HERE is
    still eligible if a NODE says it has it."""
    import os

    calls = []
    real_exists = os.path.exists
    monkeypatch.setattr(os.path, "exists",
                        lambda p: (calls.append(p), real_exists(p))[1])
    fleet = _fleet(**{"dell-linux": FakeNode({REPO: _has_repo()})})

    chosen, _ = fleet.select(REPO)

    assert chosen.node_id == "dell-linux"
    assert not any(REPO in str(c) for c in calls), \
        "eligibility was decided by stat()ing the controller's own disk"


def test_no_eligible_node_names_what_each_one_said():
    fleet = _fleet(**{"hp-linux": FakeNode(), "dell-5530": FakeNode()})

    chosen, verdicts = fleet.select(REPO)

    assert chosen is None
    error = NoEligibleNode(REPO, verdicts)
    assert "hp-linux: path_not_found" in str(error)
    assert "dell-5530: path_not_found" in str(error)


# ---------------------------------------------------------------------------
# unreachable is not the same as "does not have it"
# ---------------------------------------------------------------------------

def test_an_unreachable_node_is_unreachable_not_ineligible_forever():
    fleet = _fleet(**{"dell-linux": FakeNode(raises=OSError("connection refused")),
                      "hp-linux": FakeNode()})

    _chosen, verdicts = fleet.select(REPO)

    by_node = {v.node_id: v for v in verdicts}
    assert by_node["dell-linux"].reason == affinity.UNREACHABLE
    assert by_node["dell-linux"].reason != affinity.PATH_NOT_FOUND


def test_an_unreachable_answer_is_never_cached():
    """A fleet must not quietly shrink to whatever was up at the first probe."""
    node = FakeNode(raises=OSError("down"))
    fleet = RepoAffinity({"dell-linux": node}, node_ids=["dell-linux"])
    assert fleet.select(REPO)[0] is None

    node.raises = None
    node.payloads = {REPO: _has_repo()}
    chosen, _ = fleet.select(REPO)

    assert chosen is not None and chosen.node_id == "dell-linux", \
        "the node came back and must be re-asked"


def test_a_node_without_the_endpoint_is_named_as_such():
    class Ancient:
        pass

    fleet = RepoAffinity({"m910": Ancient()}, node_ids=["m910"])
    _chosen, verdicts = fleet.select(REPO)
    assert verdicts[0].reason == affinity.NO_EVIDENCE_ENDPOINT


# ---------------------------------------------------------------------------
# determinism and cost
# ---------------------------------------------------------------------------

def test_the_same_fleet_always_picks_the_same_node():
    """A scheduler whose placement changes between identical calls cannot be
    reasoned about."""
    def build():
        return _fleet(**{"b-node": FakeNode({REPO: _has_repo()}),
                         "a-node": FakeNode({REPO: _has_repo()})})

    assert build().select(REPO)[0].node_id == build().select(REPO)[0].node_id == "a-node"


def test_an_explicit_preference_wins_over_name_order():
    fleet = RepoAffinity({"a-node": FakeNode({REPO: _has_repo()}),
                          "dell-linux": FakeNode({REPO: _has_repo()})},
                         node_ids=["a-node", "dell-linux"], prefer=["dell-linux"])
    assert fleet.select(REPO)[0].node_id == "dell-linux"


def test_a_repeated_question_is_answered_from_cache():
    node = FakeNode({REPO: _has_repo()})
    fleet = RepoAffinity({"dell-linux": node}, node_ids=["dell-linux"])

    for _ in range(5):
        fleet.select(REPO)

    assert len(node.asked) == 1, "a fleet-wide probe per task costs more than it informs"


def test_the_cache_expires_so_a_removed_repo_is_noticed():
    clock = {"t": 0.0}
    node = FakeNode({REPO: _has_repo()})
    fleet = RepoAffinity({"dell-linux": node}, node_ids=["dell-linux"],
                         ttl_seconds=60.0, clock=lambda: clock["t"])
    assert fleet.select(REPO)[0] is not None

    node.payloads = {}          # the repository is gone
    clock["t"] = 61.0
    assert fleet.select(REPO)[0] is None


def test_invalidate_forces_a_fresh_look():
    node = FakeNode({REPO: _has_repo()})
    fleet = RepoAffinity({"dell-linux": node}, node_ids=["dell-linux"])
    fleet.select(REPO)
    fleet.invalidate(REPO)
    fleet.select(REPO)
    assert len(node.asked) == 2


# ---------------------------------------------------------------------------
# where the worktree goes, on the node that has the repo
# ---------------------------------------------------------------------------

def test_the_worktree_root_follows_the_repository_not_the_controller():
    """Computed as a string: the path belongs to another host, and stat()ing
    it locally is the exact mistake this module exists to remove."""
    assert worktree_root_for("/home/dell/workspace/urbanflow") == \
        "/home/dell/workspace/.terminal-mcp-worktrees/harness"
    assert worktree_root_for("/home/dell/workspace/urbanflow/") == \
        "/home/dell/workspace/.terminal-mcp-worktrees/harness"


def test_every_reason_is_on_the_closed_list():
    for name in ("HAS_REPO", "PATH_NOT_FOUND", "NOT_A_REPO", "UNREADABLE",
                 "UNREACHABLE", "NO_EVIDENCE_ENDPOINT"):
        assert getattr(affinity, name) in affinity.VERDICT_REASONS


# ---------------------------------------------------------------------------
# local and remote must answer the same question the same way
# ---------------------------------------------------------------------------

def test_a_missing_path_reads_the_same_locally_and_remotely(tmp_path):
    """Observed live: probing the fleet for /home/dell/workspace/urbanflow
    rejected hp-linux as `not_a_repo`, when the honest answer -- and the one
    every remote node gave -- was that the path is not there. One situation
    must not produce two reasons depending on which client asked."""
    from terminal_mcp.core import TerminalService
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.node_client import LocalNodeClient

    missing = str(tmp_path / "definitely-not-here")
    local = LocalNodeClient(TerminalService(AppConfig(PermissionsConfig(True, False),
                                                     ("test-*",), 50, 20)))
    local_verdict = verdict_from_evidence("hp-linux", local.repo_evidence(missing))

    # The remote agent's own shape for the same situation.
    remote_verdict = verdict_from_evidence("dell-linux", {
        "cwd": missing, "exists": False, "readable": False, "repo_valid": False,
        "error": "PATH_NOT_FOUND", "detail": f"{missing} does not exist on this node"})

    assert local_verdict.reason == remote_verdict.reason == affinity.PATH_NOT_FOUND
    assert local_verdict.eligible is remote_verdict.eligible is False


def test_a_real_directory_that_is_not_a_repo_reads_as_not_a_repo(tmp_path):
    """The distinction the parity fix preserves: present but not a repo is a
    different answer from not present, and only the first may be judged."""
    from terminal_mcp.core import TerminalService
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.node_client import LocalNodeClient

    plain = tmp_path / "just-a-folder"
    plain.mkdir()
    local = LocalNodeClient(TerminalService(AppConfig(PermissionsConfig(True, False),
                                                      ("test-*",), 50, 20)))

    verdict = verdict_from_evidence("hp-linux", local.repo_evidence(str(plain)))

    assert verdict.eligible is False
    assert verdict.reason == affinity.NOT_A_REPO


def test_a_real_repository_is_eligible_through_the_local_client(tmp_path):
    import subprocess

    from terminal_mcp.core import TerminalService
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.node_client import LocalNodeClient

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "f").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=repo, check=True)

    local = LocalNodeClient(TerminalService(AppConfig(PermissionsConfig(True, False),
                                                      ("test-*",), 50, 20)))
    verdict = verdict_from_evidence("hp-linux", local.repo_evidence(str(repo)))

    assert verdict.eligible is True
    assert verdict.reason == affinity.HAS_REPO
    assert verdict.branch == "main"
