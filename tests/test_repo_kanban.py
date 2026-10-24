"""Repo Kanban (repo_kanban.py + repo_kanban_dashboard.py): read-only boards
committed in each repo, joined with the session registry."""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp.config import (AppConfig, DashboardConfig, InputPolicyConfig, PermissionsConfig,
                                 RepoKanbanConfig, RepoKanbanRepo, RepoReadConfig,
                                 _load_repo_kanban_config)
from terminal_mcp.core import TerminalService
from terminal_mcp.dashboard import register_dashboard
from terminal_mcp.dashboard_nav import NAV_MARKER
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.repo_kanban import RepoKanbanError, RepoKanbanService, normalize_remote, redact
from terminal_mcp.repo_kanban_dashboard import register

SHA_A = "a" * 40
SHA_B = "b" * 40

BOARD = {
    "repo": "acme/widgets", "updated": "2026-10-24", "integrationBranch": "integration/x",
    "rules": ["Không merge main"], "preview": "https://preview.example/",
    "cards": [
        {"id": "W1", "title": "Ship <b>it</b>", "owner": "Claude", "status": "RUNNING", "branch": "feat/w1",
         "prs": [7], "issues": [3], "evidence": "", "next": "QA", "updated": "2026-10-20"},
        {"id": "W2", "title": "Done thing", "owner": "Codex", "status": "DONE", "branch": "feat/w2",
         "prs": [8], "issues": [], "evidence": "HP 10/10", "next": "—", "updated": "2026-10-21"},
        {"id": "W3", "title": "Legacy status", "owner": "Owner", "status": "TODO", "branch": "",
         "prs": [], "issues": [], "evidence": "", "next": "decide token=ghp_abcdefghijklmnopqrstuvwxyz0123",
         "updated": "2026-10-22"},
        {"id": "W4", "title": "No proof", "owner": "Robot", "status": "DONE", "branch": "", "prs": [],
         "issues": [], "evidence": "", "next": "n", "updated": "2026-10-22"},
    ],
}
LIVE = {
    "schema": "startup-pixel-kanban-live/v1", "generatedAt": "2026-10-30T00:00:00Z", "generatedBy": "claude@dell",
    "integration": {"branch": "integration/x", "exists": True, "sha": SHA_A, "qa": {"status": "DOCS_AHEAD", "commit": SHA_B}},
    "branches": {
        "feat/w1": {"exists": True, "sha": SHA_A, "date": "2026-10-20T00:00:00Z", "subject": "s", "dirtyWorktrees": 0,
                    "qa": {"status": "NONE", "commit": None, "run": None}},
        "feat/w2": {"exists": True, "sha": SHA_B, "date": "2026-10-21T00:00:00Z", "subject": "s", "dirtyWorktrees": 1,
                    "qa": {"status": "FAIL", "commit": SHA_B, "run": "r1"}},
    },
    "prs": {"7": {"state": "MERGED", "isDraft": False, "updatedAt": "2026-10-25T00:00:00Z"},
            "8": {"state": "OPEN", "isDraft": True, "updatedAt": "2026-10-21T00:00:00Z"}},
    "issues": {"3": {"state": "CLOSED"}},
    "qaRuns": [{"id": "r1", "commit": SHA_B, "passed": False, "failedStep": "browser", "seconds": 9, "completedAt": "2026-10-21"}],
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "work" / "widgets"
    (root / "docs" / "kanban").mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "remote", "add", "origin", "https://user:ghp_secret0000000000000000000000@github.com/acme/widgets.git")
    (root / "docs/kanban/board.json").write_text(json.dumps(BOARD), encoding="utf-8")
    (root / "docs/kanban/live.json").write_text(json.dumps(LIVE), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "board")
    return root


@dataclass
class Row:
    session_name: str
    node_id: str
    git_remote: str | None
    git_branch: str | None
    status: str = "ACTIVE"
    agent_type: str = "claude"
    last_known_state: str = "IDLE"
    last_activity_at: str = "2026-10-24T00:00:00Z"
    last_seen_at: str = "2026-10-24T00:00:00Z"
    cwd: str = "/secret/path"
    launch_command: str = "claude --token ghp_x"


ROWS = [
    Row("claude-w1", "dell", "git@github.com:acme/widgets.git", "feat/w1"),
    Row("codex-w2", "hp", "https://github.com/acme/widgets", "feat/w2", agent_type="codex"),
    Row("gone", "hp", "https://github.com/acme/widgets", "feat/w1", status="DELETED"),
    Row("other", "dell", "https://github.com/acme/other", "feat/w1"),
]


