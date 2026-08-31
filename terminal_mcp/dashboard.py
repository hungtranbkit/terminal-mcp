from __future__ import annotations

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .core import TerminalService
from .permissions import input_session_allowed


INPUT_ERROR_STATUS = {
    "ACCESS_DENIED": 403,
    "INPUT_DISABLED": 403,
    "SENSITIVE_TARGET": 403,
    "SESSION_NOT_FOUND": 404,
}


DASHBOARD_HTML = """<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Terminal MCP Sessions</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --green:#43d17c; --amber:#ffc857; --term-bg:#0a0e1a; --mono: ui-monospace,SFMono-Regular,Menlo,'DejaVu Sans Mono','Courier New',monospace; }
    * { box-sizing:border-box } body { margin:0; font:14px/1.5 var(--mono); background:var(--bg); color:var(--text) }
    header { display:flex; justify-content:space-between; gap:16px; align-items:center; padding:22px 28px; border-bottom:1px solid var(--line) }
    h1 { margin:0; font-size:20px } .muted { color:var(--muted) } .live { color:var(--green) }
    main { display:grid; grid-template-columns:minmax(240px,340px) 1fr; gap:18px; padding:18px; min-height:calc(100vh - 75px) }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden }
    .panel-title { padding:13px 16px; border-bottom:1px solid var(--line); color:var(--muted) }
    #sessions { padding:8px } button.session { width:100%; text-align:left; color:inherit; background:transparent; border:1px solid transparent; border-radius:8px; padding:11px; cursor:pointer }
    button.session:hover, button.session.active { background:#19243b; border-color:#344360 }
    .name { font-weight:700 } .meta { font-size:12px; color:var(--muted); margin-top:4px }
    .detail { display:grid; grid-template-rows:auto 1fr auto auto; min-width:0 } #summary { padding:14px 16px; border-bottom:1px solid var(--line) }
    .state-WAITING_INPUT { color:var(--amber) } .state-RUNNING { color:var(--green) }
    /* Terminal-style pane: a small chrome bar (title + follow/jump controls)
       above a dark, monospace, ANSI-rendering scrollback view. */
    .term { display:flex; flex-direction:column; min-height:0 }
    .term-bar { display:flex; align-items:center; gap:10px; padding:7px 12px; background:#0e1526; border-bottom:1px solid var(--line) }
    .term-dots { display:flex; gap:6px; flex:0 0 auto }
    .term-dots i { width:10px; height:10px; border-radius:50%; display:inline-block }
    .term-dots i.r { background:#ff5f57 } .term-dots i.y { background:#febc2e } .term-dots i.g { background:#28c840 }
    .term-title { color:var(--muted); font-size:12px; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap }
    .term-controls { display:flex; gap:8px; flex:0 0 auto }
    .term-btn { background:#19243b; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:5px 10px; font:12px var(--mono); cursor:pointer; white-space:nowrap }
    .term-btn:hover:not(:disabled) { background:#233252 }
    .term-btn:disabled { opacity:.5; cursor:not-allowed }
    .term-btn.paused { border-color:var(--amber); color:var(--amber) }
    #output { flex:1; margin:0; padding:14px 18px; overflow:auto; white-space:pre-wrap; word-break:break-word; line-height:1.45; font-family:var(--mono); background:var(--term-bg); color:#dce5f5 }
    #inputBar { display:flex; gap:8px; padding:12px 16px; border-top:1px solid var(--line) }
    #inputBar input[type=text] { flex:1; background:#0e1526; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:9px 11px; font:inherit }
    #inputBar button { background:#2b3f66; border:1px solid var(--line); border-radius:8px; color:var(--text); padding:9px 14px; cursor:pointer; font:inherit }
    #inputBar button:disabled { opacity:.5; cursor:not-allowed }
    #inputBar label { display:flex; align-items:center; gap:4px; color:var(--muted); font-size:12px; white-space:nowrap }
    #inputNote { padding:6px 16px 0; font-size:12px; color:var(--muted) }
    #inputNote.error { color:#ff6b6b }
    @media (max-width:760px) { main { grid-template-columns:1fr } .detail { min-height:55vh } }
  </style>
</head>
<body>
  <header><div><h1>Terminal MCP</h1><div class="muted">Whitelisted tmux session monitor</div></div><div><span class="live">● LIVE</span></div></header>
  <main>
    <section class="panel"><div class="panel-title">SESSIONS <span id="count"></span></div><div id="sessions"></div></section>
    <section class="panel detail">
      <div id="summary" class="muted">Chọn một session để xem output.</div>
      <div class="term">
        <div class="term-bar">
          <span class="term-dots"><i class="r"></i><i class="y"></i><i class="g"></i></span>
          <span class="term-title" id="termTitle"></span>
          <span class="term-controls">
            <button id="followToggle" class="term-btn" type="button" disabled>Auto-follow: ON</button>
            <button id="jumpBtn" class="term-btn" type="button" disabled>Jump to latest</button>
          </span>
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
  <script>
    let selected = null;
    let inputAllowed = false;
    let autoFollow = true;
    let lastRenderedSession = null;
    const sessionsEl = document.querySelector('#sessions');
    const outputEl = document.querySelector('#output');
    const summaryEl = document.querySelector('#summary');
    const termTitleEl = document.querySelector('#termTitle');
    const followToggleEl = document.querySelector('#followToggle');
    const jumpBtnEl = document.querySelector('#jumpBtn');
    const inputNoteEl = document.querySelector('#inputNote');
    const inputTextEl = document.querySelector('#inputText');
    const inputEnterEl = document.querySelector('#inputEnter');
    const inputSendEl = document.querySelector('#inputSend');
    const clean = value => String(value ?? '');

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
      termTitleEl.textContent = selected || '';
    }
    followToggleEl.onclick = () => {
      setAutoFollow(!autoFollow);
      if (autoFollow) outputEl.scrollTop = outputEl.scrollHeight;
    };
    jumpBtnEl.onclick = () => { setAutoFollow(true); outputEl.scrollTop = outputEl.scrollHeight; };
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
    async function sendInput() {
      if (!selected || !inputTextEl.value) return;
      inputSendEl.disabled = true;
      try {
        const response = await fetch('/dashboard/api/session/input', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({name: selected, text: inputTextEl.value, press_enter: inputEnterEl.checked}),
        });
        const data = await response.json();
        if (data.error) { setInputNote(`${data.error}${data.reason ? ': ' + data.reason : ''}`, true); }
        else { inputTextEl.value = ''; setInputNote(''); }
      } catch (error) {
        setInputNote('Không thể gửi: ' + error, true);
      } finally {
        refreshInputControls();
        await loadDetail();
      }
    }
    inputSendEl.onclick = sendInput;
    inputTextEl.addEventListener('keydown', event => { if (event.key === 'Enter') sendInput(); });
    async function loadSessions() {
      const response = await fetch('/dashboard/api/sessions', {cache:'no-store'});
      const data = await response.json(); const rows = data.sessions || [];
      document.querySelector('#count').textContent = `(${rows.length})`;
      sessionsEl.replaceChildren();
      for (const row of rows) {
        const button = document.createElement('button'); button.className = 'session' + (selected === row.name ? ' active' : '');
        const name = document.createElement('div'); name.className = 'name'; name.textContent = row.name;
        const meta = document.createElement('div'); meta.className = 'meta'; meta.textContent = `${row.windows} window · ${row.attached ? 'attached' : 'detached'}`;
        button.append(name, meta);
        button.onclick = () => { selected = row.name; inputAllowed = false; setAutoFollow(true); refreshInputControls(); refreshTermControls(); loadSessions(); loadDetail(); };
        sessionsEl.append(button);
      }
      if (selected && !rows.some(row => row.name === selected)) {
        selected = null; inputAllowed = false; refreshInputControls(); refreshTermControls();
        summaryEl.textContent = 'Session không còn tồn tại.'; outputEl.replaceChildren();
      }
    }
    async function loadDetail() {
      if (!selected) return;
      const response = await fetch(`/dashboard/api/session?name=${encodeURIComponent(selected)}`, {cache:'no-store'});
      const data = await response.json();
      if (data.error) {
        summaryEl.textContent = `${data.error}: ${selected}`; outputEl.replaceChildren();
        inputAllowed = false; refreshInputControls(); refreshTermControls();
        return;
      }
      summaryEl.replaceChildren();
      const strong = document.createElement('strong'); strong.textContent = selected + ' · ';
      // Session status (RUNNING / WAITING_INPUT / PLAN_APPROVAL / ... and its
      // reason) stays visible above the terminal pane exactly as before.
      const state = document.createElement('span'); state.className = `state-${clean(data.status.state)}`; state.textContent = clean(data.status.state);
      const reason = document.createElement('span'); reason.className = 'muted'; reason.textContent = ` — ${clean(data.status.reason)}`;
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
    async function refresh() { try { await loadSessions(); await loadDetail(); } catch (error) { summaryEl.textContent = 'Không thể tải dữ liệu: ' + error; } }
    refresh(); setInterval(refresh, 5000);
  </script>
</body>
</html>"""


