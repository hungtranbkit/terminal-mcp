# Rolling out `/v1/repo-evidence` to the node agents

## Why

The controller's pre-dispatch gate needs to know the state of the repository a
session is working in. It cannot see another node's filesystem, so before this
endpoint it ran `git` locally against the remote path, got "not a git
repository", and reported a healthy repo as broken. Every remote-node `-work`
session was undispatchable as a result.

The controller side is already live. This document covers the node side, which
is not.

## Current fleet state (measured, not assumed)

| Node | Status | Agent | Contract | `repo_evidence` capability |
|---|---|---|---|---|
| local (m910) | online | — (in-process) | 1 | yes, after this change |
| dell-linux | online | 0.12.0 | 0 | **no** |
| dell-5530 (Windows) | online | 0.12.0 | 0 | **no** |
| hp-linux | online | 0.12.0 | 0 | **no** |
| macbook | **offline** | 0.12.0 | 0 | **no** |

Every remote agent predates the contract handshake entirely (`contract=0`,
no capabilities). None of them can answer this endpoint today.

## What the endpoint returns

Success (HTTP 200):

    repo_valid          true
    cwd                 the resolved path ON THAT NODE
    exists / readable   whether the path is there and this agent can read it
    branch, head        branch name and full HEAD SHA
    dirty / clean       both, so no caller inverts a boolean by hand
    status_lines        porcelain lines; metadata only, never file contents
    has_upstream, ahead, behind
    collected_at        when the evidence was READ (ISO 8601, UTC)
    contract_version, contract_capabilities

Refusals, all deliberately distinguishable:

| Condition | HTTP | `error` | `repo_valid` |
|---|---|---|---|
| no `cwd` given | 400 | `CWD_REQUIRED` | — |
| path outside allowed roots | 403 | `PATH_NOT_ALLOWED` | — |
| path absent on the node | 200 | `PATH_NOT_FOUND` | false |
| present but unreadable | 200 | `PATH_NOT_READABLE` | false |
| present, readable, not a repo | 200 | `REPO_EVIDENCE_FAILED` | false |

"Not here" is not "not allowed": mapping both to 403 sent operators to fix a
permission that was never the problem.

## Fail-closed semantics

The controller distinguishes two failures, and the difference decides who can
override it:

* **UNAVAILABLE — "we could not look."** Old agent, unreachable node, no
  adapter, missing `repo_valid`, missing/unparseable/stale `collected_at`.
  Fails closed, and an operator may waive it per task with
  `allow_unverified_repo`.
* **REPO FAILURE — "we looked and it is bad."** The node answered about its own
  filesystem. Fails closed and is **not** waivable, because there is nothing
  uncertain to waive.

An old agent is identified by its declared capability, not by a 404. A 404 is
also what a misrouted request, a stale proxy or a half-deployed agent returns,
and none of those may be mistaken for "this node genuinely speaks an older
protocol".

Evidence older than `DEFAULT_EVIDENCE_MAX_AGE_SECONDS` (300s) is refused, as is
evidence dated in the future by more than that — a clock skewed that far makes
the age meaningless. A payload with no timestamp is refused rather than assumed
fresh.

## Versioning

`CAP_REPO_EVIDENCE` is **additive**: `CONTRACT_VERSION` stays at 1. Bumping it
would refuse every older peer for a change that does not break them. A node
that reports nothing is recorded as version 0 with no capabilities, never as
"probably compatible".

## Per-node rollout checklist

Run for one node, verify, then the next. Do not batch.

1. Confirm the node is online and note its current agent version.
2. Update that node's checkout to the deployed commit.
3. Restart **only** that node's `terminal-node-agent`. Never the controller,
   never a tmux session.
4. `GET /v1/health` on the node: expect `contract_version: 1` and
   `repo_evidence` in `contract_capabilities`.
5. `GET /v1/repo-evidence?cwd=<a real repo on that node>`: expect
   `repo_valid: true`, a branch, a HEAD SHA and a fresh `collected_at`.
6. From the controller, run one `terminal_queue_run_once` against a `-work`
   lane on that node and confirm the gate reaches `COORDINATOR_READY` instead
   of `RepoEvidenceUnavailable`.
7. Confirm that node's existing sessions are untouched (pane PIDs unchanged).

Order: **dell-linux** first (it has the `-work` lane that motivated this), then
**hp-linux**, then **dell-5530** (Windows, different agent packaging), and
**macbook** last — it is offline and must be brought up before it can be rolled.

Rollback for any node: restore its previous checkout and restart that agent.
The controller needs no change either way — an old agent is already handled as
UNAVAILABLE, which is exactly today's behaviour.

## Not covered here

Restarting node agents requires explicit approval and is deliberately outside
this lane. Nothing in this branch changes the production pin, restarts an
agent, or touches the fleet.
