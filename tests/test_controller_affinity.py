import pytest

from terminal_mcp.controller_affinity import (
    ACTIVE,
    DOWN,
    DRAINING,
    STANDBY,
    AffinityError,
    ControllerAffinityStore,
    ControllerRouter,
)


def backend(store, name, port, state=STANDBY, healthy=True, generation=1):
    return store.upsert_backend(
        name, f"http://127.0.0.1:{port}", generation, state, healthy
    )


def assert_code(code, callable_):
    with pytest.raises(AffinityError) as caught:
        callable_()
    assert caught.value.code == code
    assert str(caught.value)


def test_stateless_active(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "one", 8001, ACTIVE)
    selected = ControllerRouter(store).select_backend()
    assert selected["backend_id"] == "one"
    assert store.sessions_for_backend("one") == []


def test_legacy_binding_remains_old_after_cutover_and_reopen(tmp_path):
    path = tmp_path / "affinity.db"
    store = ControllerAffinityStore(path)
    backend(store, "old", 8001, ACTIVE)
    backend(store, "new", 8002, STANDBY, generation=2)
    router = ControllerRouter(store)
    assert router.select_backend("legacy")["backend_id"] == "old"
    router.begin_cutover("old", "new")

    reopened = ControllerRouter(ControllerAffinityStore(path))
    assert reopened.select_backend("legacy")["backend_id"] == "old"
    assert reopened.store.get_backend("old")["state"] == DRAINING


def test_unknown_new_session_after_cutover_binds_new(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "old", 8001, ACTIVE)
    backend(store, "new", 8002, generation=2)
    router = ControllerRouter(store)
    router.begin_cutover("old", "new")
    assert router.select_backend("fresh")["backend_id"] == "new"
    assert store.sessions_for_backend("new")[0]["mcp_session_id"] == "fresh"


def test_pinned_unhealthy_refuses_without_failover(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "old", 8001, ACTIVE)
    backend(store, "new", 8002, generation=2)
    router = ControllerRouter(store)
    router.select_backend("legacy")
    router.begin_cutover("old", "new")
    store.set_backend_state("old", DRAINING, healthy=False)
    assert_code("SESSION_BACKEND_UNAVAILABLE", lambda: router.select_backend("legacy"))


def test_pinned_down_refuses_without_failover(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "old", 8001, ACTIVE)
    backend(store, "new", 8002, generation=2)
    router = ControllerRouter(store)
    router.select_backend("legacy")
    router.begin_cutover("old", "new")
    store.set_backend_state("old", DOWN)
    assert_code("SESSION_BACKEND_UNAVAILABLE", lambda: router.select_backend("legacy"))


def test_multiple_active_refuses(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "one", 8001, ACTIVE)
    backend(store, "two", 8002, ACTIVE)
    router = ControllerRouter(store)
    assert_code("AMBIGUOUS_ACTIVE_BACKEND", router.select_backend)
    assert_code("AMBIGUOUS_ACTIVE_BACKEND", lambda: router.select_backend("new"))


def test_zero_active_refuses(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "one", 8001, STANDBY)
    assert_code("NO_ACTIVE_BACKEND", ControllerRouter(store).select_backend)


def test_drain_cannot_complete_with_session_then_completes(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "old", 8001, ACTIVE)
    backend(store, "new", 8002, generation=2)
    router = ControllerRouter(store)
    router.select_backend("legacy")
    router.begin_cutover("old", "new")
    status = router.drain_status("old")
    assert status["active_sessions"] == 1
    assert status["sessions"][0]["mcp_session_id"] == "legacy"
    assert status["can_stop"] is False
    assert_code("DRAIN_NOT_COMPLETE", lambda: router.complete_drain("old"))

    assert store.release_session("legacy") is True
    assert router.drain_status("old")["can_stop"] is True
    assert router.complete_drain("old")["state"] == STANDBY


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:8000",
        "http://192.168.1.2:8000",
        "http://user@localhost:8000",
        "http://localhost:8000?x=1",
        "http://localhost:8000/#fragment",
        "https://localhost:8000",
        "http://localhost",
    ],
)
def test_endpoint_rejects_external_or_invalid(endpoint, tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    with pytest.raises(ValueError):
        store.upsert_backend("bad", endpoint, 1)


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"],
)
def test_endpoint_accepts_supported_loopback(endpoint, tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    assert store.upsert_backend("ok", endpoint, 1)["endpoint"] == endpoint


def test_binding_replay_and_conflict(tmp_path):
    store = ControllerAffinityStore(tmp_path / "affinity.db")
    backend(store, "one", 8001)
    backend(store, "two", 8002)
    store.bind_session("session", "one")
    assert store.bind_session("session", "one")["backend_id"] == "one"
    assert_code(
        "SESSION_ALREADY_BOUND", lambda: store.bind_session("session", "two")
    )
