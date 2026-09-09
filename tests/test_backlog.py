"""Project Backlog -- core store/service tests.

Uses REAL git repos in tmp_path (this project's standing "test against
the real thing" posture -- the identity layer's whole job is reading real
git remotes, so faking that would test nothing).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from terminal_mcp import backlog_store as store
from terminal_mcp.backlog_service import BacklogService
from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.project_identity import normalise_git_remote, resolve_project


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, timeout=30)


def make_repo(root: Path, *, remote: str | None = "https://github.com/acme/widget.git") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    if remote:
        _git(root, "remote", "add", "origin", remote)
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def make_config(*roots: Path) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("*",),
        max_capture_lines=200, default_tail_lines=50,
        input_policy=InputPolicyConfig(allowed_session_patterns=("*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=True,
                                                 allowed_cwd_roots=tuple(str(r) for r in roots)),
    )


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "widget")


@pytest.fixture
def svc(tmp_path):
    return BacklogService(make_config(tmp_path))


# ------------------------------------------------------------ identity
def test_remote_url_styles_normalise_to_one_id():
    ids = {normalise_git_remote(u) for u in [
        "https://github.com/Acme/Widget.git", "git@github.com:Acme/Widget.git",
        "ssh://git@GITHUB.com/Acme/Widget", "https://tok@github.com/Acme/Widget.git"]}
    assert ids == {"github.com/Acme/Widget"}


def test_credentials_never_leak_into_project_id():
    assert "sekret" not in (normalise_git_remote("https://user:sekret@github.com/a/b.git") or "")


def test_same_project_from_any_subdirectory(repo):
    sub = repo / "deep" / "nested"
    sub.mkdir(parents=True)
    assert resolve_project(str(repo)).project_id == resolve_project(str(sub)).project_id


def test_two_repos_are_distinct_projects(tmp_path):
    a = make_repo(tmp_path / "a", remote="https://github.com/acme/a.git")
    b = make_repo(tmp_path / "b", remote="https://github.com/acme/b.git")
    assert resolve_project(str(a)).project_id != resolve_project(str(b)).project_id


def test_repo_without_remote_falls_back_and_says_it_is_not_portable(tmp_path):
    r = make_repo(tmp_path / "local", remote=None)
    identity = resolve_project(str(r))
    assert identity.source == "path" and identity.is_portable is False


def test_non_repo_directory_is_not_a_project(tmp_path):
    plain = tmp_path / "plain"; plain.mkdir()
    assert resolve_project(str(plain)) is None


# ------------------------------------------------------------ path security
def test_path_outside_allowed_roots_is_refused(tmp_path, repo):
    other = make_repo(tmp_path.parent / f"outside-{tmp_path.name}")
    service = BacklogService(make_config(tmp_path))
    assert service.get(str(other))["error"] == "PATH_NOT_ALLOWED"


def test_traversal_is_refused(svc, repo):
    assert svc.get(str(repo / ".." / ".." / ".." / "etc"))["error"] == "PATH_NOT_ALLOWED"


def test_non_project_path_refused_not_silently_created(svc, tmp_path):
    plain = tmp_path / "plain"; plain.mkdir()
    assert svc.get(str(plain))["error"] == "NOT_A_PROJECT"
    assert not (plain / ".terminal-mcp").exists()


# ------------------------------------------------------------ get / add
def test_get_on_project_with_no_backlog_returns_empty_plus_metadata(svc, repo):
    out = svc.get(str(repo))
    assert out["exists"] is False and out["items"] == [] and out["total"] == 0
    assert out["project"]["project_id"] == "git:github.com/acme/widget"
    assert out["revision"] == 0


def test_add_creates_file_and_items(svc, repo):
    out = svc.add(str(repo), tasks=[{"title": "First"}, {"title": "Second", "priority": "P0"}])
    assert len(out["created_ids"]) == 2 and out["revision"] == 1
    assert store.backlog_path(repo).exists()
    got = svc.get(str(repo))
    assert [i["title"] for i in got["items"]] == ["Second", "First"]  # P0 sorts first


def test_add_rejects_empty_title_and_bad_enums(svc, repo):
    assert svc.add(str(repo), tasks=[{"title": "  "}])["error"] == "INVALID_REQUEST"
    assert svc.add(str(repo), tasks=[{"title": "x", "status": "NOPE"}])["error"] == "INVALID_STATUS"
    assert svc.add(str(repo), tasks=[{"title": "x", "priority": "P9"}])["error"] == "INVALID_PRIORITY"


def test_file_is_valid_json_with_schema_version(svc, repo):
    svc.add(str(repo), tasks=[{"title": "x"}])
    doc = json.loads(store.backlog_path(repo).read_text())
    assert doc["schema_version"] == store.SCHEMA_VERSION
    assert doc["project"]["project_id"] == "git:github.com/acme/widget"
    assert doc["items"][0]["id"].startswith("blg_")


# ------------------------------------------------------------ filters
def test_filters(svc, repo):
    svc.add(str(repo), tasks=[
        {"title": "a", "priority": "P0", "type": "bug", "tags": ["ui"]},
        {"title": "b", "priority": "P2", "type": "feature"},
    ])
    assert len(svc.get(str(repo), priority="P0")["items"]) == 1
    assert len(svc.get(str(repo), type="bug")["items"]) == 1
    assert len(svc.get(str(repo), tag="ui")["items"]) == 1
    assert len(svc.get(str(repo), status="BACKLOG")["items"]) == 2
    assert len(svc.get(str(repo), status="DONE")["items"]) == 0


# ------------------------------------------------------------ update
def test_update_patches_and_bumps_revision(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.update(str(repo), task_id=tid, patch={"title": "y", "status": "READY"})
    assert out["item"]["title"] == "y" and out["revision"] == 2


def test_update_rejects_server_owned_fields(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.update(str(repo), task_id=tid, patch={"id": "hax"})
    assert out["error"] == "FIELD_NOT_WRITABLE" and "id" in out["fields"]


def test_update_cannot_set_done_directly(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    assert svc.update(str(repo), task_id=tid, patch={"status": "DONE"})["error"] == "USE_COMPLETE_TOOL"


def test_blocked_requires_a_reason(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    assert svc.update(str(repo), task_id=tid,
                      patch={"status": "BLOCKED"})["error"] == "BLOCKED_REASON_REQUIRED"
    assert svc.block(str(repo), task_id=tid, reason="waiting on API")["item"]["status"] == "BLOCKED"


def test_update_unknown_task(svc, repo):
    svc.add(str(repo), tasks=[{"title": "x"}])
    assert svc.update(str(repo), task_id="blg_nope", patch={"title": "y"})["error"] == "TASK_NOT_FOUND"


# ------------------------------------------------------------ concurrency
def test_expected_revision_conflict_is_refused(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    rev = svc.get(str(repo))["revision"]
    svc.update(str(repo), task_id=tid, patch={"title": "agent-A"})       # someone else writes
    out = svc.update(str(repo), task_id=tid, patch={"title": "agent-B"}, expected_revision=rev)
    assert out["error"] == "REVISION_CONFLICT" and out["actual_revision"] == rev + 1
    assert svc.get(str(repo))["items"][0]["title"] == "agent-A"          # A's write survived


def test_matching_expected_revision_succeeds(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    rev = svc.get(str(repo))["revision"]
    assert "error" not in svc.update(str(repo), task_id=tid, patch={"title": "ok"}, expected_revision=rev)


def test_bulk_update_is_one_revision_bump(svc, repo):
    ids = svc.add(str(repo), tasks=[{"title": "a"}, {"title": "b"}, {"title": "c"}])["created_ids"]
    before = svc.get(str(repo))["revision"]
    out = svc.bulk_update(str(repo), updates=[{"task_id": i, "patch": {"order": n}} for n, i in enumerate(ids)])
    assert out["revision"] == before + 1 and len(out["updated_ids"]) == 3


# ------------------------------------------------------------ isolation / sharing
def test_two_projects_have_independent_backlogs(tmp_path):
    a = make_repo(tmp_path / "a", remote="https://github.com/acme/a.git")
    b = make_repo(tmp_path / "b", remote="https://github.com/acme/b.git")
    service = BacklogService(make_config(tmp_path))
    service.add(str(a), tasks=[{"title": "only-a"}])
    assert service.get(str(b))["total"] == 0
    assert [i["title"] for i in service.get(str(a))["items"]] == ["only-a"]


def test_same_project_shared_across_sessions_and_subdirs(svc, repo):
    """Two 'sessions' at different cwds inside one repo see ONE backlog."""
    sub = repo / "svc" / "api"; sub.mkdir(parents=True)
    svc.add(str(repo), tasks=[{"title": "from-session-1"}])
    svc.add(str(sub), tasks=[{"title": "from-session-2"}])
    titles = {i["title"] for i in svc.get(str(sub))["items"]}
    assert titles == {"from-session-1", "from-session-2"}
    assert svc.get(str(repo))["total"] == 2


def test_session_switching_project_sees_the_other_backlog(tmp_path):
    a = make_repo(tmp_path / "a", remote="https://github.com/acme/a.git")
    b = make_repo(tmp_path / "b", remote="https://github.com/acme/b.git")
    service = BacklogService(make_config(tmp_path))
    service.add(str(a), tasks=[{"title": "task-a"}])
    service.add(str(b), tasks=[{"title": "task-b"}])
    assert [i["title"] for i in service.get(str(a))["items"]] == ["task-a"]
    assert [i["title"] for i in service.get(str(b))["items"]] == ["task-b"]


# ------------------------------------------------------------ persistence
def test_survives_a_fresh_service_instance(tmp_path, repo):
    BacklogService(make_config(tmp_path)).add(str(repo), tasks=[{"title": "persisted"}])
    fresh = BacklogService(make_config(tmp_path))          # simulates a restart
    assert [i["title"] for i in fresh.get(str(repo))["items"]] == ["persisted"]


def test_write_is_atomic_no_partial_file(svc, repo):
    svc.add(str(repo), tasks=[{"title": f"t{n}"} for n in range(50)])
    path = store.backlog_path(repo)
    json.loads(path.read_text())                            # parses => never truncated
    assert not list(path.parent.glob(".backlog-*.tmp"))     # no temp files left behind


# ------------------------------------------------------------ manual edits
def test_hand_edited_file_is_repaired_not_rejected(svc, repo):
    svc.add(str(repo), tasks=[{"title": "x"}])
    path = store.backlog_path(repo)
    doc = json.loads(path.read_text())
    doc["items"][0]["status"] = "WHATEVER"
    doc["items"].append({"title": "added by hand"})          # no id
    path.write_text(json.dumps(doc))
    out = svc.get(str(repo))
    assert out["total"] == 2 and out["repairs"]
    assert all(i["id"].startswith("blg_") for i in out["items"])


def test_validate_writes_back_the_repaired_form(svc, repo):
    svc.add(str(repo), tasks=[{"title": "x"}])
    path = store.backlog_path(repo)
    doc = json.loads(path.read_text()); doc["items"][0]["priority"] = "URGENT"
    path.write_text(json.dumps(doc))
    out = svc.validate(str(repo))
    assert out["written"] is True and out["repairs"]
    assert json.loads(path.read_text())["items"][0]["priority"] == "P2"


def test_broken_json_reports_clearly(svc, repo):
    svc.add(str(repo), tasks=[{"title": "x"}])
    store.backlog_path(repo).write_text("{not json")
    assert svc.get(str(repo))["error"] == "BACKLOG_UNREADABLE"


def test_newer_schema_version_is_refused_not_downgraded(svc, repo):
    svc.add(str(repo), tasks=[{"title": "x"}])
    path = store.backlog_path(repo)
    doc = json.loads(path.read_text()); doc["schema_version"] = 999
    path.write_text(json.dumps(doc))
    assert svc.get(str(repo))["error"] == "BACKLOG_UNREADABLE"


# ------------------------------------------------------------ verified done
def test_done_refused_without_evidence(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.complete(str(repo), task_id=tid)
    assert out["error"] == "EVIDENCE_REQUIRED"
    assert svc.get(str(repo))["items"][0]["status"] != "DONE"


def test_done_accepted_with_evidence(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.complete(str(repo), task_id=tid, commit="abc1234", test="12 passed")
    assert out["item"]["status"] == "DONE" and out["verified_by"] == "evidence"
    assert out["item"]["evidence"]["commits"] == ["abc1234"]


def test_claim_marks_in_progress_and_records_owner(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    out = svc.claim(str(repo), task_id=tid, session="s1", node_id="m910")
    assert out["item"]["status"] == "IN_PROGRESS" and out["item"]["session"] == "s1"


def test_claim_refuses_to_steal(svc, repo):
    tid = svc.add(str(repo), tasks=[{"title": "x"}])["created_ids"][0]
    svc.claim(str(repo), task_id=tid, session="s1")
    assert svc.claim(str(repo), task_id=tid, session="s2")["error"] == "ALREADY_CLAIMED"
    assert "error" not in svc.claim(str(repo), task_id=tid, session="s2", assignee="s2")


# ------------------------------------------------------------ shipped example
def test_shipped_example_is_canonical_and_needs_no_repair():
    """docs/examples/backlog.example.json must be exactly what this
    server would write -- a stale example teaches the wrong shape."""
    path = Path(__file__).resolve().parents[1] / "docs" / "examples" / "backlog.example.json"
    document, repairs = store.load(path)
    assert repairs == [], f"example is not canonical: {repairs}"
    assert store.serialise(document) == path.read_text()
    assert document["schema_version"] == store.SCHEMA_VERSION
    assert path.read_text().isascii(), "example must stay ASCII-clean"


def test_shipped_example_covers_the_interesting_states():
    path = Path(__file__).resolve().parents[1] / "docs" / "examples" / "backlog.example.json"
    document, _ = store.load(path)
    statuses = {i["status"] for i in document["items"]}
    assert {"BACKLOG", "IN_PROGRESS", "DONE"} <= statuses
    dispatched = [i for i in document["items"] if i["queue_task_id"]]
    assert dispatched, "example should show the backlog->queue link"
    done = [i for i in document["items"] if i["status"] == "DONE"]
    assert done and done[0]["evidence"]["commits"], "a DONE item must show its evidence"
