"""ONE global navigation shell, shared by every full-page dashboard screen.

WHY THIS MODULE EXISTS. Every full page in this project is a module-level
HTML constant that was written on its own -- so each one grew whatever header
its author happened to need. Some had three ad-hoc links, most had none, and
the ones with none could only be left by pressing the browser's Back button.
An operator who opened /dashboard/agents from a link had no way to reach
Projects, Nodes or Sessions at all.

The fix is deliberately NOT "paste the same menu into fifteen strings". It is
one component, injected. `apply()` takes a page's existing HTML and returns it
with the shared stylesheet before </head> and the shared bar after <body>. A
page does not opt in to the menu, does not describe the menu, and cannot get a
stale copy of the menu: it only says which entry is active and what its own
breadcrumb trail is.

THE TEST IS THE POINT. tests/test_global_nav.py walks the real route table of
a real app, fetches every full-page HTML route, and fails unless the response
carries NAV_MARKER. A page added later without the shell is a failing test,
not a bug report from a user pressing Back.

TWO SURFACES, ONE COMPONENT. The /dashboard/* pages and the webauth /app/*
pages sit behind different authentication (Cloudflare Access vs. a session
cookie), so a single menu spanning both would offer every /app user links
they cannot open and vice versa. They therefore share this component and this
stylesheet but carry their own destination list -- see DASHBOARD_NAV and
APP_NAV. That is a real boundary, not a second implementation.

SECURITY. Nothing here is per-request or caller-supplied: the destinations are
module constants and breadcrumb text is escaped on the way in, so the injected
markup is as static as the page it joins. The CSP this project already sets
(script-src/style-src 'self' 'unsafe-inline') covers the inline <style>; the
menu needs no script at all -- the mobile disclosure is a real checkbox, which
is why it is keyboard-operable with no JavaScript to load or fail.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape

NAV_MARKER = "data-global-nav"
"""The attribute the route-coverage test looks for. Present on exactly one
element per page -- the nav container itself."""

MOBILE_MARKER = "tmcp-nav__burger"
"""The mobile disclosure control. Asserted separately by the coverage test so
"the menu is present" cannot silently mean "present but unreachable on a
phone"."""


@dataclass(frozen=True)
class NavItem:
    """One destination. `key` is what a page passes as its active entry."""
    key: str
    label: str
    href: str
    #: Shown in the overflow group rather than the primary bar. The bar has to
    #: stay legible at 1366x768, which is the narrowest desktop this fleet is
    #: actually used on.
    secondary: bool = False


@dataclass(frozen=True)
class NavSurface:
    """A set of destinations that share one authentication boundary."""
    brand: str
    home: str
    items: tuple[NavItem, ...] = field(default_factory=tuple)

    def item(self, key: str) -> NavItem | None:
        return next((entry for entry in self.items if entry.key == key), None)

    @property
    def primary(self) -> tuple[NavItem, ...]:
        return tuple(entry for entry in self.items if not entry.secondary)

    @property
    def secondary(self) -> tuple[NavItem, ...]:
        return tuple(entry for entry in self.items if entry.secondary)


# The Cloudflare-Access-gated fleet dashboard. Order is the operator's normal
# path through the system -- what is being built, who is building it, what
# work exists, and only then the machines it runs on.
DASHBOARD_NAV = NavSurface(
    brand="Terminal MCP",
    home="/dashboard",
    items=(
        NavItem("home", "Home", "/dashboard"),
        NavItem("projects", "Projects", "/dashboard/projects"),
        NavItem("agents", "Agents", "/dashboard/agents"),
        NavItem("tasks", "Global Tasks", "/dashboard/tasks"),
        NavItem("sessions", "Sessions", "/dashboard/sessions"),
        NavItem("nodes", "Nodes", "/dashboard/nodes"),
        NavItem("work", "Queue / Review", "/dashboard/work"),
        NavItem("terminal-wall", "Terminals", "/dashboard/terminal-wall"),
        # Everything below is real and reachable, just not every-day. Grouped
        # so the primary bar still fits on a 1366px screen without wrapping.
        NavItem("backlog", "Backlog", "/dashboard/backlog", secondary=True),
        NavItem("notes", "Notes", "/dashboard/notes", secondary=True),
        NavItem("fleet", "Fleet", "/dashboard/fleet", secondary=True),
        NavItem("ai-usage", "AI Usage", "/dashboard/ai-usage", secondary=True),
        NavItem("audit", "Audit", "/dashboard/audit", secondary=True),
        NavItem("novaretail-dispatch", "Dispatch Ops",
                "/dashboard/ops/novaretail-dispatch", secondary=True),
        NavItem("dispatch-settings", "Dispatch Settings",
                "/dashboard/ops/dispatch-settings", secondary=True),
        NavItem("requirements", "Requirements", "/dashboard/requirements", secondary=True),
        NavItem("terminal", "Web Terminal", "/dashboard/terminal", secondary=True),
    ),
)

