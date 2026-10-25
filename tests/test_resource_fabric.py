"""Issue #96: isolated resource lease admission contracts."""
from concurrent.futures import ThreadPoolExecutor
import pytest

from terminal_mcp.resource_fabric import CapacityUnavailable, NodeOffer, ResourceLeaseStore


@pytest.fixture
def rig(tmp_path):
    now = [1000.0]
    store = ResourceLeaseStore(tmp_path / "leases.db", clock=lambda: now[0])
    store.update_offer(NodeOffer("a", 2000, 8_000, now[0], max_age_seconds=30))
    return store, now


def test_reservations_are_atomic_across_threads(rig):
    store, _ = rig

    def job(i):
        try:
            return store.reserve(task_id=f"job-{i}", node_id="a", cpu_millis=1000, memory_bytes=6000)
        except CapacityUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(job, range(12)))
    assert sum(x is not None for x in outcomes) == 1
    assert store.snapshot()[0]["memory_bytes_available"] == 2000


def test_lease_expires_and_frees_capacity(rig):
    store, now = rig
    store.reserve(task_id="one", node_id="a", cpu_millis=2000, memory_bytes=8000, ttl_seconds=5)
    now[0] += 6
    store.reserve(task_id="two", node_id="a", cpu_millis=2000, memory_bytes=8000)
    assert store.snapshot()[0]["cpu_millis_available"] == 0


def test_stale_and_unhealthy_offers_cannot_admit(rig):
    store, now = rig
    now[0] += 31
    with pytest.raises(CapacityUnavailable, match="stale"):
        store.reserve(task_id="one", node_id="a", cpu_millis=1000, memory_bytes=1)
    assert store.snapshot()[0]["eligible"] is False
    store.update_offer(NodeOffer("a", 2000, 8000, now[0], health="degraded"))
    with pytest.raises(CapacityUnavailable, match="not execution-healthy"):
        store.reserve(task_id="one", node_id="a", cpu_millis=1000, memory_bytes=1)


def test_idempotence_rejects_unsafe_resize(rig):
    store, _ = rig
    a = store.reserve(task_id="one", node_id="a", cpu_millis=1000, memory_bytes=4000)
    b = store.reserve(task_id="one", node_id="a", cpu_millis=1000, memory_bytes=4000)
    assert a == b
    with pytest.raises(CapacityUnavailable, match="different"):
        store.reserve(task_id="one", node_id="a", cpu_millis=1500, memory_bytes=4000)


def test_renew_and_release(rig):
    store, now = rig
    store.reserve(task_id="one", node_id="a", cpu_millis=1000, memory_bytes=100)
    now[0] += 10
    x = store.renew(task_id="one", node_id="a", ttl_seconds=50)
    assert x.expires_at == 1060
    assert store.release(task_id="one", node_id="a") is True
    assert store.release(task_id="one", node_id="a") is False


def test_update_rejects_stale_rollback_offer(rig):
    store, now = rig
    store.update_offer(NodeOffer("a", 3000, 10_000, now[0]-20))
    assert store.snapshot()[0]["cpu_millis_available"] == 2000


def test_future_timestamps_fail(rig):
    store, now = rig
    with pytest.raises(ValueError, match="future"):
        store.update_offer(NodeOffer("a", 2000, 8000, now[0]+60))
