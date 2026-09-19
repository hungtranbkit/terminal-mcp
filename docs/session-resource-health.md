# Session resource health — reading Context vs Usage, and when to roll over

`terminal_status` and every `terminal_batch_inspect` / `terminal_turn action=inspect`
row now carry a `resource` block. It exists so a dispatcher (ChatGPT, the queue
loop, an operator) can decide whether a session can take another task **without
looking at a screenshot**.

Implemented by `terminal_mcp/session_resource.py`; thresholds live under
`session_health` in the controller config.

---

## The two numbers are not the same number

| | `resource.context` | `resource.usage` |
|---|---|---|
| Answers | How full is **this session's** context window right now? | How much of the **account's** quota window is spent? |
| Scope | One session | Every session on that account |
| Fixed by | Rolling over / compacting this session | Waiting for the reset, or another account |
| Feeds `recommended_action` | **Yes — only this** | **No, never** |

A session at `usage.percent: 95` with `context.percent: 10` is **fine to give
work to** — it just may hit a rate limit. A session at `context.percent: 96`
with `usage.percent: 2` is **nearly out of room** no matter how much quota is
left. Treating quota as context capacity is the exact mistake this split
exists to prevent, and it is why `usage` never changes
`recommended_action`. If quota is what you care about, branch on
`usage.status` (`NORMAL` / `WARNING` / `CRITICAL` / `UNKNOWN`) and on
`usage.reset_in_minutes` / `usage.reset_at`.

---

## Payload

```json
"resource": {
  "agent": "claude",
  "model": "Opus 5",
  "observed": true,
  "context": {"used_tokens": 955002, "max_tokens": 1000000,
              "percent": 96, "status": "CRITICAL",
              "tier": "FINISH_CURRENT_AND_ROLLOVER"},
  "usage":   {"percent": 30, "reset_in_minutes": 49,
              "reset_at": "2026-09-19T12:49:00+00:00", "status": "NORMAL"},
  "git":     {"repo": "/home/kimex/workspace/terminal-mcp",
              "branch": "main", "dirty": true},
  "recommended_action": "CHECKPOINT_BEFORE_ROLLOVER",
  "rollover": {"recommended": true, "allowed": false,
               "requires_checkpoint": true,
               "blocked_reason": "GIT_DIRTY_CHECKPOINT_REQUIRED",
               "checkpoint": {"task_id": null, "resume_token": null,
                              "branch": "main", "last_commit": "1e06752 …",
                              "conversation_id": null, "worktree_path": null,
                              "cwd": "/home/kimex/workspace/terminal-mcp",
                              "last_checkpoint_at": null}},
  "policy": {"watch_percent": 70.0, "prepare_rollover_percent": 85.0,
             "finish_rollover_percent": 92.0, "checkpoint_only_percent": 97.0}
}
```

### `null` means unknown, and unknown is not zero

Every number is either read off a labelled footer field the agent itself drew,
or it is `null`. Nothing is back-computed from a percentage and nothing is
defaulted. A plain shell pane, an agent whose footer is off-screen, a footer
mid-redraw, or a shape the parser has not been shown all produce
`observed: false`, `context.percent: null`, `context.status: "UNKNOWN"` and
`recommended_action: "UNKNOWN"`.

**A consumer must treat `UNKNOWN` as "no information", not as "healthy".** If
you need certainty before dispatching a large task, require
`context.status` ∈ {`NORMAL`, `WATCH`} rather than merely
`!= "CRITICAL"`.

---

## The policy table

Applied to `context.percent` only. Configurable under `session_health`; the
low edge of each band belongs to the higher tier.

| `context.percent` | `context.tier` | `context.status` | `recommended_action` | What the dispatcher should do |
|---|---|---|---|---|
| `< 70` | `NORMAL` | `NORMAL` | `CONTINUE` | Dispatch anything. |
| `70 … <85` | `WATCH` | `WATCH` | `CONTINUE_WATCH` | Keep dispatching; prefer smaller tasks. Start checking `resource` between tasks. |
| `85 … 92` | `PREPARE_ROLLOVER` | `PREPARE_ROLLOVER` | `PREPARE_ROLLOVER` | Finish what is in flight. Get a successor session/worktree ready. No new multi-step task. |
| `>92 … 97` | `FINISH_CURRENT_AND_ROLLOVER` | `CRITICAL` | `FINISH_CURRENT_AND_ROLLOVER` | **No new large task at all.** Land the current one, checkpoint, roll over. |
| `> 97` | `CHECKPOINT_ONLY` | `CHECKPOINT_ONLY` | `CHECKPOINT_ONLY` | Nothing but a checkpoint: commit/stash, write the handoff, stop. Do not start work. |
| — | `UNKNOWN` | `UNKNOWN` | `UNKNOWN` | Nothing was observable. Do not infer health. |

