"""Read-only NovaRetail dispatch watchdog dashboard helpers."""
from __future__ import annotations
import json, subprocess
from pathlib import Path
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

STATE=Path.home()/".local/state/novaretail/dispatch_watchdog.jsonl"
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
HTML='''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>NovaRetail Dispatch Ops</title><style>body{font:14px system-ui;background:#0b1020;color:#eef2ff;margin:0}.w{max-width:1100px;margin:auto;padding:16px}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}.c{border:1px solid #26324b;border-radius:9px;padding:12px;background:#121a2d}.warn{color:#ffc857}.err{color:#ff6b6b}button{padding:8px;background:#5b8cff;border:0;border-radius:6px}pre{white-space:pre-wrap;overflow:auto}</style><main class=w><h1>NovaRetail Dispatch Watchdog</h1><div id=h class=g></div><p><button id=run>Run dispatch cycle now</button> <button onclick="load()">Refresh</button></p><h2>Timeline</h2><pre id=e></pre><script>async function load(){let x=await fetch('/dashboard/api/ops/novaretail-dispatch').then(r=>r.json());let l=x.events[0]||{};h.innerHTML=['Watchdog: '+x.timer.ActiveState,'Last result: '+(l.result||'none'),'Selected: '+(l.selected_session||'none'),'Execution started: '+!!l.execution_started,'Waiting approval: '+!!l.waiting_approval].map(v=>'<div class=c>'+v+'</div>').join('');e.textContent=JSON.stringify(x.events,null,2)}run.onclick=async()=>{await fetch('/dashboard/api/ops/novaretail-dispatch/run',{method:'POST',headers:{'X-Requested-With':'dashboard'}});setTimeout(load,600)};load()</script></main>'''
def register(server, read_guard):
 @server.custom_route('/ops/novaretail-dispatch',methods=['GET'],include_in_schema=False)
 async def page(request:Request):
  blocked,_=read_guard(request); return blocked or HTMLResponse(HTML)
 @server.custom_route('/dashboard/api/ops/novaretail-dispatch',methods=['GET'],include_in_schema=False)
 async def api(request:Request):
  blocked,_=read_guard(request); return blocked or JSONResponse({'timer':timer(),'events':events()})
 @server.custom_route('/dashboard/api/ops/novaretail-dispatch/run',methods=['POST'],include_in_schema=False)
 async def run(request:Request):
  blocked,_=read_guard(request)
  if blocked:return blocked
  p=subprocess.run(['systemctl','--user','start','novaretail-dispatch-watchdog.service'],capture_output=True,text=True,timeout=5)
  return JSONResponse({'started':p.returncode==0,'status':'requested' if p.returncode==0 else 'failed'})
