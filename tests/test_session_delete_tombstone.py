"""Explicit deletes are durable: a deleted session never comes back.

Live incident 2026-10-04: CDTM sessions "deleted" three times kept showing up
in list_sessions as RECOVERY_REQUIRED. Two defects combined:

* terminal_turn had no top-level `confirm`, so `delete(confirm=true)` was
  silently dropped by the schema and every delete was CONFIRMATION_REQUIRED
  -- the tmux session was never killed.
* the sessions' pane cwd was the shared primary checkout, whose unrelated
  dirt classified every one of them DIRTY_WORKTREE (and checkpointed that
  dirt under each session's name, 60+ times).

Plus the latent resurrection path the tombstone closes: a registry reconcile
pass that listed tmux BEFORE a delete and wrote AFTER it flipped the KILLED
row back to ACTIVE; the next pass marked it MISSING, which auto-recovery
relaunches.

Real tmux (per-run isolated socket, see conftest) for the end-to-end paths;
real git repos for classification.
"""
from __future__ import annotations

import sqlite3
import subprocess
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from terminal_mcp.compact_tools import CompactTerminalTools
from terminal_mcp.config import AgentCleanupConfig, AppConfig, PermissionsConfig, SessionLifecycleConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.grants import SessionGrantStore
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.session_reconciler import (CLOSED, RECOVERY_REQUIRED, LifecycleStore, SessionReconciler)
from terminal_mcp.session_registry import (STATUS_ACTIVE, STATUS_KILLED, STATUS_MISSING, SessionRegistryStore,
                                           instance_identity)

from tests import tmux_isolation
from tests.conftest import tmux_cmd
from tests.test_session_lifecycle_reconciler import (NOW, FakeTerminal, Info, add_worktree, by_name, git,
                                                     reconciler)
from tests.test_session_lifecycle_reconciler import repo  # noqa: F401 -- pytest fixture

NODE = "test-node"


# -- registry tombstone ------------------------------------------------------

def _registry(tmp_path) -> SessionRegistryStore:
    return SessionRegistryStore(tmp_path / "registry.db")


def _sighting(name, identity, created_epoch):
    return {"session_name": name, "cwd": None, "agent_type": "shell", "identity": identity,
            "created_epoch": created_epoch}


def test_stale_inventory_replay_cannot_revive_deleted_instance(tmp_path):
    registry = _registry(tmp_path)
    registry.upsert_seen_many(NODE, [_sighting("s", "$7@1000", 1000)])
    registry.mark_killed(NODE, "s", killed_by="mcp", identity="$7@1000")
    # A pass that listed tmux before the delete writes after it.
    registry.upsert_seen_many(NODE, [_sighting("s", "$7@1000", 1000)])
    registry.upsert_seen(NODE, "s", identity="$7@1000", created_epoch=1000)
    record = registry.get(NODE, "s")
    assert record.status == STATUS_KILLED and record.killed_at
    assert record.tombstone_identity == "$7@1000" and record.generation == 1
    # ...and the next pass, which no longer sees it, must not turn the
    # tombstone into MISSING (the status auto-recovery relaunches).
    assert registry.mark_missing(NODE, set()) == []
    assert registry.get(NODE, "s").status == STATUS_KILLED


def test_recreate_same_name_is_a_new_generation(tmp_path):
    registry = _registry(tmp_path)
    registry.upsert_seen_many(NODE, [_sighting("s", "$7@1000", 1000)])
    registry.mark_killed(NODE, "s", identity="$7@1000")
    registry.upsert_seen_many(NODE, [_sighting("s", "$9@2000", 2000)])
    record = registry.get(NODE, "s")
    assert record.status == STATUS_ACTIVE and record.killed_at is None
    assert record.generation == 2 and record.tombstone_identity is None
    registry.mark_killed(NODE, "s", identity="$9@2000")
    record = registry.get(NODE, "s")
    assert record.status == STATUS_KILLED and record.generation == 2
    assert record.tombstone_identity == "$9@2000"


def test_explicit_create_without_identity_is_always_a_new_generation(tmp_path):
    registry = _registry(tmp_path)
    registry.mark_killed(NODE, "s", identity="$7@1000")
    registry.upsert_seen(NODE, "s", agent_type="shell", created_by_controller=True)
    assert registry.get(NODE, "s").status == STATUS_ACTIVE
    assert registry.get(NODE, "s").generation == 2