`context.tier` is the policy name; `context.status` is the severity label a
dashboard can colour. They are separate fields so a UI never has to know the
rollover vocabulary.

---

## Rolling over safely

`resource.rollover` is a **hook, not an actor**. Nothing in terminal-mcp kills,
compacts or restarts a session on the strength of it — a rollover is performed
by whoever read the block.

- `rollover.recommended` — the context tier is one of the three rollover tiers.
- `rollover.allowed` — `false` when rolling over now could lose work; `null`
  when no rollover is recommended in the first place.
- `rollover.blocked_reason`
  - `GIT_DIRTY_CHECKPOINT_REQUIRED` — the working tree has uncommitted or
    untracked changes. `recommended_action` becomes
    `CHECKPOINT_BEFORE_ROLLOVER`: commit, stash, or push to a branch **first**.
  - `GIT_STATE_UNKNOWN` — git could not be read (not a repo, probe disabled,
    timeout). Not proven clean, so treated as unsafe. `recommended_action`
    still reports the honest context tier.
- `rollover.checkpoint` — the identifiers a successor session must carry:
  `branch`, `last_commit`, `cwd`, `conversation_id`, `worktree_path`,
  `last_checkpoint_at`. `task_id` / `resume_token` are present as `null` here
  because they belong to a run-journal *wait*, not to a session — take those
  from the `terminal_wait_for_state` / `terminal_resume_wait` response that
  owns the task.

**Never discard a dirty tree to free context.** The correct order is always:
checkpoint → verify the checkpoint → then roll over.

---

## Suggested dispatcher loop

```
rows = terminal_batch_inspect(targets=[...])
for row in rows.targets:
    r = row.get("resource")
    if not r or r["recommended_action"] == "UNKNOWN":
        →  no information; do not dispatch a large task on the strength of it
    elif r["recommended_action"] in ("CONTINUE", "CONTINUE_WATCH"):
        →  dispatch
    elif r["recommended_action"] == "PREPARE_ROLLOVER":
        →  let the in-flight task finish; stage a successor session
    else:  # FINISH_CURRENT_AND_ROLLOVER / CHECKPOINT_BEFORE_ROLLOVER / CHECKPOINT_ONLY
        →  if r["git"]["dirty"] is not False: checkpoint first
        →  then roll over to the successor, carrying r["rollover"]["checkpoint"]

# Separately, and never mixed into the above:
if r["usage"]["status"] == "CRITICAL":
    →  quota is nearly spent; expect throttling until usage.reset_at
```

---

## Where the numbers come from

Pane-derived, from the agent's own footer, parsed off the same 80-line capture
`terminal_status` already takes (no extra read). The observed Claude Code shape
this was built against:

```
[Opus 5 (1M context)] | Context ██████████ 96% (in: 2, cache: 955k) | Usage ███░░░░░░░ 30% (resets in 49m)
```

- `context.percent` ← the `Context …%` field (also understands
  `N% remaining` / `left` / `free`).
- `context.used_tokens` ← the footer's own stated prompt-token fields
  (`in:` + `cache:`); `out:` is excluded, and this is **never** derived from
  the percentage.
- `context.max_tokens` ← a window the model field states outright
  (`(1M context)`, `[1m]`), the same "provider stated its own window" rule
  `ai_context_window.py` uses. Unknown otherwise.
- `usage.percent` / `reset_in_minutes` ← the `Usage …% (resets in …)` field;
  `reset_at` is that offset applied to now.
- `git.*` ← the session registry's `repo_root` / `git_branch`, plus one
  bounded, cached `git status --porcelain --untracked-files=normal`
  (untracked counts as dirty, deliberately).
- `agent` ← the pane's classified agent type (`claude`, `codex`, `shell`, …).

Codex and plain shells have no such footer today, so they report
`observed: false` and nulls. For sessions whose transcripts this controller
*can* read, `ai_context_window.py` remains the stronger, structured path —
this module is the one that works when only the screen is available.

Pane text is untrusted output. The parsed model string goes through the same
redaction as every other pane-derived field, and the block carries no pane
text at all — only numbers and fixed enum strings.
