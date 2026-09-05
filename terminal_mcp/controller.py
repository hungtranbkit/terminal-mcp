"""The multi-node controller: composes NodeRegistry + one NodeClient per
registered node + a session-location cache, and exposes the SAME
operations TerminalService already does (terminal_tail/status/send/
create/kill/reopen/...), transparently routed to whichever node actually
holds the named session -- ChatGPT/the dashboard keep saying `mesflow`,
never `dell/mesflow`, unless two nodes genuinely have a same-named
session (see resolve_session below).

Phase A/B guarantee (task item 11): with only the local node registered
(today's exact deployment, nothing else configured), every method here
resolves to the SAME TerminalService instance dashboard.py/mcp_app.py
already use directly -- this class adds routing, never re-implements the
tmux/permission/audit logic TerminalService already owns. A response's
shape is the exact same dict TerminalService would have returned, with
`node_id`/`node_name` merged in (additive only, task item 9's own "nên
include node_id/node_name để debug" -- never a breaking shape change).
"""
from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import host_metrics
from .node_client import LocalNodeClient, NodeClient, NodeClientError, RemoteNodeClient
from .node_models import NODE_ONLINE, Node
from .node_registry import NodeRegistry
from .scheduler import PlacementResult, choose_node

if TYPE_CHECKING:
    from .core import TerminalService

LOCAL_NODE_ID = "local"


@dataclass
class SessionLocation:
    node_id: str
    cached_at: float


