from __future__ import annotations

import logging
from urllib.parse import urlparse

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .cf_access import verify_access_assertion
from .core import TerminalService
from .permissions import input_session_allowed, session_allowed
from .supervisor import SupervisorService, SupervisorStore
from .supervisor2 import SupervisorV2Service, build_supervisor_v2

_log = logging.getLogger(__name__)


INPUT_ERROR_STATUS = {
    "ACCESS_DENIED": 403,
    "INPUT_DISABLED": 403,
    "SENSITIVE_TARGET": 403,
    "SESSION_NOT_FOUND": 404,
    "GRANT_REQUIRED": 403,
    "READ_GRANT_REQUIRED": 403,
    "READ_RESTRICTED": 403,
    "IDENTITY_MISMATCH": 403,
    "SENSITIVE_SESSION_NOT_GRANTABLE": 403,
    "INVALID_SESSION": 400,
}


DASHBOARD_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content">
  <title>Terminal MCP Sessions</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --green:#43d17c; --amber:#ffc857; --term-bg:#0a0e1a; --mono: ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace; }
    * { box-sizing:border-box }
    /* True app-shell: the page itself never scrolls (100dvh — the *dynamic*
       viewport height — tracks a mobile browser showing/hiding its own
       chrome, unlike 100vh, which can be taller than what's actually
       visible; `height:100vh` first is a fallback for browsers without dvh
       support). `body` is a flex column so `header` keeps its natural
       height and `main` gets exactly what's left — no more hardcoded
       "75px header" magic number, so this stays correct however tall the
       header actually is (including the shrunk mobile header below).
       Only elements that opt into their own `overflow:auto` (the sessions
       list, #output) ever scroll; everything else is bounded by this shell. */
    html, body { height:100vh; height:100dvh; overflow:hidden }
    body { margin:0; font:14px/1.5 var(--mono); background:var(--bg); color:var(--text); display:flex; flex-direction:column }
    header { flex:0 0 auto; display:flex; justify-content:space-between; gap:16px; align-items:center; padding:22px 28px; border-bottom:1px solid var(--line) }
    h1 { margin:0; font-size:20px } .muted { color:var(--muted) } .live { color:var(--green) }
    .live.reconnecting { color:var(--amber) } .live.offline { color:#ff6b6b }
    .live.auth-required { color:#ffb347 }
    .header-right { display:flex; align-items:center; gap:10px }
    /* Compact by default (hidden entirely — see JS — when there are zero
       watches, so a supervisor.enabled:false deployment shows nothing extra
       at all); only turns amber/noticeable when something actually needs
       attention, otherwise a quiet muted count. */
    .supervisor-badge { background:transparent; border:1px solid var(--line); color:var(--muted); border-radius:999px; padding:4px 10px; font:12px var(--mono); cursor:pointer; white-space:nowrap }
    .supervisor-badge.attention { border-color:var(--amber); color:var(--amber) }
    #supervisorPanel { display:none; position:fixed; right:16px; top:64px; z-index:25; width:min(360px, calc(100vw - 32px)); max-height:70vh; overflow:auto; background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:0 20px 50px rgba(0,0,0,.6) }
    body.supervisor-visible #supervisorPanel { display:block }
    body.supervisor-visible #supervisorBackdrop { display:block; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:20 }
    #supervisorPanel .sp-head { padding:12px 14px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center }
    #supervisorPanel .sp-counts { display:flex; flex-wrap:wrap; gap:6px; padding:10px 14px }
    #supervisorPanel .sp-count { font-size:11px; padding:2px 8px; border-radius:999px; border:1px solid var(--line); color:var(--muted) }
    #supervisorPanel .sp-count.nonzero { color:var(--text); border-color:#344360 }
    #supervisorPanel .sp-count.attn { color:var(--amber); border-color:var(--amber) }
    #supervisorPanel .sp-events { padding:0 14px 12px }
    #supervisorPanel .sp-event { padding:8px 0; border-top:1px solid var(--line); font-size:12px }
    #supervisorPanel .sp-event .sp-target { font-weight:700 }
    #supervisorPanel .sp-event button { margin-top:4px; background:#19243b; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:3px 8px; font:11px var(--mono); cursor:pointer }
    #supervisorPanel .sp-empty { padding:10px 14px; color:var(--muted); font-size:12px }
    /* Supervisor v2 (policy-gated decision/send pipeline): compact, only
       ever shows watches with a non-default policy — a plain v1-only watch
       adds nothing here, so this never bloats the panel by default. */
    .sp-v2 { padding:0 14px 12px }
    .sp-v2:empty { display:none }
    .sp-v2 .sp-v2-item { padding:8px 0; border-top:1px solid var(--line); font-size:12px }
    .sp-v2 .sp-v2-item .sp-target { font-weight:700 }
    .sp-v2 .sp-v2-policy { display:inline-block; font-size:10px; padding:1px 6px; border-radius:999px; border:1px solid var(--line); color:var(--muted); margin-left:6px }
    .sp-v2 .sp-v2-policy.active { color:var(--amber); border-color:var(--amber) }
    .sp-v2 .sp-v2-prompt { font-family:var(--mono); background:var(--term-bg); padding:4px 6px; border-radius:4px; margin:4px 0; word-break:break-word }
    .sp-v2 .sp-v2-item button { margin-top:4px; background:#19243b; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:3px 8px; font:11px var(--mono); cursor:pointer }
    /* Last-rendered output stays visible while disconnected (never cleared),
       just visibly dimmed so it reads as "maybe stale", not "still live". */
    #output.stale { opacity:.55 }
    /* `flex:1; min-height:0` (not a fixed/min height) so `main` takes exactly
       the shell's remaining space and can still shrink below its content's
       natural size — the same "let a bounded box actually constrain its
       children instead of growing to fit them" pattern used throughout this
       chain (main -> .panel -> .detail -> .term -> #output). */
    main { display:grid; grid-template-columns:minmax(240px,340px) 1fr; grid-template-rows:minmax(0,1fr); gap:18px; padding:18px; flex:1; min-height:0 }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden; min-height:0 }
    .panel-title { padding:13px 16px; border-bottom:1px solid var(--line); color:var(--muted) }
    /* Mobile-only "☰ Sessions" reopen control (styled like the other term-bar
       buttons via .term-btn) and its drawer backdrop; both stay display:none
       outside the narrow-viewport media query below, so desktop/tablet keeps
       the sidebar permanently visible exactly as before. Living inside
       .term-bar (a flex row) rather than as a direct child of .detail means
       it never participates in .detail's own grid row template, regardless
       of which breakpoint/visibility state is active. */
    .sessions-toggle { display:none }
    #sidebarBackdrop { display:none }
    #sessions { padding:8px; overflow:auto; max-height:100% }
    /* .session is a <div role="button"> now, not a <button> -- it has to
       host a real, independently-clickable <button> child (.perm-btn, the
       row's own "Quyền truy cập" entry point) and a <input type=checkbox>
       (bulk-select), neither of which is legal inside a <button>. Keyboard
       activation (Enter/Space) is wired explicitly in JS to compensate for
       giving up the native <button> semantics. */
    .session { display:flex; align-items:flex-start; gap:8px; width:100%; text-align:left; color:inherit; background:transparent; border:1px solid transparent; border-radius:8px; padding:11px; cursor:pointer }
    .session:hover, .session.active { background:#19243b; border-color:#344360 }
    .session.needs-attention { border-color:var(--amber); background:rgba(255,200,87,.08) }
    .session .sess-check { flex:0 0 auto; margin-top:3px }
    .session .sess-main { flex:1; min-width:0 }
    /* One lock/eye/unlock icon per grantable (non-whitelisted) row only --
       a statically-whitelisted session never renders this at all, nothing
       to grant/revoke for it, ever. */
    .session .perm-btn { flex:0 0 auto; align-self:center; background:transparent; border:1px solid var(--line); border-radius:6px; color:var(--muted); padding:3px 7px; font-size:12px; cursor:pointer; line-height:1 }
    .session .perm-btn:hover { color:var(--text); border-color:var(--muted) }
    .name { font-weight:700 } .meta { font-size:12px; color:var(--muted); margin-top:4px }
    .attach-dot { display:inline-block; width:6px; height:6px; border-radius:50%; background:var(--line); margin-right:5px; vertical-align:middle }
    .attach-dot.on { background:var(--green) }
    /* Compact bulk-select bar above the session list -- only ever shown
       while 1+ grantable rows are checked (see #bulkBar[hidden] below);
       zero footprint otherwise, per the "gọn, không làm dashboard chật"
       requirement. */
    #bulkStatus { padding:4px 12px 0; font-size:11px; color:var(--muted) }
    #bulkStatus[hidden] { display:none }
    .bulk-bar { display:flex; align-items:center; flex-wrap:wrap; gap:6px; padding:8px 12px; border-bottom:1px solid var(--line); font-size:11px; color:var(--muted) }
    .bulk-bar[hidden] { display:none }
    .bulk-bar button { background:#2b3f66; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:4px 8px; cursor:pointer; font:inherit; font-size:11px }
    .bulk-bar button.danger { background:#3a2430 }
    .bulk-bar button.link { background:transparent; border:none; color:var(--muted); text-decoration:underline; padding:4px 2px }
    .bulk-bar button:disabled { opacity:.5; cursor:not-allowed }
    /* Compact attention badge: reused identically in the session list and the
       viewer header (#summary) so a WAITING_INPUT session is obvious in both
       places — driven entirely by classify_status()'s existing state string,
       nothing new is inferred from pane content here. */
    .attn-badge { display:inline-block; background:var(--amber); color:#231a00; font-size:11px; font-weight:700; padding:1px 6px; border-radius:4px; vertical-align:middle }
    /* Layout bugfix (real-device report): .detail's 6 direct children in
       DOM order are #sessionTabs, #summary, #grantBar, .term, #inputNote,
       #inputBar -- grid-template-rows must list exactly 6 tracks, in that
       order, with .term (the actual output viewport) as the one flexible
       track. It previously listed only 4 (a leftover from before
       #sessionTabs/#grantBar existed), which silently handed .term's
       intended growing row to #summary instead and let its content
       overflow into the rows below -- the reported overlap. */
    .detail { display:grid; grid-template-rows:auto auto auto minmax(0,1fr) auto auto; min-width:0; min-height:0 }
    #grantBar[hidden] { display:none } /* the plain #grantBar{display:flex} rule below would otherwise outrank the UA's own [hidden] default */
    #summary { grid-row:2; padding:14px 16px; border-bottom:1px solid var(--line) }
    .state-WAITING_INPUT { color:var(--amber) } .state-RUNNING { color:var(--green) }
    /* P0 Part C states: VERIFYING (independent verification in progress --
       amber, same "needs a look" weight as WAITING_INPUT); FAILED/BLOCKED
       (an autonomous watch's verifier rejected promotion, or none was
       configured -- red, distinct from ERROR's transient-pane-pattern
       meaning: these mean automation stopped and needs an operator). */
    .state-VERIFYING { color:var(--amber) } .state-FAILED,.state-BLOCKED { color:#ff6b6b }
    /* Terminal-style pane: a small chrome bar (title + follow/jump controls)
       above a dark, monospace, ANSI-rendering scrollback view. */
    .term { grid-row:4; display:flex; flex-direction:column; min-height:0 }
    .term-bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px 10px; padding:7px 12px; background:#0e1526; border-bottom:1px solid var(--line) }
    .term-dots { display:flex; gap:6px; flex:0 0 auto }
    .term-dots i { width:10px; height:10px; border-radius:50%; display:inline-block }
    .term-dots i.r { background:#ff5f57 } .term-dots i.y { background:#febc2e } .term-dots i.g { background:#28c840 }
    .term-title { color:var(--muted); font-size:12px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    /* flex:1 1 auto + min-width:0 (not flex:0 0 auto): a flex item's
       preferred size is its max-content (unwrapped) width by default, and
       flex-shrink:0 refuses to go below that — so .term-controls never
       actually shrank enough to let its own flex-wrap kick in, and its 7
       buttons silently overflowed the 390px shell instead of wrapping. */
    .term-controls { display:flex; flex-wrap:wrap; gap:8px; flex:1 1 auto; min-width:0 }
    .term-btn { background:#19243b; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:5px 10px; font:12px var(--mono); cursor:pointer; white-space:nowrap }
    .term-btn:hover:not(:disabled) { background:#233252 }
    .term-btn:disabled { opacity:.5; cursor:not-allowed }
    .term-btn.paused { border-color:var(--amber); color:var(--amber) }
    /* Client-side search over the currently rendered output only — no new
       backend route, no history beyond what's already on screen. Hidden
       (via the `hidden` attribute, not display:none in JS) until toggled,
       so it costs no vertical space when not in use. */
    .term-search { display:flex; align-items:center; gap:6px; padding:6px 12px; background:#0e1526; border-bottom:1px solid var(--line) }
    .term-search[hidden] { display:none } /* [hidden] and .term-search share specificity; this must win explicitly */
    .term-search input[type=text] { flex:1; min-width:0; background:var(--term-bg); border:1px solid var(--line); border-radius:6px; color:var(--text); padding:5px 8px; font:12px var(--mono) }
    .term-search-status { color:var(--muted); font-size:11px; white-space:nowrap; min-width:2.5em; text-align:center }
    /* !important: the span it marks already carries an ANSI-assigned inline
       color/background (see renderAnsi below), which would otherwise always
       win over a class selector. */
    .search-current { background:#ffd645 !important; color:#111 !important; border-radius:2px }
    #output { flex:1; min-height:0; margin:0; padding:14px 18px; overflow:auto; white-space:pre-wrap; word-break:break-word; line-height:1.45; font-family:var(--mono); background:var(--term-bg); color:#dce5f5 }
    #inputBar { grid-row:6; display:flex; gap:8px; padding:12px 16px; border-top:1px solid var(--line) }
    #inputBar input[type=text] { flex:1; background:#0e1526; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:9px 11px; font:inherit }
    #inputBar button { background:#2b3f66; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:9px 14px; cursor:pointer; font:inherit }
    #inputBar button:disabled { opacity:.5; cursor:not-allowed }
    #inputBar label { display:flex; align-items:center; gap:4px; color:var(--muted); font-size:12px; white-space:nowrap }
    #inputNote { grid-row:5; padding:6px 16px 0; font-size:12px; color:var(--muted) }
    #inputNote.error { color:#ff6b6b }
    /* Compact single-line entry point only now -- "Quyền: <label>" plus one
       "🔐 Quyền truy cập" button that opens #permModal, which does all the
       actual granting/revoking. Kept deliberately this thin (not the old
       multi-button inline bar) so re-enabling it can never reintroduce the
       real mobile overlap bug that #permModal's own separate, off-grid
       overlay design structurally avoids. */
    #grantBar { grid-row:3; display:flex; align-items:center; gap:8px; padding:8px 16px; border-bottom:1px solid var(--line); font-size:12px; color:var(--muted); flex-wrap:wrap }
    #grantBar button { background:#2b3f66; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:6px 12px; cursor:pointer; font:inherit; font-size:12px }
    /* Quyền truy cập modal -- the single reusable UI for granting/revoking
       a non-whitelisted session's read/input grant, opened from a session
       row's lock icon, a tab's own icon, this compact bar's button, or the
       bulk-select bar. Fixed-overlay + backdrop + body-class pattern,
       identical to #supervisorPanel above and for the same reason: living
       OUTSIDE .detail's own grid, it can never disturb that grid's fragile
       track count (see the comment on .detail above) regardless of what
       this modal itself ends up containing. */
    #permBackdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:30 }
    #permModal {
      display:none; position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); z-index:31;
      width:min(360px, calc(100vw - 32px)); max-height:80vh; overflow:auto;
      background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:0 20px 50px rgba(0,0,0,.6);
    }
    body.perm-modal-visible #permBackdrop, body.perm-modal-visible #permModal { display:block }
    #permModal .pm-head { padding:14px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:10px }
    #permModal .pm-head strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    #permModal .pm-body { padding:14px 16px; display:flex; flex-direction:column; gap:10px }
    .pm-state { font-size:12px; color:var(--muted) }
    .pm-presets { display:flex; flex-direction:column; gap:8px }
    .pm-presets button { background:#2b3f66; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:10px 12px; cursor:pointer; font:inherit; font-size:13px; text-align:left }
    .pm-presets button.current { border-color:var(--green) }
    .pm-presets button.danger { background:#3a2430 }
    .pm-presets button:disabled { opacity:.5; cursor:not-allowed }
    .pm-block { color:#ff9f9f; font-size:11px }
    .pm-error { color:#ff6b6b; font-size:12px }
    /* Browser-like session tabs -- always visible above output, one click
       switches session (no sidebar-open step). A single, horizontally-
       scrollable row (never wraps -- wrapping would push output down by a
       variable amount every time the tab count changes); thin/overlay
       scrollbar and touch-drag (`-webkit-overflow-scrolling`) so mobile
       gets swipe for free from the platform, no custom JS needed. */
    #sessionTabs {
      grid-row:1;
      display:flex; align-items:stretch; gap:2px; padding:4px 6px 0;
      overflow-x:auto; overflow-y:hidden; scrollbar-width:thin; -webkit-overflow-scrolling:touch;
      border-bottom:1px solid var(--line); background:var(--panel);
    }
    #sessionTabs:empty, #sessionTabs[hidden] { display:none }
    .session-tab {
      display:flex; align-items:center; gap:6px; flex:0 0 auto; max-width:220px;
      padding:7px 8px 7px 12px; border-radius:8px 8px 0 0; border:1px solid var(--line); border-bottom:none;
      background:#0f1730; color:var(--muted); cursor:pointer; font-size:12px; white-space:nowrap; user-select:none;
    }
    .session-tab:hover { color:var(--text) }
    .session-tab.active { background:var(--term-bg); color:var(--text); border-color:var(--line) }
    .session-tab.needs-attention { border-color:var(--amber) }
    .session-tab.needs-attention:not(.active) { background:rgba(255,200,87,.1) }
    .session-tab .tab-name { overflow:hidden; text-overflow:ellipsis; max-width:150px }
    /* Only rendered for a grantable (non-whitelisted) session's tab -- see
       updateTabElement -- so a normal whitelisted tab shows nothing extra. */
    .session-tab .tab-perm { flex:0 0 auto; font-size:11px; cursor:pointer; padding:0 1px }
    .session-tab .tab-perm[hidden] { display:none }
    .session-tab .tab-close {
      flex:0 0 auto; width:16px; height:16px; line-height:16px; text-align:center; border-radius:4px;
      color:var(--muted); font-size:12px; padding:0;
    }
    .session-tab .tab-close:hover { background:#3a2430; color:#ff9f9f }
    .session-tab-add {
      flex:0 0 auto; padding:7px 10px; border-radius:8px 8px 0 0; border:1px dashed var(--line);
      background:transparent; color:var(--muted); cursor:pointer; font-size:14px; align-self:center;
    }
    .session-tab-add:hover { color:var(--text); border-color:var(--muted) }
    #detachedMenu {
      position:absolute; z-index:20; margin-top:4px; min-width:220px; max-width:320px; max-height:50vh; overflow:auto;
      background:var(--panel); border:1px solid var(--line); border-radius:8px; box-shadow:0 8px 24px rgba(0,0,0,.4);
      padding:6px;
    }
    #detachedMenu button {
      display:block; width:100%; text-align:left; background:transparent; border:none; color:var(--text);
      padding:8px 10px; border-radius:6px; cursor:pointer; font:inherit; font-size:12px;
    }
    #detachedMenu button:hover { background:#19243b }
    #detachedMenu .muted-item { color:var(--muted); padding:8px 10px; font-size:12px }
    .lock-badge { background:#3a2430; color:#ff9f9f; border-radius:4px; padding:1px 6px; font-size:10px; margin-left:6px; vertical-align:middle }
    /* Matched on EITHER dimension, not just width: a phone rotated to
       landscape can easily exceed 760px of width (e.g. 852px on an iPhone
       15 Pro) while its height drops well under 760px, and a naive
       max-width-only query would stop applying mid-rotation — silently
       restoring the desktop header/sidebar/padding and, worse, dropping
       every body.fullscreen-terminal rule below (all scoped to this same
       query) even though the JS fullscreen state never changed. Matching
       on max-height too means a phone stays "mobile" in both orientations,
       while a real desktop/tablet (comfortably over 760px on both axes)
       is unaffected either way. */
    @media (max-width:760px), (max-height:760px) {
      /* Substantially smaller top chrome so the terminal gets the space:
         tighter header (title/subtitle/LIVE badge shrink together) and a
         compact, line-clamped session-status card instead of the
         desktop-sized versions. */
      header { padding:8px 12px; gap:8px }
      h1 { font-size:15px }
      #sessionTabs { padding:3px 4px 0; gap:1px }
      .session-tab { padding:6px 6px 6px 10px; font-size:11.5px; max-width:150px }
      .session-tab .tab-name { max-width:100px }
      header .muted { font-size:10px; line-height:1.25 }
      header .live { font-size:11px }
      .header-right { gap:6px }
      .supervisor-badge { padding:3px 8px; font-size:10px }
      #supervisorPanel { top:52px; right:8px; width:min(320px, calc(100vw - 16px)) }
      #summary {
        padding:8px 12px; font-size:12px; line-height:1.3;
        display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden;
      }
      /* Narrow/mobile: stack the panels (sessions on top, kept compact and
         independently scrollable; detail fills the rest) instead of sitting
         side by side — same bounded-height pattern as desktop, reused so the
         terminal pane still scrolls internally rather than the whole page. */
      main { grid-template-columns:1fr; grid-template-rows:auto minmax(0,1fr); position:relative }
      #sessions { max-height:32vh }
      .detail { min-height:0 }
      /* Smaller, tighter terminal text fits substantially more real output on
         a phone screen without hurting readability; desktop sizing (14px/1.45
         via the base #output rule above) is untouched — this only applies
         under the same narrow-viewport breakpoint as the rest of the mobile
         layout. Monospace alignment, ANSI spans, and pre-wrap wrapping are
         unaffected by font-size/line-height alone. */
      #output { font-size:11.5px; line-height:1.3; padding:10px 12px }
      .term-bar { padding:5px 10px }
      .term-btn { padding:4px 8px; font-size:11px }
      .term-search { padding:4px 8px }
      /* 16px, not smaller: iOS Safari auto-zooms the whole page on focus
         for any text input computing to under 16px, which is jarring and
         breaks the fixed app-shell (the browser overrides the layout
         viewport to zoom in). Padding still shrinks for compactness — only
         font-size is held at the zoom threshold. */
      .term-search input[type=text] { padding:4px 6px; font-size:16px }
      /* More compact input composer; it is already effectively "pinned" —
         it's the last row of the same bounded shell as everything else, and
         the app-shell above means there is no page scroll left to carry it
         away regardless. */
      #inputBar { padding:8px 10px; gap:6px }
      #inputBar input[type=text] { padding:7px 9px; font-size:16px }
      #inputBar button { padding:7px 10px; font-size:13px }
      #inputBar label { font-size:11px }
      #inputNote { padding:4px 10px 0; font-size:11px }
      /* Once a session is open, the sidebar hides by default so the terminal
         pane gets essentially the full screen (main's "auto" sidebar row
         collapses to 0 with nothing placed in it); the ☰ Sessions button
         reopens it as a floating drawer over a backdrop, on top of the still
         full-size terminal, rather than resizing/displacing it. Before any
         session is picked, the sidebar stays inline exactly as it always
         has, so first-time access to the list is never hidden. Positioned
         `absolute` against `main` (not `fixed` with a guessed pixel offset
         from the viewport top) so it always sits right below the header,
         however tall the header actually is. */
      body.has-selection .sessions-toggle { display:inline-flex }
      body.has-selection #sessionsPanel { display:none }
      body.has-selection.sidebar-visible #sessionsPanel {
        display:block; position:absolute; left:0; right:0; top:0; z-index:20;
        max-height:70vh; overflow:auto; box-shadow:0 20px 50px rgba(0,0,0,.6);
      }
      body.has-selection.sidebar-visible #sidebarBackdrop {
        display:block; position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:15;
      }
      /* Respect notch/home-indicator safe areas for the shell that now spans
         essentially the full viewport. */
      main { padding-left:max(18px, env(safe-area-inset-left)); padding-right:max(18px, env(safe-area-inset-right)); padding-bottom:max(18px, env(safe-area-inset-bottom)) }
      header { padding-top:max(8px, env(safe-area-inset-top)) }
      /* Once a session is open, the terminal panel IS the screen (the
         sidebar is a floating drawer, not a sibling taking up column
         space — see has-selection above) — so the generous desktop-style
         18px outer gutter around it is wasted width on a phone. Shrink it
         to a thin edge (still safe-area aware, never under it) without
         touching #output's own padding/font-size, so the actual output
         area is unchanged — only the empty margin around the panel
         shrinks. Deliberately not applied before a session is selected,
         so the sessions list keeps its normal comfortable padding. */
      body.has-selection main { gap:6px; padding:max(6px, env(safe-area-inset-top)) max(6px, env(safe-area-inset-right)) max(6px, env(safe-area-inset-bottom)) max(6px, env(safe-area-inset-left)) }
      body.has-selection .panel.detail { border-radius:8px }

      /* ---- fullscreen terminal mode ---------------------------------- */
      /* Hides every non-terminal chrome element (header, status card,
         sidebar/drawer controls, input composer) so essentially only the
         terminal pane remains, its own small term-bar (title + follow/
         jump/exit-fullscreen controls) acting as the "small floating
         control" this needs. #output still scrolls internally the same
         way; the config.default_tail_lines bound, ANSI rendering, and
         auto-follow/pause/jump are all untouched by anything in this
         block — it is pure presentation. */
      body.fullscreen-terminal header,
      body.fullscreen-terminal #summary,
      body.fullscreen-terminal #grantBar,
      body.fullscreen-terminal #inputNote,
      body.fullscreen-terminal #inputBar,
      body.fullscreen-terminal .sessions-toggle,
      body.fullscreen-terminal #sidebarBackdrop { display:none }
      body.fullscreen-terminal main { padding:0; gap:0 }
      body.fullscreen-terminal .detail { grid-template-rows:minmax(0,1fr) }
      body.fullscreen-terminal .panel.detail { border-radius:0; border:none }
      body.fullscreen-terminal .term-bar {
        padding-top:max(7px, env(safe-area-inset-top));
        padding-left:max(10px, env(safe-area-inset-left));
        padding-right:max(10px, env(safe-area-inset-right));
      }
      body.fullscreen-terminal #output {
        padding-bottom:max(10px, env(safe-area-inset-bottom));
        padding-left:max(12px, env(safe-area-inset-left));
        padding-right:max(12px, env(safe-area-inset-right));
      }
    }
  </style>
</head>
<body>
  <header>
    <div><h1>Terminal MCP</h1><div class="muted">Whitelisted tmux session monitor</div></div>
    <div class="header-right">
      <a href="/dashboard/sessions" class="supervisor-badge" id="sessionsAdminLink" style="text-decoration:none">⚙ Quản lý</a>
      <button id="supervisorBadge" class="supervisor-badge" type="button" hidden></button>
      <span class="live" id="liveBadge">● LIVE</span>
    </div>
  </header>
  <main>
    <section class="panel" id="sessionsPanel"><div class="panel-title">SESSIONS <span id="count"></span></div><div id="bulkStatus" hidden></div><div id="bulkBar" class="bulk-bar" hidden></div><div id="sessions"></div></section>
    <section class="panel detail">
      <div id="sessionTabs" class="session-tabs" role="tablist" aria-label="Sessions" hidden></div>
      <div id="summary" class="muted">Chọn một session để xem output.</div>
      <div id="grantBar" hidden></div>
      <div class="term">
        <div class="term-bar">
          <button id="sessionsToggle" class="sessions-toggle term-btn" type="button">☰ Sessions</button>
          <span class="term-dots"><i class="r"></i><i class="y"></i><i class="g"></i></span>
          <span class="term-title" id="termTitle"></span>
          <span class="term-controls">
            <button id="followToggle" class="term-btn" type="button" disabled>Auto-follow: ON</button>
            <button id="jumpBtn" class="term-btn" type="button" disabled>Jump to latest</button>
            <button id="fullscreenBtn" class="term-btn" type="button" disabled>⛶ Fullscreen</button>
            <button id="fontDecBtn" class="term-btn" type="button" title="Decrease terminal font size">A−</button>
            <button id="fontIncBtn" class="term-btn" type="button" title="Increase terminal font size">A+</button>
            <button id="searchToggleBtn" class="term-btn" type="button" disabled title="Search output">🔍</button>
            <button id="copyBtn" class="term-btn" type="button" disabled title="Copy output">⧉</button>
          </span>
        </div>
        <div class="term-search" id="termSearch" hidden>
          <input type="text" id="searchInput" placeholder="Tìm trong output..." autocomplete="off">
          <span class="term-search-status" id="searchStatus"></span>
          <button id="searchPrevBtn" class="term-btn" type="button">‹</button>
          <button id="searchNextBtn" class="term-btn" type="button">›</button>
          <button id="searchCloseBtn" class="term-btn" type="button">✕</button>
        </div>
        <pre id="output"></pre>
      </div>
      <div id="inputNote"></div>
      <div id="inputBar">
        <input type="text" id="inputText" placeholder="Nhập text để gửi vào session..." disabled>
        <label><input type="checkbox" id="inputEnter" checked> Enter</label>
        <button id="inputSend" disabled>Gửi</button>
      </div>
    </section>
  </main>
  <div id="sidebarBackdrop"></div>
  <div id="supervisorBackdrop"></div>
  <div id="supervisorPanel">
    <div class="sp-head">
      <strong>Supervisor</strong>
      <button id="supervisorCloseBtn" class="term-btn" type="button">✕</button>
    </div>
    <div class="sp-counts" id="supervisorCounts"></div>
    <div class="sp-events" id="supervisorEvents"></div>
    <div class="sp-v2" id="supervisorV2"></div>
  </div>
  <div id="permBackdrop"></div>
  <div id="permModal" role="dialog" aria-modal="true" aria-labelledby="permModalTitle">
    <div class="pm-head">
      <strong id="permModalTitle"></strong>
      <button id="permModalCloseBtn" class="term-btn" type="button">✕</button>
    </div>
    <div class="pm-body">
      <div class="pm-state" id="permModalState"></div>
      <div class="pm-presets" id="permModalPresets"></div>
      <div class="pm-block" id="permModalBlock"></div>
      <div class="pm-error" id="permModalError"></div>
    </div>
  </div>
  <script>
    let selected = null;
    let inputAllowed = false;
    let autoFollow = true;
    let lastRenderedSession = null;
    let sidebarForcedOpen = false;
    let fullscreenTerminal = false;
    let lastKnownRows = []; // the most recent /dashboard/api/sessions rows, for immediate tab re-render (see selectSession)
    let loadDetailSequence = 0; // generation counter -- see loadDetail's own guard for why a session-name check alone isn't enough
    let detachedMenuOpen = false;
    const sessionsEl = document.querySelector('#sessions');
    const sessionTabsEl = document.querySelector('#sessionTabs');
    const outputEl = document.querySelector('#output');
    const summaryEl = document.querySelector('#summary');
    const grantBarEl = document.querySelector('#grantBar');
    const bulkBarEl = document.querySelector('#bulkBar');
    const bulkStatusEl = document.querySelector('#bulkStatus');
    const permBackdropEl = document.querySelector('#permBackdrop');
    const permModalEl = document.querySelector('#permModal');
    const permModalTitleEl = document.querySelector('#permModalTitle');
    const permModalStateEl = document.querySelector('#permModalState');
    const permModalPresetsEl = document.querySelector('#permModalPresets');
    const permModalBlockEl = document.querySelector('#permModalBlock');
    const permModalErrorEl = document.querySelector('#permModalError');
    const permModalCloseBtnEl = document.querySelector('#permModalCloseBtn');
    const liveBadgeEl = document.querySelector('#liveBadge');
    const supervisorBadgeEl = document.querySelector('#supervisorBadge');
    const supervisorPanelEl = document.querySelector('#supervisorPanel');
    const supervisorBackdropEl = document.querySelector('#supervisorBackdrop');
    const supervisorCountsEl = document.querySelector('#supervisorCounts');
    const supervisorEventsEl = document.querySelector('#supervisorEvents');
    const supervisorV2El = document.querySelector('#supervisorV2');
    const supervisorCloseBtnEl = document.querySelector('#supervisorCloseBtn');
    const termTitleEl = document.querySelector('#termTitle');
    const followToggleEl = document.querySelector('#followToggle');
    const jumpBtnEl = document.querySelector('#jumpBtn');
    const fullscreenBtnEl = document.querySelector('#fullscreenBtn');
    const fontDecBtnEl = document.querySelector('#fontDecBtn');
    const fontIncBtnEl = document.querySelector('#fontIncBtn');
    const searchToggleBtnEl = document.querySelector('#searchToggleBtn');
    const copyBtnEl = document.querySelector('#copyBtn');
    const termSearchEl = document.querySelector('#termSearch');
    const searchInputEl = document.querySelector('#searchInput');
    const searchStatusEl = document.querySelector('#searchStatus');
    const searchPrevBtnEl = document.querySelector('#searchPrevBtn');
    const searchNextBtnEl = document.querySelector('#searchNextBtn');
    const searchCloseBtnEl = document.querySelector('#searchCloseBtn');
    const sessionsToggleEl = document.querySelector('#sessionsToggle');
    const sidebarBackdropEl = document.querySelector('#sidebarBackdrop');
    const inputNoteEl = document.querySelector('#inputNote');
    const inputTextEl = document.querySelector('#inputText');
    const inputEnterEl = document.querySelector('#inputEnter');
    const inputSendEl = document.querySelector('#inputSend');
    const clean = value => String(value ?? '');

    // ---- mobile sidebar drawer ---------------------------------------------
    // Desktop/tablet: pure CSS keeps #sessionsPanel always visible and
    // .sessions-toggle always display:none, so none of this has any visible
    // effect there. Mobile only: hides the session list once a session is
    // open (main's own sidebar row collapses to 0 with nothing placed in
    // it, so the terminal pane gets the freed height), reopenable as a
    // floating drawer over a backdrop without displacing/resizing the pane.
    function updateSidebarVisibility() {
      document.body.classList.toggle('has-selection', Boolean(selected));
      document.body.classList.toggle('sidebar-visible', sidebarForcedOpen);
    }
    sessionsToggleEl.onclick = () => { sidebarForcedOpen = !sidebarForcedOpen; updateSidebarVisibility(); };
    sidebarBackdropEl.onclick = () => { sidebarForcedOpen = false; updateSidebarVisibility(); };

    // ---- Supervisor Loop v1 summary (compact badge + overlay panel) -------
    // Read-only from this page's point of view: the badge/panel only ever
    // render what GET /dashboard/api/supervisor returns (the same
    // whitelist-guarded watch/event data the supervisor_* MCP tools expose)
    // and the only write path is "ack one event", which is local metadata
    // only — see the ack handler below, it never touches a tmux session.
    let supervisorVisible = false;
    // P0-7/P0-8: "DONE" is legacy-only now (status.py's to_legacy_state) --
    // the state_counts this renders come from the real, primary vocabulary
    // (COMPLETION_CANDIDATE: prose/marker evidence seen, not yet
    // corroborated; VERIFIED_DONE: independently corroborated).
    // P0 Part C: VERIFYING/FAILED/BLOCKED added -- see status.py's
    // SUPERVISOR_STATES docstring for what each means.
    const SUPERVISOR_STATE_ORDER = ['RUNNING', 'WAITING_INPUT', 'COMPLETION_CANDIDATE', 'VERIFYING', 'VERIFIED_DONE', 'FAILED', 'BLOCKED', 'ERROR', 'IDLE', 'UNKNOWN'];
    function toggleSupervisorPanel(open) {
      supervisorVisible = open;
      document.body.classList.toggle('supervisor-visible', open);
    }
    supervisorBadgeEl.onclick = () => toggleSupervisorPanel(!supervisorVisible);
    supervisorCloseBtnEl.onclick = () => toggleSupervisorPanel(false);
    supervisorBackdropEl.onclick = () => toggleSupervisorPanel(false);

    async function ackSupervisorEvent(id) {
      try {
        await fetch('/dashboard/api/supervisor/ack', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id}),
        });
      } catch (error) { /* best-effort; next refresh reconciles either way */ }
      await loadSupervisor();
    }

    async function loadSupervisor() {
      let data;
      try {
        const response = await fetch('/dashboard/api/supervisor', {cache: 'no-store'});
        data = await response.json();
      } catch (error) {
        return; // connection health is already surfaced by the LIVE/RECONNECTING badge
      }
      const status = data.status || {}; const events = data.events || [];
      const counts = status.state_counts || {};
      // P0 Part C: FAILED/BLOCKED both mean an autonomous watch's
      // automation has halted pending an operator -- same "needs a look"
      // weight as WAITING_INPUT/ERROR.
      const attentionCount = (counts.WAITING_INPUT || 0) + (counts.ERROR || 0) + (status.stalled_count || 0)
                            + (counts.FAILED || 0) + (counts.BLOCKED || 0);

      // Zero watches at all (e.g. supervisor.enabled:false and nobody has
      // called supervisor_watch) -> no badge, nothing extra on screen.
      if (!status.watch_count) {
        supervisorBadgeEl.hidden = true;
        if (supervisorVisible) toggleSupervisorPanel(false);
        return;
      }
      supervisorBadgeEl.hidden = false;
      supervisorBadgeEl.classList.toggle('attention', attentionCount > 0);
      supervisorBadgeEl.textContent = attentionCount > 0
        ? `🛰 ${attentionCount} needs attention` : `🛰 ${status.enabled_watch_count} watched`;

      supervisorCountsEl.replaceChildren();
      for (const state of SUPERVISOR_STATE_ORDER) {
        const count = counts[state] || 0;
        const chip = document.createElement('span');
        chip.className = 'sp-count' + (count > 0 ? ' nonzero' : '') + ((state === 'WAITING_INPUT' || state === 'ERROR') && count > 0 ? ' attn' : '');
        chip.textContent = `${state.replace('_', ' ')}: ${count}`;
        supervisorCountsEl.append(chip);
      }
      if (status.stalled_count) {
        const chip = document.createElement('span');
        chip.className = 'sp-count attn';
        chip.textContent = `STALLED: ${status.stalled_count}`;
        supervisorCountsEl.append(chip);
      }

      supervisorEventsEl.replaceChildren();
      if (!events.length) {
        const empty = document.createElement('div');
        empty.className = 'sp-empty'; empty.textContent = 'No unacknowledged events.';
        supervisorEventsEl.append(empty);
      }
      for (const event of events) {
        const row = document.createElement('div'); row.className = 'sp-event';
        const target = document.createElement('div'); target.className = 'sp-target';
        target.textContent = `${clean(event.target)} · ${clean(event.state)}`;
        const reason = document.createElement('div'); reason.className = 'muted';
        reason.textContent = clean(event.reason);
        const ackBtn = document.createElement('button'); ackBtn.type = 'button'; ackBtn.textContent = 'Ack';
        ackBtn.onclick = () => ackSupervisorEvent(event.id);
        row.append(target, reason, ackBtn);
        supervisorEventsEl.append(row);
      }
    }

    async function pauseSupervisorV2(target, kind) {
      try {
        await fetch('/dashboard/api/supervisor2/pause', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({target, kind}),
        });
      } catch (error) { /* best-effort; next refresh reconciles either way */ }
      await loadSupervisorV2();
    }

    async function loadSupervisorV2() {
      let data;
      try {
        const response = await fetch('/dashboard/api/supervisor2', {cache: 'no-store'});
        data = await response.json();
      } catch (error) {
        return;
      }
      const watches = data.watches || [];
      supervisorV2El.replaceChildren();
      for (const w of watches) {
        const item = document.createElement('div'); item.className = 'sp-v2-item';
        const header = document.createElement('div'); header.className = 'sp-target';
        header.textContent = `${clean(w.target)} `;
        const policyBadge = document.createElement('span');
        policyBadge.className = 'sp-v2-policy' + (w.policy.policy_mode !== 'observe_only' ? ' active' : '');
        policyBadge.textContent = clean(w.policy.policy_mode);
        header.append(policyBadge);
        item.append(header);

        const action = w.latest_action;
        if (action) {
          const stateLine = document.createElement('div'); stateLine.className = 'muted';
          stateLine.textContent = `action #${action.id}: ${clean(action.state)}`
            + (action.stop_reason ? ` — ${clean(action.stop_reason)}` : '');
          item.append(stateLine);
          if (action.proposed_prompt) {
            const promptEl = document.createElement('div'); promptEl.className = 'sp-v2-prompt';
            promptEl.textContent = clean(action.proposed_prompt);
            item.append(promptEl);
          }
          if (action.send_result) {
            const resultLine = document.createElement('div'); resultLine.className = 'muted';
            resultLine.textContent = `last send: ${clean(action.send_result)}`;
            item.append(resultLine);
          }
        }
        const counters = document.createElement('div'); counters.className = 'muted';
        counters.textContent = `auto actions: ${w.policy.auto_action_count}/${w.policy.max_auto_actions}`
          + (w.policy.blocked_reason ? ` · blocked: ${clean(w.policy.blocked_reason)}` : '');
        item.append(counters);

        if (w.policy.policy_mode !== 'observe_only') {
          const pauseBtn = document.createElement('button'); pauseBtn.type = 'button'; pauseBtn.textContent = 'Pause (observe only)';
          pauseBtn.onclick = () => pauseSupervisorV2(w.target, w.kind);
          item.append(pauseBtn);
        }
        supervisorV2El.append(item);
      }
    }

    // ---- minimal ANSI (SGR colour/style) renderer -------------------------
    // tmux `capture-pane -e` re-serializes its own already-resolved terminal
    // emulation state as `ESC [ ... m` (SGR) runs plus plain text — not raw,
    // unprocessed cursor-movement/OSC sequences, which tmux has already
    // applied before capture. So a small SGR-only parser is both correct and
    // sufficient; any other CSI sequence (defensively handled below, in case
    // one ever appears) is simply dropped rather than mis-rendered. Spans are
    // built with createElement/textContent only, with no raw-HTML assignment
    // anywhere, so captured pane content can never be interpreted as markup.
    const ANSI_BASE = ['#3b3b3b','#e05561','#8cc265','#d2c057','#5c96d1','#a179dc','#4bc2c5','#c6c6c6'];
    const ANSI_BRIGHT = ['#6b6b6b','#f76f7a','#a8e08a','#f0d97a','#7bb4e8','#c39bf0','#71dde0','#eeeeee'];
    function ansi256(code) {
      const n = Number(code);
      if (!Number.isFinite(n) || n < 0) return null;
      if (n < 16) return n < 8 ? ANSI_BASE[n] : ANSI_BRIGHT[n - 8];
      if (n <= 231) {
        const i = n - 16, r = Math.floor(i / 36), g = Math.floor((i % 36) / 6), b = i % 6;
        const step = v => v === 0 ? 0 : 55 + v * 40;
        return `rgb(${step(r)},${step(g)},${step(b)})`;
      }
      if (n <= 255) { const gray = 8 + (n - 232) * 10; return `rgb(${gray},${gray},${gray})`; }
      return null;
    }
    const CSI_RE = /\\x1b\\[([0-9;]*)([A-Za-z])/g;
    function ansiRuns(text) {
      const runs = [];
      let fg = null, bg = null, bold = false, dim = false, italic = false, underline = false, inverse = false;
      let last = 0, match;
      CSI_RE.lastIndex = 0;
      const flush = end => { if (end > last) runs.push({t: text.slice(last, end), fg, bg, bold, dim, italic, underline, inverse}); last = end; };
      while ((match = CSI_RE.exec(text))) {
        flush(match.index);
        last = CSI_RE.lastIndex;
        if (match[2] !== 'm') continue; // non-SGR CSI: consumed, not rendered
        const params = match[1].length ? match[1].split(';').map(Number) : [0];
        for (let i = 0; i < params.length; i++) {
          const code = params[i];
          if (code === 0) { fg = null; bg = null; bold = dim = italic = underline = inverse = false; }
          else if (code === 1) bold = true;
          else if (code === 2) dim = true;
          else if (code === 3) italic = true;
          else if (code === 4) underline = true;
          else if (code === 7) inverse = true;
          else if (code === 22) { bold = false; dim = false; }
          else if (code === 23) italic = false;
          else if (code === 24) underline = false;
          else if (code === 27) inverse = false;
          else if (code >= 30 && code <= 37) fg = ANSI_BASE[code - 30];
          else if (code === 38 && params[i + 1] === 5) { fg = ansi256(params[i + 2]); i += 2; }
          else if (code === 38 && params[i + 1] === 2) { fg = `rgb(${params[i+2]},${params[i+3]},${params[i+4]})`; i += 4; }
          else if (code === 39) fg = null;
          else if (code >= 40 && code <= 47) bg = ANSI_BASE[code - 40];
          else if (code === 48 && params[i + 1] === 5) { bg = ansi256(params[i + 2]); i += 2; }
          else if (code === 48 && params[i + 1] === 2) { bg = `rgb(${params[i+2]},${params[i+3]},${params[i+4]})`; i += 4; }
          else if (code === 49) bg = null;
          else if (code >= 90 && code <= 97) fg = ANSI_BRIGHT[code - 90];
          else if (code >= 100 && code <= 107) bg = ANSI_BRIGHT[code - 100];
        }
      }
      flush(text.length);
      return runs;
    }
    function renderAnsi(container, text) {
      container.replaceChildren();
      for (const run of ansiRuns(text)) {
        if (!run.t) continue;
        const span = document.createElement('span');
        span.textContent = run.t;
        const fg = run.inverse ? (run.bg || '#0a0e1a') : run.fg;
        const bg = run.inverse ? (run.fg || '#dce5f5') : run.bg;
        if (fg) span.style.color = fg;
        if (bg) span.style.backgroundColor = bg;
        if (run.bold) span.style.fontWeight = '700';
        if (run.dim) span.style.opacity = '0.65';
        if (run.italic) span.style.fontStyle = 'italic';
        if (run.underline) span.style.textDecoration = 'underline';
        container.append(span);
      }
    }

    // ---- search (client-side, current rendered output only) ---------------
    // Operates purely on outputEl's already-fetched, already-redacted DOM
    // text — no backend route, no history beyond what's on screen, no
    // filesystem/shell access. Only the *current* match is highlighted (a
    // plain literal, case-insensitive substring scan — never a user-supplied
    // regex) by marking its containing ANSI span; count/position are tracked
    // for all matches without needing to mutate every one of them.
    let searchMatches = []; // [{start, span}] — span is the ANSI run containing that match's start offset
    let searchIndex = -1;
    let searchQuery = '';
    let searchMarkEl = null;
    function clearSearchHighlight() {
      if (searchMarkEl) { searchMarkEl.classList.remove('search-current'); searchMarkEl = null; }
    }
    function updateSearchStatus() {
      searchStatusEl.textContent = searchMatches.length ? `${searchIndex + 1}/${searchMatches.length}`
        : (searchQuery ? '0/0' : '');
    }
    function goToMatch(i) {
      clearSearchHighlight();
      if (!searchMatches.length) { searchIndex = -1; updateSearchStatus(); return; }
      searchIndex = ((i % searchMatches.length) + searchMatches.length) % searchMatches.length;
      const match = searchMatches[searchIndex];
      if (match.span) {
        match.span.classList.add('search-current');
        searchMarkEl = match.span;
        // Not near-bottom after this is exactly the existing "user scrolled
        // away" case (see the #output scroll listener below) — auto-follow
        // pauses itself the normal way, never yanking the match back down.
        match.span.scrollIntoView({ block: 'center' });
      }
      updateSearchStatus();
    }
    function runSearch(query) {
      clearSearchHighlight();
      searchQuery = query; searchMatches = []; searchIndex = -1;
      if (!query) { updateSearchStatus(); return; }
      const text = outputEl.textContent;
      const haystack = text.toLowerCase();
      const needle = query.toLowerCase();
      if (!needle) { updateSearchStatus(); return; }
      const offsets = []; let pos = 0;
      for (const span of outputEl.children) {
        const len = span.textContent.length;
        offsets.push({ span, start: pos, end: pos + len });
        pos += len;
      }
      let from = 0;
      while (true) {
        const at = haystack.indexOf(needle, from);
        if (at === -1) break;
        const containing = offsets.find(o => at >= o.start && at < o.end);
        searchMatches.push({ start: at, span: containing ? containing.span : (offsets[offsets.length - 1] || null) });
        from = at + needle.length;
      }
      if (searchMatches.length) { goToMatch(0); } else { updateSearchStatus(); }
    }
    function closeSearch() {
      termSearchEl.hidden = true;
      searchInputEl.value = '';
      clearSearchHighlight();
      searchQuery = ''; searchMatches = []; searchIndex = -1;
      searchStatusEl.textContent = '';
    }
    searchToggleBtnEl.onclick = () => {
      termSearchEl.hidden = !termSearchEl.hidden;
      if (!termSearchEl.hidden) { searchInputEl.focus(); } else { closeSearch(); }
    };
    searchCloseBtnEl.onclick = closeSearch;
    searchInputEl.addEventListener('input', () => runSearch(searchInputEl.value));
    searchNextBtnEl.onclick = () => goToMatch(searchIndex + 1);
    searchPrevBtnEl.onclick = () => goToMatch(searchIndex - 1);
    searchInputEl.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); goToMatch(searchIndex + (event.shiftKey ? -1 : 1)); }
      else if (event.key === 'Escape') { event.stopPropagation(); closeSearch(); }
    });

    // ---- copy (plain text only, never HTML/ANSI markup) --------------------
    async function copyText(text) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch (error) {
        try {
          const ta = document.createElement('textarea');
          ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
          document.body.appendChild(ta); ta.focus(); ta.select();
          const ok = document.execCommand('copy');
          document.body.removeChild(ta);
          return ok;
        } catch (fallbackError) { return false; }
      }
    }
    copyBtnEl.onclick = async () => {
      // outputEl only ever contains plain-text span runs (see renderAnsi
      // above) — no ANSI escape bytes and no markup ever end up in
      // .textContent, whether copying a selection or the full pane.
      const selectionText = window.getSelection().toString();
      const selectionInOutput = selectionText && outputEl.contains(window.getSelection().anchorNode);
      const text = selectionInOutput ? selectionText : outputEl.textContent;
      const ok = await copyText(text);
      // Brief inline feedback on the button itself, right where the action
      // happened, rather than a status area elsewhere on the page.
      const original = copyBtnEl.textContent;
      copyBtnEl.textContent = ok ? '✓' : '✕';
      setTimeout(() => { copyBtnEl.textContent = original; }, 1200);
    };

    // ---- terminal-only font size (A-/A+), persisted locally ----------------
    // Only this one UI preference (a number) is stored — never output/session
    // content. Unitless line-height in the #output CSS rules above (1.3
    // mobile / 1.45 desktop) recomputes automatically from the element's own
    // font-size, so only font-size itself needs to be set here.
    const FONT_SIZE_KEY = 'terminal-mcp:font-size';
    const FONT_SIZE_MIN = 9, FONT_SIZE_MAX = 16, FONT_SIZE_STEP = 1;
    const FONT_SIZE_DEFAULT = window.matchMedia('(max-width:760px)').matches ? 11.5 : 14;
    let outputFontSize = FONT_SIZE_DEFAULT;
    (() => {
      try {
        const stored = parseFloat(localStorage.getItem(FONT_SIZE_KEY));
        if (Number.isFinite(stored) && stored >= FONT_SIZE_MIN && stored <= FONT_SIZE_MAX) outputFontSize = stored;
      } catch (error) { /* private mode / storage disabled: fall back to the default, not essential */ }
    })();
    function setFontSize(px) {
      outputFontSize = Math.min(FONT_SIZE_MAX, Math.max(FONT_SIZE_MIN, px));
      outputEl.style.fontSize = outputFontSize + 'px';
      fontDecBtnEl.disabled = outputFontSize <= FONT_SIZE_MIN;
      fontIncBtnEl.disabled = outputFontSize >= FONT_SIZE_MAX;
      try { localStorage.setItem(FONT_SIZE_KEY, String(outputFontSize)); } catch (error) { /* ignore */ }
    }
    fontDecBtnEl.onclick = () => setFontSize(outputFontSize - FONT_SIZE_STEP);
    fontIncBtnEl.onclick = () => setFontSize(outputFontSize + FONT_SIZE_STEP);
    setFontSize(outputFontSize); // apply the restored/default value immediately, before any session loads

    // ---- auto-follow / pause / jump-to-latest ------------------------------
    function nearBottom(el) {
      return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    }
    function setAutoFollow(value) {
      autoFollow = value;
      followToggleEl.textContent = autoFollow ? 'Auto-follow: ON' : 'Auto-follow: PAUSED';
      followToggleEl.classList.toggle('paused', !autoFollow);
    }
    function refreshTermControls() {
      followToggleEl.disabled = !selected;
      jumpBtnEl.disabled = !selected;
      fullscreenBtnEl.disabled = !selected;
      searchToggleBtnEl.disabled = !selected;
      copyBtnEl.disabled = !selected;
      termTitleEl.textContent = selected || '';
    }
    followToggleEl.onclick = () => {
      setAutoFollow(!autoFollow);
      if (autoFollow) outputEl.scrollTop = outputEl.scrollHeight;
    };
    jumpBtnEl.onclick = () => { setAutoFollow(true); outputEl.scrollTop = outputEl.scrollHeight; };

    // ---- fullscreen terminal (mobile: CSS-only chrome hide; everywhere:
    // opportunistic real Fullscreen API) --------------------------------
    // The body.fullscreen-terminal CSS class (scoped to the mobile media
    // query) is what actually delivers "essentially only the terminal"
    // everywhere, including iOS Safari, where Element.requestFullscreen()
    // is not reliably available in a normal tab. The real Fullscreen API
    // call below is progressive enhancement only — best-effort, never
    // depended on — for browsers that do support it (desktop, Android
    // Chrome), so those also get the OS/browser chrome hidden.
    const FULLSCREEN_KEY = 'terminal-mcp:fullscreen';
    function recalledFullscreen() {
      try { return localStorage.getItem(FULLSCREEN_KEY) === '1'; } catch (error) { return false; }
    }
    function setFullscreen(value, { persist = true } = {}) {
      fullscreenTerminal = value;
      document.body.classList.toggle('fullscreen-terminal', value);
      fullscreenBtnEl.textContent = value ? '✕ Exit fullscreen' : '⛶ Fullscreen';
      if (persist) {
        // A deliberate toggle updates the remembered preference; a forced
        // exit (e.g. the viewed session disappearing, below) must not — that
        // would silently wipe out an unrelated, still-valid preference.
        try { localStorage.setItem(FULLSCREEN_KEY, value ? '1' : '0'); } catch (error) { /* ignore */ }
      }
      if (value) {
        sidebarForcedOpen = false; updateSidebarVisibility(); // exiting must restore the normal layout, so never leave the drawer state stale
        if (document.documentElement.requestFullscreen) {
          document.documentElement.requestFullscreen().catch(() => {});
        }
      } else if (document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
      }
      // Entering or exiting changes #output's clientHeight (chrome shows/
      // hides around it) without moving scrollTop, so a pane that was
      // pinned to the bottom before the toggle can end up short of it
      // after — re-snap on both transitions whenever still auto-following.
      if (autoFollow) { outputEl.scrollTop = outputEl.scrollHeight; }
    }
    fullscreenBtnEl.onclick = () => setFullscreen(!fullscreenTerminal);
    document.addEventListener('fullscreenchange', () => {
      // Stay in sync if real browser fullscreen was exited some other way
      // (Esc, browser/OS UI) while the CSS-only mode was also active.
      if (!document.fullscreenElement && fullscreenTerminal) setFullscreen(false);
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && fullscreenTerminal) setFullscreen(false);
    });
    // Orientation change / resize (e.g. rotating a phone) never touches
    // fullscreenTerminal, the selected session, auto-follow, or the
    // remembered font size — none of that JS state is viewport-derived, so
    // there is nothing to "restore" here. The one real side effect of a
    // rotation is exactly like the fullscreen-toggle case just above:
    // #output's clientHeight changes size without moving scrollTop, so
    // re-snap to the bottom whenever still auto-following (a paused view
    // deliberately keeps its scroll position through a rotation too).
    window.addEventListener('resize', () => {
      if (autoFollow) outputEl.scrollTop = outputEl.scrollHeight;
    });
    window.addEventListener('orientationchange', () => {
      if (autoFollow) outputEl.scrollTop = outputEl.scrollHeight;
    });
    outputEl.addEventListener('scroll', () => {
      // A manual scroll away from the bottom pauses auto-follow so it is
      // never forcibly pulled back down; scrolling back near the bottom
      // resumes it, matching common log/chat viewers.
      if (nearBottom(outputEl)) { if (!autoFollow) setAutoFollow(true); }
      else if (autoFollow) { setAutoFollow(false); }
    });

    function setInputNote(text, isError) {
      inputNoteEl.textContent = text || '';
      inputNoteEl.className = isError ? 'error' : '';
    }
    function refreshInputControls() {
      const enabled = Boolean(selected) && inputAllowed;
      inputTextEl.disabled = !enabled; inputSendEl.disabled = !enabled;
      if (!selected) { setInputNote(''); }
      else if (!inputAllowed) { setInputNote('Input bị tắt cho session này (permission hoặc input_policy).', false); }
      else { setInputNote(''); }
    }
    // P0-4: a fresh idempotency key per click/Enter -- if the fetch below
    // fails ambiguously (e.g. a network drop after the send already
    // reached the server) and the UI is retried, the retry replays the
    // original stored result instead of risking a second real send.
    function newIdempotencyKey() {
      try { return crypto.randomUUID(); } catch (error) {
        return 'dashboard-' + Date.now() + '-' + Math.random().toString(36).slice(2);
      }
    }
    async function sendInput() {
      if (!selected || !inputTextEl.value) return;
      // Captured once, synchronously, before any await -- the explicit
      // send target and its text must never drift to whatever session
      // happens to be selected by the time the network round-trip
      // finishes (the request body below already used `selected` this
      // same way; this also fixes what happens to the UI AFTER the
      // response comes back, which previously always assumed nothing had
      // changed).
      const targetSession = selected;
      const sentText = inputTextEl.value;
      inputSendEl.disabled = true;
      // "Sending..." -- P13: brief in-flight feedback only, no retry logic
      // here or anywhere else client-side; a retry (if the operator wants
      // one) is a fresh, ordinary sendInput() call, and idempotency_key
      // (newIdempotencyKey() below) already makes a genuine network-level
      // double-submit of the SAME attempt safe at the backend layer.
      if (selected === targetSession) { setInputNote('Đang gửi…', false); }
      try {
        const response = await fetch('/dashboard/api/session/input', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            name: targetSession, text: sentText, press_enter: inputEnterEl.checked,
            idempotency_key: newIdempotencyKey(),
          }),
        });
        const data = await response.json();
        if (data.error) {
          if (selected === targetSession) {
            // "Failed" -- short reason inline; data itself (delivery_state/
            // submission_id/evidence/activation_attempts, when present) is
            // the "diagnostics" a caller inspecting the raw response/audit
            // log already has -- no separate diagnostics UI added here.
            setInputNote(`Gửi thất bại: ${data.error}${data.reason ? ' -- ' + data.reason : ''}`, true);
          }
          // else: the user has since switched away from targetSession --
          // an error for a now-invisible session must never paint onto
          // whichever different tab is currently showing.
        } else {
          // Clear only the EXACT submitted text, never newer typing in the
          // same session: the box stays editable while a send is in
          // flight, so the user may already be composing their NEXT
          // message by the time this response arrives -- wiping the box
          // unconditionally would destroy that unsent work. Compare
          // against what was actually sent before clearing either the
          // live box (still on targetSession) or the stored draft
          // (switched away and, less commonly, something already wrote a
          // new draft for it while gone).
          if (selected === targetSession) {
            if (inputTextEl.value === sentText) { inputTextEl.value = ''; drafts.set(targetSession, ''); }
            // "Accepted" vs "Unknown" -- SUBMIT_CONFIRMED/TEXT_SENT (a
            // plain append, nothing to confirm) clear the note entirely;
            // DELIVERY_UNKNOWN (Enter was sent but no adapter evidence
            // confirmed it within the verification window -- see
            // core.py's _send_text_and_verify_locked) stays visible and
            // does NOT auto-clear, so an operator does not miss a real
            // "did this actually run?" case merely because the HTTP call
            // itself returned 200. Never auto-retried from here.
            if (data.delivery_state === 'DELIVERY_UNKNOWN') {
              setInputNote(`Không rõ đã nhận hay chưa (Unknown)${data.submit_reason ? ' -- ' + data.submit_reason : ''}`, true);
            } else {
              setInputNote('');
            }
          } else if (drafts.get(targetSession) === sentText) {
            drafts.set(targetSession, '');
          }
        }
      } catch (error) {
        if (selected === targetSession) { setInputNote('Không thể gửi: ' + error, true); }
      } finally {
        refreshInputControls(); // always safe -- reflects whichever session is CURRENTLY selected
        if (selected === targetSession) { await loadDetail(); }
      }
    }
    inputSendEl.onclick = sendInput;
    inputTextEl.addEventListener('keydown', event => { if (event.key === 'Enter') sendInput(); });
    // ---- remember the last-viewed session (name only, nothing sensitive) --
    const LAST_SESSION_KEY = 'terminal-mcp:last-session';
    function rememberSession(name) {
      try { localStorage.setItem(LAST_SESSION_KEY, name); } catch (error) { /* private mode / storage disabled: not essential, ignore */ }
    }
    function recalledSession() {
      try { return localStorage.getItem(LAST_SESSION_KEY); } catch (error) { return null; }
    }
    let autoSelectAttempted = false;

    // ---- per-session read/input grants (dashboard-only; see grants.py) ----
    // Built with createElement/textContent only, same no-raw-HTML posture
    // as the rest of this file (see the supervisor panel's own comment on
    // this above). Exactly TWO ideas are ever shown to the operator here --
    // "Xem output" (read) and "Gửi prompt" (input) -- the underlying
    // allowed/whitelist/read_granted/input_granted vocabulary never
    // surfaces; a statically-whitelisted session (row.allowed) never shows
    // any grant control anywhere (row, tab, card, bulk-select) because it
    // has nothing to grant/revoke, ever -- that decision is made in one
    // place (grantable() below) and reused everywhere a control might
    // render, so it can never drift between the row/tab/card/bulk paths.
    //
    // Entry points, all opening the SAME #permModal:
    //   - a lock/eye/unlock icon on each grantable session's row (loadSessions)
    //   - the same icon on each grantable session's tab (makeTab/updateTabElement)
    //   - a compact "🔐 Quyền truy cập" line in the open session's own card,
    //     replacing the old always-multi-button inline bar (#grantBar below)
    //   - the bulk-select bar's own preset buttons, which apply directly
    //     without opening the modal at all (see renderBulkBar)
    //
    // Human-facing labels for the same reason codes grant_session_input/
    // _input_grant_block_reason already return -- purely a display
    // mapping, never a second copy of the actual authorization decision
    // (that stays server-side, computed exactly once, in core.py).
    const INPUT_BLOCK_LABELS = {
      INPUT_DISABLED: 'nhập liệu đang tắt toàn cục (permissions.terminal_input trong config.yaml)',
      ACCESS_DENIED: 'tên session khớp một mẫu bị cấm (input_policy.denied_session_patterns)',
      SENSITIVE_TARGET: 'lệnh đang chạy trong session là mục tiêu nhạy cảm (ssh/mysql/psql/sudo/passwd)',
      SENSITIVE_SESSION_NOT_GRANTABLE: 'tên session chứa từ nhạy cảm (root/ssh/password/secret/database)',
      SESSION_NOT_FOUND: 'session tmux này không còn tồn tại',
      INVALID_SESSION: 'tên session không hợp lệ',
    };
    function inputBlockLabel(reason) { return INPUT_BLOCK_LABELS[reason] || reason; }
    function grantable(row) { return !row.allowed; } // the one, reused "has anything to grant" test
    // 'full' (xem + gửi) | 'read' (chỉ xem) | 'none' (chưa cấp quyền) --
    // derived from the durable grant itself (grants.py), NOT from
    // effective_read/effective_input (which fold in the static whitelist
    // too -- irrelevant here since this is only ever called for a
    // grantable, i.e. non-whitelisted, row).
    function grantState(row) {
      if (row.grant && row.grant.input_enabled) return 'full';
      if (row.grant && row.grant.read_enabled) return 'read';
      return 'none';
    }
    function grantStateLabel(state) {
      return state === 'full' ? 'Xem + gửi' : state === 'read' ? 'Chỉ xem' : 'Chưa cấp quyền';
    }
    function effectiveLabel(row) {
      if (row.effective_input) return 'Xem + gửi';
      if (row.effective_read) return 'Chỉ xem';
      return 'Không truy cập';
    }
    function permIcon(row) {
      const state = grantState(row);
      return state === 'full' ? '🔓' : state === 'read' ? '👁' : '🔒';
    }

    // Bulk-select set (row checkboxes; see loadSessions) -- session names
    // currently checked for the bulk action bar below. Only ever contains
    // grantable() names (a whitelisted row never renders a checkbox).
    const bulkSelected = new Set();

    // One raw grant-read/grant-input mutation, no refresh of its own --
    // applyPreset below does exactly ONE combined loadSessions()+loadDetail()
    // refresh after every session in a preset (whether that's 1 or many)
    // is done, rather than one refresh per individual read/input call.
    async function postGrantRaw(path, name, enabled) {
      const response = await fetch(path, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, enabled}),
      });
      return response.json().catch(() => ({}));
    }
    // Applies preset ('full' | 'read' | 'none') to every session in
    // `names`, then refreshes from the backend -- the ONE place any grant
    // mutation actually happens, used by the modal's own preset buttons
    // and the bulk-select bar alike, so their outcome (and its real,
    // server-confirmed effective_read/effective_input) can never diverge.
    // 'read' explicitly revokes input too if it was on (a real downgrade,
    // not just "grant read and leave input untouched"); 'none' revokes
    // read, which grants.py's own set_read already cascades into revoking
    // input too -- see grants.py.
    async function applyPreset(names, preset) {
      const failures = [];
      for (const name of names) {
        if (preset === 'none') {
          const r = await postGrantRaw('/dashboard/api/session/grant-read', name, false);
          if (r && r.error) failures.push(`${name}: ${clean(r.error)}`);
          continue;
        }
        const r1 = await postGrantRaw('/dashboard/api/session/grant-read', name, true);
        if (r1 && r1.error) { failures.push(`${name}: ${clean(r1.error)}`); continue; }
        if (preset === 'read') {
          const current = lastKnownRows.find(x => x.name === name);
          if (current && current.grant && current.grant.input_enabled) {
            await postGrantRaw('/dashboard/api/session/grant-input', name, false);
          }
        } else { // 'full'
          const r2 = await postGrantRaw('/dashboard/api/session/grant-input', name, true);
          if (r2 && r2.error) failures.push(`${name}: ${clean(r2.error)}`);
        }
      }
      await loadSessions();
      if (selected && names.includes(selected)) await loadDetail();
      return failures;
    }

    // ---- #permModal: the one reusable grant/revoke UI ----------------------
    let permModalNames = null; // the session name(s) the open modal targets
    function closePermModal() {
      document.body.classList.remove('perm-modal-visible');
      permModalNames = null;
    }
    permModalCloseBtnEl.onclick = closePermModal;
    permBackdropEl.onclick = closePermModal;
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && document.body.classList.contains('perm-modal-visible')) closePermModal();
    });

    function renderPermModalBody(names) {
      const rows = names.map(n => lastKnownRows.find(r => r.name === n)).filter(Boolean);
      const bulk = names.length > 1;
      permModalTitleEl.textContent = bulk ? `${names.length} session đã chọn` : names[0];
      permModalBlockEl.textContent = '';
      permModalErrorEl.textContent = '';
      if (!bulk) {
        const row = rows[0];
        if (!row) { permModalStateEl.textContent = 'Session không còn tồn tại.'; permModalPresetsEl.replaceChildren(); return; }
        const state = grantState(row);
        const granted = grantStateLabel(state);
        const effective = effectiveLabel(row);
        permModalStateEl.textContent = granted === effective ? `Hiện tại: ${granted}` : `Đã cấp: ${granted} · Hiệu lực thực tế: ${effective}`;
        if (row.input_block_reason && state !== 'full') {
          permModalBlockEl.textContent = `Gửi prompt bị chặn: ${inputBlockLabel(row.input_block_reason)}`;
        }
      } else {
        permModalStateEl.textContent = `Áp dụng một hành động cho cả ${names.length} session.`;
      }

      permModalPresetsEl.replaceChildren();
      const presets = [
        {key: 'full', label: '🔓 Xem + gửi'},
        {key: 'read', label: '👁 Chỉ xem'},
        {key: 'none', label: '🔒 Thu hồi'},
      ];
      for (const preset of presets) {
        const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = preset.label;
        const classes = [];
        if (!bulk && grantState(rows[0]) === preset.key) classes.push('current');
        if (preset.key === 'none') classes.push('danger');
        if (classes.length) btn.className = classes.join(' ');
        btn.onclick = async () => {
          permModalPresetsEl.querySelectorAll('button').forEach(b => b.disabled = true);
          const failures = await applyPreset(names, preset.key);
          permModalErrorEl.textContent = failures.length ? `Một số session thất bại: ${failures.join('; ')}` : '';
          if (bulk) { bulkSelected.clear(); renderBulkBar(); closePermModal(); }
          else renderPermModalBody(names); // stay open, show the refreshed real state
        };
        permModalPresetsEl.appendChild(btn);
      }
    }

    async function openPermModal(names) {
      if (typeof names === 'string') names = [names];
      permModalNames = names;
      document.body.classList.add('perm-modal-visible');
      permModalTitleEl.textContent = names.length > 1 ? `${names.length} session đã chọn` : names[0];
      permModalStateEl.textContent = 'Đang tải…';
      permModalPresetsEl.replaceChildren(); permModalBlockEl.textContent = ''; permModalErrorEl.textContent = '';
      await loadSessions(); // fresh grant/effective state before showing presets, every time
      if (permModalNames === names) renderPermModalBody(names); // still open (not closed while awaiting)
    }

    // ---- bulk-select bar ---------------------------------------------------
    // Applies a preset directly to every checked session, WITHOUT opening
    // #permModal -- "chọn nhiều session -> một hành động", no extra step.
    function renderBulkBar() {
      bulkBarEl.hidden = bulkSelected.size === 0;
      bulkBarEl.replaceChildren();
      if (bulkSelected.size === 0) return;
      const label = document.createElement('span'); label.textContent = `${bulkSelected.size} đã chọn:`;
      const fullBtn = document.createElement('button'); fullBtn.type = 'button'; fullBtn.textContent = '🔓 Xem + gửi';
      const readBtn = document.createElement('button'); readBtn.type = 'button'; readBtn.textContent = '👁 Chỉ xem';
      const revokeBtn = document.createElement('button'); revokeBtn.type = 'button'; revokeBtn.className = 'danger'; revokeBtn.textContent = '🔒 Thu hồi';
      const clearBtn = document.createElement('button'); clearBtn.type = 'button'; clearBtn.className = 'link'; clearBtn.textContent = 'Bỏ chọn';
      const allBtns = [fullBtn, readBtn, revokeBtn, clearBtn];
      const run = (preset) => async () => {
        const names = [...bulkSelected];
        allBtns.forEach(b => b.disabled = true);
        bulkStatusEl.hidden = false; bulkStatusEl.textContent = `Đang áp dụng cho ${names.length} session…`;
        const failures = await applyPreset(names, preset);
        bulkSelected.clear(); renderBulkBar();
        bulkStatusEl.hidden = failures.length === 0;
        if (failures.length) bulkStatusEl.textContent = `Một số session thất bại: ${failures.join('; ')}`;
      };
      fullBtn.onclick = run('full');
      readBtn.onclick = run('read');
      revokeBtn.onclick = run('none');
      clearBtn.onclick = () => { bulkSelected.clear(); renderBulkBar(); loadSessions(); };
      bulkBarEl.append(label, fullBtn, readBtn, revokeBtn, clearBtn);
    }

    // Compact single-line entry point in the currently-open session's own
    // card (#grantBar, grid-row:3 -- see .detail's own comment above);
    // never shown for a statically-whitelisted session, which has nothing
    // to grant/revoke. Deliberately thin (one label + one button opening
    // #permModal) rather than the old multi-button inline bar, so this can
    // never reintroduce the real mobile overlap bug #permModal's own
    // separate overlay design structurally avoids.
    function renderGrantBar(name, allowed, grant, restricted, inputBlockReason, effectiveInput) {
      grantBarEl.replaceChildren();
      grantBarEl.hidden = true;
      if (allowed) return; // statically whitelisted -- nothing to grant/revoke, ever
      grantBarEl.hidden = false;
      const state = grant && grant.input_enabled ? 'full' : grant && grant.read_enabled ? 'read' : 'none';
      const granted = grantStateLabel(state);
      const effective = restricted ? 'Không truy cập' : (effectiveInput ? 'Xem + gửi' : 'Chỉ xem');
      const label = document.createElement('span');
      label.textContent = granted === effective ? `Quyền: ${effective}` : `Đã cấp: ${granted} · Hiệu lực: ${effective}`;
      const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = '🔐 Quyền truy cập';
      btn.onclick = () => openPermModal(name);
      grantBarEl.append(label, btn);
    }

    // Per-session unsent-draft text -- session-switch/detach race fix:
    // the single shared #inputText box previously kept whatever was typed
    // for the PREVIOUS session visible (and editable, and sendable) under
    // a newly-selected, unrelated session. In-memory only (not persisted
    // across a reload -- the box itself never was, either, before this).
    const drafts = new Map();

    // Shared by a manual click, a tab click, and the on-load auto-select
    // below so all three apply the exact same side effects (auto-follow
    // reset, drawer close, input-control refresh, and persisting the
    // choice for next visit).
    function selectSession(name) {
      // Re-selecting the ALREADY-active tab (clicking it again, or a
      // redundant programmatic call) must be a complete no-op: it must
      // never overwrite an in-progress, not-yet-saved draft with the
      // last-saved value for the same session, and must never reset
      // auto-follow/scroll/search state the user hasn't actually left.
      if (selected === name) return;
      if (selected) { drafts.set(selected, inputTextEl.value); }
      selected = name; inputAllowed = false; sidebarForcedOpen = false; // opening a session always closes the mobile drawer again
      inputTextEl.value = drafts.get(name) || '';
      // The previous session's output must never remain visible under the
      // new session's name while its own detail fetch is still in flight
      // (a tab click must not have to wait for the next 5s poll either).
      summaryEl.textContent = name; outputEl.replaceChildren();
      const loading = document.createElement('div'); loading.className = 'muted'; loading.textContent = 'Đang tải…';
      outputEl.appendChild(loading);
      // The previous session's grant controls (and their read/input-
      // enabled labeling) must never remain visible/clickable under the
      // new session's name either -- hidden until this target's own
      // detail arrives and renderGrantBar repaints it for real.
      grantBarEl.hidden = true; grantBarEl.replaceChildren();
      setAutoFollow(true); refreshInputControls(); refreshTermControls(); updateSidebarVisibility();
      rememberSession(name);
      closeSearch(); // a search from a different session's content wouldn't make sense to keep open
      renderSessionTabs(lastKnownRows); // reflect the new active tab immediately, not just on the next 5s poll
      // Fire immediately; loadDetail's own generation-sequence guard (see
      // below) discards this if the user switches again before it
      // resolves. A rejected fetch here is swallowed deliberately -- the
      // ordinary 5s poll (refresh()'s own try/catch) is what surfaces a
      // genuine OFFLINE/SIGN-IN-REQUIRED state; this one-off call must
      // never throw unhandled just because a click happened to race a
      // real outage.
      loadDetail().catch(() => {});
    }

    // URGENT incident fix: an expired Cloudflare Access browser session
    // makes every /dashboard/api/* fetch() land on Access's own login page
    // instead of this app's JSON -- fetch() follows that redirect
    // transparently (a normal 200 response, not a network error), so the
    // previous plain `response.json()` call threw a generic parse
    // exception indistinguishable from a real server/tunnel outage,
    // mislabeling a sign-in problem as "● OFFLINE".
    //
    // Deliberately requires POSITIVE evidence of an Access sign-in
    // redirect (the final URL landed on *.cloudflareaccess.com, or the
    // response carries Access's own `WWW-Authenticate: Cloudflare-Access`
    // challenge header, or a bare 401) before ever calling this "sign-in
    // required" -- an earlier version treated ANY non-JSON body as a sign-
    // in problem, which is wrong: a 502/503 from Cloudflare itself, an
    // nginx/proxy error page, or any other real backend failure is also
    // non-JSON, and mislabeling those as "sign in again" would send an
    // operator chasing the wrong fix for an actual outage. Every other
    // non-JSON/network/timeout failure still falls through to the
    // existing generic Error path below -- reported as OFFLINE, exactly
    // as before this fix, never silently swallowed either way. A non-2xx
    // status with a genuine JSON body is NOT treated as a failure here at
    // all -- this app's own routes legitimately answer denials that way
    // (403 READ_RESTRICTED, etc), and every caller already reads
    // `data.error` itself; only the response's CONTENT determines
    // success/failure at this layer, never its status code alone.
    class AuthRequiredError extends Error {}
    async function fetchJSON(url, options) {
      const response = await fetch(url, options);
      let landedOnAccessLogin = false;
      if (response.redirected) {
        try {
          const host = new URL(response.url).hostname;
          landedOnAccessLogin = host === 'cloudflareaccess.com' || host.endsWith('.cloudflareaccess.com');
        }
        catch (error) { /* response.url malformed/opaque -- fall through to the other signals below */ }
      }
      const accessChallengeHeader = (response.headers.get('www-authenticate') || '').includes('Cloudflare-Access');
      if (landedOnAccessLogin || accessChallengeHeader || response.status === 401) {
        throw new AuthRequiredError(`sign-in required for ${url} (status ${response.status})`);
      }
      // Status code alone is NOT the signal for "this is a real failure":
      // this app's own routes legitimately answer with a non-2xx status
      // (403 READ_RESTRICTED/ACCESS_DENIED, 400 INVALID_SESSION, ...) as
      // their NORMAL, documented way of reporting a denial -- the JSON
      // body itself (an `{"error": ...}` payload) is the actual data every
      // caller here (loadSessions/loadDetail/postGrant/...) already reads
      // and handles via `data.error`. This does NOT mean "any status
      // succeeds", though: this app's own routes never intentionally
      // answer with a 5xx (checked -- every status_code= in this file is
      // 200/400/403/404), so a 5xx is unconditionally a genuine failure
      // regardless of its body -- a JSON envelope on a 500 (a generic
      // framework error page, say) must still be reported as a real
      // failure, never silently parsed as if it were valid application
      // data with an empty/happy shape.
      if (response.status >= 500) {
        throw new Error(`server error from ${url}: status ${response.status}`);
      }
      // Only a body that ISN'T JSON at all (a proxy/gateway error page, an
      // nginx 502, etc, regardless of status code) is treated as a real,
      // generic failure past this point.
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        throw new Error(`unexpected response from ${url}: status ${response.status}, `
          + `content-type ${contentType || '(none)'}`);
      }
      return response.json();
    }

    // Detach = reversible remove/hide the session's TAB from this browser's
    // view only -- never kill-session, never clear its history/input,
    // never revoke any grant, never disconnect another client attached to
    // it. Scoped to THIS browser via localStorage (same storage class the
    // remembered-session/fullscreen preferences above already use); a
    // detached session is still fully visible/selectable from the
    // sidebar's own full session list (which never hides anything, and
    // never auto-detaches by name/pattern -- selecting it there
    // reattaches it, same as the "+" menu below), so it is never actually
    // lost, only out of the tab row until brought back.
    const DETACHED_KEY = 'terminal-mcp:detached-sessions';
    function loadDetached() {
      try { return new Set(JSON.parse(localStorage.getItem(DETACHED_KEY) || '[]')); }
      catch (error) { return new Set(); }
    }
    function saveDetached(set) {
      try { localStorage.setItem(DETACHED_KEY, JSON.stringify([...set])); }
      catch (error) { /* private mode / storage disabled: not essential, ignore */ }
    }
    let detachedSessions = loadDetached(); // never pre-populated -- nothing is auto-detached by name or pattern, ever

    function detachSession(name) {
      // Save the outgoing draft explicitly, BEFORE `selected` changes --
      // detaching must preserve it (reattaching later restores it) exactly
      // like an ordinary tab switch does, never discard it.
      if (selected === name) { drafts.set(name, inputTextEl.value); }
      detachedSessions.add(name);
      saveDetached(detachedSessions);
      if (selected === name) {
        // Move to a safe adjacent (still-attached) tab rather than leave
        // `selected` pointing at a name that no longer has a tab.
        const remaining = lastKnownRows.filter(row => !detachedSessions.has(row.name));
        if (remaining.length) {
          selectSession(remaining[0].name);
        } else {
          selected = null; inputAllowed = false;
          refreshInputControls(); refreshTermControls(); updateSidebarVisibility();
          summaryEl.textContent = 'Tất cả session đã được gỡ khỏi tab. Bấm "+" hoặc chọn từ danh sách bên trái để mở lại.';
          outputEl.replaceChildren(); grantBarEl.hidden = true;
        }
      }
      renderSessionTabs(lastKnownRows);
    }
    function reattachSession(name) {
      detachedSessions.delete(name);
      saveDetached(detachedSessions);
      closeDetachedMenu();
      selectSession(name);
    }
    function closeDetachedMenu() {
      detachedMenuOpen = false;
      document.querySelector('#detachedMenu')?.remove();
    }
    function toggleDetachedMenu(anchorEl, rows) {
      if (detachedMenuOpen) { closeDetachedMenu(); return; }
      detachedMenuOpen = true;
      const menu = document.createElement('div');
      menu.id = 'detachedMenu';
      const detachedRows = rows.filter(row => detachedSessions.has(row.name));
      if (!detachedRows.length) {
        const empty = document.createElement('div'); empty.className = 'muted-item';
        empty.textContent = 'Không có session nào đã gỡ.'; menu.appendChild(empty);
      } else {
        for (const row of detachedRows) {
          const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = `↩ ${row.name}`;
          btn.onclick = () => reattachSession(row.name);
          menu.appendChild(btn);
        }
      }
      document.body.appendChild(menu);
      const rect = anchorEl.getBoundingClientRect();
      menu.style.left = `${Math.round(rect.left + window.scrollX)}px`;
      menu.style.top = `${Math.round(rect.bottom + window.scrollY)}px`;
      // One-shot outside-click close -- deferred to the next tick so the
      // very click that opened the menu doesn't also immediately close it.
      setTimeout(() => {
        document.addEventListener('click', function handler(event) {
          if (!menu.contains(event.target)) { closeDetachedMenu(); document.removeEventListener('click', handler); }
        });
      }, 0);
    }

    // Browser-like tabs: one click switches session (no sidebar-open step).
    // Keyboard: each tab is a real <button> (native Tab-order + Enter/
    // Space activation); Delete/Backspace while a tab is focused detaches
    // it too, since the small "×" glyph alone is a poor keyboard target.
    // Stable tab order, independent of the API's own attention/activity
    // sort (rows arrive re-sorted on every poll -- looping them directly
    // would reorder the tab row itself every 5s, which real browser tabs
    // never do). A name already known keeps its position; a genuinely new
    // one is appended once, in alphabetical position among other newcomers
    // of the same poll (deterministic, never "wherever the API happened to
    // rank it this cycle"). Detaching/reattaching never reorders either --
    // it only changes which known names are currently visible.
    let tabOrder = [];
    // name -> {tab, nameEl, closeBtn} -- cached across renders so an
    // unchanged tab set patches in place (active/attention class only)
    // instead of destroying and recreating every DOM node, which would
    // otherwise reset horizontal scroll position and steal keyboard focus
    // away from whatever tab the user has focused, on every single poll.
    let tabElements = new Map();
    let lastRenderedTabOrder = null; // the exact visible (attached) ordered-name-list last painted, for the skip-rebuild check

    function updateTabElement(tab, refs, row) {
      const isActive = selected === row.name;
      const needsAttention = row.state === 'WAITING_INPUT';
      tab.className = 'session-tab' + (isActive ? ' active' : '') + (needsAttention ? ' needs-attention' : '');
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
      refs.attnEl.hidden = !needsAttention;
      // Perm icon only for a grantable (non-whitelisted) session -- a
      // whitelisted tab shows nothing extra, ever.
      refs.permEl.hidden = !grantable(row);
      if (!refs.permEl.hidden) refs.permEl.textContent = permIcon(row);
    }

    function makeTab(row) {
      const tab = document.createElement('button');
      tab.type = 'button';
      tab.setAttribute('role', 'tab');
      const nameEl = document.createElement('span'); nameEl.className = 'tab-name'; nameEl.textContent = row.name;
      tab.appendChild(nameEl);
      const attnEl = document.createElement('span'); attnEl.textContent = '⚠'; attnEl.hidden = true;
      tab.appendChild(attnEl);
      const permEl = document.createElement('span'); permEl.className = 'tab-perm'; permEl.title = 'Quyền truy cập'; permEl.hidden = true;
      permEl.onclick = (event) => { event.stopPropagation(); openPermModal(row.name); };
      tab.appendChild(permEl);
      const closeBtn = document.createElement('span'); closeBtn.className = 'tab-close'; closeBtn.textContent = '×';
      closeBtn.title = `Gỡ tab "${row.name}" (không xoá session, không thu hồi quyền)`;
      closeBtn.onclick = (event) => { event.stopPropagation(); detachSession(row.name); };
      tab.appendChild(closeBtn);
      tab.onclick = () => { if (selected !== row.name) selectSession(row.name); };
      tab.onkeydown = (event) => {
        if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); detachSession(row.name); }
      };
      const refs = {tab, attnEl, permEl};
      tabElements.set(row.name, refs);
      return tab;
    }

    function renderSessionTabs(rows) {
      lastKnownRows = rows;
      const rowsByName = new Map(rows.map(row => [row.name, row]));

      // Grow tabOrder with any genuinely new names, alphabetically among
      // this poll's newcomers; drop names that no longer exist at all
      // (a session actually gone from discovery -- if it reappears later
      // it re-enters as "new" again, which is fine).
      const known = new Set(tabOrder);
      const newcomers = rows.map(row => row.name).filter(name => !known.has(name)).sort();
      tabOrder = tabOrder.filter(name => rowsByName.has(name)).concat(newcomers);
      const tabOrderSet = new Set(tabOrder);
      for (const name of [...tabElements.keys()]) { if (!tabOrderSet.has(name)) tabElements.delete(name); }

      const visibleOrder = tabOrder.filter(name => rowsByName.has(name) && !detachedSessions.has(name));
      sessionTabsEl.hidden = rows.length === 0;

      const orderUnchanged = lastRenderedTabOrder !== null
        && visibleOrder.length === lastRenderedTabOrder.length
        && visibleOrder.every((name, i) => name === lastRenderedTabOrder[i]);

      if (orderUnchanged) {
        // Same tabs, same order -- patch active/attention state on the
        // EXISTING nodes only. Scroll position and focus are untouched
        // because no node is destroyed or replaced.
        for (const name of visibleOrder) {
          const refs = tabElements.get(name);
          if (refs) { updateTabElement(refs.tab, refs, rowsByName.get(name)); }
        }
        lastRenderedTabOrder = visibleOrder;
        return;
      }

      // Tab set/order actually changed (a session appeared, disappeared,
      // was detached, or was reattached) -- full rebuild is unavoidable
      // and acceptable only in that case.
      sessionTabsEl.replaceChildren();
      for (const name of visibleOrder) {
        const row = rowsByName.get(name);
        let refs = tabElements.get(name);
        const tab = refs ? refs.tab : makeTab(row);
        refs = tabElements.get(name);
        updateTabElement(tab, refs, row);
        sessionTabsEl.appendChild(tab);
      }
      const addBtn = document.createElement('button');
      addBtn.type = 'button'; addBtn.className = 'session-tab-add'; addBtn.title = 'Mở lại session đã gỡ';
      addBtn.textContent = '+';
      addBtn.onclick = (event) => { event.stopPropagation(); toggleDetachedMenu(addBtn, rows); };
      sessionTabsEl.appendChild(addBtn);
      lastRenderedTabOrder = visibleOrder;
    }

    async function loadSessions() {
      const data = await fetchJSON('/dashboard/api/sessions', {cache:'no-store'});
      const rows = data.sessions || [];
      // A 200 response can still legitimately carry data.error (e.g.
      // READ_DISABLED globally, alongside a correctly-empty `sessions: []`)
      // -- this is real, correct information ("reading is off"), not
      // "there simply are no sessions", and must never render identically
      // to a genuinely healthy empty state.
      document.querySelector('#count').textContent = data.error ? `(${clean(data.error)})` : `(${rows.length})`;

      // On first load only (never on the recurring 5s poll, which must not
      // fight a user's manual choice to switch sessions or clear the
      // selection): auto-open the remembered session if it still exists
      // and isn't detached, else the first attached, readable session (a
      // restricted row is real and listed now -- see
      // dashboard_list_sessions -- but auto-opening one would only greet
      // a first-time viewer with a locked placeholder instead of real
      // output; a detached one is real too, but the whole point of
      // detaching is staying out of the way until explicitly reattached).
      const readableRows = rows.filter(row => row.effective_read && !detachedSessions.has(row.name));
      if (!autoSelectAttempted) {
        autoSelectAttempted = true;
        if (!selected && readableRows.length) {
          const remembered = recalledSession();
          const target = (remembered && readableRows.some(row => row.name === remembered)) ? remembered : readableRows[0].name;
          selectSession(target);
        }
      }

      renderSessionTabs(rows);
      // Drop any bulk-selected name that's gone or no longer grantable
      // (session vanished, or became statically whitelisted) -- never
      // leaves a stale, unactionable checkbox state behind.
      const rowsByName = new Map(rows.map(row => [row.name, row]));
      let bulkChanged = false;
      for (const name of [...bulkSelected]) {
        const row = rowsByName.get(name);
        if (!row || !grantable(row)) { bulkSelected.delete(name); bulkChanged = true; }
      }
      if (bulkChanged) renderBulkBar();
      sessionsEl.replaceChildren();
      for (const row of rows) {
        // Rows already arrive sorted attention-first, then most-recent-
        // activity, then name (see the /dashboard/api/sessions route) — no
        // client-side reordering here, just rendering in the given order.
        const needsAttention = row.state === 'WAITING_INPUT';
        const canGrant = grantable(row);
        // A <div role="button">, not a real <button> -- it hosts a real
        // <button> (perm-btn) and an <input type=checkbox>, neither legal
        // inside a <button>. onkeydown below restores Enter/Space
        // activation that a native <button> would have given for free.
        const div = document.createElement('div');
        div.className = 'session' + (selected === row.name ? ' active' : '') + (needsAttention ? ' needs-attention' : '');
        div.setAttribute('role', 'button'); div.tabIndex = 0;

        if (canGrant) {
          const check = document.createElement('input'); check.type = 'checkbox'; check.className = 'sess-check';
          check.checked = bulkSelected.has(row.name);
          check.setAttribute('aria-label', `Chọn ${row.name} để thao tác hàng loạt`);
          check.onclick = (event) => event.stopPropagation(); // never trigger the row's own select-session activation
          check.onchange = () => {
            if (check.checked) bulkSelected.add(row.name); else bulkSelected.delete(row.name);
            renderBulkBar();
          };
          div.append(check);
        }

        const main = document.createElement('div'); main.className = 'sess-main';
        const name = document.createElement('div'); name.className = 'name'; name.textContent = row.name;
        if (needsAttention) {
          const badge = document.createElement('span'); badge.className = 'attn-badge'; badge.textContent = '⚠ NEEDS INPUT';
          name.append(' ', badge);
        } else if (!row.effective_read) {
          // Newly-discovered, not-yet-granted session -- still listed
          // (name/attached/windows/activity are tmux metadata, never pane
          // content), just visibly marked as not readable yet. Admin can
          // grant it directly via the perm-btn on this same row.
          const badge = document.createElement('span'); badge.className = 'lock-badge'; badge.textContent = '🔒 Chưa cấp quyền';
          name.append(' ', badge);
        }
        const meta = document.createElement('div'); meta.className = 'meta';
        const dot = document.createElement('span'); dot.className = 'attach-dot' + (row.attached ? ' on' : '');
        meta.append(dot, document.createTextNode(`${row.windows} window · ${row.attached ? 'attached' : 'detached'}`));
        main.append(name, meta);
        div.append(main);

        if (canGrant) {
          const permBtn = document.createElement('button'); permBtn.type = 'button'; permBtn.className = 'perm-btn';
          permBtn.textContent = permIcon(row); permBtn.title = 'Quyền truy cập';
          permBtn.onclick = (event) => { event.stopPropagation(); openPermModal(row.name); };
          div.append(permBtn);
        }

        const activate = () => {
          // The full sidebar list never hides anything -- picking a
          // detached session here is exactly "obvious reattach via the
          // detached sessions list": bring its tab back, then select it.
          if (detachedSessions.has(row.name)) {
            detachedSessions.delete(row.name); saveDetached(detachedSessions); renderSessionTabs(lastKnownRows);
          }
          selectSession(row.name); loadSessions(); // selectSession itself already fires loadDetail() immediately
        };
        div.onclick = activate;
        div.onkeydown = (event) => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); }
        };
        sessionsEl.append(div);
      }
      if (selected && !rows.some(row => row.name === selected)) {
        selected = null; inputAllowed = false; refreshInputControls(); refreshTermControls(); updateSidebarVisibility();
        if (fullscreenTerminal) setFullscreen(false, { persist: false }); // forced exit — the remembered preference is unrelated and must survive
        summaryEl.textContent = 'Session không còn tồn tại.'; outputEl.replaceChildren(); grantBarEl.hidden = true;
      }
    }
    async function loadDetail() {
      if (!selected) return;
      const requestedSession = selected;
      // Generation counter, not just a session-name check: a plain "is
      // `selected` still this name" guard alone misses A -> B -> A --
      // switching back to A starts a SECOND, fresh request for A while an
      // earlier, now-stale one for A is still in flight; both would pass
      // a name-only check, and the older one resolving last could paint
      // outdated content over the newer fetch's result. Only the request
      // holding the CURRENT sequence number when its response arrives is
      // allowed to render.
      const mySequence = ++loadDetailSequence;
      const data = await fetchJSON(`/dashboard/api/session?name=${encodeURIComponent(requestedSession)}`, {cache:'no-store'});
      if (selected !== requestedSession || mySequence !== loadDetailSequence) return; // stale -- session changed, or a newer request for it has since started
      if (data.error) {
        if (data.error === 'READ_RESTRICTED') {
          // Locked placeholder, not a generic error line -- this session
          // is real (it's in the list) and its output becomes visible the
          // instant read is granted, no page reload needed.
          summaryEl.replaceChildren();
          const strong = document.createElement('strong'); strong.textContent = selected + ' · ';
          const note = document.createElement('span'); note.className = 'muted'; note.textContent = 'chưa được cấp quyền xem';
          summaryEl.append(strong, note);
          outputEl.replaceChildren();
          const placeholder = document.createElement('div'); placeholder.className = 'muted';
          placeholder.textContent = '🔒 Output bị khoá. Session này chưa được cấp quyền xem.';
          outputEl.append(placeholder);
          renderGrantBar(selected, false, null, true, data.input_block_reason || null, false);
        } else {
          summaryEl.textContent = `${data.error}: ${selected}`; outputEl.replaceChildren();
          grantBarEl.hidden = true;
        }
        inputAllowed = false; refreshInputControls(); refreshTermControls();
        return;
      }
      renderGrantBar(selected, Boolean(data.allowed), data.grant || null, false, data.input_block_reason || null, Boolean(data.input_allowed));
      summaryEl.replaceChildren();
      const strong = document.createElement('strong'); strong.textContent = selected + ' · ';
      // Session status (RUNNING / WAITING_INPUT / PLAN_APPROVAL / ... and its
      // reason) stays visible above the terminal pane exactly as before.
      const state = document.createElement('span'); state.className = `state-${clean(data.status.state)}`; state.textContent = clean(data.status.state);
      const reason = document.createElement('span'); reason.className = 'muted'; reason.textContent = ` — ${clean(data.status.reason)}`;
      if (data.status.state === 'WAITING_INPUT') {
        const badge = document.createElement('span'); badge.className = 'attn-badge'; badge.textContent = '⚠ NEEDS INPUT';
        summaryEl.append(badge, document.createTextNode(' '));
      }
      summaryEl.append(strong, state, reason);
      const switchedSession = selected !== lastRenderedSession;
      if (switchedSession) setAutoFollow(true); // opening a session always starts followed
      renderAnsi(outputEl, clean(data.tail.output));
      // Lines render oldest-first/newest-last (tmux's natural order); only
      // snap to the bottom while auto-follow is on, so a user who has
      // intentionally scrolled up to read history is never pulled back down.
      if (autoFollow) { outputEl.scrollTop = outputEl.scrollHeight; }
      lastRenderedSession = selected;
      inputAllowed = Boolean(data.input_allowed); refreshInputControls(); refreshTermControls();
    }
    // ---- connection health -------------------------------------------------
    // No new backend privilege: this only reacts to the same two fetches
    // refresh() already made. Last-rendered output/status is left exactly as
    // it was on failure (never cleared) — only dimmed via #output.stale and
    // flagged via the header badge, so it reads as "maybe stale", not "still
    // live". Auto-recovers the moment a poll succeeds again; the 5s
    // setInterval below is already the retry loop, so RECONNECTING doubles
    // as "retrying" with no separate timer needed.
    let consecutiveFailures = 0;
    function setConnectionState(ok) {
      if (ok) {
        consecutiveFailures = 0;
        liveBadgeEl.textContent = '● LIVE';
        liveBadgeEl.className = 'live';
        outputEl.classList.remove('stale');
      } else {
        consecutiveFailures++;
        const offline = consecutiveFailures >= 3; // a couple of blips read as reconnecting; sustained failure reads as offline
        liveBadgeEl.textContent = offline ? '● OFFLINE' : '● RECONNECTING…';
        liveBadgeEl.className = offline ? 'live offline' : 'live reconnecting';
        if (selected) outputEl.classList.add('stale');
      }
    }
    // Distinct from setConnectionState(false): a stale/expired Cloudflare
    // Access browser session is not a server, tunnel, or network outage --
    // telling an operator to "reload and sign in again" is the correct,
    // actionable fix, and reporting it as OFFLINE would send them chasing
    // a service outage that does not exist. Every 5s retry (refresh()
    // below) re-checks and clears this the moment a fresh sign-in makes
    // the API return JSON again, exactly like the OFFLINE path recovers.
    function setAuthRequiredState() {
      consecutiveFailures = 0;
      liveBadgeEl.textContent = '● SIGN-IN REQUIRED — reload the page';
      liveBadgeEl.className = 'live auth-required';
      if (selected) outputEl.classList.add('stale');
    }
    // Restored only once, right after the very first successful load — by
    // then the restored/first session is already selected, rendered, and
    // scrolled to its latest line (see loadDetail's switchedSession path),
    // so this never competes with or skips that initial scroll-to-bottom;
    // it just layers fullscreen presentation on top afterward.
    let fullscreenRestoreAttempted = false;
    async function refresh() {
      try {
        await loadSessions(); await loadDetail(); setConnectionState(true);
        await loadSupervisor(); // independent of session connectivity above; never affects the LIVE/RECONNECTING badge
        await loadSupervisorV2();
        if (!fullscreenRestoreAttempted) {
          fullscreenRestoreAttempted = true;
          if (selected && recalledFullscreen()) setFullscreen(true, { persist: false });
        }
      } catch (error) {
        if (error instanceof AuthRequiredError) { setAuthRequiredState(); } else { setConnectionState(false); }
      }
    }
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>"""


# A separate, dedicated admin screen -- "Quản lý session": one table with
# every real tmux session (never filtered/hidden), its permission state,
# attach state, and a detach toggle, plus bulk-select. Reads the exact
# same /dashboard/api/sessions data DASHBOARD_HTML's sidebar already uses
# (no new backend endpoint) and reuses the identical grant/revoke
# semantics (grant-read/grant-input, same three presets, same "Xem
# output"/"Gửi prompt" vocabulary) -- this is genuinely the same
# capability as the main page's row/tab/card icons and bulk bar, just
# surfaced as its own full-width, sortable-by-nothing-fancy table for
# when the session count grows past what a sidebar comfortably shows.
# Detach here writes the SAME localStorage key (terminal-mcp:detached-
# sessions) the main dashboard's tabs already read, so toggling it here
# is immediately reflected in the main page's tab strip and vice versa.
SESSIONS_ADMIN_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Quản lý session</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --green:#43d17c; --amber:#ffc857; --accent:#5b8cff; --mono: ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace; }
    * { box-sizing:border-box }
    html, body { height:100vh; height:100dvh; overflow:hidden }
    body { margin:0; font:14px/1.5 var(--mono); background:var(--bg); color:var(--text); display:flex; flex-direction:column }
    header { flex:0 0 auto; display:flex; justify-content:space-between; gap:16px; align-items:center; padding:18px 24px; border-bottom:1px solid var(--line); flex-wrap:wrap }
    h1 { margin:0; font-size:18px } .muted { color:var(--muted) } .live { color:var(--green); font-size:12px }
    .live.offline { color:#ff6b6b } .live.reconnecting { color:var(--amber) } .live.auth-required { color:#ffb347 }
    a.back { color:var(--muted); text-decoration:none; font-size:12px; border:1px solid var(--line); border-radius:999px; padding:4px 10px }
    a.back:hover { color:var(--text); border-color:var(--muted) }
    .toolbar { flex:0 0 auto; display:flex; align-items:center; gap:10px; padding:10px 24px; border-bottom:1px solid var(--line); flex-wrap:wrap }
    .toolbar input[type=text] { background:#0f1730; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:7px 10px; font:inherit; font-size:13px; min-width:200px }
    .toolbar label { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:12px }
    #count { color:var(--muted); font-size:12px; margin-left:auto }
    #bulkBar { flex:0 0 auto; display:flex; align-items:center; flex-wrap:wrap; gap:6px; padding:8px 24px; border-bottom:1px solid var(--line); font-size:12px; color:var(--muted) }
    #bulkBar[hidden] { display:none }
    #bulkBar button { background:#2b3f66; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:5px 10px; cursor:pointer; font:inherit; font-size:12px }
    #bulkBar button.danger { background:#3a2430 }
    #bulkBar button.link { background:transparent; border:none; color:var(--muted); text-decoration:underline }
    #bulkBar button:disabled { opacity:.5; cursor:not-allowed }
    main { flex:1; min-height:0; overflow:auto; padding:0 24px 24px }
    table { width:100%; border-collapse:collapse; font-size:13px; min-width:760px }
    thead th { position:sticky; top:0; background:var(--bg); text-align:left; color:var(--muted); font-weight:600; padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap }
    tbody td { padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:middle }
    tbody tr:hover { background:#121a2d }
    tbody tr.needs-attention { background:rgba(255,200,87,.06) }
    .sess-name { font-weight:700 }
    .attn-badge { display:inline-block; background:var(--amber); color:#231a00; font-size:10px; font-weight:700; padding:1px 6px; border-radius:4px; margin-left:6px; vertical-align:middle }
    .perm-badge { display:inline-block; border-radius:999px; padding:2px 9px; font-size:11px; border:1px solid var(--line); white-space:nowrap }
    .perm-badge.whitelist { color:var(--muted) }
    .perm-badge.full { color:var(--green); border-color:var(--green) }
    .perm-badge.read { color:#8fb8ff; border-color:#8fb8ff }
    .perm-badge.none { color:#ff9f9f; border-color:#ff9f9f }
    .attach-dot { display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--line); margin-right:5px; vertical-align:middle }
    .attach-dot.on { background:var(--green) }
    .row-actions { display:flex; gap:6px; align-items:center; white-space:nowrap }
    .row-actions button { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:4px 9px; cursor:pointer; font:inherit; font-size:12px }
    .row-actions button:hover { background:#233252 }
    .row-actions button.on { border-color:var(--amber); color:var(--amber) }
    .empty-row td { text-align:center; color:var(--muted); padding:32px 8px }
    /* Permission modal -- identical component/behavior to the main
       dashboard's own #permModal (see DASHBOARD_HTML), duplicated here
       deliberately rather than shared (this is a standalone page, no
       shared JS module mechanism in this project -- see other pages'
       own precedent for small, self-contained duplication over a new
       cross-page dependency). */
    #permBackdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:30 }
    #permModal {
      display:none; position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); z-index:31;
      width:min(360px, calc(100vw - 32px)); max-height:80vh; overflow:auto;
      background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:0 20px 50px rgba(0,0,0,.6);
    }
    body.perm-modal-visible #permBackdrop, body.perm-modal-visible #permModal { display:block }
    #permModal .pm-head { padding:14px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:10px }
    #permModal .pm-head strong { overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    #permModal .pm-body { padding:14px 16px; display:flex; flex-direction:column; gap:10px }
    #permModal button.close { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:4px 9px; cursor:pointer; font:inherit }
    .pm-state { font-size:12px; color:var(--muted) }
    .pm-presets { display:flex; flex-direction:column; gap:8px }
    .pm-presets button { background:#2b3f66; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:10px 12px; cursor:pointer; font:inherit; font-size:13px; text-align:left }
    .pm-presets button.current { border-color:var(--green) }
    .pm-presets button.danger { background:#3a2430 }
    .pm-presets button:disabled { opacity:.5; cursor:not-allowed }
    .pm-block { color:#ff9f9f; font-size:11px }
    .pm-error { color:#ff6b6b; font-size:12px }
    @media (max-width:760px) {
      header { padding:12px 14px } .toolbar { padding:8px 14px } main { padding:0 14px 14px }
      #bulkBar { padding:8px 14px }
    }
  </style>
</head>
<body>
  <header>
    <div><h1>Quản lý session</h1><div class="muted">Toàn bộ session tmux thật, quyền, và trạng thái detach</div></div>
    <div style="display:flex;align-items:center;gap:10px">
      <a class="back" href="/dashboard">← Terminal</a>
      <span class="live" id="liveBadge">● LIVE</span>
    </div>
  </header>
  <div class="toolbar">
    <input type="text" id="searchBox" placeholder="Tìm theo tên session...">
    <label><input type="checkbox" id="onlyGrantable"> Chỉ hiện session chưa whitelist</label>
    <span id="count"></span>
  </div>
  <div id="bulkBar" hidden></div>
  <main>
    <table>
      <thead>
        <tr>
          <th></th><th>Session</th><th>Quyền</th><th>Trạng thái</th><th>Windows</th><th>Hoạt động gần nhất</th><th>Detach</th><th>Hành động</th>
        </tr>
      </thead>
      <tbody id="tbody"></tbody>
    </table>
  </main>
  <div id="permBackdrop"></div>
  <div id="permModal" role="dialog" aria-modal="true" aria-labelledby="permModalTitle">
    <div class="pm-head">
      <strong id="permModalTitle"></strong>
      <button id="permModalCloseBtn" class="close" type="button">✕</button>
    </div>
    <div class="pm-body">
      <div class="pm-state" id="permModalState"></div>
      <div class="pm-presets" id="permModalPresets"></div>
      <div class="pm-block" id="permModalBlock"></div>
      <div class="pm-error" id="permModalError"></div>
    </div>
  </div>
  <script>
    const tbodyEl = document.querySelector('#tbody');
    const countEl = document.querySelector('#count');
    const searchEl = document.querySelector('#searchBox');
    const onlyGrantableEl = document.querySelector('#onlyGrantable');
    const bulkBarEl = document.querySelector('#bulkBar');
    const liveBadgeEl = document.querySelector('#liveBadge');
    const permBackdropEl = document.querySelector('#permBackdrop');
    const permModalTitleEl = document.querySelector('#permModalTitle');
    const permModalStateEl = document.querySelector('#permModalState');
    const permModalPresetsEl = document.querySelector('#permModalPresets');
    const permModalBlockEl = document.querySelector('#permModalBlock');
    const permModalErrorEl = document.querySelector('#permModalError');
    const permModalCloseBtnEl = document.querySelector('#permModalCloseBtn');

    function clean(value) { return value == null ? '' : String(value); }

    // Same positive-evidence-of-a-sign-in-redirect fetch wrapper as the
    // main dashboard (see DASHBOARD_HTML's own fetchJSON for the full
    // rationale) -- kept byte-identical in spirit, duplicated for the
    // same "standalone page" reason as the modal above.
    class AuthRequiredError extends Error {}
    async function fetchJSON(url, options) {
      const response = await fetch(url, options);
      let landedOnAccessLogin = false;
      if (response.redirected) {
        try {
          const host = new URL(response.url).hostname;
          landedOnAccessLogin = host === 'cloudflareaccess.com' || host.endsWith('.cloudflareaccess.com');
        } catch (error) { /* opaque/malformed response.url -- fall through */ }
      }
      const accessChallengeHeader = (response.headers.get('www-authenticate') || '').includes('Cloudflare-Access');
      if (landedOnAccessLogin || accessChallengeHeader || response.status === 401) {
        throw new AuthRequiredError(`sign-in required for ${url} (status ${response.status})`);
      }
      if (response.status >= 500) throw new Error(`server error from ${url}: status ${response.status}`);
      const contentType = response.headers.get('content-type') || '';
      if (!contentType.includes('application/json')) {
        throw new Error(`unexpected response from ${url}: status ${response.status}, content-type ${contentType || '(none)'}`);
      }
      return response.json();
    }

    const DETACHED_KEY = 'terminal-mcp:detached-sessions';
    function loadDetached() {
      try { return new Set(JSON.parse(localStorage.getItem(DETACHED_KEY) || '[]')); }
      catch (error) { return new Set(); }
    }
    function saveDetached(set) {
      try { localStorage.setItem(DETACHED_KEY, JSON.stringify([...set])); }
      catch (error) { /* private mode / storage disabled: not essential, ignore */ }
    }
    let detachedSessions = loadDetached();

    function grantable(row) { return !row.allowed; }
    function grantState(row) {
      if (row.grant && row.grant.input_enabled) return 'full';
      if (row.grant && row.grant.read_enabled) return 'read';
      return 'none';
    }
    function grantStateLabel(state) {
      return state === 'full' ? 'Xem + gửi' : state === 'read' ? 'Chỉ xem' : 'Chưa cấp quyền';
    }
    const INPUT_BLOCK_LABELS = {
      INPUT_DISABLED: 'nhập liệu đang tắt toàn cục (permissions.terminal_input trong config.yaml)',
      ACCESS_DENIED: 'tên session khớp một mẫu bị cấm (input_policy.denied_session_patterns)',
      SENSITIVE_TARGET: 'lệnh đang chạy trong session là mục tiêu nhạy cảm (ssh/mysql/psql/sudo/passwd)',
      SENSITIVE_SESSION_NOT_GRANTABLE: 'tên session chứa từ nhạy cảm (root/ssh/password/secret/database)',
      SESSION_NOT_FOUND: 'session tmux này không còn tồn tại',
      INVALID_SESSION: 'tên session không hợp lệ',
    };
    function inputBlockLabel(reason) { return INPUT_BLOCK_LABELS[reason] || reason; }

    let lastKnownRows = [];
    const bulkSelected = new Set();

    async function postGrantRaw(path, name, enabled) {
      const response = await fetch(path, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, enabled}),
      });
      return response.json().catch(() => ({}));
    }
    async function applyPreset(names, preset) {
      const failures = [];
      for (const name of names) {
        if (preset === 'none') {
          const r = await postGrantRaw('/dashboard/api/session/grant-read', name, false);
          if (r && r.error) failures.push(`${name}: ${clean(r.error)}`);
          continue;
        }
        const r1 = await postGrantRaw('/dashboard/api/session/grant-read', name, true);
        if (r1 && r1.error) { failures.push(`${name}: ${clean(r1.error)}`); continue; }
        if (preset === 'read') {
          const current = lastKnownRows.find(x => x.name === name);
          if (current && current.grant && current.grant.input_enabled) {
            await postGrantRaw('/dashboard/api/session/grant-input', name, false);
          }
        } else {
          const r2 = await postGrantRaw('/dashboard/api/session/grant-input', name, true);
          if (r2 && r2.error) failures.push(`${name}: ${clean(r2.error)}`);
        }
      }
      await load();
      return failures;
    }

    let permModalNames = null;
    function closePermModal() { document.body.classList.remove('perm-modal-visible'); permModalNames = null; }
    permModalCloseBtnEl.onclick = closePermModal;
    permBackdropEl.onclick = closePermModal;
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && document.body.classList.contains('perm-modal-visible')) closePermModal();
    });

    function renderPermModalBody(names) {
      const rows = names.map(n => lastKnownRows.find(r => r.name === n)).filter(Boolean);
      const bulk = names.length > 1;
      permModalTitleEl.textContent = bulk ? `${names.length} session đã chọn` : names[0];
      permModalBlockEl.textContent = ''; permModalErrorEl.textContent = '';
      if (!bulk) {
        const row = rows[0];
        if (!row) { permModalStateEl.textContent = 'Session không còn tồn tại.'; permModalPresetsEl.replaceChildren(); return; }
        const state = grantState(row);
        const granted = grantStateLabel(state);
        const effective = row.effective_input ? 'Xem + gửi' : row.effective_read ? 'Chỉ xem' : 'Không truy cập';
        permModalStateEl.textContent = granted === effective ? `Hiện tại: ${granted}` : `Đã cấp: ${granted} · Hiệu lực thực tế: ${effective}`;
        if (row.input_block_reason && state !== 'full') {
          permModalBlockEl.textContent = `Gửi prompt bị chặn: ${inputBlockLabel(row.input_block_reason)}`;
        }
      } else {
        permModalStateEl.textContent = `Áp dụng một hành động cho cả ${names.length} session.`;
      }
      permModalPresetsEl.replaceChildren();
      const presets = [{key:'full', label:'🔓 Xem + gửi'}, {key:'read', label:'👁 Chỉ xem'}, {key:'none', label:'🔒 Thu hồi'}];
      for (const preset of presets) {
        const btn = document.createElement('button'); btn.type = 'button'; btn.textContent = preset.label;
        const classes = [];
        if (!bulk && grantState(rows[0]) === preset.key) classes.push('current');
        if (preset.key === 'none') classes.push('danger');
        if (classes.length) btn.className = classes.join(' ');
        btn.onclick = async () => {
          permModalPresetsEl.querySelectorAll('button').forEach(b => b.disabled = true);
          const failures = await applyPreset(names, preset.key);
          permModalErrorEl.textContent = failures.length ? `Một số session thất bại: ${failures.join('; ')}` : '';
          if (bulk) { bulkSelected.clear(); renderBulkBar(); closePermModal(); }
          else renderPermModalBody(names);
        };
        permModalPresetsEl.appendChild(btn);
      }
    }
    async function openPermModal(names) {
      if (typeof names === 'string') names = [names];
      permModalNames = names;
      document.body.classList.add('perm-modal-visible');
      permModalTitleEl.textContent = names.length > 1 ? `${names.length} session đã chọn` : names[0];
      permModalStateEl.textContent = 'Đang tải…';
      permModalPresetsEl.replaceChildren(); permModalBlockEl.textContent = ''; permModalErrorEl.textContent = '';
      await load();
      if (permModalNames === names) renderPermModalBody(names);
    }

    function renderBulkBar() {
      bulkBarEl.hidden = bulkSelected.size === 0;
      bulkBarEl.replaceChildren();
      if (bulkSelected.size === 0) return;
      const label = document.createElement('span'); label.textContent = `${bulkSelected.size} đã chọn:`;
      const fullBtn = document.createElement('button'); fullBtn.type='button'; fullBtn.textContent='🔓 Xem + gửi';
      const readBtn = document.createElement('button'); readBtn.type='button'; readBtn.textContent='👁 Chỉ xem';
      const revokeBtn = document.createElement('button'); revokeBtn.type='button'; revokeBtn.className='danger'; revokeBtn.textContent='🔒 Thu hồi';
      const clearBtn = document.createElement('button'); clearBtn.type='button'; clearBtn.className='link'; clearBtn.textContent='Bỏ chọn';
      const status = document.createElement('span'); status.className = 'muted';
      const allBtns = [fullBtn, readBtn, revokeBtn, clearBtn];
      const run = (preset) => async () => {
        const names = [...bulkSelected];
        allBtns.forEach(b => b.disabled = true);
        status.textContent = `Đang áp dụng cho ${names.length} session…`;
        const failures = await applyPreset(names, preset);
        bulkSelected.clear(); renderBulkBar();
        if (failures.length) { bulkBarEl.hidden = false; bulkBarEl.appendChild(status); status.textContent = `Một số session thất bại: ${failures.join('; ')}`; }
      };
      fullBtn.onclick = run('full'); readBtn.onclick = run('read'); revokeBtn.onclick = run('none');
      clearBtn.onclick = () => { bulkSelected.clear(); renderBulkBar(); };
      bulkBarEl.append(label, fullBtn, readBtn, revokeBtn, clearBtn);
    }

    function timeAgo(iso) {
      if (!iso) return '—';
      const ms = Date.now() - new Date(iso).getTime();
      if (!Number.isFinite(ms) || ms < 0) return iso;
      const s = Math.floor(ms / 1000);
      if (s < 60) return `${s}s trước`;
      const m = Math.floor(s / 60);
      if (m < 60) return `${m}p trước`;
      const h = Math.floor(m / 60);
      if (h < 24) return `${h}g trước`;
      return `${Math.floor(h / 24)}ngày trước`;
    }

    function renderRows(rows) {
      tbodyEl.replaceChildren();
      const query = searchEl.value.trim().toLowerCase();
      const filtered = rows.filter(row => {
        if (query && !row.name.toLowerCase().includes(query)) return false;
        if (onlyGrantableEl.checked && !grantable(row)) return false;
        return true;
      });
      if (!filtered.length) {
        const tr = document.createElement('tr'); tr.className = 'empty-row';
        const td = document.createElement('td'); td.colSpan = 8;
        td.textContent = rows.length ? 'Không có session khớp bộ lọc.' : 'Không có session nào.';
        tr.appendChild(td); tbodyEl.appendChild(tr);
        return;
      }
      for (const row of filtered) {
        const tr = document.createElement('tr');
        if (row.state === 'WAITING_INPUT') tr.className = 'needs-attention';

        const tdCheck = document.createElement('td');
        if (grantable(row)) {
          const check = document.createElement('input'); check.type = 'checkbox'; check.checked = bulkSelected.has(row.name);
          check.setAttribute('aria-label', `Chọn ${row.name}`);
          check.onchange = () => { if (check.checked) bulkSelected.add(row.name); else bulkSelected.delete(row.name); renderBulkBar(); };
          tdCheck.appendChild(check);
        }
        tr.appendChild(tdCheck);

        const tdName = document.createElement('td');
        const nameSpan = document.createElement('span'); nameSpan.className = 'sess-name'; nameSpan.textContent = row.name;
        tdName.appendChild(nameSpan);
        if (row.state === 'WAITING_INPUT') {
          const badge = document.createElement('span'); badge.className = 'attn-badge'; badge.textContent = '⚠ CẦN INPUT';
          tdName.appendChild(badge);
        }
        tr.appendChild(tdName);

        const tdPerm = document.createElement('td');
        const permBadge = document.createElement('span'); permBadge.className = 'perm-badge';
        if (!grantable(row)) { permBadge.classList.add('whitelist'); permBadge.textContent = 'Whitelist tĩnh'; }
        else {
          const state = grantState(row);
          permBadge.classList.add(state);
          permBadge.textContent = grantStateLabel(state);
        }
        tdPerm.appendChild(permBadge);
        tr.appendChild(tdPerm);

        const tdAttach = document.createElement('td');
        const dot = document.createElement('span'); dot.className = 'attach-dot' + (row.attached ? ' on' : '');
        tdAttach.append(dot, document.createTextNode(row.attached ? 'attached' : 'detached'));
        tr.appendChild(tdAttach);

        const tdWindows = document.createElement('td'); tdWindows.textContent = row.windows;
        tr.appendChild(tdWindows);

        const tdActivity = document.createElement('td'); tdActivity.textContent = timeAgo(row.activity);
        tr.appendChild(tdActivity);

        const tdDetach = document.createElement('td');
        const isDetached = detachedSessions.has(row.name);
        const detachBtn = document.createElement('button');
        detachBtn.type = 'button'; detachBtn.textContent = isDetached ? 'Gỡ tab' : 'Bấm để gỡ tab';
        if (isDetached) detachBtn.className = 'on';
        detachBtn.title = 'Chỉ ẩn/hiện tab trên trình duyệt này -- không đụng session tmux hay quyền thật';
        detachBtn.onclick = () => {
          if (detachedSessions.has(row.name)) detachedSessions.delete(row.name); else detachedSessions.add(row.name);
          saveDetached(detachedSessions);
          renderRows(lastKnownRows);
        };
        tdDetach.appendChild(detachBtn);
        tr.appendChild(tdDetach);

        const tdActions = document.createElement('td');
        const actions = document.createElement('div'); actions.className = 'row-actions';
        if (grantable(row)) {
          const permBtn = document.createElement('button'); permBtn.type = 'button'; permBtn.textContent = '🔐 Quyền';
          permBtn.onclick = () => openPermModal(row.name);
          actions.appendChild(permBtn);
        }
        const openBtn = document.createElement('a'); openBtn.textContent = '↗ Mở'; openBtn.href = `/dashboard#${encodeURIComponent(row.name)}`;
        openBtn.style.cssText = 'background:#19243b;border:1px solid var(--line);border-radius:6px;color:inherit;padding:4px 9px;text-decoration:none;font-size:12px';
        actions.appendChild(openBtn);
        tdActions.appendChild(actions);
        tr.appendChild(tdActions);

        tbodyEl.appendChild(tr);
      }
    }

    async function load() {
      const data = await fetchJSON('/dashboard/api/sessions', {cache:'no-store'});
      const rows = data.sessions || [];
      lastKnownRows = rows;
      countEl.textContent = data.error ? `(${clean(data.error)})` : `(${rows.length} session)`;
      // Bulk selection never keeps a stale/no-longer-grantable name.
      const byName = new Map(rows.map(r => [r.name, r]));
      let bulkChanged = false;
      for (const name of [...bulkSelected]) {
        const row = byName.get(name);
        if (!row || !grantable(row)) { bulkSelected.delete(name); bulkChanged = true; }
      }
      if (bulkChanged) renderBulkBar();
      renderRows(rows);
    }

    searchEl.oninput = () => renderRows(lastKnownRows);
    onlyGrantableEl.onchange = () => renderRows(lastKnownRows);

    async function refresh() {
      try {
        await load();
        liveBadgeEl.textContent = '● LIVE'; liveBadgeEl.className = 'live';
      } catch (error) {
        if (error instanceof AuthRequiredError) {
          liveBadgeEl.textContent = '● SIGN-IN REQUIRED'; liveBadgeEl.className = 'live auth-required';
        } else {
          liveBadgeEl.textContent = '● OFFLINE'; liveBadgeEl.className = 'live offline';
        }
      }
    }
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>"""


