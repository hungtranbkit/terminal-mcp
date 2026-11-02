# HP provider + auto-reconcile evidence — 2026-11-02

## Real sources

- Terminal MCP: `http://127.0.0.1:8766/mcp`, read tools only.
- GitHub via the existing authenticated `gh api` client; no token was passed in commands or committed.
- BrandAgent issue `hungtranbkit/facebook-sales-radar#153`: OPEN, updated `2026-10-29T04:26:25Z`.
- BrandAgent PR `hungtranbkit/facebook-sales-radar#154`: OPEN + DRAFT, updated `2026-11-02T06:33:35Z`.
- Offline POS PR `hungtranbkit/offline-pos#47`: OPEN + DRAFT, updated `2026-11-01T09:39:39Z`.
- Two Dell sessions remained separate `IDLE` / `UNKNOWN` observations.

Projection result: 2 projects, 5 cards, 0 duplicates, 0 `done`. Draft PRs mapped to Kanzo `review` with `github-draft`; they were not labelled QA or complete.

## Running local stage

- URL: `http://127.0.0.1:14327` (HP-local only).
- UI transient unit: `tmcp-kanzo-pilot-ui-82-106.service`.
- Reconciler transient unit: `tmcp-kanzo-pilot-reconcile-82-106.service`.
- State: `/home/kimex/.local/state/terminal-mcp/kanzo-pilot-82-106`.
- Listener observed on `127.0.0.1:14327` and `::1:14327` only; HTTP 200.
- Kanzo RSS observed: 3,984 KiB. Reconciler steady systemd memory observed: ~13 MiB (31.8 MiB peak during source reads).
- Successful source cycles began at `07:02:15Z`, `07:03:20Z`, `07:04:24Z`, and `07:05:29Z`: approximately 64.4–65.3 seconds including ~4–5 seconds source time after the 60-second delay.

Starting a second reconciler against the same home returned `another reconcile worker already owns this home` and exit 3. After all five UI rows were deliberately changed to `done`, the next source cycle restored 3 `todo` + 2 `review`, with no `done` rows.

The 390×844 real page screenshot is `mobile-live-390x844.png`. Its accessibility snapshot showed 5 open tasks, Todo 3, Review 2, and an empty Done column. The upstream Kanzo board intentionally scrolls columns horizontally on mobile.

Rollback is scoped and non-destructive:

```bash
systemctl --user stop tmcp-kanzo-pilot-reconcile-82-106 tmcp-kanzo-pilot-ui-82-106
```

No controller, queue, agent session, GitHub work item, firewall, tunnel, Mac process, or public route was changed.
