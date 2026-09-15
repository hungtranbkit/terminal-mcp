# hp-linux branch manifest — for the integrator

Status: **UNVERIFIED inventory** (2026-09-15). This is a hand-off record, not a
merge plan. Every classification below is from `git` on hp-linux at the time of
writing; nothing here has been merged, pushed, rebased, or force-updated.

**The headline: 10 of the 11 remaining branches exist only on this laptop.**
`origin` has never seen them. That is the single largest risk on this host — not
a merge-ordering problem, a durability one.

---

## 1. Counts

| | before | after |
|---|---|---|
| Local branches | 12 | **11** |
| Branches merged into `origin/main` | 1 (a stale pointer) | 0 |
| Branches with unique unmerged work | 11 | 11 |
| Branches present on `origin` | 1 (`feat/analysis-gate`) | 1 |
| **Branches existing ONLY on hp-linux** | **11** | **10** |
| Git worktrees | 9 | 9 (one removed, one added) |
| Separate clones (own `.git`) | 4 | 4 |
| Worktrees with uncommitted changes | 0 | 0 |

Deleted: `feat/fleet-aware-registry` — 0 commits and 0 files ahead of
`origin/main`, i.e. a bare pointer at `695b31c`. Its real work
(`2846c2e`, `blg_84f09bbc1798`) lives in the **separate clone**
`terminal-mcp-fleet-registry` on `feat/fleet-aware-registry-blg84f09`; this
repository does not contain that commit at all (`git cat-file -t 2846c2e` →
*not a valid object name*), so the deletion could not have reached it. Removed
with `git branch -d` (safe delete, refuses unmerged) — no force, no history
rewrite.

---

## 2. Classification

No branch besides the deleted pointer is merged or obsolete. Every one below is
**unique** and should be kept.

| Branch | SHA | commits / files | Worktree | Backlog item |
|---|---|---|---|---|
| `fix/supervisor-node-aware-watch` | `ca888bb` | 1 / 6 | `terminal-mcp-supervisor-lane` | `blg_sup_node_watch01` IN_PROGRESS |
| `feat/worker-discovery-dispatch` | `91745f3` | 2 / 7 | `terminal-mcp-scheduler-lane` | `blg_orch_no_workers_declared` |
| `feat/blg-324738a9eaf7-project-coordinator` | `74bb8ce` | 2 / 3 | `terminal-mcp-coord-lane` | `blg_324738a9eaf7` NEEDS_REVIEW |
| `test/live-remote-dispatch-smoke` | `17ecea1` | 3 / 15 | `terminal-mcp-smoke-lane` | `blg_65725709747b` BLOCKED, `blg_178d7b6506b7` NEEDS_REVIEW |
| `feat/fleet-audit-aggregation` | `e58e3c3` | 1 / 13 | `terminal-mcp-audit-lane` | `blg_178d7b6506b7` NEEDS_REVIEW |
| `fix/blg-20dc778df7ac-macos-platform` | `b13a39c` | 1 / 12 | `terminal-mcp-platform-lane` | `blg_20dc778df7ac` IN_PROGRESS |
| `feat/work-efficiency-telemetry` | `502782b` | 4 / 5 | `terminal-mcp-telemetry-lane` | — |
| `docs/measurement-contract` | `1f490a2` | 4 / 15 | `terminal-mcp-analysis-gate` | — |
| `feat/runbook-registry-worker` | `108e1df` | 1 / 8 | `terminal-mcp-runbook-lane` | — |
| `fix/p0-claude-submit-ghost-composer` | `c28aafc` | 1 / 12 | clone `terminal-mcp-p0-submit-fix` | — |
| `feat/analysis-gate` | `0c58831` | 1 / 13 | — | — (**on origin**) |

Work in **separate clones**, outside this repository's refs and therefore
outside any cleanup done here:

| Clone | Branch | Head |
|---|---|---|
| `terminal-mcp-fleet-registry` | `feat/fleet-aware-registry-blg84f09` | `2846c2e` |
| `terminal-mcp-test-isolation` | `fix/test-isolation-kill-reopen` | `81fe0db` (2 commits) |
| `terminal-mcp-p0-submit-fix` | `fix/p0-claude-submit-ghost-composer` | `c28aafc` |

`fix/test-isolation-kill-reopen` is worth the integrator's attention: it
isolates disposable tmux sessions in `test_kill_reopen` / `test_session_lifecycle`
(`blg_8a1389e7caeb`), which is the cause of the three environmental failures the
scheduler lane reported — a real live session named `test-http-secure` on this
host that `conftest` correctly refuses to touch.

