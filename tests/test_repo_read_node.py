"""Node-aware repo reads: the /v1/repo/{op} node-agent endpoint and
repo_service.py's routing over it.

The point being proved here is the one `coordinator.node_aware_repo_
evidence` had to prove for metadata, now for CONTENT: a repository that
lives on another machine is read by asking THAT machine, and a controller
that cannot see the files never answers about them from its own disk.

`_NodeClientOverTestClient` subclasses the REAL RemoteNodeClient and only
swaps its HTTP transport for Starlette's in-process TestClient. That is
deliberate: the query-string building, the bearer auth, the JSON
marshalling, the route matching and the 200-vs-non-200 error convention are
all the production code paths, so a break in any of them fails here rather
than only against a real second host.
"""
from __future__ import annotations

import json
import subprocess

import pytest
from starlette.testclient import TestClient

from terminal_mcp import repo_read
from terminal_mcp.config import (
    AppConfig, InputPolicyConfig, PermissionsConfig, RepoReadConfig, SessionLifecycleConfig,
)
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.node_client import NodeClientError, RemoteNodeClient
from terminal_mcp.repo_read import RepoReadPolicy
from terminal_mcp.repo_service import RepoService

TOKEN = "node-token-for-repo-reads"
REMOTE_NODE = "dell-linux"


