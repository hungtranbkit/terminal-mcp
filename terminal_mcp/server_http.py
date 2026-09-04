from __future__ import annotations

import atexit
import logging
import os
import secrets
from pathlib import Path

import anyio
import uvicorn

from .config import load_config
from .connection_store import ConnectionStore
from .controller import ControllerService
from .core import TerminalService
from .dashboard import node_token_env_var, register_dashboard
from .health import register_health
from .logging_setup import RequestIdMiddleware, SecurityHeadersMiddleware, configure_logging
from .maintenance import MaintenanceLoop
from .mcp_app import build_mcp
from .node_client import LocalNodeClient
from .node_registry import NodeRegistry
from .supervisor import SupervisorLoop, SupervisorService, SupervisorStore
from .supervisor2 import build_supervisor_v2
from .webauth import WebAuthStore
from .webauth_dashboard import register_webauth_dashboard

_log = logging.getLogger(__name__)

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8766
HTTP_PATH = "/mcp"


def bootstrap_secret_path(webauth_db_path: Path) -> Path:
    """Derived from the SAME location as the given webauth.db -- never a
    fixed path independent of which store is actually in play. A test or
    a `terminal-mcp-webauth --db /some/other.db` invocation against an
    isolated database must never read or delete a DIFFERENT store's (in
    particular production's own) bootstrap secret merely because a
    username happens to match -- scoping this to the store's own path,
    not a global default, is what makes that impossible by construction
    rather than by caller discipline."""
    return webauth_db_path.parent / f"{webauth_db_path.stem}-bootstrap.txt"


def default_bootstrap_secret_path() -> Path:
    """The production default -- default_webauth_db_path()'s own sibling
    file, computed the exact same way bootstrap_secret_path() derives any
    other store's. Only meaningful when no explicit --db/store path is in
    hand (e.g. before a WebAuthStore has been constructed at all)."""
    from .webauth import default_webauth_db_path

    return bootstrap_secret_path(default_webauth_db_path())


