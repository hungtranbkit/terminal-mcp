"""The /dashboard/live page (and its /app/live mirror).

A plain module-level HTML constant like every other dashboard page, kept out
of the 19k-line dashboard.py. Every API call uses the literal
`/dashboard/api/` prefix so the webauth mirror's substring rewrite to
`/app/api/` covers it, and the only deep link uses the `/dashboard#` form the
mirror already rewrites.

Rendering rules that matter:
  * no full reload -- one fetch every POLL_MS, skipped while one is in
    flight and while the tab is hidden;
  * a card is rebuilt only when the server's `change_token` moves, so an
    expanded tail does not jump while you read it;
  * all pane text and previews are inserted with textContent, never HTML.
"""

LIVE_SESSIONS_HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>Đang chạy — Live Sessions — Terminal MCP</title>
  <style>
    :root {
      color-scheme: dark;
      --bg:#0b1020; --panel:#121a2d; --panel2:#0f1628; --line:#26324b; --text:#eef2ff; --muted:#9aa7bd;
      --green:#43d17c; --amber:#ffc857; --red:#ff6b6b; --accent:#5b8cff; --violet:#b48cff; --term:#0a0d14;
      --mono: ui-monospace,SFMono-Regular,Menlo,Consolas,'DejaVu Sans Mono',monospace;
    }
    * { box-sizing:border-box }
    body { margin:0; background:var(--bg); color:var(--text);
           font:14px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif }
    a { color:var(--accent); text-decoration:none }
    .wrap { max-width:1500px; margin:0 auto;
            padding:10px max(12px, env(safe-area-inset-right)) 28px max(12px, env(safe-area-inset-left)) }
    .top { display:flex; flex-wrap:wrap; align-items:center; gap:8px 12px; margin:4px 0 10px }
    h1 { font-size:17px; margin:0; white-space:nowrap }
    .muted { color:var(--muted) } .spacer { flex:1 }
    .pulse { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--green);
             margin-right:6px; vertical-align:middle }
    .pulse.stale { background:var(--red) }
    .pulse.tick { animation:blink .6s ease-out }
    @keyframes blink { from { box-shadow:0 0 0 6px rgba(67,209,124,.35) } to { box-shadow:0 0 0 0 transparent } }
    .chips { display:flex; flex-wrap:wrap; gap:6px }
    .chip { background:var(--panel); border:1px solid var(--line); color:var(--text); border-radius:999px;
            padding:6px 12px; font:13px inherit; cursor:pointer; min-height:36px }
    .chip.on { border-color:var(--accent); color:#fff; background:#1b2a4d }
    .chip b { font-weight:700; margin-left:4px }
    input[type=search], select { background:var(--panel); border:1px solid var(--line); color:var(--text);
            border-radius:8px; padding:7px 10px; font-size:16px; min-height:36px; max-width:100% }
    .banner { border:1px solid var(--line); background:var(--panel2); border-radius:10px; padding:8px 12px;
              margin:0 0 10px; font-size:13px }
    .banner.warn { border-color:#6b4f12; color:var(--amber) }
    #list { display:grid; gap:10px; grid-template-columns:repeat(auto-fill, minmax(min(100%, 430px), 1fr)) }
    .card { background:var(--panel); border:1px solid var(--line); border-left:4px solid var(--line);
            border-radius:12px; padding:10px 12px; min-width:0; display:flex; flex-direction:column; gap:6px }
    .card.s-RUNNING { border-left-color:var(--green) }
    .card.s-WAITING { border-left-color:var(--amber) }
    .card.s-ERROR { border-left-color:var(--red) }
    .card.s-DONE { border-left-color:var(--violet) }
    .card.s-OFFLINE, .card.s-RESTRICTED { opacity:.75; border-left-style:dashed }
    .card.new { box-shadow:0 0 0 1px var(--accent), 0 0 18px rgba(91,140,255,.25) }
    .card.flash { animation:arrive 2.5s ease-out }
    @keyframes arrive { from { background:#1d3366 } to { background:var(--panel) } }
    .row1 { display:flex; align-items:center; gap:8px; flex-wrap:wrap; min-width:0 }
    .name { font:600 14px var(--mono); overflow-wrap:anywhere }
    .badge { font-size:10.5px; font-weight:700; letter-spacing:.03em; padding:2px 8px; border-radius:999px;
             border:1px solid currentColor; white-space:nowrap }
    .b-RUNNING { color:var(--green) } .b-WAITING { color:var(--amber) } .b-ERROR { color:var(--red) }
    .b-DONE { color:var(--violet) } .b-IDLE, .b-UNKNOWN, .b-OFFLINE, .b-RESTRICTED { color:var(--muted) }
    .b-NEW { color:#fff; background:var(--accent); border-color:var(--accent) }
    .meta { display:flex; flex-wrap:wrap; gap:4px 12px; font-size:12px; color:var(--muted) }
    .meta span b { color:var(--text); font-weight:600 }
    .reason { font-size:12px; color:var(--muted); overflow-wrap:anywhere }
    .task, .ask, .blocker, .done { font-size:12.5px; border-radius:8px; padding:6px 8px; overflow-wrap:anywhere }
    .task { background:#14203a } .ask { background:#13202a }
    .blocker { background:rgba(255,200,87,.1); color:var(--amber) }
    .done { background:rgba(180,140,255,.1) }
    .lbl { color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.04em; margin-right:6px }
    pre.tail { margin:0; background:var(--term); border:1px solid #1a2238; border-radius:8px; padding:7px 9px;
               font:12px/1.35 var(--mono); color:#d6deeb; white-space:pre-wrap; overflow-wrap:anywhere;
               max-height:13.5em; overflow:auto }
    pre.tail.full { max-height:60vh }
    .actions { display:flex; gap:8px; flex-wrap:wrap }
    .btn { background:#19243b; border:1px solid var(--line); color:var(--text); border-radius:8px; padding:5px 10px;
           font-size:12.5px; cursor:pointer; min-height:32px }
    .empty { color:var(--muted); padding:30px 8px; text-align:center }
    @media (max-width:640px) {
      .wrap { padding-top:6px } h1 { font-size:15px }
      pre.tail { max-height:10em }
    }
  </style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <h1><span class="pulse" id="pulse"></span>Đang chạy <span class="muted">· Live Sessions</span></h1>
    <span class="muted" id="updated">đang tải…</span>
    <span class="spacer"></span>
    <input type="search" id="q" placeholder="Lọc session / node / repo…" aria-label="Lọc">
    <select id="node" aria-label="Node"><option value="">Mọi node</option></select>
  </div>
  <div class="chips" id="filters" role="tablist" style="margin-bottom:10px">
    <button class="chip on" data-f="active">Đang chạy<b id="c-active">0</b></button>
    <button class="chip" data-f="recent">Gần đây<b id="c-recent">0</b></button>
    <button class="chip" data-f="idle">Idle / xong<b id="c-idle">0</b></button>
    <button class="chip" data-f="all">Tất cả<b id="c-all">0</b></button>
  </div>
  <div id="banners"></div>
  <div id="list" aria-live="polite"></div>
  <div class="empty" id="empty" hidden>Không có session nào khớp bộ lọc.</div>
</div>
<script>
(() => {
  const API = '/dashboard/api/live-sessions';
  const POLL_MS = 2000;
  let filter = 'active';
  try { filter = localStorage.getItem('tmcp-live-filter') || 'active'; } catch (e) {}
  const expanded = new Set();
  const cards = new Map();      // key -> {el, token}
  const known = new Set();      // keys seen in an earlier poll (arrival flash)
  let first = true, inflight = false, lastOk = 0, data = null;

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = String(text); return n; };

  function age(sec) {
    if (sec === null || sec === undefined) return '—';
    sec = Math.max(0, Math.round(sec));
    if (sec < 60) return sec + 's';
    if (sec < 3600) return Math.floor(sec / 60) + 'm' + (sec % 60 ? ' ' + (sec % 60) + 's' : '');
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'm';
    return Math.floor(sec / 86400) + 'd ' + Math.floor((sec % 86400) / 3600) + 'h';
  }
  function clock(epoch) {
    if (!epoch) return '—';
    const d = new Date(epoch * 1000);
    return d.toLocaleTimeString('vi-VN', {hour: '2-digit', minute: '2-digit', second: '2-digit'})
      + (Date.now() - d > 86400000 ? ' ' + d.toLocaleDateString('vi-VN') : '');
  }
  function matches(s) {
    if (filter === 'active' && !s.active) return false;
    if (filter === 'recent' && !(s.recent || s.is_new)) return false;
    if (filter === 'idle' && s.active) return false;
    const node = $('node').value;
    if (node && s.node_id !== node) return false;
    const q = $('q').value.trim().toLowerCase();
    if (q) {
      const hay = [s.session, s.node_name, s.node_id, s.agent, s.repo, s.branch,
                   s.task && s.task.title].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }
  function stat(label, value) {
    const span = el('span'); span.append(label + ' '); span.append(el('b', null, value)); return span;
  }
  function build(s, now) {
    const card = el('div', 'card s-' + s.state + (s.is_new ? ' new' : ''));
    card.dataset.key = s.key;
    const r1 = el('div', 'row1');
    r1.append(el('span', 'badge b-' + s.state, s.state));
    if (s.is_new) r1.append(el('span', 'badge b-NEW', 'NEW'));
    r1.append(el('span', 'name', s.session));
    r1.append(el('span', 'muted', '@ ' + (s.node_name || s.node_id)));
    if (s.agent) r1.append(el('span', 'badge b-IDLE', s.agent));
    if (s.attached) r1.append(el('span', 'muted', '· attached'));
    card.append(r1);

    const meta = el('div', 'meta');
    meta.append(stat('Tạo', clock(s.created_at)));
    meta.append(stat('Đã chạy', age(s.elapsed_seconds)));
    meta.append(stat('Hoạt động', s.last_activity_age_seconds === null ? '—' : age(s.last_activity_age_seconds) + ' trước'));
    if (s.branch || s.repo) {
      const repo = (s.repo || '').split('/').filter(Boolean).pop() || '';
      meta.append(stat('Repo', (repo ? repo + ' ' : '') + '⎇ ' + (s.branch || '?')
        + (s.dirty === true ? ' ●dirty' : s.dirty === false ? ' clean' : '')));
    }
    if (s.context_percent !== null && s.context_percent !== undefined) meta.append(stat('Context', s.context_percent + '%'));
    if (s.usage_percent !== null && s.usage_percent !== undefined)
      meta.append(stat('Usage', s.usage_percent + '%' + (s.usage_reset_in_minutes ? ' (reset ' + s.usage_reset_in_minutes + 'm)' : '')));
    if (s.activity_source) meta.append(stat('Nguồn', s.activity_source));
    if (s.lifecycle_state) meta.append(stat('Lifecycle', s.lifecycle_state));
    card.append(meta);
    if (s.reason) card.append(el('div', 'reason', s.reason));

    if (s.task) {
      const t = el('div', 'task');
      t.append(el('span', 'lbl', s.task.kind + ' task'));
      t.append(el('b', null, (s.task.title || s.task.id || '') + ' '));
      t.append(el('span', 'badge b-IDLE', s.task.status || '?'));
      if (s.task.summary && s.task.summary !== s.task.title) { t.append(el('br')); t.append(el('span', 'muted', s.task.summary)); }
      card.append(t);
    } else if (s.last_input_at) {
      const a = el('div', 'ask');
      a.append(el('span', 'lbl', 'direct send · ' + age(now - s.last_input_at) + ' trước'));
      a.append(el('span', null, s.last_input_preview || '(nội dung ẩn)'));
      card.append(a);
    }
    if (s.blocker) { const b = el('div', 'blocker'); b.append(el('span', 'lbl', s.input_required ? 'cần input' : 'blocker')); b.append(s.blocker); card.append(b); }
    if (s.completion) {
      const d = el('div', 'done'); d.append(el('span', 'lbl', 'kết quả · ' + (s.completion.status || '') + (s.completion.at ? ' · ' + clock(s.completion.at) : '')));
      if (s.completion.summary) d.append(s.completion.summary); card.append(d);
    }
    if (s.error) card.append(el('div', 'blocker', 'Không đọc được: ' + s.error));

    const isOpen = expanded.has(s.key);
    const lines = (isOpen && s.tail_full) ? s.tail_full : (s.lines || []);
    if (lines.length || s.readable) {
      const pre = el('pre', 'tail' + (isOpen ? ' full' : ''), lines.join('\n') || '(trống)');
      card.append(pre);
      requestAnimationFrame(() => { pre.scrollTop = pre.scrollHeight; });
    }
    const act = el('div', 'actions');
    if (s.readable && s.state !== 'OFFLINE') {
      const btn = el('button', 'btn', isOpen ? '▴ Thu gọn tail' : '▾ Mở rộng tail');
      btn.onclick = () => { if (expanded.has(s.key)) expanded.delete(s.key); else expanded.add(s.key);
        const c = cards.get(s.key); if (c) c.token = null; poll(true); };
      act.append(btn);
    }
    const open = el('a', 'btn', '↗ Mở session');
    open.href = `/dashboard#${encodeURIComponent(s.qualified || s.session)}`;
    act.append(open);
    card.append(act);
    return card;
  }
  function render() {
    if (!data) return;
    const now = data.generated_at || Date.now() / 1000;
    const sessions = data.sessions || [];
    const c = data.counts || {};
    $('c-active').textContent = c.active || 0;
    $('c-recent').textContent = sessions.filter(s => s.recent || s.is_new).length;
    $('c-idle').textContent = c.idle || 0;
    $('c-all').textContent = c.total || 0;
    const nodes = [...new Map(sessions.map(s => [s.node_id, s.node_name || s.node_id])).entries()];
    const sel = $('node'), cur = sel.value;
    if (sel.options.length - 1 !== nodes.length) {
      sel.length = 1;
      for (const [id, name] of nodes) { const o = el('option', null, name); o.value = id; sel.append(o); }
      sel.value = cur;
    }
    const banners = $('banners'); banners.textContent = '';
    for (const n of data.unreachable_nodes || [])
      banners.append(el('div', 'banner warn', `Node ${n.node_name || n.node_id} không phản hồi (${n.status}) — session của node này tạm không hiển thị.`));
    for (const e of data.source_errors || []) banners.append(el('div', 'banner warn', 'Nguồn dữ liệu lỗi: ' + e));
    if (data.error) banners.append(el('div', 'banner warn', data.error + ': ' + (data.detail || '')));

    const list = $('list');
    const visible = sessions.filter(matches);
    const keep = new Set(visible.map(s => s.key));
    for (const [key, c2] of cards) if (!keep.has(key)) { c2.el.remove(); cards.delete(key); }
    let prev = null;
    for (const s of visible) {
      let entry = cards.get(s.key);
      if (!entry || entry.token !== s.change_token) {
        const node = build(s, now);
        if (!first && !known.has(s.key)) node.classList.add('flash');
        if (entry) entry.el.replaceWith(node);
        entry = {el: node, token: s.change_token};
        cards.set(s.key, entry);
      }
      const want = prev ? prev.nextSibling : list.firstChild;
      if (want !== entry.el) list.insertBefore(entry.el, want);
      prev = entry.el;
    }
    for (const s of sessions) known.add(s.key);
    $('empty').hidden = visible.length > 0;
    first = false;
  }
  async function poll(force) {
    if (inflight) return;
    if (document.hidden && !force) return;
    inflight = true;
    try {
      const params = new URLSearchParams();
      if (expanded.size) params.set('expand', [...expanded].join(','));
      const res = await fetch(API + (params.toString() ? '?' + params : ''), {credentials: 'same-origin', cache: 'no-store'});
      if (!res.ok) throw new Error('HTTP ' + res.status);
      data = await res.json();
      lastOk = Date.now();
      const p = $('pulse'); p.classList.remove('tick', 'stale'); void p.offsetWidth; p.classList.add('tick');
      render();
    } catch (err) {
      $('pulse').classList.add('stale');
      $('updated').textContent = 'mất kết nối: ' + err.message;
    } finally { inflight = false; }
  }
  setInterval(() => {
    if (lastOk) $('updated').textContent = 'cập nhật ' + age((Date.now() - lastOk) / 1000) + ' trước';
    if (lastOk && Date.now() - lastOk > 8000) $('pulse').classList.add('stale');
  }, 1000);
  for (const b of document.querySelectorAll('#filters .chip')) {
    b.classList.toggle('on', b.dataset.f === filter);
    b.onclick = () => { filter = b.dataset.f; try { localStorage.setItem('tmcp-live-filter', filter); } catch (e) {}
      for (const x of document.querySelectorAll('#filters .chip')) x.classList.toggle('on', x === b); render(); };
  }
  $('q').oninput = render; $('node').onchange = render;
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(true); });
  poll(true);
  setInterval(() => poll(false), POLL_MS);
})();
</script>
</body>
</html>
"""
