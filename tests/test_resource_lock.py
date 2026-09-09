"""P0.6 named-resource ownership lock (lease.ResourceLockStore).

Two things are being proven here, and they are different:

  1. The NEW behaviour a resource lock needs -- project scoping, holder
     reporting, all-or-nothing multi-acquire, operator override.
  2. That generalising the pane lease did NOT change the pane lease.
     PaneLeaseStore is on core.py's send hot path; the strongest
     available evidence is that the generated SQL is byte-identical to
     the statement that shipped, which is asserted directly below rather
     than inferred from the behavioural tests passing.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from terminal_mcp.lease import (
    DEFAULT_RESOURCE_LOCK_TTL_SECONDS,
    PaneLeaseStore,
    ResourceLockStore,
    resource_lock_key,
)

PROJECT = "git:github.com/acme/widget"
OTHER_PROJECT = "git:github.com/acme/gadget"


@pytest.fixture
def locks(tmp_path) -> ResourceLockStore:
    return ResourceLockStore(tmp_path / "leases.db")


# -- 1. The pane lease is untouched --------------------------------------

def test_generated_pane_sql_is_byte_identical_to_the_shipped_statement():
    """The pane lease's atomic acquire was arrived at by reproducing a
    real race. Extracting it must not have altered one character of it."""
    shipped = ("INSERT INTO pane_leases (pane_key, owner_id, acquired_at, expires_at, renewed_at) "
               "VALUES (?, ?, ?, ?, ?) "
               "ON CONFLICT(pane_key) DO UPDATE SET owner_id = excluded.owner_id, "
               "acquired_at = excluded.acquired_at, expires_at = excluded.expires_at, "
               "renewed_at = excluded.renewed_at "
               "WHERE pane_leases.owner_id = excluded.owner_id "
               "OR pane_leases.expires_at < excluded.acquired_at")
    generated = PaneLeaseStore._acquire_sql(PaneLeaseStore.__new__(PaneLeaseStore))
    assert generated == shipped


def test_pane_lease_public_surface_is_unchanged():
    """Signatures, not just names -- core.py calls these positionally."""
    import inspect
    # Annotations are strings here (`from __future__ import annotations`),
    # which is exactly how they read in the shipped module too.
    assert str(inspect.signature(PaneLeaseStore.acquire)) == \
        "(self, pane_key: 'str', owner_id: 'str', *, ttl_seconds: 'float' = 20.0) -> 'bool'"
    assert str(inspect.signature(PaneLeaseStore.release)) == \
        "(self, pane_key: 'str', owner_id: 'str') -> 'bool'"
    assert str(inspect.signature(PaneLeaseStore.renew)) == \
        "(self, pane_key: 'str', owner_id: 'str', *, ttl_seconds: 'float' = 20.0) -> 'bool'"
    assert str(inspect.signature(PaneLeaseStore.holder)) == \
        "(self, pane_key: 'str') -> 'dict[str, Any] | None'"


def test_a_pane_lease_and_a_resource_lock_never_collide(tmp_path):
    """Same file, separate tables, no interference in either direction."""
    panes = PaneLeaseStore(tmp_path / "leases.db")
    locks = ResourceLockStore(tmp_path / "leases.db")
    assert panes.acquire("same-key", "owner-A") is True
    assert locks.acquire(PROJECT, "same-key", "owner-B")["acquired"] is True
    assert panes.holder("same-key")["owner_id"] == "owner-A"
    assert locks.holder(PROJECT, "same-key")["owner_id"] == "owner-B"
    # And pruning one does not touch the other.
    locks.release(PROJECT, "same-key", "owner-B")
    assert panes.holder("same-key") is not None


# -- 2. Core acquire / release semantics ---------------------------------

def test_acquire_release_round_trip(locks):
    result = locks.acquire(PROJECT, "src/app.py", "worker-A", reason="refactor")
    assert result["acquired"] is True
    assert result["lock"]["owner_id"] == "worker-A"
    assert result["lock"]["reason"] == "refactor"
    assert result["lock"]["expired"] is False
    assert locks.release(PROJECT, "src/app.py", "worker-A") is True
    assert locks.holder(PROJECT, "src/app.py") is None


def test_a_second_owner_is_refused_and_told_who_holds_it(locks):
    """A primitive that only says "no" leaves the caller nothing to act
    on -- the refusal must name the holder and the expiry."""
    locks.acquire(PROJECT, "branch:main", "worker-A", reason="rebasing")
    denied = locks.acquire(PROJECT, "branch:main", "worker-B")
    assert denied["acquired"] is False
    assert denied["holder"]["owner_id"] == "worker-A"
    assert denied["holder"]["reason"] == "rebasing"
    assert denied["holder"]["expires_at"]
    assert denied["resource_key"] == "branch:main"


def test_reacquire_by_the_same_owner_is_idempotent_and_extends(locks):
    first = locks.acquire(PROJECT, "src/app.py", "worker-A", ttl_seconds=5)
    time.sleep(0.02)
    again = locks.acquire(PROJECT, "src/app.py", "worker-A", ttl_seconds=60)
    assert again["acquired"] is True
    assert again["lock"]["expires_at"] > first["lock"]["expires_at"]


def test_an_expired_lock_is_reclaimed_by_a_new_owner(locks):
    locks.acquire(PROJECT, "src/app.py", "crashed-worker", ttl_seconds=-1)
    held = locks.holder(PROJECT, "src/app.py")
    assert held["expired"] is True, "an expired row is reported, not hidden"
    taken = locks.acquire(PROJECT, "src/app.py", "worker-B")
    assert taken["acquired"] is True and taken["lock"]["owner_id"] == "worker-B"


def test_release_never_removes_another_owners_lock(locks):
    locks.acquire(PROJECT, "src/app.py", "worker-A")
    assert locks.release(PROJECT, "src/app.py", "worker-B") is False
    assert locks.holder(PROJECT, "src/app.py")["owner_id"] == "worker-A"


def test_renew_requires_being_the_current_holder(locks):
    locks.acquire(PROJECT, "src/app.py", "worker-A", ttl_seconds=60)
    assert locks.renew(PROJECT, "src/app.py", "worker-B")["renewed"] is False
    renewed = locks.renew(PROJECT, "src/app.py", "worker-A", ttl_seconds=120)
    assert renewed["renewed"] is True


def test_renew_fails_once_the_lock_has_expired(locks):
    """The stop-renewing-means-gone contract: an owner that let its lock
    lapse must re-acquire (and may lose the race), never silently extend."""
    locks.acquire(PROJECT, "src/app.py", "worker-A", ttl_seconds=-1)
    result = locks.renew(PROJECT, "src/app.py", "worker-A")
    assert result["renewed"] is False
    assert "expired" in result["reason"]


def test_release_all_drops_every_lock_an_owner_holds(locks):
    for key in ("a.py", "b.py", "c.py"):
        locks.acquire(PROJECT, key, "worker-A")
    locks.acquire(OTHER_PROJECT, "d.py", "worker-A")
    locks.acquire(PROJECT, "e.py", "worker-B")

    assert locks.release_all("worker-A", project_id=PROJECT) == 3
    assert locks.holder(OTHER_PROJECT, "d.py") is not None      # other project untouched
    assert locks.holder(PROJECT, "e.py") is not None            # other owner untouched
    assert locks.release_all("worker-A") == 1                   # the remaining one


# -- 3. Project scoping ---------------------------------------------------

def test_the_same_resource_key_in_two_projects_is_two_locks(locks):
    """Without project scoping, two unrelated repos with a src/app.py
    would block each other -- the whole point of a canonical project id."""
    assert locks.acquire(PROJECT, "src/app.py", "worker-A")["acquired"] is True
    assert locks.acquire(OTHER_PROJECT, "src/app.py", "worker-B")["acquired"] is True
    assert locks.holder(PROJECT, "src/app.py")["owner_id"] == "worker-A"
    assert locks.holder(OTHER_PROJECT, "src/app.py")["owner_id"] == "worker-B"


def test_the_composite_key_cannot_be_collided_by_a_crafted_input(locks):
    """A resource_key containing the separator would let one caller
    address another project's lock. Control characters are rejected, so
    the separator can never appear inside a part."""
    with pytest.raises(ValueError, match="control characters"):
        resource_lock_key(PROJECT, "src\x1fapp.py")
    with pytest.raises(ValueError, match="control characters"):
        locks.acquire(PROJECT, "a\x1fb", "worker-A")
    with pytest.raises(ValueError):
        locks.acquire("", "src/app.py", "worker-A")
    with pytest.raises(ValueError):
        locks.acquire(PROJECT, "src/app.py", "   ")


def test_lock_key_is_never_exposed_to_callers(locks):
    """It is an internal composite; exposing it invites hand-built keys
    that skip validation."""
    result = locks.acquire(PROJECT, "src/app.py", "worker-A")
    assert "lock_key" not in result["lock"]
    assert "lock_key" not in locks.holder(PROJECT, "src/app.py")
    assert all("lock_key" not in row for row in locks.list_locks())


# -- 4. All-or-nothing multi-acquire (the deadlock story) ----------------

def test_acquire_many_takes_every_lock_or_none(locks):
    locks.acquire(PROJECT, "b.py", "worker-B")
    result = locks.acquire_many(PROJECT, ["a.py", "b.py", "c.py"], "worker-A")
    assert result["acquired"] is False
    assert result["conflict"] == "b.py"
    assert result["holder"]["owner_id"] == "worker-B"
    # NOTHING was taken -- not even a.py, which was free.
    assert locks.holder(PROJECT, "a.py") is None
    assert locks.holder(PROJECT, "c.py") is None


def test_acquire_many_succeeds_when_the_whole_set_is_free(locks):
    result = locks.acquire_many(PROJECT, ["c.py", "a.py", "b.py"], "worker-A", reason="cross-cutting")
    assert result["acquired"] is True
    assert result["resource_keys"] == ["a.py", "b.py", "c.py"]      # sorted, deterministic order
    for key in ("a.py", "b.py", "c.py"):
        assert locks.holder(PROJECT, key)["owner_id"] == "worker-A"


def test_acquire_many_rejects_an_empty_set(locks):
    with pytest.raises(ValueError):
        locks.acquire_many(PROJECT, [], "worker-A")


def test_two_agents_needing_the_same_pair_cannot_deadlock(locks):
    """The classic: A holds x wants y, B holds y wants x. With
    acquire_many one of them gets BOTH and the other gets NEITHER, so
    neither can ever be stuck holding half the set."""
    results: dict[str, dict] = {}
    barrier = threading.Barrier(2)

    def contend(owner: str, order: list[str]) -> None:
        barrier.wait()
        results[owner] = locks.acquire_many(PROJECT, order, owner)

    threads = [threading.Thread(target=contend, args=("worker-A", ["x.py", "y.py"])),
               threading.Thread(target=contend, args=("worker-B", ["y.py", "x.py"]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [owner for owner, r in results.items() if r["acquired"]]
    assert len(winners) == 1, results
    winner = winners[0]
    assert locks.holder(PROJECT, "x.py")["owner_id"] == winner
    assert locks.holder(PROJECT, "y.py")["owner_id"] == winner


def test_only_one_of_many_concurrent_acquirers_wins(locks):
    """The same race the pane lease's single-statement acquire was built
    for, on the inherited implementation."""
    outcomes: list[bool] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def grab(n: int) -> None:
        barrier.wait()
        got = locks.acquire(PROJECT, "hot.py", f"worker-{n}")["acquired"]
        with lock:
            outcomes.append(got)

    threads = [threading.Thread(target=grab, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count(True) == 1, outcomes


# -- 5. Operator override -------------------------------------------------

def test_force_release_breaks_any_lock_and_names_the_previous_holder(locks):
    locks.acquire(PROJECT, "branch:main", "worker-A", reason="rebasing", ttl_seconds=3600)
    result = locks.force_release(PROJECT, "branch:main", actor="operator",
                                 reason="worker-A's node was rebuilt")
    assert result["released"] is True
    assert result["previous_holder"]["owner_id"] == "worker-A"
    assert result["actor"] == "operator" and "rebuilt" in result["reason"]
    assert locks.holder(PROJECT, "branch:main") is None
    assert locks.acquire(PROJECT, "branch:main", "worker-B")["acquired"] is True


def test_force_release_demands_an_actor_and_a_reason(locks):
    """Breaking someone else's lock is a different action from releasing
    your own, and must never be doable without saying who and why."""
    locks.acquire(PROJECT, "branch:main", "worker-A")
    with pytest.raises(ValueError):
        locks.force_release(PROJECT, "branch:main", actor="", reason="x")
    with pytest.raises(ValueError):
        locks.force_release(PROJECT, "branch:main", actor="operator", reason="")
    assert locks.holder(PROJECT, "branch:main")["owner_id"] == "worker-A"


def test_force_release_of_an_unheld_resource_is_not_an_error(locks):
    result = locks.force_release(PROJECT, "nothing.py", actor="operator", reason="tidy")
    assert result["released"] is False and result["previous_holder"] is None


# -- 6. Reads, pruning, durability ---------------------------------------

def test_list_locks_filters_and_hides_expired_by_default(locks):
    locks.acquire(PROJECT, "a.py", "worker-A")
    locks.acquire(OTHER_PROJECT, "b.py", "worker-B")
    locks.acquire(PROJECT, "stale.py", "worker-C", ttl_seconds=-1)

    live = locks.list_locks()
    assert {r["resource_key"] for r in live} == {"a.py", "b.py"}
    assert {r["resource_key"] for r in locks.list_locks(project_id=PROJECT)} == {"a.py"}
    assert {r["resource_key"] for r in locks.list_locks(owner_id="worker-B")} == {"b.py"}
    everything = locks.list_locks(include_expired=True)
    assert {r["resource_key"] for r in everything} == {"a.py", "b.py", "stale.py"}
    assert next(r for r in everything if r["resource_key"] == "stale.py")["expired"] is True


def test_prune_removes_only_long_expired_rows(locks):
    locks.acquire(PROJECT, "live.py", "worker-A", ttl_seconds=600)
    locks.acquire(PROJECT, "old.py", "worker-B", ttl_seconds=-1000)
    assert locks.prune_expired(grace_seconds=1) == 1
    assert locks.holder(PROJECT, "live.py") is not None
    assert locks.holder(PROJECT, "old.py") is None


def test_a_lock_survives_a_new_store_instance(tmp_path):
    """Cross-process durability -- the reason this is SQLite and not an
    in-process dict, exactly as for the pane lease."""
    first = ResourceLockStore(tmp_path / "leases.db")
    first.acquire(PROJECT, "src/app.py", "worker-A", ttl_seconds=600, reason="long edit")

    second = ResourceLockStore(tmp_path / "leases.db")
    held = second.holder(PROJECT, "src/app.py")
    assert held["owner_id"] == "worker-A" and held["reason"] == "long edit"
    assert second.acquire(PROJECT, "src/app.py", "worker-B")["acquired"] is False
    assert second.renew(PROJECT, "src/app.py", "worker-A")["renewed"] is True


def test_migration_is_idempotent_and_additive(tmp_path):
    path = tmp_path / "leases.db"
    PaneLeaseStore(path)
    panes = PaneLeaseStore(path)
    panes.acquire("pane-1", "owner-A")
    ResourceLockStore(path)
    ResourceLockStore(path)

    connection = sqlite3.connect(path)
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"pane_leases", "resource_locks"} <= tables
    assert connection.execute("SELECT COUNT(*) FROM pane_leases").fetchone()[0] == 1
    assert connection.execute("PRAGMA user_version").fetchone()[0] >= 2
    connection.close()


def test_default_ttl_matches_the_task_lease_cadence():
    """A worker renewing its queue task and its locks should be able to do
    both on one timer."""
    from terminal_mcp.queue_store import QueueStore
    import inspect
    task_default = inspect.signature(QueueStore.renew_task_lease).parameters["lease_seconds"].default
    assert DEFAULT_RESOURCE_LOCK_TTL_SECONDS == task_default == 300.0