def _git(cwd, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                   env={"HOME": str(cwd), "PATH": "/usr/bin:/bin",
                        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"})


@pytest.fixture
def remote_repo(tmp_path):
    """Stands in for a repo that exists only on the remote node."""
    root = tmp_path / "node-side" / "service"
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q", "-b", "main", "service")
    (root / "handler.py").write_text("def handle_remote_request():\n    return 'from the node'\n")
    (root / ".env").write_text("SECRET_TOKEN=never-leaves-the-node\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "node-side work")
    return root


def _node_config(root) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, False),
        allowed_session_patterns=("agent-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, allowed_cwd_roots=(str(root),),
                                                 protected_sessions=()),
        # allowed_roots deliberately left empty so this exercises the real
        # fallback to session_lifecycle.allowed_cwd_roots.
        repo_read=RepoReadConfig(enabled=True),
    )


@pytest.fixture
def node(remote_repo, tmp_path):
    """A real node-agent ASGI app whose config allows only the node-side
    tree -- so anything it answers about had to come from its own policy."""
    root = remote_repo.parent
    terminal = TerminalService(_node_config(root),
                               grants=SessionGrantStore(tmp_path / "grants.db"))
    app = build_node_agent(node_id=REMOTE_NODE, terminal=terminal, token=TOKEN,
                           workspace_root=str(root))
    return TestClient(app)


class _NodeClientOverTestClient(RemoteNodeClient):
    """The production RemoteNodeClient with its transport pointed at an
    in-process ASGI app instead of a socket."""

    def __init__(self, test_client: TestClient, token: str = TOKEN) -> None:
        super().__init__("http://node.invalid", token)
        self._test_client = test_client

    def _request(self, method, path, *, params=None, body=None):
        response = self._test_client.request(
            method, path, params=params,
            content=json.dumps(body) if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}"})
        if response.status_code != 200:
            raise NodeClientError(f"{response.status_code} from {path}")
        return response.json()


class _FakeController:
    """Only the three things RepoService actually uses: client_for,
    resolve_session and discover_projects."""

    def __init__(self, clients: dict, *, local_node_id: str = "local",
                 sessions: dict | None = None, projects: list | None = None) -> None:
        self._clients = clients
        self.local_node_id = local_node_id
        self._sessions = sessions or {}
        self._projects = projects or []

    def client_for(self, node_id):
        return self._clients.get(node_id)

    def resolve_session(self, session):
        if session not in self._sessions:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        return {"node_id": self._sessions[session], "session": session}

    def discover_projects(self):
        return {"projects": self._projects, "node_errors": {}}


@pytest.fixture
def service(node, tmp_path):
    """A controller-backed RepoService whose LOCAL policy deliberately
    allows only a local-only directory -- so any successful read of the
    node-side repo proves the node answered, and could not have been
    served from this side."""
    local_only = tmp_path / "controller-side"
    local_only.mkdir()
    policy = RepoReadPolicy(allowed_roots=(str(local_only),))
    controller = _FakeController({REMOTE_NODE: _NodeClientOverTestClient(node)})
    return RepoService(policy, controller, local_node_id="local"), local_only


# -- the node endpoint itself --------------------------------------------

def test_endpoint_requires_auth(node, remote_repo):
    response = node.get("/v1/repo/status", params={"path": str(remote_repo)})
    assert response.status_code == 401


def test_endpoint_reads_status_on_the_node(node, remote_repo):
    response = node.get("/v1/repo/status", params={"path": str(remote_repo)},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["branch"] == "main"
    assert body["node_id"] == REMOTE_NODE
    assert body["repo_root"] == str(remote_repo)


def test_endpoint_enforces_the_nodes_own_allowlist(node, tmp_path):
    """The node's config -- not the caller's -- decides which of its paths
    may be read. That is the correct owner of the decision."""
    outside = tmp_path / "controller-side"
    outside.mkdir()
    response = node.get("/v1/repo/status", params={"path": str(outside)},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.json()["error"] == repo_read.REPO_NOT_ALLOWED


def test_endpoint_denies_a_credential_file(node, remote_repo):
    response = node.get("/v1/repo/read",
                        params={"path": str(remote_repo), "file": ".env"},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    body = response.json()
    assert body["error"] == repo_read.SECRET_PATH_DENIED
    assert "never-leaves-the-node" not in response.text


def test_endpoint_rejects_an_unknown_operation_as_a_200_error(node, remote_repo):
    """A 4xx here would be read as "the node is unreachable" by every layer
    above (see node_client._request) -- an argument mistake must not look
    like an outage."""
    response = node.get("/v1/repo/exec", params={"path": str(remote_repo)},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.json()["error"] == repo_read.INVALID_ARGUMENT


def test_endpoint_is_get_only_so_it_cannot_be_a_write(node, remote_repo):
    response = node.post("/v1/repo/status", params={"path": str(remote_repo)},
                         headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 405


# -- routing: a repo that only exists on the node ------------------------

def test_status_of_a_remote_repo_is_answered_by_that_node(service, remote_repo):
    repo, _ = service
    result = repo.status(node=REMOTE_NODE, path=str(remote_repo))
    assert "error" not in result
    assert result["branch"] == "main"
    assert result["node_id"] == REMOTE_NODE
    assert result["located_by"] == "node+path"


def test_read_of_a_remote_file_returns_content_from_the_node(service, remote_repo):
    repo, _ = service
    result = repo.read(node=REMOTE_NODE, path=str(remote_repo), file="handler.py")
    assert "def handle_remote_request" in result["content"]
    assert result["node_id"] == REMOTE_NODE


def test_search_of_a_remote_repo_runs_on_the_node(service, remote_repo):
    repo, _ = service
    result = repo.search(node=REMOTE_NODE, path=str(remote_repo),
                         query="handle_remote_request")
    assert [(hit["path"], hit["line"]) for hit in result["results"]] == [("handler.py", 1)]
    assert result["node_id"] == REMOTE_NODE


def test_the_same_path_read_locally_is_refused(service, remote_repo):
    """The other half of the proof: without routing, this read is exactly
    what the controller would have attempted -- and its own policy refuses
    it. So the success above cannot have come from local execution."""
    repo, _ = service
    assert repo.status(path=str(remote_repo))["error"] == repo_read.REPO_NOT_ALLOWED


def test_remote_secret_denial_survives_the_transport(service, remote_repo):
    repo, _ = service
    result = repo.read(node=REMOTE_NODE, path=str(remote_repo), file=".env")
    assert result["error"] == repo_read.SECRET_PATH_DENIED
    assert "never-leaves-the-node" not in str(result)


def test_remote_traversal_is_denied_by_the_node(service, remote_repo):
    repo, _ = service
    result = repo.read(node=REMOTE_NODE, path=str(remote_repo), file="../../etc/passwd")
    assert result["error"] == repo_read.PATH_OUTSIDE_REPO


# -- locating ------------------------------------------------------------

def test_a_session_is_read_on_the_node_it_actually_runs_on(node, remote_repo, tmp_path):
    class _WithRegistry(_NodeClientOverTestClient):
        def registry_list(self, *, recoverable_only: bool = False):
            return {"records": [{"session_name": "worker-1", "repo_root": str(remote_repo),
                                 "cwd": str(remote_repo)}]}

    local_only = tmp_path / "controller-side"
    local_only.mkdir()
    controller = _FakeController({REMOTE_NODE: _WithRegistry(node)},
                                 sessions={"worker-1": REMOTE_NODE})
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(local_only),)), controller)
    result = repo.status(session="worker-1")
    assert result["node_id"] == REMOTE_NODE
    assert result["located_by"] == "session"
    assert result["branch"] == "main"


def test_a_session_not_in_a_repo_is_reported_as_such(node, tmp_path):
    class _NoRepo(_NodeClientOverTestClient):
        def registry_list(self, *, recoverable_only: bool = False):
            return {"records": [{"session_name": "worker-1", "repo_root": None, "cwd": None}]}

    controller = _FakeController({REMOTE_NODE: _NoRepo(node)},
                                 sessions={"worker-1": REMOTE_NODE})
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), controller)
    assert repo.status(session="worker-1")["error"] == "SESSION_NOT_IN_A_REPO"


def test_a_project_with_checkouts_on_two_nodes_is_ambiguous_never_guessed(node, tmp_path):
    """Seven checkouts of one repo across three nodes is the real state of
    this fleet. Picking one would silently answer about the wrong working
    tree."""
    controller = _FakeController(
        {REMOTE_NODE: _NodeClientOverTestClient(node)},
        projects=[{"project_id": "git:github.com/o/r", "name": "r",
                   "nodes": [REMOTE_NODE, "hp"], "checkouts": ["/a/r", "/b/r"]}])
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), controller)
    result = repo.status(project="git:github.com/o/r")
    assert result["error"] == "AMBIGUOUS_REPO"
    assert len(result["candidates"]) > 1


