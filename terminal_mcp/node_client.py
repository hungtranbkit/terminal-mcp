"""How the controller talks to ONE node's tmux operations -- LocalNodeClient
(in-process, zero network hop, wraps this same process's own
TerminalService directly) and RemoteNodeClient (HTTP + bearer token,
talks to a terminal-node-agent process on another host) implement the
EXACT SAME interface, so controller.py's routing code never special-
cases "is this the local node" in its own logic -- only node_registry.py's
`endpoint` field ("local" vs an "http://..." URL) decides which
transport a given node actually gets, at controller startup, once.

This is the direct implementation of this feature's own repeated design
note: the local node is a node like any other; only its transport
differs.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol, runtime_checkable

DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0


class NodeClientError(RuntimeError):
    """Raised by RemoteNodeClient on a transport-level failure (network
    unreachable, auth rejected, node agent returned malformed JSON) --
    never for an ordinary application-level error response (a session not
    found, a permission denial), which comes back as a normal {"error":
    ...} dict exactly like TerminalService's own methods already return,
    so callers never need two different error-handling shapes depending
    on which node answered."""


@runtime_checkable
class NodeClient(Protocol):
    """One node's exposed operation set -- deliberately the same narrow
    list terminal-node-agent's own HTTP surface exposes (task: "Agent chỉ
    expose các operation cần thiết... Không expose arbitrary shell
    endpoint"). Every method's shape mirrors the corresponding
    TerminalService method 1:1 -- LocalNodeClient is a pure pass-through,
    RemoteNodeClient reconstructs the identical shape from JSON."""

    def list_sessions(self) -> dict[str, Any]: ...
    def status(self, session: str) -> dict[str, Any]: ...
    def tail(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]: ...
    def capture(self, session: str, start_line: int | None = None) -> dict[str, Any]: ...
    def send_text(self, session: str, text: str, press_enter: bool = False, dry_run: bool = False, *,
                  idempotency_key: str | None = None, origin: str | None = None, trace_id: str | None = None,
                  parent_turn_id: str | None = None, depth: int = 0) -> dict[str, Any]: ...
    def send_keys(self, session: str, keys: list[str], confirm_sensitive: bool = False) -> dict[str, Any]: ...
    def input_context(self, session: str | None = None, binding: str | None = None) -> dict[str, Any]: ...
    def create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, *,
                       initial_prompt: str | None = None, grant_mode: str = "none",
                       binding: str | None = None, requested_by: str | None = None) -> dict[str, Any]: ...
    def detach_session(self, name: str) -> dict[str, Any]: ...
    def delete_session(self, name: str) -> dict[str, Any]: ...
    def kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]: ...
    def reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                       grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]: ...
    def list_killed_sessions(self) -> dict[str, Any]: ...
    def grant_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]: ...
    def grant_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...
    def metrics(self) -> dict[str, Any]: ...


class LocalNodeClient:
    """Wraps this SAME process's own TerminalService -- no network, no
    serialization, no auth boundary (there is nothing to authenticate
    across; the caller is already this process). Every method is a plain
    1:1 delegation, never re-implemented."""

    def __init__(self, terminal: Any) -> None:
        self._terminal = terminal

    def list_sessions(self) -> dict[str, Any]:
        return self._terminal.terminal_list_sessions()

    def status(self, session: str) -> dict[str, Any]:
        return self._terminal.terminal_status(session)

    def tail(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]:
        return self._terminal.terminal_tail(session, lines, ansi=ansi)

    def capture(self, session: str, start_line: int | None = None) -> dict[str, Any]:
        return self._terminal.terminal_capture(session, start_line)

    def send_text(self, session: str, text: str, press_enter: bool = False, dry_run: bool = False, *,
                  idempotency_key: str | None = None, origin: str | None = None, trace_id: str | None = None,
                  parent_turn_id: str | None = None, depth: int = 0) -> dict[str, Any]:
        return self._terminal.terminal_send_text(session, text, press_enter, dry_run, idempotency_key=idempotency_key,
                                                  origin=origin, trace_id=trace_id, parent_turn_id=parent_turn_id,
                                                  depth=depth)

    def send_keys(self, session: str, keys: list[str], confirm_sensitive: bool = False) -> dict[str, Any]:
        return self._terminal.terminal_send_keys(session, keys, confirm_sensitive)

    def input_context(self, session: str | None = None, binding: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_input_context(session, binding)

    def create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, *,
                       initial_prompt: str | None = None, grant_mode: str = "none",
                       binding: str | None = None, requested_by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_create_session(name, agent_type, cwd, initial_prompt=initial_prompt,
                                                       grant_mode=grant_mode, binding=binding,
                                                       requested_by=requested_by)

    def detach_session(self, name: str) -> dict[str, Any]:
        return self._terminal.terminal_detach_session(name)

    def delete_session(self, name: str) -> dict[str, Any]:
        return self._terminal.terminal_delete_session(name)

    def kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_kill_session(name, confirm_name, requested_by=requested_by)

    def reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                       grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_reopen_session(name, agent_type=agent_type, cwd=cwd,
                                                       grant_mode=grant_mode, requested_by=requested_by)

    def list_killed_sessions(self) -> dict[str, Any]:
        return self._terminal.terminal_list_killed_sessions()

    def grant_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        return self._terminal.grant_session_read(name, enabled, granted_by=granted_by)

    def grant_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        return self._terminal.grant_session_input(name, enabled, granted_by=granted_by)

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def metrics(self) -> dict[str, Any]:
        from . import host_metrics
        collected = host_metrics.collect(workspace_path=str(self._terminal.config.session_lifecycle.allowed_cwd_roots[0])
                                         if self._terminal.config.session_lifecycle.allowed_cwd_roots else "/")
        return collected.__dict__


class RemoteNodeClient:
    """HTTP + bearer token -- talks to a terminal-node-agent process on
    another host. `base_url` is the node's own registered endpoint (e.g.
    "http://192.168.1.50:8790"); never routed through the OpenAI tunnel
    or the Cloudflare dashboard tunnel, always a direct LAN/VPN/SSH-
    tunneled address the operator configured (task item 2: "bind private/
    LAN hoặc tunnel, có auth bắt buộc"). Every request carries
    `Authorization: Bearer <token>` -- a missing/wrong token is refused by
    the node agent itself (401/403), never silently treated as "local"."""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items() if v is not None)
            if query:
                url = f"{url}?{query}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # node_agent.py NEVER answers an application-level error
            # (SESSION_NOT_FOUND, ACCESS_DENIED, ...) with a non-200
            # status -- every one of those comes back as a normal 200
            # with {"error": ...} in the body, exactly like calling
            # TerminalService directly would return a plain dict, never
            # an exception. The ONLY non-200 responses this agent ever
            # sends are transport/auth failures (401 UNAUTHORIZED, or an
            # unexpected 404/500 from a genuinely broken request) -- so
            # ANY HTTPError here is a real NodeClientError, never treated
            # as if it were a valid application response. Getting this
            # wrong once (silently returning a 401 body as a normal
            # result) is exactly the bug this comment is here to prevent
            # from regressing -- caught live in this feature's own
            # integration testing, see docs/multi-node.md.
            try:
                body = json.loads(exc.read())
            except (ValueError, OSError):
                body = None
            detail = body.get("error") if isinstance(body, dict) else exc.reason
            raise NodeClientError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise NodeClientError(f"{method} {path} -> {type(exc).__name__}: {exc.reason}") from exc
        except (ValueError, TimeoutError) as exc:
            raise NodeClientError(f"{method} {path} -> {type(exc).__name__}: {exc}") from exc

    def list_sessions(self) -> dict[str, Any]:
        return self._request("GET", "/v1/sessions")

    def status(self, session: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session)}/status")

    def tail(self, session: str, lines: int | None = None, *, ansi: bool = False) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session)}/tail",
                             params={"lines": lines, "ansi": int(ansi)})

    def capture(self, session: str, start_line: int | None = None) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{urllib.parse.quote(session)}/capture",
                             params={"start_line": start_line})

    def send_text(self, session: str, text: str, press_enter: bool = False, dry_run: bool = False, *,
                  idempotency_key: str | None = None, origin: str | None = None, trace_id: str | None = None,
                  parent_turn_id: str | None = None, depth: int = 0) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(session)}/send", body={
            "text": text, "press_enter": press_enter, "dry_run": dry_run, "idempotency_key": idempotency_key,
            "origin": origin, "trace_id": trace_id, "parent_turn_id": parent_turn_id, "depth": depth,
        })

    def send_keys(self, session: str, keys: list[str], confirm_sensitive: bool = False) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(session)}/send-keys",
                             body={"keys": keys, "confirm_sensitive": confirm_sensitive})

    def input_context(self, session: str | None = None, binding: str | None = None) -> dict[str, Any]:
        return self._request("GET", "/v1/input-context", params={"session": session, "binding": binding})

    def create_session(self, name: str, agent_type: str = "shell", cwd: str | None = None, *,
                       initial_prompt: str | None = None, grant_mode: str = "none",
                       binding: str | None = None, requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/v1/sessions", body={
            "name": name, "agent_type": agent_type, "cwd": cwd, "initial_prompt": initial_prompt,
            "grant_mode": grant_mode, "binding": binding, "requested_by": requested_by,
        })

    def detach_session(self, name: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/detach")

    def delete_session(self, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/sessions/{urllib.parse.quote(name)}")

    def kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/kill",
                             body={"confirm_name": confirm_name, "requested_by": requested_by})

    def reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                       grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/reopen", body={
            "agent_type": agent_type, "cwd": cwd, "grant_mode": grant_mode, "requested_by": requested_by,
        })

    def list_killed_sessions(self) -> dict[str, Any]:
        return self._request("GET", "/v1/killed-sessions")

    def grant_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/grant-read",
                             body={"enabled": enabled, "granted_by": granted_by})

    def grant_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/grant-input",
                             body={"enabled": enabled, "granted_by": granted_by})

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/v1/health")

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics")

    def ping(self) -> tuple[bool, float | None, str | None]:
        """Real health check + round-trip latency measurement -- used by
        the heartbeat poller (controller.py) and the dashboard's own
        "Test connection" button. Never raises; a failure is a normal
        (False, None, detail) result."""
        started = time.monotonic()
        try:
            self.health()
        except NodeClientError as exc:
            return False, None, str(exc)
        return True, (time.monotonic() - started) * 1000.0, None