def test_repeated_delete_keeps_the_tombstone_identity(tmp_path):
    registry = _registry(tmp_path)
    registry.mark_killed(NODE, "s", identity="$7@1000")
    registry.mark_killed(NODE, "s")  # second delete: already gone, identity unknown
    assert registry.get(NODE, "s").tombstone_identity == "$7@1000"
    registry.upsert_seen_many(NODE, [_sighting("s", "$7@1000", 1000)])
    assert registry.get(NODE, "s").status == STATUS_KILLED


def test_legacy_tombstone_without_identity_falls_back_to_kill_time(tmp_path):
    registry = _registry(tmp_path)
    registry.mark_killed(NODE, "s", now="2026-10-04T10:00:00+00:00")
    killed = 1791108000  # 2026-10-04T10:00:00Z
    registry.upsert_seen_many(NODE, [_sighting("s", "$1@x", killed - 3600)])
    assert registry.get(NODE, "s").status == STATUS_KILLED  # instance older than the kill
    registry.upsert_seen_many(NODE, [_sighting("s", "$2@y", killed + 60)])
    assert registry.get(NODE, "s").status == STATUS_ACTIVE  # created after it: new


def test_migration_backfills_generation_and_tombstone_columns(tmp_path):
    path = tmp_path / "registry.db"
    SessionRegistryStore(path).upsert_seen(NODE, "old", agent_type="shell")
    with sqlite3.connect(path) as connection:  # roll the file back to schema v4
        connection.execute("ALTER TABLE session_records DROP COLUMN generation")
        connection.execute("ALTER TABLE session_records DROP COLUMN tombstone_identity")
        connection.execute("PRAGMA user_version = 4")
    record = SessionRegistryStore(path).get(NODE, "old")
    assert record.generation == 1 and record.tombstone_identity is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] >= 5


# -- real tmux, end to end -----------------------------------------------------

def _config(tmp_path, **lifecycle) -> AppConfig:
    return AppConfig(
        permissions=PermissionsConfig(True, True), allowed_session_patterns=("lifecycle-*",),
        session_lifecycle=SessionLifecycleConfig(enabled=True, allowed_cwd_roots=(str(tmp_path),),
                                                 protected_sessions=("terminal-mcp",), **lifecycle))


def _service(tmp_path, **lifecycle) -> TerminalService:
    """Each call is a fresh process's view of the same durable stores --
    calling it twice is how these tests simulate a controller restart."""
    service = TerminalService(_config(tmp_path, **lifecycle),
                              session_registry=SessionRegistryStore(tmp_path / "registry.db"),
                              grants=SessionGrantStore(tmp_path / "grants.db"))
    service.lifecycle_store = LifecycleStore(tmp_path / "lifecycle.db")
    return service


@pytest.fixture
def name():
    created = tmux_isolation.owned_name("tomb", prefix="lifecycle")
    yield created
    tmux_isolation.kill_session(created)


def _listed(service, session) -> bool:
    return session in {row["name"] for row in service.terminal_list_sessions()["sessions"]}


def test_delete_is_durable_across_stale_replay_and_restart(tmp_path, name):
    service = _service(tmp_path)
    assert service.terminal_create_session(name, agent_type="shell", cwd=str(tmp_path))["state"] == "READY"
    assert _listed(service, name)  # registry now holds an ACTIVE row
    before_delete = service.tmux.list_sessions()  # snapshot a slow list pass took
    info = service.tmux.get_session(name)

    result = service.terminal_delete_session(name, confirm=True, requested_by="test")
    receipt = result["receipt"]
    assert result["deleted"] is True and receipt["tmux_kill"] == "killed"
    assert receipt["tmux_absent_verified"] is True
    assert receipt["instance"] == instance_identity(info.session_id, info.created_epoch)
    assert receipt["registry_tombstone"]["status"] == STATUS_KILLED
    assert receipt["registry_tombstone"]["tombstone_identity"] == receipt["instance"]

    # The slow pass now writes its pre-delete snapshot.
    service._reconcile_session_registry(before_delete, {})
    record = service.session_registry.get(service.REGISTRY_LOCAL_NODE_ID, name)
    assert record.status == STATUS_KILLED

    # Repeated delete: idempotent, tombstone intact.
    again = service.terminal_delete_session(name, confirm=True, requested_by="test")
    assert again["action"] == "already_gone" and again["receipt"]["tmux_kill"] == "already_gone"
    assert again["receipt"]["registry_tombstone"]["tombstone_identity"] == receipt["instance"]

    # Restart: a new service over the same stores, several reconcile passes.
    restarted = _service(tmp_path)
    for _ in range(3):
        assert not _listed(restarted, name)
    record = restarted.session_registry.get(restarted.REGISTRY_LOCAL_NODE_ID, name)
    assert record.status == STATUS_KILLED and record.status != STATUS_MISSING
    assert not record.recoverable or record.status == STATUS_KILLED  # auto-recovery skips KILLED


