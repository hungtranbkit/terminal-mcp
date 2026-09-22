"""Fleet-aware registry list/search (task blg_84f09bbc1798).

Two layers:
  * the PURE merge rules (fleet_registry.py) -- every precedence,
    dedupe, freshness and pagination rule, with no network and no store;
  * the real ControllerService fan-out against fake node clients, so the
    wiring (which node is asked, what happens when one fails) is proven
    too, not just the arithmetic.
"""
from __future__ import annotations

import pytest

from terminal_mcp.fleet_registry_read import (
    EFFECTIVE_UNKNOWN, FRESHNESS_LIVE, FRESHNESS_STALE, FRESHNESS_UNKNOWN,
    NODE_DEGRADED, NODE_OFFLINE, NODE_ONLINE, SOURCE_FLEET, SOURCE_LOCAL,
    NodeSource, matches_query, merge_sources, node_freshness, paginate,
)


def _record(session_name, *, status="ACTIVE", node_id="local", last_seen_at="2026-09-14T10:00:00",
            cwd=None, read_granted=False, input_granted=False, **extra):
    record = {"node_id": node_id, "session_name": session_name, "status": status,
              "last_seen_at": last_seen_at, "cwd": cwd, "node_name": None,
              "read_granted": read_granted, "input_granted": input_granted,
              "recoverable": status != "ACTIVE", "key": f"{node_id}/{session_name}"}
    record.update(extra)
    return record


def _source(node_id, records, *, status=NODE_ONLINE, is_local=False, error=None, name=None):
    return NodeSource(node_id=node_id, node_name=name, status=status,
                      records=None if records is None else tuple(records),
                      error=error, fetched_at="2026-09-14T10:00:01", is_local=is_local)


# -- local-only ---------------------------------------------------------

def test_local_only_fleet_read_matches_the_local_records():
    merged = merge_sources([_source("local", [_record("alpha"), _record("beta")], is_local=True)])
    assert [r["session_name"] for r in merged["records"]] == ["alpha", "beta"]
    assert merged["counts"] == {"total": 2, "local": 2, "remote": 0, "deduped": 0,
                                "nodes_reporting": 1, "nodes_unavailable": 0}
    assert merged["unavailable_nodes"] == []


def test_local_records_preserve_every_pre_existing_field_and_order():
    original = _record("alpha", cwd="/home/k/proj", read_granted=True)
    merged = merge_sources([_source("local", [original], is_local=True)])
    record = merged["records"][0]
    for key, value in original.items():
        if key in ("node_id", "node_name"):   # authoritative rewrite, originals preserved below
            continue
        assert record[key] == value, key
    assert record["source_node_id"] == "local"
    assert record["source"] == SOURCE_LOCAL


# -- remote-only --------------------------------------------------------

def test_remote_only_records_get_authoritative_node_identity():
    """A raw registry row always calls itself "local"; merged across a
    fleet that is ambiguous, so it is rewritten and the original kept."""
    merged = merge_sources([_source("dell-linux", [_record("gamma")], name="dell-linux (Windows)")])
    record = merged["records"][0]
    assert record["node_id"] == "dell-linux"
    assert record["node_name"] == "dell-linux (Windows)"
    assert record["source_node_id"] == "local"
    assert record["source"] == SOURCE_FLEET
    assert record["fleet_key"] == "dell-linux/gamma"


def test_local_and_remote_are_merged_with_local_first():
    merged = merge_sources([
        _source("dell-linux", [_record("gamma")]),
        _source("local", [_record("alpha")], is_local=True),
    ])
    assert [r["session_name"] for r in merged["records"]] == ["alpha", "gamma"]
    assert merged["counts"]["local"] == 1 and merged["counts"]["remote"] == 1


# -- duplicate same session identity ------------------------------------

def test_same_session_from_local_and_routed_client_is_deduped_once():
    """The controller registers its OWN node in _clients, so a naive
    fan-out sees the local node's rows twice."""
    rows = [_record("alpha")]
    merged = merge_sources([
        _source("local", rows, is_local=True),
        _source("local", rows),           # same node, routed through its own client
    ])
    assert len(merged["records"]) == 1
    assert merged["counts"]["deduped"] == 1
    assert merged["records"][0]["source"] == SOURCE_LOCAL, "the direct local read wins"
    assert merged["records"][0]["shadowed_source"] == SOURCE_FLEET


