"""Runbook Registry -- retrieval, precedence, degradation, redaction.

Every test below drives the registry through a REAL file on disk (a real
`.terminal-mcp/runbooks.json` under tmp_path, real JSON parsing, real
stat-based cache invalidation) rather than monkeypatching the loader --
the failure modes that matter here are file-shaped (absent, truncated,
hand-edited into invalid JSON, written by a newer build), and a mocked
loader would prove none of them.

The worker-facing half (what a dispatch actually does with a hit, a miss,
and an unavailable registry) is tests/test_runbook_worker_integration.py.
"""
from __future__ import annotations

import json

import pytest

from terminal_mcp.runbook_registry import (
    MATCH_CONTEXT_TAG, MATCH_FINGERPRINT, MATCH_TASK_CLASS, SCHEMA_VERSION, STATUS_HIT, STATUS_MISS,
    STATUS_STALE, STATUS_UNAVAILABLE, LookupKey, RunbookRegistry, lookup_key_for_task, runbook_path,
)


def _write_registry(tmp_path, document, *, raw: str | None = None):
    path = runbook_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(document, indent=2), encoding="utf-8")
    return path


def _entry(entry_id, **overrides):
    entry = {
        "id": entry_id,
        "title": f"Runbook {entry_id}",
        "version": 1,
        "source": f"docs/RUNBOOKS.md#{entry_id}",
        "summary": "Do the documented thing.",
        "match": {"failure_fingerprints": [f"fp_{entry_id}"]},
    }
    entry.update(overrides)
    return entry


def _document(*entries, **overrides):
    document = {"schema_version": SCHEMA_VERSION, "revision": 7, "runbooks": list(entries)}
    document.update(overrides)
    return document


@pytest.fixture
def registry(tmp_path):
    def build(document, *, raw: str | None = None):
        _write_registry(tmp_path, document, raw=raw)
        return RunbookRegistry(runbook_path(tmp_path))
    return build


# -- exact match ----------------------------------------------------------

def test_exact_fingerprint_match_returns_the_canonical_reference(registry):
    reg = registry(_document(_entry("tunnel_down", version=4)))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_tunnel_down"))
    assert result.status == STATUS_HIT
    assert result.hit is True
    assert result.matched_on == MATCH_FINGERPRINT
    assert result.runbook is not None
    assert result.runbook.id == "tunnel_down"
    assert result.runbook.version == 4
    assert result.runbook.source == "docs/RUNBOOKS.md#tunnel_down"
    # Provenance travels with the result so hit/miss telemetry can record
    # WHICH registry, at WHICH revision, answered.
    assert result.registry_revision == 7
    assert result.registry_schema_version == SCHEMA_VERSION
    assert result.registry_source.endswith("runbooks.json")


def test_task_class_and_context_tag_are_also_match_axes(registry):
    reg = registry(_document(
        _entry("by_class", match={"task_classes": ["incident.tunnel"]}),
        _entry("by_tag", match={"context_tags": ["node:hp-linux"]}),
    ))
    by_class = reg.lookup(LookupKey(task_class="incident.tunnel"))
    assert (by_class.status, by_class.matched_on, by_class.runbook.id) == (STATUS_HIT, MATCH_TASK_CLASS, "by_class")
    by_tag = reg.lookup(LookupKey(context_tags=("node:hp-linux",)))
    assert (by_tag.status, by_tag.matched_on, by_tag.runbook.id) == (STATUS_HIT, MATCH_CONTEXT_TAG, "by_tag")


def test_the_reference_never_carries_the_procedure_body(registry):
    """The body stays behind `source`. A runbook's steps can be long and
    can quote configuration; only the bounded fields travel."""
    reg = registry(_document(_entry("big", steps=["step one", "step two"], body="the whole procedure")))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_big"))
    surfaced = json.dumps(result.runbook.to_dict())
    assert "step one" not in surfaced
    assert "the whole procedure" not in surfaced


# -- no match -------------------------------------------------------------

def test_no_match_is_a_plain_miss(registry):
    reg = registry(_document(_entry("a"), _entry("b")))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_nothing_like_this"))
    assert result.status == STATUS_MISS
    assert result.hit is False
    assert result.runbook is None
    assert result.reason


def test_a_task_with_no_lookup_keys_is_a_miss_not_a_match(registry):
    """A legacy task nobody classified must never be handed a runbook by
    accident -- an empty key matches nothing, including a catch-all."""
    reg = registry(_document(_entry("catch_all", match={"context_tags": ["anything"]})))
    result = reg.lookup(LookupKey())
    assert result.status == STATUS_MISS
    assert result.runbook is None


def test_an_entry_is_credited_with_its_most_specific_matching_axis(registry):
    """An entry listing both a fingerprint and a broad tag matches on the
    fingerprint -- otherwise listing more axes would be a way to outrank a
    precise entry."""
    reg = registry(_document(_entry("both", match={"failure_fingerprints": ["fp_x"],
                                                   "context_tags": ["node:hp-linux"]})))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_x", context_tags=("node:hp-linux",)))
    assert result.matched_on == MATCH_FINGERPRINT