def test_recreate_after_delete_is_new_generation(tmp_path, name):
    service = _service(tmp_path)
    service.terminal_create_session(name, agent_type="shell", cwd=str(tmp_path))
    service.terminal_list_sessions()
    service.terminal_delete_session(name, confirm=True)
    assert service.terminal_create_session(name, agent_type="shell", cwd=str(tmp_path))["state"] == "READY"
    assert _listed(service, name)
    record = service.session_registry.get(service.REGISTRY_LOCAL_NODE_ID, name)
    assert record.status == STATUS_ACTIVE and record.generation == 2


def test_unconfirmed_delete_says_nothing_was_deleted(tmp_path, name):
    service = _service(tmp_path)
    service.terminal_create_session(name, agent_type="shell", cwd=str(tmp_path))
    result = service.terminal_delete_session(name)
    assert result["error"] == "CONFIRMATION_REQUIRED" and result["deleted"] is False
    assert result["receipt"]["tmux_kill"] == "not_attempted"
    assert _listed(service, name)


def test_delete_closes_the_lifecycle_record(tmp_path, name):
    service = _service(tmp_path)
    service.terminal_create_session(name, agent_type="shell", cwd=str(tmp_path))
    info = service.tmux.get_session(name)
    service.lifecycle_store.put({"session": name, "identity": f"{info.session_id}@{info.created_epoch}",
                                 "state": RECOVERY_REQUIRED, "reason": "uncontrolled (DIRTY_WORKTREE)",
                                 "first_uncontrolled_at": NOW, "updated_at": NOW})
    result = service.terminal_delete_session(name, confirm=True, requested_by="test")
    assert result["receipt"]["lifecycle"] == CLOSED
    stored = service.lifecycle_store.get(name)
    assert stored["state"] == CLOSED and "deleted explicitly by test" in stored["reason"]
    rec = SessionReconciler(service, store=service.lifecycle_store,
                            policy=AgentCleanupConfig(enabled=True, dry_run=True))
    assert name not in rec.lifecycle_index() and rec.lifecycle_for(name) is None


def test_capacity_does_not_count_deleted_sessions(tmp_path, name):
    service = _service(tmp_path, max_sessions=1)
    other = tmux_isolation.owned_name("tomb2", prefix="lifecycle")
    try:
        for session in [s.name for s in service.tmux.list_sessions()]:
            subprocess.run([*tmux_cmd(), "kill-session", "-t", session], capture_output=True, check=False)
        assert service.terminal_create_session(name, agent_type="shell", cwd=str(tmp_path))["state"] == "READY"
        assert service.terminal_create_session(other, agent_type="shell",
                                               cwd=str(tmp_path))["error"] == "NODE_AT_SESSION_CAPACITY"
        service.terminal_delete_session(name, confirm=True)
        service.terminal_list_sessions()
        assert service.terminal_create_session(other, agent_type="shell", cwd=str(tmp_path))["state"] == "READY"
    finally:
        tmux_isolation.kill_session(other)


def test_remote_node_delete_returns_receipt_and_tombstones(tmp_path, name):
    terminal = _service(tmp_path)
    app = build_node_agent(node_id=NODE, terminal=terminal, token="tok", workspace_root=str(tmp_path))
    client = TestClient(app)
    auth = {"Authorization": "Bearer tok"}
    assert client.post("/v1/sessions", headers=auth, json={"name": name, "agent_type": "shell"}).status_code == 200
    client.get("/v1/sessions", headers=auth)
    body = client.request("DELETE", f"/v1/sessions/{name}", headers=auth,
                          json={"confirm": True, "requested_by": "controller"}).json()
    assert body["receipt"]["tmux_kill"] == "killed"
    assert body["receipt"]["registry_tombstone"]["status"] == STATUS_KILLED
    listed = client.get("/v1/sessions", headers=auth).json()["sessions"]
    assert name not in {row["name"] for row in listed}


# -- terminal_turn confirm spellings ---------------------------------------------

def _turn_tools():
    calls = []

    def delete(name, **kw):
        calls.append((name, kw))
        return {"session": name, "deleted": True} if kw.get("confirm") is True else {
            "error": "CONFIRMATION_REQUIRED", "session": name}
    return CompactTerminalTools(None, None, handlers={"delete_session": delete}), calls


def test_turn_delete_accepts_top_level_confirm():
    tools, calls = _turn_tools()
    assert tools.turn(action="delete", target="x", confirm=True)["status"] == "OK"
    assert calls == [("x", {"confirm": True})]


