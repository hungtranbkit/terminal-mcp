"""Rollout contract for /v1/repo-evidence.

The gate refuses to dispatch on evidence it cannot stand behind. What counts
as "cannot stand behind" is the whole subject here: a node that predates the
endpoint, a payload that never says the repo was valid, a timestamp that is
missing, unparseable or old. Every one of those must read as UNAVAILABLE --
"we could not look" -- and never as a verification.

The distinction matters because UNAVAILABLE is waivable by an operator
(allow_unverified_repo) while a genuine repo failure is not. Getting it
backwards either blocks healthy work forever or waves through a repo nobody
checked.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from terminal_mcp.contract import CAP_REPO_EVIDENCE, describe
from terminal_mcp.coordinator import (
    DEFAULT_EVIDENCE_MAX_AGE_SECONDS, RepoEvidenceError, RepoEvidenceUnavailable,
    node_aware_repo_evidence,
)


def _now(offset_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")


def _payload(**overrides):
    """A well-formed answer from a current agent."""
    payload = {
        "cwd": "/remote/repo", "repo_valid": True, "exists": True, "readable": True,
        "collected_at": _now(), "branch": "main", "head": "abc1234",
        "dirty": False, "clean": True, "status_lines": [],
        "has_upstream": True, "ahead": 0, "behind": 0,
        **describe(),
    }
    payload.update(overrides)
    return payload


class _Node:
    def __init__(self, payload):
        self.payload = payload

    def repo_evidence(self, cwd):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _collect(payload, **kwargs):
    collector = node_aware_repo_evidence(local_node_id="local",
                                         node_client_factory=lambda n: _Node(payload),
                                         **kwargs)
    return collector("/remote/repo", "dell-linux")


# -- the capability itself ----------------------------------------------------

def test_the_capability_is_declared_additively():
    described = describe()
    assert CAP_REPO_EVIDENCE in described["contract_capabilities"]
    # Additive: a new capability must not bump the contract version, or every
    # older peer is refused for a change that did not break them.
    assert described["contract_version"] == 1


# -- the happy path -----------------------------------------------------------

def test_a_current_agent_is_accepted():
    evidence = _collect(_payload())
    assert evidence.branch == "main"
    assert evidence.head == "abc1234"
    assert evidence.clean is True


def test_a_dirty_remote_repo_is_reported_dirty():
    evidence = _collect(_payload(clean=False, dirty=True, status_lines=[" M app.py"]))
    assert evidence.clean is False
    assert evidence.status_lines == (" M app.py",)


def test_ahead_and_behind_survive_the_wire():
    evidence = _collect(_payload(ahead=2, behind=3))
    assert (evidence.ahead, evidence.behind) == (2, 3)
    assert evidence.diverged is True


# -- old agent ----------------------------------------------------------------

def test_an_agent_not_declaring_the_capability_is_old_not_verified():
    old = _payload(contract_capabilities=["grant_only_access"], contract_version=1)
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        _collect(old)
    message = str(excinfo.value)
    assert "OLD AGENT" in message
    assert CAP_REPO_EVIDENCE in message


def test_an_agent_with_no_endpoint_at_all_is_unavailable():
    class _NoEndpoint:
        pass

    collector = node_aware_repo_evidence(local_node_id="local",
                                         node_client_factory=lambda n: _NoEndpoint())
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        collector("/remote/repo", "dell-linux")
    assert "predates" in str(excinfo.value)


def test_a_404_is_unavailable_not_a_repo_failure():
    with pytest.raises(RepoEvidenceUnavailable):
        _collect(RuntimeError("GET /v1/repo-evidence -> HTTP 404: Not Found"))


def test_a_payload_without_capabilities_is_still_checked_on_repo_valid():
    # A very old agent reports no contract fields at all. It must not slip
    # through merely because there is no capability list to object to.
    with pytest.raises(RepoEvidenceUnavailable):
        _collect({"cwd": "/remote/repo", "branch": "main", "head": "abc",
                  "clean": True, "status_lines": []})


# -- malformed ----------------------------------------------------------------

def test_a_non_dict_answer_is_unavailable():
    with pytest.raises(RepoEvidenceUnavailable):
        _collect("not json")


def test_repo_valid_must_be_explicitly_true():
    for value in (False, None, "true", 1):
        with pytest.raises(RepoEvidenceUnavailable) as excinfo:
            _collect(_payload(repo_valid=value))
        assert "did not confirm repo_valid" in str(excinfo.value)


def test_a_node_reporting_a_broken_repo_is_a_repo_failure():
    # The node ANSWERED about its own filesystem. That is evidence, and it
    # must NOT be waivable by allow_unverified_repo.
    with pytest.raises(RepoEvidenceError) as excinfo:
        _collect(_payload(repo_valid=False, error="REPO_EVIDENCE_FAILED",
                          detail="not a git repository"))
    assert not isinstance(excinfo.value, RepoEvidenceUnavailable)
    assert "not a git repository" in str(excinfo.value)


@pytest.mark.parametrize("error,detail", [
    ("PATH_NOT_FOUND", "does not exist on this node"),
    ("PATH_NOT_READABLE", "cannot read it"),
])
def test_path_problems_are_distinguishable(error, detail):
    with pytest.raises(RepoEvidenceError) as excinfo:
        _collect(_payload(repo_valid=False, error=error, detail=detail))
    assert detail in str(excinfo.value)


# -- staleness ----------------------------------------------------------------

def test_undated_evidence_is_refused():
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        _collect(_payload(collected_at=None))
    assert "undated" in str(excinfo.value)


def test_unparseable_timestamps_are_refused():
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        _collect(_payload(collected_at="last tuesday"))
    assert "unusably" in str(excinfo.value)


def test_old_evidence_is_refused():
    stale = _now(-(DEFAULT_EVIDENCE_MAX_AGE_SECONDS + 120))
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        _collect(_payload(collected_at=stale))
    assert "old (limit" in str(excinfo.value)


def test_evidence_from_the_future_is_refused_as_clock_skew():
    ahead = _now(DEFAULT_EVIDENCE_MAX_AGE_SECONDS + 120)
    with pytest.raises(RepoEvidenceUnavailable) as excinfo:
        _collect(_payload(collected_at=ahead))
    assert "future" in str(excinfo.value)


def test_recent_evidence_inside_the_window_is_accepted():
    assert _collect(_payload(collected_at=_now(-30))).branch == "main"


def test_the_window_is_configurable():
    slightly_old = _payload(collected_at=_now(-45))
    assert _collect(slightly_old, max_age_seconds=600).branch == "main"
    with pytest.raises(RepoEvidenceUnavailable):
        _collect(slightly_old, max_age_seconds=10)


# -- the node agent's own response contract -----------------------------------

@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.email", "t@e"), ("user.name", "T")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    (root / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"], check=True)
    return root


def test_the_agent_payload_carries_every_required_field(repo):
    """Contract check against the real handler, not a fixture of it."""
    from terminal_mcp.coordinator import git_repo_evidence

    evidence = git_repo_evidence(str(repo))
    # What the endpoint composes from this, per the rollout contract.
    payload = {
        "cwd": str(repo), "repo_valid": True, "exists": True, "readable": True,
        "collected_at": _now(), "branch": evidence.branch, "head": evidence.head,
        "dirty": not evidence.clean, "clean": evidence.clean,
        "status_lines": list(evidence.status_lines),
        "has_upstream": evidence.has_upstream,
        "ahead": evidence.ahead, "behind": evidence.behind, **describe(),
    }
    for field in ("repo_valid", "cwd", "readable", "exists", "branch", "head",
                  "dirty", "collected_at", "contract_version",
                  "contract_capabilities"):
        assert field in payload, f"rollout contract is missing {field}"
    assert payload["head"] and len(payload["head"]) >= 7
    # And the collector accepts what the endpoint produces.
    assert _collect(payload).head == evidence.head


# -- end to end against a REAL disposable node agent --------------------------
#
# Not a mock. A real Starlette node-agent app over a real git repo that exists
# only on the "node" side, driven through the real RemoteNodeClient. A mocked
# node would only prove the mock agrees with itself.

TOKEN = "rollout-node-token"
REMOTE_NODE = "dell-linux"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                   env={"HOME": str(cwd), "PATH": "/usr/bin:/bin",
                        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def node_side(tmp_path):
    """A repo that exists only on the node, plus the agent serving it."""
    from starlette.testclient import TestClient

    from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                     SessionLifecycleConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.node_agent import build_node_agent

    root = tmp_path / "node-side" / "service"
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", "service")
    (root / "handler.py").write_text("def handle():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "node work")

    config = AppConfig(
        permissions=PermissionsConfig(True, False),
        allowed_session_patterns=("agent-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(
            enabled=False, allowed_cwd_roots=(str(root.parent),), protected_sessions=()))
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    app = build_node_agent(node_id=REMOTE_NODE, terminal=terminal, token=TOKEN,
                           workspace_root=str(root.parent))
    return {"repo": root, "client": TestClient(app)}


def _ask(node_side, cwd):
    response = node_side["client"].get(
        "/v1/repo-evidence", params={"cwd": str(cwd)},
        headers={"Authorization": f"Bearer {TOKEN}"})
    return response.status_code, response.json()


def test_a_real_agent_answers_the_full_contract(node_side):
    status, payload = _ask(node_side, node_side["repo"])
    assert status == 200
    assert payload["repo_valid"] is True
    assert payload["exists"] is True and payload["readable"] is True
    assert payload["branch"] == "main"
    assert len(payload["head"]) >= 7
    assert payload["dirty"] is False and payload["clean"] is True
    assert payload["collected_at"]
    assert CAP_REPO_EVIDENCE in payload["contract_capabilities"]


def test_a_real_agent_reports_dirty_after_a_real_edit(node_side):
    (node_side["repo"] / "handler.py").write_text("def handle():\n    return 2\n")
    _, payload = _ask(node_side, node_side["repo"])
    assert payload["dirty"] is True and payload["clean"] is False
    assert payload["status_lines"]


def test_a_missing_path_is_reported_as_such_not_as_a_broken_repo(node_side):
    _, payload = _ask(node_side, node_side["repo"].parent / "not-here")
    assert payload["repo_valid"] is False
    assert payload["exists"] is False
    assert payload["error"] == "PATH_NOT_FOUND"


def test_a_path_outside_the_allowed_roots_is_refused(node_side):
    status, payload = _ask(node_side, "/etc")
    assert status == 403
    assert payload["error"] == "PATH_NOT_ALLOWED"


def test_the_endpoint_requires_auth(node_side):
    response = node_side["client"].get("/v1/repo-evidence",
                                       params={"cwd": str(node_side["repo"])})
    assert response.status_code in (401, 403)


def test_the_real_payload_satisfies_the_controller_collector(node_side):
    """The end the rollout actually cares about: agent -> collector -> gate."""
    _, payload = _ask(node_side, node_side["repo"])
    evidence = _collect(payload)
    assert evidence.branch == "main"
    assert evidence.clean is True
    assert evidence.head == payload["head"]