# -- stale / version mismatch ---------------------------------------------

def test_a_document_from_a_newer_build_is_stale_not_parsed_optimistically(registry):
    reg = registry(_document(_entry("future"), schema_version=SCHEMA_VERSION + 1))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_future"))
    assert result.status == STATUS_STALE
    assert result.runbook is None
    assert "newer than supported" in result.reason
    # The skew itself is reported, so an operator can see WHICH version.
    assert result.registry_schema_version == SCHEMA_VERSION + 1


def test_an_entry_from_a_newer_build_is_stale_when_it_was_the_only_candidate(registry):
    reg = registry(_document(_entry("future", schema_version=SCHEMA_VERSION + 1)))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_future"))
    assert result.status == STATUS_STALE
    assert result.runbook is None
    assert result.stale_ids == ("future",)


def test_a_stale_entry_never_hides_a_usable_one(registry):
    """STALE must not become a way for one bad entry to suppress a good
    match -- the usable candidate still wins, and the stale id is still
    reported alongside it."""
    reg = registry(_document(
        _entry("future", schema_version=SCHEMA_VERSION + 1, match={"failure_fingerprints": ["fp_shared"]}),
        _entry("usable", match={"failure_fingerprints": ["fp_shared"]}),
    ))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_shared"))
    assert result.status == STATUS_HIT
    assert result.runbook.id == "usable"
    assert result.stale_ids == ("future",)


def test_an_older_entry_schema_is_still_usable(registry):
    """Only NEWER is refused. An entry written by an older build is still
    honest about its own meaning, so it stays usable -- refusing it would
    make every registry upgrade a flag day."""
    reg = registry(_document(_entry("old", schema_version=SCHEMA_VERSION - 1)))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_old"))
    assert result.status == STATUS_HIT


# -- unavailable registry -------------------------------------------------

def test_a_missing_registry_is_unavailable_not_an_exception(tmp_path):
    reg = RunbookRegistry(runbook_path(tmp_path))  # never created
    result = reg.lookup(LookupKey(failure_fingerprint="fp_anything"))
    assert result.status == STATUS_UNAVAILABLE
    assert result.runbook is None
    assert "unreadable" in result.reason


def test_invalid_json_is_unavailable(registry):
    reg = registry(None, raw='{"runbooks": [ this is not json')
    result = reg.lookup(LookupKey(failure_fingerprint="fp_x"))
    assert result.status == STATUS_UNAVAILABLE
    assert "not valid JSON" in result.reason


def test_a_non_object_root_is_unavailable(registry):
    reg = registry(None, raw='["a", "list", "not", "an", "object"]')
    result = reg.lookup(LookupKey(failure_fingerprint="fp_x"))
    assert result.status == STATUS_UNAVAILABLE
    assert "not a JSON object" in result.reason


def test_a_document_without_a_runbooks_list_is_unavailable(registry):
    reg = registry({"schema_version": SCHEMA_VERSION, "revision": 1})
    result = reg.lookup(LookupKey(failure_fingerprint="fp_x"))
    assert result.status == STATUS_UNAVAILABLE
    assert "no 'runbooks' list" in result.reason


def test_a_path_that_is_a_directory_is_unavailable_not_an_exception(tmp_path):
    directory = tmp_path / ".terminal-mcp" / "runbooks.json"
    directory.mkdir(parents=True)
    result = RunbookRegistry(directory).lookup(LookupKey(failure_fingerprint="fp_x"))
    assert result.status == STATUS_UNAVAILABLE


# -- multiple candidates, deterministic choice ----------------------------

def test_the_most_specific_axis_wins(registry):
    reg = registry(_document(
        _entry("tagged", match={"context_tags": ["node:hp-linux"]}),
        _entry("classed", match={"task_classes": ["incident.tunnel"]}),
        _entry("fingerprinted", match={"failure_fingerprints": ["fp_precise"]}),
    ))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_precise", task_class="incident.tunnel",
                                  context_tags=("node:hp-linux",)))
    assert result.runbook.id == "fingerprinted"
    assert result.matched_on == MATCH_FINGERPRINT
    # All three were genuinely considered -- recorded for telemetry.
    assert result.candidates == ("classed", "fingerprinted", "tagged")


def test_within_one_axis_the_highest_version_wins(registry):
    reg = registry(_document(
        _entry("rb_v1", version=1, match={"failure_fingerprints": ["fp_shared"]}),
        _entry("rb_v9", version=9, match={"failure_fingerprints": ["fp_shared"]}),
        _entry("rb_v3", version=3, match={"failure_fingerprints": ["fp_shared"]}),
    ))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_shared"))
    assert result.runbook.id == "rb_v9"
    assert result.runbook.version == 9


