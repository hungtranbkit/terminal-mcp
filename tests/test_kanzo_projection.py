from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from terminal_mcp.kanzo_projection import (
    ProjectionError,
    snapshot_from_repo_kanban,
    write_projection,
)
from terminal_mcp.kanzo_source import (
    GitHubWorkItem,
    HttpMcpReadSource,
    LiveProject,
    SourceError,
    load_live_projects,
    snapshot_from_terminal_mcp,
)
from terminal_mcp.kanzo_reconcile import reconcile_once

SYNCED = "2026-11-02T09:30:00Z"


def _snapshot() -> dict:
    return {
        "projects": [
            {
                "id": "brandagent",
                "name": "BrandAgent",
                "remote": "github.com/hungtranbkit/brandagent",
                "github_url": "https://github.com/hungtranbkit/brandagent",
                "cards": [
                    {
                        "id": "BA-17",
                        "title": "Waiting for owner",
                        "status": "BLOCKED",
                        "owner": "Claude",
                        "updated": "2026-11-01",
                        "last_updated": "2026-11-01",
                        "issues": [{"number": 17, "state": "OPEN"}],
                        "prs": [],
                        "evidence": "",
                        "next": "Owner decision",
                        "warnings": ["stale heartbeat"],
                        "sessions": [
                            {
                                "session_name": "brandagent-work",
                                "node": "hp",
                                "state": "IDLE",
                            }
                        ],
                    },
                    {
                        "id": "BA-18",
                        "title": "Verified result",
                        "status": "DONE",
                        "owner": "Codex",
                        "updated": "2026-11-02",
                        "last_updated": "2026-11-02",
                        "issues": [{"number": 18, "state": "CLOSED"}],
                        "prs": [{"number": 9, "state": "MERGED"}],
                        "evidence": "pytest PASS at abc123",
                        "next": "",
                        "warnings": [],
                        "sessions": [],
                    },
                ],
            },
            {
                "id": "offline-pos",
                "name": "Offline POS",
                "remote": "github.com/hungtranbkit/offline-pos",
                "github_url": "https://github.com/hungtranbkit/offline-pos",
                "cards": [
                    {
                        "id": "POS-3",
                        "title": "Waiting QA",
                        "status": "REVIEW",
                        "owner": "Owner",
                        "updated": "2026-11-02",
                        "last_updated": "2026-11-02",
                        "issues": [{"number": 3, "state": "OPEN"}],
                        "prs": [],
                        "evidence": "build PASS",
                        "next": "Device QA",
                        "warnings": [],
                        "sessions": [],
                    }
                ],
            },
        ]
    }


def _rows(db: Path, sql: str) -> list[tuple]:
    with sqlite3.connect(db) as conn:
        return conn.execute(sql).fetchall()


def test_projection_preserves_two_project_identity_and_truthful_states(tmp_path: Path):
    result = write_projection(_snapshot(), tmp_path / "kanzo", synced_at=SYNCED)

    assert result.projects == 2 and result.tasks == 3 and result.duplicates == 0
    db = tmp_path / "kanzo" / "kanzo.db"
    assert _rows(db, "select slug, name from projects order by slug") == [
        ("brandagent", "BrandAgent"),
        ("offline-pos", "Offline POS"),
    ]
    tasks = _rows(db, "select title, status, tags, human from tasks order by title")
    assert tasks == [
        ("BA-17 · Waiting for owner", "todo", "blocked,stale,claude", 1),
        ("BA-18 · Verified result", "done", "verified,codex", 1),
        ("POS-3 · Waiting QA", "review", "waiting-qa,owner", 1),
    ]
    # An idle session is evidence only; it never makes the blocked task done.
    description = _rows(db, "select description from tasks where title like 'BA-17%'")[
        0
    ][0]
    assert "IDLE" in description and "Canonical status: BLOCKED" in description
    assert "Source: https://github.com/hungtranbkit/brandagent/issues/17" in description
    assert f"Last synced: {SYNCED}" in description

    with sqlite3.connect(db) as conn:
        before = int(
            conn.execute("select value from meta where key = 'changes'").fetchone()[0]
        )
        conn.execute("update tasks set status = 'doing' where title like 'BA-17%'")
        conn.commit()
        after = int(
            conn.execute("select value from meta where key = 'changes'").fetchone()[0]
        )
    assert after == before + 1


