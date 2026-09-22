"""Which node actually has the repository. Asked, never assumed.

THE ASSUMPTION THIS REMOVES

The Harness ran its readiness checks against the CONTROLLER's filesystem and
concluded, from `/home/dell/workspace/urbanflow` not existing here, that the
UrbanFlow pilot could not run at all. That was wrong twice over: the repo
exists on the fleet node `dell-linux`, and the controller's own disk was
never the right place to look. A fleet where work runs on four machines
cannot decide "is this startable" by stat()ing one of them.

So eligibility is a QUESTION PUT TO EACH NODE, through the node API that
already exists for exactly this: `repo_evidence(path)` answers `exists`,
`readable` and `repo_valid` for a path on THAT host. No new endpoint, no
agent change, no second queue -- and nothing is copied anywhere. A node that
does not have the repository is rejected by name and the work goes where the
repository already is.

WHY NOT JUST CLONE IT TO THE CONTROLLER

Because the repository is not the only thing that lives on a node. The
toolchain, the installed dependencies, the worktrees, the branch state and
whatever the last run left behind are all there too, and a clone reproduces
exactly one of them. "Copy the repo until the check passes" is how a fleet
acquires two divergent copies of the same project and no way to say which is
real.

WHAT ELIGIBLE MEANS, EXACTLY

All three, on that node: the path EXISTS, it is READABLE, and it is a valid
git repository. Anything less is refused with the reason the node gave, so
"why did this not go to HP" has an answer a person can read rather than a
silent preference for somewhere else.

A node that cannot be reached is UNREACHABLE, which is deliberately not the
same as "does not have it". One is a fact about the repository and the other
is a fact about the network, and treating an unreachable node as ineligible
forever is how a fleet quietly shrinks to whatever was up at boot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

# -- verdicts, as a closed set -----------------------------------------------
HAS_REPO = "has_repo"
PATH_NOT_FOUND = "path_not_found"
NOT_A_REPO = "not_a_repo"
UNREADABLE = "unreadable"
UNREACHABLE = "unreachable"
NO_EVIDENCE_ENDPOINT = "no_evidence_endpoint"

VERDICT_REASONS: tuple[str, ...] = (
    HAS_REPO, PATH_NOT_FOUND, NOT_A_REPO, UNREADABLE, UNREACHABLE,
    NO_EVIDENCE_ENDPOINT,
)

#: How long one node's answer about one path stays usable. Short, because a
#: repository can be cloned or removed while a pilot runs; not zero, because
#: a scheduler asks this once per task and a fleet-wide probe per task would
#: cost more than the scheduling it informs.
DEFAULT_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class NodeVerdict:
    """One node's answer about one repository path."""

    node_id: str
    eligible: bool
    reason: str
    branch: str | None = None
    head: str | None = None
    clean: bool | None = None
    detail: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"node_id": self.node_id, "eligible": self.eligible,
                "reason": self.reason, "branch": self.branch, "head": self.head,
                "clean": self.clean, "detail": self.detail}


def verdict_from_evidence(node_id: str, evidence: Mapping[str, Any]) -> NodeVerdict:
    """Read one node's repo_evidence payload into a verdict.

    Pure, so the rules can be tested without a fleet. The payload shape is
    the node agent's own (`exists`/`readable`/`repo_valid`), and the older
    controller-local shape that returns only `branch`/`head` is accepted too
    -- a node whose agent predates the richer fields still answers usefully,
    and refusing it would reject a node for the age of its agent rather than
    for anything about the repository.
    """
    if not isinstance(evidence, dict):
        return NodeVerdict(node_id, False, NO_EVIDENCE_ENDPOINT,
                           detail="node returned no evidence object")
    error = evidence.get("error")
    if error == "PATH_NOT_FOUND" or evidence.get("exists") is False:
        return NodeVerdict(node_id, False, PATH_NOT_FOUND,
                           detail=str(evidence.get("detail") or "path does not exist"),
                           evidence=evidence)
    if evidence.get("readable") is False:
        return NodeVerdict(node_id, False, UNREADABLE,
                           detail=str(evidence.get("detail") or "path is not readable"),
                           evidence=evidence)
    if evidence.get("repo_valid") is False:
        return NodeVerdict(node_id, False, NOT_A_REPO,
                           detail=str(evidence.get("detail") or "not a git repository"),
                           evidence=evidence)
    if error:
        return NodeVerdict(node_id, False, NOT_A_REPO, detail=str(error),
                           evidence=evidence)
    # An older agent that answers with branch/head and no `repo_valid` has
    # told us it IS a repository, by being able to say which branch it is on.
    if evidence.get("repo_valid") is None and not evidence.get("branch") \
            and not evidence.get("head"):
        return NodeVerdict(node_id, False, NO_EVIDENCE_ENDPOINT,
                           detail="node gave no repository fields", evidence=evidence)
    return NodeVerdict(node_id, True, HAS_REPO,
                       branch=evidence.get("branch"), head=evidence.get("head"),
                       clean=evidence.get("clean"), evidence=evidence)


