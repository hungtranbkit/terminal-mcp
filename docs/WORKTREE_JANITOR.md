# Worktree Janitor — design of record

Status: **CONTRACT ONLY. No executor exists. Nothing deletes anything yet.**

This document is the canonical specification for reclaiming isolated task
worktrees. It is the source of truth for the behaviour; `docs/REQUIREMENTS.md`
carries the summary row and points here.

Filed as `docs/WORKTREE_JANITOR.md` rather than `docs/AI_WORKTREE_JANITOR.md`
purely to match the existing convention in this directory — no doc here
carries an `AI_` prefix (`WORK_RUNTIME_V1.md`, `ORCHESTRATION_ARCHITECTURE.md`,
`PROJECT_COORDINATION_ARCHITECTURE.md`). Same document either way.

---

## 1. The problem, measured

`git_isolation_service.create_isolated_task` creates a real worktree + branch
for every isolated coding task. Nothing ever reclaims one. The only removal
path in the entire codebase is `terminal_worktree_cleanup` →
`GitIsolationService.cleanup_worktree_for_task`, which is explicitly manual and
human-invoked, and whose own docstring says it is "never automatic".

Audited on m910, 2026-09-14:

- 9 linked worktrees plus the main checkout, none reclaimed.
- `/dev/sda3` (where worktrees live): 116G, **26% used, 83G free**.
- `audit.db` contains **zero** rows for any worktree/cleanup action — the
  existing manual tool has apparently never been invoked in production, and
  would leave no audit trail if it were.
- `git_worktree.py` and `git_isolation_service.py` contain **zero** audit calls.

So this is **debt hygiene, not an outage**. That framing matters: it is the
reason every default below is conservative and the reason disk pressure is
*not* permitted to relax a single safety rule. The real disk pressure observed
on this host was on tmpfs `/tmp` (a RAM-backed 7.6G filesystem with a per-user
quota), which removing worktrees **cannot relieve at all** — they are on a
different filesystem. See §9.

---

## 2. Source of truth

Each fact has exactly one authoritative source. The janitor never invents a
second one.

| Fact | Source |
|---|---|
| Which worktrees exist | `git worktree list --porcelain` in the owning repo, via `git_worktree.list_worktrees()` — authoritative including stale admin entries whose directory is gone |
| Task ↔ worktree link | `queue_tasks.metadata` JSON → `git_isolation {repo_path, branch, base_sha, worktree_path}` and `expected_cwd`, written by `create_isolated_task` |
| Task state | `queue_tasks.status`, mutated **only** by `queue_store._transition_locked` (called by `transition_task`), which already writes a `queue_events` row in the same transaction |
| Session → path usage | `session_registry.SessionRecord(node_id, session_name, cwd, repo_root, status)` — node-scoped |
| Node reachability / execution | `node_client.NodeClient` (`LocalNodeClient` / `RemoteNodeClient`) + the node-agent HTTP surface |
| Repo state evidence | `coordinator.git_repo_evidence` / `node_aware_repo_evidence` — already fail-closed (`RepoEvidenceError` = real repo problem; `RepoEvidenceUnavailable` = could not look) |
| Audit | `audit.AuditStore.record(action=, result=, reason=, actor=, node_id=, latency_ms=, policy_source=)` |
| Events | `event_bus.EventBus.publish(type=, entity_type=, entity_id=, payload=, idempotency_key=)`; new types must be added to `KNOWN_EVENT_TYPES` |
| Config | `config.AppConfig` + a frozen dataclass section and a `_load_*_config` validator that raises `ValueError` on out-of-range input, never silently clamps |

### The lifecycle chokepoint

`queue_store._transition_locked` is the **only** place a task's status changes.
It is therefore the only correct hook site.

Do **not** hook `QueueService.on_completed` or `QueueEngine.on_completed`:
audited, there are **two** call sites (`queue_service.py` and
`queue_engine.py`), so neither is a chokepoint, and a task completing via the
other path would silently never be marked for cleanup.

---

## 3. When cleanup becomes eligible

**There is no `FAILED_FINAL` status.** This was verified against the code, and
getting it wrong is failure mode F1.

- `queue_store.TERMINAL_STATUSES == (COMPLETED, SKIPPED, CANCELLED)`
- `FAILED` and `BLOCKED` are explicitly **not** terminal — `terminal_queue_retry`
  / `skip` / `cancel` all still apply to them
- `work_store.TERMINAL_WORK_STATES == (COMPLETE, CANCELLED)`

