# HP live-source staging evidence — 2026-11-02

Source was the already-running Terminal MCP Streamable HTTP endpoint at `http://127.0.0.1:8766/mcp`. Only `terminal_list_sessions`, `terminal_registry_search`, `terminal_batch_inspect`, and `terminal_session_tasks` were called during investigation; no mutation tool or direct database write was used.

## Canonical observations

| Project | Stable session ID | Repository / branch | Runtime | Canonical tasks |
|---|---|---|---|---:|
| BrandAgent | `16c73059-d6a0-496a-89de-494fc992a75b` | `hungtranbkit/facebook-sales-radar` / `feat/brandagent-brand-studio-20261029` | `ACTIVE`, inspect `IDLE` | 0 |
| Offline POS | `1283726d-cd79-409b-aa3d-b8599783da22` | `hungtranbkit/offline-pos` / `fix/offlinepos-ci-nav-browser-20261030` | `ACTIVE`, inspect `IDLE` | 0 |

`terminal_session_tasks` returned empty `queued`, `running`, `blocked_rework`, `waiting_dependency`, and `recent` arrays for both sessions. Consequently the projection produced two `session:<stable-id>` observation cards with `UNKNOWN` source state and `todo,unknown,codex` UI state. It produced no QA, completion, issue, or PR claim.

## Reproducible local run

```bash
.venv/bin/python scripts/kanzo-readonly-pilot.py live \
  --source-url http://127.0.0.1:8766/mcp \
  --projects docs/evidence/kanzo-readonly-pilot/hp-live-source-projects.json \
  --home /tmp/tmcp-kanzo-live/home \
  --synced-at 2026-11-02T07:00:00Z
```

Observed receipt: 2 projects, 2 cards, 0 duplicates. `PRAGMA integrity_check` returned `ok`.

Kanzo v0.3.0 was launched twice on isolated port 14327. Both starts returned HTTP 200; `ss -ltn` showed only `127.0.0.1:14327`. Both staging processes were stopped afterward.

Both Kanzo cards were then locally moved to `done`. Re-running the live import restored both to `todo`, retained exactly two unique `tmcp_projection.canonical_key` rows, and passed integrity check. This verifies one-way recovery across importer and Kanzo restarts.

## Acceptance blocker

This is real source-backed two-project integration, but not yet real-task acceptance: the selected real sessions have no canonical task rows. Creating a task merely to make the demo pass would create a second/fake authority and was not done. No Mac bridge, new network permission, public listener, deployment, merge, service restart, queue mutation, or auth change was made.