class RepoAffinity:
    """Finds the nodes that actually hold a repository, and picks one.

    `clients` maps node_id -> something with `.repo_evidence(path)`. That is
    the NodeClient protocol both LocalNodeClient and RemoteNodeClient already
    satisfy, so this needs no adapter and cannot reach a capability the rest
    of the fleet does not have.
    """

    def __init__(self, clients: Mapping[str, Any] | Callable[[str], Any],
                 *, node_ids: Sequence[str] = (),
                 prefer: Sequence[str] = (),
                 ttl_seconds: float = DEFAULT_TTL_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._clients = clients
        self.node_ids = tuple(node_ids) or tuple(
            clients.keys() if isinstance(clients, Mapping) else ())
        #: Tried first when more than one node has the repository. A tie is
        #: otherwise broken by node_id order, so the same fleet and the same
        #: repository always produce the same choice -- a scheduler whose
        #: placement changes between identical calls cannot be reasoned about.
        self.prefer = tuple(prefer)
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, NodeVerdict]] = {}

    def _client_for(self, node_id: str) -> Any:
        if isinstance(self._clients, Mapping):
            return self._clients.get(node_id)
        return self._clients(node_id)

    def probe_node(self, node_id: str, repo_path: str) -> NodeVerdict:
        """Ask one node about one path, through the cache."""
        key = (node_id, repo_path)
        cached = self._cache.get(key)
        now = self._clock()
        if cached is not None and (now - cached[0]) < self.ttl_seconds:
            return cached[1]
        try:
            client = self._client_for(node_id)
        except Exception as exc:  # noqa: BLE001 -- a lookup failure is "cannot ask"
            return NodeVerdict(node_id, False, UNREACHABLE, detail=str(exc)[:200])
        if client is None:
            return NodeVerdict(node_id, False, UNREACHABLE,
                               detail="no client configured for this node")
        if not hasattr(client, "repo_evidence"):
            return NodeVerdict(node_id, False, NO_EVIDENCE_ENDPOINT,
                               detail="node agent predates /v1/repo-evidence")
        try:
            evidence = client.repo_evidence(repo_path)
        except Exception as exc:  # noqa: BLE001 -- the network, not the repo
            # NOT cached: an unreachable node must be retried, or a fleet
            # quietly shrinks to whatever was up when the first probe ran.
            return NodeVerdict(node_id, False, UNREACHABLE, detail=str(exc)[:200])
        verdict = verdict_from_evidence(node_id, evidence)
        self._cache[key] = (now, verdict)
        return verdict

    def probe(self, repo_path: str) -> tuple[NodeVerdict, ...]:
        """Every node's answer, in node_id order. Nothing is hidden.

        Ineligible nodes are RETURNED rather than filtered out, because "why
        did this not go to HP" is the question an operator actually asks, and
        a selector that answers only with its winner cannot be debugged.
        """
        return tuple(sorted((self.probe_node(node_id, repo_path)
                             for node_id in self.node_ids),
                            key=lambda v: v.node_id))

    def select(self, repo_path: str) -> tuple[NodeVerdict | None, tuple[NodeVerdict, ...]]:
        """(chosen, all_verdicts). Deterministic on the same fleet state."""
        verdicts = self.probe(repo_path)
        eligible = [v for v in verdicts if v.eligible]
        if not eligible:
            return None, verdicts
        for wanted in self.prefer:
            for verdict in eligible:
                if verdict.node_id == wanted:
                    return verdict, verdicts
        return eligible[0], verdicts

    def invalidate(self, repo_path: str | None = None) -> None:
        if repo_path is None:
            self._cache.clear()
            return
        for key in [k for k in self._cache if k[1] == repo_path]:
            self._cache.pop(key, None)


class NoEligibleNode(RuntimeError):
    """No node on this fleet holds the repository this run needs.

    Carries every verdict, so the refusal names which nodes were asked and
    what each one said -- the alternative is an operator guessing whether the
    repo is missing, the node is down, or nobody looked.
    """

    def __init__(self, repo_path: str, verdicts: Sequence[NodeVerdict]) -> None:
        self.repo_path = repo_path
        self.verdicts = tuple(verdicts)
        detail = "; ".join(f"{v.node_id}: {v.reason}" for v in self.verdicts) or "no nodes"
        super().__init__(f"no node on this fleet has {repo_path} ({detail})")


def worktree_root_for(repo_path: str) -> str:
    """The worktree root the repository's own convention implies, on its node.

    `<repo>/../.terminal-mcp-worktrees/harness`, the same rule
    git_isolation_service uses here -- computed as a STRING because the path
    belongs to another host and stat()ing it locally would be the exact
    mistake this module exists to remove.
    """
    import posixpath

    parent = posixpath.dirname(repo_path.rstrip("/"))
    return posixpath.join(parent, ".terminal-mcp-worktrees", "harness")