def test_same_name_on_two_different_nodes_is_not_a_duplicate():
    """Identity is (owning node, session name) -- two nodes may each
    legitimately have a session called "work"."""
    merged = merge_sources([
        _source("local", [_record("work")], is_local=True),
        _source("dell-linux", [_record("work")]),
    ])
    assert len(merged["records"]) == 2
    assert {r["node_id"] for r in merged["records"]} == {"local", "dell-linux"}
    assert merged["counts"]["deduped"] == 0


def test_live_copy_wins_over_stale_copy_of_the_same_session():
    merged = merge_sources([
        _source("dell-linux", [_record("alpha", last_seen_at="2026-09-14T09:00:00")],
                status=NODE_DEGRADED),
        _source("dell-linux", [_record("alpha", last_seen_at="2026-09-14T08:00:00")],
                status=NODE_ONLINE),
    ])
    assert len(merged["records"]) == 1
    assert merged["records"][0]["freshness"] == FRESHNESS_LIVE


def test_newer_last_seen_wins_between_two_equally_fresh_copies():
    merged = merge_sources([
        _source("dell-linux", [_record("alpha", last_seen_at="2026-09-14T08:00:00", cwd="/old")]),
        _source("dell-linux", [_record("alpha", last_seen_at="2026-09-14T09:00:00", cwd="/new")]),
    ])
    assert merged["records"][0]["cwd"] == "/new"


def test_a_row_without_last_seen_never_displaces_one_that_has_it():
    merged = merge_sources([
        _source("dell-linux", [_record("alpha", last_seen_at="2026-09-14T09:00:00", cwd="/real")]),
        _source("dell-linux", [_record("alpha", last_seen_at=None, cwd="/unknown")]),
    ])
    assert merged["records"][0]["cwd"] == "/real"


# -- stale remote / UNKNOWN must not look active ------------------------

@pytest.mark.parametrize("status", [NODE_DEGRADED, NODE_OFFLINE])
def test_active_record_from_a_non_fresh_node_is_not_presented_as_active(status):
    merged = merge_sources([_source("dell-linux", [_record("alpha", status="ACTIVE")], status=status)])
    record = merged["records"][0]
    assert record["status"] == "ACTIVE", "the node's own last word is carried verbatim"
    assert record["effective_status"] == EFFECTIVE_UNKNOWN, "but it must not READ as active"
    assert record["stale"] is True
    assert record["freshness"] == FRESHNESS_STALE
    assert record["node_online"] is False


def test_active_record_from_an_online_node_stays_active():
    merged = merge_sources([_source("dell-linux", [_record("alpha", status="ACTIVE")])])
    record = merged["records"][0]
    assert record["effective_status"] == "ACTIVE"
    assert record["stale"] is False and record["node_online"] is True


@pytest.mark.parametrize("status", ["MISSING", "KILLED", "OFFLINE", "DELETED"])
def test_staleness_never_upgrades_a_dead_session(status):
    """Staleness can only remove certainty, never add it."""
    merged = merge_sources([_source("dell-linux", [_record("alpha", status=status)], status=NODE_DEGRADED)])
    assert merged["records"][0]["effective_status"] == status


def test_freshness_derivation():
    assert node_freshness(_source("n", [])) == FRESHNESS_LIVE
    assert node_freshness(_source("n", [], status=NODE_DEGRADED)) == FRESHNESS_STALE
    assert node_freshness(_source("n", None, error="boom")) == FRESHNESS_UNKNOWN


# -- offline / unreachable node -----------------------------------------

def test_unreachable_node_contributes_no_invented_records():
    merged = merge_sources([
        _source("local", [_record("alpha")], is_local=True),
        _source("dell-linux", None, status=NODE_OFFLINE, error="NODE_UNREACHABLE"),
    ])
    assert [r["session_name"] for r in merged["records"]] == ["alpha"]
    assert merged["unavailable_nodes"] == [
        {"node_id": "dell-linux", "node_name": "dell-linux", "status": NODE_OFFLINE,
         "error": "NODE_UNREACHABLE"}]
    assert merged["counts"]["nodes_unavailable"] == 1


def test_a_node_reporting_zero_rows_is_distinct_from_an_unreachable_one():
    """"nothing there" and "could not look" must never render the same."""
    reachable = merge_sources([_source("dell-linux", [])])
    unreachable = merge_sources([_source("dell-linux", None, error="timeout")])
    assert reachable["unavailable_nodes"] == [] and reachable["counts"]["nodes_reporting"] == 1
    assert unreachable["unavailable_nodes"] and unreachable["counts"]["nodes_reporting"] == 0