---

## 3. Exposure

`origin` = `https://github.com/hungtranbkit/terminal-mcp.git`, `main` at
`695b31c`. A `--dry-run` push of one unexposed branch succeeded
(`[new branch] feat/worker-discovery-dispatch`), so **push credentials are
present and working**. No credential was created and **nothing was pushed** —
publishing ten branches to a shared remote is the owner's call, not a cleanup
lane's.

To expose everything, per branch:

```bash
git push -u origin <branch>      # no force anywhere; all are new refs
```

---

## 4. Phase-1 / Phase-2 integration — verified, one mechanical conflict

`fix/supervisor-node-aware-watch` (`ca888bb`) is the Phase-1 work the scheduler
lane could not find on 2026-09-14. It was local-only and unpushed, which is why
the search across `origin` and all refs came back empty.

The scheduler lane's pinned contract tests were run against Phase-1's own
`supervisor.py` (throwaway detached worktree, since removed): **10 of 11 pass.**

Semantically compatible, confirmed rather than assumed:

- Phase-1's `watch_key(kind, target, node_id=None)` keeps the key string
  byte-identical for local/unknown nodes, so Phase-2's watch rows key the same.
- Phase-1 resolves the node **inside** `watch()` via `_resolve_watch_node`, so
  Phase-2 calling `watch(session=...)` without a `node_id` is correct by
  construction — no duplicate watch for a remote target.
- `upsert_watch` still re-enables an existing row, so Phase-2's create-only rule
  remains necessary and remains sufficient.
- Node-unreachable now keeps a watch ENABLED (`watch_target_unavailable`)
  instead of disabling it — which only strengthens the invariant that neither
  phase re-enables what the other disabled.

**The one conflict is mechanical:** both branches edit
`SupervisorService.watch`. Phase-2 adds `source: str = "manual"`; Phase-1 adds
node resolution. `test_supervisor_watch_accepts_the_source_kwarg` fails against
Phase-1 alone, exactly as designed — it is a merge-order signal, not a defect.
Resolution is to keep both parameters. Merge Phase-1 first, then Phase-2.

---

## 5. Backlog states

Non-terminal states below are **accurate, not stale**: in each case the code is
committed and genuinely awaits review or merge. They are listed so the
integrator can close them at merge time, not because they need correcting.

| Item | State | Branch | Reality |
|---|---|---|---|
| `blg_sup_node_watch01` | IN_PROGRESS | `fix/supervisor-node-aware-watch` | Code committed; awaiting merge |
| `blg_324738a9eaf7` | NEEDS_REVIEW | `feat/blg-324738a9eaf7-project-coordinator` | Correct |
| `blg_178d7b6506b7` | NEEDS_REVIEW | `feat/fleet-audit-aggregation` | Correct |
| `blg_20dc778df7ac` | IN_PROGRESS | `fix/blg-20dc778df7ac-macos-platform` | Code committed; awaiting merge |
| `blg_65725709747b` | BLOCKED | `test/live-remote-dispatch-smoke` | Genuinely blocked (partial pass) |
| `blg_orch_no_workers_declared` | IN_PROGRESS → **NEEDS_REVIEW** | `feat/worker-discovery-dispatch` | Corrected by this lane — the work is finished and nothing is actively progressing it |

Only the last was changed, because it is the only one this lane owns. Editing
another lane's branch to relabel its item would be a guess about work in
progress, and `IN_PROGRESS` on a branch someone may still be extending is not a
false state.

Note for whoever consolidates: `.terminal-mcp/backlog.json` is checked out
**per branch**, so eleven copies have drifted independently. The controller-side
store is the documented source of truth and HP's local `backlog.db` holds 0
rows, so none of these files is authoritative on this host.

---

## 6. Blockers

1. **Ten branches exist only on hp-linux.** A disk failure loses all of it.
   Credentials work; the decision to publish is the owner's.
2. **Phase-1 is unpushed while running in production.** The controller at
   `100.117.214.87` serves `0.13.0` at commit
   `e853ab39b8912a7c71ad0b3343f9ed8918adff4b`, which is on neither `origin` nor
   this host. `ca888bb` is not that commit, so what is actually deployed is
   still unaccounted for — the controller is running code no repository here
   contains.
3. **`SupervisorService.watch` conflicts** between Phase-1 and Phase-2 (§4).
   Mechanical; merge Phase-1 first.
4. `origin/main` has not moved since `695b31c` (2026-09-13) while eleven
   branches accumulated. Nothing is being integrated.
