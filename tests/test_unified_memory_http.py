import pytest
from starlette.testclient import TestClient
from terminal_mcp.config import AppConfig, PermissionsConfig
from terminal_mcp.core import TerminalService
from terminal_mcp.node_agent import build_node_agent
from terminal_mcp.unified_memory import MemoryStore, Principal
from terminal_mcp.memory_sync import MemoryEndpoint, synchronize


def test_three_nodes_authenticated_join_delta_restart_and_offline(tmp_path):
    stores={n:MemoryStore(tmp_path/(n+'.db'),n) for n in ('hub','a','b')}
    clients={}
    for n in ('a','b'):
        endpoint=MemoryEndpoint(stores[n],controller='hub',projects={'p'})
        app=build_node_agent(node_id=n,terminal=TerminalService(AppConfig(permissions=PermissionsConfig(False,False),allowed_session_patterns=())),token='test-only-token',memory=endpoint)
        clients[n]=TestClient(app)
        assert clients[n].post('/v1/memory/join',json={'version':1,'node':'hub'}).status_code==401
        assert clients[n].post('/v1/memory/join',headers={'Authorization':'Bearer test-only-token'},json={'version':999,'node':'hub'}).status_code==400
    def transport(node,action,body):
        r=clients[node].post('/v1/memory/'+action,headers={'Authorization':'Bearer test-only-token'},json=body)
        assert r.status_code==200,r.text
        return r.json()
    actor=Principal('a',frozenset({'writer','reader'}),frozenset({'p'}))
    stores['a'].publish(actor,'note','x','p',{'title':'searchable checkpoint'},visibility='shared')
    stores['a'].publish(actor,'note','hidden','p',{'title':'private-only'})
    synchronize(stores['hub'],'a',{'p'},transport)
    synchronize(stores['hub'],'b',{'p'},transport)
    reader=Principal('b',frozenset({'reader'}),frozenset({'p'}))
    assert stores['b'].search(reader,'checkpoint')
    assert not stores['b'].search(reader,'private-only')
    assert synchronize(stores['hub'],'b',{'p'},transport)['pushed']==0
    stores['hub'].close();stores['hub']=MemoryStore(tmp_path/'hub.db','hub')
    assert synchronize(stores['hub'],'a',{'p'},transport)['pulled']==0
    assert stores['b'].search(reader,'checkpoint') # offline local cache, no network
    assert clients['b'].post('/v1/memory/search',headers={'Authorization':'Bearer test-only-token'},json={'query':'checkpoint','projects':['other'],'roles':['admin']}).status_code==400
    for s in stores.values():s.close()


def test_memory_endpoint_opt_in(tmp_path):
    app=build_node_agent(node_id='a',terminal=TerminalService(AppConfig(permissions=PermissionsConfig(False,False),allowed_session_patterns=())),token='test')
    assert TestClient(app).post('/v1/memory/join',headers={'Authorization':'Bearer test'},json={}).status_code==404


def test_endpoint_search_sees_shared_owned_records_not_private(tmp_path):
    s=MemoryStore(tmp_path/'a.db','a')
    actor=Principal('a',frozenset({'reader','writer'}),frozenset({'p'}))
    s.publish(actor,'note','shared','p',{'title':'needle shared'},visibility='shared')
    s.publish(actor,'note','private','p',{'title':'needle private'})
    endpoint=MemoryEndpoint(s,'hub',{'p'})
    result=endpoint.call('search',{'query':'needle'})['results']
    assert len(result)==1 and result[0]['visibility']=='shared'


def test_bounded_snapshot_progress_and_owner_rebuild(tmp_path):
    hub=MemoryStore(tmp_path/'hub.db','hub');a=MemoryStore(tmp_path/'a.db','a')
    actor=Principal('a',frozenset({'writer','reader'}),frozenset({'p'}))
    for i in range(201):a.publish(actor,'note',str(i),'p',{'title':'needle '+str(i)},visibility='shared')
    endpoint=MemoryEndpoint(a,'hub',{'p'})
    transport=lambda peer,action,body:endpoint.call(action,body)
    for _ in range(3):synchronize(hub,'a',{'p'},transport,max_pages=1)
    reader=Principal('hub',frozenset({'reader'}),frozenset({'p'}))
    assert len(hub.search(reader,'needle',limit=5000))==201
    a.close()
    endpoint.store=MemoryStore(tmp_path/'rebuilt.db','a')
    endpoint.store.publish(actor,'note','0','p',{'title':'rebuilt needle'},visibility='shared')
    synchronize(hub,'a',{'p'},transport)
    assert len(hub.search(reader,'rebuilt'))==1
    assert len(hub.search(reader,'needle',limit=5000))==1


def test_controller_can_narrow_node_scope_before_transfer(tmp_path):
    hub=MemoryStore(tmp_path/'hub.db','hub');a=MemoryStore(tmp_path/'a.db','a')
    actor=Principal('a',frozenset({'writer'}),frozenset({'p','q'}))
    for p in ('p','q'):a.publish(actor,'note',p,p,{'title':p+' needle'},visibility='shared')
    endpoint=MemoryEndpoint(a,'hub',{'p','q'})
    synchronize(hub,'a',{'p'},lambda peer,action,body:endpoint.call(action,body))
    assert hub.db.execute('SELECT count(*) FROM documents').fetchone()[0]==1
    assert hub.db.execute('SELECT project FROM documents').fetchone()[0]=='p'
