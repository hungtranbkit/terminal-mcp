# Repository topology — what is canonical, and what exists nowhere else

Audited 2026-09-15. Read-only: nothing was reset, deleted, rebased or pushed.

## The finding that dissolves the question

Lanes reported different "mains" — `/home/mesflow/workspace` around `a3a9b16`,
`/home/mesflow/terminal-mcp` around `8a60235`. **There is no second clone on this
host.** Every Terminal MCP path under `/home/mesflow` resolves to one git common
directory:

    /home/mesflow/terminal-mcp/.git

`terminal-mcp-prod` and `workspace/tmcp-analysis-gate` included — they are linked
worktrees, not clones. `git rev-parse --git-common-dir` says so for all 31 paths.

The differing SHAs were different points on ONE branch, plus one detached worktree:

| Reported | What it actually is | Relation to main |
|---|---|---|
| `a3a9b16` | detached HEAD of `/home/mesflow/wt-mergecheck` | ancestor of main, 30 behind |
| `8a60235` | main's tip earlier the same day | ancestor of main, 1 behind |
| `b2419f1`+ | main's tip in `/home/mesflow/terminal-mcp` | current |
| `695b31c` | `origin/main` | ancestor, 182 behind |

`695b31c → +181 → 8a60235 → +1 → b2419f1`, all strictly linear. No divergence, no
lost commits, and no clone cleanup to do here. `/home/mesflow/workspace` is not a
git repository at all — it is a container directory; a lane reading "workspace"
was reading a worktree inside it.

## Canonical, by evidence rather than by name

**`/home/mesflow/terminal-mcp`** (common dir `/home/mesflow/terminal-mcp/.git`).

- `terminal-mcp-http.service` (the running controller) declares
  `WorkingDirectory=/home/mesflow/terminal-mcp` and runs
  `/home/mesflow/terminal-mcp/.venv/bin/terminal-mcp-http`.
- The staging node runs that same interpreter path.
- Every other Terminal MCP path on this host is a worktree of it.
- State (`backlog.db`, `audit.db`) is in `~/.local/state/terminal-mcp`, shared by
  all worktrees, so no worktree owns it.

`terminal-mcp-prod` is a detached worktree of the same repo, not a separate
deployment clone.

## Not on this host: a real second clone

`dell-linux:/home/dell/workspace/terminal-mcp` is a separate clone with 10 linked
worktrees of its own. Six of its branch tips **do not exist in the canonical object
store, and are not on `origin` either** — they exist in exactly one place on earth:

| Branch (dell only) | Unique commits | Content | Overlap with canonical |
|---|---|---|---|
| `bench/efficiency-benchmark-harness` | 6 | `terminal_mcp/bench/*`, docs | **Same goal** as merged `efficiency_benchmark.py`; different implementation |
| `p0/dell-selfhost-converged` | 8 | 4 already in main under the same SHAs; other 4 are merges | Nothing unique |
| `audit/ai-usage-contract` | 1 | `tests/test_ai_usage_contract_audit.py` + fixture | No canonical equivalent |
| `feat/impl-contract-analysis-gate` | 1 | `terminal_mcp/impl_contract.py` | Adjacent to the uncommitted `analysis_gate.py` work |
| `feat/orch-worker-capability-profiles` | 1 | `capability_profile.py`, router/scheduler edits | Overlaps merged `capability_probe.py` |
| `fix/doctor-effective-lan-bind` | 1 | `doctor.py`, `effective_bind.py` | **Same fix** as merged `fix/doctor-lan-truth` (8f3d2fa) |

Patch-id comparison against all 150 non-merge commits in `695b31c..main` found four
duplicates, all inside `p0/dell-selfhost-converged` and all already present under the
same SHAs. **Ten commits across five branches are genuinely single-copy.**

## Convergence sequence (nothing destructive, in this order)

1. **Preserve before deciding.** From canonical:
   `git remote add dell dell-linux:/home/dell/workspace/terminal-mcp && git fetch dell`
   — this copies the ten at-risk commits into the canonical object store without
   merging anything or changing a branch.
2. **Decide the three duplicate-effort pairs before merging either side**: the
   benchmark harness, the doctor LAN-bind fix, and the capability profiles. Merging
   both implementations of the same problem is how a project ends up with two.
3. **Cherry-pick the clean one**: `audit/ai-usage-contract` has no canonical
   counterpart and touches only tests plus a fixture.
4. **`p0/dell-selfhost-converged` needs nothing** — its content is already in main.
5. Only then consider making dell's clone read-only.

## Push

`PUSH_BLOCKED` on m910: no credential helper, no `~/.git-credentials`, and
`git push --dry-run` answers *"No anonymous write access."* 182 commits and 28 of 29
local branches have no upstream, so the canonical line of work exists only on this
host's disk. No credential was created; this is a decision for the repository owner.

## Worktrees

31 worktrees share the canonical repo. Six branches are unmerged (8 unique commits),
each with a live worktree. The rest are merged and their worktrees are removable once
their session ends — `wt-mergecheck`, `wt-audit`, `wt-integrate` and `wt-sqr` are
integration scratch space, not feature work.
