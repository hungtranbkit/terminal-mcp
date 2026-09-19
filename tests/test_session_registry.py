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


# -- controller identity migration: legacy `local` duplicates ----------------

def _seen(store, node_id, name, **kwargs):
    return store.upsert_seen(node_id, name, **kwargs)


def test_an_active_legacy_local_row_is_retired_when_the_canonical_node_has_one(store):
    """The exact shape the live controller ended up in: naming the controller
    changed the node id every later reconcile writes, so one running tmux
    session held two ACTIVE rows under two different ids."""
    _seen(store, "local", "hp-codex1")
    _seen(store, "hp-linux", "hp-codex1")

    result = store.retire_legacy_local_duplicates("hp-linux")

    assert result["retired"] == ["hp-codex1"]
    assert result["kept_unique"] == []
    legacy = store.get("local", "hp-codex1")
    assert legacy.status == STATUS_DELETED, "the duplicate must be tombstoned"
    assert legacy.deleted_at, "a tombstone without a timestamp is not a tombstone"
    assert "superseded by hp-linux/hp-codex1" in (legacy.notes or "")
    # The row itself is KEPT -- this is a retire, never a delete.
    assert store.get("local", "hp-codex1") is not None
    # And the canonical row is untouched.
    assert store.get("hp-linux", "hp-codex1").status == STATUS_ACTIVE


def test_a_unique_live_legacy_session_is_never_retired_or_moved(store):
    """The safety property that matters most: a legacy row with no canonical
    counterpart is a REAL live session that only exists under the old id.
    Retiring it would erase a running session from the registry; re-keying it
    would move a session between nodes on the strength of a placeholder."""
    _seen(store, "local", "only-here")
    _seen(store, "hp-linux", "something-else")

    result = store.retire_legacy_local_duplicates("hp-linux")

    assert result["retired"] == []
    assert result["kept_unique"] == ["only-here"]
    assert store.get("local", "only-here").status == STATUS_ACTIVE
    assert store.get("hp-linux", "only-here") is None, "the session was not moved"


def test_a_canonical_counterpart_that_is_not_active_does_not_authorise_a_retire(store):
    """`MISSING` on the canonical side means the canonical row is NOT covering
    a live session, so the legacy row may still be the only record of one."""
    _seen(store, "local", "s1")
    _seen(store, "hp-linux", "s1")
    store.mark_missing("hp-linux", set())
    assert store.get("hp-linux", "s1").status == STATUS_MISSING

    result = store.retire_legacy_local_duplicates("hp-linux")

    assert result["retired"] == []
    assert result["kept_unique"] == ["s1"]
    assert store.get("local", "s1").status == STATUS_ACTIVE


def test_non_active_legacy_history_is_left_completely_alone(store):
    """527 legacy rows on the live controller, only 22 of them ACTIVE. The rest
    are history and history is not ours to rewrite."""
    _seen(store, "local", "old")
    store.mark_missing("local", set())
    _seen(store, "hp-linux", "old")
    before = store.get("local", "old")

    store.retire_legacy_local_duplicates("hp-linux")

    after = store.get("local", "old")
    assert after.status == STATUS_MISSING == before.status
    assert after.deleted_at is None


def test_the_sweep_is_idempotent(store):
    _seen(store, "local", "dup")
    _seen(store, "hp-linux", "dup")

    first = store.retire_legacy_local_duplicates("hp-linux")
    stamped = store.get("local", "dup").deleted_at
    second = store.retire_legacy_local_duplicates("hp-linux")
    third = store.retire_legacy_local_duplicates("hp-linux")

    assert first["retired"] == ["dup"]
    assert second["retired"] == [] and third["retired"] == []
    assert store.get("local", "dup").deleted_at == stamped, \
        "a re-run must not re-stamp a row it already retired"
    assert (store.get("local", "dup").notes or "").count("superseded by") == 1


def test_a_deployment_that_never_named_its_controller_is_untouched(store):
    """`local` is that deployment's real, working node id -- there is no second
    id for it to be a duplicate OF."""
    _seen(store, "local", "s1")

    for canonical in ("local", "", "   ", None):
        result = store.retire_legacy_local_duplicates(canonical)
        assert result["skipped"] == "NO_CANONICAL_NODE_ID"
        assert result["retired"] == []
    assert store.get("local", "s1").status == STATUS_ACTIVE


def test_another_nodes_rows_are_never_considered(store):
    """Only the `local` placeholder migrates. A real remote node keeps its
    rows even when the canonical node happens to run a same-named session."""
    _seen(store, "dell-linux", "shared-name")
    _seen(store, "hp-linux", "shared-name")

    result = store.retire_legacy_local_duplicates("hp-linux")

    assert result["retired"] == []
    assert store.get("dell-linux", "shared-name").status == STATUS_ACTIVE


def test_a_mixed_registry_ends_up_correct_in_one_pass(store):
    _seen(store, "local", "dup-a")
    _seen(store, "local", "dup-b")
    _seen(store, "local", "unique")
    _seen(store, "local", "history")
    store.mark_missing("local", {"dup-a", "dup-b", "unique"})
    _seen(store, "hp-linux", "dup-a")
    _seen(store, "hp-linux", "dup-b")
    _seen(store, "dell-linux", "unique")  # a DIFFERENT node, not a counterpart

    result = store.retire_legacy_local_duplicates("hp-linux")

    assert result["retired"] == ["dup-a", "dup-b"]
    assert result["kept_unique"] == ["unique"]
    assert store.get("local", "dup-a").status == STATUS_DELETED
    assert store.get("local", "dup-b").status == STATUS_DELETED
    assert store.get("local", "unique").status == STATUS_ACTIVE
    assert store.get("local", "history").status == STATUS_MISSING
    assert store.get("hp-linux", "dup-a").status == STATUS_ACTIVE
