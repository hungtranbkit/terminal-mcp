"""Worktree Janitor P4 -- node-aware routing. THE CONTROLLER NEVER REMOVES A
REMOTE PATH.

Contract: docs/WORKTREE_JANITOR.md §7. This is the same lesson
`coordinator.node_aware_repo_evidence` learned for metadata and
`repo_service.RepoService` learned for file reads, now applied to deletion --
where getting it wrong is not a wrong answer but a destroyed directory.

A worktree at `C:\\Users\\tranv\\project` or `/home/dell/workspace/x` does not
exist on the controller. If the controller ran its own executor against that
path it would not fail; it would find whatever IT has at that name, classify
that, and delete that. Contract failure mode F4. So this module has exactly one
job: work out which node owns a worktree, and ask THAT node.

Two things follow, and both are enforced structurally rather than by care:

  - This module contains no removal primitive and never calls the executor. A
    test walks its AST to prove it. The only way it can cause a deletion is by
    asking a node client, and for the local node that client runs the local
    executor -- one code path, reached the same way for local and remote.
  - The node decides. What this module sends is a NOMINATION plus the identity
    it believed it was nominating (`expected_head`/`expected_branch`). The node
    re-classifies with its own config and its own filesystem and may refuse. A
    controller verdict is never authorisation.

Distinguishing failures matters here more than usual, because the operator
action differs completely:

  NODE_UNREACHABLE            the node is down / the network failed. Try later.
  NODE_LACKS_WORKTREE_JANITOR the node is UP but its agent predates these
                              routes. Deploy to that node.
  NODE_NOT_FOUND              the controller has no adapter for that node id.

None of them is ever AUTO_SAFE, and none ever falls back to running locally.
"""
from __future__ import annotations

import logging
from typing import Any

from . import worktree_janitor as wj

_LOGGER = logging.getLogger(__name__)

NODE_UNREACHABLE = "NODE_UNREACHABLE"
NODE_LACKS_WORKTREE_JANITOR = "NODE_LACKS_WORKTREE_JANITOR"
NODE_NOT_FOUND = "NODE_NOT_FOUND"
LOCAL_FALLBACK_REFUSED = "LOCAL_FALLBACK_REFUSED"

ROUTING_ERRORS = (NODE_UNREACHABLE, NODE_LACKS_WORKTREE_JANITOR, NODE_NOT_FOUND,
                  LOCAL_FALLBACK_REFUSED)