The trigger predicate is therefore exactly:

```
to_status in (COMPLETED, SKIPPED, CANCELLED)
  OR (to_status == FAILED AND attempt_count >= max_attempts)
```

A bare `FAILED` must **never** trigger cleanup: it is retryable, and the
worktree is the retry's working directory.

---

## 4. State model

Recorded on the task's own metadata as `metadata.worktree_cleanup`:

```
{state, classified_at, eligible_at, policy, reasons[], predicates{},
 attempts, last_error, removed_at, reclaimed_bytes, evidence{}}
```

```
        (terminal transition, task has git_isolation)
                        |
                        v
                 CLEANUP_PENDING ──────────── reopen/retry ──> (record cleared)
                        |
                   [classify]
          ┌─────────────┼─────────────┐
          v             v             v
     AUTO_SAFE       REVIEW        BLOCKED
          |             |             |
     [grace elapsed]  human       (terminal until
          |          approve|      re-classified;
          v          abandon        never auto-acted)
   CLEANUP_ELIGIBLE     |
          |             ├─ approve ─> CLEANUP_ELIGIBLE
     [re-classify       └─ abandon ─> CLEANUP_ABANDONED
      with FRESH
      evidence under
      the lock]
          |
      [execute]
          |
          v
    CLEANUP_DONE  (removed_at, reclaimed_bytes)
```

Transitions are idempotent. Re-entering a terminal state never resets an
existing record, never bumps `attempts`, and never revives a `CLEANUP_DONE`.

### Reopen / retry cancels cleanup

If a task leaves a terminal state (retry, reopen, reassignment) while
`worktree_cleanup.state` is `CLEANUP_PENDING`, `CLEANUP_REVIEW` or
`CLEANUP_ELIGIBLE`, the record is **cleared** — the worktree is needed again.

If the state is `CLEANUP_DONE`, the worktree is gone. The Coordinator's
existing `expected_cwd` pre-dispatch gate must refuse dispatch with a clear
reason (`WORKTREE_REMOVED`) rather than silently proceeding on the wrong
directory. **Add that reason; never weaken the gate.**

---

## 5. Classification

Three classes. **Fail-closed: anything that cannot be *evaluated* degrades
toward REVIEW/BLOCKED, never toward AUTO_SAFE.**

- **AUTO_SAFE** — every predicate below proven true, each with recorded
  evidence. The only class that may ever be auto-removed.
- **REVIEW** — safe to keep, needs a human. Anything *unknown* lands here:
  node offline, evidence stale, git error, permission denied, detached HEAD,
  first-sighting orphan.
- **BLOCKED** — actively dangerous to remove. Never auto-acted on.

### The AUTO_SAFE predicate — all nine must hold

1. **Not the main worktree.** Verified primitive: inside the candidate,
   `git rev-parse --git-dir` differs from `--git-common-dir` only in a *linked*
   worktree; they are equal in the main one. Refuse if equal. Also refuse if
   `worktree_path` equals any configured repo root.
2. **Path allowlisted.** Resolves (symlinks followed **before** the containment
   check) inside configured `allowed_roots`; not itself a symlink, mount point
   or bind mount.
3. **Clean.** `git status --porcelain` empty.
4. **Merged or preserved.**
   - merged = `git merge-base --is-ancestor <worktree_head> <integration_ref>`
     (default `main`)
   - preserved = the branch tip exists on a remote **and** `<worktree_head>` is
     an ancestor of that remote ref
   - Pushed-but-not-merged counts as *preserved* (the work is not lost) but is
     classified **REVIEW** by default; promoted to AUTO_SAFE only when
     `allow_preserved_unmerged` is true.
   - **Detached HEAD ⇒ REVIEW, never AUTO_SAFE** — there is no branch identity
     to check merge status against.
5. **No live references**, evaluated **on the owning node**: no process whose
   cwd is inside the path (Linux: `/proc/*/cwd`); no tmux pane whose
   `pane_current_path` is inside it; no `session_registry` record (any
   non-DELETED status) whose `cwd`/`repo_root` is inside it; no systemd unit
   `WorkingDirectory` inside it; no container/bind mount referencing it
   (`/proc/mounts`). Any positive match ⇒ **BLOCKED**. Inability to check ⇒
   **REVIEW**.
