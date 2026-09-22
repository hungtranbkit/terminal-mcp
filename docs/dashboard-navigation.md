# Dashboard navigation

## The complaint

> Several dashboard screens hide the main navigation/menu; once entered, you
> must use browser Back before seeing the menu again.

That was literally true. `/dashboard` carried the full menu. Every other page
carried either a single `← Dashboard` link or nothing at all, so Sessions,
Nodes, Fleet, Audit, Work, AI Usage, Projects, Agents, Global Tasks, Backlog,
Notes, the Terminal Wall and both ops screens were one-way streets with
browser Back as the navigation.

## The shell

`terminal_mcp/dashboard_nav.py` owns the destinations, the markup, the styles
and the active-state rule. `with_global_nav(html, active, layout=...)` splices
them into a page. `dashboard.py` applies it once at import, from one table
(`_GLOBAL_NAV_PAGES`); the two ops modules apply it to their own constant.

Nothing request-controlled is interpolated anywhere in the bar — every byte is
a module constant. The one dynamic element, a page's breadcrumb, is written by
the page's own JavaScript through `window.tmcpBreadcrumb`, which sets
`textContent` and never `innerHTML`.

### Layouts

These pages do not share a layout, and normalising them would have been a
redesign nobody asked for with fourteen chances to break a working screen. The
bar adapts instead:

| layout | used by | why |
| --- | --- | --- |
| `flex` | Home, Sessions, Nodes, Global Tasks, Backlog | `display:flex; flex-direction:column` over a full-height body — the bar is the first flex row |
| `flow` | Projects, Agents, Work, Fleet, Audit, Terminal Wall, Notes, both ops pages | ordinary document flow, `position:sticky` |
| `fixed` | AI Usage | a CSS grid whose children claim explicit cells; an extra auto-placed child would collide with its header, so the bar leaves the flow and the body gets `padding-top` |

## Full route inventory

Every user-facing route under `/dashboard`, as served.

| route | page | in the menu | nav |
| --- | --- | --- | --- |
| `/dashboard` | Live session console | Home | yes |
| `/dashboard/projects` | Projects + project detail | Projects | yes |
| `/dashboard/agents` | Agents | Agents | yes |
| `/dashboard/tasks` | Global Tasks | Tasks | yes |
| `/dashboard/work` | Work runtime / queue / blocked review | Review | yes |
| `/dashboard/sessions` | Session administration | Sessions | yes |
| `/dashboard/nodes` | Nodes / fleet health / onboarding | Nodes | yes |
| `/dashboard/backlog` | Project backlog | Backlog (overflow) | yes |
| `/dashboard/notes` | Notes / ideas | Notes (overflow) | yes |
| `/dashboard/fleet` | Fleet metadata registry | Fleet registry (overflow) | yes |
| `/dashboard/terminal-wall` | Every live pane at once | Terminal wall (overflow) | yes |
| `/dashboard/ai-usage` | Provider quota and spend | AI usage (overflow) | yes |
| `/dashboard/audit` | Audit and access log | Audit (overflow) | yes |
| `/dashboard/ops/dispatch-settings` | Per-project dispatch policy | Dispatch settings (overflow) | yes |
| `/dashboard/ops/novaretail-dispatch` | NovaRetail dispatch monitor | Dispatch monitor (overflow) | yes |
| `/dashboard/requirements` | The requirements document | Requirements (overflow) | served as `text/plain`, so no bar — deliberate |

### Not full pages

| route | why |
| --- | --- |
| `/dashboard/terminal` | The web terminal attaches a browser to one pane's real pty and is opened as a focused window from Sessions. A menu bar over a live terminal steals rows from the thing being watched. It keeps its own explicit link back to Sessions. |
| `/dashboard/assets/{filename}` | static asset |
| `/dashboard/api/*` | JSON |
| `/dashboard/ws/terminal` | WebSocket |

`EMBEDDED_ROUTES` in `dashboard_nav.py` carries the same list, and the
coverage test reads it — so "this route has no nav" is always a decision
somebody wrote down rather than an omission.

### A separate surface

`/app/*` (`webauth_dashboard.py`) is the password-authenticated mobile app,
not the operator dashboard: a different auth model, a different audience and
its own four pages (`/app`, `/app/sessions`, `/app/password`,
`/app/terminal`). It is deliberately out of scope here — giving it the
operator menu would advertise operator routes to an account that may not be
able to open them.

## Behaviour

- **Sticky/fixed** at the top of every page, 38px, one row, never two.
- **Active page** carries `aria-current="page"` and a highlighted border.
- **Overflow panel** (`More ▾`) holds *every* destination, not only the
  secondary ones — on a phone it is the only menu there is, and a panel that
  omitted half the site would be the original complaint in a smaller window.
- **Below 900px** the inline link row collapses into that panel.
- **Breadcrumb** for nested views (project detail, a project's agents), via
  `window.tmcpBreadcrumb([{label, href}, ...])`.
- **Keyboard**: plain `<a>` elements, so Tab and Enter work with scripting
  off; the panel toggle is a `<button>` with `aria-expanded`/`aria-controls`,
  and Escape closes it and returns focus.
- **Auth is unchanged.** Every page keeps the guard it had. Notes still has
  its own boundary on top of the dashboard's and still fires first — the menu
  is not a way to see a page you are not allowed to see.

## Tests

- `tests/test_dashboard_global_nav.py` walks the **live Starlette route
  table** of a real assembled app and fails if a full page is served without
  the marker. Also pins active state, the narrow-screen panel's completeness,
  menu links resolving 200, accessibility attributes, injection safety and the
  unchanged auth guard.
- `tests/test_dashboard_nav_browser_smoke.py` (`-m browser_smoke`) serves the
  real app on loopback and drives Chromium: it *measures* the rendered bar on
  every page at 1366×768, 1920×1080 and 390×844, then clicks a full tour of
  the dashboard without ever calling `go_back()`.

Adding a page means adding one line to `DESTINATIONS` and one line to
`_GLOBAL_NAV_PAGES`. Forgetting either fails the coverage test.
