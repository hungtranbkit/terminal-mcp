"""The /dashboard/office page -- "Văn phòng AI" (issue #67).

A pixel-art, top-down office where every character IS a live Terminal MCP
session (same rows as /dashboard/live; no invented staff -- the only fake
people appear with an explicit `?demo=1`, under a DEMO banner), next to the
AI Gateway Task Center tasks (task_center_reader.py) so that an agent going
idle never makes unfinished work disappear.

Rules that matter:
  * all art is drawn procedurally in this file -- no third-party sprite
    sheets (see docs/PIXEL_OFFICE.md for the licence survey);
  * every server string reaches the DOM through textContent, never HTML;
  * one fetch every POLL_MS, skipped while one is in flight or the tab is
    hidden; the animation loop stops while hidden and does not run at all
    under prefers-reduced-motion (or the in-page "Chuyển động: tắt");
  * the canvas is decoration over an accessible list that carries the same
    information, so keyboard and screen-reader users lose nothing;
  * IDLE / COMPOSER never reads as done: an idle agent with open work keeps a
    sticky note on the desk and an amber clipboard over its head.
"""

OFFICE_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>Văn phòng AI — Terminal MCP</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#0a0f1e; --panel:#111a2e; --panel2:#0d1527; --raise:#16213a; --line:#24314d; --line2:#30406a;
      --text:#eef2ff; --muted:#97a3bd; --dim:#6b7792;
      --green:#43d17c; --amber:#ffc857; --red:#ff6b6b; --blue:#5b8cff; --violet:#b48cff; --cyan:#5ec8ff;
      --grey:#8b93a7;
      --mono: ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono',monospace;
      --pixel: 'Silkscreen','Press Start 2P',var(--mono);
      --radius:12px;
    }
    * { box-sizing:border-box }
    html, body { background:var(--bg) }
    body { margin:0; color:var(--text); font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif }
    a { color:var(--blue); text-decoration:none } a:hover { text-decoration:underline }
    button { font:inherit; color:inherit }
    :focus-visible { outline:2px solid var(--cyan); outline-offset:2px }
    .sr { position:absolute !important; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap }

    .wrap { padding:10px max(16px, env(safe-area-inset-right)) 24px max(16px, env(safe-area-inset-left)); max-width:2200px; margin:0 auto }
    .demo-banner { background:repeating-linear-gradient(45deg,#4a3a0e,#4a3a0e 10px,#3d300b 10px,#3d300b 20px);
      color:#ffe7a3; border:1px solid #7a5d12; border-radius:10px; padding:8px 12px; margin-bottom:10px; font-weight:600 }

    /* header */
    .hd { display:flex; flex-wrap:wrap; align-items:center; gap:8px 14px; margin:2px 0 10px }
    .hd h1 { margin:0; font-size:18px; letter-spacing:.2px; display:flex; align-items:center; gap:8px; white-space:nowrap }
    .hd h1 .logo { width:22px; height:22px; image-rendering:pixelated }
    .live { font-size:12px; color:var(--muted); display:flex; align-items:center; gap:6px; white-space:nowrap }
    .dot { width:8px; height:8px; border-radius:50%; background:var(--green); box-shadow:0 0 0 0 rgba(67,209,124,.5) }
    .dot.stale { background:var(--red) }
    .dot.tick { animation:ping .7s ease-out }
    @keyframes ping { from { box-shadow:0 0 0 6px rgba(67,209,124,.35) } to { box-shadow:0 0 0 0 transparent } }
    .stats { display:flex; flex-wrap:wrap; gap:6px; flex:1; min-width:0 }
    @media (max-width:640px) {
      .hd { gap:8px }
      .hd h1 { font-size:17px }
      .stats { order:3; flex:1 1 100%; flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; padding-bottom:2px }
      .stats::-webkit-scrollbar { display:none }
      .tools { order:4; flex:1 1 100%; flex-wrap:nowrap }
      .tools .btn { padding:5px 8px; font-size:12.5px; min-height:32px }
      .live { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis }
    }
    .stat { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:999px; border:1px solid var(--line);
      background:var(--panel); font-size:12.5px; white-space:nowrap }
    .stat i { width:8px; height:8px; border-radius:2px; display:inline-block }
    .stat b { font-variant-numeric:tabular-nums }
    .tools { display:flex; gap:6px; align-items:center; flex-wrap:wrap }
    .btn { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:6px 10px; min-height:34px; cursor:pointer;
      font-size:13px; display:inline-flex; align-items:center; gap:6px; white-space:nowrap }
    .btn:hover { border-color:var(--line2); background:var(--raise) }
    .btn[aria-pressed=true] { border-color:var(--blue); background:#1a2a52 }
    .seg { display:inline-flex; border:1px solid var(--line); border-radius:8px; overflow:hidden }
    .seg .btn { border:0; border-radius:0; min-width:34px; justify-content:center }
    .seg .btn + .btn { border-left:1px solid var(--line) }

    /* must-not-miss strip */
    .alert-strip { display:none; align-items:center; gap:10px; flex-wrap:wrap; margin:0 0 10px; padding:8px 12px; border-radius:10px;
      background:linear-gradient(90deg,#3a2b07,#2a2108); border:1px solid #7a5d12; color:#ffe2a0; font-size:13px; cursor:pointer }
    .alert-strip.on { display:flex }
    @media (max-width:640px) {
      .alert-strip.on { display:block; font-size:12.5px; padding:7px 10px; line-height:1.35 }
      .alert-strip > span, .alert-strip > b { display:inline; margin-right:4px }
    }
    .alert-strip b { color:#fff }

    /* layout */
    .app { display:grid; gap:12px; grid-template-columns:minmax(0,1fr); align-items:start }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:var(--radius); min-width:0 }
    .panel > header { display:flex; align-items:center; gap:8px; padding:10px 12px; border-bottom:1px solid var(--line); flex-wrap:wrap }
    .panel > header h2 { margin:0; font-size:14px; letter-spacing:.3px }
    .tabs { display:none }
    @media (max-width:1023.98px) {
      .tabs { display:flex; gap:6px; margin-bottom:10px; position:sticky; top:0; z-index:5; background:var(--bg); padding:4px 0 }
      .tabs .btn { flex:1; justify-content:center }
      .app[data-tab=office] #tasksPanel { display:none }
      .app[data-tab=tasks] #stagePanel { display:none }
    }
    @media (min-width:1024px) {
      .app { grid-template-columns:minmax(300px,340px) minmax(0,1fr) }
      #tasksPanel { position:sticky; top:8px; max-height:calc(100vh - 70px); display:flex; flex-direction:column }
      #tasksPanel .scroll { overflow:auto; flex:1 }
    }
    @media (min-width:1600px) {
      .app { grid-template-columns:360px minmax(0,1fr) 400px }
      .app #drawer { position:sticky; top:8px; transform:none; max-height:calc(100vh - 70px); width:auto; inset:auto; display:flex }
      .app #drawer .close { display:none }
      .scrim { display:none !important }
    }

    /* stage */
    .stage-wrap { position:relative; padding:10px; }
    .canvas-box { position:relative; overflow:auto; border-radius:10px; background:#05070f; border:1px solid var(--line);
      max-height:calc(100vh - 150px); min-height:220px; overscroll-behavior:contain; touch-action:pan-x pan-y }
    canvas#office { display:block; image-rendering:pixelated; image-rendering:crisp-edges; margin:0 auto; cursor:default }
    canvas#office.hover { cursor:pointer }
    .tip { position:absolute; pointer-events:none; z-index:3; background:rgba(8,12,24,.94); border:1px solid var(--line2); border-radius:8px;
      padding:6px 9px; font-size:12px; max-width:260px; display:none; box-shadow:0 6px 18px rgba(0,0,0,.45) }
    .tip b { display:block; font-family:var(--mono); font-size:12px }
    .legend { display:flex; flex-wrap:wrap; gap:6px 14px; padding:8px 2px 0; font-size:12px; color:var(--muted) }
    .legend span { display:inline-flex; align-items:center; gap:6px }
    .legend i { width:10px; height:10px; border-radius:2px; display:inline-block }
    .empty-office { position:absolute; inset:auto 0 18px; text-align:center; color:var(--muted); font-size:13px; pointer-events:none }

    /* roster (accessible list) */
    details.roster { margin:10px 0 0; border-top:1px solid var(--line); padding-top:8px }
    details.roster > summary { cursor:pointer; color:var(--muted); font-size:13px; padding:4px 0; list-style:revert }
    .roster ul { list-style:none; margin:6px 0 0; padding:0; display:grid; gap:6px; grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr)) }
    .who { width:100%; text-align:left; background:var(--panel2); border:1px solid var(--line); border-left:4px solid var(--grey); border-radius:9px;
      padding:7px 9px; cursor:pointer; display:grid; gap:2px; min-height:44px }
    .who:hover { background:var(--raise) }
    .who[aria-current=true] { border-color:var(--cyan); box-shadow:0 0 0 1px var(--cyan) inset }
    .who .n { font:600 13px var(--mono); overflow-wrap:anywhere }
    .who .m { font-size:12px; color:var(--muted); overflow-wrap:anywhere }

    /* state colours (shared by pills, roster, legend) */
    .st-RUNNING { --st:var(--green) } .st-WAITING { --st:var(--amber) } .st-ERROR { --st:var(--red) }
    .st-DONE { --st:var(--violet) } .st-IDLE { --st:var(--cyan) } .st-UNKNOWN { --st:var(--grey) }
    .st-OFFLINE { --st:#59627a } .st-RESTRICTED { --st:#7d6b9c }
    .who[class*=st-] { border-left-color:var(--st) }
    .pill { display:inline-flex; align-items:center; gap:5px; border-radius:999px; padding:2px 8px; font-size:11.5px; font-weight:700;
      border:1px solid color-mix(in srgb, var(--st, var(--grey)) 60%, transparent); color:var(--st, var(--grey));
      background:color-mix(in srgb, var(--st, var(--grey)) 14%, transparent); white-space:nowrap }
    .pill::before { content:""; width:6px; height:6px; border-radius:50%; background:currentColor }

    /* tasks */
    .conn { font-size:11.5px; font-weight:700; border-radius:999px; padding:2px 8px; border:1px solid var(--line2); color:var(--muted) }
    .conn.connected { color:var(--green); border-color:#2d6b49 }
    .conn.not_configured, .conn.unauthorized, .conn.forbidden { color:var(--amber); border-color:#6b4f12 }
    .conn.error { color:var(--red); border-color:#6b2a2a }
    .src-note { font-size:11.5px; color:var(--dim); padding:0 12px 8px }
    .filters { display:flex; flex-wrap:wrap; gap:5px; padding:8px 12px }
    .chip { background:var(--panel2); border:1px solid var(--line); border-radius:999px; padding:4px 9px; font-size:12px; cursor:pointer; min-height:30px;
      display:inline-flex; align-items:center; gap:5px }
    .chip b { font-variant-numeric:tabular-nums; color:var(--muted) }
    .chip[aria-pressed=true] { border-color:var(--blue); background:#1a2a52 }
    .chip[aria-pressed=true] b { color:#fff }
    .chip.hot b { color:var(--amber) }
    .search { padding:0 12px 8px }
    .search input { width:100%; background:var(--panel2); border:1px solid var(--line); color:var(--text); border-radius:8px; padding:7px 10px;
      font-size:16px; min-height:36px }
    .tlist { list-style:none; margin:0; padding:0 12px 12px; display:grid; gap:8px }
    .task { background:var(--panel2); border:1px solid var(--line); border-left:4px solid var(--tc,var(--grey)); border-radius:10px; padding:8px 10px; display:grid; gap:5px; min-width:0 }
    .task.b-waiting_input { --tc:var(--amber) } .task.b-dispatch_uncertain { --tc:#ff9f43 } .task.b-blocked { --tc:var(--red) }
    .task.b-needs_review { --tc:var(--violet) } .task.b-unknown { --tc:var(--grey) } .task.b-stale { --tc:#a08a5a }
    .task.b-active { --tc:var(--green) } .task.b-verified_done { --tc:#2f7a57; opacity:.82 }
    .task .t { font-weight:650; overflow-wrap:anywhere; line-height:1.3 }
    .task .meta { font-size:11.5px; color:var(--muted); display:flex; flex-wrap:wrap; gap:4px 8px }
    .flags { display:flex; flex-wrap:wrap; gap:4px }
    .flag { font-size:11px; border-radius:6px; padding:1px 6px; background:#1d2742; border:1px solid var(--line2); color:#cdd6f0 }
    .flag.f-waiting_input, .flag.f-agent_idle_open { background:#3a2c07; border-color:#7a5d12; color:#ffd77a }
    .flag.f-dispatch_uncertain { background:#3a2410; border-color:#8a5420; color:#ffbf80 }
    .flag.f-blocked { background:#3a1414; border-color:#7a2b2b; color:#ffb1b1 }
    .flag.f-needs_review, .flag.f-done_without_evidence { background:#2a1d44; border-color:#5b3f8f; color:#d9c4ff }
    .flag.f-stale { background:#2e2a1d; border-color:#5f5536; color:#e3d6a8 }
    .flag.f-verified_done { background:#123425; border-color:#2d6b49; color:#9ff0c0 }
    .links { display:flex; flex-wrap:wrap; gap:4px }
    .lk { font-size:11.5px; border:1px solid var(--line2); border-radius:6px; padding:1px 6px; background:#121c33; white-space:nowrap }
    .lk.k-chat { border-color:#2d6b49 } .lk.k-pr { border-color:#5b3f8f } .lk.k-commit { font-family:var(--mono) }
    .task details { font-size:12.5px } .task details summary { cursor:pointer; color:var(--muted) }
    .task .req { white-space:pre-wrap; overflow-wrap:anywhere; margin:4px 0; color:#d9def0 }
    .task ol { margin:4px 0 0 18px; padding:0 }
    .sess-links { display:flex; flex-wrap:wrap; gap:4px }
    .sess-link { font:11.5px var(--mono); background:#0f1830; border:1px solid var(--line2); border-radius:6px; padding:1px 6px; cursor:pointer }
    .empty { margin:10px 12px 12px; padding:14px; border:1px dashed var(--line2); border-radius:10px; color:var(--muted); font-size:13px }
    .empty h3 { margin:0 0 4px; color:var(--amber); font-size:14px }
    .sub-h { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.6px; padding:10px 12px 4px; display:flex; gap:6px; align-items:center }
    .import { margin:0 12px 12px; border:1px solid var(--line); border-radius:10px; padding:8px 10px; font-size:12.5px; background:var(--panel2) }
    .import summary { cursor:pointer; font-weight:600 }
    .import .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-top:6px }
    .import ul { list-style:none; padding:0; margin:6px 0 0; max-height:220px; overflow:auto; display:grid; gap:3px }
    .import li { display:flex; gap:6px; align-items:baseline; font-size:12px }
    .import li .k { font-size:10.5px; font-weight:700; border-radius:4px; padding:0 5px }
    .import li .k.dup { background:#123425; color:#9ff0c0 } .import li .k.new { background:#3a2c07; color:#ffd77a }

    /* drawer */
    #drawer { position:fixed; z-index:20; top:0; right:0; bottom:0; width:min(440px,100vw); transform:translateX(105%); transition:transform .22s ease;
      display:flex; flex-direction:column; box-shadow:-12px 0 30px rgba(0,0,0,.45); border-radius:0 }
    #drawer.open { transform:none }
    @media (max-width:1023.98px) {
      #drawer { top:auto; left:0; width:100%; max-height:82vh; transform:translateY(105%); border-radius:16px 16px 0 0 }
    }
    @media (min-width:1600px) { #drawer { border-radius:var(--radius) } }
    .scrim { position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:19; display:none }
    .scrim.on { display:block }
    #drawer .body { overflow:auto; padding:12px; display:grid; gap:10px; align-content:start }
    .who-head { display:flex; gap:10px; align-items:center }
    .who-head canvas { width:48px; height:48px; image-rendering:pixelated; background:#0a1022; border:1px solid var(--line2); border-radius:10px; flex:none }
    .who-head .nm { font:700 15px var(--mono); overflow-wrap:anywhere }
    .facts { display:grid; grid-template-columns:auto minmax(0,1fr); gap:4px 10px; font-size:12.5px }
    .facts dt { color:var(--muted) } .facts dd { margin:0; overflow-wrap:anywhere; font-family:var(--mono); font-size:12px }
    .box { border:1px solid var(--line); border-radius:10px; padding:8px 10px; background:var(--panel2) }
    .box h3 { margin:0 0 4px; font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:.5px }
    .box.warn { border-color:#7a5d12; background:#2a2108 } .box.err { border-color:#7a2b2b; background:#2a1010 }
    pre.log { margin:0; max-height:300px; overflow:auto; background:#05080f; border:1px solid var(--line); border-radius:8px; padding:8px;
      font:11.5px/1.4 var(--mono); color:#cfd6e6; white-space:pre-wrap; overflow-wrap:anywhere }
    .drawer-empty { color:var(--muted); font-size:13px; padding:20px 12px; text-align:center }
    .actions { display:flex; gap:6px; flex-wrap:wrap }

    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { animation:none !important; transition:none !important }
    }
    html[data-motion=off] *, html[data-motion=off] *::before, html[data-motion=off] *::after { animation:none !important; transition:none !important }
    html[data-theme=day] { --bg:#eef1f8; --panel:#ffffff; --panel2:#f6f8fc; --raise:#eef2fb; --line:#d9dfec; --line2:#c3cce0; --text:#141a2b;
      --muted:#5b6680; --dim:#7a849b; color-scheme:light }
    html[data-theme=day] .btn[aria-pressed=true], html[data-theme=day] .chip[aria-pressed=true] { background:#dfe8ff }
    html[data-theme=day] .chip[aria-pressed=true] b { color:#14204a }
    html[data-theme=day] .task .req { color:#2a3350 }
    html[data-theme=day] .flag { background:#eef2fb; color:#2a3350 }
    html[data-theme=day] .lk, html[data-theme=day] .sess-link { background:#f1f4fb }
    html[data-theme=day] .tip { background:rgba(255,255,255,.97); color:#141a2b }
    html[data-theme=day] .box.warn { background:#fff6dd } html[data-theme=day] .box.err { background:#ffe9e9 }
    html[data-theme=day] .alert-strip { background:linear-gradient(90deg,#fff1c7,#ffe9b0); color:#5a4205 }
    html[data-theme=day] .alert-strip b { color:#2a1d00 }
  </style>
</head>
<body>
<div class="wrap">
  <div class="demo-banner" id="demoBanner" hidden>DEMO — dữ liệu giả lập để xem giao diện, KHÔNG phải phiên hay task thật. Bỏ <code>?demo=1</code> để xem dữ liệu live.</div>
  <div class="hd">
    <h1><canvas class="logo" id="logo" width="11" height="11" aria-hidden="true"></canvas>Văn phòng AI</h1>
    <span class="live" id="live" role="status" aria-live="polite"><span class="dot" id="dot"></span><span id="liveText">đang tải…</span></span>
    <div class="stats" id="stats" aria-label="Tổng hợp trạng thái phiên"></div>
    <div class="tools">
      <div class="seg" role="group" aria-label="Thu phóng">
        <button class="btn" id="zoomOut" type="button" aria-label="Thu nhỏ">−</button>
        <button class="btn" id="zoomFit" type="button" aria-label="Vừa khung">Vừa</button>
        <button class="btn" id="zoomIn" type="button" aria-label="Phóng to">+</button>
      </div>
      <button class="btn" id="themeBtn" type="button" aria-pressed="false">🌙 Đêm</button>
      <button class="btn" id="motionBtn" type="button" aria-pressed="true">Chuyển động: bật</button>
    </div>
  </div>
  <div class="alert-strip" id="alertStrip" role="button" tabindex="0" aria-label="Xem task chưa xong"></div>
  <div class="tabs" role="tablist" aria-label="Chế độ xem">
    <button class="btn" role="tab" id="tabOffice" aria-selected="true" aria-controls="stagePanel" type="button">🏢 Văn phòng</button>
    <button class="btn" role="tab" id="tabTasks" aria-selected="false" aria-controls="tasksPanel" type="button">📋 Tasks <b id="tabTaskCount"></b></button>
  </div>

  <div class="app" id="app" data-tab="office">
    <aside class="panel" id="tasksPanel" aria-labelledby="tasksTitle">
      <header>
        <h2 id="tasksTitle">ChatGPT Tasks</h2>
        <span class="conn" id="conn">…</span>
        <span class="spacer" style="flex:1"></span>
        <a class="lk" href="https://github.com/hungtranbkit/chatgpt-ai-gateway/issues/115" target="_blank" rel="noopener noreferrer" title="AI Gateway Task Center — registry gốc">#115</a>
      </header>
      <div class="scroll">
        <div class="src-note" id="connDetail"></div>
        <div class="filters" id="filters" role="group" aria-label="Lọc task"></div>
        <div class="search"><label class="sr" for="q">Tìm task</label><input id="q" type="search" placeholder="Tìm task, project, repo, issue…" autocomplete="off"></div>
        <div id="tcEmpty"></div>
        <ul class="tlist" id="tlist" aria-label="Task từ AI Gateway Task Center"></ul>
        <div class="sub-h" id="labelsHead">Task trên phiên Terminal <span class="conn connected" title="Nhãn task hiện có của Terminal MCP — không phải registry riêng">nhãn live</span></div>
        <ul class="tlist" id="llist" aria-label="Task gắn trên phiên Terminal"></ul>
        <details class="import" id="importBox">
          <summary>Nhập conversations.json (thủ công) · <span id="histState">Chưa kết nối</span></summary>
          <p id="histDetail" style="margin:6px 0;color:var(--muted)"></p>
          <div class="row">
            <label><input type="checkbox" id="consent"> Tôi đồng ý cho trình duyệt đọc file export này (không tải lên, không lưu).</label>
          </div>
          <div class="row">
            <label class="sr" for="convFile">Chọn conversations.json</label>
            <input type="file" id="convFile" accept=".json,application/json" disabled>
          </div>
          <div id="importOut" aria-live="polite"></div>
          <div class="row">
            <button class="btn" id="importConfirm" type="button" disabled title="Gateway chưa có endpoint import — không ghi dữ liệu">Xác nhận nhập (Chưa kết nối)</button>
            <button class="btn" id="importCopy" type="button" hidden>Sao chép danh sách chưa có</button>
          </div>
        </details>
      </div>
    </aside>

    <section class="panel" id="stagePanel" aria-labelledby="stageTitle">
      <header>
        <h2 id="stageTitle">Sàn văn phòng</h2>
        <span class="muted" style="color:var(--muted);font-size:12px" id="stageSub"></span>
      </header>
      <div class="stage-wrap">
        <div class="canvas-box" id="canvasBox">
          <canvas id="office" tabindex="0" role="img" aria-label="Văn phòng AI"></canvas>
          <div class="tip" id="tip" role="presentation"></div>
          <div class="empty-office" id="emptyOffice" hidden>Chưa có phiên Terminal MCP nào — văn phòng trống (không tạo nhân viên giả).</div>
        </div>
        <div class="legend" aria-hidden="true">
          <span><i style="background:var(--green)"></i>Đang làm · gõ phím</span>
          <span><i style="background:var(--amber)"></i>Chờ input · giơ tay</span>
          <span><i style="background:var(--red)"></i>Lỗi</span>
          <span><i style="background:var(--violet)"></i>Báo xong → phòng họp review</span>
          <span><i style="background:var(--cyan)"></i>Rảnh (IDLE/COMPOSER) → cafe · ngủ khi lâu</span>
          <span><i style="background:#59627a"></i>Offline</span>
          <span>📝 giấy vàng = task chưa xong</span>
        </div>
        <details class="roster" id="roster" open>
          <summary>Danh sách nhân sự (<span id="rosterCount">0</span>) — bản truy cập được của sàn văn phòng</summary>
          <ul id="rosterList"></ul>
        </details>
      </div>
    </section>

    <aside class="panel" id="drawer" aria-labelledby="drawerTitle" tabindex="-1">
      <header>
        <h2 id="drawerTitle">Chi tiết phiên</h2>
        <span style="flex:1"></span>
        <button class="btn close" id="drawerClose" type="button" aria-label="Đóng chi tiết">✕</button>
      </header>
      <div class="body" id="drawerBody"><div class="drawer-empty">Chọn một nhân vật trên sàn hoặc trong danh sách để xem phiên, log và task.</div></div>
    </aside>
  </div>
  <div class="scrim" id="scrim"></div>
</div>

<script>
(() => {
  'use strict';
  const API = '/dashboard/api/office';
  const POLL_MS = 4000;
  const T = 16;                        // world px per tile
  const SLEEP_AFTER_S = 20 * 60;       // idle this long -> asleep on the sofa
  const $ = (id) => document.getElementById(id);
  const qs = new URLSearchParams(location.search);
  const DEMO = qs.get('demo') === '1';
  const root = document.documentElement;

  const pref = (key, value) => {
    try {
      if (value === undefined) return localStorage.getItem('tmcp.office.' + key);
      localStorage.setItem('tmcp.office.' + key, value);
    } catch (e) { /* private mode: preferences are a convenience only */ }
    return null;
  };
  const mqReduce = window.matchMedia ? matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };
  let motionOff = pref('motion') ? pref('motion') === 'off' : mqReduce.matches;
  let theme = pref('theme') || ((window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches) ? 'day' : 'night');
  let zoomStep = Number(pref('zoom') || 0);   // 0 = fit, +/- steps

  const S = {
    data: null, sessions: [], byKey: new Map(), tasks: [], tasksBySession: {},
    selected: qs.get('session') || null, filter: 'unfinished', search: '',
    lastOk: 0, inflight: false, failures: 0, layout: null, agents: new Map(), first: true,
  };

  const STATE_LABEL = {
    RUNNING: 'Đang làm', WAITING: 'Chờ input', ERROR: 'Lỗi', DONE: 'Báo xong · cần review',
    IDLE: 'Rảnh (IDLE/COMPOSER) — chưa phải xong', UNKNOWN: 'Không rõ', OFFLINE: 'Offline', RESTRICTED: 'Hạn chế quyền đọc',
  };
  const STATE_SHORT = { RUNNING: 'Đang làm', WAITING: 'Chờ input', ERROR: 'Lỗi', DONE: 'Cần review', IDLE: 'Rảnh',
    UNKNOWN: 'Không rõ', OFFLINE: 'Offline', RESTRICTED: 'Hạn chế' };
  const STATE_ORDER = ['RUNNING', 'WAITING', 'ERROR', 'DONE', 'IDLE', 'UNKNOWN', 'RESTRICTED', 'OFFLINE'];
  const STATE_COLOR = { RUNNING: '#43d17c', WAITING: '#ffc857', ERROR: '#ff6b6b', DONE: '#b48cff', IDLE: '#5ec8ff',
    UNKNOWN: '#8b93a7', OFFLINE: '#59627a', RESTRICTED: '#7d6b9c' };

  // ---------------------------------------------------------------- helpers
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function ago(epoch) {
    if (!epoch) return '—';
    const s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 60) return Math.round(s) + 's trước';
    if (s < 3600) return Math.round(s / 60) + ' phút trước';
    if (s < 86400) return Math.round(s / 3600) + ' giờ trước';
    return Math.round(s / 86400) + ' ngày trước';
  }
  function abs(epoch) { return epoch ? new Date(epoch * 1000).toLocaleString('vi-VN') : ''; }
  function hash(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function lastSeen(s) { return s.last_activity_at || s.finished_at || s.activated_at || s.created_at || null; }
  function safeUrl(url) { try { const u = new URL(url); return (u.protocol === 'https:' || u.protocol === 'http:') ? u.href : null; } catch (e) { return null; } }

  // ------------------------------------------------------------- palettes
  const PAL = {
    night: {
      sky: '#121a3a', sky2: '#1d2752', star: '#dfe6ff', wall: '#262c48', wallTop: '#323a60', trim: '#171b31', base: '#1b2038',
      floorA: '#3a2e29', floorB: '#41342e', seam: '#2c231f', cafeA: '#2b3638', cafeB: '#334043', carpet: '#2a2e58', carpetEdge: '#3a3f75',
      desk: '#7a4f33', deskTop: '#9c6844', deskEdge: '#55361f', monitor: '#1a1f2e', bezel: '#2b3247', screenOn: '#4fb8ff', screenOff: '#11141e',
      chair: '#394166', chair2: '#2a3150', plant: '#3fa66b', plant2: '#2c7d4f', pot: '#a85a37', counter: '#5b3f31', counterTop: '#c09772',
      sofa: '#6a3f5e', sofa2: '#53304a', table: '#8a6a52', board: '#e9ecf5', boardEdge: '#8a93ad', glow: 'rgba(255,214,140,.10)',
      shadow: 'rgba(0,0,0,.35)', label: '#e9edff', labelBg: 'rgba(10,14,30,.78)', partition: '#20253f',
      rugs: ['#2f3d6d', '#2f5a4e', '#5a3c62', '#5e4b2f', '#2f5163', '#633645'], night: true,
    },
    day: {
      sky: '#8fd0ff', sky2: '#c4e8ff', star: '#ffffff', wall: '#c9cfe0', wallTop: '#dfe3ef', trim: '#9aa3bd', base: '#b6bdd2',
      floorA: '#c99d74', floorB: '#d2a77e', seam: '#b48861', cafeA: '#d9d2c4', cafeB: '#e4ddd0', carpet: '#7f8fd6', carpetEdge: '#6b7bc4',
      desk: '#a8714a', deskTop: '#c88f62', deskEdge: '#7a5032', monitor: '#2a3043', bezel: '#3b4460', screenOn: '#7fd0ff', screenOff: '#232838',
      chair: '#5a6aa8', chair2: '#465590', plant: '#4fbf7a', plant2: '#36985c', pot: '#c46a42', counter: '#7a5440', counterTop: '#e6c39c',
      sofa: '#b0577f', sofa2: '#8f4366', table: '#a98466', board: '#ffffff', boardEdge: '#9aa3bd', glow: 'rgba(255,255,255,0)',
      shadow: 'rgba(40,30,20,.22)', label: '#18203a', labelBg: 'rgba(255,255,255,.86)', partition: '#a9b1c8',
      rugs: ['#9fb1f0', '#9fd8c0', '#d6b0e0', '#ecd09a', '#a8d4e8', '#f0b0bd'], night: false,
    },
  };
  const SKIN = ['#f2c9a0', '#e0ac7e', '#c68b5e', '#9b6640', '#7a4b2c', '#f5d6b8'];
  const HAIR = ['#2a1d16', '#4a2f1f', '#7a4a24', '#d9a441', '#1c1c24', '#a33b2b', '#6b6f80', '#e8e2d0'];
  const SHIRT = ['#4f7cff', '#43b97f', '#e0645c', '#c27bd8', '#f0a33c', '#3fb6c6', '#7d8cff', '#e2789f', '#5aa469', '#d9b23c'];
  const PANTS = ['#2d3550', '#3b2f2a', '#26323a', '#3a3a46'];

  function looks(key) {
    const h = hash(key || '?');
    return { skin: SKIN[h % SKIN.length], hair: HAIR[(h >>> 3) % HAIR.length], shirt: SHIRT[(h >>> 7) % SHIRT.length],
      pants: PANTS[(h >>> 11) % PANTS.length], style: (h >>> 13) % 4, seed: h };
  }

  // --------------------------------------------------------------- layout
  // World is measured in tiles. Office (desk pods, one per node) on the left
  // or top; meeting room and cafe on the right or below. Recomputed only when
  // the session set or the stage width changes, never per frame.
  function groupByNode(sessions, unreachable) {
    const groups = new Map();
    for (const s of sessions) {
      const id = s.node_id || 'local';
      if (!groups.has(id)) groups.set(id, { id, name: s.node_name || id, sessions: [], unreachable: false });
      groups.get(id).sessions.push(s);
    }
    for (const u of unreachable || []) {
      const id = typeof u === 'string' ? u : (u.node_id || u.id || '?');
      if (!groups.has(id)) groups.set(id, { id, name: (u && u.node_name) || id, sessions: [], unreachable: true });
      else groups.get(id).unreachable = true;
    }
    const list = [...groups.values()];
    list.forEach(g => g.sessions.sort((a, b) => String(a.session).localeCompare(String(b.session))));
    list.sort((a, b) => (b.sessions.length - a.sessions.length) || a.name.localeCompare(b.name));
    return list;
  }

  function computeLayout(widthTiles, sessions, unreachable) {
    const portrait = widthTiles < 26;
    const side = 12;
    const officeW = portrait ? widthTiles : widthTiles - side;
    const deskW = 3, rowH = 4;
    const perRow = Math.max(1, Math.floor((officeW - 2) / deskW));
    const groups = groupByNode(sessions, unreachable);
    const pods = []; const desks = new Map();
    let y = 3;
    groups.forEach((g, gi) => {
      const n = Math.max(1, g.sessions.length);
      const cols = Math.min(perRow, n);
      const rows = Math.ceil(n / perRow);
      const podW = Math.max(cols * deskW + 1, Math.min(officeW - 2, 9));
      const pod = { id: g.id, name: g.name, count: g.sessions.length, unreachable: g.unreachable,
        x: 1, y, w: podW, h: 1 + rows * rowH, rug: gi };
      pods.push(pod);
      g.sessions.forEach((s, i) => {
        const c = i % perRow, r = Math.floor(i / perRow);
        const dx = pod.x + 0.5 + c * deskW, dy = y + 1 + r * rowH;
        desks.set(s.key, { key: s.key, x: dx * T, y: dy * T, w: (deskW - 0.5) * T, h: T + 6,
          seat: { x: (dx + 1.25) * T, y: (dy + 2) * T + 6 }, pod });
      });
      y += pod.h + 1;
    });
    const officeH = Math.max(y + 1, portrait ? 10 : 22);
    let W, H, office, meeting, cafe, doors;
    if (portrait) {
      const half = Math.floor(widthTiles / 2);
      const restH = 11;
      W = widthTiles; H = officeH + restH;
      office = { x: 0, y: 2, w: W, h: officeH - 2 };
      meeting = { x: 0, y: officeH, w: half, h: restH };
      cafe = { x: half, y: officeH, w: W - half, h: restH };
      doors = { 'office|meeting': { x: Math.floor(half / 2), y: officeH, axis: 'h' },
        'office|cafe': { x: half + Math.floor((W - half) / 2), y: officeH, axis: 'h' },
        'meeting|cafe': { x: half, y: officeH + Math.floor(restH / 2) + 1, axis: 'v' } };
    } else {
      W = widthTiles; H = Math.max(officeH, 22);
      const mH = 10;
      office = { x: 0, y: 2, w: officeW, h: H - 2 };
      meeting = { x: officeW, y: 2, w: side, h: mH };
      cafe = { x: officeW, y: 2 + mH, w: side, h: H - 2 - mH };
      doors = { 'office|meeting': { x: officeW, y: 2 + Math.floor(mH / 2) + 1, axis: 'v' },
        'office|cafe': { x: officeW, y: 2 + mH + Math.min(5, Math.floor((H - 2 - mH) / 2)), axis: 'v' },
        'meeting|cafe': { x: officeW + Math.floor(side / 2), y: 2 + mH, axis: 'h' } };
    }
    // Furniture spots inside the meeting room and cafe, in px.
    const px = r => ({ x: r.x * T, y: r.y * T, w: r.w * T, h: r.h * T });
    const M = px(meeting), C = px(cafe);
    const table = { x: M.x + 2 * T, y: M.y + 4 * T, w: M.w - 4 * T, h: 2 * T };
    const meetSeats = [];
    for (let i = 0; i < Math.max(2, Math.floor(table.w / (1.5 * T))); i++) {
      const sx = table.x + T * 0.75 + i * 1.5 * T;
      meetSeats.push({ x: sx, y: table.y - 2, face: 'down' });
      meetSeats.push({ x: sx, y: table.y + table.h + 12, face: 'up' });
    }
    const counter = { x: C.x + T, y: C.y + T, w: Math.min(6 * T, C.w - 2 * T), h: T + 4 };
    const sofa = { x: C.x + C.w - 5 * T, y: C.y + C.h - 2.5 * T, w: 4 * T, h: T + 6 };
    const cafeTables = [];
    const ctCols = Math.max(1, Math.floor((C.w - 2 * T) / (4 * T)));
    const ctRows = Math.max(1, Math.floor((C.h - 6 * T) / (3.5 * T)));
    for (let r = 0; r < ctRows; r++) for (let c = 0; c < ctCols; c++) {
      cafeTables.push({ x: C.x + 2 * T + c * 4 * T, y: C.y + 3.5 * T + r * 3.5 * T });
    }
    const sleepSpots = [];
    for (let i = 0; i < 3; i++) sleepSpots.push({ x: sofa.x + 8 + i * 20, y: sofa.y + 12, face: 'down', sofa: true });
    cafeTables.forEach(t => { sleepSpots.push({ x: t.x - 6, y: t.y + 10, face: 'down' }); sleepSpots.push({ x: t.x + 22, y: t.y + 10, face: 'down' }); });
    return { W, H, portrait, office, meeting, cafe, doors, pods, desks, M, C, table, meetSeats, counter, sofa, cafeTables, sleepSpots,
      wander: { x: C.x + T * 1.2, y: C.y + T * 3, w: C.w - T * 2.4, h: Math.max(T, C.h - T * 5.5) } };
  }

  function zoneOf(L, x, y) {
    const inR = (r) => x >= r.x * T && x < (r.x + r.w) * T && y >= r.y * T - 2 * T && y < (r.y + r.h) * T;
    if (inR(L.meeting)) return 'meeting';
    if (inR(L.cafe)) return 'cafe';
    return 'office';
  }
  function doorPath(L, from, to) {
    if (from === to) return [];
    const key = [from, to].sort().join('|');
    const d = L.doors[key] || L.doors[from + '|' + to] || L.doors[to + '|' + from];
    if (!d) return [];
    const dx = d.x * T, dy = d.y * T;
    if (d.axis === 'v') {
      const side = (z) => (z === 'office' ? -1 : 1) * (L.portrait ? (z === 'meeting' ? -1 : 1) : 1);
      return [{ x: dx + side(from) * 10, y: dy }, { x: dx + side(to) * 10, y: dy }];
    }
    const upper = (z) => L.portrait ? z === 'office' : z === 'meeting';
    return [{ x: dx, y: dy + (upper(from) ? -10 : 10) }, { x: dx, y: dy + (upper(to) ? -10 : 10) }];
  }

  // ---------------------------------------------------------- agent model
  function idleSeconds(s) { const t = lastSeen(s); return t ? Date.now() / 1000 - t : Infinity; }
  function openWork(s) {
    const linked = (S.tasksBySession[s.key] || []).length;
    const label = s.current_task && (s.current_task.summary || s.current_task.task_id || s.current_task.summary_withheld);
    const durable = s.task && s.task.status && !/^(COMPLETED|DONE|CANCELLED|SKIPPED|VERIFIED_DONE)$/i.test(String(s.task.status));
    return linked > 0 || !!label || !!durable;
  }

  function plan(L) {
    // Decide every agent's destination for the current data.
    const idle = [], review = [];
    for (const s of S.sessions) {
      if (s.state === 'IDLE') idle.push(s); else if (s.state === 'DONE') review.push(s);
    }
    const targets = new Map();
    let sleepIdx = 0, wanderIdx = 0;
    idle.sort((a, b) => idleSeconds(b) - idleSeconds(a)).forEach(s => {
      const asleep = idleSeconds(s) > SLEEP_AFTER_S;
      if (asleep && sleepIdx < L.sleepSpots.length) {
        const spot = L.sleepSpots[sleepIdx++];
        targets.set(s.key, { zone: 'cafe', x: spot.x, y: spot.y, pose: 'sleep', sofa: !!spot.sofa });
      } else {
        const h = hash(s.key + wanderIdx++);
        const w = L.wander;
        targets.set(s.key, { zone: 'cafe', x: w.x + (h % 1000) / 1000 * w.w, y: w.y + ((h >>> 10) % 1000) / 1000 * w.h, pose: 'coffee', wander: true });
      }
    });
    review.forEach((s, i) => {
      const seat = L.meetSeats[i % L.meetSeats.length];
      const extra = Math.floor(i / L.meetSeats.length);
      targets.set(s.key, { zone: 'meeting', x: seat.x + extra * 6, y: seat.y + extra * 4, pose: 'review', face: seat.face });
    });
    for (const s of S.sessions) {
      if (targets.has(s.key)) continue;
      const d = L.desks.get(s.key);
      if (!d) continue;
      const pose = { RUNNING: 'type', WAITING: 'raise', ERROR: 'alert', OFFLINE: 'ghost', RESTRICTED: 'locked' }[s.state] || 'sit';
      targets.set(s.key, { zone: 'office', x: d.seat.x, y: d.seat.y, pose, desk: true });
    }
    return targets;
  }

  function syncAgents(firstLoad) {
    const L = S.layout; if (!L) return;
    const targets = plan(L);
    const seen = new Set();
    for (const s of S.sessions) {
      const t = targets.get(s.key); if (!t) continue;
      seen.add(s.key);
      let a = S.agents.get(s.key);
      if (!a) {
        const enter = !firstLoad && !motionOff;
        const start = enter ? { x: L.office.x * T + T * 1.5, y: (L.office.y + L.office.h) * T - T } : t;
        a = { key: s.key, x: start.x, y: start.y, path: [], look: looks(s.key), pose: t.pose, wait: 0, walkT: 0, face: 'down' };
        S.agents.set(s.key, a);
      }
      a.s = s; a.target = t;
      if (motionOff) { a.x = t.x; a.y = t.y; a.path = []; continue; }
      const goalChanged = !a.goal || Math.abs(a.goal.x - t.x) > 1 || Math.abs(a.goal.y - t.y) > 1 || a.goal.zone !== t.zone;
      if (goalChanged && !(t.wander && a.goal && a.goal.zone === 'cafe' && zoneOf(L, a.x, a.y) === 'cafe')) {
        const from = zoneOf(L, a.x, a.y);
        a.path = [...doorPath(L, from, t.zone), { x: t.x, y: t.y }];
        a.goal = { x: t.x, y: t.y, zone: t.zone };
      }
    }
    for (const key of [...S.agents.keys()]) if (!seen.has(key)) S.agents.delete(key);
  }

  function stepAgents(dt) {
    const L = S.layout; if (!L) return;
    const speed = 34;
    for (const a of S.agents.values()) {
      if (a.path.length) {
        const p = a.path[0];
        const dx = p.x - a.x, dy = p.y - a.y, dist = Math.hypot(dx, dy);
        const step = speed * dt;
        if (dist <= step) { a.x = p.x; a.y = p.y; a.path.shift(); }
        else { a.x += dx / dist * step; a.y += dy / dist * step; }
        a.walkT += dt;
        a.face = Math.abs(dx) > Math.abs(dy) ? (dx < 0 ? 'left' : 'right') : (dy < 0 ? 'up' : 'down');
        a.walking = true;
      } else {
        a.walking = false;
        if (a.target && a.target.wander) {
          a.wait -= dt;
          if (a.wait <= 0) {
            const w = L.wander; const r1 = Math.random(), r2 = Math.random();
            a.path = [{ x: w.x + r1 * w.w, y: w.y + r2 * w.h }];
            a.wait = 3 + Math.random() * 5;
          }
        }
      }
    }
  }

  // -------------------------------------------------------------- drawing
  const canvas = $('office');
  const ctx = canvas.getContext('2d');
  const world = document.createElement('canvas');
  const wctx = world.getContext('2d');
  const bg = document.createElement('canvas');
  const bctx = bg.getContext('2d');
  let scale = 2, dpr = 1, bgDirty = true;

  function R(c, x, y, w, h, color) { c.fillStyle = color; c.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h)); }

  function drawPlant(c, x, y, P, big) {
    R(c, x - 4, y - 4, 8, 6, P.pot); R(c, x - 5, y - 5, 10, 2, '#7a3f24');
    const leaves = big ? [[-6, -14, 5, 9], [1, -16, 5, 11], [-2, -20, 4, 8], [-8, -9, 4, 5], [4, -10, 4, 5]] : [[-5, -11, 4, 7], [1, -12, 4, 8], [-2, -15, 4, 6]];
    leaves.forEach(([lx, ly, w, h], i) => R(c, x + lx, y + ly, w, h, i % 2 ? P.plant2 : P.plant));
  }
  function drawWindow(c, x, y, w, P, t) {
    R(c, x, y, w, T, P.trim); R(c, x + 2, y + 2, w - 4, T - 4, P.sky);
    R(c, x + 2, y + 2, w - 4, 4, P.sky2);
    if (P.night) { [[5, 6], [w - 9, 5], [Math.floor(w / 2), 9]].forEach(([sx, sy]) => R(c, x + sx, y + sy, 1, 1, P.star)); R(c, x + w - 8, y + 4, 3, 3, '#f5f0c8'); }
    else { R(c, x + 6, y + 8, 7, 2, '#ffffff'); R(c, x + 8, y + 7, 4, 1, '#ffffff'); }
    R(c, x + Math.floor(w / 2) - 1, y + 2, 2, T - 4, P.trim);
  }

  function drawBackground() {
    const L = S.layout, P = PAL[theme];
    bg.width = L.W * T; bg.height = L.H * T;
    const c = bctx;
    c.clearRect(0, 0, bg.width, bg.height);
    // floors
    for (let ty = 0; ty < L.H; ty++) for (let tx = 0; tx < L.W; tx++) {
      const x = tx * T, y = ty * T;
      const z = zoneOf(L, x + 1, y + 1);
      if (z === 'cafe') R(c, x, y, T, T, (tx + ty) % 2 ? P.cafeA : P.cafeB);
      else if (z === 'meeting') R(c, x, y, T, T, P.carpet);
      else { R(c, x, y, T, T, ty % 2 ? P.floorA : P.floorB); R(c, x + ((ty * 7) % T), y, 1, T, P.seam); R(c, x, y + T - 1, T, 1, P.seam); }
    }
    // meeting carpet border
    const M = L.M;
    c.strokeStyle = P.carpetEdge; c.lineWidth = 2; c.strokeRect(M.x + T + 1, M.y + T * 2 + 1, M.w - 2 * T - 2, M.h - 3 * T - 2);
    // top wall + windows + bookshelves
    R(c, 0, 0, L.W * T, 2 * T, P.wall); R(c, 0, 0, L.W * T, 4, P.wallTop); R(c, 0, 2 * T - 3, L.W * T, 3, P.base);
    for (let x = T; x < L.office.w * T - 3 * T; x += 7 * T) drawWindow(c, x, 6, 3 * T, P);
    for (let x = 4 * T + 8; x < L.office.w * T - 3 * T; x += 7 * T) { // bookshelf
      R(c, x, 4, 2 * T, 2 * T - 6, '#5a3a26'); for (let r = 0; r < 3; r++) for (let b = 0; b < 6; b++) R(c, x + 2 + b * 5, 7 + r * 8, 4, 6, SHIRT[(b + r * 3) % SHIRT.length]);
    }
    // partitions
    const wallV = (x, y0, y1, door) => { for (let y = y0; y < y1; y += 2) { if (door && y >= door.y * T - 14 && y <= door.y * T + 12) continue; R(c, x - 2, y, 4, 2, P.partition); } };
    const wallH = (y, x0, x1, door) => { for (let x = x0; x < x1; x += 2) { if (door && x >= door.x * T - 14 && x <= door.x * T + 14) continue; R(c, x, y - 2, 2, 4, P.partition); } };
    if (L.portrait) {
      wallH(L.meeting.y * T, 0, L.W * T, null);
      for (const k of ['office|meeting', 'office|cafe']) { const d = L.doors[k]; R(c, d.x * T - 14, d.y * T - 2, 28, 4, P.cafeA); }
      wallV(L.cafe.x * T, L.cafe.y * T, L.H * T, L.doors['meeting|cafe']);
    } else {
      wallV(L.meeting.x * T, 2 * T, L.H * T, null);
      for (const k of ['office|meeting', 'office|cafe']) { const d = L.doors[k]; R(c, d.x * T - 2, d.y * T - 14, 4, 28, P.floorB); }
      wallH(L.cafe.y * T, L.meeting.x * T, L.W * T, L.doors['meeting|cafe']);
    }
    // pods: rug + name plate
    L.pods.forEach(p => {
      const rx = p.x * T, ry = p.y * T, rw = p.w * T, rh = p.h * T;
      c.globalAlpha = P.night ? 0.55 : 0.5; R(c, rx, ry + 6, rw, rh - 6, P.rugs[p.rug % P.rugs.length]); c.globalAlpha = 1;
      if (p.unreachable) { c.strokeStyle = '#ff6b6b'; c.setLineDash([4, 3]); c.strokeRect(rx + .5, ry + 6.5, rw - 1, rh - 7); c.setLineDash([]); }
    });
    // meeting room: table, whiteboard, plants
    const tb = L.table;
    R(c, tb.x + 2, tb.y + 4, tb.w, tb.h, P.shadow); R(c, tb.x, tb.y, tb.w, tb.h, P.table); R(c, tb.x, tb.y, tb.w, 3, P.deskTop);
    for (let i = 0; i < 3; i++) R(c, tb.x + 8 + i * 22, tb.y + 9, 8, 6, '#f2f2f2');
    R(c, M.x + 2 * T, M.y + 4, Math.min(5 * T, M.w - 4 * T), T + 6, P.boardEdge); R(c, M.x + 2 * T + 2, M.y + 6, Math.min(5 * T, M.w - 4 * T) - 4, T + 2, P.board);
    R(c, M.x + 2 * T + 6, M.y + 10, 20, 2, '#4f7cff'); R(c, M.x + 2 * T + 6, M.y + 15, 30, 2, '#e0645c'); R(c, M.x + 2 * T + 40, M.y + 9, 10, 8, '#43b97f');
    drawPlant(c, M.x + M.w - T, M.y + 2 * T + 4, P, true);
    // cafe: counter, coffee machine, tables, sofa, plants
    const C = L.C, k = L.counter;
    R(c, k.x + 2, k.y + 4, k.w, k.h, P.shadow); R(c, k.x, k.y, k.w, k.h, P.counter); R(c, k.x, k.y, k.w, 5, P.counterTop);
    R(c, k.x + 6, k.y - 10, 12, 14, '#2a2d36'); R(c, k.x + 8, k.y - 8, 8, 3, '#ff6b6b'); R(c, k.x + 10, k.y - 2, 4, 3, '#d8d8d8');
    R(c, k.x + 26, k.y - 5, 6, 7, '#f2f2f2'); R(c, k.x + 34, k.y - 5, 6, 7, '#f2f2f2'); R(c, k.x + 50, k.y - 6, 14, 8, '#e0a85a');
    L.cafeTables.forEach(t => { R(c, t.x + 2, t.y + 3, 18, 14, P.shadow); R(c, t.x, t.y, 18, 14, P.table); R(c, t.x, t.y, 18, 3, P.deskTop); R(c, t.x + 6, t.y + 4, 4, 4, '#f2f2f2'); });
    const so = L.sofa;
    R(c, so.x + 2, so.y + 3, so.w, so.h, P.shadow); R(c, so.x, so.y - 6, so.w, 8, P.sofa2); R(c, so.x, so.y, so.w, so.h, P.sofa);
    R(c, so.x - 3, so.y - 4, 5, so.h + 4, P.sofa2); R(c, so.x + so.w - 2, so.y - 4, 5, so.h + 4, P.sofa2);
    drawPlant(c, C.x + C.w - T, C.y + T + 6, P, true); drawPlant(c, C.x + T, C.y + C.h - T, P, false);
    // break corner along the office's bottom wall: rug, water cooler, printer, cabinets
    const ob = (L.office.y + L.office.h) * T;
    const lastPod = L.pods.length ? L.pods[L.pods.length - 1] : null;
    const freeTop = lastPod ? (lastPod.y + lastPod.h + 1) * T : 4 * T;
    if (ob - freeTop >= 4 * T) {
      const rw = Math.min(L.office.w * T - 6 * T, 12 * T), rx = (L.office.w * T - rw) / 2, ry = ob - 3.5 * T;
      R(c, rx, ry, rw, 2 * T, P.rugs[1]); R(c, rx + 3, ry + 3, rw - 6, 2 * T - 6, P.rugs[0]);
      for (let i = 0; i < 3; i++) { R(c, rx + 12 + i * 5 * T, ry - 2, 3 * T, T - 2, P.sofa2); R(c, rx + 12 + i * 5 * T, ry + 4, 3 * T, T - 6, P.sofa); }
      const cx = 3 * T, cy = ob - 2 * T;
      R(c, cx, cy - 14, 12, 18, '#d8dde8'); R(c, cx + 2, cy - 22, 8, 9, '#7fc7ff'); R(c, cx + 3, cy - 20, 2, 5, '#bfe6ff'); R(c, cx + 4, cy - 4, 4, 2, '#3b6cff');
      const px2 = L.office.w * T - 5 * T, py2 = ob - 2 * T;
      R(c, px2 + 2, py2 - 6, 24, 12, P.shadow); R(c, px2, py2 - 10, 24, 12, '#c9ced9'); R(c, px2 + 3, py2 - 13, 18, 4, '#f4f4f4'); R(c, px2 + 4, py2 - 6, 6, 2, '#43d17c');
      for (let i = 0; i < 2; i++) { const fx = L.office.w * T - 2 * T - 4 + i * 0, fy = freeTop + 6 + i * 22; R(c, fx, fy, 18, 20, '#7c8499'); R(c, fx + 2, fy + 4, 14, 1, '#5b6378'); R(c, fx + 2, fy + 12, 14, 1, '#5b6378'); R(c, fx + 7, fy + 6, 4, 2, '#d0d4de'); }
    }
    drawPlant(c, L.office.x * T + T, (L.office.y + L.office.h) * T - 6, P, true);
    if (!L.portrait) drawPlant(c, L.office.w * T - T, (L.office.y + L.office.h) * T - 6, P, false);
    bgDirty = false;
  }

  function drawDesk(c, d, s, a, t, P) {
    const x = d.x, y = d.y, w = d.w;
    const atDesk = a && a.target && a.target.desk && !a.walking;
    R(c, x + 2, y + 5, w, T + 2, P.shadow);
    R(c, x, y + 4, w, T, P.desk); R(c, x, y + 4, w, 3, P.deskTop); R(c, x, y + 4 + T - 2, w, 2, P.deskEdge);
    // monitor
    const mx = x + w / 2 - 8, my = y - 6;
    R(c, mx + 6, my + 12, 4, 4, P.bezel); R(c, mx, my, 16, 12, P.bezel);
    let scr = P.screenOff;
    const st = s ? s.state : 'OFFLINE';
    if (st === 'RUNNING' && atDesk) scr = P.screenOn;
    else if (st === 'WAITING') scr = (Math.floor(t * 2) % 2) ? '#ffc857' : '#b58a2e';
    else if (st === 'ERROR') scr = (Math.floor(t * 3) % 2) ? '#ff5d6c' : '#8a2a35';
    else if (st === 'UNKNOWN' || st === 'RESTRICTED') scr = '#3a4058';
    else if (st !== 'OFFLINE') scr = P.night ? '#1d2c48' : '#40527a';
    R(c, mx + 1, my + 1, 14, 10, scr);
    if (st === 'RUNNING' && atDesk) {
      const off = motionOff ? 0 : Math.floor(t * 6) % 8;
      for (let i = 0; i < 4; i++) { const ly = my + 2 + ((i * 2 + off) % 8); R(c, mx + 2 + (i % 2) * 2, ly, 5 + ((i * 3 + off) % 6), 1, '#d6f3ff'); }
    } else if (st !== 'OFFLINE' && st !== 'WAITING' && st !== 'ERROR' && !atDesk) {
      R(c, mx + 3 + (motionOff ? 0 : Math.floor(t) % 8), my + 4, 2, 2, '#7aa2ff'); // screensaver
    }
    // keyboard + mug
    R(c, x + w / 2 - 6, y + 10, 12, 3, '#cfd5e6'); R(c, x + w - 8, y + 8, 4, 5, '#f2f2f2');
    // chair (drawn under agent)
    if (!atDesk) { R(c, d.seat.x - 6, d.seat.y - 9, 12, 8, P.chair); R(c, d.seat.x - 6, d.seat.y - 2, 12, 3, P.chair2); }
    // sticky note: open work while the agent is away from the desk
    if (s && openWork(s) && !(atDesk && (st === 'RUNNING'))) {
      const bob = (!motionOff && st === 'IDLE') ? (Math.floor(t * 2) % 2) : 0;
      R(c, x + 3, y + 5 - bob, 7, 7, '#ffe066'); R(c, x + 3, y + 5 - bob, 7, 2, '#f5c400'); R(c, x + 6, y + 7 - bob, 1, 3, '#7a5d12'); R(c, x + 6, y + 11 - bob, 1, 1, '#7a5d12');
    }
  }

  function drawPerson(c, a, t, P) {
    const L = a.look; const pose = a.walking ? 'walk' : (a.target ? a.target.pose : 'sit');
    const x = Math.round(a.x), y = Math.round(a.y);
    const ghost = pose === 'ghost';
    c.save();
    if (ghost) c.globalAlpha = 0.38;
    const skin = ghost ? '#9aa3b8' : L.skin, shirt = ghost ? '#6b7387' : L.shirt, hair = ghost ? '#4b5266' : L.hair, pants = ghost ? '#4b5266' : L.pants;
    const back = (pose === 'type' || pose === 'raise' || pose === 'alert' || pose === 'sit' || pose === 'locked' || pose === 'ghost') || (pose === 'review' && a.target.face === 'up') || (pose === 'walk' && a.face === 'up');
    const sitting = pose !== 'walk' && pose !== 'coffee' && !(pose === 'review' && a.target.face === 'down' && false);
    // shadow
    c.fillStyle = P.shadow; c.beginPath(); c.ellipse(x, y, 6, 2.5, 0, 0, Math.PI * 2); c.fill();
    const breathe = motionOff ? 0 : (Math.floor(t * 1.5 + (L.seed % 7)) % 2);
    const bodyY = y - 13 + (sitting ? 2 : 0);
    // legs
    if (pose === 'walk') {
      const ph = Math.floor(a.walkT * 8) % 2;
      R(c, x - 3, y - 4 - ph, 2, 4 + ph, pants); R(c, x + 1, y - 4 - (1 - ph), 2, 4 + (1 - ph), pants);
      R(c, x - 3, y - 1 - ph, 2, 1, '#1a1a22'); R(c, x + 1, y - 2 + ph, 2, 1, '#1a1a22');
    } else if (pose === 'sleep' && a.target.sofa) {
      R(c, x - 4, y - 3, 8, 2, pants);
    } else {
      R(c, x - 3, y - 4, 2, 3, pants); R(c, x + 1, y - 4, 2, 3, pants);
    }
    // chair back when seated at desk (facing desk -> we see the back)
    if (back && sitting && a.target && a.target.desk) { R(c, x - 6, y - 9, 12, 8, P.chair); R(c, x - 6, y - 2, 12, 3, P.chair2); }
    // body
    R(c, x - 4, bodyY, 8, 7, shirt); R(c, x - 4, bodyY + 6, 8, 1, 'rgba(0,0,0,.25)');
    // arms
    const armL = { x: x - 6, y: bodyY + 1 }, armR = { x: x + 4, y: bodyY + 1 };
    if (pose === 'type') {
      const k = motionOff ? 0 : Math.floor(t * 10 + (L.seed % 5)) % 2;
      R(c, armL.x, armL.y - 1 - k, 2, 5, shirt); R(c, armR.x, armR.y - 2 + k, 2, 5, shirt);
      R(c, armL.x, armL.y - 2 - k, 2, 1, skin); R(c, armR.x, armR.y - 3 + k, 2, 1, skin);
    } else if (pose === 'raise') {
      const wave = motionOff ? 0 : Math.floor(t * 3) % 2;
      R(c, armL.x, armL.y, 2, 5, shirt); R(c, armR.x + wave, armR.y - 10, 2, 11, shirt); R(c, armR.x + wave, armR.y - 12, 2, 2, skin);
    } else if (pose === 'coffee') {
      R(c, armL.x, armL.y, 2, 5, shirt); R(c, armR.x, armR.y, 2, 4, shirt); R(c, armR.x + 1, armR.y + 3, 3, 3, '#f2f2f2');
      if (!motionOff && Math.floor(t * 2 + L.seed) % 3 === 0) R(c, armR.x + 2, armR.y - 1, 1, 2, 'rgba(255,255,255,.7)');
    } else if (pose === 'review') {
      R(c, armL.x, armL.y, 2, 5, shirt); R(c, armR.x, armR.y - 1, 2, 4, shirt); R(c, armR.x - 1, armR.y - 3, 4, 5, '#f2f2f2');
    } else {
      R(c, armL.x, armL.y, 2, 5, shirt); R(c, armR.x, armR.y, 2, 5, shirt);
      R(c, armL.x, armL.y + 5, 2, 1, skin); R(c, armR.x, armR.y + 5, 2, 1, skin);
    }
    // head
    const hy = bodyY - 7 + (pose === 'sleep' ? 2 : 0) - (pose === 'type' ? 0 : 0) + (breathe && pose === 'sleep' ? 1 : 0);
    const hx = x - 4 + (pose === 'sleep' ? 1 : 0);
    R(c, hx, hy, 8, 7, skin);
    if (back) {
      R(c, hx, hy, 8, 6, hair); R(c, hx + 1, hy + 6, 6, 1, hair);
    } else {
      R(c, hx, hy, 8, 2, hair);
      if (L.style === 1) { R(c, hx - 1, hy, 1, 5, hair); R(c, hx + 8, hy, 1, 5, hair); }
      if (L.style === 2) { R(c, hx, hy + 2, 2, 2, hair); }
      if (L.style === 3) { R(c, hx + 1, hy - 1, 6, 1, hair); }
      const blink = !motionOff && (Math.floor(t * 0.7 + L.seed % 11) % 9 === 0);
      if (pose === 'sleep' || blink) { R(c, hx + 2, hy + 4, 2, 1, '#2a1d16'); R(c, hx + 5, hy + 4, 2, 1, '#2a1d16'); }
      else { R(c, hx + 2, hy + 3, 1, 2, '#1a1424'); R(c, hx + 5, hy + 3, 1, 2, '#1a1424'); }
    }
    c.restore();
    return { headY: hy, x };
  }

  const GLYPH = {
    '!': ['010', '010', '010', '000', '010'],
    '?': ['111', '001', '010', '000', '010'],
    'z': ['111', '001', '010', '100', '111'],
    'L': ['010', '101', '111', '111', '111'],
    'v': ['000', '001', '101', '010', '000'],
    'c': ['111', '101', '101', '101', '111'],
  };
  function bubble(c, x, y, ch, fg, bgc, t, bob) {
    const off = bob && !motionOff ? Math.round(Math.sin(t * 4) * 1.2) : 0;
    const bx = x - 5, by = y - 11 + off;
    R(c, bx, by, 11, 9, bgc); R(c, bx + 1, by - 1, 9, 1, bgc); R(c, bx + 1, by + 9, 9, 1, bgc); R(c, x - 1, by + 10, 2, 2, bgc);
    const g = GLYPH[ch] || GLYPH['?'];
    for (let r = 0; r < g.length; r++) for (let q = 0; q < 3; q++) if (g[r][q] === '1') R(c, bx + 4 + q, by + 2 + r, 1, 1, fg);
  }

  function drawWorld(t) {
    const L = S.layout, P = PAL[theme];
    if (bgDirty) drawBackground();
    world.width = L.W * T; world.height = L.H * T;
    const c = wctx;
    c.imageSmoothingEnabled = false;
    c.drawImage(bg, 0, 0);
    // desks, then agents sorted by y (simple depth)
    for (const [key, d] of L.desks) drawDesk(c, d, S.byKey.get(key), S.agents.get(key), t, P);
    const list = [...S.agents.values()].sort((a, b) => a.y - b.y);
    for (const a of list) {
      const s = a.s; if (!s) continue;
      const head = drawPerson(c, a, t, P);
      const pose = a.walking ? 'walk' : a.target.pose;
      const top = head.headY;
      if (s.state === 'WAITING') bubble(c, a.x, top - 2, '!', '#3a2a00', '#ffc857', t, true);
      else if (s.state === 'ERROR') bubble(c, a.x, top - 2, '!', '#ffffff', '#e5484d', t, true);
      else if (s.state === 'DONE') bubble(c, a.x, top - 2, '?', '#ffffff', '#8f63e8', t, true);
      else if (s.state === 'UNKNOWN') bubble(c, a.x, top - 2, '?', '#ffffff', '#6b7387', t, false);
      else if (s.state === 'RESTRICTED') bubble(c, a.x, top - 2, 'L', '#ffffff', '#6b5a8c', t, false);
      else if (pose === 'sleep') {
        const k = motionOff ? 0 : (t * 0.8) % 1;
        c.globalAlpha = motionOff ? 1 : 1 - k; bubble(c, a.x + 4, top - 2 - Math.round(k * 6), 'z', '#1b2a4d', '#cfe1ff', t, false); c.globalAlpha = 1;
      } else if (s.state === 'IDLE' && openWork(s)) bubble(c, a.x, top - 2, 'c', '#3a2a00', '#ffc857', t, true);
      if (S.selected === s.key) {
        const pulse = motionOff ? 0 : Math.floor(t * 3) % 2;
        c.strokeStyle = '#5ec8ff'; c.lineWidth = 1; c.strokeRect(Math.round(a.x) - 8 - pulse + .5, top - 3 - pulse + .5, 16 + pulse * 2, (a.y - top) + 5 + pulse * 2);
      }
    }
    // night light pools
    if (P.night) { c.fillStyle = P.glow; for (const d of L.desks.values()) { c.beginPath(); c.arc(d.x + d.w / 2, d.y + 8, 14, 0, Math.PI * 2); c.fill(); } }
  }

  function render(t) {
    if (!S.layout) return;
    drawWorld(t);
    const L = S.layout;
    const k = scale * dpr;
    const cw = Math.round(L.W * T * k), ch = Math.round(L.H * T * k);
    if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
    canvas.style.width = Math.round(L.W * T * scale) + 'px'; canvas.style.height = Math.round(L.H * T * scale) + 'px';
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(world, 0, 0, cw, ch);
    // crisp labels on the display canvas
    const P = PAL[theme];
    ctx.textBaseline = 'middle';
    const fs = Math.max(9, Math.round(5.2 * k)) ;
    ctx.font = `600 ${Math.min(fs, 13 * dpr)}px ui-monospace,Menlo,Consolas,monospace`;
    const label = (text, x, y, color, bgc) => {
      const w = ctx.measureText(text).width + 8 * dpr, h = Math.min(fs, 13 * dpr) + 6 * dpr;
      ctx.fillStyle = bgc; ctx.fillRect(Math.round(x - w / 2), Math.round(y - h / 2), Math.round(w), Math.round(h));
      ctx.fillStyle = color; ctx.textAlign = 'center'; ctx.fillText(text, Math.round(x), Math.round(y + 0.5));
    };
    for (const p of L.pods) {
      const txt = `${p.name} · ${p.count} phiên${p.unreachable ? ' · mất kết nối' : ''}`;
      ctx.font = `700 ${Math.min(fs, 13 * dpr)}px ui-monospace,Menlo,Consolas,monospace`;
      const w = ctx.measureText(txt).width + 10 * dpr;
      ctx.fillStyle = P.labelBg; ctx.fillRect(Math.round(p.x * T * k), Math.round((p.y * T - 2) * k), Math.round(w), Math.round(9 * k));
      ctx.fillStyle = p.unreachable ? '#ff8080' : P.label; ctx.textAlign = 'left';
      ctx.fillText(txt, Math.round(p.x * T * k + 5 * dpr), Math.round((p.y * T + 2.5) * k));
    }
    ctx.font = `600 ${Math.min(fs, 12 * dpr)}px ui-monospace,Menlo,Consolas,monospace`;
    for (const [key, d] of L.desks) {
      const s = S.byKey.get(key); if (!s) continue;
      const name = String(s.session || '?'); const short = name.length > 12 ? name.slice(0, 11) + '…' : name;
      label(short, (d.x + d.w / 2) * k, (d.y + T + 4 + 22) * k, S.selected === key ? '#5ec8ff' : P.label, P.labelBg);
    }
    for (const a of S.agents.values()) {
      if (!a.s || a.target.desk) continue;
      if (S.selected === a.key || a.target.zone === 'meeting') label(String(a.s.session).slice(0, 14), a.x * k, (a.y + 7) * k, S.selected === a.key ? '#5ec8ff' : P.label, P.labelBg);
    }
    // zone titles
    ctx.font = `700 ${Math.min(fs, 12 * dpr)}px ui-monospace,Menlo,Consolas,monospace`; ctx.textAlign = 'left';
    const zt = (txt, r) => { ctx.fillStyle = P.labelBg; const w = ctx.measureText(txt).width + 10 * dpr; ctx.fillRect(Math.round((r.x * T + 4) * k), Math.round((r.y * T + 3) * k), w, Math.round(8 * k)); ctx.fillStyle = P.label; ctx.fillText(txt, Math.round((r.x * T + 4) * k + 5 * dpr), Math.round((r.y * T + 7) * k)); };
    zt('☕ Cafe', { x: L.cafe.x, y: L.cafe.y + (L.portrait ? 0.2 : 0.3) });
    zt('🗂 Phòng họp · review', { x: L.meeting.x, y: L.meeting.y + 0.2 });
  }

  // ---------------------------------------------------------- loop & sizing
  let raf = 0, lastTs = 0, lastDraw = 0;
  function loop(ts) {
    raf = 0;
    if (document.hidden || motionOff) return;
    const dt = Math.min(0.1, (ts - (lastTs || ts)) / 1000); lastTs = ts;
    stepAgents(dt);
    if (ts - lastDraw >= 1000 / 24) { render(ts / 1000); lastDraw = ts; }
    raf = requestAnimationFrame(loop);
  }
  function kick() {
    if (motionOff) { render(0); return; }
    if (!raf && !document.hidden) { lastTs = 0; raf = requestAnimationFrame(loop); }
  }

  const ZOOMS = [0.6, 0.75, 1, 1.25, 1.5, 2];
  function relayout() {
    const box = $('canvasBox');
    const avail = Math.max(280, box.clientWidth - 2);
    dpr = Math.min(3, window.devicePixelRatio || 1);
    let base = avail >= 1700 ? 3 : avail >= 820 ? 2 : avail >= 560 ? 1.5 : 1.25;
    const vh = window.innerHeight || 800;
    if (avail >= 820 && vh < 900 && avail < 1200) base = 2;
    const factor = ZOOMS[Math.max(0, Math.min(ZOOMS.length - 1, 2 + zoomStep))];
    scale = base * factor;
    scale = Math.max(1, Math.round(scale * dpr) / dpr);
    const MIN_TILES = 20;
    if (zoomStep <= 0 && avail / (T * scale) < MIN_TILES) {
      // Never wider than the phone: shrink to fit 20 tiles, snapped to whole device pixels.
      scale = Math.max(0.5, Math.floor(avail / (MIN_TILES * T) * dpr) / dpr);
    }
    const tiles = Math.max(MIN_TILES, Math.floor(avail / (T * scale)));
    box.style.background = PAL[theme].wall;
    const key = tiles + '|' + S.sessions.map(s => s.key).join(',') + '|' + JSON.stringify(S.data ? S.data.unreachable_nodes || [] : []);
    if (!S.layout || S.layoutKey !== key) {
      S.layout = computeLayout(tiles, S.sessions, S.data ? S.data.unreachable_nodes : []);
      S.layoutKey = key; bgDirty = true;
      for (const a of S.agents.values()) a.goal = null;
      syncAgents(S.first);
    }
    render(performance.now() / 1000);
  }
  let resizeTimer = 0;
  window.addEventListener('resize', () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(relayout, 120); });

  // ------------------------------------------------------------ hit test
  function hitTest(ev) {
    if (!S.layout) return null;
    const rect = canvas.getBoundingClientRect();
    const x = (ev.clientX - rect.left) / scale, y = (ev.clientY - rect.top) / scale;
    let best = null, bestD = 1e9;
    for (const a of S.agents.values()) {
      if (x >= a.x - 8 && x <= a.x + 8 && y >= a.y - 24 && y <= a.y + 3) { const d = Math.abs(a.y - y); if (d < bestD) { best = a.key; bestD = d; } }
    }
    if (best) return best;
    for (const [key, d] of S.layout.desks) if (x >= d.x && x <= d.x + d.w && y >= d.y - 8 && y <= d.y + T + 30) return key;
    return null;
  }
  canvas.addEventListener('click', (ev) => { const key = hitTest(ev); if (key) select(key, true); });
  canvas.addEventListener('mousemove', (ev) => {
    const key = hitTest(ev); const tip = $('tip');
    canvas.classList.toggle('hover', !!key);
    if (!key) { tip.style.display = 'none'; return; }
    const s = S.byKey.get(key); if (!s) return;
    tip.replaceChildren(el('b', '', s.session), el('span', '', `${STATE_SHORT[s.state] || s.state} · ${s.node_name || s.node_id}`),
      el('div', '', s.current_task && s.current_task.summary ? s.current_task.summary : (s.branch ? `⎇ ${s.branch}` : '')));
    const box = $('canvasBox').getBoundingClientRect();
    tip.style.left = Math.min(box.width - 270, ev.clientX - box.left + 14 + $('canvasBox').scrollLeft) + 'px';
    tip.style.top = (ev.clientY - box.top + 14 + $('canvasBox').scrollTop) + 'px';
    tip.style.display = 'block';
  });
  canvas.addEventListener('mouseleave', () => { $('tip').style.display = 'none'; });
  canvas.addEventListener('keydown', (ev) => {
    const keys = S.sessions.map(s => s.key); if (!keys.length) return;
    let i = keys.indexOf(S.selected);
    if (ev.key === 'ArrowRight' || ev.key === 'ArrowDown') { i = (i + 1) % keys.length; select(keys[i], false); ev.preventDefault(); }
    else if (ev.key === 'ArrowLeft' || ev.key === 'ArrowUp') { i = (i - 1 + keys.length) % keys.length; select(keys[i], false); ev.preventDefault(); }
    else if (ev.key === 'Enter' && S.selected) { openDrawer(true); }
  });

  // ------------------------------------------------------------- selection
  function select(key, open) {
    S.selected = key;
    const url = new URL(location.href); url.searchParams.set('session', key); history.replaceState(null, '', url);
    renderRoster(); renderDrawer();
    if (open) openDrawer(true);
    render(performance.now() / 1000);
    poll(true);
  }
  function openDrawer(focus) {
    $('drawer').classList.add('open');
    if (window.innerWidth < 1600) $('scrim').classList.add('on');
    if (focus) $('drawer').focus({ preventScroll: true });
  }
  function closeDrawer() { $('drawer').classList.remove('open'); $('scrim').classList.remove('on'); canvas.focus({ preventScroll: true }); }
  $('drawerClose').addEventListener('click', closeDrawer);
  $('scrim').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && $('drawer').classList.contains('open')) closeDrawer(); });

  // ------------------------------------------------------------ roster
  function renderRoster() {
    const ul = $('rosterList');
    const items = [...S.sessions].sort((a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state) || String(a.session).localeCompare(String(b.session)));
    $('rosterCount').textContent = String(items.length);
    ul.replaceChildren(...items.map(s => {
      const li = el('li');
      const b = el('button', 'who st-' + s.state); b.type = 'button';
      b.setAttribute('aria-current', S.selected === s.key ? 'true' : 'false');
      const top = el('span', 'n', s.session);
      const pill = el('span', 'pill st-' + s.state, STATE_SHORT[s.state] || s.state); pill.style.marginLeft = '6px';
      top.append(pill);
      const meta = [s.node_name || s.node_id, s.agent, s.repo ? s.repo.split('/').pop() : null, s.branch ? '⎇ ' + s.branch : null, 'thấy ' + ago(lastSeen(s))].filter(Boolean).join(' · ');
      const work = s.current_task && s.current_task.summary ? el('span', 'm', '📌 ' + s.current_task.summary) : null;
      const open = (S.tasksBySession[s.key] || []).length;
      const tc = open ? el('span', 'm', `📋 ${open} task Task Center`) : null;
      b.append(top, el('span', 'm', meta)); if (work) b.append(work); if (tc) b.append(tc);
      b.addEventListener('click', () => select(s.key, true));
      li.append(b); return li;
    }));
  }

  // ------------------------------------------------------------ drawer
  function avatar(s) {
    const cv = document.createElement('canvas'); cv.width = 24; cv.height = 24;
    const c = cv.getContext('2d');
    const a = { x: 12, y: 22, look: looks(s.key), target: { pose: s.state === 'OFFLINE' ? 'ghost' : 'coffee', face: 'down' }, walking: false, walkT: 0, face: 'down' };
    c.imageSmoothingEnabled = false;
    drawPerson(c, a, 0, PAL[theme]);
    cv.setAttribute('aria-hidden', 'true');
    return cv;
  }
  function factList(rows) {
    const dl = el('dl', 'facts');
    rows.forEach(([k, v, title]) => { if (v === null || v === undefined || v === '') return; const dt = el('dt', '', k); const dd = el('dd', '', v); if (title) dd.title = title; dl.append(dt, dd); });
    return dl;
  }
  function renderDrawer() {
    const body = $('drawerBody');
    const s = S.byKey.get(S.selected);
    if (!s) {
      body.replaceChildren(el('div', 'drawer-empty', S.selected ? 'Phiên này không còn trong danh sách live (đã đóng hoặc node mất kết nối).' : 'Chọn một nhân vật trên sàn hoặc trong danh sách để xem phiên, log và task.'));
      return;
    }
    const head = el('div', 'who-head');
    const txt = el('div');
    const nm = el('div', 'nm', s.session);
    const pill = el('span', 'pill st-' + s.state, STATE_LABEL[s.state] || s.state);
    txt.append(nm, pill);
    head.append(avatar(s), txt);
    const parts = [head];
    if (s.state === 'IDLE' && openWork(s)) {
      const w = el('div', 'box warn'); w.append(el('h3', '', 'Agent rảnh — task CHƯA xong'), el('div', '', 'IDLE/COMPOSER chỉ nghĩa là phiên đang chờ lượt mới. Task vẫn mở cho tới khi có bằng chứng verified_done.'));
      parts.push(w);
    }
    if (s.blocker) { const b = el('div', s.state === 'ERROR' ? 'box err' : 'box warn'); b.append(el('h3', '', s.state === 'WAITING' ? 'Đang chờ input' : 'Blocker'), el('div', '', s.blocker)); parts.push(b); }
    parts.push(factList([
      ['Node', s.node_name || s.node_id], ['Agent', [s.agent, s.model].filter(Boolean).join(' · ')], ['Repo', s.repo], ['Branch', s.branch ? s.branch + (s.dirty ? ' (dirty)' : '') : null],
      ['cwd', s.cwd], ['Lý do', s.reason], ['Thấy lần cuối', ago(lastSeen(s)), abs(lastSeen(s))], ['Tạo lúc', s.created_at ? ago(s.created_at) : null, abs(s.created_at)],
      ['Input gần nhất', s.last_input_at ? ago(s.last_input_at) : null, abs(s.last_input_at)], ['Context', s.context_percent != null ? s.context_percent + '%' : null],
      ['Nguồn hoạt động', s.activity_source],
    ]));
    if (s.current_task) {
      const b = el('div', 'box'); b.append(el('h3', '', 'Task hiện tại (nhãn phiên)'));
      b.append(el('div', '', s.current_task.summary || (s.current_task.summary_withheld ? '(ẩn — cần quyền operator)' : '—')));
      const sub = [s.current_task.task_id, s.current_task.source, s.current_task.updated_at ? ago(s.current_task.updated_at) : null].filter(Boolean).join(' · ');
      if (sub) b.append(el('div', 'm', sub));
      parts.push(b);
    }
    if (s.task) {
      const b = el('div', 'box'); b.append(el('h3', '', `Task bền (${s.task.kind})`));
      b.append(el('div', '', [s.task.id, s.task.status].filter(Boolean).join(' · ')));
      if (s.task.title) b.append(el('div', 'm', s.task.title));
      parts.push(b);
    }
    const linked = (S.tasksBySession[s.key] || []).map(id => S.tasks.find(t => t.task_id === id)).filter(Boolean);
    const tb = el('div', 'box'); tb.append(el('h3', '', `Task Center (${linked.length})`));
    if (!linked.length) tb.append(el('div', 'm', S.data && S.data.task_center && S.data.task_center.connection === 'connected' ? 'Chưa có task Task Center nào gắn bằng chứng với phiên này.' : 'Task Center: ' + ((S.data && S.data.task_center && S.data.task_center.connection_label) || 'Chưa kết nối')));
    linked.forEach(t => { const r = el('div'); r.append(el('span', 'flag f-' + t.bucket, (t.flag_labels || [])[0] || t.state), document.createTextNode(' ' + t.title)); tb.append(r); });
    parts.push(tb);
    const logBox = el('div'); logBox.append(el('h3', '', 'Log (tail)'));
    logBox.firstChild.style.cssText = 'margin:0 0 4px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px';
    const pre = el('pre', 'log'); pre.textContent = (s.tail_full || s.lines || []).join('\n') || (s.readable === false ? '(không có quyền đọc pane)' : '(trống)');
    logBox.append(pre); parts.push(logBox);
    const act = el('div', 'actions');
    const live = el('a', 'btn', '↗ Mở trong Đang chạy'); live.href = '/dashboard/live';
    const dash = el('a', 'btn', '⌨ Mở phiên trên Dashboard'); dash.href = '/dashboard#' + encodeURIComponent(s.qualified || s.session);
    act.append(live, dash); parts.push(act);
    const keepScroll = body.querySelector('pre.log'); const atBottom = !keepScroll || keepScroll.scrollTop + keepScroll.clientHeight >= keepScroll.scrollHeight - 8;
    body.replaceChildren(...parts);
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  // ------------------------------------------------------------- tasks
  const FILTERS = [
    ['unfinished', 'Chưa xong'], ['waiting_input', 'Chờ input'], ['dispatch_uncertain', 'Dispatch chưa chắc'], ['blocked', 'Bị chặn'],
    ['needs_review', 'Cần review'], ['stale', 'Cũ'], ['unknown', 'Không rõ'], ['agent_idle_open', 'Agent rảnh'], ['verified_done', 'Đã xác minh'], ['all', 'Tất cả'],
  ];
  const HOT = new Set(['waiting_input', 'dispatch_uncertain', 'blocked', 'agent_idle_open', 'stale', 'unknown']);
  function taskMatches(t) {
    if (S.filter === 'unfinished' && !t.unfinished) return false;
    if (S.filter !== 'unfinished' && S.filter !== 'all' && !t.flags.includes(S.filter)) return false;
    if (S.search) {
      const hay = [t.title, t.project, t.task_id, t.external_id, t.node, t.requirement, ...(t.links || []).map(l => l.url + ' ' + l.label), ...(t.linked_sessions || []).map(x => x.session)].join(' ').toLowerCase();
      if (!hay.includes(S.search)) return false;
    }
    return true;
  }
  function renderFilters(counts) {
    const box = $('filters');
    box.replaceChildren(...FILTERS.map(([key, label]) => {
      const n = key === 'all' ? S.tasks.length : (counts[key] || 0);
      const b = el('button', 'chip' + (HOT.has(key) && n ? ' hot' : ''), label); b.type = 'button';
      b.setAttribute('aria-pressed', S.filter === key ? 'true' : 'false');
      b.append(el('b', '', n));
      b.addEventListener('click', () => { S.filter = key; renderTasks(); });
      return b;
    }));
  }
  function linkChip(l) {
    const url = safeUrl(l.url);
    const node = url ? el('a', 'lk k-' + l.kind) : el('span', 'lk k-' + l.kind);
    const icon = { chat: '💬', issue: '◉', pr: '⇄', commit: '⎔', tests: '✓', github: '⌥', link: '↗' }[l.kind] || '↗';
    node.textContent = icon + ' ' + l.label;
    if (url) { node.href = url; node.target = '_blank'; node.rel = 'noopener noreferrer'; node.title = url; }
    return node;
  }
  function taskCard(t) {
    const li = el('li', 'task b-' + t.bucket);
    li.append(el('div', 't', t.title || '(không tiêu đề)'));
    const meta = el('div', 'meta');
    [t.project && '📁 ' + t.project, '⟲ ' + ago(t.updated_at), 'nguồn: ' + t.source + (t.state_source ? ' / ' + t.state_source : ''), t.node && '🖥 ' + t.node]
      .filter(Boolean).forEach(x => meta.append(el('span', '', x)));
    meta.lastChild && (meta.firstChild.title = abs(t.updated_at));
    li.append(meta);
    const flags = el('div', 'flags');
    flags.append(el('span', 'flag', 'state: ' + t.state));
    t.flags.forEach((f, i) => flags.append(el('span', 'flag f-' + f, (t.flag_labels || [])[i] || f)));
    li.append(flags);
    if (t.blocker || t.next_action) {
      const nx = el('div', 'meta');
      if (t.blocker) nx.append(el('span', '', '⛔ ' + t.blocker));
      if (t.next_action) nx.append(el('span', '', '→ ' + t.next_action));
      li.append(nx);
    }
    if ((t.links || []).length) { const ls = el('div', 'links'); t.links.forEach(l => ls.append(linkChip(l))); li.append(ls); }
    if ((t.linked_sessions || []).length) {
      const ss = el('div', 'sess-links');
      t.linked_sessions.forEach(x => {
        const b = el('button', 'sess-link', `${x.node_id}/${x.session} · ${STATE_SHORT[x.state] || x.state}`); b.type = 'button';
        b.addEventListener('click', () => { switchTab('office'); select(x.key, true); });
        ss.append(b);
      });
      li.append(ss);
    }
    if (t.requirement || (t.acceptance_criteria || []).length) {
      const d = el('details'); d.append(el('summary', '', `Yêu cầu gốc${t.acceptance_criteria.length ? ' + ' + t.acceptance_criteria.length + ' AC' : ''}`));
      if (t.requirement) d.append(el('div', 'req', t.requirement));
      if (t.acceptance_criteria.length) { const ol = el('ol'); t.acceptance_criteria.forEach(a => ol.append(el('li', '', a))); d.append(ol); }
      li.append(d);
    }
    const id = el('div', 'meta'); id.append(el('span', '', t.task_id)); if (t.external_id) id.append(el('span', '', 'ext: ' + t.external_id));
    id.append(el('span', '', `${t.evidence_count} bằng chứng`)); li.append(id);
    return li;
  }
  function renderTasks() {
    const tc = (S.data && S.data.task_center) || {};
    const conn = $('conn'); conn.className = 'conn ' + (tc.connection || 'not_configured');
    conn.textContent = (tc.connection_label || 'Chưa kết nối') + (tc.stale ? ' · dữ liệu cũ' : '');
    const detail = [tc.detail, tc.host ? 'Gateway: ' + tc.host : null, tc.fetched_at ? 'đọc ' + ago(tc.fetched_at) : null,
      tc.stale && tc.last_good_at ? 'bản tốt cuối: ' + ago(tc.last_good_at) : null, tc.truncated ? '⚠ danh sách bị cắt — có thể thiếu task' : null].filter(Boolean).join(' · ');
    $('connDetail').textContent = detail;
    const counts = tc.flag_counts || {};
    renderFilters(counts);
    const empty = $('tcEmpty');
    if (tc.connection !== 'connected' && !S.tasks.length) {
      const box = el('div', 'empty'); box.append(el('h3', '', tc.connection_label || 'Chưa kết nối'));
      box.append(el('div', '', tc.connection === 'forbidden' ? 'Task Center chỉ hiển thị cho operator/owner.' :
        'Không có dữ liệu Task Center — không hiển thị task giả. Task gắn trên phiên Terminal vẫn liệt kê bên dưới.'));
      empty.replaceChildren(box);
    } else empty.replaceChildren();
    const shown = S.tasks.filter(taskMatches);
    const ul = $('tlist');
    ul.replaceChildren(...shown.map(taskCard));
    if (S.tasks.length && !shown.length) ul.append(el('li', 'empty', 'Không có task khớp bộ lọc này.'));
    // live session labels (existing Terminal MCP task labels — not a registry)
    const labelled = S.sessions.filter(s => s.current_task || s.task).sort((a, b) => STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state));
    $('labelsHead').hidden = !labelled.length;
    $('llist').replaceChildren(...labelled.map(s => {
      const li = el('li', 'task b-' + ({ WAITING: 'waiting_input', ERROR: 'blocked', DONE: 'needs_review', IDLE: 'stale', UNKNOWN: 'unknown', OFFLINE: 'unknown' }[s.state] || 'active'));
      li.append(el('div', 't', (s.current_task && s.current_task.summary) || (s.task && (s.task.title || s.task.id)) || '(ẩn — cần quyền operator)'));
      const meta = el('div', 'meta');
      meta.append(el('span', 'pill st-' + s.state, STATE_SHORT[s.state] || s.state), el('span', '', `${s.node_name || s.node_id}/${s.session}`), el('span', '', '⟲ ' + ago(lastSeen(s))));
      li.append(meta);
      if (s.state === 'IDLE') { const fl = el('div', 'flags'); fl.append(el('span', 'flag f-agent_idle_open', 'Agent rảnh — chưa có bằng chứng xong')); li.append(fl); }
      li.style.cursor = 'pointer'; li.tabIndex = 0;
      const go = () => { switchTab('office'); select(s.key, true); };
      li.addEventListener('click', go); li.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
      return li;
    }));
    const hist = tc.history_import || { label: 'Chưa kết nối', detail: '' };
    $('histState').textContent = hist.label; $('histDetail').textContent = hist.detail || '';
    // must-not-miss strip
    const open = counts.unfinished || 0;
    const parts = [];
    if (open) parts.push(`${open} task chưa xong`);
    ['waiting_input', 'dispatch_uncertain', 'blocked', 'agent_idle_open', 'stale'].forEach(k => { if (counts[k]) parts.push(`${counts[k]} ${FILTERS.find(f => f[0] === k)[1].toLowerCase()}`); });
    const waitingSessions = S.sessions.filter(s => s.state === 'WAITING').length;
    if (waitingSessions) parts.push(`${waitingSessions} phiên chờ input`);
    const strip = $('alertStrip');
    strip.classList.toggle('on', parts.length > 0);
    strip.replaceChildren(el('span', '', '⚠'), el('b', '', parts.join(' · ')), el('span', '', '— bấm để xem'));
    $('tabTaskCount').textContent = open ? `(${open})` : '';
  }

  // --------------------------------------------------- conversations.json
  $('consent').addEventListener('change', (e) => { $('convFile').disabled = !e.target.checked; if (!e.target.checked) { $('convFile').value = ''; $('importOut').replaceChildren(); $('importCopy').hidden = true; } });
  let unmatched = [];
  $('convFile').addEventListener('change', async (e) => {
    const file = e.target.files && e.target.files[0]; const out = $('importOut');
    if (!file || !$('consent').checked) return;
    if (file.size > 300 * 1024 * 1024) { out.replaceChildren(el('p', '', 'File quá lớn (>300MB) để xem trước trong trình duyệt.')); return; }
    out.replaceChildren(el('p', '', 'Đang đọc cục bộ…'));
    let data;
    try { data = JSON.parse(await file.text()); } catch (err) { out.replaceChildren(el('p', '', 'Không đọc được JSON: ' + err.message)); return; }
    const convs = (Array.isArray(data) ? data : (data && data.conversations) || []).filter(c => c && typeof c === 'object');
    const known = new Set();
    for (const t of S.tasks) {
      [t.external_id, t.external_url, t.requirement, ...(t.links || []).map(l => l.url)].forEach(v => {
        const m = String(v || '').match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi); (m || []).forEach(id => known.add(id.toLowerCase()));
      });
    }
    const seen = new Set();
    const rows = [];
    for (const c of convs) {
      const id = String(c.conversation_id || c.id || '').toLowerCase();
      if (!id || seen.has(id)) continue; seen.add(id);
      rows.push({ id, title: String(c.title || '(không tiêu đề)').slice(0, 160), at: Number(c.update_time || c.create_time || 0), dup: known.has(id) });
    }
    rows.sort((a, b) => b.at - a.at);
    unmatched = rows.filter(r => !r.dup);
    const ul = el('ul');
    rows.slice(0, 300).forEach(r => { const li = el('li'); li.append(el('span', 'k ' + (r.dup ? 'dup' : 'new'), r.dup ? 'ĐÃ CÓ' : 'CHƯA CÓ'), el('span', '', r.title), el('span', 'm', r.at ? ago(r.at) : '')); ul.append(li); });
    out.replaceChildren(el('p', '', `${convs.length} cuộc trò chuyện · ${rows.length} sau khi bỏ trùng · ${rows.length - unmatched.length} đã có trong Task Center · ${unmatched.length} chưa có (có thể bị sót). Chỉ xem trước — không tải lên, không lưu.`), ul);
    $('importCopy').hidden = !unmatched.length;
  });
  $('importCopy').addEventListener('click', async () => {
    const text = unmatched.map(r => `- ${r.title} — https://chatgpt.com/c/${r.id}`).join('\n');
    try { await navigator.clipboard.writeText(text); $('importCopy').textContent = 'Đã sao chép ✓'; } catch (e) { $('importCopy').textContent = 'Không sao chép được'; }
  });

  // ------------------------------------------------------------- header
  function renderStats() {
    const by = {}; S.sessions.forEach(s => { by[s.state] = (by[s.state] || 0) + 1; });
    const box = $('stats');
    box.replaceChildren(...STATE_ORDER.filter(k => by[k]).map(k => {
      const sp = el('span', 'stat'); const i = el('i'); i.style.background = STATE_COLOR[k];
      sp.append(i, el('span', '', STATE_SHORT[k]), el('b', '', by[k])); return sp;
    }));
    if (!S.sessions.length) box.append(el('span', 'stat', 'Chưa có phiên'));
    const nodes = new Set(S.sessions.map(s => s.node_id));
    $('stageSub').textContent = `${S.sessions.length} phiên · ${nodes.size} node` + ((S.data && S.data.unreachable_nodes || []).length ? ` · ${S.data.unreachable_nodes.length} node mất kết nối` : '');
    canvas.setAttribute('aria-label', `Sàn văn phòng: ${S.sessions.length} phiên. ` + STATE_ORDER.filter(k => by[k]).map(k => `${by[k]} ${STATE_SHORT[k]}`).join(', ') + '. Dùng phím mũi tên để chọn, Enter để mở chi tiết; danh sách nhân sự bên dưới chứa cùng thông tin.');
    $('emptyOffice').hidden = S.sessions.length > 0;
  }
  function setLive(ok) {
    const dot = $('dot');
    dot.classList.toggle('stale', !ok);
    if (ok) { dot.classList.remove('tick'); void dot.offsetWidth; dot.classList.add('tick'); }
    $('liveText').textContent = ok ? (DEMO ? 'DEMO' : 'live · cập nhật ' + new Date().toLocaleTimeString('vi-VN')) : `mất kết nối (${S.failures}) — đang hiển thị dữ liệu cuối`;
  }

  // ---------------------------------------------------------------- data
  function apply(data) {
    S.data = data;
    S.sessions = (data.sessions || []).filter(s => s && s.key);
    S.byKey = new Map(S.sessions.map(s => [s.key, s]));
    const tc = data.task_center || {};
    S.tasks = tc.tasks || [];
    S.tasksBySession = tc.tasks_by_session || {};
    const box = $('canvasBox');
    const key0 = S.layoutKey;
    relayout();
    if (S.layoutKey === key0) syncAgents(S.first);
    S.first = false;
    renderStats(); renderRoster(); renderTasks(); renderDrawer();
    kick();
  }
  async function poll(force) {
    if (S.inflight || (document.hidden && !force)) return;
    if (DEMO) { apply(demoData()); setLive(true); return; }
    S.inflight = true;
    try {
      const params = new URLSearchParams();
      if (S.selected) params.set('expand', S.selected);
      const res = await fetch(API + (params.toString() ? '?' + params : ''), { credentials: 'same-origin', cache: 'no-store' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      S.failures = 0; setLive(true);
      try { apply(data); } catch (err) { console.error('office render failed', err); }
    } catch (e) { S.failures += 1; setLive(false); }
    finally { S.inflight = false; }
  }

  // ------------------------------------------------------------ controls
  function applyTheme() {
    root.dataset.theme = theme;
    $('themeBtn').textContent = theme === 'day' ? '☀ Ngày' : '🌙 Đêm';
    $('themeBtn').setAttribute('aria-pressed', theme === 'day' ? 'true' : 'false');
    bgDirty = true; drawLogo();
  }
  function applyMotion() {
    root.dataset.motion = motionOff ? 'off' : 'on';
    $('motionBtn').textContent = 'Chuyển động: ' + (motionOff ? 'tắt' : 'bật');
    $('motionBtn').setAttribute('aria-pressed', motionOff ? 'false' : 'true');
    if (motionOff) { for (const a of S.agents.values()) if (a.target) { a.x = a.target.x; a.y = a.target.y; a.path = []; } render(0); }
    else { for (const a of S.agents.values()) a.goal = null; syncAgents(false); kick(); }
  }
  $('themeBtn').addEventListener('click', () => { theme = theme === 'day' ? 'night' : 'day'; pref('theme', theme); applyTheme(); render(performance.now() / 1000); if (S.selected) renderDrawer(); });
  $('motionBtn').addEventListener('click', () => { motionOff = !motionOff; pref('motion', motionOff ? 'off' : 'on'); applyMotion(); });
  if (mqReduce.addEventListener) mqReduce.addEventListener('change', (e) => { if (!pref('motion')) { motionOff = e.matches; applyMotion(); } });
  const zoom = (d) => { zoomStep = d === 0 ? 0 : Math.max(-2, Math.min(3, zoomStep + d)); pref('zoom', String(zoomStep)); S.layoutKey = null; relayout(); };
  $('zoomIn').addEventListener('click', () => zoom(1)); $('zoomOut').addEventListener('click', () => zoom(-1)); $('zoomFit').addEventListener('click', () => zoom(0));
  $('q').addEventListener('input', (e) => { S.search = e.target.value.trim().toLowerCase(); renderTasks(); });
  function switchTab(tab) {
    $('app').dataset.tab = tab;
    $('tabOffice').setAttribute('aria-selected', tab === 'office' ? 'true' : 'false');
    $('tabTasks').setAttribute('aria-selected', tab === 'tasks' ? 'true' : 'false');
    if (tab === 'office') { S.layoutKey = null; relayout(); }
  }
  $('tabOffice').addEventListener('click', () => switchTab('office'));
  $('tabTasks').addEventListener('click', () => switchTab('tasks'));
  const toTasks = () => { S.filter = 'unfinished'; renderTasks(); if (window.innerWidth < 1024) switchTab('tasks'); else $('tasksPanel').scrollIntoView({ behavior: motionOff ? 'auto' : 'smooth', block: 'start' }); };
  $('alertStrip').addEventListener('click', toTasks);
  $('alertStrip').addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toTasks(); } });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { poll(true); kick(); } });

  function drawLogo() {
    const c = $('logo').getContext('2d'); c.clearRect(0, 0, 11, 11);
    const px = ['00111111100', '01000000010', '01011111010', '01010001010', '01011111010', '01000000010', '00111111100', '00001110000', '00011111000', '01111111110', '00000000000'];
    px.forEach((row, y) => [...row].forEach((v, x) => { if (v === '1') { c.fillStyle = y < 7 ? (theme === 'day' ? '#3b6cff' : '#5ec8ff') : '#9c6844'; c.fillRect(x, y, 1, 1); } }));
    c.fillStyle = '#43d17c'; c.fillRect(4, 3, 3, 1);
  }

  // --------------------------------------------------------- explicit demo
  function demoData() {
    const now = Date.now() / 1000;
    const mk = (node, name, state, extra) => Object.assign({ key: `${node}/${name}`, session: name, qualified: `${node}/${name}`, node_id: node, node_name: node,
      agent: 'claude', state, reason: '', last_activity_at: now - 30, created_at: now - 4000, lines: ['$ pytest -q', '........ 128 passed', '> working…'], readable: true }, extra || {});
    const sessions = [
      mk('hp-linux', 'tmcp-pixel-office', 'RUNNING', { repo: 'hungtranbkit/terminal-mcp', branch: 'feat/issue67-pixel-office', current_task: { summary: 'Văn phòng AI pixel + Task Center', task_id: 'dt_d9274d5bb89c4ad0', source: 'title', updated_at: now - 300 } }),
      mk('hp-linux', 'gw-taskcenter', 'WAITING', { agent: 'codex', repo: 'hungtranbkit/chatgpt-ai-gateway', branch: 'feat/task-center-p1', blocker: 'Approve migration? (y/n)', current_task: { summary: 'Task Center P1 import', task_id: 'dt_aaaabbbbcccc', updated_at: now - 900 } }),
      mk('hp-linux', 'novaretail-api', 'RUNNING', { agent: 'codex', repo: 'novaretail/api', branch: 'fix/orders' }),
      mk('hp-linux', 'docs-writer', 'IDLE', { last_activity_at: now - 300, current_task: { summary: 'Viết runbook deploy', task_id: 'dt_1111222233334444', updated_at: now - 4000 } }),
      mk('hp-linux', 'old-refactor', 'IDLE', { last_activity_at: now - 7200 }),
      mk('m910', 'parseflow-ml', 'ERROR', { agent: 'claude', repo: 'parseflow/ml', branch: 'main', blocker: 'Traceback: CUDA OOM', reason: 'error text in pane' }),
      mk('m910', 'review-pr-66', 'DONE', { repo: 'hungtranbkit/terminal-mcp', branch: 'fix/codex-composer' }),
      mk('m910', 'mam-landing', 'IDLE', { last_activity_at: now - 9000 }),
      mk('mac-mini', 'ios-build', 'OFFLINE', { agent: 'claude', last_activity_at: now - 20000 }),
      mk('mac-mini', 'secret-ops', 'RESTRICTED', { readable: false, lines: [] }),
    ];
    const task = (id, title, state, flags, extra) => Object.assign({ task_id: id, title, project: 'Terminal MCP', state, state_source: 'user', source: 'manual', external_id: '', external_url: '', node: '',
      requirement: 'Yêu cầu từ ChatGPT: ' + title, acceptance_criteria: ['Có test', 'Có screenshot 390/1366/1920'], links: [], evidence_count: 1, has_evidence: true,
      next_action: null, blocker: null, created_at: now - 86400, updated_at: now - 1200, unfinished: !flags.includes('verified_done'), flags, bucket: flags[0], flag_labels: flags.map(f => ({ waiting_input: 'Chờ input', dispatch_uncertain: 'Dispatch chưa chắc', blocked: 'Bị chặn', needs_review: 'Cần review', stale: 'Cũ / không cập nhật', active: 'Đang làm', verified_done: 'Đã xác minh xong', agent_idle_open: 'Agent rảnh, task CHƯA xong', unknown: 'Không rõ' }[f] || f)), linked_sessions: [] }, extra || {});
    const tasks = [
      task('tc_demo01', 'Task Center P1: import conversations.json', 'waiting_input', ['waiting_input'], { project: 'AI Gateway', links: [{ kind: 'issue', url: 'https://github.com/hungtranbkit/chatgpt-ai-gateway/issues/115', label: '#115' }, { kind: 'chat', url: 'https://chatgpt.com/c/00000000-0000-0000-0000-000000000000', label: 'ChatGPT chat' }], linked_sessions: [{ key: 'hp-linux/gw-taskcenter', session: 'gw-taskcenter', node_id: 'hp-linux', state: 'WAITING' }], blocker: 'Chờ owner duyệt migration' }),
      task('tc_demo02', 'Viết runbook deploy HP', 'running', ['active', 'agent_idle_open'], { linked_sessions: [{ key: 'hp-linux/docs-writer', session: 'docs-writer', node_id: 'hp-linux', state: 'IDLE' }] }),
      task('tc_demo03', 'ParseFlow holdout eval', 'running', ['dispatch_uncertain'], { project: 'ParseFlow', node: 'm910', next_action: 'Kiểm tra phiên trên m910 còn sống' }),
      task('tc_demo04', 'Fix composer submit regression', 'needs_review', ['needs_review'], { links: [{ kind: 'pr', url: 'https://github.com/hungtranbkit/terminal-mcp/pull/66', label: 'PR #66' }, { kind: 'commit', url: '', label: '76ebad57' }] }),
      task('tc_demo05', 'Dọn worktree cũ', 'new', ['active', 'stale'], { updated_at: now - 3 * 86400 }),
      task('tc_demo06', 'Unified memory multi-node', 'verified_done', ['verified_done'], { links: [{ kind: 'pr', url: 'https://github.com/hungtranbkit/terminal-mcp/pull/60', label: 'PR #60' }, { kind: 'tests', url: 'https://github.com/hungtranbkit/terminal-mcp/actions', label: 'CI' }] }),
      task('tc_demo07', 'Đồng bộ Scheduled Tasks', 'unknown', ['unknown']),
    ];
    const counts = { unfinished: 0 }; tasks.forEach(t => { if (t.unfinished) counts.unfinished++; t.flags.forEach(f => counts[f] = (counts[f] || 0) + 1); });
    const by = {}; tasks.forEach(t => t.linked_sessions.forEach(l => (by[l.key] = by[l.key] || []).push(t.task_id)));
    return { sessions, unreachable_nodes: [], task_center: { connection: 'connected', connection_label: 'DEMO', detail: 'Dữ liệu demo — không phải Task Center thật.', tasks, flag_counts: counts, tasks_by_session: by,
      history_import: { label: 'Chưa kết nối', detail: 'Gateway chưa có endpoint import conversations.json. Xem trước + đối chiếu trùng chạy hoàn toàn trong trình duyệt; không tải lên, không ghi.' } } };
  }

  // ----------------------------------------------------------------- boot
  if (DEMO) $('demoBanner').hidden = false;
  applyTheme(); applyMotion();
  if (window.innerWidth >= 1600) $('drawer').classList.add('open');
  else if (S.selected) openDrawer(false);
  relayout();
  poll(true);
  setInterval(() => poll(false), POLL_MS);
})();
</script>
</body>
</html>
"""
