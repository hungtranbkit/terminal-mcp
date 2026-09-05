"""SessionKnowledgeStore -- the durable, searchable record of a session's
REAL output (session_knowledge.py). Exercises the actual SQLite/FTS5
storage directly (no TerminalService involved -- that integration lives
in test_session_knowledge_integration.py), matching test_session_
registry.py's own convention for its sibling store.
"""
from __future__ import annotations

import time

import pytest

from terminal_mcp.session_knowledge import (
    CHECKPOINT_MANUAL,
    PROVENANCE_BACKFILLED,
    PROVENANCE_LIVE,
    SessionKnowledgeStore,
    make_instance_id,
)


@pytest.fixture
def store(tmp_path):
    return SessionKnowledgeStore(tmp_path / "knowledge.db", max_chunks_per_instance=10)


INSTANCE = make_instance_id(session_id="$1", pane_id="%1", created_epoch=1000)


def test_make_instance_id_disambiguates_recreated_sessions():
    a = make_instance_id(session_id="$1", pane_id="%1", created_epoch=1000)
    b = make_instance_id(session_id="$2", pane_id="%1", created_epoch=2000)
    assert a != b


def test_append_output_creates_meta_lazily_via_ensure_meta(store, tmp_path):
    store.ensure_meta("local", "sess-1", INSTANCE, cwd=str(tmp_path), agent_type="shell", backend_type="tmux")
    written = store.append_output("local", "sess-1", INSTANCE, "hello world\n")
    assert written == 1
    meta = store.get_meta("local", "sess-1")
    assert meta is not None
    assert meta.total_chunks == 1
    assert meta.total_chars == len("hello world\n")
    assert meta.last_captured_at is not None


def test_append_output_splits_large_text_into_multiple_chunks(store, tmp_path):
    store.ensure_meta("local", "sess-big", INSTANCE, cwd=str(tmp_path))
    big_text = "x" * (store.max_chunk_chars * 2 + 5)
    written = store.append_output("local", "sess-big", INSTANCE, big_text)
    assert written == 3
    timeline = store.timeline("local", "sess-big", INSTANCE, limit=10)
    assert len(timeline) == 3
    assert "".join(c.text for c in timeline) == big_text
    # seq is monotonic and gapless within one append call.
    assert [c.seq for c in timeline] == [0, 1, 2]


def test_append_output_empty_text_writes_nothing(store, tmp_path):
    store.ensure_meta("local", "sess-empty", INSTANCE, cwd=str(tmp_path))
    assert store.append_output("local", "sess-empty", INSTANCE, "") == 0
    assert store.append_output("local", "sess-empty", INSTANCE, "   \n\n") == 0
    meta = store.get_meta("local", "sess-empty")
    assert meta.total_chunks == 0


