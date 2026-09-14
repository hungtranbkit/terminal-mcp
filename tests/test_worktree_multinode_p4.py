"""Worktree Janitor P4 -- multi-node execution at the owning node-agent.

The thing being proved is that a deletion happens on the machine that owns the
directory and nowhere else. So the node side is a REAL Starlette ASGI app
(`build_node_agent`) exercising the real route handlers, real bearer auth and
real JSON marshalling, and the client side is the REAL `RemoteNodeClient` with
only its HTTP transport swapped for the in-process TestClient -- the same shape
tests/test_repo_read_node.py uses. A hand-written fake on either side would
prove nothing about the wiring that actually breaks.

Named per acceptance criterion (AC1-AC7).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import worktree_janitor as wj
from terminal_mcp import worktree_router as router
from terminal_mcp.config import (
    AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig,
    WorktreeJanitorConfig,
)
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.node_client import LocalNodeClient, NodeClientError, RemoteNodeClient
from terminal_mcp.worktree_router import WorktreeRouter

TOKEN = "node-token-for-worktree-janitor"
REMOTE_NODE = "dell-linux"

_ENV = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True, env={**_ENV, "HOME": str(cwd)})


@pytest.fixture
def node_repo(tmp_path):
    """A repo that exists only on the 'remote' node, with one clean+merged
    linked worktree old enough to be a candidate."""
    root = tmp_path / "node-side" / "service"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("*.db\n.env\n")
    (root / "handler.py").write_text("def handle():\n    return 1\n")
    (root / "payload.bin").write_bytes(b"z" * 4096)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "node-side work")
    worktree = root.parent / "wt-node"
    _git(root, "worktree", "add", "-q", "-b", "task/node", str(worktree), "main")
    old = time.time() - 99_999
    os.utime(worktree, (old, old))
    return root, worktree


def _node_config(root, *, mode="auto_execute"):
    return AppConfig(
        permissions=PermissionsConfig(True, False),
        allowed_session_patterns=("agent-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, allowed_cwd_roots=(str(root),),
                                                 protected_sessions=()),
        worktree_janitor=WorktreeJanitorConfig(mode=mode, allowed_roots=(str(root),),
                                               grace_seconds=300),
    )


@pytest.fixture
def node(node_repo, tmp_path):
    root, _ = node_repo
    terminal = TerminalService(_node_config(root.parent),
                               grants=SessionGrantStore(tmp_path / "grants.db"))
    app = build_node_agent(node_id=REMOTE_NODE, terminal=terminal, token=TOKEN,
                           workspace_root=str(root.parent))
    return TestClient(app)


class _NodeClientOverTestClient(RemoteNodeClient):
    """The production RemoteNodeClient, transport swapped for an in-process app.

    Subclassed rather than reimplemented so the URL building, bearer header,
    JSON body encoding and the 200-vs-non-200 convention are all the real
    code paths."""

    def __init__(self, test_client: TestClient, token: str = TOKEN) -> None:
        super().__init__("http://node.invalid", token)
        self._test_client = test_client

    def _request(self, method, path, *, params=None, body=None):
        response = self._test_client.request(
            method, path, params=params,
            content=json.dumps(body) if body is not None else None,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"})
        if response.status_code != 200:
            raise NodeClientError(f"{response.status_code} from {path}")
        return response.json()


class _Controller:
    def __init__(self, clients: dict, local_node_id: str = "local") -> None:
        self._clients = clients
        self.local_node_id = local_node_id

    def client_for(self, node_id):
        return self._clients.get(node_id)


_TASK = {"id": "T1", "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
         "terminal_at": 0.0, "metadata": {}}


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


# == AC1: the routes exist, authenticated, 200-with-error convention =======

def test_ac1_candidates_requires_auth(node, node_repo):
    root, _ = node_repo
    assert node.get("/v1/worktree/candidates",
                    params={"repo_path": str(root)}).status_code == 401


def test_ac1_cleanup_requires_auth(node, node_repo):
    _, worktree = node_repo
    assert node.post("/v1/worktree/cleanup",
                     json={"worktree_path": str(worktree)}).status_code == 401


def test_ac1_candidates_classifies_on_the_node(node, node_repo):
    root, worktree = node_repo
    response = node.get("/v1/worktree/candidates", params={"repo_path": str(root)},
                        headers=_auth())
    assert response.status_code == 200
    body = response.json()
    assert body["node_id"] == REMOTE_NODE
    paths = {c["worktree_path"] for c in body["candidates"]}
    assert str(worktree) in paths
    assert str(root) in paths  # the main worktree is reported, and BLOCKED
    main = next(c for c in body["candidates"] if c["worktree_path"] == str(root))
    assert main["policy_class"] == wj.BLOCKED


def test_ac1_cleanup_is_post_only(node, node_repo):
    _, worktree = node_repo
    assert node.get("/v1/worktree/cleanup", headers=_auth()).status_code == 405


def test_ac1_an_argument_error_is_a_200_with_a_code_not_a_4xx(node):
    """A 4xx would be read as "the node is unreachable" by node_client._request,
    which is a different and worse thing to tell an operator."""
    missing_repo = node.get("/v1/worktree/candidates", headers=_auth())
    assert missing_repo.status_code == 200
    assert missing_repo.json()["error"] == "INVALID_ARGUMENT"

    missing_path = node.post("/v1/worktree/cleanup", json={}, headers=_auth())
    assert missing_path.status_code == 200
    assert missing_path.json()["error"] == "INVALID_ARGUMENT"


def test_ac1_the_nodes_own_allowlist_decides(node, tmp_path):
    """The node's config, not the caller's, governs the node's paths."""
    outside = tmp_path / "controller-side"
    outside.mkdir()
    _git(outside, "init", "-q", "-b", "main")
    (outside / "f.txt").write_text("x\n")
    _git(outside, "add", "-A")
    _git(outside, "commit", "-q", "-m", "c")
    body = node.get("/v1/worktree/candidates", params={"repo_path": str(outside)},
                    headers=_auth()).json()
    for candidate in body["candidates"]:
        assert candidate["policy_class"] != wj.AUTO_SAFE


