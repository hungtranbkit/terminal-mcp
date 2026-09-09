# Project Backlog

Planning layer for Terminal MCP: **what a project intends to do**, kept
in the project itself, shared by every session on that repo.

## Backlog vs Task Queue (the distinction this feature exists to enforce)

| | **Backlog** (this feature) | **Task Queue** (`queue_store.py`) |
|---|---|---|
| Answers | what the project *intends* to do | what is *executing* now |
| Scope | project / repo | lane / session |
| Lives in | `.terminal-mcp/backlog.json` **in the repo** | controller SQLite |
| Lifetime | survives clones, machines, sessions | runtime |
| Created when | work is *identified* | work is *dispatched* |

A backlog item never *becomes* a queue task. `backlog_dispatch` **creates**
one through the existing canonical `QueueService.create_task` and links
both sides, so `backlog_id → queue task_id → session → commit/test` stays
traceable. This is why the audit that preceded implementation mattered:
`queue_store.queue_tasks` is the one canonical task table (planner, PM,
incident and release all layer on it via metadata rather than adding
tables), and this feature adds **no** second task engine.

## Source of truth: the controller, keyed by project

**`project_id` (canonical, git-derived) → controller-side SQLite.**
The repo file `<repo>/.terminal-mcp/backlog.json` is an **export/import
projection**, not the live store.

### Why this changed (measured, not assumed)

The first design made the repo file authoritative. Measuring the actual
fleet killed that:

| project | nodes | checkouts |
|---|---|---|
| `git:github.com/hungtranbkit/terminal-mcp` | local, m910, macbook | **7** |
| `git:github.com/hungtranbkit/offline-pos` | dell-5530, local | 3 |

One project genuinely lives in many checkouts on many machines. A backlog
stored in *one* checkout is invisible to every other node working the
same project — verified: the controller answered `PATH_NOT_ALLOWED` for
m910's, macbook's and dell-5530's checkout paths, because **those paths do
not exist on the controller at all**. The old "portable, reaches other
nodes" claim was not true in practice.

The *identity* half was already right — all 7 checkouts collapse to one
`project_id`. Only the **storage** was wrong. So the controller (the one
point every node already talks to) holds the canonical backlog, and the
file keeps the portability/review benefits without being what agents race
on.

### Addressing a project

Three ways, in precedence order:

1. **`project_id`** — e.g. `git:github.com/acme/widget`. Works with no
   checkout on this machine at all.
2. **`project_node_id` + `project_session`** — "the project THIS session
   is working on", resolved from the **owning node's own registry**. This
   is what makes a remote session's backlog reachable: the controller
   cannot stat a path on another machine, but it can read what that node
   reported.
3. **`path`** — a local checkout, gated by `allowed_cwd_roots`.

Ambiguity fails loudly rather than guessing — editing the wrong project's
plan is worse than an error.

### Export / import

- `terminal_backlog_export(path)` writes the project's backlog to a local
  checkout's `.terminal-mcp/backlog.json` — commit it for review and
  portability.
- `terminal_backlog_import(path)` reads it back. **Merges by default**: a
  known item id is updated, an unknown one added, and nothing local is
  deleted — adopting a teammate's committed backlog must never silently
  drop work only this controller knows about. `replace=True` is the
  deliberate destructive form.

Hand-edits to a committed file are still supported; the repair now
happens at the **import** boundary, and a corrupt or newer-schema file is
refused there while the controller's own backlog stays untouched.

### Concurrency

The model changed with the store. The controller is the single writer, so:

- a **per-project** in-process lock serializes its own concurrent
  requests (per project, so two projects never block each other),
- SQLite's transaction makes the item swap atomic,
- **`expected_revision` still guards the cross-agent race** — two ChatGPT
  sessions editing one project conflict safely with `REVISION_CONFLICT`.

The old `fcntl.flock` on a repo file no longer governs anything and was
removed rather than left as decoration.

## Project identity

Identity comes from the **repository**, never the cwd string, so `/repo`,
`/repo/sub/dir`, and a second checkout of the same remote all resolve to
one `project_id`. Precedence, most durable first:

1. **`git remote origin`, normalised** → `git:github.com/acme/widget`.
   All URL styles collapse to one id (`https://`, `git@host:path`,
   `ssh://`, with or without `.git`), and any embedded credential is
   stripped — a token must never end up in a file that gets committed.
2. **`PROJECT.yaml` → `project.code`** → `code:my-project`, when there is
   no remote.
3. **Real repo root path** → `path:/abs/path`, reported with
   `is_portable: false` so nobody mistakes a machine-local id for a
   portable one.

A directory that is not in a git repo is **refused** (`NOT_A_PROJECT`)
rather than getting a backlog somewhere meaningless.

## Item schema (`schema_version: 1`)

