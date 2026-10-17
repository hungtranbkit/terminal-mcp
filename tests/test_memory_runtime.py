import json
import pytest
from terminal_mcp.memory_runtime import MemoryRuntime
from terminal_mcp.unified_memory import MemoryError


def test_runtime_is_opt_in_config_bound_and_refuses_unenrolled_peer(tmp_path):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'node':'hub','controller':'hub','database':str(tmp_path/'memory.db'),'projects':{'p':{}},'peers':{'a':['p']}}))
    runtime=MemoryRuntime.load(path,node='hub')
    result=runtime.tick()
    assert result['peers'][0]['state']=='NEEDS_AUTH'
    assert not runtime.search('anything')['results']
    runtime.close()
    with pytest.raises(MemoryError):MemoryRuntime.load(path,node='other')


def test_default_private_runtime_projector(tmp_path):
    path=tmp_path/'config.json'
    path.write_text(json.dumps({'node':'a','controller':'hub','database':str(tmp_path/'memory.db'),'projects':{'p':{}}}))
    runtime=MemoryRuntime.load(path,node='a')
    runtime.tick()
    assert runtime.endpoint.call('pull',{'cursor':0})['objects']==[]
    assert runtime.status()['node']=='a'
    runtime.close()


def test_mcp_search_defaults_to_actual_terminal_instance(tmp_path,monkeypatch):
    from terminal_mcp.mcp_app import build_mcp
    from terminal_mcp.config import AppConfig,PermissionsConfig
    monkeypatch.setattr('terminal_mcp.mcp_app.load_config',lambda:AppConfig(permissions=PermissionsConfig(True,False),allowed_session_patterns=()))
    runtime=MemoryRuntime({'node':'hub','controller':'hub','database':str(tmp_path/'memory.db'),'projects':{'p':{}}})
    runtime.store.publish(runtime.actor,'note','x','p',{'title':'needle'})
    server=build_mcp(memory=runtime)
    fn=server._tool_manager._tools['terminal_memory_search'].fn
    assert len(fn('needle')['results'])==1
    assert fn('needle',project='forbidden')['error']=='BLOCKED_PERMISSION'
    runtime.close()


def test_global_read_disable_withdraws_session_even_with_valid_grant(tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from terminal_mcp.config import AppConfig,PermissionsConfig
    db=tmp_path/'sessions.db'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TABLE session_records(node_id,session_name,status,agent_type,git_branch,last_commit,repo_root,read_granted)')
        con.execute('INSERT INTO session_records VALUES (?,?,?,?,?,?,?,?)',('a','needle-session','READY','codex','main','abc',str(tmp_path),1))
    terminal=SimpleNamespace(config=AppConfig(permissions=PermissionsConfig(False,False),allowed_session_patterns=()),_read_authorized=lambda name:True)
    runtime=MemoryRuntime({'node':'a','controller':'hub','database':str(tmp_path/'memory.db'),'projects':{'p':{'roots':[str(tmp_path)]}},'session_database':str(db),'share_projects':['p']},terminal=terminal)
    runtime.tick()
    assert not runtime.search('needle')['results']
    terminal.config=AppConfig(permissions=PermissionsConfig(True,False),allowed_session_patterns=())
    runtime.tick()
    assert runtime.search('needle')['results']
    terminal.config=AppConfig(permissions=PermissionsConfig(False,False),allowed_session_patterns=())
    runtime.tick()
    assert not runtime.search('needle')['results']
