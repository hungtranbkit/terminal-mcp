"""Fleet audit aggregation -- ordering, dedupe, cursor paging, payload.

Pure-function level: no controller, no registry, no network. The
scatter-gather half (real nodes, offline nodes, local-only deployments)
is tests/test_fleet_audit_controller.py.
"""
from __future__ import annotations

from terminal_mcp.fleet_audit import (
    DEFAULT_LIMIT, MAX_LIMIT, SCHEMA_VERSION, FleetAuditPage, NodeReport, attach_provenance, decode_cursor,
    encode_cursor, merge_pages,
)


def _row(row_id, timestamp, **overrides):
    row = {"id": row_id, "timestamp": timestamp, "action": "terminal_send_text", "session": "lane-a",
           "result": "SENT", "text_sha256": "abc123", "preview": "do the thing", "text_length": 12,
           "source_transport": "mcp", "server_version": "1.0.0"}
    row.update(overrides)
    return row


# -- provenance -----------------------------------------------------------

def test_provenance_overwrites_the_nodes_own_idea_of_its_node_id():
    """A remote node reports its rows as node_id "local" (its own point of
    view). Defaulting instead of overwriting is the documented bug that
    mislabeled every remote row -- so this must overwrite."""
    stamped = attach_provenance(_row(41, "2026-09-14T10:00:00", node_id="local"), "dell-5530")
    assert stamped["node_id"] == "dell-5530"
    assert stamped["node_row_id"] == 41
    assert stamped["audit_uid"] == "dell-5530:41"


def test_provenance_does_not_mutate_the_row_it_was_given():
    original = _row(7, "2026-09-14T10:00:00")
    attach_provenance(original, "m910")
    assert "audit_uid" not in original
    assert "node_id" not in original


def test_aggregation_adds_provenance_and_nothing_else():
    """No secret payload expansion: the aggregated field set is exactly
    the local row's fields plus the three provenance fields. In particular
    no raw prompt text is introduced -- audit rows carry a redacted
    preview and a sha256, and that is all that travels."""
    local = _row(1, "2026-09-14T10:00:00")
    page, _ = merge_pages({"local": [local]}, limit=10)
    assert set(page[0]) == set(local) | {"node_id", "node_row_id", "audit_uid"}
    assert "text" not in page[0]


# -- duplicate ids across nodes ------------------------------------------

def test_the_same_local_id_on_two_nodes_is_two_distinct_rows():
    """The core of the backlog item: input_audit.id is a per-node
    AUTOINCREMENT, so id 41 exists on every node. Deduping on `id` would
    silently destroy a real audit row."""
    page, _ = merge_pages({
        "hp": [_row(41, "2026-09-14T10:00:02")],
        "m910": [_row(41, "2026-09-14T10:00:01")],
    }, limit=10)
    assert [row["audit_uid"] for row in page] == ["hp:41", "m910:41"]
    assert len(page) == 2


def test_the_same_row_served_twice_is_emitted_once():
    """Idempotency in the other direction: one node answering twice (a
    retry, or one host registered under two ids pointing at it) must not
    double-count."""
    row = _row(41, "2026-09-14T10:00:02")
    page, _ = merge_pages({"hp": [row, dict(row)]}, limit=10)
    assert [r["audit_uid"] for r in page] == ["hp:41"]


# -- deterministic ordering ----------------------------------------------

def test_ordering_is_timestamp_desc_then_node_asc_then_row_desc():
    page, _ = merge_pages({
        "bbb": [_row(5, "2026-09-14T10:00:01")],
        "aaa": [_row(9, "2026-09-14T10:00:01"), _row(8, "2026-09-14T10:00:01")],
        "ccc": [_row(1, "2026-09-14T10:00:02")],
    }, limit=10)
    assert [r["audit_uid"] for r in page] == ["ccc:1", "aaa:9", "aaa:8", "bbb:5"]


def test_ordering_does_not_depend_on_which_node_answered_first():
    """Two controllers, or two runs with nodes responding in a different
    order, must produce identical pages."""
    nodes = {
        "hp": [_row(2, "2026-09-14T10:00:01"), _row(1, "2026-09-14T10:00:01")],
        "m910": [_row(2, "2026-09-14T10:00:01")],
        "dell": [_row(7, "2026-09-14T10:00:03")],
    }
    forward, _ = merge_pages(nodes, limit=10)
    reversed_order, _ = merge_pages(dict(reversed(list(nodes.items()))), limit=10)
    assert [r["audit_uid"] for r in forward] == [r["audit_uid"] for r in reversed_order]
    assert [r["audit_uid"] for r in forward] == ["dell:7", "hp:2", "hp:1", "m910:2"]


def test_a_row_with_a_missing_timestamp_sorts_last_instead_of_raising():
    page, _ = merge_pages({"hp": [_row(1, "2026-09-14T10:00:00"), _row(2, None)]}, limit=10)
    assert [r["audit_uid"] for r in page] == ["hp:1", "hp:2"]