```jsonc
{
  "id": "blg_9f2c1a4b77de",       // server-owned, stable
  "title": "Add rate limiting",
  "description": "",
  "status": "BACKLOG",
  "priority": "P1",                // P0..P3
  "type": "feature",               // feature|bug|chore|incident|research|docs|test
  "order": 3,
  "created_at": "...", "updated_at": "...",
  "source": "chatgpt",
  "dependencies": ["blg_..."],
  "acceptance_criteria": ["429 after N requests"],
  "tags": ["api"],
  "assignee": null, "session": null, "node_id": null,
  "queue_task_id": null,           // set by dispatch -- the traceability link
  "branch": null, "worktree": null,
  "blocked_reason": null,
  "evidence": { "commits": [], "tests": [], "deploys": [], "notes": [] },
  "history": [ { "at": "...", "event": "created", "by": "chatgpt" } ]
}
```

Unknown keys are **preserved** on rewrite: a human or a newer version may
have added something, and dropping their data silently would be worse
than carrying it.

## Status lifecycle

```
BACKLOG ──► READY ──► IN_PROGRESS ──► NEEDS_REVIEW ──► DONE
   │           │           │                            ▲
   └───────────┴───────────┴──► BLOCKED ────────────────┘
                     └──► CANCELLED
```

- **BACKLOG** — captured, not promised. Write here the moment work is
  identified, even with no session to run it.
- **READY** — groomed and dispatchable.
- **NEEDS_REVIEW** — finished but *unverified*. Use this instead of
  forcing DONE.
- **DONE** — **verified**. Gated: see below.

### The verified-done gate

`backlog_complete` refuses DONE on an agent's say-so. It requires **either**
real evidence (a commit SHA / test result / deploy ref) **or** a linked
queue task that reached `COMPLETED` — which `queue_store.py`'s own comment
defines as the spec's `VERIFIED_DONE`. Otherwise it returns
`EVIDENCE_REQUIRED`. `backlog_update` cannot set DONE at all.

## Concurrency

Several agents on several nodes share one checkout, so:

- **File lock** (`fcntl.flock` on a separate `.lock` file) wraps every
  read-modify-write. A separate file is used because the data file is
  *replaced* by each save — a lock on the old inode would protect nothing.
- **Atomic write**: temp file in the same directory + `os.replace`. A
  reader sees the old or the new file, never a truncated one.
- **Optimistic revision**: `revision` increments per write; pass
  `expected_revision` and a stale write is refused with
  `REVISION_CONFLICT` instead of clobbering the other agent.

On a platform without `fcntl` (a Windows node) locking degrades to
none, and `expected_revision` remains the protection — documented rather
than silently assumed.

## Security

- Every path goes through `lifecycle.resolve_cwd`, the **same**
  `allowed_cwd_roots` + symlink-resolution gate session creation uses. The
  gate runs *before* any git introspection, so traversal is refused
  without touching the target. The discovered repo root is re-checked too,
  since walking up from an allowed subdirectory could otherwise escape.
- Every write is audited (`backlog_add|update|claim|dispatch|block|complete`).
- Dashboard writes additionally pass `_mutation_guard` (Cloudflare Access
  / webauth identity).

## Auto-detecting git projects

`terminal_project_list` answers "what projects is this fleet working on?"
from data every node **already records** — no new tracking, no scanning,
no config. Each node's session registry stores `repo_root`/`git_remote`/
`git_branch` per session; normalising each remote collapses every checkout
of one repo onto one `project_id`.

It merges that live discovery with the projects that already have a
backlog, so a project with sessions but **no backlog captured yet** is
visible rather than forgotten. Each row carries `nodes`, `checkouts`,
`session_count`, `has_backlog`, `open_total`.

## MCP tools

| Tool | Purpose |
|---|---|
| `terminal_backlog_get` | read + filter; returns `project`, `revision`, `counts`, `items` |
| `terminal_backlog_add` | capture new work (use immediately, even with nothing to run it) |
| `terminal_backlog_update` | patch one item (cannot set DONE) |
| `terminal_backlog_bulk_update` | many patches, one atomic write |
| `terminal_backlog_claim` | take ownership → IN_PROGRESS |
| `terminal_backlog_dispatch` | promote to a real queue task (the crossing point) |
| `terminal_backlog_block` | BLOCKED + required reason |
| `terminal_backlog_complete` | DONE, evidence-gated |
| `terminal_backlog_validate` | re-normalise a project's stored items |
| `terminal_backlog_export` | write the backlog to a local checkout's file (commit it) |
| `terminal_backlog_import` | read a committed file back in (merges by default) |
| `terminal_project_list` | auto-detected git projects across the fleet |

## Dashboard panel