def register_dashboard(server: MCPServer, terminal: TerminalService,
                       supervisor: SupervisorService | None = None,
                       supervisor_v2: SupervisorV2Service | None = None) -> None:
    if supervisor is None:
        supervisor = SupervisorService(terminal, SupervisorStore())
    if supervisor_v2 is None:
        supervisor_v2 = build_supervisor_v2(supervisor)

    def _origin_allowed(request: Request) -> bool:
        # CSRF defense (P1 hardening item #3), always on, no config
        # required: the dashboard's own JS always sends Origin (fetch()
        # does, on every request, same-origin included) or falls back to
        # Referer, and no legitimate cross-site caller has any reason to
        # POST to a mutation route -- so a missing or cross-origin Origin/
        # Referer is refused outright, before this request touches
        # anything. The request's own Host header is always an accepted
        # origin (this is what "same-origin" means); dashboard.allowed_
        # origins lets an operator add more (e.g. behind a proxy that
        # rewrites Host).
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin:
            return False
        parsed = urlparse(origin)
        origin_value = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
        if not origin_value:
            return False
        host = request.headers.get("host", "")
        same_origin = {f"https://{host}", f"http://{host}"}
        return origin_value in same_origin or origin_value in terminal.config.dashboard.allowed_origins

    def _cloudflare_access_guard(request: Request):
        """The Cloudflare Access JWT verification step alone (P1 item #2)
        -- shared by _mutation_guard (POST routes, layered under
        mutations_enabled + CSRF/Origin below) and _read_guard (GET
        routes, which need neither of those -- see _read_guard). Returns
        (response, identity): response is non-None exactly when the
        request is blocked; identity is the verified AccessIdentity when
        configured, else always None. No-op (request always allowed,
        identity always None) unless cloudflare_access_team_domain/
        audience are both configured -- every CF Access usage in this
        project is the same opt-in, no-op-unless-configured shape."""
        team_domain = terminal.config.dashboard.cloudflare_access_team_domain
        audience = terminal.config.dashboard.cloudflare_access_audience
        if not (team_domain and audience):
            return None, None
        token = request.headers.get("cf-access-jwt-assertion") or request.cookies.get("CF_Authorization")
        identity = verify_access_assertion(token, team_domain=team_domain, audience=audience)
        if identity is None:
            return JSONResponse({"error": "CLOUDFLARE_ACCESS_VERIFICATION_FAILED"}, status_code=403,
                                headers={"Cache-Control": "no-store"}), None
        return None, identity

    def _mutation_guard(request: Request):
        """Independent boundary in front of every dashboard POST route
        (session input, supervisor ack, supervisor2 pause) -- checked
        before any of them touch terminal/supervisor/supervisor_v2 at all.
        This is *in addition to*, never instead of, the guards those calls
        already enforce themselves (terminal_input, whitelist,
        input_policy, binding input_enabled, etc.). Returns (response,
        identity): response is non-None exactly when the request is
        blocked (caller returns it immediately); identity is the verified
        Cloudflare Access AccessIdentity when cloudflare_access_team_domain/
        audience are configured (P1 item #2), else always None -- see
        cf_access.py. Order matters: cheapest/most-global checks first."""
        if not terminal.config.dashboard.mutations_enabled:
            return JSONResponse({"error": "DASHBOARD_MUTATIONS_DISABLED"}, status_code=403,
                                headers={"Cache-Control": "no-store"}), None
        if not _origin_allowed(request):
            return JSONResponse({"error": "ORIGIN_NOT_ALLOWED"}, status_code=403,
                                headers={"Cache-Control": "no-store"}), None
        return _cloudflare_access_guard(request)

    def _read_guard(request: Request):
        """P0 audit re-pass finding: GET/read routes (the dashboard page
        itself and every /dashboard/api/* GET) previously had NO app-level
        authentication at all -- only the POST/mutation routes went
        through _mutation_guard. Read access (session listings, pane
        content, supervisor status) relied entirely on network/tunnel
        topology (only reachable through a Cloudflare-Access-protected
        hostname) for protection -- exactly the gap cf_access.py's own
        module docstring warns against: edge-level Access enforcement
        'says nothing to THIS application about a request that does
        arrive here'. This is the CF Access check alone -- deliberately
        NOT _mutation_guard's other two checks: no CSRF/Origin check (a
        normal top-level GET navigation, e.g. loading the dashboard URL
        directly in a browser, does not reliably send an Origin header,
        and Referer is not a meaningful CSRF signal for a plain read
        either way), and no mutations_enabled gate (reading must stay
        available independent of whether writes are enabled -- this is
        the 'read-only dashboard' tunnel's whole purpose, per its own
        systemd unit description). Same no-op-unless-configured shape as
        every other CF Access usage here: with no team_domain/audience
        configured, GET routes are completely unaffected by this."""
        return _cloudflare_access_guard(request)

    @server.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
    async def dashboard(request: Request) -> HTMLResponse | JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        return HTMLResponse(
            DASHBOARD_HTML,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    @server.custom_route("/dashboard/sessions", methods=["GET"], include_in_schema=False)
    async def dashboard_sessions_admin(request: Request) -> HTMLResponse | JSONResponse:
        # Same read guard as /dashboard itself -- this is a second VIEW of
        # the exact same /dashboard/api/sessions data and the exact same
        # grant-read/grant-input mutation routes, not a new privilege
        # surface. See SESSIONS_ADMIN_HTML's own module-level comment.
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        return HTMLResponse(
            SESSIONS_ADMIN_HTML,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    @server.custom_route("/dashboard/api/sessions", methods=["GET"], include_in_schema=False)
    async def sessions(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        # P1 items #4/#5: dashboard_list_sessions/terminal_status are both
        # blocking (a real tmux subprocess round-trip, sometimes an sqlite
        # read) -- run off the event loop via anyio.to_thread so one slow
        # tmux call can never stall every other concurrent request this
        # single async server is handling. The per-session status calls
        # below are also each an independent tmux subprocess (the N+1 this
        # route always had) -- fired concurrently in a task group rather
        # than serially, so N sessions cost ~one round-trip's worth of
        # wall-clock time instead of N.
        #
        # dashboard_list_sessions() lists EVERY real tmux session, not just
        # whitelisted ones -- session name/attached/windows/created/
        # activity are tmux metadata, not pane CONTENT, so this is safe to
        # show for a non-whitelisted session too (a dashboard viewer could
        # already see the same by running `tmux ls`). Each row's
        # effective_read says whether content is actually reachable
        # (statically whitelisted, or explicitly granted); a row that
        # isn't gets state="RESTRICTED" -- terminal_status/_granted is
        # never even called for it, so no content-classification heuristic
        # runs against a pane this viewer has no access to.
        listed = await anyio.to_thread.run_sync(terminal.dashboard_list_sessions)
        rows = listed.get("sessions")
        if isinstance(rows, list):
            async def _fill_state(row: dict) -> None:
                if not row.get("effective_read"):
                    row["state"] = "RESTRICTED"
                    return
                # Reuses the exact same classify_status() heuristic terminal_status()/
                # terminal_status_granted() already applies to a single session — no
                # new/looser interpretation of pane content, so WAITING_INPUT here
                # means exactly what it means everywhere else in this project, and
                # UNKNOWN stays UNKNOWN when evidence is weak, same as always.
                fetch = terminal.terminal_status if row["allowed"] else terminal.terminal_status_granted
                status = await anyio.to_thread.run_sync(fetch, row["name"])
                row["state"] = status.get("state", "UNKNOWN")

            async with anyio.create_task_group() as tg:
                for row in rows:
                    tg.start_soon(_fill_state, row)
            # Stable multi-key sort applied least-significant-key first: name
            # (deterministic fallback for ties) -> activity descending (most
            # recent first) -> attention-needed first. No session is ever
            # dropped, only reordered.
            rows.sort(key=lambda r: r["name"])
            rows.sort(key=lambda r: r.get("activity") or "", reverse=True)
            rows.sort(key=lambda r: 0 if r.get("state") == "WAITING_INPUT" else 1)
        return JSONResponse(listed, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session", methods=["GET"], include_in_schema=False)
    async def session_detail(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        name = request.query_params.get("name", "")
        # A statically-whitelisted session keeps its EXACT existing path
        # (terminal_status/terminal_tail, completely untouched) -- a
        # dashboard-granted-but-not-whitelisted one uses the parallel
        # *_granted methods instead (same guard shape, checks the grant
        # instead of the static whitelist). A session that is neither
        # never reaches terminal_status/_granted at all: a clear,
        # explicit READ_RESTRICTED response, never a silent failure or a
        # generic 404 that could be mistaken for "session doesn't exist".
        if not session_allowed(name, terminal.config) and not (
            (grant := terminal.grants.get(name)) is not None and grant.read_enabled
        ):
            # Proactive, not just reactive: an operator opening a
            # never-granted session sees up front whether input would ALSO
            # be blocked by policy once read is granted, rather than only
            # discovering it after a first click.
            block_reason = await anyio.to_thread.run_sync(terminal._input_grant_block_reason, name)
            return JSONResponse(
                {"error": "READ_RESTRICTED", "session": name, "input_block_reason": block_reason},
                status_code=403, headers={"Cache-Control": "no-store"},
            )
        use_granted = not session_allowed(name, terminal.config)
        status_fn = terminal.terminal_status_granted if use_granted else terminal.terminal_status
        tail_fn = (lambda: terminal.terminal_tail_granted(name, ansi=True)) if use_granted \
            else (lambda: terminal.terminal_tail(name, ansi=True))

        # P1 item #4: both calls below are independent tmux subprocess
        # round-trips -- run concurrently, off the event loop.
        status_result: dict = {}
        tail_result: dict = {}

        async def _status() -> None:
            status_result.update(await anyio.to_thread.run_sync(status_fn, name))

        async def _tail() -> None:
            # Uses config.default_tail_lines (already the project's one source of truth
            # for "how many recent lines" — see config.yaml) rather than a hardcoded
            # count. tmux capture-pane already returns that window oldest-line-first,
            # newest-line-last, so the dashboard renders it in natural chronological
            # order with no reordering needed. ansi=True keeps colour/style escape
            # sequences for the terminal-style renderer; it goes through the exact
            # same whitelist/permission guard as every other read, and through
            # redact_ansi_safe (see terminal_mcp/redaction.py) rather than the plain
            # redactor, so a secret can never survive because it was colour-coded.
            tail_result.update(await anyio.to_thread.run_sync(tail_fn))

        # Both fire regardless of whether one turns out to be an error (the
        # original sequential code short-circuited before calling
        # terminal_tail at all on an errored status) -- a harmless, rare-
        # path tradeoff: terminal_tail/_granted re-checks its own
        # authorization itself (same defense-in-depth as every other read
        # here) and returns its own error with no side effects either way,
        # so running both concurrently costs one redundant tmux call only
        # on the already-uncommon error path, in exchange for the success
        # path (the overwhelming majority of requests) needing one round-
        # trip's worth of wall-clock time instead of two.
        async with anyio.create_task_group() as tg:
            tg.start_soon(_status)
            tg.start_soon(_tail)

        status, tail = status_result, tail_result
        if "error" in status:
            return JSONResponse(status, status_code=403 if status["error"] in
                                ("ACCESS_DENIED", "READ_RESTRICTED") else 404)
        if "error" in tail:
            return JSONResponse(tail, status_code=403 if tail["error"] == "READ_RESTRICTED" else 404)
        # effective_input mirrors dashboard_list_sessions' own definition:
        # statically allowed (unchanged meaning) OR an active input grant
        # -- the dashboard only ever reveals its input composer when this
        # is true, never merely because read succeeded.
        grant = terminal.grants.get(name)
        input_allowed = bool(
            terminal.config.permissions.terminal_input
            and (input_session_allowed(name, terminal.config) or (grant is not None and grant.input_enabled))
        )
        allowed = session_allowed(name, terminal.config)
        body = {
            "session": name, "status": status, "tail": tail, "input_allowed": input_allowed,
            "allowed": allowed,
            "grant": {"read_enabled": bool(grant and grant.read_enabled),
                     "input_enabled": bool(grant and grant.input_enabled)},
        }
        # Same UX-gap fix as dashboard_list_sessions: only relevant (and
        # only computed) when there is actually something to explain.
        if not allowed and not input_allowed:
            body["input_block_reason"] = await anyio.to_thread.run_sync(
                terminal._input_grant_block_reason, name)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/input", methods=["POST"], include_in_schema=False)
    async def session_input(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        text = body.get("text") if isinstance(body, dict) else None
        press_enter = bool(body.get("press_enter", False)) if isinstance(body, dict) else False
        idempotency_key = body.get("idempotency_key") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(text, str) or not text:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if idempotency_key is not None and not isinstance(idempotency_key, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        # Identity attribution (P1 item #1's smallest-compatible-equivalent):
        # never gates the send itself (terminal_send_text's own guards are
        # unchanged and unaffected either way) -- just makes a dashboard-
        # driven send attributable to a verified identity in the logs when
        # Cloudflare Access is configured, distinct from an anonymous one
        # when it is not.
        _log.info("dashboard session_input session=%s identity=%s", name, identity.email if identity else None)
        # A statically input-whitelisted session keeps its EXACT existing
        # send path (terminal_send_text, completely untouched) -- any
        # session with a read grant that ISN'T input-whitelisted (whether
        # or not input is currently enabled on that grant -- an input-
        # revoked grant correctly gets terminal_send_text_granted's own
        # explicit GRANT_REQUIRED from this route, rather than falling
        # back to terminal_send_text's generic ACCESS_DENIED, which would
        # be equally safe but a less informative reason for a caller who
        # just revoked their own grant) routes through the parallel
        # terminal_send_text_granted, which re-verifies the grant's
        # pinned identity against the session's current tmux identity at
        # send time.
        grant = terminal.grants.get(name)
        use_granted = grant is not None and not input_session_allowed(name, terminal.config)
        send_fn = terminal.terminal_send_text_granted if use_granted else terminal.terminal_send_text
        result = await anyio.to_thread.run_sync(
            lambda: send_fn(name, text, press_enter=press_enter, idempotency_key=idempotency_key)
        )
        status_code = 200
        if "error" in result:
            status_code = INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/grant-read", methods=["POST"], include_in_schema=False)
    async def session_grant_read(request: Request) -> JSONResponse:
        # Grants a session outside the static whitelist read access from
        # the dashboard specifically -- see core.py's grant_session_read
        # for the full guard chain (sensitive-name floor, session must
        # currently exist). Revoking read (enabled=false) also revokes
        # any input grant for the same session -- input without read makes
        # no sense and is never left dangling (SessionGrantStore.set_read).
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(enabled, bool):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        granted_by = identity.email if identity else None
        _log.info("dashboard grant_read session=%s enabled=%s identity=%s", name, enabled, granted_by)
        result = await anyio.to_thread.run_sync(
            lambda: terminal.grant_session_read(name, enabled, granted_by=granted_by)
        )
        terminal.audit.record(
            action="grant_read", session=name, result="GRANTED" if (enabled and "error" not in result)
            else ("REVOKED" if "error" not in result else "BLOCKED"),
            reason=result.get("error") or granted_by, source_transport="dashboard",
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/grant-input", methods=["POST"], include_in_schema=False)
    async def session_grant_input(request: Request) -> JSONResponse:
        # Same shape as grant-read above, for input specifically -- see
        # core.py's grant_session_input: requires read already granted,
        # still respects the global terminal_input gate, input_policy
        # deny patterns, the sensitive-current-command guard, and pins
        # the session's identity at the moment of grant (re-verified at
        # every send by terminal_send_text_granted).
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(enabled, bool):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        granted_by = identity.email if identity else None
        _log.info("dashboard grant_input session=%s enabled=%s identity=%s", name, enabled, granted_by)
        result = await anyio.to_thread.run_sync(
            lambda: terminal.grant_session_input(name, enabled, granted_by=granted_by)
        )
        terminal.audit.record(
            action="grant_input", session=name, result="GRANTED" if (enabled and "error" not in result)
            else ("REVOKED" if "error" not in result else "BLOCKED"),
            reason=result.get("error") or granted_by, source_transport="dashboard",
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/supervisor", methods=["GET"], include_in_schema=False)
    async def supervisor_summary(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        # Read-only: reuses the same SupervisorService the MCP tools use, so
        # the dashboard can never see anything a supervisor_* tool call
        # couldn't already show (same whitelist-guarded watch data). Both
        # calls are sqlite reads -- cheap individually, but still blocking,
        # so one thread-hop for the pair (P1 item #4) rather than two.
        def _compute() -> dict:
            status = supervisor.status()
            events = supervisor.list_events(unacknowledged_only=True, limit=20)["events"]
            return {"status": status, "events": events}

        result = await anyio.to_thread.run_sync(_compute)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/supervisor/ack", methods=["POST"], include_in_schema=False)
    async def supervisor_ack(request: Request) -> JSONResponse:
        # Simple safe local metadata action only: this only ever calls
        # SupervisorStore.ack_event, which just stamps acknowledged_at in
        # SQLite. No terminal_send/tmux call is reachable from this route.
        blocked, _identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        event_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(event_id, int):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        result = await anyio.to_thread.run_sync(supervisor.ack_event, event_id)
        status_code = 404 if "error" in result else 200
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/supervisor2", methods=["GET"], include_in_schema=False)
    async def supervisor2_summary(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        # Read-only: for every watch with a non-observe_only v2 policy, its
        # policy/counters plus its most recent action (if any) — the same
        # data the supervisor2_* MCP tools expose, nothing extra computed
        # for this view. All sqlite (P1 item #4: one thread-hop for the
        # whole loop, not per-call) -- the N+1 shape here is far cheaper
        # per-iteration than the tmux-backed /sessions route (item #5), so
        # unlike that route this is left as a single-threaded loop rather
        # than parallelized: N tiny sqlite reads gain little from fan-out
        # and a task group's own overhead would likely cost more than it
        # saves at the sizes this ever runs at (per-watch v2 policies).
        def _compute() -> list[dict]:
            rows = []
            for watch in supervisor.list_watches()["watches"]:
                policy = supervisor_v2.store.get_policy(watch["watch_key"])
                if policy["policy_mode"] == "observe_only" and policy["created_at"] is None:
                    continue  # never configured for v2 at all — nothing to show
                actions = supervisor_v2.store.list_actions(watch_key=watch["watch_key"], limit=1)
                rows.append({
                    "watch_key": watch["watch_key"], "target": watch["target"], "kind": watch["kind"],
                    "watch_state": watch["state"], "policy": policy,
                    "latest_action": actions[0] if actions else None,
                })
            return rows

        rows = await anyio.to_thread.run_sync(_compute)
        return JSONResponse({"watches": rows}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/supervisor2/pause", methods=["POST"], include_in_schema=False)
    async def supervisor2_pause(request: Request) -> JSONResponse:
        # STOP/PAUSE only: sets policy_mode back to observe_only. Local
        # metadata only, same as the v1 ack route — no terminal_send/tmux
        # call is reachable from this route either.
        blocked, _identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        target = body.get("target") if isinstance(body, dict) else None
        kind = body.get("kind") if isinstance(body, dict) else None
        if not isinstance(target, str) or kind not in ("session", "binding"):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        kwargs = {"session": target} if kind == "session" else {"binding": target}
        result = await anyio.to_thread.run_sync(lambda: supervisor_v2.set_policy(policy_mode="observe_only", **kwargs))
        status_code = 404 if "error" in result else 200
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})
