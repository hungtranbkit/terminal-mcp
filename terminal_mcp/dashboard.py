from __future__ import annotations

import contextlib
import hmac
import logging
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlparse

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from . import lan_discovery, network_bind, remote_connect, tunnel_diagnostics
from .cf_access import verify_access_assertion
from .agent_availability import available_agent_types
from .connection_store import ConnectionStore, generate_node_token
from .controller import ControllerService, build_default_controller
from .node_client import RemoteNodeClient
from .core import TerminalService
from .node_models import NODE_ONLINE, node_to_dict
from .permissions import input_session_allowed, session_allowed, valid_session_name
from .supervisor import SupervisorService, SupervisorStore
from .supervisor2 import SupervisorV2Service, build_supervisor_v2
from .webterm import WebTerminalProcess, pump_websocket
from .webterm_assets import ASSETS

_log = logging.getLogger(__name__)


def node_token_env_var(node_id: str) -> str:
    """The ONE naming convention for a remote node's heartbeat-auth env
    var -- shared by node_generate_onboarding (what it tells an operator
    to export), node_heartbeat (what it reads to verify an inbound
    push), and the LAN-discovery/remote-connect routes below (which set
    it themselves at runtime, in-process, instead of asking an operator
    to export it by hand). Real bug fixed while adding those routes:
    node_generate_onboarding already replaced '-' with '_' (env var names
    don't take hyphens); node_heartbeat's own lookup did not, so a
    hyphenated node_id (e.g. "m-910") would generate one env var name at
    onboarding time and look up a DIFFERENT (invalid) one at heartbeat
    time -- silently rejecting every heartbeat from that node forever.
    Centralized here so the two can never drift apart again."""
    return f"TERMINAL_MCP_NODE_TOKEN_{node_id.upper().replace('-', '_')}"


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
    # Session lifecycle (create/detach/delete) error -> status mapping --
    # same table, same convention as every code above it.
    "SESSION_LIFECYCLE_DISABLED": 403,
    "INVALID_SESSION_NAME": 400,
    "SENSITIVE_SESSION_NOT_CREATABLE": 403,
    "INVALID_AGENT_TYPE": 400,
    "INVALID_GRANT_MODE": 400,
    "SESSION_ALREADY_EXISTS": 409,
    "INVALID_CWD": 400,
    "CWD_NOT_FOUND": 400,
    "CWD_NOT_ALLOWED": 403,
    "NO_ALLOWED_CWD_ROOTS": 500,
    "LAUNCHER_NOT_CONFIGURED": 500,
    "LAUNCH_FAILED": 502,
    "SESSION_PROTECTED": 403,
    "TMUX_ERROR": 502,
    # Web terminal (webterm.py) error -> status mapping, same table/convention.
    "WEB_TERMINAL_DISABLED": 403,
    # Kill/Reopen (core.py's terminal_kill_session/terminal_reopen_session).
    "CONFIRMATION_MISMATCH": 400,
    "REOPEN_METADATA_INCOMPLETE": 422,
    # Nodes (multi-node session management, controller.py/node_registry.py).
    "NODE_NOT_FOUND": 404,
    "NODE_UNREACHABLE": 502,
    "AMBIGUOUS_SESSION": 409,
    "NO_ELIGIBLE_NODE": 503,
    "PLATFORM_MISMATCH": 400,
    "AGENT_TYPE_NOT_AVAILABLE_ON_TARGET": 409,
    "NODE_DRAINING": 409,
}


_node_to_dict = node_to_dict  # local alias -- every route below predates the move to node_models.py


