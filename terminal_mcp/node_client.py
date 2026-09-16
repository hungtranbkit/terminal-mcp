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

    def __init__(self, message: str, *, http_status: int | None = None,
                 error_code: str | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code


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
                       binding: str | None = None, requested_by: str | None = None,
                       show_on_desktop: bool = False,
                       resume_session_id: str | None = None) -> dict[str, Any]: ...
    def detach_session(self, name: str) -> dict[str, Any]: ...
    def delete_session(self, name: str) -> dict[str, Any]: ...
    def kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]: ...
    def rename_session(self, name: str, new_name: str, *, requested_by: str | None = None) -> dict[str, Any]: ...
    def reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                       grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]: ...
    def registry_reopen(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                        grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]: ...
    def registry_list(self, *, recoverable_only: bool = False) -> dict[str, Any]: ...
    def list_killed_sessions(self) -> dict[str, Any]: ...
    def grant_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]: ...
    def grant_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]: ...
    def health(self) -> dict[str, Any]: ...
    def execution_probe(self, timeout_seconds: float = 3.0) -> dict[str, Any]: ...
    def self_heal(self, action: str) -> dict[str, Any]: ...
    def metrics(self) -> dict[str, Any]: ...
    def environment(self, roles: tuple[str, ...] = ("node",)) -> dict[str, Any]: ...
    def repo_evidence(self, cwd: str) -> dict[str, Any]: ...
    def repo_op(self, op: str, path: str, params: dict[str, Any]) -> dict[str, Any]: ...
    def worktree_candidates(self, repo_path: str) -> dict[str, Any]: ...
    def worktree_cleanup(self, worktree_path: str, *, repo_path: str | None = None,
                         expected_head: str | None = None, expected_branch: str | None = None,
                         task: dict[str, Any] | None = None,
                         dry_run: bool = True) -> dict[str, Any]: ...
    def describe_permissions(self, session: str) -> dict[str, Any]: ...
    def set_permissions(self, session: str, *, read: bool | None, input: bool | None,
                        expected_revision: int | None, actor: str | None) -> dict[str, Any]: ...
    def refresh_capabilities(self) -> dict[str, Any]: ...
    def knowledge_search(self, query: str, *, session_name: str | None = None, project: str | None = None,
                         since: str | None = None, until: str | None = None, limit: int = 20) -> dict[str, Any]: ...
    def knowledge_timeline(self, session_name: str, *, since: str | None = None, until: str | None = None,
                           limit: int = 200) -> dict[str, Any]: ...
    def knowledge_recover(self, session_name: str) -> dict[str, Any]: ...
    def knowledge_checkpoint(self, session_name: str, summary: str) -> dict[str, Any]: ...
    def watchdog_session_events(self, *, unacknowledged_only: bool = False, limit: int = 50) -> dict[str, Any]: ...
    def watchdog_acknowledge_session_event(self, event_id: int, *, by: str | None = None) -> dict[str, Any]: ...


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
                       binding: str | None = None, requested_by: str | None = None,
                       show_on_desktop: bool = False,
                       resume_session_id: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_create_session(name, agent_type, cwd, initial_prompt=initial_prompt,
                                                       grant_mode=grant_mode, binding=binding,
                                                       requested_by=requested_by, show_on_desktop=show_on_desktop,
                                                       resume_session_id=resume_session_id)

    def detach_session(self, name: str) -> dict[str, Any]:
        return self._terminal.terminal_detach_session(name)

    def delete_session(self, name: str) -> dict[str, Any]:
        return self._terminal.terminal_delete_session(name)

    def kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_kill_session(name, confirm_name, requested_by=requested_by)

    def rename_session(self, name: str, new_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_rename_session(name, new_name, requested_by=requested_by)

    def reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                       grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_reopen_session(name, agent_type=agent_type, cwd=cwd,
                                                       grant_mode=grant_mode, requested_by=requested_by)

    def registry_reopen(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                        grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        # Phase 0 node-agent restart-safety audit (2026-09-06): the
        # Persistent Session Registry's own honest, MISSING/OFFLINE-
        # aware reopen (session_registry.py) -- distinct from
        # reopen_session above, which only ever knows about sessions an
        # explicit terminal_kill_session call recorded (killed_sessions.
        # py). This is the ONLY reopen path that works for a session that
        # vanished via a node-agent restart (never explicitly Killed).
        return self._terminal.terminal_registry_reopen(name, agent_type=agent_type, cwd=cwd,
                                                        grant_mode=grant_mode, requested_by=requested_by)

    def registry_list(self, *, recoverable_only: bool = False) -> dict[str, Any]:
        # Auto Recovery follow-up (2026-09-07): the fleet-aware read this
        # feature's own reconciliation engine needs -- session_registry.
        # py is per-node-agent-process-local (each node has its OWN
        # session_registry.db), so a recovery engine running on the
        # controller has no other way to see a REMOTE node's own
        # registry rows. Real gap identified in this feature's own audit
        # (registry_list/_search/_get were never fleet-aware before this
        # -- see docs/REQUIREMENTS.md's own Phase 0 note on that
        # disclosed scope cut).
        return self._terminal.terminal_registry_list(recoverable_only=recoverable_only)

    def list_killed_sessions(self) -> dict[str, Any]:
        return self._terminal.terminal_list_killed_sessions()

    def grant_read(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        return self._terminal.grant_session_read(name, enabled, granted_by=granted_by)

    def grant_input(self, name: str, enabled: bool, *, granted_by: str | None = None) -> dict[str, Any]:
        return self._terminal.grant_session_input(name, enabled, granted_by=granted_by)

    def health(self) -> dict[str, Any]:
        return {"status": "ok"}

    def execution_probe(self, timeout_seconds: float = 3.0) -> dict[str, Any]:
        result = self._terminal.terminal_list_sessions()
        if "error" in result:
            return {"execution_ok": False, "error": result["error"]}
        return {"execution_ok": True, "agent_process_alive": True,
                "session_count": len(result.get("sessions", [])),
                "session_backend": type(self._terminal.tmux).__name__}

    def self_heal(self, action: str) -> dict[str, Any]:
        return {"error": "SELF_HEAL_NOT_SUPPORTED_FOR_LOCAL_NODE", "action": action}

    def metrics(self) -> dict[str, Any]:
        from . import host_metrics
        collected = host_metrics.collect(workspace_path=str(self._terminal.config.session_lifecycle.allowed_cwd_roots[0])
                                         if self._terminal.config.session_lifecycle.allowed_cwd_roots else "/")
        return collected.__dict__

    def environment(self, roles: tuple[str, ...] = ("node",)) -> dict[str, Any]:
        from . import node_profile
        return node_profile.inventory(tuple(roles))

    def repo_evidence(self, cwd: str) -> dict[str, Any]:
        """Git metadata for a path on THIS host (controller == node here)."""
        from .coordinator import RepoEvidenceError, git_repo_evidence

        try:
            evidence = git_repo_evidence(cwd)
        except RepoEvidenceError as exc:
            return {"error": "REPO_EVIDENCE_FAILED", "detail": str(exc)}
        return {"cwd": cwd, "branch": evidence.branch, "head": evidence.head,
                "clean": evidence.clean, "status_lines": list(evidence.status_lines),
                "has_upstream": evidence.has_upstream,
                "ahead": evidence.ahead, "behind": evidence.behind}

    def repo_op(self, op: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Read-only repo introspection on THIS host (controller == node).

        Goes through the SAME repo_read.run_operation the remote node's
        HTTP endpoint calls, with the policy built from this process's own
        config -- so a local repo and a remote one are read by identical
        code under identical limits, and a behaviour difference between
        them would be a bug in the transport, never in the engine."""
        from . import repo_read

        config = self._terminal.config
        policy = config.repo_read.to_policy(config.session_lifecycle.allowed_cwd_roots)
        return repo_read.run_operation(op, path, params or {}, policy)

    def worktree_candidates(self, repo_path: str) -> dict[str, Any]:
        """Classify this host's worktrees (controller == node here).

        Goes through the SAME worktree_janitor.scan the remote node's HTTP
        handler calls, with the policy built from this process's own config --
        so local and remote are decided by identical code under identical
        rules, and any difference between them would be a transport bug rather
        than a policy one."""
        from . import worktree_janitor

        config = self._terminal.config.worktree_janitor
        result = worktree_janitor.scan(repo_path, config.to_policy())
        result["node_id"] = "local"
        return result

    def worktree_cleanup(self, worktree_path: str, *, repo_path: str | None = None,
                         expected_head: str | None = None, expected_branch: str | None = None,
                         task: dict[str, Any] | None = None,
                         dry_run: bool = True) -> dict[str, Any]:
        """Remove one worktree on this host. Same executor, same identity
        pre-check and same force=False guarantee the remote handler applies."""
        from . import git_worktree
        from .lease import ResourceLockStore
        from .worktree_executor import WorktreeExecutor

        config = self._terminal.config.worktree_janitor
        if expected_head or expected_branch:
            observed = git_worktree.worktree_status(repo_path or worktree_path, worktree_path)
            if not observed.get("exists"):
                return {"outcome": "SKIPPED", "error": "WORKTREE_ABSENT",
                        "worktree_path": worktree_path, "node_id": "local"}
            if expected_head and observed.get("head_sha") != expected_head:
                return {"outcome": "ABORTED", "error": "IDENTITY_MISMATCH",
                        "worktree_path": worktree_path, "node_id": "local",
                        "expected_head": expected_head, "observed_head": observed.get("head_sha")}
            if expected_branch and observed.get("branch") != expected_branch:
                return {"outcome": "ABORTED", "error": "IDENTITY_MISMATCH",
                        "worktree_path": worktree_path, "node_id": "local",
                        "expected_branch": expected_branch,
                        "observed_branch": observed.get("branch")}
        executor = WorktreeExecutor(
            config.to_policy(), audit=getattr(self._terminal, "audit", None),
            locks=ResourceLockStore(self._terminal.leases.path), node_id="local")
        from . import worktree_janitor

        probes = worktree_janitor.collect_local_probes(
            getattr(self._terminal, "session_registry", None))
        result = executor.execute({"worktree_path": worktree_path, "node_id": "local"},
                                  task=task, repo_path=repo_path, dry_run=dry_run, **probes)
        payload = result.to_dict()
        payload["node_id"] = "local"
        return payload

    def describe_permissions(self, session: str) -> dict[str, Any]:
        return self._terminal.describe_session_permissions(session)

    def set_permissions(self, session: str, *, read=None, input=None,
                        expected_revision=None, actor=None) -> dict[str, Any]:
        return self._terminal.set_session_permissions(
            session, read=read, input=input, expected_revision=expected_revision, actor=actor)

    def refresh_capabilities(self) -> dict[str, Any]:
        from .agent_availability import available_agent_types
        from .launcher_resolution import resolve_launcher
        commands = self._terminal.config.session_lifecycle.launch_commands
        return {
            "agent_types": list(available_agent_types(commands)),
            "launcher_paths": {agent: resolve_launcher(command) for agent, command in commands},
        }

    def knowledge_search(self, query: str, *, session_name: str | None = None, project: str | None = None,
                         since: str | None = None, until: str | None = None, limit: int = 20) -> dict[str, Any]:
        return self._terminal.terminal_knowledge_search(query, session_name=session_name, project=project,
                                                        since=since, until=until, limit=limit)

    def knowledge_timeline(self, session_name: str, *, since: str | None = None, until: str | None = None,
                           limit: int = 200) -> dict[str, Any]:
        return self._terminal.terminal_knowledge_timeline(session_name, since=since, until=until, limit=limit)

    def knowledge_recover(self, session_name: str) -> dict[str, Any]:
        return self._terminal.terminal_knowledge_recover(session_name)

    def knowledge_checkpoint(self, session_name: str, summary: str) -> dict[str, Any]:
        return self._terminal.terminal_knowledge_checkpoint(session_name, summary)

    def watchdog_session_events(self, *, unacknowledged_only: bool = False, limit: int = 50) -> dict[str, Any]:
        return self._terminal.terminal_watchdog_events(unacknowledged_only=unacknowledged_only, limit=limit)

    def watchdog_acknowledge_session_event(self, event_id: int, *, by: str | None = None) -> dict[str, Any]:
        return self._terminal.terminal_watchdog_acknowledge(event_id, by=by)


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

    def set_token(self, token: str) -> None:
        """Point this client at a rotated credential in place.

        Every request reads `self._token` when it is sent, so replacing
        it here is enough -- no client needs rebuilding and no in-flight
        caller holds a stale one. An empty token is deliberately allowed:
        that is how revocation stops this controller from continuing to
        command a node with a credential it has just refused."""
        self._token = token

    def fleet_exchange(self, *, objects: list[dict[str, Any]], since: str | None,
                       source_node: str) -> dict[str, Any]:
        """One fleet-metadata exchange with this node.

        A node agent older than the fleet endpoint answers 404, which
        _request turns into NodeClientError -- the caller records that node as
        unsupported rather than failed, so rolling the fleet out one machine
        at a time never makes the healthy ones look broken.
        """
        return self._request("POST", "/v1/fleet/objects",
                             body={"objects": objects, "since": since, "from": source_node})

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None,
                timeout_seconds: float | None = None) -> dict[str, Any]:
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
            with urllib.request.urlopen(request, timeout=(self.timeout if timeout_seconds is None
                                                         else min(self.timeout, timeout_seconds))) as response:
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
            raise NodeClientError(f"{method} {path} -> HTTP {exc.code}: {detail}",
                                  http_status=exc.code,
                                  error_code=str(detail) if detail is not None else None) from exc
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
                       binding: str | None = None, requested_by: str | None = None,
                       show_on_desktop: bool = False,
                       resume_session_id: str | None = None) -> dict[str, Any]:
        return self._request("POST", "/v1/sessions", body={
            "name": name, "agent_type": agent_type, "cwd": cwd, "initial_prompt": initial_prompt,
            "grant_mode": grant_mode, "binding": binding, "requested_by": requested_by,
            "show_on_desktop": show_on_desktop, "resume_session_id": resume_session_id,
        })

    def detach_session(self, name: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/detach")

    def delete_session(self, name: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v1/sessions/{urllib.parse.quote(name)}")

    def kill_session(self, name: str, confirm_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/kill",
                             body={"confirm_name": confirm_name, "requested_by": requested_by})

    def rename_session(self, name: str, new_name: str, *, requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/rename",
                             body={"new_name": new_name, "requested_by": requested_by})

    def reopen_session(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                       grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/reopen", body={
            "agent_type": agent_type, "cwd": cwd, "grant_mode": grant_mode, "requested_by": requested_by,
        })

    def registry_reopen(self, name: str, *, agent_type: str | None = None, cwd: str | None = None,
                        grant_mode: str = "none", requested_by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{urllib.parse.quote(name)}/registry-reopen", body={
            "agent_type": agent_type, "cwd": cwd, "grant_mode": grant_mode, "requested_by": requested_by,
        })

    def registry_list(self, *, recoverable_only: bool = False) -> dict[str, Any]:
        return self._request("GET", "/v1/registry", params={"recoverable_only": int(recoverable_only)})

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

    def execution_probe(self, timeout_seconds: float = 3.0) -> dict[str, Any]:
        try:
            return self._request("GET", "/v1/execution-health", timeout_seconds=timeout_seconds)
        except NodeClientError as exc:
            # Rolling upgrade compatibility: the authenticated sessions
            # listing exercises the same backend on agents predating the
            # richer endpoint. Only 404 falls back; auth/transport failures
            # retain their evidence.
            if exc.http_status != 404:
                raise
        result = self._request("GET", "/v1/sessions", timeout_seconds=timeout_seconds)
        if "error" in result:
            return {"execution_ok": False, "error": result["error"]}
        return {"execution_ok": True, "agent_process_alive": True,
                "session_count": len(result.get("sessions", [])),
                "probe_mode": "legacy_sessions_fallback"}

    def self_heal(self, action: str) -> dict[str, Any]:
        if action != "graceful_agent_restart":
            return {"error": "SELF_HEAL_ACTION_NOT_ALLOWLISTED", "action": action}
        return self._request("POST", "/v1/internal/shutdown", body={})

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics")

    def environment(self, roles: tuple[str, ...] = ("node",)) -> dict[str, Any]:
        return self._request("GET", "/v1/environment?roles=" + ",".join(roles))

    def repo_evidence(self, cwd: str) -> dict[str, Any]:
        """Ask THIS node for git metadata about one of its own paths.

        A node whose agent predates this endpoint answers 404. That is
        surfaced as an error payload rather than swallowed, because the
        caller must tell "this repo is dirty" apart from "this node cannot
        say" -- confusing the two is the bug this endpoint exists to fix.
        """
        import urllib.parse as _urlparse

        return self._request(
            "GET", "/v1/repo-evidence?cwd=" + _urlparse.quote(str(cwd), safe=""))

    def repo_op(self, op: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """Ask THIS node to read one of its own repositories.

        A node whose agent predates /v1/repo/{op} answers 404, which
        surfaces as a NodeClientError from `_request` and is reported by
        the caller as NODE_UNREACHABLE/unsupported -- never as "the repo
        said no". That is the same distinction /v1/repo-evidence had to
        draw, and confusing the two here would be worse: a caller would
        conclude a file does not exist when the truth is that nobody
        looked.
        """
        import urllib.parse as _urlparse

        query: list[tuple[str, str]] = [("path", str(path))]
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            if isinstance(value, bool):
                query.append((key, "true" if value else "false"))
            elif isinstance(value, (list, tuple)):
                # Repeated key, not a joined string: a pathspec may legally
                # contain a comma.
                query.extend((key, str(item)) for item in value)
            else:
                query.append((key, str(value)))
        return self._request(
            "GET", f"/v1/repo/{_urlparse.quote(str(op), safe='')}?"
                   + _urlparse.urlencode(query))

    def worktree_candidates(self, repo_path: str) -> dict[str, Any]:
        """Ask THIS node to classify its own worktrees.

        A node whose agent predates these routes answers 404, which _request
        raises as NodeClientError -- reported by the caller as
        NODE_LACKS_WORKTREE_JANITOR, kept distinct from NODE_UNREACHABLE. An old
        agent and a dead one need different operator action."""
        import urllib.parse as _urlparse

        return self._request("GET", "/v1/worktree/candidates?repo_path="
                            + _urlparse.quote(str(repo_path), safe=""))

    def worktree_cleanup(self, worktree_path: str, *, repo_path: str | None = None,
                         expected_head: str | None = None, expected_branch: str | None = None,
                         task: dict[str, Any] | None = None,
                         dry_run: bool = True) -> dict[str, Any]:
        """Ask THIS node to remove one of its own worktrees.

        The controller never removes a remote path itself -- it cannot see that
        filesystem, and a same-named directory here is exactly what it would
        delete instead. expected_head/expected_branch travel so the node can
        refuse a stale view rather than act on it."""
        return self._request("POST", "/v1/worktree/cleanup", body={
            "worktree_path": worktree_path, "repo_path": repo_path,
            "expected_head": expected_head, "expected_branch": expected_branch,
            "task": task, "dry_run": dry_run})

    def describe_permissions(self, session: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/sessions/{session}/permissions")

    def set_permissions(self, session: str, *, read=None, input=None,
                        expected_revision=None, actor=None) -> dict[str, Any]:
        return self._request("POST", f"/v1/sessions/{session}/permissions",
                             {"read": read, "input": input,
                              "expected_revision": expected_revision, "actor": actor})

    def refresh_capabilities(self) -> dict[str, Any]:
        return self._request("POST", "/v1/capabilities/refresh")

    def knowledge_search(self, query: str, *, session_name: str | None = None, project: str | None = None,
                         since: str | None = None, until: str | None = None, limit: int = 20) -> dict[str, Any]:
        return self._request("GET", "/v1/knowledge/search", params={
            "query": query, "session_name": session_name, "project": project,
            "since": since, "until": until, "limit": limit,
        })

    def knowledge_timeline(self, session_name: str, *, since: str | None = None, until: str | None = None,
                           limit: int = 200) -> dict[str, Any]:
        return self._request("GET", f"/v1/knowledge/timeline/{urllib.parse.quote(session_name)}",
                             params={"since": since, "until": until, "limit": limit})

    def knowledge_recover(self, session_name: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/knowledge/recover/{urllib.parse.quote(session_name)}")

    def knowledge_checkpoint(self, session_name: str, summary: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/knowledge/checkpoint/{urllib.parse.quote(session_name)}",
                             body={"summary": summary})

    def watchdog_session_events(self, *, unacknowledged_only: bool = False, limit: int = 50) -> dict[str, Any]:
        return self._request("GET", "/v1/watchdog/events",
                             params={"unacknowledged_only": int(unacknowledged_only), "limit": limit})

    def watchdog_acknowledge_session_event(self, event_id: int, *, by: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/watchdog/acknowledge/{event_id}", body={"by": by})

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
