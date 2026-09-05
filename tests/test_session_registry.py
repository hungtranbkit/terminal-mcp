"""session_registry.py: the Persistent Session Registry store itself, in
isolation (no TerminalService/dashboard wiring -- see test_session_
registry_integration.py for that)."""
from __future__ import annotations

import subprocess

import pytest

from terminal_mcp.session_registry import (
    STATUS_ACTIVE, STATUS_DELETED, STATUS_KILLED, STATUS_MISSING, STATUS_OFFLINE,
    SessionRegistryStore, probe_project_info,
)


@pytest.fixture
def store(tmp_path):
    return SessionRegistryStore(tmp_path / "session_registry.db")


def _git_repo(tmp_path, name="proj"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://example.com/t/proj.git"], cwd=repo, check=True)
    (repo / "README.md").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


# -- probe_project_info -----------------------------------------------------

def test_probe_project_info_real_git_repo(tmp_path):
    repo = _git_repo(tmp_path)
    info = probe_project_info(str(repo))
    assert info["repo_root"] == str(repo)
    assert info["git_remote"] == "https://example.com/t/proj.git"
    assert info["git_branch"]  # main or master depending on git config
    assert info["last_commit"] and "init" in info["last_commit"]


def test_probe_project_info_non_repo_dir_is_all_none(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    info = probe_project_info(str(plain))
    assert info == {"repo_root": None, "git_remote": None, "git_branch": None, "last_commit": None}


def test_probe_project_info_nonexistent_path_is_all_none():
    info = probe_project_info("/no/such/path/anywhere")
    assert info == {"repo_root": None, "git_remote": None, "git_branch": None, "last_commit": None}


def test_probe_project_info_none_cwd_is_all_none():
    assert probe_project_info(None) == {"repo_root": None, "git_remote": None, "git_branch": None, "last_commit": None}


# -- upsert_seen / reconcile -------------------------------------------------

def test_upsert_seen_creates_active_record_with_backfilled_project(store, tmp_path):
    repo = _git_repo(tmp_path)
    record = store.upsert_seen("local", "myproj", node_name="Local", backend_type="tmux",
                              cwd=str(repo), agent_type="claude", last_known_state="RUNNING")
    assert record.status == STATUS_ACTIVE
    assert record.repo_root == str(repo)
    assert record.git_remote == "https://example.com/t/proj.git"
    assert record.metadata_complete is True
    assert record.key() == "local/myproj"


def test_upsert_seen_shell_agent_is_metadata_complete_without_cwd(store):
    record = store.upsert_seen("local", "plainshell", agent_type="shell", cwd=None)
    assert record.metadata_complete is True  # shell needs no cwd to be reopenable (matches killed_sessions.py)


def test_upsert_seen_reviving_a_missing_session_clears_killed_offline_fields(store):
    store.upsert_seen("local", "s1", cwd="/tmp")
    store.mark_killed("local", "s1", killed_by="op")
    revived = store.get("local", "s1")
    assert revived.status == STATUS_KILLED
    revived2 = store.upsert_seen("local", "s1", cwd="/tmp")
    assert revived2.status == STATUS_ACTIVE
    assert revived2.killed_at is None


def test_upsert_seen_preserves_existing_project_info_when_not_reprobed(store, tmp_path):
    repo = _git_repo(tmp_path)
    store.upsert_seen("local", "s1", cwd=str(repo))
    # A later reconcile pass for the SAME cwd must not need to re-probe --
    # and even if it did, the value must still be correct/unchanged.
    again = store.upsert_seen("local", "s1", cwd=str(repo))
    assert again.repo_root == str(repo)


# -- mark_missing / mark_node_offline ----------------------------------------

def test_mark_missing_transitions_only_vanished_sessions(store):
    store.upsert_seen("local", "still-here", cwd="/tmp")
    store.upsert_seen("local", "gone-now", cwd="/tmp")
    vanished = store.mark_missing("local", {"still-here"})
    assert vanished == ["gone-now"]
    assert store.get("local", "still-here").status == STATUS_ACTIVE
    assert store.get("local", "gone-now").status == STATUS_MISSING


def test_mark_missing_is_idempotent_and_scoped_to_one_node(store):
    store.upsert_seen("local", "s1", cwd="/tmp")
    store.upsert_seen("worker", "s1", cwd="/tmp")  # same NAME, different node -- distinct row
    store.mark_missing("local", set())
    assert store.get("local", "s1").status == STATUS_MISSING
    assert store.get("worker", "s1").status == STATUS_ACTIVE  # untouched -- different node
    # Idempotent: calling again with the same (empty) seen-set changes nothing further.
    vanished_again = store.mark_missing("local", set())
    assert vanished_again == []


def test_mark_node_offline_only_touches_that_node_and_only_active_rows(store):
    store.upsert_seen("worker", "s1", cwd="/tmp")
    store.upsert_seen("worker", "s2", cwd="/tmp")
    store.mark_killed("worker", "s2", killed_by="op")  # already not ACTIVE
    store.upsert_seen("local", "s3", cwd="/tmp")
    count = store.mark_node_offline("worker")
    assert count == 1  # only s1 was ACTIVE
    assert store.get("worker", "s1").status == STATUS_OFFLINE
    assert store.get("worker", "s2").status == STATUS_KILLED  # unchanged
    assert store.get("local", "s3").status == STATUS_ACTIVE  # different node, untouched


# -- mark_killed / purge -----------------------------------------------------

def test_mark_killed_keeps_the_record_never_removes_it(store):
    store.upsert_seen("local", "s1", cwd="/tmp", agent_type="shell")
    store.mark_killed("local", "s1", killed_by="operator@example.com")
    record = store.get("local", "s1")
    assert record is not None
    assert record.status == STATUS_KILLED
    assert record.killed_at is not None
    assert record.recoverable is True  # metadata_complete (shell) + KILLED


def test_purge_is_a_separate_action_from_kill_and_keeps_a_tombstone(store):
    store.upsert_seen("local", "s1", cwd="/tmp", agent_type="shell")
    store.mark_killed("local", "s1", killed_by="op")
    purged = store.purge("local", "s1", purged_by="operator@example.com")
    assert purged is True
    record = store.get("local", "s1")
    assert record is not None  # tombstone kept, never a bare row delete
    assert record.status == STATUS_DELETED
    assert record.deleted_at is not None
    assert record.recoverable is False  # DELETED is never recoverable
    assert "operator@example.com" in (record.notes or "")


def test_purge_of_nonexistent_record_returns_false(store):
    assert store.purge("local", "ghost") is False


# -- search -------------------------------------------------------------------

def test_search_finds_by_name_cwd_repo_and_node(store, tmp_path):
    repo = _git_repo(tmp_path, "quan_ly_ban_hang_repo")
    store.upsert_seen("local", "quan_ly_ban_hang", cwd=str(repo), agent_type="claude")
    store.upsert_seen("local", "unrelated", cwd="/tmp/unrelated")
    by_name = store.search("quan_ly_ban_hang")
    assert {r.session_name for r in by_name} == {"quan_ly_ban_hang"}
    by_path = store.search(str(repo))
    assert {r.session_name for r in by_path} == {"quan_ly_ban_hang"}
    by_node = store.search("local")
    assert {r.session_name for r in by_node} == {"quan_ly_ban_hang", "unrelated"}
    by_nothing = store.search("does-not-exist-anywhere")
    assert by_nothing == []


# -- node-aware identity / persistence across restart ------------------------

def test_same_session_name_two_nodes_are_distinct_records(store):
    store.upsert_seen("local", "window", cwd="/tmp")
    store.upsert_seen("dell-5530", "window", cwd=None, backend_type="windows_pty")
    local_row = store.get("local", "window")
    remote_row = store.get("dell-5530", "window")
    assert local_row is not None and remote_row is not None
    assert local_row.key() == "local/window"
    assert remote_row.key() == "dell-5530/window"
    assert local_row.cwd != remote_row.cwd


def test_records_persist_across_a_fresh_store_instance_same_path(tmp_path):
    # Simulates a service restart: a brand new SessionRegistryStore object
    # pointed at the same file must see everything the old one wrote.
    path = tmp_path / "session_registry.db"
    first = SessionRegistryStore(path)
    first.upsert_seen("local", "s1", cwd="/tmp", agent_type="shell")
    first.mark_killed("local", "s1", killed_by="op")
    second = SessionRegistryStore(path)
    record = second.get("local", "s1")
    assert record is not None
    assert record.status == STATUS_KILLED


def test_touch_grant_is_a_noop_for_a_session_with_no_registry_row_yet(store):
    store.touch_grant("local", "ghost", read_granted=True, input_granted=True)
    assert store.get("local", "ghost") is None  # never silently creates a row


def test_touch_grant_updates_existing_record(store):
    store.upsert_seen("local", "s1", cwd="/tmp")
    store.touch_grant("local", "s1", read_granted=True, input_granted=True)
    record = store.get("local", "s1")
    assert record.read_granted is True and record.input_granted is True


# -- upsert_manual (migration/backfill entry point) --------------------------

def test_upsert_manual_backfills_a_gone_session_with_no_other_trace(store, tmp_path):
    repo = _git_repo(tmp_path, "offline-pos")
    record = store.upsert_manual("local", "quan_ly_ban_hang", status=STATUS_MISSING,
                                 cwd=str(repo), agent_type="claude",
                                 notes="backfilled from audit.db send_text preview + filesystem search")
    assert record.status == STATUS_MISSING
    assert record.repo_root == str(repo)
    assert record.recoverable is True


def test_upsert_manual_never_overwrites_an_existing_record(store):
    store.upsert_seen("local", "s1", cwd="/tmp", agent_type="shell")
    store.mark_killed("local", "s1", killed_by="op")
    result = store.upsert_manual("local", "s1", status=STATUS_MISSING, cwd="/somewhere/else")
    assert result.status == STATUS_KILLED  # untouched -- upsert_manual only fills GAPS
    assert result.cwd == "/tmp"


# -- watchdog: unexpected drop events -----------------------------------

def test_record_and_list_drop_events(store):
    event_id = store.record_drop_event("local", "s1", "session_missing", detail="cwd=/tmp agent_type=shell")
    events = store.list_drop_events()
    assert len(events) == 1
    assert events[0]["id"] == event_id
    assert events[0]["session_name"] == "s1"
    assert events[0]["kind"] == "session_missing"
    assert events[0]["acknowledged"] == 0
    assert events[0]["recovered"] == 0


def test_list_drop_events_unacknowledged_only_filters(store):
    a = store.record_drop_event("local", "s1", "session_missing")
    store.record_drop_event("local", "s2", "session_missing")
    store.acknowledge_drop_event(a)
    events = store.list_drop_events(unacknowledged_only=True)
    assert len(events) == 1
    assert events[0]["session_name"] == "s2"


def test_acknowledge_drop_event(store):
    event_id = store.record_drop_event("local", "s1", "session_missing")
    assert store.acknowledge_drop_event(event_id, by="tester") is True
    events = store.list_drop_events()
    assert events[0]["acknowledged"] == 1
    assert events[0]["acknowledged_by"] == "tester"


def test_acknowledge_drop_event_unknown_id_returns_false(store):
    assert store.acknowledge_drop_event(99999) is False


def test_mark_drop_event_recovered(store):
    event_id = store.record_drop_event("local", "s1", "session_missing")
    assert store.mark_drop_event_recovered(event_id) is True
    events = store.list_drop_events()
    assert events[0]["recovered"] == 1


def test_mark_drop_events_recovered_for_marks_all_unrecovered_for_that_session(store):
    # A session that dropped, got reopened, dropped again -- both events
    # must be marked recovered once it's seen ACTIVE again, not just the
    # most recent one.
    first = store.record_drop_event("local", "s1", "session_missing")
    second = store.record_drop_event("local", "s1", "session_missing")
    other = store.record_drop_event("local", "s2", "session_missing")

    count = store.mark_drop_events_recovered_for("local", "s1")
    assert count == 2
    events_by_id = {e["id"]: e for e in store.list_drop_events()}
    assert events_by_id[first]["recovered"] == 1
    assert events_by_id[second]["recovered"] == 1
    assert events_by_id[other]["recovered"] == 0  # a DIFFERENT session's event untouched


def test_mark_drop_events_recovered_for_is_a_noop_with_nothing_to_recover(store):
    assert store.mark_drop_events_recovered_for("local", "never-dropped") == 0


def test_drop_events_are_node_scoped(store):
    store.record_drop_event("node-a", "s1", "session_missing")
    store.record_drop_event("node-b", "s1", "session_missing")
    assert store.mark_drop_events_recovered_for("node-a", "s1") == 1
    events_by_node = {e["node_id"]: e for e in store.list_drop_events()}
    assert events_by_node["node-a"]["recovered"] == 1
    assert events_by_node["node-b"]["recovered"] == 0