# The password-authenticated /app surface. A much smaller world on purpose:
# these are the only pages a webauth session can open.
APP_NAV = NavSurface(
    brand="Terminal MCP",
    home="/app",
    items=(
        NavItem("app-home", "Home", "/app"),
        NavItem("app-sessions", "Sessions", "/app/sessions"),
        NavItem("app-terminal", "Terminal", "/app/terminal"),
        NavItem("app-password", "Password", "/app/password", secondary=True),
    ),
)


NAV_CSS = """
<style id="tmcp-global-nav-css">
/* The shared global navigation shell. Scoped entirely under .tmcp-nav* so it
   cannot restyle the page it is injected into -- the pages here were written
   independently and several use bare element selectors. */
:root { --tmcp-nav-h: 44px; }
.tmcp-nav, .tmcp-crumbs { box-sizing: border-box; font-family: ui-sans-serif, system-ui,
  -apple-system, "Segoe UI", Roboto, sans-serif; }
.tmcp-nav *, .tmcp-crumbs * { box-sizing: border-box; }
.tmcp-nav {
  position: sticky; top: 0; z-index: 9999;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  min-height: var(--tmcp-nav-h); padding: 0 12px;
  background: #0d1424; border-bottom: 1px solid #1f2a44; color: #e6ecf8;
}
.tmcp-nav__brand {
  font-weight: 700; font-size: 13px; letter-spacing: .02em;
  color: #e6ecf8; text-decoration: none; white-space: nowrap; padding: 6px 0;
}
.tmcp-nav__brand:hover { color: #fff; }
.tmcp-nav__menu {
  display: flex; align-items: center; gap: 2px; flex-wrap: wrap;
  list-style: none; margin: 0; padding: 0; flex: 1 1 auto; min-width: 0;
}
.tmcp-nav__link {
  display: inline-block; padding: 6px 9px; border-radius: 6px;
  font-size: 12px; line-height: 1.2; white-space: nowrap;
  color: #9fb0d0; text-decoration: none; border: 1px solid transparent;
}
.tmcp-nav__link:hover { background: #17203a; color: #e6ecf8; }
/* Active page state. Both a colour and a shape change, so it survives a
   high-contrast or forced-colours mode that drops the background. */
.tmcp-nav__link.is-active {
  background: #17203a; color: #fff; font-weight: 700;
  border-color: #2b3a5e; box-shadow: inset 0 -2px 0 #4c8dff;
}
.tmcp-nav__link:focus-visible, .tmcp-nav__burger:focus-visible,
.tmcp-nav__more > summary:focus-visible, .tmcp-crumbs a:focus-visible {
  outline: 2px solid #4c8dff; outline-offset: 1px;
}
/* Overflow group. <details> rather than a scripted menu: it opens on Enter
   and Space natively and needs no JavaScript to be usable. */
.tmcp-nav__more { position: relative; }
.tmcp-nav__more > summary {
  list-style: none; cursor: pointer;
  display: inline-block; padding: 6px 9px; border-radius: 6px;
  font-size: 12px; color: #9fb0d0; border: 1px solid transparent; white-space: nowrap;
}
.tmcp-nav__more > summary::-webkit-details-marker { display: none; }
.tmcp-nav__more > summary:hover { background: #17203a; color: #e6ecf8; }
.tmcp-nav__more[open] > summary { background: #17203a; color: #e6ecf8; }
.tmcp-nav__more.has-active > summary {
  color: #fff; font-weight: 700; box-shadow: inset 0 -2px 0 #4c8dff;
}
.tmcp-nav__drawer {
  position: absolute; right: 0; top: calc(100% + 4px); z-index: 10000;
  display: flex; flex-direction: column; gap: 2px; min-width: 190px;
  margin: 0; padding: 6px; list-style: none;
  background: #0d1424; border: 1px solid #2b3a5e; border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, .5);
}
.tmcp-nav__drawer .tmcp-nav__link { display: block; white-space: nowrap; }
/* The mobile disclosure IS the checkbox -- appearance:none keeps the glyph,
   the native control keeps Space/Enter and the focus ring for free. */
.tmcp-nav__toggle-label { display: none; }
.tmcp-nav__burger {
  display: none; appearance: none; -webkit-appearance: none;
  width: 34px; height: 30px; margin: 0; cursor: pointer;
  background: transparent; border: 1px solid #2b3a5e; border-radius: 6px;
  color: #e6ecf8; font-size: 15px; line-height: 28px; text-align: center;
}
.tmcp-nav__burger::before { content: "\\2630"; }
.tmcp-nav__burger:checked::before { content: "\\2715"; }
.tmcp-nav__burger:hover { background: #17203a; }
/* Breadcrumbs. Nested trail, separate row, never competing with the bar. */
.tmcp-crumbs {
  /* The bar is a <div role="navigation"> precisely so a page's bare `nav {}`
     rule cannot reach it -- but the breadcrumb IS a <nav>, and the same rules
     do reach it. AI Usage styles `nav` as a column-flex sidebar with a right
     border, which stacked the trail vertically. Every property the trail's
     own layout depends on is therefore stated here rather than inherited. */
  display: flex; flex-direction: row; align-items: center; gap: 6px; flex-wrap: wrap;
  padding: 5px 12px; margin: 0; font-size: 11px; color: #7e8db0;
  background: #0a1020; border: 0; border-bottom: 1px solid #1f2a44;
  overflow: visible;
}
.tmcp-crumbs ol { display: contents; list-style: none; margin: 0; padding: 0; }
.tmcp-crumbs a { color: #9fb0d0; text-decoration: none; }
.tmcp-crumbs a:hover { color: #e6ecf8; text-decoration: underline; }
.tmcp-crumbs li + li::before { content: "/"; margin: 0 6px 0 0; color: #3d4b6b; }
.tmcp-crumbs [aria-current="page"] { color: #e6ecf8; font-weight: 600; }
/* 1366x768 is the narrowest desktop this fleet actually runs on: tighten
   rather than collapse, so the primary bar still shows every destination. */
@media (max-width: 1400px) {
  .tmcp-nav__link, .tmcp-nav__more > summary { padding: 6px 7px; font-size: 11.5px; }
  .tmcp-nav { gap: 8px; }
}
@media (max-width: 860px) {
  .tmcp-nav { flex-wrap: nowrap; }
  .tmcp-nav__burger { display: inline-block; order: 3; margin-left: auto; }
  .tmcp-nav__menu {
    display: none; order: 4; flex: 1 0 100%; flex-direction: column;
    align-items: stretch; gap: 2px; padding: 6px 0 10px;
  }
  .tmcp-nav__burger:checked ~ .tmcp-nav__menu { display: flex; }
  .tmcp-nav__link { padding: 9px 10px; font-size: 13px; }
  .tmcp-nav__more { width: 100%; }
  .tmcp-nav__more > summary { padding: 9px 10px; font-size: 13px; }
  .tmcp-nav__drawer { position: static; min-width: 0; border: 0; box-shadow: none;
    padding: 2px 0 2px 12px; background: transparent; }
}
@media print { .tmcp-nav, .tmcp-crumbs { display: none; } }
</style>
"""


