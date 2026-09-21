"""`terminal-node-agent` -- the lightweight process a WORKER node (e.g. a
future M910) runs. Wraps ONE local TerminalService (the exact same class
Dell's own controller uses for its local node -- zero duplicated tmux/
permission/audit logic) and exposes ONLY the narrow operation set
node_client.py's NodeClient Protocol defines (task item 2: "Agent chỉ
expose các operation cần thiết... Không expose arbitrary shell
endpoint") -- no raw shell, no arbitrary command execution, no tmux
socket access from outside this process.

Every route (except /v1/health, a bare liveness probe) requires
`Authorization: Bearer <token>` matching this agent's own configured
shared secret (env `TERMINAL_MCP_NODE_TOKEN`, or `--token-file`) --
mirrors this project's existing CONTROL_PLANE_API_KEY-via-env convention
(tunnel-client) rather than inventing a new auth style. A missing/wrong
token is refused (401) before touching TerminalService at all.

Also runs a background heartbeat loop: every `heartbeat_interval_seconds`
(default 20s), POSTs this node's own collected metrics
(host_metrics.collect) to the controller's heartbeat-receiving route
(dashboard.py's /dashboard/api/nodes/{node_id}/heartbeat), authenticated
with the SAME shared token. If the controller is unreachable, this loop
just keeps retrying -- tmux/Claude/Codex sessions on THIS node are
completely unaffected either way (task item 2: "Nếu controller mất kết
nối, tmux/Claude/Codex trên node vẫn tiếp tục sống" -- this process
never blocks any session operation on the heartbeat loop's own success).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import anyio
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from . import __version__, host_metrics
from .agent_availability import available_agent_types
from .capability_probe import probe_capabilities
from .auth_throttle import AuthThrottle, client_key
from .contract import describe as contract_describe
from . import node_profile
from .launcher_resolution import resolve_launcher
from .config import load_config
from .coordinator import RepoEvidenceError, git_repo_evidence
from . import repo_read
from . import git_worktree, worktree_janitor
from .lifecycle import resolve_cwd
from .core import TerminalService
from .node_client import LocalNodeClient
from .webterm import WebTerminalProcess, pump_websocket

_log = logging.getLogger(__name__)

# Process generation id (Phase 0 node-agent restart-safety audit,
# 2026-09-06, task item 2: "process generation" as part of the explicit,
# no-fake-resurrection recovery contract) -- a fresh random id computed
# ONCE per process, at import time, never persisted/reused across a
# restart. Exposed in /v1/health and every heartbeat push so the
# controller/dashboard/session_registry can tell "this is a NEW agent
# process instance" apart from the one that was running a moment ago,
# even though node_id/host/port are all unchanged -- e.g. to show
# "sessions reported by generation X are gone; this is generation Y"
# rather than silently conflating two different process lifetimes under
# the same node_id. Deliberately NOT a monotonic counter (would need its
# own persisted state to survive correctly across every possible restart
# path, including a fresh state dir) -- a random id is simpler and just
# as sufficient for "is this the same process instance or not".
AGENT_GENERATION = secrets.token_hex(8)


def _read_token(token: str | None, token_file: str | None) -> str:
    if token:
        return token
    env_token = os.environ.get("TERMINAL_MCP_NODE_TOKEN")
    if env_token:
        return env_token
    if token_file:
        return Path(token_file).expanduser().read_text().strip()
    raise SystemExit("no node token configured -- set TERMINAL_MCP_NODE_TOKEN, or pass --token/--token-file")


class AgentCredential:
    """This agent's own bearer token, swappable while the process runs.

    blg_a3cc401d8275. The token used to be a closure variable captured at
    startup, which is why rotating one meant editing a file and
    restarting the agent -- and why rotating dell-5530 was deferred on
    2026-09-09 rather than cost six live sessions.

    Holding it here instead makes two things possible. The controller can
    hand this agent a replacement over the already-authenticated
    heartbeat channel and it takes effect on the next request, no
    restart. And during the handoff the agent keeps accepting the
    PREVIOUS token for a short window -- the controller only moves its
    own outbound copy once it has seen a heartbeat signed with the new
    one, so there is an unavoidable moment where it is still calling in
    with the old token. Accepting both is what makes that moment
    invisible instead of a burst of 401s.
    """

    PREVIOUS_GRACE_SECONDS = 900

    def __init__(self, token: str, *, token_file: str | None = None,
                 previous_grace_seconds: float | None = None) -> None:
        self._token = token
        self._previous: str | None = None
        self._previous_until = 0.0
        self.token_file = token_file
        self._grace = (self.PREVIOUS_GRACE_SECONDS if previous_grace_seconds is None
                       else previous_grace_seconds)

    @classmethod
    def of(cls, value: "str | AgentCredential") -> "AgentCredential":
        return value if isinstance(value, cls) else cls(value)

    @property
    def current(self) -> str:
        return self._token

    @property
    def token_id(self) -> str:
        """The same 12-hex fingerprint the controller uses, so a log line
        on either machine names the same credential without either of
        them writing it down."""
        return hashlib.sha256(self._token.encode("utf-8")).hexdigest()[:12]

    def accepts(self, presented: str) -> bool:
        if hmac.compare_digest(presented, self._token):
            return True
        if self._previous and time.time() < self._previous_until:
            return hmac.compare_digest(presented, self._previous)
        return False

    def adopt(self, token: str) -> bool:
        """Take a replacement token. Idempotent -- adopting the token we
        already hold is a no-op, so a duplicated controller hint cannot
        push the real previous token out of its grace window."""
        if not token or token == self._token:
            return False
        self._previous = self._token
        self._previous_until = time.time() + self._grace
        self._token = token
        self._persist(token)
        return True

    def _persist(self, token: str) -> None:
        """Survive a restart. Best-effort on purpose: an agent that
        cannot write its token file has still rotated successfully in
        memory, and failing the rotation over it would be worse than the
        next restart falling back to the old token (which the controller
        still accepts in grace)."""
        if not self.token_file:
            return
        try:
            target = Path(self.token_file).expanduser()
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(descriptor, token.encode())
            finally:
                os.close(descriptor)
            # O_CREAT's mode applies only to a file it CREATES -- the
            # common case here is overwriting one the installer wrote,
            # whose mode is whatever that installer chose.
            os.chmod(target, 0o600)
        except OSError:
            _log.exception("could not persist the rotated node token to %s -- "
                           "it is live in memory but a restart will fall back", self.token_file)


def _auth_ok(request: Request, expected_token: "str | AgentCredential") -> bool:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return False
    presented = header[len("Bearer "):]
    # Constant-time comparison -- a timing side-channel on this specific
    # check would leak the shared secret one byte at a time to a network
    # attacker; every other auth comparison in this project (webauth.py)
    # already uses the same discipline for the same reason.
    return AgentCredential.of(expected_token).accepts(presented)


def build_node_agent(*, node_id: str, terminal: TerminalService, token: "str | AgentCredential",
                     workspace_root: str = "/",
                     fleet: "FleetService | None" = None,
                     throttle: "AuthThrottle | None" = None,
                     trust_forwarded_for: bool = False) -> Starlette:
    # A plain string still works (every existing caller and test passes
    # one); it is wrapped so that THIS process has exactly one credential
    # object, and a rotation adopted by the heartbeat loop is immediately
    # true for every route below.
    credential = AgentCredential.of(token)
    client = LocalNodeClient(terminal)
    # Brute-force protection for the shared bearer secret. Default-on: an
    # opt-in guard protects only the deployments that already thought about
    # it, which are not the ones that need it.
    #
    # `trust_forwarded_for` stays OFF unless a deployment declares a trusted
    # proxy. Honouring the header by default would let any caller choose their
    # own throttle bucket and never be limited at all.
    throttle = throttle if throttle is not None else AuthThrottle()

    # Built lazily and cached: a node agent must still start and serve
    # sessions on a box where the fleet cache cannot be opened (read-only
    # state dir, a disk that filled). A fleet endpoint that answers
    # FLEET_REGISTRY_UNAVAILABLE is a degraded fleet; an agent that refuses
    # to boot is a lost node.
    _fleet: dict[str, object] = {"service": fleet, "tried": fleet is not None}

    def fleet_service():
        if not _fleet["tried"]:
            _fleet["tried"] = True
            try:
                from .fleet_registry import FleetRegistryStore
                from .fleet_service import FleetService

                _fleet["service"] = FleetService(
                    FleetRegistryStore(local_node_id=node_id), local_node_id=node_id)
            except Exception:  # noqa: BLE001 -- never block the agent on this
                _log.exception("fleet registry unavailable on this node")
                _fleet["service"] = None
        return _fleet["service"]

    def require_auth(request: Request) -> JSONResponse | None:
        """Throttled bearer check.

        The throttle is consulted BEFORE the comparison, so a locked-out
        source never reaches it. The 401 body is byte-identical whether the
        token was wrong, malformed or absent: a response that differs by cause
        tells an attacker which of those they achieved.
        """
        key = client_key(
            request.client.host if request.client else None,
            forwarded_for=request.headers.get("x-forwarded-for"),
            trust_forwarded=trust_forwarded_for)
        decision = throttle.check(key)
        if not decision.allowed:
            # 429, not 401. The source is being rate limited, which is true
            # regardless of whether the token it is about to present is
            # correct -- and saying 401 here would let an attacker use the
            # status code to distinguish "locked" from "wrong token".
            return JSONResponse({"error": "TOO_MANY_ATTEMPTS"}, status_code=429,
                                headers=decision.headers())
        # `credential`, not `token`: main replaced the plain shared string with
        # a credential object that also accepts the previous token during a
        # rotation grace window. Comparing against the raw `token` argument
        # here would have quietly reverted rotation for every request.
        if not _auth_ok(request, credential):
            throttle.record_failure(key)
            return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
        throttle.record_success(key)
        return None

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "node_id": node_id, "version": __version__,
                             "agent_generation": AGENT_GENERATION, **contract_describe()})

    async def execution_health(request: Request) -> JSONResponse:
        """Cheap authenticated proof that the execution backend answers.

        Process liveness alone is the public /v1/health contract. This route
        crosses the TerminalService/backend boundary and therefore prevents a
        live HTTP process with a dead tmux/PTY child from reading green.
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            result = await anyio.to_thread.run_sync(terminal.terminal_list_sessions)
        except Exception as exc:  # noqa: BLE001 - normalized, no traceback/body leakage
            return JSONResponse({"execution_ok": False, "agent_process_alive": True,
                                 "error": type(exc).__name__,
                                 "agent_generation": AGENT_GENERATION})
        if not isinstance(result, dict) or result.get("error"):
            return JSONResponse({"execution_ok": False, "agent_process_alive": True,
                                 "error": str((result or {}).get("error") or "MALFORMED_BACKEND_RESPONSE")[:200],
                                 "agent_generation": AGENT_GENERATION})
        return JSONResponse({"execution_ok": True, "agent_process_alive": True,
                             "session_count": len(result.get("sessions", [])),
                             "session_backend": type(terminal.tmux).__name__,
                             "agent_generation": AGENT_GENERATION})

    async def internal_shutdown(request: Request) -> JSONResponse:
        """Deterministic, graceful self-shutdown (Phase 0 node-agent
        restart-safety audit, 2026-09-06) -- REPLACES relying on
        `schtasks /end`/an external TerminateProcess to stop this
        process for a restart. Real, live-reproduced bug this exists to
        fix: `schtasks /end` on this project's own real Scheduled Task
        deployment shape was confirmed (both against the real dell-5530
        node-agent AND a fully isolated disposable one, same result
        twice) to be non-deterministic and unsafe -- it left the actual
        node-agent process (the one holding the port) running untouched
        while SEPARATELY, silently killing that process's own ConPTY
        session children, with no error and no visible sign anything had
        happened. Never once did it actually achieve the restart it was
        asked for either. This endpoint sidesteps that whole ambiguity:
        the process asks uvicorn to stop serving via its own supported
        `should_exit` mechanism and then returns from `main()` through
        the ordinary, unwound Python interpreter shutdown path -- no
        TerminateProcess, no reliance on Task Scheduler's own process-
        tree bookkeeping at all. A caller (deploy tooling, an operator)
        still separately triggers the Scheduled Task's own start trigger
        (or waits for its at-logon/at-startup trigger) to bring a NEW
        instance up once this one has actually exited -- this endpoint
        only ever stops, never restarts, by design (a single action
        doing both would remove the caller's own ability to verify the
        stop actually completed, e.g. by polling for the port to free,
        before starting a new one).

        Whether a session's ConPTY child processes actually survive THIS
        graceful exit path (as opposed to the schtasks-driven kill this
        replaces) is exactly the open question the disposable-session
        restart tests (this task's own item 3) exist to answer with real
        evidence -- never asserted here."""
        if (blocked := require_auth(request)) is not None:
            return blocked
        request.app.state.shutdown_event.set()
        return JSONResponse({"shutdown_requested": True, "node_id": node_id,
                             "agent_generation": AGENT_GENERATION, **contract_describe()})

    async def metrics(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(lambda: client.metrics())
        return JSONResponse(result)

    async def repo_evidence(request: Request) -> JSONResponse:
        """Git branch/HEAD/dirty state for a path ON THIS NODE.

        Exists because the controller cannot see this node's filesystem: its
        pre-dispatch gate needs repo evidence for a session running here, and
        running `git` locally against this node's cwd told it only that the
        path does not exist -- which it then reported as a broken repo.

        Metadata ONLY. Branch name, commit id, porcelain status lines and
        ahead/behind counts; never file contents, never a diff. The path is
        confined to this node's configured allowed_cwd_roots, so this cannot
        be used to probe arbitrary directories.
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        cwd = (request.query_params.get("cwd") or "").strip()
        if not cwd:
            return JSONResponse({"error": "CWD_REQUIRED"}, status_code=400)
        # `terminal.config`, not a bare `config`: this endpoint referred to
        # an undefined name from the day it was written, so EVERY request to
        # it raised NameError and answered 500. The controller's own
        # node_aware_repo_evidence reports a non-200 as
        # RepoEvidenceUnavailable ("we could not look") and fails closed, so
        # the breakage was safe but silent -- remote repo evidence had never
        # once actually been collected. Found 2026-09-14 while adding
        # /v1/repo/{op}; see tests/test_repo_read_node.py's own regression
        # test for this exact route.
        resolved, error = resolve_cwd(cwd, terminal.config)
        if error is not None:
            # "Not here" is not "not allowed". resolve_cwd reports both, and
            # mapping them both to 403 told a caller its path was forbidden
            # when the node simply did not have it -- sending whoever read
            # that off to fix a permission that was never the problem.
            if error.get("error") == "CWD_NOT_FOUND":
                return JSONResponse({
                    "cwd": cwd, "repo_valid": False, "exists": False, "readable": False,
                    "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "error": "PATH_NOT_FOUND",
                    "detail": f"{cwd} does not exist on this node",
                    **contract_describe(),
                }, status_code=200)
            return JSONResponse({"error": "PATH_NOT_ALLOWED", "detail": error},
                                status_code=403)
        # Say WHICH precondition failed. "the path is not here", "it is here
        # but unreadable" and "it is a broken repo" call for different actions
        # from whoever is looking; collapsing them made a healthy repo on an
        # unreachable path indistinguishable from a corrupt one.
        path = Path(str(resolved))
        exists = path.is_dir()
        readable = bool(exists and os.access(str(path), os.R_OK | os.X_OK))
        # When the evidence was READ, so a caller can tell fresh from stale
        # rather than trusting that a reply is about the present moment.
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        base = {"cwd": str(resolved), "exists": exists, "readable": readable,
                "collected_at": collected_at, **contract_describe()}
        if not exists or not readable:
            return JSONResponse({
                **base, "repo_valid": False,
                "error": "PATH_NOT_READABLE" if exists else "PATH_NOT_FOUND",
                "detail": (f"{resolved} exists but this agent cannot read it"
                           if exists else f"{resolved} does not exist on this node"),
            }, status_code=200)
        try:
            evidence = git_repo_evidence(str(resolved))
        except RepoEvidenceError as exc:
            # A real repo problem on this node -- reported as such, and
            # distinct from "this node cannot answer", which is a transport
            # failure the caller sees as a non-200 instead.
            return JSONResponse({**base, "repo_valid": False,
                                 "error": "REPO_EVIDENCE_FAILED", "detail": str(exc)},
                                status_code=200)
        return JSONResponse({
            **base, "repo_valid": True,
            "branch": evidence.branch, "head": evidence.head,
            # `dirty` next to `clean`: callers phrase this both ways, and
            # inverting a boolean by hand is where such a check gets quietly
            # reversed.
            "dirty": not evidence.clean, "clean": evidence.clean,
            "status_lines": list(evidence.status_lines),
            "has_upstream": evidence.has_upstream,
            "ahead": evidence.ahead, "behind": evidence.behind,
        })

    async def repo_op(request: Request) -> JSONResponse:
        """Read-only repository introspection for a path ON THIS NODE --
        the content-bearing sibling of /v1/repo-evidence.

        Exists for the same reason that endpoint does: the controller
        cannot see this node's filesystem, so a repo that lives on
        dell-linux/hp/windows can only be read by asking the agent that
        runs there. /v1/repo-evidence deliberately returns metadata only;
        an external agent that wants to READ the code needs this.

        GET-only, and the operation is looked up in
        repo_read.OPERATIONS -- a fixed table of ten read functions. There
        is no path by which a request can name a git subcommand, a shell
        string or an argv list, so this endpoint cannot write, check out,
        reset or fetch anything no matter what it is sent. The allowlist,
        the secret-path denial, the redaction and the output caps are all
        applied inside repo_read against THIS node's own config.
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        op = (request.path_params.get("op") or "").strip()
        # 200 with an error CODE, not a 4xx: this agent never answers an
        # application-level refusal with a non-200 status (see
        # node_client._request's own comment -- every non-200 is read as a
        # transport failure by every layer above, so a bad argument sent as
        # 400 would be reported to the caller as "the node is unreachable").
        # A 404 from this route still means what it should: an agent old
        # enough not to have the endpoint at all.
        if op not in repo_read.OPERATIONS:
            return JSONResponse({"error": "INVALID_ARGUMENT", "op": op, "node_id": node_id,
                                 "detail": f"unknown repo operation; known: "
                                           f"{', '.join(repo_read.OPERATION_NAMES)}"})
        path = (request.query_params.get("path") or "").strip()
        if not path:
            return JSONResponse({"error": "INVALID_ARGUMENT", "op": op, "node_id": node_id,
                                 "detail": "path is required"})
        # `paths` is genuinely multi-valued (a pathspec list), so it is read
        # with getlist rather than collapsed to whichever copy came last.
        raw: dict[str, object] = {}
        for key in request.query_params.keys():
            if key in ("path", "op"):
                continue
            values = request.query_params.getlist(key)
            raw[key] = values if len(values) > 1 else values[0]
        config = terminal.config
        policy = config.repo_read.to_policy(config.session_lifecycle.allowed_cwd_roots)
        result = await anyio.to_thread.run_sync(
            lambda: repo_read.run_operation(op, path, raw, policy))
        result["node_id"] = node_id
        # A refusal is a 200 carrying an error CODE, exactly like
        # /v1/repo-evidence's REPO_EVIDENCE_FAILED: the caller has to be
        # able to tell "this node answered and said no" from "this node
        # could not be reached", and a non-2xx status means the latter to
        # every layer above.
        return JSONResponse(result)

    async def worktree_candidates(request: Request) -> JSONResponse:
        """Classify the worktrees of a repo ON THIS NODE (audit-only).

        Exists for the same reason /v1/repo/{op} does: the controller cannot see
        this node's filesystem. A worktree at C:\\Users\\tranv\\project or
        /home/dell/workspace/x does not exist there, and a controller that ran
        the classifier locally against that path would be answering about
        whatever it happens to have at the same name -- contract failure mode F4.

        READ-ONLY. This route cannot remove anything; the classifier it calls
        has no deletion primitive at all.
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        repo_path = (request.query_params.get("repo_path") or "").strip()
        if not repo_path:
            return JSONResponse({"error": "INVALID_ARGUMENT", "node_id": node_id,
                                 "detail": "repo_path is required"})
        config = terminal.config.worktree_janitor
        result = await anyio.to_thread.run_sync(
            lambda: worktree_janitor.scan(repo_path, config.to_policy()))
        result["node_id"] = node_id
        return JSONResponse(result)

    async def worktree_cleanup(request: Request) -> JSONResponse:
        """Remove ONE worktree on this node, if this node's own policy agrees.

        The controller never removes a remote path -- it asks here, and this
        handler decides using THIS node's config, THIS node's filesystem and
        evidence it gathers itself. A controller's verdict is not accepted as
        authorisation; it is at most a nomination.

        `expected_head`/`expected_branch` are optimistic concurrency: the
        controller says what it believed it was asking about, and a mismatch
        means its view is stale -- refused rather than acted on. That is the
        multi-node analogue of the executor's own EVIDENCE_CHANGED abort.

        POST because it mutates. Application-level refusals return HTTP 200 with
        an error code, this agent's documented convention (node_client._request
        reads any non-200 as a transport failure, so a 4xx here would be
        reported upstream as "the node is unreachable" -- which is a different
        and much worse thing to tell an operator than "the node said no").
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            body = {}
        worktree_path = str(body.get("worktree_path") or "").strip()
        repo_path = str(body.get("repo_path") or "").strip()
        if not worktree_path:
            return JSONResponse({"error": "INVALID_ARGUMENT", "node_id": node_id,
                                 "detail": "worktree_path is required"})

        config = terminal.config.worktree_janitor
        expected_head = body.get("expected_head")
        expected_branch = body.get("expected_branch")

        def _run() -> dict:
            from .lease import ResourceLockStore
            from .worktree_executor import WorktreeExecutor

            # Identity check FIRST, against what this node observes right now.
            if expected_head or expected_branch:
                observed = git_worktree.worktree_status(repo_path or worktree_path, worktree_path)
                if not observed.get("exists"):
                    return {"outcome": "SKIPPED", "error": "WORKTREE_ABSENT",
                            "worktree_path": worktree_path,
                            "detail": "this node has no such worktree"}
                if expected_head and observed.get("head_sha") != expected_head:
                    return {"outcome": "ABORTED", "error": "IDENTITY_MISMATCH",
                            "worktree_path": worktree_path,
                            "detail": "head moved since the controller looked",
                            "expected_head": expected_head, "observed_head": observed.get("head_sha")}
                if expected_branch and observed.get("branch") != expected_branch:
                    return {"outcome": "ABORTED", "error": "IDENTITY_MISMATCH",
                            "worktree_path": worktree_path,
                            "detail": "branch changed since the controller looked",
                            "expected_branch": expected_branch,
                            "observed_branch": observed.get("branch")}

            executor = WorktreeExecutor(
                config.to_policy(), audit=terminal.audit,
                locks=ResourceLockStore(terminal.leases.path),
                node_id=node_id)
            # THIS node's own liveness probes. Collected here rather than
            # accepted from the request: the controller cannot see this
            # filesystem, so anything it sent would be a guess about someone
            # else's machine. Without them every candidate classifies UNKNOWN
            # and the endpoint is silently inert.
            probes = worktree_janitor.collect_local_probes(
                getattr(terminal, "session_registry", None))
            result = executor.execute(
                {"worktree_path": worktree_path, "node_id": node_id},
                task=body.get("task") if isinstance(body.get("task"), dict) else None,
                repo_path=repo_path or None,
                dry_run=bool(body.get("dry_run", True)), **probes)
            return result.to_dict()

        payload = await anyio.to_thread.run_sync(_run)
        payload["node_id"] = node_id
        return JSONResponse(payload)

    async def environment(request: Request) -> JSONResponse:
        """This node's audit against deploy/node-profile.yaml.

        Lets a controller ask the whole fleet "who is missing what?" in one
        pass instead of someone logging into each machine and finding out at
        failover time. Carries statuses and remediation commands only --
        never credential contents, by construction (see node_profile).
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        roles = tuple(filter(None, (request.query_params.get("roles") or "node").split(",")))
        try:
            result = await anyio.to_thread.run_sync(lambda: node_profile.inventory(roles))
        except node_profile.ProfileError as exc:
            return JSONResponse({"error": "PROFILE_UNAVAILABLE", "detail": str(exc)}, status_code=503)
        return JSONResponse({"node_id": node_id, **result})

    async def session_permissions(request: Request) -> JSONResponse:
        """Effective + requested permissions for one session on THIS node.

        The node that owns the session is authoritative for its grants; a
        controller asks here rather than keeping a second copy that can drift.
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(
            lambda: client.describe_permissions(request.path_params["name"]))
        return JSONResponse(result, status_code=400 if "error" in result else 200)

    async def set_session_permissions(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.set_permissions(
            request.path_params["name"], read=body.get("read"), input=body.get("input"),
            expected_revision=body.get("expected_revision"), actor=body.get("actor")))
        status = 200
        if "error" in result:
            status = 409 if result["error"] == "REVISION_CONFLICT" else 400
        return JSONResponse(result, status_code=status)

    async def refresh_capabilities(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        commands = terminal.config.session_lifecycle.launch_commands
        return JSONResponse({
            "agent_types": list(available_agent_types(commands)),
            "launcher_paths": {agent: resolve_launcher(command) for agent, command in commands},
            "agent_version": __version__,
        })

    async def list_sessions(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(client.list_sessions)
        return JSONResponse(result)

    async def session_status(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        result = await anyio.to_thread.run_sync(lambda: client.status(name))
        return JSONResponse(result)

    async def session_tail(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        lines_raw = request.query_params.get("lines")
        lines = int(lines_raw) if lines_raw not in (None, "", "None") else None
        ansi = request.query_params.get("ansi") == "1"
        result = await anyio.to_thread.run_sync(lambda: client.tail(name, lines, ansi=ansi))
        return JSONResponse(result)

    async def session_capture(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        start_raw = request.query_params.get("start_line")
        start_line = int(start_raw) if start_raw not in (None, "", "None") else None
        result = await anyio.to_thread.run_sync(lambda: client.capture(name, start_line))
        return JSONResponse(result)

    async def session_send(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.send_text(
            name, body.get("text", ""), bool(body.get("press_enter", False)), bool(body.get("dry_run", False)),
            idempotency_key=body.get("idempotency_key"), origin=body.get("origin"),
            trace_id=body.get("trace_id"), parent_turn_id=body.get("parent_turn_id"),
            depth=int(body.get("depth") or 0),
        ))
        return JSONResponse(result)

    async def session_send_keys(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        name = request.path_params["name"]
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(
            lambda: client.send_keys(name, list(body.get("keys", [])), bool(body.get("confirm_sensitive", False)))
        )
        return JSONResponse(result)

    async def input_context(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        session = request.query_params.get("session")
        binding = request.query_params.get("binding")
        result = await anyio.to_thread.run_sync(lambda: client.input_context(session, binding))
        return JSONResponse(result)

    async def create_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.create_session(
            body.get("name", ""), body.get("agent_type", "shell"), body.get("cwd"),
            initial_prompt=body.get("initial_prompt"), grant_mode=body.get("grant_mode", "none"),
            binding=body.get("binding"), requested_by=body.get("requested_by"),
            show_on_desktop=bool(body.get("show_on_desktop", False)),
            resume_session_id=body.get("resume_session_id"),
        ))
        return JSONResponse(result)

    async def detach_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(lambda: client.detach_session(request.path_params["name"]))
        return JSONResponse(result)

    async def put_file(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "INVALID_JSON"}, status_code=400)
        result = await anyio.to_thread.run_sync(lambda: client.put_file(
            body.get("path") or "", body.get("content_b64") or "",
            overwrite=bool(body.get("overwrite")), mode=body.get("mode"),
            requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def delete_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.delete_session(
            request.path_params["name"], confirm=body.get("confirm") is True,
            requested_by=body.get("requested_by")))
        return JSONResponse(result)

    async def kill_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.kill_session(
            request.path_params["name"], body.get("confirm_name", ""), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def rename_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.rename_session(
            request.path_params["name"], body.get("new_name", ""), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def reopen_session(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.reopen_session(
            request.path_params["name"], agent_type=body.get("agent_type"), cwd=body.get("cwd"),
            grant_mode=body.get("grant_mode", "none"), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def registry_reopen_session(request: Request) -> JSONResponse:
        # Phase 0 node-agent restart-safety audit (2026-09-06): the
        # Persistent Session Registry's own honest, MISSING/OFFLINE-aware
        # reopen -- see LocalNodeClient.registry_reopen's own docstring
        # for why this is a SEPARATE route from reopen_session above, not
        # a replacement for it. This is the recovery path a node-agent
        # restart actually needs (never Killed, so reopen_session's own
        # killed_sessions.py-backed metadata has nothing for it).
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.registry_reopen(
            request.path_params["name"], agent_type=body.get("agent_type"), cwd=body.get("cwd"),
            grant_mode=body.get("grant_mode", "none"), requested_by=body.get("requested_by"),
        ))
        return JSONResponse(result)

    async def registry_list(request: Request) -> JSONResponse:
        # Auto Recovery follow-up (2026-09-07): the fleet-aware read a
        # reconciliation engine running on the controller needs --
        # session_registry.py is per-node-agent-process-local (each node
        # has its OWN session_registry.db), so this is the only way the
        # controller can see a REMOTE node's own registry rows at all.
        if (blocked := require_auth(request)) is not None:
            return blocked
        recoverable_only = request.query_params.get("recoverable_only") == "1"
        result = await anyio.to_thread.run_sync(lambda: client.registry_list(recoverable_only=recoverable_only))
        return JSONResponse(result)

    async def fleet_objects(request: Request) -> JSONResponse:
        """The peer half of fleet metadata sync -- merge what came in, answer
        with what this node has.

        Deliberately symmetric with the caller (see fleet_sync.py): a node
        running this IS a peer, not a passive spoke. It is what makes the
        fleet view survive the controller disappearing -- every node that has
        synced once holds a complete durable copy on its own disk.

        Read-write on METADATA only, and never on a session: nothing reachable
        from here can start, stop, rename or type into anything. Payloads are
        scrubbed on the way in by `merge`, which REFUSES a secret rather than
        stripping it, so a peer cannot push credentials onto this node.
        """
        if (blocked := require_auth(request)) is not None:
            return blocked
        service = fleet_service()
        if service is None:
            return JSONResponse({"error": "FLEET_REGISTRY_UNAVAILABLE",
                                 "objects": [], "merge": {}}, status_code=200)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 -- a malformed body is a client error
            body = {}
        if not isinstance(body, dict):
            body = {}
        source = str(body.get("from") or "peer")
        result = await anyio.to_thread.run_sync(
            lambda: service.sync.handle_exchange(body, source_node=source))
        return JSONResponse(result)

    async def fleet_view(request: Request) -> JSONResponse:
        """This node's own answer to "what does the fleet look like" -- read
        entirely from its local cache, so it keeps answering with the
        controller gone. That is the whole point of the endpoint existing on
        the AGENT rather than only on the controller."""
        if (blocked := require_auth(request)) is not None:
            return blocked
        service = fleet_service()
        if service is None:
            return JSONResponse({"error": "FLEET_REGISTRY_UNAVAILABLE"}, status_code=200)
        return JSONResponse(await anyio.to_thread.run_sync(service.offline_view))

    async def killed_sessions(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(client.list_killed_sessions)
        return JSONResponse(result)

    async def session_grant_read(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.grant_read(
            request.path_params["name"], bool(body.get("enabled", False)), granted_by=body.get("granted_by"),
        ))
        return JSONResponse(result)

    async def session_grant_input(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.grant_input(
            request.path_params["name"], bool(body.get("enabled", False)), granted_by=body.get("granted_by"),
        ))
        return JSONResponse(result)

    async def knowledge_search(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        params = request.query_params
        lines_raw = params.get("limit")
        result = await anyio.to_thread.run_sync(lambda: client.knowledge_search(
            params.get("query", ""), session_name=params.get("session_name"), project=params.get("project"),
            since=params.get("since"), until=params.get("until"),
            limit=int(lines_raw) if lines_raw else 20,
        ))
        return JSONResponse(result)

    async def knowledge_timeline(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        params = request.query_params
        limit_raw = params.get("limit")
        result = await anyio.to_thread.run_sync(lambda: client.knowledge_timeline(
            request.path_params["name"], since=params.get("since"), until=params.get("until"),
            limit=int(limit_raw) if limit_raw else 200,
        ))
        return JSONResponse(result)

    async def knowledge_recover(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        result = await anyio.to_thread.run_sync(lambda: client.knowledge_recover(request.path_params["name"]))
        return JSONResponse(result)

    async def knowledge_checkpoint(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        result = await anyio.to_thread.run_sync(lambda: client.knowledge_checkpoint(
            request.path_params["name"], body.get("summary", ""),
        ))
        return JSONResponse(result)

    async def watchdog_events(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        params = request.query_params
        limit_raw = params.get("limit")
        result = await anyio.to_thread.run_sync(lambda: client.watchdog_session_events(
            unacknowledged_only=params.get("unacknowledged_only") == "1",
            limit=int(limit_raw) if limit_raw else 50,
        ))
        return JSONResponse(result)

    async def watchdog_acknowledge(request: Request) -> JSONResponse:
        if (blocked := require_auth(request)) is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        event_id = int(request.path_params["event_id"])
        result = await anyio.to_thread.run_sync(
            lambda: client.watchdog_acknowledge_session_event(event_id, by=body.get("by")))
        return JSONResponse(result)

    async def terminal_ws(websocket: WebSocket) -> None:
        # Open Terminal for a REMOTE node (task's own "Open Terminal trên
        # Windows phải mở được web terminal vào persistent session"),
        # generalized for any remote node (Linux or Windows) -- the
        # dashboard's own /dashboard/ws/terminal route (dashboard.py)
        # proxies here for a non-local session, see that route's own
        # comment. Auth: bearer token, same shared secret as every other
        # route here -- a browser can't set a WS handshake header, but
        # the only caller of THIS route is the controller's own dashboard
        # proxy (a Python client, which can), never a browser directly.
        # A query-param fallback exists for robustness (some WS client
        # libraries make custom headers awkward), same trust boundary
        # either way (this whole surface is bearer-token-only, no
        # separate whitelist/permission re-check -- see this file's own
        # module docstring for that established trust model).
        header = websocket.headers.get("authorization", "")
        presented = header[len("Bearer "):] if header.startswith("Bearer ") else websocket.query_params.get("token", "")
        # The websocket carries the same shared secret and is reachable by the
        # same attacker, so it is throttled by the same counter. Leaving it out
        # would have left an unthrottled door beside a locked one -- and this
        # one also accepts the token in a query parameter.
        ws_key = client_key(
            websocket.client.host if websocket.client else None,
            forwarded_for=websocket.headers.get("x-forwarded-for"),
            trust_forwarded=trust_forwarded_for)
        if not throttle.check(ws_key).allowed:
            await websocket.close(code=4429)
            return
        # Same reason as require_auth: credential.accepts(), not a direct
        # compare_digest against `token`, so a peer presenting the freshly
        # rotated secret is accepted and has its lockout cleared.
        if not credential.accepts(presented):
            throttle.record_failure(ws_key)
            await websocket.close(code=4401)
            return
        throttle.record_success(ws_key)
        session = websocket.query_params.get("session", "")
        readonly = websocket.query_params.get("readonly") == "1"
        if not session:
            await websocket.close(code=4400)
            return
        exists = await anyio.to_thread.run_sync(lambda: terminal.tmux.get_session(session) is not None)
        if not exists:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        # Backend-aware: a tmux-backed node reuses webterm.py's own
        # WebTerminalProcess (`tmux attach-session`) unchanged, exactly
        # like dashboard.py's LOCAL route already does; a Windows node
        # (no `.binary` attribute -- WindowsSessionBackend has no tmux
        # binary at all) gets a WindowsTerminalViewer instead. Both
        # implement the identical read/write/resize/alive/close shape,
        # so pump_websocket (webterm.py) itself is used unmodified either
        # way -- never a second, backend-specific pump implementation.
        tmux_binary = getattr(terminal.tmux, "binary", None)
        if tmux_binary is not None:
            proc = await anyio.to_thread.run_sync(
                lambda: WebTerminalProcess(tmux_binary, session, readonly=readonly, takeover=False)
            )
        else:
            from .windows_webterm import WindowsTerminalViewer
            proc = await anyio.to_thread.run_sync(
                lambda: WindowsTerminalViewer(terminal.tmux, session, readonly=readonly)
            )
        try:
            await websocket.send_json({"type": "ready", "session": session, "readonly": readonly})
            await pump_websocket(websocket, proc)
        finally:
            await anyio.to_thread.run_sync(proc.close)

    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/execution-health", execution_health, methods=["GET"]),
        Route("/v1/metrics", metrics, methods=["GET"]),
        Route("/v1/environment", environment, methods=["GET"]),
        Route("/v1/repo-evidence", repo_evidence, methods=["GET"]),
        Route("/v1/repo/{op}", repo_op, methods=["GET"]),
        Route("/v1/worktree/candidates", worktree_candidates, methods=["GET"]),
        Route("/v1/worktree/cleanup", worktree_cleanup, methods=["POST"]),
        Route("/v1/capabilities/refresh", refresh_capabilities, methods=["POST"]),
        Route("/v1/sessions", list_sessions, methods=["GET"]),
        Route("/v1/sessions", create_session, methods=["POST"]),
        Route("/v1/sessions/{name}/status", session_status, methods=["GET"]),
        Route("/v1/sessions/{name}/tail", session_tail, methods=["GET"]),
        Route("/v1/sessions/{name}/capture", session_capture, methods=["GET"]),
        Route("/v1/sessions/{name}/send", session_send, methods=["POST"]),
        Route("/v1/sessions/{name}/send-keys", session_send_keys, methods=["POST"]),
        Route("/v1/input-context", input_context, methods=["GET"]),
        Route("/v1/sessions/{name}/detach", detach_session, methods=["POST"]),
        Route("/v1/sessions/{name}", delete_session, methods=["DELETE"]),
        Route("/v1/files", put_file, methods=["POST"]),
        Route("/v1/sessions/{name}/kill", kill_session, methods=["POST"]),
        Route("/v1/sessions/{name}/rename", rename_session, methods=["POST"]),
        Route("/v1/sessions/{name}/reopen", reopen_session, methods=["POST"]),
        Route("/v1/sessions/{name}/registry-reopen", registry_reopen_session, methods=["POST"]),
        Route("/v1/registry", registry_list, methods=["GET"]),
        Route("/v1/sessions/{name}/grant-read", session_grant_read, methods=["POST"]),
        Route("/v1/sessions/{name}/grant-input", session_grant_input, methods=["POST"]),
        Route("/v1/sessions/{name}/permissions", session_permissions, methods=["GET"]),
        Route("/v1/sessions/{name}/permissions", set_session_permissions, methods=["POST"]),
        Route("/v1/killed-sessions", killed_sessions, methods=["GET"]),
        # Fleet metadata: an exchange (POST) and this node's own cached view
        # (GET). Metadata only -- no session control reachable from either.
        Route("/v1/fleet/objects", fleet_objects, methods=["POST"]),
        Route("/v1/fleet/view", fleet_view, methods=["GET"]),
        Route("/v1/knowledge/search", knowledge_search, methods=["GET"]),
        Route("/v1/knowledge/timeline/{name}", knowledge_timeline, methods=["GET"]),
        Route("/v1/knowledge/recover/{name}", knowledge_recover, methods=["GET"]),
        Route("/v1/knowledge/checkpoint/{name}", knowledge_checkpoint, methods=["POST"]),
        Route("/v1/watchdog/events", watchdog_events, methods=["GET"]),
        Route("/v1/watchdog/acknowledge/{event_id}", watchdog_acknowledge, methods=["POST"]),
        Route("/v1/internal/shutdown", internal_shutdown, methods=["POST"]),
        WebSocketRoute("/v1/ws/terminal", terminal_ws, name="node_agent_terminal_ws"),
    ]
    app = Starlette(routes=routes)
    # threading.Event, not anyio.Event -- safe to touch from a plain sync
    # context too (no event-loop affinity), and the only thing ever done
    # with it is a fast, non-blocking .set()/.is_set() from Starlette's
    # async request handler above and the polling watcher below.
    app.state.shutdown_event = threading.Event()
    return app


async def watch_for_shutdown(app: Starlette, server: "uvicorn.Server", *,
                             poll_interval_seconds: float = 0.2) -> None:
    """Shared by node_agent.py's and windows_agent.py's own `main()` --
    run as a sibling task alongside `server.serve()`; translates a
    graceful-shutdown request (internal_shutdown route above) into
    uvicorn's own supported `should_exit` flag, which `server.serve()`
    itself already polls to unwind cleanly. See internal_shutdown's own
    docstring for the real bug this whole mechanism replaces."""
    while not app.state.shutdown_event.is_set():
        await anyio.sleep(poll_interval_seconds)
    server.should_exit = True


def _collect_rotated_token(*, controller_url: str, node_id: str, credential: AgentCredential,
                           hint: dict, timeout: float = 10.0) -> bool:
    """Fetch the replacement token the controller says is waiting.

    Authenticated with the token this agent is ALREADY holding -- that is
    the whole authorization, and it is why this can be a plain HTTP call
    rather than a second enrollment: only the holder of the current (or
    still-in-grace) credential can collect its successor.

    Nothing here is logged but fingerprints. `hint["token_id"]` is
    checked against what actually arrives, so a truncated or mismatched
    response is discarded rather than adopted -- adopting the wrong
    string would lock this agent out until someone drove to the machine.
    """
    path = str(hint.get("collect_path") or f"/dashboard/api/nodes/{node_id}/token/refresh")
    url = f"{controller_url.rstrip('/')}{path}"
    request = urllib.request.Request(url, data=b"{}", method="POST")
    request.add_header("Authorization", f"Bearer {credential.current}")
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode() or "{}")
    fresh = str(payload.get("token") or "")
    if not fresh:
        _log.warning("controller offered a token rotation but returned no token (token_id=%s)",
                     hint.get("token_id"))
        return False
    expected = hint.get("token_id")
    actual = hashlib.sha256(fresh.encode("utf-8")).hexdigest()[:12]
    if expected and actual != expected:
        _log.warning("refusing a rotated token: controller announced token_id=%s but delivered %s",
                     expected, actual)
        return False
    adopted = credential.adopt(fresh)
    if adopted:
        _log.info("adopted rotated node token token_id=%s (no restart)", actual)
    return adopted


async def _heartbeat_loop(*, node_id: str, terminal: TerminalService, controller_url: str,
                          token: "str | AgentCredential",
                          workspace_root: str, interval_seconds: float, platform: str = "linux",
                          session_backend: str = "tmux", shell_capabilities: tuple[str, ...] = (),
                          wsl_available: bool = False) -> None:
    """Runs forever (until the process exits) -- a single failed push is
    logged and retried next cycle, never raised past this loop, so a
    controller outage can never crash the node agent (task item 2's own
    "controller mất kết nối, node vẫn sống" guarantee applies to THIS
    process's own survival too, not only to tmux underneath it).
    `platform`/`session_backend`/`shell_capabilities`/`wsl_available`
    (multi-node Windows support): shared with windows_agent.py's own
    main(), which calls this SAME function with those set to the real,
    Windows-appropriate values -- not a separate, duplicated heartbeat
    loop implementation per platform."""
    url = f"{controller_url.rstrip('/')}/dashboard/api/nodes/{node_id}/heartbeat"
    credential = AgentCredential.of(token)
    while True:
        try:
            metrics = host_metrics.collect(workspace_path=workspace_root)
            sessions = await anyio.to_thread.run_sync(terminal.terminal_list_sessions)
            session_rows = sessions.get("sessions", [])
            agent_counts: dict[str, int] = {}
            for row in session_rows:
                command = (row.get("pane_current_command") or "").casefold()
                if command:
                    agent_counts[command] = agent_counts.get(command, 0) + 1
            agent_types = available_agent_types(terminal.config.session_lifecycle.launch_commands)
            body = json.dumps({
                "metrics": metrics.__dict__, "tmux_session_count": len(session_rows),
                "agent_counts": agent_counts, "agent_types": list(agent_types),
                "agent_version": __version__, "agent_generation": AGENT_GENERATION, "labels": [],
                # Protocol generation + feature flags, so the controller can
                # degrade LOUDLY against an older node instead of assuming.
                **contract_describe(),
                # P0.3: probed tool/runtime capabilities. Cached with a TTL
                # inside probe_capabilities, so a 20s heartbeat does not
                # re-walk PATH every cycle.
                "capabilities": list(probe_capabilities()),
                "platform": platform, "session_backend": session_backend,
                "shell_capabilities": list(shell_capabilities), "wsl_available": wsl_available,
            }).encode()
            request = urllib.request.Request(url, data=body, method="POST")
            request.add_header("Authorization", f"Bearer {credential.current}")
            request.add_header("Content-Type", "application/json")

            def _push() -> dict:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read().decode() or "{}")

            answer = await anyio.to_thread.run_sync(_push)
            # The controller answers a heartbeat with a rotation hint when
            # it has a replacement token staged for this node. Collecting
            # it here -- on the node's own schedule, over the channel it
            # has already authenticated -- is what makes rotation cost
            # zero restarts and zero hand-edited files.
            hint = (answer or {}).get("token_refresh") or {}
            if hint.get("available") and hint.get("token_id") != credential.token_id:
                await anyio.to_thread.run_sync(
                    lambda: _collect_rotated_token(controller_url=controller_url, node_id=node_id,
                                                   credential=credential, hint=hint))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.warning("heartbeat push to controller failed (will retry): %s: %s", type(exc).__name__, exc)
        except Exception:  # noqa: BLE001 -- this loop must never die
            _log.exception("unexpected error in heartbeat loop -- will retry next cycle")
        await anyio.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="terminal-node-agent")
    parser.add_argument("--node-id", required=True, help="This node's own id, as registered on the controller")
    parser.add_argument("--controller-url", required=True, help="e.g. http://192.168.1.10:8766")
    parser.add_argument("--config", default=None, help="Path to this node's own config.yaml")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address -- LAN/private only, never 0.0.0.0 "
                        "without a firewall/VPN boundary in front of it (task item 2)")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default=None)
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--heartbeat-interval-seconds", type=float, default=20.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = _read_token(args.token, args.token_file)
    # ONE credential object shared by the HTTP app and the heartbeat loop:
    # a token the loop adopts is instantly the token every route accepts.
    credential = AgentCredential(token, token_file=args.token_file)
    config = load_config(args.config)
    terminal = TerminalService(config)
    # Retired session-name whitelist -> real grants, same as the controller
    # does at its own startup. Every node type runs this so a fleet cannot end
    # up with one machine still honouring a whitelist the others dropped.
    try:
        _migration = terminal.migrate_whitelist_to_grants()
        if _migration.get("read_granted") or _migration.get("input_granted") or _migration.get("errors"):
            _log.info("session-access migration: read=%s input=%s errors=%s",
                      _migration.get("read_granted"), _migration.get("input_granted"), _migration.get("errors"))
    except Exception:  # noqa: BLE001 -- never block startup on a migration
        _log.exception("session-access migration failed -- grants left unchanged")
    # Default-open model: clear deny rows the SYSTEM wrote as bookkeeping so
    # they stop reading as a security decision. A deny an actual person
    # authored is preserved.
    try:
        _deny_migration = terminal.migrate_deny_records_to_default_open()
        if _deny_migration.get("cleared") or _deny_migration.get("errors"):
            _log.info("session-access migration: cleared system deny rows=%s preserved=%s errors=%s",
                      _deny_migration.get("cleared"), _deny_migration.get("preserved_user_denies"),
                      _deny_migration.get("errors"))
    except Exception:  # noqa: BLE001 -- never block startup on a migration
        _log.exception("deny-record migration failed -- grants left unchanged")
    app = build_node_agent(node_id=args.node_id, terminal=terminal, token=credential,
                           workspace_root=(config.session_lifecycle.allowed_cwd_roots[0]
                                          if config.session_lifecycle.allowed_cwd_roots else "/"))

    workspace_root = (config.session_lifecycle.allowed_cwd_roots[0]
                      if config.session_lifecycle.allowed_cwd_roots else "/")

    async def _heartbeat_task() -> None:
        # start_soon only supports positional args -- this closure is
        # just that adapter, keeping _heartbeat_loop's own signature
        # keyword-only (clearer at every OTHER call site, e.g. tests).
        await _heartbeat_loop(node_id=args.node_id, terminal=terminal, controller_url=args.controller_url,
                              token=credential, workspace_root=workspace_root,
                              interval_seconds=args.heartbeat_interval_seconds)

    async def run() -> None:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_heartbeat_task)
            server_config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
            server = uvicorn.Server(server_config)

            async def _shutdown_watch() -> None:
                await watch_for_shutdown(app, server)

            tg.start_soon(_shutdown_watch)
            await server.serve()
            # server.serve() returning (external signal OR the graceful
            # /v1/internal/shutdown path above) must actually end this
            # process -- without this, the task group's own __aexit__
            # would wait forever on _heartbeat_task (an intentional,
            # never-returns-on-its-own `while True` loop), silently
            # hanging the process past what looks like a clean shutdown.
            tg.cancel_scope.cancel()

    anyio.run(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
