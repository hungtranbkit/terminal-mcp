"""Task Center read-only adapter (issue #67): never miss, never fake, never write."""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from terminal_mcp import task_center_reader as tcr
from terminal_mcp.task_center_reader import (STALE_AFTER_SECONDS, HttpStatusError, TaskCenterConfigError,
                                             TaskCenterReader, build_task_view, normalize_task,
                                             parse_requirement, validate_base_url)

NOW = 1_800_000_000.0


def _row(task_id="tc_" + "a" * 32, state="running", **extra):
    row = {"task_id": task_id, "user_id": "u1", "project": "Terminal MCP", "title": "Pixel office",
           "details": "", "source": "manual", "external_id": "", "external_url": "", "node": "",
           "state": state, "state_source": "user", "evidence": [], "created_at": NOW - 100,
           "updated_at": NOW - 60}
    row.update(extra)
    return row


def _session(key="hp-linux/s1", state="IDLE", task_id=None, **extra):
    node, name = key.split("/", 1)
    entry = {"key": key, "session": name, "qualified": key, "node_id": node, "state": state,
             "agent": "claude", "current_task": {"task_id": task_id} if task_id else None, "task": None}
    entry.update(extra)
    return entry


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "aigw_session"
    path.write_text("tok-123\n")
    os.chmod(path, 0o600)
    return str(path)


class FakeGateway:
    def __init__(self, rows=None, status=200):
        self.rows = rows or []
        self.status = status
        self.calls = []

    def __call__(self, base, path, params, token):
        self.calls.append((base, path, dict(params or {}), token))
        if self.status != 200:
            raise HttpStatusError(self.status)
        if path.endswith("/summary"):
            return {"total": len(self.rows), "counts": {}, "sources": {"manual": "local",
                                                                      "chatgpt_history": "not_connected"}}
        offset = int((params or {}).get("offset", 0))
        limit = int((params or {}).get("limit", 100))
        page = self.rows[offset:offset + limit]
        nxt = offset + len(page) if offset + len(page) < len(self.rows) else None
        return {"data": page, "total": len(self.rows), "next_offset": nxt}


# -- states and flags ---------------------------------------------------------

def test_states_are_distinct_buckets():
    view = build_task_view({"rows": [
        _row("tc_1", "waiting_input"), _row("tc_2", "blocked"), _row("tc_3", "needs_review"),
        _row("tc_4", "unknown"), _row("tc_5", "running"),
        _row("tc_6", "new", updated_at=NOW - STALE_AFTER_SECONDS - 5),
        _row("tc_7", "running", evidence=[{"kind": "dispatch_uncertain"}]),
        _row("tc_8", "made_up_state"),
    ]}, [], now=NOW)
    buckets = {t["task_id"]: t["bucket"] for t in view["tasks"]}
    assert buckets == {"tc_1": "waiting_input", "tc_2": "blocked", "tc_3": "needs_review",
                       "tc_4": "unknown", "tc_5": "active", "tc_6": "stale",
                       "tc_7": "dispatch_uncertain", "tc_8": "unknown"}
    assert view["flag_counts"]["unfinished"] == 8


def test_verified_done_requires_evidence():
    bare = normalize_task(_row(state="verified_done", evidence=[{"kind": "user_note", "note": "ok"}]), now=NOW)
    proven = normalize_task(_row(state="verified_done", evidence=[
        {"kind": "test", "url": "https://github.com/o/r/actions/runs/1"}]), now=NOW)
    assert "verified_done" not in bare["flags"]
    assert {"needs_review", "done_without_evidence"} <= set(bare["flags"]) and bare["unfinished"]
    assert proven["flags"] == ["verified_done"] and not proven["unfinished"]


def test_old_verified_done_is_not_stale():
    task = normalize_task(_row(state="verified_done", updated_at=NOW - 10 * STALE_AFTER_SECONDS,
                               evidence=[{"kind": "commit", "sha": "abcdef1234567"}]), now=NOW)
    assert task["flags"] == ["verified_done"]


def test_idle_linked_session_never_finishes_a_task():
    view = build_task_view({"rows": [_row("tc_" + "b" * 32, "running")]},
                           [_session(state="IDLE", task_id="tc_" + "b" * 32)], now=NOW)
    task = view["tasks"][0]
    assert task["unfinished"] is True
    assert "agent_idle_open" in task["flags"]
    assert task["linked_sessions"][0]["key"] == "hp-linux/s1"
    assert view["tasks_by_session"] == {"hp-linux/s1": [task["task_id"]]}


