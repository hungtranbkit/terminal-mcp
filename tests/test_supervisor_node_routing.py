"""Supervisor remote-session watch identity/routing (P1).

LIVE EVIDENCE THIS FIXES (reproduced from the controller, 2026-09-14):
`terminal_list_sessions` sees hp-work/hp1/hp2/hp3-work on node_id=hp-linux
and win2 on node_id=dell-5530, but `supervisor_watch(session=<name>)`
followed by `supervisor_run_once` marked every one of those watches
`target_missing` and disabled it immediately, while local watches
(gatefix2-work/mcp-work) resolved normally.

ROOT CAUSE the tests below pin down: SupervisorService resolved and polled
session names against the LOCAL TerminalService only
(`self.terminal.terminal_status(target)`), never through the multi-node
router that `terminal_list_sessions`/`terminal_status` already use. A
session on another node is simply not in this host's tmux, so the poll saw
MISSING and took the permanent `target_missing` disable path.

A fake controller is used deliberately: these assertions are about
IDENTITY and ROUTING decisions (which node a watch binds to, which adapter
the poll goes through, what happens when a node is unreachable), and a
real second machine would make them slow and non-deterministic without
testing anything extra. The real-transport half was covered separately by
the live RemoteNodeClient smoke.
"""
from __future__ import annotations

import pytest

from terminal_mcp.config import AppConfig, InputPolicyConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.supervisor import (
    LOCAL_NODE_ID, NODE_UNAVAILABLE_ERRORS, SupervisorService, SupervisorStore, watch_key,
)


class FakeController:
    """Stands in for ControllerService, implementing exactly the two
    methods the supervisor is allowed to use: resolve_session and
    terminal_status. If this ever needs a third, that is the signal that
    node RPC logic is leaking into supervisor.py."""

    def __init__(self, locations: dict[str, list[str]] | None = None,
                 statuses: dict[str, dict] | None = None):
        # session name -> the node ids that hold it
        self.locations = locations or {}
        # "node/name" -> status payload
        self.statuses = statuses or {}
        self.status_calls: list[str] = []
        self.resolve_calls: list[str] = []

    def resolve_session(self, session: str) -> dict:
        self.resolve_calls.append(session)
        if "/" in session:
            node_id, _, bare = session.partition("/")
            return {"node_id": node_id, "session": bare}
        nodes = self.locations.get(session, [])
        if not nodes:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        if len(nodes) > 1:
            return {"error": "AMBIGUOUS_SESSION", "session": session, "nodes": nodes,
                    "detail": f"session {session!r} exists on multiple nodes {nodes!r}"}
        return {"node_id": nodes[0], "session": session}

    def terminal_status(self, session: str) -> dict:
        self.status_calls.append(session)
        return dict(self.statuses.get(session, {"error": "SESSION_NOT_FOUND", "session": session}))


def _config(tmp_path) -> AppConfig:
    return AppConfig(permissions=PermissionsConfig(True, True),
                     allowed_session_patterns=("hp-*", "win*", "local-*"),
                     max_capture_lines=200, default_tail_lines=50,
                     input_policy=InputPolicyConfig(allowed_session_patterns=("hp-*", "win*", "local-*")))


@pytest.fixture
def supervisor(tmp_path):
    from terminal_mcp.audit import AuditStore
    from terminal_mcp.grants import SessionGrantStore

    # Isolated stores: TerminalService otherwise defaults to this host's
    # real ~/.local/state/terminal-mcp/*.db.
    terminal = TerminalService(_config(tmp_path),
                               audit=AuditStore(tmp_path / "audit.db"),
                               grants=SessionGrantStore(tmp_path / "grants.db"))
    return SupervisorService(terminal, SupervisorStore(tmp_path / "supervisor.db"))


def _running(node_id: str) -> dict:
    return {"state": "RUNNING", "exists": True, "last_output": "working on it", "node_id": node_id}


# -- canonical identity ---------------------------------------------------

def test_the_watch_key_of_a_local_session_is_byte_identical_to_the_legacy_one():
    """The whole backward-compatibility story rests on this: every
    persisted `session:<name>` row must keep being addressed by the exact
    same string after the upgrade."""
    assert watch_key("session", "mcp-work") == "session:mcp-work"
    assert watch_key("session", "mcp-work", None) == "session:mcp-work"
    assert watch_key("session", "mcp-work", LOCAL_NODE_ID) == "session:mcp-work"
    assert watch_key("binding", "mesflow-dev", "dell-5530") == "binding:mesflow-dev"


def test_a_remote_session_gets_a_node_qualified_key():
    assert watch_key("session", "hp-work", "hp-linux") == "session:hp-linux/hp-work"


def test_two_nodes_holding_the_same_name_are_two_distinct_watches():
    assert watch_key("session", "win2", "dell-5530") != watch_key("session", "win2", "m910")


# -- watch resolution -----------------------------------------------------

