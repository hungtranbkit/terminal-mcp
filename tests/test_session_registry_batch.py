"""Batched reconcile: `SessionRegistryStore.upsert_seen_many`.

WHY THIS EXISTS. `_reconcile_session_registry` ran `upsert_seen` (three
sqlite connections: two `get` reads and one write) plus
`mark_drop_events_recovered_for` (a fourth) for every live session, on
EVERY fleet listing -- and the task router's candidate collection is a
fleet listing. Measured live on hp-linux with 39 sessions that was 156
connections per call and 11.9-15.8s of wall clock, which was the largest
single component of project_start latency; batched it is 0.065-0.11s.

The batch is only allowed to be faster, never different, so most of this
file is equivalence: the same rows, the same preserve-what-we-were-not-
told rules, the same drop-event close-out.
"""
from __future__ import annotations

from terminal_mcp.session_registry import SessionRegistryStore

# Generated per row and therefore never equal between two independent
# writes; every other column must match exactly.
_NOT_COMPARABLE = {"stable_session_id", "created_at", "last_seen_at",
                   "last_activity_at", "grant_updated_at"}


def _entry(name: str, **overrides):
    row = {"session_name": name, "backend_type": "tmux", "cwd": None,
           "agent_type": "shell", "launcher_type": "shell",
           "read_granted": False, "input_granted": False, "binding_names": ()}
    row.update(overrides)
    return row


def _comparable(record):
    return {key: value for key, value in record.__dict__.items() if key not in _NOT_COMPARABLE}


def test_batch_writes_the_same_row_the_per_session_path_does(tmp_path):
    one = SessionRegistryStore(tmp_path / "one.db")
    many = SessionRegistryStore(tmp_path / "many.db")
    entries = [_entry(f"s{index}", cwd=str(tmp_path), binding_names=("bind",))
               for index in range(5)]

    for entry in entries:
        one.upsert_seen("local", entry["session_name"], backend_type="tmux", cwd=str(tmp_path),
                        agent_type="shell", launcher_type="shell", binding_names=("bind",))
    written = many.upsert_seen_many("local", entries)

    assert set(written) == {entry["session_name"] for entry in entries}
    for entry in entries:
        name = entry["session_name"]
        assert _comparable(one.get("local", name)) == _comparable(many.get("local", name))
        assert many.get("local", name).status == "ACTIVE"


def test_batch_preserves_fields_this_pass_was_not_told_about(tmp_path):
    """A discovery pass never clears what an explicit create recorded."""
    store = SessionRegistryStore(tmp_path / "registry.db")
    store.upsert_seen("local", "resumable", backend_type="tmux", cwd=str(tmp_path),
                      agent_type="claude", launch_command="claude --resume",
                      conversation_id="conv-1", worktree_path=str(tmp_path),
                      created_by_controller=True)

    # The reconcile pass knows none of those things and must not erase them.
    store.upsert_seen_many("local", [_entry("resumable", cwd=str(tmp_path), agent_type="claude")])

    record = store.get("local", "resumable")
    assert record.launch_command == "claude --resume"
    assert record.conversation_id == "conv-1"
    assert record.worktree_path == str(tmp_path)
    # created_by_controller latches TRUE: a discovery pass can never disown
    # a session this controller actually created.
    assert record.created_by_controller is True


def test_batch_revives_a_missing_session_and_closes_its_drop_event(tmp_path):
    store = SessionRegistryStore(tmp_path / "registry.db")
    store.upsert_seen("local", "flaky", backend_type="tmux", agent_type="shell")
    store.mark_missing("local", set())
    store.record_drop_event("local", "flaky", "session_missing")
    assert store.get("local", "flaky").status == "MISSING"
    assert [event for event in store.list_drop_events() if not event["recovered"]]

    store.upsert_seen_many("local", [_entry("flaky")])

    assert store.get("local", "flaky").status == "ACTIVE"
    assert not [event for event in store.list_drop_events() if not event["recovered"]]


def test_batch_uses_one_connection_for_the_whole_pass(tmp_path):
    """The point of the batch. 39 sessions must not mean 156 connections."""
    store = SessionRegistryStore(tmp_path / "registry.db")
    entries = [_entry(f"s{index}") for index in range(39)]
    opened = 0
    original = store._connect

    def counting_connect():
        nonlocal opened
        opened += 1
        return original()

    store._connect = counting_connect
    store.upsert_seen_many("local", entries)
    assert opened == 1

    opened = 0
    for entry in entries:
        store.upsert_seen("local", entry["session_name"])
        store.mark_drop_events_recovered_for("local", entry["session_name"])
    # The shape this batch replaces, asserted so the saving is not silently
    # given back: four connections per session.
    assert opened == 4 * len(entries)


def test_empty_batch_touches_nothing(tmp_path):
    store = SessionRegistryStore(tmp_path / "registry.db")
    assert store.upsert_seen_many("local", []) == {}
    assert store.list() == []