def test_the_final_tiebreak_is_deterministic_and_independent_of_file_order(tmp_path):
    """Two workers reading the same registry must follow the same
    procedure. With axis and version tied, the choice falls to the lowest
    id -- NOT to whichever entry happens to appear first in the file."""
    entries = [
        _entry("zzz_last", version=2, match={"failure_fingerprints": ["fp_tie"]}),
        _entry("aaa_first", version=2, match={"failure_fingerprints": ["fp_tie"]}),
        _entry("mmm_middle", version=2, match={"failure_fingerprints": ["fp_tie"]}),
    ]
    chosen = set()
    for ordering in (entries, list(reversed(entries)), [entries[1], entries[2], entries[0]]):
        path = _write_registry(tmp_path, _document(*ordering))
        result = RunbookRegistry(path).lookup(LookupKey(failure_fingerprint="fp_tie"))
        chosen.add(result.runbook.id)
    assert chosen == {"aaa_first"}


def test_a_malformed_entry_is_skipped_without_losing_the_good_ones(registry):
    reg = registry(_document(
        "not-a-dict-at-all",
        {"title": "no id, so unusable", "match": {"failure_fingerprints": ["fp_shared"]}},
        _entry("good", match={"failure_fingerprints": ["fp_shared"]}),
    ))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_shared"))
    assert result.status == STATUS_HIT
    assert result.runbook.id == "good"


# -- secret handling ------------------------------------------------------

def test_credential_shaped_keys_never_reach_the_reference(registry):
    """Layer one: a field whose NAME looks like a credential is dropped
    before a ref is built, whatever it contains."""
    reg = registry(_document(_entry(
        "leaky", token="ghp_realisticlookingvalue", aws_secret_access_key="x", environment="prod-secrets",
    )))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_leaky"))
    surfaced = json.dumps(result.to_dict())
    assert "ghp_realisticlookingvalue" not in surfaced
    assert "prod-secrets" not in surfaced


def test_secret_shapes_inside_surfaced_strings_are_redacted(registry):
    """Layer two: the fields that DO travel are run through the project's
    own redaction before they can reach a dispatched prompt."""
    reg = registry(_document(_entry(
        "documented",
        summary="Re-auth with token=hunter2 then restart, using ghp_abcdefghijklmnopqrstuvwxyz0123456789.",
    )))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_documented"))
    assert "hunter2" not in result.runbook.summary
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in result.runbook.summary
    assert "REDACTED" in result.runbook.summary


def test_an_unbounded_summary_cannot_flood_a_dispatched_prompt(registry):
    reg = registry(_document(_entry("verbose", summary="x" * 5000)))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_verbose"))
    assert len(result.runbook.summary) <= 500


def test_a_missing_title_falls_back_to_the_id_rather_than_being_blank(registry):
    reg = registry(_document(_entry("untitled", title=None)))
    result = reg.lookup(LookupKey(failure_fingerprint="fp_untitled"))
    assert result.runbook.title == "untitled"


# -- freshness ------------------------------------------------------------

def test_an_edited_registry_is_picked_up_without_reconstructing_it(tmp_path):
    """The parsed document is cached on (mtime_ns, size), so a dispatch
    loop is not re-parsing per task -- but an operator fixing a runbook
    must not have to restart the controller to make it take effect."""
    path = _write_registry(tmp_path, _document(_entry("v1", version=1)))
    reg = RunbookRegistry(path)
    assert reg.lookup(LookupKey(failure_fingerprint="fp_v1")).runbook.version == 1
    _write_registry(tmp_path, _document(_entry("v1", version=2)))
    assert reg.lookup(LookupKey(failure_fingerprint="fp_v1")).runbook.version == 2


def test_a_registry_that_disappears_degrades_instead_of_serving_a_stale_cache(tmp_path):
    path = _write_registry(tmp_path, _document(_entry("gone")))
    reg = RunbookRegistry(path)
    assert reg.lookup(LookupKey(failure_fingerprint="fp_gone")).status == STATUS_HIT
    path.unlink()
    assert reg.lookup(LookupKey(failure_fingerprint="fp_gone")).status == STATUS_UNAVAILABLE


# -- lookup keys are read, never guessed ----------------------------------

class _Task:
    def __init__(self, metadata):
        self.metadata = metadata
        self.last_error = "tunnel returned HTTP 502 from cloudflared"
        self.title = "incident: tunnel down"


def test_lookup_keys_come_only_from_explicit_metadata(registry):
    """Classifying a failure is someone else's job. This never mines
    `last_error` or a title for a fingerprint -- a task nobody classified
    gets a clean miss instead of a guessed match."""
    key = lookup_key_for_task(_Task({}))
    assert key.empty is True
    assert key.failure_fingerprint is None

    key = lookup_key_for_task(_Task({"failure_fingerprint": "cf_tunnel_502", "task_class": "incident.tunnel",
                                     "context_tags": ["node:hp-linux", 7]}))
    assert key.failure_fingerprint == "cf_tunnel_502"
    assert key.task_class == "incident.tunnel"
    assert key.context_tags == ("node:hp-linux", "7")


def test_lookup_keys_tolerate_a_task_with_junk_metadata():
    assert lookup_key_for_task(_Task(None)).empty is True
    assert lookup_key_for_task(_Task({"failure_fingerprint": 42, "context_tags": "not-a-list"})).empty is True
    assert lookup_key_for_task(object()).empty is True