def _service(repo: Path, **repo_kwargs) -> RepoKanbanService:
    entry = RepoKanbanRepo(id="widgets", name="Widgets", path=str(repo), ref="main", project="widgets-app", **repo_kwargs)
    return RepoKanbanService(RepoKanbanConfig(repos=(entry,)), allowed_roots=(str(repo.parent),), sessions=lambda: ROWS)


def test_normalize_remote_drops_userinfo():
    assert normalize_remote("https://user:tok@github.com/acme/widgets.git") == "github.com/acme/widgets"
    assert normalize_remote("git@github.com:acme/widgets.git") == "github.com/acme/widgets"
    assert normalize_remote("ssh://git@github.com:22/acme/widgets") == "github.com/acme/widgets"
    assert normalize_remote("not a url") is None


def test_redact_masks_credentials_and_caps_length():
    assert "ghp_" not in redact("use ghp_abcdefghijklmnopqrstuvwxyz0123 now")
    assert redact("password: hunter2 ok") == "[redacted] ok"
    assert redact("Bearer abcdefghijklmnop.qrs") == "[redacted]"
    assert len(redact("x" * 5000)) == 600


def test_list_projects_counts_columns_by_owner_and_joins_sessions(repo):
    result = _service(repo).list_projects()
    [project] = result["projects"]
    assert project["status"] == "OK" and project["error"] is None
    assert project["remote"] == "github.com/acme/widgets"
    assert project["identity"] == "git:github.com/acme/widgets" and project["project"] == "widgets-app"
    assert project["counts"]["RUNNING"]["Claude"] == 1
    assert project["counts"]["DONE"]["Codex"] == 1
    assert project["counts"]["NEW"]["Owner"] == 1          # legacy TODO maps to NEW
    assert project["counts"]["DONE"]["Unknown"] == 1
    assert project["integration"] == {"branch": "integration/x", "sha": SHA_A, "qa": "DOCS_AHEAD"}
    assert project["sessions"] == {"total": 2, "active": 2}  # deleted + other repo excluded
    assert project["last_updated"] == "2026-10-25"
    assert "ghp_secret" not in json.dumps(result)


def test_board_cards_carry_live_facts_warnings_and_sessions(repo):
    board = _service(repo).get_board("widgets")
    cards = {card["id"]: card for card in board["cards"]}
    w1, w2, w3, w4 = cards["W1"], cards["W2"], cards["W3"], cards["W4"]
    assert w1["prs"] == [{"number": 7, "state": "MERGED", "draft": False, "updated_at": "2026-10-25T00:00:00Z"}]
    assert w1["issues"] == [{"number": 3, "state": "CLOSED"}]
    assert w1["warnings"] == ["mọi PR đã merge nhưng thẻ chưa DONE", "RUNNING nhưng 3 ngày không có commit"]
    assert [s["session_name"] for s in w1["sessions"]] == ["claude-w1"]
    assert w2["warnings"] == ["DONE nhưng QA tại head FAIL"]
    assert w2["branch_live"]["dirtyWorktrees"] == 1 and w2["branch_live"]["qa"]["status"] == "FAIL"
    assert w3["status"] == "NEW" and "[redacted]" in w3["next"]
    assert w4["owner"] == "Unknown"
    assert w4["warnings"] == ["owner không hợp lệ", "DONE nhưng thiếu bằng chứng"]
    assert board["qa_runs"][0]["failed_step"] == "browser"
    # Session rows never carry cwd or the launch command.
    serialized = json.dumps(board)
    assert "/secret/path" not in serialized and "--token" not in serialized


def test_reads_the_committed_ref_never_the_worktree(repo):
    (repo / "docs/kanban/board.json").write_text(json.dumps({**BOARD, "cards": []}), encoding="utf-8")
    assert len(_service(repo).get_board("widgets")["cards"]) == 4