def register_dashboard(server: MCPServer, terminal: TerminalService) -> None:
    @server.custom_route("/dashboard", methods=["GET"], include_in_schema=False)
    async def dashboard(_: Request) -> HTMLResponse:
        return HTMLResponse(
            DASHBOARD_HTML,
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    @server.custom_route("/dashboard/api/sessions", methods=["GET"], include_in_schema=False)
    async def sessions(_: Request) -> JSONResponse:
        return JSONResponse(terminal.terminal_list_sessions(), headers={"Cache-Control": "no-store"})

    @server.custom_route("/dashboard/api/session", methods=["GET"], include_in_schema=False)
    async def session_detail(request: Request) -> JSONResponse:
        name = request.query_params.get("name", "")
        status = terminal.terminal_status(name)
        if "error" in status:
            return JSONResponse(status, status_code=403 if status["error"] == "ACCESS_DENIED" else 404)
        # Uses config.default_tail_lines (already the project's one source of truth
        # for "how many recent lines" — see config.yaml) rather than a hardcoded
        # count. tmux capture-pane already returns that window oldest-line-first,
        # newest-line-last, so the dashboard renders it in natural chronological
        # order with no reordering needed. ansi=True keeps colour/style escape
        # sequences for the terminal-style renderer; it goes through the exact
        # same whitelist/permission guard as every other read, and through
        # redact_ansi_safe (see terminal_mcp/redaction.py) rather than the plain
        # redactor, so a secret can never survive because it was colour-coded.
        tail = terminal.terminal_tail(name, ansi=True)
        if "error" in tail:
            return JSONResponse(tail, status_code=404)
        input_allowed = (
            terminal.config.permissions.terminal_input
            and input_session_allowed(name, terminal.config)
        )
        return JSONResponse(
            {"session": name, "status": status, "tail": tail, "input_allowed": input_allowed},
            headers={"Cache-Control": "no-store"},
        )

    @server.custom_route("/dashboard/api/session/input", methods=["POST"], include_in_schema=False)
    async def session_input(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        text = body.get("text") if isinstance(body, dict) else None
        press_enter = bool(body.get("press_enter", False)) if isinstance(body, dict) else False
        if not isinstance(name, str) or not name or not isinstance(text, str) or not text:
            return JSONResponse({"error": "INVALID_REQUEST"}, status_code=400)
        result = terminal.terminal_send_text(name, text, press_enter=press_enter)
        status_code = 200
        if "error" in result:
            status_code = INPUT_ERROR_STATUS.get(result["error"], 400)
        return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})
