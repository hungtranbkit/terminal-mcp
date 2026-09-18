import json
from starlette.testclient import TestClient
from starlette.responses import JSONResponse
from terminal_mcp.novaretail_dispatch_dashboard import events,timer,register
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.core import TerminalService
from terminal_mcp.config import AppConfig,PermissionsConfig
def test_events_missing_malformed_and_limit(tmp_path):
 p=tmp_path/'x';assert events(p)==[];p.write_text('{bad}\n'+ '\n'.join(json.dumps({'timestamp':str(i),'result':'OK'}) for i in range(105)));assert len(events(p,100))==100
def test_timer_inactive(monkeypatch):
 import terminal_mcp.novaretail_dispatch_dashboard as m
 monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:type('P',(),{'stdout':'ActiveState=inactive\n'})());assert timer()['ActiveState']=='inactive'
def test_routes_guard_and_fixed_runner():
 s=build_mcp(TerminalService(AppConfig(PermissionsConfig(True,True),()))); calls=[];register(s,lambda r:(None,None),lambda r:(JSONResponse({'denied':1},status_code=403),None),lambda:calls.append(1) or True);c=TestClient(s.streamable_http_app());assert c.get('/ops/novaretail-dispatch').status_code==200;assert c.get('/dashboard/api/ops/novaretail-dispatch').status_code==200;assert c.post('/dashboard/api/ops/novaretail-dispatch/run').status_code==403;assert not calls
