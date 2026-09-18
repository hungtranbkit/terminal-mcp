"""Collaborators handed to the services that need them, on every surface.

The defect this pins: `build_mcp()` defaulted `controller` AFTER constructing
RecoveryEngine and RecoveryLoop with it. `server.py` builds the stdio surface by
calling `build_mcp()` bare, so on that surface both were constructed with
controller=None -- present in the tool list, and holding nothing to route with.
Auto-recovery and reconcile_node were silently non-functional there.

A wiring order defect leaves no error and no log line; the feature simply does
nothing. So it gets a test that asks what the built objects actually hold.
"""
from __future__ import annotations

import pytest

from terminal_mcp.controller import ControllerService
from terminal_mcp.mcp_app import build_mcp


@pytest.fixture
def built(monkeypatch):
    """Capture the collaborators each service was constructed with."""
    import terminal_mcp.mcp_app as app

    seen = {}
    real_engine, real_loop = app.RecoveryEngine, app.RecoveryLoop

    class EngineSpy(real_engine):
        def __init__(self, registry, controller, leases, config, *a, **kw):
            seen["recovery_controller"] = controller
            super().__init__(registry, controller, leases, config, *a, **kw)

    class LoopSpy(real_loop):
        def __init__(self, engine, controller, *a, **kw):
            seen["recovery_loop_controller"] = controller
            super().__init__(engine, controller, *a, **kw)

    monkeypatch.setattr(app, "RecoveryEngine", EngineSpy)
    monkeypatch.setattr(app, "RecoveryLoop", LoopSpy)
    server = build_mcp()
    seen["server"] = server
    return seen


def test_the_stdio_surface_gives_recovery_a_real_controller(built):
    """`build_mcp()` bare is the stdio surface. Nothing may be constructed with
    a None collaborator it will later dereference."""
    assert isinstance(built["recovery_controller"], ControllerService), \
        "RecoveryEngine was built with no controller -- recovery cannot route"
    assert isinstance(built["recovery_loop_controller"], ControllerService), \
        "RecoveryLoop was built with no controller"


def test_the_supervisor_gets_the_fleets_view_on_the_stdio_surface():
    """The fleet hooks must be wired on EVERY surface, not only the HTTP one --
    otherwise remote watches keep resolving against local tmux exactly as
    before, on whichever surface was forgotten."""
    import terminal_mcp.mcp_app as app

    captured = {}
    real = app.SupervisorService

    class Spy(real):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["supervisor"] = self

    original = app.SupervisorService
    app.SupervisorService = Spy
    try:
        build_mcp()
    finally:
        app.SupervisorService = original

    supervisor = captured["supervisor"]
    assert supervisor.fleet_status is not None, "fleet_status was never wired"
    assert supervisor.fleet_sessions is not None, "fleet_sessions was never wired"
    assert callable(supervisor.fleet_status)


def test_an_explicitly_passed_controller_is_still_the_one_used(built):
    """The reorder must not start overriding a caller's own controller -- the
    HTTP surface passes a fully configured one."""
    import terminal_mcp.mcp_app as app

    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService

    terminal = TerminalService(AppConfig(PermissionsConfig(True, False), ("test-*",), 50, 20))
    sentinel = build_default_controller_sentinel(terminal)
    seen = {}
    real_engine = app.RecoveryEngine

    class EngineSpy(real_engine):
        def __init__(self, registry, controller, leases, config, *a, **kw):
            seen["controller"] = controller
            super().__init__(registry, controller, leases, config, *a, **kw)

    app.RecoveryEngine = EngineSpy
    try:
        build_mcp(terminal, controller=sentinel)
    finally:
        app.RecoveryEngine = real_engine

    assert seen["controller"] is sentinel, "a caller-supplied controller was discarded"


def build_default_controller_sentinel(terminal):
    from terminal_mcp.controller import build_default_controller

    return build_default_controller(terminal)
