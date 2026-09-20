"""ONE global navigation shell, injected into every dashboard full page.

THE COMPLAINT THIS ANSWERS

"Several dashboard screens hide the main menu; once you enter one you have to
press browser Back before you can see the menu again." That was literally
true. `/dashboard` carried the whole menu; every other page carried either a
single `← Dashboard` link or nothing at all, so Sessions, Nodes, Fleet, Audit,
Work, AI Usage, Projects, Agents, Global Tasks, Backlog, Notes and the Terminal
Wall were all one-way streets. Browser Back was the navigation.

WHY A SHARED COMPONENT AND NOT FOURTEEN COPIES

The menu was hand-copied before, which is why it existed in exactly one place:
copying it fourteen more times would have meant fourteen places to forget the
next time a page is added. So this module owns the destinations, the markup,
the styles and the active-state rule, and `with_global_nav` splices them into
a page's existing HTML. Adding a page means adding one line to DESTINATIONS
and one call; `tests/test_dashboard_global_nav.py` fails if a full page route
exists without one.

INJECTION SAFETY

Every byte of the rendered nav is a module constant. No request data, no
session name, no query string and no database value is interpolated anywhere,
so there is no injection surface to escape. The one piece of dynamic content
-- a page's breadcrumb -- is written from the page's own JavaScript through
`window.tmcpBreadcrumb`, which sets `textContent` and never `innerHTML`.

LAYOUT, WITHOUT REWRITING FOURTEEN PAGES

These pages do not share a layout. Some are `display:flex; flex-direction:
column` over a full-height body, one is a CSS grid with explicitly-placed
children, and the rest are ordinary document flow. Rather than normalise them
-- a redesign nobody asked for, with fourteen chances to break a working
screen -- the nav adapts:

    LAYOUT_FLEX   the bar is the first flex child; the page's own rows follow
    LAYOUT_FLOW   the bar sits at the top of the document and sticks there
    LAYOUT_FIXED  the bar is taken out of flow and the body gets padding-top

FIXED is for the grid page, whose children claim explicit grid cells that an
extra auto-placed child would collide with. It is the most invasive of the
three and therefore the one used least.
"""
from __future__ import annotations

import re
from typing import NamedTuple

#: Height of the bar, in px. Compact on purpose -- this is chrome on an
#: operations screen, and every pixel it takes is a pixel of session output
#: somebody is not reading.
NAV_HEIGHT_PX = 38

LAYOUT_FLEX = "flex"
LAYOUT_FLOW = "flow"
LAYOUT_FIXED = "fixed"

#: The marker the route-coverage test looks for. Changing it is fine;
#: changing it in only one of the two places is not, which is why the test
#: imports this constant rather than spelling the string again.
NAV_MARKER = "tmcp-global-nav"


class Destination(NamedTuple):
    """One place the menu can take you."""
    key: str
    label: str
    href: str
    #: Primary destinations stay visible on a wide screen. Secondary ones move
    #: into the overflow menu there -- still one click, never a dead end, and
    #: never behind browser Back.
    primary: bool = True
    #: Short hint, shown as a tooltip. Never rendered as HTML.
    title: str = ""
    #: Does this destination serve an HTML page that CAN carry the bar? The
    #: requirements doc is served as text/plain on purpose (no markdown
    #: dependency, no injection surface), so it is a real destination with no
    #: menu of its own. Linked anyway -- a reader gets there in one click and
    #: leaves with Back, which is the one place Back is the right answer.
    renders_nav: bool = True


#: THE INFORMATION ARCHITECTURE. Projects and Agents lead, because that is the
#: layer an operator thinks in; the session/node machinery stays directly
#: reachable but second, which is the ordering the audit asked for.
DESTINATIONS: tuple[Destination, ...] = (
    Destination("home", "Home", "/dashboard", True, "Live session console"),
    Destination("projects", "Projects", "/dashboard/projects", True,
                "Projects, phases, PM and specialist teams"),
    Destination("agents", "Agents", "/dashboard/agents", True,
                "Durable agents, their skills and their work"),
    Destination("tasks", "Tasks", "/dashboard/tasks", True,
                "Every task in the fleet, in one board"),
    Destination("work", "Review", "/dashboard/work", True,
                "Work runtime: queue, inbox and blocked review"),
    Destination("sessions", "Sessions", "/dashboard/sessions", True,
                "Session administration and access grants"),
    Destination("nodes", "Nodes", "/dashboard/nodes", True,
                "Fleet nodes, health and onboarding"),
    Destination("backlog", "Backlog", "/dashboard/backlog", False,
                "Project backlog and planning"),
    Destination("notes", "Notes", "/dashboard/notes", False, "Notes and ideas"),
    Destination("fleet", "Fleet registry", "/dashboard/fleet", False,
                "Fleet metadata registry"),
    Destination("wall", "Terminal wall", "/dashboard/terminal-wall", False,
                "Every live pane at once"),
    Destination("ai-usage", "AI usage", "/dashboard/ai-usage", False,
                "Provider quota and spend"),
    Destination("audit", "Audit", "/dashboard/audit", False, "Audit and access log"),
    Destination("dispatch-settings", "Dispatch settings", "/dashboard/ops/dispatch-settings",
                False, "Per-project dispatch policy"),
    Destination("dispatch-monitor", "Dispatch monitor", "/dashboard/ops/novaretail-dispatch",
                False, "NovaRetail dispatch monitor"),
    Destination("requirements", "Requirements", "/dashboard/requirements", False,
                "The requirements document, as served", renders_nav=False),
)