def test_reconcile_is_atomic_and_does_not_duplicate_cards(tmp_path: Path):
    home = tmp_path / "kanzo"
    first = write_projection(_snapshot(), home, synced_at=SYNCED)
    before = (home / "kanzo.db").read_bytes()
    second = write_projection(_snapshot(), home, synced_at=SYNCED)

    assert first.digest == second.digest
    assert before == (home / "kanzo.db").read_bytes()
    assert _rows(home / "kanzo.db", "select count(*) from tmcp_projection") == [(3,)]
    assert not list(home.glob("*.tmp"))

    # Kanzo is writable by design. A local move is never accepted as canonical;
    # the next one-way reconciliation restores the source state.
    with sqlite3.connect(home / "kanzo.db") as conn:
        conn.execute("update tasks set status = 'done' where title like 'BA-17%'")
        conn.commit()
    write_projection(_snapshot(), home, synced_at=SYNCED)
    assert _rows(
        home / "kanzo.db", "select status from tasks where title like 'BA-17%'"
    ) == [("todo",)]


def test_duplicate_canonical_keys_and_cross_project_repo_leakage_are_rejected(
    tmp_path: Path,
):
    duplicate = _snapshot()
    duplicate["projects"][0]["cards"].append(dict(duplicate["projects"][0]["cards"][0]))
    with pytest.raises(ProjectionError, match="duplicate canonical task"):
        write_projection(duplicate, tmp_path / "duplicate", synced_at=SYNCED)

    mismatched = _snapshot()
    mismatched["projects"][1]["cards"][0]["issues"] = [
        {
            "number": 3,
            "state": "OPEN",
            "url": "https://github.com/hungtranbkit/brandagent/issues/3",
        }
    ]
    with pytest.raises(ProjectionError, match="cross-project source"):
        write_projection(mismatched, tmp_path / "mismatch", synced_at=SYNCED)

    traversal = _snapshot()
    traversal["projects"][1]["cards"][0]["issues"] = [
        {
            "number": 3,
            "state": "OPEN",
            "url": "https://github.com/hungtranbkit/offline-pos/../../brandagent/issues/3",
        }
    ]
    with pytest.raises(ProjectionError, match="cross-project source"):
        write_projection(traversal, tmp_path / "traversal", synced_at=SYNCED)


def test_invalid_or_unverified_done_stays_out_of_done(tmp_path: Path):
    snapshot = _snapshot()
    card = snapshot["projects"][0]["cards"][1]
    card["evidence"] = ""
    card["warnings"] = ["DONE nhưng thiếu bằng chứng"]
    write_projection(snapshot, tmp_path / "kanzo", synced_at=SYNCED)

    assert _rows(
        tmp_path / "kanzo" / "kanzo.db",
        "select status, tags from tasks where title like 'BA-18%'",
    ) == [("review", "qa-pending,codex")]

    snapshot = _snapshot()
    card = snapshot["projects"][0]["cards"][1]
    card["warnings"] = ["DONE nhưng code đổi sau lần QA PASS cuối"]
    write_projection(snapshot, tmp_path / "stale", synced_at=SYNCED)
    assert _rows(
        tmp_path / "stale" / "kanzo.db",
        "select status, tags from tasks where title like 'BA-18%'",
    ) == [("review", "qa-pending,codex")]


def test_snapshot_must_be_sanitized_and_bounded(tmp_path: Path):
    snapshot = _snapshot()
    snapshot["projects"][0]["cards"][0]["next"] = (
        "token ghp_abcdefghijklmnopqrstuvwxyz012345"
    )
    with pytest.raises(ProjectionError, match="credential-shaped"):
        write_projection(snapshot, tmp_path / "kanzo", synced_at=SYNCED)

    snapshot = _snapshot()
    snapshot["projects"][0]["cards"][0]["title"] = "x" * 1001
    with pytest.raises(ProjectionError, match="too long"):
        write_projection(snapshot, tmp_path / "kanzo", synced_at=SYNCED)