def test_append_output_redacts_secrets_before_persisting(store, tmp_path):
    store.ensure_meta("local", "sess-secret", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-secret", INSTANCE, "token=abc123secretvalue\nsafe line here\n")
    timeline = store.timeline("local", "sess-secret", INSTANCE, limit=10)
    joined = "\n".join(c.text for c in timeline)
    assert "abc123secretvalue" not in joined
    assert "<REDACTED>" in joined
    assert "safe line here" in joined


def test_append_output_second_call_appends_not_overwrites(store, tmp_path):
    store.ensure_meta("local", "sess-2", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-2", INSTANCE, "line one\n")
    store.append_output("local", "sess-2", INSTANCE, "line two\n")
    timeline = store.timeline("local", "sess-2", INSTANCE, limit=10)
    assert [c.text for c in timeline] == ["line one\n", "line two\n"]
    assert [c.seq for c in timeline] == [0, 1]


def test_cursor_roundtrip(store, tmp_path):
    store.ensure_meta("local", "sess-cursor", INSTANCE, cwd=str(tmp_path))
    assert store.get_cursor("local", "sess-cursor", INSTANCE) is None
    store.set_cursor("local", "sess-cursor", INSTANCE, "1234")
    assert store.get_cursor("local", "sess-cursor", INSTANCE) == "1234"


def test_timeline_respects_since_until(store, tmp_path):
    store.ensure_meta("local", "sess-time", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-time", INSTANCE, "first\n")
    mid = _now_iso()
    time.sleep(0.05)
    store.append_output("local", "sess-time", INSTANCE, "second\n")
    later = store.timeline("local", "sess-time", INSTANCE, since=mid)
    assert [c.text for c in later] == ["second\n"]


def test_timeline_defaults_to_latest_instance_when_unspecified(store, tmp_path):
    old_instance = make_instance_id(session_id="$1", pane_id="%1", created_epoch=100)
    new_instance = make_instance_id(session_id="$2", pane_id="%1", created_epoch=200)
    store.ensure_meta("local", "sess-multi", old_instance, cwd=str(tmp_path))
    store.append_output("local", "sess-multi", old_instance, "old process output\n")
    store.ensure_meta("local", "sess-multi", new_instance, cwd=str(tmp_path))
    store.append_output("local", "sess-multi", new_instance, "new process output\n")
    timeline = store.timeline("local", "sess-multi")  # no instance given
    assert [c.text for c in timeline] == ["new process output\n"]


def test_search_finds_content_by_plain_text(store, tmp_path):
    store.ensure_meta("local", "sess-search", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-search", INSTANCE, "deploying quan_ly_ban_hang to production\n")
    results = store.search("quan_ly_ban_hang")
    assert len(results) == 1
    assert "quan_ly_ban_hang" in results[0]["text"]
    assert results[0]["session_name"] == "sess-search"


def test_search_no_match_returns_empty(store, tmp_path):
    store.ensure_meta("local", "sess-search2", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-search2", INSTANCE, "hello world\n")
    assert store.search("nonexistent-term-xyz") == []


def test_search_filtered_by_project_matches_cwd(store, tmp_path):
    proj_dir = tmp_path / "quan_ly_ban_hang"
    proj_dir.mkdir()
    store.ensure_meta("local", "sess-proj", INSTANCE, cwd=str(proj_dir))
    store.append_output("local", "sess-proj", INSTANCE, "deployment finished successfully\n")
    store.ensure_meta("local", "sess-other", make_instance_id(session_id="$9", pane_id="%9", created_epoch=999),
                      cwd=str(tmp_path / "unrelated"))
    store.append_output("local", "sess-other", make_instance_id(session_id="$9", pane_id="%9", created_epoch=999),
                        "deployment finished successfully\n")
    results = store.search("deployment finished", project="quan_ly_ban_hang")
    assert len(results) == 1
    assert results[0]["session_name"] == "sess-proj"


def test_search_filtered_by_node_and_session_name(store, tmp_path):
    store.ensure_meta("node-a", "sess-x", INSTANCE, cwd=str(tmp_path))
    store.append_output("node-a", "sess-x", INSTANCE, "unique-marker-alpha\n")
    store.ensure_meta("node-b", "sess-x", INSTANCE, cwd=str(tmp_path))
    store.append_output("node-b", "sess-x", INSTANCE, "unique-marker-alpha\n")
    results = store.search("unique-marker-alpha", node_id="node-a")
    assert all(r["node_id"] == "node-a" for r in results)
    assert len(results) == 1


def test_missing_session_history_is_empty_not_an_error(store):
    assert store.get_meta("local", "never-existed") is None
    assert store.timeline("local", "never-existed") == []
    assert store.recovery_brief("local", "never-existed") is None


def test_recovery_brief_structure_never_claims_process_restored(store, tmp_path):
    store.ensure_meta("local", "sess-recover", INSTANCE, cwd=str(tmp_path),
                      agent_type="claude", backend_type="tmux", lifecycle_state="KILLED")
    store.append_output("local", "sess-recover", INSTANCE, "important context line\n")
    store.add_checkpoint("local", "sess-recover", INSTANCE, kind=CHECKPOINT_MANUAL,
                        summary="manual checkpoint before kill")
    brief = store.recovery_brief("local", "sess-recover")
    assert brief is not None
    assert brief["recovered_process"] is False
    assert brief["checkpoint"]["summary"] == "manual checkpoint before kill"
    assert "important context line" in brief["recovery_brief_text"]
    assert brief["untrusted_output"] is True


def test_compaction_triggers_past_retention_cap_and_leaves_a_checkpoint(store, tmp_path):
    store.ensure_meta("local", "sess-compact", INSTANCE, cwd=str(tmp_path))
    for i in range(15):  # cap is 10 (fixture)
        store.append_output("local", "sess-compact", INSTANCE, f"line {i}\n")
    meta = store.get_meta("local", "sess-compact")
    timeline = store.timeline("local", "sess-compact", INSTANCE, limit=100)
    assert len(timeline) <= 10
    # The newest content must survive compaction -- only the OLDEST rolls off.
    assert timeline[-1].text == "line 14\n"
    checkpoint = store.last_checkpoint("local", "sess-compact", INSTANCE)
    assert checkpoint is not None
    assert checkpoint["kind"] == "compaction"
    assert "rolled off retention" in checkpoint["summary"]


def test_prune_before_removes_old_session_instances_entirely(store, tmp_path):
    store.ensure_meta("local", "sess-old", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-old", INSTANCE, "ancient output\n")
    # Force last_captured_at into the past directly (real elapsed time in
    # a test would be absurd) -- same technique session_registry's own
    # tests use for time-based assertions.
    with store._connection() as connection:
        connection.execute(
            "UPDATE session_knowledge SET last_captured_at = '2000-01-01T00:00:00+00:00' "
            "WHERE node_id='local' AND session_name='sess-old'"
        )
    pruned = store.prune_before(older_than_days=1)
    assert pruned == 1
    assert store.get_meta("local", "sess-old") is None
    assert store.timeline("local", "sess-old", INSTANCE) == []


def test_restart_persistence_reopening_the_same_file(tmp_path):
    db_path = tmp_path / "persist.db"
    store1 = SessionKnowledgeStore(db_path)
    store1.ensure_meta("local", "sess-persist", INSTANCE, cwd=str(tmp_path))
    store1.append_output("local", "sess-persist", INSTANCE, "persisted output\n")
    store1.add_checkpoint("local", "sess-persist", INSTANCE, kind=CHECKPOINT_MANUAL, summary="cp1")

    store2 = SessionKnowledgeStore(db_path)  # simulates a process restart
    meta = store2.get_meta("local", "sess-persist")
    assert meta is not None
    assert meta.total_chunks == 1
    timeline = store2.timeline("local", "sess-persist", INSTANCE)
    assert [c.text for c in timeline] == ["persisted output\n"]
    assert store2.last_checkpoint("local", "sess-persist", INSTANCE)["summary"] == "cp1"


def test_provenance_backfilled_is_tracked_separately_from_live(store, tmp_path):
    store.ensure_meta("local", "sess-backfill", INSTANCE, cwd=str(tmp_path), provenance=PROVENANCE_BACKFILLED)
    store.append_output("local", "sess-backfill", INSTANCE, "old scrollback content\n", source=PROVENANCE_BACKFILLED)
    meta = store.get_meta("local", "sess-backfill")
    assert meta.provenance == PROVENANCE_BACKFILLED
    timeline = store.timeline("local", "sess-backfill", INSTANCE)
    assert timeline[0].source == PROVENANCE_BACKFILLED


def test_search_query_with_fts_operator_characters_is_treated_as_literal(store, tmp_path):
    store.ensure_meta("local", "sess-fts-safe", INSTANCE, cwd=str(tmp_path))
    store.append_output("local", "sess-fts-safe", INSTANCE, "some real output here\n")
    # Must not raise even though these characters have FTS5 syntax
    # meaning -- treated as literal text to search for, never parsed as a
    # column filter or boolean query.
    assert store.search('col:"weird" OR NEAR(x y)') == []


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
