"""Unit coverage for lease.py's PaneLeaseStore -- pure acquire/renew/
release/expiry semantics, no tmux involved. Cross-process, real-tmux-pane
integration (concurrent HTTP+STDIO-style sends racing for one pane, a
dashboard+MCP race, restart-while-held, session-recreate, pane-replacement,
stale-lease reclaim, idempotency replay) lives in test_p0_lease_safety.py."""
from __future__ import annotations

import time

from terminal_mcp.lease import PaneLeaseStore


def test_first_acquire_succeeds_second_owner_fails(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    assert store.acquire("pane-1", "owner-a", ttl_seconds=30) is True
    assert store.acquire("pane-1", "owner-b", ttl_seconds=30) is False


def test_same_owner_reacquire_is_idempotent(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    assert store.acquire("pane-1", "owner-a", ttl_seconds=30) is True
    assert store.acquire("pane-1", "owner-a", ttl_seconds=30) is True  # renews, not a conflict


def test_release_only_removes_own_lease(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    store.acquire("pane-1", "owner-a", ttl_seconds=30)
    assert store.release("pane-1", "owner-b") is False  # not the holder -- no-op
    assert store.holder("pane-1")["owner_id"] == "owner-a"
    assert store.release("pane-1", "owner-a") is True
    assert store.holder("pane-1") is None


def test_expired_lease_is_reclaimed_by_a_new_owner(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    assert store.acquire("pane-1", "owner-a", ttl_seconds=0.05) is True
    time.sleep(0.15)
    assert store.acquire("pane-1", "owner-b", ttl_seconds=30) is True
    assert store.holder("pane-1")["owner_id"] == "owner-b"


def test_renew_extends_only_for_current_holder(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    store.acquire("pane-1", "owner-a", ttl_seconds=30)
    assert store.renew("pane-1", "owner-b", ttl_seconds=30) is False  # not the holder
    assert store.renew("pane-1", "owner-a", ttl_seconds=60) is True


def test_independent_panes_never_contend(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    assert store.acquire("pane-1", "owner-a", ttl_seconds=30) is True
    assert store.acquire("pane-2", "owner-b", ttl_seconds=30) is True


def test_prune_expired_removes_old_rows_past_grace(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    store.acquire("pane-1", "owner-a", ttl_seconds=0.05)
    time.sleep(0.15)
    assert store.prune_expired(grace_seconds=0.0) == 1
    assert store.holder("pane-1") is None


def test_lease_db_file_permissions_are_owner_only(tmp_path):
    store = PaneLeaseStore(tmp_path / "leases.db")
    store.acquire("pane-1", "owner-a", ttl_seconds=30)
    mode = (tmp_path / "leases.db").stat().st_mode & 0o777
    assert mode == 0o600