DESTINATION_KEYS = frozenset(d.key for d in DESTINATIONS)

#: Pages that are deliberately NOT full pages, with the reason. Documented
#: rather than quietly skipped -- the coverage test reads this list, so
#: "this route has no nav" is always an answer somebody wrote down.
EMBEDDED_ROUTES: dict[str, str] = {
    "/dashboard/terminal": (
        "the web terminal attaches a browser to one pane's real pty and is "
        "opened as a focused window from Sessions; a menu bar over a live "
        "terminal steals rows from the thing being watched. It keeps its own "
        "explicit link back to Sessions."),
    "/dashboard/assets/{filename}": "static asset, not a page",
    "/dashboard/api/*": "JSON, not a page",
}


def _css() -> str:
    return f"""
<style id="tmcp-global-nav-style">
  :root {{ --tmcp-nav-h: {NAV_HEIGHT_PX}px; }}
  /* DEFENSIVE RESET, not decoration. This bar is a <nav>, and a page that
     styles the bare `nav` element wins over nothing -- it only loses to a
     class. AI Usage really does this: its own sidebar rule sets grid-row 2,
     display flex, flex-direction column, overflow-y auto and a right border
     on the bare element -- and every one of those leaked in. The bar came out
     as two centred rows with its overflow panel clipped away. So every
     property the layout depends on is stated here rather than left to luck. */
  .{NAV_MARKER} {{
    box-sizing: border-box; display: flex; flex-direction: row; flex-wrap: nowrap;
    align-items: center; justify-content: flex-start; gap: 4px;
    height: var(--tmcp-nav-h); min-height: var(--tmcp-nav-h); max-height: var(--tmcp-nav-h);
    width: auto; max-width: none; margin: 0; padding: 0 8px;
    grid-row: auto; grid-column: auto; overflow: visible;
    background: #0e1526; border: 0; border-bottom: 1px solid #26324b;
    box-shadow: none; text-align: left;
    font: 12px/1.2 ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace;
    color: #9aa7bd; z-index: 2147483000;
  }}
  .{NAV_MARKER}.tmcp-nav-flow {{ position: sticky; top: 0; }}
  .{NAV_MARKER}.tmcp-nav-flex {{ flex: 0 0 auto; }}
  .{NAV_MARKER}.tmcp-nav-fixed {{ position: fixed; top: 0; left: 0; right: 0; }}
  body.tmcp-nav-offset {{ box-sizing: border-box; padding-top: var(--tmcp-nav-h); }}
  .{NAV_MARKER} a {{ color: #9aa7bd; text-decoration: none; }}
  .tmcp-nav-brand {{
    font-weight: 700; color: #eef2ff !important; padding: 0 8px 0 2px;
    white-space: nowrap; letter-spacing: .02em;
  }}
  .tmcp-nav-links {{
    display: flex; flex-direction: row; align-items: center; gap: 2px;
    min-width: 0; flex: 1 1 auto; overflow: visible; padding: 0; margin: 0;
  }}
  .tmcp-nav-links a {{
    display: inline-flex; align-items: center; width: auto; min-height: 0;
    margin: 0; padding: 5px 9px; border-radius: 7px; white-space: nowrap;
    border: 1px solid transparent; background: none; box-shadow: none;
    text-align: left; font: inherit;
  }}
  .tmcp-nav-links a:hover, .tmcp-nav-links a:focus-visible {{
    background: #17203a; color: #eef2ff; outline: none; border-color: #26324b;
  }}
  .tmcp-nav-links a[aria-current="page"] {{
    background: #17203a; color: #eef2ff; border-color: #3b78ff; font-weight: 700;
  }}
  .tmcp-nav-more {{ position: relative; }}
  .tmcp-nav-btn {{
    display: inline-flex; align-items: center; width: auto; min-height: 0;
    margin: 0; background: none; border: 1px solid #26324b; color: #9aa7bd;
    cursor: pointer; border-radius: 7px; padding: 5px 9px; font: inherit;
    white-space: nowrap; text-align: center; box-shadow: none;
  }}
  .tmcp-nav-btn:hover, .tmcp-nav-btn:focus-visible {{ color: #eef2ff; border-color: #3b78ff; outline: none; }}
  .tmcp-nav-panel {{
    display: none; position: absolute; right: 0; top: calc(100% + 4px);
    background: #121a2d; border: 1px solid #26324b; border-radius: 10px;
    padding: 6px; min-width: 190px; max-height: 70vh; overflow: auto;
    box-shadow: 0 10px 30px rgba(0,0,0,.45);
  }}
  .tmcp-nav-panel.open {{ display: block; }}
  .tmcp-nav-panel a {{
    display: block; width: auto; min-height: 0; margin: 0; padding: 7px 10px;
    border-radius: 7px; white-space: nowrap; background: none; border: 0;
    box-shadow: none; text-align: left; font: inherit;
  }}
  .tmcp-nav-panel a:hover, .tmcp-nav-panel a:focus-visible {{ background: #17203a; color: #eef2ff; outline: none; }}
  .tmcp-nav-panel a[aria-current="page"] {{ color: #eef2ff; font-weight: 700; }}
  .tmcp-crumb {{
    display: flex; align-items: center; gap: 6px; margin: 0 0 0 6px; padding: 0;
    list-style: none; min-width: 0; overflow: hidden;
  }}
  .tmcp-crumb li {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 30ch; }}
  .tmcp-crumb li + li::before {{ content: "/"; margin-right: 6px; color: #56627d; }}
  .tmcp-crumb a {{ color: #9aa7bd; }}
  .tmcp-crumb a:hover {{ color: #eef2ff; }}
  .tmcp-crumb [aria-current="page"] {{ color: #eef2ff; }}
  /* 1366x768 is the smallest desktop this fleet is driven from, and the
     seven primary destinations have to survive it without wrapping. Below
     that the whole set moves into the overflow panel rather than growing a
     second row -- the bar is chrome, and chrome does not get two rows. */
  @media (max-width: 900px) {{
    .tmcp-nav-links {{ display: none; }}
    .{NAV_MARKER} .tmcp-nav-more {{ margin-left: auto; }}
    .tmcp-nav-panel {{ min-width: 220px; }}
    .tmcp-nav-panel .tmcp-nav-narrow-only {{ display: block; }}
  }}
  @media (min-width: 901px) {{
    .tmcp-nav-panel .tmcp-nav-narrow-only {{ display: none; }}
  }}
  @media (prefers-reduced-motion: no-preference) {{
    .tmcp-nav-links a, .tmcp-nav-btn {{ transition: background .12s ease, border-color .12s ease; }}
  }}
</style>
"""