def _link(item: NavItem, active: str) -> str:
    is_active = item.key == active
    classes = "tmcp-nav__link is-active" if is_active else "tmcp-nav__link"
    # aria-current is the part a screen reader announces; the class is the
    # part the coverage test and the eye both read.
    current = ' aria-current="page"' if is_active else ""
    return (f'<li><a class="{classes}" href="{escape(item.href, quote=True)}"{current}>'
            f'{escape(item.label)}</a></li>')


def render(surface: NavSurface, active: str = "",
           breadcrumbs: tuple[tuple[str, str | None], ...] = ()) -> str:
    """The shared bar, plus a breadcrumb row when the page supplies a trail.

    `breadcrumbs` is a sequence of (label, href-or-None); the last entry is
    the current page and is never a link.
    """
    primary = "".join(_link(item, active) for item in surface.primary)
    overflow = surface.secondary
    more = ""
    if overflow:
        active_in_more = any(item.key == active for item in overflow)
        drawer = "".join(_link(item, active) for item in overflow)
        more_class = "tmcp-nav__more has-active" if active_in_more else "tmcp-nav__more"
        label = next((item.label for item in overflow if item.key == active), "System")
        more = (f'<li><details class="{more_class}">'
                f'<summary aria-label="More destinations">{escape(label)} ▾</summary>'
                f'<ul class="tmcp-nav__drawer">{drawer}</ul>'
                f'</details></li>')

    bar = (
        f'<div class="tmcp-nav" {NAV_MARKER} role="navigation" aria-label="Global">'
        f'<a class="tmcp-nav__brand" href="{escape(surface.home, quote=True)}">'
        f'{escape(surface.brand)}</a>'
        # The checkbox precedes the menu so the CSS sibling selector can open
        # it; aria-label carries the accessible name the glyph cannot.
        f'<input type="checkbox" class="{MOBILE_MARKER}" id="tmcp-nav-toggle"'
        f' aria-label="Toggle navigation menu" aria-controls="tmcp-nav-menu">'
        f'<ul class="tmcp-nav__menu" id="tmcp-nav-menu">{primary}{more}</ul>'
        f'</div>'
    )

    if not breadcrumbs:
        return bar
    crumbs = []
    last = len(breadcrumbs) - 1
    for index, (label, href) in enumerate(breadcrumbs):
        text = escape(str(label))
        if href and index != last:
            crumbs.append(f'<li><a href="{escape(str(href), quote=True)}">{text}</a></li>')
        else:
            crumbs.append(f'<li aria-current="page">{text}</li>')
    trail = (f'<nav class="tmcp-crumbs" aria-label="Breadcrumb">'
             f'<ol>{"".join(crumbs)}</ol></nav>')
    return bar + trail


