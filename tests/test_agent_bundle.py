"""The node-agent bundle: building it, and serving it to a node that has a
credential.

WHY IT EXISTS. deploy/install-node-agent.ps1 can install the agent, but it
needs a terminal-mcp CHECKOUT on the node (`pip install -e "$RepoDir"`). A
machine that arrived through Add Node has none and cannot get one: the
repository is private, and the Dashboard hostname is Access-gated to a
machine that has no Access session. So onboarding could reach "registered
and heartbeating" and stop -- 8790 closed, no transport, Create Session
offering a node it could never reach.

The rule these encode: the controller publishes the same source tree that
script already consumes, and serves it over ONE exact machine-facing route
authenticated by the node's own bearer token. Not an anonymous download.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from terminal_mcp import agent_bundle as ab

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "node-agent"
    monkeypatch.setenv("TERMINAL_MCP_AGENT_BUNDLE_DIR", str(root))
    return root


def _build(tmp_path) -> Path:
    return ab.build(REPO, tmp_path / "bundle.zip")


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------

def test_the_bundle_carries_exactly_what_the_install_script_consumes(tmp_path):
    names = set(zipfile.ZipFile(_build(tmp_path)).namelist())
    # install-node-agent.ps1 refuses to run without pyproject.toml, copies
    # config.example.yaml when the node has none, and pip needs the package.
    for required in ab.BUNDLE_FILES:
        assert required in names, required
    assert "terminal_mcp/windows_agent.py" in names, "the agent entry point"
    assert "terminal_mcp/node_agent.py" in names
    assert "terminal_mcp/__init__.py" in names


def test_the_bundle_leaves_out_what_a_node_does_not_need(tmp_path):
    """A node runs the agent; it does not rebuild the project. Smaller is
    less to verify."""
    names = zipfile.ZipFile(_build(tmp_path)).namelist()
    for name in names:
        assert not name.startswith("tests/"), name
        assert not name.startswith("helper/"), name
        assert not name.startswith(".git"), name
        assert "__pycache__" not in name, name
        assert not name.endswith((".pyc", ".pyo", ".so")), name


def test_two_builds_of_one_tree_are_byte_identical(tmp_path):
    """A hash that moves for no reason is a hash nobody can verify."""
    first, second = ab.build(REPO, tmp_path / "a.zip"), ab.build(REPO, tmp_path / "b.zip")
    assert ab.sha256_of(first) == ab.sha256_of(second)


def test_a_missing_source_tree_is_refused_rather_than_half_built(tmp_path):
    with pytest.raises(ab.BundleError) as caught:
        ab.build(tmp_path / "not-a-repo", tmp_path / "x.zip")
    assert caught.value.code == "MISSING_SOURCE"


# ---------------------------------------------------------------------------
# publishing and resolving
# ---------------------------------------------------------------------------

def test_publish_records_the_hash_of_the_stored_bytes(tmp_path, store):
    built = _build(tmp_path)
    manifest = ab.publish(built, version="0.1.0-dev", build_sha="abc1234")
    entry = manifest["bundle"]
    stored = store / "0.1.0-dev" / ab.BUNDLE_NAME
    assert entry["sha256"] == ab.sha256_of(stored)
    assert entry["size"] == stored.stat().st_size
    assert entry["version"] == "0.1.0-dev" and entry["build_sha"] == "abc1234"


def test_resolve_returns_the_newest_published_version(tmp_path, store):
    built = _build(tmp_path)
    ab.publish(built, version="0.1.0-dev", build_sha="old")
    ab.publish(built, version="0.2.0-dev", build_sha="new")
    assert ab.resolve()["version"] == "0.2.0-dev"


def test_a_bundle_whose_bytes_drifted_is_refused_not_served(tmp_path, store):
    """The node verifies against this manifest, so the manifest has to
    describe the file the node will actually receive."""
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")
    (store / "0.1.0-dev" / ab.BUNDLE_NAME).write_bytes(b"tampered")

    with pytest.raises(ab.BundleError) as caught:
        ab.resolve()
    assert caught.value.code == "HASH_MISMATCH"


def test_nothing_published_is_an_honest_answer_not_a_crash(store):
    assert ab.available() == {"published": False, "bundle": None}
    with pytest.raises(ab.BundleError) as caught:
        ab.resolve()
    assert caught.value.code == "NOT_PUBLISHED"


def test_available_never_leaks_the_path(tmp_path, store):
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")
    found = ab.available()
    assert found["published"] is True
    assert "path" not in found["bundle"]
    assert set(found["bundle"]) == {"version", "build_sha", "sha256", "size", "name"}


# ---------------------------------------------------------------------------
# extraction safety -- what the node will do with these bytes
# ---------------------------------------------------------------------------

def test_no_member_can_escape_the_extraction_directory(tmp_path):
    """The node extracts this under ProgramData. A member with an absolute
    path or a .. segment would write outside it."""
    for name in zipfile.ZipFile(_build(tmp_path)).namelist():
        assert not name.startswith("/"), name
        assert not name.startswith("\\"), name
        assert ".." not in Path(name).parts, name
        assert ":" not in name, name        # no C:\ drive-absolute members


def test_every_member_is_a_regular_file_with_fixed_permissions(tmp_path):
    """No symlinks, no directory entries with surprising modes -- the
    archive is data, not a filesystem to be trusted."""
    for info in zipfile.ZipFile(_build(tmp_path)).infolist():
        mode = info.external_attr >> 16
        assert not (mode & 0o170000 == 0o120000), "symlink member: %s" % info.filename
        assert info.date_time == (1980, 1, 1, 0, 0, 0), info.filename


# ---------------------------------------------------------------------------
# the route: a node's OWN credential, and nothing else
# ---------------------------------------------------------------------------

import os                                                              # noqa: E402

from starlette.testclient import TestClient                            # noqa: E402

from terminal_mcp.config import (                                      # noqa: E402
    AppConfig,
    DashboardConfig,
    InputPolicyConfig,
    InputPolicyConfig as _IPC,
    PermissionsConfig,
    SessionLifecycleConfig,
)
from terminal_mcp.controller import ControllerService                  # noqa: E402
from terminal_mcp.core import TerminalService                          # noqa: E402
from terminal_mcp.dashboard import register_dashboard                  # noqa: E402
from terminal_mcp.mcp_app import build_mcp                             # noqa: E402
from terminal_mcp.node_client import LocalNodeClient                   # noqa: E402
from terminal_mcp.node_registry import NodeRegistry                    # noqa: E402

NODE = "win-work"
TOKEN = "f" * 64
BUNDLE_PATH = "/dashboard/api/nodes/%s/agent-bundle" % NODE


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_%s" % NODE.upper().replace("-", "_"), TOKEN)
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        input_policy=_IPC(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),)),
    )
    service = TerminalService(config)
    server = build_mcp(service)
    registry = NodeRegistry(tmp_path / "nodes.db")
    controller = ControllerService(registry, local_client=LocalNodeClient(service),
                                   local_workspace_root=str(tmp_path))
    controller.registry.register(node_id=NODE, display_name=NODE, hostname="w",
                                 endpoint="http://w:8790", auth_token_ref="env")
    register_dashboard(server, service, controller=controller)
    return TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": "Bearer %s" % token}


def test_a_node_fetches_the_bundle_with_its_own_token(client, tmp_path, store):
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")

    response = client.get(BUNDLE_PATH, headers=_auth())

    assert response.status_code == 200
    assert response.headers["X-Terminal-Mcp-Agent-Version"] == "0.1.0-dev"
    assert response.headers["X-Terminal-Mcp-Agent-Build-Sha"] == "abc1234"
    assert len(response.content) == int(response.headers["X-Terminal-Mcp-Agent-Size"])
    # The hash the node verifies against describes the bytes it just got.
    import hashlib
    assert hashlib.sha256(response.content).hexdigest() == response.headers["X-Terminal-Mcp-Agent-Sha256"]
    assert zipfile.ZipFile(__import__("io").BytesIO(response.content)).namelist()


def test_head_returns_the_metadata_without_the_bytes(client, tmp_path, store):
    """This is what makes the installer idempotent: it compares version and
    sha256 against what is on disk and skips a download it already has."""
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")

    response = client.head(BUNDLE_PATH, headers=_auth())

    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["X-Terminal-Mcp-Agent-Sha256"]
    assert int(response.headers["Content-Length"]) > 0


def test_no_token_is_refused(client, tmp_path, store):
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")
    response = client.get(BUNDLE_PATH)
    assert response.status_code == 401
    assert response.content[:4] != b"PK\x03\x04"


def test_a_wrong_token_is_refused(client, tmp_path, store):
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")
    for bogus in ("0" * 64, "", "not-a-token", TOKEN[:-1]):
        response = client.get(BUNDLE_PATH, headers=_auth(bogus))
        assert response.status_code == 401, bogus


def test_a_node_cannot_fetch_with_another_nodes_token(client, tmp_path, store, monkeypatch):
    """A node can only ever fetch with its OWN credential."""
    monkeypatch.setenv("TERMINAL_MCP_NODE_TOKEN_OTHER", "a" * 64)
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")

    response = client.get("/dashboard/api/nodes/other/agent-bundle", headers=_auth())

    assert response.status_code == 401


def test_auth_is_checked_before_the_store_is_touched(client, store):
    """Nothing published AND no token must answer on the credential, not
    leak whether a bundle exists."""
    response = client.get(BUNDLE_PATH)
    assert response.status_code == 401


def test_nothing_published_is_a_404_for_an_authenticated_node(client, store):
    response = client.get(BUNDLE_PATH, headers=_auth())
    assert response.status_code == 404
    assert response.json()["error"] == "NOT_PUBLISHED"


def test_a_tampered_store_is_never_served(client, tmp_path, store):
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")
    (store / "0.1.0-dev" / ab.BUNDLE_NAME).write_bytes(b"tampered")

    response = client.get(BUNDLE_PATH, headers=_auth())

    assert response.status_code == 500
    assert response.json()["error"] == "HASH_MISMATCH"
    assert b"tampered" not in response.content


def test_the_response_never_carries_the_token(client, tmp_path, store):
    ab.publish(_build(tmp_path), version="0.1.0-dev", build_sha="abc1234")
    response = client.get(BUNDLE_PATH, headers=_auth())
    for name, value in response.headers.items():
        assert TOKEN not in value, name
