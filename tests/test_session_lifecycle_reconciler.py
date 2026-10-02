"""Agent-session lifecycle reconciler (session_reconciler.py).

Real git repositories/worktrees in tmp_path decide clean/dirty/merged; the
tmux inventory is stubbed so the host's own live sessions never matter.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from terminal_mcp.chatgpt_sidecar import translate_legacy_call
from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.config import (AgentCleanupConfig, AppConfig, PermissionsConfig,
                                 SessionLifecycleConfig, _load_session_lifecycle_config)
from terminal_mcp.core import TerminalService
from terminal_mcp.session_reconciler import SessionReconciler, probe_git

NOW = 2_000_000_000.0
OLD = NOW - 5 * 3600
# A finished Claude turn: the "· done <time>" footer is the idle marker.
AGENT_IDLE_TAIL = ["● finished", "✻ Worked for 2m · done 10:05 AM", "╭────╮", "│ >  │", "╰────╯",
                   "  ? for shortcuts"]


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("a\n")
    git(root, "add", "a.txt")
    git(root, "commit", "-qm", "init")
    return root


def add_worktree(repo, name, *, commit=False, merge=False):
    path = repo.parent / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    if commit:
        (path / f"{name}.txt").write_text("x\n")
        git(path, "add", ".")
        git(path, "commit", "-qm", name)
    if merge:
        git(repo, "merge", "-q", "--ff-only", name)
    return path


@dataclass
class Info:
    name: str
    pane_current_command: str
    pane_current_path: str
    attached: bool = False
    activity_epoch: int = int(OLD)
    pane_dead: bool = False
    pane_in_mode: bool = False
    session_id: str = "$1"
    pane_id: str = "%1"


class FakeTmux:
    def __init__(self, sessions, tails=None):
        self.sessions = {s.name: s for s in sessions}
        self.tails = tails or {}

    def list_sessions(self):
        return list(self.sessions.values())

    def get_session(self, name):
        return self.sessions.get(name)

    def capture_lines(self, name, lines):
        return self.tails.get(name, AGENT_IDLE_TAIL)


class FakeTerminal:
    def __init__(self, sessions, *, policy=None, protected=("terminal-mcp",), tails=None):
        self.tmux = FakeTmux(sessions, tails)
        self.config = SimpleNamespace(
            session_lifecycle=SimpleNamespace(protected_sessions=protected,
                                              agent_cleanup=policy or AgentCleanupConfig(enabled=True, dry_run=False)),
            permissions=SimpleNamespace(admin_sessions=()))
        self.deletes = []
        self.audit = SimpleNamespace(record=lambda **kw: None)

    def _session_delete_runtime_blocker(self, name, info):
        return "SESSION_ATTACHED" if info is not None and info.attached else None

    def terminal_delete_session(self, name, *, confirm=False, requested_by=None):
        if confirm is not True:
            return {"error": "CONFIRMATION_REQUIRED"}
        self.deletes.append((name, requested_by))
        self.tmux.sessions.pop(name, None)
        return {"action": "deleted", "session": name}


def reconciler(term, tmp_path, **kw):
    policy = AgentCleanupConfig(enabled=True, dry_run=False, scrollback_dir=str(tmp_path / "reaped"),
                                **kw.pop("policy", {}))
    return SessionReconciler(term, policy=policy, clock=lambda: NOW, **kw)


def by_name(report):
    return {row["session"]: row for row in report["sessions"]}


def test_completed_clean_merged_worktree_is_closed(repo, tmp_path):
    wt = add_worktree(repo, "task-done", commit=True, merge=True)
    term = FakeTerminal([Info("task-done-claude", "claude", str(wt))])
    report = reconciler(term, tmp_path).run()
    assert [r["session"] for r in report["closed"]] == ["task-done-claude"]
    assert report["closed"][0]["classification"] == "COMPLETED_CLEAN"
    assert term.deletes == [("task-done-claude", "session-reconciler")]
    assert (tmp_path / "reaped").iterdir().__next__().read_text()  # scrollback saved


def test_dirty_and_unmerged_worktrees_are_preserved(repo, tmp_path):
    dirty = add_worktree(repo, "task-dirty", commit=True, merge=True)
    (dirty / "wip.txt").write_text("uncommitted\n")
    unmerged = add_worktree(repo, "task-unmerged", commit=True, merge=False)
    term = FakeTerminal([Info("dirty-agent", "codex", str(dirty)),
                         Info("unmerged-agent", "claude", str(unmerged))])
    report = reconciler(term, tmp_path).run()
    rows = by_name(report)
    assert rows["dirty-agent"]["classification"] == "DIRTY_WORKTREE"
    assert rows["unmerged-agent"]["classification"] == "UNMERGED_BRANCH"
    assert term.deletes == [] and report["closed"] == []


def test_active_protected_attached_service_and_shell_are_preserved(repo, tmp_path):
    wt = add_worktree(repo, "task-merged", commit=True, merge=True)
    sessions = [
        Info("busy-task", "claude", str(wt)),
        Info("terminal-mcp", "claude", str(wt)),
        Info("attached-agent", "claude", str(wt), attached=True),
        Info("dev-server", "pnpm", str(wt)),
        Info("named-tunnel", "cloudflared", str(wt)),
        Info("plain-shell", "bash", str(wt)),
        Info("recent-agent", "claude", str(wt), activity_epoch=int(NOW - 60)),
        Info("thinking-agent", "claude", str(wt)),
    ]
    term = FakeTerminal(sessions, tails={"thinking-agent": ["✻ Thinking… (esc to interrupt)"]})
    rec = reconciler(term, tmp_path, active_refs=lambda name: ["supervised task"] if name == "busy-task" else [])
    rows = by_name(rec.run())
    assert rows["busy-task"]["classification"] == "ACTIVE_TASK"
    assert rows["terminal-mcp"]["classification"] == "PROTECTED"
    assert rows["attached-agent"]["classification"] == "ATTACHED"
    assert rows["dev-server"]["classification"] == "SERVICE"
    assert rows["named-tunnel"]["classification"] == "SERVICE"
    assert rows["plain-shell"]["classification"] == "SHELL"
    assert rows["recent-agent"]["classification"] == "AGENT_RECENT"
    assert rows["thinking-agent"]["classification"] != "COMPLETED_CLEAN"
    assert term.deletes == []


def test_orphan_with_deleted_worktree_is_closed(repo, tmp_path):
    wt = add_worktree(repo, "task-gone", commit=True, merge=True)
    git(repo, "worktree", "remove", str(wt))
    term = FakeTerminal([Info("gone-agent", "claude", str(wt))])
    report = reconciler(term, tmp_path).run()
    assert report["closed"][0]["classification"] == "ORPHAN_WORKTREE_MISSING"
    assert term.deletes == [("gone-agent", "session-reconciler")]


def test_primary_checkout_needs_explicit_opt_in(repo, tmp_path):
    term = FakeTerminal([Info("primary-agent", "claude", str(repo))])
    assert by_name(reconciler(term, tmp_path).run())["primary-agent"]["classification"] == "IDLE_PRIMARY_CHECKOUT"
    assert term.deletes == []
    opted = reconciler(term, tmp_path, policy={"close_clean_primary_checkouts": True}).run()
    assert [r["session"] for r in opted["closed"]] == ["primary-agent"]


def test_non_repo_cwd_is_preserved(tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    term = FakeTerminal([Info("loose-agent", "codex", str(plain))])
    assert by_name(reconciler(term, tmp_path).run())["loose-agent"]["classification"] == "NO_REPO"
    assert term.deletes == []


def test_dry_run_closes_nothing_and_reports_candidates(repo, tmp_path):
    wt = add_worktree(repo, "task-dry", commit=True, merge=True)
    term = FakeTerminal([Info("dry-agent", "claude", str(wt))])
    report = reconciler(term, tmp_path).run(dry_run=True)
    assert report["dry_run"] is True and report["close_candidates"] == ["dry-agent"]
    assert report["closed"] == [] and term.deletes == []


def test_reconcile_is_idempotent(repo, tmp_path):
    wt = add_worktree(repo, "task-twice", commit=True, merge=True)
    term = FakeTerminal([Info("twice-agent", "claude", str(wt))])
    rec = reconciler(term, tmp_path)
    assert len(rec.run()["closed"]) == 1
    second = rec.run()
    assert second["closed"] == [] and second["refused"] == []
    assert term.deletes == [("twice-agent", "session-reconciler")]


def test_preflight_refusal_preserves_the_session(repo, tmp_path):
    wt = add_worktree(repo, "task-pre", commit=True, merge=True)
    term = FakeTerminal([Info("pre-agent", "claude", str(wt))])
    rec = reconciler(term, tmp_path, preflight=lambda name: {"error": "SESSION_HAS_ACTIVE_RUN"})
    report = rec.run()
    assert report["closed"] == [] and report["refused"][0]["refused"] == "SESSION_HAS_ACTIVE_RUN"
    assert term.deletes == []


def test_max_closes_per_run_bounds_a_pass(repo, tmp_path):
    sessions = []
    for i in range(3):
        wt = add_worktree(repo, f"task-cap{i}", commit=True, merge=True)
        sessions.append(Info(f"cap-agent-{i}", "claude", str(wt)))
    term = FakeTerminal(sessions)
    assert len(reconciler(term, tmp_path, policy={"max_closes_per_run": 2}).run()["closed"]) == 2


def test_probe_git_reports_missing_path_as_proven_absent(tmp_path):
    assert probe_git(str(tmp_path / "nope")) == {"exists": False}


# -- configuration --------------------------------------------------------

def test_agent_cleanup_config_defaults_off_and_parses():
    assert SessionLifecycleConfig().agent_cleanup.enabled is False
    cfg = _load_session_lifecycle_config({"enabled": True, "agent_cleanup": {
        "enabled": True, "dry_run": False, "idle_hours": 3, "agent_commands": ["claude", "codex"]}})
    assert cfg.agent_cleanup.enabled and not cfg.agent_cleanup.dry_run
    assert cfg.agent_cleanup.idle_hours == 3.0
    with pytest.raises(ValueError):
        _load_session_lifecycle_config({"agent_cleanup": {"idle_hours": 0.1}})
    with pytest.raises(ValueError):
        _load_session_lifecycle_config({"agent_cleanup": {"agent_commands": []}})


# -- delete confirmation paths ---------------------------------------------

def _service(tmp_path, **lifecycle):
    return TerminalService(AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("rc-*",),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),),
                                                 **lifecycle)))


def test_confirmation_required_names_the_supported_path(tmp_path):
    result = _service(tmp_path).terminal_delete_session("rc-x")
    assert result["error"] == "CONFIRMATION_REQUIRED"
    assert "args={'confirm': true}" in result["next_action"]


def test_capacity_reclaim_supplies_confirmation(tmp_path):
    service = _service(tmp_path, reap_idle_sessions=True)
    calls = []
    service._idle_reap_candidates = lambda: [SimpleNamespace(name="rc-idle")]
    service.terminal_delete_session = lambda name, **kw: calls.append((name, kw)) or {"action": "deleted"}
    assert service._reclaim_idle_sessions(1)["reclaimed"] == ["rc-idle"]
    assert calls == [("rc-idle", {"confirm": True, "requested_by": "idle-reaper"})]


def test_legacy_delete_translation_keeps_confirm():
    translated = translate_legacy_call("terminal_delete_session", {"name": "x", "confirm": True})
    assert translated["action"] == "delete_session"
    assert translated["args"] == {"confirm": True}
    assert "args" not in translate_legacy_call("terminal_delete_session", {"name": "x"})


def test_compact_reconcile_routes_args_and_target():
    calls = []
    tools = CompactTerminalTools(None, None, handlers={
        "session_reconcile": lambda **kw: calls.append(("reconcile", kw)) or {"ok": True},
        "session_lifecycle": lambda **kw: calls.append(("lifecycle", kw)) or {"ok": True}})
    assert tools.turn(action="session_reconcile", args={"dry_run": True})["status"] == "OK"
    assert tools.turn(action="session_lifecycle", target="nf21")["status"] == "OK"
    assert calls == [("reconcile", {"dry_run": True}), ("lifecycle", {"session": "nf21"})]
    assert tools.turn(action="session_reconcile", args={"force": True})["error"] == "UNKNOWN_ARGS"