class WorktreeRouter:
    """Routes classification and cleanup to the node that owns the filesystem.

    `local_node_id` is the one id for which a client is the in-process
    LocalNodeClient. Every other id must resolve to a real adapter or the
    request is refused -- never silently handled here."""

    def __init__(self, controller: Any = None, *, local_node_id: str = "local") -> None:
        self.controller = controller
        self.local_node_id = local_node_id

    # -- resolution --------------------------------------------------------

    def _client(self, node_id: str) -> tuple[Any, dict[str, Any] | None]:
        if self.controller is None:
            if node_id == self.local_node_id:
                return None, {"error": NODE_NOT_FOUND, "node_id": node_id,
                              "detail": "no controller is configured, so not even the "
                                        "local node has an adapter"}
            return None, {"error": NODE_NOT_FOUND, "node_id": node_id,
                          "detail": "no controller is configured to reach another node"}
        try:
            client = self.controller.client_for(node_id)
        except Exception as exc:  # noqa: BLE001 -- any lookup failure is "cannot reach"
            return None, {"error": NODE_UNREACHABLE, "node_id": node_id,
                          "detail": f"adapter lookup failed: {exc}"[:200]}
        if client is None:
            return None, {"error": NODE_NOT_FOUND, "node_id": node_id}
        return client, None

    @staticmethod
    def _supports(client: Any, method: str) -> bool:
        return callable(getattr(client, method, None))

    # -- classification ----------------------------------------------------

    def candidates(self, node_id: str, repo_path: str) -> dict[str, Any]:
        """Classify `repo_path`'s worktrees on `node_id`. Read-only.

        An unreachable or too-old node yields an error payload whose candidates
        list is EMPTY rather than absent -- a caller iterating results must not
        have to distinguish "no candidates" from "could not ask", and getting
        that wrong in the permissive direction would mean an operator seeing a
        clean report for a node nobody managed to contact."""
        client, error = self._client(node_id)
        if error is not None:
            return {**error, "repo_path": repo_path, "candidates": [], "counts": {}}
        if not self._supports(client, "worktree_candidates"):
            return {"error": NODE_LACKS_WORKTREE_JANITOR, "node_id": node_id,
                    "repo_path": repo_path, "candidates": [], "counts": {},
                    "detail": "this node's agent predates /v1/worktree/candidates -- "
                              "deploy to it rather than waiting for it to recover"}
        try:
            payload = client.worktree_candidates(repo_path)
        except Exception as exc:  # noqa: BLE001
            return self._transport_error(node_id, exc, repo_path=repo_path)
        if not isinstance(payload, dict):
            return {"error": NODE_UNREACHABLE, "node_id": node_id, "repo_path": repo_path,
                    "candidates": [], "counts": {},
                    "detail": f"node returned no usable payload: {payload!r}"[:200]}
        payload.setdefault("node_id", node_id)
        payload.setdefault("candidates", [])
        return payload

    def candidates_for_nodes(self, targets: dict[str, str]) -> dict[str, Any]:
        """`{node_id: repo_path}` -> one combined report.

        One node's failure never hides another's results: each is reported under
        its own key, and `unreachable` lists the ones that could not be asked so
        a total is never mistaken for a complete picture."""
        reports: dict[str, Any] = {}
        unreachable: list[dict[str, Any]] = []
        actionable = 0
        for node_id, repo_path in targets.items():
            report = self.candidates(node_id, repo_path)
            reports[node_id] = report
            if report.get("error"):
                unreachable.append({"node_id": node_id, "error": report["error"]})
            actionable += int(report.get("actionable_count") or 0)
        return {"nodes": reports, "unreachable": unreachable,
                "actionable_count": actionable,
                "complete": not unreachable}

    # -- cleanup -----------------------------------------------------------

    def cleanup(self, node_id: str, worktree_path: str, *, repo_path: str | None = None,
                expected_head: str | None = None, expected_branch: str | None = None,
                task: dict[str, Any] | None = None,
                dry_run: bool = True) -> dict[str, Any]:
        """Ask `node_id` to remove one of ITS OWN worktrees.

        `dry_run` defaults to True here as well as in the executor: a routing
        layer that defaulted to acting would undo the executor's own caution for
        every caller that forgot the argument.

        Note what is NOT here: no branch that removes anything when the node
        cannot be reached. An unreachable node means the worktree stays."""
        client, error = self._client(node_id)
        if error is not None:
            return {**error, "worktree_path": worktree_path, "outcome": "SKIPPED"}
        if not self._supports(client, "worktree_cleanup"):
            return {"error": NODE_LACKS_WORKTREE_JANITOR, "node_id": node_id,
                    "worktree_path": worktree_path, "outcome": "SKIPPED",
                    "detail": "this node's agent predates /v1/worktree/cleanup"}
        try:
            payload = client.worktree_cleanup(
                worktree_path, repo_path=repo_path, expected_head=expected_head,
                expected_branch=expected_branch, task=task, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            return self._transport_error(node_id, exc, worktree_path=worktree_path,
                                         outcome="SKIPPED")
        if not isinstance(payload, dict):
            return {"error": NODE_UNREACHABLE, "node_id": node_id, "outcome": "SKIPPED",
                    "worktree_path": worktree_path,
                    "detail": f"node returned no usable payload: {payload!r}"[:200]}
        payload.setdefault("node_id", node_id)
        payload.setdefault("worktree_path", worktree_path)
        return payload

    # -- errors ------------------------------------------------------------

    @staticmethod
    def _transport_error(node_id: str, exc: Exception, **extra: Any) -> dict[str, Any]:
        """A 404 means "this agent is too old"; anything else means "could not
        reach it". Both are refusals; they are different refusals, and telling
        an operator to wait when they should deploy (or vice versa) wastes the
        outage."""
        text = str(exc)
        too_old = "404" in text
        payload = {
            "error": NODE_LACKS_WORKTREE_JANITOR if too_old else NODE_UNREACHABLE,
            "node_id": node_id, "detail": text[:300],
        }
        payload.update(extra)
        if "repo_path" in extra:
            # A classification caller iterates `candidates`; give it an empty
            # list rather than a missing key so a failure cannot be mistaken
            # for "this node is clean".
            payload.setdefault("candidates", [])
            payload.setdefault("counts", {})
        return payload


def classification_for_unreachable(node_id: str, worktree_path: str,
                                   reason: str) -> dict[str, Any]:
    """The verdict a caller must use for a worktree it could not ask about.

    REVIEW, never AUTO_SAFE. "We could not look" is not evidence of safety, and
    this is the one place a tired caller might be tempted to treat silence as
    consent."""
    return {
        "worktree_path": worktree_path, "node_id": node_id,
        "policy_class": wj.REVIEW, "actionable": False,
        "reasons": [reason],
        "evidence": {"routed": False, "reason": reason},
    }
