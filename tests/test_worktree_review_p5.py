"""Worktree Janitor P5 -- the review queue, the panel API and the report.

Two things are being pinned here that are easy to get wrong in a UI layer and
expensive to get wrong in this one: that no surface offers a force affordance,
and that a sensitive file is named but never quoted. Both are asserted against
the real payloads rather than trusted to review.

Named per acceptance criterion (AC1-AC6).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from terminal_mcp import worktree_cleanup as wc
from terminal_mcp import worktree_janitor as wj
from terminal_mcp import worktree_review as review
from terminal_mcp.queue_store import QueueStore
from terminal_mcp.worktree_review import WorktreeReviewService

_ENV = {"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@x",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@x",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True, env={**_ENV, "HOME": str(cwd)})


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / ".gitignore").write_text("*.db\n.env\n__pycache__/\n")
    (root / "a.txt").write_text("one\n")
    (root / "payload.bin").write_bytes(b"z" * 4096)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _worktree(repo, name, *, age=99_999):
    path = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", f"task/{name}", str(path), "main")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _policy(tmp_path, mode="observe_only"):
    return wj.JanitorPolicy(mode=mode, allowed_roots=(str(tmp_path),), grace_seconds=0)


def _classify(path, tmp_path, *, task=None, **kw):
    kw.setdefault("process_cwds", [])
    kw.setdefault("tmux_paths", [])
    kw.setdefault("session_paths", [])
    kw.setdefault("service_roots", [])
    kw.setdefault("evidence_collected_at", time.time())
    return wj.classify({"worktree_path": str(path)}, _policy(tmp_path), task=task, **kw).to_dict()


def _task(**kw):
    base = {"id": "T1", "status": "COMPLETED", "attempt_count": 1, "max_attempts": 3,
            "terminal_at": 0.0, "metadata": {}}
    base.update(kw)
    return base


def _store_with_record(tmp_path, state, *, worktree_path="/w"):
    store = QueueStore(tmp_path / "queue.db")
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": worktree_path},
                     wc.METADATA_KEY: {"state": state}}}])
    return store, task_id


# == AC2: the panel's numbers and words ===================================

def test_ac2_reclaimable_bytes_are_broken_down_by_class(repo, tmp_path):
    clean = _worktree(repo, "wt-clean")
    dirty = _worktree(repo, "wt-dirty")
    (dirty / "wip.txt").write_text("x\n")
    candidates = [_classify(clean, tmp_path, task=_task()),
                  _classify(dirty, tmp_path, task=_task())]
    report = review.build_report(candidates, mode="observe_only")
    assert report["counts"][wj.AUTO_SAFE] == 1
    assert report["counts"][wj.BLOCKED] == 1
    assert report["reclaimable_bytes_by_class"][wj.AUTO_SAFE] > 0
    assert report["reclaimable_bytes_by_class"][wj.BLOCKED] > 0


def test_ac2_the_headline_reclaimable_total_counts_only_auto_safe(repo, tmp_path):
    """A total that summed BLOCKED bytes would promise space that is never
    coming -- the most misleading number this panel could show."""
    dirty = _worktree(repo, "wt-onlydirty")
    (dirty / "wip.txt").write_text("x\n")
    report = review.build_report([_classify(dirty, tmp_path, task=_task())],
                                mode="observe_only")
    assert report["counts"][wj.BLOCKED] == 1
    assert report["reclaimable_bytes_by_class"][wj.BLOCKED] > 0
    assert report["reclaimable_bytes"] == 0
    assert report["actionable_count"] == 0


def test_ac2_blocked_reasons_are_rendered_in_plain_language(repo, tmp_path):
    dirty = _worktree(repo, "wt-words")
    (dirty / "wip.txt").write_text("x\n")
    report = review.build_report([_classify(dirty, tmp_path, task=_task())],
                                mode="observe_only")
    row = report["blocked"][0]
    assert wj.DIRTY in row["reasons"], "the machine code stays, for branching"
    assert any("uncommitted changes" in e for e in row["explanations"])
    assert row["explanations"] != row["reasons"], "a code is not an explanation"


@pytest.mark.parametrize("code", [wj.DIRTY, wj.UNMERGED_UNPUSHED, wj.VALUABLE_IGNORED_DATA,
                                  wj.PROCESS_IN_USE, wj.MAIN_WORKTREE, wj.DETACHED_HEAD,
                                  wj.TASK_NOT_FINAL, wj.GRACE_NOT_ELAPSED])
def test_ac2_every_reason_an_operator_will_see_has_a_sentence(code):
    sentence = review.explain(code)
    assert sentence != code, f"{code} has no plain-language explanation"
    assert len(sentence) > 20


def test_ac2_an_unknown_reason_falls_back_to_the_code_not_an_invention():
    """Inventing an explanation for a code this table has not been taught would
    be worse than showing the raw code: an operator would act on the fiction."""
    assert review.explain("SOME_FUTURE_REASON") == "SOME_FUTURE_REASON"


def test_ac2_the_oldest_candidate_is_identified(repo, tmp_path):
    """Age is the WORKTREE's own age, not the classification's. Deriving it from
    when we classified made every candidate ~0s old, so "oldest" meant nothing."""
    old_wt = _worktree(repo, "wt-old", age=90_000)
    new_wt = _worktree(repo, "wt-new", age=60)
    report = review.build_report(
        [_classify(new_wt, tmp_path, task=_task()), _classify(old_wt, tmp_path, task=_task())],
        mode="observe_only")
    assert report["oldest_candidate"]["worktree_path"] == str(old_wt)
    assert report["oldest_candidate"]["age_seconds"] > 80_000


def test_ac2_an_unstattable_candidate_has_an_unknown_age_not_zero(tmp_path):
    """None and 0 must not look the same in a panel sorted by age."""
    report = review.build_report(
        [{"worktree_path": "/gone", "policy_class": wj.REVIEW, "reasons": [],
          "evidence": {"worktree_age_seconds": None}}], mode="observe_only")
    assert report["review_queue"][0]["age_seconds"] is None
    assert report["oldest_candidate"] is None


@pytest.mark.parametrize("mode,enforcing", [("observe_only", False), ("suggest_only", False),
                                            ("auto_execute", True)])
def test_ac2_the_observe_only_badge_reflects_the_mode(mode, enforcing):
    report = review.build_report([], mode=mode)
    assert report["enforcing"] is enforcing
    assert report["observe_only"] is (not enforcing)


def test_ac2_the_report_says_which_filesystem_a_reclaim_affects():
    """So nobody reads a worktree reclaim as relief for a different mount."""
    assert "does not free any other mount" in review.build_report([], mode="observe_only")[
        "filesystem_note"]


# == AC3: approve / abandon ===============================================

def test_ac3_approve_moves_a_review_item_to_eligible(tmp_path):
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_REVIEW)
    result = WorktreeReviewService(store).decide(task_id, review.APPROVE, actor="op@x")
    assert result["state"] == wc.CLEANUP_ELIGIBLE
    assert store.get_task(task_id).metadata[wc.METADATA_KEY]["state"] == wc.CLEANUP_ELIGIBLE
    assert store.get_task(task_id).metadata[wc.METADATA_KEY]["reviewed_by"] == "op@x"


def test_ac3_approve_is_a_nomination_not_a_deletion(tmp_path):
    """The reply must not let a caller believe the worktree is gone -- the
    executor still re-checks with fresh evidence under its lock."""
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_REVIEW)
    result = WorktreeReviewService(store).decide(task_id, review.APPROVE)
    assert result["state"] != wc.CLEANUP_DONE
    assert "re-check" in result["detail"]


def test_ac3_abandon_sets_abandoned_and_stops_re_proposal(tmp_path):
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_REVIEW)
    result = WorktreeReviewService(store).decide(task_id, review.ABANDON, actor="op@x")
    assert result["state"] == wc.CLEANUP_ABANDONED
    record = store.get_task(task_id).metadata[wc.METADATA_KEY]
    assert record["state"] == wc.CLEANUP_ABANDONED
    # The sweep's converge half only looks at PENDING/ELIGIBLE, so ABANDONED is
    # structurally out of its reach -- that IS the "stops being re-proposed".
    assert record["state"] not in (wc.CLEANUP_PENDING, wc.CLEANUP_ELIGIBLE)
    assert not store.list_worktree_cleanup_tasks(
        states=(wc.CLEANUP_PENDING, wc.CLEANUP_ELIGIBLE))


def test_ac3_an_abandoned_item_is_not_silently_re_approved(tmp_path):
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_ABANDONED)
    result = WorktreeReviewService(store).decide(task_id, review.APPROVE)
    assert result["error"] == "ABANDONED"
    assert store.get_task(task_id).metadata[wc.METADATA_KEY]["state"] == wc.CLEANUP_ABANDONED


def test_ac3_a_removed_worktree_cannot_be_reviewed(tmp_path):
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_DONE)
    for decision in (review.APPROVE, review.ABANDON):
        assert WorktreeReviewService(store).decide(task_id, decision)["error"] == "ALREADY_REMOVED"


def test_ac3_only_approve_and_abandon_are_accepted(tmp_path):
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_REVIEW)
    service = WorktreeReviewService(store)
    for bogus in ("force", "delete", "remove", "FORCE", "purge", ""):
        result = service.decide(task_id, bogus)
        assert result["error"] in ("INVALID_DECISION",), bogus
        assert store.get_task(task_id).metadata[wc.METADATA_KEY]["state"] == wc.CLEANUP_REVIEW


def test_ac3_a_task_with_no_cleanup_record_is_refused(tmp_path):
    store = QueueStore(tmp_path / "queue.db")
    task_id, = store.append_tasks("demo", [{"title": "t", "prompt": "p" * 60}])
    assert WorktreeReviewService(store).decide(task_id, review.APPROVE)["error"] == \
        "NO_CLEANUP_RECORD"


def test_ac3_the_decision_is_audited_with_who_made_it(tmp_path):
    from terminal_mcp.audit import AuditStore

    audit = AuditStore(tmp_path / "audit.db")
    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_REVIEW)
    WorktreeReviewService(store, audit=audit).decide(task_id, review.ABANDON, actor="op@x")
    rows = [r for r in audit.list(limit=20) if r["action"].startswith("worktree_review")]
    assert rows and rows[0]["actor"] == "op@x"
    assert wc.CLEANUP_ABANDONED in rows[0]["reason"]


def test_ac3_an_audit_failure_does_not_lose_the_decision(tmp_path):
    class _BrokenAudit:
        def record(self, **kwargs):
            raise RuntimeError("audit gone")

    store, task_id = _store_with_record(tmp_path, wc.CLEANUP_REVIEW)
    result = WorktreeReviewService(store, audit=_BrokenAudit()).decide(task_id, review.APPROVE)
    assert result["state"] == wc.CLEANUP_ELIGIBLE


# == AC4: no force affordance anywhere ====================================

def test_ac4_the_report_payload_offers_no_force_action(repo, tmp_path):
    """Checked against KEYS and ACTION VALUES, not as a substring sweep: a
    worktree legitimately named `wt-enforcement` would trip a naive scan, and a
    test that cries wolf on a path gets muted."""
    dirty = _worktree(repo, "wt-plain")
    (dirty / "wip.txt").write_text("x\n")
    report = review.build_report([_classify(dirty, tmp_path, task=_task())],
                                mode="auto_execute")

    def _keys(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                yield key
                yield from _keys(value)
        elif isinstance(obj, list):
            for item in obj:
                yield from _keys(item)

    forbidden = {"force", "purge", "delete_now", "remove_now", "force_remove"}
    assert not (set(_keys(report)) & forbidden)
    for row in report["blocked"] + report["review_queue"]:
        assert not (set(row["actions"]) & forbidden)
        assert set(row["actions"]) <= set(review.DECISIONS)


def test_ac4_blocked_items_offer_no_actions_at_all(repo, tmp_path):
    """A human cannot approve past a predicate, and showing a button that would
    be refused teaches them the refusals are negotiable."""
    dirty = _worktree(repo, "wt-noactions")
    (dirty / "wip.txt").write_text("x\n")
    report = review.build_report([_classify(dirty, tmp_path, task=_task())],
                                mode="observe_only")
    assert report["blocked"][0]["actions"] == []


def test_ac4_review_items_offer_exactly_approve_and_abandon(repo, tmp_path):
    detached = _worktree(repo, "wt-detached")
    head = _git(detached, "rev-parse", "HEAD").stdout.strip()
    _git(detached, "checkout", "-q", "--detach", head)
    report = review.build_report([_classify(detached, tmp_path, task=_task())],
                                mode="observe_only")
    assert report["review_queue"][0]["actions"] == [review.APPROVE, review.ABANDON]


def test_ac4_the_decision_vocabulary_is_exactly_two_words():
    assert set(review.DECISIONS) == {"approve", "abandon"}


# The P5 surfaces, by name. Scoped deliberately: dashboard.py and mcp_app.py
# both contain PRE-EXISTING force= forwarding for unrelated features (a
# wall-cache refresh, session recovery, and the manual terminal_worktree_cleanup
# escape hatch the contract keeps for a human and P7 reconciles). Asserting
# over those whole files would fail on code this phase neither wrote nor owns,
# and a test that fails for reasons outside its subject gets muted.
_P5_FUNCTIONS = ("dashboard_worktrees", "dashboard_worktrees_review",
                 "terminal_worktree_janitor_report")


def test_ac4_no_p5_surface_can_pass_force():
    """The dangerous construct, not the word. These surfaces must stay free to
    DOCUMENT that they never force -- a prose scan would forbid explaining the
    guarantee, which is how that kind of test ends up deleted."""
    import ast

    checked = set()
    for path in ("terminal_mcp/worktree_review.py", "terminal_mcp/dashboard.py",
                 "terminal_mcp/mcp_app.py"):
        tree = ast.parse(Path(path).read_text())
        targets = []
        if path.endswith("worktree_review.py"):
            targets.append(tree)  # the whole module is P5's
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and \
                    fn.name in _P5_FUNCTIONS:
                targets.append(fn)
                checked.add(fn.name)
        for target in targets:
            for call in (n for n in ast.walk(target) if isinstance(n, ast.Call)):
                for keyword in call.keywords:
                    if keyword.arg == "force":
                        assert isinstance(keyword.value, ast.Constant) and \
                            keyword.value.value is False, \
                            f"{path}:{getattr(target, 'name', '<module>')} passes a non-False force="
    assert checked == set(_P5_FUNCTIONS), f"a P5 surface was not found: {checked}"


# == AC5: paths only, never contents ======================================

def test_ac5_a_sensitive_ignored_file_is_named_but_never_quoted(repo, tmp_path):
    path = _worktree(repo, "wt-secret")
    (path / ".env").write_text("SECRET_VALUE_MUST_NEVER_REACH_A_DASHBOARD=1\n")
    (path / "data.db").write_bytes(b"sqlite-ish bytes with SECRET_ROW inside")
    report = review.build_report([_classify(path, tmp_path, task=_task())],
                                mode="observe_only")
    serialized = json.dumps(report)
    # The refusal is actionable: the operator can see WHY and which class of file.
    assert wj.VALUABLE_IGNORED_DATA in report["blocked"][0]["reasons"]
    assert any("worth keeping" in e for e in report["blocked"][0]["explanations"])
    # ...and no content leaked.
    assert "SECRET_VALUE_MUST_NEVER_REACH_A_DASHBOARD" not in serialized
    assert "SECRET_ROW" not in serialized


def test_ac5_the_report_never_carries_a_content_field(repo, tmp_path):
    path = _worktree(repo, "wt-nocontent")
    report = review.build_report([_classify(path, tmp_path, task=_task())],
                                mode="observe_only")
    serialized = json.dumps(report)
    for field in ('"content"', '"text"', '"patch"', '"diff"', '"lines"'):
        assert field not in serialized


# == AC6: the MCP report ==================================================

def test_ac6_the_report_tool_is_registered():
    import asyncio

    from terminal_mcp.mcp_app import build_mcp

    names = {t.name for t in asyncio.run(build_mcp().list_tools())}
    assert "terminal_worktree_janitor_report" in names


def test_ac6_the_tool_and_the_panel_share_one_summariser():
    """The guarantee behind "the same data": both surfaces call build_report, so
    they cannot drift. Asserted on the source of both call sites."""
    mcp_app = Path("terminal_mcp/mcp_app.py").read_text()
    dashboard = Path("terminal_mcp/dashboard.py").read_text()
    assert "worktree_review.build_report(" in mcp_app
    assert "worktree_review.build_report(" in dashboard


def test_ac6_no_repo_roots_is_an_explicit_error_not_an_empty_success(tmp_path):
    """An empty report would read as "nothing to clean", which is a different
    and wrong statement from "nothing was configured to look at"."""
    import asyncio

    from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                     SessionLifecycleConfig, WorktreeJanitorConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp

    config = AppConfig(
        permissions=PermissionsConfig(True, False), allowed_session_patterns=("agent-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, protected_sessions=()),
        worktree_janitor=WorktreeJanitorConfig(repo_roots=()))
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "g.db"))
    server = build_mcp(service=terminal)
    result = asyncio.run(server.call_tool("terminal_worktree_janitor_report", {}))
    payload = json.loads(result.content[0].text)
    assert payload["error"] == "NO_REPO_ROOTS"
    assert payload["observe_only"] is True


def test_ac6_a_real_report_over_mcp_summarises_a_real_repo(repo, tmp_path):
    import asyncio

    from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                     SessionLifecycleConfig, WorktreeJanitorConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp

    _worktree(repo, "wt-mcp")
    config = AppConfig(
        permissions=PermissionsConfig(True, False), allowed_session_patterns=("agent-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, protected_sessions=()),
        worktree_janitor=WorktreeJanitorConfig(allowed_roots=(str(tmp_path),),
                                               repo_roots=(str(repo),)))
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "g2.db"))
    server = build_mcp(service=terminal)
    result = asyncio.run(server.call_tool("terminal_worktree_janitor_report", {}))
    payload = json.loads(result.content[0].text)
    assert payload["total"] >= 2  # main + the linked one
    assert payload["observe_only"] is True
    assert payload["complete"] is True
    assert "force" not in json.dumps(payload).lower()


# == AC1: the dashboard routes ============================================

def test_ac1_the_routes_are_in_the_pinned_inventory():
    source = Path("tests/test_dashboard.py").read_text()
    assert '"/dashboard/api/worktrees": {"GET", "HEAD"}' in source
    assert '"/dashboard/api/worktrees/review": {"POST"}' in source


def test_ac1_the_review_route_is_mutation_guarded():
    """Same posture as every sibling POST: the guard is called before the body
    is even read."""
    import ast

    tree = ast.parse(Path("terminal_mcp/dashboard.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "dashboard_worktrees_review":
            names = [n.func.id for n in ast.walk(fn) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name)]
            assert "_mutation_guard" in names
            return
    raise AssertionError("dashboard_worktrees_review not found")


def test_ac1_the_panel_route_is_read_only():
    """A GET route that could mutate would be a CSRF hole by construction."""
    import ast

    tree = ast.parse(Path("terminal_mcp/dashboard.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "dashboard_worktrees":
            called = {n.func.attr for n in ast.walk(fn) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)}
            for forbidden in ("decide", "execute", "patch_worktree_cleanup",
                              "remove_worktree"):
                assert forbidden not in called, f"the panel must not call {forbidden}"
            return
    raise AssertionError("dashboard_worktrees not found")


def test_the_review_service_cannot_remove_anything():
    """Structural: approving changes a state. It must not be able to delete."""
    import ast

    tree = ast.parse(Path(review.__file__).read_text())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    for forbidden in ("rmtree", "remove", "unlink", "remove_worktree", "execute"):
        assert forbidden not in called, f"the review service must not call {forbidden}()"
    assert "WorktreeExecutor" not in Path(review.__file__).read_text()


# == end to end through the real dashboard routes =========================

def _dashboard_client(tmp_path, repo, *, mode="observe_only"):
    """The real dashboard, registered on a real MCPServer, with a real
    QueueStore -- so the routes, the guard and the store wiring are all
    production code."""
    from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                     SessionLifecycleConfig, WorktreeJanitorConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp
    from terminal_mcp.queue_service import QueueService

    config = AppConfig(
        permissions=PermissionsConfig(True, False), allowed_session_patterns=("agent-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, protected_sessions=()),
        worktree_janitor=WorktreeJanitorConfig(mode=mode, allowed_roots=(str(tmp_path),),
                                               repo_roots=(str(repo),), grace_seconds=0))
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "g.db"))
    queue = QueueService(store=QueueStore(tmp_path / "queue.db"))
    server = build_mcp(service=terminal, queue=queue)
    register_dashboard(server, terminal, queue=queue)
    # streamable_http_app() + an Origin header: the same shape every other
    # dashboard test uses, so the CSRF/origin path is the production one.
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    return client, queue.store


def test_e2e_the_panel_route_returns_a_real_report(tmp_path, repo):
    _worktree(repo, "wt-panel")
    client, _ = _dashboard_client(tmp_path, repo)
    response = client.get("/dashboard/api/worktrees")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 2  # main + linked
    assert payload["observe_only"] is True
    assert "does not free any other mount" in payload["filesystem_note"]
    assert response.headers.get("Cache-Control") == "no-store"


def test_e2e_the_panel_reports_blocked_reasons_in_words(tmp_path, repo):
    dirty = _worktree(repo, "wt-e2e-dirty")
    (dirty / "wip.txt").write_text("x\n")
    client, _ = _dashboard_client(tmp_path, repo)
    payload = client.get("/dashboard/api/worktrees").json()
    rows = [r for r in payload["blocked"] if r["worktree_path"] == str(dirty)]
    assert rows and any("uncommitted changes" in e for e in rows[0]["explanations"])


def test_e2e_review_is_refused_from_a_disallowed_origin(tmp_path, repo):
    """The CSRF half of the guard, proven by a cross-origin POST."""
    client, _ = _dashboard_client(tmp_path, repo)
    response = client.post("/dashboard/api/worktrees/review",
                           json={"task_id": "T1", "decision": "approve"},
                           headers={"Origin": "http://evil.example"})
    assert response.status_code == 403, response.status_code
    assert response.json()["error"] == "ORIGIN_NOT_ALLOWED"


def test_e2e_review_approves_a_real_record_end_to_end(tmp_path, repo):
    """The whole chain: real route, real guard, real store, real state change."""
    client, store = _dashboard_client(tmp_path, repo)
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": str(repo.parent / "wt-x")},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_REVIEW}}}])
    response = client.post("/dashboard/api/worktrees/review",
                           json={"task_id": task_id, "decision": "approve"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == wc.CLEANUP_ELIGIBLE
    assert store.get_task(task_id).metadata[wc.METADATA_KEY]["state"] == wc.CLEANUP_ELIGIBLE


def test_e2e_review_rejects_a_force_style_decision(tmp_path, repo):
    """Belt and braces at the HTTP boundary: even a hand-crafted request cannot
    smuggle a third decision past the route."""
    client, store = _dashboard_client(tmp_path, repo)
    task_id, = store.append_tasks("demo", [{
        "title": "t", "prompt": "p" * 60,
        "metadata": {"git_isolation": {"worktree_path": "/w"},
                     wc.METADATA_KEY: {"state": wc.CLEANUP_REVIEW}}}])
    response = client.post("/dashboard/api/worktrees/review",
                           json={"task_id": task_id, "decision": "force"})
    assert response.status_code == 400
    assert response.json()["error"] == "INVALID_DECISION"
    assert store.get_task(task_id).metadata[wc.METADATA_KEY]["state"] == wc.CLEANUP_REVIEW


def test_e2e_no_repo_roots_is_an_explicit_error(tmp_path, repo):
    from terminal_mcp.config import (AppConfig, InputPolicyConfig, PermissionsConfig,
                                     SessionLifecycleConfig, WorktreeJanitorConfig)
    from terminal_mcp.core import TerminalService
    from terminal_mcp.dashboard import register_dashboard
    from terminal_mcp.grants import SessionGrantStore
    from terminal_mcp.mcp_app import build_mcp

    config = AppConfig(
        permissions=PermissionsConfig(True, False), allowed_session_patterns=("agent-*",),
        input_policy=InputPolicyConfig(allowed_session_patterns=("agent-*",)),
        session_lifecycle=SessionLifecycleConfig(enabled=False, protected_sessions=()),
        worktree_janitor=WorktreeJanitorConfig(repo_roots=()))
    terminal = TerminalService(config, grants=SessionGrantStore(tmp_path / "g3.db"))
    server = build_mcp(service=terminal)
    register_dashboard(server, terminal)
    client = TestClient(server.streamable_http_app(), headers={"Origin": "http://testserver"})
    payload = client.get("/dashboard/api/worktrees").json()
    assert payload["error"] == "NO_REPO_ROOTS"