# == AC2: the node re-classifies and refuses a stale view ==================

def test_ac2_a_head_mismatch_is_refused(node, node_repo):
    root, worktree = node_repo
    body = node.post("/v1/worktree/cleanup", headers=_auth(), json={
        "worktree_path": str(worktree), "repo_path": str(root),
        "expected_head": "0" * 40, "dry_run": False}).json()
    assert body["error"] == "IDENTITY_MISMATCH"
    assert body["outcome"] == "ABORTED"
    assert worktree.is_dir(), "a stale view must never authorise a removal"


def test_ac2_a_branch_mismatch_is_refused(node, node_repo):
    root, worktree = node_repo
    body = node.post("/v1/worktree/cleanup", headers=_auth(), json={
        "worktree_path": str(worktree), "repo_path": str(root),
        "expected_branch": "task/something-else", "dry_run": False}).json()
    assert body["error"] == "IDENTITY_MISMATCH"
    assert worktree.is_dir()


def test_ac2_a_matching_identity_proceeds(node, node_repo):
    root, worktree = node_repo
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    body = node.post("/v1/worktree/cleanup", headers=_auth(), json={
        "worktree_path": str(worktree), "repo_path": str(root),
        "expected_head": head, "expected_branch": "task/node", "dry_run": True}).json()
    assert body.get("error") != "IDENTITY_MISMATCH"


def test_ac2_an_absent_worktree_is_reported_not_invented(node, node_repo, tmp_path):
    root, _ = node_repo
    body = node.post("/v1/worktree/cleanup", headers=_auth(), json={
        "worktree_path": str(tmp_path / "never-existed"), "repo_path": str(root),
        "expected_head": "0" * 40, "dry_run": False}).json()
    assert body["error"] == "WORKTREE_ABSENT"


def test_ac2_the_node_decides_with_its_own_mode(node_repo, tmp_path):
    """observe_only ON THE NODE means nothing is removed, whatever the caller
    asks for -- the node's config is what governs its own filesystem."""
    root, worktree = node_repo
    terminal = TerminalService(_node_config(root.parent, mode="observe_only"),
                               grants=SessionGrantStore(tmp_path / "g2.db"))
    client = TestClient(build_node_agent(node_id=REMOTE_NODE, terminal=terminal,
                                         token=TOKEN, workspace_root=str(root.parent)))
    body = client.post("/v1/worktree/cleanup", headers=_auth(), json={
        "worktree_path": str(worktree), "repo_path": str(root), "task": _TASK,
        "dry_run": False}).json()
    assert body["outcome"] == "WOULD_REMOVE"
    assert worktree.is_dir()


# == AC3: both transports, identical engine ===============================

def test_ac3_the_protocol_and_both_clients_expose_the_methods():
    for client in (LocalNodeClient, RemoteNodeClient):
        assert callable(getattr(client, "worktree_candidates", None)), client.__name__
        assert callable(getattr(client, "worktree_cleanup", None)), client.__name__


def test_ac3_remote_candidates_through_the_real_client(node, node_repo):
    root, worktree = node_repo
    payload = _NodeClientOverTestClient(node).worktree_candidates(str(root))
    assert payload["node_id"] == REMOTE_NODE
    assert str(worktree) in {c["worktree_path"] for c in payload["candidates"]}


