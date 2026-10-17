"""Disposable loopback staging; no production configuration or services touched.
Run with the repository interpreter: python scripts/stage_unified_memory.py
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def worker(node, database, fd):
    import uvicorn
    from terminal_mcp.config import AppConfig, PermissionsConfig
    from terminal_mcp.core import TerminalService
    from terminal_mcp.node_agent import build_node_agent
    from terminal_mcp.memory_sync import MemoryEndpoint
    from terminal_mcp.unified_memory import MemoryStore
    store=MemoryStore(database,node)
    app=build_node_agent(node_id=node,terminal=TerminalService(AppConfig(permissions=PermissionsConfig(False,False),allowed_session_patterns=())),token='disposable-staging-only',memory=MemoryEndpoint(store,'hub',{'demo'}))
    uvicorn.run(app,fd=int(fd),log_level='error')


def stage():
    from terminal_mcp.node_client import RemoteNodeClient
    from terminal_mcp.unified_memory import MemoryStore,Principal
    from terminal_mcp.memory_sync import synchronize
    import httpx
    processes=[];sockets=[]
    with tempfile.TemporaryDirectory(prefix='tmcp-memory-staging-') as directory:
        base=Path(directory)
        try:
            actor=Principal('a',frozenset({'writer','reader'}),frozenset({'demo'}))
            a=MemoryStore(base/'a.db','a')
            for i in range(401):a.publish(actor,'note',str(i),'demo',{'title':f'checkpoint {i}'},visibility='shared')
            a.publish(actor,'note','private','demo',{'title':'private marker'})
            a.publish(actor,'note','secret','demo',{'title':'filtered','body':'Authorization: Bearer '+'z'*40},visibility='shared')
            a.close()
            clients={}
            for node in ('a','b'):
                sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen();sockets.append(sock)
                home=base/node;home.mkdir()
                env={k:v for k,v in os.environ.items() if not k.startswith(('TERMINAL_MCP_','TMCP_'))}
                env.update(HOME=str(home),XDG_STATE_HOME=str(home/'state'),XDG_CONFIG_HOME=str(home/'config'),PYTHONPATH=str(ROOT))
                proc=subprocess.Popen([sys.executable,__file__,'worker',node,str(base/(node+'.db')),str(sock.fileno())],env=env,pass_fds=(sock.fileno(),),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                processes.append(proc)
                url=f'http://127.0.0.1:{sock.getsockname()[1]}'
                clients[node]=RemoteNodeClient(url,'disposable-staging-only')
                for _ in range(100):
                    try:
                        clients[node].memory_request('join',{'version':1,'node':'hub'});break
                    except Exception:
                        if proc.poll() is not None:raise RuntimeError('staging worker exited')
                        time.sleep(.05)
                else:raise RuntimeError('staging startup timed out')
                assert httpx.post(url+'/v1/memory/join',json={'version':1,'node':'hub'}).status_code==401
            hub=MemoryStore(base/'hub.db','hub')
            transport=lambda peer,action,body:clients[peer].memory_request(action,body)
            for _ in range(4):synchronize(hub,'a',{'demo'},transport,max_pages=1)
            synchronize(hub,'b',{'demo'},transport)
            result=clients['b'].memory_request('search',{'query':'checkpoint','limit':50})
            assert len(result['results'])==50
            assert not clients['b'].memory_request('search',{'query':'private marker'})['results']
            assert 'z'*40 not in json.dumps(clients['b'].memory_request('search',{'query':'filtered'}))
            hub.close();hub=MemoryStore(base/'hub.db','hub')
            assert synchronize(hub,'a',{'demo'},transport)['pulled']==0
            processes[1].terminate();processes[1].wait(timeout=10)
            b=MemoryStore(base/'b.db','b');reader=Principal('b',frozenset({'reader'}),frozenset({'demo'}))
            assert len(b.search(reader,'checkpoint',limit=5000))==401
            b.close()
            # Owner publishes withdrawal while process stays running; WAL transactions synchronize safely.
            a=MemoryStore(base/'a.db','a');a.publish(actor,'note','0','demo',{},visibility='private');a.close()
            synchronize(hub,'a',{'demo'},transport)
            assert len(hub.search(Principal('hub',frozenset({'reader'}),frozenset({'demo'})),'checkpoint',limit=5000))==400
            hub.close()
            print(json.dumps({'state':'PASS','transport':'real loopback HTTP','nodes':3,'shared_records':402,'checks':['authenticated join','multi-page progress','controller relay','private default','redaction','durable restart','offline cache','withdrawal'],'production_touched':False}))
        finally:
            for process in processes:
                if process.poll() is None:process.terminate();process.wait(timeout=10)
            for sock in sockets:sock.close()


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='worker':worker(*sys.argv[2:])
    else:stage()