**`/dashboard/backlog`** — its own page, reachable from the main
dashboard menu (📋 Project Backlog), alongside `/dashboard/nodes` and
`/dashboard/tasks`. It is a *view* over the API routes below, not a new
privilege surface: reads pass `_read_guard`, and every write goes back
through the `_mutation_guard`-ed API, which is itself path-gated by
`BacklogService` — the page cannot reach a project the API would refuse.

It shows, per project (enter any path inside the repo; it is remembered
in `localStorage`):

- counts — open / in progress / blocked / done / total, plus `rev`, the
  backlog file path, and a `repairs` chip if the file needed normalising
- filters — status, priority, and an "only open" toggle
- per item — status/priority/type chips, tags, acceptance criteria,
  blocked reason, the owning session, and a **queue** chip linking to
  Global Tasks when the item has been dispatched
- actions — add, move status, **Dispatch** (prompts for a session), and
  **Complete** (prompts for evidence, because the API requires it)

Every write sends `expected_revision`, so the UI cannot bypass the
optimistic-concurrency check that protects concurrent agents.

**XSS posture is deliberate here**, because backlog text is written by
*agents* into a repo file and rendered in a browser: the page builds all
content with `textContent` and never assigns `innerHTML`/`outerHTML`, has
no inline `on*=` handler attributes, and `tests/test_backlog_panel.py`
pins all three (an item titled `<img src=x onerror=...>` renders as
text).

API routes: `GET /dashboard/api/backlog`, `POST /dashboard/api/backlog/{add,update,dispatch,complete}`.

## The workflow ChatGPT should follow

```
get → analyse → add/update → dispatch → verify → complete
```

```python
b = terminal_backlog_get(path="/home/dell/workspace/terminal-mcp")
# b["revision"] -> 7, b["counts"], b["items"]

terminal_backlog_add(path=..., tasks=[
    {"title": "Add rate limiting", "priority": "P1", "type": "feature",
     "acceptance_criteria": ["429 after N requests"]}])

terminal_backlog_update(path=..., task_id="blg_...", patch={"status": "READY"},
                        expected_revision=b["revision"])      # safe concurrent write

d = terminal_backlog_dispatch(path=..., task_id="blg_...", session="win1")
# d["queue_task_id"] -> now traceable end to end

terminal_backlog_complete(path=..., task_id="blg_...", commit="a1b2c3d",
                          test="52 passed")
```

**Never** create a queue task for work that cannot run yet — add it to the
backlog and dispatch later. That is how work stops being lost between
sessions.

## Project Brief integration — WIRED

A session's recovery brief (`terminal_knowledge_recover`, backed by
`session_knowledge.recovery_brief`) already knows which **repo** the
session was working in (`meta.repo_root`), and the backlog is keyed on
exactly that repo — so the brief reports what the project still intends
to do instead of keeping a second, drifting list.

The brief gains a `project_backlog` field:

```jsonc
"project_backlog": {
  "available": true,
  "project": { "project_id": "git:github.com/acme/widget", ... },
  "backlog_file": "/repo/.terminal-mcp/backlog.json",
  "open_total": 7, "unrun_total": 3,
  "counts": { "BACKLOG": 5, "IN_PROGRESS": 2, ... },
  "open_items":  [ { "id", "title", "status", "priority", "type",
                     "queue_task_id", "tags", "blocked_reason" } ],
  "unrun_items": [ ... ]                 // never dispatched
}
```

and `recovery_brief_text` gains a readable block, with `[unrun]` marking
items nothing is executing:

```
-- open project backlog: 2 open, 1 never dispatched (source of truth: /repo/.terminal-mcp/backlog.json) --
[P0] BACKLOG      blg_1c77e0aa3b52  Flaky test: test_session_reattach times out  [unrun]
[P1] IN_PROGRESS  blg_9f2c1a4b77de  Add rate limiting to the public API
```

Design points worth keeping:

- **Read, never cached.** The brief reads the backlog file each time, so
  it cannot drift from the source of truth — pinned by a test that adds an
  item between two briefs.
- **Never fatal.** A session outside `allowed_cwd_roots`, a non-repo cwd,
  a project with no backlog, or a corrupt file each attach
  `{"available": false, "reason": ...}` and leave the rest of the brief
  intact. Recovering context must not depend on the backlog existing.
- **Marked untrusted.** `project_backlog` is added to the brief's
  `untrusted_fields`. Backlog text is deliberate structured data rather
  than a raw pane scrape, but it is still *agent-written* — a confused or
  compromised agent could park injection text in a title that then lands
  in another agent's brief.
- **Same path gate.** The `BacklogService` is built from the
  `TerminalService`'s own config, so `allowed_cwd_roots` is the one used
  for session creation — never a second, looser policy. It is lazily
  constructed rather than a constructor argument, so no other caller (or
  test) has to know the backlog exists.