def test_repo_kanban_service_is_the_only_projection_source():
    snapshot = _snapshot()

    class Service:
        def list_projects(self):
            return {
                "projects": [
                    {key: value for key, value in project.items() if key != "cards"}
                    for project in snapshot["projects"]
                ]
            }

        def get_board(self, repo_id):
            return next(
                project for project in snapshot["projects"] if project["id"] == repo_id
            )

    result = snapshot_from_repo_kanban(Service(), ["offline-pos", "brandagent"])
    assert [project["id"] for project in result["projects"]] == [
        "brandagent",
        "offline-pos",
    ]
    assert len(result["projects"][0]["cards"]) == 2

    with pytest.raises(ProjectionError, match="not configured"):
        snapshot_from_repo_kanban(Service(), ["not-allowed"])


def test_live_source_uses_real_session_identity_and_idle_is_unknown():
    class Source:
        def call(self, tool, arguments):
            if tool == "terminal_registry_search":
                name = arguments["query"]
                return {
                    "records": [{
                        "node_id": "dell-linux",
                        "session_name": name,
                        "stable_session_id": "16c73059-d6a0-496a-89de-494fc992a75b",
                        "status": "ACTIVE",
                        "agent_type": "codex",
                        "cwd": "/home/dell/workspace/brand-agent-studio-20261029",
                        "git_remote": "https://github.com/hungtranbkit/facebook-sales-radar.git",
                        "git_branch": "feat/brandagent-brand-studio-20261029",
                        "last_commit": "7b501242 docs: clarify BrandAgent handoff head",
                        "last_seen_at": "2026-11-02T06:43:42Z",
                    }]
                }
            if tool == "terminal_batch_inspect":
                return {"targets": [{
                    "node": "dell-linux",
                    "session": "dell-codex-brandagent-autocore-v1-20261030",
                    "state": "IDLE",
                    "reason": "codex is back at its composer; the turn has finished",
                    "cwd": "/home/dell/workspace/brand-agent-studio-20261029",
                }]}
            if tool == "terminal_session_tasks":
                return {
                    "session": arguments["session"], "queued": [], "running": [],
                    "blocked_rework": [], "waiting_dependency": [], "recent": [],
                }
            raise AssertionError(tool)

    snapshot = snapshot_from_terminal_mcp(Source(), [LiveProject(
        id="brandagent",
        name="BrandAgent",
        remote="github.com/hungtranbkit/facebook-sales-radar",
        node_id="dell-linux",
        session_name="dell-codex-brandagent-autocore-v1-20261030",
    )])

    card = snapshot["projects"][0]["cards"][0]
    assert card["id"] == "session:16c73059-d6a0-496a-89de-494fc992a75b"
    assert card["status"] == "UNKNOWN"
    assert card["sessions"][0]["state"] == "IDLE"
    assert "No canonical queue task" in card["warnings"][0]
    assert card["evidence"] == ""
    assert card["issues"] == [] and card["prs"] == []


def test_live_source_projects_real_queue_task_without_inventing_qa(tmp_path: Path):
    class Source:
        def call(self, tool, arguments):
            if tool == "terminal_registry_search":
                return {"records": [{
                    "node_id": "dell-linux", "session_name": arguments["query"],
                    "stable_session_id": "1283726d-cd79-409b-aa3d-b8599783da22",
                    "status": "ACTIVE", "agent_type": "codex",
                    "git_remote": "https://github.com/hungtranbkit/offline-pos.git",
                    "git_branch": "fix/offlinepos-ci-nav-browser-20261030",
                    "last_seen_at": "2026-11-02T06:43:42Z",
                }]}
            if tool == "terminal_batch_inspect":
                return {"targets": [{"node": "dell-linux", "session": arguments["targets"][0].split("/", 1)[1], "state": "IDLE"}]}
            if tool == "terminal_session_tasks":
                return {"session": arguments["session"], "queued": [], "running": [],
                        "blocked_rework": [], "waiting_dependency": [],
                        "recent": [{"id": "task-32", "title": "Fix CI navigation",
                                    "node_id": "dell-linux",
                                    "status": "COMPLETED", "updated_at": "2026-11-02T06:40:00Z",
                                    "verification_evidence": ""}]}
            raise AssertionError(tool)

    snapshot = snapshot_from_terminal_mcp(Source(), [LiveProject(
        id="offline-pos", name="Offline POS",
        remote="github.com/hungtranbkit/offline-pos", node_id="dell-linux",
        session_name="dell-codex-offlinepos-ci32-claude-handoff-20261030",
    )])
    card = snapshot["projects"][0]["cards"][0]
    assert card["id"] == "queue:task-32"
    assert card["status"] == "DONE"
    assert card["evidence"] == ""
    write_projection(snapshot, tmp_path / "kanzo-live-source", synced_at=SYNCED)