DASHBOARD_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,interactive-widget=resizes-content">
  <title>Terminal MCP Sessions</title>
  <style>
    :root {
      color-scheme: dark;
      /* App chrome (header, tab bar, menus, modals) -- deliberately a
         touch darker/flatter than before so the terminal surface below
         reads as the visual focus, not just another panel among many
         (task item 1: "terminal area là trọng tâm"). */
      --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd;
      --green:#43d17c; --amber:#ffc857; --red:#ff6b6b; --accent:#3b78ff;
      --mono: ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace;
      /* Terminal surface tokens (task: "tạo theme token rõ ràng cho
         terminal surface, không hardcode rải rác") -- Windows Terminal's
         own default ("Campbell") dark colour scheme, applied identically
         to BOTH this page's poll-based output pane (ansiRuns/renderAnsi
         below read --ansi-0..15 by SGR code) AND the real xterm.js
         terminal (webterm.py's own Terminal({theme:...}) reads these
         exact same values) -- one palette, never hardcoded a second time,
         so a colour means the same thing on every terminal surface in
         this app. */
      --term-bg:#0c0c0c; --term-fg:#cccccc; --term-cursor:#ffffff; --term-selection:rgba(255,255,255,.28);
      --ansi-0:#0c0c0c; --ansi-1:#c50f1f; --ansi-2:#13a10e; --ansi-3:#c19c00;
      --ansi-4:#0037da; --ansi-5:#881798; --ansi-6:#3a96dd; --ansi-7:#cccccc;
      --ansi-8:#767676; --ansi-9:#e74856; --ansi-10:#16c60c; --ansi-11:#f9f1a5;
      --ansi-12:#3b78ff; --ansi-13:#b4009e; --ansi-14:#61d6d6; --ansi-15:#f2f2f2;
    }
    /* Thin, dark scrollbars everywhere a pane scrolls internally (task
       item 1: "Scrollbar gọn, giống desktop terminal") -- Firefox via the
       standard property, WebKit/Blink (Chrome/Edge/Safari) via the
       vendor-prefixed pseudo-elements; both degrade harmlessly to the
       platform default on any engine that supports neither. */
    * { scrollbar-width:thin; scrollbar-color:#3a4560 transparent; }
    ::-webkit-scrollbar { width:10px; height:10px; }
    ::-webkit-scrollbar-track { background:transparent; }
    ::-webkit-scrollbar-thumb { background:#3a4560; border-radius:6px; border:2px solid transparent; background-clip:padding-box; }
    ::-webkit-scrollbar-thumb:hover { background:#4a5878; background-clip:padding-box; }
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
    /* Connection health banner (task: "self-healing, có chẩn đoán rõ") --
       one quiet, always-present label (never a popup/toast), colored only
       when something isn't simply "Connected". #connHealthBadge[hidden]
       is used while the very first poll hasn't resolved yet, never after. */
    #connHealthBadge { cursor:default }
    #connHealthBadge[hidden] { display:none }
    #connHealthBadge.conn-recovering { border-color:var(--amber); color:var(--amber) }
    #connHealthBadge.conn-tunnel-stale, #connHealthBadge.conn-mcp-down { border-color:#ff6b6b; color:#ff6b6b }
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
       chain (main -> .panel.detail -> .term -> #output). Single column,
       two rows now (tab bar above, the session panel below) -- task items
       2/3: the tab bar is the ONE session-navigation surface on this page,
       replacing the old left sidebar (see .tabbar below) rather than
       sitting alongside it, so there is still only ever one list. */
    main { display:grid; grid-template-columns:1fr; grid-template-rows:auto minmax(0,1fr); gap:0; padding:0; flex:1; min-height:0 }
    .panel { background:var(--panel); border:1px solid var(--line); overflow:hidden; min-height:0 }
    .panel.detail { border-left:none; border-right:none; border-bottom:none }

    /* ---- Tab bar: Windows-Terminal-style horizontal session switcher ----
       (task item 2) -- the ONE navigation list on this page. A tab is a
       compact chip (status dot + name only -- task item 3: no node/host/
       status label here, that lives in the session detail header once
       opened, see loadDetail's summary render); the destructive "kill"
       affordance only appears on hover/focus (task: "Nút close/kill chỉ
       hiện khi hover") and reuses the EXISTING typed-confirmation
       #killModal below -- there is no bare "close tab" action, because
       closing a tab has no meaning independent of the underlying tmux
       session: the server-driven list would simply show it again on the
       very next poll if it still exists. Horizontal overflow scrolls with
       a thin scrollbar (see the global ::-webkit-scrollbar rules above);
       touch-action/-webkit-overflow-scrolling/overscroll-behavior-x below
       are the task's own explicit "kéo ngang được trên iPhone/Android,
       touch-friendly momentum scrolling, không để page body ăn gesture
       ngang" -- html/body above never scroll at all (100dvh + overflow:
       hidden), so overscroll-behavior-x:contain here is what stops an
       iOS rubber-band swipe on this strip from propagating anywhere else,
       not a page-scroll conflict (there isn't one). */
    .tabbar {
      display:flex; align-items:stretch; overflow-x:auto; overflow-y:hidden; background:var(--panel);
      border-bottom:1px solid var(--line); scrollbar-width:thin;
      -webkit-overflow-scrolling:touch; touch-action:pan-x; overscroll-behavior-x:contain;
    }
    .tabbar-empty { padding:12px 16px; color:var(--muted); font-size:13px }
    .tab {
      position:relative; display:flex; align-items:center; gap:7px; flex:0 0 auto; max-width:220px; min-width:0;
      padding:9px 10px 9px 12px; cursor:pointer; color:var(--muted); border-right:1px solid var(--line);
      border-bottom:2px solid transparent; white-space:nowrap;
    }
    .tab:hover { background:#171f33; color:var(--text) }
    .tab.active { background:var(--bg); color:var(--text); border-bottom-color:var(--accent) }
    .tab.needs-attention:not(.active) { border-bottom-color:var(--amber) }
    .tab-dot { flex:0 0 auto; display:inline-block; width:7px; height:7px; border-radius:50%; background:var(--line) }
    .tab-dot.on { background:var(--green) }
    .tab-dot.err { background:var(--red) } /* real supervisor state (FAILED/ERROR/BLOCKED) -- never a fake/invented one */
    .tab-dot.idle { background:var(--muted) }
    .tab-name { overflow:hidden; text-overflow:ellipsis; font-size:13px; font-weight:600 }
    /* Compact attention badge -- reused identically in the tab bar and the
       viewer header (#summary) so a WAITING_INPUT session is obvious in
       both places, driven entirely by classify_status()'s existing state
       string, nothing new inferred from pane content here. */
    .attn-badge { display:inline-block; background:var(--amber); color:#231a00; font-size:11px; font-weight:700; padding:1px 6px; border-radius:4px; vertical-align:middle }
    .tab .attn-badge { flex:0 0 auto; margin-left:2px }
    /* Hover/focus-only close (kill) button -- kept a fixed 18px hit target
       even hidden (visibility, not display:none) so the tab's own layout
       never shifts width when the button appears/disappears. */
    .tab-close {
      flex:0 0 auto; visibility:hidden; width:18px; height:18px; display:flex; align-items:center; justify-content:center;
      border-radius:4px; background:transparent; border:none; color:var(--muted); cursor:pointer; font:12px var(--mono); padding:0;
    }
    .tab:hover .tab-close, .tab:focus-within .tab-close { visibility:visible }
    .tab-close:hover { background:#3a2430; color:#ff9f9f }
    .tab-close:disabled { visibility:hidden !important; cursor:not-allowed }
    /* Layout bugfix (real-device report), still applicable with the
       top session-tabs bar removed: .detail's 5 direct children in DOM
       order are #summary, #grantBar, .term, #inputNote, #inputBar --
       grid-template-rows must list exactly 5 tracks, in that order, with
       .term (the actual output viewport) as the one flexible track, or
       its intended growing row silently goes to #summary instead and lets
       its content overflow into the rows below. */
    .detail { display:grid; grid-template-rows:auto auto minmax(0,1fr) auto auto; min-width:0; min-height:0 }
    #grantBar[hidden] { display:none } /* the plain #grantBar{display:flex} rule below would otherwise outrank the UA's own [hidden] default */
    #summary { grid-row:1; padding:14px 16px; border-bottom:1px solid var(--line) }
    .state-WAITING_INPUT { color:var(--amber) } .state-RUNNING { color:var(--green) }
    /* P0 Part C states: VERIFYING (independent verification in progress --
       amber, same "needs a look" weight as WAITING_INPUT); FAILED/BLOCKED
       (an autonomous watch's verifier rejected promotion, or none was
       configured -- red, distinct from ERROR's transient-pane-pattern
       meaning: these mean automation stopped and needs an operator). */
    .state-VERIFYING { color:var(--amber) } .state-FAILED,.state-BLOCKED { color:#ff6b6b }
    /* Terminal-style pane: a slim chrome bar (title + a handful of core
       controls) above a dark, monospace, ANSI-rendering scrollback view.
       No macOS-style traffic-light dots (task item 1/3: "tránh chrome
       thừa" -- Windows Terminal itself has none either); a session's
       state already shows via the tab bar's own dot + this bar's title,
       so a second, purely decorative status indicator here would be
       redundant chrome, not information. */
    .term { grid-row:3; display:flex; flex-direction:column; min-height:0 }
    .term-bar { display:flex; flex-wrap:wrap; align-items:center; gap:8px 10px; padding:7px 12px; background:#0e1526; border-bottom:1px solid var(--line) }
    .term-title { color:var(--muted); font-size:12px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    /* flex:1 1 auto + min-width:0 (not flex:0 0 auto): a flex item's
       preferred size is its max-content (unwrapped) width by default, and
       flex-shrink:0 refuses to go below that — so .term-controls never
       actually shrank enough to let its own flex-wrap kick in, and its
       buttons silently overflowed a narrow shell otherwise. Only the
       core, frequently-used controls live directly in this row now
       (task item 3: "action ít dùng đưa vào menu ...") -- Font size/
       Search/Copy/Access/Kill move into the "⋯" menu (see .menu below). */
    .term-controls { display:flex; flex-wrap:wrap; align-items:center; gap:8px; flex:1 1 auto; min-width:0; justify-content:flex-end }
    .term-btn { background:#19243b; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:5px 10px; font:12px var(--mono); cursor:pointer; white-space:nowrap }
    .term-btn:hover:not(:disabled) { background:#233252 }
    .term-btn:disabled { opacity:.5; cursor:not-allowed }
    .term-btn.paused { border-color:var(--amber); color:var(--amber) }
    /* ---- Reusable "⋯" overflow menu (task item 3) -- one small component,
       used for the header's own menu and each term-bar's menu alike, never
       a bespoke dropdown per screen. Click-to-open (not hover, so it works
       identically on touch); closes on an outside click or Escape (wired
       once, generically, in JS below via .menu-open/data-menu). */
    .menu { position:relative; flex:0 0 auto }
    .menu-panel {
      display:none; position:absolute; right:0; top:calc(100% + 6px); z-index:26; min-width:190px;
      background:var(--panel); border:1px solid var(--line); border-radius:10px; box-shadow:0 16px 40px rgba(0,0,0,.55);
      padding:6px; flex-direction:column; gap:2px;
    }
    .menu.open .menu-panel { display:flex }
    .menu-panel button, .menu-panel a {
      display:flex; align-items:center; gap:8px; width:100%; text-align:left; background:transparent; border:none;
      color:var(--text); border-radius:6px; padding:8px 10px; font:13px var(--mono); cursor:pointer; text-decoration:none; white-space:nowrap;
    }
    .menu-panel button:hover, .menu-panel a:hover { background:#19243b }
    .menu-panel button:disabled { opacity:.4; cursor:not-allowed }
    .menu-panel a.disabled { opacity:.4; cursor:not-allowed; pointer-events:none } /* an <a> has no disabled attribute -- termOpenRealBtn's own gating */
    .menu-panel button.danger { color:#ff9f9f }
    .menu-panel button.danger:hover { background:#3a2430 }
    .menu-panel hr { border:none; border-top:1px solid var(--line); margin:4px 2px }
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
    #output {
      flex:1; min-height:0; margin:0; padding:10px 18px; overflow:auto; overflow-anchor:none; cursor:text;
      white-space:pre-wrap; word-break:break-word; line-height:1.45; font-family:var(--mono);
      background:var(--term-bg); color:var(--term-fg);
    }
    #output::selection, #output ::selection { background:var(--term-selection); color:inherit }
    /* A real terminal's own blinking block cursor (task item 1: "Cursor
       rõ ràng") -- appended as the LAST child of #output only while a
       readable session is selected (see renderAnsi's own caller), never
       during the loading/locked/error placeholder states. Pure CSS
       blink, no JS timer; respects the OS "reduce motion" preference the
       same as the rest of this page (no other animation exists here to
       gate, so this is the one rule that needs it). */
    .term-cursor { display:inline-block; width:0.6em; height:1.1em; background:var(--term-cursor); vertical-align:text-bottom; animation:term-cursor-blink 1.05s steps(1) infinite }
    @media (prefers-reduced-motion:reduce) { .term-cursor { animation:none } }
    @keyframes term-cursor-blink { 0%,49% { opacity:1 } 50%,100% { opacity:0 } }
    /* ---- Input row: an inline terminal prompt, not a separate boxed web
       form (task item 1/4) -- blends into the same dark terminal surface
       (--term-bg) right below the output, with a prompt glyph standing in
       for a real shell's own "PS>"/"$" prompt. Functionally UNCHANGED:
       still a single-line composer + explicit "press Enter" checkbox +
       Send button (core.py's own terminal_send_text press_enter
       semantics, and the idempotency-key/delivery-state handling in the
       JS below, are untouched -- only the visual chrome around them). */
    #inputBar { grid-row:5; display:flex; align-items:center; gap:8px; padding:10px 16px; background:var(--term-bg); border-top:1px solid var(--line) }
    #inputPrompt { flex:0 0 auto; color:var(--ansi-10); font-weight:700; user-select:none }
    #inputBar input[type=text] { flex:1; min-width:0; background:transparent; border:none; color:var(--term-fg); padding:8px 2px; font:inherit }
    #inputBar input[type=text]:focus { outline:none }
    #inputBar input[type=text]:disabled { opacity:.5 }
    #inputBar button { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:7px 13px; cursor:pointer; font:inherit }
    #inputBar button:hover:not(:disabled) { background:#233252 }
    #inputBar button:disabled { opacity:.5; cursor:not-allowed }
    #inputBar label { display:flex; align-items:center; gap:4px; color:var(--muted); font-size:12px; white-space:nowrap }
    #inputNote { grid-row:4; padding:6px 16px 0; font-size:12px; color:var(--muted) }
    #inputNote.error { color:#ff6b6b }
    /* Compact single-line entry point only now -- "Quyền: <label>" plus one
       "🔐 Quyền truy cập" button that opens #permModal, which does all the
       actual granting/revoking. Kept deliberately this thin (not the old
       multi-button inline bar) so re-enabling it can never reintroduce the
       real mobile overlap bug that #permModal's own separate, off-grid
       overlay design structurally avoids. */
    #grantBar { grid-row:2; display:flex; align-items:center; gap:8px; padding:8px 16px; border-bottom:1px solid var(--line); font-size:12px; color:var(--muted); flex-wrap:wrap }
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
    /* ---- Kill confirmation modal ------------------------------------
       Same fixed-overlay + backdrop + body-class pattern as #permModal
       (living OUTSIDE .detail's own grid, so it can never disturb that
       grid's fragile track count) -- deliberately a SEPARATE modal, not
       a repurposed #permModal, so a destructive action can never be
       mistaken for the harmless grant/revoke one. The Kill button stays
       disabled until the typed confirmation exactly matches the session
       name -- the second, server-enforced check (core.py's confirm_name)
       is the real floor; this is the human-facing half of it. */
    #killBackdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:30 }
    #killModal {
      display:none; position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); z-index:31;
      width:min(380px, calc(100vw - 32px)); max-height:80vh; overflow:auto;
      background:var(--panel); border:1px solid #5a2f38; border-radius:12px; box-shadow:0 20px 50px rgba(0,0,0,.6);
    }
    body.kill-modal-visible #killBackdrop, body.kill-modal-visible #killModal { display:block }
    #killModal .km-head { padding:14px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center; gap:10px }
    #killModal .km-head strong { color:#ff9f9f; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    #killModal .km-body { padding:14px 16px; display:flex; flex-direction:column; gap:10px; font-size:13px }
    #killModal .km-warn { color:var(--amber); font-size:12px; background:rgba(255,200,87,.1); border:1px solid var(--amber); border-radius:8px; padding:8px 10px }
    #killModal input[type=text] { background:#0e1526; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:9px 11px; font:inherit }
    #killModal .km-actions { display:flex; justify-content:flex-end; gap:8px }
    #killModal .km-actions button { border-radius:8px; padding:9px 14px; cursor:pointer; font:inherit; font-size:13px; border:1px solid var(--line); background:#19243b; color:var(--text) }
    #killModal .km-actions button.danger { background:#3a2430; border-color:#ff9f9f; color:#ff9f9f }
    #killModal .km-actions button.danger:disabled { opacity:.4; cursor:not-allowed }
    #killModal .km-error { color:#ff6b6b; font-size:12px }
    /* ---- Tab bar row + killed-sessions reopen menu --------------------
       .tabbar itself scrolls horizontally (see its own rule above);
       .tabbar-side-btn is a FIXED trailing sibling (never scrolls away)
       reusing the same .menu/.menu-panel dropdown component as the
       header/term-bar menus -- compact, zero footprint until there's
       actually a killed session to reopen (task's own original design
       intent, unchanged), never a second navigation surface for LIVE
       sessions (only ever lists sessions that no longer exist). */
    /* min-width:0 is load-bearing, not decorative (task item 2's real
       root cause, confirmed live): .tabbar-row is a grid item of `main`
       (grid-template-rows:auto minmax(0,1fr)) -- a grid/flex item's
       default min-width is `auto`, i.e. "never shrink below your own
       content's natural width", not 0. Without this override,
       .tabbar-row grew to fit ALL of #tabbar + the side buttons
       unwrapped (measured 1599px on a 390px-wide phone viewport) instead
       of being clamped to main's actual column width -- so #tabbar's own
       `flex:1; min-width:0` (already correct) never had anything to
       shrink AGAINST, and overflow-x:auto on it never actually engaged;
       the tab strip just silently ran off the right edge of the screen
       with no way to reach it, on every viewport narrower than the full
       tab strip's content width (mobile, not just very narrow desktop). */
    .tabbar-row { display:flex; align-items:stretch; background:var(--panel); border-bottom:1px solid var(--line); min-width:0 }
    .tabbar-row .tabbar { flex:1; min-width:0; border-bottom:none }
    .tabbar-side-btn {
      flex:0 0 auto; background:transparent; border:none; border-left:1px solid var(--line); color:var(--muted); cursor:pointer;
      padding:0 14px; font:12px var(--mono); white-space:nowrap;
      display:flex; align-items:center; text-decoration:none; /* also used on the "+ New session" <a> */
    }
    .tabbar-side-btn:hover { color:var(--text); background:#171f33 }
    .killed-panel { min-width:260px; max-width:min(360px, calc(100vw - 24px)); max-height:60vh; overflow:auto }
    .killed-row { padding:8px 8px; border-radius:8px; font-size:12px }
    .killed-row + .killed-row { border-top:1px solid var(--line) }
    .killed-row .kr-name { font-weight:700 }
    .killed-row .kr-meta { color:var(--muted); margin-top:2px }
    .killed-row .kr-incomplete { color:var(--amber) }
    .killed-row button { margin-top:6px; background:#2b3f66; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:4px 10px; cursor:pointer; font:inherit; font-size:11px }
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
      header .muted { font-size:10px; line-height:1.25 }
      header .live { font-size:11px }
      .header-right { gap:6px }
      .supervisor-badge { padding:3px 8px; font-size:10px }
      #supervisorPanel { top:52px; right:8px; width:min(320px, calc(100vw - 16px)) }
      #summary {
        padding:8px 12px; font-size:12px; line-height:1.3;
        display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden;
      }
      /* main is already a single column (tab bar row + detail row) on
         desktop too now (task item 2/5 -- see the base `main` rule above),
         so nothing to override here: the tab bar itself already IS the
         compact, always-visible mobile navigation (task item 5: "tab bar
         compact, terminal full-screen, không sidebar chiếm chỗ") with no
         separate drawer/backdrop mechanism needed at all. */
      .tab { max-width:140px; padding:8px 8px 8px 10px }
      .tab-name { font-size:12.5px }
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
      /* No mobile drawer needed any more (task item 5): the tab bar is
         already the full mobile-appropriate session navigation, always
         visible, taking one slim row -- there is no tall sidebar left to
         hide/reopen as an overlay. Respect notch/home-indicator safe
         areas for the shell, which now spans essentially the full
         viewport either way. */
      main { padding-left:max(18px, env(safe-area-inset-left)); padding-right:max(18px, env(safe-area-inset-right)); padding-bottom:max(18px, env(safe-area-inset-bottom)) }
      header { padding-top:max(8px, env(safe-area-inset-top)) }
      /* Once a session is open, the terminal panel IS effectively the
         screen -- the generous desktop-style 18px outer gutter around it
         is wasted width on a phone. Shrink it to a thin edge (still
         safe-area aware, never under it) without touching #output's own
         padding/font-size, so the actual output area is unchanged — only
         the empty margin around the panel shrinks. Deliberately not
         applied before a session is selected, so the tab bar/empty state
         keeps its normal comfortable padding. */
      body.has-selection main { gap:0; padding:max(0px, env(safe-area-inset-top)) max(0px, env(safe-area-inset-right)) max(6px, env(safe-area-inset-bottom)) max(0px, env(safe-area-inset-left)) }
      body.has-selection .panel.detail { border-radius:0; border-left:none; border-right:none }

      /* ---- fullscreen terminal mode ---------------------------------- */
      /* Hides every non-terminal chrome element (header, status card, tab
         bar, input composer) so essentially only the terminal pane
         remains, its own small term-bar (title + follow/jump/exit-
         fullscreen controls) acting as the "small floating control" this
         needs. #output still scrolls internally the same way; the
         config.default_tail_lines bound, ANSI rendering, and auto-follow/
         pause/jump are all untouched by anything in this block — it is
         pure presentation. */
      body.fullscreen-terminal header,
      body.fullscreen-terminal .tabbar,
      body.fullscreen-terminal #summary,
      body.fullscreen-terminal #grantBar,
      body.fullscreen-terminal #inputNote,
      body.fullscreen-terminal #inputBar { display:none }
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
      <button id="supervisorBadge" class="supervisor-badge" type="button" hidden></button>
      <span class="supervisor-badge" id="connHealthBadge" title="Kết nối OpenAI Secure MCP Tunnel" hidden></span>
      <span class="live" id="liveBadge">● LIVE</span>
      <!-- Task item 3: rarely-used navigation (admin screens) lives in one
           "⋯" menu instead of a row of separate buttons -- LIVE/conn-health/
           Supervisor stay directly visible above since they're STATUS, not
           navigation. -->
      <div class="menu" id="headerMenu">
        <button class="term-btn" id="headerMenuBtn" type="button" aria-haspopup="true" aria-expanded="false" title="Menu">⋯</button>
        <div class="menu-panel" id="headerMenuPanel" role="menu">
          <a href="/dashboard/sessions" id="sessionsAdminLink" role="menuitem">⚙ Quản lý session</a>
          <a href="/dashboard/nodes" id="nodesAdminLink" role="menuitem">🖥 Nodes</a>
        </div>
      </div>
    </div>
  </header>
  <main>
    <!-- Task item 2/3: the ONE session-navigation surface on this page --
         Windows-Terminal-style horizontal tabs, replacing the old left
         sidebar entirely (never both at once, see the .tabbar CSS comment
         above for why that specific duplication was removed once already
         in this project's own history). One click switches; a hover-only
         "✕" opens the SAME typed-confirmation kill flow as before (see
         makeTabCloseButton) -- there is no separate, weaker "close tab". -->
    <div class="tabbar-row">
      <nav class="tabbar" id="tabbar" role="tablist" aria-label="Sessions"></nav>
      <!-- Task item 1: the tab bar itself had no session-creation entry
           point at all -- the Create Session form (with its full node
           selector, agent-type filter, submit-time revalidation) already
           exists and works on /dashboard/sessions (verified live, real
           browser, real production node data), it just wasn't reachable
           from here without already knowing about that separate admin
           page. Rather than duplicate that whole form's logic into a
           second copy here (new feature surface, and the task's own
           "không redesign lớn"), this is a direct, one-click Windows-
           Terminal-style "+" straight to it -- same admin page the "⚙
           Quản lý session" menu item already links to. -->
      <a class="tabbar-side-btn" id="newSessionLinkBtn" href="/dashboard/sessions" title="Tạo session mới (node/host, agent type, ...)">+ New session</a>
      <div class="menu" id="killedMenu">
        <button class="tabbar-side-btn" id="killedToggle" type="button" hidden aria-haspopup="true" aria-expanded="false"><span id="killedToggleLabel"></span></button>
        <div class="menu-panel killed-panel" id="killedList" role="menu"></div>
      </div>
    </div>
    <section class="panel detail">
      <div id="summary" class="muted">Chọn một session để xem output.</div>
      <div id="grantBar" hidden></div>
      <div class="term">
        <div class="term-bar">
          <span class="term-title" id="termTitle"></span>
          <span class="term-controls">
            <button id="followToggle" class="term-btn" type="button" disabled>Auto-follow: ON</button>
            <button id="fullscreenBtn" class="term-btn" type="button" disabled title="Fullscreen (Esc để thoát)">⛶</button>
            <div class="menu" id="termMenu">
              <button class="term-btn" id="termMenuBtn" type="button" disabled aria-haspopup="true" aria-expanded="false" title="Thêm tuỳ chọn">⋯</button>
              <div class="menu-panel" id="termMenuPanel" role="menu">
                <button id="jumpBtn" type="button" disabled>↓ Jump to latest</button>
                <button id="searchToggleBtn" type="button" disabled>🔍 Tìm trong output</button>
                <button id="copyBtn" type="button" disabled>⧉ Copy output</button>
                <button id="fontDecBtn" type="button" disabled>A− Chữ nhỏ hơn</button>
                <button id="fontIncBtn" type="button" disabled>A+ Chữ lớn hơn</button>
                <hr>
                <a id="termOpenRealBtn" href="#" role="menuitem">🖥 Mở terminal thật (xterm.js)</a>
                <button id="termAccessBtn" type="button" disabled>🔐 Quyền truy cập</button>
                <button id="termKillBtn" type="button" class="danger" disabled>🗑 Kill session</button>
              </div>
            </div>
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
        <span id="inputPrompt">❯</span>
        <input type="text" id="inputText" placeholder="Nhập text để gửi vào session..." disabled>
        <label><input type="checkbox" id="inputEnter" checked> Enter</label>
        <button id="inputSend" disabled>Gửi</button>
      </div>
    </section>
  </main>
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
  <div id="killBackdrop"></div>
  <div id="killModal" role="dialog" aria-modal="true" aria-labelledby="killModalTitle">
    <div class="km-head">
      <strong id="killModalTitle">Kill session</strong>
      <button id="killModalCloseBtn" class="term-btn" type="button">✕</button>
    </div>
    <div class="km-body">
      <div id="killModalWarn" class="km-warn" hidden></div>
      <div>Gõ chính xác tên session để xác nhận:</div>
      <input type="text" id="killConfirmInput" autocomplete="off" spellcheck="false">
      <div class="km-error" id="killModalError"></div>
      <div class="km-actions">
        <button id="killCancelBtn" type="button">Huỷ</button>
        <button id="killConfirmBtn" class="danger" type="button" disabled>🗑 Kill session</button>
      </div>
    </div>
  </div>
  <script>
    let selected = null;
    let inputAllowed = false;
    let autoFollow = true;
    let lastRenderedSession = null;
    let fullscreenTerminal = false;
    let lastKnownRows = []; // the most recent /dashboard/api/sessions rows, reused by openPermModal/openKillModal without a re-fetch
    let loadDetailSequence = 0; // generation counter -- see loadDetail's own guard for why a session-name check alone isn't enough
    // session name -> its persistent tab <div>, reused across every
    // renderRows() call instead of rebuilt (task item 4's own root cause:
    // the 5s poll used to call tabbarEl.replaceChildren() + recreate every
    // tab from scratch on EVERY refresh, whether or not anything actually
    // changed -- a tap/click that landed between "browser dispatches the
    // event" and "renderRows tears the target node out of the DOM" was
    // silently lost, needing a second click to land on the now-stable
    // replacement node. Touch is far more exposed than a mouse click
    // (longer touchstart-to-click latency), but a plain desktop click can
    // race it too. Reusing the same node forever (only its classes/text/
    // handlers-closure-over-row get updated in place) means a click can
    // never be "stolen" by a rebuild again.
    const tabEls = new Map();
    const tabbarEl = document.querySelector('#tabbar');
    const outputEl = document.querySelector('#output');
    const summaryEl = document.querySelector('#summary');
    const grantBarEl = document.querySelector('#grantBar');
    const killedToggleEl = document.querySelector('#killedToggle');
    const killedToggleLabelEl = document.querySelector('#killedToggleLabel');
    const killedListEl = document.querySelector('#killedList');
    const killBackdropEl = document.querySelector('#killBackdrop');
    const killModalEl = document.querySelector('#killModal');
    const killModalTitleEl = document.querySelector('#killModalTitle');
    const killModalWarnEl = document.querySelector('#killModalWarn');
    const killConfirmInputEl = document.querySelector('#killConfirmInput');
    const killModalErrorEl = document.querySelector('#killModalError');
    const killModalCloseBtnEl = document.querySelector('#killModalCloseBtn');
    const killCancelBtnEl = document.querySelector('#killCancelBtn');
    const killConfirmBtnEl = document.querySelector('#killConfirmBtn');
    const permBackdropEl = document.querySelector('#permBackdrop');
    const permModalEl = document.querySelector('#permModal');
    const permModalTitleEl = document.querySelector('#permModalTitle');
    const permModalStateEl = document.querySelector('#permModalState');
    const permModalPresetsEl = document.querySelector('#permModalPresets');
    const permModalBlockEl = document.querySelector('#permModalBlock');
    const permModalErrorEl = document.querySelector('#permModalError');
    const permModalCloseBtnEl = document.querySelector('#permModalCloseBtn');
    const liveBadgeEl = document.querySelector('#liveBadge');
    const connHealthBadgeEl = document.querySelector('#connHealthBadge');
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
    const inputNoteEl = document.querySelector('#inputNote');
    const inputTextEl = document.querySelector('#inputText');
    const inputEnterEl = document.querySelector('#inputEnter');
    const inputSendEl = document.querySelector('#inputSend');
    const termMenuBtnEl = document.querySelector('#termMenuBtn');
    const termOpenRealBtnEl = document.querySelector('#termOpenRealBtn');
    const termAccessBtnEl = document.querySelector('#termAccessBtn');
    const termKillBtnEl = document.querySelector('#termKillBtn');
    const clean = value => String(value ?? '');

    // ---- mobile layout: shrink the outer gutter once a session is open ----
    // (task item 5) -- the tab bar itself needs no drawer/backdrop any more
    // (it's already a single, always-visible slim row, on every viewport
    // size); this only toggles the CSS class the mobile media query above
    // uses to reclaim the desktop-style outer padding once a session fills
    // the screen.
    function updateLayoutState() {
      document.body.classList.toggle('has-selection', Boolean(selected));
    }

    // ---- Reusable "⋯" overflow menu (task item 3) --------------------------
    // Click-to-open/click-outside-or-Escape-to-close, shared by the header
    // menu, each term-bar menu, and the killed-sessions menu -- one small
    // generic component instead of a bespoke dropdown per screen. At most
    // one menu open at a time (opening one closes any other).
    function closeAllMenus() {
      for (const menu of document.querySelectorAll('.menu.open')) {
        menu.classList.remove('open');
        const btn = menu.querySelector('[aria-haspopup]');
        if (btn) btn.setAttribute('aria-expanded', 'false');
      }
    }
    function wireMenu(menuEl, btnEl) {
      btnEl.onclick = (event) => {
        event.stopPropagation();
        const willOpen = !menuEl.classList.contains('open');
        closeAllMenus();
        if (willOpen && !btnEl.disabled) {
          menuEl.classList.add('open');
          btnEl.setAttribute('aria-expanded', 'true');
        }
      };
      // A click inside the panel itself (e.g. a menu item's own confirm()
      // dialog trigger) must not bubble to the document-level listener
      // below and immediately close the menu before the item's own onclick
      // handler runs.
      menuEl.querySelector('.menu-panel').addEventListener('click', event => event.stopPropagation());
    }
    document.addEventListener('click', closeAllMenus);
    document.addEventListener('keydown', event => { if (event.key === 'Escape') closeAllMenus(); });
    wireMenu(document.querySelector('#headerMenu'), document.querySelector('#headerMenuBtn'));
    wireMenu(document.querySelector('#termMenu'), document.querySelector('#termMenuBtn'));
    wireMenu(document.querySelector('#killedMenu'), killedToggleEl);

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
    // Single source of truth (task item 6): the 16 base ANSI colours are the
    // same --ansi-0..15 custom properties the :root palette defines (the
    // Windows Terminal "Campbell" scheme by default), read once here rather
    // than hardcoded a second time -- changing the theme tokens re-colours
    // both the raw terminal surface AND every ANSI-coloured span this
    // renderer builds, with nothing to keep in sync by hand.
    const ROOT_STYLE = getComputedStyle(document.documentElement);
    function ansiVar(n) { return (ROOT_STYLE.getPropertyValue(`--ansi-${n}`) || '').trim() || '#c6c6c6'; }
    const ANSI_BASE = [0, 1, 2, 3, 4, 5, 6, 7].map(ansiVar);
    const ANSI_BRIGHT = [8, 9, 10, 11, 12, 13, 14, 15].map(ansiVar);
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
    // OSC sequences (ESC ] ... BEL-or-ESC\\) -- e.g. an OSC 8 hyperlink, or a
    // window-title set -- a real CLI can legitimately emit these (caught
    // live verifying this redesign against a real Claude Code session,
    // whose own output includes one), and tmux `capture-pane -e` passes
    // them through untouched. CSI_RE above only ever matches `ESC [`, so an
    // OSC sequence has no closing bracket/letter for it to consume --
    // without this, it would leak into the rendered pane as literal
    // garbage text instead of being silently dropped like every other
    // non-SGR control sequence already is.
    const OSC_RE = /\\x1b\\][^\\x07\\x1b]*(?:\\x07|\\x1b\\\\)/g;
    function ansiRuns(text) {
      text = text.replace(OSC_RE, '');
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
      termMenuBtnEl.disabled = !selected;
      termTitleEl.textContent = selected || '';
      refreshTermActionMenu();
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
        updateLayoutState(); // exiting must restore the normal layout
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
    // Task item 4: "clicking the terminal must focus the correct input" --
    // like a real terminal, clicking anywhere in the output jumps focus to
    // the input line below it. Skipped while the click just finished a
    // click-drag text selection (selection.isCollapsed is false only in
    // that case for a plain click) so copying text is never disrupted, and
    // skipped entirely while input isn't actually allowed for this session
    // (matches inputTextEl's own disabled gating in refreshInputControls).
    outputEl.addEventListener('click', () => {
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      if (!inputTextEl.disabled) inputTextEl.focus();
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
    // any grant control anywhere because it has nothing to grant/revoke,
    // ever -- that decision is made in one place (grantable() below) and
    // reused everywhere a control might render, so it can never drift.
    //
    // Entry points, both opening the SAME #permModal (no per-row lock/eye
    // icon anymore -- see item 2 of the dashboard cleanup this backs):
    //   - the term-bar's "🔐 Quyền truy cập" menu item, scoped to whichever
    //     session is currently selected (refreshTermActionMenu)
    //   - a compact "🔐 Quyền truy cập" line in the open session's own card
    //     (#grantBar below)
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
    // `names` (#permModal always calls this with exactly one -- the array
    // shape is kept only because applyPreset itself is otherwise generic),
    // then refreshes from the backend -- the ONE place any grant mutation
    // actually happens, so its outcome (and its real, server-confirmed
    // effective_read/effective_input) can never diverge from what's shown.
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

    // ---- #permModal: the one reusable grant/revoke UI (Access action) ------
    // Single-session only -- the old multi-select bulk-grant path lived in
    // the sidebar's row checkboxes, removed along with them (item 1 of the
    // dashboard cleanup this backs); bulk grant management, if needed,
    // still lives on the separate /dashboard/sessions admin screen.
    let permModalName = null; // the session name the open modal targets
    function closePermModal() {
      document.body.classList.remove('perm-modal-visible');
      permModalName = null;
    }
    permModalCloseBtnEl.onclick = closePermModal;
    permBackdropEl.onclick = closePermModal;
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && document.body.classList.contains('perm-modal-visible')) closePermModal();
    });

    function renderPermModalBody(name) {
      const row = lastKnownRows.find(r => r.name === name);
      permModalTitleEl.textContent = name;
      permModalBlockEl.textContent = '';
      permModalErrorEl.textContent = '';
      if (!row) { permModalStateEl.textContent = 'Session không còn tồn tại.'; permModalPresetsEl.replaceChildren(); return; }
      const state = grantState(row);
      const granted = grantStateLabel(state);
      const effective = effectiveLabel(row);
      permModalStateEl.textContent = granted === effective ? `Hiện tại: ${granted}` : `Đã cấp: ${granted} · Hiệu lực thực tế: ${effective}`;
      if (row.input_block_reason && state !== 'full') {
        permModalBlockEl.textContent = `Gửi prompt bị chặn: ${inputBlockLabel(row.input_block_reason)}`;
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
        if (state === preset.key) classes.push('current');
        if (preset.key === 'none') classes.push('danger');
        if (classes.length) btn.className = classes.join(' ');
        btn.onclick = async () => {
          permModalPresetsEl.querySelectorAll('button').forEach(b => b.disabled = true);
          const failures = await applyPreset([name], preset.key);
          permModalErrorEl.textContent = failures.length ? clean(failures.join('; ')) : '';
          renderPermModalBody(name); // stay open, show the refreshed real state
        };
        permModalPresetsEl.appendChild(btn);
      }
    }

    async function openPermModal(name) {
      permModalName = name;
      document.body.classList.add('perm-modal-visible');
      permModalTitleEl.textContent = name;
      permModalStateEl.textContent = 'Đang tải…';
      permModalPresetsEl.replaceChildren(); permModalBlockEl.textContent = ''; permModalErrorEl.textContent = '';
      await loadSessions(); // fresh grant/effective state before showing presets, every time
      if (permModalName === name) renderPermModalBody(name); // still open (not closed while awaiting)
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
      selected = name; inputAllowed = false;
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
      setAutoFollow(true); refreshInputControls(); refreshTermControls(); updateLayoutState();
      rememberSession(name);
      closeSearch(); // a search from a different session's content wouldn't make sense to keep open
      renderRows(lastKnownRows); // reflect the new active/selected row immediately, not just on the next 5s poll
      // Task item 2: the newly-active tab must always be scrolled into
      // view -- opening a session via the killed-sessions list, a URL/
      // hash, or a tab currently scrolled off past the edge of a narrow
      // (mobile) tab strip must never leave the user unable to see which
      // tab is actually selected. 'nearest' -- never yanks an
      // already-visible tab to a different edge on every poll.
      const activeRefs = tabEls.get(name);
      if (activeRefs) activeRefs.tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
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

    // ---- tab bar: the ONE session navigation surface (task item 2/3) --
    // Windows-Terminal-style horizontal tabs, replacing the old left
    // sidebar entirely -- never both at once (see the .tabbar CSS comment
    // above for why that exact duplication was already removed once
    // before in this project's history). Click a tab (or Enter/Space on
    // it) to select/open it -- no checkbox, no per-row lock/eye icon.
    // Contextual actions that used to live in a per-row button column
    // (Open Terminal / Access / Kill) now live in the term-bar's "⋯" menu
    // instead, operating on whichever session is currently selected -- see
    // refreshTermActionMenu below -- EXCEPT closing/killing a tab, which
    // stays directly on the tab itself (hover-reveal "✕") since that is
    // the one action a user expects to reach without first selecting the
    // tab. It opens the exact same typed-confirmation kill modal as
    // before -- there is no separate, weaker "close tab" semantic.
    const WEBTERM_PAGE = '/dashboard/terminal';
    let sessionLifecycleEnabled = false;
    let protectedSessions = new Set();
    let webTerminalEnabled = false;

    function tabDotClass(row) {
      // Reflects only states the backend already reports (classify_status's
      // real SUPERVISOR_STATES via row.state) -- never an invented one.
      if (row.state === 'FAILED' || row.state === 'ERROR' || row.state === 'BLOCKED') return 'tab-dot err';
      if (row.state === 'IDLE') return 'tab-dot idle';
      if (row.state === 'RESTRICTED' || row.state === 'UNKNOWN') return 'tab-dot';
      return 'tab-dot on'; // RUNNING / WAITING_INPUT / COMPLETION_CANDIDATE / VERIFYING / VERIFIED_DONE -- process alive & reachable
    }

    function buildTabEl(name) {
      // Called exactly once per session name for as long as it keeps
      // appearing in the list -- see the tabEls comment above for why
      // this node identity must never be torn down and recreated on a
      // routine poll. Static structure only; every value that can change
      // between polls (classes, text, title, handlers-over-the-latest-
      // row) is set by updateTabEl, called every time, including right
      // after this.
      const tab = document.createElement('div');
      tab.className = 'tab';
      tab.setAttribute('role', 'tab');
      tab.tabIndex = 0;
      const dot = document.createElement('span');
      const label = document.createElement('span'); label.className = 'tab-name';
      const badge = document.createElement('span'); badge.className = 'attn-badge'; badge.textContent = '⚠'; badge.hidden = true;
      const closeBtn = document.createElement('button');
      closeBtn.type = 'button'; closeBtn.className = 'tab-close'; closeBtn.textContent = '✕';
      tab.append(dot, label, badge, closeBtn);
      tabbarEl.append(tab);
      return { tab, dot, label, badge, closeBtn };
    }

    function updateTabEl(refs, row) {
      const { tab, dot, label, badge, closeBtn } = refs;
      const needsAttention = row.state === 'WAITING_INPUT';
      const isActive = selected === row.name;
      tab.className = 'tab' + (isActive ? ' active' : '') + (needsAttention ? ' needs-attention' : '');
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
      // The old per-row "N window · attached/no terminal attached" line
      // has no room in a compact tab -- kept as a hover tooltip instead
      // of dropped outright. Node/host info deliberately does NOT appear
      // here (task item 3: "Tab chỉ hiện tên session") -- it moved to
      // the session detail header once opened (see loadDetail's summary
      // render), not duplicated in both places.
      tab.title = `${row.name} · ${row.windows} window · ${row.attached ? 'Terminal attached' : 'No terminal attached'}`
        + (row.effective_read ? '' : ' · chưa cấp quyền xem');
      dot.className = tabDotClass(row);
      label.textContent = row.name;
      badge.hidden = !needsAttention;

      const isProtected = protectedSessions.has(row.name);
      closeBtn.disabled = !sessionLifecycleEnabled || isProtected;
      closeBtn.title = !sessionLifecycleEnabled ? ''
        : isProtected ? 'Session này được bảo vệ, không thể kill qua dashboard'
        : `Kill "${row.name}"`;
      // Re-bound every update (cheap) rather than captured once at build
      // time, so these always act on the CURRENT row (kill_reopen_ready
      // etc. can legitimately change between polls) without needing a
      // separate mutable-row indirection layer.
      closeBtn.onclick = (event) => {
        event.stopPropagation();
        if (closeBtn.disabled) return;
        openKillModal(row.name, row.kill_reopen_ready !== false);
      };
      const activate = () => selectSession(row.name);
      tab.onclick = activate;
      tab.onkeydown = (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); activate(); }
      };
    }

    function renderRows(rows) {
      lastKnownRows = rows;
      const emptyEl = tabbarEl.querySelector('.tabbar-empty');
      if (!rows.length) {
        if (!emptyEl) {
          const empty = document.createElement('div'); empty.className = 'tabbar-empty'; empty.textContent = 'Không có session nào.';
          tabbarEl.append(empty);
        }
        for (const [name, refs] of tabEls) { refs.tab.remove(); tabEls.delete(name); }
        refreshTermActionMenu();
        if (selected) { selected = null; inputAllowed = false; refreshInputControls(); refreshTermControls(); updateLayoutState();
          if (fullscreenTerminal) setFullscreen(false, { persist: false });
          summaryEl.textContent = 'Session không còn tồn tại.'; outputEl.replaceChildren(); grantBarEl.hidden = true; }
        return;
      }
      if (emptyEl) emptyEl.remove();
      // Drop tabs for sessions no longer in the list.
      const currentNames = new Set(rows.map(row => row.name));
      for (const [name, refs] of tabEls) {
        if (!currentNames.has(name)) { refs.tab.remove(); tabEls.delete(name); }
      }
      // Rows already arrive sorted attention-first, then most-recent-
      // activity, then name (see the /dashboard/api/sessions route) — no
      // client-side reordering here, just placing each tab (reused if it
      // already exists, built once if not) at its correct position.
      // appendChild on a node already in the DOM MOVES it rather than
      // duplicating it, so this reorders in place without ever recreating
      // an existing tab's element.
      for (const row of rows) {
        let refs = tabEls.get(row.name);
        if (!refs) { refs = buildTabEl(row.name); tabEls.set(row.name, refs); }
        else { tabbarEl.append(refs.tab); }
        updateTabEl(refs, row);
      }
      refreshTermActionMenu();
      if (selected && !rows.some(row => row.name === selected)) {
        selected = null; inputAllowed = false; refreshInputControls(); refreshTermControls(); updateLayoutState();
        if (fullscreenTerminal) setFullscreen(false, { persist: false }); // forced exit — the remembered preference is unrelated and must survive
        summaryEl.textContent = 'Session không còn tồn tại.'; outputEl.replaceChildren(); grantBarEl.hidden = true;
      }
    }

    // ---- term-bar "⋯" menu: Open Terminal / Access / Kill, now scoped to
    // whichever session is currently selected (task item 3 -- these used
    // to be a per-row button column, duplicating the tab's own identity;
    // now there is exactly one place they act on: the open tab). ----------
    function refreshTermActionMenu() {
      const row = selected ? lastKnownRows.find(r => r.name === selected) : null;

      if (row && row.effective_read) {
        termOpenRealBtnEl.href = `${WEBTERM_PAGE}?session=${encodeURIComponent(row.name)}`;
        termOpenRealBtnEl.classList.remove('disabled'); termOpenRealBtnEl.removeAttribute('aria-disabled');
        termOpenRealBtnEl.title = row.effective_input
          ? 'Mở web terminal thật (xterm.js), gắn trực tiếp vào tmux session này -- gõ được'
          : 'Mở web terminal thật (xterm.js) ở chế độ CHỈ XEM -- chưa có quyền input';
        if (!webTerminalEnabled) {
          termOpenRealBtnEl.removeAttribute('href');
          termOpenRealBtnEl.classList.add('disabled'); termOpenRealBtnEl.setAttribute('aria-disabled', 'true');
          termOpenRealBtnEl.title = 'Tính năng web terminal đang tắt (dashboard.web_terminal_enabled trong config.yaml)';
        }
      } else {
        termOpenRealBtnEl.removeAttribute('href');
        termOpenRealBtnEl.classList.add('disabled'); termOpenRealBtnEl.setAttribute('aria-disabled', 'true');
        termOpenRealBtnEl.title = row ? 'Chưa có quyền xem session này' : '';
      }

      const canGrant = Boolean(row) && grantable(row);
      termAccessBtnEl.disabled = !canGrant;
      termAccessBtnEl.title = canGrant ? 'Xem/cấp quyền truy cập cho session này' : '';
      termAccessBtnEl.onclick = () => { if (row) { closeAllMenus(); openPermModal(row.name); } };

      const isProtected = row ? protectedSessions.has(row.name) : false;
      termKillBtnEl.disabled = !row || !sessionLifecycleEnabled || isProtected;
      termKillBtnEl.title = !row || !sessionLifecycleEnabled ? ''
        : isProtected ? 'Session này được bảo vệ, không thể kill qua dashboard'
        : 'Dừng & giải phóng process/RAM của session này (có thể mở lại sau)';
      termKillBtnEl.onclick = () => {
        if (row && !termKillBtnEl.disabled) { closeAllMenus(); openKillModal(row.name, row.kill_reopen_ready !== false); }
      };
    }

    async function loadSessions() {
      const data = await fetchJSON('/dashboard/api/sessions', {cache:'no-store'});
      const rows = data.sessions || [];
      sessionLifecycleEnabled = data.session_lifecycle_enabled === true;
      protectedSessions = new Set(data.protected_sessions || []);
      webTerminalEnabled = data.web_terminal_enabled === true;
      // A 200 response can still legitimately carry data.error (e.g.
      // READ_DISABLED globally, alongside a correctly-empty `sessions: []`)
      // -- this is real, correct information ("reading is off"), not
      // "there simply are no sessions", and must never silently disappear
      // just because the old dedicated #count element is gone now that the
      // tab bar replaced the sidebar. Surfaced via #summary's own default/
      // empty-state text, the one place already guaranteed visible whenever
      // nothing is selected.
      // Only ever touches the plain, still-showing placeholder -- never a
      // more specific sticky message (e.g. renderRows's own "Session
      // không còn tồn tại." for a session that just disappeared), which
      // must stay exactly as it was until the user acts.
      if (!selected && summaryEl.textContent.startsWith('Chọn một session')) {
        summaryEl.textContent = data.error
          ? `Chọn một session để xem output. (${clean(data.error)})`
          : 'Chọn một session để xem output.';
      }

      // Paint the authoritative fresh rows BEFORE any auto-select below --
      // real bug, caught live in this redesign's own browser verification:
      // selectSession() repaints via renderRows(lastKnownRows) internally,
      // and on the very first cold load lastKnownRows was still its []
      // startup default (this is the FIRST time renderRows(rows) has ever
      // run) -- selectSession's own repaint then saw an empty list, its
      // "session no longer exists" branch fired, and immediately un-
      // selected the session it had just auto-opened. Populating
      // lastKnownRows here first means selectSession's internal repaint
      // below always sees the real, current rows, cold-start included.
      renderRows(rows);
      if (sessionLifecycleEnabled) loadKilledSessions(); else killedToggleEl.hidden = true;

      // On first load only (never on the recurring 5s poll, which must not
      // fight a user's manual choice to switch sessions or clear the
      // selection): auto-open the remembered session if it still exists,
      // else the first readable session (a restricted row is real and
      // listed now -- see dashboard_list_sessions -- but auto-opening one
      // would only greet a first-time viewer with a locked placeholder
      // instead of real output).
      const readableRows = rows.filter(row => row.effective_read);
      if (!autoSelectAttempted) {
        autoSelectAttempted = true;
        if (!selected && readableRows.length) {
          const remembered = recalledSession();
          const target = (remembered && readableRows.some(row => row.name === remembered)) ? remembered : readableRows[0].name;
          selectSession(target);
        }
      }
    }

    // ---- Kill confirmation modal (destructive; item 7/8 of the design
    // this backs) -- a SEPARATE modal from #permModal on purpose, so a
    // destructive action can never be mistaken for the harmless grant/
    // revoke one. The Kill button stays disabled until the typed
    // confirmation exactly matches the session name; core.py's own
    // confirm_name check is the real, server-enforced floor -- this is
    // only the human-facing half of it.
    let killModalName = null;
    function closeKillModal() {
      document.body.classList.remove('kill-modal-visible');
      killModalName = null; killConfirmInputEl.value = ''; killModalErrorEl.textContent = '';
    }
    function openKillModal(name, likelyReopenable) {
      killModalName = name;
      killModalTitleEl.textContent = `Kill "${name}"`;
      killConfirmInputEl.value = ''; killModalErrorEl.textContent = '';
      killConfirmBtnEl.disabled = true;
      killModalWarnEl.hidden = likelyReopenable !== false;
      if (!killModalWarnEl.hidden) {
        killModalWarnEl.textContent = '⚠ Session này chưa từng được tạo qua lifecycle service (hoặc chưa nhận diện được agent/thư mục làm việc) -- sau khi kill có thể KHÔNG reopen tự động được, có thể phải chọn agent/thư mục thủ công.';
      }
      document.body.classList.add('kill-modal-visible');
      killConfirmInputEl.focus();
    }
    killModalCloseBtnEl.onclick = closeKillModal;
    killCancelBtnEl.onclick = closeKillModal;
    killBackdropEl.onclick = closeKillModal;
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && document.body.classList.contains('kill-modal-visible')) closeKillModal();
    });
    killConfirmInputEl.oninput = () => {
      killConfirmBtnEl.disabled = killModalName === null || killConfirmInputEl.value !== killModalName;
    };
    killConfirmBtnEl.onclick = async () => {
      const name = killModalName;
      if (!name || killConfirmInputEl.value !== name) return; // client-side floor; core.py's own confirm_name check is the real one
      killConfirmBtnEl.disabled = true;
      const response = await fetch('/dashboard/api/session/kill', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name, confirm_name: killConfirmInputEl.value}),
      });
      const result = await response.json().catch(() => ({}));
      if (result && result.error) {
        killModalErrorEl.textContent = clean(result.error);
        killConfirmBtnEl.disabled = killConfirmInputEl.value !== killModalName;
        return;
      }
      closeKillModal();
      if (selected === name) {
        selected = null; inputAllowed = false; refreshInputControls(); refreshTermControls(); updateLayoutState();
        summaryEl.textContent = 'Chọn một session để xem output.'; outputEl.replaceChildren(); grantBarEl.hidden = true;
      }
      await loadSessions();
    };

    // ---- killed-sessions reopen list (item 9-11) -----------------------
    // Compact, collapsed by default, zero footprint until there's actually
    // a killed session to reopen -- never a second navigation surface for
    // LIVE sessions (only ever lists sessions that no longer exist). Its
    // open/closed state is now just the generic .menu component (wired via
    // wireMenu(#killedMenu, killedToggleEl) above) -- this only populates
    // rows and shows/hides the toggle button itself.
    let lastKilledEntries = [];
    function renderKilledSessions(entries) {
      lastKilledEntries = entries;
      killedToggleEl.hidden = entries.length === 0;
      killedToggleLabelEl.textContent = `🗑 Đã kill (${entries.length})`;
      killedListEl.replaceChildren();
      for (const entry of entries) {
        const row = document.createElement('div'); row.className = 'killed-row';
        const name = document.createElement('div'); name.className = 'kr-name'; name.textContent = entry.name;
        const meta = document.createElement('div'); meta.className = 'kr-meta';
        meta.textContent = entry.metadata_complete
          ? `${clean(entry.agent_type)}${entry.working_directory ? ' · ' + clean(entry.working_directory) : ''}`
          : '';
        row.append(name, meta);
        if (!entry.metadata_complete) {
          const warn = document.createElement('div'); warn.className = 'kr-incomplete';
          warn.textContent = '⚠ Thiếu metadata -- reopen sẽ cần chọn agent/thư mục thủ công.';
          row.append(warn);
        }
        const reopenBtn = document.createElement('button'); reopenBtn.type = 'button'; reopenBtn.textContent = '↩ Reopen';
        reopenBtn.onclick = () => reopenKilledSession(entry);
        row.append(reopenBtn);
        // Task item 9: "cho phép đổi node nếu user chọn Move/Reopen
        // elsewhere" -- default Reopen above always stays on the same
        // node (server-side default); this is the explicit, separate
        // opt-in to pick a different one. A plain prompt() pair, same
        // minimal-UI convention as the incomplete-metadata path above --
        // this is a rare/advanced action, not worth a dedicated modal.
        const moveBtn = document.createElement('button'); moveBtn.type = 'button'; moveBtn.textContent = '↩▾ Reopen elsewhere';
        moveBtn.onclick = () => reopenKilledSessionElsewhere(entry);
        row.append(moveBtn);
        killedListEl.appendChild(row);
      }
    }
    async function loadKilledSessions() {
      try {
        const data = await fetchJSON('/dashboard/api/killed-sessions', {cache:'no-store'});
        renderKilledSessions(data.killed_sessions || []);
      } catch (error) { /* transient poll failure -- next 5s cycle retries, same as loadSessions itself */ }
    }
    async function reopenKilledSession(entry) {
      let agentType = null, workingDirectory = null;
      if (!entry.metadata_complete) {
        // Fail closed, never guess (item 10/12): ask explicitly rather
        // than inventing an agent_type/cwd. A plain, minimal prompt()
        // pair is deliberate here -- this is the rare/incomplete-metadata
        // path, not worth a dedicated modal for.
        agentType = window.prompt(
          `Không đủ metadata để tự reopen "${entry.name}".\nNhập agent_type (shell / claude / codex):`, 'shell');
        if (!agentType) return;
        if (agentType !== 'shell') {
          workingDirectory = window.prompt('Nhập working_directory an toàn (trong allowed_cwd_roots):', '');
          if (!workingDirectory) return;
        }
      }
      const body = {name: entry.name};
      if (agentType) body.agent_type = agentType;
      if (workingDirectory) body.working_directory = workingDirectory;
      const response = await fetch('/dashboard/api/session/reopen', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (result && result.error) {
        window.alert(`Reopen thất bại: ${clean(result.error)}${result.missing ? ' (thiếu: ' + result.missing.join(', ') + ')' : ''}`);
        return;
      }
      await loadSessions();
      selectSession(entry.name);
    }

    // Task item 9: "cho phép đổi node nếu user chọn Move/Reopen
    // elsewhere" -- fetches the current node list fresh (never cached;
    // this is a rare action, correctness here matters more than one
    // extra request) so the prompt always reflects real, current
    // node ids -- never a stale list from whenever the page first loaded.
    async function reopenKilledSessionElsewhere(entry) {
      let nodes = [];
      try {
        const response = await fetch('/dashboard/api/nodes', {cache: 'no-store'});
        const data = await response.json().catch(() => ({}));
        nodes = (data && data.nodes) || [];
      } catch (error) { /* fall through -- the prompt below still works with an empty list */ }
      const choices = nodes.map(n => `${n.id}${n.id === 'local' ? ' (Local/Dell)' : ''} [${n.status}]`).join(`\n`);
      const targetNode = window.prompt(
        `Reopen "${entry.name}" trên node nào?\nNode hiện có:\n${choices || '(không tải được danh sách node)'}\n\nNhập node_id:`, '');
      if (!targetNode) return;
      let agentType = entry.agent_type || null, workingDirectory = entry.working_directory || null;
      if (!entry.metadata_complete) {
        agentType = window.prompt(`Không đủ metadata để tự reopen "${entry.name}".\nNhập agent_type (shell / claude / codex):`, 'shell');
        if (!agentType) return;
        if (agentType !== 'shell') {
          workingDirectory = window.prompt('Nhập working_directory an toàn (trong allowed_cwd_roots của node đích):', '');
          if (!workingDirectory) return;
        }
      }
      const body = {name: entry.name, node: targetNode};
      if (agentType) body.agent_type = agentType;
      if (workingDirectory) body.working_directory = workingDirectory;
      const response = await fetch('/dashboard/api/session/reopen', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
      });
      const result = await response.json().catch(() => ({}));
      if (result && result.error) {
        window.alert(`Reopen elsewhere thất bại: ${clean(result.error)}${result.detail ? ' -- ' + clean(result.detail) : ''}`);
        return;
      }
      await loadSessions();
      selectSession(entry.name);
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
      // Node/host (task item 3: moved out of the tab label into here,
      // the session's own detail header, shown only once opened). Sourced
      // from the already-fetched session list (every row is tagged with
      // node_id/node_name -- local and remote alike, see the
      // /dashboard/api/sessions route) rather than re-fetching -- this
      // detail endpoint itself doesn't carry it.
      const rowForNode = lastKnownRows.find(row => row.name === selected);
      if (rowForNode && rowForNode.node_name && rowForNode.node_id !== 'local') {
        const nodeLabel = document.createElement('span'); nodeLabel.className = 'muted';
        nodeLabel.textContent = ` · node: ${rowForNode.node_name}`;
        summaryEl.append(nodeLabel);
      }
      const switchedSession = selected !== lastRenderedSession;
      if (switchedSession) setAutoFollow(true); // opening a session always starts followed
      renderAnsi(outputEl, clean(data.tail.output));
      // Blinking cursor glyph at the very end of the rendered output --
      // purely cosmetic (task item 1/6, "clear cursor"); only ever appended
      // on this success path, never for the READ_RESTRICTED placeholder or
      // an error state above, and rebuilt fresh on every render since
      // renderAnsi() itself already replaces #output's children wholesale.
      const cursor = document.createElement('span'); cursor.className = 'term-cursor';
      outputEl.appendChild(cursor);
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

    // ---- connection health banner (OpenAI Secure MCP Tunnel) ---------------
    // One quiet, always-present label -- never a popup, never re-fetched on
    // the same fast 5s cadence as session polling (task item 5: "không spam
    // UI"); the underlying check itself already skips the slower external
    // DNS/TLS probe (see dashboard.py's own route comment), so this stays
    // cheap even at this interval. A fetch failure here never touches
    // liveBadgeEl/setConnectionState -- this banner's own health has
    // nothing to do with whether THIS dashboard page can reach the server.
    const CONN_BADGE_CLASSES = {
      'Connected': '', 'Recovering': 'conn-recovering',
      'Local OK but tunnel stale': 'conn-tunnel-stale', 'Local MCP down': 'conn-mcp-down',
    };
    async function loadConnectionHealth() {
      try {
        const data = await fetchJSON('/dashboard/api/connection-health', {cache: 'no-store'});
        const label = clean(data.banner) || 'Connected';
        connHealthBadgeEl.hidden = false;
        connHealthBadgeEl.textContent = label;
        connHealthBadgeEl.className = 'supervisor-badge ' + (CONN_BADGE_CLASSES[label] || '');
      } catch (error) {
        // Sign-in-required/offline is already surfaced by the main LIVE
        // badge -- this banner just goes quiet rather than showing a
        // second, redundant error state.
        connHealthBadgeEl.hidden = true;
      }
    }
    loadConnectionHealth(); setInterval(loadConnectionHealth, 30000);
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
    /* Node label (task item 6: "sidebar hiển thị node label nhỏ") -- a
       compact pill, same shape as .perm-badge but a distinct muted tone
       so it never reads as a permission state. */
    .node-badge { display:inline-block; border-radius:999px; padding:1px 7px; font-size:10px; border:1px solid var(--line); color:var(--muted); margin-left:6px; vertical-align:middle; white-space:nowrap }
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
    .toolbar button.primary { background:var(--accent); border:1px solid var(--accent); border-radius:8px; color:#fff; padding:7px 14px; cursor:pointer; font:inherit; font-size:13px; font-weight:600 }
    .toolbar button.primary:hover { filter:brightness(1.08) }
    .toolbar button.primary:disabled { opacity:.5; cursor:not-allowed; filter:none }
    .row-actions button.danger { border-color:#ff6b6b; color:#ff9f9f }
    .row-actions button:disabled { opacity:.4; cursor:not-allowed }
    /* Create-session modal -- same component/behavior as #permModal above. */
    #csBackdrop { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:30 }
    #csModal {
      display:none; position:fixed; top:50%; left:50%; transform:translate(-50%,-50%); z-index:31;
      width:min(420px, calc(100vw - 32px)); max-height:85vh; overflow:auto;
      background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:0 20px 50px rgba(0,0,0,.6);
    }
    body.cs-modal-visible #csBackdrop, body.cs-modal-visible #csModal { display:block }
    #csModal .cs-head { padding:14px 16px; border-bottom:1px solid var(--line); display:flex; justify-content:space-between; align-items:center }
    #csModal .cs-body { padding:16px; display:flex; flex-direction:column; gap:12px }
    #csModal label { font-size:12px; color:var(--muted) }
    #csModal input[type=text], #csModal select {
      width:100%; margin-top:4px; padding:9px 10px; border-radius:8px; border:1px solid var(--line);
      background:#0f1730; color:var(--text); font:inherit; font-size:13px;
    }
    #csModal .cs-agent-choices { display:flex; gap:8px }
    #csModal .cs-agent-choices button {
      flex:1; padding:9px 6px; border-radius:8px; border:1px solid var(--line); background:#19243b;
      color:var(--text); cursor:pointer; font:inherit; font-size:13px;
    }
    #csModal .cs-agent-choices button.selected { border-color:var(--accent); color:var(--accent) }
    #csModal .cs-submit { background:var(--accent); border:none; border-radius:8px; color:#fff; padding:10px; cursor:pointer; font:inherit; font-size:14px; font-weight:600 }
    #csModal .cs-submit:disabled { opacity:.5; cursor:not-allowed }
    #csModal button.close { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:4px 9px; cursor:pointer; font:inherit }
    #csModal .cs-error { color:#ff6b6b; font-size:12px; min-height:14px }
    #csModal .cs-hint { color:var(--muted); font-size:11px }
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
    <button id="newSessionBtn" class="primary" type="button">+ Tạo session</button>
    <input type="text" id="searchBox" placeholder="Tìm theo tên session...">
    <label><input type="checkbox" id="onlyGrantable"> Chỉ hiện session chưa whitelist</label>
    <span id="count"></span>
  </div>
  <div id="bulkBar" hidden></div>
  <main>
    <table>
      <thead>
        <tr>
          <th></th><th>Session</th><th>Quyền</th><th>Trạng thái</th><th>Windows</th><th>Hoạt động gần nhất</th><th>Hành động</th>
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
  <div id="csBackdrop"></div>
  <div id="csModal" role="dialog" aria-modal="true" aria-labelledby="csModalTitle">
    <div class="cs-head">
      <strong id="csModalTitle">Tạo session mới</strong>
      <button id="csModalCloseBtn" class="close" type="button">✕</button>
    </div>
    <form class="cs-body" id="csForm">
      <div>
        <label for="csName">Tên session (bắt buộc)</label>
        <input type="text" id="csName" maxlength="128" placeholder="vd: codex-my-task" autocomplete="off" required>
        <div class="cs-hint">Chỉ chữ/số/._- , không dấu cách, không ký tự shell.</div>
      </div>
      <div>
        <label>Loại session</label>
        <div class="cs-agent-choices" id="csAgentChoices">
          <button type="button" data-agent="shell" class="selected">Shell</button>
          <button type="button" data-agent="claude">Claude</button>
          <button type="button" data-agent="codex">Codex</button>
        </div>
      </div>
      <div>
        <label for="csNode">Node/Host</label>
        <select id="csNode"><option value="auto">Auto (Recommended)</option></select>
        <div class="cs-hint" id="csNodeHint"></div>
      </div>
      <div>
        <label for="csCwd">Working directory (tuỳ chọn)</label>
        <input type="text" id="csCwd" maxlength="512" placeholder="để trống = mặc định" autocomplete="off">
        <div class="cs-hint">Phải nằm trong thư mục được phép cấu hình sẵn trên server.</div>
      </div>
      <div class="cs-error" id="csError"></div>
      <button type="submit" class="cs-submit" id="csSubmitBtn">Tạo session</button>
    </form>
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
    const newSessionBtnEl = document.querySelector('#newSessionBtn');
    const csBackdropEl = document.querySelector('#csBackdrop');
    const csFormEl = document.querySelector('#csForm');
    const csNameEl = document.querySelector('#csName');
    const csCwdEl = document.querySelector('#csCwd');
    const csAgentChoicesEl = document.querySelector('#csAgentChoices');
    const csNodeEl = document.querySelector('#csNode');
    const csNodeHintEl = document.querySelector('#csNodeHint');
    const csErrorEl = document.querySelector('#csError');
    const csSubmitBtnEl = document.querySelector('#csSubmitBtn');
    const csModalCloseBtnEl = document.querySelector('#csModalCloseBtn');

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
    let protectedSessions = new Set();
    let sessionLifecycleEnabled = true; // optimistic default until the first /dashboard/api/sessions response is seen
    let webTerminalEnabled = true; // same optimistic-default convention as sessionLifecycleEnabled above
    const WEBTERM_PAGE = '/dashboard/terminal';

    // -- Create-session modal ---------------------------------------------
    let csSelectedAgent = 'shell';
    let csNodesCache = []; // last-fetched /dashboard/api/nodes rows, reused when the agent-type choice changes

    // Multi-node create-session UX (task's own item 1-8): node select
    // defaults to Auto, options are Local + every registered remote node,
    // filtered/disabled by whether the CURRENTLY-selected agent type is
    // actually available there -- mirrors scheduler.py's own _eligible
    // capability check exactly (agent_type "shell" needs nothing special;
    // anything else must appear in that node's own reported agent_types),
    // never a second, independently-drifting notion of "supported".
    function nodeCapable(node, agentType) {
      return agentType === 'shell' || (node.agent_types || []).includes(agentType);
    }
    function nodeSummaryLabel(node) {
      const os = node.platform === 'windows' ? 'Windows' : 'Linux';
      const health = node.status !== 'online' ? 'Offline'
        : node.capacity_status === 'overloaded' ? 'Overloaded'
        : node.capacity_status === 'busy' ? 'Busy'
        : node.capacity_status === 'healthy' ? 'Healthy' : 'Unknown';
      const ram = (node.ram_percent === null || node.ram_percent === undefined) ? '' : ` · RAM ${Math.round(node.ram_percent)}%`;
      return `${node.display_name || node.id} · ${os} · ${health}${ram}`;
    }
    function renderNodeOptions() {
      const previous = csNodeEl.value || 'auto';
      csNodeEl.replaceChildren();
      const autoOpt = document.createElement('option'); autoOpt.value = 'auto'; autoOpt.textContent = 'Auto (Recommended)';
      csNodeEl.append(autoOpt);
      for (const node of csNodesCache) {
        const opt = document.createElement('option');
        opt.value = node.id;
        opt.textContent = (node.id === 'local' ? 'Local/Dell' : node.display_name || node.id) + ' — ' + nodeSummaryLabel(node);
        const capable = nodeCapable(node, csSelectedAgent);
        const online = node.status === 'online';
        if (!capable) {
          opt.disabled = true;
          opt.textContent += `  (thiếu agent_type=${csSelectedAgent})`;
        } else if (!online) {
          opt.disabled = true;
          opt.textContent += '  (offline)';
        }
        csNodeEl.append(opt);
      }
      // A previously-picked node that's now missing/disabled falls back to
      // Auto rather than silently submitting against a stale selection.
      const stillValid = [...csNodeEl.options].some(o => o.value === previous && !o.disabled);
      csNodeEl.value = stillValid ? previous : 'auto';
      csNodeHintEl.textContent = csNodeEl.value === 'auto'
        ? 'Scheduler tự chọn node phù hợp còn healthy nhất.'
        : '';
    }
    async function loadNodesForCreateModal() {
      try {
        const response = await fetch('/dashboard/api/nodes', {cache: 'no-store'});
        const data = await response.json().catch(() => ({}));
        csNodesCache = (data && data.nodes) || [];
      } catch (error) {
        csNodesCache = [];
      }
      renderNodeOptions();
    }

    function closeCreateModal() {
      document.body.classList.remove('cs-modal-visible');
      csErrorEl.textContent = ''; csFormEl.reset();
      csSelectedAgent = 'shell';
      csAgentChoicesEl.querySelectorAll('button').forEach(b => b.classList.toggle('selected', b.dataset.agent === 'shell'));
    }
    function openCreateModal() {
      csErrorEl.textContent = '';
      document.body.classList.add('cs-modal-visible');
      csNameEl.focus();
      loadNodesForCreateModal(); // fresh every open -- item 13: newly-registered nodes show up with no reload needed
    }
    newSessionBtnEl.onclick = openCreateModal;
    csModalCloseBtnEl.onclick = closeCreateModal;
    csBackdropEl.onclick = closeCreateModal;
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && document.body.classList.contains('cs-modal-visible')) closeCreateModal();
    });
    csAgentChoicesEl.querySelectorAll('button').forEach(btn => {
      btn.onclick = () => {
        csSelectedAgent = btn.dataset.agent;
        csAgentChoicesEl.querySelectorAll('button').forEach(b => b.classList.toggle('selected', b === btn));
        renderNodeOptions(); // re-filter the SAME cached node list -- no re-fetch needed just for this
      };
    });
    // Client-side mirror of permissions.py's SAFE_SESSION_RE -- pure UX
    // (an early, friendly error instead of a round-trip); the server
    // enforces this same shape independently and authoritatively no
    // matter what this check does or doesn't catch.
    const SAFE_SESSION_NAME_RE = /^[A-Za-z0-9_.-]{1,128}$/;
    csFormEl.onsubmit = async (event) => {
      event.preventDefault();
      const name = csNameEl.value.trim();
      const cwd = csCwdEl.value.trim();
      const chosenNode = csNodeEl.value || 'auto';
      csErrorEl.textContent = '';
      if (!SAFE_SESSION_NAME_RE.test(name) || name.startsWith('-') || name.startsWith('.')) {
        csErrorEl.textContent = 'Tên session không hợp lệ (chỉ chữ/số/._- , không bắt đầu bằng "-" hoặc ".").';
        return;
      }
      // Task item 7: re-validate the explicit node choice right before
      // submit -- it may have gone offline/overloaded/lost capability in
      // the time the form was open. Auto is always re-checked by the
      // server's own scheduler regardless, so only an EXPLICIT pick needs
      // this extra round-trip.
      if (chosenNode !== 'auto') {
        await loadNodesForCreateModal();
        const fresh = csNodesCache.find(n => n.id === chosenNode);
        if (!fresh || fresh.status !== 'online' || !nodeCapable(fresh, csSelectedAgent)) {
          csErrorEl.textContent = `Node "${chosenNode}" không còn khả dụng (offline hoặc thiếu capability) -- chọn Auto hoặc node khác.`;
          csNodeEl.value = 'auto';
          return;
        }
        if (fresh.capacity_status === 'overloaded') {
          csErrorEl.textContent = `⚠ Node "${chosenNode}" đang overloaded -- vẫn có thể tạo, nhưng cân nhắc chọn Auto/node khác. Bấm "Tạo session" lần nữa để xác nhận.`;
          csErrorEl.dataset.overloadedConfirm = chosenNode;
          if (csErrorEl.dataset.lastOverloadedConfirm !== chosenNode) {
            csErrorEl.dataset.lastOverloadedConfirm = chosenNode;
            return; // first submit just warns; a second submit (same node) proceeds
          }
        }
      }
      csSubmitBtnEl.disabled = true; csSubmitBtnEl.textContent = 'Đang tạo…';
      try {
        const response = await fetch('/dashboard/api/session/create', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name, agent_type: csSelectedAgent, cwd: cwd || null, node: chosenNode}),
        });
        const result = await response.json().catch(() => ({}));
        if (result && result.error) {
          csErrorEl.textContent = `Lỗi: ${clean(result.error)}${result.reason ? ' -- ' + clean(result.reason) : ''}`;
          return;
        }
        closeCreateModal();
        await load();
      } catch (error) {
        csErrorEl.textContent = `Lỗi mạng: ${clean(error && error.message)}`;
      } finally {
        csSubmitBtnEl.disabled = false; csSubmitBtnEl.textContent = 'Tạo session';
      }
    };

    // Real tmux detach-client (⏏ Tách below) -- never a browser-local
    // hide/show toggle (that feature, tied to the main dashboard's now-
    // removed tab bar, no longer exists on either screen).
    async function detachSessionReal(name, btn) {
      btn.disabled = true;
      try {
        const response = await fetch('/dashboard/api/session/detach', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name}),
        });
        const result = await response.json().catch(() => ({}));
        if (result && result.error) { alert(`Không tách được "${name}": ${clean(result.error)}`); }
        await load();
      } catch (error) {
        alert(`Lỗi mạng khi tách "${name}": ${clean(error && error.message)}`);
      } finally {
        btn.disabled = false;
      }
    }

    // Dừng & xóa hẳn session -- terminate the process, never recoverable.
    // Kill (docs: item 7/8 of the Kill/Reopen design) supersedes plain
    // delete here -- same real tmux kill-session + binding/grant cleanup,
    // PLUS reopen-metadata capture terminal_delete_session never did. One
    // destructive mechanism system-wide (core.py's terminal_kill_session),
    // reused by both this admin table and the main dashboard's sidebar --
    // never a second, parallel "delete" path. Typed confirmation (not
    // just confirm()) matches the main dashboard's #killModal; this
    // screen uses a plain prompt() instead of a dedicated modal, since it
    // already has no modal chrome of its own beyond #permModal/#createModal.
    async function killSessionReal(name, btn) {
      const typed = window.prompt(
        `Kill (dừng & giải phóng RAM) session "${name}"?\n\nGõ chính xác tên session để xác nhận -- `
        + `không thể hoàn tác. Nếu đủ metadata, có thể Reopen lại sau từ danh sách "Đã kill" trên dashboard chính.`,
        '',
      );
      if (typed === null) return; // cancelled
      if (typed !== name) { alert('Tên không khớp -- đã huỷ kill.'); return; }
      btn.disabled = true;
      try {
        const response = await fetch('/dashboard/api/session/kill', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name, confirm_name: typed}),
        });
        const result = await response.json().catch(() => ({}));
        if (result && result.error) { alert(`Không kill được "${name}": ${clean(result.error)}`); }
        await load();
      } catch (error) {
        alert(`Lỗi mạng khi kill "${name}": ${clean(error && error.message)}`);
      } finally {
        btn.disabled = false;
      }
    }

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
        // Node label (task item 6) -- only shown for a NON-local node
        // (the obvious default on a single-node deployment needs no
        // label; a remote node's own session does, so an operator always
        // knows where it actually runs before Kill/Delete/Access).
        if (row.node_id && row.node_id !== 'local') {
          const nodeBadge = document.createElement('span'); nodeBadge.className = 'node-badge';
          nodeBadge.textContent = row.node_name || row.node_id;
          tdName.appendChild(nodeBadge);
        }
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

        // Process/session liveness ("● Running" -- always true for a row
        // in this list; tmux drops a dead pane's session from its own
        // listing) is a separate axis from terminal CLIENT attachment
        // (real terminal or this dashboard's Open Terminal) -- a
        // detached session is not a dead one, so this never says just
        // "detached" as if it were.
        const tdAttach = document.createElement('td');
        const dot = document.createElement('span'); dot.className = 'attach-dot on';
        tdAttach.append(dot, document.createTextNode(
          `Running · ${row.attached ? 'Terminal attached' : 'No terminal attached'}`));
        tr.appendChild(tdAttach);

        const tdWindows = document.createElement('td'); tdWindows.textContent = row.windows;
        tr.appendChild(tdWindows);

        const tdActivity = document.createElement('td'); tdActivity.textContent = timeAgo(row.activity);
        tr.appendChild(tdActivity);

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

        // Open Terminal: a real, interactive xterm.js session attached
        // directly to this tmux session's own pty (webterm.py) -- distinct
        // from "↗ Mở" above, which only shows already-captured pane TEXT
        // through the polling API. Requires read access (row.effective_read)
        // same as "↗ Mở"; whether typing works once open is a separate,
        // server-enforced decision (row.effective_input) the terminal page
        // itself surfaces live, never assumed here. Made visually prominent
        // for a detached session specifically (requirement: "với session
        // detached nút Open Terminal nổi bật") -- accent-colored instead of
        // the flat row-action grey every other button here uses.
        if (row.effective_read) {
          const termBtn = document.createElement('a');
          termBtn.textContent = row.attached ? '🖥 Mở Terminal' : '🖥 Mở Terminal ⚡';
          termBtn.href = `${WEBTERM_PAGE}?session=${encodeURIComponent(row.name)}`;
          termBtn.title = row.effective_input
            ? 'Mở web terminal thật (xterm.js), gắn trực tiếp vào tmux session này -- gõ được'
            : 'Mở web terminal thật (xterm.js) ở chế độ CHỈ XEM -- chưa có quyền input';
          const prominent = !row.attached;
          termBtn.style.cssText = prominent
            ? 'background:var(--accent);border:1px solid var(--accent);border-radius:6px;color:#fff;padding:4px 9px;text-decoration:none;font-size:12px;font-weight:600'
            : 'background:#19243b;border:1px solid var(--line);border-radius:6px;color:inherit;padding:4px 9px;text-decoration:none;font-size:12px';
          if (!webTerminalEnabled) {
            termBtn.removeAttribute('href');
            termBtn.style.opacity = '.4'; termBtn.style.cursor = 'not-allowed';
            termBtn.title = 'Tính năng web terminal đang tắt (dashboard.web_terminal_enabled trong config.yaml)';
          }
          actions.appendChild(termBtn);
        }

        // Real tmux detach/delete -- gated on session_lifecycle_enabled
        // (server-side truth, mirrored here only so the button correctly
        // shows as disabled instead of round-tripping to a guaranteed
        // SESSION_LIFECYCLE_DISABLED). Delete is additionally disabled
        // for any protected session (always includes "terminal-mcp").
        const tachBtn = document.createElement('button'); tachBtn.type = 'button'; tachBtn.textContent = '⏏ Tách';
        tachBtn.title = 'Ngắt kết nối tmux client thật (không kill session, không mất dữ liệu)';
        tachBtn.disabled = !sessionLifecycleEnabled;
        tachBtn.onclick = () => detachSessionReal(row.name, tachBtn);
        actions.appendChild(tachBtn);

        const isProtected = protectedSessions.has(row.name);
        const killBtn = document.createElement('button'); killBtn.type = 'button'; killBtn.className = 'danger';
        killBtn.textContent = '🗑 Kill';
        killBtn.title = isProtected ? 'Session này được bảo vệ, không thể kill qua dashboard'
          : 'Dừng & giải phóng RAM của session này (có thể Reopen lại sau nếu đủ metadata)';
        killBtn.disabled = !sessionLifecycleEnabled || isProtected;
        killBtn.onclick = () => killSessionReal(row.name, killBtn);
        actions.appendChild(killBtn);

        tdActions.appendChild(actions);
        tr.appendChild(tdActions);

        tbodyEl.appendChild(tr);
      }
    }

    async function load() {
      const data = await fetchJSON('/dashboard/api/sessions', {cache:'no-store'});
      const rows = data.sessions || [];
      lastKnownRows = rows;
      protectedSessions = new Set(data.protected_sessions || ['terminal-mcp']);
      sessionLifecycleEnabled = data.session_lifecycle_enabled !== false;
      webTerminalEnabled = data.web_terminal_enabled === true;
      newSessionBtnEl.disabled = !sessionLifecycleEnabled;
      newSessionBtnEl.title = sessionLifecycleEnabled ? ''
        : 'Tính năng tạo session đang tắt (session_lifecycle.enabled: false trong config.yaml)';
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


# Node management (task item 4/16, multi-node session management) -- a
# third dedicated admin screen, same "own page, not folded into
# DASHBOARD_HTML's tab UI" precedent SESSIONS_ADMIN_HTML already set.
# Reads /dashboard/api/nodes (overview cards) and /dashboard/api/node?id=
# (detail: full metrics + that node's own session list), and the two
# mutation routes /dashboard/api/node/drain and /dashboard/api/node/test-
# connection -- no new backend beyond what dashboard.py already registers
# below. On a single-node deployment (today) this always shows exactly
# one card ("local") -- genuinely useful even then, as the same "why is
# capacity_status busy/overloaded" visibility terminal_list_nodes gives
# ChatGPT/Claude, just for a human. Deliberately NOT auto-polished with
# charts/history -- current values + a manual/interval refresh, matching
# the task's own "không cần biểu đồ phức tạp, số liệu rõ ràng là đủ" (an
# explicit, not literal-elsewhere-quoted, scope cut: keep this simple).
NODES_ADMIN_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Quản lý node</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --green:#43d17c; --amber:#ffc857; --red:#ff6b6b; --accent:#5b8cff; --mono: ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace; }
    * { box-sizing:border-box }
    html, body { height:100vh; height:100dvh; overflow:hidden }
    body { margin:0; font:14px/1.5 var(--mono); background:var(--bg); color:var(--text); display:flex; flex-direction:column }
    header { flex:0 0 auto; display:flex; justify-content:space-between; gap:16px; align-items:center; padding:18px 24px; border-bottom:1px solid var(--line); flex-wrap:wrap }
    h1 { margin:0; font-size:18px } .muted { color:var(--muted) }
    .live { color:var(--green); font-size:12px } .live.offline { color:var(--red) } .live.auth-required { color:#ffb347 }
    a.back { color:var(--muted); text-decoration:none; font-size:12px; border:1px solid var(--line); border-radius:999px; padding:4px 10px }
    a.back:hover { color:var(--text); border-color:var(--muted) }
    button.icon-btn { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:5px 11px; cursor:pointer; font:inherit; font-size:12px }
    button.icon-btn:hover { background:#233252 }
    main { flex:1; min-height:0; overflow:auto; padding:18px 24px 24px }
    #cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:14px }
    .node-card { background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; cursor:pointer; transition:border-color .15s }
    .node-card:hover { border-color:var(--accent) }
    .node-card.selected { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) }
    .node-card .nc-head { display:flex; justify-content:space-between; align-items:center; gap:8px }
    .node-card .nc-name { font-weight:700; font-size:15px }
    .node-card .nc-host { color:var(--muted); font-size:11px }
    .status-dot { display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:6px; vertical-align:middle }
    .status-dot.online { background:var(--green) } .status-dot.degraded { background:var(--amber) } .status-dot.offline { background:var(--red) }
    .badge { display:inline-block; border-radius:999px; padding:2px 9px; font-size:11px; border:1px solid var(--line); white-space:nowrap }
    .badge.healthy { color:var(--green); border-color:var(--green) }
    .badge.busy { color:var(--amber); border-color:var(--amber) }
    .badge.overloaded { color:var(--red); border-color:var(--red) }
    .badge.unknown { color:var(--muted) }
    .badge.draining { color:var(--amber); border-color:var(--amber) }
    .nc-metrics { display:grid; grid-template-columns:1fr 1fr; gap:6px 12px; margin-top:10px; font-size:12px }
    .metric-row { display:flex; justify-content:space-between; color:var(--muted) }
    .metric-row b { color:var(--text); font-weight:600 }
    .meter { height:5px; border-radius:3px; background:#0f1730; overflow:hidden; margin-top:2px }
    .meter > div { height:100%; background:var(--accent) }
    .meter.warn > div { background:var(--amber) } .meter.danger > div { background:var(--red) }
    .nc-foot { margin-top:10px; display:flex; justify-content:space-between; align-items:center; font-size:11px; color:var(--muted) }
    .empty { text-align:center; color:var(--muted); padding:48px 8px }
    #detail { margin-top:20px; background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px 18px }
    #detail[hidden] { display:none }
    #detail .d-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; flex-wrap:wrap }
    #detail h2 { margin:0 0 2px; font-size:16px }
    #detail .d-actions { display:flex; gap:8px; flex-wrap:wrap }
    #detail .d-actions button { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:6px 12px; cursor:pointer; font:inherit; font-size:12px }
    #detail .d-actions button:hover { background:#233252 }
    #detail .d-actions button.danger { border-color:var(--red); color:#ff9f9f }
    #detail .d-actions button:disabled { opacity:.5; cursor:not-allowed }
    #detail table { width:100%; border-collapse:collapse; font-size:12px; margin-top:14px }
    #detail thead th { text-align:left; color:var(--muted); font-weight:600; padding:6px 8px; border-bottom:1px solid var(--line) }
    #detail tbody td { padding:6px 8px; border-bottom:1px solid var(--line) }
    #detail .d-empty { color:var(--muted); font-size:12px; padding:10px 0 }
    #detail .d-msg { font-size:12px; margin-top:8px; min-height:14px }
    #detail .d-msg.error { color:var(--red) } #detail .d-msg.ok { color:var(--green) }
    #detail .d-sessions-error { color:var(--red); font-size:12px; margin-top:10px }
    /* -- Connect Node panel (LAN discovery + SSH/Cloudflare/agent-token
       connect, task's own "bổ sung LAN discovery + Cloudflare SSH")
       -- reuses every existing token/class above (no new font, no new
       color scale), just new layout for this one panel. */
    .cx-tabs { display:flex; gap:6px; flex-wrap:wrap; margin-top:12px }
    .cx-tab { background:#0f1730; border:1px solid var(--line); border-radius:999px; color:var(--muted); padding:6px 14px; cursor:pointer; font:inherit; font-size:12px }
    .cx-tab.active { color:var(--text); border-color:var(--accent); background:#16223d }
    .cx-pane { margin-top:14px } .cx-pane[hidden] { display:none }
    .cx-field { display:flex; flex-direction:column; gap:4px; font-size:12px; color:var(--muted) }
    .cx-field input[type=text], .cx-field input[type=password], .cx-field input[type=number], .cx-field select, .cx-field textarea {
      padding:7px 9px; border-radius:6px; border:1px solid var(--line); background:#0f1730; color:var(--text); font:inherit; font-size:12px;
    }
    .cx-field textarea { min-height:70px; resize:vertical }
    .cx-row { display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end }
    .cx-radio { display:flex; gap:14px; font-size:12px; color:var(--muted) }
    .cx-radio label { display:flex; align-items:center; gap:5px; cursor:pointer }
    .cx-status { display:inline-block; border-radius:999px; padding:2px 9px; font-size:11px; border:1px solid var(--line) }
    .cx-status.ok, .cx-status.already_connected, .cx-status.connectable { color:var(--green); border-color:var(--green) }
    .cx-status.needs_setup { color:var(--amber); border-color:var(--amber) }
    .cx-status.unknown, .cx-status.host_key_new { color:var(--muted) }
    .cx-status.err, .cx-status.host_key_mismatch, .cx-status.auth_fail, .cx-status.unreachable { color:var(--red); border-color:var(--red) }
    #cxScanTable { width:100%; border-collapse:collapse; font-size:12px; margin-top:10px }
    #cxScanTable thead th { text-align:left; color:var(--muted); font-weight:600; padding:6px 8px; border-bottom:1px solid var(--line) }
    #cxScanTable tbody td { padding:6px 8px; border-bottom:1px solid var(--line); vertical-align:top }
    #cxScanTable button { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:4px 10px; cursor:pointer; font:inherit; font-size:11px }
    #cxScanTable button:hover { background:#233252 }
    @media (max-width:760px) {
      header { padding:12px 14px } main { padding:14px } #cards { grid-template-columns:1fr }
    }
  </style>
</head>
<body>
  <header>
    <div><h1>Quản lý node</h1><div class="muted">Trạng thái, tài nguyên và session theo từng node</div></div>
    <div style="display:flex;align-items:center;gap:10px">
      <button class="icon-btn" id="connectNodeBtn" type="button">+ Connect Node</button>
      <button class="icon-btn" id="addNodeBtn" type="button">+ Thêm node</button>
      <button class="icon-btn" id="refreshBtn" type="button">⟳ Refresh</button>
      <a class="back" href="/dashboard">← Terminal</a>
      <span class="badge unknown" id="endpointsBadge" title="Controller endpoints" hidden></span>
      <span class="live" id="liveBadge">● LIVE</span>
    </div>
  </header>
  <main>
    <div id="onboardPanel" hidden style="background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
        <strong>Thêm node mới</strong>
        <button class="icon-btn" id="onboardCloseBtn" type="button">✕</button>
      </div>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:12px">
        <label style="font-size:12px;color:var(--muted)">Node ID<br><input id="obNodeId" type="text" placeholder="m910" style="margin-top:4px;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:#0f1730;color:var(--text);font:inherit"></label>
        <label style="font-size:12px;color:var(--muted)">Hostname<br><input id="obHostname" type="text" placeholder="m910.local" style="margin-top:4px;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:#0f1730;color:var(--text);font:inherit"></label>
        <label style="font-size:12px;color:var(--muted)">Endpoint<br><input id="obEndpoint" type="text" placeholder="http://192.168.1.50:8790" style="margin-top:4px;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:#0f1730;color:var(--text);font:inherit;min-width:220px"></label>
        <label style="font-size:12px;color:var(--muted)">Platform<br>
          <select id="obPlatform" style="margin-top:4px;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:#0f1730;color:var(--text);font:inherit">
            <option value="windows">Windows</option><option value="linux">Linux</option>
          </select>
        </label>
        <button class="icon-btn" id="obGenerateBtn" type="button" style="align-self:flex-end">Generate</button>
      </div>
      <div id="obResult" style="margin-top:12px"></div>
    </div>
    <div id="connectPanel" hidden style="background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
        <strong>Connect Node</strong>
        <button class="icon-btn" id="connectCloseBtn" type="button">✕</button>
      </div>
      <div class="cx-tabs" role="tablist">
        <button class="cx-tab active" data-cx-pane="cxScan" type="button" role="tab">Scan LAN</button>
        <button class="cx-tab" data-cx-pane="cxSsh" type="button" role="tab">Add Remote SSH</button>
        <button class="cx-tab" data-cx-pane="cxCloudflare" type="button" role="tab">Add via Cloudflare Tunnel</button>
        <button class="cx-tab" data-cx-pane="cxToken" type="button" role="tab">Add by Agent Token</button>
      </div>

      <!-- Scan LAN -->
      <div class="cx-pane" id="cxScan">
        <div class="cx-row">
          <button class="icon-btn" id="cxScanBtn" type="button">🔍 Scan LAN</button>
          <button class="icon-btn" id="cxScanCancelBtn" type="button" hidden>Cancel scan</button>
          <span class="muted" id="cxScanState" style="font-size:12px"></span>
        </div>
        <div class="muted" style="font-size:11px;margin-top:8px">
          Chỉ quét các subnet private/link-local của chính NIC controller này (không quét internet); mỗi lần quét bị
          rate-limit và giới hạn concurrency/timeout -- xem docs/multi-node.md.
        </div>
        <div style="overflow-x:auto">
          <table id="cxScanTable" hidden>
            <thead><tr><th>IP</th><th>Hostname</th><th>MAC</th><th>OS (khả năng)</th><th>Ports</th><th>Trạng thái</th><th></th></tr></thead>
            <tbody id="cxScanBody"></tbody>
          </table>
        </div>
        <div class="d-empty" id="cxScanEmpty">Chưa quét lần nào.</div>
      </div>

      <!-- Add Remote SSH (direct LAN/IP) -->
      <div class="cx-pane" id="cxSsh" hidden>
        <input type="hidden" id="cxSshTransport" value="lan_ssh">
        <div class="cx-row">
          <label class="cx-field">Host/IP<input type="text" id="cxSshHost" placeholder="192.168.1.50"></label>
          <label class="cx-field" style="width:90px">Port<input type="number" id="cxSshPort" value="22"></label>
          <label class="cx-field">Username<input type="text" id="cxSshUser" placeholder="pi"></label>
        </div>
        <div class="cx-row" style="margin-top:10px">
          <div class="cx-radio">
            <label><input type="radio" name="cxSshAuth" value="key" checked> SSH key (ưu tiên)</label>
            <label><input type="radio" name="cxSshAuth" value="password"> Password</label>
          </div>
        </div>
        <label class="cx-field" id="cxSshKeyWrap" style="margin-top:8px">Private key (PEM, dán trực tiếp -- không lưu lại)<textarea id="cxSshKey" spellcheck="false" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea></label>
        <label class="cx-field" id="cxSshPassWrap" style="margin-top:8px" hidden>Password (không lưu lại)<input type="password" id="cxSshPass"></label>
        <div class="cx-row" style="margin-top:10px">
          <button class="icon-btn" id="cxSshTestBtn" type="button">Test Connection</button>
          <span id="cxSshTestResult"></span>
        </div>
        <div id="cxSshTrustBox" hidden style="margin-top:8px">
          <div class="muted" style="font-size:11px">Host key mới, chưa được tin cậy:</div>
          <pre id="cxSshFingerprint" style="white-space:pre-wrap;word-break:break-all;background:#0f1730;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:11px;margin:4px 0"></pre>
          <button class="icon-btn" id="cxSshTrustBtn" type="button">✓ Trust &amp; pin host key</button>
        </div>
        <div id="cxSshBootstrapBox" hidden style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
          <div class="cx-radio">
            <label><input type="radio" name="cxSshOs" value="linux" checked> Linux/Unix</label>
            <label><input type="radio" name="cxSshOs" value="windows"> Windows (OpenSSH)</label>
          </div>
          <div class="cx-row" style="margin-top:8px">
            <label class="cx-field">Node ID<input type="text" id="cxSshNodeId" placeholder="pi-01"></label>
            <label class="cx-field">Display name<input type="text" id="cxSshDisplayName" placeholder="(tuỳ chọn)"></label>
          </div>
          <div class="cx-row" style="margin-top:8px">
            <label class="cx-field">Controller URL (để node mới push heartbeat về)<input type="text" id="cxSshControllerUrl" placeholder="http://192.168.1.10:8766"></label>
            <label class="cx-field">Bind host (agent HTTP -- mặc định = host ở trên)<input type="text" id="cxSshBindHost" placeholder="(tuỳ chọn)"></label>
          </div>
          <div class="cx-row" style="margin-top:10px">
            <button class="icon-btn" id="cxSshBootstrapBtn" type="button">🚀 Bootstrap &amp; Connect</button>
          </div>
          <div class="d-msg" id="cxSshBootstrapMsg"></div>
        </div>
      </div>

      <!-- Add via Cloudflare Tunnel -->
      <div class="cx-pane" id="cxCloudflare" hidden>
        <input type="hidden" id="cxCfTransport" value="cloudflare_ssh">
        <div class="muted" style="font-size:11px">Dùng khi máy đích không mở port 22 public -- SSH được tunnel qua Cloudflare Access
          (`cloudflared access ssh --hostname &lt;hostname&gt;`). Cần cloudflared đã cài trên controller này và Access
          application SSH đã cấu hình cho hostname bên dưới.</div>
        <div class="cx-row" style="margin-top:10px">
          <label class="cx-field">Cloudflare Access SSH hostname<input type="text" id="cxCfHost" placeholder="ssh.m910.example.com"></label>
          <label class="cx-field" style="width:90px">Port<input type="number" id="cxCfPort" value="22"></label>
          <label class="cx-field">Username<input type="text" id="cxCfUser" placeholder="pi"></label>
        </div>
        <div class="cx-row" style="margin-top:10px">
          <div class="cx-radio">
            <label><input type="radio" name="cxCfAuth" value="key" checked> SSH key (ưu tiên)</label>
            <label><input type="radio" name="cxCfAuth" value="password"> Password</label>
          </div>
        </div>
        <label class="cx-field" id="cxCfKeyWrap" style="margin-top:8px">Private key (PEM, dán trực tiếp -- không lưu lại)<textarea id="cxCfKey" spellcheck="false"></textarea></label>
        <label class="cx-field" id="cxCfPassWrap" style="margin-top:8px" hidden>Password (không lưu lại)<input type="password" id="cxCfPass"></label>
        <div class="cx-row" style="margin-top:10px">
          <button class="icon-btn" id="cxCfTestBtn" type="button">Test Connection</button>
          <span id="cxCfTestResult"></span>
        </div>
        <div id="cxCfTrustBox" hidden style="margin-top:8px">
          <div class="muted" style="font-size:11px">Host key mới, chưa được tin cậy:</div>
          <pre id="cxCfFingerprint" style="white-space:pre-wrap;word-break:break-all;background:#0f1730;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:11px;margin:4px 0"></pre>
          <button class="icon-btn" id="cxCfTrustBtn" type="button">✓ Trust &amp; pin host key</button>
        </div>
        <div id="cxCfBootstrapBox" hidden style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
          <div class="cx-radio">
            <label><input type="radio" name="cxCfOs" value="linux" checked> Linux/Unix</label>
            <label><input type="radio" name="cxCfOs" value="windows"> Windows (OpenSSH)</label>
          </div>
          <div class="cx-row" style="margin-top:8px">
            <label class="cx-field">Node ID<input type="text" id="cxCfNodeId" placeholder="m910"></label>
            <label class="cx-field">Display name<input type="text" id="cxCfDisplayName" placeholder="(tuỳ chọn)"></label>
          </div>
          <div class="cx-row" style="margin-top:8px">
            <label class="cx-field">Controller URL (để node mới push heartbeat về)<input type="text" id="cxCfControllerUrl" placeholder="http://controller-host:8766"></label>
            <label class="cx-field">Agent endpoint host (bắt buộc cho Linux -- xem ghi chú)<input type="text" id="cxCfAgentEndpointHost" placeholder="LAN/VPN IP, hoặc host:port của Access TCP app khác"></label>
          </div>
          <div class="muted" style="font-size:11px;margin-top:6px">Sau khi cài agent qua SSH-over-tunnel, controller cần một địa chỉ
            HTTP thật để nói chuyện với agent đó -- Cloudflare Access SSH KHÔNG tự động cấp đường đó. Dùng IP LAN/VPN trực tiếp nếu có,
            hoặc một Cloudflare Access TCP application riêng bạn đã cấu hình cho port agent (tự thiết lập trên Cloudflare Zero Trust,
            tính năng này không tự tạo hộ).</div>
          <div class="cx-row" style="margin-top:10px">
            <button class="icon-btn" id="cxCfBootstrapBtn" type="button">🚀 Bootstrap &amp; Connect</button>
          </div>
          <div class="d-msg" id="cxCfBootstrapMsg"></div>
        </div>
      </div>

      <!-- Add by Agent Token -->
      <div class="cx-pane" id="cxToken" hidden>
        <div class="cx-row">
          <label class="cx-field">Node ID<input type="text" id="cxTokNodeId" placeholder="m910"></label>
          <label class="cx-field">Display name<input type="text" id="cxTokDisplayName" placeholder="(tuỳ chọn)"></label>
        </div>
        <div class="cx-row" style="margin-top:8px">
          <label class="cx-field">Endpoint<input type="text" id="cxTokEndpoint" placeholder="http://192.168.1.50:8790"></label>
          <label class="cx-field">Token<input type="password" id="cxTokToken"></label>
        </div>
        <div class="cx-row" style="margin-top:10px">
          <button class="icon-btn" id="cxTokConnectBtn" type="button">🔗 Connect</button>
        </div>
        <div class="d-msg" id="cxTokMsg"></div>
      </div>

      <div id="cxWindowsBox" hidden style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
        <div class="d-msg ok">Không có cài đặt WinRM/PowerShell-remoting tự động (task's own honesty policy) -- làm theo hướng dẫn thủ công dưới đây.</div>
        <div id="cxWindowsInstructions" style="margin-top:8px;font-size:12px"></div>
      </div>
    </div>
    <div id="cards"><div class="empty">Đang tải...</div></div>
    <div id="detail" hidden></div>
  </main>
  <script>
    let selectedNodeId = null;
    let nodesCache = [];

    function pct(value) { return (value === null || value === undefined) ? '—' : value.toFixed(0) + '%'; }
    function meterClass(value) { if (value === null || value === undefined) return ''; if (value >= 90) return 'danger'; if (value >= 75) return 'warn'; return ''; }
    function fmtBytes(n) {
      if (n === null || n === undefined) return '—';
      const units = ['B','KB','MB','GB','TB']; let i = 0; let v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return v.toFixed(1) + units[i];
    }
    function heartbeatAge(iso) {
      if (!iso) return 'never';
      const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
      if (secs < 60) return Math.round(secs) + 's ago';
      if (secs < 3600) return Math.round(secs / 60) + 'm ago';
      return Math.round(secs / 3600) + 'h ago';
    }

    class AuthRequiredError extends Error {}
    async function api(path, options) {
      const response = await fetch(path, options);
      if (response.status === 401 || response.status === 403) {
        const body = await response.json().catch(() => ({}));
        if (body.error === 'CLOUDFLARE_ACCESS_VERIFICATION_FAILED' || body.error === 'AUTH_REQUIRED') {
          throw new AuthRequiredError(body.error);
        }
      }
      const data = await response.json().catch(() => ({}));
      return { ok: response.ok, status: response.status, data };
    }

    function osIcon(node) { return node.platform === 'windows' ? '🪟' : '🐧'; }
    function capabilityLine(node) {
      const parts = [node.session_backend || '—'];
      if (node.claude_available) parts.push('claude ✓');
      if (node.codex_available) parts.push('codex ✓');
      if (node.wsl_available) parts.push('WSL');
      return parts.join(' · ');
    }

    function nodeCardHtml(node) {
      const capBadge = node.capacity_status || 'unknown';
      const draining = node.draining ? '<span class="badge draining">draining</span>' : '';
      return `
        <div class="node-card${node.id === selectedNodeId ? ' selected' : ''}" data-node-id="${node.id}">
          <div class="nc-head">
            <div><span class="status-dot ${node.status}"></span><span title="${node.platform || 'linux'}">${osIcon(node)}</span> <span class="nc-name">${node.display_name}</span>
              <div class="nc-host">${node.id} · ${node.hostname}</div>
              <div class="nc-host">${capabilityLine(node)}</div></div>
            <div style="display:flex;gap:6px;align-items:center"><span class="badge ${capBadge}">${capBadge}</span>${draining}</div>
          </div>
          <div class="nc-metrics">
            <div>
              <div class="metric-row"><span>CPU</span><b>${pct(node.cpu_percent)}</b></div>
              <div class="meter ${meterClass(node.cpu_percent)}"><div style="width:${node.cpu_percent || 0}%"></div></div>
            </div>
            <div>
              <div class="metric-row"><span>RAM</span><b>${pct(node.ram_percent)}</b></div>
              <div class="meter ${meterClass(node.ram_percent)}"><div style="width:${node.ram_percent || 0}%"></div></div>
            </div>
            <div>
              <div class="metric-row"><span>Swap</span><b>${pct(node.swap_percent)}</b></div>
              <div class="meter ${meterClass(node.swap_percent)}"><div style="width:${node.swap_percent || 0}%"></div></div>
            </div>
            <div>
              <div class="metric-row"><span>Disk</span><b>${pct(node.disk_percent)}</b></div>
              <div class="meter ${meterClass(node.disk_percent)}"><div style="width:${node.disk_percent || 0}%"></div></div>
            </div>
          </div>
          <div class="nc-foot">
            <span>${node.tmux_session_count ?? 0} session${(node.tmux_session_count ?? 0) === 1 ? '' : 's'}</span>
            <span>heartbeat: ${heartbeatAge(node.last_heartbeat_at)}</span>
          </div>
          ${(node.overload_reasons && node.overload_reasons.length) ? `<div class="d-sessions-error" style="margin-top:8px">${node.overload_reasons.join('; ')}</div>` : ''}
        </div>`;
    }

    function renderCards() {
      const container = document.getElementById('cards');
      if (!nodesCache.length) { container.innerHTML = '<div class="empty">Chưa có node nào đăng ký.</div>'; return; }
      container.innerHTML = nodesCache.map(nodeCardHtml).join('');
      container.querySelectorAll('.node-card').forEach((card) => {
        card.addEventListener('click', () => { selectedNodeId = card.dataset.nodeId; renderCards(); loadDetail(selectedNodeId); });
      });
    }

    async function loadDetail(nodeId) {
      const detail = document.getElementById('detail');
      detail.hidden = false;
      detail.innerHTML = '<div class="d-empty">Đang tải chi tiết...</div>';
      const result = await api('/dashboard/api/node?id=' + encodeURIComponent(nodeId));
      if (!result.ok) { detail.innerHTML = `<div class="d-sessions-error">${result.data.error || 'Không tải được'}</div>`; return; }
      const node = result.data.node;
      const sessions = result.data.sessions || [];
      const isLocal = node.endpoint === 'local';
      detail.innerHTML = `
        <div class="d-head">
          <div><h2>${osIcon(node)} ${node.display_name} <span class="muted" style="font-weight:400">(${node.id})</span></h2>
            <div class="muted">${node.hostname} · ${node.endpoint} · ${node.platform || 'linux'}/${node.session_backend || 'tmux'} · agent_version=${node.agent_version || '—'}</div>
            <div class="muted">${capabilityLine(node)}${(node.shell_capabilities && node.shell_capabilities.length) ? ' · shells: ' + node.shell_capabilities.join(', ') : ''}</div></div>
          <div class="d-actions">
            <button id="refreshDetailBtn" type="button">⟳ Refresh</button>
            <button id="testConnBtn" type="button"${isLocal ? ' disabled title="Node local luôn kết nối"' : ''}>Test connection</button>
            <button id="drainBtn" type="button">${node.draining ? 'Resume (nhận session mới)' : 'Drain (ngừng nhận session mới)'}</button>
          </div>
        </div>
        <div class="d-msg" id="detailMsg"></div>
        <table>
          <thead><tr><th>Session</th><th>Node</th><th>Windows</th><th>Đã đính kèm</th></tr></thead>
          <tbody>
            ${sessions.length ? sessions.map((s) => `<tr><td>${s.name}</td><td>${s.node_id || node.id}</td><td>${s.windows ?? '—'}</td><td>${s.attached ? 'có' : 'không'}</td></tr>`).join('')
                              : '<tr><td colspan="4" class="d-empty">' + (result.data.sessions_error ? 'Không lấy được danh sách session: ' + result.data.sessions_error : 'Không có session nào trên node này') + '</td></tr>'}
          </tbody>
        </table>`;
      document.getElementById('refreshDetailBtn').addEventListener('click', () => loadDetail(nodeId));
      document.getElementById('testConnBtn').addEventListener('click', async () => {
        const msg = document.getElementById('detailMsg');
        msg.textContent = 'Đang kiểm tra...'; msg.className = 'd-msg';
        const r = await api('/dashboard/api/node/test-connection', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ node_id: nodeId }),
        });
        if (r.ok && r.data.ok) { msg.textContent = `OK — latency ${r.data.latency_ms?.toFixed(0) ?? '?'}ms`; msg.className = 'd-msg ok'; }
        else { msg.textContent = 'Thất bại: ' + (r.data.detail || r.data.error || 'unknown'); msg.className = 'd-msg error'; }
      });
      document.getElementById('drainBtn').addEventListener('click', async () => {
        const nextDraining = !node.draining;
        const r = await api('/dashboard/api/node/drain', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ node_id: nodeId, draining: nextDraining }),
        });
        const msg = document.getElementById('detailMsg');
        if (r.ok) { msg.textContent = nextDraining ? 'Node đã chuyển sang draining.' : 'Node đã resume.'; msg.className = 'd-msg ok'; loadAll(); loadDetail(nodeId); }
        else { msg.textContent = 'Thất bại: ' + (r.data.error || 'unknown'); msg.className = 'd-msg error'; }
      });
    }

    function renderEndpointsBadge(endpoints) {
      const el = document.getElementById('endpointsBadge');
      if (!endpoints) { el.hidden = true; return; }
      el.hidden = false;
      if (endpoints.lan) {
        el.className = 'badge healthy';
        el.textContent = `LAN: ${endpoints.lan}`;
        el.title = `Loopback: ${endpoints.loopback}\nLAN: ${endpoints.lan} (allowed: ${(endpoints.allowed_cidrs || []).join(', ')})\n${endpoints.firewall_reminder || ''}\nTunnel: ${endpoints.tunnel || ''}`;
      } else {
        el.className = 'badge unknown';
        el.textContent = 'LAN: off';
        el.title = `Loopback: ${endpoints.loopback}\nLAN bind not configured (TERMINAL_MCP_LAN_BIND unset)${endpoints.lan_error ? ' -- ' + endpoints.lan_error : ''}\nTunnel: ${endpoints.tunnel || ''}`;
      }
    }

    async function loadAll() {
      const liveBadgeEl = document.getElementById('liveBadge');
      try {
        const result = await api('/dashboard/api/nodes');
        if (!result.ok) throw new Error(result.data.error || 'failed');
        nodesCache = result.data.nodes || [];
        renderCards();
        renderEndpointsBadge(result.data.controller_endpoints);
        liveBadgeEl.textContent = '● LIVE'; liveBadgeEl.className = 'live';
      } catch (error) {
        if (error instanceof AuthRequiredError) { liveBadgeEl.textContent = '● SIGN-IN REQUIRED'; liveBadgeEl.className = 'live auth-required'; }
        else { liveBadgeEl.textContent = '● OFFLINE'; liveBadgeEl.className = 'live offline'; }
      }
    }

    document.getElementById('refreshBtn').addEventListener('click', () => { loadAll(); if (selectedNodeId) loadDetail(selectedNodeId); });

    document.getElementById('addNodeBtn').addEventListener('click', () => {
      document.getElementById('onboardPanel').hidden = false;
      document.getElementById('obResult').innerHTML = '';
    });
    document.getElementById('onboardCloseBtn').addEventListener('click', () => { document.getElementById('onboardPanel').hidden = true; });
    document.getElementById('obGenerateBtn').addEventListener('click', async () => {
      const nodeId = document.getElementById('obNodeId').value.trim();
      const hostname = document.getElementById('obHostname').value.trim();
      const endpoint = document.getElementById('obEndpoint').value.trim();
      const platform = document.getElementById('obPlatform').value;
      const resultEl = document.getElementById('obResult');
      if (!nodeId || !hostname || !endpoint) { resultEl.innerHTML = '<div class="d-sessions-error">Điền đủ Node ID, Hostname, Endpoint.</div>'; return; }
      resultEl.innerHTML = '<div class="d-empty">Đang tạo...</div>';
      const r = await api('/dashboard/api/node/generate-onboarding', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ node_id: nodeId, hostname, endpoint, platform }),
      });
      if (!r.ok) { resultEl.innerHTML = `<div class="d-sessions-error">${r.data.detail || r.data.error || 'Lỗi'}</div>`; return; }
      const pre = (label, text) => `<div style="margin-top:8px"><div class="muted" style="font-size:11px">${label}</div><pre style="white-space:pre-wrap;word-break:break-all;background:#0f1730;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:11px;user-select:all">${text}</pre></div>`;
      resultEl.innerHTML = `
        <div class="d-msg ok">Token được tạo MỘT LẦN duy nhất -- lưu lại ngay, không hiển thị lại.</div>
        ${pre('1) Chạy trên node mới:', r.data.install_command)}
        ${pre('2) Thêm vào config.yaml của controller:', r.data.config_yaml_block)}
        ${pre('3) Export biến môi trường nơi terminal-mcp-http.service đọc env (rồi safe-restart):', r.data.env_var + '=' + r.data.token)}
        <div class="muted" style="font-size:11px;margin-top:6px">4) Xác minh: terminal-mcp-doctor nodes -- ${nodeId} phải chuyển status=online trong vài giây sau khi node-agent kết nối.</div>`;
    });

    // ======================================================================
    // Connect Node: Scan LAN / Add Remote SSH / Add via Cloudflare Tunnel /
    // Add by Agent Token. Discovered-device fields (hostname via reverse
    // DNS, MAC, agent_info) come from OTHER machines on the network, not
    // this operator -- unlike the rest of this admin-authored-data page
    // (which uses innerHTML template strings throughout), every one of
    // those fields is rendered via createElement/textContent ONLY, never
    // innerHTML, so a hostile device's own PTR record or /v1/health
    // response body can never be interpreted as markup here.
    // ======================================================================
    (function () {
      const panel = document.getElementById('connectPanel');
      document.getElementById('connectNodeBtn').addEventListener('click', () => { panel.hidden = false; });
      document.getElementById('connectCloseBtn').addEventListener('click', () => { panel.hidden = true; });

      for (const tab of document.querySelectorAll('.cx-tab')) {
        tab.addEventListener('click', () => {
          for (const t of document.querySelectorAll('.cx-tab')) t.classList.remove('active');
          tab.classList.add('active');
          for (const pane of document.querySelectorAll('.cx-pane')) pane.hidden = true;
          document.getElementById(tab.dataset.cxPane).hidden = false;
        });
      }

      function wireAuthToggle(radioName, keyWrapId, passWrapId) {
        for (const radio of document.querySelectorAll(`input[name="${radioName}"]`)) {
          radio.addEventListener('change', () => {
            const isKey = document.querySelector(`input[name="${radioName}"]:checked`).value === 'key';
            document.getElementById(keyWrapId).hidden = !isKey;
            document.getElementById(passWrapId).hidden = isKey;
          });
        }
      }
      wireAuthToggle('cxSshAuth', 'cxSshKeyWrap', 'cxSshPassWrap');
      wireAuthToggle('cxCfAuth', 'cxCfKeyWrap', 'cxCfPassWrap');

      function credentialBody(prefix) {
        const authRadio = document.querySelector(`input[name="cx${prefix}Auth"]:checked`);
        const cred = {};
        if (!authRadio) return null;
        if (authRadio.value === 'key') {
          const key = document.getElementById(`cx${prefix}Key`).value;
          if (!key.trim()) return null;
          cred.private_key_pem = key;
        } else {
          const pass = document.getElementById(`cx${prefix}Pass`).value;
          if (!pass) return null;
          cred.password = pass;
        }
        return cred;
      }

      function showWindowsGuidance(nodeId, hostname, controllerUrl) {
        if (!nodeId || !hostname || !controllerUrl) {
          alert('Cần Node ID, Host/hostname, và Controller URL để tạo hướng dẫn Windows.');
          return;
        }
        api('/dashboard/api/nodes/connect/windows/bootstrap', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ node_id: nodeId, hostname, controller_url: controllerUrl }),
        }).then(r => {
          const box = document.getElementById('cxWindowsBox');
          const list = document.getElementById('cxWindowsInstructions');
          list.replaceChildren();
          if (!r.ok) {
            const err = document.createElement('div'); err.className = 'd-msg error';
            err.textContent = r.data.detail || r.data.error || 'Lỗi'; list.append(err);
            box.hidden = false; return;
          }
          for (const line of (r.data.instructions || [])) {
            const p = document.createElement('div'); p.textContent = line; list.append(p);
          }
          const pre = document.createElement('pre');
          pre.style.cssText = 'white-space:pre-wrap;word-break:break-all;background:#0f1730;border:1px solid var(--line);border-radius:6px;padding:8px;font-size:11px;margin-top:8px;user-select:all';
          pre.textContent = r.data.install_command || '';
          list.append(pre);
          const tokenNote = document.createElement('div'); tokenNote.className = 'muted';
          tokenNote.style.fontSize = '11px'; tokenNote.style.marginTop = '6px';
          tokenNote.textContent = 'Token được tạo MỘT LẦN duy nhất, đã nằm trong lệnh trên -- không hiển thị lại.';
          list.append(tokenNote);
          box.hidden = false;
        });
      }

      // -- Scan LAN ------------------------------------------------------
      let scanPollTimer = null;
      const scanBtn = document.getElementById('cxScanBtn');
      const scanCancelBtn = document.getElementById('cxScanCancelBtn');
      const scanStateEl = document.getElementById('cxScanState');
      const scanTable = document.getElementById('cxScanTable');
      const scanBody = document.getElementById('cxScanBody');
      const scanEmpty = document.getElementById('cxScanEmpty');

      function statusLabel(status) {
        return { already_connected: 'Đã kết nối', connectable: 'Có thể kết nối (agent)',
                needs_setup: 'Cần cài đặt', unknown: 'Không rõ' }[status] || status;
      }

      function renderScanDevices(devices) {
        scanBody.replaceChildren();
        if (!devices.length) { scanTable.hidden = true; scanEmpty.hidden = false; return; }
        scanTable.hidden = false; scanEmpty.hidden = true;
        for (const device of devices) {
          const tr = document.createElement('tr');
          const cells = [device.ip, device.hostname || '—', device.mac || '—',
                        device.os_guess ? (device.os_guess === 'windows' ? '🪟 Windows (khả năng)' : '🐧 Linux/Unix (khả năng)') : '—',
                        (device.open_ports || []).join(', ') || '—'];
          for (const value of cells) {
            const td = document.createElement('td'); td.textContent = value; tr.append(td);
          }
          const statusTd = document.createElement('td');
          const statusSpan = document.createElement('span');
          statusSpan.className = 'cx-status ' + device.status; statusSpan.textContent = statusLabel(device.status);
          statusTd.append(statusSpan); tr.append(statusTd);
          const actionTd = document.createElement('td');
          if (device.status !== 'already_connected') {
            const connectBtn = document.createElement('button'); connectBtn.type = 'button'; connectBtn.textContent = 'Connect';
            connectBtn.addEventListener('click', () => prefillFromScan(device));
            actionTd.append(connectBtn);
          }
          tr.append(actionTd);
          scanBody.append(tr);
        }
      }

      function prefillFromScan(device) {
        if (device.agent_reachable) {
          for (const t of document.querySelectorAll('.cx-tab')) t.classList.remove('active');
          document.querySelector('[data-cx-pane="cxToken"]').classList.add('active');
          for (const pane of document.querySelectorAll('.cx-pane')) pane.hidden = true;
          document.getElementById('cxToken').hidden = false;
          document.getElementById('cxTokEndpoint').value = `http://${device.ip}:8790`;
          if (device.agent_info && device.agent_info.node_id) document.getElementById('cxTokNodeId').value = device.agent_info.node_id;
          return;
        }
        for (const t of document.querySelectorAll('.cx-tab')) t.classList.remove('active');
        document.querySelector('[data-cx-pane="cxSsh"]').classList.add('active');
        for (const pane of document.querySelectorAll('.cx-pane')) pane.hidden = true;
        document.getElementById('cxSsh').hidden = false;
        document.getElementById('cxSshHost').value = device.ip;
        if (device.os_guess === 'windows') {
          const radio = document.querySelector('input[name="cxSshOs"][value="windows"]');
          if (radio) radio.checked = true;
        }
      }

      function pollScan() {
        api('/dashboard/api/nodes/discovery/status').then(r => {
          if (!r.ok) return;
          const result = r.data;
          if (result.state === 'running') {
            scanStateEl.textContent = `Đang quét ${(result.subnets || []).join(', ') || '...'}`;
            scanCancelBtn.hidden = false; scanBtn.disabled = true;
            scanPollTimer = setTimeout(pollScan, 1200);
          } else {
            scanBtn.disabled = false; scanCancelBtn.hidden = true;
            const count = (result.devices || []).length;
            scanStateEl.textContent = result.state === 'never_run' ? ''
              : `${result.state === 'done' ? 'Xong' : result.state}${result.truncated ? ' (bị cắt bớt do giới hạn quét)' : ''} -- ${count} thiết bị`;
            renderScanDevices(result.devices || []);
          }
        });
      }
      scanBtn.addEventListener('click', () => {
        api('/dashboard/api/nodes/discovery/scan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(() => { clearTimeout(scanPollTimer); pollScan(); });
      });
      scanCancelBtn.addEventListener('click', () => {
        api('/dashboard/api/nodes/discovery/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
          .then(() => pollScan());
      });
      pollScan();

      // -- Add Remote SSH / Add via Cloudflare Tunnel (shared logic,
      // parameterized by field-id prefix "Ssh"/"Cf") --------------------
      function wireSshFlow(prefix, transportType) {
        const hostEl = document.getElementById(`cx${prefix}Host`);
        const portEl = document.getElementById(`cx${prefix}Port`);
        const userEl = document.getElementById(`cx${prefix}User`);
        const testBtn = document.getElementById(`cx${prefix}TestBtn`);
        const resultEl = document.getElementById(`cx${prefix}TestResult`);
        const trustBox = document.getElementById(`cx${prefix}TrustBox`);
        const fingerprintEl = document.getElementById(`cx${prefix}Fingerprint`);
        const trustBtn = document.getElementById(`cx${prefix}TrustBtn`);
        const bootstrapBox = document.getElementById(`cx${prefix}BootstrapBox`);
        const bootstrapBtn = document.getElementById(`cx${prefix}BootstrapBtn`);
        const bootstrapMsg = document.getElementById(`cx${prefix}BootstrapMsg`);

        function targetBody() {
          return { transport_type: transportType, host: hostEl.value.trim(),
                  port: portEl.value ? Number(portEl.value) : undefined, username: userEl.value.trim() };
        }

        function runTest() {
          const body = targetBody();
          const cred = credentialBody(prefix);
          if (cred) body.credential = cred;
          resultEl.textContent = 'Đang kiểm tra...';
          trustBox.hidden = true; bootstrapBox.hidden = true;
          return api(`/dashboard/api/nodes/connect/ssh/test`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
          }).then(r => {
            const stage = r.data.stage || (r.ok ? 'ok' : 'unreachable');
            const span = document.createElement('span'); span.className = 'cx-status ' + stage;
            span.textContent = stage + (r.data.detail ? `: ${r.data.detail}` : '');
            resultEl.replaceChildren(span);
            if (stage === 'host_key_new') {
              fingerprintEl.textContent = r.data.fingerprint || '';
              trustBox.hidden = false;
            } else if (stage === 'ok') {
              bootstrapBox.hidden = false;
            }
            return r;
          });
        }
        testBtn.addEventListener('click', runTest);
        trustBtn.addEventListener('click', () => {
          api('/dashboard/api/nodes/connect/ssh/trust-hostkey', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(targetBody()),
          }).then(r => {
            if (r.ok) { trustBox.hidden = true; runTest(); }
            else alert(r.data.detail || r.data.error || 'Lỗi trust host key');
          });
        });
        bootstrapBtn.addEventListener('click', () => {
          const osChoice = document.querySelector(`input[name="cx${prefix}Os"]:checked`).value;
          const nodeId = document.getElementById(`cx${prefix}NodeId`).value.trim();
          const displayName = document.getElementById(`cx${prefix}DisplayName`).value.trim();
          const controllerUrl = document.getElementById(`cx${prefix}ControllerUrl`).value.trim();
          if (osChoice === 'windows') {
            showWindowsGuidance(nodeId, hostEl.value.trim(), controllerUrl);
            return;
          }
          const cred = credentialBody(prefix);
          if (!nodeId || !controllerUrl || !cred) {
            bootstrapMsg.className = 'd-msg error'; bootstrapMsg.textContent = 'Cần Node ID, Controller URL, và credential (key/password).';
            return;
          }
          const body = Object.assign(targetBody(), { credential: cred, node_id: nodeId, display_name: displayName, controller_url: controllerUrl });
          if (prefix === 'Ssh') {
            const bindHost = document.getElementById('cxSshBindHost').value.trim();
            if (bindHost) body.bind_host = bindHost;
          } else {
            body.agent_endpoint_host = document.getElementById('cxCfAgentEndpointHost').value.trim();
          }
          bootstrapMsg.className = 'd-msg'; bootstrapMsg.textContent = 'Đang bootstrap (có thể mất tới 1 phút)...';
          bootstrapBtn.disabled = true;
          api('/dashboard/api/nodes/connect/ssh/bootstrap', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
          }).then(r => {
            bootstrapBtn.disabled = false;
            if (r.ok) {
              bootstrapMsg.className = 'd-msg ok'; bootstrapMsg.textContent = `Đã kết nối node ${r.data.node_id} (${r.data.endpoint}).`;
              loadAll();
            } else {
              bootstrapMsg.className = 'd-msg error';
              bootstrapMsg.textContent = (r.data.detail || r.data.error || 'Lỗi') +
                (r.data.stderr ? ` -- stderr: ${r.data.stderr.slice(0, 300)}` : '');
            }
          });
        });
      }
      wireSshFlow('Ssh', 'lan_ssh');
      wireSshFlow('Cf', 'cloudflare_ssh');

      // -- Add by Agent Token ---------------------------------------------
      document.getElementById('cxTokConnectBtn').addEventListener('click', () => {
        const nodeId = document.getElementById('cxTokNodeId').value.trim();
        const displayName = document.getElementById('cxTokDisplayName').value.trim();
        const endpoint = document.getElementById('cxTokEndpoint').value.trim();
        const token = document.getElementById('cxTokToken').value;
        const msg = document.getElementById('cxTokMsg');
        if (!nodeId || !endpoint || !token) { msg.className = 'd-msg error'; msg.textContent = 'Cần Node ID, Endpoint, Token.'; return; }
        msg.className = 'd-msg'; msg.textContent = 'Đang kết nối...';
        api('/dashboard/api/nodes/connect/agent-token', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ node_id: nodeId, display_name: displayName, endpoint, token }),
        }).then(r => {
          if (r.ok) { msg.className = 'd-msg ok'; msg.textContent = `Đã kết nối node ${r.data.node_id}.`; loadAll(); }
          else { msg.className = 'd-msg error'; msg.textContent = r.data.detail || r.data.error || 'Lỗi'; }
        });
      });
    })();

    loadAll(); setInterval(loadAll, 8000);
  </script>