def test_waiting_session_marks_task_waiting_input():
    view = build_task_view({"rows": [_row("tc_x", "running", details="dispatch dt_0123456789abcdef")]},
                           [_session(state="WAITING", task_id="dt_0123456789abcdef")], now=NOW)
    assert view["tasks"][0]["bucket"] == "waiting_input"


def test_claimed_session_missing_is_dispatch_uncertain_not_stale():
    row = _row("tc_y", "running", node="m910", details="Chạy trên session: parse-eval\nAC:\n- pass")
    view = build_task_view({"rows": [row]}, [_session("hp-linux/other", "RUNNING")], now=NOW)
    assert view["tasks"][0]["bucket"] == "dispatch_uncertain"
    live = build_task_view({"rows": [row]}, [_session("m910/parse-eval", "RUNNING")], now=NOW)
    assert live["tasks"][0]["bucket"] == "active"
    assert live["tasks"][0]["linked_sessions"][0]["session"] == "parse-eval"


def test_titles_alone_never_link_sessions():
    view = build_task_view({"rows": [_row(title="s1")]}, [_session()], now=NOW)
    assert view["tasks"][0]["linked_sessions"] == []


def test_unfinished_sort_first_and_nothing_filtered():
    rows = [_row("tc_done", "verified_done", evidence=[{"kind": "pr", "url": "https://github.com/o/r/pull/2"}],
                 updated_at=NOW), _row("tc_open", "new", updated_at=NOW - 5000)]
    view = build_task_view({"rows": rows}, [], now=NOW)
    assert [t["task_id"] for t in view["tasks"]] == ["tc_open", "tc_done"]


# -- provenance, requirement, links ---------------------------------------------

def test_requirement_and_acceptance_criteria_are_parsed():
    req, ac = parse_requirement("Build the office.\nKeep it pretty.\n\nAcceptance criteria:\n- 390px works\n"
                                "- [ ] reduced motion\n2. tests pass\n")
    assert req.startswith("Build the office.")
    assert ac == ["390px works", "reduced motion", "tests pass"]
    _, ac2 = parse_requirement("Tiêu chí nghiệm thu: không bỏ sót task")
    assert ac2 == ["không bỏ sót task"]


def test_links_are_classified_and_deduplicated():
    task = normalize_task(_row(
        external_url="https://chatgpt.com/c/11111111-2222-3333-4444-555555555555",
        details="See https://github.com/o/r/issues/67 and https://github.com/o/r/issues/67.",
        evidence=[{"kind": "pr", "url": "https://github.com/o/r/pull/9"},
                  {"kind": "commit", "sha": "c3f7a2530000"},
                  {"kind": "test", "url": "https://github.com/o/r/actions/runs/5"},
                  {"kind": "x", "url": "https://github.com/o/r/commit/abcdef1234"}]), now=NOW)
    kinds = [(l["kind"], l["label"]) for l in task["links"]]
    assert ("chat", "ChatGPT chat") in kinds
    assert kinds.count(("issue", "#67")) == 1
    assert ("pr", "PR #9") in kinds and ("tests", "https://github.com/o/r/actions/runs/5") in kinds
    assert ("commit", "abcdef12") in kinds and ("commit", "c3f7a253") in kinds
    assert task["source"] == "manual" and task["state_source"] == "user"


# -- reader: connection states ----------------------------------------------------

def test_not_configured_reads_chua_ket_noi_and_makes_no_call():
    gw = FakeGateway([_row()])
    snap = TaskCenterReader(None, None, fetch=gw).snapshot()
    assert snap["connection"] == "not_configured"
    assert snap["connection_label"] == "Chưa kết nối"
    assert snap["rows"] == [] and gw.calls == []
    view = build_task_view(snap, [])
    assert view["history_import"]["label"] == "Chưa kết nối"


def test_connected_reader_paginates_and_only_gets(token_file, monkeypatch):
    monkeypatch.setattr(tcr, "PAGE_LIMIT", 2)
    gw = FakeGateway([_row(f"tc_{i}") for i in range(5)])
    snap = TaskCenterReader("https://gw.example", token_file, fetch=gw).snapshot()
    assert snap["connection"] == "connected" and len(snap["rows"]) == 5 and not snap["truncated"]
    assert [c[1] for c in gw.calls] == ["/api/workspace/task-center/summary"] + \
        ["/api/workspace/task-center/tasks"] * 3
    assert all(c[3] == "tok-123" for c in gw.calls)
    assert "tok-123" not in json.dumps(snap)


