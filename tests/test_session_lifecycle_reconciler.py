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
from terminal_mcp.config import (AgentCleanupConfig, AppConfig, PermissionsConfig, RequiredServiceConfig,
                                 SessionLifecycleConfig, _load_session_lifecycle_config)
from terminal_mcp.core import TerminalService
from terminal_mcp.session_reconciler import LifecycleStore, SessionReconciler, probe_git

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
    created_epoch: int = int(OLD) - 60


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
    kw.setdefault("store", LifecycleStore(tmp_path / "lifecycle.db"))
    kw.setdefault("clock", lambda: NOW)
    return SessionReconciler(term, policy=policy, **kw)


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


# -- hard lifecycle rule: RECOVERY_REQUIRED -> CLEANUP_ELIGIBLE ---------------

GRACE_S = 30 * 60


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


def git_out(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def test_uncontrolled_recovers_and_is_kept(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    term = FakeTerminal([Info("loose-agent", "codex", str(loose))])
    clock, owner = Clock(), []
    rec = reconciler(term, tmp_path, clock=clock, active_refs=lambda name: list(owner))
    first = by_name(rec.run())["loose-agent"]
    assert first["classification"] == "NO_REPO"
    assert first["lifecycle_state"] == "RECOVERY_REQUIRED" and first["action"] == "recover"
    assert first["lifecycle"]["grace_expires_at"] and first["lifecycle"]["first_uncontrolled_at"]
    # Within the grace period a verified owner appears: control regained.
    clock.now += 10 * 60
    owner.append("supervised task")
    second = by_name(rec.run())["loose-agent"]
    assert second["lifecycle_state"] == "CONTROLLED" and second["lifecycle"]["recovered_at"]
    clock.now += 2 * GRACE_S
    assert by_name(rec.run())["loose-agent"]["lifecycle_state"] == "CONTROLLED"
    assert term.deletes == []
    # Owner gone again: a fresh grace period starts, not the old expired one.
    owner.clear()
    again = by_name(rec.run())["loose-agent"]
    assert again["lifecycle_state"] == "RECOVERY_REQUIRED" and term.deletes == []


def test_pane_output_change_resets_the_grace_timer(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    term = FakeTerminal([Info("loose-agent", "codex", str(loose))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    rec.run()
    clock.now += GRACE_S - 60
    term.tmux.tails["loose-agent"] = AGENT_IDLE_TAIL + ["new output line"]
    row = by_name(rec.run())["loose-agent"]
    assert row["lifecycle_state"] == "RECOVERY_REQUIRED" and row["lifecycle"]["recovered_at"]
    clock.now += 120  # old timer would have expired; the reset one has not
    assert by_name(rec.run())["loose-agent"]["lifecycle_state"] == "RECOVERY_REQUIRED"
    assert term.deletes == []


def test_uncontrolled_grace_expiry_closes_the_session(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    unknown_tail = ["some output", "nothing recognisable here"]
    term = FakeTerminal([Info("loose-agent", "codex", str(loose)),
                         Info("unknown-agent", "claude", str(loose))],
                        tails={"unknown-agent": unknown_tail})
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    first = rec.run()
    assert first["closed"] == [] and set(first["recovery_required"]) == {"loose-agent", "unknown-agent"}
    assert "reprobed_pane" in by_name(first)["unknown-agent"]["recovery_actions"]
    clock.now += GRACE_S + 1
    report = rec.run()
    assert sorted(r["session"] for r in report["closed"]) == ["loose-agent", "unknown-agent"]
    assert all(r["lifecycle_state"] == "CLOSED" for r in report["closed"])
    assert rec.store.get("loose-agent")["state"] == "CLOSED"
    assert rec.store.get("loose-agent")["closed_at"] == clock.now


def test_dirty_uncontrolled_is_checkpointed_then_closed(repo, tmp_path):
    wt = add_worktree(repo, "task-wip", commit=True, merge=False)
    (wt / "a.txt").write_text("edited\n")
    (wt / "new.txt").write_text("untracked work\n")
    status_before = git_out(wt, "status", "--porcelain")
    term = FakeTerminal([Info("wip-agent", "claude", str(wt))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    first = rec.run()
    assert by_name(first)["wip-agent"]["classification"] == "DIRTY_WORKTREE"
    assert by_name(first)["wip-agent"]["lifecycle_state"] == "RECOVERY_REQUIRED"
    checkpoint = first["checkpoints"][0]
    assert checkpoint["kind"] == "dirty" and checkpoint["ref"].startswith("refs/terminal-mcp/checkpoints/wip-agent/")
    # The worktree and its real index are untouched.
    assert git_out(wt, "status", "--porcelain") == status_before
    assert git_out(repo, "show", f"{checkpoint['ref']}:new.txt") == "untracked work"
    assert git_out(repo, "show", f"{checkpoint['ref']}:a.txt") == "edited"
    assert "untracked work" in open(checkpoint["patch"]).read()
    clock.now += GRACE_S + 1
    report = rec.run()
    assert [r["session"] for r in report["closed"]] == ["wip-agent"]
    closed = report["closed"][0]
    assert closed["checkpoint"]["ok"] and closed["checkpoint"]["commit"] == checkpoint["commit"]  # reused
    assert git_out(repo, "rev-parse", checkpoint["ref"]) == checkpoint["commit"]
    assert closed["lifecycle"]["checkpoint"]["ref"] == checkpoint["ref"]


def test_unmerged_clean_branch_is_pinned_then_closed(repo, tmp_path):
    wt = add_worktree(repo, "task-unpushed", commit=True, merge=False)
    head = git_out(wt, "rev-parse", "HEAD")
    term = FakeTerminal([Info("unpushed-agent", "codex", str(wt))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    rec.run()
    clock.now += GRACE_S + 1
    report = rec.run()
    closed = report["closed"][0]
    assert closed["checkpoint"]["kind"] == "unmerged" and closed["checkpoint"]["commit"] == head
    assert git_out(repo, "rev-parse", closed["checkpoint"]["ref"]) == head


def test_failed_checkpoint_fails_closed_and_retries(repo, tmp_path):
    wt = add_worktree(repo, "task-big", commit=True, merge=True)
    (wt / "huge.bin").write_bytes(b"\0" * (2 * 1024 * 1024))
    term = FakeTerminal([Info("big-agent", "claude", str(wt))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock, policy={"checkpoint_max_untracked_mb": 1})
    rec.run()
    clock.now += GRACE_S + 1
    report = rec.run()
    assert report["closed"] == [] and report["refused"][0]["refused"] == "CHECKPOINT_FAILED"
    assert rec.store.get("big-agent")["state"] == "BLOCKED"
    assert by_name(rec.inspect())["big-agent"]["lifecycle_state"] == "BLOCKED"
    assert term.deletes == []
    (wt / "huge.bin").unlink()
    (wt / "small.txt").write_text("small\n")
    clock.now += 60
    assert [r["session"] for r in rec.run()["closed"]] == ["big-agent"]


def test_active_task_is_exempt_however_long(repo, tmp_path):
    wt = add_worktree(repo, "task-busy", commit=True, merge=False)
    (wt / "wip.txt").write_text("in progress\n")
    term = FakeTerminal([Info("busy-agent", "claude", str(wt))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock, active_refs=lambda name: ["queue task"])
    for _ in range(4):
        row = by_name(rec.run())["busy-agent"]
        assert row["classification"] == "ACTIVE_TASK" and row["lifecycle_state"] == "CONTROLLED"
        clock.now += GRACE_S
    assert term.deletes == []


def test_required_service_exempt_only_while_healthy(tmp_path):
    term = FakeTerminal([Info("nf-live", "pnpm", str(tmp_path)),
                         Info("nf-tunnel", "cloudflared", str(tmp_path)),
                         Info("stray-server", "node", str(tmp_path))])
    healthy = {"nf-live": True, "nf-tunnel": False}
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock,
                     health_check=lambda info, spec: (healthy[info.name], "stub"),
                     policy={"required_services": (RequiredServiceConfig("nf-*"),)})
    rows = by_name(rec.run())
    assert rows["nf-live"]["classification"] == "REQUIRED_SERVICE"
    assert rows["nf-live"]["lifecycle_state"] == "CONTROLLED"
    assert rows["nf-tunnel"]["lifecycle_state"] == "RECOVERY_REQUIRED"  # declared but unhealthy
    assert rows["stray-server"]["lifecycle_state"] == "RECOVERY_REQUIRED"  # not declared
    clock.now += GRACE_S + 1
    report = rec.run()
    assert sorted(r["session"] for r in report["closed"]) == ["nf-tunnel", "stray-server"]
    assert by_name(report)["nf-live"]["lifecycle_state"] == "CONTROLLED"
    assert ("nf-live", "session-reconciler") not in term.deletes


def test_recent_service_and_open_editor_are_not_closed(tmp_path):
    term = FakeTerminal([Info("fresh-server", "node", str(tmp_path), activity_epoch=int(NOW - 60)),
                         Info("editing", "vim", str(tmp_path))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    rec.run()
    clock.now += GRACE_S + 1
    rows = by_name(rec.run())
    assert rows["editing"]["lifecycle_state"] == "BLOCKED"
    assert rows["fresh-server"]["classification"] in ("SERVICE_RECENT", "SERVICE")
    assert ("editing", "session-reconciler") not in term.deletes


def test_repeated_cleanup_is_idempotent_and_name_reuse_starts_fresh(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    term = FakeTerminal([Info("loose-agent", "codex", str(loose))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    rec.run()
    clock.now += GRACE_S + 1
    assert len(rec.run()["closed"]) == 1
    for _ in range(3):
        clock.now += GRACE_S + 1
        again = rec.run()
        assert again["closed"] == [] and again["refused"] == []
    assert term.deletes == [("loose-agent", "session-reconciler")]
    assert rec.store.get("loose-agent")["state"] == "CLOSED"
    # A brand-new tmux session reusing the name does not inherit the old timer.
    term.tmux.sessions["loose-agent"] = Info("loose-agent", "codex", str(loose), session_id="$9",
                                             created_epoch=int(clock.now))
    row = by_name(rec.run())["loose-agent"]
    assert row["lifecycle_state"] == "RECOVERY_REQUIRED" and term.deletes == [("loose-agent", "session-reconciler")]


def test_grace_timer_survives_a_restart(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    term = FakeTerminal([Info("loose-agent", "codex", str(loose))])
    store_path = tmp_path / "persist.db"
    clock = Clock()
    reconciler(term, tmp_path, clock=clock, store=LifecycleStore(store_path)).run()
    clock.now += GRACE_S + 1
    restarted = reconciler(term, tmp_path, clock=clock, store=LifecycleStore(store_path))
    assert [r["session"] for r in restarted.run()["closed"]] == ["loose-agent"]


def test_inspect_is_read_only_and_dry_run_never_closes(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    term = FakeTerminal([Info("loose-agent", "codex", str(loose))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock)
    view = rec.inspect()
    assert view["recovery_required"] == ["loose-agent"] and rec.store.get("loose-agent") is None
    rec.run(dry_run=True)  # dry run records the timer...
    clock.now += GRACE_S + 1
    dry = rec.run(dry_run=True)
    assert dry["close_candidates"] == ["loose-agent"] and term.deletes == []  # ...but never closes
    assert rec.lifecycle_for("loose-agent")["state"] == "CLEANUP_ELIGIBLE"
    assert rec.lifecycle_index()["loose-agent"]["grace_expires_at"]


def test_compact_inspect_carries_lifecycle():
    tools = CompactTerminalTools(None, None)
    tools.batch_inspect = lambda targets, **kw: {"targets": [{"target": t} for t in targets]}
    tools.lifecycle_lookup = lambda name: {"state": "RECOVERY_REQUIRED"} if name == "s1" else None
    result = tools.turn(action="inspect", target="s1")
    assert result["result"]["targets"][0]["lifecycle"] == {"state": "RECOVERY_REQUIRED"}


def test_recovery_config_parses_and_validates():
    cfg = _load_session_lifecycle_config({"agent_cleanup": {
        "recovery_grace_minutes": 20, "required_services": [
            "tunnel-*", {"session": "nf-live", "health_url": "http://127.0.0.1:3000/"}]}}).agent_cleanup
    assert cfg.recovery_grace_minutes == 20.0 and cfg.checkpoint_dirty_work is True
    assert cfg.required_services == (RequiredServiceConfig("tunnel-*"),
                                     RequiredServiceConfig("nf-live", "http://127.0.0.1:3000/"))
    assert AgentCleanupConfig().recovery_grace_minutes == 30.0
    for bad in ({"recovery_grace_minutes": 1}, {"recovery_grace_minutes": 0},
                {"required_services": [{"health_url": "x"}]},
                {"required_services": [{"session": "a", "health_url": "ftp://x"}]}):
        with pytest.raises(ValueError):
            _load_session_lifecycle_config({"agent_cleanup": bad})


def test_regenerable_untracked_dirs_are_not_unique_work(repo, tmp_path):
    wt = add_worktree(repo, "task-venv", commit=True, merge=True)
    (wt / ".venv" / "lib").mkdir(parents=True)
    (wt / ".venv" / "lib" / "big.so").write_bytes(b"\0" * (2 * 1024 * 1024))
    term = FakeTerminal([Info("venv-agent", "claude", str(wt))])
    clock = Clock()
    rec = reconciler(term, tmp_path, clock=clock, policy={"checkpoint_max_untracked_mb": 1})
    assert by_name(rec.run())["venv-agent"]["classification"] == "DIRTY_WORKTREE"
    clock.now += GRACE_S + 1
    report = rec.run()
    assert [r["session"] for r in report["closed"]] == ["venv-agent"]
    assert report["closed"][0]["checkpoint"]["kind"] == "none"
    assert git_out(repo, "for-each-ref", "refs/terminal-mcp") == ""


def test_recent_controller_input_counts_as_activity(repo, tmp_path):
    wt = add_worktree(repo, "task-driven", commit=True, merge=False)
    (wt / "wip.txt").write_text("in progress\n")
    term = FakeTerminal([Info("driven-agent", "claude", str(wt))])  # tmux stamp 5h old
    clock, last = Clock(), {"driven-agent": NOW - 600}
    rec = reconciler(term, tmp_path, clock=clock, last_input=lambda name: last.get(name))
    row = by_name(rec.run())["driven-agent"]
    assert row["classification"] == "AGENT_RECENT" and row["lifecycle_state"] == "CONTROLLED"
    clock.now += 3 * 3600  # operator went quiet: the rule applies again
    assert by_name(rec.run())["driven-agent"]["lifecycle_state"] == "RECOVERY_REQUIRED"


def test_audit_last_input_epoch(tmp_path):
    from terminal_mcp.audit import AuditStore
    audit = AuditStore(tmp_path / "audit.db")
    assert audit.last_input_epoch("s1") is None
    audit.record(action="send_text", session="s1", result="BLOCKED", text="x")
    assert audit.last_input_epoch("s1") is None
    audit.record(action="send_text", session="s1", result="SENT", text="x")
    assert abs(audit.last_input_epoch("s1") - __import__("time").time()) < 60


def test_compact_list_sessions_keeps_lifecycle_state():
    tools = CompactTerminalTools(None, None, handlers={"list_sessions": lambda: {"sessions": [
        {"name": "a", "node_id": "local", "lifecycle": {"state": "RECOVERY_REQUIRED", "reason": "x"}},
        {"name": "b", "node_id": "local"}]}})
    rows = tools.turn(action="list_sessions")["result"]["sessions"]
    assert rows[0]["lifecycle_state"] == "RECOVERY_REQUIRED" and "lifecycle" not in rows[0]
    assert "lifecycle_state" not in rows[1]