def test_errors_are_codes_not_stderr(repo, tmp_path):
    service = _service(repo, board_path="docs/missing.json")
    [project] = service.list_projects()["projects"]
    assert project["error"]["code"] == "BOARD_MISSING"
    assert project["sessions"]["total"] == 2               # still shows who works there
    assert _service(repo).list_projects()["projects"][0]["error"] is None
    bad_ref = RepoKanbanService(RepoKanbanConfig(repos=(RepoKanbanRepo(id="w", name="W", path=str(repo), ref="nope"),)),
                                allowed_roots=(str(repo.parent),))
    assert bad_ref.list_projects()["projects"][0]["error"]["code"] == "REF_NOT_FOUND"
    outside = RepoKanbanService(RepoKanbanConfig(repos=(RepoKanbanRepo(id="w", name="W", path=str(repo)),)),
                                allowed_roots=(str(tmp_path / "elsewhere"),))
    assert outside.list_projects()["projects"][0]["error"]["code"] == "REPO_OUTSIDE_ALLOWED_ROOTS"
    with pytest.raises(RepoKanbanError) as excinfo:
        _service(repo).get_board("../etc")
    assert excinfo.value.code == "REPO_NOT_FOUND"


def test_invalid_json_is_reported(repo):
    (repo / "docs/kanban/board.json").write_text("{nope", encoding="utf-8")
    _git(repo, "commit", "-qam", "break")
    assert _service(repo).list_projects()["projects"][0]["error"]["code"] == "INVALID_JSON"


def test_config_loader_is_strict():
    config = _load_repo_kanban_config({"repos": [{"id": "sp", "name": "Startup Pixel", "path": "/srv/sp",
                                                  "ref": "origin/integration/x", "project": "startup-pixel"}]})
    assert config.repos[0].ref == "origin/integration/x" and config.repos[0].project == "startup-pixel"
    assert _load_repo_kanban_config({}).repos == ()
    for bad in ({"repos": [{"id": "Bad Id", "path": "/x"}]},
                {"repos": [{"id": "a", "path": "relative"}]},
                {"repos": [{"id": "a", "path": "/x", "ref": "--upload-pack=evil"}]},
                {"repos": [{"id": "a", "path": "/x", "ref": "a..b"}]},
                {"repos": [{"id": "a", "path": "/x", "board_path": "../../etc/passwd"}]},
                {"repos": [{"id": "a", "path": "/x"}, {"id": "a", "path": "/y"}]},
                {"repos": [{"id": "a", "path": "/x", "project": "No Slug"}]},
                {"enabled": "yes"}):
        with pytest.raises(ValueError):
            _load_repo_kanban_config(bad)


@pytest.fixture
def client(repo, tmp_path):
    logging.disable(logging.CRITICAL)
    config = AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("test-*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("test-*",)),
        dashboard=DashboardConfig(), repo_read=RepoReadConfig(allowed_roots=(str(repo.parent),)),
        repo_kanban=RepoKanbanConfig(repos=(RepoKanbanRepo(id="widgets", name="Widgets", path=str(repo), ref="main"),)),
    )
    service = TerminalService(config, grants=SessionGrantStore(tmp_path / "grants.db"))
    server = build_mcp(service)
    register_dashboard(server, service)
    yield TestClient(server.streamable_http_app(), base_url="https://testserver")
    logging.disable(logging.NOTSET)


def test_routes_serve_the_page_and_read_only_json(client):
    page = client.get("/dashboard/repo-kanban")
    assert page.status_code == 200 and NAV_MARKER in page.text
    assert page.headers["cache-control"] == "no-store" and page.headers["x-frame-options"] == "DENY"
    assert "innerHTML" not in page.text
    projects = client.get("/dashboard/api/repo-kanban/projects").json()
    assert [p["id"] for p in projects["projects"]] == ["widgets"]
    board = client.get("/dashboard/api/repo-kanban/board?repo=widgets").json()
    assert len(board["cards"]) == 4
    missing = client.get("/dashboard/api/repo-kanban/board?repo=nope")
    assert missing.status_code == 404 and missing.json()["error"] == "REPO_NOT_FOUND"
    # No mutation route exists.
    assert client.post("/dashboard/api/repo-kanban/board?repo=widgets", headers={"Origin": "https://testserver"}).status_code == 405


def test_routes_honour_the_read_guard(repo):
    class Server:
        routes = {}

        def custom_route(self, path, methods, include_in_schema=False):
            def wrap(fn):
                Server.routes[path] = fn
                return fn
            return wrap

    from starlette.responses import JSONResponse
    denied = JSONResponse({"error": "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"}, status_code=403)
    register(Server(), lambda request: (denied, None), _service(repo))
    assert set(Server.routes) == {"/dashboard/repo-kanban", "/dashboard/api/repo-kanban/projects",
                                  "/dashboard/api/repo-kanban/board"}
    import anyio
    for route in Server.routes.values():
        assert anyio.run(route, None).status_code == 403