def _link(destination: Destination, active: str, *, narrow_only: bool = False) -> str:
    current = ' aria-current="page"' if destination.key == active else ""
    klass = ' class="tmcp-nav-narrow-only"' if narrow_only else ""
    title = f' title="{destination.title}"' if destination.title else ""
    return f'<a href="{destination.href}"{current}{title}{klass}>{destination.label}</a>'


def nav_html(active: str = "") -> str:
    """The bar itself. Pure constants -- see this module's injection note."""
    primary = "".join(_link(d, active) for d in DESTINATIONS if d.primary)
    # The panel carries EVERY destination, not just the secondary ones: on a
    # narrow screen it is the only menu there is, and a menu that omits half
    # the site on a phone is the original complaint in a smaller window.
    overflow = "".join(
        _link(d, active, narrow_only=d.primary) for d in DESTINATIONS)
    return f"""
<nav class="{NAV_MARKER}" aria-label="Main navigation" data-tmcp-active="{active}">
  <a class="tmcp-nav-brand" href="/dashboard">Terminal&nbsp;MCP</a>
  <div class="tmcp-nav-links">{primary}</div>
  <ol class="tmcp-crumb" id="tmcpCrumb" aria-label="Breadcrumb"></ol>
  <div class="tmcp-nav-more">
    <button type="button" class="tmcp-nav-btn" id="tmcpNavMore"
            aria-expanded="false" aria-haspopup="true" aria-controls="tmcpNavPanel">More ▾</button>
    <div class="tmcp-nav-panel" id="tmcpNavPanel" role="menu">{overflow}</div>
  </div>
</nav>
<script id="tmcp-global-nav-script">
(function () {{
  var button = document.getElementById('tmcpNavMore');
  var panel = document.getElementById('tmcpNavPanel');
  if (button && panel) {{
    var close = function () {{
      panel.classList.remove('open');
      button.setAttribute('aria-expanded', 'false');
    }};
    button.addEventListener('click', function (event) {{
      event.stopPropagation();
      var open = panel.classList.toggle('open');
      button.setAttribute('aria-expanded', open ? 'true' : 'false');
    }});
    document.addEventListener('click', function (event) {{
      if (!panel.contains(event.target)) close();
    }});
    // Escape closes it and returns focus to the control that opened it --
    // otherwise a keyboard user is stranded inside a panel they cannot see.
    document.addEventListener('keydown', function (event) {{
      if (event.key === 'Escape' && panel.classList.contains('open')) {{
        close();
        button.focus();
      }}
    }});
  }}
  // Breadcrumb for a nested view. textContent only: a project name, a session
  // name and a branch are all user-controlled strings, and this bar is on
  // every page.
  window.tmcpBreadcrumb = function (trail) {{
    var list = document.getElementById('tmcpCrumb');
    if (!list) return;
    list.textContent = '';
    (trail || []).forEach(function (step, index) {{
      var item = document.createElement('li');
      var last = index === (trail.length - 1);
      if (step && step.href && !last) {{
        var link = document.createElement('a');
        link.href = step.href;
        link.textContent = String(step.label == null ? '' : step.label);
        item.appendChild(link);
      }} else {{
        item.textContent = String((step && step.label) == null ? '' : step.label);
        if (last) item.setAttribute('aria-current', 'page');
      }}
      list.appendChild(item);
    }});
  }};
}})();
</script>
"""


