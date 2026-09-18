"""Read-only NovaRetail dispatch watchdog dashboard helpers."""
from __future__ import annotations
import json, subprocess
from pathlib import Path
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

STATE=Path.home()/".local/state/novaretail/dispatch_watchdog.jsonl"
WORKERS=("codex1","codex2","codex-work","ake-claude-coder-01")
def events(path=STATE, limit=100):
    rows=[]
    try: lines=path.read_text(encoding="utf-8").splitlines()[-limit:]
    except FileNotFoundError: return []
    for line in lines:
        try:
            item=json.loads(line)
            if isinstance(item,dict): rows.append({k:item.get(k) for k in ("timestamp","selected_session","result","submission_id","correlation_id","submit_confirmed","execution_started","waiting_approval","attempted_sessions")})
        except json.JSONDecodeError: continue
    return list(reversed(rows))
def timer():
    try:
        p=subprocess.run(["systemctl","--user","show","novaretail-dispatch-watchdog.timer","-p","ActiveState","-p","NextElapseUSecRealtime"],capture_output=True,text=True,timeout=3)
        return dict(x.split("=",1) for x in p.stdout.splitlines() if "=" in x)
    except Exception:return {"ActiveState":"unknown"}
HTML='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>NovaRetail Dispatch Ops</title><style>body{font:14px system-ui;background:#0b1020;color:#eef2ff;margin:0}.w{max-width:1100px;margin:auto;padding:16px}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}.c,.row{border:1px solid #26324b;border-radius:9px;padding:10px;background:#121a2d}.warn{border-color:#ffc857}.err{border-color:#ff6b6b}button,select{padding:8px;background:#5b8cff;border:0;border-radius:6px;color:#07101f}.rows{display:grid;gap:7px}.muted{color:#9aa7bd;font-size:12px}@media(max-width:600px){.w{padding:10px}}</style><main class=w><h1>NovaRetail Dispatch Watchdog</h1><div id=h class=g></div><p><button id=run>Run dispatch cycle now</button> <button onclick="load()">Refresh</button></p><h2>Workers</h2><div id=w class=g></div><h2>Submission reliability</h2><div id=r class=g></div><p><select id=fr><option value="">All results</option></select><select id=fw><option value="">All workers</option></select></p><h2>Timeline</h2><div id=e class=rows></div><script>const esc=x=>String(x??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));let data={events:[]};function short(x){return x?String(x).slice(0,12):'—'}function render(){let l=data.events[0]||{},t=data.timer||{};h.innerHTML=[`Watchdog: ${t.ActiveState||'unknown'}`,`Next: ${t.NextElapseUSecRealtime||'unavailable'}`,`Last: ${l.result||'none'}`,`Selected: ${l.selected_session||'none'}`].map(v=>`<div class=c>${esc(v)}</div>`).join('');w.innerHTML=data.workers.map(x=>`<div class=c><b>${esc(x.session)}</b><div>${esc(x.state)}</div><small>${esc(x.node)} · eligible ${x.eligible}</small></div>`).join('');r.innerHTML=[`confirmed ${!!l.submit_confirmed}`,`started ${!!l.execution_started}`,`approval ${!!l.waiting_approval}`,`submission ${short(l.submission_id)} · correlation ${short(l.correlation_id)}`].map(x=>`<div class=c>${esc(x)}</div>`).join('');let rows=data.events.filter(x=>(!fr.value||x.result===fr.value)&&(!fw.value||x.selected_session===fw.value));e.innerHTML=rows.map(x=>`<div class="row ${x.waiting_approval?'warn':''}"><b>${esc(x.result)}</b> · ${esc(x.selected_session||'none')}<div class=muted>${esc(x.timestamp)} · enter ${esc(x.attempted_sessions?.[0]?.submit?.evidence?.enter_count??'—')} · started ${!!x.execution_started}</div></div>`).join('')||'<div class=row>No events</div>'}async function load(){data=await fetch('/dashboard/api/ops/novaretail-dispatch').then(r=>r.json());for(const x of data.events){if(![...fr.options].some(o=>o.value===x.result))fr.add(new Option(x.result,x.result));if(x.selected_session&&! [...fw.options].some(o=>o.value===x.selected_session))fw.add(new Option(x.selected_session,x.selected_session))}render()}fr.onchange=fw.onchange=render;run.onclick=async()=>{await fetch('/dashboard/api/ops/novaretail-dispatch/run',{method:'POST',headers:{'X-Requested-With':'dashboard'}});setTimeout(load,600)};load()</script></main>'''
def register(server, read_guard, mutation_guard, run_service=None):
 run_service=run_service or (lambda: subprocess.run(['systemctl','--user','start','novaretail-dispatch-watchdog.service'],capture_output=True,text=True,timeout=5).returncode==0)
 @server.custom_route('/ops/novaretail-dispatch',methods=['GET'],include_in_schema=False)
 async def page(request:Request):
  blocked,_=read_guard(request); return blocked or HTMLResponse(HTML)
 @server.custom_route('/dashboard/api/ops/novaretail-dispatch',methods=['GET'],include_in_schema=False)
 async def api(request:Request):
  blocked,_=read_guard(request)
  return blocked or JSONResponse({'timer':timer(),'events':events(),'workers':[{'session':w,'state':'UNKNOWN','eligible':False,'input_required':None,'node':'unavailable','last_activity':None,'task':None} for w in WORKERS]})
 @server.custom_route('/dashboard/api/ops/novaretail-dispatch/run',methods=['POST'],include_in_schema=False)
 async def run(request:Request):
  blocked,_=mutation_guard(request)
  if blocked:return blocked
  ok=run_service(); return JSONResponse({'started':bool(ok),'status':'requested' if ok else 'failed'})
