"""The scheduler that keeps the fleet registry from decaying into a lie.

Without it, every node's metadata crossed the staleness threshold fifteen
minutes after each controller start and the auth view could only answer
UNKNOWN_STALE -- observed live with the whole fleet 6.8 hours old. The tests
that matter are the ones about that: the interval must guarantee freshness,
a dead peer must not poison a good local refresh, and a node that will never
support the endpoint must not be hammered forever.
"""
from __future__ import annotations

import threading
import time

import pytest

from terminal_mcp.config import FleetSyncConfig, _load_fleet_sync_config
from terminal_mcp.fleet_loop import (DEFAULT_INTERVAL_SECONDS, FleetSyncLoop,
                                     FleetSyncLoopConfig, _looks_unsupported)
from terminal_mcp.fleet_service import STALE_AFTER_SECONDS


class _Node:
    def __init__(self, node_id, endpoint="http://x:8790"):
        self.id = node_id
        self.endpoint = endpoint


class _Outcome:
    def __init__(self, ok, error=None, peer="p"):
        self.ok = ok
        self.error = error
        self.peer_node = peer

    def as_dict(self):
        return {"peer_node": self.peer_node, "ok": self.ok, "error": self.error}


class _Sync:
    """Stands in for ControllerFleetSync with the shape the loop uses."""

    def __init__(self, nodes=(), outcomes=None, refresh_raises=False):
        self.refresh_calls = 0
        self.exchanges: list[str] = []
        self.probes: list[str] = []
        self._outcomes = outcomes or {}
        self._refresh_raises = refresh_raises
        outer = self

        class _Service:
            local_node_id = "local"

            def refresh_local(self, **kwargs):
                outer.refresh_calls += 1
                if outer._refresh_raises:
                    raise RuntimeError("projection exploded")
                return {"nodes": len(kwargs.get("nodes") or []),
                        "sessions": len(kwargs.get("sessions") or [])}

            class sync:  # noqa: N801 -- mirrors the real attribute path
                @staticmethod
                def exchange(node_id, transport=None, endpoint=None):
                    outer.exchanges.append(node_id)
                    return outer._outcomes.get(node_id, _Outcome(True, peer=node_id))

                # An unproven peer is probed first (see FleetSyncService.probe).
                # Both are recorded in `exchanges` so the existing tests still
                # count "times we talked to this peer", which is what they are
                # actually about.
                @staticmethod
                def probe(node_id, transport=None, endpoint=None):
                    outer.probes.append(node_id)
                    outer.exchanges.append(node_id)
                    return outer._outcomes.get(node_id, _Outcome(True, peer=node_id))

        class _Controller:
            @staticmethod
            def list_nodes():
                return list(nodes)

        self.service = _Service()
        self.controller = _Controller()

    def _peer_transport(self):
        return lambda **kwargs: {}


def _loop(sync, **config):
    settings = {"enabled": True, "interval_seconds": 60, "peer_exchange_enabled": True}
    settings.update(config)
    return FleetSyncLoop(sync=sync, config=FleetSyncLoopConfig(**settings),
                         sources=lambda: {"sessions": ["s"], "connections": []})


# -- the freshness guarantee ---------------------------------------------------

def test_the_default_interval_keeps_metadata_inside_the_stale_threshold():
    """The whole point. An interval at or past the threshold would guarantee
    the exact condition the loop exists to prevent."""
    assert DEFAULT_INTERVAL_SECONDS < STALE_AFTER_SECONDS
    # Three cycles of headroom, so two consecutive failures still leave the
    # data fresh.
    assert DEFAULT_INTERVAL_SECONDS * 3 <= STALE_AFTER_SECONDS


def test_config_refuses_an_interval_that_cannot_keep_up():
    with pytest.raises(ValueError, match="at least 60"):
        _load_fleet_sync_config({"interval_seconds": 30})
    with pytest.raises(ValueError, match="stale by definition"):
        _load_fleet_sync_config({"interval_seconds": 900})
    assert _load_fleet_sync_config({"interval_seconds": 120}).interval_seconds == 120