class ControllerService:
    def __init__(self, registry: NodeRegistry, *, local_node_id: str = LOCAL_NODE_ID,
                local_client: NodeClient | None = None, local_display_name: str = "Local",
                local_hostname: str | None = None, local_workspace_root: str = "/") -> None:
        self.registry = registry
        self.local_node_id = local_node_id
        self.local_workspace_root = local_workspace_root
        self._clients: dict[str, NodeClient] = {}
        self._session_location_cache: dict[str, SessionLocation] = {}
        # Cache TTL: short enough that a session created/killed/moved
        # elsewhere is noticed quickly, long enough that a chatty MCP
        # client (many tool calls in a row for the same session) doesn't
        # re-probe every node on every single call.
        self.session_cache_ttl_seconds = 20.0

        if local_client is not None:
            self._clients[local_node_id] = local_client
        import socket
        hostname = local_hostname or socket.gethostname()
        self.registry.register(local_node_id, display_name=local_display_name, hostname=hostname, endpoint="local")

    # -- node registration (remote nodes) ---------------------------------

    def register_remote_node(self, node_id: str, *, display_name: str, hostname: str, endpoint: str,
                             token: str, max_sessions: int | None = None,
                             timeout: float = 10.0) -> None:
        self.registry.register(node_id, display_name=display_name, hostname=hostname, endpoint=endpoint,
                               auth_token_ref=f"node:{node_id}", max_sessions=max_sessions)
        self._clients[node_id] = RemoteNodeClient(endpoint, token, timeout=timeout)

    def client_for(self, node_id: str) -> NodeClient | None:
        return self._clients.get(node_id)

    # -- local self-heartbeat ----------------------------------------------
    # No background thread: cheap enough (a few /proc reads + one
    # shutil.disk_usage call) to compute fresh at the top of any route/
    # tool call that is about to make a scheduling or dashboard-rendering
    # decision -- see refresh_local_heartbeat's own callers below and in
    # dashboard.py/mcp_app.py. A remote node's heartbeat, by contrast,
    # MUST be push-based (task item 2) -- see node_agent.py.
    def refresh_local_heartbeat(self, *, tmux_session_count: int, agent_counts: dict[str, int],
                                agent_types: tuple[str, ...], agent_version: str | None) -> Node | None:
        metrics = host_metrics.collect(workspace_path=self.local_workspace_root)
        return self.registry.heartbeat(
            self.local_node_id, metrics=metrics, tmux_session_count=tmux_session_count,
            agent_counts=agent_counts, agent_types=agent_types, agent_version=agent_version,
            labels=(), latency_ms=0.0,
        )

    def receive_remote_heartbeat(self, node_id: str, *, metrics: host_metrics.NodeMetrics,
                                 tmux_session_count: int, agent_counts: dict[str, int],
                                 agent_types: tuple[str, ...], agent_version: str | None,
                                 labels: tuple[str, ...], platform: str = "linux",
                                 session_backend: str = "tmux", shell_capabilities: tuple[str, ...] = (),
                                 wsl_available: bool = False) -> Node | None:
        """Called by the heartbeat-receiving HTTP route (dashboard.py) once
        the pushing node agent's bearer token has already been verified
        against this node_id -- this method itself does no auth, it
        trusts its caller entirely, exactly like every other internal
        TerminalService-shaped method in this project. platform/
        session_backend/shell_capabilities/wsl_available (multi-node
        Windows support) are whatever the pushing node agent itself
        reported -- this method never infers or overrides them."""
        return self.registry.heartbeat(node_id, metrics=metrics, tmux_session_count=tmux_session_count,
                                       agent_counts=agent_counts, agent_types=agent_types,
                                       agent_version=agent_version, labels=labels, platform=platform,
                                       session_backend=session_backend, shell_capabilities=shell_capabilities,
                                       wsl_available=wsl_available)

    # -- session location resolution ---------------------------------------

    def invalidate_session_location(self, session: str) -> None:
        self._session_location_cache.pop(session, None)

    def resolve_session(self, session: str, *, now: float | None = None) -> dict[str, Any]:
        """Returns {"node_id": ..., "client": ...} or {"error": ...}.
        `node/session` qualified names are checked first (never ambiguous
        by construction); a bare name is resolved via the location cache,
        falling back to probing every ONLINE node's own session list on a
        cache miss. Two nodes genuinely holding the same bare name is
        reported as AMBIGUOUS_SESSION -- never routed by guessing (task
        item 3's own explicit requirement)."""
        now = time.monotonic() if now is None else now
        if "/" in session:
            node_id, _, bare = session.partition("/")
            if self.registry.get(node_id) is None:
                return {"error": "NODE_NOT_FOUND", "node_id": node_id}
            return {"node_id": node_id, "session": bare}

        cached = self._session_location_cache.get(session)
        if cached is not None and (now - cached.cached_at) < self.session_cache_ttl_seconds:
            return {"node_id": cached.node_id, "session": session}

        found_on: list[str] = []
        for node in self.registry.list():
            if node.status != NODE_ONLINE:
                continue
            client = self._clients.get(node.id)
            if client is None:
                continue
            try:
                listing = client.list_sessions()
            except NodeClientError:
                continue
            names = {row["name"] for row in listing.get("sessions", [])}
            if session in names:
                found_on.append(node.id)

        if not found_on:
            return {"error": "SESSION_NOT_FOUND", "session": session}
        if len(found_on) > 1:
            return {"error": "AMBIGUOUS_SESSION", "session": session, "nodes": found_on,
                    "detail": f"session {session!r} exists on multiple nodes {found_on!r} -- "
                             f"use a qualified name like '{found_on[0]}/{session}'"}
        self._session_location_cache[session] = SessionLocation(node_id=found_on[0], cached_at=now)
        return {"node_id": found_on[0], "session": session}

    # -- routed operations ---------------------------------------------------
    # Each one: resolve -> get client -> call -> merge node_id/node_name in.
    # A session-not-found/ambiguous resolution short-circuits with the
    # SAME error shape resolve_session already produced, before ever
    # touching a node client.

    def _route(self, session: str, op: str, call) -> dict[str, Any]:
        resolution = self.resolve_session(session)
        if "error" in resolution:
            return resolution
        node_id, bare = resolution["node_id"], resolution["session"]
        client = self._clients.get(node_id)
        if client is None:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id, "detail": "no client configured for this node"}
        node = self.registry.get(node_id)
        try:
            result = call(client, bare)
        except NodeClientError as exc:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id, "detail": str(exc)}
        if isinstance(result, dict):
            result.setdefault("node_id", node_id)
            result.setdefault("node_name", node.display_name if node else node_id)
        return result

    def terminal_tail(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]:
        return self._route(session, "tail", lambda client, name: client.tail(name, lines, ansi=ansi))

    def terminal_status(self, session: str) -> dict[str, Any]:
        return self._route(session, "status", lambda client, name: client.status(name))

    def terminal_capture(self, session: str, start_line: int | None = None) -> dict[str, Any]:
        return self._route(session, "capture", lambda client, name: client.capture(name, start_line))

    def terminal_send_text(self, session: str, text: str, press_enter: bool = False, dry_run: bool = False,
                           **kwargs: Any) -> dict[str, Any]:
        return self._route(session, "send_text",
                           lambda client, name: client.send_text(name, text, press_enter, dry_run, **kwargs))

    def terminal_send_keys(self, session: str, keys: list[str], confirm_sensitive: bool = False) -> dict[str, Any]:
        return self._route(session, "send_keys", lambda client, name: client.send_keys(name, keys, confirm_sensitive))

    def terminal_input_context(self, session: str | None = None, binding: str | None = None) -> dict[str, Any]:
        if session is None:
            # Binding-only lookup -- bindings are local-node-scoped in
            # this phase (task's own documented Phase A/B limitation, see
            # docs/multi-node.md); routed to the local node directly.
            client = self._clients.get(self.local_node_id)
            result = client.input_context(session, binding) if client else {"error": "NODE_UNREACHABLE"}
            if isinstance(result, dict):
                result.setdefault("node_id", self.local_node_id)
            return result
        return self._route(session, "input_context", lambda client, name: client.input_context(name, binding))

    def terminal_detach_session(self, name: str) -> dict[str, Any]:
        return self._route(name, "detach", lambda client, bare: client.detach_session(bare))

    def terminal_delete_session(self, name: str) -> dict[str, Any]:
        return self._route(name, "delete", lambda client, bare: client.delete_session(bare))

    def terminal_kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._route(name, "kill", lambda client, bare: client.kill_session(
            bare, confirm_name.split("/", 1)[-1] if "/" in confirm_name else confirm_name, requested_by=requested_by,
        ))

    # -- Session Knowledge Store (session_knowledge.py) -----------------------
    # timeline/recover/checkpoint are single-session -- routed exactly like
    # tail/status above (resolve the session's own node, forward there).
    # search is fleet-wide (task item 10: "output remote được ingest về
    # controller... mất mạng không mất dữ liệu") -- queries every currently
    # ONLINE node's own knowledge store and merges; an unreachable/offline
    # node just contributes zero results (never fails the whole search, and
    # never loses anything that node captured -- it stays on that node's
    # own disk, searchable again the moment it's reachable).

    def terminal_knowledge_timeline(self, session: str, *, since: str | None = None, until: str | None = None,
                                    limit: int = 200) -> dict[str, Any]:
        return self._route(session, "knowledge_timeline",
                           lambda client, name: client.knowledge_timeline(name, since=since, until=until, limit=limit))

    def terminal_knowledge_recover(self, session: str) -> dict[str, Any]:
        return self._route(session, "knowledge_recover", lambda client, name: client.knowledge_recover(name))

    def terminal_knowledge_checkpoint(self, session: str, summary: str) -> dict[str, Any]:
        return self._route(session, "knowledge_checkpoint",
                           lambda client, name: client.knowledge_checkpoint(name, summary))

    def terminal_knowledge_search_fleet(self, query: str, *, session_name: str | None = None,
                                        project: str | None = None, since: str | None = None,
                                        until: str | None = None, limit: int = 20) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for node in self.registry.list():
            if node.status != NODE_ONLINE:
                continue
            client = self._clients.get(node.id)
            if client is None:
                continue
            try:
                response = client.knowledge_search(query, session_name=session_name, project=project,
                                                   since=since, until=until, limit=limit)
            except NodeClientError as exc:
                errors[node.id] = str(exc)
                continue
            if "error" in response:
                errors[node.id] = str(response["error"])
                continue
            for row in response.get("results", []):
                # Real bug fixed here: a plain setdefault() is a no-op --
                # each row already carries its OWN "node_id" from
                # session_knowledge.py's own DB column, which is always
                # the string "local" from THAT remote node's own private
                # per-process point of view (core.py's REGISTRY_LOCAL_
                # NODE_ID convention -- see its own docstring), never this
                # controller's real node_id for it. Every remote node's
                # results were silently mislabeled "local" until this was
                # an explicit overwrite instead.
                row["node_id"] = node.id
                results.append(row)
        results.sort(key=lambda r: r.get("captured_at", ""), reverse=True)
        return {"query": query, "results": results[:limit], "node_errors": errors,
                "untrusted_output": True, "untrusted_fields": ["results"]}

    def terminal_grant_session_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        """Node-aware equivalent of TerminalService.grant_session_read --
        real bug fixed here (found live against a Windows session named
        'window' on a remote node): the dashboard's grant-read/grant-
        input routes used to call the LOCAL TerminalService's own grant
        store directly, no matter which node the session actually lived
        on -- silently a no-op (SESSION_NOT_FOUND from the wrong node)
        for any non-local session. Routed through the exact same
        resolve_session/_route every other multi-node-aware operation
        here already uses, so a same-named session on two different
        nodes is refused as AMBIGUOUS_SESSION (task item 3) rather than
        guessed, exactly like kill/detach/delete already behave."""
        return self._route(name, "grant_read", lambda client, bare: client.grant_read(
            bare, enabled, granted_by=granted_by))

    def terminal_grant_session_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        """Node-aware equivalent of TerminalService.grant_session_input -- see
        terminal_grant_session_read's own docstring just above for why this
        exists."""
        return self._route(name, "grant_input", lambda client, bare: client.grant_input(
            bare, enabled, granted_by=granted_by))

    def _find_killed_session_node(self, name: str) -> tuple[str | None, dict[str, Any] | None]:
        """Which ONLINE node's own killed-sessions list actually contains
        `name`, plus that entry's own row (agent_type/working_directory/
        metadata_complete) -- (None, None) if none do. A killed session's
        metadata lives wherever it was killed (killed_sessions.py is
        per-process/per-node, exactly like each node's own bindings/audit
        stores already are), so this is genuinely the only correct way to
        find it -- never a live-session lookup (resolve_session/_route),
        which is wrong here by construction: a killed session is, by
        definition, not in ANY node's live tmux listing, so that
        resolution would always report SESSION_NOT_FOUND for exactly the
        case this exists to handle. Real bug caught while wiring the
        dashboard's own reopen route through this class for the first
        time (task's own multi-node create-session UX work) -- the
        previous _route-based implementation would have silently broken
        reopen for any session whose 20s session-location cache entry had
        already expired, on a single-node deployment too, not only multi-
        node."""
        for node in self.registry.list():
            if node.status != NODE_ONLINE:
                continue
            client = self._clients.get(node.id)
            if client is None:
                continue
            try:
                listing = client.list_killed_sessions()
            except NodeClientError:
                continue
            for row in listing.get("killed_sessions", []):
                if row.get("name") == name:
                    return node.id, row
        return None, None

    def terminal_reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                                grant_mode: str = "none", requested_by: str | None = None,
                                node: str | None = None) -> dict[str, Any]:
        """Reopens on the SAME node it was killed on by default (node=
        None, task item 9's own "mặc định reopen trên node cũ") --
        resolved via _find_killed_session_node, never live-session
        resolution (see that method's own docstring for why). An explicit
        `node` moves it elsewhere instead (task item 9's "cho phép đổi
        node nếu user chọn Move/Reopen elsewhere") -- the killed
        session's own remembered agent_type/cwd are used as defaults
        exactly like the same-node path, `agent_type`/`cwd` given here
        still override them field-by-field; this is functionally a fresh
        terminal_create_session on the new node, so it gets that method's
        own full safety guarantees (NEVER falls back to the old node on
        failure, an incompatible target agent_type/platform fails clearly
        before anything is touched -- unlike terminal_move_session, there
        is no "source" here to protect since the session is already
        killed, but the same "target-first, fail loud" posture applies)."""
        killed_node_id, killed_row = self._find_killed_session_node(name)
        if killed_node_id is None:
            return {"error": "SESSION_NOT_FOUND", "session": name,
                    "detail": "not found in any online node's own killed-sessions list"}
        effective_agent_type = agent_type or (killed_row or {}).get("agent_type")
        effective_cwd = cwd if cwd is not None else (killed_row or {}).get("working_directory")

        if node is None or node == killed_node_id:
            client = self._clients.get(killed_node_id)
            if client is None:
                return {"error": "NODE_UNREACHABLE", "node_id": killed_node_id}
            try:
                result = client.reopen_session(name, agent_type=agent_type, cwd=cwd, grant_mode=grant_mode,
                                               requested_by=requested_by)
            except NodeClientError as exc:
                return {"error": "NODE_UNREACHABLE", "node_id": killed_node_id, "detail": str(exc)}
            if isinstance(result, dict):
                result.setdefault("node_id", killed_node_id)
                node_row = self.registry.get(killed_node_id)
                result.setdefault("node_name", node_row.display_name if node_row else killed_node_id)
                if "error" not in result:
                    self._session_location_cache[name] = SessionLocation(node_id=killed_node_id, cached_at=time.monotonic())
            return result

        if not effective_agent_type:
            return {"error": "REOPEN_METADATA_INCOMPLETE", "session": name, "missing": "agent_type",
                    "detail": "killed session's saved metadata has no agent_type -- supply one explicitly to reopen elsewhere"}
        # Real bug caught by this feature's own test suite: terminal_
        # create_session's own SESSION_ALREADY_EXISTS check calls
        # resolve_session(name) -- which, on a cache HIT, trusts the
        # cached location WITHOUT re-verifying the session is still
        # there (by design, for the common case -- see resolve_session's
        # own docstring). A session just killed on `killed_node_id`
        # within the last session_cache_ttl_seconds still has exactly
        # that stale, no-longer-true cache entry, so create_session would
        # incorrectly report SESSION_ALREADY_EXISTS on the node it's
        # KILLED on instead of creating on the new one. Never true here:
        # this whole branch only runs once _find_killed_session_node has
        # already confirmed the session is gone from that node's own
        # killed-sessions bookkeeping's perspective.
        self.invalidate_session_location(name)
        result = self.terminal_create_session(name, effective_agent_type, effective_cwd, node=node,
                                              grant_mode=grant_mode, requested_by=requested_by)
        if isinstance(result, dict) and "error" not in result:
            result["moved_from"] = killed_node_id
        return result

    # -- move (task item 7): explicit prepare/create-on-target/verify/stop-
    # source workflow. NEVER live migration -- no process memory, no
    # scrollback, no shell history crosses nodes; a fresh process is
    # created on the target from an explicit agent_type/cwd, exactly like
    # terminal_reopen_session already does for "same node, new process
    # under an old name", just landing on a different node instead.
    #
    # Deliberately requires the CALLER to supply agent_type/cwd rather
    # than attempting to auto-detect them from the source's live pane --
    # this project's NodeClient surface has no "peek observed command/cwd
    # without killing" operation (task item 2's own "không expose
    # arbitrary shell endpoint" kept this feature from growing a new,
    # narrowly-scoped-but-still-new HTTP method for it), and the
    # project's own standing rule (terminal_reopen_session's fail-closed
    # posture) is to ask explicitly rather than ever guess. This mirrors
    # exactly what an operator already does for a same-node kill+reopen
    # with incomplete metadata.
    #
    # Workspace sync is explicitly OUT OF SCOPE for this method: it never
    # copies files between nodes itself (git clone/rsync is the
    # operator's own job, run manually or via deploy tooling, BEFORE
    # calling this) -- `cwd` must already resolve on the TARGET node's
    # own config.session_lifecycle.allowed_cwd_roots, exactly like any
    # other terminal_create_session call; a path that only exists on the
    # source fails exactly like any other invalid cwd (CWD_NOT_FOUND/
    # CWD_NOT_ALLOWED), not a special "sync it for me" behavior.
    #
    # Ordering is CREATE-ON-TARGET FIRST, verify READY, THEN stop the
    # source -- not the reverse. A failed create leaves the source
    # completely untouched (a failed move is a no-op, never a data-loss
    # event); stopping the source first would risk leaving nothing
    # working anywhere if the target creation then failed. See
    # docs/multi-node.md for the full rationale and Known Limitations.
    def terminal_move_session(self, name: str, target_node_id: str, *, agent_type: str = "shell",
                              cwd: str | None = None, requested_by: str | None = None) -> dict[str, Any]:
        resolution = self.resolve_session(name)
        if "error" in resolution:
            return resolution
        source_node_id, bare = resolution["node_id"], resolution["session"]
        if source_node_id == target_node_id:
            return {"error": "ALREADY_ON_THAT_NODE", "session": name, "node_id": source_node_id}
        target_node = self.registry.get(target_node_id)
        if target_node is None:
            return {"error": "NODE_NOT_FOUND", "node_id": target_node_id}
        if target_node.status != NODE_ONLINE:
            return {"error": "NODE_UNREACHABLE", "node_id": target_node_id,
                    "detail": f"target node status={target_node.status}"}
        if target_node.draining:
            return {"error": "NODE_DRAINING", "node_id": target_node_id,
                    "detail": "target node is draining -- not accepting new sessions"}
        if agent_type not in ("shell", *target_node.agent_types):
            # Cross-platform (or same-platform) capability mismatch --
            # task's own explicit "nếu không tương thích phải fail rõ
            # ràng trước khi dừng source": failed here, BEFORE the
            # source is ever touched, exactly like every other
            # incompatibility this method rejects. A cheap local check
            # (target_node.agent_types is already known from the last
            # heartbeat, no network round-trip needed) -- cwd
            # compatibility has no such local shortcut and is instead
            # caught by create_session's own real resolve_cwd check on
            # the target below, with the exact same "target first, never
            # touch source on failure" guarantee.
            return {"error": "AGENT_TYPE_NOT_AVAILABLE_ON_TARGET", "node_id": target_node_id,
                    "detail": f"agent_type={agent_type!r} not available on {target_node_id!r} "
                              f"(has {target_node.agent_types!r}) -- move refused, source untouched"}
        target_client = self._clients.get(target_node_id)
        if target_client is None:
            return {"error": "NODE_UNREACHABLE", "node_id": target_node_id, "detail": "no client configured for this node"}
        source_client = self._clients.get(source_node_id)
        if source_client is None:
            return {"error": "NODE_UNREACHABLE", "node_id": source_node_id, "detail": "no client configured for this node"}

        try:
            create_result = target_client.create_session(bare, agent_type, cwd, requested_by=requested_by)
        except NodeClientError as exc:
            return {"error": "NODE_UNREACHABLE", "node_id": target_node_id, "detail": str(exc), "phase": "create_on_target"}
        if "error" in create_result:
            create_result.setdefault("phase", "create_on_target")
            create_result.setdefault("node_id", target_node_id)
            return create_result
        if create_result.get("state") != "READY":
            # CREATED-but-still-starting or FAILED -- never proceed to
            # stop the source on anything less than a confirmed-READY
            # target. The disposable session this attempt made on the
            # target (if any) is left as-is for the caller to inspect or
            # clean up -- never silently deleted, never guessed about.
            return {"error": "MOVE_TARGET_NOT_READY", "phase": "create_on_target", "node_id": target_node_id,
                    "target_state": create_result.get("state"), "detail": create_result}

        try:
            stop_result = source_client.kill_session(bare, bare, requested_by=requested_by)
        except NodeClientError as exc:
            return {"error": "MOVE_STOP_SOURCE_FAILED", "phase": "stop_source", "node_id": source_node_id,
                    "detail": str(exc), "target_node_id": target_node_id, "create_result": create_result,
                    "warning": "the session now exists on BOTH nodes -- stopping the source failed; "
                              "stop it manually once you've confirmed the target is working correctly"}
        if "error" in stop_result:
            stop_result.setdefault("phase", "stop_source")
            stop_result["warning"] = ("the session now exists on BOTH nodes -- stopping the source failed; "
                                      "stop it manually once you've confirmed the target is working correctly")
            stop_result["create_result"] = create_result
            return stop_result

        self._session_location_cache[bare] = SessionLocation(node_id=target_node_id, cached_at=time.monotonic())
        return {
            "session": bare, "moved_from": source_node_id, "moved_to": target_node_id,
            "node_id": target_node_id, "node_name": target_node.display_name,
            "create_result": create_result, "stop_source_result": stop_result,
        }

    # -- create (needs a node CHOICE, not a resolution) ---------------------

    def terminal_create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, *,
                                node: str = "auto", platform: str | None = None, initial_prompt: str | None = None,
                                grant_mode: str = "none", binding: str | None = None,
                                requested_by: str | None = None, show_on_desktop: bool = False) -> dict[str, Any]:
        existing = self.resolve_session(name)
        if "error" not in existing:
            return {"error": "SESSION_ALREADY_EXISTS", "session": name, "node_id": existing["node_id"]}

        if node == "auto":
            placement = choose_node(self.registry.list(), required_agent_type=agent_type, required_platform=platform)
            if placement.node_id is None:
                return {"error": "NO_ELIGIBLE_NODE", "session": name, "reason": placement.reason,
                        "excluded": list(placement.excluded)}
            node_id = placement.node_id
        else:
            node_id = node
            explicit_node = self.registry.get(node_id)
            if explicit_node is None:
                return {"error": "NODE_NOT_FOUND", "node_id": node_id}
            if platform is not None and explicit_node.platform != platform:
                # An explicit node_id that doesn't match an explicitly
                # required platform is a caller error, never silently
                # honored -- fail clearly rather than place a Windows-
                # only workload on a Linux node just because its id was
                # named directly.
                return {"error": "PLATFORM_MISMATCH", "node_id": node_id,
                        "detail": f"node {node_id!r} is platform={explicit_node.platform!r}, required {platform!r}"}
            if explicit_node.status != NODE_ONLINE:
                # Real gap found wiring the dashboard's own multi-node
                # create-session UX through here for the first time: this
                # branch had no online check at all, unlike auto placement
                # (choose_node's own _eligible gate) and terminal_move_
                # session's target-node check just below -- an operator
                # explicitly picking a node the UI itself would have shown
                # as offline got no clear, fast failure here, only
                # whatever NodeClientError (or, worse, silent apparent
                # success against a stale/nonexistent client) the actual
                # network call happened to produce. Fails BEFORE the
                # network call now, same "target-first, fail loud" posture
                # as move -- task item 5's own explicit "không silently
                # fallback... Fail rõ ràng" requirement.
                return {"error": "NODE_UNREACHABLE", "node_id": node_id,
                        "detail": f"node status={explicit_node.status!r}, not online"}

        client = self._clients.get(node_id)
        if client is None:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id}
        try:
            result = client.create_session(name, agent_type, cwd, initial_prompt=initial_prompt,
                                           grant_mode=grant_mode, binding=binding, requested_by=requested_by,
                                           show_on_desktop=show_on_desktop)
        except NodeClientError as exc:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id, "detail": str(exc)}
        if isinstance(result, dict) and "error" not in result:
            node_row = self.registry.get(node_id)
            result.setdefault("node_id", node_id)
            result.setdefault("node_name", node_row.display_name if node_row else node_id)
            self._session_location_cache[name] = SessionLocation(node_id=node_id, cached_at=time.monotonic())
        return result

    # -- fleet-wide views -----------------------------------------------------

    def terminal_list_sessions(self) -> dict[str, Any]:
        """Merges every ONLINE node's own session list, tagging each row
        with node_id/node_name -- OFFLINE/DEGRADED nodes are reported
        separately (never silently dropped -- a caller checking why a
        session isn't listed needs to see "that node is offline", not an
        empty list indistinguishable from "no sessions exist there")."""
        sessions: list[dict[str, Any]] = []
        unreachable: list[dict[str, Any]] = []
        for node in self.registry.list():
            if node.status != NODE_ONLINE:
                unreachable.append({"node_id": node.id, "node_name": node.display_name, "status": node.status})
                continue
            client = self._clients.get(node.id)
            if client is None:
                unreachable.append({"node_id": node.id, "node_name": node.display_name, "status": "no_client"})
                continue
            try:
                listing = client.list_sessions()
            except NodeClientError as exc:
                unreachable.append({"node_id": node.id, "node_name": node.display_name, "status": "error",
                                    "detail": str(exc)})
                continue
            for row in listing.get("sessions", []):
                row = dict(row)
                row.setdefault("node_id", node.id)
                row.setdefault("node_name", node.display_name)
                sessions.append(row)
        return {"sessions": sessions, "unreachable_nodes": unreachable}

    def terminal_list_killed_sessions(self) -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for node in self.registry.list():
            if node.status != NODE_ONLINE:
                continue
            client = self._clients.get(node.id)
            if client is None:
                continue
            try:
                listing = client.list_killed_sessions()
            except NodeClientError:
                continue
            for row in listing.get("killed_sessions", []):
                row = dict(row)
                row.setdefault("node_id", node.id)
                row.setdefault("node_name", node.display_name)
                entries.append(row)
        return {"killed_sessions": entries}

    # -- node views (dashboard/doctor) ---------------------------------------

    def list_nodes(self) -> list[Node]:
        # Watchdog (task: "theo dõi và noti khi node rớt đột ngột"): every
        # fleet-wide status read already happening here (dashboard's own
        # periodic /dashboard/api/nodes poll) doubles as the reconcile
        # pass that detects an ONLINE -> DEGRADED/OFFLINE transition --
        # zero extra network cost, same discipline session_registry's own
        # reconcile-as-a-listing-side-effect already uses. Never let a
        # registry bug break node listing itself.
        try:
            self.registry.sync_status_transitions()
        except Exception:  # noqa: BLE001
            pass
        return self.registry.list()

    def node_status(self, node_id: str) -> Node | None:
        return self.registry.get(node_id)

    def node_sessions(self, node_id: str) -> dict[str, Any]:
        node = self.registry.get(node_id)
        if node is None:
            return {"error": "NODE_NOT_FOUND", "node_id": node_id}
        client = self._clients.get(node_id)
        if client is None:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id}
        try:
            return client.list_sessions()
        except NodeClientError as exc:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id, "detail": str(exc)}

    # -- watchdog: node online/offline transitions ---------------------------

    def terminal_watchdog_node_events(self, *, unacknowledged_only: bool = False, limit: int = 50) -> dict[str, Any]:
        events = self.registry.list_status_events(unacknowledged_only=unacknowledged_only, limit=limit)
        return {"events": events}

    def terminal_watchdog_acknowledge_node_event(self, event_id: int, *, by: str | None = None) -> dict[str, Any]:
        ok = self.registry.acknowledge_status_event(event_id, by=by)
        if not ok:
            return {"error": "EVENT_NOT_FOUND", "event_id": event_id}
        return {"event_id": event_id, "acknowledged": True}

    # -- watchdog: session-dropped-unexpectedly events, FLEET-WIDE -----------
    # (extends the earlier local-node-only pass, task's own explicit "mở
    # rộng fleet-wide cho session drop"). Each node's own session_
    # registry.db only ever knows about ITS OWN sessions -- there is no
    # shared/central store to query directly (deliberately: a node keeps
    # working and detecting its own drops even fully network-partitioned
    # from the controller) -- so this asks every currently-ONLINE node in
    # parallel-in-spirit (sequentially here, same as terminal_knowledge_
    # search_fleet, since NodeClient calls are already fast local-process
    # calls or short HTTP round-trips) and merges, tolerating one node's
    # failure without losing another's results or failing the whole call.

    def terminal_watchdog_session_events_fleet(self, *, unacknowledged_only: bool = False,
                                               limit: int = 50) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for node in self.registry.list():
            if node.status != NODE_ONLINE:
                continue
            client = self._clients.get(node.id)
            if client is None:
                continue
            try:
                response = client.watchdog_session_events(unacknowledged_only=unacknowledged_only, limit=limit)
            except NodeClientError as exc:
                errors[node.id] = str(exc)
                continue
            if "error" in response:
                errors[node.id] = str(response["error"])
                continue
            for row in response.get("events", []):
                # Same real bug class as terminal_knowledge_search_fleet
                # above (see its own comment): each event row already
                # carries "node_id" from session_registry.py's own DB
                # column, always "local" from THAT node's own private
                # point of view -- an explicit overwrite, never
                # setdefault(), or every remote node's events would be
                # mislabeled "local".
                row["node_id"] = node.id
                results.append(row)
        results.sort(key=lambda e: e.get("detected_at", ""), reverse=True)
        return {"events": results[:limit], "node_errors": errors}

    def terminal_watchdog_acknowledge_session_event(self, node_id: str, event_id: int, *,
                                                    by: str | None = None) -> dict[str, Any]:
        """Unlike the node-level acknowledge above (one shared table, no
        node to route to), a session-drop event only exists on the ONE
        node that detected it -- `node_id` (from the fleet listing's own
        already-corrected tag above) says which."""
        client = self._clients.get(node_id)
        if client is None:
            return {"error": "NODE_NOT_FOUND", "node_id": node_id}
        try:
            result = client.watchdog_acknowledge_session_event(event_id, by=by)
        except NodeClientError as exc:
            return {"error": "NODE_UNREACHABLE", "node_id": node_id, "detail": str(exc)}
        if isinstance(result, dict):
            result.setdefault("node_id", node_id)
        return result

    def test_connection(self, node_id: str) -> dict[str, Any]:
        node = self.registry.get(node_id)
        if node is None:
            return {"error": "NODE_NOT_FOUND", "node_id": node_id}
        if node_id == self.local_node_id:
            return {"ok": True, "latency_ms": 0.0}
        client = self._clients.get(node_id)
        if client is None or not isinstance(client, RemoteNodeClient):
            return {"ok": False, "detail": "no remote client configured for this node"}
        ok, latency_ms, detail = client.ping()
        return {"ok": ok, "latency_ms": latency_ms, "detail": detail}

    def set_draining(self, node_id: str, draining: bool) -> dict[str, Any]:
        ok = self.registry.set_draining(node_id, draining)
        if not ok:
            return {"error": "NODE_NOT_FOUND", "node_id": node_id}
        return {"node_id": node_id, "draining": draining}

    def choose_node_for(self, *, agent_type: str = "shell", platform: str | None = None) -> PlacementResult:
        return choose_node(self.registry.list(), required_agent_type=agent_type, required_platform=platform)


