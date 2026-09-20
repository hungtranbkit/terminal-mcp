# Dashboard navigation

## The complaint

> Several dashboard screens hide the main navigation/menu; once entered, you
> must use browser Back before seeing the menu again.

`/dashboard` carried the full menu. Every other full page carried a single
`← Dashboard` link or nothing at all, so Sessions, Nodes, Fleet, Audit, Work,
AI Usage, Projects, Agents, Global Tasks, Backlog, Notes, the Terminal Wall
and both ops screens were one-way streets with browser Back as the navigation.

## The shell

`terminal_mcp/dashboard_nav.py` owns the destinations, the markup and the
stylesheet. `apply(html, surface, active, breadcrumbs)` takes a page's
existing HTML and injects the bar into it; a page says which entry is active
and, when it has one, its own breadcrumb trail. A page never describes the
menu, so the menu cannot go stale on one page and not another.

`tests/test_global_nav.py` walks the real route table and fails when a full
page is served without it — that is the check that stops a new page shipping
without navigation.

### Two surfaces, one component

`/dashboard/*` is behind Cloudflare Access; `/app/*` is behind a session
cookie. A single menu spanning both would offer every `/app` user a row of
links they cannot open, so they share the component and the stylesheet and
differ only in their destination list (`DASHBOARD_NAV` / `APP_NAV`).

## Destinations, as shipped

| destination | route | in the bar |
| --- | --- | --- |
| Home | `/dashboard` | primary |
| Projects | `/dashboard/projects` | primary |
| Agents | `/dashboard/agents` | primary |
| Global Tasks | `/dashboard/tasks` | primary |
| Sessions | `/dashboard/sessions` | primary |
| Nodes | `/dashboard/nodes` | primary |
| Queue / Review | `/dashboard/work` | primary |
| Terminals | `/dashboard/terminal-wall` | primary |
| Backlog | `/dashboard/backlog` | overflow |
| Notes | `/dashboard/notes` | overflow |
| Fleet | `/dashboard/fleet` | overflow |
| AI Usage | `/dashboard/ai-usage` | overflow |
| Audit | `/dashboard/audit` | overflow |
| Dispatch monitor | `/dashboard/ops/novaretail-dispatch` | overflow |
| Dispatch settings | `/dashboard/ops/dispatch-settings` | overflow |
| Requirements | `/dashboard/requirements` | overflow |
| Web Terminal | `/dashboard/terminal` | overflow |

`/app` adds Home, Sessions and Terminal as primary and Password as overflow.

Not pages, and therefore not in the menu: `/dashboard/assets/{filename}`
(static), `/dashboard/api/*` (JSON) and `/dashboard/ws/terminal` (WebSocket).

## Two rendering defects this cost, and how they were found

The markup test cannot see layout. Both of these passed every markup
assertion and were visibly broken in a browser; both were found by
`tests/test_dashboard_nav_browser_smoke.py`, which measures the RENDERED bar.

**AI Usage put the bar in its sidebar.** That page's `<body>` is a CSS grid
(`grid-template-columns: 196px 1fr`), so the injected bar and breadcrumb were
auto-placed into its cells: the bar came out 196px wide and 161px tall instead
of 44, the page grew a third implicit column, and its 196px sidebar was
squeezed to 17px. Placing them by hand would mean the page's row numbers and
the shell's markup having to agree forever, so both are taken OUT of the grid
on that page — fixed to the top, with the body reserving the space.

**The breadcrumb inherited a page's `nav {}` rule.** The bar is a
`<div role="navigation">` precisely so a bare element selector cannot reach
it, but the breadcrumb IS a `<nav>` — and AI Usage styles `nav` as a
column-flex sidebar with a right border, which stacked the trail vertically.
`.tmcp-crumbs` now states every property its layout depends on rather than
inheriting it.

## Tests

- `tests/test_global_nav.py` — the route-table sweep: every full page carries
  the bar, menu links resolve, active state, nested breadcrumbs, both
  surfaces, auth guards unchanged.
- `tests/test_dashboard_nav_browser_smoke.py` (`-m browser_smoke`) — real
  Chromium against the real app on loopback. Measures the bar's rendered
  height, position, width, flex direction and overflow on every page at
  1366×768, 1920×1080 and 390×844; opens the overflow drawer on each; and
  clicks a full tour of the dashboard without ever calling `go_back()`.