def test_it_is_on_by_default():
    """Needing to turn this ON to get correct data would not be a supported
    default -- it is what makes an already-shipped feature tell the truth."""
    assert FleetSyncConfig().enabled is True
    assert FleetSyncConfig().peer_exchange_enabled is True


# -- one cycle -----------------------------------------------------------------

def test_a_cycle_refreshes_local_truth_and_exchanges_with_every_peer():
    sync = _Sync(nodes=[_Node("local"), _Node("hp-linux"), _Node("dell-linux")])
    result = _loop(sync).run_once()
    assert sync.refresh_calls == 1
    assert sync.exchanges == ["hp-linux", "dell-linux"], "local is not its own peer"
    assert result["errors"] == []


def test_a_failing_peer_never_poisons_a_good_local_refresh():
    """Split deliberately: one unreachable node must not make the metadata
    everything reads go stale for a reason unrelated to it."""
    sync = _Sync(nodes=[_Node("hp-linux")],
                 outcomes={"hp-linux": _Outcome(False, "OSError: timed out")})
    result = _loop(sync).run_once()
    assert sync.refresh_calls == 1
    assert result["refreshed"] is not None
    assert result["peers"][0]["ok"] is False


def test_a_broken_local_refresh_is_recorded_not_raised():
    sync = _Sync(nodes=[_Node("hp-linux")], refresh_raises=True)
    result = _loop(sync).run_once()
    assert any(e.startswith("refresh:") for e in result["errors"])
    assert sync.exchanges == ["hp-linux"], "peers are still swept"


def test_unreadable_sources_still_produce_a_cycle():
    sync = _Sync(nodes=[])

    def _explode():
        raise OSError("registry locked")

    loop = FleetSyncLoop(sync=sync, config=FleetSyncLoopConfig(), sources=_explode)
    result = loop.run_once()
    assert any(e.startswith("sources:") for e in result["errors"])
    assert sync.refresh_calls == 1, "a refresh with empty sources beats no refresh"


def test_peer_exchange_can_be_turned_off_without_losing_the_local_refresh():
    """For a fleet whose agents all predate /v1/fleet/* -- keep the half that
    makes this controller's own dashboard correct, drop the 404s."""
    sync = _Sync(nodes=[_Node("hp-linux")])
    result = _loop(sync, peer_exchange_enabled=False).run_once()
    assert sync.refresh_calls == 1
    assert sync.exchanges == []
    assert result["peers"] == []


# -- backoff -------------------------------------------------------------------

@pytest.mark.parametrize("error,unsupported", [
    ("NodeClientError: HTTP 404 Not Found", True),
    ("HTTPError: 404", True),
    ("OSError: timed out", False),
    ("ConnectionRefusedError: [Errno 111]", False),
    (None, False),
])
def test_a_missing_route_is_told_apart_from_a_node_being_down(error, unsupported):
    """One is a build that will never answer until upgraded; the other may be
    back next cycle. Backing off identically would either hammer the first or
    abandon the second."""
    assert _looks_unsupported(error) is unsupported


def test_an_unsupported_peer_backs_off_hard_instead_of_being_hammered():
    """Four of five agents on this fleet answer 404 forever. Retrying every
    cycle is pure noise in the log and pure load on the node."""
    sync = _Sync(nodes=[_Node("hp-linux")],
                 outcomes={"hp-linux": _Outcome(False, "HTTP 404 Not Found")})
    loop = _loop(sync)
    loop.run_once()
    assert sync.exchanges == ["hp-linux"]

    # Next cycle is skipped, and reported as skipped rather than silently
    # doing nothing.
    result = loop.run_once()
    assert sync.exchanges == ["hp-linux"], "not retried"
    assert result["skipped_peers"][0]["peer_node"] == "hp-linux"
    assert result["skipped_peers"][0]["unsupported"] is True


def test_backoff_grows_but_never_becomes_permanent():
    """A node agent gets upgraded eventually; a loop that gave up forever
    would need a controller restart to notice."""
    sync = _Sync(nodes=[_Node("hp-linux")],
                 outcomes={"hp-linux": _Outcome(False, "HTTP 404")})
    loop = _loop(sync)
    attempts = 0
    for _ in range(60):
        before = len(sync.exchanges)
        loop.run_once()
        if len(sync.exchanges) > before:
            attempts += 1
    assert attempts >= 4, "it must keep trying, just rarely"
    assert attempts < 30, "and must not retry every cycle"