def test_an_unknown_project_lists_what_is_known(node, tmp_path):
    controller = _FakeController({}, projects=[{"project_id": "git:github.com/o/r", "name": "r",
                                                "nodes": [], "checkouts": []}])
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), controller)
    result = repo.status(project="git:github.com/nope/nope")
    assert result["error"] == "REPO_NOT_LOCATED"
    assert "git:github.com/o/r" in result["known_projects"]


def test_a_call_with_no_locator_at_all_is_refused(tmp_path):
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)))
    assert repo.status()["error"] == repo_read.INVALID_ARGUMENT


# -- "could not look" is never "the repo said no" ------------------------

def test_an_unreachable_node_is_reported_as_unreachable(tmp_path):
    class _Dead(RemoteNodeClient):
        def repo_op(self, op, path, params):
            raise NodeClientError("connection refused")

    controller = _FakeController({REMOTE_NODE: _Dead("http://dead.invalid", "t")})
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), controller)
    result = repo.status(node=REMOTE_NODE, path="/anything")
    assert result["error"] == repo_read.NODE_UNREACHABLE
    assert "connection refused" in result["detail"]


def test_a_node_agent_predating_the_endpoint_is_named_as_such(tmp_path):
    """A 404 must not be reported as a missing file: the difference between
    "nobody looked" and "it is not there" is the whole reason this
    distinction is coded explicitly."""
    class _Old:
        pass  # no repo_op attribute at all

    controller = _FakeController({REMOTE_NODE: _Old()})
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), controller)
    result = repo.status(node=REMOTE_NODE, path="/anything")
    assert result["error"] == "NODE_LACKS_REPO_READ"


def test_an_unregistered_node_is_not_silently_run_locally(tmp_path):
    controller = _FakeController({})
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), controller)
    assert repo.status(node="nonexistent", path="/anything")["error"] == "NODE_NOT_FOUND"


# -- local reads do not depend on remote auth ---------------------------

def test_a_local_repo_stays_readable_when_the_remote_is_unreachable(tmp_path):
    """Requirement 6: a repository already on disk must be fully readable
    with no GitHub auth and no reachable remote. Nothing in the read path
    touches the network -- this test pins that by pointing the repo at a
    remote that cannot resolve and then reading it anyway."""
    root = tmp_path / "local-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "main.py").write_text("def local_only():\n    return 1\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "local work")
    _git(root, "remote", "add", "origin",
         "https://git.invalid.example/private/repo.git")

    policy = RepoReadPolicy(allowed_roots=(str(tmp_path),), timeout_seconds=10.0)
    repo = RepoService(policy)
    assert repo.status(path=str(root))["branch"] == "main"
    assert "def local_only" in repo.read(path=str(root), file="main.py")["content"]
    assert repo.search(path=str(root), query="local_only")["count"] == 1
    assert repo.log(path=str(root))["commits"][0]["subject"] == "local work"
    assert "main.py" in repo.show_commit(path=str(root), commit="HEAD")["patch"]

    # And the remote is visible WITHOUT any network call, because
    # check_auth defaults to false.
    remotes = repo.remotes(path=str(root))
    assert remotes["remotes"][0]["normalised"] == "git.invalid.example/private/repo"
    assert remotes["auth"]["checked"] is False