</body>
</html>"""


# Web terminal (xterm.js over a WebSocket -- webterm.py). A standalone
# page, deliberately NOT folded into DASHBOARD_HTML's own tab UI: this is
# the one screen in the whole dashboard that genuinely needs a dedicated,
# fullscreen, mobile-first layout (fit-to-viewport, on-screen-keyboard-
# aware resize, no other dashboard chrome competing for space) rather
# than sharing DASHBOARD_HTML's multi-tab shell -- see the task's own
# "không redesign lớn dashboard" constraint: this adds one new page and
# one new row-action button (below, in SESSIONS_ADMIN_HTML/
# APP_SESSIONS_ADMIN_HTML), nothing about any existing screen changes.
# `?session=NAME` (required) and `&takeover=1` (optional) are read
# client-side from location.search, same query-param convention
# session_detail's own /dashboard/api/session?name= already uses --
# never server-templated, so this exact same static HTML document is
# reused for every session, and webauth_dashboard.py's APP_WEBTERM_HTML
# is this string with the same handful of literal-substring rewrites
# every other page here already gets (see that module).
WEBTERM_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover">
  <title>Terminal</title>
  <link rel="stylesheet" href="/dashboard/assets/xterm.css">
  <style>
    :root {
      color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --green:#43d17c; --amber:#ffc857; --err:#ff6b6b; --accent:#5b8cff; --mono: ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace;
      /* Same Windows Terminal "Campbell" palette as the dashboard's own
         terminal-surface tokens (task item 6, single preset shared across
         both real screens that render terminal output) -- this page is the
         REAL xterm.js terminal, so these are also what the Terminal()
         theme object below reads at startup, not just CSS decoration. */
      --term-bg:#0c0c0c; --term-fg:#cccccc; --term-cursor:#ffffff; --term-selection:rgba(255,255,255,.28);
      --ansi-0:#0c0c0c; --ansi-1:#c50f1f; --ansi-2:#13a10e; --ansi-3:#c19c00; --ansi-4:#0037da; --ansi-5:#881798; --ansi-6:#3a96dd; --ansi-7:#cccccc;
      --ansi-8:#767676; --ansi-9:#e74856; --ansi-10:#16c60c; --ansi-11:#f9f1a5; --ansi-12:#3b78ff; --ansi-13:#b4009e; --ansi-14:#61d6d6; --ansi-15:#f2f2f2;
    }
    * { box-sizing:border-box }
    html, body { height:100vh; height:100dvh; overflow:hidden }
    body { margin:0; font:13px/1.4 var(--mono); background:var(--bg); color:var(--text); display:flex; flex-direction:column }
    header { flex:0 0 auto; display:flex; align-items:center; gap:8px; padding:8px 10px; border-bottom:1px solid var(--line); flex-wrap:wrap; padding-top:max(8px, env(safe-area-inset-top)) }
    a.back { color:var(--muted); text-decoration:none; font-size:12px; border:1px solid var(--line); border-radius:999px; padding:4px 9px; flex:0 0 auto }
    a.back:hover { color:var(--text); border-color:var(--muted) }
    .sess-name { font-weight:700; font-size:13px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:38vw }
    .pill { display:inline-flex; align-items:center; gap:4px; border-radius:999px; padding:2px 8px; font-size:11px; border:1px solid var(--line); color:var(--muted); white-space:nowrap }
    .pill.on { color:var(--green); border-color:var(--green) }
    .pill.warn { color:var(--amber); border-color:var(--amber) }
    .pill.err { color:var(--err); border-color:var(--err) }
    .spacer { flex:1 }
    .hdr-btn { background:#19243b; border:1px solid var(--line); border-radius:6px; color:var(--text); padding:5px 9px; cursor:pointer; font:inherit; font-size:12px; flex:0 0 auto }
    .hdr-btn:hover { background:#233252 }
    .hdr-btn:disabled { opacity:.4; cursor:not-allowed }
    #termWrap { flex:1; min-height:0; position:relative; background:var(--term-bg); padding:4px 6px }
    #termHost { width:100%; height:100% }
    .xterm { padding:2px }
    #banner { flex:0 0 auto; display:none; padding:7px 12px; font-size:12px; text-align:center }
    #banner.show { display:block }
    #banner.reconnecting { background:rgba(255,200,87,.15); color:var(--amber) }
    #banner.error { background:rgba(255,107,107,.15); color:var(--err) }
    #banner button { margin-left:10px; background:transparent; border:1px solid currentColor; border-radius:6px; color:inherit; padding:2px 8px; cursor:pointer; font:inherit; font-size:11px }
    #fontRow { display:flex; gap:2px }
  </style>
</head>
<body>
  <header>
    <a class="back" href="/dashboard/sessions">← Sessions</a>
    <span class="sess-name" id="sessName"></span>
    <span class="pill" id="connPill">● đang kết nối…</span>
    <span class="pill" id="modePill" style="display:none"></span>
    <span class="pill" id="attachPill" style="display:none"></span>
    <div class="spacer"></div>
    <div id="fontRow">
      <button class="hdr-btn" id="fontMinusBtn" type="button" title="Chữ nhỏ hơn">A-</button>
      <button class="hdr-btn" id="fontPlusBtn" type="button" title="Chữ lớn hơn">A+</button>
    </div>
    <button class="hdr-btn" id="takeoverBtn" type="button" style="display:none" title="Ngắt client khác đang gắn vào session này và chiếm quyền">⚡ Chiếm quyền</button>
  </header>
  <div id="banner"></div>
  <div id="termWrap"><div id="termHost"></div></div>
  <script src="/dashboard/assets/xterm.js"></script>
  <script src="/dashboard/assets/xterm-addon-fit.js"></script>
  <script>
    const params = new URLSearchParams(location.search);
    const sessionName = params.get('session') || '';
    let takeover = params.get('takeover') === '1';
    document.getElementById('sessName').textContent = sessionName || '(thiếu tên session)';

    const connPillEl = document.getElementById('connPill');
    const modePillEl = document.getElementById('modePill');
    const attachPillEl = document.getElementById('attachPill');
    const bannerEl = document.getElementById('banner');
    const takeoverBtnEl = document.getElementById('takeoverBtn');
    const WS_PATH = '/dashboard/ws/terminal';

    const FONT_KEY = 'terminal-mcp:webterm-font-size';
    function loadFontSize() {
      const raw = parseInt(localStorage.getItem(FONT_KEY) || '14', 10);
      return Number.isFinite(raw) ? Math.min(24, Math.max(9, raw)) : 14;
    }
    let fontSize = loadFontSize();

    // Single source of truth (task item 6): the same --term-*/--ansi-*
    // custom properties the :root palette above defines (Windows Terminal
    // "Campbell" by default) drive xterm.js's own theme too, read once at
    // startup rather than a second hardcoded copy.
    const rootStyle = getComputedStyle(document.documentElement);
    function themeVar(name, fallback) { return (rootStyle.getPropertyValue(name) || '').trim() || fallback; }
    const xtermTheme = {
      background: themeVar('--term-bg', '#0c0c0c'), foreground: themeVar('--term-fg', '#cccccc'),
      cursor: themeVar('--term-cursor', '#ffffff'), cursorAccent: themeVar('--term-bg', '#0c0c0c'),
      selectionBackground: themeVar('--term-selection', 'rgba(255,255,255,.28)'),
      black: themeVar('--ansi-0', '#0c0c0c'), red: themeVar('--ansi-1', '#c50f1f'),
      green: themeVar('--ansi-2', '#13a10e'), yellow: themeVar('--ansi-3', '#c19c00'),
      blue: themeVar('--ansi-4', '#0037da'), magenta: themeVar('--ansi-5', '#881798'),
      cyan: themeVar('--ansi-6', '#3a96dd'), white: themeVar('--ansi-7', '#cccccc'),
      brightBlack: themeVar('--ansi-8', '#767676'), brightRed: themeVar('--ansi-9', '#e74856'),
      brightGreen: themeVar('--ansi-10', '#16c60c'), brightYellow: themeVar('--ansi-11', '#f9f1a5'),
      brightBlue: themeVar('--ansi-12', '#3b78ff'), brightMagenta: themeVar('--ansi-13', '#b4009e'),
      brightCyan: themeVar('--ansi-14', '#61d6d6'), brightWhite: themeVar('--ansi-15', '#f2f2f2'),
    };
    const term = new Terminal({
      cursorBlink: true, fontSize, fontFamily: "ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace",
      scrollback: 5000, theme: xtermTheme, allowProposedApi: true,
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(document.getElementById('termHost'));
    fitAddon.fit();

    function applyFontSize(next) {
      fontSize = Math.min(24, Math.max(9, next));
      term.options.fontSize = fontSize;
      try { localStorage.setItem(FONT_KEY, String(fontSize)); } catch (error) { /* private mode -- non-essential */ }
      fitAddon.fit();
      sendResize();
    }
    document.getElementById('fontMinusBtn').onclick = () => applyFontSize(fontSize - 1);
    document.getElementById('fontPlusBtn').onclick = () => applyFontSize(fontSize + 1);

    if (!sessionName) {
      connPillEl.textContent = '● lỗi'; connPillEl.className = 'pill err';
      bannerEl.textContent = 'Thiếu tham số ?session=<tên session>.';
      bannerEl.className = 'show error';
      term.write('\\r\\n\\x1b[31mThiếu tham số session.\\x1b[0m\\r\\n');
      throw new Error('missing session param');
    }

    let ws = null;
    let closedByUser = false;
    let reconnectAttempt = 0;
    let reconnectTimer = null;
    let readonlyMode = false;
    const textEncoder = new TextEncoder();

    function wsUrl() {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const q = new URLSearchParams({ session: sessionName });
      if (takeover) q.set('takeover', '1');
      return `${proto}//${location.host}${WS_PATH}?${q.toString()}`;
    }

    function sendResize() {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
      }
    }

    function setBanner(text, kind) {
      if (!text) { bannerEl.className = ''; bannerEl.textContent = ''; return; }
      bannerEl.textContent = text; bannerEl.className = `show ${kind || ''}`;
    }

    function scheduleReconnect() {
      if (closedByUser) return;
      reconnectAttempt += 1;
      const delayMs = Math.min(10000, 1000 * Math.pow(2, Math.min(4, reconnectAttempt - 1)));
      connPillEl.textContent = '● mất kết nối'; connPillEl.className = 'pill err';
      setBanner(`Mất kết nối -- thử lại sau ${Math.round(delayMs / 1000)}s… (session tmux vẫn đang chạy)`, 'reconnecting');
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connect, delayMs);
    }

    function connect() {
      clearTimeout(reconnectTimer);
      connPillEl.textContent = '● đang kết nối…'; connPillEl.className = 'pill';
      ws = new WebSocket(wsUrl());
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        reconnectAttempt = 0;
        setBanner('', '');
        sendResize();
      };

      ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
          let payload = null;
          try { payload = JSON.parse(event.data); } catch (error) { return; }
          if (!payload || typeof payload !== 'object') return;
          if (payload.type === 'ready') {
            readonlyMode = !!payload.readonly;
            connPillEl.textContent = '● LIVE'; connPillEl.className = 'pill on';
            modePillEl.style.display = '';
            if (readonlyMode) {
              modePillEl.textContent = '👁 Chỉ xem'; modePillEl.className = 'pill warn';
              term.options.disableStdin = true;
            } else {
              modePillEl.textContent = '⌨ Có thể gõ'; modePillEl.className = 'pill on';
              term.options.disableStdin = false;
            }
            if (typeof payload.attached === 'boolean') {
              attachPillEl.style.display = '';
              attachPillEl.textContent = payload.attached ? '● attached (nơi khác)' : '○ detached';
              attachPillEl.className = payload.attached ? 'pill warn' : 'pill';
              takeoverBtnEl.style.display = (payload.attached && !readonlyMode && !takeover) ? '' : 'none';
            }
          } else if (payload.type === 'closed') {
            setBanner('Phiên attach đã kết thúc (tiến trình tmux thoát hoặc session bị đóng).', 'error');
          }
          return;
        }
        term.write(new Uint8Array(event.data));
      };

      ws.onclose = (event) => {
        if (closedByUser) return;
        if (event.code >= 4400 && event.code < 4500) {
          // Server-side refusal (auth/permission/not-found) -- never a
          // transient network blip, so retrying forever would just spam
          // an unauthorized/nonexistent target. Shown once, no auto-retry.
          connPillEl.textContent = '● từ chối'; connPillEl.className = 'pill err';
          const reasons = {
            4401: 'Chưa đăng nhập.', 4403: 'Không có quyền mở terminal cho session này (hoặc tính năng đang tắt).',
            4404: 'Session không còn tồn tại.',
          };
          setBanner(reasons[event.code] || `Kết nối bị từ chối (mã ${event.code}).`, 'error');
          return;
        }
        scheduleReconnect();
      };
      ws.onerror = () => { /* onclose always follows; handled there */ };
    }

    term.onData((data) => {
      if (readonlyMode) return;
      ws && ws.readyState === WebSocket.OPEN && ws.send(textEncoder.encode(data));
    });

    takeoverBtnEl.onclick = () => {
      if (!confirm('Chiếm quyền sẽ ngắt kết nối của client khác đang gắn vào session này (không mất dữ liệu, chỉ ngắt kết nối xem). Tiếp tục?')) return;
      takeover = true;
      closedByUser = true;
      if (ws) ws.close();
      closedByUser = false;
      connect();
    };

    // Re-fit on any viewport change: orientation flip, iOS on-screen
    // keyboard show/hide (which resizes window.visualViewport, NOT
    // window itself, on iOS Safari), or the container simply changing
    // size. Debounced (rAF) so a flurry of resize events -- normal
    // during an iOS keyboard animation -- sends at most one resize
    // message per frame instead of flooding the pty.
    let fitPending = false;
    function requestFit() {
      if (fitPending) return;
      fitPending = true;
      requestAnimationFrame(() => {
        fitPending = false;
        try { fitAddon.fit(); } catch (error) { /* host not laid out yet -- next event will retry */ }
        sendResize();
      });
    }
    window.addEventListener('resize', requestFit);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', requestFit);
    new ResizeObserver(requestFit).observe(document.getElementById('termWrap'));

    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && (!ws || ws.readyState === WebSocket.CLOSED) && !closedByUser) {
        reconnectAttempt = 0;
        connect();
      }
    });
    window.addEventListener('beforeunload', () => { closedByUser = true; if (ws) ws.close(); });

    connect();
  </script>
</body>
</html>"""