def test_live_source_refuses_task_without_matching_node_identity():
    class Source:
        def call(self, tool, arguments):
            if tool == "terminal_registry_search":
                return {"records": [{
                    "node_id": "dell-linux", "session_name": arguments["query"],
                    "stable_session_id": "stable", "status": "ACTIVE",
                    "git_remote": "https://github.com/hungtranbkit/offline-pos.git",
                }]}
            if tool == "terminal_batch_inspect":
                return {"targets": []}
            if tool == "terminal_session_tasks":
                return {"queued": [{"id": "wrong-node-task", "node_id": "macbook"}],
                        "running": [], "blocked_rework": [],
                        "waiting_dependency": [], "recent": []}
            raise AssertionError(tool)

    with pytest.raises(SourceError, match="node identity"):
        snapshot_from_terminal_mcp(Source(), [LiveProject(
            id="offline-pos", name="Offline POS",
            remote="github.com/hungtranbkit/offline-pos", node_id="dell-linux",
            session_name="dell-codex-offlinepos-ci32-claude-handoff-20261030",
        )])


def test_live_source_rejects_repo_or_session_identity_mismatch():
    class Source:
        def call(self, tool, arguments):
            return {"records": [{
                "node_id": "other-node", "session_name": arguments["query"],
                "stable_session_id": "stable", "status": "ACTIVE",
                "git_remote": "https://github.com/attacker/wrong.git",
            }]}

    with pytest.raises(SourceError, match="identity mismatch"):
        snapshot_from_terminal_mcp(Source(), [LiveProject(
            id="offline-pos", name="Offline POS",
            remote="github.com/hungtranbkit/offline-pos", node_id="dell-linux",
            session_name="dell-codex-offlinepos-ci32-claude-handoff-20261030",
        )])


def test_http_source_is_loopback_only_and_calls_only_read_tools():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            request = json.loads(self.rfile.read(length))
            calls.append(request)
            if request["method"] == "initialize":
                body = {"jsonrpc": "2.0", "id": request["id"], "result": {}}
                self.send_response(200)
                self.send_header("Mcp-Session-Id", "test-session")
            elif request["method"] == "notifications/initialized":
                self.send_response(202)
                body = {}
            else:
                body = {"jsonrpc": "2.0", "id": request["id"], "result": {
                    "content": [{"type": "text", "text": json.dumps({"records": []})}]
                }}
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            encoded = json.dumps(body).encode()
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = HttpMcpReadSource(f"http://127.0.0.1:{server.server_port}/mcp")
        assert source.call("terminal_registry_search", {"query": "x"}) == {"records": []}
        with pytest.raises(SourceError, match="read tool"):
            source.call("terminal_send_text", {"session": "x", "text": "no"})
        with pytest.raises(SourceError, match="loopback"):
            HttpMcpReadSource("http://192.0.2.1:8766/mcp")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert [call["method"] for call in calls] == [
        "initialize", "notifications/initialized", "tools/call"
    ]


def test_http_source_never_forwards_a_redirect():
    forwarded = []

    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            forwarded.append(True)
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            forwarded.append(True)
            self.send_response(200)
            self.end_headers()

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_POST(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/mcp")
            self.end_headers()

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (sink, redirect)
    ]
    for thread in threads:
        thread.start()
    try:
        with pytest.raises(SourceError, match="read failed"):
            HttpMcpReadSource(f"http://127.0.0.1:{redirect.server_port}/mcp")
    finally:
        for server in (redirect, sink):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join()
    assert forwarded == []


def test_live_project_config_requires_explicit_real_source_identifiers(tmp_path: Path):
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"projects": [{
        "id": "offline-pos", "name": "Offline POS",
        "remote": "github.com/hungtranbkit/offline-pos",
        "node_id": "dell-linux",
        "session_name": "dell-codex-offlinepos-ci32-claude-handoff-20261030",
    }]}), encoding="utf-8")
    projects = load_live_projects(path)
    assert projects == [LiveProject(
        id="offline-pos", name="Offline POS",
        remote="github.com/hungtranbkit/offline-pos", node_id="dell-linux",
        session_name="dell-codex-offlinepos-ci32-claude-handoff-20261030",
    )]

    path.write_text(json.dumps({"projects": [{"id": "offline-pos"}]}), encoding="utf-8")
    with pytest.raises(SourceError, match="exactly"):
        load_live_projects(path)

    path.write_text(" " * 2_000_001, encoding="utf-8")
    with pytest.raises(SourceError, match="too large"):
        load_live_projects(path)