6. **No valuable ignored data.** `git status --porcelain --ignored=matching`
   → `!!` entries, each classified:
   - SAFE_CACHE: `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`,
     `node_modules`, `dist`, `build`, `.venv`, `*.pyc`
   - VALUABLE: `*.db`, `*.sqlite*`, `.env*`, `*.pem`, `*.key`, `*credentials*`,
     `node-agent.env`, `evidence/`, anything matching
     `repo_read.SECRET_PATH_GLOBS`, `*.log` above a configured size
   Any VALUABLE entry ⇒ **BLOCKED**, listing the **paths only — never contents**.
7. **Repo evidence fresh** — collected at decision time on the owning node,
   age ≤ `max_evidence_age_seconds`. Stale or unavailable ⇒ REVIEW.
8. **Grace elapsed** — `now >= terminal_transition_at + grace_seconds`.
9. **Lock held** — see §6.

### Reason codes

Machine-readable, stable, asserted in tests:

`DIRTY`, `UNMERGED_UNPUSHED`, `DETACHED_HEAD`, `VALUABLE_IGNORED_DATA`,
`PROCESS_IN_USE`, `TMUX_IN_USE`, `SESSION_IN_USE`, `SERVICE_ROOT`,
`MAIN_WORKTREE`, `PATH_NOT_ALLOWED`, `SYMLINK_OR_MOUNT`, `EVIDENCE_STALE`,
`NODE_UNREACHABLE`, `NODE_LACKS_WORKTREE_JANITOR`, `GRACE_NOT_ELAPSED`,
`ADMIN_ENTRY_STALE`, `PERMISSION_DENIED`, `TASK_NOT_FINAL`, `LOCK_UNAVAILABLE`,
`ORPHAN_UNCONFIRMED`, `EVIDENCE_CHANGED`.

---

## 6. Concurrency, locks, idempotency

- **Lock:** `ResourceLockStore` (the existing store — never a second lock
  table), key `worktree_janitor:<node_id>:<worktree_path>`, held across
  **classify + remove** as one critical section. `LOCK_UNAVAILABLE` ⇒ skip this
  candidate this run; never wait long, never force.
- **Re-classify under the lock.** A candidate that was AUTO_SAFE at scan time
  must be re-classified with **fresh** evidence immediately before removal. If
  it is no longer AUTO_SAFE, abort with `EVIDENCE_CHANGED` and remove nothing.
- **Idempotent execution.** If the directory is already gone but metadata says
  PENDING, converge to `CLEANUP_DONE` — this is the normal crash-recovery path,
  not an error.
- **`git worktree prune`** runs **only** after a successful removal, or when a
  stale admin entry has been positively identified. Never speculatively, since
  another worktree may be mid-creation (F7).
- **Bounded retries.** Transient failures increment `attempts`; after
  `max_attempts` the candidate becomes REVIEW. Never an infinite retry loop.

---

## 7. Multi-node ownership

The controller **cannot see another node's filesystem**. A repo at
`C:\Users\tranv\project` or `/home/dell/workspace/x` does not exist here, and
running git against that path locally answers about a path that means nothing.
This is the lesson `coordinator.node_aware_repo_evidence` already learned for
metadata, applied to deletion.

- **The controller never removes a path on another node.** Remote removal
  happens only by asking the owning node-agent, over an authenticated endpoint
  (`GET /v1/worktree/candidates`, `POST /v1/worktree/cleanup`), mirroring the
  existing `/v1/repo/{op}` pattern.
- The node **re-classifies locally with its own config and fresh evidence**
  before acting, and refuses on `expected_head`/`expected_branch` mismatch
  (optimistic concurrency against a stale controller view).
- Each node enforces **its own** allowlist over its own paths. That is the
  correct owner of the decision.
- Application-level refusals return **HTTP 200 with an error code** — this
  agent's documented convention; a 4xx is read as "unreachable" by
  `node_client._request`. A genuinely missing route stays 404 and maps to
  `NODE_LACKS_WORKTREE_JANITOR`, kept **distinct** from `NODE_UNREACHABLE`.
- Node offline ⇒ REVIEW. Never AUTO_SAFE, never a local fallback.

---

## 8. Audit and reclaimed bytes

Two rows per removal attempt, through the **existing** `AuditStore`:

| When | action | Contents |
|---|---|---|
| **Before** | `worktree_cleanup_attempt` | node, repo_path, worktree_path, branch, policy class, full predicate/evidence set, **predicted** `reclaimed_bytes` |
| **After** | `worktree_cleanup_result` | `result=OK\|DENIED\|FAILED`, reason code, **observed** `reclaimed_bytes`, `latency_ms` |

