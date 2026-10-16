import asyncio
import json
import pytest
from terminal_mcp.mcp_app import build_mcp
from terminal_mcp.browser_gateway import BrowserGateway
from terminal_mcp.config import BrowserGatewayConfig

def test_cached_browser_fields_are_in_actual_mcp_schema():
 server=build_mcp()
 tool=next(t for t in asyncio.run(server.list_tools()) if t.name=="terminal_turn")
 assert {"steps","viewport","screenshot","allow_mutations"} <= set(tool.input_schema["properties"])

def structured(value):
 if hasattr(value,"content"): return structured(value.content)
 if isinstance(value,dict): return value
 if isinstance(value,(list,tuple)):
  for x in value:
   if isinstance(x,dict): return x
   if isinstance(x,(list,tuple)):
    try: return structured(x)
    except AssertionError: pass
   if hasattr(x,"text"): return json.loads(x.text)
 raise AssertionError("missing structured result: "+repr(value)[:700])

@pytest.mark.parametrize("text,verdict",[("Ready","PASS"),("Must not exist","FAIL")])
def test_cached_assertion_and_viewport_reach_real_evaluator(monkeypatch,text,verdict):
 import terminal_mcp.mcp_app as app
 jobs=[]
 gateway=BrowserGateway(BrowserGatewayConfig(enabled=True),runner=lambda job,*a: jobs.append(job) or {"ok":True,"observation":{"status":200,"text":"Ready"}})
 def turn(self,**kw): return gateway.verify(kw["url"],**(kw.get("args") or {}))
 monkeypatch.setattr(app.CompactTerminalTools,"turn",turn)
 result=structured(asyncio.run(build_mcp().call_tool("terminal_turn",{"action":"browser_verify","url":"https://8.8.8.8","steps":[{"action":"assert_text","text":text}],"viewport":{"width":390,"height":844},"screenshot":"never"})))
 assert result["status"]==verdict
 assert result["summary"]["total"]==1
 assert jobs[0]["viewport"]=={"width":390,"height":844}

@pytest.mark.parametrize("extra",[{"steps":[{"action":"click","selector":"button"}]},{"steps":[{"action":"assert_text","text":""}]},{"viewport":{"width":True,"height":844}},{"steps":[{"action":"assert_text","text":"Ready"}],"args":{"assertions":["text contains: Different"]}},{"allow_mutations":True}])
def test_cached_bad_or_mutating_requests_fail_before_backend(monkeypatch,extra):
 import terminal_mcp.mcp_app as app
 calls=[]
 monkeypatch.setattr(app.CompactTerminalTools,"turn",lambda self,**kw:calls.append(kw) or {"status":"PASS"})
 result=structured(asyncio.run(build_mcp().call_tool("terminal_turn",{"action":"browser_verify","url":"https://8.8.8.8",**extra})))
 assert result["status"]=="ERROR"
 assert not calls


@pytest.mark.parametrize("action", ["inspect", "list_sessions", "browser_status", "create_session"])
def test_cached_default_screenshot_does_not_break_nonbrowser_actions(action):
 from terminal_mcp.browser_compat import normalize_cached_browser_args
 canonical={"probe":False} if action=="browser_status" else None
 assert normalize_cached_browser_args(action,canonical,screenshot="on_failure") is canonical

def test_cached_screenshot_action_ignores_wrapper_default_policy():
 from terminal_mcp.browser_compat import normalize_cached_browser_args
 assert normalize_cached_browser_args("browser_screenshot",None,screenshot="on_failure",viewport={"width":390,"height":844}) == {"viewport_width":390,"viewport_height":844}

def test_nondefault_browser_options_on_nonbrowser_action_are_refused():
 from terminal_mcp.browser_compat import normalize_cached_browser_args
 with pytest.raises(ValueError): normalize_cached_browser_args("inspect",None,steps=[{"action":"assert_text","text":"x"}])

def test_unmodified_canonical_args_remain_identical():
 from terminal_mcp.browser_compat import normalize_cached_browser_args
 canonical={"assertions":["text contains: Ready"],"viewport_width":390}
 assert normalize_cached_browser_args("browser_verify",canonical) is canonical