def build_default_controller(terminal: "TerminalService") -> ControllerService:
    """The single-node fallback used when a caller of build_mcp()/
    register_dashboard() doesn't pass its own ControllerService --
    exercised by every test in this project (30+ call sites predate this
    feature) and by any future ad-hoc embedding of this project's
    services, never by the real production entry point.

    Deliberately backed by a PRIVATE, per-call temp-file registry, never
    NodeRegistry()'s own real default path
    (~/.local/state/terminal-mcp/nodes.db) -- that path is real
    production state; a test (or any other caller not deliberately
    opting into multi-node persistence) constructing a plain
    `register_dashboard(server, service)` must never write a "local" node
    row into it. This is not a hypothetical: exactly that happened once,
    caught by this feature's own test suite run polluting the real file
    on this very host, before this function existed -- see
    docs/multi-node.md. server_http.py's own main() builds and passes an
    EXPLICIT, persistent ControllerService (the real default path) to
    both build_mcp and register_dashboard instead of ever relying on this
    fallback, so the live production deployment is unaffected either way.

    A plain SQLite file (not ":memory:") is used on purpose: NodeRegistry
    opens a fresh connection per call (the same pattern as every other
    store in this project), and SQLite's ":memory:" database is private
    to a single connection -- every value written would vanish the
    instant that connection closes, breaking basic register-then-read
    within the very same test. A real (temp, auto-cleaned-by-the-OS)
    file behaves identically to production, just never in a real
    location."""
    workspace_root = (terminal.config.session_lifecycle.allowed_cwd_roots[0]
                      if terminal.config.session_lifecycle.allowed_cwd_roots else "/")
    temp_dir = tempfile.mkdtemp(prefix="terminal-mcp-nodes-")
    registry = NodeRegistry(Path(temp_dir) / "nodes.db")
    return ControllerService(registry, local_client=LocalNodeClient(terminal), local_workspace_root=workspace_root)