def test_provider_work_items_are_separate_real_cards_and_draft_is_not_done():
    class Source:
        def call(self, tool, arguments):
            if tool == "terminal_registry_search":
                repo = "facebook-sales-radar" if "brandagent" in arguments["query"] else "offline-pos"
                return {"records": [{
                    "node_id": "dell-linux", "session_name": arguments["query"],
                    "stable_session_id": f"stable-{repo}", "status": "ACTIVE",
                    "agent_type": "codex",
                    "git_remote": f"https://github.com/hungtranbkit/{repo}.git",
                    "last_seen_at": "2026-11-02T06:43:42Z",
                }]}
            if tool == "terminal_batch_inspect":
                return {"targets": [{"node": "dell-linux", "session": target.split("/", 1)[1], "state": "IDLE"} for target in arguments["targets"]]}
            if tool == "terminal_session_tasks":
                return {name: [] for name in ("queued", "running", "blocked_rework", "waiting_dependency", "recent")}
            raise AssertionError(tool)

    class GitHub:
        def get(self, item):
            if item.kind == "issue":
                return {"number": 153, "title": "Brand Studio", "state": "open",
                        "html_url": "https://github.com/hungtranbkit/facebook-sales-radar/issues/153",
                        "created_at": "2026-10-29T03:46:44Z", "updated_at": "2026-10-29T04:26:25Z",
                        "labels": [{"name": "in-review"}]}
            return {"number": item.number, "title": "Draft delivery", "state": "open", "draft": True,
                    "html_url": f"https://github.com/{item.repo}/pull/{item.number}",
                    "created_at": "2026-10-29T04:26:08Z", "updated_at": "2026-11-02T06:33:35Z"}

    projects = [
        LiveProject("brandagent", "BrandAgent", "github.com/hungtranbkit/facebook-sales-radar", "dell-linux", "dell-codex-brandagent-autocore-v1-20261030",
                    (GitHubWorkItem("hungtranbkit/facebook-sales-radar", "issue", 153), GitHubWorkItem("hungtranbkit/facebook-sales-radar", "pr", 154))),
        LiveProject("offline-pos", "Offline POS", "github.com/hungtranbkit/offline-pos", "dell-linux", "dell-codex-offlinepos-ci32-claude-handoff-20261030",
                    (GitHubWorkItem("hungtranbkit/offline-pos", "pr", 47),)),
    ]
    snapshot = snapshot_from_terminal_mcp(Source(), projects, github=GitHub())
    cards = [card for project in snapshot["projects"] for card in project["cards"]]
    assert len(cards) == 5
    assert sum(card["id"].startswith("session:") for card in cards) == 2
    assert {card["id"] for card in cards if card["id"].startswith("github:")} == {
        "github:issue:153", "github:pr:154", "github:pr:47"
    }
    drafts = [card for card in cards if card["id"] in {"github:pr:154", "github:pr:47"}]
    assert all(card["status"] == "DRAFT" and card["evidence"] == "" for card in drafts)
    assert all(card["status"] != "DONE" for card in cards)


def test_reconcile_failure_preserves_last_good_database_and_marks_stale(tmp_path: Path):
    home = tmp_path / "kanzo"
    first = reconcile_once(lambda: _snapshot(), home, synced_at=SYNCED)
    before = (home / "kanzo.db").read_bytes()

    def outage():
        raise SourceError("provider unavailable token=secret-value")

    failed = reconcile_once(outage, home, synced_at="2026-11-02T09:31:00Z")
    assert failed["ok"] is False and failed["stale"] is True
    assert "secret-value" not in json.dumps(failed)
    assert (home / "kanzo.db").read_bytes() == before
    status = json.loads((home / "tmcp-reconcile-status.json").read_text())
    assert status["last_success_at"] == SYNCED
    assert status["stale"] is True
    assert first["digest"] == status["last_good_digest"]