def test_truncation_is_reported_never_silent(token_file, monkeypatch):
    monkeypatch.setattr(tcr, "PAGE_LIMIT", 1)
    monkeypatch.setattr(tcr, "MAX_PAGES", 2)
    snap = TaskCenterReader("https://gw.example", token_file,
                            fetch=FakeGateway([_row(f"tc_{i}") for i in range(4)])).snapshot()
    assert snap["truncated"] is True and len(snap["rows"]) == 2


def test_outage_keeps_last_good_tasks_marked_stale(token_file):
    clock = [NOW]
    gw = FakeGateway([_row("tc_keep")])
    reader = TaskCenterReader("https://gw.example", token_file, fetch=gw, ttl_seconds=0,
                              clock=lambda: clock[0])
    assert reader.snapshot()["rows"][0]["task_id"] == "tc_keep"
    gw.status = 502
    clock[0] += 30
    snap = reader.snapshot()
    assert snap["connection"] == "error" and snap["stale"] is True
    assert [r["task_id"] for r in snap["rows"]] == ["tc_keep"]
    gw.status = 401
    snap = reader.snapshot()
    assert snap["connection"] == "unauthorized" and snap["rows"]


def test_snapshot_is_cached(token_file):
    clock = [NOW]
    gw = FakeGateway([_row()])
    reader = TaskCenterReader("https://gw.example", token_file, fetch=gw, ttl_seconds=20,
                              clock=lambda: clock[0])
    reader.snapshot()
    clock[0] += 5
    assert reader.snapshot()["cached"] is True
    assert len(gw.calls) == 2


def test_from_env(token_file):
    reader = TaskCenterReader.from_env({tcr.URL_ENV: "https://gw.example", tcr.SESSION_FILE_ENV: token_file})
    assert reader.configured
    assert not TaskCenterReader.from_env({}).configured


# -- safety --------------------------------------------------------------------------

@pytest.mark.parametrize("url", ["ftp://x", "http://gw.example", "https://u:p@gw.example",
                                 "https://gw.example/?a=1", "not a url"])
def test_unsafe_urls_are_refused(url):
    with pytest.raises(TaskCenterConfigError):
        validate_base_url(url)


def test_loopback_http_and_https_are_allowed():
    assert validate_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert validate_base_url("https://gw.example/base/") == "https://gw.example/base"


def test_world_readable_session_file_is_refused(tmp_path):
    path = tmp_path / "tok"
    path.write_text("tok")
    os.chmod(path, 0o644)
    snap = TaskCenterReader("https://gw.example", str(path), fetch=FakeGateway()).snapshot()
    assert snap["connection"] == "error" and "chmod 600" in snap["detail"]


def test_only_whitelisted_paths_can_be_fetched():
    for path in ("/api/workspace/task-center/tasks/x/../../admin", "/api/admin", "/api/workspace/task-center"):
        with pytest.raises(TaskCenterConfigError):
            tcr.http_get_json("https://gw.example", path, None, "t")


class _Handler(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append((self.command, self.path, self.headers.get("Cookie"),
                              self.headers.get("X-CSRF-Token")))
        if self.path.startswith("/api/workspace/task-center/summary"):
            body = json.dumps({"total": 1, "counts": {"new": 1}, "sources": {}}).encode()
            self.send_response(200)
        elif self.path.startswith("/api/workspace/task-center/tasks"):
            self.send_response(302)
            self.send_header("Location", "http://evil.invalid/steal")
            body = b""
        else:
            self.send_response(404)
            body = b""
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def test_real_http_get_sends_cookie_no_csrf_and_refuses_redirects(token_file):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        snap = TaskCenterReader(base, token_file).snapshot()
    finally:
        server.shutdown()
    assert snap["connection"] == "error" and "302" in snap["detail"]
    assert all(m == "GET" for m, *_ in _Handler.seen)
    assert _Handler.seen[0][2] == "aigw_session=tok-123"
    assert all(csrf is None for *_, csrf in _Handler.seen)
    assert not any("evil" in p for _, p, *_ in _Handler.seen)


def test_explicit_session_without_node_links_only_when_unique():
    row = _row("tc_z", "running", details="session: build-1")
    one = build_task_view({"rows": [row]}, [_session("hp-linux/build-1", "RUNNING")], now=NOW)
    assert [l["key"] for l in one["tasks"][0]["linked_sessions"]] == ["hp-linux/build-1"]
    two = build_task_view({"rows": [row]}, [_session("hp-linux/build-1", "RUNNING"),
                                            _session("m910/build-1", "RUNNING")], now=NOW)
    assert two["tasks"][0]["linked_sessions"] == []