def test_a_remote_session_binds_to_its_node_at_watch_time(supervisor):
    supervisor.controller = FakeController(locations={"hp-work": ["hp-linux"]})
    result = supervisor.watch(session="hp-work")
    assert "error" not in result, result
    assert result["node_id"] == "hp-linux"
    assert result["watch_key"] == "session:hp-linux/hp-work"


def test_a_local_session_keeps_the_legacy_identity_and_no_node_id(supervisor):
    """Only-local must preserve current behaviour exactly."""
    supervisor.controller = FakeController(locations={"local-work": [LOCAL_NODE_ID]})
    result = supervisor.watch(session="local-work")
    assert result["watch_key"] == "session:local-work"
    assert result["node_id"] in (None, LOCAL_NODE_ID)


def test_a_session_that_resolves_nowhere_still_creates_a_legacy_watch(supervisor):
    """Watching a session that does not exist YET is existing, deliberate
    behaviour -- it stays unpinned until its first successful poll. Node
    resolution must not turn that into an error."""
    supervisor.controller = FakeController(locations={})
    result = supervisor.watch(session="hp-later")
    assert "error" not in result, result
    assert result["watch_key"] == "session:hp-later"
    assert result["node_id"] is None


def test_with_no_controller_wired_behaviour_is_unchanged(supervisor):
    """A v1-only/single-node deployment never calls a controller at all."""
    assert supervisor.controller is None
    result = supervisor.watch(session="local-work")
    assert result["watch_key"] == "session:local-work"
    assert result["node_id"] is None


# -- ambiguity ------------------------------------------------------------

def test_a_name_on_two_nodes_is_an_explicit_ambiguity_and_creates_no_watch(supervisor):
    supervisor.controller = FakeController(locations={"win2": ["dell-5530", "m910"]})

    result = supervisor.watch(session="win2")

    assert result["error"] == "AMBIGUOUS_SESSION"
    assert sorted(result["nodes"]) == ["dell-5530", "m910"]
    assert "dell-5530/win2" in result["detail"] or "<node_id>/win2" in result["detail"]
    # Nothing was created -- picking one would silently watch the wrong machine.
    assert supervisor.list_watches()["watches"] == []


def test_a_qualified_name_resolves_an_ambiguity_without_guessing(supervisor):
    supervisor.controller = FakeController(locations={"win2": ["dell-5530", "m910"]})

    result = supervisor.watch(session="dell-5530/win2")

    assert "error" not in result, result
    assert result["node_id"] == "dell-5530"
    assert result["target"] == "win2"
    assert result["watch_key"] == "session:dell-5530/win2"


def test_both_nodes_can_be_watched_separately_without_colliding(supervisor):
    supervisor.controller = FakeController(locations={"win2": ["dell-5530", "m910"]})
    supervisor.watch(session="dell-5530/win2")
    supervisor.watch(session="m910/win2")
    keys = {w["watch_key"] for w in supervisor.list_watches()["watches"]}
    assert keys == {"session:dell-5530/win2", "session:m910/win2"}


# -- polling through the remote-aware adapter -----------------------------

def test_a_remote_watch_polls_through_the_controller_and_is_not_target_missing(supervisor):
    """The exact live failure: this must NOT come back target_missing."""
    controller = FakeController(locations={"hp-work": ["hp-linux"]},
                                statuses={"hp-linux/hp-work": _running("hp-linux")})
    supervisor.controller = controller
    supervisor.watch(session="hp-work")

    supervisor.run_once()

    watch = supervisor.list_watches()["watches"][0]
    assert watch["enabled"] is True
    assert watch["disabled_reason"] is None
    assert watch["state"] != "UNKNOWN"
    # Polled through the controller, using the QUALIFIED name.
    assert controller.status_calls == ["hp-linux/hp-work"]


def test_a_remote_windows_session_resolves_the_same_way(supervisor):
    controller = FakeController(locations={"win2": ["dell-5530"]},
                                statuses={"dell-5530/win2": _running("dell-5530")})
    supervisor.controller = controller
    supervisor.watch(session="win2")

    supervisor.run_once()

    watch = supervisor.list_watches()["watches"][0]
    assert watch["node_id"] == "dell-5530"
    assert watch["enabled"] is True
    assert controller.status_calls == ["dell-5530/win2"]


def test_a_local_watch_never_goes_through_the_controller(supervisor):
    """Local polls must keep using the local adapter -- no behaviour
    change, and no needless node round trip."""
    controller = FakeController(locations={"local-work": [LOCAL_NODE_ID]})
    supervisor.controller = controller
    supervisor.watch(session="local-work")
    controller.status_calls.clear()

    supervisor.run_once()

    assert controller.status_calls == []


# -- node offline is recoverable, not fatal -------------------------------

@pytest.mark.parametrize("error", sorted(NODE_UNAVAILABLE_ERRORS))
def test_an_unreachable_node_keeps_the_watch_and_its_identity(supervisor, error):
    controller = FakeController(locations={"hp-work": ["hp-linux"]},
                                statuses={"hp-linux/hp-work": {"error": error, "detail": "node is down"}})
    supervisor.controller = controller
    supervisor.watch(session="hp-work")

    supervisor.run_once()

    watch = supervisor.list_watches()["watches"][0]
    assert watch["enabled"] is True, "an unreachable NODE must never disable the watch"
    assert watch["disabled_reason"] is None
    assert watch["node_id"] == "hp-linux"
    assert watch["watch_key"] == "session:hp-linux/hp-work"
    events = supervisor.list_events()["events"]
    assert events[0]["event_type"] == "watch_target_unavailable"