The before-row is written **before** the removal is attempted, so a crash
mid-removal still leaves a record of what was about to happen.

**Never recorded:** file contents, diffs, or the contents of any valuable
ignored file. Paths and sizes only. This log is the one an operator greps
freely; a cleanup audit that itself stored the secret would defeat the denial
it is recording.

`reclaimed_bytes` is a bounded walk that never follows symlinks and never
crosses filesystems, returning a best-effort number plus a `partial` flag.

---

## 9. Disk pressure

When free space on the filesystem holding the candidates crosses a threshold,
the janitor may **only**:

- (a) shorten `grace_seconds` toward `grace_floor_seconds` — never below,
- (b) reorder AUTO_SAFE candidates largest-first,
- (c) raise alert severity and emit a pressure event.

It may **never** downgrade or skip a predicate, promote REVIEW/BLOCKED, pass
`--force`, or touch a dirty/unmerged tree. See **I7**.

Pressure is evaluated **per-mount**, on the candidate's own filesystem, reusing
`host_metrics` rather than a second implementation. This is not pedantry: on
m910 the worktree filesystem was 26% used with 83G free while tmpfs `/tmp` was
at 74% and had already hit an `EDQUOT` that broke unrelated tooling. Removing
worktrees frees the *worktree* filesystem only. The report must state which
filesystem a reclaim affects, so nobody is misled into thinking it relieves
`/tmp`.

---

## 10. Invariants (I1–I8)

Each must be expressed as a **test**, not a comment.

| # | Invariant |
|---|---|
| **I1** | The janitor **never** passes `force=True` / `--force`. Not under disk pressure, not on retry, not ever. Every janitor path calls `git_worktree.remove_worktree(..., force=False)`. |
| **I2** | The main worktree and any configured repo root can never be selected as a candidate. |
| **I3** | A dirty, or unmerged-and-unpushed, worktree is never removed — regardless of disk pressure. |
| **I4** | The controller never removes a path on another node. No `os`/`shutil`/`subprocess` removal primitive is reachable from the controller-side path. |
| **I5** | Classification is pure and read-only. Its only side effects are audit rows and events. |
| **I6** | Every removal writes an audit row **before** the attempt and one **after**, including reclaimed bytes and the evidence set. |
| **I7** | Disk pressure may only shorten grace toward a floor and reorder candidates. It may never downgrade a predicate, promote a class, or enable force. |
| **I8** | The feature flag defaults to `observe_only`. Nothing is ever removed until an operator explicitly opts in. |

---

## 11. Dangerous failure modes (F1–F13)

Each needs a named test.

| # | Failure |
|---|---|
| **F1** | Deleting a worktree a retry still needs — i.e. treating a bare `FAILED` as final. |
| **F2** | Deleting unmerged work that exists nowhere else (branch local only, never pushed, not merged). |
| **F3** | Deleting an ignored-but-valuable file: a task's sqlite db, a `.env`, collected evidence. |
| **F4** | The controller running a removal against a path that only exists on another node — thereby deleting the **controller's** like-named directory. |
| **F5** | Two janitors racing one worktree: double removal, or removal during another's classification. |
| **F6** | Crash between `git worktree remove` and the metadata write — state says PENDING while the directory is gone. Recovery must be idempotent and converge to `CLEANUP_DONE`. |
| **F7** | A stale admin entry (directory gone, git still lists it) treated as live; or `git worktree prune` run while another worktree is mid-creation. |
| **F8** | Root-owned or permission-denied artifacts inside the tree causing a *partial* removal that leaves a half-deleted worktree. Must be detected and left BLOCKED, never retried with force. |
| **F9** | Windows locked directory: removal fails. Must surface as `PERMISSION_DENIED`/BLOCKED with no retry-loop and no force. |
| **F10** | Symlink / mount / bind-mount escape — `worktree_path` containing a symlink into a real repo. |
| **F11** | Evidence staleness: classifying AUTO_SAFE on evidence gathered minutes ago, after a human has started working in the tree. |
| **F12** | Task reopen/retry after `CLEANUP_DONE` — must fail loudly (`WORKTREE_REMOVED`) rather than silently recreating and losing the link. |
| **F13** | Parent/child (planner split): a parent's worktree removed while a child still works in it, or a child's removal attributed to the parent. |

---

## 12. Rollout: observe_only → suggest_only → auto_execute

