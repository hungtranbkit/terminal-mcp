"""The `repo_*` tools as an MCP CLIENT actually sees them.

This is the layer that matters for the stated goal -- ChatGPT reading Git
directly -- and it is a genuinely separate thing from repo_read.py working:
a tool that is never registered, or whose schema omits a parameter, is
invisible or unusable no matter how correct the engine is. So these tests
go through `list_tools()`/`call_tool()` on a real built MCPServer rather
than calling the Python functions.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from terminal_mcp.mcp_app import build_mcp

pytestmark = pytest.mark.anyio

REPO_TOOLS = ("repo_status", "repo_head", "repo_branches", "repo_remotes", "repo_tree",
              "repo_read", "repo_search", "repo_diff", "repo_log", "repo_show_commit")


@pytest.fixture(scope="module")
def server():
    return build_mcp()


def _git(cwd, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
                   env={"HOME": str(cwd), "PATH": "/usr/bin:/bin",
                        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
                        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x"})


async def test_every_repo_tool_is_listed_on_the_mcp_surface(server):
    """The literal acceptance criterion: an MCP client's tool list contains
    these ten names. Until this passes, no client can read a repository, so
    nothing else in this feature is usable."""
    listed = {tool.name for tool in await server.list_tools()}
    assert set(REPO_TOOLS) <= listed


async def test_no_repo_write_tool_exists_on_the_mcp_surface(server):
    """V1 is read-only, asserted at the surface a caller actually sees.
    A future write capability has to add a name here deliberately."""
    listed = {tool.name for tool in await server.list_tools()}
    forbidden = {"repo_write", "repo_edit", "repo_checkout", "repo_commit", "repo_push",
                 "repo_reset", "repo_clean", "repo_apply_patch", "repo_fetch", "repo_pull",
                 "repo_branch_create", "repo_delete", "repo_exec", "repo_git"}
    assert listed & forbidden == set()
    # And nothing repo_-prefixed beyond the ten reads.
    assert {name for name in listed if name.startswith("repo_")} == set(REPO_TOOLS)


async def test_each_tool_advertises_a_usable_schema(server):
    """A client builds its call from this schema. Every tool must offer the
    four locators, and the two tools with a required argument must declare
    it required -- otherwise a client happily sends a call that cannot
    work."""
    tools = {tool.name: tool for tool in await server.list_tools()}
    for name in REPO_TOOLS:
        schema = tools[name].input_schema
        properties = schema.get("properties", {})
        assert {"path", "session", "project", "node"} <= set(properties), name
        assert (tools[name].description or "").strip(), f"{name} has no description"
    assert "query" in tools["repo_search"].input_schema.get("required", [])
    assert "commit" in tools["repo_show_commit"].input_schema.get("required", [])


def _payload(result):
    """call_tool hands back content blocks; the tool's dict is the JSON in
    the first text block -- which is exactly what an MCP client parses."""
    return json.loads(result.content[0].text)


def _config(root):
    """A server config whose repo_read allowlist is this test's tmp_path.

    Built explicitly rather than by monkeypatching HOME: the real
    config.yaml in this repo names this deployment's own workspace roots, so
    a test that relied on the ambient config would either read the real
    machine or fail depending on where it ran."""
    from terminal_mcp.config import (
        AppConfig, InputPolicyConfig, PermissionsConfig, RepoReadConfig, SessionLifecycleConfig,
    )

    return AppConfig(
        permissions=PermissionsConfig(True, False),
        allowed_session_patterns=("agent-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, protected_sessions=()),
        repo_read=RepoReadConfig(enabled=True, allowed_roots=(str(root),)),
    )


@pytest.fixture
def scoped_server(tmp_path):
    """A real MCPServer built over a TerminalService whose config allows only
    tmp_path -- the whole registration/dispatch path, with a boundary this
    test can reason about."""
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore

    terminal = TerminalService(_config(tmp_path),
                               grants=SessionGrantStore(tmp_path / "grants.db"))
    return build_mcp(service=terminal)


@pytest.fixture
def readable_repo(tmp_path):
    root = tmp_path / "toolrepo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "engine.py").write_text("def tool_visible_symbol():\n    return 'ok'\n")
    (root / "notes.md").write_text("# Notes\n\nsome notes\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@x", "-c", "user.name=T", "commit", "-q", "-m", "tool test commit")
    return root


async def test_calling_repo_status_through_the_mcp_surface(scoped_server, readable_repo):
    result = _payload(await scoped_server.call_tool("repo_status", {"path": str(readable_repo)}))
    assert result["branch"] == "main"
    assert result["node_id"] == "local"
    assert result["located_by"] == "path"


async def test_calling_repo_search_then_repo_read_through_the_mcp_surface(scoped_server, readable_repo):
    """The real workflow: find a symbol, then read the file around it."""
    found = _payload(await scoped_server.call_tool(
        "repo_search", {"path": str(readable_repo), "query": "tool_visible_symbol"}))
    assert [(hit["path"], hit["line"]) for hit in found["results"]] == [("engine.py", 1)]

    content = _payload(await scoped_server.call_tool(
        "repo_read", {"path": str(readable_repo), "file": found["results"][0]["path"],
                      "start_line": 1, "end_line": 2}))
    assert "def tool_visible_symbol" in content["content"]
    assert content["lines_returned"] == 2


async def test_calling_repo_tree_log_and_show_commit_through_the_mcp_surface(scoped_server, readable_repo):
    tree = _payload(await scoped_server.call_tool("repo_tree", {"path": str(readable_repo), "depth": 1}))
    assert "engine.py" in {entry["path"] for entry in tree["entries"]}

    log = _payload(await scoped_server.call_tool("repo_log", {"path": str(readable_repo), "limit": 5}))
    assert log["commits"][0]["subject"] == "tool test commit"

    shown = _payload(await scoped_server.call_tool(
        "repo_show_commit", {"path": str(readable_repo), "commit": log["commits"][0]["commit"]}))
    assert "engine.py" in shown["patch"]


async def test_a_refusal_reaches_the_client_as_an_error_code(scoped_server, readable_repo):
    """A client has to be able to branch on the reason, so refusals travel
    as codes in a normal result -- not as an exception and not as prose."""
    denied = _payload(await scoped_server.call_tool(
        "repo_read", {"path": str(readable_repo), "file": "../../../etc/passwd"}))
    assert denied["error"] == "PATH_OUTSIDE_REPO"

    outside = _payload(await scoped_server.call_tool("repo_status", {"path": "/etc"}))
    assert outside["error"] in ("REPO_NOT_ALLOWED", "NOT_A_GIT_REPO")

    nowhere = _payload(await scoped_server.call_tool("repo_status", {}))
    assert nowhere["error"] == "INVALID_ARGUMENT"