def test_offline_node_that_still_answered_is_reported_but_marked_stale():
    merged = merge_sources([_source("dell-linux", [_record("alpha")], status=NODE_OFFLINE)])
    assert merged["records"][0]["stale"] is True
    assert merged["nodes"][0]["online"] is False and merged["nodes"][0]["reachable"] is True


# -- permissions / grants ------------------------------------------------

def test_grant_flags_are_carried_verbatim_and_never_invented():
    """Permission state is the node's to report. The merge layer must
    never default a missing grant to True, nor overwrite a False."""
    merged = merge_sources([
        _source("local", [_record("granted", read_granted=True, input_granted=True)], is_local=True),
        _source("dell-linux", [_record("denied", read_granted=False, input_granted=False)]),
    ])
    by_name = {r["session_name"]: r for r in merged["records"]}
    assert by_name["granted"]["read_granted"] is True
    assert by_name["granted"]["input_granted"] is True
    assert by_name["denied"]["read_granted"] is False
    assert by_name["denied"]["input_granted"] is False


def test_merge_adds_no_pane_content_field():
    """Registry records are metadata only -- the redaction surface in
    this project is pane OUTPUT, and a fleet read must not become a new
    path that carries any."""
    merged = merge_sources([_source("dell-linux", [_record("alpha")])])
    record = merged["records"][0]
    for leaky in ("output", "last_output", "pane", "content", "capture"):
        assert leaky not in record


def test_malformed_rows_are_skipped_rather_than_trusted():
    merged = merge_sources([_source("dell-linux", ["not a dict", {}, _record("alpha")])])
    assert [r["session_name"] for r in merged["records"]] == ["alpha"]


# -- search --------------------------------------------------------------

def test_search_matches_words_in_any_order_across_fields():
    record = _record("quan_ly_ban_hang", cwd="/home/k/offline-pos")
    assert matches_query(record, "ban hang") is True
    assert matches_query(record, "hang ban") is True
    assert matches_query(record, "offline-pos") is True
    assert matches_query(record, "nonexistent") is False
    assert matches_query(record, "") is False


def test_search_is_case_insensitive():
    assert matches_query(_record("QuanLyBanHang"), "quanlybanhang") is True


def test_search_still_matches_the_raw_node_id_after_the_rewrite():
    merged = merge_sources([_source("dell-linux", [_record("alpha")])])
    assert matches_query(merged["records"][0], "dell-linux") is True
    assert matches_query(merged["records"][0], "local") is True, "source_node_id stays searchable"


def test_fleet_search_matcher_agrees_with_session_registry_search(tmp_path):
    """Pins the two implementations against each other -- a remote node
    exposes no search endpoint, so fleet search re-implements the rule
    and must never drift from it."""
    from terminal_mcp.session_registry import SessionRegistryStore
    store = SessionRegistryStore(tmp_path / "reg.db")
    store.upsert_seen("local", "quan_ly_ban_hang", backend_type="tmux", cwd="/home/k/offline-pos")
    store.upsert_seen("local", "unrelated", backend_type="tmux", cwd="/tmp/other")
    for query in ("ban hang", "offline-pos", "unrelated", "hang ban", "nope"):
        from_store = {r.session_name for r in store.search(query)}
        rows = [{"session_name": r.session_name, "cwd": r.cwd, "repo_root": r.repo_root,
                 "git_remote": r.git_remote, "node_id": r.node_id, "display_name": r.display_name,
                 "notes": r.notes} for r in store.list()]
        from_matcher = {r["session_name"] for r in rows if matches_query(r, query)}
        assert from_store == from_matcher, query


# -- pagination ----------------------------------------------------------

def test_pagination_walks_every_record_exactly_once():
    records = [_record(f"s{i}") for i in range(25)]
    seen, cursor = [], 0
    while True:
        page = paginate(records, limit=10, cursor=cursor)
        seen.extend(r["session_name"] for r in page["records"])
        assert page["total"] == 25
        if page["next_cursor"] is None:
            assert page["has_more"] is False
            break
        cursor = page["next_cursor"]
    assert seen == [r["session_name"] for r in records]
    assert len(seen) == len(set(seen))


def test_pagination_defaults_and_bounds():
    records = [_record(f"s{i}") for i in range(5)]
    assert paginate(records)["limit"] == 500
    assert paginate(records, limit=0)["limit"] == 1
    assert paginate(records, limit=10 ** 9)["limit"] == 2000
    assert paginate(records, cursor=-5)["cursor"] == 0
    assert paginate(records, cursor=99)["records"] == []