# -- cursor / pagination --------------------------------------------------

def test_a_cursor_round_trips():
    row = attach_provenance(_row(41, "2026-09-14T10:00:02"), "hp")
    assert decode_cursor(encode_cursor(row)) == ("2026-09-14T10:00:02", "hp", 41)


def test_an_unparseable_cursor_restarts_from_the_newest_page():
    """A position hint, not a contract -- a bad cursor must not fail a
    whole fleet read."""
    assert decode_cursor("not-base64-at-all!!") is None
    assert decode_cursor("") is None
    assert decode_cursor(None) is None
    page, _ = merge_pages({"hp": [_row(1, "2026-09-14T10:00:00")]}, limit=10, cursor="garbage!!")
    assert [r["audit_uid"] for r in page] == ["hp:1"]


def test_paging_walks_every_row_exactly_once_with_no_gaps_or_repeats():
    nodes = {
        "hp": [_row(i, f"2026-09-14T10:00:{i:02d}") for i in range(9, 0, -1)],
        "m910": [_row(i, f"2026-09-14T10:00:{i:02d}") for i in range(9, 0, -1)],
    }
    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = merge_pages(nodes, limit=4, cursor=cursor)
        seen.extend(row["audit_uid"] for row in page)
        pages += 1
        if cursor is None or pages > 20:
            break
    assert len(seen) == 18
    assert len(set(seen)) == 18  # no repeats
    assert seen == sorted(set(seen), key=seen.index)  # order preserved across pages


def test_a_full_page_always_returns_a_cursor_and_a_short_page_never_does():
    """The merge cannot see past what it fetched, so "page was full" is
    the only honest signal that more may exist."""
    nodes = {"hp": [_row(i, f"2026-09-14T10:00:{i:02d}") for i in range(3, 0, -1)]}
    page, cursor = merge_pages(nodes, limit=3)
    assert len(page) == 3 and cursor is not None
    page, cursor = merge_pages(nodes, limit=5)
    assert len(page) == 3 and cursor is None


def test_the_page_after_the_last_row_is_empty_and_terminates():
    nodes = {"hp": [_row(1, "2026-09-14T10:00:01")]}
    page, cursor = merge_pages(nodes, limit=1)
    assert len(page) == 1 and cursor is not None
    page, cursor = merge_pages(nodes, limit=1, cursor=cursor)
    assert page == [] and cursor is None


def test_a_cursor_at_a_tied_timestamp_resumes_across_nodes_correctly():
    """The tie-break case a timestamp-only cursor would get wrong: four
    rows share one timestamp across two nodes."""
    nodes = {
        "aaa": [_row(2, "2026-09-14T10:00:01"), _row(1, "2026-09-14T10:00:01")],
        "bbb": [_row(2, "2026-09-14T10:00:01"), _row(1, "2026-09-14T10:00:01")],
    }
    first, cursor = merge_pages(nodes, limit=2)
    second, _ = merge_pages(nodes, limit=2, cursor=cursor)
    assert [r["audit_uid"] for r in first] == ["aaa:2", "aaa:1"]
    assert [r["audit_uid"] for r in second] == ["bbb:2", "bbb:1"]


def test_limit_is_clamped_to_the_same_bounds_the_local_store_uses():
    nodes = {"hp": [_row(i, f"2026-09-14T10:00:{i:02d}") for i in range(9, 0, -1)]}
    assert len(merge_pages(nodes, limit=0)[0]) == 1
    assert len(merge_pages(nodes, limit=-5)[0]) == 1
    assert MAX_LIMIT == 500 and DEFAULT_LIMIT == 50


# -- response shape -------------------------------------------------------

def test_the_page_reports_partial_results_and_names_the_failing_nodes():
    page = FleetAuditPage(
        rows=[], next_cursor=None, complete=False,
        nodes=[NodeReport(node_id="hp", ok=True, rows=3, fetched_at="2026-09-14T10:00:00"),
               NodeReport(node_id="dell", ok=False, error="node is offline", status="offline")],
    ).to_dict()
    assert page["complete"] is False
    assert page["partial"] is True
    assert page["node_errors"] == {"dell": "node is offline"}
    assert [n["node_id"] for n in page["nodes"]] == ["hp", "dell"]
    assert page["schema_version"] == SCHEMA_VERSION
    # Aggregated rows carry remote-agent text, so they are flagged for any
    # caller that renders them -- same posture as the knowledge fleet read.
    assert page["untrusted_output"] is True
    assert page["untrusted_fields"] == ["events"]


def test_a_fully_answered_page_is_marked_complete():
    page = FleetAuditPage(rows=[], nodes=[NodeReport(node_id="hp", ok=True)]).to_dict()
    assert page["complete"] is True and page["partial"] is False
    assert page["node_errors"] == {}