def test_the_same_watch_resumes_when_the_node_comes_back(supervisor):
    controller = FakeController(locations={"hp-work": ["hp-linux"]},
                                statuses={"hp-linux/hp-work": {"error": "NODE_UNREACHABLE"}})
    supervisor.controller = controller
    created = supervisor.watch(session="hp-work")
    supervisor.run_once()

    controller.statuses["hp-linux/hp-work"] = _running("hp-linux")
    supervisor.run_once()

    watches = supervisor.list_watches()["watches"]
    assert len(watches) == 1, "the returning node must resume the SAME watch, not need a new one"
    assert watches[0]["watch_key"] == created["watch_key"]
    assert watches[0]["enabled"] is True
    assert watches[0]["state"] != "UNKNOWN"


def test_a_genuinely_missing_session_still_disables_as_before(supervisor):
    """The recoverable path must not swallow the real one: a session the
    node says is gone keeps its existing permanent behaviour."""
    controller = FakeController(locations={"hp-work": ["hp-linux"]},
                                statuses={"hp-linux/hp-work": {"state": "MISSING", "exists": False,
                                                               "reason": "session no longer exists"}})
    supervisor.controller = controller
    supervisor.watch(session="hp-work")

    supervisor.run_once()

    watch = supervisor.list_watches()["watches"][0]
    assert watch["enabled"] is False
    assert watch["disabled_reason"] == "target_missing"


# -- security -------------------------------------------------------------

def test_a_session_outside_the_allowlist_cannot_be_watched_on_any_node(supervisor):
    controller = FakeController(locations={"secret-thing": ["hp-linux"]})
    supervisor.controller = controller

    result = supervisor.watch(session="secret-thing")

    assert result["error"] == "ACCESS_DENIED"
    assert supervisor.list_watches()["watches"] == []
    # And the denial happened BEFORE any node was asked, so the reply
    # cannot leak which nodes hold that name.
    assert controller.resolve_calls == []


def test_a_denied_qualified_name_is_also_refused_before_resolution(supervisor):
    controller = FakeController(locations={"secret-thing": ["hp-linux"]})
    supervisor.controller = controller

    result = supervisor.watch(session="hp-linux/secret-thing")

    assert result["error"] == "ACCESS_DENIED"
    assert result["session"] == "secret-thing"
    assert controller.resolve_calls == []


# -- persistence / restart / idempotency ----------------------------------

def test_node_identity_survives_a_restart(supervisor, tmp_path):
    supervisor.controller = FakeController(locations={"hp-work": ["hp-linux"]})
    created = supervisor.watch(session="hp-work")

    # A brand-new service over the SAME database file -- a real restart.
    reopened = SupervisorService(supervisor.terminal, SupervisorStore(tmp_path / "supervisor.db"))
    watch = reopened.list_watches()["watches"][0]

    assert watch["watch_key"] == created["watch_key"]
    assert watch["node_id"] == "hp-linux"


def test_repeated_watch_calls_create_no_duplicate_rows(supervisor):
    supervisor.controller = FakeController(locations={"hp-work": ["hp-linux"]})
    first = supervisor.watch(session="hp-work")
    second = supervisor.watch(session="hp-work")
    third = supervisor.watch(session="hp-linux/hp-work")  # same target, qualified

    assert first["created"] is True
    assert second["created"] is False
    assert third["created"] is False
    assert len(supervisor.list_watches()["watches"]) == 1


def test_unwatch_by_bare_name_still_finds_a_remote_watch(supervisor):
    supervisor.controller = FakeController(locations={"hp-work": ["hp-linux"]})
    supervisor.watch(session="hp-work")

    result = supervisor.unwatch(session="hp-work")

    assert result["disabled"] is True
    assert result["watch_key"] == "session:hp-linux/hp-work"
    assert supervisor.list_watches()["watches"][0]["disabled_reason"] == "manual_unwatch"


def test_a_legacy_local_row_written_before_this_feature_still_loads(supervisor, tmp_path):
    """Simulates a database from the previous build: a `session:<name>` row
    with no node_id at all."""
    store = supervisor.store
    row, created = store.upsert_watch("session", "legacy-local", source="manual")
    assert created and row["watch_key"] == "session:legacy-local"
    assert row["node_id"] is None

    reopened = SupervisorService(supervisor.terminal, SupervisorStore(tmp_path / "supervisor.db"))
    view = {w["watch_key"]: w for w in reopened.list_watches()["watches"]}
    assert "session:legacy-local" in view
    assert view["session:legacy-local"]["node_id"] is None
    # And it is still addressable by the call that always addressed it.
    assert reopened.unwatch(session="legacy-local")["disabled"] is True