def register_dashboard(server: MCPServer, terminal: TerminalService,
                       supervisor: SupervisorService | None = None,
                       supervisor_v2: SupervisorV2Service | None = None,
                       controller: ControllerService | None = None,
                       connection_store: ConnectionStore | None = None) -> None:
    if supervisor is None:
        supervisor = SupervisorService(terminal, SupervisorStore())
    if supervisor_v2 is None:
        supervisor_v2 = build_supervisor_v2(supervisor)
    if controller is None:
        # Single-node default (Phase A/B backward compatibility, task
        # item 11): every EXISTING caller of register_dashboard (tests,
        # webauth_dashboard.py, and server_http.py unless it explicitly
        # builds a real multi-node ControllerService) gets a controller
        # wrapping ONLY this same `terminal` instance as the local node
        # -- routed operations resolve to it directly, identical
        # behavior/response shape to calling `terminal` methods straight,
        # plus the additive node_id/node_name fields. See
        # build_default_controller's own docstring for why this is a
        # PRIVATE temp registry, never the real production nodes.db path.
        controller = build_default_controller(terminal)
    if connection_store is None:
        # Same private-temp-file discipline as build_default_controller's
        # own registry default just above (see that docstring for the
        # real production-state-pollution incident this guards against --
        # this feature's own ConnectionStore is exactly the same class of
        # risk: every test/ad-hoc caller of register_dashboard must never
        # write into the real ~/.local/state/terminal-mcp/connections.db).
        # server_http.py's real main() always passes an explicit,
        # persistent ConnectionStore instead of relying on this fallback.
        import tempfile
        connection_store = ConnectionStore(Path(tempfile.mkdtemp(prefix="terminal-mcp-connections-")) / "connections.db")
    discovery_config = terminal.config.nodes.discovery
    discovery = lan_discovery.DiscoveryService(
        agent_port=discovery_config.agent_port, concurrency=discovery_config.concurrency,
        host_timeout=discovery_config.host_timeout_seconds, max_hosts_per_scan=discovery_config.max_hosts_per_scan,
        overall_timeout_seconds=discovery_config.overall_timeout_seconds, cooldown_seconds=discovery_config.cooldown_seconds,
    )
    host_key_store = remote_connect.HostKeyStore()
    ssh_known_hosts_dir = connection_store.path.parent / "ssh_known_hosts"

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

    @server.custom_route("/dashboard/nodes", methods=["GET"], include_in_schema=False)
    async def dashboard_nodes_admin(request: Request) -> HTMLResponse | JSONResponse:
        # Same read guard as /dashboard itself -- a VIEW over the existing
        # /dashboard/api/nodes(/node) data and the existing drain/test-
        # connection mutation routes, not a new privilege surface. See
        # NODES_ADMIN_HTML's own module-level comment.
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        return HTMLResponse(
            NODES_ADMIN_HTML,
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
            # Node label (task item 6/16): every LOCAL row is tagged
            # explicitly (never left for the client to assume) --
            local_node = controller.node_status(controller.local_node_id)
            local_node_name = local_node.display_name if local_node else controller.local_node_id
            for row in rows:
                row.setdefault("node_id", controller.local_node_id)
                row.setdefault("node_name", local_node_name)

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

            # -- Remote-node sessions (task item 6: "sidebar hiển thị node
            # label"; item 10: sessions on a remote node must be visible
            # to route Kill/Delete correctly) -- additive merge on top of
            # the local list above, never replacing it. Each remote row
            # already carries real effective_read/effective_input/allowed
            # (computed by THAT node's own TerminalService.
            # terminal_list_sessions -- the same authorization fields
            # dashboard_list_sessions reports locally, just via the
            # narrower fleet-wide NodeClient surface, which is the reason
            # this was deferred before: it's a genuinely different method
            # than dashboard_list_sessions, not an unauthenticated one --
            # see docs/multi-node.md's own note on this). What it does NOT
            # carry: dashboard-specific fields (kill_reopen_ready,
            # classify_status state) -- filled in here the same way local
            # rows are, via a qualified "node/session" status call so a
            # same-named session on two different nodes can never be
            # confused.
            fleet = await anyio.to_thread.run_sync(controller.terminal_list_sessions)
            remote_rows = [row for row in fleet.get("sessions", []) if row.get("node_id") != controller.local_node_id]
            for row in remote_rows:
                row.setdefault("kill_reopen_ready", True)
                row["grant"] = {"read_enabled": row.get("read_granted", False), "input_enabled": row.get("input_granted", False)}

            async def _fill_remote_state(row: dict) -> None:
                if not row.get("effective_read"):
                    row["state"] = "RESTRICTED"
                    return
                qualified = f"{row['node_id']}/{row['name']}"
                status = await anyio.to_thread.run_sync(controller.terminal_status, qualified)
                row["state"] = status.get("state", "UNKNOWN")

            async with anyio.create_task_group() as tg:
                for row in remote_rows:
                    tg.start_soon(_fill_remote_state, row)
            rows.extend(remote_rows)

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

    # -- Session lifecycle: create/detach/delete/kill/reopen -- routed
    # through `controller`, the SAME multi-node-aware entry point
    # mcp_app.py's own MCP tools already used (one implementation, two
    # entry points -- see controller.py's own module docstring for the
    # Phase A/B "local node behaves exactly like calling TerminalService
    # directly" guarantee this relies on). SESSION_LIFECYCLE_DISABLED
    # (403) unless an operator has explicitly set session_lifecycle.
    # enabled: true in config.yaml.
    def _routed(call):
        """Every lifecycle mutation below needs the LOCAL node's own
        heartbeat fresh before it can even be considered ONLINE for
        resolve_session/choose_node -- unlike the GET routes above (which
        always refresh it themselves), a mutation isn't guaranteed to run
        after a recent GET that already did. Real bug caught wiring this:
        a bare TestClient hitting /session/create first, with no prior
        /nodes or /sessions poll, got NO_ELIGIBLE_NODE even for node=
        "auto" on a genuinely healthy single local node -- because that
        node had simply never heartbeated yet. One shared wrapper so this
        can never be forgotten on a future new lifecycle route either."""
        _refresh_local_heartbeat()
        return call()

    @server.custom_route("/dashboard/api/session/create", methods=["POST"], include_in_schema=False)
    async def session_create(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        agent_type = body.get("agent_type", "shell") if isinstance(body, dict) else None
        cwd = body.get("cwd") if isinstance(body, dict) else None
        # Multi-node create-session UX (task's own item 1-5/11): "node"
        # defaults to "auto" (the scheduler picks -- task item 4's own
        # "Auto phải gọi scheduler hiện có, không hardcode local"), or an
        # explicit node_id from /dashboard/api/nodes. Routed through
        # `controller`, never `terminal` directly -- real bug fixed here:
        # every dashboard lifecycle route (create/detach/delete/kill/
        # reopen) called `terminal.` directly before this, completely
        # bypassing the controller/multi-node layer that mcp_app.py's own
        # MCP tools already used correctly -- an explicit node selection
        # from THIS form would have silently created on the LOCAL node
        # regardless of what the operator picked (item 5's own explicit
        # "không silently fallback sang local nếu node lỗi" -- the old
        # code did exactly that, unconditionally, every time).
        node = body.get("node") if isinstance(body, dict) else None
        if node is not None and not isinstance(node, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        node = node.strip() if isinstance(node, str) and node.strip() else "auto"
        if not isinstance(name, str) or not name or not isinstance(agent_type, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if cwd is not None and not isinstance(cwd, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        granted_by = identity.email if identity else None
        _log.info("dashboard create_session name=%s agent_type=%s node=%s identity=%s", name, agent_type, node, granted_by)
        # The dashboard's "Tạo session" button never requests a grant or an
        # initial prompt -- explicit, separate opt-ins this route simply
        # doesn't expose (see core.py's terminal_create_session docstring:
        # creation itself never implies access). An operator who wants the
        # new session readable/sendable still grants it explicitly, same
        # as any other non-whitelisted session.
        result = await anyio.to_thread.run_sync(
            lambda: _routed(lambda: controller.terminal_create_session(name, agent_type, cwd, node=node, requested_by=granted_by))
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/detach", methods=["POST"], include_in_schema=False)
    async def session_detach(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        _log.info("dashboard detach_session name=%s identity=%s", name, identity.email if identity else None)
        # Routed through controller (task item 10: "Kill/Delete chỉ tác
        # động đúng session trên đúng node") -- resolves to whichever
        # node this session actually lives on, local or remote.
        result = await anyio.to_thread.run_sync(lambda: _routed(lambda: controller.terminal_detach_session(name)))
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/delete", methods=["POST"], include_in_schema=False)
    async def session_delete(request: Request) -> JSONResponse:
        # The UI-level "are you sure, this kills SESSION_NAME" confirmation
        # is the browser's confirm() dialog (see SESSIONS_ADMIN_HTML) --
        # this route's own safety floor is auth+CSRF (same _mutation_guard
        # as every other mutation here) plus core.py's protected_sessions
        # check, which is enforced regardless of what any client sends.
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        _log.info("dashboard delete_session name=%s identity=%s", name, identity.email if identity else None)
        # Routed through controller (task item 10) -- see session_detach's
        # own comment just above.
        result = await anyio.to_thread.run_sync(lambda: _routed(lambda: controller.terminal_delete_session(name)))
        if "error" not in result:
            await anyio.to_thread.run_sync(lambda: supervisor.unwatch(session=name, delete=False))
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/kill", methods=["POST"], include_in_schema=False)
    async def session_kill(request: Request) -> JSONResponse:
        # `confirm_name` must exactly match `name` -- enforced again inside
        # terminal_kill_session itself (never trust a client-side confirm()
        # dialog alone for a destructive action); this route's own floor is
        # the same auth+CSRF _mutation_guard as every mutation here.
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        confirm_name = body.get("confirm_name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name or not isinstance(confirm_name, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        requested_by = identity.email if identity else "dashboard"
        _log.info("dashboard kill_session name=%s identity=%s", name, identity.email if identity else None)
        result = await anyio.to_thread.run_sync(
            # Routed through controller (task item 10) -- see
            # session_detach's own comment for why every dashboard
            # lifecycle route needed this same fix.
            lambda: _routed(lambda: controller.terminal_kill_session(name, confirm_name, requested_by=requested_by))
        )
        if "error" not in result:
            await anyio.to_thread.run_sync(lambda: supervisor.unwatch(session=name, delete=False))
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session/reopen", methods=["POST"], include_in_schema=False)
    async def session_reopen(request: Request) -> JSONResponse:
        # agent_type/working_directory are OPTIONAL overrides -- omitted,
        # this uses saved Kill metadata; supplied, they replace the saved
        # value for that field (never merged/guessed) -- see
        # terminal_reopen_session's own docstring (core.py).
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        agent_type = body.get("agent_type") if isinstance(body, dict) else None
        working_directory = body.get("working_directory") if isinstance(body, dict) else None
        # Task item 9: "mặc định reopen trên node cũ; cho phép đổi node
        # nếu user chọn Move/Reopen elsewhere" -- omitted/None (the
        # default) reopens on the same node the session was killed on
        # (controller.terminal_reopen_session's own default); an explicit
        # node_id moves it there instead.
        node = body.get("node") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if agent_type is not None and not isinstance(agent_type, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if working_directory is not None and not isinstance(working_directory, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        if node is not None and not isinstance(node, str):
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        requested_by = identity.email if identity else "dashboard"
        _log.info("dashboard reopen_session name=%s node=%s identity=%s", name, node, identity.email if identity else None)
        # Routed through controller (task item 10) -- same fix as every
        # other lifecycle route above; controller.terminal_reopen_session
        # itself resolves via each node's own killed-sessions list, never
        # live-session lookup (see its own docstring for the real, related
        # bug that would otherwise cause -- a session past the 20s
        # location-cache window would report SESSION_NOT_FOUND).
        result = await anyio.to_thread.run_sync(
            lambda: _routed(lambda: controller.terminal_reopen_session(
                name, agent_type=agent_type, cwd=working_directory, node=node, requested_by=requested_by))
        )
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/killed-sessions", methods=["GET"], include_in_schema=False)
    async def killed_sessions_list(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        result = await anyio.to_thread.run_sync(terminal.terminal_list_killed_sessions)
        status_code = 200 if "error" not in result else INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    # -- Web terminal: xterm.js over a WebSocket, attached directly to an
    # existing tmux session's real pty (webterm.py). See WEBTERM_HTML's
    # own module-level comment for why this is a standalone page rather
    # than folded into DASHBOARD_HTML, and TerminalService.
    # terminal_web_terminal_access (core.py) for the one place read/input
    # authorization for this feature is actually decided -- this route
    # calls that and nothing else, exactly like every other route here
    # defers its authorization decision to TerminalService.

    @server.custom_route("/dashboard/assets/{filename}", methods=["GET"], include_in_schema=False)
    async def webterm_asset(request: Request) -> Response:
        # Static, versioned-by-vendored-file bytes (xterm.js/css, the fit
        # addon) -- no session content, nothing request-specific, so this
        # is deliberately NOT behind _read_guard: an operator's Cloudflare
        # Access/webauth login gate exists to protect tmux pane content
        # and session control, not a copy of a public JS library. Immutable
        # cache: the URL never changes without a code deploy (no cache-
        # busting query string), so a long max-age is safe and correct.
        asset = ASSETS.get(request.path_params["filename"])
        if asset is None:
            return Response(status_code=404)
        content, content_type = asset
        return Response(content, media_type=content_type,
                        headers={"Cache-Control": "public, max-age=86400, immutable"})

    @server.custom_route("/dashboard/terminal", methods=["GET"], include_in_schema=False)
    async def webterm_page(request: Request) -> HTMLResponse | JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        return HTMLResponse(
            WEBTERM_HTML,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    def _resolve_remote_web_terminal_access(session: str) -> dict | None:
        """Only reached after terminal.terminal_web_terminal_access(session)
        already returned SESSION_NOT_FOUND -- meaning every check UP TO
        the physical-existence one (web_terminal_enabled, require_read,
        valid_session_name, and READ authorization via
        _read_authorized_with_grant) already passed; that function
        returns ACCESS_DENIED before ever reaching SESSION_NOT_FOUND if
        read isn't authorized. This only has to resolve WHERE the
        session actually lives and whether INPUT is authorized -- read
        access is already a settled question by the time this runs.

        input authorization for a remote session uses revalidate_
        identity=False (same as the discovery endpoints' own coarse
        signal) -- a real, documented Phase A/B limitation: per-call
        identity re-pinning (P0-2) needs this node's own tmux, which a
        remote session doesn't have. A statically-whitelisted session
        (input_session_allowed) is completely unaffected (that check
        short-circuits before identity revalidation ever matters);
        only a GRANT-based (non-whitelisted) remote session's input
        authorization is coarser than the local case."""
        resolution = controller.resolve_session(session)
        if "error" in resolution:
            return None
        node_id = resolution["node_id"]
        if node_id == controller.local_node_id:
            return None  # already confirmed not-found locally -- fail safe, never loop back to itself
        node = controller.node_status(node_id)
        if node is None or node.status != NODE_ONLINE:
            return None
        client = controller.client_for(node_id)
        if not isinstance(client, RemoteNodeClient):
            return None
        grant = terminal.grants.get(session)
        input_enabled = bool(
            terminal.config.permissions.terminal_input
            and terminal._input_authorized_with_grant(session, grant, revalidate_identity=False)[0]
        )
        return {"node_id": node_id, "client": client, "input": input_enabled}

    async def _proxy_remote_terminal_ws(websocket: WebSocket, remote_access: dict) -> None:
        """Relays an already-`accept()`-pending browser WebSocket to a
        REMOTE node's own /v1/ws/terminal (node_agent.py) -- this
        process never touches tmux/WindowsSessionBackend directly for a
        remote session, it's a pure byte/frame relay, same trust
        boundary as every other routed operation in this project
        (controller.py never re-implements what a node agent already
        does, only routes to it)."""
        import websockets as _websockets_client

        client: RemoteNodeClient = remote_access["client"]
        session = websocket.query_params.get("session", "")
        input_enabled = remote_access["input"]
        remote_base = client.base_url.replace("https://", "wss://").replace("http://", "ws://")
        remote_url = f"{remote_base}/v1/ws/terminal?session={quote(session)}&readonly={0 if input_enabled else 1}"

        await websocket.accept()
        try:
            async with _websockets_client.connect(
                remote_url, extra_headers={"Authorization": f"Bearer {client._token}"}, open_timeout=10,
            ) as remote_ws:
                async def _from_remote() -> None:
                    async for message in remote_ws:
                        if websocket.client_state != WebSocketState.CONNECTED:
                            break
                        if isinstance(message, (bytes, bytearray)):
                            await websocket.send_bytes(bytes(message))
                        else:
                            await websocket.send_text(message)

                async def _from_browser() -> None:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        data = message.get("bytes")
                        if data is not None:
                            await remote_ws.send(data)
                            continue
                        text = message.get("text")
                        if text is not None:
                            await remote_ws.send(text)

                async with anyio.create_task_group() as tg:
                    tg.start_soon(_from_remote)
                    try:
                        await _from_browser()
                    except WebSocketDisconnect:
                        pass
                    finally:
                        tg.cancel_scope.cancel()
        except Exception as exc:  # noqa: BLE001 -- a remote-connect failure must close cleanly, never hang the browser socket
            _log.warning("remote web terminal proxy to node_id=%s failed: %s: %s",
                        remote_access.get("node_id"), type(exc).__name__, exc)
            with contextlib.suppress(Exception):
                if websocket.client_state == WebSocketState.CONNECTED:
                    await websocket.send_json({"type": "closed", "reason": "remote_node_unreachable"})
        with contextlib.suppress(Exception):
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()

    async def dashboard_terminal_ws(websocket: WebSocket) -> None:
        # _read_guard/_origin_allowed are plain functions of .headers/
        # .cookies -- a Starlette WebSocket exposes both with the exact
        # same interface a Request does, so these are the SAME closures
        # every HTTP route above already uses, not a parallel copy of the
        # auth/CSRF decision. Checked before accept(): the ASGI websocket
        # handshake protocol treats close() before accept() as refusing
        # the handshake itself (surfaces to the browser as a failed
        # connection, close code included), never as an accepted socket
        # that then immediately closes.
        blocked, _identity = _read_guard(websocket)
        if blocked is not None:
            await websocket.close(code=4401)
            return
        if not _origin_allowed(websocket):
            await websocket.close(code=4403)
            return
        session = websocket.query_params.get("session", "")
        takeover_requested = websocket.query_params.get("takeover") == "1"
        if not valid_session_name(session):
            await websocket.close(code=4400)
            return
        access = await anyio.to_thread.run_sync(terminal.terminal_web_terminal_access, session)
        if access.get("error") == "SESSION_NOT_FOUND":
            # Not on the LOCAL node specifically -- task's own "Open
            # Terminal trên Windows/remote node phải mở được web
            # terminal", generalized to any remote node (Linux or
            # Windows). Every check terminal_web_terminal_access itself
            # would have applied UP TO the physical-existence check
            # (web_terminal_enabled, require_read, valid_session_name,
            # already checked above) still applies -- only the "does it
            # exist" step is redirected to controller.resolve_session
            # instead of this node's own tmux.
            remote_access = await anyio.to_thread.run_sync(lambda: _resolve_remote_web_terminal_access(session))
            if remote_access is not None:
                await _proxy_remote_terminal_ws(websocket, remote_access)
                return
            await websocket.close(code=4404)
            return
        if "error" in access:
            code = 4403
            await websocket.close(code=code)
            return
        input_enabled = bool(access["input"])
        # Takeover (tmux `-d`, detaching any other attached client) is a
        # disruptive, explicit choice (see WEBTERM_HTML's confirm()
        # prompt) -- only ever honored for a caller who already has input
        # authorization; a read-only viewer's takeover=1 is silently
        # dropped rather than granted, never upgraded into one.
        takeover = takeover_requested and input_enabled
        await websocket.accept()
        terminal.audit.record(action="web_terminal_open", session=session, result="OPENED",
                              reason=f"input={input_enabled} takeover={takeover}", source_transport="dashboard")
        proc = await anyio.to_thread.run_sync(
            lambda: WebTerminalProcess(terminal.tmux.binary, session, readonly=not input_enabled, takeover=takeover)
        )
        try:
            await websocket.send_json({"type": "ready", "session": session, "readonly": not input_enabled,
                                       "attached": access.get("attached", False)})
            await pump_websocket(websocket, proc)
        finally:
            await anyio.to_thread.run_sync(proc.close)
            terminal.audit.record(action="web_terminal_close", session=session, result="CLOSED",
                                  source_transport="dashboard")

    # MCPServer.custom_route only supports plain HTTP (Route) -- it has no
    # WebSocket-route decorator. streamable_http_app() builds the actual
    # Starlette app from `_custom_starlette_routes` verbatim
    # (`routes.extend(custom_starlette_routes)` into `Starlette(routes=
    # routes, ...)`), which accepts any Starlette BaseRoute, so appending
    # a WebSocketRoute directly to that same list -- the one every
    # @server.custom_route call above already populates -- works exactly
    # like a supported route type, without a parallel app/server instance.
    server._custom_starlette_routes.append(
        WebSocketRoute("/dashboard/ws/terminal", endpoint=dashboard_terminal_ws, name="dashboard_terminal_ws")
    )

    @server.custom_route("/dashboard/api/connection-health", methods=["GET"], include_in_schema=False)
    async def connection_health(request: Request) -> JSONResponse:
        # One coarse label (task item 5: "chỉ cần một trạng thái tổng...
        # không spam UI") for the dashboard's own small header banner --
        # the full breakdown lives in `terminal-mcp-doctor connection`,
        # never duplicated here. skip_network_check=True: this route is
        # polled by every open dashboard tab, so it deliberately never
        # does the (slower, external) DNS/TLS probe to api.openai.com on
        # every request -- mcp_local/tunnel_process/tunnel_ready alone
        # (all local-only checks) are enough to pick one of the 4 banner
        # labels; the watchdog's own periodic diagnose() call (every 45s,
        # not once per open browser tab) is what actually exercises the
        # network check.
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked

        def _compute() -> dict:
            state = tunnel_diagnostics.WatchdogState.load(tunnel_diagnostics.default_state_path())
            diag = tunnel_diagnostics.diagnose(state=state, skip_network_check=True)
            return {"banner": tunnel_diagnostics.banner_status(diag, state), "diagnosis": diag}

        result = await anyio.to_thread.run_sync(_compute)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    # -- Nodes (multi-node session management) -------------------------------
    # See controller.py's own module docstring for the full design. Every
    # GET here refreshes the LOCAL node's own heartbeat first (cheap --
    # a few /proc reads, no network -- see ControllerService.
    # refresh_local_heartbeat's own docstring for why this needs no
    # background thread) so the dashboard never shows a stale/never-
    # heartbeated local node; a REMOTE node's freshness depends entirely
    # on its own push (node_agent.py's heartbeat loop) -- this route
    # never blocks on, or synchronously polls, a remote node.
    def _refresh_local_heartbeat() -> None:
        # A real tmux listing (terminal.tmux, not terminal_list_sessions()'s
        # own dashboard-facing row shape, which deliberately never exposes
        # pane_current_command past its narrow internal use) -- the same
        # source of truth node_agent.py's heartbeat loop uses for a remote
        # node, so a node's agent_counts always means the same thing
        # regardless of which node reported it.
        try:
            items = terminal.tmux.list_sessions()
        except Exception:  # noqa: BLE001 -- a metrics refresh must never break a dashboard read
            items = []
        agent_counts: dict[str, int] = {}
        for item in items:
            command = (item.pane_current_command or "").casefold()
            if command:
                agent_counts[command] = agent_counts.get(command, 0) + 1
        agent_types = available_agent_types(terminal.config.session_lifecycle.launch_commands)
        controller.refresh_local_heartbeat(
            tmux_session_count=len(items), agent_counts=agent_counts,
            agent_types=agent_types, agent_version=None,
        )

    @server.custom_route("/dashboard/api/nodes", methods=["GET"], include_in_schema=False)
    async def nodes_list(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked

        def _compute() -> dict:
            _refresh_local_heartbeat()
            nodes = controller.list_nodes()
            # Task item 7: "Doctor/dashboard phải hiển thị rõ controller
            # endpoints" -- same resolver terminal-mcp-doctor connection
            # uses, so the Nodes page and the CLI never show two
            # independently-drifting answers.
            endpoints = network_bind.describe_endpoints(
                # 8766 -- server_http.py's own HTTP_PORT constant, not
                # imported directly to avoid a circular import
                # (server_http.py itself imports register_dashboard from
                # this module).
                port=8766, lan_bind_env=os.environ.get("TERMINAL_MCP_LAN_BIND"),
                cidrs_env=os.environ.get("TERMINAL_MCP_ALLOWED_NODE_CIDRS"),
            )
            return {"nodes": [_node_to_dict(n) for n in nodes], "controller_endpoints": endpoints}

        result = await anyio.to_thread.run_sync(_compute)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/node", methods=["GET"], include_in_schema=False)
    async def node_detail(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        node_id = request.query_params.get("id", "")
        if not node_id:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)

        def _compute() -> dict:
            if node_id == controller.local_node_id:
                _refresh_local_heartbeat()
            node = controller.node_status(node_id)
            if node is None:
                return {"error": "NODE_NOT_FOUND", "node_id": node_id}
            sessions = controller.node_sessions(node_id)
            return {"node": _node_to_dict(node), "sessions": sessions.get("sessions", []),
                   "sessions_error": sessions.get("error")}

        result = await anyio.to_thread.run_sync(_compute)
        status_code = 404 if result.get("error") == "NODE_NOT_FOUND" else 200
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/node/drain", methods=["POST"], include_in_schema=False)
    async def node_drain(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        node_id = body.get("node_id") if isinstance(body, dict) else None
        draining = bool(body.get("draining", True)) if isinstance(body, dict) else True
        if not isinstance(node_id, str) or not node_id:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        _log.info("dashboard node_drain node_id=%s draining=%s identity=%s", node_id, draining,
                 identity.email if identity else None)
        result = await anyio.to_thread.run_sync(lambda: controller.set_draining(node_id, draining))
        status_code = 200 if "error" not in result else 404
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/node/test-connection", methods=["POST"], include_in_schema=False)
    async def node_test_connection(request: Request) -> JSONResponse:
        blocked, _identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        node_id = body.get("node_id") if isinstance(body, dict) else None
        if not isinstance(node_id, str) or not node_id:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        result = await anyio.to_thread.run_sync(lambda: controller.test_connection(node_id))
        status_code = 200 if "error" not in result else 404
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/node/generate-onboarding", methods=["POST"], include_in_schema=False)
    async def node_generate_onboarding(request: Request) -> JSONResponse:
        # Onboard flow (task's own "Add/onboard Windows node flow ngắn
        # gọn"): generates a fresh, random token SERVER-SIDE and returns
        # the exact config.yaml block + env var + one-line install
        # command to run on the new node -- never writes anything to the
        # registry or to config.yaml itself (registering a REMOTE node
        # still requires the same manual "add to config.yaml, export the
        # env var, safe-restart" steps every other remote node already
        # needs -- see docs/multi-node.md's own Bringing up the M910
        # section, this is the same flow, just Windows-flavored and with
        # the token generated for the operator instead of by hand). The
        # generated token is returned exactly once, in this one response
        # -- never persisted, never logged.
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        node_id = body.get("node_id") if isinstance(body, dict) else None
        display_name = body.get("display_name") if isinstance(body, dict) else None
        hostname = body.get("hostname") if isinstance(body, dict) else None
        endpoint = body.get("endpoint") if isinstance(body, dict) else None
        platform_kind = (body.get("platform") if isinstance(body, dict) else None) or "windows"
        if not all(isinstance(v, str) and v.strip() for v in (node_id, hostname, endpoint)):
            return JSONResponse({"error": "INVALID_REQUEST", "detail": "node_id, hostname, endpoint are required"},
                                status_code=400)
        if not re.match(r"^[A-Za-z0-9_-]+$", node_id):
            return JSONResponse({"error": "INVALID_REQUEST", "detail": "node_id must be alphanumeric/-/_ only"},
                                status_code=400)
        display_name = display_name or node_id
        token = secrets.token_hex(32)
        env_var = node_token_env_var(node_id)
        config_yaml_block = (
            "nodes:\n  remote:\n"
            f"    - node_id: {node_id}\n"
            f"      display_name: \"{display_name}\"\n"
            f"      hostname: \"{hostname}\"\n"
            f"      endpoint: \"{endpoint}\"\n"
            f"      token_env: {env_var}\n"
        )
        if platform_kind == "windows":
            install_command = (
                f".\\deploy\\install-node-agent.ps1 -ControllerUrl <http(s)://controller-host:8766> "
                f"-NodeId {node_id} -Token {token}"
            )
        else:
            install_command = (
                f"./deploy/install-node-agent.sh --controller <http://controller-host:8766> --node-id {node_id}"
                " (then paste the token below into the printed node-agent.env)"
            )
        _log.info("dashboard node_generate_onboarding node_id=%s platform=%s identity=%s",
                 node_id, platform_kind, identity.email if identity else None)
        return JSONResponse({
            "node_id": node_id, "token": token, "env_var": env_var,
            "config_yaml_block": config_yaml_block, "install_command": install_command,
        }, headers={"Cache-Control": "no-store"})

    # ======================================================================
    # LAN device discovery + remote connect/bootstrap (Nodes page "Connect
    # Node": Scan LAN / Add Remote SSH / Add via Cloudflare Tunnel / Add by
    # Agent Token). Every route below is a POST/GET under
    # /dashboard/api/nodes/discovery/* or /dashboard/api/nodes/connect/*,
    # through the SAME _mutation_guard/_read_guard as every other node
    # route above -- no separate auth model. Every mutation route logs
    # only non-secret identifiers (node_id/host/transport_type/username/
    # result) via _log, matching this file's own existing node_drain/
    # node_test_connection/node_generate_onboarding convention -- never a
    # password/private key/token in a log line (task's own "audit
    # connection attempts nhưng redact secret").
    # ======================================================================

    def _remote_connect_config():
        return terminal.config.nodes.remote_connect

    @server.custom_route("/dashboard/api/nodes/discovery/status", methods=["GET"], include_in_schema=False)
    async def discovery_status(request: Request) -> JSONResponse:
        blocked, _identity = _read_guard(request)
        if blocked is not None:
            return blocked
        result = discovery.status()
        if result is None:
            return JSONResponse({"scan_id": None, "state": "never_run"}, headers={"Cache-Control": "no-store"})
        return JSONResponse(result.to_dict(), headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/discovery/scan", methods=["POST"], include_in_schema=False)
    async def discovery_scan(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        if not terminal.config.nodes.discovery.enabled:
            return JSONResponse({"error": "DISCOVERY_DISABLED"}, status_code=403, headers={"Cache-Control": "no-store"})
        known_endpoints = {node.id: node.endpoint for node in controller.list_nodes() if node.endpoint != "local"}
        result, started = discovery.start_scan(known_endpoints=known_endpoints)
        _log.info("dashboard discovery_scan scan_id=%s started=%s identity=%s",
                 result.scan_id, started, identity.email if identity else None)
        payload = result.to_dict()
        payload["started"] = started
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/discovery/cancel", methods=["POST"], include_in_schema=False)
    async def discovery_cancel(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        cancelled = await discovery.cancel()
        _log.info("dashboard discovery_cancel cancelled=%s identity=%s", cancelled, identity.email if identity else None)
        return JSONResponse({"cancelled": cancelled}, headers={"Cache-Control": "no-store"})

    def _validate_ssh_target(body: dict) -> tuple[remote_connect.SshTarget | None, JSONResponse | None]:
        transport_type = body.get("transport_type")
        if transport_type not in (remote_connect.TRANSPORT_LAN_SSH, remote_connect.TRANSPORT_CLOUDFLARE_SSH):
            return None, JSONResponse({"error": "INVALID_REQUEST", "detail": "transport_type must be lan_ssh or cloudflare_ssh"},
                                      status_code=400)
        try:
            if transport_type == remote_connect.TRANSPORT_LAN_SSH:
                host = remote_connect.validate_hostname_or_ip(
                    body.get("host", ""), allow_public=_remote_connect_config().allow_public_manual_add)
            else:
                host = remote_connect.validate_cloudflare_hostname(body.get("host", ""))
            username = remote_connect.validate_username(body.get("username", ""))
            port = remote_connect.validate_port(body.get("port"), default=22)
        except remote_connect.ValidationError as exc:
            return None, JSONResponse({"error": "INVALID_REQUEST", "detail": str(exc)}, status_code=400)
        return remote_connect.SshTarget(transport_type=transport_type, host=host, port=port, username=username), None

    def _credential_from_body(body: dict) -> tuple[remote_connect.SshCredential | None, JSONResponse | None]:
        raw = body.get("credential") or {}
        if not isinstance(raw, dict):
            return None, JSONResponse({"error": "INVALID_REQUEST", "detail": "credential must be an object"}, status_code=400)
        try:
            username = remote_connect.validate_username(body.get("username", ""))
            credential = remote_connect.SshCredential(
                username=username, password=raw.get("password") or None,
                private_key_pem=raw.get("private_key_pem") or None, key_passphrase=raw.get("key_passphrase") or None,
            )
        except remote_connect.ValidationError as exc:
            return None, JSONResponse({"error": "INVALID_REQUEST", "detail": str(exc)}, status_code=400)
        return credential, None

    def _pinned_fingerprint_for(target: remote_connect.SshTarget) -> str | None:
        for saved in connection_store.list():
            if (saved.transport_type == target.transport_type and saved.hostname == target.host
                    and (saved.port or 22) == target.port):
                return saved.host_key_fingerprint
        return host_key_store.pinned_for(target)

    @server.custom_route("/dashboard/api/nodes/connect/ssh/trust-hostkey", methods=["POST"], include_in_schema=False)
    async def connect_ssh_trust_hostkey(request: Request) -> JSONResponse:
        # The ONE explicit "approve this host key" action (task item 4) --
        # never invoked implicitly by test/bootstrap. A fingerprint
        # already pinned for this exact target is silently re-confirmed
        # (idempotent), but a CHANGED fingerprint still requires this same
        # explicit call again -- there is no separate "auto-update".
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        target, error = _validate_ssh_target(body if isinstance(body, dict) else {})
        if error is not None:
            return error

        def _probe():
            return remote_connect.probe_host_key(target)
        probe = await anyio.to_thread.run_sync(_probe)
        if not probe.ok:
            return JSONResponse({"error": probe.error_class or "UNREACHABLE", "detail": probe.error}, status_code=502)
        host_key_store.trust(target, probe.fingerprint)
        _log.info("dashboard connect_ssh_trust_hostkey transport=%s host=%s port=%s fingerprint=%s identity=%s",
                 target.transport_type, target.host, target.port, probe.fingerprint, identity.email if identity else None)
        return JSONResponse({"trusted": True, "fingerprint": probe.fingerprint}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/connect/ssh/test", methods=["POST"], include_in_schema=False)
    async def connect_ssh_test(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        target, error = _validate_ssh_target(body)
        if error is not None:
            return error
        pinned = _pinned_fingerprint_for(target)
        has_credential = bool(body.get("credential"))

        def _run_test():
            if has_credential:
                return None  # handled below, after we know pinned/probe status
            return remote_connect.test_connection(target, known_hosts_dir=ssh_known_hosts_dir,
                                                  host_key_store=host_key_store, pinned_fingerprint=pinned)

        if not has_credential:
            result = await anyio.to_thread.run_sync(_run_test)
        else:
            credential, cred_error = _credential_from_body(body)
            if cred_error is not None:
                return cred_error
            if pinned is None:
                key_only = await anyio.to_thread.run_sync(
                    lambda: remote_connect.test_connection(target, known_hosts_dir=ssh_known_hosts_dir,
                                                           host_key_store=host_key_store, pinned_fingerprint=None))
                result = key_only
            else:
                result = await anyio.to_thread.run_sync(
                    lambda: remote_connect.test_connection_with_credential(
                        target, credential, known_hosts_dir=ssh_known_hosts_dir, pinned_fingerprint=pinned,
                        timeout=_remote_connect_config().ssh_connect_timeout_seconds * 2))
        _log.info("dashboard connect_ssh_test transport=%s host=%s port=%s stage=%s ok=%s identity=%s",
                 target.transport_type, target.host, target.port, result.stage, result.ok,
                 identity.email if identity else None)
        return JSONResponse({
            "stage": result.stage, "ok": result.ok, "fingerprint": result.fingerprint, "detail": result.detail,
        }, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/connect/ssh/bootstrap", methods=["POST"], include_in_schema=False)
    async def connect_ssh_bootstrap(request: Request) -> JSONResponse:
        # Linux only -- see connect_windows_bootstrap for why Windows
        # always returns manual guidance instead of a live install here.
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        target, error = _validate_ssh_target(body)
        if error is not None:
            return error
        try:
            node_id = remote_connect.validate_node_id(body.get("node_id", ""))
        except remote_connect.ValidationError as exc:
            return JSONResponse({"error": "INVALID_REQUEST", "detail": str(exc)}, status_code=400)
        if controller.node_status(node_id) is not None:
            return JSONResponse({"error": "NODE_ALREADY_EXISTS", "node_id": node_id}, status_code=409)
        credential, cred_error = _credential_from_body(body)
        if cred_error is not None:
            return cred_error
        pinned = _pinned_fingerprint_for(target)
        if pinned is None:
            return JSONResponse({"error": "HOST_KEY_NOT_TRUSTED",
                                 "detail": "call /dashboard/api/nodes/connect/ssh/trust-hostkey first"},
                                status_code=409)
        controller_url = body.get("controller_url")
        if not isinstance(controller_url, str) or not controller_url.strip():
            return JSONResponse({"error": "INVALID_REQUEST", "detail": "controller_url is required -- must be an "
                                 "address the NEW node's own heartbeat loop can reach back to this controller on"},
                                status_code=400)
        if target.transport_type == remote_connect.TRANSPORT_LAN_SSH:
            bind_host = body.get("bind_host") or target.host
        else:
            bind_host = body.get("agent_endpoint_host")
            if not bind_host:
                return JSONResponse({"error": "AGENT_ENDPOINT_REQUIRED", "detail": (
                    "a Cloudflare-tunnel SSH target has no direct network address for this controller to reach "
                    "the new agent's own HTTP API afterward -- supply agent_endpoint_host (a directly-reachable "
                    "LAN/VPN address for that host, or a second Cloudflare Access TCP hostname/local sidecar port "
                    "you've already set up for the agent's own port; this feature cannot auto-provision that "
                    "second Access application)")}, status_code=400)
        token = generate_node_token()
        agent_port = terminal.config.nodes.discovery.agent_port

        def _bootstrap():
            return remote_connect.run_linux_bootstrap(
                target, credential, node_id=node_id, controller_url=controller_url, token=token,
                bind_host=bind_host, known_hosts_dir=ssh_known_hosts_dir, pinned_fingerprint=pinned,
                timeout=_remote_connect_config().bootstrap_timeout_seconds,
            )
        bootstrap_result = await anyio.to_thread.run_sync(_bootstrap)
        _log.info("dashboard connect_ssh_bootstrap node_id=%s transport=%s host=%s ok=%s identity=%s",
                 node_id, target.transport_type, target.host, bootstrap_result.ok, identity.email if identity else None)
        if not bootstrap_result.ok:
            return JSONResponse({
                "error": "BOOTSTRAP_FAILED", "stdout": bootstrap_result.stdout[-4000:],
                "stderr": bootstrap_result.stderr[-4000:], "returncode": bootstrap_result.returncode,
            }, status_code=502, headers={"Cache-Control": "no-store"})

        endpoint = f"http://{bind_host}:{agent_port}"

        def _health_probe() -> bool:
            client = RemoteNodeClient(endpoint, token, timeout=5.0)
            for _attempt in range(5):
                ok, _latency, _detail = client.ping()
                if ok:
                    return True
                import time as _time
                _time.sleep(1.5)
            return False

        healthy = await anyio.to_thread.run_sync(_health_probe)
        if not healthy:
            return JSONResponse({
                "error": "AGENT_NOT_REACHABLE_AFTER_BOOTSTRAP", "endpoint": endpoint,
                "detail": "the bootstrap script reported success but the new agent's /v1/health never answered "
                         "-- check bind_host/firewall, or the printed node-agent.log on the remote host",
                "stdout": bootstrap_result.stdout[-4000:],
            }, status_code=502, headers={"Cache-Control": "no-store"})

        token_file = connection_store.write_token(node_id, token)
        connection_store.save(node_id, transport_type=target.transport_type, endpoint=endpoint, hostname=target.host,
                              username=target.username, port=target.port, host_key_fingerprint=pinned,
                              token_file=token_file)
        controller.register_remote_node(node_id, display_name=body.get("display_name") or node_id,
                                        hostname=target.host, endpoint=endpoint, token=token)
        # The new agent's own heartbeat loop pushes to node_heartbeat
        # (below), which authenticates via THIS env var -- see
        # node_token_env_var's own docstring. Set in-process (this
        # controller's own os.environ, not the shell's) so the very next
        # heartbeat push already verifies correctly, no separate manual
        # export step needed for a discovery/SSH-connected node.
        os.environ[node_token_env_var(node_id)] = token
        node = controller.node_status(node_id)
        return JSONResponse({"ok": True, "node_id": node_id, "endpoint": endpoint,
                            "node": _node_to_dict(node) if node else None}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/connect/windows/bootstrap", methods=["POST"], include_in_schema=False)
    async def connect_windows_bootstrap(request: Request) -> JSONResponse:
        # Always manual guidance -- see remote_connect.windows_bootstrap_
        # guidance's own docstring for why this never claims a live
        # WinRM/PowerShell-remoting install.
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            node_id = remote_connect.validate_node_id(body.get("node_id", ""))
        except remote_connect.ValidationError as exc:
            return JSONResponse({"error": "INVALID_REQUEST", "detail": str(exc)}, status_code=400)
        hostname = (body.get("hostname") or "").strip()
        controller_url = (body.get("controller_url") or "").strip()
        if not hostname or not controller_url:
            return JSONResponse({"error": "INVALID_REQUEST", "detail": "hostname and controller_url are required"},
                                status_code=400)
        token = generate_node_token()
        guidance = remote_connect.windows_bootstrap_guidance(node_id=node_id, controller_url=controller_url,
                                                              token=token, hostname=hostname)
        _log.info("dashboard connect_windows_bootstrap node_id=%s hostname=%s identity=%s",
                 node_id, hostname, identity.email if identity else None)
        return JSONResponse(guidance, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/connect/agent-token", methods=["POST"], include_in_schema=False)
    async def connect_agent_token(request: Request) -> JSONResponse:
        blocked, identity = _mutation_guard(request)
        if blocked is not None:
            return blocked
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        try:
            node_id = remote_connect.validate_node_id(body.get("node_id", ""))
        except remote_connect.ValidationError as exc:
            return JSONResponse({"error": "INVALID_REQUEST", "detail": str(exc)}, status_code=400)
        endpoint = (body.get("endpoint") or "").strip()
        token = body.get("token") or ""
        if not endpoint.startswith(("http://", "https://")) or not token:
            return JSONResponse({"error": "INVALID_REQUEST", "detail": "endpoint (http(s)://host:port) and token are required"},
                                status_code=400)
        if controller.node_status(node_id) is not None:
            return JSONResponse({"error": "NODE_ALREADY_EXISTS", "node_id": node_id}, status_code=409)
        host_part = re.sub(r"^https?://", "", endpoint).split("/", 1)[0].split(":", 1)[0]
        try:
            remote_connect.validate_hostname_or_ip(host_part, allow_public=_remote_connect_config().allow_public_manual_add)
        except remote_connect.ValidationError as exc:
            return JSONResponse({"error": "INVALID_REQUEST", "detail": str(exc)}, status_code=400)

        def _probe() -> tuple[bool, str | None]:
            client = RemoteNodeClient(endpoint, token, timeout=8.0)
            ok, _latency, detail = client.ping()
            return ok, detail
        healthy, detail = await anyio.to_thread.run_sync(_probe)
        _log.info("dashboard connect_agent_token node_id=%s endpoint=%s healthy=%s identity=%s",
                 node_id, endpoint, healthy, identity.email if identity else None)
        if not healthy:
            return JSONResponse({"error": "AGENT_NOT_REACHABLE", "detail": detail}, status_code=502,
                                headers={"Cache-Control": "no-store"})

        token_file = connection_store.write_token(node_id, token)
        connection_store.save(node_id, transport_type="agent_token", endpoint=endpoint, hostname=host_part,
                              token_file=token_file)
        controller.register_remote_node(node_id, display_name=body.get("display_name") or node_id,
                                        hostname=host_part, endpoint=endpoint, token=token)
        # See node_token_env_var's own docstring -- makes the node's
        # (already-running) heartbeat loop verify successfully against
        # THIS controller the moment its next push arrives.
        os.environ[node_token_env_var(node_id)] = token
        node = controller.node_status(node_id)
        return JSONResponse({"ok": True, "node_id": node_id, "endpoint": endpoint,
                            "node": _node_to_dict(node) if node else None}, headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/nodes/{node_id}/heartbeat", methods=["POST"], include_in_schema=False)
    async def node_heartbeat(request: Request) -> JSONResponse:
        # Machine-to-machine (a remote node's own terminal-node-agent
        # process pushing its heartbeat) -- NOT a browser, so this is
        # bearer-token authenticated (per-node shared secret, task item
        # 2), never the Cloudflare Access / webauth cookie guards every
        # other dashboard route uses. A missing/wrong token is refused
        # before touching the registry at all -- never silently accepted
        # as "must be the local node" or similarly guessed.
        node_id = request.path_params["node_id"]
        expected_token = os.environ.get(node_token_env_var(node_id))
        header = request.headers.get("authorization", "")
        presented = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        if not expected_token or not hmac.compare_digest(presented, expected_token):
            return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)

        def _compute() -> dict:
            from .host_metrics import NodeMetrics
            metrics_raw = body.get("metrics") or {}
            metrics = NodeMetrics(**{k: metrics_raw.get(k) for k in NodeMetrics.__dataclass_fields__})
            node = controller.receive_remote_heartbeat(
                node_id, metrics=metrics, tmux_session_count=int(body.get("tmux_session_count") or 0),
                agent_counts=dict(body.get("agent_counts") or {}),
                agent_types=tuple(body.get("agent_types") or ()),
                agent_version=body.get("agent_version"), labels=tuple(body.get("labels") or ()),
                platform=body.get("platform") or "linux", session_backend=body.get("session_backend") or "tmux",
                shell_capabilities=tuple(body.get("shell_capabilities") or ()),
                wsl_available=bool(body.get("wsl_available", False)),
            )
            if node is None:
                return {"error": "NODE_NOT_FOUND", "node_id": node_id}
            return {"ok": True, "node_id": node_id}

        result = await anyio.to_thread.run_sync(_compute)
        status_code = 200 if "error" not in result else 404
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
