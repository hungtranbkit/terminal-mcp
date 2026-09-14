"""The worker half: what QueueEngine._dispatch actually does with a
runbook lookup.

Drives the REAL QueueEngine against a REAL QueueStore (real SQLite, real
status transitions, real queue_events rows) with the same FakeOps posture
tests/test_queue_engine.py established -- the point of these tests is the
integration contract (what reaches a worker, what lands in the evidence
trail, and what happens when the registry is absent), which a mocked
engine would not exercise.

SAFETY: every session name here is a disposable fixture -- never
`window`/`window2`.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from terminal_mcp.coordinator import CoordinatorGate, RepoEvidence
from terminal_mcp.queue_engine import QueueEngine, build_dispatch_text
from terminal_mcp.queue_store import QueueStore, RUNNING
from terminal_mcp.runbook_registry import (
    SCHEMA_VERSION, STATUS_UNAVAILABLE, LookupKey, RunbookLookup, RunbookRegistry, runbook_path,
)


class FakeOps:
    """Records every send so a test can assert on the exact text a worker
    would have received."""

    def __init__(self):
        self.sent: list[dict] = []

    def terminal_status(self, session):
        return {"state": "IDLE", "node_id": "local", "cwd": "/repo/a"}

    def terminal_tail(self, session, lines=None):
        return {"output": ""}

    def terminal_send_text(self, session, text, press_enter=False, dry_run=False, **kwargs):
        self.sent.append({"session": session, "text": text, "idempotency_key": kwargs.get("idempotency_key")})
        return {"sent": True, "delivery_state": "SUBMIT_CONFIRMED", "node_id": "local"}


@pytest.fixture
def store(tmp_path):
    return QueueStore(tmp_path / "queue.db")


@pytest.fixture
def ops():
    return FakeOps()


def _always_ready_gate():
    return CoordinatorGate(evidence_collector=lambda cwd: RepoEvidence(branch="main", head="x", clean=True,
                                                                       status_lines=()))


PROMPT = "please do the real work carefully and completely"


def _make_task(store, *, metadata=None, session="lane-a"):
    (task_id,) = store.set_tasks(session, [{"prompt": PROMPT, "metadata": metadata or {}}])
    return task_id


def _build_registry(tmp_path, entries, *, revision=3, schema_version=SCHEMA_VERSION):
    path = runbook_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": schema_version, "revision": revision, "runbooks": entries}),
                    encoding="utf-8")
    return RunbookRegistry(path)


def _entry(entry_id="tunnel_down", **overrides):
    entry = {
        "id": entry_id, "title": "Restore the Cloudflare tunnel", "version": 5,
        "source": "docs/CONTROLLER_RUNBOOK.md#tunnel-down",
        "summary": "Check cloudflared status before touching DNS.",
        "match": {"failure_fingerprints": ["cf_tunnel_502"]},
    }
    entry.update(overrides)
    return entry


def _dispatch(engine, session="lane-a"):
    """claim -> coordinator review -> dispatch, the real three-tick path."""
    assert engine.tick(session).action == "CLAIMED"
    assert engine.tick(session).action == "COORDINATOR_READY"
    return engine.tick(session)


def _runbook_events(store, session="lane-a"):
    return [event for event in store.list_events(session) if event["event_type"].startswith("RUNBOOK_")]


# -- legacy worker behaviour ----------------------------------------------

def test_an_engine_with_no_registry_dispatches_byte_for_byte_what_it_always_did(store, ops):
    """The whole feature must be inert for a lane that never opted in.
    Asserted against build_dispatch_text's own output rather than a
    hand-copied string, so this stays true if the wrapper legitimately
    changes for some other reason."""
    task_id = _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate())  # no runbooks=
    assert _dispatch(engine).action == "DISPATCHED"

    # attempt_count was bumped by the DISPATCHING transition, so rebuild
    # against the pre-dispatch attempt the engine actually used -- that
    # makes this a full-string equality check, not a prefix check.
    task = store.get_task(task_id)
    pre_dispatch = dataclasses.replace(task, attempt_count=task.attempt_count - 1)
    expected = build_dispatch_text(pre_dispatch, nonce=task.verification_nonce)
    assert ops.sent[0]["text"] == expected
    assert "Runbook reference" not in ops.sent[0]["text"]
    assert "TERMINAL_MCP_COMPLETION" in ops.sent[0]["text"]
    assert _runbook_events(store) == []


def test_a_task_with_no_lookup_keys_records_no_event_even_with_a_registry(store, ops, tmp_path):
    """A legacy task under a registry-enabled controller stays silent --
    nothing was asked, so a miss would be noise, not evidence."""
    _make_task(store, metadata={})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=_build_registry(tmp_path, [_entry()]))
    assert _dispatch(engine).action == "DISPATCHED"
    assert "Runbook reference" not in ops.sent[0]["text"]
    assert _runbook_events(store) == []


# -- hit ------------------------------------------------------------------

def test_a_hit_attaches_an_advisory_reference_without_touching_the_prompt(store, ops, tmp_path):
    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=_build_registry(tmp_path, [_entry()]))
    assert _dispatch(engine).action == "DISPATCHED"

    text = ops.sent[0]["text"]
    # The task's own prompt is still first and verbatim, and the
    # completion protocol is untouched.
    assert text.startswith(PROMPT)
    assert "TERMINAL_MCP_COMPLETION" in text
    # The reference is a POINTER, marked advisory.
    assert "Runbook reference (ADVISORY" in text
    assert "id=tunnel_down version=5" in text
    assert "docs/CONTROLLER_RUNBOOK.md#tunnel-down" in text
    assert "the task above wins" in text


def test_a_hit_lands_in_the_evidence_trail_with_source_and_version(store, ops, tmp_path):
    """hit/miss/source/version tracking reuses queue_events -- no new
    table, no migration, one place to reconstruct what a worker was told."""
    task_id = _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=_build_registry(tmp_path, [_entry()], revision=11))
    _dispatch(engine)

    events = _runbook_events(store)
    assert [event["event_type"] for event in events] == ["RUNBOOK_HIT"]
    assert events[0]["task_id"] == task_id
    payload = json.loads(events[0]["metadata"])
    assert payload["status"] == "HIT"
    assert payload["matched_on"] == "failure_fingerprint"
    assert payload["runbook"]["id"] == "tunnel_down"
    assert payload["runbook"]["version"] == 5
    assert payload["registry_revision"] == 11
    assert payload["registry_schema_version"] == SCHEMA_VERSION
    assert payload["registry_source"].endswith("runbooks.json")


def test_several_candidates_are_recorded_alongside_the_one_that_was_chosen(store, ops, tmp_path):
    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(), runbooks=_build_registry(tmp_path, [
        _entry("rb_old", version=1), _entry("rb_new", version=9),
    ]))
    _dispatch(engine)
    payload = json.loads(_runbook_events(store)[0]["metadata"])
    assert payload["runbook"]["id"] == "rb_new"
    assert payload["candidates"] == ["rb_new", "rb_old"]


# -- destructive runbooks stay advisory -----------------------------------

def test_a_destructive_runbook_is_referenced_with_a_warning_and_never_executed(store, ops, tmp_path):
    """Retrieval is advisory. The engine has no execute path at all, so
    the proof is that the ONLY thing that happened is the one ordinary
    dispatch send, and that the notice says not to run the steps."""
    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=_build_registry(tmp_path, [_entry(destructive=True,
                                                                   summary="Drop the replica and re-sync.")]))
    assert _dispatch(engine).action == "DISPATCHED"

    assert len(ops.sent) == 1  # one dispatch; nothing else was sent anywhere
    text = ops.sent[0]["text"]
    assert "DESTRUCTIVE" in text
    assert "Do NOT execute them from this reference" in text
    assert "Confirm with the operator first" in text


# -- miss / stale / unavailable -------------------------------------------

def test_a_miss_dispatches_normally_and_is_recorded(store, ops, tmp_path):
    _make_task(store, metadata={"failure_fingerprint": "something_nobody_wrote_a_runbook_for"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=_build_registry(tmp_path, [_entry()]))
    assert _dispatch(engine).action == "DISPATCHED"
    assert "Runbook reference" not in ops.sent[0]["text"]
    assert [event["event_type"] for event in _runbook_events(store)] == ["RUNBOOK_MISS"]


def test_a_stale_registry_is_recorded_as_stale_and_attaches_nothing(store, ops, tmp_path):
    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=_build_registry(tmp_path, [_entry()], schema_version=SCHEMA_VERSION + 1))
    assert _dispatch(engine).action == "DISPATCHED"
    assert "Runbook reference" not in ops.sent[0]["text"]
    events = _runbook_events(store)
    assert [event["event_type"] for event in events] == ["RUNBOOK_STALE"]
    assert "newer than supported" in json.loads(events[0]["metadata"])["reason"]


def test_an_unavailable_registry_never_blocks_a_dispatch(store, ops, tmp_path):
    """The fallback that matters: a controller configured for a registry
    that was never deployed still dispatches every task, exactly as a
    pre-registry build would."""
    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=RunbookRegistry(runbook_path(tmp_path)))  # file never created
    result = _dispatch(engine)

    assert result.action == "DISPATCHED"
    assert store.get_task(result.task_id).status == RUNNING
    assert ops.sent[0]["text"].startswith(PROMPT)
    assert "Runbook reference" not in ops.sent[0]["text"]
    events = _runbook_events(store)
    assert [event["event_type"] for event in events] == ["RUNBOOK_UNAVAILABLE"]
    assert json.loads(events[0]["metadata"])["status"] == STATUS_UNAVAILABLE


def test_a_registry_that_raises_outright_cannot_fail_a_dispatch(store, ops):
    """Belt and braces: RunbookRegistry.lookup already never raises, so
    this substitutes one that does, proving the engine's own boundary
    holds even if that guarantee is ever broken."""

    class ExplodingRegistry:
        def lookup(self, key):
            raise RuntimeError("registry backend on fire")

    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(), runbooks=ExplodingRegistry())
    result = _dispatch(engine)

    assert result.action == "DISPATCHED"
    assert store.get_task(result.task_id).status == RUNNING
    assert ops.sent[0]["text"].startswith(PROMPT)
    assert "Runbook reference" not in ops.sent[0]["text"]


def test_a_registry_returning_a_hit_without_a_runbook_is_treated_as_no_attachment(store, ops):
    """A malformed result object must degrade, not crash the wrapper."""

    class WeirdRegistry:
        def lookup(self, key):
            return RunbookLookup(status="HIT", runbook=None, reason="inconsistent")

    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502"})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(), runbooks=WeirdRegistry())
    assert _dispatch(engine).action == "DISPATCHED"
    assert "Runbook reference" not in ops.sent[0]["text"]


# -- the lookup key the engine actually builds ----------------------------

def test_the_engine_looks_up_with_the_keys_from_the_tasks_own_metadata(store, ops, tmp_path):
    seen: list[LookupKey] = []

    class RecordingRegistry:
        def __init__(self, inner):
            self.inner = inner

        def lookup(self, key):
            seen.append(key)
            return self.inner.lookup(key)

    _make_task(store, metadata={"failure_fingerprint": "cf_tunnel_502", "task_class": "incident.tunnel",
                                "context_tags": ["node:hp-linux"]})
    engine = QueueEngine(store, ops, coordinator=_always_ready_gate(),
                         runbooks=RecordingRegistry(_build_registry(tmp_path, [_entry()])))
    _dispatch(engine)

    assert len(seen) == 1
    assert seen[0].failure_fingerprint == "cf_tunnel_502"
    assert seen[0].task_class == "incident.tunnel"
    assert seen[0].context_tags == ("node:hp-linux",)