def test_ac3_remote_cleanup_through_the_real_client_really_removes(node, node_repo):
    root, worktree = node_repo
    assert worktree.is_dir()
    payload = _NodeClientOverTestClient(node).worktree_cleanup(
        str(worktree), repo_path=str(root), task=_TASK, dry_run=False)
    assert payload["outcome"] == "REMOVED", payload
    assert not worktree.exists(), "the node really removed its own worktree"
    assert payload["node_id"] == REMOTE_NODE


def test_ac3_local_and_remote_run_the_same_engine(node_repo, tmp_path):
    """Identical classification from both paths for the same repo, which is what
    "identical engine code" has to mean in practice."""
    root, _ = node_repo
    terminal = TerminalService(_node_config(root.parent),
                               grants=SessionGrantStore(tmp_path / "g3.db"))
    local = LocalNodeClient(terminal).worktree_candidates(str(root))
    client = TestClient(build_node_agent(node_id=REMOTE_NODE, terminal=terminal,
                                         token=TOKEN, workspace_root=str(root.parent)))
    remote = _NodeClientOverTestClient(client).worktree_candidates(str(root))

    def _classes(payload):
        return {c["worktree_path"]: c["policy_class"] for c in payload["candidates"]}

    assert _classes(local) == _classes(remote)
    assert local["node_id"] == "local" and remote["node_id"] == REMOTE_NODE


# == AC4: the controller never removes a remote path ======================

def test_ac4_the_router_contains_no_removal_primitive():
    """Structural. The router's only route to a deletion is asking a node
    client; it must not be able to do it itself."""
    import ast

    tree = ast.parse(Path(router.__file__).read_text())
    called = set()
    for node_ in ast.walk(tree):
        if isinstance(node_, ast.Call):
            func = node_.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    for forbidden in ("rmtree", "remove", "unlink", "rmdir", "removedirs",
                      "remove_worktree", "execute", "run", "Popen"):
        assert forbidden not in called, f"the router must not call {forbidden}()"
    imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
    assert "shutil" not in imported and "subprocess" not in imported
    source = Path(router.__file__).read_text()
    assert "WorktreeExecutor" not in source, "the router must not construct an executor"


def test_ac4_a_remote_candidate_never_reaches_the_local_executor(node, node_repo, tmp_path):
    """A remote path handed to the router must be acted on by the node, and the
    controller's own like-named directory must be untouched (F4)."""
    root, worktree = node_repo
    # A decoy at the same absolute path is impossible; instead assert the router
    # never invoked a local executor by making the local node's client explode.
    class _ExplodingLocal:
        def worktree_cleanup(self, *a, **k):
            raise AssertionError("the local node must never be asked for a remote path")

        def worktree_candidates(self, *a, **k):
            raise AssertionError("the local node must never be asked for a remote path")

    controller = _Controller({REMOTE_NODE: _NodeClientOverTestClient(node),
                              "local": _ExplodingLocal()})
    result = WorktreeRouter(controller).cleanup(
        REMOTE_NODE, str(worktree), repo_path=str(root), task=_TASK, dry_run=False)
    assert result["outcome"] == "REMOVED"
    assert result["node_id"] == REMOTE_NODE
    assert not worktree.exists()


def test_ac4_routing_defaults_to_dry_run(node, node_repo):
    root, worktree = node_repo
    controller = _Controller({REMOTE_NODE: _NodeClientOverTestClient(node)})
    result = WorktreeRouter(controller).cleanup(REMOTE_NODE, str(worktree),
                                               repo_path=str(root), task=_TASK)
    assert result["outcome"] == "WOULD_REMOVE"
    assert worktree.is_dir()


# == AC5: offline vs too-old, never AUTO_SAFE, never local fallback =======

def test_ac5_an_unreachable_node_is_reported_as_unreachable(tmp_path):
    class _Dead(RemoteNodeClient):
        def worktree_candidates(self, repo_path):
            raise NodeClientError("connection refused")

        def worktree_cleanup(self, *a, **k):
            raise NodeClientError("connection refused")

    controller = _Controller({REMOTE_NODE: _Dead("http://dead.invalid", "t")})
    r = WorktreeRouter(controller)
    assert r.candidates(REMOTE_NODE, "/x")["error"] == router.NODE_UNREACHABLE
    cleanup = r.cleanup(REMOTE_NODE, "/x", dry_run=False)
    assert cleanup["error"] == router.NODE_UNREACHABLE
    assert cleanup["outcome"] == "SKIPPED", "nothing may be removed when we cannot ask"


def test_ac5_an_old_agent_is_node_lacks_worktree_janitor(tmp_path):
    """Distinct from unreachable: the node is UP, its agent is old. One says
    wait, the other says deploy."""
    class _Old:
        pass  # no worktree_* methods at all

    controller = _Controller({REMOTE_NODE: _Old()})
    r = WorktreeRouter(controller)
    assert r.candidates(REMOTE_NODE, "/x")["error"] == router.NODE_LACKS_WORKTREE_JANITOR
    assert r.cleanup(REMOTE_NODE, "/x")["error"] == router.NODE_LACKS_WORKTREE_JANITOR


