"""Node-aware routing for the read-only repo tools: read the repository
WHERE IT ACTUALLY LIVES.

This is the same lesson `coordinator.node_aware_repo_evidence` already
had to learn the hard way, applied to content instead of metadata. A repo
at `C:\\Users\\tranv\\project` or `/home/dell/workspace/x` does not exist
on the controller's filesystem; running git against that path locally does
not produce a wrong answer about the repo, it produces an answer about a
path that means nothing here. So a request is LOCATED first and executed
second, and a request that cannot be located is refused rather than run
against whatever the controller happens to have at that path.

Four ways to say which repo is meant, in the order they are resolved:

  `node` + `path`  -- explicit, unambiguous, no discovery at all.
  `session`        -- "the repo the session I am watching is working in".
                      Resolved through the controller's own session->node
                      resolution and that node's own registry record, so
                      this works for a remote session whose cwd the
                      controller cannot stat.
  `project`        -- a project_id/name from project_identity. Resolved
                      through the checkouts the fleet already reports.
                      Two checkouts on different nodes is AMBIGUOUS_REPO
                      with the candidates listed, never a guess.
  `path` alone     -- local node.

Every result carries `node_id` and `located_by`, so a caller can always
tell WHICH machine answered and HOW that machine was chosen -- a reply
that silently came from the wrong host is the exact failure this module
exists to prevent.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import repo_read
from .repo_read import RepoReadPolicy

_LOGGER = logging.getLogger(__name__)

AMBIGUOUS_REPO = "AMBIGUOUS_REPO"
REPO_NOT_LOCATED = "REPO_NOT_LOCATED"
NODE_NOT_FOUND = "NODE_NOT_FOUND"
NODE_UNREACHABLE = repo_read.NODE_UNREACHABLE
NODE_LACKS_REPO_READ = "NODE_LACKS_REPO_READ"
SESSION_NOT_IN_A_REPO = "SESSION_NOT_IN_A_REPO"


class RepoService:
    """The one object the MCP tools call. Holds the policy (for local
    execution) and the controller (for locating and reaching nodes);
    without a controller it degrades to local-only, which is exactly what
    a single-node deployment and most tests want."""

    def __init__(self, policy: RepoReadPolicy, controller: Any = None, *,
                 local_node_id: str = "local", audit: Any = None) -> None:
        self.policy = policy
        self.controller = controller
        self.local_node_id = local_node_id
        self.audit = audit

    # -- locating ---------------------------------------------------------

    def _local_client(self) -> Any:
        if self.controller is not None:
            client = self.controller.client_for(self.local_node_id)
            if client is not None:
                return client
        return None

    def locate(self, *, path: str | None = None, session: str | None = None,
               project: str | None = None, node: str | None = None) -> dict[str, Any]:
        """-> {"node_id", "path", "located_by"} or {"error": ...}.

        Never executes anything. Kept separate from `run` so the choice of
        machine is independently testable, and so a caller can ask "where
        would this go?" without reading a byte."""
        if node and path:
            if self.controller is not None and self.controller.client_for(node) is None:
                return {"error": NODE_NOT_FOUND, "node_id": node}
            return {"node_id": node, "path": path, "located_by": "node+path"}
        if session:
            return self._locate_by_session(session, fallback_path=path)
        if project:
            return self._locate_by_project(project, node=node)
        if path:
            return {"node_id": self.local_node_id, "path": path, "located_by": "path"}
        return {"error": repo_read.INVALID_ARGUMENT,
                "detail": "give one of: path, session, project, or node+path"}

    def _locate_by_session(self, session: str, *, fallback_path: str | None = None) -> dict[str, Any]:
        if self.controller is None:
            if fallback_path:
                return {"node_id": self.local_node_id, "path": fallback_path,
                        "located_by": "path (no controller configured)"}
            return {"error": REPO_NOT_LOCATED, "session": session,
                    "detail": "no controller is configured, so a session cannot be located"}
        resolution = self.controller.resolve_session(session)
        if "error" in resolution:
            return resolution
        node_id = resolution["node_id"]
        bare = resolution.get("session", session)
        client = self.controller.client_for(node_id)
        if client is None:
            return {"error": NODE_NOT_FOUND, "node_id": node_id}
        try:
            listing = client.registry_list(recoverable_only=False)
        except Exception as exc:  # noqa: BLE001 -- any transport failure is "cannot look"
            return {"error": NODE_UNREACHABLE, "node_id": node_id, "detail": str(exc)[:300]}
        for row in listing.get("records", []):
            if row.get("session_name") != bare:
                continue
            # repo_root is what a *repo* read wants; cwd is the fallback for
            # a record written before project info was backfilled (repo_read
            # resolves the enclosing repo from any path inside it anyway).
            repo_path = row.get("repo_root") or row.get("cwd")
            if not repo_path:
                return {"error": SESSION_NOT_IN_A_REPO, "node_id": node_id, "session": bare}
            return {"node_id": node_id, "path": repo_path, "located_by": "session",
                    "session": bare}
        if fallback_path:
            return {"node_id": node_id, "path": fallback_path,
                    "located_by": "session node + explicit path", "session": bare}
        return {"error": REPO_NOT_LOCATED, "node_id": node_id, "session": bare,
                "detail": f"node {node_id!r} has no registry record for session {bare!r}"}

    def _locate_by_project(self, project: str, *, node: str | None = None) -> dict[str, Any]:
        if self.controller is None:
            return {"error": REPO_NOT_LOCATED, "project": project,
                    "detail": "no controller is configured, so a project cannot be located"}
        discovered = self.controller.discover_projects()
        wanted = project.strip()
        candidates: list[dict[str, Any]] = []
        for entry in discovered.get("projects", []):
            if wanted not in (entry.get("project_id"), entry.get("name")):
                continue
            for node_id in entry.get("nodes", []):
                if node and node_id != node:
                    continue
                for checkout in entry.get("checkouts", []):
                    candidates.append({"node_id": node_id, "path": checkout})
        if not candidates:
            return {"error": REPO_NOT_LOCATED, "project": project,
                    "known_projects": [entry.get("project_id")
                                       for entry in discovered.get("projects", [])][:25],
                    "detail": "no checkout of this project is reported by any online node"}
        if len(candidates) > 1:
            # A project with checkouts on two machines is a real, routine
            # state in this fleet (7 checkouts of this very repo across 3
            # nodes). Picking one would mean silently answering about the
            # wrong machine's working tree.
            return {"error": AMBIGUOUS_REPO, "project": project, "candidates": candidates[:25],
                    "detail": "more than one checkout matches -- pass node and path to choose one"}
        chosen = candidates[0]
        return {"node_id": chosen["node_id"], "path": chosen["path"],
                "located_by": "project", "project": wanted}

    # -- executing --------------------------------------------------------

    def run(self, op: str, *, path: str | None = None, session: str | None = None,
            project: str | None = None, node: str | None = None,
            params: dict[str, Any] | None = None, actor: str | None = None) -> dict[str, Any]:
        """Locate, then execute one named read operation there.

        A local target runs the engine in-process; a remote one goes over
        the node client's `repo_op`. Both paths end in the SAME
        repo_read.run_operation (see LocalNodeClient/the node endpoint), so
        the policy that applies is always the policy of the host that owns
        the files -- which is the correct answer: a node's own config is
        what says which of ITS paths may be read."""
        if op not in repo_read.OPERATIONS:
            return {"error": repo_read.INVALID_ARGUMENT, "op": op,
                    "detail": f"unknown repo operation; known: "
                              f"{', '.join(repo_read.OPERATION_NAMES)}"}
        located = self.locate(path=path, session=session, project=project, node=node)
        if "error" in located:
            self._audit(op, located, actor=actor, result="DENIED",
                        reason=str(located.get("error")))
            return located
        node_id, target = located["node_id"], located["path"]
        started = time.monotonic()
        if node_id == self.local_node_id:
            result = repo_read.run_operation(op, target, params or {}, self.policy)
        else:
            result = self._run_remote(node_id, op, target, params or {})
        result.setdefault("op", op)
        result["node_id"] = result.get("node_id") or node_id
        result["located_by"] = located["located_by"]
        latency_ms = (time.monotonic() - started) * 1000.0
        self._audit(op, {"node_id": node_id, "path": target}, actor=actor,
                    result=("DENIED" if result.get("error") else "OK"),
                    reason=result.get("error"), latency_ms=latency_ms)
        return result

    def _run_remote(self, node_id: str, op: str, path: str,
                    params: dict[str, Any]) -> dict[str, Any]:
        if self.controller is None:
            return {"error": NODE_UNREACHABLE, "node_id": node_id,
                    "detail": "no controller is configured to reach another node"}
        client = self.controller.client_for(node_id)
        if client is None:
            return {"error": NODE_NOT_FOUND, "node_id": node_id}
        if not hasattr(client, "repo_op"):
            return {"error": NODE_LACKS_REPO_READ, "node_id": node_id,
                    "detail": f"node {node_id!r} does not expose repo reads "
                              f"(its agent predates the /v1/repo/{{op}} endpoint)"}
        try:
            payload = client.repo_op(op, path, params)
        except Exception as exc:  # noqa: BLE001 -- transport, 404 and timeout alike
            # "Could not look" -- deliberately never conflated with a repo
            # or path refusal, which is a 200 carrying an error code.
            return {"error": NODE_UNREACHABLE, "node_id": node_id, "op": op,
                    "detail": str(exc)[:300]}
        if not isinstance(payload, dict):
            return {"error": NODE_UNREACHABLE, "node_id": node_id, "op": op,
                    "detail": f"node returned no usable payload: {payload!r}"}
        return payload

    # -- audit ------------------------------------------------------------

    def _audit(self, op: str, located: dict[str, Any], *, actor: str | None,
               result: str, reason: str | None = None,
               latency_ms: float | None = None) -> None:
        """Record WHAT was asked for and WHETHER it was allowed -- never
        what came back.

        The row carries the operation, the node, the repo path and the
        outcome. It deliberately does not carry file content, a patch, a
        matched search line or a redaction sample: this log is the thing an
        operator greps freely, and a read-audit that itself stores the
        secret would defeat the entire point of denying the read. `text` is
        left unset for the same reason -- AuditStore would fingerprint and
        preview it."""
        if self.audit is None:
            return
        try:
            self.audit.record(
                action=f"repo_{op}", session=None, result=result,
                reason=(reason or None), source_transport="mcp", actor=actor,
                node_id=located.get("node_id"), latency_ms=latency_ms,
                policy_source="repo_read")
        except Exception:  # noqa: BLE001 -- a read must not fail because logging did
            _LOGGER.warning("repo_read audit write failed for op=%s", op, exc_info=True)

    # -- the ten operations ----------------------------------------------
    # Thin, explicitly-typed wrappers rather than one generic entry point:
    # these are what the MCP tools call, and an MCP tool's signature IS its
    # schema for the client, so each parameter has to be a real named
    # argument somewhere.

    def status(self, **where: Any) -> dict[str, Any]:
        return self.run("status", **where)

    def head(self, **where: Any) -> dict[str, Any]:
        return self.run("head", **where)

    def branches(self, *, limit: int | None = None, **where: Any) -> dict[str, Any]:
        return self.run("branches", params={"limit": limit}, **where)

    def remotes(self, *, check_auth: bool = False, **where: Any) -> dict[str, Any]:
        return self.run("remotes", params={"check_auth": check_auth}, **where)

    def tree(self, *, subpath: str | None = None, depth: int | None = None,
             limit: int | None = None, **where: Any) -> dict[str, Any]:
        return self.run("tree", params={"subpath": subpath, "depth": depth, "limit": limit},
                        **where)

    def read(self, *, file: str | None = None, start_line: int | None = None,
             end_line: int | None = None, max_bytes: int | None = None,
             **where: Any) -> dict[str, Any]:
        return self.run("read", params={"file": file, "start_line": start_line,
                                        "end_line": end_line, "max_bytes": max_bytes}, **where)

    def search(self, *, query: str, paths: list[str] | None = None,
               max_results: int | None = None, regex: bool = False,
               ignore_case: bool = False, include_untracked: bool = True,
               **where: Any) -> dict[str, Any]:
        return self.run("search", params={
            "query": query, "paths": paths, "max_results": max_results, "regex": regex,
            "ignore_case": ignore_case, "include_untracked": include_untracked}, **where)

    def diff(self, *, base: str | None = None, head: str | None = None, staged: bool = False,
             paths: list[str] | None = None, stat_only: bool = False,
             max_bytes: int | None = None, **where: Any) -> dict[str, Any]:
        return self.run("diff", params={"base": base, "head": head, "staged": staged,
                                        "paths": paths, "stat_only": stat_only,
                                        "max_bytes": max_bytes}, **where)

    def log(self, *, limit: int | None = None, file: str | None = None,
            base: str | None = None, head: str | None = None, **where: Any) -> dict[str, Any]:
        return self.run("log", params={"limit": limit, "file": file, "base": base, "head": head},
                        **where)

    def show_commit(self, *, commit: str, file: str | None = None, stat_only: bool = False,
                    max_bytes: int | None = None, **where: Any) -> dict[str, Any]:
        return self.run("show_commit", params={"commit": commit, "file": file,
                                               "stat_only": stat_only, "max_bytes": max_bytes},
                        **where)


def build_repo_service(terminal: Any, controller: Any = None) -> RepoService:
    """Wire a RepoService from the objects mcp_app/server_http already
    hold: the policy from this host's config (falling back to the
    session-lifecycle allowlist), the controller for routing, and the
    TerminalService's OWN AuditStore -- never a second audit database."""
    config = terminal.config
    policy = config.repo_read.to_policy(config.session_lifecycle.allowed_cwd_roots)
    local_node_id = getattr(controller, "local_node_id", "local") if controller else "local"
    return RepoService(policy, controller, local_node_id=local_node_id,
                       audit=getattr(terminal, "audit", None))