def test_a_peer_that_comes_back_is_forgiven_immediately():
    sync = _Sync(nodes=[_Node("hp-linux")],
                 outcomes={"hp-linux": _Outcome(False, "HTTP 404")})
    loop = _loop(sync)
    loop.run_once()
    sync._outcomes["hp-linux"] = _Outcome(True, peer="hp-linux")
    for _ in range(3):           # wait out the short backoff
        loop.run_once()
    assert loop.status()["peers"]["hp-linux"]["failures"] == 0
    assert loop.status()["peers"]["hp-linux"]["skip_cycles"] == 0


# -- lifecycle and introspection -----------------------------------------------

def test_the_loop_refreshes_immediately_rather_than_after_one_interval():
    """A controller that just started has the emptiest cache it will ever
    have; making an operator wait five minutes for a correct dashboard is the
    gap this closes."""
    sync = _Sync(nodes=[])
    loop = _loop(sync, interval_seconds=3600)
    loop.start()
    try:
        deadline = time.time() + 5
        while sync.refresh_calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert sync.refresh_calls >= 1
        assert loop.is_alive()
    finally:
        loop.stop()
    assert not loop.is_alive()


def test_a_disabled_loop_starts_no_thread():
    sync = _Sync(nodes=[])
    loop = _loop(sync, enabled=False)
    loop.start()
    try:
        assert loop.is_alive() is False
        assert sync.refresh_calls == 0
    finally:
        loop.stop()


def test_a_cycle_that_throws_does_not_kill_the_loop(monkeypatch):
    """A loop that dies leaves the cache decaying with nothing to notice."""
    sync = _Sync(nodes=[])
    loop = _loop(sync, interval_seconds=60)
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("bad cycle")

    monkeypatch.setattr(loop, "run_once", _boom)
    loop.start()
    try:
        deadline = time.time() + 5
        while calls["n"] == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert calls["n"] >= 1
        assert loop.is_alive(), "the thread survived a throwing cycle"
    finally:
        loop.stop()


def test_status_separates_the_loop_being_alive_from_the_data_being_fresh():
    """Different claims, and only the second one matters to whoever is
    reading the fleet view."""
    sync = _Sync(nodes=[_Node("hp-linux")])
    loop = _loop(sync)
    assert loop.status()["age_seconds"] is None, "nothing has run yet"
    loop.run_once()
    status = loop.status()
    assert status["cycles"] == 1
    assert status["age_seconds"] is not None and status["age_seconds"] < 5
    assert status["enabled"] is True
    assert status["last_error"] is None


def test_status_reports_why_a_peer_is_being_skipped():
    sync = _Sync(nodes=[_Node("hp-linux")],
                 outcomes={"hp-linux": _Outcome(False, "HTTP 404 Not Found")})
    loop = _loop(sync)
    loop.run_once()
    peer = loop.status()["peers"]["hp-linux"]
    assert peer["unsupported"] is True
    assert peer["skip_cycles"] >= 1
    assert "404" in peer["last_error"]


# -- readiness integration -------------------------------------------------------

def _service(tmp_path):
    from terminal_mcp.fleet_registry import KIND_NODE, FleetRegistryStore
    from terminal_mcp.fleet_service import FleetService

    store = FleetRegistryStore(tmp_path / "fleet.db", local_node_id="a")
    store.publish(KIND_NODE, "node:a", {"node_id": "a", "contract_version": 1})
    return FleetService(store, local_node_id="a")


class _Status:
    def __init__(self, **fields):
        self.fields = {"enabled": True, "running": True, "interval_seconds": 300,
                       "age_seconds": 3.0, "cycles": 5, "last_error": None, "peers": {}}
        self.fields.update(fields)

    def status(self):
        return self.fields


def _loop_check(service):
    return next((c for c in service.readiness()["checks"]
                 if c["check"] == "fleet_refresh_loop"), None)