def test_turn_delete_confirm_spellings_agree_or_fail():
    tools, calls = _turn_tools()
    assert tools.turn(action="delete", target="x", confirm=True, args={"confirm": True})["status"] == "OK"
    bad = tools.turn(action="delete", target="x", confirm=False, args={"confirm": True})
    assert bad["error"] == "INVALID_ARGUMENT"
    refused = tools.turn(action="delete", target="x")
    assert refused["status"] == "FAILED" and refused["result"]["error"] == "CONFIRMATION_REQUIRED"


# -- lifecycle classification: shared checkout vs owned worktree ------------------

class _Registry:
    def __init__(self, worktrees):
        self.worktrees = worktrees

    def get(self, node_id, name):
        path = self.worktrees.get(name)
        return SimpleNamespace(worktree_path=path) if path else None


def _with_registry(term, worktrees):
    term.session_registry = _Registry(worktrees)
    term.REGISTRY_LOCAL_NODE_ID = "local"
    return term


def _checkpoint_refs(repo_path):
    out = subprocess.run(["git", "-C", str(repo_path), "for-each-ref", "refs/terminal-mcp"],
                         capture_output=True, text=True, check=True).stdout
    return out.strip()


def test_shared_primary_checkout_dirt_is_not_this_sessions_work(repo, tmp_path):  # noqa: F811
    (repo / "PROJECT_CONTEXT.md").write_text("someone else's edit\n")
    git(repo, "checkout", "-q", "-b", "feature-checked-out-in-primary")
    term = _with_registry(FakeTerminal([Info("cdtm-a", "claude", str(repo), session_id="$1"),
                                        Info("cdtm-b", "claude", str(repo), session_id="$2")]), {})
    report = reconciler(term, tmp_path).run()
    rows = by_name(report)
    for session in ("cdtm-a", "cdtm-b"):
        assert rows[session]["classification"] == "IDLE_PRIMARY_CHECKOUT"
        assert "not attributed" in rows[session]["reason"]
    assert report["checkpoints"] == [] and _checkpoint_refs(repo) == ""
    assert term.deletes == []


def test_owned_worktree_gone_is_complete_despite_dirty_shared_checkout(repo, tmp_path):  # noqa: F811
    wt = add_worktree(repo, "task-owned", commit=True, merge=True)
    git(repo, "worktree", "remove", str(wt))
    (repo / "PROJECT_CONTEXT.md").write_text("unrelated dirt\n")
    term = _with_registry(FakeTerminal([Info("owned-agent", "claude", str(repo))]),
                          {"owned-agent": str(wt)})
    report = reconciler(term, tmp_path).run()
    assert report["closed"][0]["classification"] == "ORPHAN_WORKTREE_MISSING"
    assert term.deletes == [("owned-agent", "session-reconciler")]


def test_owned_worktree_merged_clean_is_complete(repo, tmp_path):  # noqa: F811
    wt = add_worktree(repo, "task-merged-owned", commit=True, merge=True)
    (repo / "unrelated.txt").write_text("dirt\n")
    term = _with_registry(FakeTerminal([Info("merged-agent", "claude", str(repo))]),
                          {"merged-agent": str(wt)})
    assert reconciler(term, tmp_path).run()["closed"][0]["classification"] == "COMPLETED_CLEAN"


def test_owned_dirty_worktree_is_still_its_own_work(repo, tmp_path):  # noqa: F811
    wt = add_worktree(repo, "task-wip-owned", commit=True, merge=True)
    (wt / "wip.txt").write_text("mine\n")
    term = _with_registry(FakeTerminal([Info("wip-agent", "claude", str(repo))]), {"wip-agent": str(wt)})
    rows = by_name(reconciler(term, tmp_path).run())
    assert rows["wip-agent"]["classification"] == "DIRTY_WORKTREE"
    assert term.deletes == []


def test_stale_snapshot_never_reopens_a_closed_lifecycle(repo, tmp_path):  # noqa: F811
    store = LifecycleStore(tmp_path / "lifecycle.db")
    info = Info("closed-agent", "claude", str(repo))
    store.put({"session": "closed-agent", "identity": f"{info.session_id}@{info.created_epoch}",
               "state": CLOSED, "reason": "deleted explicitly by mcp", "closed_at": NOW, "updated_at": NOW})
    term = _with_registry(FakeTerminal([info]), {})
    rows = by_name(reconciler(term, tmp_path, store=store).run())
    assert rows["closed-agent"]["lifecycle_state"] == CLOSED
    assert store.get("closed-agent")["state"] == CLOSED and term.deletes == []