def delete_bootstrap_secret_if_matches(username: str, webauth_db_path: Path) -> bool:
    """Removes the one-time bootstrap secret file once (and only once)
    its account has actually changed its password -- shared by the CLI
    (terminal-mcp-webauth set-password) and the web password-change form
    alike, so the file's lifecycle has exactly one implementation. Scoped
    to `webauth_db_path`'s own sibling bootstrap file specifically -- see
    bootstrap_secret_path's own docstring for why that matters."""
    path = bootstrap_secret_path(webauth_db_path)
    if not path.exists():
        return False
    try:
        content = path.read_text()
    except OSError:
        return False
    if f"username: {username}" not in content:
        return False
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _ensure_webauth_bootstrap(webauth: WebAuthStore) -> None:
    """Creates exactly one local admin account, with a strong random
    password, the first time the password-login store is ever empty --
    never on any later start once an account exists. The password itself
    is written ONCE to a state-directory-local file and nowhere else
    (never logged, never printed to stdout/stderr) -- see that file's own
    contents for the required next step, which deletes it automatically
    once the password is actually changed (either via the CLI or the
    forced first-login web form).

    Ordering matters for safety: the secret file is written, at mode 600
    from the moment the file descriptor is created (os.open's own mode
    argument -- never a plaintext-mode write followed by a later chmod,
    which would leave a real, if brief, window at the process's default
    permissions) BEFORE the account itself is created. If that write
    fails for any reason, this returns without ever creating the account
    -- the alternative (account created, secret lost) would strand an
    account nobody can ever log into; this way, a future restart's own
    call to this same function retries cleanly instead."""
    if webauth.has_any_user():
        return
    username = "admin"
    password = secrets.token_urlsafe(18)  # ~24 URL-safe chars of real entropy
    path = bootstrap_secret_path(webauth.path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = (
        "Terminal MCP -- one-time password-login bootstrap\n"
        "===================================================\n\n"
        f"  username: {username}\n"
        f"  password: {password}\n\n"
        "Log in at the password-login URL (README's \"Password login\" section\n"
        "has the exact address). The first login forces a password change\n"
        "immediately (a short in-app form) before anything else is reachable;\n"
        "changing it there deletes this file automatically. Alternatively, from\n"
        "a local shell on this machine:\n\n"
        f"  terminal-mcp-webauth set-password {username}\n\n"
        "Until the password is changed, this file is the ONLY copy of the\n"
        "bootstrap password -- it is never logged, printed, or committed\n"
        "anywhere else.\n"
    )
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:  # takes ownership of fd -- closes it exactly once, on any exit path
            handle.write(content)
    except OSError:
        _log.error("webauth: failed to write bootstrap secret file %s -- no account created, will retry on next start", path)
        return
    webauth.create_or_replace_user(username, password, must_change_password=True)
    _log.warning("webauth: created bootstrap admin account -- one-time password written to %s (mode 600)", path)


async def _serve(server) -> None:
    """Builds and runs the same Starlette app server.run(transport=
    "streamable-http", ...) would (streamable_http_app() + uvicorn), but
    driven directly rather than through that convenience wrapper -- it
    exposes no hook to add ASGI middleware, and P1 items #7/#11 (request-
    id correlation, security response headers) need one. log_config=None
    is deliberate: uvicorn's own default logging setup would otherwise
    call logging.config.dictConfig() and silently replace the root
    handler configure_logging() (called before this) just installed."""
    starlette_app = server.streamable_http_app(
        streamable_http_path=HTTP_PATH, json_response=True, host=HTTP_HOST,
    )
    starlette_app.add_middleware(SecurityHeadersMiddleware)
    starlette_app.add_middleware(RequestIdMiddleware)
    config = uvicorn.Config(starlette_app, host=HTTP_HOST, port=HTTP_PORT, log_level="info", log_config=None)
    await uvicorn.Server(config).serve()


def main() -> None:
    configure_logging()
    # The bind address is intentionally not configurable: remote access must go
    # through an authenticated HTTPS tunnel terminating on this loopback port.
    config = load_config()
    terminal = TerminalService(config)
    supervisor = SupervisorService(terminal, SupervisorStore())
    supervisor_v2 = build_supervisor_v2(supervisor)
    # ONE explicit, persistent (real default ~/.local/state/terminal-mcp/
    # nodes.db path) ControllerService, shared by both build_mcp and
    # register_dashboard -- the actual production deployment never falls
    # into either function's own private-temp-registry fallback default
    # (see controller.py's build_default_controller docstring for why
    # that fallback exists and why it must never be the production path).
    # A single shared instance also means the MCP tool surface and the
    # dashboard see the exact same node registry state and session-
    # location cache, not two independently-drifting copies.
    workspace_root = (config.session_lifecycle.allowed_cwd_roots[0]
                      if config.session_lifecycle.allowed_cwd_roots else "/")
    registry = NodeRegistry(overload_thresholds=config.nodes.overload_thresholds,
                            heartbeat_thresholds=config.nodes.heartbeat_thresholds)
    controller = ControllerService(registry, local_client=LocalNodeClient(terminal),
                                   local_workspace_root=workspace_root)
    for remote in config.nodes.remote_nodes:
        # Fail SOFT, not startup-fatal: a misconfigured/not-yet-deployed
        # remote node (token not exported yet, typo'd endpoint) must never
        # take down the whole controller -- it just never leaves OFFLINE
        # until the operator finishes onboarding it (task item 10's own
        # "registry never disappears / marks stale" applies here too).
        token = os.environ.get(remote.token_env)
        if not token:
            _log.warning("nodes: skipping remote node %r -- environment variable %s is not set "
                        "(this node will not be registered until it is)", remote.node_id, remote.token_env)
            continue
        controller.register_remote_node(remote.node_id, display_name=remote.display_name, hostname=remote.hostname,
                                        endpoint=remote.endpoint, token=token, max_sessions=remote.max_sessions,
                                        timeout=remote.timeout_seconds)
        _log.info("nodes: registered remote node %r (%s)", remote.node_id, remote.endpoint)

    # ONE explicit, persistent (real default ~/.local/state/terminal-mcp/
    # connections.db) ConnectionStore, same "never fall into the private-
    # temp-file test default" discipline as ControllerService/NodeRegistry
    # just above -- see dashboard.py's register_dashboard docstring for
    # why that fallback exists and must never be the production path.
    connection_store = ConnectionStore()
    # Re-hydrate any node connected at runtime via the LAN-discovery/
    # remote-connect dashboard feature (LAN SSH, Cloudflare-tunnel SSH, or
    # agent-token) from ITS last run -- task's own "controller restart
    # không làm mất remote session đang chạy": the remote node-agent
    # process itself lives on a separate host and was never touched by
    # this restart; this only re-establishes THIS controller's own
    # client/registration for it, using the bearer token already saved to
    # a local 0600 file at connect time (see connection_store.py) -- no
    # credential re-entry, no operator action needed. A config.yaml-
    # declared remote node (the loop just above) always wins if the same
    # node_id appears in both places.
    config_node_ids = {remote.node_id for remote in config.nodes.remote_nodes}
    for saved in connection_store.list():
        if saved.node_id in config_node_ids:
            continue
        token = connection_store.read_token(saved.token_file) if saved.token_file else None
        if not token:
            _log.warning("nodes: skipping saved connection %r -- token file missing/unreadable "
                        "(re-connect it from the Nodes page)", saved.node_id)
            continue
        controller.register_remote_node(saved.node_id, display_name=saved.node_id,
                                        hostname=saved.hostname or saved.node_id, endpoint=saved.endpoint,
                                        token=token)
        # Same env var dashboard.py's node_heartbeat route re-reads on
        # every inbound push from this node -- see its own
        # node_token_env_var docstring. Without this, the node would
        # re-register successfully here but every one of its real
        # heartbeat pushes would still be rejected as UNAUTHORIZED after
        # a restart.
        os.environ[node_token_env_var(saved.node_id)] = token
        _log.info("nodes: re-registered previously-connected node %r (%s, transport=%s)",
                 saved.node_id, saved.endpoint, saved.transport_type)

    server = build_mcp(terminal, supervisor, supervisor_v2, controller)
    register_dashboard(server, terminal, supervisor, supervisor_v2, controller, connection_store)
    webauth = WebAuthStore()
    _ensure_webauth_bootstrap(webauth)
    register_webauth_dashboard(server, terminal, webauth, supervisor, supervisor_v2)
    register_health(server, terminal, supervisor)

    # Supervisor tools (watch/status/events/run_once, and the v2 policy/
    # claim/decide/approve/send tools) are always available — only the
    # *automatic* background poll thread is gated here. A plain daemon
    # thread with an interruptible stop Event is the simplest correct way
    # to add a background poller without needing an asyncio/lifespan hook.
    # One process -> at most one loop instance; atexit.register gives it a
    # best-effort clean stop on normal interpreter exit (systemd's SIGTERM
    # still just ends the process, same as before this feature — the loop
    # is a daemon thread either way).
    # The loop drives supervisor_v2.run_once() (a strict superset of
    # supervisor.run_once() — v1's own poll plus v2's reconciliation pass),
    # so an approved_auto_continue chain progresses automatically once
    # enabled, with no separate v2 loop/thread needed.
    if config.supervisor.enabled:
        loop = SupervisorLoop(supervisor_v2)
        loop.start()
        atexit.register(loop.stop)

    # P1 hardening item #9: unconditional, unlike the supervisor loop
    # above -- audit.db accumulates from any terminal_send_text/_keys call
    # regardless of whether Supervisor Loop v1 is enabled, so its
    # retention/WAL maintenance is baseline hygiene, not gated behind that
    # unrelated opt-in.
    maintenance_loop = MaintenanceLoop(
        audit=terminal.audit, supervisor2_store=supervisor_v2.store,
        bindings_path=terminal.bindings.path, config=config.maintenance,
        leases=terminal.leases,
    )
    maintenance_loop.start()
    atexit.register(maintenance_loop.stop)

    anyio.run(lambda: _serve(server))


if __name__ == "__main__":
    main()