def test_a_failed_auth_probe_does_not_fail_the_remotes_read(tmp_path):
    """GIT_AUTH_REQUIRED belongs INSIDE `auth`: the remotes themselves were
    read fine, and a caller that only wanted to know the URL must not be
    handed a top-level error because the network was down."""
    root = tmp_path / "no-auth-repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "f.txt").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "c")
    _git(root, "remote", "add", "origin", "https://git.invalid.example/private/repo.git")

    policy = RepoReadPolicy(allowed_roots=(str(tmp_path),), timeout_seconds=15.0)
    result = repo_read.repo_remotes(str(root), policy, check_auth=True)
    assert "error" not in result
    assert result["remotes"][0]["name"] == "origin"
    assert result["auth"]["checked"] is True
    assert result["auth"]["ok"] is False
    assert result["auth"]["error"] == repo_read.GIT_AUTH_REQUIRED


# -- audit ---------------------------------------------------------------

def test_an_invocation_is_audited_without_recording_any_content(tmp_path):
    """The audit row says WHAT was asked and WHETHER it was allowed. It must
    never carry file content, a patch or a matched line -- this log is the
    one an operator greps freely, so a read-audit holding the secret would
    defeat the denial it is recording."""
    root = tmp_path / "audited"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "f.txt").write_text("password = super-secret-value\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "c")

    from terminal_mcp.audit import AuditStore

    audit = AuditStore(tmp_path / "audit.db")
    repo = RepoService(RepoReadPolicy(allowed_roots=(str(tmp_path),)), audit=audit)
    assert "error" not in repo.read(path=str(root), file="f.txt")
    repo.read(path=str(root), file=".env")  # a denial is audited too

    rows = audit.list(limit=10)
    actions = {row["action"]: row for row in rows}
    assert actions["repo_read"]["result"] in ("OK", "DENIED")
    assert {row["result"] for row in rows} == {"OK", "DENIED"}
    assert "super-secret-value" not in json.dumps(rows)
    assert all(row.get("text_preview") in (None, "") for row in rows)


# -- /v1/repo-evidence regression ----------------------------------------

def test_repo_evidence_endpoint_actually_answers(node, remote_repo):
    """Regression, found 2026-09-14 while adding /v1/repo/{op}.

    This endpoint (the metadata-only sibling, added for the Coordinator's
    pre-dispatch gate) referred to an undefined `config` name, so every
    single request to it raised NameError and answered 500 -- meaning the
    controller had NEVER once successfully collected repo evidence from a
    remote node. It failed closed (node_aware_repo_evidence reports a
    non-200 as RepoEvidenceUnavailable, which the gate treats as "could
    not look"), so nothing was ever wrong-but-believed -- it was just
    silently never working. Nothing exercised the route end-to-end, which
    is why a NameError survived; this test is that exercise.
    """
    response = node.get("/v1/repo-evidence", params={"cwd": str(remote_repo)},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    body = response.json()
    assert body["branch"] == "main"
    assert len(body["head"]) == 40
    # The fixture commits everything, .env included -- which also makes the
    # secret-denial tests above stronger: that file is genuinely tracked in
    # this repository and is refused anyway.
    assert body["clean"] is True
    assert body["has_upstream"] is False


def test_repo_evidence_endpoint_enforces_the_cwd_allowlist(node, tmp_path):
    outside = tmp_path / "controller-side"
    outside.mkdir()
    response = node.get("/v1/repo-evidence", params={"cwd": str(outside)},
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 403
    assert response.json()["error"] == "PATH_NOT_ALLOWED"


def test_repo_evidence_reaches_the_controller_gate_through_the_node_client(node, remote_repo):
    """The whole chain the Coordinator gate actually uses: collector ->
    node client -> HTTP -> node -> git. With the NameError in place this
    returned RepoEvidenceUnavailable ("this node cannot say") for a
    perfectly healthy repository."""
    from terminal_mcp.coordinator import node_aware_repo_evidence

    client = _NodeClientOverTestClient(node)
    collect = node_aware_repo_evidence(local_node_id="local",
                                       node_client_factory=lambda node_id: client)
    evidence = collect(str(remote_repo), REMOTE_NODE)
    assert evidence.branch == "main"
    assert evidence.clean is True
    assert len(evidence.head) == 40