def test_ac5_a_404_is_read_as_an_old_agent_not_an_outage():
    class _FourOhFour(RemoteNodeClient):
        def worktree_candidates(self, repo_path):
            raise NodeClientError("404 from /v1/worktree/candidates")

    controller = _Controller({REMOTE_NODE: _FourOhFour("http://x.invalid", "t")})
    result = WorktreeRouter(controller).candidates(REMOTE_NODE, "/x")
    assert result["error"] == router.NODE_LACKS_WORKTREE_JANITOR


def test_ac5_an_unknown_node_is_not_silently_run_locally():
    controller = _Controller({})
    result = WorktreeRouter(controller).cleanup("nonexistent", "/x", dry_run=False)
    assert result["error"] == router.NODE_NOT_FOUND
    assert result["outcome"] == "SKIPPED"


def test_ac5_no_controller_means_no_routing_not_a_local_guess():
    result = WorktreeRouter(None).cleanup(REMOTE_NODE, "/x", dry_run=False)
    assert result["error"] == router.NODE_NOT_FOUND
    assert result["outcome"] == "SKIPPED"


def test_ac5_an_unreachable_node_classifies_as_review_never_auto_safe():
    verdict = router.classification_for_unreachable(REMOTE_NODE, "/x", router.NODE_UNREACHABLE)
    assert verdict["policy_class"] == wj.REVIEW
    assert verdict["actionable"] is False


def test_ac5_a_failed_node_yields_an_empty_candidate_list_not_a_missing_key():
    """A caller iterating results must not mistake "could not ask" for "clean"."""
    controller = _Controller({})
    result = WorktreeRouter(controller).candidates(REMOTE_NODE, "/x")
    assert result["candidates"] == []
    assert result["counts"] == {}


def test_ac5_one_nodes_failure_does_not_hide_anothers_results(node, node_repo):
    root, _ = node_repo
    controller = _Controller({REMOTE_NODE: _NodeClientOverTestClient(node)})
    report = WorktreeRouter(controller).candidates_for_nodes(
        {REMOTE_NODE: str(root), "ghost": "/nowhere"})
    assert report["nodes"][REMOTE_NODE]["candidates"], "the good node still reported"
    assert report["nodes"]["ghost"]["error"] == router.NODE_NOT_FOUND
    assert report["complete"] is False
    assert [u["node_id"] for u in report["unreachable"]] == ["ghost"]


# == AC7: a locked directory is a refusal, not a retry ====================

def test_ac7_a_locked_directory_surfaces_as_a_refusal_with_no_force(node, node_repo):
    """The Windows-locked-directory shape, reproduced on POSIX with a
    permission-denied subtree: git cannot empty it, so the removal fails. It
    must be reported once, with no retry and no force."""
    root, worktree = node_repo
    locked = worktree / "locked"
    locked.mkdir()
    (locked / "f.txt").write_text("x\n")
    (worktree / "untracked-so-git-must-touch-it.txt").write_text("y\n")
    locked.chmod(0o500)
    try:
        body = node.post("/v1/worktree/cleanup", headers=_auth(), json={
            "worktree_path": str(worktree), "repo_path": str(root), "dry_run": False}).json()
        assert body["outcome"] != "REMOVED"
        assert worktree.is_dir(), "a failed removal leaves the worktree intact"
    finally:
        locked.chmod(0o700)


def test_ac7_the_node_never_passes_force(node_repo):
    """I1 holds on the node side too -- asserted structurally on the handler
    and the local client, which are the two places that build an executor."""
    for path in ("terminal_mcp/node_agent.py", "terminal_mcp/node_client.py"):
        source = Path(path).read_text()
        assert "force=True" not in source, f"{path} must never pass force=True"


def test_ac7_a_dirty_remote_worktree_is_never_removed(node, node_repo):
    root, worktree = node_repo
    (worktree / "wip.txt").write_text("unsaved\n")
    body = node.post("/v1/worktree/cleanup", headers=_auth(), json={
        "worktree_path": str(worktree), "repo_path": str(root), "dry_run": False}).json()
    assert body["outcome"] != "REMOVED"
    assert worktree.is_dir()
    assert (worktree / "wip.txt").read_text() == "unsaved\n"


# == contract surface ====================================================

def test_routing_errors_are_pinned():
    assert set(router.ROUTING_ERRORS) == {
        router.NODE_UNREACHABLE, router.NODE_LACKS_WORKTREE_JANITOR,
        router.NODE_NOT_FOUND, router.LOCAL_FALLBACK_REFUSED}
    assert router.NODE_UNREACHABLE != router.NODE_LACKS_WORKTREE_JANITOR