_HEAD_CLOSE = re.compile(r"</head\s*>", re.IGNORECASE)
_BODY_OPEN = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
# Several pages here are written as tag soup -- a doctype, a <title>, a
# <style> and then content, with <head>/<body> left implied. That is valid
# HTML5 and the browser builds both elements itself, so the only thing missing
# is somewhere obvious to inject. These find the end of the document's
# preamble; anything placed after it lands in the implied <head> if it is a
# <style> and opens the implied <body> if it is not, which is exactly what is
# wanted.
_TITLE_CLOSE = re.compile(r"</title\s*>", re.IGNORECASE)
_DOCTYPE = re.compile(r"<!doctype[^>]*>", re.IGNORECASE)


def apply(html: str, surface: NavSurface, active: str = "",
          breadcrumbs: tuple[tuple[str, str | None], ...] = ()) -> str:
    """Return `html` with the shared stylesheet and bar injected.

    Idempotent: a page that already carries the marker is returned unchanged,
    so double-wrapping a route cannot produce two menus.

    Works on a page with explicit <head>/<body> and on one written as tag soup
    (doctype, title, style, content -- both elements implied). Several pages
    in this project are the second kind, and the route-coverage test found
    them by failing on them, which is what it is for. An empty string is
    returned unchanged: there is no page there to decorate.
    """
    if not html or NAV_MARKER in html:
        return html
    if not breadcrumbs:
        breadcrumbs = default_breadcrumbs(surface, active)
    bar = render(surface, active, breadcrumbs)

    head = _HEAD_CLOSE.search(html)
    body = _BODY_OPEN.search(html)
    if head is not None and body is not None:
        # Inject tail-first so the earlier match offset stays valid.
        html = html[:body.end()] + bar + html[body.end():]
        return html[:head.start()] + NAV_CSS + html[head.start():]

    # Implied <head>/<body>. Insert both pieces together after the preamble:
    # the stylesheet is still the last thing before content, so it is parsed
    # into the implied head, and the bar is the first content, so it is the
    # first thing in the implied body.
    preamble = _TITLE_CLOSE.search(html) or _DOCTYPE.search(html)
    at = preamble.end() if preamble is not None else 0
    return html[:at] + NAV_CSS + bar + html[at:]


def default_breadcrumbs(surface: NavSurface,
                        active: str) -> tuple[tuple[str, str | None], ...]:
    """Home / <section> for an ordinary page; just Home on the home page.

    A page with a deeper trail (a single project, one agent) passes its own.
    """
    item = surface.item(active)
    home = surface.item("home") or surface.item("app-home")
    if item is None or home is None:
        return ()
    if item.key == home.key:
        return ((home.label, None),)
    return ((home.label, home.href), (item.label, None))