def test_readiness_says_nothing_about_a_loop_that_does_not_exist_here(tmp_path):
    """A node agent and every test build this service without a loop.
    Inventing a verdict about one would be worse than silence."""
    assert _loop_check(_service(tmp_path)) is None


def test_readiness_reports_a_healthy_loop(tmp_path):
    service = _service(tmp_path)
    service.attach_sync_loop(_Status())
    check = _loop_check(service)
    assert check["status"] == "PASS"
    assert "every 300s" in check["summary"]


def test_a_dead_loop_is_FAIL_because_the_data_will_rot(tmp_path):
    """Stale metadata now has an actionable cause. Reporting only "metadata
    is old" sends an operator to look at the nodes when the problem is here."""
    service = _service(tmp_path)
    service.attach_sync_loop(_Status(running=False))
    check = _loop_check(service)
    assert check["status"] == "FAIL"
    assert "not running" in check["summary"]


def test_a_disabled_loop_is_WARN_not_FAIL(tmp_path):
    """Turning it off is a supported choice; it should not read like a
    malfunction."""
    service = _service(tmp_path)
    service.attach_sync_loop(_Status(enabled=False, running=False))
    assert _loop_check(service)["status"] == "WARN"


def test_a_loop_reporting_errors_is_WARN(tmp_path):
    service = _service(tmp_path)
    service.attach_sync_loop(_Status(last_error="refresh:OSError"))
    check = _loop_check(service)
    assert check["status"] == "WARN"
    assert "refresh:OSError" in check["summary"]


def test_readiness_never_fails_because_introspection_did(tmp_path):
    class _Broken:
        def status(self):
            raise RuntimeError("no")

    service = _service(tmp_path)
    service.attach_sync_loop(_Broken())
    assert _loop_check(service)["status"] == "FAIL"
    assert service.readiness()["status"] in {"PASS", "WARN", "FAIL"}


def test_the_controller_attaches_the_loop_so_readiness_can_see_it():
    """Wiring asserted, because a loop nobody attached reports nothing and
    the check silently disappears -- the failure mode is invisible."""
    import inspect

    from terminal_mcp import server_http

    source = inspect.getsource(server_http)
    assert "fleet.attach_sync_loop(fleet_loop)" in source
    assert "fleet_loop.start()" in source
    assert "atexit.register(fleet_loop.stop)" in source


# -- probe before pushing ---------------------------------------------------------

def test_an_unproven_peer_is_probed_not_handed_the_whole_export():
    """Measured on the real fleet: an agent without /v1/fleet/* does not
    answer a tidy 404 -- it resets the connection while a 363-object export
    is still going out, so the failure arrives as ECONNRESET and looks like a
    network fault. Knock first."""
    calls: list[str] = []

    class _Probing(_Sync):
        def __init__(self):
            super().__init__(nodes=[_Node("hp-linux")])
            outer = self

            class _S:
                local_node_id = "local"

                @staticmethod
                def refresh_local(**kwargs):
                    return {}

                class sync:  # noqa: N801
                    @staticmethod
                    def probe(node_id, transport=None, endpoint=None):
                        calls.append("probe")
                        return _Outcome(True, peer=node_id)

                    @staticmethod
                    def exchange(node_id, transport=None, endpoint=None):
                        calls.append("exchange")
                        return _Outcome(True, peer=node_id)

            self.service = _S()

    sync = _Probing()
    loop = _loop(sync)
    loop.run_once()
    assert calls == ["probe"], "first contact must be cheap"
    loop.run_once()
    assert calls == ["probe", "exchange"], "a proven peer gets the real thing"


@pytest.mark.parametrize("error", [
    "NodeClientError: POST /v1/fleet/objects -> URLError: [Errno 104] Connection reset by peer",
    "NodeClientError: POST /v1/fleet/objects -> URLError: [Errno 32] Broken pipe",
])
def test_a_reset_on_the_fleet_route_is_treated_as_an_unsupported_build(error):
    """On a node whose heartbeat and status calls work, a reset on THIS route
    specifically means the route is not there -- so it earns the long backoff
    rather than being retried like a flaky network."""
    assert _looks_unsupported(error) is True
