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
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a2d; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd; --green:#43d17c; --amber:#ffc857; }
    * { box-sizing:border-box } body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; background:var(--bg); color:var(--text) }
    header { display:flex; justify-content:space-between; gap:16px; align-items:center; padding:22px 28px; border-bottom:1px solid var(--line) }
    h1 { margin:0; font-size:20px } .muted { color:var(--muted) } .live { color:var(--green) }
    main { display:grid; grid-template-columns:minmax(240px,340px) 1fr; gap:18px; padding:18px; min-height:calc(100vh - 75px) }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; overflow:hidden }
    .panel-title { padding:13px 16px; border-bottom:1px solid var(--line); color:var(--muted) }
    #sessions { padding:8px } button.session { width:100%; text-align:left; color:inherit; background:transparent; border:1px solid transparent; border-radius:8px; padding:11px; cursor:pointer }
    button.session:hover, button.session.active { background:#19243b; border-color:#344360 }
    .name { font-weight:700 } .meta { font-size:12px; color:var(--muted); margin-top:4px }
    .detail { display:grid; grid-template-rows:auto 1fr; min-width:0 } #summary { padding:14px 16px; border-bottom:1px solid var(--line) }
    pre { margin:0; padding:18px; overflow:auto; white-space:pre-wrap; word-break:break-word; color:#dce5f5 }
    .state-WAITING_INPUT { color:var(--amber) } .state-RUNNING { color:var(--green) }
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
      <pre id="output"></pre>
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
    const sessionsEl = document.querySelector('#sessions');
    const outputEl = document.querySelector('#output');
    const summaryEl = document.querySelector('#summary');
    const inputNoteEl = document.querySelector('#inputNote');
    const inputTextEl = document.querySelector('#inputText');
    const inputEnterEl = document.querySelector('#inputEnter');
    const inputSendEl = document.querySelector('#inputSend');
    const clean = value => String(value ?? '');
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
        button.append(name, meta); button.onclick = () => { selected = row.name; inputAllowed = false; refreshInputControls(); loadSessions(); loadDetail(); }; sessionsEl.append(button);
      }
      if (selected && !rows.some(row => row.name === selected)) {
        selected = null; inputAllowed = false; refreshInputControls();
        summaryEl.textContent = 'Session không còn tồn tại.'; outputEl.textContent = '';
      }
    }
    async function loadDetail() {
      if (!selected) return;
      const response = await fetch(`/dashboard/api/session?name=${encodeURIComponent(selected)}`, {cache:'no-store'});
      const data = await response.json();
      if (data.error) { summaryEl.textContent = `${data.error}: ${selected}`; outputEl.textContent = ''; inputAllowed = false; refreshInputControls(); return; }
      summaryEl.replaceChildren();
      const strong = document.createElement('strong'); strong.textContent = selected + ' · ';
      const state = document.createElement('span'); state.className = `state-${clean(data.status.state)}`; state.textContent = clean(data.status.state);
      const reason = document.createElement('span'); reason.className = 'muted'; reason.textContent = ` — ${clean(data.status.reason)}`;
      summaryEl.append(strong, state, reason); outputEl.textContent = clean(data.tail.output);
      // Lines render oldest-first/newest-last (tmux's natural order); snap the
      // scroll position to the bottom so the newest output is visible without
      // manual scrolling through the whole window on every refresh.
      outputEl.scrollTop = outputEl.scrollHeight;
      inputAllowed = Boolean(data.input_allowed); refreshInputControls();
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
        # order with no reordering needed.
        tail = terminal.terminal_tail(name)
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
