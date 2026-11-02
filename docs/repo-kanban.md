# Repo Kanban (`/dashboard/repo-kanban`)

A read-only, multi-repo task board. Each repository keeps its own board **in
the repo**, and the dashboard reads it from a committed ref and joins it with
the session registry. The board cannot drift from the work, because agents
update it in the same commit as the work.

## Contract `repo-kanban/v1`

`docs/kanban/board.json` (curated, required):

```json
{
  "repo": "owner/name", "updated": "YYYY-MM-DD", "integrationBranch": "…",
  "preview": "free text", "rules": ["…"],
  "cards": [{
    "id": "W07", "title": "…", "owner": "Claude|Codex|Owner|External",
    "status": "NEW|RUNNING|REVIEW|BLOCKED|DONE", "branch": "feat/…",
    "prs": [154], "issues": [130], "evidence": "…", "next": "…", "updated": "YYYY-MM-DD"
  }]
}
```

- `DONE` and `REVIEW` require `evidence`. The dashboard flags violations and never hides them.
- Legacy `TODO`/`IN_PROGRESS` map to `NEW`/`RUNNING`.

`docs/kanban/live.json` (optional; written by the repo's own sync tool, e.g. Startup Pixel `tools/kanban-sync.mjs`):

- `generatedAt`, `generatedBy`;
- `integration {branch, sha, qa}`;
- `branches[name] {exists, sha, date, subject, dirtyWorktrees, qa {status: AT_HEAD|DOCS_AHEAD|STALE|FAIL|NONE, commit, run}}`;
- `prs[n] {state, isDraft, updatedAt}`;
- `issues[n] {state}`;
- `qaRuns[] {id, commit, passed, failedStep, seconds, completedAt}`.

## What the dashboard adds

- **Per card:**
  - `last_updated` is the newest of the card date, the branch commit and any PR update.
  - Drift warnings: all PRs merged but not DONE, a closed PR, RUNNING with no commit for 3 days, DONE with STALE or FAIL QA, a missing branch, an invalid owner or status, DONE/REVIEW without evidence.
  - Sessions on the card branch.
- **Per repo:** counts per column × owner, integration head + QA, sessions (active/total) matched by normalised `git_remote`, and the time of the last sync.
- **Statuses never change automatically.** Live facts only add warnings.

## Configuration (`repo_kanban` in config.yaml)

```yaml
repo_kanban:
  repos:
    - id: startup-pixel                       # [a-z0-9._-], used in ?repo=
      name: Startup Pixel
      path: /home/dell/workspace/startup-pixel-game-dev   # inside repo_read roots
      ref: origin/integration/world-designer-alpha-art-rc-20261022
      project: startup-pixel                  # optional /dashboard/projects slug
```

The controller does not fetch. Keep `ref` fresh with the repo's own sync, which runs `git fetch`, or with a timer.

## Security

- **Repos and inputs:** repos come only from config. Requests name a repo id, never a path or a ref.
- **What is read:** files are read with `git show <ref>:<path>`, never from a worktree, so uncommitted edits stay private.
- **What is returned:**
  - Fields are whitelisted and length-capped, and credential-shaped text is redacted.
  - Remotes lose userinfo.
  - Session rows carry only name, node, agent type, state, branch and activity time. They never carry the cwd, the launch command or notes.
- **Routes:** all three are GET-only behind the dashboard `_read_guard` (Cloudflare Access when configured). There is no mutation route.
- **Errors:** they are codes (`REPO_NOT_FOUND`, `REPO_UNAVAILABLE`, `REPO_OUTSIDE_ALLOWED_ROOTS`, `REF_NOT_FOUND`, `BOARD_MISSING`, `INVALID_JSON`, `INVALID_BOARD`), never git stderr.
- **Rendering:** the page renders agent-written text with `textContent` only.