def test_manifest_records_disposable_one_way_contract(tmp_path: Path):
    result = write_projection(_snapshot(), tmp_path / "kanzo", synced_at=SYNCED)
    manifest = json.loads((tmp_path / "kanzo" / "tmcp-projection.json").read_text())
    assert manifest == {
        "authority": "terminal-mcp",
        "digest": result.digest,
        "direction": "one-way",
        "disposable": True,
        "projects": 2,
        "schema": "tmcp-kanzo-projection/v1",
        "synced_at": SYNCED,
        "tasks": 3,
    }


def test_existing_non_projection_kanzo_home_is_never_overwritten(tmp_path: Path):
    home = tmp_path / "personal-kanzo"
    home.mkdir()
    database = home / "kanzo.db"
    database.write_bytes(b"owner data")

    with pytest.raises(ProjectionError, match="not a Terminal MCP projection"):
        write_projection(_snapshot(), home, synced_at=SYNCED)
    assert database.read_bytes() == b"owner data"


def test_cli_writes_projection_from_explicit_snapshot(tmp_path: Path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps(_snapshot()), encoding="utf-8")
    home = tmp_path / "kanzo"
    command = [
        sys.executable,
        "scripts/kanzo-readonly-pilot.py",
        "project",
        "--snapshot",
        str(snapshot),
        "--home",
        str(home),
        "--synced-at",
        SYNCED,
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout)
    assert receipt["projects"] == 2 and receipt["tasks"] == 3
    assert receipt["database"] == str(home / "kanzo.db")


def test_launcher_refuses_public_listener_and_execs_loopback_binary(tmp_path: Path):
    root = Path(__file__).parents[1]
    launcher = root / "scripts/kanzo-pilot-launch.sh"
    fake = tmp_path / "kanzo"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --version ]; then echo 'kanzo 0.3.0'; exit 0; fi\n"
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    write_projection(_snapshot(), home, synced_at=SYNCED)

    unsafe = subprocess.run(
        [
            str(launcher),
            "--binary",
            str(fake),
            "--home",
            str(home),
            "--listen",
            "0.0.0.0",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert unsafe.returncode != 0 and "loopback" in unsafe.stderr.lower()

    safe = subprocess.run(
        [
            str(launcher),
            "--binary",
            str(fake),
            "--home",
            str(home),
            "--listen",
            "127.0.0.1",
            "--port",
            "14242",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert safe.returncode == 0, safe.stderr
    assert "--home" in safe.stdout and str(home.resolve()) in safe.stdout
    config = (home / "config.toml").read_text(encoding="utf-8")
    assert 'listen = "127.0.0.1"' in config and "port = 14242" in config
    assert "mcp_http = false" in config


def test_launcher_refuses_non_projection_home_and_wrong_kanzo_version(tmp_path: Path):
    root = Path(__file__).parents[1]
    launcher = root / "scripts/kanzo-pilot-launch.sh"
    fake = tmp_path / "kanzo"
    fake.write_text("#!/bin/sh\necho 'kanzo 9.9.9'\n", encoding="utf-8")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / "kanzo.db").write_bytes(b"personal")

    personal = subprocess.run(
        [str(launcher), "--binary", str(fake), "--home", str(home)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert personal.returncode != 0 and "projection" in personal.stderr.lower()

    (home / "kanzo.db").unlink()
    write_projection(_snapshot(), home, synced_at=SYNCED)
    wrong_version = subprocess.run(
        [str(launcher), "--binary", str(fake), "--home", str(home)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert wrong_version.returncode != 0 and "0.3.0" in wrong_version.stderr

    valid = tmp_path / "valid-kanzo"
    valid.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = --version ]; then echo 'kanzo 0.3.0'; exit 0; fi\n"
        "printf '%s\\n' \"$@\"\n",
        encoding="utf-8",
    )
    valid.chmod(0o755)
    manifest = json.loads((home / "tmcp-projection.json").read_text())
    manifest["digest"] = "not-the-database-digest"
    (home / "tmcp-projection.json").write_text(json.dumps(manifest), encoding="utf-8")
    optimized = subprocess.run(
        [str(launcher), "--binary", str(valid), "--home", str(home)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONOPTIMIZE": "2"},
    )
    assert optimized.returncode != 0 and "do not match" in optimized.stderr