Mirrors `supervisor2.POLICY_MODES`, this project's audited precedent for
exactly this kind of escalation, and keeps its default.

1. **`observe_only` (default everywhere)** — classify, report, audit. Zero
   removals. Run for a defined observation window and export the report.
2. **`suggest_only`** — additionally emit events/alerts and populate the review
   queue. Still zero removals.
3. **`auto_execute`** — AUTO_SAFE candidates removed automatically. Enable
   **per-repo / per-node first**, never globally in one step, and only after
   the observation window shows zero misclassifications.

Promotion to step 3 requires an explicit human go-ahead, consistent with this
project's standing rule for outward-facing behaviour changes (see the per-lane
`auto_dispatch_enabled` decision in the Backlog). Record the observed
misclassification rate in `docs/REQUIREMENTS.md` **before** flipping.

---

## 13. Acceptance tests

Written against **real** git repositories in `tmp_path` — real `git init`, real
worktrees, real symlinks. Never a mocked `git`: the containment, secret-denial
and merge rules *are* the product, and a mock proves nothing about what
`git status --ignored` or `Path.resolve()` actually do. Same posture as
`tests/test_repo_read.py`.

### P0 — classification engine (audit-only)

- Source-level test: the module contains **no** deletion primitive
  (`shutil.rmtree`, `os.remove`, `worktree remove`), checked by walking the AST
  for call names rather than grepping prose.
- main worktree ⇒ refused (`MAIN_WORKTREE`)
- **clean + merged ⇒ AUTO_SAFE**
- **dirty ⇒ BLOCKED / `DIRTY`**
- **unmerged, local-only ⇒ BLOCKED / `UNMERGED_UNPUSHED`**
- pushed-but-not-merged ⇒ REVIEW; AUTO_SAFE when `allow_preserved_unmerged=True`
- detached HEAD ⇒ REVIEW / `DETACHED_HEAD`
- valuable ignored file (`.env`, `*.db`) ⇒ BLOCKED, paths listed, contents absent
- cache-only ignored entries ⇒ not blocking
- stale admin entry ⇒ `ADMIN_ENTRY_STALE`
- orphan (no task) ⇒ REVIEW on first sighting
- symlinked `worktree_path` ⇒ BLOCKED / `SYMLINK_OR_MOUNT`
- path outside `allowed_roots` ⇒ `PATH_NOT_ALLOWED`
- config validation: every out-of-range field raises `ValueError` at load
- default mode is `observe_only`

### P1 — lifecycle state

- COMPLETED / SKIPPED / CANCELLED isolated task ⇒ `CLEANUP_PENDING` with correct
  `eligible_at`
- bare `FAILED` ⇒ **no** record (F1)
- `FAILED` at `attempt_count >= max_attempts` ⇒ record created
- non-isolated task ⇒ never a record
- hook is idempotent across repeated transitions
- retry/reopen clears a PENDING/REVIEW record
- dispatch after `CLEANUP_DONE` ⇒ refused with `WORKTREE_REMOVED` (F12)
- a task with no `worktree_cleanup` key behaves exactly as before

### P2 — executor

- **AUTO_SAFE clean+merged worktree is really removed**, directory gone,
  `reclaimed_bytes > 0`
- **active process / tmux pane / session record inside the path ⇒ not removed**
  (`PROCESS_IN_USE` / `TMUX_IN_USE` / `SESSION_IN_USE`)
- dirty tree not removed **even with disk pressure simulated at 99% full** (I3, I7)
- `force` never passed — asserted on the call arguments (I1)
- candidate that turns dirty between scan and execute ⇒ `EVIDENCE_CHANGED`,
  nothing removed (F11)
- two concurrent janitors ⇒ exactly one removal (F5)
- already-removed directory ⇒ converges idempotently to `CLEANUP_DONE` (F6)
- permission-denied subtree ⇒ BLOCKED, no partial state (F8)
- audit rows exist before and after, carry reclaimed bytes, and contain no file
  content (I6)
- remote candidate never reaches the local executor (I4)

---

## 14. Explicitly out of scope

- Any write/mutating git operation beyond `git worktree remove` and a guarded
  `git worktree prune`.
- Reclaiming anything that is not an isolated task worktree.
- Relieving tmpfs `/tmp` pressure — a different filesystem; see §9.
- Changing `terminal_worktree_cleanup`'s existing `force=True` escape hatch for
  a human. It stays, but P7 routes it through the same lock/audit/evidence
  recording so there is one removal implementation.