def with_global_nav(html: str, active: str = "", *, layout: str = LAYOUT_FLOW) -> str:
    """Splice the shared bar into one page's HTML.

    Idempotent: a page that already carries the marker is returned unchanged,
    so a module imported twice cannot stack two menus.
    """
    if NAV_MARKER in html:
        return html
    if active and active not in DESTINATION_KEYS:
        raise ValueError(f"unknown nav destination {active!r}; add it to DESTINATIONS")
    if layout not in (LAYOUT_FLEX, LAYOUT_FLOW, LAYOUT_FIXED):
        raise ValueError(f"unknown nav layout {layout!r}")

    style = _css()
    if "</head>" in html:
        html = html.replace("</head>", style + "</head>", 1)
    else:
        # No explicit head either. A <style> before the first content element
        # is still in the implied head as far as the parser is concerned, and
        # putting it at the very front would sit above <!doctype html>.
        anchor = re.search(r"<(?:main|h1|div|section|article|body)\b", html, re.IGNORECASE)
        html = (html[:anchor.start()] + style + html[anchor.start():]) if anchor else style + html

    bar = nav_html(active).replace(f'class="{NAV_MARKER}"',
                                   f'class="{NAV_MARKER} tmcp-nav-{layout}"', 1)

    match = re.search(r"<body[^>]*>", html, re.IGNORECASE)
    if match is None:
        # A fragment page -- the two small ops dashboards are written as
        # `<!doctype html><meta ...><style>...</style><main>...`, with no
        # explicit body tag at all. The browser still builds one, so the bar
        # just has to become the first thing inside it: immediately before the
        # first content element rather than appended at the end, where it
        # would render underneath the page it is supposed to head.
        content = re.search(r"<(?:main|h1|div|section|article)\b", html, re.IGNORECASE)
        if content is None:  # pragma: no cover -- no content to head
            return html + bar
        return html[:content.start()] + bar + html[content.start():]
    body_tag = match.group(0)
    if layout == LAYOUT_FIXED:
        # The bar is out of flow, so the body has to make room for it. Done as
        # a class rather than an inline style so the page's own stylesheet can
        # still win if it ever needs to.
        if 'class="' in body_tag:
            body_tag = body_tag.replace('class="', 'class="tmcp-nav-offset ', 1)
        else:
            body_tag = body_tag[:-1].rstrip() + ' class="tmcp-nav-offset">'
    return html[:match.start()] + body_tag + bar + html[match.end():]


def full_page_routes() -> tuple[str, ...]:
    """Every dashboard route that must carry the bar."""
    return tuple(d.href for d in DESTINATIONS if d.renders_nav)
